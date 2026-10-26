"""공용 모델 저장소 목록. 작업이 쓰는 모델 기록과 모델 선택 화면에 쓴다."""
import os
import re
import time

from .config import settings

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")
FOLDERS = ("checkpoints", "diffusion_models", "unet", "loras", "vae", "text_encoders", "clip", "clip_vision",
           "controlnet", "upscale_models", "embeddings", "hypernetworks", "style_models", "gligen", "photomaker",
           "audio_encoders", "model_patches", "latent_upscale_models", "vae_approx")
VIDEO_RE = re.compile(r"(wan|ltx|hunyuan|cogvideo|mochi|svd|animatediff|vace|skyreels|framepack|kandinsky.?5|video)",
                      re.I)

_index: dict[str, list[tuple[str, str]]] = {}
_index_at = 0.0


def _build():
    idx: dict[str, list[tuple[str, str]]] = {}
    for folder in FOLDERS:
        base = os.path.join(settings.models_dir, folder)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base, followlinks=True):
            for fn in files:
                if fn.lower().endswith(MODEL_EXTS):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    idx.setdefault(rel, []).append((folder, full))
    return idx


def index(max_age: float = 60.0):
    global _index, _index_at
    if time.monotonic() - _index_at > max_age:
        _index = _build()
        _index_at = time.monotonic()
    return _index


def resolve(name: str) -> tuple[str, str] | None:
    """ComfyUI 위젯 값(폴더 기준 상대 경로) → (폴더, 절대 경로)."""
    hits = index().get(name.replace("\\", "/"))
    return hits[0] if hits else None


def catalog() -> dict[str, list[dict]]:
    """모델 선택 화면용: 이미지·영상 생성 모델."""
    image, video = [], []
    for rel, hits in sorted(index().items()):
        for folder, full in hits:
            if folder not in ("checkpoints", "diffusion_models", "unet"):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:   # 깨진 링크, 방금 지워진 파일
                continue
            item = {"name": rel, "folder": folder, "size_gb": round(size / 1e9, 1)}
            (video if VIDEO_RE.search(rel) else image).append(item)
            break
    return {"image": image, "video": video}
