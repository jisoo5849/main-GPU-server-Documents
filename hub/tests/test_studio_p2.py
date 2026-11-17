"""P2 시험: 의도 규칙(5.3), Z-Image 이미지 레시피(6.8), 결과 → 입력 복사(7.3). DB·GPU·Pillow 없이 돈다.

  cd hub && python tests/test_studio_p2.py
"""
import copy
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.studio import files, intent, recipes as R  # noqa: E402

# ── 의도 규칙 ──
IMG = {"media": "image", "action": "image"}
VID = {"media": "video", "action": "i2v"}
VID_T2V = {"media": "video", "action": "t2v"}


def d(text, n=0, last=None, used_default=False, hint=None):
    return intent.decide(text, n, last, used_default, hint)


def rule(text, n=0, last=None, **kw):
    return d(text, n, last, **kw)["rule"]


# R0: 사진만 → i2v
r = d(intent.DEFAULT_I2V_TEXT, 1, None, used_default=True)
assert (r["rule"], r["action"], r["source"]) == ("R0", "i2v", "upload")
# R2: 인사·질문·보기 요청은 GPU 없이 답
for t in ("안녕", "고마워요", "도움말", "뭐 할 수 있어?", "왜 이렇게 나왔어?", "방금 그림 보여줘"):
    r = d(t, 0, IMG)
    assert r["rule"] == "R2" and r["action"] == "answer" and r["reply"], t
# 만들기 말이 있는 질문은 R2 가 아니다
assert rule("고양이를 그려줄 수 있어?") == "R9"
# R2b: 참고 말 + 첨부 + 영상·편집 말 없음 → 글만으로 이미지 + 안내 한 줄
r = d("이런 그림체로 고양이", 1)
assert (r["rule"], r["action"], r["attachment_role"]) == ("R2b", "image", "reference") and r["notes"]
# R3: 첨부 + 영상 말, R10: 첨부 + 모호한 말은 i2v (D19 기본값)
assert rule("영상으로 만들어줘", 1) == "R3" and rule("그냥 이렇게", 1) == "R10"
assert d("그냥 이렇게", 1)["action"] == "i2v"
# R4: 첨부 + 편집 말. 레시피가 없으면 api 가 '준비 중' 으로 끝낸다 (later 는 쓰지 않는다)
r = d("이 사진을 수채화 스타일로 바꿔줘", 1)
assert (r["rule"], r["action"], r["source"], r["later"]) == ("R4", "edit", "upload", False)
# R5: 이미지 결과 + 가리키는 말 + 영상 말 → i2v·last_result. 영상 결과 뒤는 아님
r = d("이걸로 영상 만들어줘", 0, IMG)
assert (r["rule"], r["action"], r["source"], r["later"]) == ("R5", "i2v", "last_result", False)
assert rule("이걸로 영상 만들어줘", 0, VID) == "R8"
# R6b 가 R6·R6a 보다 앞 ("하나 더" 의 '더' 가 고치는 말에 걸리지 않게)
for last in (IMG, VID):
    r = d("하나 더", 0, last)
    assert (r["rule"], r["carry"]) == ("R6b", "repeat"), last
assert d("하나 더", 0, IMG)["action"] == "image" and d("한 번 더", 0, VID)["action"] == "i2v"
# R6a: 이미지 결과 + 고치는 말 → image·revise. R6: 영상 결과 + 고치는 말 → P3
r = d("더 밝게", 0, IMG)
assert (r["rule"], r["action"], r["carry"], r["later"]) == ("R6a", "image", "revise", False)
r = d("더 천천히", 0, VID)
assert (r["rule"], r["action"], r["carry"], r["later"]) == ("R6", "i2v", "revise", False)
# R7: 이미지 결과 + 편집 말 → P3. R7b: 영상 결과 + 편집 말 → i2v 면 정해 둔 답, t2v 면 revise(P3)
r = d("배경만 노을로 바꿔줘", 0, IMG)
assert (r["rule"], r["action"], r["source"], r["later"]) == ("R7", "edit", "last_result", False)
r = d("배경 바꿔줘", 0, VID)
assert (r["rule"], r["action"], r["reply"]) == ("R7b", "answer", intent.ANSWER_I2V_EDIT)
r = d("배경 바꿔줘", 0, VID_T2V)
assert (r["rule"], r["action"], r["carry"], r["later"]) == ("R7b", "t2v", "revise", False)
# R8: 첨부 없음 + 영상 말 → t2v (P3). R9: 그 밖 → image
r = d("바다 위를 나는 드론 영상")
assert (r["rule"], r["action"], r["later"]) == ("R8", "t2v", False)
for t in ("비 오는 밤 골목의 작은 카페", "향수병 광고 사진", "니키가 나침반을 들고 있는 그림"):
    r = d(t)
    assert (r["rule"], r["action"], r["later"]) == ("R9", "image", False), t
