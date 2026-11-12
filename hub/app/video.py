"""영상 렌더러: 요청과 첫 화면을 보고 영상 모델용 프롬프트를 쓴다 (docs/video-renderer.md).

- 이미지 프롬프트는 정지 장면의 외형을 길게 설명하는 글이라 I2V 에 그대로 넣으면 움직임 지시가 약하다.
  움직임만 프로필은 외형을 다시 쓰지 않고 카메라·대상 움직임·환경 움직임·속도·마무리만 쓴다.
- 사용자 말이 최우선이다. '천천히', '드론', '루프' 같은 영상 말과 '눈 오는' 같은 날씨 말은 선택과 글 모두에서 지킨다.
- 제품·공간·풍경 요청은 제작진처럼 단계 메뉴(crew_library 의 video_motion 칸)에서 고르고, 그 밖의 요청은 규칙만으로 쓴다.
- 첫 화면이 있으면(I2V) 시각 모델이 먼저 읽어 실제 그림에 있는 것만 움직이게 한다.
- T2V 는 첫 화면이 없으므로 이미지 확장(도우미·제작진)으로 장면을 먼저 쓰고 그 뒤에 움직임을 붙인다.
- 장문 프로필(러다이트식, `"long": true`)은 한 줄 요청을 구도 → 카메라와 빛 → 시간 순서의 움직임 → 마무리 네 단락의
  영어 상세 프롬프트로 쓴다. 외형도 함께 쓰므로 '외형 반복 금지' 검사는 하지 않는다 (docs/video-renderer.md 13절).

순수 함수는 hub/tests/test_video.py 가 GPU·DB 없이 검증한다.
"""
import asyncio
import base64
import io
import json
import math
import os
import random
import re
from functools import lru_cache

from . import crew_lib, promptx
from .config import settings
from .crew import PERSON_NOTE_RE, PERSON_REQ_RE, TEXT_NOTE_RE, skip_reason
from .promptx import (HAND_WORDS_RE, NEGATION_RE, PromptxError, clean_input, hides_hands, image_in,
                      parse_json_reply, repair_message, user_allows)

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_knowledge.json")
FACET = "video_motion"
MENU_SIZE = 8
STILL_GROUP = "still_background"
KO_KEYS = ("camera", "motion", "timing")
FRAME_MAX_SIDE = 1024
FRAME_MAX_BYTES = 40 * 1024 * 1024   # 이보다 큰 파일은 읽지 않는다 (허브 메모리 보호)
RESTATE_WORDS = 6        # 첫 화면 프롬프트와 이만큼 연달아 같은 낱말이 나오면 외형을 다시 쓴 것으로 본다
HANGUL_RE = re.compile(r"[가-힣]")

# 한 클립 안에서 장면을 바꾸라는 말 (컷 구성은 v2 에서 클립을 나눠 처리한다).
# 'transitions to' 는 화면·장면이 주어이거나(사이에 부사가 껴도) 뒤 세 낱말 안에 구도 말(close-up, view, shot)이 올 때 센다.
# 장문의 빛 변화('the cool light slowly transitions to warm gold')는 컷이 아니다. 'it' 은 빛 문장에도 흔해 주어로 보지 않는다
CUT_RE = re.compile(r"\b(?:cuts? to|jump cuts?|hard cut|smash cut|scene changes?|next scene|new scene"
                    r"|(?:(?:scene|shot|clip|view|image|picture|camera|frame|video|footage)(?: (?:\w+ly|then))*|then)"
                    r" transitions? (?:to|into)"
                    r"|transitions? (?:to|into) (?:[\w-]+ ){0,3}?(?:close[- ]?ups?|view|shot|interior|exterior))\b", re.I)
# 카메라 이동 종류. 둘 이상이면 짧은 클립에서 하나도 제대로 안 된다고 본다 [추정, A/B 로 조정].
# 크레인과 틸트는 함께 쓰는 한 동작이 많아 한 종류로 센다.
# camera·drone 뒤 30자 안의 동사를 센다. 쉼표·and·as·while·when·until 에서 멈추되(", thin mist lifts", "as steam rises"),
# 바로 뒤가 카메라 자신의 다음 이동이면 계속 본다(", then tilts up", ", as it rises", ", the view rising"). 다른 주어는 관사가
# 없을 때가 많아(steam, thin mist) 주어를 가려내지 않는다. 'camera left/right'(빛 방향)는 빼며, shot·lens 는 바로 뒤
# (then·-ly 부사는 건너뜀)의 동사만 본다 ('a wide shot of rising mist' 는 이동이 아니다)
_VERB = (r"(?:push(?:es|ing)?|doll(?:y|ies|ying)|zoom(?:s|ing)?|mov(?:es?|ing)|glid(?:es?|ing)|drift(?:s|ing)?|fl(?:ies|ying)"
         r"|pull(?:s|ing)?|backs?|pan(?:s|ning)?|orbit(?:s|ing)?|arc(?:s|ing)?|circl(?:es|ing)|track(?:s|ing)?|truck(?:s|ing)?"
         r"|follow(?:s|ing)?|slid(?:es|ing)|cran(?:es|ing)|ris(?:es|ing)|lift(?:s|ing)?|descend(?:s|ing)?|lower(?:s|ing)?"
         r"|climb(?:s|ing)?|tilt(?:s|ing)?)\b")
_OWN = (r"(?:\s*(?:,|\b(?:and|then|also|now|it|as|while|when|until|(?:the|its) (?:view|frame|framing|height|path|angle))\b"
        r"|\b\w+ly\b))*\s*" + _VERB)
_GAP = r"(?:(?!(?:,|\b(?:and|as|while|when|until)\b)(?!" + _OWN + r"))[^.;]){0,30}?"
_CAM = r"(?:\b(?:camera(?![- ](?:left|right)\b)|drone)\b" + _GAP + r"|\b(?:shot|lens)\s+(?:(?:then|also|now|\w+ly)\s+)*)\b"
CAMERA_MOVES = {
    "push": re.compile(_CAM + r"(?:push(?:es|ing)?|doll(?:y|ies|ying)|zoom(?:s|ing)?|moves?|glides?|drifts?|flies) (?:\w+ ){0,2}?"
                       r"(?:in|forward|closer)\b(?! (?:a|an|the) )|\b(?:push|dolly|zoom)[- ]in\b", re.I),
    "pull": re.compile(_CAM + r"(?:pull(?:s|ing)?|doll(?:y|ies|ying)|zoom(?:s|ing)?|backs?) (?:\w+ ){0,2}?(?:back|out|away)\b"
                       r"|\b(?:pull[- ]back|zoom[- ]out|dolly[- ]out)\b", re.I),
    "pan": re.compile(_CAM + r"pan(?:s|ning)?\b|\b(?:slow|gentle|smooth|left|right) pan\b", re.I),
    "orbit": re.compile(_CAM + r"(?:orbit(?:s|ing)?|arc(?:s|ing)?|circl(?:es|ing))\b|\borbit(?:s|ing)? (?:around|the)\b", re.I),
    "track": re.compile(_CAM + r"(?:track(?:s|ing)?|truck(?:s|ing)?|follow(?:s|ing)?|slid(?:es|ing))\b|\btracking shot\b", re.I),
    "crane": re.compile(_CAM + r"(?:cran(?:es|ing)|ris(?:es|ing)|lifts?|descends?|lowers?|climbs?|tilt(?:s|ing)?)\b"
                        r"|\bcrane (?:shot|move)\b", re.I),
}

