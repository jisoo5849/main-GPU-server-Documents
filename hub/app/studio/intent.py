"""의도 판단 중 P1 에 쓰는 부분 (docs/chat-studio-design.md 5.3): 빈 글 처리(R0), 글에 든 길이·비율·소리 지정 뽑기.

P1 은 첨부가 있으면 I2V 다(R1). 나머지 규칙(R2~R10)은 P2 이후다.
"""
import re

# 제작진 분야 키워드에 걸리지 않는 문장이다 ('자연스럽게' 는 풍경 키워드 '자연' 에 걸린다, crew_knowledge.json)
DEFAULT_I2V_TEXT = "이 사진 속 장면이 부드럽게 움직이는 짧은 영상"
MAX_TEXT = 2000
MAX_SECONDS = 5.0
MIN_SECONDS = 0.5

SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*초")
ASPECT_RES = (
    ("portrait", re.compile(r"세로|9\s*:\s*16", re.I)),
    ("landscape", re.compile(r"가로|16\s*:\s*9", re.I)),
    ("square", re.compile(r"정사각|1\s*:\s*1")),
)
SOUND_RE = re.compile(r"소리|음악|효과음|사운드|BGM", re.I)


class InputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_text(text) -> str:
    """앞뒤 공백을 지운다. 공백·줄바꿈만 있으면 빈 글이다 (5.3 R0)."""
    t = str(text or "").strip()
    if len(t) > MAX_TEXT:
        raise InputError("too_long", f"글은 {MAX_TEXT}자까지 쓸 수 있습니다.")
    return t


def resolve_text(text, n_attachments: int) -> tuple[str, bool]:
    """(요청 글, 기본 요청문을 썼는지). 글도 사진도 없으면 InputError(empty_message)."""
    t = normalize_text(text)
    if t:
        return t, False
    if n_attachments > 0:
        return DEFAULT_I2V_TEXT, True
    raise InputError("empty_message", "글이나 사진을 보내 주세요.")


def extract_spec(text: str) -> dict:
    """{seconds: float|None, aspect: str|None, sound: bool, notes: [한국어 안내]}.
    5초를 넘는 길이는 5초로 맞추고, 소리·비율은 말없이 무시하지 않고 안내를 남긴다. 안내는 결과 뒤에 한 줄로 보인다."""
    notes = []
    seconds = None
    m = SECONDS_RE.search(text or "")
    if m:
        v = float(m.group(1))
        if v > MAX_SECONDS:
            seconds = MAX_SECONDS
            notes.append("한 번에 5초까지라 5초로 만들었습니다")
        elif v >= MIN_SECONDS:
            seconds = v
    aspect = next((name for name, rx in ASPECT_RES if rx.search(text or "")), None)
    sound = bool(SOUND_RE.search(text or ""))
    if sound:
        notes.append("소리는 아직 넣지 못합니다")
    return {"seconds": seconds, "aspect": aspect, "sound": sound, "notes": notes}


def i2v_notes(spec: dict) -> list[str]:
    """i2v 는 사진 비율을 따르므로 비율 지정이 있으면 알린다"""
    notes = list(spec.get("notes") or [])
    if spec.get("aspect"):
        notes.append("영상 비율은 사진 비율을 따릅니다")
    return notes
