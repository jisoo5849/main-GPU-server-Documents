"""의도 판단 (docs/chat-studio-design.md 5.3): 빈 글 처리(R0), 규칙 R2~R10, 글에 든 길이·비율·소리 지정 뽑기.

규칙으로만 정한다(LLM 은 P4). 순수 함수만 둔다. 어느 레시피가 있는지는 모르므로 정한 action 의 레시피가
없으면 부르는 쪽(api)이 "이 기능은 지금 준비 중입니다"로 끝낸다.
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


# ── 규칙 R2~R10 (5.3) ──
# '애니메이션 스타일', '움직이는 느낌', '클립보드' 같은 이미지 요청이 영상 말로 잡히지 않게 뒤따르는 말을 본다
VIDEO_RE = re.compile(r"동영상|영상|비디오|움직이게|움직이는(?!\s*(?:느낌|듯))|클립(?!보드)"
                      r"|애니메이션(?!\s*(?:스타일|풍|체|느낌|그림))")
STRONG_EDIT_RE = re.compile(r"바꿔|바꾸|지워|지우|없애|빼줘|빼고|고쳐|편집|배경만|색만")
WEAK_EDIT_RE = re.compile(r"추가해|넣어")      # "배경에 벚꽃을 넣어서 그려줘" 는 새 그림 요청이다
DRAW_RE = re.compile(r"그려|그리|만들어|생성")
NEWPIC_RE = re.compile(r"그려|그리|새로")      # 영상 뒤에 이 말이 있으면 고치기가 아니라 새 요청이다 ("더 큰 파도가 치는 바다 그림 그려줘")
MAKE_RE = re.compile(r"만들|그려|그리|바꿔|바꾸|생성")
GREET_RE = re.compile(r"안녕|고마워|고맙|감사|도움말|뭐\s*할\s*수\s*있")
ASK_RE = re.compile(r"[?？]|왜|어떻게|뭐야|보여\s*줘|알려\s*줘")
REF_RE = re.compile(r"그림체|스타일|참고|이런\s*느낌|처럼")
POINT_RE = re.compile(r"이걸로|이거|그걸로|방금|이\s*(?:이미지|그림|사진)")
REPEAT_RE = re.compile(r"하나\s*(?:만\s*)?더|한\s*번\s*더|다시\s*만들어|또\s*만들어")
REVISE_RE = re.compile(r"더(?![가-힣])|덜|다시|하게\s*(?:바꿔|해|만들)|바꿔")

ANSWER_HELP = "만들고 싶은 장면을 적어 주세요. 결과 설명과 프롬프트 보기는 아직 하지 않습니다."
ANSWER_I2V_EDIT = "영상의 배경과 모습은 첫 화면 사진에서 정해집니다. 사진을 먼저 편집한 뒤 영상으로 만들어 주세요."
NOTE_REFERENCE = "참고 이미지는 아직 쓰지 못해 글만 보고 그렸습니다"


def decide(text: str, n_attachments: int, last: dict | None = None, used_default: bool = False,
           video_hint=None) -> dict:
    """규칙 R0~R10 으로 정한다. last 는 직전 결과 {"action": "image"|"i2v"|..., "media": "image"|"video"} 또는 None.
    video_hint(text)->bool 은 카메라·속도 말 같은 영상 낱말을 더 알려 주는 선택 함수다.
    돌려주는 값: action(image|i2v|t2v|edit|answer), source(upload|last_result|none), carry(new|revise|repeat),
    attachment_role, rule, reply(answer 일 때 글), notes(결과 뒤 안내), later(이 기능은 아직 없음)"""
    t = text or ""
    att = n_attachments > 0
    last_media = (last or {}).get("media")
    # 카메라·속도 말("천천히", "줌 인")은 사진이 있거나 직전 결과가 이미지일 때만 영상 말로 본다. 사진 없이 "느리게 흐르는 강"을
    # 쓰면 이미지 요청인데 글→영상(P3)으로 가 버리기 때문이다 [추정: 첫 시험 뒤 고친다]
    is_video_word = bool(VIDEO_RE.search(t)) or bool(video_hint and (att or last_media == "image") and video_hint(t))
    is_edit_word = bool(STRONG_EDIT_RE.search(t)) or (bool(WEAK_EDIT_RE.search(t)) and not DRAW_RE.search(t))

    def out(rule, action, source="none", carry="new", role="none", reply=None, notes=(), later=False):
        return {"rule": rule, "action": action, "source": source, "carry": carry, "attachment_role": role,
                "reply": reply, "notes": list(notes), "later": later}

    if used_default and att:                                                  # R0 → R3
        return out("R0", "i2v", "upload", role="first_frame")
    # 짧은 인사·질문만 답으로 끝낸다. 긴 글이나 사진이 있으면 만들 말이 섞였을 수 있다 ("안녕이라고 쓰인 간판")
    if not att and len(t) <= 40 and not is_video_word and not is_edit_word and not MAKE_RE.search(t) \
            and (GREET_RE.search(t) or ASK_RE.search(t)):
        return out("R2", "answer", reply=ANSWER_HELP)                         # R2
    if att and REF_RE.search(t) and not is_video_word and not is_edit_word:   # R2b
        return out("R2b", "image", role="reference", notes=[NOTE_REFERENCE])
    if att and is_video_word:                                                 # R3
        return out("R3", "i2v", "upload", role="first_frame")
    if att and is_edit_word:                                                  # R4 (P3)
        return out("R4", "edit", "upload", role="edit_target")
    if not att and is_video_word and last_media == "image" and (POINT_RE.search(t) or REPEAT_RE.search(t)):   # R5
        return out("R5", "i2v", "last_result", role="first_frame")
    if not att and last and REPEAT_RE.search(t):                              # R6b
        return out("R6b", last.get("action") or "image", "none", "repeat")
    if not att and last_media == "video" and REVISE_RE.search(t) and not is_edit_word and not NEWPIC_RE.search(t):   # R6
        return out("R6", last.get("action") or "i2v", "none", "revise")
    if not att and last_media == "image" and REVISE_RE.search(t) and not is_edit_word and "새로" not in t:   # R6a
        return out("R6a", "image", "none", "revise")
    if not att and last_media == "image" and is_edit_word:                    # R7 (P3)
        return out("R7", "edit", "last_result", role="edit_target")
    if not att and last_media == "video" and is_edit_word and not NEWPIC_RE.search(t):   # R7b
        if last.get("action") == "t2v":
            return out("R7b", "t2v", "none", "revise")
        return out("R7b", "answer", reply=ANSWER_I2V_EDIT)
    if not att and is_video_word:                                             # R8 (P3)
        return out("R8", "t2v")
    if not att:                                                               # R9
        return out("R9", "image")
    return out("R10", "i2v", "upload", role="first_frame")                    # R10 기본값 (D19)