# 카메라·속도 말은 사진이 있거나 직전 결과가 이미지일 때만 영상 말로 본다
hint = lambda t: "천천히" in t or "줌" in t  # noqa: E731
assert rule("천천히 흐르는 강물", 0, None, hint=hint) == "R9"
assert rule("천천히 흐르는 강물", 1, None, hint=hint) == "R3"
assert rule("이걸로 천천히 줌 인", 0, IMG, hint=hint) == "R5"
# 첫 규칙이 이긴다: 직전 결과가 없으면 "다시/더" 도 새 이미지
assert rule("더 밝게") == "R9"

# 검토에서 나온 오분류 (이미지 요청이 막히거나 엉뚱하게 가던 것)
assert rule("안녕하세요 라고 쓰인 간판이 있는 거리를 그려줘") == "R9"           # 인사말이 들어 있어도 만들 말이 있으면 이미지
assert rule("노을 지는 바다는 어떻게 생겼을까 그려줘") == "R9"
assert rule("안녕하세요라고 쓰인 간판이 있는 한적한 시골 거리의 낡은 가게 앞 풍경, 비 오는 저녁 분위기") == "R9"   # 긴 글은 R2 가 아니다
assert rule("감사하는 모습", 1) == "R10"                                          # 사진이 있으면 답으로 끝내지 않는다
assert rule("애니메이션 스타일 고양이 그림") == "R9"
assert rule("움직이는 느낌의 구름 그림") == "R9"
assert rule("클립보드를 든 사람") == "R9"
assert rule("애니메이션으로 만들어줘", 1) == "R3"
assert rule("영상으로 다시 만들어줘", 0, IMG) == "R5"                              # 같은 이미지를 다시 그리지 않고 영상으로
assert rule("배경에 벚꽃을 넣어서 그려줘", 0, IMG) == "R9"                          # 새 그림 요청이지 편집이 아니다
assert rule("배경에 벚꽃을 넣어줘", 0, IMG) == "R7"
assert rule("배경만 노을로 바꿔서 그려줘", 0, IMG) == "R7"                         # 강한 편집 말은 그려줘 가 있어도 편집

# P3 검토: 영상 뒤의 새 그림 요청을 고치기로 가로채지 않는다
VIDI = {"media": "video", "action": "i2v"}
assert rule("더 큰 파도가 치는 바다 그림 그려줘", 0, VIDI) == "R9"
assert rule("고양이 그림 다시 그려줘 더 귀엽게", 0, VIDI) == "R9"
assert rule("새로 고양이 영상 만들어줘 더 빠르게", 0, VIDI) == "R8"
assert rule("배경을 바꿔서 그려줘", 0, VIDI) == "R9"
assert rule("더 천천히", 0, VIDI) == "R6" and rule("더 천천히 만들어줘", 0, VIDI) == "R6"
assert rule("새로 더 밝게 그려줘", 0, IMG) == "R9"

