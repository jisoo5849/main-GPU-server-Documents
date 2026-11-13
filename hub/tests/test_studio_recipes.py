"""레시피 시험 (6절): 예시 Wan I2V 레시피가 V1~V9·V13 을 통과하고, 일부러 깨뜨리면 잡힌다. DB·GPU 없이 돈다.

  cd hub && python tests/test_studio_recipes.py
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.studio import recipes as R  # noqa: E402

rs = R.load_all()
assert "wan22_i2v_14b_fast" in rs, list(rs)
rec = rs["wan22_i2v_14b_fast"]


def bad(report):
    return {r["id"] for r in report if not r["ok"]}


rep = R.check_recipe(rec, {"wan22_i2v_long", "wan22_i2v"})
assert not bad(rep), [r for r in rep if not r["ok"]]

# V1: 모르는 영상 프로필·필수 칸·한도
x = copy.deepcopy(rec); x["video_profile"] = "nope"
assert "V1" in bad(R.check_recipe(x, {"wan22_i2v_long"}))
x = copy.deepcopy(rec); del x["limits"]
assert "V1" in bad(R.check_recipe(x))
x = copy.deepcopy(rec); x["limits"]["max_run_seconds"] = 0
assert "V1" in bad(R.check_recipe(x))
# V2: 자리의 노드가 없음, 링크에 값 넣기
x = copy.deepcopy(rec); x["inject"]["seed"]["node"] = "9999"
assert "V2" in bad(R.check_recipe(x))
x = copy.deepcopy(rec); x["prompt"]["129:86"]["inputs"]["noise_seed"] = ["97", 0]
assert "V2" in bad(R.check_recipe(x))
# V2: fixed 기대값이 prompt 와 다름 (4스텝 LoRA 가 꺼진 채로 내보냄)
x = copy.deepcopy(rec); x["prompt"]["129:131"]["inputs"]["value"] = False
assert "V2" in bad(R.check_recipe(x))
# V4: 프롬프트 자리를 부정 글 노드로 바꾸면 거부
x = copy.deepcopy(rec); x["inject"]["prompt"]["node"] = "129:89"
assert "V4" in bad(R.check_recipe(x))
# V5: 부정 자리가 프롬프트 노드와 같음
x = copy.deepcopy(rec); x["fixed"]["negative"]["node"] = x["inject"]["prompt"]["node"]
assert "V5" in bad(R.check_recipe(x))
# V6: 첫 화면 자리가 LoadImage 가 아님
x = copy.deepcopy(rec); x["inject"]["first_frame"]["node"] = "129:93"
assert "V6" in bad(R.check_recipe(x))
# V7: 저장 노드의 prefix 가 inject 에 없음, suffix 에 /
x = copy.deepcopy(rec); x["inject"]["save_prefix"] = x["inject"]["save_prefix"][:1]
assert "V7" in bad(R.check_recipe(x))
x = copy.deepcopy(rec); x["inject"]["save_prefix"][0]["suffix"] = "a/b"
assert "V7" in bad(R.check_recipe(x))
# V9: LLM 노드, 자리가 아닌 LoadImage
x = copy.deepcopy(rec); x["prompt"]["777"] = {"class_type": "OllamaGenerate", "inputs": {}}
assert "V9" in bad(R.check_recipe(x))
x = copy.deepcopy(rec); x["prompt"]["778"] = {"class_type": "LoadImage", "inputs": {"image": "example.png"}}
assert "V9" in bad(R.check_recipe(x))
# V13: 샘플러가 띠에 없음
x = copy.deepcopy(rec); x["progress"]["bands"][2]["sampler"] = False
assert "V13" in bad(R.check_recipe(x))

# roles_of: 문서의 값 (129:93 = positive, 129:89 = negative)
assert R.roles_of(rec["prompt"], "129:93") == {"positive"}
assert R.roles_of(rec["prompt"], "129:89") == {"negative"}

# Z-Image 형태: 글 노드 하나가 0 조건 노드를 거쳐 negative 로도 감 → zero_out 이면 통과, node 면 거부
z = {"27": {"class_type": "CLIPTextEncode", "inputs": {"text": "x"}},
     "33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["27", 0]}},
     "3": {"class_type": "KSampler", "inputs": {"positive": ["27", 0], "negative": ["33", 0], "seed": 1}}}
assert R.roles_of(z, "27") == {"positive", "negative_zero"}
zr = {"schema": "studio-recipe-1", "id": "zimg_test", "version": 1, "title": "t", "quality_label": "q", "action": "image",
      "negative_source": "zero_out", "prompt": z,
      "inject": {"prompt": {"node": "27", "input": "text", "type": "string"},
                 "seed": {"node": "3", "input": "seed", "type": "int", "min": 0, "max": 9}},
      "outputs": {"main": {"node": "3", "kind": "image"}}, "defaults": {"width": 1024, "height": 1024},
      "progress": {"bands": [{"key": "g", "label": "생성", "weight": 1, "nodes": ["3"], "sampler": True}]},
      "limits": {"max_run_seconds": 300}}
r4 = {r["id"]: r for r in R.check_recipe(zr)}
assert r4["V4"]["ok"] and r4["V5"]["ok"], r4
zr["negative_source"] = "node"
r4 = {r["id"]: r for r in R.check_recipe(zr)}
assert not r4["V4"]["ok"] and not r4["V5"]["ok"]

# build: 값 넣기와 검사
vals = {"prompt": "a calm scene", "first_frame": "studio/u/a.png", "seed": 5, "frames": 81, "width": 640, "height": 640,
        "check_index": 40, "save_prefix": ["bob/abcd1234/video", "bob/abcd1234/check_frame"]}
p = R.build(rec, vals)
assert p["129:93"]["inputs"]["text"] == "a calm scene" and p["129:98"]["inputs"]["length"] == 81
assert p["108"]["inputs"]["filename_prefix"] == "bob/abcd1234/video" and p["901"]["inputs"]["filename_prefix"].endswith("check_frame")
assert rec["prompt"]["129:93"]["inputs"]["text"] != "a calm scene"      # 원본은 바뀌지 않는다
for k, v in (("frames", 80), ("frames", 85), ("width", 641), ("width", 100), ("seed", -1), ("prompt", "  "),
             ("first_frame", "../x.png"), ("first_frame", "/etc/passwd")):
    try:
        R.build(rec, {**vals, k: v})
        raise AssertionError(f"{k}={v!r} 는 거부돼야 한다")
    except R.RecipeError:
        pass
try:
    R.build(rec, {k: v for k, v in vals.items() if k != "seed"})
    raise AssertionError("값이 빠지면 거부")
except R.RecipeError:
    pass
# 프레임 4n+1
for f in (5, 9, 81):
    R.check_value(rec["inject"]["frames"], f)

# 크기 (문서 6.6 의 예)
assert R.size_for(1, 1) == (640, 640)
assert R.size_for(16, 9) == (848, 480)
assert R.size_for(9, 16) == (480, 848)
assert R.size_for(4, 3) == (736, 560)
assert max(R.size_for(1000, 10)) <= 1024
assert R.default_size(rec) == (640, 640)
assert R.default_size(rec, photo=(1920, 1080)) == (848, 480)
assert R.default_size(rec, aspect="portrait") == (480, 848)

# 사용자 폴더
assert R.user_folder("bob", 3) == "bob" and R.user_folder("Bob Kim", 3) == "u3" and R.user_folder("a", 9) == "u9"
assert R.prefix_value({"suffix": "video"}, "bob", 3, "0123456789abcdef") == "bob/01234567/video"
print("ok studio_recipes")
