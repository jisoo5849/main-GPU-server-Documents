"""업로드 이미지와 결과 파일 (docs/chat-studio-design.md 7.3·7.4·14.4).

- 올린 사진은 내용으로 형식을 확인하고(확장자 말고), 크기를 줄여 PNG 로 저장한다. 원본 파일 이름은 쓰지 않는다.
- 저장 위치: <MGS_ENVS>/state/<채팅 작업공간>/input/studio/<사용자 폴더>/<첨부 uuid>.png (LoadImage 값은 studio/<사용자 폴더>/<uuid>.png)
- 결과 파일은 결과 폴더(MGS_RESULTS) 안에서만 읽는다. 폴더 밖으로 나가는 경로는 거부한다.
"""
import hashlib
import io
import os
import uuid

from ..config import settings
from . import recipes

MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".mp4": "video/mp4", ".webm": "video/webm"}


class UploadError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def sniff_image(data: bytes) -> str | None:
    """파일 머리로 PNG·JPEG·WEBP 만 받는다 (SVG 등은 거부)"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def input_subdir(username: str, user_id: int) -> str:
    return f"studio/{recipes.user_folder(username, user_id)}"


def input_dir(slug: str) -> str:
    return os.path.join(settings.state_path(slug), "input")


def process_image(data: bytes) -> tuple[bytes, int, int]:
    """검사하고 저장용으로 줄인 PNG 바이트와 (너비, 높이). Pillow 는 허브 이미지에만 있다."""
    if len(data) > settings.studio_upload_max_mb * 1024 * 1024:
        raise UploadError("too_large", f"사진은 {settings.studio_upload_max_mb}MB 까지 올릴 수 있습니다.", 413)
    if sniff_image(data) is None:
        raise UploadError("bad_format", "PNG·JPEG·WEBP 사진만 올릴 수 있습니다.", 415)
    try:
        from PIL import Image, ImageOps
    except ImportError as e:   # 개발 PC 에는 없을 수 있다. 허브 이미지에는 있다 (hub/requirements.txt)
        raise UploadError("server", "사진을 처리할 수 없습니다.", 500) from e
    try:
        img = Image.open(io.BytesIO(data))
        w0, h0 = img.size          # 열기만 하고 픽셀을 읽기 전에 크기를 본다 (압축 폭탄 방어)
        if max(w0, h0) > settings.studio_upload_max_side or w0 * h0 > settings.studio_upload_max_side ** 2:
            raise UploadError("too_big_pixels", f"사진의 긴 변은 {settings.studio_upload_max_side}px 까지입니다.", 413)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img
        img.thumbnail((settings.studio_upload_store_side, settings.studio_upload_store_side))
        out = io.BytesIO()
        img.save(out, format="PNG")      # 다시 저장하면 메타데이터(위치 정보 등)가 빠진다
        return out.getvalue(), img.size[0], img.size[1]
    except UploadError:
        raise
    except Exception as e:  # noqa: BLE001 - 깨진 파일
        raise UploadError("bad_image", "사진을 읽을 수 없습니다.", 400) from e


def write_upload(env: dict, username: str, user_id: int, png: bytes) -> tuple[str, str, str]:
    """입력 폴더에 저장한다. (첨부 id, 입력 폴더 기준 상대 경로, sha256)"""
    att_id = str(uuid.uuid4())
    sub = input_subdir(username, user_id)
    folder = os.path.join(input_dir(env["slug"]), sub)
    os.makedirs(folder, exist_ok=True)
    rel = f"{sub}/{att_id}.png"
    path = os.path.join(input_dir(env["slug"]), rel)
    with open(path, "wb") as f:
        f.write(png)
    try:    # 실행 공간(작업공간 주인 권한)이 읽을 수 있게
        os.chmod(path, 0o664)
        os.chmod(folder, 0o775)
        os.chown(path, env["linux_uid"], settings.group_gid)
    except OSError:
        pass
    return att_id, rel, hashlib.sha256(png).hexdigest()


def write_sample_png(env: dict) -> str:
    """레시피 검사(V12)용 64x64 회색 PNG 를 입력 폴더의 studio/_check/sample.png 에 만든다 (Pillow 없이). 경로를 돌려준다"""
    import struct
    import zlib

    def chunk(tag: bytes, body: bytes) -> bytes:
        c = struct.pack(">I", len(body)) + tag + body
        return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    row = b"\x00" + b"\x80" * 64
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(row * 64)) + chunk(b"IEND", b""))
    path = os.path.join(input_dir(env["slug"]), "studio", "_check", "sample.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    try:
        os.chmod(path, 0o664)
        os.chmod(os.path.dirname(path), 0o775)
    except OSError:
        pass
    return path


def write_sample_wav(env: dict) -> str:
    """레시피 검사(V12)용 0.5초 무음 WAV 를 입력 폴더의 studio/_check/sample.wav 에 만든다 (목소리 복제·입 맞춤 레시피의 LoadAudio)"""
    import struct
    rate, n = 16000, 8000
    data = bytes(2 * n)              # 16비트 무음
    wav = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
           + b"data" + struct.pack("<I", len(data)) + data)
    path = os.path.join(input_dir(env["slug"]), "studio", "_check", "sample.wav")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(wav)
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass
    return path


def write_silence_wav(path: str, seconds: float = 0.12, rate: int = 48000) -> str:
    """짧은 무음 WAV (모노 16비트, 다듬은 대사 소리와 같은 48kHz). 두 사람 대사 컷에서 말하지 않는 사람 자리에 넣는다
    (11-24 메인 X6: 듣는 사람 자리에 0.12초 무음을 넣자 그 사람은 입을 다물고 들었다). 경로를 돌려준다"""
    import struct
    n = max(1, int(round(float(seconds) * rate)))
    data = bytes(2 * n)
    wav = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
           + b"data" + struct.pack("<I", len(data)) + data)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(wav)
    return path


def turn_input_rel(username: str, user_id: int, turn_id, name: str) -> str:
    """요청 하나가 만든 입력 파일(대사 소리 등)의 입력 폴더 기준 상대 경로: studio/<사용자 폴더>/<요청 앞 8자>/<이름>"""
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("파일 이름이 올바르지 않습니다")
    return f"{input_subdir(username, user_id)}/{str(turn_id)[:8]}/{name}"


def input_write_path(env: dict, rel: str) -> str:
    """입력 폴더 안에 쓸 파일의 절대 경로 (폴더를 만든다). 폴더 밖으로 나가면 ValueError"""
    base = os.path.realpath(input_dir(env["slug"]))
    p = os.path.realpath(os.path.join(base, rel))
    if not p.startswith(base + os.sep):
        raise ValueError("입력 폴더 밖 경로입니다")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def own_input(env: dict, path: str):
    """허브가 입력 폴더에 쓴 파일을 실행 공간(작업공간 주인 권한)이 읽을 수 있게 한다 (write_upload 와 같다)"""
    try:
        os.chmod(path, 0o664)
        os.chmod(os.path.dirname(path), 0o775)
        os.chown(path, env["linux_uid"], settings.group_gid)
    except (OSError, KeyError, AttributeError):       # AttributeError: 개발 PC(Windows)에는 chown 이 없다
        pass


def input_path(env: dict, rel: str) -> str | None:
    base = os.path.realpath(input_dir(env["slug"]))
    p = os.path.realpath(os.path.join(base, rel))
    return p if p.startswith(base + os.sep) and os.path.isfile(p) else None


def result_path(rel: str) -> str | None:
    """결과 폴더 안의 파일 경로. 폴더 밖이거나 없으면 None"""
    base = os.path.realpath(settings.results_dir)
    p = os.path.realpath(os.path.join(base, rel))
    return p if p.startswith(base + os.sep) and os.path.isfile(p) else None


def fingerprint(rel: str) -> tuple[int | None, str | None]:
    """결과 파일의 (바이트, sha256). 나중에 파일이 바뀌었는지 비교하려고 결과를 받을 때 남긴다 (5.13, 7.3)"""
    p = result_path(rel)
    if p is None:
        return None, None
    h = hashlib.sha256()
    n = 0
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def copy_result_as_input(env: dict, username: str, user_id: int, result_rel: str,
                         expected_sha: str | None) -> tuple[str, str, str, int, int, int]:
    """직전 결과 이미지를 입력 폴더로 복사한다 (7.3). (첨부 id, 입력 상대 경로, sha256, 너비, 높이, 바이트).
    결과 폴더는 누구나 고칠 수 있으므로 결과를 받을 때 남긴 sha256 과 다르면 복사하지 않는다. 다시 인코딩하므로
    메타데이터도 빠진다."""
    p = result_path(result_rel)
    if p is None:
        raise UploadError("gone", "결과 파일을 찾을 수 없어 이어서 만들 수 없습니다.", 404)
    with open(p, "rb") as f:
        data = f.read()
    if not expected_sha or hashlib.sha256(data).hexdigest() != expected_sha:
        # 지문이 없는 결과도 바뀌었는지 알 수 없으므로 복사하지 않는다 (결과 폴더는 누구나 고칠 수 있다)
        raise UploadError("changed", "결과 파일이 바뀌어 이어서 만들 수 없습니다.", 409)
    png, w, h = process_image(data)
    att_id, rel, sha = write_upload(env, username, user_id, png)
    return att_id, rel, sha, w, h, len(png)


def mime_of(rel: str) -> str:
    return MIME_BY_EXT.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")


def pick_output(outputs: list[dict], node_id: str, kind: str, exts: list[str] | None) -> dict | None:
    """job_outputs 에서 레시피가 가리킨 노드의 결과를 고른다. 확장자까지 맞아야 한다 (7.4)"""
    for o in outputs:
        if str(o.get("node_id")) != str(node_id):
            continue
        ext = os.path.splitext(o["rel_path"])[1].lower()
        if exts and ext not in exts:
            continue
        return o
    return None


def split_rel(rel: str) -> tuple[str, str, str]:
    """결과 상대 경로 '<사용자>/chat/<날짜>/…/파일' → (결과 폴더 주인, 하위 폴더, 파일 이름)"""
    parts = rel.replace("\\", "/").split("/")
    return parts[0], "/".join(parts[1:-1]), parts[-1]
