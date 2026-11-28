"""제작진 모드(B2): 분야별 전문가 지식을 붙인 초안으로 프롬프트 도우미를 돕는다 (docs/crew-mode.md).

- 사용자의 말이 최우선이다. 요청에 적힌 장르·스타일·날씨·색·조명·분위기(crew_lib.user_demands)는 메뉴 선택이 덮어쓸 수 없고,
  코드가 초안과 최종 글을 검사해 빠지면 고쳐 쓰게 한다 (11-11 시험: 'SF 영화처럼' 요청에서 SF 가 사라졌다).
- 단계마다 고를 선택지는 수천~수만 개 라이브러리(crew_library/*.jsonl)에서 요청마다 골라 메뉴로 준다. 사용자 말과 맞는 항목을
  먼저 넣고, 나머지는 여러 그룹에서 골고루 무작위로 채워 같은 선택('어두운 프리미엄')만 반복되지 않게 한다.
- 분야 메뉴가 있는 요청(제품·공간·풍경)에만 켜지고, 캐릭터 정본·손 숨김·사람이 나오는 요청은 기존 도우미 경로를 탄다.
  단 인물 라이브러리(cast_library.json)에 있는 정본 인물 장면은 정본 장면 분야(cast_scene·cast_film, CREW_CAST_MODE)로 보내
  빛·시간·카메라·구도·색·대기·분위기만 고르게 한다 (11-26, 모습·옷·손·소품·사람 수·화풍은 정본 그대로).
- 초안은 같은 GPU 차례 안에서 받고, 영문 메모는 도우미의 '꼭 지킬 특징' 칸에 더해진다 (정본 장면은 따로 'Crew look plan' 칸에).

순수 함수는 hub/tests/test_crew.py 가 GPU·DB 없이 검증한다.
"""
import asyncio
import random
import re
from functools import lru_cache

from . import cast_lib, crew_lib
from .config import settings
from .crew_lib import any_word, word_pattern
from .promptx import (HAND_WORDS_RE, NEGATION_RE, STYLE_CONFLICTS, PromptxError, hides_hands, parse_json_reply,
                      repair_message)

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
# 정본 장면 메모의 금지어 (11-26): 메뉴 금지어 + 사람 대명사('she raises one hand') + 손 낱말
CAST_PRONOUN_RE = re.compile(r"\b(?:she|he|her|his|him|hers|herself|himself)\b", re.I)
# 화풍 낱말: 화풍은 style 칸·라이브러리 화풍이 정한다 ('cinematic anime look' 이 반실사 정본을 덮는다)
CAST_STYLE_RE = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in STYLE_CONFLICTS) + r"|anime|manga|cartoons?|watercolou?rs?|"
                           r"illustrations?|illustrated|paintings?|painterly|photos?|photographs?|photographic|film still|"
                           r"ad look|concept art)\b", re.I)
# 외형 글에서 인물별 금지어를 뽑을 때 버리는 말: 꾸밈말·색·흔한 장면 말 (face·eyes 는 'eye level'·'soft light on the faces' 를 막지 않게,
# flowers·herb·chest 는 장소의 꽃밭·약초·상자를 막지 않게 뺀다)
_LOOK_STOP = set("""a an the with and over at of on in its two long short small big thin thick straight round dark pale light wild white
black brown red blue green grey gray golden ochre cream beige amber silver warm calm neat tall lean early around forties forty year
years old twelve twelve-year-old stocky bushy messy tousled worn muddy open stand-up stand up pointed middle parted chest neck
embroidered flowers metal knit linen herb eyes face wine wire flat his her rimmed brimmed deep soft rich""".split())


def spec_of(cat: str) -> dict:
    """분야 정의. 정본 장면 분야(cast_categories)는 route 점수에 끼지 않게 categories 와 따로 둔다"""
    kn = load_knowledge()
    return kn["categories"][cat] if cat in kn["categories"] else kn["cast_categories"][cat]


def steps_of(cat: str) -> list[dict]:
    return spec_of(cat)["steps"]


def is_cast(cat: str) -> bool:
    return cat in load_knowledge().get("cast_categories", {})


@lru_cache(maxsize=32)
def _guard_re(names: str):
    words = [n.strip() for n in names.split(",") if n.strip()]
    return any_word(words, crew_lib.GUARD_PARTICLES) if words else None


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


# ── 정본 인물 장면 (11-26, CREW_CAST_MODE) ──
def _guard_hits(text: str) -> set:
    """요청에 낱말 단위로 나온 정본 이름(소문자)"""
    t = re.sub(r"\s+", " ", text)
    return {w.lower() for w in (n.strip() for n in settings.crew_guard_names.split(",")) if w and _guard_re(w).search(t)}


