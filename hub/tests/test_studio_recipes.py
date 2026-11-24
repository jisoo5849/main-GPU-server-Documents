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

# ── 장면 잇기(11-20): 새 action·자리·optional 자리 ──
for a in ("edit_ref", "i2v_end", "compose_ref"):
    assert a in R.ACTIONS and a not in R.PART_ACTIONS
assert R.REF_ACTIONS == {"edit_ref", "compose_ref"}
for n in ("ref_image", "ref_image2", "end_frame", "last_index"):
    assert n in R.SLOT_NAMES
PROFILES = {"wan22_i2v_long", "wan22_t2v_long", "ltx25_i2v_long"}
# 저장소의 모든 레시피(파생 포함)가 검사를 통과하고 표본 값으로 값을 넣을 수 있다
for rid, r in rs.items():
    rp = R.check_recipe(r, PROFILES)
    assert R.recipe_ok(rp), (rid, [x for x in rp if not x["ok"]])
    R.build(r, R.sample_values(r))
assert {r["action"] for r in rs.values()} == {"i2v", "t2v", "image", "edit", "music", "voice", "voice_clone", "talk",
                                              "scene_image", "edit_ref", "i2v_end", "compose_ref"}

# pick: edit_ref 는 edit 와 따로 고른다 (qwen_image_edit 은 그대로)
assert R.pick(rs, set(), "edit", "fast")["id"] == "qwen_image_edit"
assert R.pick(rs, set(), "edit", "high")["id"] == "qwen_image_edit_quality"
assert R.pick(rs, set(), "edit_ref", "fast")["id"] == "qwen_image_edit_ref"
assert R.pick(rs, set(), "edit_ref", "high")["id"] == "qwen_image_edit_ref_quality"
assert R.pick(rs, set(), "i2v_end", "fast", "ltx")["id"] == "ltx25_i2v_sound_end"
assert R.pick(rs, set(), "i2v_end", "high", "ltx")["id"] == "ltx23_dev_i2v_sound_quality_end"
assert R.pick(rs, set(), "i2v_end", "fast") is None                      # wan 에는 끝 키프레임 레시피가 없다
assert R.pick(rs, {"qwen_image_edit_ref"}, "edit_ref", "fast") is None
assert R.pick(rs, set(), "compose_ref", "fast")["id"] == "qwen_image_compose_ref"
assert R.pick(rs, set(), "compose_ref", "high")["id"] == "qwen_image_compose_ref_quality"
assert R.pick(rs, {"qwen_image_compose_ref"}, "compose_ref", "fast") is None
assert R.pick(rs, set(), "i2v", "fast", "ltx")["id"] == "ltx25_i2v_sound"    # 옛 i2v 는 그대로

# edit_ref: 높음 변형은 가속 스위치만 끈다
er = rs["qwen_image_edit_ref"]
assert rs["qwen_image_edit_ref_quality"]["prompt"]["170:168"]["inputs"]["value"] is False
assert er["prompt"]["170:168"]["inputs"]["value"] is True and er["version"] == 1
assert er["estimate"]["gpu_seconds"] == 17
assert set(er["inject"]) == {"prompt", "edit_image", "ref_image", "ref_image2", "seed", "save_prefix"}
assert er["inject"]["ref_image"]["optional"] and er["inject"]["ref_image2"]["optional"] and not er["inject"]["edit_image"].get("optional")
assert "ref_image" not in rs["qwen_image_edit"]["inject"] and "42" not in rs["qwen_image_edit"]["prompt"]       # 기존 편집 레시피는 그대로
ev = {"prompt": "x", "edit_image": "studio/u/a.png", "seed": 1, "save_prefix": ["bob/abcd1234/image"]}
TEXTS = ("170:151", "170:149")


def dangling(prompt):
    return [(nid, k) for nid, n in prompt.items() for k, v in n["inputs"].items() if R.is_link(v) and str(v[0]) not in prompt]


