"""promptops 단위 테스트. 개발 PC(Windows 포함)에서도 실행된다.

  cd hub && python -m pytest tests     (pytest 가 없으면: python tests/test_promptops.py)
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import promptops  # noqa: E402


def sample_prompt():
    return {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "model": ["4", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl/base.safetensors"}},
        "5": {"class_type": "LoraLoader", "inputs": {"lora_name": "style.safetensors", "strength_model": 1.0}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
        "10": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": "clips/run", "frame_rate": 16}},
        "11": {"class_type": "LoadImage", "inputs": {"image": "photo.png", "upload": "image"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": "missing.png"}},
    }


def test_rewrite_filename_prefix():
    p = sample_prompt()
    n = promptops.rewrite_filename_prefix(p, "main", "2026-10-23")
    assert n == 2
    assert p["9"]["inputs"]["filename_prefix"] == "main/2026-10-23/ComfyUI"
    assert p["10"]["inputs"]["filename_prefix"] == "main/2026-10-23/clips/run"


def test_rewrite_blocks_parent_paths():
    p = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "../../etc/x"}}}
    promptops.rewrite_filename_prefix(p, "main", "d")
    assert ".." not in p["1"]["inputs"]["filename_prefix"]


def test_find_models():
    names = [m["name"] for m in promptops.find_models(sample_prompt())]
    assert names == ["sdxl/base.safetensors", "style.safetensors"]


def test_pin_inputs():
    tmp = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp, "photo.png"), "wb") as f:
            f.write(b"png")
        p = sample_prompt()
        pinned = promptops.pin_inputs(p, tmp, "job-1", shutil.copy2)
        assert pinned == ["photo.png"]
        assert p["11"]["inputs"]["image"] == "_mgs_pinned/job-1/photo.png"
        assert os.path.isfile(os.path.join(tmp, "_mgs_pinned", "job-1", "photo.png"))
        assert p["12"]["inputs"]["image"] == "missing.png"
        # 원본을 바꿔도 고정본은 그대로
        with open(os.path.join(tmp, "photo.png"), "wb") as f:
            f.write(b"changed")
        with open(os.path.join(tmp, "_mgs_pinned", "job-1", "photo.png"), "rb") as f:
            assert f.read() == b"png"
    finally:
        shutil.rmtree(tmp)


def test_pin_inputs_rejects_escape():
    tmp = tempfile.mkdtemp()
    try:
        p = {"1": {"class_type": "LoadImage", "inputs": {"image": "../outside.png"}}}
        assert promptops.pin_inputs(p, tmp, "j", shutil.copy2) == []
    finally:
        shutil.rmtree(tmp)


def test_split_sensitive():
    extra, secrets = promptops.split_sensitive({"api_key_comfy_org": "k", "extra_pnginfo": {}})
    assert "api_key_comfy_org" not in extra and secrets == {"api_key_comfy_org": "k"}


def test_filter_node_errors():
    ok, rem = promptops.filter_node_errors({"type": "missing_node_type"}, {}, True)
    assert ok and rem == {}
    ok, _ = promptops.filter_node_errors({"type": "missing_node_type"}, {}, False)
    assert not ok
    errs = {"4": {"errors": [{"type": "value_not_in_list"}], "class_type": "CheckpointLoaderSimple"}}
    ok, rem = promptops.filter_node_errors({"type": "prompt_outputs_failed_validation"}, errs, True)
    assert not ok and "4" in rem


def test_history_outputs():
    entry = {"outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "main/2026-10-23", "type": "output"}]},
                         "10": {"gifs": [{"filename": "v.mp4", "subfolder": "x", "type": "output"}],
                                "animated": [True]}}}
    files = promptops.history_outputs(entry)
    assert [f["filename"] for f in files] == ["a.png", "v.mp4"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
