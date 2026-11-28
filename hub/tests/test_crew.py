"""제작진 모드(crew)·라이브러리(crew_lib) 단위 테스트. GPU·DB 없이 돈다.

  cd hub && python tests/test_crew.py

검사 예시는 실제로 나온 실패다: G0 V04 신발 광고가 사람 다리 컷, 11-11 'SF 영화처럼' 요청에서 SF 가 사라짐,
'눈 오는 날' 이 '비 오는 날' 로, 제품 광고 4건이 모두 '어두운 프리미엄' 으로 쏠림.
"""
import asyncio
import copy
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crew, crew_lib  # noqa: E402

GOOD = {
    "choices": {"presentation": "product_presentation.front_single", "background": "location_set.dark_premium",
                "lighting": "lighting.rim_backlight", "camera": "camera.eye_level_medium",
                "color": "color_palette.warm_orange", "mood": "mood_emotion.luxurious", "genre": ["genre_style.none"]},
    "ko": {"plan": "고급스러운 향수 광고입니다.", "direction": "병을 화면 중앙에 정면으로 둡니다.",
           "camera_lighting": "역광과 림 라이트로 윤곽을 살립니다.", "details": "유리의 반사와 금속 캡의 질감을 보여 줍니다."},
    "notes_en": ["A single perfume bottle centered in the frame", "Dark gradient studio backdrop",
                 "Soft backlight with thin rim light on the glass edges", "Crisp reflections on the glass and metal cap"],
}
SF = "SF 영화처럼 보이는 향수 광고"
# 가짜 LLM 이 받은 메뉴(스키마 enum)의 첫 항목을 고르게 하는 표시. 초안 검사는 고른 id 가 그 요청의 메뉴 안인지 본다.
FIT = dict(GOOD, fit_menu=True)


def fit_reply(reply, payload):
    reply = copy.deepcopy(reply)
    if isinstance(reply, dict) and reply.pop("fit_menu", False) and isinstance(payload.get("format"), dict):
        props = payload["format"]["properties"]["choices"]["properties"]
        reply["choices"] = {k: ([v["items"]["enum"][0]] if v["type"] == "array" else v["enum"][0]) for k, v in props.items()}
    return reply


# ── 지식 파일·라이브러리 ────────────────────────────────────────────────
def test_knowledge_and_library_integrity():
    kn = crew.load_knowledge()
    assert set(kn["categories"]) == {"product", "space", "landscape"}
    for cat, spec in kn["categories"].items():
        assert spec["keywords"] and spec["rule"]
        keys = [st["key"] for st in spec["steps"]]
        assert len(keys) == len(set(keys)) >= 6 and "genre" in keys, cat
        for st in spec["steps"]:
            assert st["facet"] in crew_lib.FACETS
            pool = [i for i in crew_lib.load_library()["by_facet"][st["facet"]]
                    if crew_lib.fits(crew_lib.entry(i), cat)]
            assert len(pool) >= 4, (cat, st["key"], len(pool))
    lib = crew_lib.load_library()
    assert crew_lib.NONE_GENRE in lib["entries"]
    for e in lib["entries"].values():
        assert e["facet"] in crew_lib.FACETS and e["en"] == e["en"].lower()
        assert all(t in e["en"] for t in e["terms"]), e["id"]


def test_route_known_fields():
    cases = {"향수 광고 만들어 줘": "product", "신발 광고": "product", "고급 시계 광고": "product",
             "새로 연 카페 홍보 이미지": "space", "따뜻한 느낌의 집 안 풍경": "space",
             "비 오는 날 분위기 사진": "landscape", "여행 포스터 같은 바다 풍경": "landscape",
             "눈 오는 날 거리 풍경": "landscape", SF: "product"}
    for text, cat in cases.items():
        rt = crew.route(text)
        assert rt and rt["category"] == cat, (text, rt)


def test_route_skips_character_person_and_ambiguous():
    assert crew.route("니키가 나오는 신비로운 장면") is None
    assert crew.route("niki in a cafe interior") is None
    assert crew.route("손은 소매 속에 숨기고 신발 광고") is None
    assert crew.route("책상 위 고양이") is None
    assert crew.route("베스트 향수 광고")["category"] == "product"
    for text in ("비 오는 날 우산을 쓴 소녀", "아이스크림 먹는 고양이", "고양이가 방향을 바라본다",
                 "seated girl reading a book", "bathroom mushroom", "우주 방랑자", "마법사가 숲속에서 마법을 쓴다"):
        assert crew.route(text) is None, text
    assert crew.route("방에서 찍은 사진")["category"] == "space"
    assert crew.route("하얀 shoes 광고")["category"] == "product"


def test_guard_names_need_whole_words():
    assert crew.route("베스킨라빈스 매장 홍보 이미지")["category"] == "space"
    assert crew.skip_reason("니키타 영화 포스터 카페") is None
    assert crew.skip_reason("니키가 카페에 있는 장면") == "character"
    assert crew.skip_reason("niki in a cafe") == "character"
    assert crew.skip_reason("아이스크림 병 광고") is None
    assert crew.skip_reason("아이가 신발을 신은 장면") == "person"


def test_route_purpose_words():
    assert "포스터" in crew.route("여행 포스터 같은 바다 풍경")["purposes"]
    assert not crew.route("고급 시계 광고")["purposes"].get("포스터")


