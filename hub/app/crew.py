"""제작진 모드(B2): 분야별 전문가 지식을 붙인 초안으로 프롬프트 도우미를 돕는다.

2026-11-10 G0 A/B 시험 결과(docs/crew-mode.md): 같은 모델이 두 번 추측하는 '지식 없는 제작진'은 도우미보다 낫지 않았고,
분야별 선택 메뉴와 전문가 규칙을 붙인 초안(B2)이 제품·공간·풍경에서 더 상업적인 결과를 냈다. 그래서 이 모드는
- 분야 메뉴가 있는 요청(제품·공간·풍경)에만 켜지고, 그 밖의 요청과 캐릭터 정본·손 숨김·사람이 나오는 요청은 기존 도우미 경로를 탄다.
- LLM 이 메뉴에서 단계마다 하나씩 고르고(스키마 enum 으로 강제), 한국어 지시서 네 칸과 영문 메모 3~6줄을 쓴다.
- 코드가 검사하고(check_crew) 어긋나면 고쳐 쓰게 한다. 그래도 어긋나면 이 모드를 버리고 도우미로 돌아간다.
- 영문 메모는 도우미의 '꼭 지킬 특징' 칸에 더해지고, 최종 프롬프트는 도우미가 만든다 (같은 GPU 차례 안에서).

이 파일의 순수 함수는 hub/tests/test_crew.py 가 GPU·DB 없이 검증한다.
"""
import json
import os
import re
from functools import lru_cache

from .config import settings
from .promptx import HAND_WORDS_RE, NEGATION_RE, PromptxError, hides_hands, parse_json_reply, repair_message

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crew_knowledge.json")
GENERIC_KEYWORDS = ("광고", "풍경")   # 여러 분야에 걸치는 말은 반 점만 준다
HANGUL_RE = re.compile(r"[가-힣]")
MIN_NOTES, MAX_NOTES, MAX_NOTE_WORDS, MAX_TOTAL_WORDS = 3, 6, 28, 110

# 한글 낱말은 앞뒤가 한글이면 다른 낱말의 일부다 ("아이스크림" 속 "아이", "우산" 속 "산"). 조사만 허용한다.
PARTICLES = ("이랑|이나|이란|이라는|에서|에도|으로|처럼|같은|같이|하고|까지|부터|들|이|가|은|는|을|를|의|에|와|과|도|로|만|나|랑")


def _ko_word(word: str) -> str:
    return rf"(?<![가-힣A-Za-z0-9]){re.escape(word)}(?:{PARTICLES})?(?![가-힣A-Za-z0-9])"


def _word(word: str) -> str:
    """키워드 하나를 낱말 단위 정규식으로 (영문은 \\b 와 복수 s, 한글은 위 규칙)."""
    word = re.sub(r"\s+", " ", word.strip())
    if word.isascii():
        return rf"\b{re.escape(word)}s?\b"
    return _ko_word(word)


def _any_word(words) -> re.Pattern:
    return re.compile("|".join(_word(w) for w in words), re.I)


# 요청에 사람이 있는지 (제작진 모드는 사람이 나오는 요청을 기존 도우미에 맡긴다)
PERSON_WORDS = ("사람", "인물", "모델", "소녀", "소년", "남자", "여자", "아이", "손님", "점원", "사장", "커플", "가족", "친구",
                "주인공", "청년", "노인", "마법사", "여행자", "아기", "학생", "person", "people", "man", "men", "woman", "women",
                "girl", "boy", "child", "children", "customer", "barista", "model", "wizard", "traveler")
PERSON_REQ_RE = _any_word(PERSON_WORDS)
# 제작진 메모에 사람이 들어갔는지 (G0 V04: 신발 광고가 사람 다리 컷이 됐다). 시곗바늘(watch hands)·소파 팔걸이(arm)는 사람이 아니다.
PERSON_NOTE_RE = re.compile(
    r"\b(?:person|people|man|men|woman|women|girl|boy|child|children|customer|barista|model|human|someone|figure)\b"
    r"|\b(?:his|her|their|human|model's|person's|woman's|man's)\s+(?:hands?|fingers?|arms?|legs?)\b"
    r"|\b(?:hands?|fingers?)\s+(?:holding|holds|gripping|grips|touching|touches|presenting|spraying|pouring)\b", re.I)
# 요청에 없는 글자·로고 (메모가 글자를 지시하지 못하게 한다)
TEXT_NOTE_RE = re.compile(r"\b(?:text|letters?|lettering|logos?|brand(?:ing)?|captions?|slogan|typography|inscription|"
                          r"labels?|signage|signs?|words?|writing)\b|[\"“”]", re.I)


@lru_cache(maxsize=1)
def load_knowledge() -> dict:
    with open(KNOWLEDGE_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=8)
def _guard_re(names: str):
    words = [n.strip() for n in names.split(",") if n.strip()]
    return _any_word(words) if words else None


def mentions_guarded_name(text: str) -> bool:
    """캐릭터 정본 이름이 요청에 있는지. 이름 뒤에 조사만 붙은 경우만 센다 ("베스킨라빈스", "니키타" 는 아니다)."""
    rx = _guard_re(settings.crew_guard_names)
    return bool(rx and rx.search(re.sub(r"\s+", " ", text)))


