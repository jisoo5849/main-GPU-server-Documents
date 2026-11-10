"""제작진 모드(crew) 단위 테스트: 분야 라우팅, 지식 파일 무결성, 초안 검사, 메모 합치기. GPU·DB 없이 돈다.

  cd hub && python tests/test_crew.py

검사 예시는 2026-11-10 G0 A/B 시험에서 실제로 나온 실패(V04 신발 광고가 사람 다리 컷, V07 시계 다이얼의 지어낸 글자)다.
"""
import asyncio
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crew  # noqa: E402

GOOD = {
    "choices": {"presentation": "front_single", "background": "dark_premium", "focus": "gloss",
                "lighting": "rim_backlight", "mood": "luxurious"},
    "ko": {"plan": "고급스러운 향수 광고입니다.", "direction": "병을 화면 중앙에 정면으로 둡니다.",
           "camera_lighting": "역광과 림 라이트로 윤곽을 살립니다.", "details": "유리의 반사와 금속 캡의 질감을 보여 줍니다."},
    "notes_en": ["A single perfume bottle centered in the frame", "Dark gradient studio backdrop",
                 "Soft backlight with thin rim light on the glass edges", "Crisp reflections on the glass and metal cap"],
}


def test_knowledge_integrity():
    kn = crew.load_knowledge()
    assert set(kn["categories"]) == {"product", "space", "landscape"}
    for cat, spec in kn["categories"].items():
        assert spec["keywords"] and spec["rule"]
        keys = [st["key"] for st in spec["steps"]]
        assert len(keys) == len(set(keys)) >= 4, cat
        for st in spec["steps"]:
            ids = [o["id"] for o in st["options"]]
            assert len(ids) == len(set(ids)) >= 4, (cat, st["key"])
            assert all(o["ko"] and o["en"] for o in st["options"])
    assert all(w in kn["purpose_words"] for w in ("포스터", "광고"))


def test_route_known_fields():
    cases = {"향수 광고 만들어 줘": "product", "신발 광고": "product", "고급 시계 광고": "product",
             "새로 연 카페 홍보 이미지": "space", "따뜻한 느낌의 집 안 풍경": "space",
             "비 오는 날 분위기 사진": "landscape", "여행 포스터 같은 바다 풍경": "landscape"}
    for text, cat in cases.items():
        rt = crew.route(text)
        assert rt and rt["category"] == cat, (text, rt)


def test_route_skips_character_and_ambiguous():
    assert crew.route("니키가 나오는 신비로운 장면") is None            # 캐릭터 정본이 우선
    assert crew.route("niki in a cafe interior") is None
    assert crew.route("니키가 카페 창가에서 책을 안고 있음, 손은 소매 속에 숨김") is None
    assert crew.route("손은 소매 속에 숨기고 신발 광고") is None          # 손 숨김 요청은 기존 도우미로
    assert crew.route("책상 위 고양이") is None                           # 분야 메뉴 없음
    assert crew.route("카페 광고") is None or crew.route("카페 광고")["category"] == "space"
    assert crew.route("베스트 향수 광고")["category"] == "product"        # 베스트 가 이름 베스로 잡히지 않음


def test_route_purpose_words():
    rt = crew.route("여행 포스터 같은 바다 풍경")
    assert "포스터" in rt["purposes"]
    assert not crew.route("고급 시계 광고")["purposes"].get("포스터")


def test_good_draft_passes():
    assert crew.check_crew(GOOD, "product", "향수 광고 만들어 줘") == []


def test_invalid_choice_and_missing_step():
    bad = copy.deepcopy(GOOD)
    bad["choices"]["background"] = "neon_city"
    del bad["choices"]["mood"]
    probs = crew.check_crew(bad, "product", "향수 광고")
    assert any('"background"' in p for p in probs) and any('"mood"' in p for p in probs)


def test_person_in_notes_is_caught():
    # G0 V04: 신발 광고가 사람이 서 있는 다리 컷이 됐다
    bad = copy.deepcopy(GOOD)
    bad["notes_en"] = ["One person standing on a reflective floor", "Arms hanging at the sides", "Soft studio light"]
    assert any("names no person" in p for p in crew.check_crew(bad, "product", "신발 광고"))
    assert not any("names no person" in p for p in crew.check_crew(bad, "product", "모델이 신발을 신은 광고"))


def test_sofa_arm_is_not_a_person():
    ok = copy.deepcopy(GOOD)
    ok["notes_en"] = ["A cozy sofa with a knit throw draped over one arm", "A low table with wooden legs",
                      "Soft evening light"]
    assert not any("names no person" in p for p in crew.check_crew(ok, "space", "따뜻한 느낌의 집 안 풍경"))


