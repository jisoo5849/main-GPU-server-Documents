"""제작진 모드(B2): 분야별 전문가 지식을 붙인 초안으로 프롬프트 도우미를 돕는다 (docs/crew-mode.md).

- 사용자의 말이 최우선이다. 요청에 적힌 장르·스타일·날씨·색·조명·분위기(crew_lib.user_demands)는 메뉴 선택이 덮어쓸 수 없고,
  코드가 초안과 최종 글을 검사해 빠지면 고쳐 쓰게 한다 (11-11 시험: 'SF 영화처럼' 요청에서 SF 가 사라졌다).
- 단계마다 고를 선택지는 수천~수만 개 라이브러리(crew_library/*.jsonl)에서 요청마다 골라 메뉴로 준다. 사용자 말과 맞는 항목을
  먼저 넣고, 나머지는 여러 그룹에서 골고루 무작위로 채워 같은 선택('어두운 프리미엄')만 반복되지 않게 한다.
- 분야 메뉴가 있는 요청(제품·공간·풍경)에만 켜지고, 캐릭터 정본·손 숨김·사람이 나오는 요청은 기존 도우미 경로를 탄다.
- 초안은 같은 GPU 차례 안에서 받고, 영문 메모는 도우미의 '꼭 지킬 특징' 칸에 더해진다.

순수 함수는 hub/tests/test_crew.py 가 GPU·DB 없이 검증한다.
"""
import asyncio
import random
import re
from functools import lru_cache

from . import crew_lib
from .config import settings
from .crew_lib import any_word, word_pattern
from .promptx import HAND_WORDS_RE, NEGATION_RE, PromptxError, hides_hands, parse_json_reply, repair_message

KNOWLEDGE_PATH = crew_lib.KNOWLEDGE_PATH
load_knowledge = crew_lib.load_knowledge   # 지식 파일은 crew_lib 한 곳에서 읽는다
GENERIC_KEYWORDS = ("광고", "풍경")   # 여러 분야에 걸치는 말은 반 점만 준다
HANGUL_RE = re.compile(r"[가-힣]")
MIN_NOTES, MAX_NOTES, MAX_NOTE_WORDS, MAX_TOTAL_WORDS = 3, 6, 28, 110
MENU_SIZE = 8

# 요청에 사람이 있는지 (제작진 모드는 사람이 나오는 요청을 기존 도우미에 맡긴다)
PERSON_WORDS = ("사람", "인물", "모델", "소녀", "소년", "남자", "여자", "아이", "손님", "점원", "사장", "커플", "가족", "친구",
                "주인공", "청년", "노인", "마법사", "여행자", "아기", "학생", "person", "people", "man", "men", "woman", "women",
                "girl", "boy", "child", "children", "customer", "barista", "model", "wizard", "traveler")
PERSON_REQ_RE = any_word(PERSON_WORDS)
# 제작진 메모에 사람이 들어갔는지 (G0 V04: 신발 광고가 사람 다리 컷이 됐다). 시곗바늘(watch hands)·소파 팔걸이(arm)는 사람이 아니다.
PERSON_NOTE_RE = re.compile(
    # 'barista counter·station' 은 카페 공간의 가구다 (장문 영상 글 검토, 11-11)
    r"\b(?:person|people|men|woman|women|girl|boy|child|children|customer|"
    r"barista(?!(?:'s)? (?:counter|station|bar|area|stand)\b)|model|someone)\b"
    # 'man-made pier', 'human eye level', 'the slender figure of the bottle' 은 사람이 아니다 (영상 장문 2차 검토, 11-11)
    r"|\bman\b(?!-made)|\bhuman\b(?![- ](?:eye|scale)\b)"
    r"|\bfigure\b(?! of (?:the|a|an|its) (?!(?:person|man|woman|girl|boy|child)\b)\w)"
    r"|\b(?:his|her|their|human|model's|person's|woman's|man's)\s+(?:hands?|fingers?|arms?|legs?)\b"
    r"|\b(?:hands?|fingers?)\s+(?:holding|holds|gripping|grips|touching|touches|presenting|spraying|pouring)\b", re.I)
# 요청에 없는 글자·로고 (메모가 글자를 지시하지 못하게 한다). 'signs of wear', 'first signs of dawn', 'brand-new' 는 글자가 아니다.
# 'neon signs of nearby bars' 처럼 of 가 붙은 진짜 간판은 계속 잡는다
_SIGN_IDIOM = r"(?:wear|tear|age|ageing|aging|use|usage|life|time|damage|weathering|rust|patina|dawn|daybreak|sunrise|morning|spring)"
TEXT_NOTE_RE = re.compile(r"\b(?:text|letters?|lettering|logos?|brand(?:ing)?(?![- ]new\b)|captions?|slogan|typography|inscription|"
                          rf"labels?|signage|signs?(?! of (?:[a-z-]+ )?{_SIGN_IDIOM}\b)|words?|writing)\b|[\"“”]", re.I)