for rid in ("qwen_image_edit_ref", "qwen_image_edit_ref_quality"):
    r = rs[rid]
    full = R.build(r, {**ev, "ref_image": "studio/u/b.png", "ref_image2": "studio/u/c.png"})
    assert full["42"]["inputs"]["image"] == "studio/u/b.png" and full["43"]["inputs"]["image"] == "studio/u/c.png" and not dangling(full)
    for t in TEXTS:
        assert full[t]["inputs"]["image2"] == ["42", 0] and full[t]["inputs"]["image3"] == ["43", 0]
    # 키가 없거나 None 이면 비운 것: 노드와 링크가 모두 사라지고 끊어진 링크가 없다
    for vals in (ev, {**ev, "ref_image": None, "ref_image2": None}):
        none = R.build(r, vals)
        assert "42" not in none and "43" not in none and not dangling(none), dangling(none)
        for t in TEXTS:
            assert "image2" not in none[t]["inputs"] and "image3" not in none[t]["inputs"] and none[t]["inputs"]["image1"] == ["170:160", 0]
    # 한 장만 채우면 그 자리만 남는다
    one = R.build(r, {**ev, "ref_image": "studio/u/b.png"})
    assert "42" in one and "43" not in one and not dangling(one) and one["170:151"]["inputs"]["image2"] == ["42", 0]
    assert "image3" not in one["170:151"]["inputs"] and "image3" not in one["170:149"]["inputs"]
    assert "42" in r["prompt"] and "image2" in r["prompt"]["170:151"]["inputs"]      # 원본 레시피는 깎이지 않는다
# 비운 그래프는 qwen_image_edit 과 같다 (그림 한 장 편집)
assert R.build(er, ev) == R.build(rs["qwen_image_edit"], ev)
# 필수 자리는 값이 없으면 여전히 거부 (None 도 거부)
for k in ("edit_image", "prompt", "seed"):
    for vals in ({kk: vv for kk, vv in ev.items() if kk != k}, {**ev, k: None}):
        try:
            R.build(er, vals)
            raise AssertionError(f"{k} 가 비면 거부")
        except R.RecipeError:
            pass
try:
    R.build(er, {**ev, "ref_image": "../x.png"})
    raise AssertionError("참고 그림 경로도 검사한다")
except R.RecipeError:
    pass
# sample_values 는 optional 자리도 채우고, 채운 값과 비운 값 둘 다 build 가 된다
sv = R.sample_values(er)
assert sv["ref_image"] == R.SAMPLE_IMAGE and sv["ref_image2"] == R.SAMPLE_IMAGE and "end_frame" not in sv
assert "42" in R.build(er, sv) and "42" not in R.build(er, {k: v for k, v in sv.items() if not k.startswith("ref_")})
# V6 (edit_ref): 참고 그림이 글 노드 둘 다의 image2/image3 에 닿아야 한다
x = copy.deepcopy(er); del x["prompt"]["170:149"]["inputs"]["image2"]
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(er); del x["prompt"]["170:151"]["inputs"]["image3"]
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(er); x["prompt"]["170:149"]["inputs"]["image1"] = ["42", 0]       # 편집 대상이 부정 글 노드 image1 에 안 닿음
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(er); del x["inject"]["ref_image2"]
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(er); x["inject"]["edit_image"]["optional"] = True               # 편집 대상은 비울 수 없다
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(er); x["inject"]["seed"]["optional"] = True                     # optional 은 이미지 자리만
assert "V2" in bad(R.check_recipe(x))
x = copy.deepcopy(er); x["prompt"]["44"] = {"class_type": "LoadImage", "inputs": {"image": "example.png"}}
assert "V9" in bad(R.check_recipe(x))
x = copy.deepcopy(rs["qwen_image_edit"]); x["inject"]["ref_image"] = {"node": "41", "input": "image", "type": "input_image"}
assert "V6" in bad(R.check_recipe(x))                                              # edit 레시피에는 참고 그림 자리가 없다
assert not bad(R.check_recipe(rs["qwen_image_edit"]))                              # 옛 편집 레시피의 규칙은 그대로
x = copy.deepcopy(rs["qwen_image_edit"]); x["prompt"]["170:149"]["inputs"]["image1"] = ["42", 0]
x["prompt"]["42"] = {"class_type": "LoadImage", "inputs": {"image": "a.png"}}
assert "V9" in bad(R.check_recipe(x))

