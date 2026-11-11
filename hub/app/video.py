"""영상 렌더러: 요청과 첫 화면을 보고 영상 모델용 움직임 프롬프트를 쓴다 (docs/video-renderer.md).

- 이미지 프롬프트는 정지 장면의 외형을 길게 설명하는 글이라 I2V 에 그대로 넣으면 움직임 지시가 약하다.
  렌더러는 외형을 다시 쓰지 않고 카메라·대상 움직임·환경 움직임·속도·마무리만 쓴다.
- 사용자 말이 최우선이다. '천천히', '드론', '루프' 같은 영상 말과 '눈 오는' 같은 날씨 말은 선택과 글 모두에서 지킨다.
- 제품·공간·풍경 요청은 제작진처럼 단계 메뉴(crew_library 의 video_motion 칸)에서 고르고, 그 밖의 요청은 규칙만으로 쓴다.
- 첫 화면이 있으면(I2V) 시각 모델이 먼저 읽어 실제 그림에 있는 것만 움직이게 한다.
- T2V 는 첫 화면이 없으므로 이미지 확장(도우미·제작진)으로 장면을 먼저 쓰고 그 뒤에 움직임을 붙인다.

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
                      parse_json_reply, repair_message)

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video_knowledge.json")
FACET = "video_motion"
MENU_SIZE = 8
STILL_GROUP = "still_background"
KO_KEYS = ("camera", "motion", "timing")
FRAME_MAX_SIDE = 1024
FRAME_MAX_BYTES = 40 * 1024 * 1024   # 이보다 큰 파일은 읽지 않는다 (허브 메모리 보호)
RESTATE_WORDS = 6        # 첫 화면 프롬프트와 이만큼 연달아 같은 낱말이 나오면 외형을 다시 쓴 것으로 본다
HANGUL_RE = re.compile(r"[가-힣]")

# 한 클립 안에서 장면을 바꾸라는 말 (컷 구성은 v2 에서 클립을 나눠 처리한다)
CUT_RE = re.compile(r"\b(?:cuts? to|jump cuts?|hard cut|smash cut|scene changes?|transitions? (?:to|into)|next scene|new scene)\b",
                    re.I)
# 카메라 이동 종류. 둘 이상이면 짧은 클립에서 하나도 제대로 안 된다고 본다 [추정, A/B 로 조정].
# 크레인과 틸트는 함께 쓰는 한 동작이 많아 한 종류로 센다.
_CAM = r"\b(?:camera|drone|shot|lens)\b[^.;]{0,30}?\b"
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

FRAME_RULES = """You look at one still image that will be the first frame of a short video. Report only what you actually see.
Return only JSON:
{"subject": "the main subject in a few words", "where": "where the main subject sits in the frame", "setting": "the place and background in a few words", "people": 0, "movable": ["things in the image that could move naturally, such as liquid, steam, smoke, fabric, hair, leaves, water, clouds, light, particles"], "shot": "shot size and camera angle", "text_visible": false}
"people" is the number of people visible; count a visible human hand or arm as one person."""

SYSTEM = """You are a small video crew, a director of photography and a motion designer, writing the motion for one short clip.
Model: {model}. Clip: about {seconds} seconds, one continuous shot.
{mode_rule}
Rules:
{rules}
{field}Return JSON only:
{choices_line}- "ko": three short Korean sentences, one each for camera, motion and timing, explaining the plan to the user.
- "prompt": one English paragraph of {lo} to {hi} words. {style}"""


@lru_cache(maxsize=1)
def load_knowledge() -> dict:
    with open(KNOWLEDGE_PATH, encoding="utf-8") as f:
        return json.load(f)


def steps_of(cat: str) -> list[dict]:
    return load_knowledge()["categories"][cat]["steps"]


# ── 모델 프로필 ─────────────────────────────────────────────

def profile_of(pid: str | None) -> tuple[str, dict]:
    ps = load_knowledge()["profiles"]
    pid = str(pid or "") or next(iter(ps))
    if pid not in ps:
        raise PromptxError(f"모르는 영상 프로필입니다: {pid}")
    return pid, ps[pid]


def profile_list() -> list[dict]:
    """패널의 선택 목록 (모델 이름은 파일에만 있다)."""
    return [{"id": k, "title": p["title"], "mode": p["mode"], "default_seconds": p["default_seconds"],
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

def checked_demands(said: str) -> list[dict]:
    """영상 글에서 반드시 보여야 할 사용자 말: 카메라·속도·루프(영상 말)와 날씨·시간(눈·비·안개는 움직이는 것)."""
    q = crew_lib.query(said)
    return list(q.video_demands) + [d for d in q.demands if d["facet"] == "time_weather"]


def demand_lines(said: str) -> list[str]:
    return [f"- The user wrote '{d['tag']}'. The clip must clearly show it, using words such as: {', '.join(d['terms'][:6])}."
            for d in checked_demands(said)]


def context_lines(said: str) -> list[str]:
    """첫 화면이 이미 따르는 사용자 말 (장르·색·조명·분위기). 움직임이 그와 어긋나지 않게 알려만 준다."""
    tags = [d["tag"] for d in crew_lib.query(said).demands if d["facet"] != "time_weather"]
    return [f"- The look already follows the user's words '{t}'; keep the motion consistent with them." for t in tags]


def people_ok(said: str, frame: dict | None = None, scene: str = "", known: bool = True) -> bool:
    """사람을 써도 되는지. 요청·첫 화면·장면 어디든 사람이 있으면 된다.

    known 이 False 면(I2V 인데 첫 화면을 읽지 않았거나 못 읽음) 그림에 사람이 있는지 모르므로 막지 않는다.
    """
    if PERSON_REQ_RE.search(said) or skip_reason(said) in ("person", "character"):
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
            "movable": movable, "shot": s("shot"), "text_visible": data.get("text_visible") is True}


def frame_text(frame: dict) -> str:
    lines = [f"- subject: {frame['subject']}" + (f" ({frame['where']})" if frame.get("where") else ""),
             f"- setting: {frame['setting']}", f"- people: {frame['people']}"]
    if frame.get("movable"):
        lines.append("- things that can move: " + ", ".join(frame["movable"]))
    if frame.get("shot"):
        lines.append(f"- shot: {frame['shot']}")
    return "\n".join(lines)


async def read_frame(call, b64: str) -> dict:
    data = await call({"messages": frame_messages(b64), "format": "json", "think": False, "options": {"temperature": 0}})
    return parse_frame(parse_json_reply((data.get("message") or {}).get("content", "")))


# ── 지시문·스키마·검사 ────────────────────────────────────────

def build_messages(text: str, notes: str, profile: dict, seconds: float, frame: dict | None = None, scene: str = "",
                   cat: str | None = None, menus: dict | None = None, rng=None) -> list[dict]:
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
    system = SYSTEM.format(model=profile["title"], seconds=seconds, mode_rule=kn["modes"][profile["mode"]],
                           rules="\n".join(f"- {r}" for r in kn["global_rules"]), field=field,
                           choices_line=choices_line, lo=lo, hi=hi, style=profile.get("style", ""))
    parts = [f"Request (Korean): {text}"]
    if notes:
        parts.append(f"Must-keep details from the user: {notes}")
    if frame:
        parts.append("FIRST FRAME (what the image actually shows, read by a vision model):\n" + frame_text(frame))
    if scene:
        label = ("Prompt the first frame was made from (reference only; do not restate it)" if profile["mode"] == "i2v"
                 else "SCENE (fixed, placed before your text; do not restate it)")
        parts.append(f"{label}: {scene}")
    dl = demand_lines(said)
    if dl:
        parts.append("USER WORDS (top priority, choose matching menu ids and show each one clearly in the prompt):\n"
                     + "\n".join(dl))
    cl = context_lines(said)
    if cl:
        parts.append("\n".join(cl))
    if not (cat and menus):
        il = idea_lines(said, rng)
        if il:
            parts.append("Optional ideas from our library (use one only if it fits the request and the first frame):\n"
                         + "\n".join(il))
    if not people_ok(said, frame, scene, frame is not None or profile["mode"] == "t2v"):
        parts.append("No person appears in the request, the scene or the first frame; keep every human out of the clip.")
    if hides_hands(said):
        parts.append("The hands stay hidden: never write hand, finger, palm, fist, holding, grip, grasp or clutching.")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]


def build_schema(cat: str | None = None, menus: dict | None = None) -> dict:
    s = {"type": "string"}
    props, req = {}, []
    if cat and menus:   # 고른 항목을 먼저 쓰게 한다 (글을 쓴 뒤에 끼워 맞추지 않게)
        cp = {st["key"]: {"type": "string", "enum": [e["id"] for e in menus[st["key"]]]} for st in steps_of(cat)}
        props["choices"] = {"type": "object", "properties": cp, "required": list(cp)}
        req.append("choices")
    props["ko"] = {"type": "object", "properties": {k: s for k in KO_KEYS}, "required": list(KO_KEYS)}
    props["prompt"] = s
    return {"type": "object", "properties": props, "required": req + ["ko", "prompt"]}


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
    if not isinstance(result, dict):
        return ["Return a JSON object with ko and prompt."]
    problems: list[str] = []
    demands = checked_demands(said)
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
    prompt = str(result.get("prompt") or "").strip()
    if not prompt:
        return problems + ['"prompt" is empty.']
    if HANGUL_RE.search(prompt):
        problems.append('"prompt" must be English only.')
    lo, hi = profile["words"]
    n = len(prompt.split())
    if not lo <= n <= hi:
        problems.append(f"The prompt has {n} words; write {lo} to {hi} words.")
    full = f"{scene} {prompt}" if profile["mode"] == "t2v" else prompt
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
        problems.append(f"Use one main camera move for the whole clip; the prompt combines: {', '.join(moves)}.")
    if scene:
        same = restated(prompt, scene)
        if same:
            problems.append(f'Do not restate the look already fixed by the first frame or scene ("{same}"); describe motion only.')
    return problems


def describe(result: dict, cat: str) -> dict:
    """패널에 보여 줄 고른 항목 (한국어 이름). 검사를 통과한 초안에만 쓴다."""
    choices = result.get("choices") or {}
    picked = []
    for st in steps_of(cat):
        eid = choices.get(st["key"])
        e = crew_lib.entry(eid) if isinstance(eid, str) else None
        picked.append({"step": st["title"], "id": eid, "ko": e["ko"] if e else ""})
    return {"picked": picked}


def compose(mode: str, scene: str, motion: str, style: str = "") -> str:
    """최종 프롬프트. I2V 는 움직임만(외형과 그림체는 첫 화면이 정한다), T2V 는 장면 + 움직임 + 그림체 고정 문장."""
    motion = motion.strip().rstrip(".")
    if mode != "t2v":
        return f"{motion}." if motion else ""
    body = ". ".join(p for p in (scene.strip().rstrip("."), motion) if p)
    return f"{body}. {style.strip()}" if style.strip() else f"{body}."


async def draft(call, text: str, notes: str, profile: dict, seconds: float, frame: dict | None = None,
                scene: str = "", cat: str | None = None, menus: dict | None = None, repair_rounds: int | None = None,
                rng=None):
    """한 GPU 차례(llm_turn) 안에서 움직임 초안을 받는다. (결과, 회차, 남은 위반) 을 돌려준다."""
    rounds_max = settings.video_repair_rounds if repair_rounds is None else repair_rounds
    said = f"{text} {notes}".strip()
    messages = await asyncio.to_thread(build_messages, text, notes, profile, seconds, frame, scene, cat, menus, rng)
    schema = build_schema(cat, menus)
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
            if mode == "t2v" and not scene:
                # T2V 는 첫 화면이 없으므로 같은 차례 안에서 이미지 확장(제작진·도우미)으로 장면부터 쓴다
                scene_info = await promptx.expand_in_turn(call, text, style, notes, crew)
                scene = scene_info["body"]
                advice += [a for a in scene_info.get("advice") or [] if a not in advice]
            if cat:
                menus = await asyncio.to_thread(build_menus, said, cat, rng)
                result, rounds, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene,
                                                   cat, menus, rng=rng)
                crew_info = {"category": cat, "title": load_knowledge()["categories"][cat]["title"], "rounds": rounds,
                             "used": not left, "violations": left}
                if left:
                    advice.append("영상 제작진 초안이 규칙을 통과하지 못해 단계 메뉴 없이 다시 썼습니다.")
                else:
                    crew_info.update(describe(result, cat))
            if not cat or left:
                result, more, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene, rng=rng)
                rounds += more
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    motion = str(result.get("prompt") or "").strip()
    ko = result.get("ko") if isinstance(result.get("ko"), dict) else {}
    return {"prompt": compose(mode, scene, motion, style), "motion": motion,
            "scene": scene if mode == "t2v" else "", "profile": pid, "profile_title": profile["title"], "mode": mode,
            "settings": suggested, "frame": frame, "ko": {k: str(ko.get(k) or "") for k in KO_KEYS},
            "crew": crew_info, "scene_crew": (scene_info or {}).get("crew"),
            "scene_violations": (scene_info or {}).get("violations") or [],
            "violations": left, "rounds": rounds, "model": model, "frame_model": frame_model if frame else None,
            "advice": advice}


async def _safe_read(call, b64: str, advice: list) -> dict | None:
    """첫 화면 읽기가 실패해도 글만 보고 계속 쓴다."""
    try:
        return await read_frame(call, b64)
    except (PromptxError, RuntimeError):   # 시각 모델 오류(Ollama 실패 포함)도 같은 처리
        advice.append("첫 화면을 읽지 못해 요청 글만 보고 움직임을 썼습니다.")
        return None