# 장문 프로필에서 막는 품질 유행어. Wan 은 자연 문장 설명으로 배웠고, 이런 말은 화면에 보이는 것을 하나도 정하지 않는다 [추정].
# 사용자가 직접 쓴 말(예: 4K)은 막지 않는다 (promptx.user_allows)
BUZZ_RE = re.compile(r"\b(?:8k|4k|uhd|ultra[- ]hd|masterpieces?|best[- ]quality|high[- ]quality|(?:ultra|hyper|highly)[- ]detailed|"
                     r"award[- ]winning|trending on artstation)\b", re.I)
# 1인칭 요청인데 보는 사람 자신이 화면에 나오는 구도. 러다이트 파일럿 영상은 프롬프트가 1인칭이었는데 어깨너머 샷이 나왔다.
# his/her/their·him/her/them·man/woman 은 화면 속 다른 사람(her hair)이나 배경(peaks behind them)에도 쓰여서 보지 않는다
_ROLE = r"(?:pilot|driver|rider|player|viewer|operator|captain|runner|skier|cyclist)"
SELF_BODY_RE = re.compile(
    r"\bover[- ]the[- ]shoulders?\b|\bover-shoulder\b"
    r"|\b(?:behind|rear view of) the (?:pilot|driver|viewer|operator|captain)\b"
    rf"|\bthe {_ROLE}['’]s (?:head|hair|face|neck|shoulders?|back|headset|helmet)\b"
    # 'a view of the pilot's own hands'(자기 손), 'a clear view of the rider ahead'(앞사람)는 위반이 아니다
    rf"|\bview of (?:a|an|the) (?:seated )?{_ROLE}\b(?!['’]s| (?:ahead|in front)\b)"
    # 러다이트 실패 글의 'a seated pilot in a headset' (앉아 있는 사람을 화면 속 인물로 그린다)
    rf"|\b(?:a|the) seated {_ROLE}\b(?!['’]s)"
    r"|\bfrom (?:just |directly |right )?behind them\b|\bcamera(?: \w+){0,2}? behind them\b", re.I)
POV_TERM = "first-person"   # crew_intent.json 의 1인칭 의도 확인어 (이 말이 있으면 1인칭 요구로 본다)
# 사전 낱말 맞추기는 '1인칭뷰', '1인칭으로만', '조종사 시점' 을 놓쳐서 1인칭 말은 따로 넓게 본다 (부정 '1인칭 말고' 는 다른 말과 같이 보지 않는다).
# '영상 내 시점 변화 없이' 의 '내' 는 '안' 이라서 영상·화면·장면 같은 말 뒤의 '내 시점' 은 1인칭으로 보지 않는다
POV_RE = re.compile(r"(?<![0-9가-힣])(?:1|일) ?인칭"
                    r"|(?<![가-힣])(?:운전자|운전석|조종사|조종석|파일럿|라이더|주인공|승객|나의)(?:의)? ?시점"
                    r"|(?<![가-힣])(?<!영상 )(?<!화면 )(?<!장면 )(?<!프레임 )(?<!클립 )(?<!컷 )(?<!샷 )(?<!씬 )내 ?시점"
                    r"|(?<![a-z])(?:point[- ]of[- ]view|pov|first[- ]person)(?![a-z])", re.I)
# 장문은 참고 글을 다시 쓸 수 있어 '네온 간판' 이 그대로 옮겨지고 영상 모델은 가짜 글자를 그린다. 모델이 보는 참고 글에서만 바꾼다.
# 물건은 그대로 둔다: 'street signs' 를 'street lights'(가로등)로 바꾸면 첫 화면 메모가 그림에 없는 빛을 말하게 된다
SIGN_RE = re.compile(r"\b(neon|shop|store|storefront|street|lit|glowing|hanging|illuminated) sign(s|age)?\b|\bsignage\b", re.I)
_SIGN_SWAP = {"neon": "neon light", "shop": "shopfront board", "store": "shopfront board", "storefront": "shopfront board",
              "street": "plain street panel", "hanging": "hanging board"}

FRAME_RULES = """You look at one still image that will be the first frame of a short video. Report only what you actually see.
Return only JSON:
{"subject": "the main subject in a few words", "where": "where the main subject sits in the frame", "setting": "the place and background in a few words", "people": 0, "movable": ["things in the image that could move naturally, such as liquid, steam, smoke, fabric, hair, leaves, water, clouds, light, particles"], "shot": "shot size and camera angle", "light": "the main light source, its direction and quality, and the time of day if it is visible", "colours": "the main colours and the overall colour tone", "text_visible": false}
"people" is the number of people visible; count a visible human hand or arm as one person."""

SYSTEM = """You are a small video crew, a director of photography and a motion designer, writing the motion for one short clip.
Model: {model}. Clip: about {seconds} seconds, one continuous shot.
{mode_rule}
Rules:
{rules}
{field}Return JSON only:
{choices_line}- "ko": three short Korean sentences, one each for camera, motion and timing, explaining the plan to the user.
- "prompt": one English paragraph of {lo} to {hi} words. {style}"""

# 장문(러다이트 감독 agent 식): 한 줄 요청 → 네 단락의 길고 자세한 영어 프롬프트. 단락을 칸으로 나눠 받아 순서와 빠짐을 코드가 지킨다
SYSTEM_LONG = """You are the director of a small film crew. You turn a short request into one long, detailed, cinematic description of a single shot for a video model, written as flowing prose the way a director briefs a cinematographer.
Model: {model}. Clip: about {seconds} seconds, one continuous shot, silent.
{mode_rule}
Rules:
{rules}
{field}Return JSON only:
{choices_line}- "ko": three short Korean sentences, one each for camera, motion and timing, explaining the plan to the user.
{parts}
The four parts are joined in this order into one English prompt of {lo} to {hi} words in total (aim for about {target}, roughly {part_target} words per part); each part has at least {part_min} words. {style}{example}"""


@lru_cache(maxsize=1)
def load_knowledge() -> dict:
    with open(KNOWLEDGE_PATH, encoding="utf-8") as f:
        return json.load(f)


def steps_of(cat: str) -> list[dict]:
    return load_knowledge()["categories"][cat]["steps"]


def long_spec() -> dict:
    """장문 프로필의 규칙·단락 정의 (video_knowledge.json 의 long)."""
    return load_knowledge()["long"]


def is_long(profile: dict) -> bool:
    return bool(profile.get("long"))


def owns_look(profile: dict) -> bool:
    """T2V 장문은 장면 글을 앞에 붙이지 않으므로 이 글이 외형의 전부다 (사용자의 장르·색·조명·분위기 말도 이 글에 있어야 한다)."""
    return is_long(profile) and profile["mode"] == "t2v"


def part_spec(part: dict, mode: str) -> str:
    """단락 설명. I2V 는 첫 화면이 외형을 정하므로 첫 화면 메모·참고 글에 있는 말로만 쓰게 하는 설명을 따로 둔다."""
    return part.get(f"spec_{mode}") or part["spec"]


