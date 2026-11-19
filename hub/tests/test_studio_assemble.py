"""긴 영상 합치기(studio/assemble.py) 시험 (docs/long-video-design.md 7.3).
명령을 만드는 함수는 순수 함수라 ffmpeg 없이 돈다. ffmpeg 가 있는 곳(허브 이미지)에서는 짧은 시험 클립을 실제로 합쳐 본다.

  cd hub && python tests/test_studio_assemble.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.studio import assemble as A  # noqa: E402

MEAS = {"input_i": -19.2, "input_tp": -3.1, "input_lra": 4.0, "input_thresh": -29.5, "target_offset": 0.2}


def t_concat_list_quotes_paths_and_pins_each_clip_length():
    assert A.concat_list(["/r/a.mp4", "/r/it's.mp4"]) == "file '/r/a.mp4'\nfile '/r/it'\\''s.mp4'\n"
    # 영상 길이를 적어 두면 소리가 영상보다 긴 클립이 있어도 다음 장면이 제때 시작한다 (11-17 검토)
    assert A.concat_list(["/r/a.mp4", "/r/b.mp4"], [10.04, 8.0]) == \
        "file '/r/a.mp4'\nduration 10.040000\nfile '/r/b.mp4'\nduration 8.000000\n"


def t_gain_is_capped_and_silence_is_left_alone():
    assert A.gain_db({"input_i": -30.0}, -20) == 10.0
    assert A.gain_db({"input_i": -40.0}, -20) == 12.0          # 올림은 +12dB 까지 (잡음이 커지지 않게)
    assert A.gain_db({"input_i": 0.0}, -20) == -20.0
    assert A.gain_db({"input_i": 10.0}, -20) == -24.0          # 내림은 -24dB 까지
    assert A.gain_db({"input_i": -70.0}, -20) == 0.0           # 거의 무음인 장면은 올리지 않는다
    assert A.gain_db({"input_i": float("-inf")}, -20) == 0.0 and A.gain_db(None, -20) == 0.0


def t_parse_loudnorm_reads_the_last_json_block():
    err = ('frame=1\n[Parsed_loudnorm_0 @ 0x1]\n{\n\t"input_i" : "-23.51",\n\t"input_tp" : "-5.02",\n\t"input_lra" : "3.10",\n'
           '\t"input_thresh" : "-33.80",\n\t"output_i" : "-14.00",\n\t"target_offset" : "0.12"\n}\n')
    m = A.parse_loudnorm(err)
    assert m["input_i"] == -23.51 and m["input_tp"] == -5.02 and m["target_offset"] == 0.12
    assert A.parse_loudnorm("no json here") is None
    assert A.parse_loudnorm(err.replace('"-23.51"', '"-inf"'))["input_i"] == float("-inf")


def t_mix_filter_levels_scenes_and_lays_one_music_track():
    f = A.build_mix_filter([10.04, 8.0], [True, False], [3.5, 0.0], -8.0)
    assert "[0:a]aresample=48000" in f and "apad,atrim=0:10.040" in f and "volume=3.50dB" in f
    assert "anullsrc=r=48000:cl=stereo,atrim=0:8.000" in f            # 소리 없는 장면은 같은 길이의 무음으로 자리를 채운다
    assert "afade=t=in:st=0:d=0.08" in f and "afade=t=out:st=9.960:d=0.08" in f    # 장면 경계의 짧은 페이드
    assert "concat=n=2:v=0:a=1[sfx]" in f
    assert "[2:a]aresample=48000" in f and "atrim=0:18.040" in f and "volume=-8.00dB" in f     # 음악은 전체 길이 한 곡
    assert "afade=t=in:st=0:d=1.5" in f and "afade=t=out:st=15.040:d=3.0" in f
    assert f.endswith("[sfx][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]")
    f2 = A.build_mix_filter([5.0], [True], [0.0], None)
    assert f2.endswith("concat=n=1:v=0:a=1[mix]") and "[1:a]" not in f2 and "[mus]" not in f2


def t_mix_and_final_commands():
    cmd = A.build_mix_cmd(["a.mp4", "b.mp4"], "m.mp3", "FILTER", "/t/mix.wav")
    assert cmd[:4] == ["ffmpeg", "-hide_banner", "-nostats", "-y"] and cmd.count("-i") == 3 and cmd[-1] == "/t/mix.wav"
    assert cmd[cmd.index("-filter_complex") + 1] == "FILTER" and cmd[cmd.index("-map") + 1] == "[mix]"
    assert cmd[cmd.index("-c:a") + 1] == "pcm_f32le"          # 중간 믹스는 실수형: 0dBFS 를 넘는 피크가 잘리지 않는다
    assert A.build_mix_cmd(["a.mp4"], None, "F", "/t/m.wav").count("-i") == 1
    fin = A.build_final_cmd("/t/list.txt", "/t/mix.wav", "/r/out.mp4", True, MEAS)
    assert fin[fin.index("-f") + 1] == "concat" and fin[fin.index("-c:v") + 1] == "copy" and "-shortest" in fin
    af = fin[fin.index("-af") + 1]
    assert "I=-14.0" in af and "measured_I=-19.20" in af and "offset=0.20" in af and "linear=true" in af
    assert af.endswith("aresample=48000") and fin[-1] == "/r/out.mp4" and "+faststart" in fin
    assert fin[fin.index("-c:a") + 1] == "aac" and fin[fin.index("-map", fin.index("-map") + 1) + 1] == "1:a:0"
    silent = A.build_final_cmd("/t/list.txt", None, "/r/out.mp4", False, None)
    assert "-an" in silent and "libx264" in silent and "-af" not in silent       # 코덱 값이 다르면 다시 인코딩
    assert "linear=true" not in A.final_audio_filter({"input_i": float("-inf")})   # 측정에 실패하면 한 번에(dynamic)
    assert "linear=true" not in A.final_audio_filter(None)


def t_transitions_are_clamped_and_total_shrinks_by_overlaps():
    assert A.fit_transitions([10, 10, 10], None) == [0.0, 0.0] and A.fit_transitions([10, 10, 10], [0.6]) == [0.0, 0.0]   # 길이가 안 맞으면 전부 컷
    assert A.fit_transitions([10, 10, 10], [0.6, 0]) == [0.6, 0.0]
    assert A.fit_transitions([10, 1.0], [0.6]) == [0.45]                  # 짧은 쪽 절반을 넘지 않는다
    assert A.fit_transitions([10, 0.2], [0.6]) == [0.0] and A.fit_transitions([10, 10], ["x"]) == [0.0]
    assert abs(A.timeline_total([10, 10, 10], [0.6, 0.0]) - 29.4) < 1e-9


def t_video_filter_mixes_dissolves_and_cuts():
    f = A.build_video_filter([10, 10, 10], [0.6, 0.0], 24)
    assert f.count("fps=24,settb=AVTB,setsar=1,format=yuv420p") == 3
    assert "[v0][v1]xfade=transition=fade:duration=0.600:offset=9.400[x1]" in f
    assert f.endswith("[x1][v2]concat=n=2:v=1:a=0[v]")                    # 컷은 그대로 잇는다
    f2 = A.build_video_filter([10, 10, 10], [0.6, 0.6], 24)
    assert "[x1][v2]xfade=transition=fade:duration=0.600:offset=18.800[v]" in f2    # 앞 디졸브로 줄어든 길이 기준
    assert A.build_video_filter([5], [], 24).endswith("[v0]null[v]")


def t_mix_filter_crossfades_at_dissolves_and_keeps_edge_fades_at_cuts():
    f = A.build_mix_filter([10, 10, 10], [True, True, True], [0, 0, 0], None, [0.6, 0.0])
    assert "[a0][a1]acrossfade=d=0.600:c1=tri:c2=tri[y1]" in f and f.endswith("[y1][a2]concat=n=2:v=0:a=1[mix]")
    a0 = [p for p in f.split(";") if p.endswith("[a0]")][0]
    a1 = [p for p in f.split(";") if p.endswith("[a1]")][0]
    assert "afade=t=in" in a0 and "afade=t=out" not in a0                  # 디졸브 쪽 가장자리에는 페이드를 넣지 않는다
    assert "afade=t=in" not in a1 and "afade=t=out" in a1                  # 앞이 디졸브, 뒤가 컷
    fm = A.build_mix_filter([10, 10], [True, True], [0, 0], -8.0, [0.6])
    assert "atrim=0:19.400" in fm and "[y" not in fm.split("[sfx]")[0].split(";")[-1] or "[sfx]" in fm       # 음악은 겹침을 뺀 전체 길이
    assert A.build_mix_filter([10, 10], [True, True], [0, 0], None, [0.0]) == A.build_mix_filter([10, 10], [True, True], [0, 0], None)


def t_xfade_command_reencodes_and_uses_mix_audio():
    cmd = A.build_xfade_cmd(["a.mp4", "b.mp4"], "/t/mix.wav", "/r/o.mp4", "VF", MEAS)
    assert cmd.count("-i") == 3 and cmd[cmd.index("-filter_complex") + 1] == "VF" and cmd[cmd.index("-map") + 1] == "[v]"
    assert "libx264" in cmd and "2:a:0" in cmd and "-shortest" in cmd and cmd[-1] == "/r/o.mp4"
    assert "-an" in A.build_xfade_cmd(["a.mp4"], None, "/r/o.mp4", "VF", None)


def t_dialogue_starts_overlap_each_cut():
    starts, total = A.dialogue_starts([6.04, 5.04, 5.04], 0.5)
    assert starts == [0.0, 5.54, 10.08] and abs(total - 15.12) < 1e-6
    assert A.dialogue_starts([6.0, 5.0], [0.0]) == ([0.0, 6.0], 11.0)            # 이음새마다 겹침 목록도 받는다 (0 이면 컷)
    assert A.dialogue_starts([4.0], 0.5) == ([0.0], 4.0) and A.dialogue_starts([], 0.5) == ([], 0.0)


def t_dialogue_audio_filter_places_lines_and_ducks_music():
    starts = [0.0, 5.54, 10.08]
    lines = [{"shot": 0}, {"shot": 1, "delay": 0.1}, {"shot": 2, "gain_db": -3}]
    f = A.build_dialogue_audio_filter(3, lines, starts, 0.2, 15.12, True, -14.0)
    assert "[3:a]aresample=48000" in f and "adelay=200|200" in f                 # 첫 대사는 샷 시작 + 0.2초
    assert "[4:a]" in f and "adelay=5840|5840" in f                              # 5.54 + 0.2 + 0.1
    assert "[5:a]" in f and "volume=-3.00dB[l2]" in f and "adelay=10280|10280" in f
    assert "[6:a]aresample=48000" in f and "volume=-14.00dB" in f and "atrim=0:15.120" in f   # 음악은 마지막 입력
    assert "afade=t=in:st=0:d=1.5" in f and "afade=t=out:st=13.120:d=2.0" in f
    assert "amix=inputs=4:duration=longest:dropout_transition=0:normalize=0" in f
    assert "loudnorm=I=-14.0:TP=-1.5:LRA=11" in f and f.endswith("[a]")
    nomusic = A.build_dialogue_audio_filter(2, [{"shot": 0}], [0.0, 5.0], 0.2, 9.5, False, -14.0)
    assert "[mus]" not in nomusic and "amix=inputs=1" in nomusic and "[2:a]" in nomusic


def t_dialogue_command_maps_video_and_mixed_audio():
    cmd = A.build_dialogue_cmd(["a.mp4", "b.mp4"], ["l0.mp3", "l1.mp3"], "m.mp3", "/r/o.mp4", "VF", "AF")
    assert cmd.count("-i") == 5 and cmd[cmd.index("-filter_complex") + 1] == "VF;AF"
    assert cmd[cmd.index("-map") + 1] == "[v]" and "[a]" in cmd and "libx264" in cmd and cmd[-1] == "/r/o.mp4"
    assert A.build_dialogue_cmd(["a.mp4"], [], None, "/r/o.mp4", "VF", "AF").count("-i") == 1


def t_trims_and_subtitle_filters():
    f = A.build_video_filter([3.0, 5.0], [0.5], 24.0, [3.0, None])
    assert "[0:v]trim=0:3.000,setpts=PTS-STARTPTS,fps=24" in f and "[1:v]fps=24" in f
    sf = A.build_subtitle_filter(4, [(1.0, 2.5), (6.0, 8.0)])
    assert "[4:v]format=rgba[sp0]" in sf and "enable='between(t,1.000,2.500)'" in sf and sf.endswith("[vs]")
    assert "[5:v]format=rgba[sp1]" in sf and "[v][sp0]overlay" in sf and "[sb0][sp1]overlay" in sf
    cmd = A.build_dialogue_cmd(["a.mp4"], ["l.mp3"], "m.mp3", "o.mp4", "VF", "AF", ["s.png"], "[vs]")
    assert cmd.count("-i") == 4 and cmd[cmd.index("-map") + 1] == "[vs]"


def t_run_dialogue_refuses_bad_input():
    if not A.available():
        return
    for lines, msg in (([{"file": "/nope.mp3", "shot": 0}], "없습니다"), ([{"file": __file__, "shot": 5}], "범위 밖")):
        try:
            A.run_dialogue(["x.mp4"], lines, None, "o.mp4")
        except A.AssembleError as e:
            assert msg in str(e), str(e)
        else:
            raise AssertionError("나쁜 입력으로 합치기가 돌았다")


def t_real_dialogue_join_when_ffmpeg_is_present():
    """ffmpeg 가 있는 곳에서 샷 3개(소리 없는 영상) + 대사 3개 + 음악을 실제로 합쳐 길이·소리를 본다"""
    if not A.available():
        print("  (ffmpeg 가 없어 실제 대사 합치기는 건너뜀)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        def clip(name, sec, src):
            path = os.path.join(tmp, name)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"{src}=size=320x176:rate=24:duration={sec}", "-c:v", "libx264", "-pix_fmt", "yuv420p", path], check=True)
            return path

        def tone(name, sec, hz):
            path = os.path.join(tmp, name)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"sine=frequency={hz}:duration={sec}:sample_rate=48000", path], check=True)
            return path
        clips = [clip("a.mp4", 4, "testsrc"), clip("b.mp4", 3, "testsrc2"), clip("c.mp4", 3, "smptebars")]
        lines = [{"file": tone("l0.mp3", 2.5, 440), "shot": 0}, {"file": tone("l1.mp3", 2.0, 330), "shot": 1},
                 {"file": tone("l2.mp3", 2.0, 550), "shot": 2}]
        music = tone("m.mp3", 12, 220)
        out = os.path.join(tmp, "o", "dialogue.mp4")
        info = A.run_dialogue(clips, lines, music, out)
        got = A.probe(out)
        assert abs(got["duration"] - 9.0) < 0.3 and got["audio"] and info["shots"] == 3 and info["lines"] == 3   # 10 - 2*0.5
        assert info["overlaps"] == [0.5, 0.5] and not os.path.exists(out + ".part.mp4")
        info2 = A.run_dialogue(clips, lines, None, os.path.join(tmp, "o", "nomusic.mp4"), overlap=0.0)
        assert abs(A.probe(os.path.join(tmp, "o", "nomusic.mp4"))["duration"] - 10.0) < 0.3 and info2["music"] is False


def t_voice_trim_command_and_subtitle_wrapping():
    """대사 소리: 앞뒤 무음과 작은 숨·잡음 꼬리를 자르고 끝에 0.3초 무음 (11-19 숲길: 10초 대사 뒤 22초 잡음 꼬리)"""
    cmd = A.build_voice_cmd("in.mp3", "out.wav")
    af = cmd[cmd.index("-af") + 1]
    assert af.count("silenceremove=start_periods=1:start_threshold=-50dB") == 2 and af.count("areverse") == 2
    assert af.endswith("apad=pad_dur=0.30") and cmd[-1] == "out.wav" and "pcm_s16le" in cmd
    # 자막 줄바꿈: 띄어쓰기에서 나누고, 긴 낱말은 글자 단위로, 세 줄을 넘으면 마지막 줄에 모은다
    fits = lambda t: len(t) <= 10      # noqa: E731
    assert A.wrap_subtitle("아픔은 없애는 게 아니란다. 함께 걸어가는 법을", fits) == ["아픔은 없애는 게", "아니란다. 함께", "걸어가는 법을"]
    assert A.wrap_subtitle("가" * 25, fits) == ["가" * 10, "가" * 10, "가" * 5]
    assert len(A.wrap_subtitle("하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘", lambda t: len(t) <= 6)) == 3
    assert A.wrap_subtitle("  ", fits) == []


def t_subtitle_png_is_transparent_when_pillow_and_font_exist():
    """ComfyUI 로 저장한 PNG 는 투명도가 빠져 자막 구간이 검게 덮였다 (11-19). 허브가 RGBA 로 바로 그린다"""
    try:
        from PIL import Image
    except ImportError:
        print("  (Pillow 가 없어 자막 그림 시험은 건너뜀)")
        return
    if not A.subtitle_font():
        print("  (한글 글꼴이 없어 자막 그림 시험은 건너뜀)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        out = A.render_subtitle("할아버지, 아픈 건 언제 없어져요?", (640, 352), os.path.join(tmp, "s.png"))
        img = Image.open(out)
        assert img.mode == "RGBA" and img.size == (640, 352) and img.getpixel((5, 5))[3] == 0
        box = img.getbbox()
        assert box and box[1] > 352 * 0.6                     # 글자는 아래쪽에만 있다
    keep = A.subtitle_font
    A.subtitle_font = lambda: None
    try:                                                     # 글꼴이 없으면 그리지 않는다 (자막 없이 합치고 안내 한 줄)
        assert A.render_subtitle("x", (64, 64), os.path.join(tempfile.gettempdir(), "never.png")) is None
    finally:
        A.subtitle_font = keep


def t_same_video_needs_matching_streams():
    p = {"width": 1280, "height": 704, "vcodec": "h264", "fps": "24/1", "pix_fmt": "yuv420p"}
    assert A.same_video([p, dict(p)]) and not A.same_video([p, dict(p, fps="25/1")]) and not A.same_video([])


def t_run_refuses_without_ffmpeg_or_clips():
    if not A.available():
        try:
            A.run(["x.mp4"], None, "o.mp4")
        except A.AssembleError as e:
            assert "ffmpeg" in str(e)
        else:
            raise AssertionError("ffmpeg 없이 합치기가 돌았다")
        return
    try:
        A.run([], None, "o.mp4")
    except A.AssembleError as e:
        assert "장면" in str(e)
    else:
        raise AssertionError("빈 목록을 받았다")


def t_real_join_when_ffmpeg_is_present():
    """허브 이미지처럼 ffmpeg 가 있으면 시험 클립 두 개(소리 있음·없음)와 음악으로 실제로 합쳐 길이·소리를 본다"""
    if not A.available():
        print("  (ffmpeg 가 없어 실제 합치기는 건너뜀)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        def make(name, seconds, audio):
            path = os.path.join(tmp, name)
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                   f"testsrc=size=320x176:rate=24:duration={seconds}"]
            if audio:
                cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000"]
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac", "-shortest"] if audio else []) + [path]
            subprocess.run(cmd, check=True, capture_output=True)
            return path
        a, b = make("a.mp4", 2, True), make("b.mp4", 3, False)
        music = os.path.join(tmp, "m.mp3")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=7:sample_rate=48000", music], check=True, capture_output=True)
        out = os.path.join(tmp, "out", "long.mp4")
        info = A.run([a, b], music, out)
        got = A.probe(out)
        assert abs(got["duration"] - 5.0) < 0.3 and got["audio"] and info["scenes"] == 2 and info["music"]
        assert not os.path.exists(out + ".part.mp4")
        # 디졸브: 겹친 만큼 짧아지고(2+3-0.6), 소리도 이어진다
        c = make("c.mp4", 3, True)
        out2 = os.path.join(tmp, "out", "dissolve.mp4")
        info2 = A.run([a, c], music, out2, True, [0.6])
        got2 = A.probe(out2)
        assert abs(got2["duration"] - 4.4) < 0.3 and got2["audio"] and info2["dissolves"] == 1 and not info2["copy_video"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("ok studio_assemble")
