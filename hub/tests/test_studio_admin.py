"""레시피 도구 시험: Export (API) → 뼈대(studio-import), V11 노드 검사, V12 표본 값, 표본 PNG. DB·GPU 없이 돈다.

  cd hub && python tests/test_studio_admin.py
"""
import argparse
import copy
import json
import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.studio import admin, files, recipes as R  # noqa: E402

rs = R.load_all()
wan, zi = rs["wan22_i2v_14b_fast"], rs["zimage_turbo_t2i"]


def bad(report):
    return {x["id"] for x in report if not x["ok"]}


# ── skeleton_from_api: 레시피의 그래프에서 다시 뼈대를 만들면 같은 자리를 찾는다 ──
for rec, action in ((wan, "i2v"), (zi, "image")):
    sk, todo = R.skeleton_from_api(rec["prompt"], "test_" + action, action)
    for name in ("prompt", "seed", "width", "height"):
        assert sk["inject"][name]["node"] == rec["inject"][name]["node"], (action, name)
        assert sk["inject"][name]["input"] == rec["inject"][name]["input"], (action, name)
    assert sk["negative_source"] == rec["negative_source"], action
    assert sk["outputs"]["main"]["node"] == rec["outputs"]["main"]["node"], action
    assert sk["inject"]["save_prefix"][0]["node"] == rec["outputs"]["main"]["node"]
    assert not bad(R.check_recipe(sk, None)), [x for x in R.check_recipe(sk, None) if not x["ok"]]
    assert any("limits" in t for t in todo)
assert R.skeleton_from_api(wan["prompt"], "t", "i2v")[0]["inject"]["first_frame"]["node"] == "97"
assert R.skeleton_from_api(wan["prompt"], "t", "i2v")[0]["inject"]["frames"]["node"] == "129:98"
# 길이가 수식 링크면 프레임 자리를 만들지 못하고 사람에게 알린다 (메인에서 내보낸 원본이 이 모양이다)
linked = copy.deepcopy(wan["prompt"])
linked["129:98"]["inputs"]["length"] = ["129:163", 1]
sk, todo = R.skeleton_from_api(linked, "t", "i2v")
assert "frames" not in sk["inject"] and any("프레임 수" in t for t in todo)
# 부정 글 노드가 둘이면 못 정한다고 알린다
two = copy.deepcopy(wan["prompt"])
two["x1"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "n2", "clip": ["129:84", 0]}}
two["129:98"]["inputs"]["negative"] = ["129:89", 0]
two["129:86"]["inputs"]["negative"] = ["x1", 0]
assert any("부정 글 노드" in t for t in R.skeleton_from_api(two, "t", "i2v")[1])

# ── V11: /object_info 에 노드·입력이 있는가 ──
oi = {"CLIPTextEncode": {"input": {"required": {"text": ["STRING"], "clip": ["CLIP"]}}},
      "KSampler": {"input": {"required": {"seed": ["INT"], "model": ["MODEL"]}}},
      "OllamaX": {"input": {"required": {}}, "api_node": True}}
p = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "a", "clip": ["9", 0]}},
     "2": {"class_type": "KSampler", "inputs": {"seed": 1, "model": ["9", 0]}}}
assert R.check_object_info(p, oi) == ([], [])
p2 = copy.deepcopy(p); p2["2"]["inputs"]["steps"] = 8                       # 모르는 입력
e, w = R.check_object_info(p2, oi)
assert e and "steps" in e[0]
p3 = copy.deepcopy(p); p3["3"] = {"class_type": "Nope", "inputs": {"a": 1}}  # 편집 공간(CPU)에 없는 노드
e, w = R.check_object_info(p3, oi, accept_missing=True)
assert not e and w and "Nope" in w[0]
e, w = R.check_object_info(p3, oi, accept_missing=False)
assert e and not w
p4 = copy.deepcopy(p); p4["4"] = {"class_type": "OllamaX", "inputs": {}}     # API 노드는 거부
assert R.check_object_info(p4, oi)[0]
p5 = copy.deepcopy(p); p5["1"]["inputs"]["format.codec"] = "auto"            # 점 이름은 첫 마디만 본다
assert R.check_object_info(p5, {**oi, "CLIPTextEncode": {"input": {"required": {"text": [], "clip": [], "format": []}}}})[0] == []

# ── V12 표본 값: 레시피마다 필요한 자리만 ──
sv = R.sample_values(wan)
assert set(sv) == set(wan["inject"]) and sv["first_frame"] == R.SAMPLE_IMAGE and sv["frames"] == 81 and sv["check_index"] == 40
assert sv["save_prefix"] == ["_check/00000000/video", "_check/00000000/check_frame"]
built = R.build(wan, sv)
assert built["97"]["inputs"]["image"] == "studio/_check/sample.png"
szv = R.sample_values(zi)
assert set(szv) == set(zi["inject"]) and "first_frame" not in szv and "frames" not in szv
R.build(zi, szv)

# ── 표본 PNG: 올바른 PNG 이고 입력 폴더 안에 만들어진다 ──
settings.envs_dir = tempfile.mkdtemp()
path = files.write_sample_png({"slug": "studio-chat"})
assert path.replace("\\", "/").endswith("input/studio/_check/sample.png")
b = open(path, "rb").read()
assert b[:8] == b"\x89PNG\r\n\x1a\n"
i = 8
while i < len(b):
    ln = struct.unpack(">I", b[i:i + 4])[0]
    tag, body = b[i + 4:i + 8], b[i + 8:i + 8 + ln]
    assert zlib.crc32(tag + body) & 0xFFFFFFFF == struct.unpack(">I", b[i + 8 + ln:i + 12 + ln])[0]
    i += 12 + ln
w, h = struct.unpack(">II", b[16:24])
assert (w, h) == (64, 64)

# ── studio-import 명령: 파일에 쓰고, API 형식이 아니면 거부, 덮어쓰기는 --force ──
tmp = tempfile.mkdtemp()
src = os.path.join(tmp, "api.json")
json.dump(zi["prompt"], open(src, "w", encoding="utf-8"))
out = os.path.join(tmp, "my_recipe.json")
ns = argparse.Namespace(file=src, id="my_recipe", action="image", title="내 레시피", out=out, force=False)
admin.studio_import(ns)
made = json.load(open(out, encoding="utf-8"))
assert made["id"] == "my_recipe" and made["title"] == "내 레시피" and made["source"]["imported"]
try:
    admin.studio_import(ns)
    raise AssertionError("이미 있으면 거부")
except SystemExit as e:
    assert "이미 있습니다" in str(e)
ns.force = True
admin.studio_import(ns)
ui = os.path.join(tmp, "ui.json")
json.dump({"nodes": [], "links": []}, open(ui, "w"))
ns2 = argparse.Namespace(file=ui, id="x1x", action="image", title=None, out=os.path.join(tmp, "x.json"), force=False)
try:
    admin.studio_import(ns2)
    raise AssertionError("UI 형식은 거부")
except SystemExit as e:
    assert "API 형식" in str(e)
print("ok studio_admin")
