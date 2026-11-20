"""인물 라이브러리 (11-19): 이야기에 이미 정해진 인물의 외형·목소리·기준 그림·그림체.

11-19 메인 첫 시험에서 스튜디오가 만든 대사 영상은 실사였고 인물도 손으로 만든 니키·푸토르와 달랐다. 요청 글에 외형을 길게 써도
인물 그림은 글에서 새로 그려져 같은 얼굴이 되지 않는다. 그래서 정해진 인물은 여기에 두고:
- 요청 글에 이름이 나오면 구성안을 쓰는 모델에게 그 인물을 알려 주고(외형·그림체),
- 구성안이 나온 뒤 설정집의 외형·목소리를 이 값으로 덮어쓰고(모델이 다르게 바꿔 쓰지 못하게),
- 기준 그림(ref)이 있으면 말하는 얼굴·듣는 얼굴은 그 그림을 편집해 만든다 (감독 `_dlg_casts`).

순수 함수만 둔다(파일 읽기 외에 DB·네트워크 없음). 라이브러리 파일은 `cast_library.json` (STUDIO_CAST_FILE 로 바꿀 수 있다).
"""
import difflib
import json
import logging
import os
import re

from .config import settings

log = logging.getLogger("mgs.cast")

LIB_FILE = os.path.join(os.path.dirname(__file__), "cast_library.json")
REQUIRED = ("id", "names", "look")
# 사용자가 일부러 실사를 말했으면 라이브러리의 그림체로 바꾸지 않는다
REAL_STYLE_RE = re.compile(r"실사|포토\s*리얼|photo\s*-?real|live[- ]action", re.I)
# 화풍 열쇠 (11-20 장면 잇기 설계 4절): 한 영화의 인물은 모두 한 화풍이어야 해서, 화풍 글을 세 열쇠 중 하나로 줄여 인물마다 그 화풍의 기준 그림을 고른다
STYLE_KEYS = ("photo", "semi", "anime")
_SEMI_RE = re.compile(r"semi\s*-?\s*(?:real|photo)|반\s*실사|painterly|digital\s+painting|\bpainted\b|수채|유화", re.I)
_PHOTO_RE = re.compile(REAL_STYLE_RE.pattern + r"|photograph\w*", re.I)
_ANIME_RE = re.compile(r"anime|animation|animated|cartoon|cel[\s-]*shad|\b2\s*-?\s*d\b|애니|만화|웹툰", re.I)
_cache: dict = {}


def style_key(style: str) -> str:
    """화풍 글 → 열쇠 'photo'(실사)·'semi'(반실사)·'anime'(애니). 위에서부터 먼저 맞는 것:
    1) semi-realistic·painting·반실사 등 (라이브러리 화풍 글은 'semi-realistic'을 품어 실사 낱말에 걸리지 않게 먼저 본다)
    2) 실사·photorealistic·live action·photograph  3) anime·2D·cel·animation·cartoon·애니·만화
    4) 어디에도 안 맞거나 비었으면 semi (우리 기본 그림체)"""
    t = str(style or "")
    if _SEMI_RE.search(t):
        return "semi"
    if _PHOTO_RE.search(t):
        return "photo"
    if _ANIME_RE.search(t):
        return "anime"
    return "semi"


def ref_for(entry: dict, key: str) -> str | None:
    """인물의 그 화풍 기준 그림 경로. 없으면 None (영화 시작 때 그 화풍으로 바꿔 만든다).
    key 가 'legacy' 이면 예전 기준 그림 ref (2.5D 만화풍): 다른 열쇠에는 ref 를 대신 내주지 않는다 (화풍이 섞이므로)"""
    if not isinstance(entry, dict):
        return None
    if key == "legacy":
        return entry.get("ref") or None
    refs = entry.get("refs")
    v = refs.get(key) if isinstance(refs, dict) else None
    return v if isinstance(v, str) and v.strip() else None


def _clean_refs(e: dict) -> dict:
    """refs 는 {화풍 열쇠: 결과 폴더 안 상대 경로}만 남긴다 (틀린 칸 하나로 인물을 버리지 않는다)"""
    refs = e.get("refs")
    if refs is None:
        return e
    good = {k: v for k, v in refs.items() if isinstance(refs, dict) and k in STYLE_KEYS and isinstance(v, str)
            and v.strip() and not v.startswith("/") and ".." not in v} if isinstance(refs, dict) else {}
    if good != refs:
        log.warning("인물 라이브러리 %s 의 refs 에 형식이 틀린 칸이 있어 뺍니다", e.get("id"))
    return {**e, "refs": good}