# ── 사용자 의도 최우선 ───────────────────────────────────────────────────
def test_user_demands_from_library_tags():
    d = crew_lib.user_demands(SF)
    assert any(x["facet"] == "genre_style" and "genre_style.sci_fi" in x["ids"] for x in d), d
    snow = crew_lib.user_demands("눈 오는 날 거리 풍경")
    assert any(x["facet"] == "time_weather" and "time_weather.snowy" in x["ids"] for x in snow)
    assert crew_lib.user_demands("책상 위 고양이") == []


def test_sf_must_survive_in_choice_and_notes():
    rt = crew.route(SF)
    erased = copy.deepcopy(GOOD)                                  # 11-11: 메뉴 기본값이 SF 를 지운 초안
    probs = crew.check_crew(erased, "product", SF, SF)
    assert any("'sf'" in p and 'step "genre"' in p for p in probs), probs
    assert any("notes_en must clearly show" in p for p in probs)
    ok = copy.deepcopy(GOOD)
    # 'SF 영화처럼' 은 SF 와 영화(시네마틱) 두 가지 요구다. 장르는 요구마다 맞는 항목을 하나씩 고른다
    ds = [d for d in crew_lib.user_demands(SF) if d["facet"] == "genre_style"]
    assert {d["tag"] for d in ds} >= {"sf", "영화처럼"}
    ok["choices"]["genre"] = ["genre_style.sci_fi"] + [d["ids"][0] for d in ds if "genre_style.sci_fi" not in d["ids"]]
    ok["choices"]["mood"] = "mood_emotion.futuristic"
    ok["notes_en"] = ["A perfume bottle in a futuristic sci-fi lab", "Neon accents glowing on the glass edges",
                      "A cinematic widescreen frame with sleek metallic surfaces"]
    assert crew.check_crew(ok, "product", SF, SF) == [], crew.check_crew(ok, "product", SF, SF)
    menus = crew.build_menus(SF, rt, random.Random(3))
    assert "genre_style.sci_fi" in [e["id"] for e in menus["genre"]]   # 요구 항목은 메뉴에 반드시 들어간다
    user = crew.build_messages(SF, rt, menus=menus)[1]["content"]
    assert "USER WORDS" in user and "'sf'" in user


def test_snow_must_not_become_rain():
    rt = crew.route("눈 오는 날 거리 풍경")
    menus = crew.build_menus("눈 오는 날 거리 풍경", rt, random.Random(1))
    assert "time_weather.snowy" in [e["id"] for e in menus["time_weather"]]
    base = {"choices": {"setting": menus["setting"][0]["id"], "time_weather": "time_weather.rainy",
                        "composition": menus["composition"][0]["id"], "color": menus["color"][0]["id"],
                        "atmosphere": menus["atmosphere"][0]["id"], "mood": menus["mood"][0]["id"],
                        "genre": ["genre_style.none"]},
            "ko": GOOD["ko"], "notes_en": ["A quiet street under drizzle", "Wet ground with soft reflections",
                                           "Layered buildings fading into pale blue"]}
    probs = crew.check_crew(base, "landscape", "눈 오는 날 거리 풍경")
    assert any('step "time_weather"' in p for p in probs) and any("notes_en must clearly show" in p for p in probs)
    snow = copy.deepcopy(base)
    snow["choices"]["time_weather"] = "time_weather.snowy"
    snow["notes_en"] = ["A quiet street under falling snow", "White ground with soft muffled light",
                        "Layered buildings fading into pale blue"]
    assert crew.check_crew(snow, "landscape", "눈 오는 날 거리 풍경") == []


def test_helper_also_honors_user_words():
    from app import promptx
    good = {"prompt": ("a single perfume bottle centered on a dark reflective surface, soft rim light outlining the glass, "
                       "close-up shot at eye level with a 100mm lens, a dark gradient studio backdrop, polished glass with "
                       "crisp reflections and brushed metal texture, calm luxurious mood, fine dust floating in the light"),
            "keep": ["perfume bottle"],
            "categories": {"lighting": "soft rim light outlining the glass", "camera": "close-up shot at eye level with a 100mm lens",
                           "environment": "a dark gradient studio backdrop",
                           "texture": "polished glass with crisp reflections and brushed metal texture"}, "people": 0}
    probs = promptx.check_expansion(good, "", SF, request_text=SF)
    assert any("'sf'" in p for p in probs), probs                  # 도우미도 SF 가 빠지면 고쳐 쓰게 한다
    sf = dict(good, prompt=good["prompt"] + ", futuristic sci-fi city glow behind the bottle")
    assert not any("'sf'" in p for p in promptx.check_expansion(sf, "", SF, request_text=SF))
    msgs = promptx.build_expand_messages(SF, "", "", rng=random.Random(2))
    assert "USER WORDS" in msgs[1]["content"] and "Optional ideas" in msgs[1]["content"]
    assert "user's words come first" in msgs[0]["content"]
    real = dict(good, prompt=good["prompt"] + ", photorealistic")   # 사용자가 실사를 원하면 그림체 충돌 말도 허용
    assert any('"photorealistic"' in p for p in promptx.check_expansion(real, "A painted look", "향수 광고"))
    assert not any('"photorealistic"' in p for p in promptx.check_expansion(real, "A painted look", "실사 향수 광고"))


