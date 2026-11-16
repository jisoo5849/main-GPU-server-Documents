"""프롬프트 자동 확장(메타 규칙)과 결과 이미지 자동 검사.

1) 확장: 짧은 요청을 메인 Ollama 의 LLM 이 생성용 영문 프롬프트로 늘린다. 시스템 지시(META_RULES)로 규칙을 주고,
   돌아온 결과를 코드가 다시 검사해(check_expansion) 어긋나면 고쳐 쓰게 한다. 그림체 문장은 LLM 이 손대지 않게
   코드가 끝에 그대로 붙인다.
2) 검사: 결과 이미지를 시각 모델(VLM)에 보여 팔·다리·머리 같은 해부학 오류와, 꼭 있어야 할 특징(예: 소매 속에
   숨긴 손)이 보이는지 묻는다. 시각 모델은 여러 사람이 있는 그림에서 개수를 잘 틀리므로, 사람마다 상자를 받아
   잘라 낸 뒤 손·팔·다리를 하나씩 나열하게 하고 개수는 코드가 센다.

둘 다 GPU 를 쓰므로 llm.llm_turn 으로 다른 작업과 같은 대기열을 거친다. 아래 순수 함수들은 hub/tests 에서 검증한다.
"""
import asyncio
import base64
import io
import json
import os
import re

from . import crew_lib
from .config import settings

# ── 메타 규칙 (확장) ─────────────────────────────────────

REQUIRED_CATEGORIES = ("lighting", "camera", "environment", "texture")

META_RULES = """You rewrite a short image request into one detailed prompt for an image generation model.
Follow every rule:
1. Subject lock, the user's words come first: keep every subject, object, action, count, color and name the user gave, and every genre, style, era, mood, weather, season and place the user named. These words decide the look; never replace, drop or soften them for your own default taste, and do not add main characters or story events the user did not ask for.
2. Visual details: always describe the lighting, the camera (shot size, angle, lens or framing), the environment (place and background) and the textures (materials and surfaces). Add composition when it helps.
3. Positive phrasing only: describe what should be seen. Never write negations such as "no", "not", "without", "avoid", "never" or "-free". Instead of "no text", write "a clean plain background".
4. Clear bodies: state how many people or animals are in the picture. For each one, describe the pose and what the arms, hands and legs are doing so the body reads clearly. If there are two or more, place each one (left, right, foreground, background) and give each a clearly different hair color, clothing color or size so they do not merge into one figure.
5. Style lock: if a style is given, it will be appended after your prompt by the system. Do not repeat it and do not add any other style or quality words such as photorealistic, 8K, Unreal Engine, octane render or 3D render. If no style is given, choose one style that fits the request and state it once.
6. Words image models misread: write "figure" or "outline" instead of "silhouette" and "bending down" instead of "ducking". Do not name existing films, franchises, artists, studios, characters, creatures, brands or real people: when the request names one, describe what it looks like in plain visible terms (body shape, number and form of limbs, surface material, colours, scale, texture, setting), as if briefing an artist who has never seen it; for a real person write a generic figure with only the requested clothing, pose and setting, never a likeness.
7. Write in English, 60 to 160 words, as one paragraph of comma-separated phrases.
8. Hidden hands: if the request or the must-keep details say the hands stay hidden in the sleeves, image models draw hands whenever the text mentions them. Then never write the words hand, hands, finger, fingers, fingertips, palm, fist, holding, hold, grip, gripping, grasp or clutching anywhere, including the "keep" list (keep "long sleeves" instead). Describe the sleeves as "oversized sleeves hanging well past the wrists, the cuffs closed and drooping". Make every carried object rest on the body or the sleeve: "the umbrella handle tucked inside the drooping sleeve cuff and propped against the shoulder", "the book hugged against the chest by both sleeves".
9. Actions done for someone: if the action is done for or to another person (Korean "~해 주는", "~에게", "~와 함께", "둘이"; English "for her friend", "shares with", "gives to"), that person must appear too. Count them in "people", give them their own position (left, right, foreground, background) and a clearly different hair color and clothing color, and describe how the action connects the two, for example "the girl on the left raises the umbrella over the taller boy on the right".
10. Variety: choose the lighting, camera, palette and setting that best express the user's words. Do not fall back to the same safe look (a dark studio, luxury gold) unless the request asks for it; optional ideas may be offered, use them only when they fit the user's words.
Return only JSON in this shape:
{"prompt": "...", "keep": ["the English words for each subject, object, action and detail the user gave, copied exactly as they appear in your prompt"], "categories": {"lighting": "...", "camera": "...", "environment": "...", "texture": "..."}, "people": 0}
Each categories value must be a phrase copied exactly from your prompt. "people" is the number of people or characters in the picture."""

