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
    assert t.startswith("A person who looks like this is fully inside the frame and the face is clearly visible from the front or three-quarter: ")
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("ok studio_checks")