# ── 다양성 ────────────────────────────────────────────────────────────
def test_menus_vary_and_are_not_stuck_on_dark_premium():
    rt = crew.route("향수 광고 만들어 줘")
    seen_bg, seen_order = set(), set()
    for s in range(12):
        menus = crew.build_menus("향수 광고 만들어 줘", rt, random.Random(s))
        ids = [e["id"] for e in menus["background"]]
        seen_bg.update(ids)
        seen_order.add(tuple(ids))
    assert len(seen_order) > 6                     # 매번 다른 메뉴
    assert len(seen_bg) >= 5                       # 여러 배경이 돌아가며 나온다
    system = crew.build_messages("향수 광고", rt)[0]["content"]
    assert "do not default to a dark premium studio look" in system


def test_retrieve_puts_must_first_and_adds_none_genre():
    ents = crew_lib.retrieve("네온 사이버펑크 도시", "genre_style", k=6, rng=random.Random(0))
    assert len(ents) >= 6 and crew_lib.NONE_GENRE in [e["id"] for e in ents]
    must = crew_lib.retrieve("향수 광고", "lighting", k=5, rng=random.Random(0), must=["lighting.neon_glow"])
    assert "lighting.neon_glow" in [e["id"] for e in must]


# ── 초안 검사 (기존 규칙) ─────────────────────────────────────────────
def test_good_draft_passes():
    assert crew.check_crew(GOOD, "product", "향수 광고 만들어 줘") == []


def test_invalid_choice_and_missing_step():
    bad = copy.deepcopy(GOOD)
    bad["choices"]["background"] = "location_set.no_such_option"
    del bad["choices"]["mood"]
    bad["choices"]["genre"] = []
    probs = crew.check_crew(bad, "product", "향수 광고")
    assert any('"background"' in p for p in probs) and any('"mood"' in p for p in probs)
    assert any('"genre"' in p for p in probs)


def test_person_text_negation_rules():
    bad = copy.deepcopy(GOOD)
    bad["notes_en"] = ["One person standing on a reflective floor", "Arms hanging at the sides", "Soft studio light"]
    assert any("names no person" in p for p in crew.check_crew(bad, "product", "신발 광고"))
    ok = copy.deepcopy(GOOD)
    ok["notes_en"] = ["Hour and minute hands on the watch face", "Polished steel case", "A sofa with a throw over one arm"]
    assert crew.check_crew(ok, "product", "시계 광고") == []
    logo = copy.deepcopy(GOOD)
    logo["notes_en"] = ["A wristwatch with the brand logo on the dial", 'Engraved "LUXE" text', "Dark premium backdrop"]
    assert any("logos" in p for p in crew.check_crew(logo, "product", "고급 시계 광고"))
    neg = copy.deepcopy(GOOD)
    neg["notes_en"] = ["A bottle without any clutter", "배경은 어둡게", "x " * 40]
    probs = crew.check_crew(neg, "product", "향수 광고")
    assert any('"without"' in p for p in probs) and any("English only" in p for p in probs)
    assert any("words" in p for p in probs)


def test_text_and_person_notes_keep_real_signs_and_skip_idioms():
    # of 가 붙은 진짜 간판은 계속 잡고, 'signs of wear'·'man-made' 같은 관용 표현은 넘긴다 (영상 장문 2차 검토, 11-11)
    signs = copy.deepcopy(GOOD)
    signs["notes_en"].append("The glowing neon signs of nearby bars reflect on the wet asphalt")
    assert any("logos" in p for p in crew.check_crew(signs, "product", "향수 광고"))
    ok = copy.deepcopy(GOOD)
    ok["notes_en"] += ["The metal cap shows faint signs of wear, the bottle resting on a man-made stone ledge",
                       "The slender figure of the bottle reads at human eye level"]          # 메모는 3~6줄
    assert crew.check_crew(ok, "product", "향수 광고") == [], crew.check_crew(ok, "product", "향수 광고")


def test_malformed_model_output_never_raises():
    for broken in ({"choices": {"presentation": ["x"]}, "ko": {}, "notes_en": 3},
                   {"choices": "x", "ko": [], "notes_en": None},
                   {"choices": {}, "ko": {"plan": 1}, "notes_en": [["nested"], None, {"a": 1}]}, [], "text", 7):
        probs = crew.check_crew(broken, "product", "향수 광고")
        assert probs and all(isinstance(p, str) for p in probs), broken


def test_schema_uses_the_menu_and_multi_genre():
    rt = crew.route("향수 광고")
    menus = crew.build_menus("향수 광고", rt, random.Random(5))
    sch = crew.build_schema("product", menus)
    props = sch["properties"]["choices"]["properties"]
    assert set(props) == {"presentation", "background", "lighting", "camera", "color", "mood", "genre"}
    assert props["genre"]["type"] == "array" and props["genre"]["maxItems"] == 3
    assert props["background"]["enum"] == [e["id"] for e in menus["background"]]
    json.dumps(sch)


