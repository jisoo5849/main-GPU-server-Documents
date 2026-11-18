"""긴 영상 합치기 (docs/long-video-design.md 7.3): 장면 클립을 잇고, 장면 소리 크기를 고르게 하고, 배경음악을 섞는다.

GPU 를 쓰지 않는다. ComfyUI 노드로 합치면 모든 프레임을 메모리에 올려야 해서(3분 720p ≈ 140GB) 허브의 ffmpeg 로 파일 단위로 한다.
- 영상: 다시 인코딩하지 않고 이어 붙인다 (concat 목록 + -c copy). 모든 장면이 같은 레시피·크기다.
- 소리: 장면마다 크기를 재서(loudnorm 측정) 같은 크기가 되게 한 번에 올리거나 내린다(올림 +12dB, 내림 -24dB 까지).
  거의 무음인 장면은 올리지 않는다 (잡음이 커지지 않게). 장면 경계에는 짧은 페이드로 딸깍 소리를 막는다.
- 음악: 장면 소리보다 8dB 작게 깔고 처음·끝을 페이드. 다 섞은 뒤 다시 재서 -14 LUFS(유튜브 기준)로 맞춘다.
명령을 만드는 함수(build_*)는 순수 함수라 ffmpeg 없이 시험한다 (hub/tests/test_studio_assemble.py).
"""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile

SCENE_LUFS = -20.0
MUSIC_LUFS = -28.0
FINAL_LUFS = -14.0
FINAL_TP = -1.5
GAIN_UP_MAX, GAIN_DOWN_MAX = 12.0, -24.0
SILENT_LUFS = -60.0        # 이보다 작으면 무음으로 보고 크기를 바꾸지 않는다
EDGE_FADE = 0.08           # 장면 경계 페이드(초)
MUSIC_FADE_IN, MUSIC_FADE_OUT = 1.5, 3.0
DURATION_SLACK = 0.5       # 결과 길이가 장면 합과 이만큼 넘게 다르면 실패로 본다
TIMEOUT = 900
LOUDNORM_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


class AssembleError(RuntimeError):
    """합치기 실패 (한국어 설명)"""


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(cmd: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssembleError(f"ffmpeg 가 {timeout}초 안에 끝나지 않았습니다") from None
    except OSError as e:
        raise AssembleError(f"ffmpeg 를 실행하지 못했습니다: {e}") from None
    if r.returncode != 0:
        raise AssembleError("ffmpeg 오류: " + (r.stderr or "").strip()[-600:])
    return r


def probe(path: str) -> dict:
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], 60)
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    dur = float((v or {}).get("duration") or fmt.get("duration") or 0)
    return {"duration": dur, "video": v is not None, "audio": a is not None,
            "width": (v or {}).get("width"), "height": (v or {}).get("height"), "vcodec": (v or {}).get("codec_name"),
            "fps": (v or {}).get("r_frame_rate"), "pix_fmt": (v or {}).get("pix_fmt")}


def parse_loudnorm(stderr: str) -> dict | None:
    """loudnorm print_format=json 이 stderr 끝에 내는 측정값"""
    m = None
    for m in LOUDNORM_RE.finditer(stderr or ""):
        pass
    if m is None:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    out = {}
    for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            out[k] = float(data.get(k))
        except (TypeError, ValueError):
            out[k] = float("-inf")
    return out