def test_text_and_logo_is_caught():
    # G0 V07: 시계 다이얼에 요청에 없던 글자가 생겼다 (노트에 글자 말이 들어가는 것만 막을 수 있고, 모델이 스스로 그리는 글자는 결과 검사 몫)
    bad = copy.deepcopy(GOOD)
    bad["notes_en"] = ["A wristwatch with the brand logo on the dial", 'Engraved "LUXE" text', "Dark premium backdrop"]
    assert any("logos" in p for p in crew.check_crew(bad, "product", "고급 시계 광고"))


def test_negation_language_and_length():
    bad = copy.deepcopy(GOOD)
    bad["notes_en"] = ["A bottle without any clutter", "배경은 어둡게", "x " * 40]
    probs = crew.check_crew(bad, "product", "향수 광고")
    assert any('"without"' in p for p in probs)
    assert any("English only" in p for p in probs)
    assert any("words" in p for p in probs)
    few = dict(GOOD, notes_en=["Only one line"])
    assert any("lines" in p for p in crew.check_crew(few, "product", "향수 광고"))
    nokorean = copy.deepcopy(GOOD)
    nokorean["ko"]["plan"] = "luxury perfume ad"
    assert any('"ko.plan"' in p for p in crew.check_crew(nokorean, "product", "향수 광고"))


def test_schema_enforces_menu_ids():
    sch = crew.build_schema("product")
    props = sch["properties"]["choices"]["properties"]
    assert set(props) == {"presentation", "background", "focus", "lighting", "mood"}
    assert "dark_premium" in props["background"]["enum"]
    assert sch["properties"]["notes_en"]["minItems"] == crew.MIN_NOTES
    json.dumps(sch)   # JSON 직렬화 가능해야 Ollama format 으로 보낼 수 있다


def test_messages_carry_menu_rules_and_purpose():
    rt = crew.route("여행 포스터 같은 바다 풍경")
    msgs = crew.build_messages("여행 포스터 같은 바다 풍경", rt, style="A painted look", notes="")
    system = msgs[0]["content"]
    assert "EXPERT MENU" in system and "panorama" in system and "poster" in system.lower()
    assert "A painted look" in msgs[1]["content"]


def test_describe_and_merge_notes():
    d = crew.describe(GOOD, "product")
    assert d["picked"][1]["ko"] == "어두운 프리미엄 배경" and d["ko"]["plan"].startswith("고급스러운")
    merged = crew.merge_notes("유리 병 형태 유지.", GOOD)
    assert merged.startswith("유리 병 형태 유지. A single perfume bottle")
    assert crew.merge_notes("", GOOD).startswith("A single perfume bottle")
    assert crew.merge_notes("사용자 특징", {"notes_en": []}) == "사용자 특징"


def test_draft_repairs_once_then_gives_up():
    class Call:
        def __init__(self, replies):
            self.replies, self.calls = list(replies), []

        async def __call__(self, payload):
            self.calls.append(payload)
            return {"message": {"content": json.dumps(self.replies.pop(0))}}

    rt = crew.route("신발 광고")
    person = dict(GOOD, notes_en=["One person standing on the floor", "Arms at the sides", "Soft light"])
    call = Call([person, GOOD])
    result, rounds, left = asyncio.run(crew.draft(call, "신발 광고", rt, repair_rounds=1))
    assert left == [] and rounds == 2 and len(call.calls) == 2
    assert call.calls[1]["messages"][-1]["content"].startswith("Your JSON broke these rules")
    assert call.calls[0]["format"]["properties"]["choices"]   # 스키마를 format 으로 보냄

    stubborn = Call([person, person])
    _, rounds, left = asyncio.run(crew.draft(stubborn, "신발 광고", rt, repair_rounds=1))
    assert left and rounds == 2


# ── promptx.expand 와 이어 붙이기 (가짜 llm 으로, GPU·DB 없이) ──────────────────────────
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