def test_describe_and_merge_notes():
    d = crew.describe(GOOD, "product")
    assert crew_lib.entry("location_set.dark_premium")["ko"] == d["picked"][1]["ko"]
    assert d["ko"]["plan"].startswith("고급스러운")
    assert crew.merge_notes("유리 병 형태 유지.", GOOD).startswith("유리 병 형태 유지. A single perfume bottle")
    assert crew.merge_notes("사용자 특징", {"notes_en": []}) == "사용자 특징"


class Call:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, payload):
        self.calls.append(payload)
        return {"message": {"content": json.dumps(fit_reply(self.replies.pop(0), payload))}}


def test_draft_repairs_once_then_gives_up():
    rt = crew.route("신발 광고")
    person = dict(FIT, notes_en=["One person standing on the floor", "Arms at the sides", "Soft light"])
    call = Call([person, FIT])
    _, rounds, left = asyncio.run(crew.draft(call, "신발 광고", rt, repair_rounds=1, rng=random.Random(0)))
    assert left == [] and rounds == 2 and len(call.calls) == 2
    assert call.calls[1]["messages"][-1]["content"].startswith("Your JSON broke these rules")
    stubborn = Call([person, person])
    _, rounds, left = asyncio.run(crew.draft(stubborn, "신발 광고", rt, repair_rounds=1))
    assert left and rounds == 2


# ── promptx.expand 와 이어 붙이기 (가짜 llm) ─────────────────────────────
EXPANDED = {
    "prompt": ("a single luxury perfume bottle centered in a dark premium studio, soft backlight with thin rim light, "
               "close-up shot at eye level with a 100mm lens, a dark gradient studio backdrop, polished glass with crisp "
               "reflections and brushed metal texture, calm luxurious mood, fine dust floating in the light beam"),
    "keep": ["perfume bottle"],
    "categories": {"lighting": "soft backlight with thin rim light", "camera": "close-up shot at eye level with a 100mm lens",
                   "environment": "a dark gradient studio backdrop",
                   "texture": "polished glass with crisp reflections and brushed metal texture"},
    "people": 0,
}