NEGATION_RE = re.compile(r"\b(?:no|not|without|never|avoid|avoiding|don't|do not|nothing|none)\b|\w+-free\b", re.I)
# 그림체를 고정했을 때 섞이면 그림체를 깨는 말
STYLE_CONFLICTS = ("photorealistic", "photo-realistic", "hyperrealistic", "hyper-realistic", "8k", "4k",
                   "unreal engine", "octane", "3d render", "cgi", "ray tracing", "ray-traced", "dslr")
# 이번 컨셉아트 작업에서 엉뚱한 그림을 부른 말 (silhouette → 배경의 사람 모양 그림자, ducking → 오리)
MISREAD_WORDS = {"silhouette": "figure / outline", "ducking": "bending down"}
# 손을 숨기라고 한 요청: 글에 손·쥐는 동작이 나오면 Z-Image 가 손을 그렸다 (2026-10-27 우산 소녀 시험 2장 모두)
HIDDEN_HANDS_RE = re.compile(r"손[^.\n]{0,20}(소매|숨|감춰|가려|안\s*보|보이지)|소매[^.\n]{0,20}손"
                             r"|hands?\b[^.\n]{0,30}\b(hidden|tucked|inside|covered|concealed)[^.\n]{0,20}sleeve"
                             r"|sleeves?\b[^.\n]{0,20}\b(cover|hide|hiding|covering)[^.\n]{0,10}hands?", re.I)
HAND_WORDS_RE = re.compile(r"\b(?:hands?(?!-(?:drawn|painted|made|crafted))|fingers?|fingertips?|palms?|fists?|holding|holds?|grip(?:s|ping)?"
                           r"|grasp(?:s|ing)?|clutch(?:es|ing)?)\b", re.I)
# 다른 사람에게 해 주는 동작 (받는 사람이 그림에서 빠졌다: "우산을 씌워 주는 소녀" → 소녀 혼자)
FOR_SOMEONE_RE = re.compile(r"[가-힣](?:아|어|여|해|워|와|줘)\s?주(?:는|고|며|다|기|던|었)|에게|한테|와\s?함께|과\s?함께"
                            r"|둘이|두\s?(?:사람|명)|\b(?:for|with|to)\s+(?:her|his|their|a|an|the)\s+"
                            r"(?:friend|brother|sister|mother|father|mom|dad|boy|girl|man|woman|child|partner)\b"
                            r"|\btwo\s+(?:people|girls|boys|friends|children|kids)\b", re.I)
POSITION_RE = re.compile(r"\b(?:left|right|foreground|background|behind|beside|next to|in front of)\b", re.I)


class PromptxError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean_input(text, limit: int = 2000) -> str:
    text = str(text or "").strip()
    if not text:
        raise PromptxError("내용이 비어 있습니다.")
    if len(text) > limit:
        raise PromptxError(f"{limit}자 이내로 적어 주세요.")
    return text


# 장소는 장면 자체를 바꿀 수 있어 아이디어에서 뺀다 (장소는 사용자가 정하거나 주제 유지 규칙이 지킨다)
IDEA_FACETS = ("lighting", "camera", "color_palette", "atmosphere")