# compose_ref(11-20 새 방식): edit_ref 와 같은 모델·글 노드인데 KSampler 가 빈 잠재(영상 크기)에서 그린다
cr = rs["qwen_image_compose_ref"]
assert cr["version"] == 1 and cr["estimate"]["gpu_seconds"] == 17
assert set(cr["inject"]) == {"prompt", "edit_image", "ref_image", "ref_image2", "seed", "save_prefix", "width", "height"}
assert cr["prompt"]["170:169"]["inputs"]["latent_image"] == ["44", 0] and cr["prompt"]["44"]["class_type"] == "EmptySD3LatentImage"
assert not any(n["class_type"] == "VAEEncode" for n in cr["prompt"].values())
assert rs["qwen_image_compose_ref_quality"]["prompt"]["170:168"]["inputs"]["value"] is False
# 기준 그림 크기와 상관없이 영상 비율: 그림 1 사진 비율은 쓰지 않고 감독이 넘긴 크기 (기본은 1280×704)
assert R.default_size(cr) == (1280, 704) and R.default_size(cr, aspect="portrait")[0] < R.default_size(cr, aspect="portrait")[1]
for rid in ("qwen_image_compose_ref", "qwen_image_compose_ref_quality"):
    r = rs[rid]
    cv = {**ev, "width": 1280, "height": 704}
    full = R.build(r, {**cv, "ref_image": "studio/u/b.png", "ref_image2": "studio/u/c.png"})
    assert full["44"]["inputs"]["width"] == 1280 and full["44"]["inputs"]["height"] == 704 and not dangling(full)
    for t in TEXTS:
        assert full[t]["inputs"]["image1"] == ["170:160", 0] and full[t]["inputs"]["image2"] == ["42", 0] and full[t]["inputs"]["image3"] == ["43", 0]
    one = R.build(r, cv)                                     # 기준 그림 한 장만 (한 사람 장면)
    assert "42" not in one and "43" not in one and not dangling(one)
    try:
        R.build(r, {**cv, "width": 1000})                    # 16 단위가 아님
        raise AssertionError("width 단위 검사")
    except R.RecipeError:
        pass
# V6 (compose_ref): 잠재가 그림 1에서 오면(=고쳐 그리기) 거부, 크기 자리가 빈 잠재에 없으면 거부
x = copy.deepcopy(cr); x["prompt"]["170:156"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["170:160", 0], "vae": ["170:146", 0]}}
x["prompt"]["170:169"]["inputs"]["latent_image"] = ["170:156", 0]
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(cr); del x["inject"]["width"]
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(cr); x["prompt"]["170:149"]["inputs"]["image1"] = ["42", 0]
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(cr); del x["inject"]["ref_image"]
assert "V6" in bad(R.check_recipe(x))
x = copy.deepcopy(er); x["action"] = "compose_ref"                                  # 고쳐 그리는 그래프를 새로 그리기로 쓸 수 없다
assert "V6" in bad(R.check_recipe(x))

# i2v_end: 끝 프레임 가이드
for rid, base_id in (("ltx25_i2v_sound_end", "ltx25_i2v_sound"), ("ltx23_dev_i2v_sound_quality_end", "ltx23_dev_i2v_sound_quality")):
    r, o = rs[rid], rs[base_id]
    assert r["action"] == "i2v_end" and r["engine"] == o["engine"] and R.quality_of(r) == R.quality_of(o) and r["version"] == 1
    assert r["limits"] == o["limits"] and r["defaults"] == o["defaults"] and r["video_profile"] == o["video_profile"]
    assert set(r["inject"]) == set(o["inject"]) | {"end_frame"} and set(r["outputs"]) == set(o["outputs"]) == {"main", "check", "last"}
    assert not r["inject"]["end_frame"].get("optional") and r["inject"]["end_frame"]["type"] == "input_image"
    guides = [nid for nid, n in r["prompt"].items() if n["class_type"] == "LTXVAddGuide"]
    assert len(guides) == 2 and all(r["prompt"][g]["inputs"]["frame_idx"] == -1 and r["prompt"][g]["inputs"]["strength"] == 1.0 for g in guides)
    assert sum(1 for n in r["prompt"].values() if n["class_type"] == "LTXVCropGuides") == 2
    vals_e = R.sample_values(r)
    assert "end_frame" in vals_e and "first_frame" in vals_e and "last_index" not in vals_e
    en, fn = r["inject"]["end_frame"]["node"], r["inject"]["first_frame"]["node"]
    pe = R.build(r, {**vals_e, "end_frame": "studio/u/end.png", "first_frame": "studio/u/first.png"})
    assert pe[en]["inputs"]["image"] == "studio/u/end.png" and pe[fn]["inputs"]["image"] == "studio/u/first.png" and not dangling(pe)
    for vals in ({k: v for k, v in vals_e.items() if k != "end_frame"}, {**vals_e, "end_frame": None}):       # 끝 프레임은 필수
        try:
            R.build(r, vals)
            raise AssertionError("end_frame 이 비면 거부")
        except R.RecipeError:
            pass
    # V6: 두 LTXVAddGuide 의 image 가 끝 프레임에서 LTXVPreprocess 를 거쳐 와야 한다
    for g in guides:
        x = copy.deepcopy(r); x["prompt"][g]["inputs"]["image"] = [fn, 0]             # 첫 화면 그림을 가이드로 (끝 프레임이 안 닿음)
        assert "V6" in bad(R.check_recipe(x, PROFILES)), (rid, g)
    x = copy.deepcopy(r); x["inject"]["end_frame"]["node"] = guides[0]            # LoadImage 가 아님
    assert "V6" in bad(R.check_recipe(x, PROFILES)) or "V2" in bad(R.check_recipe(x, PROFILES))
    x = copy.deepcopy(r); del x["inject"]["end_frame"]
    assert "V6" in bad(R.check_recipe(x, PROFILES))
    x = copy.deepcopy(r); x["inject"]["end_frame"]["optional"] = True
    assert "V6" in bad(R.check_recipe(x, PROFILES))
    x = copy.deepcopy(r); x["prompt"][guides[1]]["class_type"] = "LTXVCropGuides"   # 가이드가 하나뿐
    assert "V6" in bad(R.check_recipe(x, PROFILES))
    x = copy.deepcopy(r); x["video_profile"] = "nope"
    assert "V1" in bad(R.check_recipe(x, PROFILES))
    x = copy.deepcopy(r); x["prompt"]["778"] = {"class_type": "LoadImage", "inputs": {"image": "example.png"}}
    assert "V9" in bad(R.check_recipe(x, PROFILES))
    x = copy.deepcopy(r); x["action"] = "i2v"                                      # end_frame 은 i2v_end 에서만
    assert "V6" in bad(R.check_recipe(x, PROFILES))
    x = copy.deepcopy(r); del x["inject"]["first_frame"]                           # 첫 화면 자리는 i2v_end 에도 필요
    assert "V6" in bad(R.check_recipe(x, PROFILES))