# 글에 든 길이·비율·소리
sp = intent.extract_spec("세로로 3초 동안 소리 있게")
assert sp["seconds"] == 3.0 and sp["aspect"] == "portrait" and sp["sound"] and not sp["notes"]     # 소리는 이제 넣는다
sp = intent.extract_spec("10초짜리")
assert sp["seconds"] == 5.0 and any("5초까지" in n for n in sp["notes"])                          # 소리 없는 영상은 5초까지
sp = intent.extract_spec("10초짜리", intent.MAX_SECONDS_SOUND)
assert sp["seconds"] == 10.0 and not sp["notes"]
sp = intent.extract_spec("30초 영상", intent.MAX_SECONDS_SOUND)
assert sp["seconds"] == 20.0 and any("20초까지" in n for n in sp["notes"])
assert "한 번에 20초까지라 20초로 만들었습니다" in sp["notes"]
# 분 단위 길이와 긴 영상 (docs/long-video-design.md 7.1)
for txt, want in (("3분짜리 영상", 180.0), ("1분 30초 영상", 90.0), ("1분 반 영상", 90.0), ("90초 영상", 90.0),
                  ("2분 영상", 120.0), ("0.5분", 30.0), ("영상 만들어줘", None), ("이 부분을 충분히 보여줘", None),
                  ("3분만 만들어줘", 180.0), ("3분 동안 걷는 영상", 180.0), ("처음 10초는 천천히, 전체 3분 영상", 180.0),
                  # 길이가 아닌 '분' (11-17 검토): 시각·거리·비율·분할·분기·전후
                  ("해 지기 10분 전 바닷가 영상", None), ("역에서 5분 거리 공원 영상", None), ("화면의 3분의 1 을 하늘로", None),
                  ("4분할 화면 영상", None), ("1분기 실적 발표 영상", None), ("3시 15분 시계탑 영상", None),
                  ("10분 후 폭발하는 영상", None), ("5분마다 바뀌는 신호등", None), ("10초짜리 영상, 5분 거리 공원", 10.0),
                  ("3시 15분 시계탑, 20초 영상", 20.0)):
    assert intent.asked_seconds(txt) == want, (txt, intent.asked_seconds(txt))
sp = intent.extract_spec("비 오는 거리 3분 영상", intent.MAX_SECONDS_LONG)
assert sp["seconds"] == 180.0 and not sp["notes"]
sp = intent.extract_spec("5분짜리 영상", intent.MAX_SECONDS_LONG)
assert sp["seconds"] == 180.0 and "한 번에 3분까지라 3분으로 만들었습니다" in sp["notes"]     # 받침 있는 말 뒤는 '으로'
assert intent.is_long(30) and intent.is_long(180) and not intent.is_long(20) and not intent.is_long(None)
assert intent.long_label(180) == "3분" and intent.long_label(90) == "1분 30초" and intent.long_label(45) == "45초"
assert intent.clean_options({"seconds": 180}) == {"seconds": 180.0} and intent.clean_options({"seconds": "60"}) == {"seconds": 60.0}
sp = intent.apply_options(intent.extract_spec("5분 영상", intent.MAX_SECONDS_LONG), {"seconds": 60.0})
assert sp["seconds"] == 60.0 and not sp["notes"]                                    # 칸이 글보다 우선, '3분까지' 안내는 뺀다
# '소리 없이' 는 소리 없는 영상으로, '음악 없이' 는 소리 포함에서 음악만 뺀다(영상 렌더러가 따로 본다)
for t in ("소리 없이 만들어줘", "무음 영상", "소리는 필요 없어", "소리 빼고", "silent video", "소리 없는 영상 만들어줘", "소리 없는 클립으로"):
    assert intent.MUTE_RE.search(t), t
for t in ("빗소리가 들리는 영상", "음악 없이 만들어줘", "소리가 나는 영상"):
    assert not intent.MUTE_RE.search(t), t

# ── 로그인 첫 화면 (D1b) ──
import app.studio as studio_pkg  # noqa: E402

