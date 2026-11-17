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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("ok studio_assemble")
