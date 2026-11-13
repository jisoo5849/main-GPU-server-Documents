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


def input_path(env: dict, rel: str) -> str | None:
    base = os.path.realpath(input_dir(env["slug"]))
    p = os.path.realpath(os.path.join(base, rel))
    return p if p.startswith(base + os.sep) and os.path.isfile(p) else None


def result_path(rel: str) -> str | None:
    """결과 폴더 안의 파일 경로. 폴더 밖이거나 없으면 None"""
    base = os.path.realpath(settings.results_dir)
    p = os.path.realpath(os.path.join(base, rel))
    return p if p.startswith(base + os.sep) and os.path.isfile(p) else None


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