_old = (settings.studio_enabled, settings.studio_allowed_users)
try:
    settings.studio_enabled, settings.studio_allowed_users = True, []
    assert studio_pkg.wants_studio_home({"username": "bob", "role": "user", "home": "studio"})
    assert not studio_pkg.wants_studio_home({"username": "bob", "role": "user", "home": None})
    assert not studio_pkg.wants_studio_home({"username": "bob", "role": "user"})       # 열이 없던 옛 행
    assert not studio_pkg.wants_studio_home(None)
    settings.studio_allowed_users = ["kim"]
    assert not studio_pkg.wants_studio_home({"username": "bob", "role": "user", "home": "studio"})   # 시범 사용자가 아니면 ComfyUI
    assert studio_pkg.wants_studio_home({"username": "kim", "role": "user", "home": "studio"})
    assert studio_pkg.wants_studio_home({"username": "root", "role": "admin", "home": "studio"})
    settings.studio_enabled = False
    assert not studio_pkg.wants_studio_home({"username": "kim", "role": "user", "home": "studio"})   # 스튜디오가 꺼지면 ComfyUI
finally:
    settings.studio_enabled, settings.studio_allowed_users = _old

# ── 화면의 길이·비율 칸 (D17) ──
assert intent.clean_options(None) == {} and intent.clean_options({}) == {}
assert intent.clean_options({"seconds": "", "aspect": "auto"}) == {}
assert intent.clean_options({"seconds": 3, "aspect": "portrait"}) == {"seconds": 3.0, "aspect": "portrait"}
assert intent.clean_options({"seconds": "4"}) == {"seconds": 4.0}
assert intent.clean_options({"seconds": 20}) == {"seconds": 20.0} and intent.clean_options({"seconds": "10"}) == {"seconds": 10.0}
assert intent.clean_options({"sound": "on"}) == {"sound": "on"} and intent.clean_options({"sound": "off"}) == {"sound": "off"}
assert intent.clean_options({"sound": ""}) == {} and intent.clean_options({"sound": "auto"}) == {}
for bad_opt in ({"seconds": 10 ** 400}, {"seconds": 7}, {"seconds": 1}, {"seconds": 21}, {"seconds": 240}, {"seconds": "abc"},
                {"seconds": True},
                {"aspect": "wide"}, {"sound": "yes"}, {"sound": True}, "x", [1]):
    try:
        intent.clean_options(bad_opt)
        raise AssertionError(bad_opt)
    except intent.InputError as ex:
        assert ex.code == "bad_request"
base = intent.extract_spec("10초 세로로")                      # 글: 5초 넘음 안내 + 세로
assert base["seconds"] == 5.0 and base["aspect"] == "portrait" and base["notes"]
sp = intent.apply_options(base, {"seconds": 3.0, "aspect": "landscape"})   # 칸이 글보다 우선, 낡은 안내는 뺀다
assert sp["seconds"] == 3.0 and sp["aspect"] == "landscape" and not any("5초까지" in n for n in sp["notes"])
assert intent.apply_options(base, {}) == base
assert base["seconds"] == 5.0                                  # 원본은 바뀌지 않는다

# ── 소리 포함 영상 레시피 (LTX-2.5) ──
ltx = R.load_all()["ltx25_i2v_sound"]
assert R.engine_of(ltx) == "ltx" and R.engine_of(R.load_all()["wan22_i2v_14b_fast"]) == "wan" and ltx["sound"] is True
assert R.recipe_ok(R.check_recipe(ltx, {"ltx25_i2v_long"}))
# 긴 영상: LTX 레시피 둘은 마지막 프레임(끝에서 첫 프레임)을 따로 저장하고, 배경음악 레시피가 있다
for rid in ("ltx25_i2v_sound", "ltx23_dev_i2v_sound_quality"):
    r_ = R.load_all()[rid]
    assert r_["outputs"]["last"]["node"] == "903" and r_["prompt"]["902"]["inputs"]["batch_index"] == -1
    assert any(s["node"] == "903" and s["suffix"] == "last_frame" for s in r_["inject"]["save_prefix"])
