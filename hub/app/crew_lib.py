"""제작진·도우미 공용 선택지 라이브러리 (docs/crew-mode.md).

수천~수만 개의 선택지(장르·장소·조명·카메라·색·재질·소품·분위기·움직임 …)를 `crew_library/*.jsonl` 에 두고,
요청마다 관련된 것만 골라(검색) LLM 에 보여 준다. 수만 개를 한 번에 프롬프트에 넣을 수는 없기 때문이다.

- user_demands: 요청에 사용자가 직접 적은 장르·날씨·색·조명·분위기 말. 이것이 최우선이고 덮어쓸 수 없다.
- retrieve: 분야·단계별 메뉴. 사용자 말에 맞는 항목을 먼저 넣고, 나머지는 여러 그룹에서 골고루 무작위로 채운다
  (같은 '어두운 프리미엄' 만 반복되던 쏠림을 줄인다).

GPU·DB 를 쓰지 않는 순수 함수이며 hub/tests/test_crew.py 가 검증한다.
"""
import json
import logging
import os
import random
import re
from functools import lru_cache

log = logging.getLogger("mgs.crew_lib")

LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crew_library")
FACETS = ("genre_style", "location_set", "lighting", "time_weather", "camera", "composition", "color_palette",
          "material_texture", "atmosphere", "props", "mood_emotion", "product_presentation", "video_motion")
# 사용자가 이 분야의 말을 쓰면 반드시 지킨다 (장소·소품은 주제 유지 규칙이 이미 지킨다)
ENFORCED_FACETS = ("genre_style", "time_weather", "color_palette", "lighting", "mood_emotion")
NONE_GENRE = "genre_style.none"

# 한글 낱말은 앞뒤가 한글이면 다른 낱말의 일부다 ("아이스크림" 속 "아이", "우산" 속 "산"). 조사만 허용한다.
PARTICLES = ("이랑|이나|이란|이라는|에서|에도|으로|처럼|같은|같이|하고|까지|부터|스러운|스럽게|적인|적으로|풍의|풍|"
             "들|이|가|은|는|을|를|의|에|와|과|도|로|만|나|랑|한|하게")


def _ko_word(word: str) -> str:
    return rf"(?<![가-힣A-Za-z0-9]){re.escape(word)}(?:{PARTICLES})?(?![가-힣A-Za-z0-9])"


def word_pattern(word: str) -> str:
    """키워드 하나를 낱말 단위 정규식으로 (영문은 \\b 와 복수 s, 한글은 조사만 허용)."""
    word = re.sub(r"\s+", " ", word.strip())
    if word.isascii():
        return rf"(?<![A-Za-z0-9]){re.escape(word)}s?(?![A-Za-z0-9])"
    return _ko_word(word)