def cast_entries(text: str, notes: str = "") -> list[dict]:
    """요청에 나온 정본 인물 중 인물 라이브러리에 있는 것 (같은 사람의 다른 모습 포함). 가드 이름이 낱말로 맞고 cast_lib.known_in 에도
    나와야 한다: known_in 은 한글 이름을 부분 일치로 찾아 '니키타' 에도 걸린다"""
    both = f"{text} {notes}"
    hits = _guard_hits(both)
    if not hits:
        return []
    known = cast_lib.known_in(both)
    base = {e["id"] for e in known if not e.get("variant_of") and any(n.lower() in hits for n in e["names"])}
    return [e for e in known if e["id"] in base or e.get("variant_of") in base]


def cast_block_reason(text: str, notes: str = "") -> str | None:
    """정본 이름이 있는 요청을 정본 장면 분야로 보낼 수 없는 이유 (보낼 수 있으면 None).
    hidden_hands: 11-10 V05 가 실패한 조건, character: 라이브러리에 없는 정본 이름(대조할 외형이 없다), person: 일반 사람 낱말
    (정해진 모습이 없어 제작진이 외형을 짓게 된다)"""
    both = re.sub(r"\s+", " ", f"{text} {notes}")
    if hides_hands(both):
        return "hidden_hands"
    ents = cast_entries(text, notes)
    if not ents:
        return "character"
    covered = {n.lower() for e in ents for n in e["names"]}
    if _guard_hits(both) - covered:
        return "character"
    if PERSON_REQ_RE.search(both):
        return "person"
    return None


def cast_route(text: str, notes: str = "", category: str = "cast_scene") -> dict | None:
    """정본 인물 장면의 분야 (CREW_CAST_MODE 가 off 거나 보낼 수 없으면 None). 색 단계는 라이브러리 화풍(rich warm colours)에 맞춰
    따뜻한·중립 묶음만 쓴다(palette, 11-26 사용자 결정 2). 사용자가 말한 색은 must 로 들어온다"""
    if settings.crew_cast_mode == "off" or cast_block_reason(text, notes):
        return None
    kn = load_knowledge()
    purposes = {w: d for w, d in kn["purpose_words"].items() if w in text}
    return {"category": category, "title": kn["cast_categories"][category]["title"], "score": 0, "purposes": purposes,
            "cast": [e["id"] for e in cast_entries(text, notes)], "palette": list(kn.get("cast_palette_warm") or []) or None}


def cast_by_id(ids) -> list[dict]:
    lib = {e["id"]: e for e in cast_lib.library()}
    return [lib[i] for i in ids or [] if i in lib]


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
    cast = is_cast(cat)
    # 정본 장면: 단계마다 정한 묶음(only)만, 손·옷·머리·지어낸 사람 항목은 빼고(ban), fits 는 보지 않는다(mood 는 character 표시가 0개)
    fcat, ban = (None, crew_lib.CAST_BAN_EN_RE) if cast else (cat, None)
    demands = crew_lib.query(text).demands
    menus = {}
    for st in steps_of(cat):
        must = []
        mine = [crew_lib.usable(d["ids"], text, fcat, ban) for d in demands if d["facet"] == st["facet"]]
        mine = [ids for ids in mine if ids]
        if len(mine) > 1:
            both = sorted(set(mine[0]).intersection(*mine[1:]))
            must += rng.sample(both, min(2, len(both)))
        for ids in mine:
            must += rng.sample(ids, min(2, len(ids)))
        if cast:
            # 색은 라이브러리 화풍에 맞춘 따뜻한·중립 묶음(palette)만. 사용자가 말한 색(must)은 화풍을 바꾸는 묶음(must_skip)만 아니면 넣는다
            only = (rt.get("palette") or st.get("only")) if st["facet"] == "color_palette" else st.get("only")
            menus[st["key"]] = crew_lib.retrieve(text, st["facet"], k=size, cat=None, rng=rng, must=must, only=only,
                                                 ban=ban, must_skip=st.get("must_skip"))
            continue
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