def model_name(profile: dict) -> str:
    # 지시문에는 모델 이름만 넣는다 ('러다이트식' 은 영어로 Luddite 로 읽힐 수 있다)
    return profile["title"].split(" · ")[0]


def unsign(text: str) -> str:
    def swap(m: re.Match) -> str:
        lead = m.group(1)
        if not lead:
            return "panels"                                                  # signage
        new = _SIGN_SWAP.get(lead.lower(), f"{lead.lower()} panel") + ("s" if m.group(2) else "")   # lit·glowing → panel
        return new[0].upper() + new[1:] if lead[0].isupper() else new
    return SIGN_RE.sub(swap, text or "")


def frame_known(frame: dict | None) -> bool:
    """첫 화면을 실제로 읽었는지 (스키마와 다른 시각 응답의 빈 값 dict 도 '모름' 으로 본다)."""
    return bool(frame and (frame.get("subject") or frame.get("setting")))


# ── 모델 프로필 ─────────────────────────────────────────────

def profile_of(pid: str | None) -> tuple[str, dict]:
    ps = load_knowledge()["profiles"]
    pid = str(pid or "") or next(iter(ps))
    if pid not in ps:
        raise PromptxError(f"모르는 영상 프로필입니다: {pid}")
    return pid, ps[pid]


def profile_list() -> list[dict]:
    """패널의 선택 목록 (모델 이름은 파일에만 있다)."""
    return [{"id": k, "title": p["title"], "mode": p["mode"], "long": is_long(p), "default_seconds": p["default_seconds"],
             "max_seconds": round((p["max_frames"] - 1) / p["fps"], 2)}
            for k, p in load_knowledge()["profiles"].items()]


