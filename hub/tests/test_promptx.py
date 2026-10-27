"""promptx 단위 테스트 (메타 규칙 검사, JSON 꺼내기, 검사 결과 정리). GPU·DB 없이 돈다.

  cd hub && python tests/test_promptx.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import promptx  # noqa: E402
from app.config import settings  # noqa: E402

GOOD = {
    "prompt": ("A plump tabby cat sitting upright on a vintage mahogany desk, front paws together, tail curled "
               "around its legs, a single cat in a quiet study, warm morning sunlight through a tall window, "
               "medium shot from a low eye-level angle with a 50mm lens, shallow depth of field, a cozy study "
               "with bookshelves softly blurred in the background, soft fluffy fur and polished wood grain, "
               "calm peaceful mood, gentle dust motes floating in the light beam"),
    "keep": ["tabby cat", "desk"],
    "categories": {"lighting": "warm morning sunlight through a tall window",
                   "camera": "medium shot from a low eye-level angle with a 50mm lens",
                   "environment": "a cozy study with bookshelves softly blurred in the background",
                   "texture": "soft fluffy fur and polished wood grain"},
    "people": 1,
}


def test_good_expansion_passes():
    assert promptx.check_expansion(GOOD, "", "책상 위 고양이") == []


def test_subject_and_category_rules():
    bad = dict(GOOD, keep=["dog"], categories={**GOOD["categories"], "lighting": "neon lights"})
    problems = promptx.check_expansion(bad)
    assert any('"dog"' in p for p in problems)
    assert any("lighting phrase" in p for p in problems)
    missing = dict(GOOD, categories={k: v for k, v in GOOD["categories"].items() if k != "texture"})
    assert any('"texture" is missing' in p for p in promptx.check_expansion(missing))


def test_negation_style_and_misread_rules():
    p = GOOD["prompt"] + ", no text, without blur, 8K, Unreal Engine, a dark silhouette by the door"
    problems = promptx.check_expansion(dict(GOOD, prompt=p), style="hand-drawn 2D animation style")
    joined = " ".join(problems)
    assert '"no"' in joined and '"without"' in joined
    assert '"8k"' in joined and '"unreal engine"' in joined
    assert "silhouette" in joined
    # 사용자가 직접 쓴 말은 막지 않는다
    assert not any("silhouette" in x for x in promptx.check_expansion(dict(GOOD, prompt=p), "", "silhouette art"))
    # 그림체를 고정하지 않았으면 품질 말은 허용
    assert not any("8k" in x for x in promptx.check_expansion(dict(GOOD, prompt=p)))


def test_length_rule():
    short = dict(GOOD, prompt="a cat on a desk, warm light")
    assert any("words" in p for p in promptx.check_expansion(short))


def test_parse_json_reply():
    assert promptx.parse_json_reply('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert promptx.parse_json_reply('Here you go: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    try:
        promptx.parse_json_reply("no json here")
    except promptx.PromptxError as e:
        assert e.status == 502
    else:
        raise AssertionError("JSON 이 없으면 오류여야 한다")


def test_final_prompt_appends_style_verbatim():
    style = "Hand-drawn 2D animation style, flat cel shading."
    assert promptx.final_prompt({"prompt": "a cat."}, style) == "a cat. " + style
    assert promptx.final_prompt({"prompt": "a cat"}) == "a cat."


def test_expand_messages_contain_rules_and_style():
    msgs = promptx.build_expand_messages("책상 위 고양이", "STYLE X", "손은 소매 속")
    assert msgs[0]["role"] == "system" and "Subject lock" in msgs[0]["content"]
    assert "STYLE X" in msgs[1]["content"] and "손은 소매 속" in msgs[1]["content"]


def test_parse_inspection():
    data = {"people": 2, "anatomy_ok": True, "issues": [],
            "traits": [{"trait": "hands hidden inside sleeves", "present": True}]}
    r = promptx.parse_inspection(data, ["hands hidden inside sleeves"])
    assert r["ok"] and r["people"] == 2 and r["traits"][0]["present"]
    # 문제가 하나라도 있으면 anatomy_ok 가 true 라도 실패로 본다
    bad = {"anatomy_ok": True, "issues": [{"type": "extra_limb", "where": "left", "detail": "three arms"}]}
    r = promptx.parse_inspection(bad)
    assert not r["ok"] and r["issues"][0]["type"] == "extra_limb"
    # 특징 문장을 바꿔 적어도 순서로 맞춘다, 빠지면 없음으로
    r = promptx.parse_inspection({"issues": [], "traits": [{"trait": "sleeves hide hands", "present": False}]},
                                 ["hands hidden inside sleeves"])
    assert not r["ok"] and r["traits"][0]["trait"] == "hands hidden inside sleeves"
    r = promptx.parse_inspection({"issues": []}, ["a"])
    assert not r["ok"]


def test_clean_expect():
    assert promptx.clean_expect("a\n\n b \n") == ["a", "b"]
    assert len(promptx.clean_expect([str(i) for i in range(20)])) == 8


def test_result_image_path_stays_inside():
    with tempfile.TemporaryDirectory() as d:
        settings.results_dir = d
        os.makedirs(os.path.join(d, "kim", "main"))
        open(os.path.join(d, "kim", "main", "a.png"), "wb").close()
        assert promptx.result_image_path("kim", "a.png", "main").endswith("a.png")
        for name, sub in [("../x.png", ""), ("a.png", "../../etc"), ("a.txt", "main"), ("b.png", "main")]:
            try:
                promptx.result_image_path("kim", name, sub)
            except promptx.PromptxError:
                continue
            raise AssertionError(f"막아야 한다: {sub}/{name}")


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"ok ({n} tests)")
