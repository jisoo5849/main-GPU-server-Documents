"""끝 프레임·장면 잇기 검사 기준(studio/checks.py) 시험 (docs/scene-chaining-design.md 3.3·4절). 순수 함수라 DB·GPU 없이 돈다.

  cd hub && python tests/test_studio_checks.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.studio import checks  # noqa: E402

NIKI = ("a twelve-year-old girl with two long black braids and thin straight bangs, pale face, dark blue eyes, a wine-red "
        "round-brimmed hat, a wine-red cape with small embroidered flowers over a wine-red blouse with a big bow at the neck")


def t_cast_trait_is_short_and_keeps_the_front_of_the_look():
    t = checks.cast_trait(NIKI, "니키")
    # 몸 전체가 아니라 얼굴이 화면 안인지 묻는다 (대사 클로즈업은 가슴에서 잘려도 맞는 그림, 11-21)
    assert t.startswith("A person who looks like this is in the picture with the whole face inside the frame, clearly visible from the front or three-quarter: ")
    assert len(t) <= 200 and not t.endswith((",", ".", " "))
    assert "twelve-year-old girl" in t and "cape" not in t              # 첫 14낱말까지만
    assert checks.cast_trait("a tall man.").endswith(": a tall man")
    assert len(checks.cast_trait("word " * 60)) <= 200


def t_style_negative_names_what_the_picture_is_not():
    assert "not a glossy 3D render" in checks.style_negative("semi") and "not cel-shaded" in checks.style_negative("semi")
    assert "2D anime-style" in checks.style_negative("anime") and "not a photograph" in checks.style_negative("anime")
    assert "live-action film still" in checks.style_negative("photo")
    assert checks.style_negative("") == "" and checks.style_negative("watercolor") == ""
    assert checks.style_trait("x") == "The whole picture is in this art style: x"


def t_end_rule_is_a_lower_bound_and_exact_zero_only_without_people():
    r = checks.end_rule(2, 0, False)
    assert r["end"] and r["want"] == 2 and r["more_ok"] and not r["zero"] and not r["fewer_ok"]
    assert checks.end_rule(2, 1, True)["want"] == 2 and checks.end_rule(2, 1, True)["more_ok"]      # 동물·행인이 있어도 아래쪽은 본다
    assert "extra_limb" in checks.end_rule(1, 1, False)["skip"] and "face" not in checks.end_rule(1, 0, False)["skip"]
    z = checks.end_rule(0, 0, False)
    assert z["want"] is None and z["zero"] and z["end"]
    assert not checks.end_rule(0, 0, True)["zero"] and not checks.end_rule(0, 1, False)["zero"]


def t_end_problems_codes():
    r = checks.end_rule(2, 0, False)
    ok = {"people": 2, "issues": [], "traits": [{"present": True}, {"present": True}]}
    assert checks.end_problems(ok, r, ["niki", "putor"]) == []
    assert checks.end_problems({"people": 4, "issues": [], "traits": [{"present": True}, {"present": True}]}, r, ["niki", "putor"]) == []
    assert checks.end_problems(None, r, ["niki"]) == ["end:unchecked"] and checks.end_problems("x", r, []) == ["end:unchecked"]
    few = {"people": 1, "traits": [{"present": True}, {"present": False, "note": "face turned away"}]}
    assert checks.end_problems(few, r, ["niki", "putor"]) == ["end:people:1/2", "end:missing:putor"]
    # 특징이 앞에 다른 것이 있으면 first_trait_index 부터가 인물 특징이다
    shifted = {"people": 2, "traits": [{"present": False, "note": "x"}, {"present": True}, {"present": False}]}
    assert checks.end_problems(shifted, r, ["niki", "putor"], 1) == ["end:missing:putor"]
    # 모르겠다(None)는 사람이 한 명도 없을 때만 없는 것으로 본다
    unsure = {"people": 2, "traits": [{"present": None}, {"present": None}]}
    assert checks.end_problems(unsure, r, ["a", "b"]) == []
    nobody = {"people": 0, "traits": [{"present": None}]}
    assert checks.end_problems(nobody, checks.end_rule(1, 0, False), ["a"]) == ["end:people:0/1", "end:missing:a"]
    # 사람 없는 장면에 사람이 보이면
    assert checks.end_problems({"people": 1}, checks.end_rule(0, 0, False), []) == ["end:extra:1"]
    assert checks.end_problems({"people": 0}, checks.end_rule(0, 0, False), []) == []
    assert checks.end_problems({"people": 5}, checks.end_rule(0, 0, True), []) == []
    # 몸의 이상: 입 맞춤 예외 없음, 동물이면 팔다리 뺌
    body = {"people": 1, "issues": [{"type": "merged_bodies"}, {"type": "face"}, {"type": "extra_limb"}, {"type": "extra_fingers"}]}
    assert checks.end_problems(body, checks.end_rule(1, 0, False), []) == ["body:extra_limb,face,merged_bodies"]
    assert checks.end_problems(body, checks.end_rule(1, 1, False), []) == ["body:face,merged_bodies"]
    # 화풍: 마지막 특징이고 이유를 붙여 '아님'일 때만, 화풍을 안 줬으면(실사 영화) 안 본다
    st = {"people": 1, "traits": [{"present": True}, {"present": False, "note": "looks like a 3D render"}]}
    assert checks.end_problems(st, checks.end_rule(1, 0, False), ["a"], 0, "x") == ["style"]
    assert checks.end_problems(st, checks.end_rule(1, 0, False), ["a"], 0, "") == []
    nonote = {"people": 1, "traits": [{"present": True}, {"present": False, "note": ""}]}
    assert checks.end_problems(nonote, checks.end_rule(1, 0, False), ["a"], 0, "x") == []
    only_cast = {"people": 1, "traits": [{"present": True}]}                   # 마지막 특징이 인물 특징이면 화풍으로 읽지 않는다
    assert checks.end_problems(only_cast, checks.end_rule(1, 0, False), ["a"], 0, "x") == []
    assert checks.end_problems({"people": 1, "traits": [{"present": False, "note": "x"}]}, checks.end_rule(1, 0, False), ["a"], 0, "x") == ["end:missing:a"]


def t_better_ranks_end_failures_worst():
    assert checks.better(None, ["a"]) and checks.better(["a", "b"], ["a"]) and not checks.better(["a"], ["b"])
    assert not checks.better(["body:face", "style"], ["end:missing:niki"])          # 끝 프레임 실패는 몸 문제 둘보다 나쁘다
    assert checks.better(["end:missing:niki"], ["body:face", "style", "people:3/1"])
    assert not checks.better(["end:people:0/1"], ["end:people:0/1"])
    assert checks.better(["end:people:0/2", "end:missing:a"], ["end:people:1/2"])
    assert checks.better(["end:unchecked"], [])


def t_describe_labels():
    assert checks.describe(["end:missing:niki", "end:people:1/2"]) == "끝 프레임에 인물 없음"
    assert checks.describe(["end:extra:1"]) == "끝 프레임에 인물 없음" and checks.describe(["end:unchecked"]) == "끝 프레임 확인 못 함"
    assert checks.describe(["people:2/1", "body:face", "style", "end:missing:a"]) == "사람 수·몸 모양·화풍·끝 프레임에 인물 없음"
    assert checks.describe([]) == "" and checks.describe(None) == ""


def t_cut_rule_counts_people_at_every_size():
    r = checks.cut_rule(2, 0, False)
    assert checks.problems({"people": 3}, r) == ["people:3/2"] and checks.problems({"people": 1}, r) == ["people:1/2"]
    assert checks.problems({"people": 2}, r) == []
    assert checks.problems({"people": 6}, checks.cut_rule(2, 0, True)) == [] and checks.problems({"people": 1}, checks.cut_rule(2, 0, True)) == ["people:1/2"]
    a = checks.cut_rule(1, 1, False)
    assert a["want"] == 1 and a["more_ok"] and checks.problems({"people": 2}, a) == [] and checks.problems({"people": 0}, a) == ["people:0/1"]
    assert checks.problems({"people": 1, "issues": [{"type": "extra_limb"}]}, a) == []
    assert checks.cut_rule(0, 0, False)["want"] == 0 and checks.problems({"people": 1}, checks.cut_rule(0, 0, False)) == ["people:1/0"]


def t_sign_trait_uses_the_first_two_signature_items():
    t = checks.sign_trait("a girl with braids", ["wine-red round-brimmed hat", "two long black braids", "wine-red cape"])
    assert t == "A person with wine-red round-brimmed hat and two long black braids is in the picture with the face visible"
    assert checks.sign_trait("a man in a navy coat", []) == checks.cast_trait("a man in a navy coat")      # 소품이 없으면 외형으로
    assert checks.sign_trait("x", None).startswith("A person who looks like")
    assert len(checks.sign_trait("x", ["a" * 150, "b" * 150])) <= checks.TRAIT_MAX


def t_cast_problems_reads_people_each_character_body_and_style():
    r = checks.cut_rule(2, 0, False)
    ok = {"people": 2, "issues": [], "traits": [{"present": True}, {"present": True}, {"present": True}]}
    assert checks.cast_problems(ok, r, ["niki", "beth"], style="style") == []
    miss = {"people": 2, "issues": [{"type": "merged_bodies"}], "traits": [{"present": True}, {"present": False, "note": "no hat"},
                                                                           {"present": False, "note": "glossy 3D"}]}
    assert checks.cast_problems(miss, r, ["niki", "beth"], style="style") == ["cast:missing:beth", "body:merged_bodies", "style"]
    assert checks.cast_problems({"people": 3, "traits": []}, r, ["niki", "beth"]) == ["people:3/2"]
    # 모르겠다(None)는 틀린 것으로 보지 않는다. 화풍 줄이 없으면(style 빈 글) 마지막 줄을 화풍으로 읽지 않는다
    assert checks.cast_problems({"people": 2, "traits": [{"present": None}, {"present": False, "note": "x"}]}, r, ["niki", "beth"]) == ["cast:missing:beth"]
    assert checks.cast_problems(None, r, ["niki"]) == ["cast:unchecked"]
    # 인물 모습이 틀린 것은 몸 모양·화풍보다 나쁘다 (가장 나은 그림 고르기)
    assert checks.better(["cast:missing:beth"], ["body:face", "style"]) and not checks.better(["style"], ["cast:missing:a"])
    assert checks.describe(["cast:missing:beth", "people:3/2"]) == "인물 모습(기준 그림과 다름)·사람 수"
    assert checks.describe(["cast:unchecked"]) == "인물 모습 확인 못 함"


def t_compose_rule_counts_exactly_unless_a_crowd_is_named():
    r = checks.compose_rule(1, 0, False)
    assert r["want"] == 1 and not r["more_ok"] and not r["fewer_ok"]
    assert checks.cast_problems({"people": 2, "traits": []}, r, ["B"]) == ["people:2/1"]      # 베스 혼자 장면에 2명 (11-21 메인)
    assert checks.compose_rule(1, 0, True)["more_ok"] and checks.compose_rule(1, 1, False)["more_ok"]
    assert checks.compose_rule(1, 1, False)["skip"] == checks.ANIMAL_SKIP
    assert checks.CROWD_RE.search("a crowd of shoppers in the market")
    assert checks.CROWD_RE.search("Niki walks through the crowded market full of people")      # 실제 군중 (11-21 검토)
    assert checks.CROWD_RE.search("Children play in the square")
    assert not checks.CROWD_RE.search("The mother smiles at the girl while her father laughs with a woman")


def t_close_up_skips_the_floating_limb_false_positive():
    """가슴 아래가 잘린 클로즈업의 '팔다리 떠 있음'은 오탐이다 (11-24 메인: 같은 그림을 다섯 번 다시 그렸다)"""
    insp = {"people": 1, "issues": [{"type": "floating_limb"}], "traits": []}
    assert checks.cast_problems(insp, checks.compose_rule(1, 0, False), ["B"]) == ["body:floating_limb"]
    assert checks.cast_problems(insp, checks.compose_rule(1, 0, False, close=True), ["B"]) == []
    assert checks.problems(insp, checks.clip_rule(1, 0, talk=True)) == []                      # 입 맞춤 클립도
    assert checks.problems(insp, checks.clip_rule(1, 0, talk=False)) == ["body:floating_limb"]
    real = {"people": 1, "issues": [{"type": "extra_head"}], "traits": []}
    assert checks.cast_problems(real, checks.compose_rule(1, 0, False, close=True), ["B"]) == ["body:extra_head"]   # 진짜 이상은 그대로


def t_layout_problems_reads_the_figure_line_and_ignores_blank_faces():
    ok = {"people": 0, "issues": [{"type": "face"}, {"type": "missing_head"}], "traits": [{"present": True}]}
    assert checks.layout_problems(ok, 2) == []              # 형상을 사람으로 세지 않아도, 얼굴이 없는 것도 문제가 아니다
    bad = {"people": 3, "issues": [{"type": "merged_bodies"}], "traits": [{"present": False, "note": "three figures"},
                                                                          {"present": False, "note": "glossy 3D"}]}
    assert checks.layout_problems(bad, 2, style="style") == ["layout:figures", "people:3/2", "body:merged_bodies", "style"]
    assert checks.layout_problems({"people": 4, "traits": []}, 2, animals=1) == []      # 동물이 있으면 사람 수를 보지 않는다
    assert checks.layout_problems({"people": 1, "traits": [{"present": None}]}, 2) == []
    assert checks.layout_problems(None, 2) == ["layout:unchecked"]
    assert checks.layout_trait(1).startswith("Exactly 1 faceless mannequin figure with blank heads") and "3 faceless mannequin figures" in checks.layout_trait(3)
    assert checks.layout_trait(2).endswith("every head fully inside the frame")       # 몸이 아니라 머리 (클로즈업 구도는 몸이 잘린다)
    assert checks.describe(["layout:figures"]) == "구도 그림의 형상 수" and checks.describe(["layout:unchecked"]) == "구도 그림 확인 못 함"
    assert not checks.better(["style", "body:face"], ["layout:figures"])          # 형상 수가 틀린 것이 더 나쁘다


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("ok studio_checks")
