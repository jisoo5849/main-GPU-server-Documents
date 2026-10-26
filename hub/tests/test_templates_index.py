"""templates_index 단위 테스트 (파일 시스템 없이).

  cd hub && python tests/test_templates_index.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import templates_index as T  # noqa: E402


def loader(ntype, value, directory, url="https://example.com/m"):
    return {"type": ntype, "widgets_values": [value, "default"],
            "properties": {"models": [{"name": value, "url": url, "directory": directory}]}}


def sample():
    t2i = {"nodes": [loader("UNETLoader", "z_image_turbo_bf16.safetensors", "diffusion_models"),
                     loader("CLIPLoader", "qwen_3_4b.safetensors", "text_encoders"),
                     loader("VAELoader", "ae.safetensors", "vae"),
                     {"type": "MarkdownNote", "widgets_values": ["see z_image_turbo_bf16.safetensors"]}]}
    upscale = {"nodes": [{"type": "uuid-subgraph", "widgets_values": ["z_image_turbo_bf16.safetensors"]}],
               "definitions": {"subgraphs": [{"nodes": [loader("UNETLoader", "z_image_turbo_bf16.safetensors",
                                                               "diffusion_models"),
                                                        loader("UpscaleModelLoader", "4x.pth", "upscale_models")]}]}}
    int8 = {"nodes": [loader("UNETLoader", "z_image_turbo_int8_convrot.safetensors", "diffusion_models")]}
    meta = {
        "image_z_image_turbo": {"title": "Z-Image-Turbo t2i", "models": ["Z-Image-Turbo"], "usage": 15000},
        "utility_upscale": {"title": "Upscale", "models": ["Z-Image-Turbo"], "usage": 99999, "isApp": True},
        "image_z_image_turbo_int8": {"title": "Z int8", "models": ["Z-Image-Turbo"], "usage": 10},
    }
    return T.build_index({"image_z_image_turbo": t2i, "utility_upscale": upscale,
                          "image_z_image_turbo_int8": int8}, meta), t2i


def test_family_key():
    assert T.family_key("z_image_turbo_bf16.safetensors") == T.family_key("z_image_turbo_int8_convrot.safetensors")
    assert T.family_key("krea2_turbo_fp8_scaled.safetensors") == T.family_key("krea2_turbo_int8_convrot.safetensors")
    assert T.family_key("wan_animate_2_int8_convrot.safetensors") != \
        T.family_key("wan_animate_2_distill_int8_convrot.safetensors")
    assert T.family_key("qwen_image_2.1_int8_convrot.safetensors") == "qwen_image_2.1"
    assert T.family_key("model-Q4_K_M.gguf") == "model"


def test_index_ignores_note_text_and_reads_subgraphs():
    idx, _ = sample()
    assert set(idx["exact"]["z_image_turbo_bf16.safetensors"]) == {"image_z_image_turbo", "utility_upscale"}
    assert "4x.pth" in idx["exact"]
    assert idx["required"]["image_z_image_turbo"]["ae.safetensors"] == "vae"


def test_lookup_rank_and_missing():
    idx, _ = sample()
    server = {"z_image_turbo_bf16.safetensors", "qwen_3_4b.safetensors", "ae.safetensors"}
    hits = T.lookup(idx, "z_image_turbo_bf16.safetensors", server)
    # 앱(isApp) 템플릿은 사용량이 많아도 뒤로
    assert [h["name"] for h in hits] == ["image_z_image_turbo", "utility_upscale"]
    assert hits[0]["missing"] == [] and hits[0]["match"] == "exact"
    assert [m["file"] for m in hits[1]["missing"]] == ["4x.pth"]


def test_lookup_family_when_no_exact():
    idx, _ = sample()
    server = {"z_image_turbo_fp8_scaled.safetensors"}
    hits = T.lookup(idx, "z_image_turbo_fp8_scaled.safetensors", server)
    assert hits and all(h["match"] == "family" for h in hits)


def test_folder_must_match_loader():
    idx, _ = sample()
    server = {"z_image_turbo_bf16.safetensors"}
    # 같은 파일이라도 체크포인트 폴더에 있으면 UNETLoader 템플릿은 고르지 않는다
    assert T.lookup(idx, "z_image_turbo_bf16.safetensors", server, folder="checkpoints") == []
    assert T.lookup(idx, "z_image_turbo_bf16.safetensors", server, folder="unet")   # unet == diffusion_models


def test_gguf_not_matched_to_safetensors():
    idx, _ = sample()
    assert T.lookup(idx, "z_image_turbo-Q4_K_M.gguf", {"z_image_turbo-Q4_K_M.gguf"}) == []


def test_missing_by_directory():
    idx, _ = sample()
    server = {"z_image_turbo_bf16.safetensors", "qwen_3_4b.safetensors", "ae.safetensors"}
    # ae.safetensors 가 vae 폴더가 아닌 곳에 있으면 빠진 것으로 본다
    by_dir = {"diffusion_models": {"z_image_turbo_bf16.safetensors"}, "text_encoders": {"qwen_3_4b.safetensors"},
              "loras": {"ae.safetensors"}}
    hits = T.lookup(idx, "z_image_turbo_bf16.safetensors", server, "diffusion_models", by_dir)
    assert [m["file"] for m in hits[0]["missing"]] == ["ae.safetensors"]


def test_substitute_exact_values_only():
    _, t2i = sample()
    out = T.substitute(t2i, {"z_image_turbo_bf16.safetensors": "zimg/z_image_turbo_int8_convrot.safetensors"})
    unet = out["nodes"][0]
    assert unet["widgets_values"][0] == "zimg/z_image_turbo_int8_convrot.safetensors"
    assert unet["properties"]["models"] == []          # 다른 양자화의 다운로드 주소는 지운다
    assert out["nodes"][1]["properties"]["models"]      # 다른 모델은 그대로
    assert out["nodes"][3]["widgets_values"][0] == "see z_image_turbo_bf16.safetensors"   # 메모 글은 그대로
    assert t2i["nodes"][0]["widgets_values"][0] == "z_image_turbo_bf16.safetensors"       # 원본은 안 바뀜


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