def run_expand(replies, text, notes="", crew_flag=None):
    import contextlib
    import types

    calls = []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose):
        async def call(payload):
            calls.append(payload)
            return {"message": {"content": json.dumps(fit_reply(replies.pop(0), payload))}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        from app import promptx
        result = asyncio.run(promptx.expand({"id": 1}, "127.0.0.1", text, "", notes, crew_flag))
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved
    return result, calls


def test_expand_uses_crew_in_the_same_turn():
    result, calls = run_expand([FIT, EXPANDED], "향수 광고 만들어 줘")
    assert len(calls) == 2 and isinstance(calls[0]["format"], dict) and calls[1]["format"] == "json"
    assert "A single perfume bottle centered in the frame" in calls[1]["messages"][-1]["content"]
    assert "Optional ideas" not in calls[1]["messages"][-1]["content"]   # 초안을 쓰면 추가 아이디어는 주지 않는다
    c = result["crew"]
    assert c["used"] and c["category"] == "product"
    assert result["violations"] == []


def test_expand_skips_crew_and_gives_ideas():
    # 라이브러리에 없는 정본 이름은 지금도 건너뛴다 (니키는 11-26 부터 정본 장면 분야로 간다)
    result, calls = run_expand([EXPANDED], "퀼린스가 나오는 장면의 향수 광고")
    assert len(calls) == 1 and result["crew"] is None and any("캐릭터 정본" in a for a in result["advice"])
    result, calls = run_expand([EXPANDED], "향수 광고 만들어 줘", crew_flag=False)
    assert len(calls) == 1 and result["crew"] is None
    assert "Optional ideas" in calls[0]["messages"][-1]["content"]
    result, calls = run_expand([EXPANDED], "책상 위 고양이", crew_flag=True)
    assert len(calls) == 1 and not any("제작진" in a for a in result["advice"])


def test_expand_falls_back_when_crew_keeps_failing():
    person = dict(FIT, notes_en=["One person standing on the floor", "Arms at the sides", "Soft light"])
    result, calls = run_expand([person, person, EXPANDED], "신발 광고", notes="유리 병 형태 유지")
    assert len(calls) == 3 and result["crew"]["used"] is False
    assert "One person standing" not in calls[2]["messages"][-1]["content"]
    assert "유리 병 형태 유지" in calls[2]["messages"][-1]["content"]
    weird = {"choices": {"presentation": ["x"]}, "ko": {}, "notes_en": 3}
    result, calls = run_expand([weird, weird, EXPANDED], "향수 광고 만들어 줘")
    assert result["crew"]["used"] is False and result["violations"] == []

# ── 관련성 필터 (11-11 전체 라이브러리 점검에서 나온 문제) ─────────────────────────────
def test_menus_drop_people_other_products_and_homonyms():
    rt = crew.route("향수 광고 만들어 줘")
    for seed in range(6):
        menus = crew.build_menus("향수 광고 만들어 줘", rt, random.Random(seed))
        for key, ents in menus.items():
            for e in ents:
                assert not crew_lib.PEOPLE_EN_RE.search(e["en"]), (key, e["id"])        # 요청에 사람이 없다
                if e["facet"] in crew_lib.STYLE_FACETS:
                    assert "향수" not in e["ko"], (key, e["id"])                         # 향수(nostalgia) 동음이의어
                own = crew_lib._subjects_in(" ".join([e["ko"]] + e.get("tags_ko", [])))
                assert not own or "향수" in own, (key, e["id"], own)                      # 다른 제품 연출 없음


def test_named_place_drives_setting_menu():
    for req, groups in (("따뜻한 느낌의 집 안 풍경", ("home_room",)), ("새로 연 카페 홍보 이미지", ("cafe_food",)),
                        ("눈 오는 날 거리 풍경", ("urban_street", "seed_landscape"))):
        rt = crew.route(req)
        menus = crew.build_menus(req, rt, random.Random(4))
        inside = [e for e in menus["setting"] if e["group"].startswith(groups)]
        assert len(inside) == len(menus["setting"]) >= 6, (req, [e["group"] for e in menus["setting"]])


def test_dark_genres_only_when_asked():
    for req in ("바다가 보이는 풍경", "향수 광고 만들어 줘", "따뜻한 느낌의 집 안 풍경"):
        rt = crew.route(req)
        for seed in range(8):
            g = crew.build_menus(req, rt, random.Random(seed))["genre"]
            bad = [e["id"] for e in g if e.get("dark")]
            assert not bad, (req, seed, bad)
        for key, ents in crew.build_menus(req, rt, random.Random(0)).items():   # 다른 단계도 (전장 연기, 유령 저택 …)
            assert not [e["id"] for e in ents if e.get("dark")], (req, key)
    rt = crew.route("호러 영화 같은 향수 광고")
    g = crew.build_menus("호러 영화 같은 향수 광고", rt, random.Random(1))["genre"]
    assert sum(1 for e in g if "horror" in e["id"]) >= 2, [e["id"] for e in g]   # 사용자가 말하면 나온다


def test_mood_labels_match_category():
    rt = crew.route("바다가 보이는 풍경")
    for seed in range(8):
        for e in crew.build_menus("바다가 보이는 풍경", rt, random.Random(seed))["mood"]:
            assert not e["ko"].endswith(("(실내)", "(제품)", "(인물)", "(음식)", "(건축)")), e["ko"]


def test_intent_boosts_related_style_options():
    rt = crew.route("따뜻한 느낌의 집 안 풍경")
    menus = crew.build_menus("따뜻한 느낌의 집 안 풍경", rt, random.Random(4))
    warm = [e for e in menus["lighting"] if any(w in e["en"] for w in ("warm", "cozy", "golden"))]
    assert len(warm) >= 4, [e["en"] for e in menus["lighting"]]


def test_helper_ideas_skip_people_when_none_requested():
    from app import promptx
    for seed in range(5):
        lines = promptx.idea_lines("비 오는 날 분위기 사진", random.Random(seed))
        assert lines and not any(crew_lib.PEOPLE_EN_RE.search(l) for l in lines), lines



# ── 11-11 검토에서 나온 문제 ─────────────────────────────────────────────
def test_two_demands_in_one_single_step():
    """'눈 오는 밤' 은 날씨·시간 한 단계에 요구가 둘이다. 하나만 맞아도 선택은 통과, 메모에는 둘 다 보여야 한다."""
    req = "눈 오는 밤 거리 풍경"
    ds = [d for d in crew_lib.user_demands(req) if d["facet"] == "time_weather"]
    assert {d["tag"] for d in ds} == {"눈 오는", "밤"}, ds
    rt = crew.route(req)
    menus = crew.build_menus(req, rt, random.Random(2))
    offered = {e["id"] for e in menus["time_weather"]}
    assert all(offered & set(d["ids"]) for d in ds)                       # 두 요구 모두 메뉴에 들어간다
    snow_id = next(i for i in ds[0]["ids"] + ds[1]["ids"] if i in offered and "snow" in i)
    draft = {"choices": {k: (["genre_style.none"] if k == "genre" else v[0]["id"]) for k, v in menus.items()},
             "ko": GOOD["ko"], "notes_en": ["A quiet street at night under falling snow", "White ground under moonlit sky",
                                            "Layered buildings fading into deep blue"]}
    draft["choices"]["time_weather"] = snow_id
    assert crew.check_crew(draft, "landscape", req, req, menus) == [], crew.check_crew(draft, "landscape", req, req, menus)
    draft["notes_en"][0] = "A quiet street under falling snow"
    draft["notes_en"][1] = "White ground under a pale sky"
    probs = crew.check_crew(draft, "landscape", req, req, menus)
    assert any("'밤'" in p and "notes_en" in p for p in probs), probs  # 밤은 메모로 지켜야 한다


def test_terms_match_whole_words():
    assert not crew_lib.has_term("soft film grain, a bright rainbow", ["rain", "rainy"])   # grain·rainbow 는 비가 아니다
    assert crew_lib.has_term("a rainy street", ["rain"]) and crew_lib.has_term("heavy rains", ["rain"])
    assert crew_lib.has_term("snowy rooftops", ["snow"]) and not crew_lib.has_term("layered panels", ["red"])
    from app import promptx
    prompt = {"prompt": "a quiet street with film grain " * 10, "keep": ["street"],
              "categories": {"lighting": "film grain", "camera": "film grain", "environment": "quiet street",
                             "texture": "film grain"}, "people": 0}
    probs = promptx.check_expansion(prompt, "", "비 오는 날 거리", "비 오는 날 거리")
    assert any("'비 오는'" in p for p in probs), probs


def test_korean_adjective_forms_are_user_words():
    for text, tag in (("따뜻하고 아늑한 거실", "따뜻하고"), ("따뜻하게 보이는 카페", "따뜻하게")):
        assert any(d["tag"] == tag for d in crew_lib.user_demands(text)), (text, crew_lib.user_demands(text))
    assert "신비롭게" in crew_lib.ko_variants("신비로운") and "어둡게" in crew_lib.ko_variants("어두운")
    assert "푸르게" in crew_lib.ko_variants("푸른") and crew_lib.ko_variants("눈 오는") == []


def test_notes_field_words_are_user_words():
    rt = crew.route("향수 광고")
    user = crew.build_messages("향수 광고", rt, notes="SF 느낌")[1]["content"]
    assert "USER WORDS" in user and "'sf'" in user
    from app import promptx
    msgs = promptx.build_expand_messages("향수 광고", "", "crew notes in english", said="향수 광고 SF 느낌")
    assert "'sf'" in msgs[1]["content"]
    result, calls = run_expand([EXPANDED, EXPANDED, EXPANDED], "향수 광고", notes="SF 느낌", crew_flag=False)
    assert any("'sf'" in v for v in result["violations"]), result["violations"]   # 특징 칸의 SF 도 검사한다


def test_must_items_respect_filters():
    lib = crew_lib.load_library()
    person = next(i for i in lib["by_facet"]["lighting"] if crew_lib.PEOPLE_EN_RE.search(lib["entries"][i]["en"]))
    got = [e["id"] for e in crew_lib.retrieve("향수 광고", "lighting", k=5, rng=random.Random(0), must=[person])]
    assert person not in got                                              # 요구 항목이어도 요청에 없는 사람은 뺀다
    assert crew_lib.usable([person], "향수 광고", "product") == []


def test_menu_membership_is_checked():
    rt = crew.route("향수 광고 만들어 줘")
    menus = crew.build_menus("향수 광고 만들어 줘", rt, random.Random(1))
    draft = {"choices": {k: ([v[0]["id"]] if k == "genre" else v[0]["id"]) for k, v in menus.items()},
             "ko": GOOD["ko"], "notes_en": GOOD["notes_en"]}
    assert crew.check_crew(draft, "product", "향수 광고 만들어 줘", None, menus) == []
    outside = next(i for i in crew_lib.load_library()["by_facet"]["lighting"]
                   if i not in {e["id"] for e in menus["lighting"]})
    draft["choices"]["lighting"] = outside
    assert any("not in the menu" in p for p in crew.check_crew(draft, "product", "향수 광고 만들어 줘", None, menus))


def test_demand_spans_keep_separate_words():
    tags = {d["tag"] for d in crew_lib.user_demands("파스텔 색감의 파스텔화 향수 광고")}
    assert {"파스텔", "파스텔화"} <= tags, tags                            # 색 '파스텔' 이 장르 '파스텔화' 에 먹히지 않는다
    tags = {d["tag"] for d in crew_lib.user_demands("다크 판타지 포스터")}
    assert "다크 판타지" in tags and "판타지" not in tags, tags              # 같은 자리의 짧은 말은 긴 말에 합친다
    tags = {d["tag"] for d in crew_lib.user_demands("다크 판타지와 밝은 판타지를 섞은 포스터")}
    assert {"다크 판타지", "판타지"} <= tags, tags                          # 떨어진 자리에 또 쓰면 둘 다 남는다


def test_dark_items_when_user_asks():
    rt = crew.route("유령의 집 같은 거실")
    menus = crew.build_menus("유령의 집 같은 거실", rt, random.Random(0))
    assert any(e.get("dark") for ents in menus.values() for e in ents)  # 사용자가 말하면 어두운 항목도 나온다


# ── 정본 인물 장면 (11-26, CREW_CAST_MODE) ─────────────────────────────
CAFE = "니키가 카페 창가에서 책을 읽는 장면"
CAST_NOTES = ["Niki in the left third of the frame by the cafe window", "Soft warm window light from the left",
              "Medium shot at eye level with a 35mm lens", "Fine dust drifting in the window light"]
CAST_FIT = {"choices": {}, "fit_menu": True, "ko": GOOD["ko"], "notes_en": CAST_NOTES}


class cast_mode:
    """설정 CREW_CAST_MODE 를 잠시 바꾼다"""
    def __init__(self, mode):
        self.mode = mode

    def __enter__(self):
        self.saved = crew.settings.crew_cast_mode
        crew.settings.crew_cast_mode = self.mode

    def __exit__(self, *a):
        crew.settings.crew_cast_mode = self.saved


def cast_result(text=CAFE, seed=0):
    rt = crew.cast_route(text)
    menus = crew.build_menus(text, rt, random.Random(seed))
    choices = {st["key"]: menus[st["key"]][0]["id"] for st in crew.steps_of(rt["category"])}
    return rt, menus, {"choices": choices, "ko": GOOD["ko"], "notes_en": list(CAST_NOTES)}


def test_cast_categories_integrity():
    kn = crew.load_knowledge()
    allowed = {"lighting", "time_weather", "camera", "composition", "color_palette", "atmosphere", "mood_emotion"}
    assert set(kn["cast_categories"]) == {"cast_scene", "cast_film"}
    groups = {}
    for e in crew_lib.load_library()["entries"].values():
        groups.setdefault(e["facet"], set()).add(e["group"])
    for cat, spec in kn["cast_categories"].items():
        assert spec["rule"] and spec["title"] and "keywords" not in spec, cat
        facets = [st["facet"] for st in spec["steps"]]
        assert set(facets) <= allowed, (cat, facets)
        for bad in ("genre_style", "props", "location_set", "product_presentation", "material_texture", "video_motion"):
            assert bad not in facets
        for st in spec["steps"]:
            for g in st.get("only") or []:
                assert any(x.startswith(g) for x in groups[st["facet"]]), (cat, st["key"], g)
    assert {st["facet"] for st in kn["cast_categories"]["cast_film"]["steps"]} == {
        "time_weather", "lighting", "color_palette", "atmosphere", "mood_emotion"}      # 카메라·구도는 G0 대사 컷 규칙과 부딪힌다
    assert kn["cast_palette_warm"] == ["hue_family_warm", "hue_family_neutral"]


def test_cast_route_library_names_only():
    from app import promptx
    with cast_mode("safe"):
        rt, advice = promptx.crew_route(CAFE)
        assert rt["category"] == "cast_scene" and rt["cast"] == ["niki"] and not advice
        rt, advice = promptx.crew_route("퀼린스가 숲에")
        assert rt is None and any("캐릭터 정본" in a for a in advice)       # 대조할 외형이 없는 정본 이름
        rt, advice = promptx.crew_route("니키와 소년이 숲에서")
        assert rt is None and any("사람이 나오는" in a for a in advice)
        rt, advice = promptx.crew_route("니키, 손은 소매 속에 숨기고 숲에 서 있다")
        assert rt is None and any("손 숨김" in a for a in advice)             # 11-10 V05 실패 조건은 계속 건너뜀
        assert promptx.crew_route("니키와 퀼린스가 숲에서")[0] is None
        assert promptx.crew_route(CAFE, crew=False) == (None, [])
        assert promptx.crew_route(CAFE, cast=False)[0] is None                 # 영상 렌더러의 클립 움직임
        assert crew.cast_route("제임스와 베스가 부엌에서")["cast"] == ["james", "james_glasses", "beth"]
        assert crew.route("niki in a cafe interior") is None                  # route 와 skip_reason 은 그대로
    with cast_mode("off"):
        rt, advice = promptx.crew_route(CAFE)
        assert rt is None and advice == [promptx.CREW_SKIP_ADVICE["character"]]
        assert crew.cast_route(CAFE) is None and not crew.film_crew_on(CAFE)
    with cast_mode("image"):
        assert promptx.crew_route(CAFE)[0]["category"] == "cast_scene" and not crew.film_crew_on(CAFE)


def test_guard_particles():
    for text in ("니키에게 편지를 주는 카페", "니키한테 어울리는 방", "니키야 카페 가자", "니키네 집 거실", "니키와의 산책 숲 풍경"):
        assert crew.skip_reason(text) == "character", text
    assert crew.skip_reason("니키타 영화 포스터 카페") is None
    assert crew.skip_reason("소녀에게 주는 선물 광고") is None      # 조사 추가는 정본 이름에만: 사람 낱말 판정은 그대로


def test_cast_menus_exclude_body_wardrobe_people():
    texts = [CAFE, "니키가 마법을 쓰는 신비로운 숲속 장면", "푸토르가 약초방에서 약초를 고르는 장면",
             "제임스와 베스가 비 오는 밤 부엌에서", "니키가 파란 조명 아래 숲에 있는 장면"]
    cam = tuple(next(st for st in crew.steps_of("cast_scene") if st["key"] == "camera")["only"])
    for text in texts:
        for cat in ("cast_scene", "cast_film"):
            rt = crew.cast_route(text, category=cat)
            assert rt, text
            for seed in range(21 if text == CAFE else 3):          # 시드 0~20 은 한 요청만 (메뉴 뽑기가 느리다)
                menus = crew.build_menus(text, rt, random.Random(seed))
                for key, ents in menus.items():
                    assert ents, (text, key)
                    for e in ents:
                        assert not crew_lib.CAST_BAN_EN_RE.search(e["en"]), (text, e["id"], e["en"])
                        assert not crew_lib.PEOPLE_EN_RE.search(e["en"]), (text, e["id"], e["en"])
                        assert e["id"] != crew_lib.NONE_GENRE and e["facet"] != "genre_style"
                        if key == "camera":
                            assert e["group"].startswith(cam), e["id"]
    # 색은 따뜻한·중립 묶음만, 사용자가 말한 색(파란)은 must 로 들어오되 화풍을 바꾸는 묶음(색연필 등)은 빠진다
    rt = crew.cast_route("니키가 파란 조명 아래 숲에 있는 장면")
    for seed in range(4):
        color = crew.build_menus("니키가 파란 조명 아래 숲에 있는 장면", rt, random.Random(seed))["color"]
        assert any("blue" in e["en"] for e in color)
        assert not any(e["group"] in ("art_medium", "film_grade", "duotone") for e in color)
    color = crew.build_menus(CAFE, crew.cast_route(CAFE), random.Random(3))["color"]
    assert all(e["group"].startswith(("hue_family_warm", "hue_family_neutral")) for e in color)


def test_check_crew_cast_rules():
    rt, menus, good = cast_result()
    ents = crew.cast_by_id(rt["cast"])
    assert crew.check_crew(good, "cast_scene", CAFE, CAFE, menus, cast=ents) == []
    for line in ("Niki with one arm slightly raised to channel magical energy",       # 11-10 V05 원문
                 "Niki's cape glows deep teal", "she raises one hand toward the window", "a bustling crowd outside the window",
                 "cinematic anime look with soft colours", "Niki's braids catch the window light",
                 "Niki's bangs and bow glow in the warm light"):                         # bangs·bow 는 외형 글에서 뽑은 금지어로만 잡힌다
        bad = dict(good, notes_en=CAST_NOTES[:3] + [line])
        probs = crew.check_crew(bad, "cast_scene", CAFE, CAFE, menus, cast=ents)
        assert probs and all(p.startswith(crew.CAST_PROBLEM) for p in probs), (line, probs)
    sf = "SF 느낌으로 니키가 카페 창가에 있는 장면"
    rt2, menus2, good2 = cast_result(sf)
    probs = crew.check_crew(good2, "cast_scene", sf, sf, menus2, cast=crew.cast_by_id(rt2["cast"]))
    assert not any("sci" in p.lower() or "'sf'" in p.lower() for p in probs), probs   # 장르 요구는 화풍 칸이 맡는다
    text_note = dict(good, notes_en=CAST_NOTES[:3] + ["A clock face with the word OPEN on the wall"])
    probs = crew.check_crew(text_note, "cast_scene", CAFE, CAFE, menus, cast=ents)
    assert any(p.startswith("Do not mention text") for p in probs)                     # 글자 검사는 그대로 (G0 V07)


def test_sanitize_cast_notes():
    ents = crew.cast_by_id(["niki"])
    r = {"notes_en": CAST_NOTES + ["Niki's cape glows deep teal"]}
    assert crew.sanitize_cast_notes(r, ents)["notes_en"] == CAST_NOTES
    r = {"notes_en": CAST_NOTES[:2] + ["she raises one hand", "a bustling crowd"]}
    assert crew.sanitize_cast_notes(r, ents) is None                                   # 3줄 미만이면 버린다
    assert crew.sanitize_cast_notes({"notes_en": 3}, ents) is None
    # draft: 고쳐 써도 금지 줄 하나만 남으면 그 줄만 지우고 쓴다
    rt = crew.cast_route(CAFE)
    stubborn = dict(CAST_FIT, notes_en=CAST_NOTES + ["Niki's cape glows deep teal"])
    result, rounds, left = asyncio.run(crew.draft(Call([stubborn, stubborn]), CAFE, rt, repair_rounds=1, rng=random.Random(0)))
    assert left == [] and result["notes_en"] == CAST_NOTES and rounds == 2


def test_expand_uses_cast_crew_in_same_turn():
    with cast_mode("safe"):
        result, calls = run_expand([CAST_FIT, EXPANDED], CAFE)
    assert len(calls) == 2 and isinstance(calls[0]["format"], dict)
    assert calls[0]["messages"][0]["content"].startswith("You are a small production crew of experts preparing one still image")
    assert "keep visible: wine-red round-brimmed hat" in calls[0]["messages"][0]["content"]
    helper = calls[1]["messages"][-1]["content"]
    assert "Crew look plan (light, camera and air only" in helper and "Soft warm window light from the left" in helper
    assert "Fixed characters" in helper and "two long black braids" in helper
    assert "Must-keep details" not in helper                                           # 사용자 말 칸에 섞지 않는다
    assert "Optional ideas" not in helper
    c = result["crew"]
    assert c["used"] and c["category"] == "cast_scene" and c["cast"] == ["niki"]
    assert any("캐릭터 정본 장면이라" in a for a in result["advice"])
    with cast_mode("off"):
        result, calls = run_expand([EXPANDED], CAFE)
    assert len(calls) == 1 and result["crew"] is None and "Fixed characters" not in calls[0]["messages"][-1]["content"]


def test_draft_passes_num_ctx():
    rt = crew.cast_route(CAFE, category="cast_film")
    call = Call([CAST_FIT])
    _, rounds, left = asyncio.run(crew.draft(call, CAFE, rt, options={"num_ctx": 16384}, rng=random.Random(0)))
    assert left == [] and rounds == 1
    assert call.calls[0]["options"] == {"num_ctx": 16384, "temperature": 0.6}
    assert "Output: the shared look of every scene of one short film." in call.calls[0]["messages"][-1]["content"]
    call = Call([CAST_FIT])
    with cast_mode("safe"):
        info = asyncio.run(crew.draft_film(call, CAFE, num_ctx=12288, rng=random.Random(0)))
    look = info["look"]
    assert info["used"] and info["cast"] == ["niki"] and call.calls[0]["options"]["num_ctx"] == 12288
    assert look["light"] and len(look["light"]) <= crew.LOOK_LIGHT_MAX and len(look["air"]) <= crew.LOOK_AIR_MAX
    assert len(crew.look_plan_text(look)) <= crew.LOOK_PLAN_MAX and "light and palette:" in crew.look_plan_text(look)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"ok ({len(tests)} tests)")