def measure(path: str) -> dict | None:
    """소리 크기 측정 (첫 소리 줄). 소리가 없으면 None"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-af",
                        f"loudnorm=I={FINAL_LUFS}:TP={FINAL_TP}:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return None
    return parse_loudnorm(r.stderr)


def gain_db(measured: dict | None, target: float) -> float:
    """측정값을 목표 크기로 옮기는 고정 이득(dB). 무음·측정 실패는 0"""
    i = (measured or {}).get("input_i")
    if i is None or not math.isfinite(i) or i < SILENT_LUFS:
        return 0.0
    return round(max(GAIN_DOWN_MAX, min(GAIN_UP_MAX, target - i)), 2)


def concat_list(paths: list[str], durations: list[float] | None = None) -> str:
    """ffmpeg concat 목록. 작은따옴표는 '\\'' 로 감싼다. durations(영상 길이)를 주면 파일마다 duration 줄을 붙인다:
    concat 은 다음 파일을 앞 파일의 컨테이너 길이만큼 밀어서, 소리가 영상보다 긴 클립이면 컷마다 화면이 멈춘다 (11-17 검토)"""
    out = []
    for i, p in enumerate(paths):
        out.append("file '" + p.replace("'", "'\\''") + "'\n")
        if durations:
            out.append(f"duration {float(durations[i]):.6f}\n")
    return "".join(out)


def same_video(probes: list[dict]) -> bool:
    keys = ("width", "height", "vcodec", "fps", "pix_fmt")
    return bool(probes) and all(all(p.get(k) == probes[0].get(k) for k in keys) for p in probes)


def fit_transitions(durations: list[float], transitions: list[float] | None) -> list[float]:
    """이음새마다 겹침 길이(초). 0 이면 컷. 이웃 두 클립 중 짧은 쪽의 절반을 넘지 않게 자른다. 길이가 안 맞거나 없으면 전부 컷"""
    n = len(durations)
    if not transitions or len(transitions) != n - 1:
        return [0.0] * max(0, n - 1)
    out = []
    for i, o in enumerate(transitions):
        try:
            o = float(o)
        except (TypeError, ValueError):
            o = 0.0
        cap = min(durations[i], durations[i + 1]) / 2 - 0.05
        out.append(round(max(0.0, min(o, cap)), 3) if o > 0.05 and cap > 0.05 else 0.0)
    return out


def timeline_total(durations: list[float], overlaps: list[float]) -> float:
    return sum(durations) - sum(overlaps)


def build_video_filter(durations: list[float], overlaps: list[float], fps: float) -> str:
    """장면 영상(입력 0..N-1)을 이음새마다 컷(concat) 또는 디졸브(xfade)로 잇는 filter_complex. 출력 라벨 [v]"""
    n = len(durations)
    parts = [f"[{i}:v]fps={fps:g},settb=AVTB,setsar=1,format=yuv420p[v{i}]" for i in range(n)]
    cur, cur_len = "[v0]", durations[0]
    for i in range(1, n):
        o = overlaps[i - 1]
        out = "[v]" if i == n - 1 else f"[x{i}]"
        if o > 0:
            parts.append(f"{cur}[v{i}]xfade=transition=fade:duration={o:.3f}:offset={cur_len - o:.3f}{out}")
            cur_len += durations[i] - o
        else:
            parts.append(f"{cur}[v{i}]concat=n=2:v=1:a=0{out}")
            cur_len += durations[i]
        cur = out
    if n == 1:
        parts.append("[v0]null[v]")
    return ";".join(parts)


def build_mix_filter(durations: list[float], has_audio: list[bool], gains: list[float],
                     music_gain: float | None, overlaps: list[float] | None = None) -> str:
    """장면 소리(입력 0..N-1)와 음악(입력 N)을 섞는 filter_complex. 출력 라벨 [mix].
    overlaps(이음새마다 겹침 초)가 있으면 겹치는 이음새는 소리도 교차 페이드(acrossfade)로 잇고 가장자리 페이드를 빼며, 컷은 그대로 잇는다"""
    parts, labels = [], []
    n = len(durations)
    ov = overlaps if overlaps and any(o > 0 for o in overlaps) else None
    for i, (d, has, g) in enumerate(zip(durations, has_audio, gains)):
        fo = max(0.0, d - EDGE_FADE)
        if has:
            src = (f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=0:{d:.3f},"
                   f"asetpts=PTS-STARTPTS,volume={g:.2f}dB")
        else:
            src = f"anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f},asetpts=PTS-STARTPTS"
        fades = ""
        if ov is None or i == 0 or ov[i - 1] <= 0:
            fades += f",afade=t=in:st=0:d={EDGE_FADE}"
        if ov is None or i == n - 1 or ov[i] <= 0:
            fades += f",afade=t=out:st={fo:.3f}:d={EDGE_FADE}"
        parts.append(f"{src}{fades}[a{i}]")
        labels.append(f"[a{i}]")
    total = timeline_total(durations, ov or [])
    sfx = "[mix]" if music_gain is None else "[sfx]"
    if ov is None:
        parts.append("".join(labels) + f"concat=n={n}:v=0:a=1{sfx}")
    elif n == 1:
        parts.append(f"[a0]anull{sfx}")
    else:
        cur = "[a0]"
        for i in range(1, n):
            out = sfx if i == n - 1 else f"[y{i}]"
            if ov[i - 1] > 0:
                parts.append(f"{cur}[a{i}]acrossfade=d={ov[i - 1]:.3f}:c1=tri:c2=tri{out}")
            else:
                parts.append(f"{cur}[a{i}]concat=n=2:v=0:a=1{out}")
            cur = out
    if music_gain is None:
        return ";".join(parts)
    fo = max(0.0, total - MUSIC_FADE_OUT)
    parts.append(f"[{n}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=0:{total:.3f},"
                 f"asetpts=PTS-STARTPTS,volume={music_gain:.2f}dB,afade=t=in:st=0:d={MUSIC_FADE_IN},"
                 f"afade=t=out:st={fo:.3f}:d={MUSIC_FADE_OUT}[mus]")
    parts.append("[sfx][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]")
    return ";".join(parts)


def build_mix_cmd(clips: list[str], music: str | None, filt: str, out_wav: str) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y"]
    for c in clips:
        cmd += ["-i", c]
    if music:
        cmd += ["-i", music]
    # 중간 믹스는 32비트 실수로: 장면 이득·음악을 더한 피크가 0dBFS 를 넘어도 잘리지 않고 마지막 음량 맞춤이 내린다
    return cmd + ["-filter_complex", filt, "-map", "[mix]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", out_wav]


def final_audio_filter(measured: dict | None) -> str:
    """다 섞은 소리를 -14 LUFS 로. 측정값이 있으면 고정 이득(linear), 없으면 한 번에(dynamic)"""
    base = f"loudnorm=I={FINAL_LUFS}:TP={FINAL_TP}:LRA=11"
    m = measured or {}
    if all(math.isfinite(m.get(k, float("-inf"))) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
        base += (f":measured_I={m['input_i']:.2f}:measured_TP={m['input_tp']:.2f}:measured_LRA={m['input_lra']:.2f}"
                 f":measured_thresh={m['input_thresh']:.2f}:offset={m['target_offset']:.2f}:linear=true")
    return base + ",aresample=48000"


def build_final_cmd(list_file: str, mix_wav: str | None, out: str, copy_video: bool, measured: dict | None) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-f", "concat", "-safe", "0", "-i", list_file]
    if mix_wav:
        cmd += ["-i", mix_wav]
    cmd += ["-map", "0:v:0"]
    cmd += ["-c:v", "copy"] if copy_video else ["-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p"]
    if mix_wav:
        cmd += ["-map", "1:a:0", "-af", final_audio_filter(measured), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest"]
    else:
        cmd += ["-an"]
    return cmd + ["-movflags", "+faststart", out]


def build_xfade_cmd(clips: list[str], mix_wav: str | None, out: str, vfilter: str, measured: dict | None) -> list[str]:
    """디졸브가 있을 때의 합치기: 영상은 filter_complex 로 잇고(다시 인코딩), 소리는 미리 섞은 wav 를 쓴다"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y"]
    for c in clips:
        cmd += ["-i", c]
    if mix_wav:
        cmd += ["-i", mix_wav]
    cmd += ["-filter_complex", vfilter, "-map", "[v]", "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p"]
    if mix_wav:
        cmd += ["-map", f"{len(clips)}:a:0", "-af", final_audio_filter(measured), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest"]
    else:
        cmd += ["-an"]
    return cmd + ["-movflags", "+faststart", out]


def _fps(p: dict) -> float:
    try:
        a, _, b = str(p.get("fps") or "").partition("/")
        v = float(a) / float(b or 1)
        return v if 1 <= v <= 120 else 24.0
    except (ValueError, ZeroDivisionError):
        return 24.0


def run(clips: list[str], music: str | None, out: str, sound: bool = True, transitions: list[float] | None = None) -> dict:
    """장면 클립(절대 경로, 순서대로)과 음악을 합쳐 out 에 쓴다. 실패하면 AssembleError. 같은 이름이 있으면 덮어쓴다.
    transitions: 이음새(장면 i 와 i+1 사이)마다 디졸브 길이(초), 0 이면 컷. 없으면 전부 컷(다시 인코딩하지 않음)"""
    if not available():
        raise AssembleError("허브에 ffmpeg 가 없습니다 (hub/Dockerfile 에 넣은 판으로 업데이트해야 합니다)")
    if not clips:
        raise AssembleError("이어 붙일 장면이 없습니다")
    probes = [probe(c) for c in clips]
    if not all(p["video"] and p["duration"] > 0 for p in probes):
        raise AssembleError("영상이 없는 장면 파일이 있습니다")
    sizes = {(p["width"], p["height"]) for p in probes}
    if len(sizes) > 1:
        raise AssembleError(f"장면 크기가 서로 다릅니다: {sorted(sizes)}")
    durations = [p["duration"] for p in probes]
    overlaps = fit_transitions(durations, transitions)
    dissolve = any(o > 0 for o in overlaps)
    total = timeline_total(durations, overlaps)
    copy_video = same_video(probes) and not dissolve
    info = {"scenes": len(clips), "seconds": round(total, 3), "copy_video": copy_video, "music": bool(music and sound),
            "dissolves": sum(1 for o in overlaps if o > 0)}
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp_out = out + ".part.mp4"
    with tempfile.TemporaryDirectory(prefix="studio-long-") as tmp:
        list_file = os.path.join(tmp, "list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            f.write(concat_list(clips, durations))
        mix_wav, measured = None, None
        if sound:
            has_audio = [p["audio"] for p in probes]
            gains = [gain_db(measure(c), SCENE_LUFS) if has else 0.0 for c, has in zip(clips, has_audio)]
            music_gain = gain_db(measure(music), MUSIC_LUFS) if music else None
            if music and music_gain == 0.0:
                music_gain = MUSIC_LUFS - SCENE_LUFS      # 음악 크기를 못 재면 장면 소리보다 8dB 작게
            mix_wav = os.path.join(tmp, "mix.wav")
            _run(build_mix_cmd(clips, music, build_mix_filter(durations, has_audio, gains, music_gain, overlaps), mix_wav))
            measured = measure(mix_wav)
            info.update(gains=gains, music_gain=music_gain, mix=measured)
        if dissolve:
            _run(build_xfade_cmd(clips, mix_wav, tmp_out, build_video_filter(durations, overlaps, _fps(probes[0])), measured))
        else:
            _run(build_final_cmd(list_file, mix_wav, tmp_out, copy_video, measured))
    got = probe(tmp_out)
    if abs(got["duration"] - total) > DURATION_SLACK or (sound and not got["audio"]):
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        raise AssembleError(f"합친 영상의 길이({got['duration']:.2f}초)가 장면 합({total:.2f}초)과 다르거나 소리가 없습니다")
    os.replace(tmp_out, out)
    info.update(duration=round(got["duration"], 3), bytes=os.path.getsize(out))
    return info
