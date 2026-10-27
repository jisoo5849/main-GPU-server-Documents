"""프롬프트 자동 확장(메타 규칙)과 결과 이미지 자동 검사.

1) 확장: 짧은 요청을 메인 Ollama 의 LLM 이 생성용 영문 프롬프트로 늘린다. 시스템 지시(META_RULES)로 규칙을 주고,
   돌아온 결과를 코드가 다시 검사해(check_expansion) 어긋나면 고쳐 쓰게 한다. 그림체 문장은 LLM 이 손대지 않게
   코드가 끝에 그대로 붙인다.
2) 검사: 결과 이미지를 시각 모델(VLM)에 보여 팔·다리·머리 같은 해부학 오류와, 꼭 있어야 할 특징(예: 소매 속에
   숨긴 손)이 보이는지 묻는다.

둘 다 GPU 를 쓰므로 llm.llm_turn 으로 다른 작업과 같은 대기열을 거친다. 아래 순수 함수들은 hub/tests 에서 검증한다.
"""
import base64
import json
import os
import re

from .config import settings

# ── 메타 규칙 (확장) ─────────────────────────────────────

REQUIRED_CATEGORIES = ("lighting", "camera", "environment", "texture")

META_RULES = """You rewrite a short image request into one detailed prompt for an image generation model.
Follow every rule:
1. Subject lock: keep every subject, object, action, count, color and name the user gave. Do not replace, drop or contradict them, and do not add main characters or story events the user did not ask for.
2. Visual details: always describe the lighting, the camera (shot size, angle, lens or framing), the environment (place and background) and the textures (materials and surfaces). Add composition when it helps.
3. Positive phrasing only: describe what should be seen. Never write negations such as "no", "not", "without", "avoid", "never" or "-free". Instead of "no text", write "a clean plain background".
4. Clear bodies: state how many people or animals are in the picture. For each one, describe the pose and what the arms, hands and legs are doing so the body reads clearly. If there are two or more, place each one (left, right, foreground, background) and give each a clearly different hair color, clothing color or size so they do not merge into one figure.
5. Style lock: if a style is given, it will be appended after your prompt by the system. Do not repeat it and do not add any other style or quality words such as photorealistic, 8K, Unreal Engine, octane render or 3D render. If no style is given, choose one style that fits the request and state it once.
6. Words image models misread: write "figure" or "outline" instead of "silhouette" and "bending down" instead of "ducking". Do not name existing films, artists, studios or characters.
7. Write in English, 60 to 160 words, as one paragraph of comma-separated phrases.
Return only JSON in this shape:
{"prompt": "...", "keep": ["the English words for each subject, object, action and detail the user gave, copied exactly as they appear in your prompt"], "categories": {"lighting": "...", "camera": "...", "environment": "...", "texture": "..."}, "people": 0}
Each categories value must be a phrase copied exactly from your prompt. "people" is the number of people or characters in the picture."""

NEGATION_RE = re.compile(r"\b(?:no|not|without|never|avoid|avoiding|don't|do not|nothing|none)\b|\w+-free\b", re.I)
# 그림체를 고정했을 때 섞이면 그림체를 깨는 말
STYLE_CONFLICTS = ("photorealistic", "photo-realistic", "hyperrealistic", "hyper-realistic", "8k", "4k",
                   "unreal engine", "octane", "3d render", "cgi", "ray tracing", "ray-traced", "dslr")
# 이번 컨셉아트 작업에서 엉뚱한 그림을 부른 말 (silhouette → 배경의 사람 모양 그림자, ducking → 오리)
MISREAD_WORDS = {"silhouette": "figure / outline", "ducking": "bending down"}


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


def build_expand_messages(text: str, style: str = "", notes: str = "") -> list[dict]:
    parts = [f"Request: {text}"]
    parts.append(f"Style (appended after your prompt by the system, do not repeat it): {style}" if style
                 else "Style: none given, choose one that fits the request.")
    if notes:
        parts.append(f"Must-keep details (treat them like the request itself): {notes}")
    return [{"role": "system", "content": META_RULES}, {"role": "user", "content": "\n".join(parts)}]


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


def check_expansion(result: dict, style: str = "", user_text: str = "") -> list[str]:
    """메타 규칙 검사. 어긋난 점을 영문 문장 목록으로 돌려준다 (그대로 고쳐 쓰기 지시에 넣는다)."""
    problems: list[str] = []
    prompt = str(result.get("prompt") or "").strip()
    if not prompt:
        return ['"prompt" is empty.']
    low = prompt.lower()
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
            if t in low and t not in style.lower():
                problems.append(f'"{t}" conflicts with the locked style; remove it.')
    for w, alt in MISREAD_WORDS.items():
        if re.search(rf"\b{w}", low) and w not in user_text.lower():
            problems.append(f'Avoid "{w}" (image models misread it); write "{alt}" instead.')
    return problems


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


def clean_expect(raw) -> list[str]:
    if isinstance(raw, str):
        raw = raw.splitlines()
    out = [str(x).strip()[:200] for x in (raw or []) if str(x).strip()]
    return out[:8]


def result_image_path(username: str, filename: str, subfolder: str = "") -> str:
    """결과 폴더 안의 이미지 경로 (폴더 밖으로 나가는 경로는 거부)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise PromptxError("파일 이름이 올바르지 않습니다.")
    if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise PromptxError("이미지 파일만 검사할 수 있습니다.")
    base = os.path.realpath(os.path.join(settings.results_dir, username))
    path = os.path.realpath(os.path.join(base, subfolder or "", filename))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise PromptxError("결과 폴더에서 파일을 찾지 못했습니다.", 404)
    return path


# ── GPU 를 쓰는 부분 (대기열 경유) ──────────────────────────

async def expand(user, ip: str, text: str, style: str = "", notes: str = "") -> dict:
    from . import llm   # llm → scheduler 를 불러오므로 순환을 피해 여기서

    text = clean_input(text)
    style = str(style or "").strip()[:2000]
    notes = str(notes or "").strip()[:2000]
    messages = build_expand_messages(text, style, notes)
    model = settings.expand_model
    rounds, result, problems = 0, {}, []
    try:
        async with llm.llm_turn(user, ip, model, "prompt_expand") as call:
            for rounds in range(1, settings.expand_repair_rounds + 2):
                data = await call({"messages": messages, "format": "json", "options": {"temperature": 0.4}})
                content = (data.get("message") or {}).get("content", "")
                result = parse_json_reply(content)
                problems = check_expansion(result, style, text + " " + notes)
                if not problems:
                    break
                messages = messages + [{"role": "assistant", "content": content}, repair_message(problems)]
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    return {"prompt": final_prompt(result, style), "body": str(result.get("prompt") or ""),
            "keep": result.get("keep") or [], "categories": result.get("categories") or {},
            "people": result.get("people"), "violations": problems, "rounds": rounds, "model": model}


async def inspect(user, ip: str, username: str, filename: str, subfolder: str = "", expect=None) -> dict:
    from . import llm

    path = result_image_path(username, filename, subfolder)
    expect = clean_expect(expect)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    model = settings.inspect_model
    try:
        async with llm.llm_turn(user, ip, model, "image_inspect") as call:
            data = await call({"messages": build_inspect_messages(b64, expect), "format": "json",
                               "options": {"temperature": 0}})
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    parsed = parse_inspection(parse_json_reply((data.get("message") or {}).get("content", "")), expect)
    return {**parsed, "file": f"{subfolder}/{filename}".lstrip("/"), "model": model}
