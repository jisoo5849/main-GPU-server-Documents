"""ComfyUI 작업(API 형식 prompt) 다루기: 저장 위치 바꾸기, 사용 모델 찾기, 입력 파일 고정.

외부 의존성이 없어서 개발 PC에서도 테스트할 수 있다 (hub/tests).
"""
import os

MODEL_INPUT_KEYS = {
    "ckpt_name", "unet_name", "lora_name", "vae_name", "clip_name", "clip_name1", "clip_name2", "clip_name3",
    "clip_name4", "control_net_name", "style_model_name", "gligen_name", "upscale_model_name", "model_name",
    "clip_vision_name", "lora_01", "lora_02", "lora_03", "lora_04", "photomaker_model_name", "audio_encoder_name",
    "model_path", "text_encoder", "text_encoder_name",
}
MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")
SENSITIVE_KEYS = ("auth_token_comfy_org", "api_key_comfy_org")
PINNED_DIR = "_mgs_pinned"


def _nodes(prompt: dict):
    for node_id, node in (prompt or {}).items():
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
            yield str(node_id), node


def rewrite_filename_prefix(prompt: dict, env_name: str, day: str) -> int:
    """저장 노드의 filename_prefix 앞에 '<환경>/<날짜>/' 를 붙인다. 바꾼 노드 수를 돌려준다."""
    count = 0
    for _nid, node in _nodes(prompt):
        value = node["inputs"].get("filename_prefix")
        if isinstance(value, str):
            value = value.lstrip("/").replace("..", "_")
            node["inputs"]["filename_prefix"] = f"{env_name}/{day}/{value or 'ComfyUI'}"
            count += 1
    return count


def find_models(prompt: dict) -> list[dict]:
    """작업이 참조하는 모델 파일 이름 목록."""
    found, seen = [], set()
    for nid, node in _nodes(prompt):
        for key, value in node["inputs"].items():
            if not isinstance(value, str):
                continue
            if key in MODEL_INPUT_KEYS or value.lower().endswith(MODEL_EXTS):
                if value.lower().endswith(MODEL_EXTS) and value not in seen:
                    seen.add(value)
                    found.append({"node": nid, "class_type": node.get("class_type"), "key": key, "name": value})
    return found


def _strip_annotation(value: str) -> tuple[str, str]:
    for tag in (" [input]", " [output]", " [temp]"):
        if value.endswith(tag):
            return value[: -len(tag)], tag
    return value, ""


def pin_inputs(prompt: dict, input_dir: str, job_id: str, copy_fn) -> list[str]:
    """Load 계열 노드가 쓰는 입력 파일을 작업 전용 사본으로 고정하고 prompt 를 그 경로로 바꾼다.

    접수 뒤 원본 입력 파일이 바뀌거나 지워져도 대기 중인 작업은 접수 때 파일로 실행된다.
    """
    pinned = []
    real_input = os.path.realpath(input_dir)
    for _nid, node in _nodes(prompt):
        if "load" not in str(node.get("class_type", "")).lower():
            continue
        for key, value in list(node["inputs"].items()):
            if not isinstance(value, str) or not value or value.startswith(PINNED_DIR):
                continue
            name, tag = _strip_annotation(value)
            if tag and tag != " [input]":
                continue
            src = os.path.realpath(os.path.join(input_dir, name))
            if not src.startswith(real_input + os.sep) or not os.path.isfile(src):
                continue
            rel = f"{PINNED_DIR}/{job_id}/{name}"
            dst = os.path.join(input_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            copy_fn(src, dst)
            node["inputs"][key] = rel + tag
            pinned.append(name)
    return pinned


def split_sensitive(extra_data: dict) -> tuple[dict, dict]:
    """extra_data 에서 Comfy 계정 키를 분리한다 (DB 에 저장하지 않는다)."""
    extra = dict(extra_data or {})
    secrets = {k: extra.pop(k) for k in SENSITIVE_KEYS if k in extra}
    return extra, secrets


def filter_node_errors(error: dict | None, node_errors: dict, accept_missing: bool) -> tuple[bool, dict]:
    """편집 공간(CPU)의 검증 결과를 해석한다.

    CUDA가 있어야만 불러와지는 노드는 편집 공간에서 '없는 노드'로 나온다. accept_missing 이면
    그런 오류만 있을 때는 접수하고, 실행 공간에서 다시 검사하게 한다.
    """
    etype = (error or {}).get("type")
    if etype == "missing_node_type" and accept_missing:
        return True, {}
    if not node_errors and not error:
        return True, {}
    if node_errors and accept_missing:
        remaining = {}
        for nid, ne in node_errors.items():
            errs = [e for e in ne.get("errors", []) if e.get("type") != "missing_node_type"]
            if errs:
                remaining[nid] = {**ne, "errors": errs}
        if not remaining and etype in (None, "prompt_outputs_failed_validation", "missing_node_type"):
            return True, {}
        return False, remaining
    return False, node_errors


def history_outputs(history_entry: dict) -> list[dict]:
    """ComfyUI history 항목에서 결과 파일 목록을 뽑는다."""
    files = []
    for node_id, out in (history_entry.get("outputs") or {}).items():
        if not isinstance(out, dict):
            continue
        for media, items in out.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and it.get("filename"):
                    files.append({
                        "node_id": str(node_id),
                        "media": media,
                        "filename": it["filename"],
                        "subfolder": it.get("subfolder", ""),
                        "type": it.get("type", "output"),
                    })
    return files