def _valid(e) -> bool:
    return (isinstance(e, dict) and all(e.get(k) for k in REQUIRED) and isinstance(e["names"], list)
            and all(isinstance(n, str) and n.strip() for n in e["names"]) and isinstance(e["look"], str)
            and re.fullmatch(r"[a-z0-9_]{2,32}", str(e["id"])) is not None)


def library() -> list[dict]:
    """라이브러리 인물 목록. 파일이 없거나 깨졌으면 빈 목록(요청은 평소처럼 만든다). 파일 수정 시각이 바뀌면 다시 읽는다"""
    path = getattr(settings, "studio_cast_file", "") or LIB_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("인물 라이브러리를 읽지 못했습니다 (%s): %s", path, e)
        return []
    out = [e for e in (data.get("characters") or []) if _valid(e)]
    if len(out) != len(data.get("characters") or []):
        log.warning("인물 라이브러리에 형식이 틀린 항목이 있어 건너뜁니다 (%s)", path)
    out = _resolve_variants([_clean_refs(e) for e in out], path)
    _cache[path] = (mtime, out)
    return out


# 같은 사람의 다른 모습 (11-20: 제임스는 무언가에 집중할 때만 안경을 쓴다). 변형 항목은 variant_of 에 기본 인물 id 를, variant_when 에
# 그 모습을 쓸 때(영어 글)를 적고, 목소리·말투·그림체는 기본 인물 것을 이어받는다
# (기준 그림 ref·refs 와 signature 는 이어받지 않는다: 안경 쓴 얼굴은 자기 그림이 따로 있다). 이름으로는 찾지 않고 기본 인물이 나오면 따라 나온다
INHERIT = ("style", "speech", "voice", "voice_ko", "voice_fx", "voice_ref", "voice_ref_text")


def _resolve_variants(entries: list[dict], path: str = "") -> list[dict]:
    by_id = {e["id"]: e for e in entries if not e.get("variant_of")}
    out = []
    for e in entries:
        base = by_id.get(e.get("variant_of")) if e.get("variant_of") else None
        if e.get("variant_of") and base is None:
            log.warning("인물 라이브러리 변형 %s 의 기본 인물 %s 가 없어 건너뜁니다 (%s)", e["id"], e.get("variant_of"), path)
            continue
        if base is not None:
            e = {**{k: base[k] for k in INHERIT if base.get(k)}, **e}
        out.append(e)
    return out


def _name_re(name: str):
    """한글 이름은 조사가 붙으므로 부분 일치, 영문 이름은 낱말 경계로 (Niki 가 Nikita 에 걸리지 않게)"""
    n = re.escape(name.strip())
    return re.compile(n if re.search(r"[가-힣]", name) else rf"(?<![A-Za-z]){n}(?![A-Za-z])", re.I)


def known_in(text: str) -> list[dict]:
    """글에 이름이 나오는 라이브러리 인물 (글에 처음 나오는 차례)"""
    hits = []
    lib = library()
    for e in lib:
        if e.get("variant_of"):
            continue
        pos = [m.start() for n in e["names"] for m in [_name_re(n).search(text or "")] if m]
        if pos:
            hits.append((min(pos), e))
    out = []
    for _, e in sorted(hits, key=lambda h: h[0]):
        out.append(e)
        out += [v for v in lib if v.get("variant_of") == e["id"]]       # 같은 사람의 다른 모습은 기본 인물을 따라 알려 준다
    return out


def _squash(text) -> str:
    return re.sub(r"[\s()（）\[\]·.,'’\-]+", "", str(text or "").lower())


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def match_character(char: dict, entries: list[dict]) -> dict | None:
    """설정집 인물 하나와 같은 라이브러리 인물: 한글 이름(name_ko)이나 id 가 라이브러리 이름과 같거나 이름을 포함한다.
    이름도 id 도 안 맞으면(대사 없는 영상의 구성안은 name_ko 칸이 없고 모델이 id 를 'A' 로 쓸 수 있다) 외형 글이 라이브러리 외형을
    그대로 옮긴 것(거의 같은 글)인지 본다"""
    name = str(char.get("name_ko") or "").strip()
    cid = str(char.get("id") or "").strip()
    for e in entries:                                   # 1) 라이브러리 id 가 그대로, 또는 이름이 (띄어쓰기·괄호 빼고) 똑같다
        if cid and cid.lower() == e["id"].lower():
            return e
    for e in entries:
        if any((name and _squash(name) == _squash(n)) or (cid and _squash(cid) == _squash(n)) for n in e["names"]):
            return e
    best, best_len = None, 0                            # 2) 이름을 포함한다: 가장 긴 이름이 맞는 쪽 (변형 이름은 기본 인물 이름을 품는다)
    for e in entries:
        for n in e["names"]:
            if ((name and _name_re(n).search(name)) or (cid and cid.lower() == n.lower())) and len(n) > best_len:
                best, best_len = e, len(n)
    if best is not None:
        return best
    look = _norm(char.get("look"))
    if look:
        for e in entries:
            if difflib.SequenceMatcher(None, look, _norm(e["look"])).ratio() >= 0.9:
                return e
    return None