def demand_lines(text: str) -> list[str]:
    """요청에 사용자가 직접 적은 장르·날씨·색·조명·분위기 (사용자 의도 사전 crew_intent.json). 최우선이다."""
    return [f"- The user wrote '{d['tag']}' ({d['facet']}): show it clearly with words such as {', '.join(d['terms'][:6])}."
            for d in crew_lib.query(text).demands]


def idea_lines(text: str, rng=None, per_facet: int = 2) -> list[str]:
    """라이브러리에서 요청과 맞거나 다양하게 뽑은 선택 아이디어 (같은 모습만 반복되지 않게)."""
    out = []
    for fa in IDEA_FACETS:
        prefer = crew_lib.idea_prefer().get(fa) or None
        ents = crew_lib.retrieve(text, fa, k=per_facet, rng=rng, prefer=prefer, wild=0.0)
        if ents:
            out.append(f"- {fa}: " + " | ".join(e["en"] for e in ents))
    return out


def build_expand_messages(text: str, style: str = "", notes: str = "", ideas: bool = True, rng=None,
                          said: str | None = None) -> list[dict]:
    """said 는 사용자가 쓴 말 전부(요청 + 특징 칸)다. notes 에는 제작진 영문 메모가 더해질 수 있어 따로 받는다."""
    said = said if said is not None else f"{text} {notes}".strip()
    parts = [f"Request: {text}"]
    parts.append(f"Style (appended after your prompt by the system, do not repeat it): {style}" if style
                 else "Style: none given, choose one that fits the request.")
    dl = demand_lines(said)
    if dl:
        parts.append("USER WORDS (top priority, they decide the look):\n" + "\n".join(dl))
    if notes:
        parts.append(f"Must-keep details (treat them like the request itself): {notes}")
    if ideas:
        il = idea_lines(said, rng)
        if il:
            parts.append("Optional ideas from our library (use a few only if they fit the user's words):\n" + "\n".join(il))
    return [{"role": "system", "content": META_RULES}, {"role": "user", "content": "\n".join(parts)}]


# 사용자가 직접 원하면 그림체 충돌 말도 허용한다 (사용자 의도 최우선)
STYLE_ALLOW_KO = {"photorealistic": ("실사", "사진처럼", "포토리얼", "사진 같은"), "photo-realistic": ("실사", "사진처럼"),
                  "hyperrealistic": ("극사실", "하이퍼리얼"), "hyper-realistic": ("극사실",), "8k": ("8k", "8K"),
                  "4k": ("4k", "4K"), "3d render": ("3d", "3D", "쓰리디", "렌더"), "cgi": ("cg", "CG"),
                  "dslr": ("dslr", "DSLR")}


def user_allows(term: str, user_text: str) -> bool:
    low = (user_text or "").lower()
    return term in low or any(k.lower() in low for k in STYLE_ALLOW_KO.get(term, ()))


def parse_json_reply(content: str) -> dict:
    """모델 답에서 JSON 객체를 꺼낸다 (생각 블록·코드 울타리·앞뒤 말이 붙어도)."""
    s = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip())
    try:
        data = json.loads(s)
    except ValueError:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise PromptxError("모델 답에서 JSON 을 찾지 못했습니다.", 502) from None
        try:
            data = json.loads(s[start:end + 1])
        except ValueError:
            raise PromptxError("모델 답의 JSON 이 깨져 있습니다.", 502) from None
    if not isinstance(data, dict):
        raise PromptxError("모델 답이 JSON 객체가 아닙니다.", 502)
    return data


