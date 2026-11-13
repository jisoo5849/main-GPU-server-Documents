"""이미지 편집 지시문 쓰기 (docs/chat-studio-design.md 5.5). 한국어 요청을 영문 편집 지시문 한두 문장으로 바꾼다.

편집용 규칙은 아직 충분히 검증하지 않았다 [TBD]. 기존 정규식(영문만·부정어 없음·글자 말 없음)을 그대로 쓰고,
걸리면 한 번 고쳐 쓴다. 그래도 걸리면 생성은 그대로 하고 위반만 기록한다. 순수 함수와 차례 안 함수만 둔다.
"""
import json
import re

KO_TEXT_RE = re.compile(r"간판|글자|글씨|문구|문자|텍스트|로고|표지판|라벨|이름표|자막")   # 사용자가 글자·간판을 고치라고 한 경우
MIN_WORDS, MAX_WORDS = 8, 60

SYSTEM = (
    "You write one image-editing instruction for an image editing model. The user wrote the request in Korean. "
    "Reply with JSON only: {\"prompt\": \"...\"}.\n"
    "Rules:\n"
    "1. English only, one or two sentences, 8 to 60 words.\n"
    "2. Say what to change and what to keep unchanged, for example: \"Replace the background with a warm sunset sky and keep "
    "the person, pose and lighting on the person unchanged.\"\n"
    "3. Use only the change the user asked for. Do not add new objects, styles or ideas.\n"
    "4. Do not use negative words (no, not, without, don't). Say what to keep instead.\n"
    "5. Do not mention text, letters, logos or signs unless the user asked for them.")


def build_messages(text: str, frame: dict | None = None) -> list[dict]:
    user = f"User request (Korean): {text}"
    if isinstance(frame, dict) and (frame.get("subject") or frame.get("environment")):
        user += "\nWhat the picture shows: " + json.dumps(
            {k: frame.get(k) for k in ("subject", "environment") if frame.get(k)}, ensure_ascii=False)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def check(prompt: str, user_text: str = "") -> list[str]:
    """위반 목록(영문). 비어 있으면 통과"""
    from .. import crew, promptx, video
    problems = []
    p = (prompt or "").strip()
    if not p:
        return ["The instruction is empty."]
    if video.HANGUL_RE.search(p):
        problems.append("The instruction must be English only. Remove Korean characters.")
    if promptx.NEGATION_RE.search(p):
        problems.append("Do not use negative words. Say what to keep instead.")
    if crew.TEXT_NOTE_RE.search(p) and not KO_TEXT_RE.search(user_text or "") and not crew.TEXT_NOTE_RE.search(user_text or ""):
        problems.append("Do not mention text, letters, logos or signs.")
    n = len(p.split())
    if n < MIN_WORDS or n > MAX_WORDS:
        problems.append(f"The instruction has {n} words; write {MIN_WORDS} to {MAX_WORDS} words.")
    return problems


async def write_edit_in_turn(call, text: str, frame: dict | None = None, repair_rounds: int = 1) -> dict:
    """이미 받은 GPU 차례(call) 안에서 편집 지시문을 쓴다. {"prompt", "violations", "rounds"}"""
    from .. import promptx
    messages = build_messages(text, frame)
    prompt, problems, rounds = "", [], 0
    for rounds in range(1, repair_rounds + 2):
        data = await call({"messages": messages, "format": "json", "think": False, "options": {"temperature": 0.3}})
        content = (data.get("message") or {}).get("content", "")
        prompt = str(promptx.parse_json_reply(content).get("prompt") or "").strip()
        problems = check(prompt, text)
        if not problems:
            break
        messages = messages + [{"role": "assistant", "content": content},
                               {"role": "user", "content": "Fix these problems and reply with JSON only: " + " ".join(problems)}]
    return {"prompt": prompt, "violations": problems, "rounds": rounds}