# 정본 인물 장면 (11-26): 인물은 시스템이 기준 그림으로 그린다. 제작진은 빛·시간·카메라·공간 배치·색·대기·분위기만 정한다
CAST_SYSTEM = """You are a small production crew of experts preparing {output}: director of photography and lighting designer (light, time, weather, colour of the light and surroundings), camera operator (shot size, angle, lens, layout of the space) and atmosphere artist (air and mood). The characters are fixed by the system from reference pictures; nobody in the crew designs them.
Rules:
{rules}
Field rule: {field_rule}
CAST (fixed by the system, never describe them):
{cast}
{purpose}MENU for this request (picked from a large library; each line shows id = Korean name (look)). Use only ids from the menu and build the notes around those choices.
{menu}
Return JSON only:
- "choices": for every step the chosen id.
- "ko": four short Korean sentences, one each for plan, direction, camera_lighting, details, explaining the choices to the user.
- "notes_en": {lo} to {hi} short positive English phrases (at most {words} words each) about the light, time and weather, camera distance and angle, the layout of the space, the colour of the light and surroundings, the air and the mood. Name a character only with a place in the frame (for example "Niki in the left third of the frame"); never write about a character's body, hands, arms, pose, clothes, hair, face, expression or belongings, never write he, she, his or her, never add other people, and never name an art style."""


def demand_lines(text: str, facets=None) -> list[str]:
    """사용자 요구를 지시문으로 (최우선). facets 를 주면 그 칸의 요구만 (정본 장면: 장르 요구는 화풍 칸이 맡는다)."""
    out = []
    for d in crew_lib.query(text).demands:
        if facets is not None and d["facet"] not in facets:
            continue
        out.append(f"- The user wrote '{d['tag']}' ({d['facet']}). This comes first: the image must clearly show it, "
                   f"using words such as: {', '.join(d['terms'][:6])}.")
    return out


def build_messages(text: str, rt: dict, style: str = "", notes: str = "", menus: dict | None = None,
                   cast: list[dict] | None = None) -> list[dict]:
    kn = load_knowledge()
    cat = rt["category"]
    spec = spec_of(cat)
    menus = menus or build_menus(f"{text} {notes}", rt)
    purpose = ""
    if rt.get("purposes"):
        purpose = ("PURPOSE named in the request (it decides composition and brightness, no menu choice may contradict it): "
                   + " | ".join(rt["purposes"].values()) + "\n")
    rules = "\n".join(f"- {r}" for r in kn["global_rules"])
    film = cat == "cast_film"
    output = "the shared look of every scene of one short film" if film else "one still image"
    if is_cast(cat):
        # 외형 전문은 넣지 않는다 (메모가 외형을 베껴 쓰지 않게). 알아볼 소품만 '보이게 둘 것' 으로 알린다
        lines = "\n".join(f"- {e['names'][-1]}: keep visible: {', '.join(e.get('signature') or []) or 'the reference look'}"
                          for e in (cast or []) if not e.get("variant_of"))
        system = CAST_SYSTEM.format(output=output, rules=rules, field_rule=spec["rule"], cast=lines or "- (none)",
                                    purpose=purpose, menu=menu_text(menus, cat), lo=MIN_NOTES, hi=MAX_NOTES,
                                    words=MAX_NOTE_WORDS)
    else:
        system = CREW_SYSTEM.format(rules=rules, field_rule=spec["rule"],
                                    purpose=purpose, menu=menu_text(menus, cat), lo=MIN_NOTES, hi=MAX_NOTES,
                                    words=MAX_NOTE_WORDS)
    parts = [f"Request (Korean): {text}", f"Output: {output}."]
    dl = demand_lines(f"{text} {notes}", {st["facet"] for st in steps_of(cat)} if is_cast(cat) else None)
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


CAST_PROBLEM = "For the fixed characters"


def cast_terms(entries) -> re.Pattern | None:
    """정본 인물의 외형(look)·알아볼 소품(signature)에서 뽑은 명사 금지어 ("Niki's cape glows deep teal" 의 cape)"""
    words = set()
    for e in entries or []:
        for src in [e.get("look") or ""] + list(e.get("signature") or []):
            for tok in re.findall(r"[a-z][a-z'-]*[a-z]", src.lower()):
                parts = tok.split("-")
                for w in {tok, *parts} if any(p not in _LOOK_STOP for p in parts) else ():
                    if len(w) < 3 or w in _LOOK_STOP:
                        continue
                    words.add(w)
                    if len(w) > 4 and w.endswith("s") and not w.endswith("ss") and not w.endswith("sses"):
                        words.add(w[:-1])          # braids → braid 도 잡는다 (glasses 는 glass 로 줄이지 않는다)
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")(?:s|es)?\b", re.I)