assert rs["ltx25_i2v_sound_end"]["estimate"]["gpu_seconds"] == 60 and rs["ltx23_dev_i2v_sound_quality_end"]["estimate"]["gpu_seconds"] == 98
# 옛 i2v 레시피는 end_frame 을 모른다
assert "end_frame" not in rs["ltx25_i2v_sound"]["inject"] and not any(n["class_type"] == "LTXVAddGuide" for n in rs["ltx25_i2v_sound"]["prompt"].values())

# 대사 레시피 판 3: 마지막 프레임 출력과 last_index (판 4: 부정 글에 카메라 움직임·얼굴 잘림, 11-24)
tk = rs["ltx23_ia2v_talk"]
assert "camera tilting down" in tk["prompt"]["340:314"]["inputs"]["text"] and "head cut off" in tk["prompt"]["340:314"]["inputs"]["text"]
assert tk["version"] == 4 and set(tk["outputs"]) == {"main", "check", "last"} and tk["outputs"]["last"]["node"] == "903"
assert tk["inject"]["last_index"] == {"node": "902", "input": "batch_index", "type": "int", "min": 0, "max": 480}
assert [s["suffix"] for s in tk["inject"]["save_prefix"]] == ["talk", "check_frame", "last_frame"]
assert tk["prompt"]["902"]["inputs"]["image"] == tk["prompt"]["900"]["inputs"]["image"] and tk["prompt"]["903"]["inputs"]["images"] == ["902", 0]
tv = R.sample_values(tk)
assert tv["last_index"] <= 480 and len(tv["save_prefix"]) == 3
tp = R.build(tk, {**tv, "last_index": 100})
assert tp["902"]["inputs"]["batch_index"] == 100 and tp["903"]["inputs"]["filename_prefix"].endswith("last_frame")
for idx in (-1, 481, 1.5, None):
    try:
        R.build(tk, {**tv, "last_index": idx})
        raise AssertionError(f"last_index={idx!r} 거부")
    except R.RecipeError:
        pass
try:
    R.build(tk, {k: v for k, v in tv.items() if k != "last_index"})
    raise AssertionError("last_index 는 필수")
except R.RecipeError:
    pass
x = copy.deepcopy(tk); x["inject"]["save_prefix"] = x["inject"]["save_prefix"][:2]   # 저장 노드 903 의 prefix 가 빠짐
assert "V7" in bad(R.check_recipe(x, PROFILES))
print("ok studio_recipes scene chaining")
print("ok studio_recipes")