def suggest_settings(profile: dict, seconds=None) -> dict:
    """길이(초)에 맞는 프레임 수와 fps 제안. Wan 은 4n+1 프레임이고 한 번에 만드는 길이에 한계가 있다."""
    fps, step, maxf = int(profile["fps"]), int(profile.get("frame_step", 4)), int(profile["max_frames"])
    try:
        sec = float(seconds)
    except (TypeError, ValueError, OverflowError):
        sec = float(profile["default_seconds"])
    if not math.isfinite(sec) or sec <= 0:
        sec = float(profile["default_seconds"])   # inf·nan·0·음수는 기본 길이로 (int(inf) 는 예외를 낸다)
    sec = min(max(0.5, sec), 600.0)
    want = int(round(sec * fps)) + 1
    frames = min(maxf, max(step + 1, ((want - 1) // step) * step + 1))
    return {"frames": frames, "fps": fps, "seconds": round((frames - 1) / fps, 2), "capped": want > maxf}


# ── 사용자 요구 ─────────────────────────────────────────────

def checked_demands(said: str, look: bool = False) -> list[dict]:
    """영상 글에서 반드시 보여야 할 사용자 말: 카메라·속도·루프(영상 말)와 날씨·시간(눈·비·안개는 움직이는 것).

    look 이 True 면(T2V 장문: 이 글이 외형의 전부) 장르·색·조명·분위기 말도 넣는다.
    """
    q = crew_lib.query(said)
    return list(q.video_demands) + [d for d in q.demands if look or d["facet"] == "time_weather"]


def demand_lines(said: str, look: bool = False) -> list[str]:
    return [f"- The user wrote '{d['tag']}'. The clip must clearly show it, using words such as: {', '.join(d['terms'][:6])}."
            for d in checked_demands(said, look)]


def context_lines(said: str) -> list[str]:
    """첫 화면이 이미 따르는 사용자 말 (장르·색·조명·분위기). 움직임이 그와 어긋나지 않게 알려만 준다."""
    tags = [d["tag"] for d in crew_lib.query(said).demands if d["facet"] != "time_weather"]
    return [f"- The look already follows the user's words '{t}'; keep the motion consistent with them." for t in tags]


def pov_wanted(said: str) -> bool:
    """사용자가 1인칭(POV) 시점을 원하는지 ('파일럿의 1인칭시점', '1인칭뷰', '조종사 시점', 'POV')."""
    return bool(POV_RE.search(said or "")) or any(POV_TERM in d["terms"] for d in crew_lib.query(said).video_demands)


def pov_line() -> str:
    return ("The user asked for a first-person view: the camera is the viewer's eyes and the viewer never appears as a "
            "figure. Open \"shot\" with \"First-person POV from\" and the position, show only what lies ahead, and show the "
            "viewer's body only as their own hands or forearms at the bottom or side edge of the frame. Never write that "
            "the viewer sits, wears or looks, never describe the viewer's clothing, headset, helmet, head, hair, face, "
            "shoulders or back, and never frame the shot over a shoulder or from behind the viewer. Other people in the "
            "scene are described normally.")


def people_ok(said: str, frame: dict | None = None, scene: str = "", known: bool = True) -> bool:
    """사람을 써도 되는지. 요청·첫 화면·장면 어디든 사람이 있으면 된다.

    known 이 False 면(I2V 인데 첫 화면을 읽지 않았거나 못 읽음) 그림에 사람이 있는지 모르므로 막지 않는다.
    1인칭 요청은 보는 사람이 있는 것이다 (자기 손·팔뚝이 화면 가장자리에 나올 수 있다. '파일럿' 은 사람 말 사전에 없다).
    """
    if PERSON_REQ_RE.search(said) or skip_reason(said) in ("person", "character") or pov_wanted(said):
        return True
    if frame and frame.get("people"):
        return True
    if scene and crew_lib.PEOPLE_EN_RE.search(scene):
        return True
    return not known


def allowed_moves(said: str) -> int:
    """사용자가 직접 쓴 카메라 이동 수만큼은 허용한다 (예: '오빗하면서 다가가는' → 2). 기본은 1."""
    kinds = set()
    for d in crew_lib.query(said).video_demands:
        kinds |= set(camera_moves("the camera " + ", the camera ".join(d["terms"])))
    return max(1, len(kinds))


# ── 메뉴 ──────────────────────────────────────────────────

def _menu(said: str, cat: str | None, groups, rng, size: int, must=None) -> list[dict]:
    rng = rng or random.Random()
    must = list(must or [])
    ents = crew_lib.retrieve(said, FACET, k=size + 3, cat=cat, rng=rng, must=must, wild=0, only=list(groups))
    # 글자·로고·간판을 지시하는 항목(라벨 클로즈업, 네온 간판)은 영상 모델이 가짜 글자를 그리므로 뺀다
    ents = [e for e in ents if not TEXT_NOTE_RE.search(e["en"])]
    # 사용자 요구 항목은 자르지 않도록 앞에 두고 자른 뒤 다시 섞는다
    ents = [e for e in ents if e["id"] in must] + [e for e in ents if e["id"] not in must]
    ents = ents[:max(size, len([e for e in ents if e["id"] in must]))]
    rng.shuffle(ents)
    return ents


def build_menus(said: str, cat: str, rng: random.Random | None = None, size: int = MENU_SIZE) -> dict:
    """단계마다 video_motion 칸의 해당 묶음에서 메뉴를 뽑는다. 사용자 영상 요구 항목을 먼저 넣는다.

    한 단계에 요구가 둘 이상이면(예: '천천히 다가가는') 둘 다 맞는 항목을 먼저 넣는다.
    """
    rng = rng or random.Random()
    ents = crew_lib.load_library()["entries"]
    demands = crew_lib.query(said).video_demands
    menus = {}
    for st in steps_of(cat):
        groups = tuple(st["groups"])
        mine = []
        for d in demands:
            ids = [i for i in crew_lib.usable(d["ids"], said, cat) if ents[i]["group"].startswith(groups)
                   and not TEXT_NOTE_RE.search(ents[i]["en"])]
            if ids:
                mine.append(ids)
        must = []
        if len(mine) > 1:
            both = sorted(set(mine[0]).intersection(*mine[1:]))
            must += rng.sample(both, min(2, len(both)))
        for ids in mine:
            must += [i for i in rng.sample(ids, min(2, len(ids))) if i not in must]
        menu = _menu(said, cat, groups, rng, size, must)
        if st.get("still") and not any(e["group"] == STILL_GROUP for e in menu):
            pool = crew_lib.retrieve(said, FACET, k=1, cat=cat, rng=rng, wild=0, only=[STILL_GROUP])
            menu += pool[:1]   # '배경은 고요하게' 도 고를 수 있게 (모든 영상이 배경까지 움직이지 않게)
        menus[st["key"]] = menu
    return menus


def menu_text(menus: dict, cat: str) -> str:
    lines = []
    for st in steps_of(cat):
        opts = "; ".join(f"{e['id']} = {e['ko']} ({e['en']})" for e in menus[st["key"]])
        lines.append(f"- {st['key']} ({st['title']}, choose exactly one id): {opts}")
    return "\n".join(lines)


def idea_lines(said: str, rng: random.Random | None = None) -> list[str]:
    """분야 메뉴가 없는 요청에 주는 선택 아이디어 (같은 움직임만 반복되지 않게)."""
    out = []
    for st in load_knowledge().get("idea_steps") or []:
        ents = _menu(said, None, st["groups"], rng, 2)
        if ents:
            out.append(f"- {st['key']}: " + " | ".join(e["en"] for e in ents))
    return out


# ── 첫 화면 읽기 ─────────────────────────────────────────────

def frame_file(username: str, slug: str, image: dict) -> str:
    """패널이 보낸 첫 화면(LoadImage 의 입력 이미지나 결과 이미지)의 경로. 폴더 밖은 거부한다."""
    filename = str(image.get("filename") or "").strip()
    sub = str(image.get("subfolder") or "").strip().strip("/")
    ftype = str(image.get("type") or "input")
    for tag in ("output", "input", "temp"):
        if filename.endswith(f" [{tag}]"):
            filename, ftype = filename[: -len(tag) - 3], tag
    if "/" in filename:   # LoadImage 값은 'pasted/a.png' 처럼 하위 폴더를 이름에 붙인다
        head, filename = filename.rsplit("/", 1)
        sub = f"{sub}/{head}".strip("/")
    if ftype == "output":
        return image_in(os.path.join(settings.results_dir, username), filename, sub, "결과 폴더")
    if ftype == "input":
        return image_in(os.path.join(settings.state_path(slug), "input"), filename, sub, "입력 폴더")
    raise PromptxError("첫 화면은 입력 폴더나 결과 폴더의 이미지만 쓸 수 있습니다.")


def frame_b64(path: str) -> str:
    """시각 모델에 보낼 첫 화면. 큰 이미지는 줄인다 (허브 이미지에만 Pillow 가 있다)."""
    if os.path.getsize(path) > FRAME_MAX_BYTES:
        raise PromptxError("첫 화면 이미지가 너무 큽니다.")
    with open(path, "rb") as f:
        raw = f.read()
    try:
        from PIL import Image
    except ImportError:
        return base64.b64encode(raw).decode()
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((FRAME_MAX_SIDE, FRAME_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "PNG")
    except Exception:  # noqa: BLE001 - 깨진 이미지
        raise PromptxError("첫 화면 이미지를 읽지 못했습니다.") from None
    return base64.b64encode(buf.getvalue()).decode()


def frame_messages(b64: str) -> list[dict]:
    return [{"role": "system", "content": FRAME_RULES},
            {"role": "user", "content": "Describe this first frame.", "images": [b64]}]


def parse_frame(data) -> dict:
    data = data if isinstance(data, dict) else {}

    def s(k):
        return str(data.get(k) or "").strip()[:160]

    raw = data.get("movable")
    movable = [str(x).strip()[:80] for x in raw if str(x).strip()][:8] if isinstance(raw, list) else []
    try:
        people = max(0, int(data.get("people") or 0))
    except (TypeError, ValueError):
        people = 0
    return {"subject": s("subject"), "where": s("where"), "setting": s("setting"), "people": people,
            "movable": movable, "shot": s("shot"), "light": s("light"), "colours": s("colours"),
            "text_visible": data.get("text_visible") is True}


def frame_text(frame: dict) -> str:
    lines = [f"- subject: {frame['subject']}" + (f" ({frame['where']})" if frame.get("where") else ""),
             f"- setting: {frame['setting']}", f"- people: {frame['people']}"]
    if frame.get("movable"):
        lines.append("- things that can move: " + ", ".join(frame["movable"]))
    if frame.get("shot"):
        lines.append(f"- shot: {frame['shot']}")
    # 장문 I2V 가 빛·색을 지어내지 않고 실제 그림의 것을 쓰게 한다
    if frame.get("light"):
        lines.append(f"- light: {frame['light']}")
    if frame.get("colours"):
        lines.append(f"- colours: {frame['colours']}")
    return "\n".join(lines)


async def read_frame(call, b64: str) -> dict:
    data = await call({"messages": frame_messages(b64), "format": "json", "think": False, "options": {"temperature": 0}})
    return parse_frame(parse_json_reply((data.get("message") or {}).get("content", "")))


# ── 지시문·스키마·검사 ────────────────────────────────────────

def build_messages(text: str, notes: str, profile: dict, seconds: float, frame: dict | None = None, scene: str = "",
                   cat: str | None = None, menus: dict | None = None, rng=None, style: str = "") -> list[dict]:
    kn = load_knowledge()
    said = f"{text} {notes}".strip()
    if cat and menus:
        field = (f"Field rule: {kn['categories'][cat]['rule']}\n"
                 "MENU for this request (picked from a large library; each line shows id = Korean name (motion pattern)). "
                 "Choose exactly one id per step and build the prompt around those choices.\n"
                 f"{menu_text(menus, cat)}\n")
        choices_line = '- "choices": for every step the chosen id.\n'
    else:
        field = kn["general_rule"] + "\n"
        choices_line = ""
    lo, hi = profile["words"]
    long, mode, pov = is_long(profile), profile["mode"], pov_wanted(said)
    if long:
        spec = long_spec()
        # 밀도 예시는 T2V 에만 준다 (I2V 는 외형을 첫 화면에서만 가져와야 해서 예시의 빛·색을 따라 쓰면 안 된다)
        ex = spec.get("example") if mode == "t2v" else None
        example = ("\n\nEXAMPLE of the length, density and kind of detail wanted (a different, live-action request: never "
                   "copy its subject, objects, setting, sentences or film finish; take the medium and finish from the "
                   "request, the LOOK REFERENCE and the STYLE line):\n"
                   + "\n".join(f'- "{p["key"]}": {ex[p["key"]]}' for p in spec["parts"])) if ex else ""
        target = (lo + hi) // 2
        system = SYSTEM_LONG.format(
            model=model_name(profile), seconds=seconds, mode_rule=spec["modes"][mode],
            rules="\n".join(f"- {r}" for r in kn["global_rules"] + spec["rules"]), field=field,
            choices_line=choices_line, parts="\n".join(f'- "{p["key"]}": one English paragraph: {part_spec(p, mode)}.'
                                                       for p in spec["parts"]),
            lo=lo, hi=hi, target=target, part_target=target // len(spec["parts"]), part_min=spec["part_min_words"],
            style=profile.get("style", ""), example=example)
    else:
        system = SYSTEM.format(model=model_name(profile), seconds=seconds, mode_rule=kn["modes"][mode],
                               rules="\n".join(f"- {r}" for r in kn["global_rules"]), field=field,
                               choices_line=choices_line, lo=lo, hi=hi, style=profile.get("style", ""))
    see = unsign if long else (lambda t: t)   # 장문은 참고 글을 다시 쓰므로 '네온 간판' 을 '네온 불빛' 으로 보여 준다
    parts = [f"Request (Korean): {text}"]
    if notes:
        parts.append(f"Must-keep details from the user: {notes}")
    if frame:
        parts.append("FIRST FRAME (what the image actually shows, read by a vision model):\n" + see(frame_text(frame)))
    if scene:
        if long and mode == "i2v":   # 장문은 외형까지 쓰므로 참고 글을 다시 써도 된다. 대신 어긋나지 않게 한다
            label = "Prompt the first frame was made from (reference: stay consistent with it and never contradict it)"
        elif long and pov:   # 이미지 도우미는 1인칭을 몰라 장면 속 사람을 밖에서 그렸을 수 있다
            label = ("LOOK REFERENCE (an image prompt for this shot: keep its setting, colours, materials, lighting and "
                     "style; the user asked for a first-person view, so the person it shows is the viewer: describe only "
                     "what they see and at most their own hands or forearms, never how that person looks from outside)")
        elif long:
            label = ("LOOK REFERENCE (an image prompt for this shot: keep its subject, setting, colours, materials, "
                     "lighting and style; you may reuse its words)")
        else:
            label = ("Prompt the first frame was made from (reference only; do not restate it)" if mode == "i2v"
                     else "SCENE (fixed, placed before your text; do not restate it)")
        parts.append(f"{label}: {see(scene)}")
    if long and mode == "t2v" and style.strip():   # compose() 가 T2V 끝에 그림체 문장을 붙인다
        parts.append("STYLE (appended after your text by the system; open \"shot\" by naming this medium in a few words, "
                     "describe light, colour and texture the way this medium shows them, and write no live-action, "
                     f"film-look, film-grain or other photographic words that contradict it): {style.strip()}")
    look = owns_look(profile)
    dl = demand_lines(said, look)
    if dl:
        parts.append("USER WORDS (top priority, choose matching menu ids and show each one clearly in the prompt):\n"
                     + "\n".join(dl))
    cl = [] if look else context_lines(said)   # T2V 장문에는 첫 화면이 없어 '외형은 이미 따른다' 가 거짓이다
    if cl:
        parts.append("\n".join(cl))
    if not (cat and menus):
        il = idea_lines(said, rng)
        if il:
            parts.append("Optional ideas from our library (use one only if it fits the request and the first frame):\n"
                         + "\n".join(il))
    if pov:
        parts.append(pov_line())
    if not people_ok(said, frame, scene, frame is not None or profile["mode"] == "t2v"):
        parts.append("No person appears in the request, the scene or the first frame; keep every human out of the clip.")
    if hides_hands(said):
        parts.append("The hands stay hidden: never write hand, finger, palm, fist, holding, grip, grasp or clutching.")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]


def build_schema(cat: str | None = None, menus: dict | None = None, long: bool = False) -> dict:
    s = {"type": "string"}
    props, req = {}, []
    if cat and menus:   # 고른 항목을 먼저 쓰게 한다 (글을 쓴 뒤에 끼워 맞추지 않게)
        cp = {st["key"]: {"type": "string", "enum": [e["id"] for e in menus[st["key"]]]} for st in steps_of(cat)}
        props["choices"] = {"type": "object", "properties": cp, "required": list(cp)}
        req.append("choices")
    props["ko"] = {"type": "object", "properties": {k: s for k in KO_KEYS}, "required": list(KO_KEYS)}
    if long:   # 장문은 네 단락을 칸으로 받는다 (순서를 바꾸거나 단락을 빼먹지 않게)
        keys = [p["key"] for p in long_spec()["parts"]]
        props.update({k: s for k in keys})
        return {"type": "object", "properties": props, "required": req + ["ko"] + keys}
    props["prompt"] = s
    return {"type": "object", "properties": props, "required": req + ["ko", "prompt"]}


def assemble(result) -> str:
    """장문 프로필의 네 단락을 정해진 순서로 잇는다. 단락 안의 줄바꿈은 한 칸으로 줄인다."""
    if not isinstance(result, dict):
        return ""
    paras = [re.sub(r"\s+", " ", str(result.get(p["key"]) or "")).strip() for p in long_spec()["parts"]]
    return "\n\n".join(p for p in paras if p)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def restated(prompt: str, scene: str, n: int = RESTATE_WORDS) -> str | None:
    """첫 화면(장면) 프롬프트와 n 낱말 넘게 연달아 같은 구절. 없으면 None."""
    sw = _words(scene)
    grams = {tuple(sw[i:i + n]) for i in range(len(sw) - n + 1)}
    pw = _words(prompt)
    for i in range(len(pw) - n + 1):
        if tuple(pw[i:i + n]) in grams:
            return " ".join(pw[i:i + n])
    return None


def camera_moves(prompt: str) -> list[str]:
    return [k for k, rx in CAMERA_MOVES.items() if rx.search(prompt or "")]


def demand_choice_problems(demands: list[dict], picked: dict, cat: str, menus: dict) -> list[str]:
    """사용자 영상 요구마다, 그와 맞는 항목이 메뉴에 있었다면 어느 단계에서든 하나는 골라야 한다.

    다만 그 요구를 고를 수 있던 단계가 모두 다른 요구에 쓰였으면 묻지 않는다 ('드론 + 푸시인' 처럼 카메라 한 칸에
    요구가 둘이고 둘 다 맞는 항목이 없을 때 초안이 영영 통과하지 못했다). 그 요구는 글의 확인어로 지킨다.
    """
    hit = lambda d, key: picked.get(key) in set(d["ids"])  # noqa: E731
    out = []
    for d in demands:
        steps = [st["key"] for st in steps_of(cat) if set(d["ids"]) & {e["id"] for e in menus.get(st["key"], [])}]
        if not steps or any(hit(d, k) for k in steps):
            continue
        free = [k for k in steps if not any(o is not d and hit(o, k) for o in demands)]
        if free:
            out.append(f"The user wrote '{d['tag']}', so step \"{free[0]}\" must use an id that matches it.")
    return out


def check_video(result, profile: dict, said: str, frame: dict | None = None, scene: str = "",
                cat: str | None = None, menus: dict | None = None) -> list[str]:
    """움직임 초안 검사. 어긋난 점을 영문 문장 목록으로 돌려준다 (그대로 고쳐 쓰기 지시에 넣는다)."""
    long = is_long(profile)
    if not isinstance(result, dict):
        return ["Return a JSON object with ko and the four parts." if long else "Return a JSON object with ko and prompt."]
    problems: list[str] = []
    demands = checked_demands(said, owns_look(profile))
    if cat and menus:
        choices = result.get("choices") if isinstance(result.get("choices"), dict) else {}
        picked = {}
        for st in steps_of(cat):
            v = choices.get(st["key"])
            if not isinstance(v, str) or v not in {e["id"] for e in menus[st["key"]]}:
                problems.append(f'Step "{st["key"]}" needs exactly one id from its menu.')
            else:
                picked[st["key"]] = v
        if picked.get("subject") and picked.get("subject") == picked.get("environment"):
            problems.append('Steps "subject" and "environment" must use different ids.')
        problems += demand_choice_problems([d for d in demands if d["facet"] == FACET], picked, cat, menus)
    ko = result.get("ko") if isinstance(result.get("ko"), dict) else {}
    for k in KO_KEYS:
        if not HANGUL_RE.search(str(ko.get(k) or "")):
            problems.append(f'"ko.{k}" must be a Korean sentence.')
    if long:
        spec = long_spec()
        need = int(spec["part_min_words"])
        for p in spec["parts"]:
            n = len(str(result.get(p["key"]) or "").split())
            if n < need:
                problems.append(f'"{p["key"]}" has {n} words; write at least {need} words: '
                                f'{part_spec(p, profile["mode"])}.')
        prompt = assemble(result)
    else:
        prompt = str(result.get("prompt") or "").strip()
    if not prompt:
        return problems + ['"prompt" is empty.']
    if HANGUL_RE.search(prompt):
        # 장문에는 prompt 칸이 없으므로 고칠 단락 이름을 알려 준다
        bad = ([p["key"] for p in long_spec()["parts"] if HANGUL_RE.search(str(result.get(p["key"]) or ""))]
               if long else []) or ["prompt"]
        problems.append(" and ".join(f'"{k}"' for k in bad) + " must be English only.")
    lo, hi = profile["words"]
    n = len(prompt.split())
    if not lo <= n <= hi:
        problems.append(f"The prompt has {n} words; write {lo} to {hi} words.")
    # 장문은 장면까지 스스로 쓰므로 프롬프트 하나가 전부다
    full = f"{scene} {prompt}" if profile["mode"] == "t2v" and not long else prompt
    for d in demands:
        # 카메라·속도 말은 움직임 글에 있어야 한다 (장면 글의 'slow shutter' 로 통과하지 않게). 날씨는 장면에 있어도 된다
        if not crew_lib.has_term(prompt if d["facet"] == FACET else full, d["terms"]):
            problems.append(f"The user wrote '{d['tag']}'; the prompt must clearly show it with words such as: "
                            + ", ".join(d["terms"][:6]) + ".")
    seen = set()
    for m in NEGATION_RE.finditer(prompt):
        if m.group(0).lower() not in seen:
            seen.add(m.group(0).lower())
            problems.append(f'Negative wording "{m.group(0)}" in the prompt; state what should be seen instead.')
    if TEXT_NOTE_RE.search(prompt):
        problems.append("Do not mention text, letters, logos, labels, signs or quotation marks in the prompt.")
    if not people_ok(said, frame, scene, frame is not None or profile["mode"] == "t2v") and PERSON_NOTE_RE.search(prompt):
        problems.append("No person appears in the request or the first frame, so do not add any person or human hands.")
    if hides_hands(said):
        # 영상 글의 'hold' 는 카메라가 멈춘다는 뜻으로 쓴다 ('a calm final hold'). 손 낱말로 보지 않는다
        found = sorted({m.group(0).lower() for m in HAND_WORDS_RE.finditer(prompt)} - {"hold", "holds"})
        if found:
            problems.append("The hands must stay hidden, so remove: " + ", ".join(found) + ".")
    if CUT_RE.search(prompt):
        problems.append("Keep one continuous shot: do not write cuts, scene changes or transitions.")
    moves = camera_moves(prompt)
    if len(moves) > allowed_moves(said):
        # 걸린 구절을 함께 보여 줘야 긴 글에서 어느 절을 고칠지 안다
        seen_moves = "; ".join(f'{k} ("{CAMERA_MOVES[k].search(prompt).group(0)}")' for k in moves)
        problems.append(f"Use one main camera move for the whole clip; the prompt combines: {seen_moves}.")
    if pov_wanted(said):
        m = SELF_BODY_RE.search(prompt)
        if m:
            problems.append(f'The user asked for a first-person view, so the camera is the viewer\'s eyes ("{m.group(0)}"): '
                            "show only what lies ahead and at most the viewer's own hands or forearms at the frame edge; "
                            "the viewer never appears as a figure, is never framed over a shoulder or from behind, and "
                            "other people in the scene stay as they are.")
    if long:
        buzz = [t for t in sorted({m.group(0).lower() for m in BUZZ_RE.finditer(prompt)}) if not user_allows(t, said)]
        if buzz:
            problems.append("Replace quality buzzwords with visible details (light, lens, colour, texture): "
                            + ", ".join(buzz) + ".")
    elif scene:   # 장문은 외형까지 쓰므로 장면 글을 다시 써도 된다
        same = restated(prompt, scene)
        if same:
            problems.append(f'Do not restate the look already fixed by the first frame or scene ("{same}"); describe motion only.')
    return problems


PART_KO = {"shot": "구도", "camera_light": "카메라와 빛", "action": "움직임", "ending": "마무리"}
# 검사 문구(영문)를 한국어 안내로 풀어 쓴다. 영문 네 단락에서 무엇을 고칠지 알기 어려운 사용자에게 보여 준다
_KO_RULES = [
    (re.compile(r"^The prompt has (\d+) words; write (\d+) to (\d+) words"),
     lambda m: f"글이 {m[1]}낱말입니다. 권장 범위는 {m[2]}~{m[3]}낱말이라 "
               f"{'줄여야' if int(m[1]) > int(m[3]) else '늘려야'} 합니다."),
    (re.compile(r'^"(\w+)" has (\d+) words; write at least (\d+) words'),
     lambda m: f"'{PART_KO.get(m[1], m[1])}' 단락이 {m[2]}낱말로 짧습니다. 최소 {m[3]}낱말은 써야 합니다."),
    (re.compile(r"must be English only"), lambda m: "한글이 섞여 있습니다. 프롬프트는 영문만 써야 합니다."),
    (re.compile(r"^The user wrote '(.+?)'; the prompt must clearly show it"),
     lambda m: f"요청에 쓰신 '{m[1]}'이(가) 글에 뚜렷하게 나타나지 않습니다."),
    (re.compile(r'^Negative wording "(.+?)"'),
     lambda m: f"부정 표현('{m[1]}')이 있습니다. 영상 모델은 부정어를 잘 이해하지 못해 '무엇이 보여야 하는지'로 바꿔야 합니다."),
    (re.compile(r"^Do not mention text, letters"),
     lambda m: "글자·로고·간판·따옴표가 들어 있습니다. 영상에 가짜 글자가 그려질 수 있어 빼야 합니다."),
    (re.compile(r"^No person appears"),
     lambda m: "요청과 첫 화면에 사람이 없는데 글에 사람이나 손이 들어 있습니다."),
    (re.compile(r"^The hands must stay hidden, so remove: (.+)\."),
     lambda m: f"손을 숨기라고 하셨는데 손과 관련된 말이 있습니다: {m[1]}"),
    (re.compile(r"^Keep one continuous shot"),
     lambda m: "컷 전환이나 장면 전환 표현이 있습니다. 한 번에 이어지는 한 컷이어야 합니다."),
    (re.compile(r"^Use one main camera move.*combines: (.+)\.$"),
     lambda m: f"카메라 이동이 둘 이상입니다 ({m[1]}). 하나만 남겨야 합니다."),
    (re.compile(r'^The user asked for a first-person view.*?\("(.+?)"\)'),
     lambda m: f"1인칭 요청인데 보는 사람 자신이 화면에 나오는 표현('{m[1]}')이 있습니다."),
    (re.compile(r"^Replace quality buzzwords.*?: (.+)\.$"),
     lambda m: f"품질 유행어({m[1]})가 있습니다. 화면에 보이는 구체적인 묘사(빛·렌즈·색·질감)로 바꿔야 합니다."),
    (re.compile(r"^Do not restate the look"),
     lambda m: "첫 화면에 이미 있는 외형을 다시 설명했습니다. 움직임만 써야 합니다."),
    (re.compile(r'^"ko\.(\w+)" must be a Korean sentence'),
     lambda m: "한국어 설명 칸이 비어 있거나 한국어가 아닙니다."),
    (re.compile(r"^Steps? \"|^Step "), lambda m: "제작진 단계의 선택이 규칙에 맞지 않습니다."),
    (re.compile(r'^"prompt" is empty'), lambda m: "프롬프트가 비어 있습니다."),
]


def explain_ko(problems) -> list[str]:
    """검사 위반(영문)을 한국어 안내로 바꾼다. 풀이를 모르는 문구는 영문 그대로 둔다."""
    out = []
    for p in problems or []:
        p = str(p)
        for rx, fmt in _KO_RULES:
            m = rx.search(p)
            if m:
                out.append(fmt(m))
                break
        else:
            out.append(p)
    return out


def describe(result: dict, cat: str) -> dict:
    """패널에 보여 줄 고른 항목 (한국어 이름). 검사를 통과한 초안에만 쓴다."""
    choices = result.get("choices") or {}
    picked = []
    for st in steps_of(cat):
        eid = choices.get(st["key"])
        e = crew_lib.entry(eid) if isinstance(eid, str) else None
        picked.append({"step": st["title"], "id": eid, "ko": e["ko"] if e else ""})
    return {"picked": picked}


def compose(mode: str, scene: str, motion: str, style: str = "", long: bool = False) -> str:
    """최종 프롬프트. I2V 는 움직임만(외형과 그림체는 첫 화면이 정한다), T2V 는 장면 + 움직임 + 그림체 고정 문장.

    장문은 네 단락이 전부다(장면을 이미 담았다). T2V 면 그림체 고정 문장만 마지막 단락으로 붙인다.
    """
    if long:
        body = motion.strip()
        return f"{body}\n\n{style.strip()}" if mode == "t2v" and body and style.strip() else body
    motion = motion.strip().rstrip(".")
    if mode != "t2v":
        return f"{motion}." if motion else ""
    body = ". ".join(p for p in (scene.strip().rstrip("."), motion) if p)
    return f"{body}. {style.strip()}" if style.strip() else f"{body}."


async def draft(call, text: str, notes: str, profile: dict, seconds: float, frame: dict | None = None,
                scene: str = "", cat: str | None = None, menus: dict | None = None, repair_rounds: int | None = None,
                rng=None, style: str = "", start: tuple[str, list[str]] | None = None):
    """한 GPU 차례(llm_turn) 안에서 움직임 초안을 받는다. (결과, 회차, 남은 위반) 을 돌려준다.

    start=(앞서 쓴 답의 JSON 글, 그 위반 목록) 이면 처음 쓰기 없이 그 답을 고쳐 쓰는 것부터 시작한다(자동 수정)."""
    rounds_max = settings.video_repair_rounds if repair_rounds is None else repair_rounds
    said = f"{text} {notes}".strip()
    long = is_long(profile)
    messages = await asyncio.to_thread(build_messages, text, notes, profile, seconds, frame, scene, cat, menus, rng,
                                       style)
    schema = build_schema(cat, menus, long=long)
    if start:
        messages = messages + [{"role": "assistant", "content": start[0]}, repair_message(start[1])]
    result, problems = {}, ["no answer"]
    best = None   # 고쳐 쓴 답이 더 나빠지면(깨진 JSON 등) 앞의 답을 돌려준다
    for rnd in range(1, rounds_max + 2):
        data = await call({"messages": messages, "format": schema, "think": False, "options": {"temperature": 0.5}})
        content = (data.get("message") or {}).get("content", "")
        try:
            result = parse_json_reply(content)
        except PromptxError as e:
            result, problems = {}, [str(e)]
        else:
            if long and isinstance(result, dict):
                result["prompt"] = assemble(result)   # 네 단락을 이어 아래(검사·가장 나은 답 고르기·결과)에서 한 글로 쓴다
            problems = check_video(result, profile, said, frame, scene, cat, menus)
        if not problems:
            return result, rnd, []
        if result.get("prompt") and (best is None or len(problems) < len(best[1])):
            best = (result, problems)
        messages = messages + [{"role": "assistant", "content": content}, repair_message(problems)]
    if best:
        result, problems = best
    return result, rounds_max + 1, problems


# ── GPU 를 쓰는 부분 (대기열 경유) ──────────────────────────

async def render(user, ip: str, username: str, slug: str, text: str, notes: str = "", style: str = "",
                 profile_id: str | None = None, seconds=None, image: dict | None = None, image_prompt: str = "",
                 crew=None, read_frame_on: bool = True, rng=None) -> dict:
    """요청 → (첫 화면 읽기) → (T2V 장면) → 움직임 초안 → 최종 영상 프롬프트와 설정 제안."""
    from . import llm   # llm → scheduler 를 불러오므로 순환을 피해 여기서

    text = clean_input(text)
    notes = str(notes or "").strip()[:2000]
    style = str(style or "").strip()[:2000]
    scene = str(image_prompt or "").strip()[:4000]
    pid, profile = profile_of(profile_id)
    mode = profile["mode"]
    suggested = suggest_settings(profile, seconds)
    said = f"{text} {notes}".strip()
    advice = []
    if suggested["capped"]:
        advice.append(f"이 모델은 한 번에 약 {suggested['seconds']}초({suggested['frames']}프레임)까지라 그 길이로 맞췄습니다. "
                      "더 긴 영상은 클립을 나눠 이어 붙여야 합니다.")
    b64 = None
    if mode == "i2v" and isinstance(image, dict) and image.get("filename") and read_frame_on:
        try:   # 첫 화면을 못 찾거나 못 열어도 글만 보고 계속 쓴다
            b64 = await asyncio.to_thread(frame_b64, frame_file(username, slug, image))
        except (PromptxError, OSError) as e:
            advice.append(f"첫 화면을 읽지 못해 요청 글만 보고 움직임을 썼습니다 ({e}).")
    elif mode == "i2v" and not (isinstance(image, dict) and image.get("filename")):
        advice.append("첫 화면 이미지를 찾지 못해 요청 글만 보고 움직임을 썼습니다. LoadImage 노드가 있으면 첫 화면을 읽습니다.")
    rt, crew_advice = promptx.crew_route(text, notes, crew)
    advice += [a.replace("제작진 모드를", "영상 제작진 단계를") for a in crew_advice]
    cat = rt["category"] if rt else None
    model, frame_model = settings.expand_model, settings.video_frame_model
    frame, scene_info, crew_info, menus = None, None, None, None
    result, rounds, left = {}, 0, []
    try:
        if b64 is not None and frame_model != model:
            # 시각 모델이 확장 모델과 다르면 차례를 따로 받는다 (한 차례에는 모델 하나만 올린다)
            async with llm.llm_turn(user, ip, frame_model, "video_frame") as call:
                frame = await _safe_read(call, b64, advice)
        async with llm.llm_turn(user, ip, model, "video_expand") as call:
            if b64 is not None and frame_model == model:
                frame = await _safe_read(call, b64, advice)
            if is_long(profile) and mode == "i2v" and not frame_known(frame) and not scene and profile.get("fallback"):
                # 첫 화면도 참고 글도 모르면 장문은 빛·색·위치를 지어 쓰게 된다. 이번 요청만 움직임만 프로필로 쓴다
                pid, profile = profile_of(profile["fallback"])
                advice.append("첫 화면 정보와 도우미 확장 결과가 없어 장문 대신 움직임만 썼습니다. "
                              "장문은 첫 화면을 읽거나 도우미 결과가 있을 때 씁니다.")
            if mode == "t2v" and not scene:
                # T2V 는 첫 화면이 없으므로 같은 차례 안에서 이미지 확장(제작진·도우미)으로 장면부터 쓴다
                scene_info = await promptx.expand_in_turn(call, text, style, notes, crew)
                scene = scene_info["body"]
                advice += [a for a in scene_info.get("advice") or [] if a not in advice]
            if cat:
                menus = await asyncio.to_thread(build_menus, said, cat, rng)
                result, rounds, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene,
                                                   cat, menus, rng=rng, style=style)
                crew_info = {"category": cat, "title": load_knowledge()["categories"][cat]["title"], "rounds": rounds,
                             "used": not left, "violations": left}
                if left:
                    advice.append("영상 제작진 초안이 규칙을 통과하지 못해 단계 메뉴 없이 다시 썼습니다.")
                else:
                    crew_info.update(describe(result, cat))
            if not cat or left:
                result, more, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene, rng=rng,
                                                 style=style)
                rounds += more
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    long = is_long(profile)
    motion = str(result.get("prompt") or "").strip()
    ko = result.get("ko") if isinstance(result.get("ko"), dict) else {}
    parts = {p["key"]: str(result.get(p["key"]) or "").strip() for p in long_spec()["parts"]} if long else None
    return {"prompt": compose(mode, scene, motion, style, long), "motion": motion, "long": long, "parts": parts,
            "scene": scene if mode == "t2v" else "", "profile": pid, "profile_title": profile["title"], "mode": mode,
            "settings": suggested, "frame": frame, "ko": {k: str(ko.get(k) or "") for k in KO_KEYS},
            "crew": crew_info, "scene_crew": (scene_info or {}).get("crew"),
            "scene_violations": (scene_info or {}).get("violations") or [],
            "violations": left, "violations_ko": explain_ko(left), "rounds": rounds, "model": model,
            "frame_model": frame_model if frame else None, "advice": advice}


async def fix(user, ip: str, username: str, slug: str, text: str, notes: str = "", style: str = "",
              profile_id: str | None = None, seconds=None, current=None, frame=None, scene: str = "") -> dict:
    """패널에 떠 있는 영상 프롬프트의 규칙 위반을 한 GPU 차례 안에서 자동으로 고쳐 쓴다.

    새로 쓰지 않고 현재 글과 위반 목록을 모델에 주고 고쳐 받는다(요청의 뜻과 잘 쓴 부분을 지키려고).
    위반은 패널이 보낸 값을 믿지 않고 다시 검사한다. 첫 화면은 다시 읽지 않고 패널이 가진 읽기 결과를 쓴다.
    """
    from . import llm

    text = clean_input(text)
    notes = str(notes or "").strip()[:2000]
    style = str(style or "").strip()[:2000]
    scene = str(scene or "").strip()[:4000]
    pid, profile = profile_of(profile_id)
    mode, long = profile["mode"], is_long(profile)
    suggested = suggest_settings(profile, seconds)
    said = f"{text} {notes}".strip()
    cur = current if isinstance(current, dict) else {}
    ko_in = cur.get("ko") if isinstance(cur.get("ko"), dict) else {}
    result: dict = {"ko": {k: str(ko_in.get(k) or "")[:400] for k in KO_KEYS}}
    if long:
        parts_in = cur.get("parts") if isinstance(cur.get("parts"), dict) else {}
        for p in long_spec()["parts"]:
            result[p["key"]] = str(parts_in.get(p["key"]) or "")[:6000]
        result["prompt"] = assemble(result)
    else:
        result["prompt"] = str(cur.get("motion") or "")[:4000]
    frame = parse_frame(frame) if isinstance(frame, dict) else None
    before = check_video(result, profile, said, frame, scene)
    after, rounds, left = result, 0, []
    if before:
        model = settings.expand_model
        start = (json.dumps({k: v for k, v in result.items() if k != "prompt"}, ensure_ascii=False), before)
        try:
            async with llm.llm_turn(user, ip, model, "video_fix") as call:
                after, rounds, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene,
                                                  repair_rounds=2, style=style, start=start)
        except llm.HelperBusy as e:
            raise PromptxError(str(e), 503) from None
        except RuntimeError as e:
            raise PromptxError(str(e), 502) from None
        if not after.get("prompt") or len(left) >= len(before):   # 더 나아지지 않았으면 원래 글을 지킨다
            after, left = result, before
    motion = str(after.get("prompt") or "").strip()
    ko = after.get("ko") if isinstance(after.get("ko"), dict) else {}
    parts = {p["key"]: str(after.get(p["key"]) or "").strip() for p in long_spec()["parts"]} if long else None
    return {"prompt": compose(mode, scene, motion, style, long), "motion": motion, "long": long, "parts": parts,
            "ko": {k: str(ko.get(k) or "") for k in KO_KEYS}, "profile": pid, "mode": mode,
            "violations": left, "violations_ko": explain_ko(left), "before_ko": explain_ko(before),
            "words_before": len(str(result.get("prompt") or "").split()), "words_after": len(motion.split()),
            "rounds": rounds, "changed": bool(before) and after is not result, "fixed": bool(before) and not left}


async def _safe_read(call, b64: str, advice: list) -> dict | None:
    """첫 화면 읽기가 실패해도 글만 보고 계속 쓴다."""
    try:
        return await read_frame(call, b64)
    except (PromptxError, RuntimeError):   # 시각 모델 오류(Ollama 실패 포함)도 같은 처리
        advice.append("첫 화면을 읽지 못해 요청 글만 보고 움직임을 썼습니다.")
        return None