def prompt_block(entries: list[dict] | None, speech: bool = True, by_id: bool = False) -> str:
    """구성안 지시문 끝에 붙이는 '이미 정해진 인물' 글 (영어). 목소리는 넣지 않는다: 구성안 검사가 모델이 쓴 목소리 글을 보므로
    긴 목소리 글을 베끼게 하지 않고, 목소리는 구성안이 나온 뒤 라이브러리 값으로 바꾼다.
    speech 가 거짓이면 말투를 빼고(대사 없는 영상), by_id 가 참이면 이름 칸(name_ko) 대신 라이브러리 id 를 인물 id 로 쓰게 한다
    (대사 없는 영상의 구성안에는 name_ko 칸이 없다)"""
    if not entries:
        return ""
    # 말투(speech): 11-19 메인 시험에서 모델이 푸토르를 존댓말 "선생님" 말투로 써서, 손으로 만든 때의 할아버지 말투("얘야, ~란다")와 달랐다
    given = 'with the given "id" (shown first)' if by_id else 'with the given "name_ko"'
    lines = ["\nKnown characters: these people already exist and must appear exactly as described. Put each in bible.characters "
             f'{given} and copy its look word for word; never change their age, build, hair, clothing or colours. '
             "The scenes must use only what the look lists." + (" Write each one's spoken lines in the given speech manner." if speech else "")]
    for e in entries:
        tag = f'- id "{e["id"]}" ({e["names"][0]})' if by_id else f'- name_ko "{e["names"][0]}"'
        extra = ""
        if e.get("variant_of"):
            base = next((b for b in entries if b["id"] == e["variant_of"]), None)
            base_tag = "the same person" if base is None else (f'id "{base["id"]}"' if by_id else f'name_ko "{base["names"][0]}"')
            extra = (f' This is the same person as {base_tag} in a different look: use this entry only in a scene where '
                     f'{e.get("variant_when") or "the situation fits"}, never put both entries in one scene, and use the other entry in every other scene.')
        lines.append(f'{tag}: {e["look"]}' + (f' Speech: {e["speech"]}.' if speech and e.get("speech") else "") + extra)
    style = next((e.get("style") for e in entries if e.get("style")), "")
    if style:
        lines.append(f'Visual style of the whole film (copy it into bible.style): {style}')
    return "\n".join(lines)


def apply_to_story(story: dict, entries: list[dict], text: str = "", keep_style: bool = False) -> dict:
    """구성안의 설정집을 라이브러리로 덮어쓴다. {설정집 인물 id: 라이브러리 id} 를 돌려준다.
    - 같은 인물: 외형(look)·목소리(voice·voice_ko)·후처리(voice_fx)·기준 그림(ref·refs)·알아볼 소품(signature)과 표시 이름을 라이브러리 값으로.
    - 그림체: 모델이 그림체를 정하지 않았거나 실사로 정했고(keep_style 이 거짓) 사용자가 실사를 말하지 않았으면 라이브러리의 그림체로.
    keep_style 은 구성안의 그림체가 이미 사실이 아닌 화풍(애니메이션 등)이라는 뜻이다: 사용자가 말한 화풍을 지킨다"""
    matched: dict = {}
    if not entries or not isinstance(story, dict):
        return matched
    bible = story.get("bible") or {}
    for c in bible.get("characters") or []:
        e = match_character(c, entries)
        if e is None:
            continue
        c["look"] = e["look"]
        c["name_ko"] = e["names"][0] if re.search(r"[가-힣]", e["names"][0]) else (c.get("name_ko") or e["names"][0])
        c["library"] = e["id"]
        # voice_ref(+voice_ref_text): 승인된 기준 목소리 소리 파일과 그 글. 있으면 영상마다 목소리를 새로 설계하지 않고 이 소리를 복제한다
        for k in ("voice", "voice_ko", "voice_fx", "ref", "voice_ref", "voice_ref_text"):
            if e.get(k):
                c[k] = e[k]
        # refs(화풍별 기준 그림)·signature(알아볼 소품): 새 감독 코드가 읽는다. ref 는 예전 코드를 위해 그대로 둔다
        for k in ("refs", "signature"):
            if e.get(k):
                c[k] = e[k]
        matched[str(c.get("id"))] = e["id"]
    if matched and not keep_style and not REAL_STYLE_RE.search(text or ""):
        style = next((e.get("style") for e in entries if e["id"] in matched.values() and e.get("style")), "")
        if style:
            bible["style"] = style
    return matched