def steps_of(cat: str) -> list[dict]:
    return load_knowledge()["categories"][cat]["steps"]


@lru_cache(maxsize=8)
def _guard_re(names: str):
    words = [n.strip() for n in names.split(",") if n.strip()]
    return any_word(words) if words else None


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
    return {cat: [(k, re.compile(word_pattern(k), re.I)) for k in spec["keywords"]]
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


def build_menus(text: str, rt: dict, rng: random.Random | None = None, size: int = MENU_SIZE) -> dict:
    """단계마다 라이브러리에서 메뉴를 고른다. 사용자 요구 항목은 반드시 넣는다.

    text 는 요청과 사용자가 적은 특징을 합친 글이다(특징 칸에 쓴 'SF 느낌' 도 사용자 요구다).
    한 단계에 요구가 둘 이상이면(예: '눈 오는 밤') 둘 다 맞는 항목을 먼저 넣는다.
    """
    rng = rng or random.Random()
    cat = rt["category"]
    demands = crew_lib.query(text).demands
    menus = {}
    for st in steps_of(cat):
        must = []
        mine = [crew_lib.usable(d["ids"], text, cat) for d in demands if d["facet"] == st["facet"]]
        mine = [ids for ids in mine if ids]
        if len(mine) > 1:
            both = sorted(set(mine[0]).intersection(*mine[1:]))
            must += rng.sample(both, min(2, len(both)))
        for ids in mine:
            must += rng.sample(ids, min(2, len(ids)))
        prefer, wild, focus = st.get("prefer"), 0.25, None
        if st["facet"] == "location_set":
            hint = place_groups(text)
            if hint:              # 사용자가 장소를 말했으면 그 장소 묶음에서만 고른다 (예: '집 안' → 집 안의 방들)
                prefer, wild, focus = hint, 0, hint
        menus[st["key"]] = crew_lib.retrieve(text, st["facet"], k=size, cat=cat, rng=rng, must=must,
                                             prefer=prefer, wild=wild, focus=focus)
    return menus


@lru_cache(maxsize=1)
def _place_res() -> list:
    return [(re.compile(word_pattern(w), re.I), g) for w, g in load_knowledge().get("place_hints", {}).items()]


def place_groups(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text)
    out = []
    for rx, groups in _place_res():
        if rx.search(t):
            out += [g for g in groups if g not in out]
    return out


def menu_text(menus: dict, cat: str) -> str:
    lines = []
    for st in steps_of(cat):
        opts = "; ".join(f"{e['id']} = {e['ko']} ({e['en']})" for e in menus[st["key"]])
        pick = "choose 1 to 3 ids" if st.get("multi") else "choose exactly one id"
        lines.append(f"- {st['key']} ({st['title']}, {pick}): {opts}")
    return "\n".join(lines)


CREW_SYSTEM = """You are a small production crew of experts preparing one still image: planner (what and why), director (setting, subject action, composition), cinematographer and lighting designer (shot size, angle, lens, light setup, color), detail artist (materials, props, atmosphere).
Rules:
{rules}
Field rule: {field_rule}
{purpose}MENU for this request (picked from a large library; each line shows id = Korean name (look)). Use only ids from the menu and build the notes around those choices.
{menu}
Return JSON only:
- "choices": for every step the chosen id (genre: a list of 1 to 3 ids).
- "ko": four short Korean sentences, one each for plan, direction, camera_lighting, details, explaining the choices to the user.
- "notes_en": {lo} to {hi} short positive English phrases (at most {words} words each) that state the choices as things to be seen: subject and its arrangement, setting, light, composition, materials and one atmosphere detail."""


def demand_lines(text: str) -> list[str]:
    """사용자 요구를 지시문으로 (최우선)."""
    out = []
    for d in crew_lib.query(text).demands:
        out.append(f"- The user wrote '{d['tag']}' ({d['facet']}). This comes first: the image must clearly show it, "
                   f"using words such as: {', '.join(d['terms'][:6])}.")
    return out


def build_messages(text: str, rt: dict, style: str = "", notes: str = "", menus: dict | None = None) -> list[dict]:
    kn = load_knowledge()
    cat = rt["category"]
    spec = kn["categories"][cat]
    menus = menus or build_menus(f"{text} {notes}", rt)
    purpose = ""
    if rt.get("purposes"):
        purpose = ("PURPOSE named in the request (it decides composition and brightness, no menu choice may contradict it): "
                   + " | ".join(rt["purposes"].values()) + "\n")
    system = CREW_SYSTEM.format(rules="\n".join(f"- {r}" for r in kn["global_rules"]), field_rule=spec["rule"],
                                purpose=purpose, menu=menu_text(menus, cat), lo=MIN_NOTES, hi=MAX_NOTES,
                                words=MAX_NOTE_WORDS)
    parts = [f"Request (Korean): {text}", "Output: one still image."]
    dl = demand_lines(f"{text} {notes}")
    if dl:
        parts.append("USER WORDS (top priority, choose the matching menu ids and show them all in the notes; when one step "
                     "has two of them, pick an id that fits both or the closest one and show both in the notes):\n"
                     + "\n".join(dl))
    if notes:
        parts.append(f"Must-keep details from the user (do not contradict them): {notes}")
    if style:
        parts.append(f"The system will append this style at the end; do not repeat or change it: {style}")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]