mus = R.load_all()["ace_step_music"]
assert mus["action"] == "music" and R.recipe_ok(R.check_recipe(mus)) and R.pick(R.load_all(), set(), "music")["id"] == "ace_step_music"
assert R.pick(R.load_all(), {"ace_step_music"}, "music") is None
mv = R.sample_values(mus)
assert mv["duration"] == [60.0, 60.0] and mv["bpm"] == 90 and mv["keyscale"] == "C major" and "frames" not in mv
mb = R.build(mus, dict(mv, duration=[182.0, 182.0], prompt="calm piano instrumental"))
assert mb["98"]["inputs"]["seconds"] == 182.0 and mb["94"]["inputs"]["duration"] == 182.0 and mb["94"]["inputs"]["lyrics"] == "[Instrumental]"
assert mb["94"]["inputs"]["tags"] == "calm piano instrumental" and mb["111"]["inputs"]["filename_prefix"].endswith("/music")
assert R.quality_variant(ltx) is None and "ltx25_i2v_sound_quality" not in R.load_all()     # 가속 스위치가 없어 높음이 없다
vals = R.sample_values(ltx)
assert vals["seconds"] == 5 and "frames" not in vals and vals["check_index"] <= ltx["inject"]["check_index"]["max"]
built = R.build(ltx, dict(vals, seconds=20, width=1280, height=704))
assert built["398:362"]["inputs"]["value"] == 20 and built["398:372"]["inputs"]["value"] == 1280 and built["398:360"]["inputs"]["value"] == 704
assert R.default_size(ltx) == (1280, 704) and R.default_size(ltx, aspect="portrait") == (704, 1280)
assert R.default_size(ltx, photo=(1360, 768))[0] % 64 == 0 and R.default_size(ltx, photo=(1360, 768))[1] % 64 == 0
for bad_v in ({"seconds": 1}, {"seconds": 21}, {"width": 1000}):
    try:
        R.build(ltx, dict(vals, **bad_v))
        raise AssertionError(bad_v)
    except R.RecipeError:
        pass
# 엔진을 말하지 않으면 늘 소리 없는 Wan, 소리를 고르면 LTX
_all = R.load_all()
assert R.pick(_all, set(), "i2v")["id"] == "wan22_i2v_14b_fast" and R.pick(_all, set(), "i2v", "fast", "ltx")["id"] == "ltx25_i2v_sound"
assert R.pick(_all, set(), "i2v", "high")["id"] == "wan22_i2v_14b_fast_quality"
# 소리 포함 높음: 증류 없는 LTX-2.3 dev (1단계 30스텝·CFG 3.5, 2단계 보정은 증류 LoRA 3스텝)
assert R.pick(_all, set(), "i2v", "high", "ltx")["id"] == "ltx23_dev_i2v_sound_quality"
assert R.pick(_all, {"ltx23_dev_i2v_sound_quality"}, "i2v", "high", "ltx") is None       # 꺼지면 높음 없음 (빠른 레시피로 내려가는 건 감독이 한다)
ltxh = _all["ltx23_dev_i2v_sound_quality"]
assert R.engine_of(ltxh) == "ltx" and R.quality_of(ltxh) == "high" and ltxh["sound"] is True and ltxh["quality_label"] == "높음"
assert R.recipe_ok(R.check_recipe(ltxh, {"ltx25_i2v_long"})) and R.quality_variant(ltxh) is None
assert ltxh["prompt"]["320:S1G"]["inputs"]["cfg"] == 3.5 and ltxh["prompt"]["320:SCH"]["inputs"]["steps"] == 30     # 1단계는 증류 없이
assert ltxh["prompt"]["320:283"]["inputs"]["guider"] == ["320:S1G", 0] and ltxh["prompt"]["320:282"]["inputs"]["cfg"] == 1
assert ltxh["prompt"]["320:313"]["inputs"]["text"] and "cartoon" not in ltxh["prompt"]["320:313"]["inputs"]["text"]    # 만화풍 요청을 막지 않는다
lv = R.build(ltxh, dict(R.sample_values(ltxh), seconds=10, width=1280, height=704))
assert lv["320:301"]["inputs"]["value"] == 10 and lv["320:312"]["inputs"]["value"] == 1280 and lv["320:299"]["inputs"]["value"] == 704
assert ltxh["estimate"]["gpu_seconds"] > ltx["estimate"]["gpu_seconds"] and R.default_size(ltxh) == (1280, 704)
assert R.pick(_all, {"ltx25_i2v_sound"}, "i2v", "fast", "ltx") is None and R.pick(_all, set(), "t2v", "fast", "ltx") is None
assert R.pick(_all, set(), "image")["id"] == "zimage_turbo_t2i"