def check_expansion(result: dict, style: str = "", user_text: str = "", request_text: str | None = None) -> list[str]:
    """메타 규칙 검사. 어긋난 점을 영문 문장 목록으로 돌려준다 (그대로 고쳐 쓰기 지시에 넣는다).

    user_text 는 요청+특징, request_text 는 사용자 요구(장르·날씨·색 등)를 뽑을 요청 글이다(없으면 user_text).
    """
    problems: list[str] = []
    prompt = str(result.get("prompt") or "").strip()
    if not prompt:
        return ['"prompt" is empty.']
    low = prompt.lower()
    # 사용자 의도 최우선: 요청에 적힌 장르·날씨·색·조명·분위기가 최종 글에 보여야 한다
    for d in crew_lib.query(request_text if request_text is not None else user_text).demands:
        if not crew_lib.has_term(prompt, d["terms"]):
            problems.append(f"The user asked for '{d['tag']}'; show it clearly in the prompt with words such as: "
                            + ", ".join(d["terms"][:6]) + ".")
    words = len(prompt.split())
    if words < 40 or words > 220:
        problems.append(f"The prompt has {words} words; write 60 to 160 words.")
    keep = [str(k).strip() for k in (result.get("keep") or []) if str(k).strip()]
    if not keep:
        problems.append('"keep" is empty; list the English words for every subject, object and action the user gave.')
    for k in keep:
        if k.lower() not in low:
            problems.append(f'Kept term "{k}" does not appear in the prompt; keep the user\'s subject exactly.')
    cats = result.get("categories") if isinstance(result.get("categories"), dict) else {}
    for c in REQUIRED_CATEGORIES:
        val = str(cats.get(c) or "").strip()
        if not val:
            problems.append(f'Category "{c}" is missing; describe the {c} in the prompt.')
        elif val.lower() not in low:
            problems.append(f'The {c} phrase "{val}" is not copied exactly from the prompt.')
    seen = set()
    for m in NEGATION_RE.finditer(prompt):
        w = m.group(0).lower()
        if w not in seen:
            seen.add(w)
            problems.append(f'Negative wording "{m.group(0)}" found; describe what should be seen instead.')
    if style:
        for t in STYLE_CONFLICTS:
            if t in low and t not in style.lower() and not user_allows(t, user_text):
                problems.append(f'"{t}" conflicts with the locked style; remove it.')
    for w, alt in MISREAD_WORDS.items():
        if re.search(rf"\b{w}", low) and w not in user_text.lower():
            problems.append(f'Avoid "{w}" (image models misread it); write "{alt}" instead.')
    if hides_hands(user_text):
        found = sorted({m.group(0).lower() for m in HAND_WORDS_RE.finditer(prompt + " " + " ".join(keep))})
        if found:
            problems.append("The hands must stay hidden, so remove these words (also from keep): "
                            + ", ".join(f'"{w}"' for w in found)
                            + '. Rest carried objects on the body or tuck them into the drooping sleeve cuffs.')
    if for_someone(user_text):
        try:
            people = int(result.get("people") or 0)
        except (TypeError, ValueError):
            people = 0
        if people < 2:
            problems.append("The action is done for another person, so that person must be in the picture too; "
                            'set "people" to at least 2 and describe them.')
        elif len({m.group(0).lower() for m in POSITION_RE.finditer(prompt)}) < 1:
            problems.append("Place each person (left, right, foreground or background) so they read as separate figures.")
    return problems


def hides_hands(user_text: str) -> bool:
    return bool(HIDDEN_HANDS_RE.search(user_text or ""))


def for_someone(user_text: str) -> bool:
    return bool(FOR_SOMEONE_RE.search(user_text or ""))


def repair_message(problems: list[str]) -> dict:
    return {"role": "user", "content": "Your JSON broke these rules:\n- " + "\n- ".join(problems)
            + "\nRewrite it and return the full JSON again."}


def final_prompt(result: dict, style: str = "") -> str:
    p = str(result.get("prompt") or "").strip().rstrip(".")
    return f"{p}. {style.strip()}" if style.strip() else f"{p}."


# ── 결과 자동 검사 ───────────────────────────────────────