def skip_reason(text: str, notes: str = "") -> str | None:
    """제작진 모드를 건너뛸 이유. 없으면 None. character: 캐릭터 정본, hidden_hands: 손 숨김, person: 사람이 나오는 요청."""
    both = re.sub(r"\s+", " ", f"{text} {notes}")
    if mentions_guarded_name(both):
        return "character"
    if hides_hands(both):
        return "hidden_hands"
    if PERSON_REQ_RE.search(both):
        return "person"
    return None


@lru_cache(maxsize=1)
def _keyword_res() -> dict:
    return {cat: [(k, re.compile(_word(k), re.I)) for k in spec["keywords"]]
            for cat, spec in load_knowledge()["categories"].items()}


def route(text: str, notes: str = "") -> dict | None:
    """요청을 분야로 나눈다. 건너뛸 이유가 있거나 점수가 같은 모호한 요청은 None (기존 도우미로).

    지금은 낱말 단위 키워드 점수다. 요청이 늘면 bge-m3 임베딩 검색으로 바꾼다 [TBD].
    """
    if skip_reason(text, notes):
        return None
    both = re.sub(r"\s+", " ", f"{text} {notes}")
    kn = load_knowledge()
    scores = {}
    for cat, pats in _keyword_res().items():
        s = sum((0.5 if k in GENERIC_KEYWORDS else 1.0) for k, rx in pats if rx.search(both))
        if s > 0:
            scores[cat] = s
    if not scores:
        return None
    best = max(scores.values())
    top = [c for c, s in scores.items() if s == best]
    if len(top) != 1:
        return None
    purposes = {w: d for w, d in kn["purpose_words"].items() if w in text}
    return {"category": top[0], "title": kn["categories"][top[0]]["title"], "score": best, "purposes": purposes}


def menu_text(cat: str) -> str:
    lines = []
    for st in load_knowledge()["categories"][cat]["steps"]:
        opts = "; ".join(f"{o['id']} = {o['ko']} ({o['en']})" for o in st["options"])
        lines.append(f"- {st['key']} ({st['title']}): {opts}")
    return "\n".join(lines)


CREW_SYSTEM = """You are a small production crew of experts preparing one still image: planner (what and why), director (setting, subject action, composition), cinematographer and lighting designer (shot size, angle, lens, light setup, color), detail artist (materials, props, atmosphere).
Rules:
{rules}
Field rule: {field_rule}
{purpose}EXPERT MENU for this field. For every step choose exactly one option id that best fits the request, and build the notes around those choices. Never invent an id outside the menu.
{menu}
Return JSON only:
- "choices": the chosen id for every step.
- "ko": four short Korean sentences, one each for plan, direction, camera_lighting, details, explaining the choices to the user.
- "notes_en": {lo} to {hi} short positive English phrases (at most {words} words each) that state the choices as things to be seen: subject and its arrangement, setting, light, composition, materials and one atmosphere detail."""


def build_messages(text: str, rt: dict, style: str = "", notes: str = "") -> list[dict]:
    kn = load_knowledge()
    spec = kn["categories"][rt["category"]]
    purpose = ""
    if rt.get("purposes"):
        purpose = ("PURPOSE named in the request (it decides composition and brightness, no menu choice may contradict it): "
                   + " | ".join(rt["purposes"].values()) + "\n")
    system = CREW_SYSTEM.format(rules="\n".join(f"- {r}" for r in kn["global_rules"]), field_rule=spec["rule"],
                                purpose=purpose, menu=menu_text(rt["category"]), lo=MIN_NOTES, hi=MAX_NOTES,
                                words=MAX_NOTE_WORDS)
    parts = [f"Request (Korean): {text}", "Output: one still image."]
    if notes:
        parts.append(f"Must-keep details from the user (do not contradict them): {notes}")
    if style:
        parts.append(f"The system will append this style at the end; do not repeat or change it: {style}")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]


def build_schema(cat: str) -> dict:
    s = {"type": "string"}
    steps = load_knowledge()["categories"][cat]["steps"]
    return {"type": "object", "properties": {
        "choices": {"type": "object", "properties": {st["key"]: {"type": "string", "enum": [o["id"] for o in st["options"]]}
                                                     for st in steps}, "required": [st["key"] for st in steps]},
        "ko": {"type": "object", "properties": {k: s for k in ("plan", "direction", "camera_lighting", "details")},
               "required": ["plan", "direction", "camera_lighting", "details"]},
        "notes_en": {"type": "array", "items": s, "minItems": MIN_NOTES, "maxItems": MAX_NOTES}},
        "required": ["choices", "ko", "notes_en"]}