# ── 화질 높음 (가속 끔 변형) ──
all_r = R.load_all()
assert {"wan22_i2v_14b_fast_quality", "wan22_t2v_14b_fast_quality", "qwen_image_edit_quality"} <= set(all_r)
assert "zimage_turbo_t2i_quality" not in all_r                     # Z-Image 는 가속 스위치가 없어 높음이 없다
for rid in ("wan22_i2v_14b_fast", "wan22_t2v_14b_fast", "qwen_image_edit"):
    fast, hq = all_r[rid], all_r[rid + "_quality"]
    assert R.quality_of(fast) == "fast" and R.quality_of(hq) == "high" and hq["derived_from"] == rid
    assert fast["fixed"]["turbo"]["value"] is True and hq["fixed"]["turbo"]["value"] is False
    tn, ti = hq["fixed"]["turbo"]["node"], hq["fixed"]["turbo"]["input"]
    assert hq["prompt"][tn]["inputs"][ti] is False and fast["prompt"][tn]["inputs"][ti] is True    # 원본은 그대로
    assert hq["inject"] == fast["inject"] and hq["outputs"] == fast["outputs"] and hq["action"] == fast["action"]
    scale = 6 if fast["action"] in ("i2v", "t2v") else 5          # 영상은 해상도도 올려 더 걸린다
    assert hq["estimate"]["gpu_seconds"] == fast["estimate"]["gpu_seconds"] * scale and hq["limits"]["max_run_seconds"] >= 3600
    assert all(x["ok"] for x in R.check_recipe(hq, {"wan22_i2v_long", "wan22_t2v_long"})), rid
    assert hq["quality_label"] == "높음" and "높음" in hq["title"]
    sv = R.sample_values(hq)
    R.build(hq, sv)
# 부정 프롬프트가 있는 Wan 은 엉뚱한 물체·사람을 막는 말이 더해진다 (CFG>1 이라 실제로 적용된다). 부정이 빈 편집은 그대로
wt = all_r["wan22_t2v_14b_fast"]["fixed"]["negative"]
assert all_r["wan22_t2v_14b_fast_quality"]["prompt"][wt["node"]]["inputs"][wt["input"]].endswith(R.QUALITY_NEGATIVE_EXTRA)
assert not all_r["wan22_t2v_14b_fast"]["prompt"][wt["node"]]["inputs"][wt["input"]].endswith(R.QUALITY_NEGATIVE_EXTRA)
qn = all_r["qwen_image_edit"]["fixed"]["negative"]
assert all_r["qwen_image_edit_quality"]["prompt"][qn["node"]]["inputs"][qn["input"]] == ""
# 높음의 Wan 은 해상도도 올린다 (1024×576, 세로·정사각도 같은 면적). 빠른 레시피와 편집은 그대로
hqw = all_r["wan22_t2v_14b_fast_quality"]
assert R.default_size(hqw) == (1024, 576) and R.default_size(hqw, aspect="portrait") == (576, 1024) and R.default_size(hqw, aspect="square") == (768, 768)
assert R.default_size(all_r["wan22_t2v_14b_fast"]) == (848, 480) and R.default_size(all_r["wan22_i2v_14b_fast_quality"]) == (1024, 576)
assert max(R.default_size(hqw, photo=(1000, 10))) <= 1024 and "width" not in all_r["qwen_image_edit_quality"]["defaults"]
# 파생 레시피를 다시 파생하지 않고, 지문은 내용으로 안정적으로 만든다
assert R.quality_variant(all_r["wan22_t2v_14b_fast_quality"]) is None and R.quality_variant(all_r["zimage_turbo_t2i"]) is None
assert R.raw_bytes(all_r["wan22_t2v_14b_fast_quality"]) == R.raw_bytes(R.load_all()["wan22_t2v_14b_fast_quality"])
assert R.raw_bytes(all_r["wan22_t2v_14b_fast"]) != R.raw_bytes(all_r["wan22_t2v_14b_fast_quality"])
# 화질에 맞는 레시피 고르기
assert R.pick(all_r, set(), "t2v")["id"] == "wan22_t2v_14b_fast" and R.pick(all_r, set(), "t2v", "high")["id"] == "wan22_t2v_14b_fast_quality"
assert R.pick(all_r, {"wan22_t2v_14b_fast_quality"}, "t2v", "high") is None and R.pick(all_r, set(), "image", "high") is None
# 화면의 화질 칸
assert intent.clean_options({"quality": "high"}) == {"quality": "high"}
assert intent.clean_options({"quality": ""}) == {} and intent.clean_options({"quality": "fast"}) == {} and intent.clean_options({"quality": "auto"}) == {}
for bad_q in ("ultra", "HIGH", 1, True):
    try:
        intent.clean_options({"quality": bad_q})
        raise AssertionError(bad_q)
    except intent.InputError as ex:
        assert ex.code == "bad_request"