def any_word(words) -> re.Pattern:
    return re.compile("|".join(word_pattern(w) for w in words), re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


@lru_cache(maxsize=1)
def load_library() -> dict:
    """crew_library/*.jsonl 을 읽는다. 같은 id 는 먼저 읽은 것을 쓴다(seed.jsonl 이 먼저)."""
    entries, by_facet, tags, toks = {}, {f: [] for f in FACETS}, {}, {}
    files = sorted(os.listdir(LIB_DIR)) if os.path.isdir(LIB_DIR) else []
    files.sort(key=lambda f: (f != "seed.jsonl", f))
    bad = 0
    for fn in files:
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(LIB_DIR, fn), encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if e["facet"] not in by_facet or e["id"] in entries or not e["en"] or not e["terms"]:
                        continue
                except (ValueError, KeyError, TypeError):
                    bad += 1
                    continue
                e.setdefault("fits", ["any"])
                e.setdefault("group", "misc")
                entries[e["id"]] = e
                by_facet[e["facet"]].append(e["id"])
                for t in list(e.get("tags_ko") or []) + list(e.get("tags_en") or []):
                    t = _norm(t)
                    if len(t.replace(" ", "")) < 2 or t in STOP_TOKENS:   # 'sf', '3d' 같은 두 글자 영문은 쓴다
                        continue
                    tags.setdefault(t.lower(), set()).add(e["id"])
                # 낱말 색인: '도시 거리 풍경' 같은 구절 태그도 사용자가 쓴 '거리' 한 낱말과 맞게 한다
                for tok in tokens(" ".join([e["ko"]] + list(e.get("tags_ko") or []) + list(e.get("tags_en") or []))):
                    toks.setdefault(tok, set()).add(e["id"])
    if bad:
        log.warning("crew_library: 읽지 못한 줄 %d", bad)
    return {"entries": entries, "by_facet": by_facet, "tags": tags, "tokens": toks}


# 낱말 색인에서 뺄 흔한 말 (요청에 늘 나와서 관련성을 흐린다)
STOP_TOKENS = {"오는", "같은", "있는", "하는", "느낌", "분위기", "사진", "이미지", "장면", "스타일", "광고", "영상", "만들어",
               "보이는", "처럼", "그림", "배경", "조명", "색감", "질감", "구도", "카메라", "소품", "연출", "무드", "감성", "효과",
               "제품", "공간", "풍경", "느낌의", "스러운", "the", "and", "with", "look", "style", "photo", "image", "scene",
               "light", "color", "mood", "product", "shot", "view"}
TOKEN_SUFFIXES = sorted(PARTICLES.split("|"), key=len, reverse=True)


def tokens(text: str) -> set:
    """한국어는 두 글자 이상 낱말에서 조사를 떼고, 영문은 네 글자 이상 낱말을 쓴다."""
    out = set()
    for w in re.findall(r"[가-힣]{2,}", str(text or "")):
        for suf in TOKEN_SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= 2:
                w = w[: -len(suf)]
                break
        if w not in STOP_TOKENS:
            out.add(w)
    out |= {w for w in re.findall(r"[a-z]{4,}", str(text or "").lower()) if w not in STOP_TOKENS}
    return out


@lru_cache(maxsize=None)
def _tag_re(tag: str) -> re.Pattern:
    # 태그가 4만 개라 한꺼번에 컴파일하면 51초가 걸렸다(개발 PC). 요청 글에 실제로 들어 있는 태그만 그때 만든다.
    return re.compile(word_pattern(tag), re.I)


def entry(eid: str) -> dict | None:
    return load_library()["entries"].get(eid)


def mentions(text: str) -> dict:
    """요청에 나온 태그 → 그 태그를 가진 항목 id 들. 낱말 단위로만 센다."""
    t = _norm(text)
    tl = t.lower()
    lib = load_library()
    out = {}
    for tag, ids in lib["tags"].items():
        if tag in tl and _tag_re(tag).search(t):
            out[tag] = ids
    return out


INTENT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crew_intent.json")


# 한글 음절 조합 (형용사 활용형을 만들 때 받침을 바꾼다)
_JONG_N, _JONG_B, _JUNG_EU = 4, 17, 18


def _split(ch: str):
    c = ord(ch) - 0xAC00
    return c // 588, (c % 588) // 28, c % 28


def _join(cho: int, jung: int, jong: int) -> str:
    return chr(0xAC00 + cho * 588 + jung * 28 + jong)


def ko_variants(word: str) -> list[str]:
    """형용사 관형형에서 '~게/~고' 활용형을 만든다. 사용자는 '따뜻한' 만큼 '따뜻하고', '따뜻하게' 로도 쓴다.

    따뜻한 → 따뜻하게·따뜻하고, 신비로운 → 신비롭게, 어두운 → 어둡게, 밝은 → 밝게, 푸른 → 푸르게.
    '눈 오는' 같은 동사형(~는)은 만들지 않는다.
    """
    w = word.strip()
    if len(w.replace(" ", "")) < 2 or not re.fullmatch(r"[가-힣 ]+", w):
        return []
    head, last = w[:-1], w[-1]
    extra: tuple = ()
    if last == "한":
        stem, extra = head + "하", ("해", "해서", "함")
    elif last == "운" and head and "가" <= head[-1] <= "힣":
        cho, jung, jong = _split(head[-1])
        if jong:
            return []
        stem = head[:-1] + _join(cho, jung, _JONG_B)          # ㅂ 불규칙: 신비로운 → 신비롭게
    elif last == "은":
        stem = head                                            # 밝은 → 밝게
    else:
        cho, jung, jong = _split(last)
        if jong != _JONG_N or jung != _JUNG_EU or last == "는":
            return []
        stem = head + _join(cho, jung, 0)                      # 푸른 → 푸르게, 슬픈 → 슬프게
    if not stem.strip() or stem.endswith(" "):
        return []
    return [stem + e for e in ("게", "고", "며", "지만") + extra]


@lru_cache(maxsize=1)
def load_intents() -> list[dict]:
    """사용자 의도 사전(crew_intent.json). 장르·화풍·시대·날씨·시간·색·조명·분위기 말만 들어 있다.

    라이브러리 태그 전체로 요구를 뽑으면 대상 이름이 요구로 잡혔다(예: '향수 광고' 의 향수(perfume) → 향수(nostalgia)
    분위기, '우산' → 비 오는 날씨). 그래서 '반드시 지킬 말' 은 이 사전으로 정확하게 정하고, 라이브러리는 메뉴에만 쓴다.
    """
    with open(INTENT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for it in data["intents"]:
        forms = list(it["words"])
        for w in it["words"]:
            forms += [v for v in ko_variants(w) if v not in forms]
        out.append({**it, "res": [(w, re.compile(word_pattern(w), re.I)) for w in forms]})
    return out


def user_demands(text: str, facets=ENFORCED_FACETS) -> list[dict]:
    """사용자가 직접 적은 장르·화풍·날씨·색·조명·분위기. 이것이 최우선이다.

    돌려주는 값: [{"facet", "tag": 요청에 나온 말, "ids": 그 말이 태그로 붙은 라이브러리 항목(제작진 선택 검사용),
                  "terms": 최종 글에 하나는 보여야 할 영문 확인어}]
    한 말이 다른 요구의 더 긴 말과 같은 자리에 겹치면(예: '포스터' 와 '여행 포스터') 긴 것만 남긴다.
    자리로 보므로 '눈'(눈 오는)과 '눈부신' 처럼 떨어진 두 말은 둘 다 남는다.
    """
    t = _norm(text)
    lib = load_library()
    hits = []   # (의도, 처음 맞은 말, 그 의도가 맞은 모든 자리)
    for it in load_intents():
        if it["facet"] not in facets:
            continue
        spans, first = [], None
        for w, rx in it["res"]:
            found = [m.span() for m in rx.finditer(t)]
            if found and first is None:
                first = w
            spans += found
        if first:
            hits.append((it, first, spans))

    def inside(span, other_spans):
        a, b = span
        return any(a2 <= a and b <= b2 and (b2 - a2) > (b - a) for a2, b2 in other_spans)

    out = []
    for it, w, spans in hits:
        others = [sp for it2, _, sps in hits if it2 is not it for sp in sps]
        if all(inside(sp, others) for sp in spans):
            continue
        ids = set()
        for word in it["words"] + it["check"]:
            ids |= {i for i in lib["tags"].get(_norm(word).lower(), ()) if lib["entries"][i]["facet"] == it["facet"]}
        out.append({"facet": it["facet"], "tag": w.lower(), "ids": sorted(ids), "terms": list(it["check"])})
    return out


# 영문 확인어는 낱말 앞에서 시작하고 흔한 어미만 붙을 때 맞는 것으로 본다 ('grain' 속 'rain', 'rainbow' 는 비가 아니다)
_TERM_SUFFIX = r"(?:s|es|y|ier|iest|ing|ed|ly|ish|en|er|est)?"


@lru_cache(maxsize=1024)
def terms_re(terms: tuple) -> re.Pattern:
    alts = "|".join(re.escape(t.lower()) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alts}){_TERM_SUFFIX}(?![a-z0-9])", re.I)


def has_term(text: str, terms) -> bool:
    """영문 글에 확인어 중 하나가 낱말로 들어 있는지."""
    terms = tuple(t for t in terms if t)
    return bool(terms) and bool(terms_re(terms).search(str(text or "")))


def fits(e: dict, cat: str | None) -> bool:
    f = e.get("fits") or ["any"]
    return not cat or "any" in f or cat in f


# 요청에 사람이 없으면 사람이 나오는 항목은 메뉴·아이디어에서 뺀다 (G0: 신발 광고가 사람 다리 컷이 됐다)
PERSON_TEXT_RE = any_word(("사람", "인물", "모델", "소녀", "소년", "남자", "여자", "아이", "손님", "점원", "커플", "가족", "친구",
                           "주인공", "청년", "노인", "아기", "학생", "person", "people", "man", "woman", "girl", "boy", "child"))
PEOPLE_EN_RE = re.compile(
    r"\b(?:person|people|man|men|woman|women|girl|girls|boy|boys|child|children|kid|kids|baby|crowd|hikers?|joggers?|"
    r"skaters?|surfers?|swimmers?|tourists?|workers?|chefs?|baristas?|customers?|shoppers?|couples?|family|families|"
    r"someone|figures?|faces?|heads?|hair|portrait|dancers?|models?|athletes?|players?|riders?|drivers?|passengers?|pedestrians?|crowds?|"
    r"students?|audience|performers?|bride|groom|soldiers?|knights?|wizards?|monks?|vendors?|fishermen|farmers?)\b", re.I)


KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crew_knowledge.json")


@lru_cache(maxsize=1)
def load_knowledge() -> dict:
    """crew_knowledge.json (분야 정의, 대상 이름, 장소 말, 어두운 말, 도우미 아이디어 묶음). crew.py 도 이것을 쓴다."""
    with open(KNOWLEDGE_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def subject_words() -> tuple:
    """제품·공간 분야의 대상 이름 (향수, 신발, 시계, 카페 …). 다른 제품이 섞이거나 동음이의어로 분위기가 잡히는 것을 막는 데 쓴다."""
    kn = load_knowledge()
    words = set(kn.get("subject_nouns") or [])
    for cat in ("product", "space"):
        words |= {w for w in kn["categories"][cat]["keywords"] if w not in ("광고", "제품", "상품", "product")}
    return tuple(sorted(words, key=len, reverse=True))


@lru_cache(maxsize=1)
def _dark_re():
    words = load_knowledge().get("dark_words") or []
    return any_word(words) if words else None


def idea_prefer() -> dict:
    return load_knowledge().get("idea_prefer", {})


def _subjects_in(text: str) -> set:
    t = _norm(text)
    return {w for w in subject_words() if _tag_re(w).search(t)}


@lru_cache(maxsize=None)
def _entry_subjects(eid: str) -> frozenset:
    # 항목 글은 바뀌지 않으므로 한 번만 잰다 (요청마다 수천 항목 × 대상 이름 정규식을 돌리지 않게)
    e = load_library()["entries"][eid]
    return frozenset(_subjects_in(" ".join([e["ko"]] + list(e.get("tags_ko") or []))))


STYLE_FACETS = ("mood_emotion", "color_palette", "lighting", "time_weather")


class Query:
    """요청 하나에 대해 한 번만 계산하는 값 (단계마다 다시 계산하지 않는다)."""

    def __init__(self, text: str):
        self.text = _norm(text)
        self.people_ok = bool(PERSON_TEXT_RE.search(self.text))
        self.subjects = frozenset(_subjects_in(self.text))
        self.mentions = mentions(self.text)
        self.tokens = tokens(self.text)
        self.demands = user_demands(self.text)
        terms = tuple(t for d in self.demands for t in d["terms"])
        self.boost_re = terms_re(terms) if terms else None
        # 어두운 항목(호러·전쟁·범죄·재난 …)은 사용자가 그런 말을 썼을 때만 나온다 ('바다' 태그로 '심해 괴물 호러' 가 끼지 않게)
        rx = _dark_re()
        ents = load_library()["entries"]
        self.dark_ok = bool(rx and rx.search(self.text)) or any(
            ents[i].get("dark") for d in self.demands for i in d["ids"])


@lru_cache(maxsize=64)
def query(text: str) -> Query:
    return Query(text)


def allowed(eid: str, facet: str, cat: str | None, q: Query) -> bool:
    """메뉴·아이디어에 넣어도 되는 항목인지. 사용자 요구 항목(must)에도 같은 거름을 쓴다."""
    e = load_library()["entries"].get(eid)
    if not e or e["facet"] != facet or not fits(e, cat):
        return False
    if not q.people_ok and PEOPLE_EN_RE.search(e["en"]):
        return False                  # 요청에 없는 사람
    if e.get("dark") and not q.dark_ok:
        return False
    if q.subjects:
        own = _entry_subjects(eid)
        if own and not own & q.subjects:
            return False              # 다른 제품의 연출 (향수 광고에 핸드백 연출)
        if own and facet in STYLE_FACETS:
            return False              # 동음이의어 (향수 광고에 '향수 어린' 분위기)
    return True


def usable(ids, text: str, cat: str | None) -> list[str]:
    """사용자 요구 항목 중 이 요청의 메뉴에 넣을 수 있는 것."""
    q = query(text)
    ents = load_library()["entries"]
    return [i for i in ids if i in ents and allowed(i, ents[i]["facet"], cat, q)]


def retrieve(text: str, facet: str, k: int = 8, cat: str | None = None, rng: random.Random | None = None,
             must: list[str] | None = None, prefer: list[str] | None = None, wild: float = 0.25,
             focus: list[str] | None = None) -> list[dict]:
    """한 단계의 메뉴. ① 사용자 요구 항목 ② 요청 말과 맞는 항목 ③ 핵심 그룹(prefer)에서 고르게 ④ 나머지 그룹에서 일부(wild)
    순으로 채우고 순서를 섞는다. 요청에 없는 사람·다른 제품·어두운 항목은 뺀다(allowed)."""
    rng = rng or random.Random()
    lib = load_library()
    ents = lib["entries"]
    q = query(text)
    picked: list[str] = []

    def ok(i):
        return allowed(i, facet, cat, q)

    def add(i):
        if i not in picked and i in ents and ents[i]["facet"] == facet:
            picked.append(i)

    for i in must or []:
        if ok(i):
            add(i)
    score = {}
    subj = set(subject_words())
    for tag, ids in q.mentions.items():
        if facet in STYLE_FACETS and tag in subj:
            continue   # '향수(perfume)' 가 '향수(nostalgia)' 분위기로 잡히지 않게
        for i in ids:
            if ok(i):
                score[i] = score.get(i, 0) + 1 + len(tag) / 20
    for tok in q.tokens:
        if facet in STYLE_FACETS and tok in subj:
            continue
        for i in lib["tokens"].get(tok, ()):
            if ents[i]["facet"] == facet and ok(i):
                score[i] = score.get(i, 0) + 0.6
    # 사용자 요구(장르·날씨·색·조명·분위기)의 영문 확인어가 들어 있는 항목도 앞으로 (예: '따뜻한' → warm 조명·색)
    if q.boost_re:
        for i in lib["by_facet"][facet]:
            if q.boost_re.search(ents[i]["en"]) and ok(i):
                score[i] = score.get(i, 0) + 0.5
    if focus:   # 사용자가 장소를 말했으면 그 장소 묶음 안에서만 점수 항목을 고른다 ('집 안' 에 따뜻한 카지노가 끼지 않게)
        score = {i: v for i, v in score.items() if ents[i]["group"].startswith(tuple(focus))}
    # 요청 말과 맞는 항목으로 메뉴 대부분을 채우고, 같은 점수 안에서는 무작위로 골라 매번 다른 메뉴가 되게 한다.
    ranked = sorted(score, key=lambda x: (-round(score[x], 1), rng.random()))
    for i in ranked[: max(2, k - 2)]:
        add(i)

    def by_group(ids):
        groups = {}
        for i in ids:
            groups.setdefault(ents[i]["group"], []).append(i)
        return groups

    def fill(groups, limit):
        order = list(groups)
        rng.shuffle(order)
        while len(picked) < limit and order:
            for g in list(order):
                if len(picked) >= limit:
                    break
                if groups[g]:
                    add(groups[g].pop(rng.randrange(len(groups[g]))))
                if not groups[g]:
                    order.remove(g)

    pool = [i for i in lib["by_facet"][facet] if i not in picked and ok(i)]
    if prefer:
        core = [i for i in pool if any(ents[i]["group"].startswith(p) for p in prefer)]
        core_set = set(core)
        rest = [i for i in pool if i not in core_set]
        n_wild = max(1, round(k * wild)) if wild > 0 and rest else 0   # wild=0 이면 다양성 칸 없음 (사용자가 장소를 말한 경우)
        fill(by_group(core), k - n_wild)
        fill(by_group(rest), k)
        fill(by_group([i for i in core if i not in picked]), k)   # 다양성 그룹이 모자라면 핵심에서 마저
    else:
        fill(by_group(pool), k)
    if facet == "genre_style" and NONE_GENRE in ents and NONE_GENRE not in picked:
        picked.append(NONE_GENRE)
    out = [ents[i] for i in picked]
    rng.shuffle(out)
    return out


def counts() -> dict:
    lib = load_library()
    return {f: len(v) for f, v in lib["by_facet"].items()}