def cast_bad_words(line: str, terms: re.Pattern | None = None) -> list[str]:
    """정본 장면 메모 한 줄에 든 금지어: 손·몸 동작, 옷·머리, 사람·형상, 얼굴이 가려지는 화면, 대명사, 사람, 화풍, 인물별 외형 명사"""
    found = []
    for rx in (crew_lib.CAST_BAN_EN_RE, CAST_PRONOUN_RE, HAND_WORDS_RE, PERSON_NOTE_RE, CAST_STYLE_RE, terms):
        if rx is not None:
            found += [m.group(0).lower() for m in rx.finditer(line)]
    return found


def sanitize_cast_notes(result: dict, entries) -> dict | None:
    """마지막 회차에 정본 금지어만 남았을 때: 그 줄만 지운다. 남은 줄이 MIN_NOTES 보다 적으면 None (초안을 버린다)"""
    raw = result.get("notes_en") if isinstance(result, dict) else None
    if not isinstance(raw, list):
        return None
    terms = cast_terms(entries)
    keep = [n for n in raw if isinstance(n, str) and n.strip() and not cast_bad_words(n, terms)]
    if len(keep) < MIN_NOTES:
        return None
    return {**result, "notes_en": keep}


def check_crew(result, cat: str, user_text: str = "", request_text: str | None = None,
               menus: dict | None = None, cast: list[dict] | None = None) -> list[str]:
    """제작진 초안 검사. 어긋난 점을 영문 문장 목록으로 돌려준다 (그대로 고쳐 쓰기 지시에 넣는다).

    user_text 는 요청+특징(사람·손 규칙용), request_text 는 사용자 요구를 뽑을 글(없으면 user_text).
    menus 를 주면 고른 id 가 그 요청의 메뉴 안에 있는지까지 본다.
    cast 를 주면(정본 인물 장면) 그 분야 단계 칸의 요구만 보고, 메모 줄마다 정본 금지어(cast_bad_words)를 본다.
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
    if cast is not None:      # 장르 요구('SF 느낌')는 정본 장면 제작진이 아니라 화풍 칸이 맡는다
        facets = {st["facet"] for st in steps_of(cat)}
        demands = [d for d in demands if d["facet"] in facets]
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
    if cast is not None:
        # 사람 검사는 조건 없이 (cast_bad_words 안의 PERSON_NOTE_RE). 줄 단위로 모아 sanitize_cast_notes 가 같은 기준으로 지운다
        terms = cast_terms(cast)
        bad = sorted({w for n in notes for w in cast_bad_words(n, terms)})
        if bad:
            problems.append(f"{CAST_PROBLEM} (drawn by the system), write only about light, time, camera, the space, colour, "
                            "air and mood; remove: " + ", ".join(bad) + ".")
    elif not PERSON_REQ_RE.search(user_text) and PERSON_NOTE_RE.search(blob):
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
                rng: random.Random | None = None, cast: list[dict] | None = None, options: dict | None = None):
    """한 GPU 차례(llm_turn) 안에서 제작진 초안을 받는다. (결과, 회차, 남은 위반) 을 돌려준다.

    회차는 처음 시도를 포함한다(repair_rounds=1 이면 최대 2회). 위반이 남으면 호출한 쪽이 이 모드를 버린다.
    cast 는 정본 장면 분야의 인물 (없으면 rt["cast"] 로 찾는다). options 는 호출 options 에 더할 값: 구성안 차례 안에서는
    num_ctx 를 구성안과 같은 값으로 준다 (Ollama 는 num_ctx 가 바뀌면 모델을 다시 올린다). 온도는 늘 0.6.
    """
    rounds_max = settings.crew_repair_rounds if repair_rounds is None else repair_rounds
    cat = rt["category"]
    if cast is None and is_cast(cat):
        cast = cast_by_id(rt.get("cast"))
    said = f"{text} {notes}".strip()   # 사용자가 쓴 말 전부 (요청 + 특징 칸)
    # 라이브러리 검색은 CPU 를 0.1~1초 쓰므로 스레드에서 돌린다 (허브는 포털·중계·ComfyUI 화면을 한 프로세스로 처리한다)
    menus = await asyncio.to_thread(build_menus, said, rt, rng)
    messages = build_messages(text, rt, style, notes, menus, cast)
    schema = build_schema(cat, menus)
    opts = {**(options or {}), "temperature": 0.6}
    result, problems = {}, ["no answer"]
    for rnd in range(1, rounds_max + 2):
        data = await call({"messages": messages, "format": schema, "think": False, "options": dict(opts)})
        content = (data.get("message") or {}).get("content", "")
        try:
            result = parse_json_reply(content)
        except PromptxError as e:
            result, problems = {}, [str(e)]
        else:
            problems = check_crew(result, cat, said, said, menus, cast=cast)
        if not problems:
            return result, rnd, []
        messages = messages + [{"role": "assistant", "content": content}, repair_message(problems)]
    if cast is not None and problems and all(p.startswith(CAST_PROBLEM) for p in problems):
        # 남은 위반이 정본 금지어뿐이면 그 줄만 지운다 (나머지 줄은 빛·구도라 쓸 만하다). 지운 뒤 다시 검사해 요구 확인어가 빠졌으면 버린다
        cleaned = sanitize_cast_notes(result, cast)
        if cleaned is not None and not check_crew(cleaned, cat, said, said, menus, cast=cast):
            return cleaned, rounds_max + 1, []
    return result, rounds_max + 1, problems


# ── 영화 전체 초안 (긴 영상·대사 영상, CREW_CAST_MODE=safe) ──
LOOK_LIGHT_MAX, LOOK_AIR_MAX, LOOK_PART_MAX, LOOK_PLAN_MAX = 160, 100, 100, 600


def _clip(text: str, limit: int) -> str:
    t = " ".join(str(text or "").split()).rstrip(" .,;")
    if len(t) <= limit:
        return t
    cut = t[:limit]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:.")


def film_crew_on(text: str, notes: str = "") -> bool:
    """이 영화 요청에 영화 전체 제작진 초안을 쓰는가: 정본 인물이 나오고 CREW_CAST_MODE=safe"""
    return (settings.crew_mode != "off" and settings.crew_cast_mode == "safe"
            and cast_route(text, notes, "cast_film") is not None)


def film_look(result: dict, cat: str = "cast_film") -> dict:
    """구성안·장소 그림에 넣을 영화 빛 계획. LLM 메모가 아니라 고른 라이브러리 항목의 영문 원문(이미 거른 것)을 쓰고 길이를 묶는다:
    정지 그림 지시문(_with_style)은 넘치면 앞 글의 끝부터 잘라 compose 의 'hands not visible' 문장이 먼저 잘린다"""
    choices = result.get("choices") or {}
    got = {}
    for st in steps_of(cat):
        ids = _chosen(choices.get(st["key"]))
        e = crew_lib.entry(ids[0]) if ids else None
        if e:
            got[st["key"]] = e

    def en(k):
        return got[k]["en"] if k in got else ""

    return {"time": _clip(en("time_weather"), LOOK_PART_MAX),
            "light": _clip(", ".join(x for x in (en("lighting"), en("color")) if x), LOOK_LIGHT_MAX),
            "air": _clip(en("atmosphere"), LOOK_AIR_MAX), "mood": _clip(en("mood"), LOOK_PART_MAX),
            "light_terms": list(got["lighting"]["terms"]) if "lighting" in got else [],
            "picked": describe(result, cat)["picked"]}


def look_plan_text(look: dict | None) -> str:
    """구성안 지시에 붙이는 빛 계획 한 줄 (LOOK_PLAN_MAX 자 안: 구성안 차례 문맥 길이의 예약분을 넘지 않게)"""
    if not look:
        return ""
    parts = [f"{k}: {look[f]}" for k, f in (("time and weather", "time"), ("light and palette", "light"), ("air", "air"),
                                           ("mood", "mood")) if look.get(f)]
    return _clip("; ".join(parts), LOOK_PLAN_MAX)


async def draft_film(call, text: str, notes: str = "", num_ctx: int | None = None, rng: random.Random | None = None) -> dict | None:
    """구성안 차례(video_story) 안에서 영화 전체의 시간·빛·색·대기·분위기 초안 하나. 쓸 수 없는 요청이면 None.
    돌려주는 값 {category, cast, used, rounds, violations, picked, look}: look 이 있을 때만 구성안·장소 그림에 쓴다"""
    if not film_crew_on(text, notes):
        return None
    rt = cast_route(text, notes, "cast_film")
    cast = cast_by_id(rt["cast"])
    try:
        result, rounds, left = await draft(call, text, rt, "", notes, rng=rng, cast=cast,
                                           options={"num_ctx": num_ctx} if num_ctx else None)
    except Exception:  # noqa: BLE001 - 초안이 어떻게 깨져도 구성안은 예전처럼 쓴다
        result, rounds, left = {}, 0, ["crew draft failed"]
    info = {"category": "cast_film", "cast": rt["cast"], "used": not left, "rounds": rounds, "violations": left[:6]}
    if not left:
        look = film_look(result)
        info.update(picked=look["picked"], look=look)
    return info