assert intent.apply_options({"seconds": None}, {"quality": "high"})["quality"] == "high"
assert "quality" not in intent.apply_options({"seconds": None}, {})

# ── Z-Image 이미지 레시피 ──
rs = R.load_all()
zi = rs["zimage_turbo_t2i"]


def bad(report):
    return {x["id"] for x in report if not x["ok"]}


rep = R.check_recipe(zi)
assert not bad(rep), [x for x in rep if not x["ok"]]
assert zi["action"] == "image" and zi["negative_source"] == "zero_out" and "fixed" in zi and not zi["fixed"]
assert R.roles_of(zi["prompt"], "57:27") == {"positive", "negative_zero"}
# 0 조건 노드를 떼면 V5, 글 노드가 positive 에 안 닿으면 V4
x = copy.deepcopy(zi); del x["prompt"]["57:33"]; x["prompt"]["57:3"]["inputs"]["negative"] = ["57:27", 0]
assert "V5" in bad(R.check_recipe(x))
x = copy.deepcopy(zi); x["prompt"]["57:3"]["inputs"]["positive"] = ["57:33", 0]
assert "V4" in bad(R.check_recipe(x))
# 샘플러가 띠에 없으면 V13, 저장 prefix 가 빠지면 V7
x = copy.deepcopy(zi); x["progress"]["bands"][1]["sampler"] = False
assert "V13" in bad(R.check_recipe(x))
x = copy.deepcopy(zi); x["inject"]["save_prefix"] = []
assert "V7" in bad(R.check_recipe(x))
# 값 넣기
vals = {"prompt": "a small cafe in the rain", "seed": 7, "width": 1024, "height": 1024, "save_prefix": ["bob/abcd1234/image"]}
p = R.build(zi, vals)
assert p["57:27"]["inputs"]["text"] == "a small cafe in the rain" and p["57:3"]["inputs"]["seed"] == 7
assert p["9"]["inputs"]["filename_prefix"] == "bob/abcd1234/image"
for k, v in (("width", 1552), ("width", 100), ("height", 1001), ("prompt", "")):
    try:
        R.build(zi, {**vals, k: v})
        raise AssertionError(f"{k}={v!r}")
    except R.RecipeError:
        pass
assert R.default_size(zi) == (1024, 1024)
w, h = R.default_size(zi, aspect="portrait")
assert w < h and w % 16 == 0 and h % 16 == 0 and max(w, h) <= 1536, (w, h)
assert R.default_size(zi, aspect="landscape")[0] > R.default_size(zi, aspect="landscape")[1]

# ── 결과 → 입력 복사 (7.3) ──
tmp = tempfile.mkdtemp()
settings.results_dir = os.path.join(tmp, "results")
settings.envs_dir = os.path.join(tmp, "envs")
rel = "studio/chat/2026-11-13/bob/abcd1234/image_00001_.png"
os.makedirs(os.path.dirname(os.path.join(settings.results_dir, rel)))
data = b"\x89PNG\r\n\x1a\n" + b"x" * 100
with open(os.path.join(settings.results_dir, rel), "wb") as f:
    f.write(data)