def build_schema(cat: str, menus: dict) -> dict:
    s = {"type": "string"}
    props, req = {}, []
    for st in steps_of(cat):
        enum = [e["id"] for e in menus[st["key"]]]
        if st.get("multi"):
            props[st["key"]] = {"type": "array", "items": {"type": "string", "enum": enum}, "minItems": 1, "maxItems": 3}
        else:
            props[st["key"]] = {"type": "string", "enum": enum}
        req.append(st["key"])
    return {"type": "object", "properties": {
        "choices": {"type": "object", "properties": props, "required": req},
        "ko": {"type": "object", "properties": {k: s for k in ("plan", "direction", "camera_lighting", "details")},
               "required": ["plan", "direction", "camera_lighting", "details"]},
        "notes_en": {"type": "array", "items": s, "minItems": MIN_NOTES, "maxItems": MAX_NOTES}},
        "required": ["choices", "ko", "notes_en"]}


def _chosen(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def check_crew(result, cat: str, user_text: str = "", request_text: str | None = None,
               menus: dict | None = None) -> list[str]:
    """제작진 초안 검사. 어긋난 점을 영문 문장 목록으로 돌려준다 (그대로 고쳐 쓰기 지시에 넣는다).

    user_text 는 요청+특징(사람·손 규칙용), request_text 는 사용자 요구를 뽑을 글(없으면 user_text).
    menus 를 주면 고른 id 가 그 요청의 메뉴 안에 있는지까지 본다.
    모델이 스키마를 어겨 엉뚱한 형식(리스트, 숫자 등)을 줘도 예외 없이 위반으로 돌려준다.
    """
    if not isinstance(result, dict):
        return ["Return a JSON object with choices, ko and notes_en."]
    problems: list[str] = []
    choices = result.get("choices") if isinstance(result.get("choices"), dict) else {}
    picked_by_facet = {}
    for st in steps_of(cat):
        ids = _chosen(choices.get(st["key"]))
        if not st.get("multi") and len(ids) != 1 or st.get("multi") and not 1 <= len(ids) <= 3:
            problems.append(f'Step "{st["key"]}" needs {"1 to 3 ids" if st.get("multi") else "one id"} from the menu.')
            continue
        offered = {e["id"] for e in menus[st["key"]]} if menus and st["key"] in menus else None
        for i in ids:
            e = crew_lib.entry(i)
            if not e or e["facet"] != st["facet"] or (offered is not None and i not in offered):
                problems.append(f'Step "{st["key"]}" has an id that is not in the menu: {i}.')
        picked_by_facet.setdefault(st["facet"], set()).update(ids)
    ko = result.get("ko") if isinstance(result.get("ko"), dict) else {}
    for k in ("plan", "direction", "camera_lighting", "details"):
        if not HANGUL_RE.search(str(ko.get(k) or "")):
            problems.append(f'"ko.{k}" must be a Korean sentence.')
    raw = result.get("notes_en")
    notes = [str(n).strip() for n in raw if isinstance(n, (str, int, float)) and str(n).strip()] if isinstance(raw, list) else []
    if not MIN_NOTES <= len(notes) <= MAX_NOTES:
        problems.append(f'"notes_en" must be a list of {MIN_NOTES} to {MAX_NOTES} lines; it has {len(notes)}.')
    blob = " ".join(notes)
    # 사용자 말이 최우선: 요구마다 메뉴 선택과 글 둘 다에서 지켜졌는지 본다
    demands = crew_lib.query(request_text if request_text is not None else user_text).demands
    problems += _demand_choice_problems(demands, picked_by_facet, cat, menus)
    for d in demands:
        if not crew_lib.has_term(blob, d["terms"]):
            problems.append(f"The user wrote '{d['tag']}'; notes_en must clearly show it with words such as: "
                            + ", ".join(d["terms"][:6]) + ".")
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


def _demand_choice_problems(demands: list[dict], picked_by_facet: dict, cat: str, menus: dict | None) -> list[str]:
    """요구가 있는 단계에서 그 요구와 맞는 id 를 골랐는지.

    하나만 고르는 단계에 요구가 둘이면(예: '눈 오는 밤' 의 눈과 밤) 둘 다 맞는 항목이 드물어, 어느 하나와 맞으면 된다
    (나머지는 영문 메모의 확인어로 지킨다). 장르처럼 여러 개를 고르는 단계는 요구마다 맞아야 한다(3개까지).
    메뉴에 그 요구와 맞는 항목이 없으면(사람이 나오는 항목뿐이라 거른 경우 등) 선택은 묻지 않는다.
    """
    out = []
    for st in steps_of(cat):
        if st["facet"] not in picked_by_facet:
            continue
        offered = {e["id"] for e in menus[st["key"]]} if menus and st["key"] in menus else None
        mine = []
        for d in demands:
            if d["facet"] != st["facet"]:
                continue
            ids = set(d["ids"]) & offered if offered is not None else set(d["ids"])
            if ids:
                mine.append((d, ids))
        if not mine:
            continue
        picked = picked_by_facet[st["facet"]]
        if st.get("multi") and len(mine) <= 3:
            for d, ids in mine:
                if not picked & ids:
                    out.append(f"The user wrote '{d['tag']}', so step \"{st['key']}\" must include a matching id.")
        elif not any(picked & ids for _, ids in mine):
            tags = "', '".join(d["tag"] for d, _ in mine)
            out.append(f"The user wrote '{tags}', so step \"{st['key']}\" must use an id that matches one of them.")
    return out


def describe(result: dict, cat: str) -> dict:
    """패널에 보여 줄 한국어 지시서와 선택한 항목 (한국어 이름). 검사를 통과한 초안에만 쓴다."""
    choices = result.get("choices") or {}
    picked = []
    for st in steps_of(cat):
        ids = _chosen(choices.get(st["key"]))
        names = [crew_lib.entry(i)["ko"] for i in ids if crew_lib.entry(i)]
        picked.append({"step": st["title"], "id": ids[0] if len(ids) == 1 else ids, "ko": ", ".join(names)})
    ko = result.get("ko") or {}
    return {"picked": picked, "ko": {k: str(ko.get(k) or "") for k in ("plan", "direction", "camera_lighting", "details")}}


def merge_notes(user_notes: str, result: dict) -> str:
    """도우미의 '꼭 지킬 특징' 칸에 넣을 글: 사용자가 적은 특징 + 제작진 영문 메모."""
    lines = [str(n).strip().rstrip(".") for n in result.get("notes_en") or [] if str(n).strip()]
    crew = ". ".join(lines)
    user_notes = (user_notes or "").strip()
    return f"{user_notes.rstrip('.')}. {crew}" if user_notes and crew else (user_notes or crew)


async def draft(call, text: str, rt: dict, style: str = "", notes: str = "", repair_rounds: int | None = None,
                rng: random.Random | None = None):
    """한 GPU 차례(llm_turn) 안에서 제작진 초안을 받는다. (결과, 회차, 남은 위반) 을 돌려준다.

    회차는 처음 시도를 포함한다(repair_rounds=1 이면 최대 2회). 위반이 남으면 호출한 쪽이 이 모드를 버린다.
    """
    rounds_max = settings.crew_repair_rounds if repair_rounds is None else repair_rounds
    said = f"{text} {notes}".strip()   # 사용자가 쓴 말 전부 (요청 + 특징 칸)
    # 라이브러리 검색은 CPU 를 0.1~1초 쓰므로 스레드에서 돌린다 (허브는 포털·중계·ComfyUI 화면을 한 프로세스로 처리한다)
    menus = await asyncio.to_thread(build_menus, said, rt, rng)
    messages = build_messages(text, rt, style, notes, menus)
    schema = build_schema(rt["category"], menus)
    result, problems = {}, ["no answer"]
    for rnd in range(1, rounds_max + 2):
        data = await call({"messages": messages, "format": schema, "think": False, "options": {"temperature": 0.6}})
        content = (data.get("message") or {}).get("content", "")
        try:
            result = parse_json_reply(content)
        except PromptxError as e:
            result, problems = {}, [str(e)]
        else:
            problems = check_crew(result, rt["category"], said, said, menus)
        if not problems:
            return result, rnd, []
        messages = messages + [{"role": "assistant", "content": content}, repair_message(problems)]
    return result, rounds_max + 1, problems