INSPECT_RULES = """You are a strict quality inspector for illustrations and character art.
Look at the image carefully and report only what you actually see.
Check anatomy for every person or creature: extra or missing arms, legs, hands or fingers; a missing or extra head; a head that is not connected to the body (missing neck); two bodies merged into one; floating limbs; a face that is melted or duplicated.
Also check each expected trait the user lists and say whether it is clearly visible.
Return only JSON in this shape:
{"people": 0, "anatomy_ok": true, "issues": [{"type": "extra_limb | missing_limb | extra_fingers | missing_head | extra_head | detached_head | merged_bodies | floating_limb | face | text_artifact | other", "where": "short location in the image", "detail": "short description"}], "traits": [{"trait": "...", "present": true, "note": "..."}]}
Use an empty issues list when everything is fine. Do not invent problems."""


def build_inspect_messages(image_b64: str, expect: list[str] | None = None) -> list[dict]:
    expect = [e for e in (expect or []) if e]
    text = "Inspect this image."
    if expect:
        text += " Expected traits:\n" + "\n".join(f"- {e}" for e in expect)
    else:
        text += " No expected traits; return an empty traits list."
    return [{"role": "system", "content": INSPECT_RULES},
            {"role": "user", "content": text, "images": [image_b64]}]


def parse_inspection(data: dict, expect: list[str] | None = None) -> dict:
    issues = []
    for it in data.get("issues") or []:
        if isinstance(it, dict):
            issues.append({"type": str(it.get("type") or "other"), "where": str(it.get("where") or ""),
                           "detail": str(it.get("detail") or "")})
        elif str(it).strip():
            issues.append({"type": "other", "where": "", "detail": str(it)})
    traits = []
    by_name = {str(t.get("trait") or "").strip().lower(): t for t in data.get("traits") or [] if isinstance(t, dict)}
    for e in expect or []:
        t = by_name.get(e.strip().lower())
        if t is None and len(by_name) == len(expect or []):
            # 모델이 특징 문장을 바꿔 적은 경우 순서대로 맞춘다
            t = list(by_name.values())[(expect or []).index(e)]
        present = bool(t.get("present")) if t else False
        traits.append({"trait": e, "present": present, "note": str((t or {}).get("note") or "")})
    anatomy_ok = bool(data.get("anatomy_ok", not issues)) and not issues
    try:
        people = int(data.get("people") or 0)
    except (TypeError, ValueError):
        people = 0
    return {"ok": anatomy_ok and all(t["present"] for t in traits), "anatomy_ok": anatomy_ok,
            "people": people, "issues": issues, "traits": traits}


LOCATE_RULES = """Find every person or character figure in the image, including each separate view on a turnaround or character sheet.
Return only JSON: {"people": [{"label": "short description, e.g. front view girl in green coat", "box": [x1, y1, x2, y2]}]}
Box coordinates are on a 0-1000 scale relative to the image width and height. One box per figure, covering the whole body from head to feet."""

BODY_RULES = """You are checking one figure cropped from an illustration. Other figures may be partly visible at the edges; ignore them.
List what you actually see on the main figure, one entry per body part. Do not count, list each one separately.
Return only JSON:
{"heads": [{"where": "..."}], "head_connected_to_body": true,
 "arms": [{"side": "left | right | other", "where": "...", "doing": "..."}],
 "hands": [{"where": "...", "doing": "...", "attached_to": "left arm | right arm | nothing"}],
 "legs": [{"where": "..."}],
 "stray_part": false, "stray_detail": ""}
A hand hidden inside a sleeve is not listed. Set stray_part true if any hand, arm or leg appears that is not attached to the figure's own shoulders or hips."""

MAX_FIGURES = 8


def build_locate_messages(image_b64: str) -> list[dict]:
    return [{"role": "system", "content": LOCATE_RULES},
            {"role": "user", "content": "Find every figure in this image.", "images": [image_b64]}]


def build_body_messages(crop_b64: str, label: str = "") -> list[dict]:
    text = "Check the main figure in this crop" + (f" ({label})." if label else ".")
    return [{"role": "system", "content": BODY_RULES},
            {"role": "user", "content": text, "images": [crop_b64]}]


