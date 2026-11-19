"""인물 라이브러리 (11-19): 이야기에 이미 정해진 인물의 외형·목소리·기준 그림·그림체.

11-19 메인 첫 시험에서 스튜디오가 만든 대사 영상은 실사였고 인물도 손으로 만든 니키·푸토르와 달랐다. 요청 글에 외형을 길게 써도
인물 그림은 글에서 새로 그려져 같은 얼굴이 되지 않는다. 그래서 정해진 인물은 여기에 두고:
- 요청 글에 이름이 나오면 구성안을 쓰는 모델에게 그 인물을 알려 주고(외형·그림체),
- 구성안이 나온 뒤 설정집의 외형·목소리를 이 값으로 덮어쓰고(모델이 다르게 바꿔 쓰지 못하게),
- 기준 그림(ref)이 있으면 말하는 얼굴·듣는 얼굴은 그 그림을 편집해 만든다 (감독 `_dlg_casts`).

순수 함수만 둔다(파일 읽기 외에 DB·네트워크 없음). 라이브러리 파일은 `cast_library.json` (STUDIO_CAST_FILE 로 바꿀 수 있다).
"""
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
_cache: dict = {}


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
    _cache[path] = (mtime, out)
    return out


def _name_re(name: str):
    """한글 이름은 조사가 붙으므로 부분 일치, 영문 이름은 낱말 경계로 (Niki 가 Nikita 에 걸리지 않게)"""
    n = re.escape(name.strip())
    return re.compile(n if re.search(r"[가-힣]", name) else rf"(?<![A-Za-z]){n}(?![A-Za-z])", re.I)


def known_in(text: str) -> list[dict]:
    """글에 이름이 나오는 라이브러리 인물 (글에 처음 나오는 차례)"""
    hits = []
    for e in library():
        pos = [m.start() for n in e["names"] for m in [_name_re(n).search(text or "")] if m]
        if pos:
            hits.append((min(pos), e))
    return [e for _, e in sorted(hits, key=lambda h: h[0])]


def match_character(char: dict, entries: list[dict]) -> dict | None:
    """설정집 인물 하나와 같은 라이브러리 인물: 한글 이름(name_ko)이나 id 가 라이브러리 이름과 같거나 이름을 포함한다"""
    name = str(char.get("name_ko") or "").strip()
    cid = str(char.get("id") or "").strip()
    for e in entries:
        for n in e["names"]:
            if (name and _name_re(n).search(name)) or (cid and cid.lower() == n.lower()):
                return e
    return None


def prompt_block(entries: list[dict] | None) -> str:
    """구성안 지시문 끝에 붙이는 '이미 정해진 인물' 글 (영어). 목소리는 넣지 않는다: 구성안 검사가 모델이 쓴 목소리 글을 보므로
    긴 목소리 글을 베끼게 하지 않고, 목소리는 구성안이 나온 뒤 라이브러리 값으로 바꾼다"""
    if not entries:
        return ""
    lines = ["\nKnown characters: these people already exist and must appear exactly as described. Put each in bible.characters "
             'with the given "name_ko" and copy its look word for word; never change their age, build, hair, clothing or colours. '
             "The scenes must use only what the look lists."]
    for e in entries:
        lines.append(f'- name_ko "{e["names"][0]}": {e["look"]}')
    style = next((e.get("style") for e in entries if e.get("style")), "")
    if style:
        lines.append(f'Visual style of the whole film (copy it into bible.style): {style}')
    return "\n".join(lines)


def apply_to_story(story: dict, entries: list[dict], text: str = "", keep_style: bool = False) -> dict:
    """구성안의 설정집을 라이브러리로 덮어쓴다. {설정집 인물 id: 라이브러리 id} 를 돌려준다.
    - 같은 인물: 외형(look)·목소리(voice·voice_ko)·후처리(voice_fx)·기준 그림(ref)과 표시 이름을 라이브러리 값으로.
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
        for k in ("voice", "voice_ko", "voice_fx", "ref"):
            if e.get(k):
                c[k] = e[k]
        matched[str(c.get("id"))] = e["id"]
    if matched and not keep_style and not REAL_STYLE_RE.search(text or ""):
        style = next((e.get("style") for e in entries if e["id"] in matched.values() and e.get("style")), "")
        if style:
            bible["style"] = style
    return matched