def check_crew(result, cat: str, user_text: str = "") -> list[str]:
    """제작진 초안 검사. 어긋난 점을 영문 문장 목록으로 돌려준다 (그대로 고쳐 쓰기 지시에 넣는다).

    모델이 스키마를 어겨 엉뚱한 형식(리스트, 숫자 등)을 줘도 예외 없이 위반으로 돌려준다.
    """
    if not isinstance(result, dict):
        return ["Return a JSON object with choices, ko and notes_en."]
    problems: list[str] = []
    steps = load_knowledge()["categories"][cat]["steps"]
    choices = result.get("choices") if isinstance(result.get("choices"), dict) else {}
    for st in steps:
        ids = {o["id"] for o in st["options"]}
        v = choices.get(st["key"])
        if not isinstance(v, str) or v not in ids:
            problems.append(f'Step "{st["key"]}" needs one id from: {", ".join(sorted(ids))}.')
    ko = result.get("ko") if isinstance(result.get("ko"), dict) else {}
    for k in ("plan", "direction", "camera_lighting", "details"):
        if not HANGUL_RE.search(str(ko.get(k) or "")):
            problems.append(f'"ko.{k}" must be a Korean sentence.')
    raw = result.get("notes_en")
    notes = [str(n).strip() for n in raw if isinstance(n, (str, int, float)) and str(n).strip()] if isinstance(raw, list) else []
    if not MIN_NOTES <= len(notes) <= MAX_NOTES:
        problems.append(f'"notes_en" must be a list of {MIN_NOTES} to {MAX_NOTES} lines; it has {len(notes)}.')
    blob = " ".join(notes)
    if HANGUL_RE.search(blob):
        problems.append('"notes_en" must be English only.')
    if len(blob.split()) > MAX_TOTAL_WORDS or any(len(n.split()) > MAX_NOTE_WORDS for n in notes):
        problems.append(f"Keep each note under {MAX_NOTE_WORDS} words and all notes under {MAX_TOTAL_WORDS} words.")
    seen = set()
    for m in NEGATION_RE.finditer(blob):
        w = m.group(0).lower()
        if w not in seen:
            seen.add(w)
            problems.append(f'Negative wording "{m.group(0)}" in notes_en; state what should be seen instead.')
    if TEXT_NOTE_RE.search(blob):
        problems.append("Do not mention text, letters, logos, brand names, labels or quotation marks in notes_en.")
    if not PERSON_REQ_RE.search(user_text) and PERSON_NOTE_RE.search(blob):
        problems.append("The request names no person, so do not put any person or human hands in notes_en.")
    if hides_hands(user_text):
        found = sorted({m.group(0).lower() for m in HAND_WORDS_RE.finditer(blob)})
        if found:
            problems.append("The hands must stay hidden, so remove: " + ", ".join(found) + ".")
    return problems


def describe(result: dict, cat: str) -> dict:
    """패널에 보여 줄 한국어 지시서와 선택한 항목 (한국어 이름). 검사를 통과한 초안에만 쓴다."""
    steps = load_knowledge()["categories"][cat]["steps"]
    by_id = {st["key"]: {o["id"]: o["ko"] for o in st["options"]} for st in steps}
    choices = result.get("choices") or {}
    picked = [{"step": st["title"], "id": choices.get(st["key"]), "ko": by_id[st["key"]].get(choices.get(st["key"]), "")}
              for st in steps]
    ko = result.get("ko") or {}
    return {"picked": picked, "ko": {k: str(ko.get(k) or "") for k in ("plan", "direction", "camera_lighting", "details")}}


def merge_notes(user_notes: str, result: dict) -> str:
    """도우미의 '꼭 지킬 특징' 칸에 넣을 글: 사용자가 적은 특징 + 제작진 영문 메모."""
    lines = [str(n).strip().rstrip(".") for n in result.get("notes_en") or [] if str(n).strip()]
    crew = ". ".join(lines)
    user_notes = (user_notes or "").strip()
    return f"{user_notes.rstrip('.')}. {crew}" if user_notes and crew else (user_notes or crew)


async def draft(call, text: str, rt: dict, style: str = "", notes: str = "", repair_rounds: int | None = None):
    """한 GPU 차례(llm_turn) 안에서 제작진 초안을 받는다. (결과, 회차, 남은 위반) 을 돌려준다.

    회차는 처음 시도를 포함한다(repair_rounds=1 이면 최대 2회). 위반이 남으면 호출한 쪽이 이 모드를 버린다.
    도우미가 같은 차례에서 이어 받으므로 모델을 다시 올리지 않는다.
    """
    rounds_max = settings.crew_repair_rounds if repair_rounds is None else repair_rounds
    messages = build_messages(text, rt, style, notes)
    schema = build_schema(rt["category"])
    result, problems = {}, ["no answer"]
    for rnd in range(1, rounds_max + 2):
        data = await call({"messages": messages, "format": schema, "think": False, "options": {"temperature": 0.4}})
        content = (data.get("message") or {}).get("content", "")
        try:
            result = parse_json_reply(content)
        except PromptxError as e:
            result, problems = {}, [str(e)]
        else:
            problems = check_crew(result, rt["category"], f"{text} {notes}")
        if not problems:
            return result, rnd, []
        messages = messages + [{"role": "assistant", "content": content}, repair_message(problems)]
    return result, rounds_max + 1, problems