def run_expand(replies, text, notes="", crew=None):
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
            return {"message": {"content": json.dumps(replies.pop(0))}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        from app import promptx
        result = asyncio.run(promptx.expand({"id": 1}, "127.0.0.1", text, "", notes, crew))
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved
    return result, calls


def test_expand_uses_crew_in_the_same_turn():
    result, calls = run_expand([GOOD, EXPANDED], "향수 광고 만들어 줘")
    assert len(calls) == 2 and isinstance(calls[0]["format"], dict)        # 초안 한 번 + 확장 한 번
    assert calls[1]["format"] == "json"
    assert "A single perfume bottle centered in the frame" in calls[1]["messages"][-1]["content"]
    c = result["crew"]
    assert c["used"] and c["category"] == "product" and c["ko"]["plan"] and c["picked"][1]["ko"] == "어두운 프리미엄 배경"
    assert result["violations"] == [] and result["prompt"].startswith("a single luxury perfume bottle")


def test_expand_skips_crew_for_characters_and_when_off():
    result, calls = run_expand([EXPANDED], "니키가 나오는 신비로운 장면 향수 광고")
    assert len(calls) == 1 and result["crew"] is None
    result, calls = run_expand([EXPANDED], "향수 광고 만들어 줘", crew=False)
    assert len(calls) == 1 and result["crew"] is None
    result, calls = run_expand([EXPANDED], "책상 위 고양이", crew=True)     # 분야가 없으면 조용히 도우미로
    assert len(calls) == 1 and result["crew"] is None and not any("제작진" in a for a in result["advice"])
    result, calls = run_expand([EXPANDED], "니키가 나오는 신비로운 장면")
    assert any("캐릭터 정본" in a for a in result["advice"])
    result, calls = run_expand([EXPANDED], "비 오는 날 우산을 쓴 소녀")
    assert len(calls) == 1 and result["crew"] is None and any("사람이 나오는" in a for a in result["advice"])


def test_expand_falls_back_when_crew_keeps_failing():
    person = dict(GOOD, notes_en=["One person standing on the floor", "Arms at the sides", "Soft light"])
    result, calls = run_expand([person, person, EXPANDED], "신발 광고", notes="유리 병 형태 유지")
    assert len(calls) == 3
    assert result["crew"]["used"] is False and result["crew"]["violations"]
    assert "One person standing" not in calls[2]["messages"][-1]["content"]      # 실패한 초안은 쓰지 않는다
    assert "유리 병 형태 유지" in calls[2]["messages"][-1]["content"]              # 사용자의 특징은 그대로
    assert any("통과하지 못해" in a for a in result["advice"])

# ── 독립 검토(2026-11-10)에서 나온 실제 오류의 재발 방지 ─────────────────────────────────
def test_route_uses_whole_words():
    # 한글 짧은 키워드("산", "방", "크림", "아이")와 영문 부분 문자열("sea", "room")이 다른 낱말 속에서 잡히던 오류
    for text in ("비 오는 날 우산을 쓴 소녀", "아이스크림 먹는 고양이", "고양이가 방향을 바라본다",
                 "seated girl reading a book", "bathroom mushroom", "우주 방랑자", "마법사가 숲속에서 마법을 쓴다"):
        assert crew.route(text) is None, text
    assert crew.route("방에서 찍은 사진")["category"] == "space"          # 조사가 붙은 낱말은 잡힌다
    assert crew.route("고요한 sea view")["category"] == "landscape"
    assert crew.route("하얀 shoes 광고")["category"] == "product"         # 영문 복수형


def test_guard_names_need_whole_words():
    assert crew.route("베스킨라빈스 매장 홍보 이미지")["category"] == "space"
    assert crew.skip_reason("니키타 영화 포스터 카페") is None
    assert crew.skip_reason("니키가 카페에 있는 장면") == "character"
    assert crew.skip_reason("niki in a cafe") == "character"
    assert crew.skip_reason("아이스크림 병 광고") is None                   # '아이' 가 아이스크림 속에서 사람으로 잡히지 않는다
    assert crew.skip_reason("아이가 신발을 신은 장면") == "person"


def test_watch_hands_and_sofa_arms_are_not_people():
    ok = copy.deepcopy(GOOD)
    ok["notes_en"] = ["Hour and minute hands on the watch face", "Polished steel case with a sapphire crystal", "Soft side light"]
    assert crew.check_crew(ok, "product", "시계 광고") == []
    bad = copy.deepcopy(GOOD)
    bad["notes_en"] = ["A hand holding the bottle", "Dark backdrop", "Soft side light"]
    assert any("names no person" in p for p in crew.check_crew(bad, "product", "향수 광고"))
    bad["notes_en"] = ["Her hands rest on the table", "Dark backdrop", "Soft side light"]
    assert any("names no person" in p for p in crew.check_crew(bad, "product", "향수 광고"))


def test_malformed_model_output_never_raises():
    for broken in ({"choices": {"presentation": ["front_single"]}, "ko": {}, "notes_en": 3},
                   {"choices": "x", "ko": [], "notes_en": None},
                   {"choices": {}, "ko": {"plan": 1}, "notes_en": [["nested"], None, {"a": 1}]}, [], "text", 7):
        probs = crew.check_crew(broken, "product", "향수 광고")
        assert probs and all(isinstance(p, str) for p in probs), broken


def test_expand_falls_back_on_unexpected_crew_errors():
    weird = {"choices": {"presentation": ["front_single"]}, "ko": {}, "notes_en": 3}
    result, calls = run_expand([weird, weird, EXPANDED], "향수 광고 만들어 줘")
    assert len(calls) == 3 and result["crew"]["used"] is False
    assert result["violations"] == [] and result["prompt"].startswith("a single luxury perfume bottle")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"ok ({len(tests)} tests)")