def parse_boxes(data: dict, width: int, height: int, pad: float = 0.06) -> list[dict]:
    """0~1000 상자를 픽셀 상자로 바꾼다. 여백을 두고, 너무 작거나 거의 겹치는 상자는 버린다."""
    out = []
    for p in (data.get("people") or [])[:MAX_FIGURES * 2]:
        box = p.get("box") if isinstance(p, dict) else None
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 20 or y2 - y1 < 20:
            continue
        px, py = (x2 - x1) * pad, (y2 - y1) * pad
        b = (max(0, int((x1 - px) * width / 1000)), max(0, int((y1 - py) * height / 1000)),
             min(width, int((x2 + px) * width / 1000)), min(height, int((y2 + py) * height / 1000)))
        if any(_overlap(b, o["box"]) > 0.85 for o in out):
            continue
        out.append({"label": str(p.get("label") or "")[:80], "box": b})
    return out[:MAX_FIGURES]


def _overlap(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
    return ix * iy / small


def body_issues(data: dict, where: str = "") -> list[dict]:
    """부위 나열 결과에서 개수를 세어 문제로 바꾼다 (모델에게 세게 하지 않는다)."""
    def n(key):
        v = data.get(key)
        return len(v) if isinstance(v, list) else 0

    issues = []
    for key, name in (("hands", "hands"), ("arms", "arms"), ("legs", "legs")):
        if n(key) > 2:
            issues.append({"type": "extra_limb", "where": where, "detail": f"{n(key)} {name} listed"})
    if n("heads") > 1:
        issues.append({"type": "extra_head", "where": where, "detail": f"{n('heads')} heads listed"})
    elif n("heads") == 0:
        issues.append({"type": "missing_head", "where": where, "detail": "no head listed"})
    elif data.get("head_connected_to_body") is False:
        issues.append({"type": "detached_head", "where": where, "detail": "head not connected to the body"})
    loose = [h for h in data.get("hands") or [] if isinstance(h, dict)
             and str(h.get("attached_to") or "").strip().lower() in ("nothing", "none")]
    if data.get("stray_part") is True or loose:
        detail = str(data.get("stray_detail") or "") or "; ".join(str(h.get("where") or "") for h in loose)
        issues.append({"type": "floating_limb", "where": where, "detail": detail or "stray body part"})
    return issues


def merge_figures(parsed: dict, figures: list[dict]) -> dict:
    """사람별 부위 검사 결과를 전체 검사 결과에 더한다."""
    extra = [i for f in figures for i in f["issues"]]
    anatomy_ok = parsed["anatomy_ok"] and not extra
    return {**parsed, "issues": parsed["issues"] + extra, "anatomy_ok": anatomy_ok,
            "ok": anatomy_ok and all(t["present"] for t in parsed["traits"]),
            "people": max(parsed["people"], len(figures)),
            "figures": [{"label": f["label"], "box": list(f["box"]), "hands": f["hands"], "arms": f["arms"],
                         "issues": len(f["issues"])} for f in figures]}


def image_size(image_bytes: bytes) -> tuple[int, int]:
    from PIL import Image   # 허브 이미지에만 있다 (단위 테스트는 없어도 돈다)

    return Image.open(io.BytesIO(image_bytes)).size


def crop_figures(image_bytes: bytes, boxes: list[dict], max_side: int = 1024) -> list[str]:
    """상자마다 잘라 base64 PNG 로. 작은 조각은 키워서 손이 보이게 한다."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    out = []
    for b in boxes:
        c = img.crop(b["box"])
        scale = max_side / max(c.size)
        if scale < 1 or max(c.size) < 512:
            c = c.resize((max(1, int(c.width * scale)), max(1, int(c.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        c.save(buf, "PNG")
        out.append(base64.b64encode(buf.getvalue()).decode())
    return out


def clean_expect(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.splitlines()
    out = [str(x).strip()[:200] for x in (raw or []) if str(x).strip()]
    return out[:8]


def image_in(base: str, filename: str, subfolder: str = "", where: str = "결과 폴더") -> str:
    """base 폴더 안의 이미지 경로 (폴더 밖으로 나가는 경로는 거부)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise PromptxError("파일 이름이 올바르지 않습니다.")
    if len(filename) > 255 or len(subfolder or "") > 1024 or "\0" in filename or "\0" in (subfolder or ""):
        raise PromptxError("파일 이름이 올바르지 않습니다.")   # 아주 긴 경로·NUL 문자는 경로 처리 전에 거부
    if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise PromptxError("이미지 파일만 읽을 수 있습니다.")
    base = os.path.realpath(base)
    path = os.path.realpath(os.path.join(base, subfolder or "", filename))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise PromptxError(f"{where}에서 파일을 찾지 못했습니다.", 404)
    return path


def result_image_path(username: str, filename: str, subfolder: str = "") -> str:
    """결과 폴더 안의 이미지 경로 (폴더 밖으로 나가는 경로는 거부)."""
    return image_in(os.path.join(settings.results_dir, username), filename, subfolder)


# ── GPU 를 쓰는 부분 (대기열 경유) ──────────────────────────

CREW_SKIP_ADVICE = {
    "character": "캐릭터 정본이 있는 요청이라 정본 특징을 지키려고 제작진 모드를 쓰지 않았습니다.",
    "hidden_hands": "손 숨김 요청이라 제작진 모드를 쓰지 않았습니다.",
    "person": "사람이 나오는 요청이라 제작진 모드를 쓰지 않았습니다.",
}


def crew_route(text: str, notes: str = "", crew=None) -> tuple[dict | None, list[str]]:
    """제작진 분야와 건너뛴 이유 안내. crew 는 None(설정을 따름) / True / False."""
    from . import crew as crew_mod

    want_crew = settings.crew_mode != "off" and crew is not False
    rt = crew_mod.route(text, notes) if want_crew else None
    advice = []
    if want_crew and rt is None:
        reason = crew_mod.skip_reason(text, notes)
        if reason:   # 분야가 없는 요청은 조용히 도우미로 가고, 건너뛴 이유가 분명할 때만 알린다
            advice.append(CREW_SKIP_ADVICE[reason])
    return rt, advice


async def expand_in_turn(call, text: str, style: str = "", notes: str = "", crew=None) -> dict:
    """이미 받은 GPU 차례(call) 안에서 확장한다. 영상 렌더러의 T2V 도 같은 차례 안에서 장면을 만들 때 쓴다."""
    from . import crew as crew_mod

    rt, crew_advice = crew_route(text, notes, crew)
    crew_info = None
    rounds, result, problems = 0, {}, []
    used_notes = notes
    if rt is not None:
        # 같은 차례 안에서 제작진 초안을 먼저 받는다 (모델을 다시 올리지 않는다)
        try:
            draft, crew_rounds, left = await crew_mod.draft(call, text, rt, style, notes)
        except Exception:  # noqa: BLE001 - 초안이 어떻게 깨져도 기존 도우미로 돌아간다
            draft, crew_rounds, left = {}, 0, ["crew draft failed"]
        crew_info = {"category": rt["category"], "title": rt["title"], "rounds": crew_rounds,
                     "used": not left, "violations": left}
        if not left:
            used_notes = crew_mod.merge_notes(notes, draft)
            crew_info.update(crew_mod.describe(draft, rt["category"]))
            crew_info["notes_en"] = [str(n) for n in draft.get("notes_en") or []]
        else:
            crew_advice.append("제작진 초안이 규칙을 통과하지 못해 기존 도우미로 처리했습니다.")
    # 제작진 초안을 쓴 경우에는 이미 메뉴에서 골랐으므로 추가 아이디어를 주지 않는다.
    # 아이디어 검색은 CPU 를 쓰므로 스레드에서 (허브는 포털·중계·ComfyUI 화면을 한 프로세스로 처리한다)
    said = f"{text} {notes}".strip()
    messages = await asyncio.to_thread(build_expand_messages, text, style, used_notes,
                                       not (crew_info and crew_info.get("used")), None, said)
    for rounds in range(1, settings.expand_repair_rounds + 2):
        # 생각 단계를 끄면 같은 결과를 훨씬 빨리 낸다 (메인 측정: 27b 확장 한 번 약 100초)
        data = await call({"messages": messages, "format": "json", "think": False,
                           "options": {"temperature": 0.4}})
        content = (data.get("message") or {}).get("content", "")
        result = parse_json_reply(content)
        problems = check_expansion(result, style, said, request_text=said)
        if not problems:
            break
        messages = messages + [{"role": "assistant", "content": content}, repair_message(problems)]
    return {"prompt": final_prompt(result, style), "body": str(result.get("prompt") or ""),
            "keep": result.get("keep") or [], "categories": result.get("categories") or {},
            "people": result.get("people"), "violations": problems, "rounds": rounds,
            "crew": crew_info, "advice": crew_advice + expand_advice(text + " " + notes)}


async def expand(user, ip: str, text: str, style: str = "", notes: str = "", crew=None) -> dict:
    """짧은 요청을 생성용 프롬프트로 늘린다. crew 는 제작진 모드(B2): None 이면 설정(CREW_MODE)을 따르고,
    True/False 면 그 요청에 한해 켜거나 끈다 (분야 메뉴가 없는 요청과 캐릭터 정본 요청은 켜도 건너뛴다)."""
    from . import llm   # llm → scheduler 를 불러오므로 순환을 피해 여기서

    text = clean_input(text)
    style = str(style or "").strip()[:2000]
    notes = str(notes or "").strip()[:2000]
    model = settings.expand_model
    try:
        async with llm.llm_turn(user, ip, model, "prompt_expand") as call:
            out = await expand_in_turn(call, text, style, notes, crew)
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    return {**out, "model": model}


def expand_advice(user_text: str) -> list[str]:
    """글만으로는 잘 안 지켜지는 요청에 붙이는 안내 (패널에 그대로 보여 준다)."""
    advice = []
    if hides_hands(user_text):
        advice.append("손 숨김은 프롬프트만으로는 잘 지켜지지 않습니다. 구도가 좋은 컷을 고른 뒤 "
                      "Qwen-Image-Edit 로 소매를 덮는 편집 단계를 계획하세요.")
    if for_someone(user_text):
        advice.append("두 사람 장면은 한 몸으로 붙기 쉽습니다. 결과 자동 검사의 인물 수를 확인하세요.")
    return advice


async def inspect(user, ip: str, username: str, filename: str, subfolder: str = "", expect=None) -> dict:
    from . import llm

    path = result_image_path(username, filename, subfolder)
    expect = clean_expect(expect)
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()
    model = settings.inspect_model

    async def ask(call, messages):
        data = await call({"messages": messages, "format": "json", "think": False, "options": {"temperature": 0}})
        return parse_json_reply((data.get("message") or {}).get("content", ""))

    figures = []
    try:
        # 한 차례 안에서 전체 검사 → 사람 찾기 → 사람마다 부위 나열 (모델을 다시 올리지 않는다)
        async with llm.llm_turn(user, ip, model, "image_inspect") as call:
            whole = await ask(call, build_inspect_messages(b64, expect))
            try:
                w, h = image_size(raw)
                boxes = parse_boxes(await ask(call, build_locate_messages(b64)), w, h)
                for box, crop in zip(boxes, crop_figures(raw, boxes)):
                    body = await ask(call, build_body_messages(crop, box["label"]))
                    figures.append({**box, "issues": body_issues(body, box["label"] or f"figure at {box['box']}"),
                                    "hands": len(body.get("hands") or []), "arms": len(body.get("arms") or [])})
            except PromptxError:
                figures = []    # 사람 찾기가 실패해도 전체 검사 결과는 돌려준다
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    parsed = merge_figures(parse_inspection(whole, expect), figures)
    return {**parsed, "file": f"{subfolder}/{filename}".lstrip("/"), "model": model}