n, sha = files.fingerprint(rel)
assert n == len(data) and sha == hashlib.sha256(data).hexdigest()
assert files.fingerprint("studio/chat/nope.png") == (None, None)
assert files.fingerprint("../../etc/passwd") == (None, None)       # 결과 폴더 밖은 읽지 않는다

files.process_image = lambda b: (b, 640, 480)          # Pillow 없는 개발 PC 용
if not hasattr(os, "chown"):                        # Windows 개발 PC 에는 없다 (메인은 Linux)
    os.chown = lambda *a, **k: None
env = {"slug": "studio-chat", "linux_uid": 0}
aid, irel, isha, w, h, nb = files.copy_result_as_input(env, "bob", 7, rel, sha)
assert irel == f"studio/bob/{aid}.png" and (w, h) == (640, 480) and isha == sha
assert os.path.isfile(files.input_path(env, irel))
try:
    files.copy_result_as_input(env, "bob", 7, rel, "0" * 64)       # 파일이 바뀐 경우
    raise AssertionError("바뀐 파일은 거부")
except files.UploadError as e:
    assert e.code == "changed" and e.status == 409
try:
    files.copy_result_as_input(env, "bob", 7, rel, None)           # 지문이 없는 결과는 바뀌었는지 알 수 없어 복사하지 않는다
    raise AssertionError("지문 없으면 거부")
except files.UploadError as e:
    assert e.code == "changed"
try:
    files.copy_result_as_input(env, "bob", 7, "studio/chat/none.png", sha)
    raise AssertionError("없는 파일은 거부")
except files.UploadError as e:
    assert e.code == "gone"
# ── 편집 지시문 (5.5) ──
import asyncio  # noqa: E402
import json  # noqa: E402

from app.studio import edit  # noqa: E402

GOOD = "Replace the background with a warm sunset sky and keep the person, pose and lighting on the person unchanged."
assert edit.check(GOOD) == []
assert any("English only" in p for p in edit.check("배경을 노을로 바꾸기. Replace the background with a warm sunset sky now"))
assert any("negative" in p for p in edit.check("Replace the background with a sunset sky and do not change the person at all"))
assert any("text" in p.lower() for p in edit.check("Replace the background with a sunset sky and add a big text logo on the wall"))
assert any("words" in p for p in edit.check("Make it warm."))
assert any("words" in p for p in edit.check(" ".join(["word"] * 70)))
assert edit.check("") == ["The instruction is empty."]
SIGN = "Replace the sign on the shop with a wooden plank and keep the shop front unchanged."
assert any("text" in p.lower() for p in edit.check(SIGN, "가게를 더 예쁘게 바꿔줘"))
assert edit.check(SIGN, "간판을 나무판으로 바꿔줘") == []                  # 사용자가 간판을 말했으면 허용
m = edit.build_messages("배경만 노을로", {"subject": "a girl", "environment": "street"})
assert "a girl" in m[1]["content"] and "배경만 노을로" in m[1]["content"] and m[0]["role"] == "system"


async def run_edit(replies):
    seen = []

    async def call(payload):
        seen.append(payload)
        return {"message": {"content": json.dumps({"prompt": replies[min(len(seen) - 1, len(replies) - 1)]})}}
    return await edit.write_edit_in_turn(call, "배경만 노을로 바꿔줘"), seen


r, seen = asyncio.run(run_edit([GOOD]))
assert r["prompt"] == GOOD and r["violations"] == [] and r["rounds"] == 1 and len(seen) == 1
r, seen = asyncio.run(run_edit(["배경 노을", GOOD]))                 # 걸리면 한 번 고쳐 쓴다
assert r["prompt"] == GOOD and r["violations"] == [] and r["rounds"] == 2 and len(seen) == 2
assert "Fix these problems" in seen[1]["messages"][-1]["content"]
r, seen = asyncio.run(run_edit(["배경 노을", "여전히 한글"]))         # 그래도 걸리면 위반만 남기고 끝낸다
assert r["violations"] and r["rounds"] == 2 and len(seen) == 2
print("ok studio_p2")
