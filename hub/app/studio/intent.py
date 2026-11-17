"""의도 판단 (docs/chat-studio-design.md 5.3): 빈 글 처리(R0), 규칙 R2~R10, 글에 든 길이·비율·소리 지정 뽑기.

규칙으로만 정한다(LLM 은 P4). 순수 함수만 둔다. 어느 레시피가 있는지는 모르므로 정한 action 의 레시피가
없으면 부르는 쪽(api)이 "이 기능은 지금 준비 중입니다"로 끝낸다.
"""
import re

# 제작진 분야 키워드에 걸리지 않는 문장이다 ('자연스럽게' 는 풍경 키워드 '자연' 에 걸린다, crew_knowledge.json)
DEFAULT_I2V_TEXT = "이 사진 속 장면이 부드럽게 움직이는 짧은 영상"
MAX_TEXT = 2000
MAX_SECONDS = 5.0              # 소리 없는 영상(Wan)
MAX_SECONDS_SOUND = 20.0       # 소리 포함 영상(LTX-2.5) 한 장면
MAX_SECONDS_LONG = 180.0       # 긴 영상(여러 장면을 이어 붙임, docs/long-video-design.md 7절). 첫 목표 3분 (11-17 사용자 결정)
MIN_SECONDS = 0.5

SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*초")
# "3분", "1분 30초", "1분 반", "2분짜리". '부분'·'충분' 같은 말에 걸리지 않게 숫자 바로 뒤의 '분'만 본다.
# 길이가 아닌 '분'은 뺀다 (11-17 검토): "10분 전·후·뒤", "5분 거리", "3분의 1", "4분할", "1분기", "5분마다", "30분 만에", "1분 간격"
_NOT_LENGTH = r"(?!\s*(?:의|전|후|뒤|거리|기|할|간격|마다|만에|째))"
MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*분" + _NOT_LENGTH + r"(?:\s*(?:(\d+(?:\.\d+)?)\s*초|(반)))?")
CLOCK_BEFORE_RE = re.compile(r"\d\s*시\s*$")      # "3시 15분" 은 시각이다
ASPECT_RES = (
    ("portrait", re.compile(r"세로|9\s*:\s*16", re.I)),
    ("landscape", re.compile(r"가로|16\s*:\s*9", re.I)),
    ("square", re.compile(r"정사각|1\s*:\s*1")),
)
SOUND_RE = re.compile(r"소리|음악|효과음|사운드|BGM", re.I)
# '소리 없이' 는 소리가 없는 영상(Wan)으로. '음악 없이' 는 소리 포함이되 음악만 뺀다 (영상 렌더러가 따로 본다)
MUTE_RE = re.compile(r"소리\s*(?:는|도|를|가)?\s*(?:없이|빼|꺼|필요\s*없)|소리\s*없는\s*(?:영상|동영상|클립|비디오|버전)|무음|음소거"
                     r"|\bsilent\b|\bno sound\b|without sound", re.I)


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


def extract_spec(text: str, max_seconds: float = MAX_SECONDS) -> dict:
    """{seconds: float|None, aspect: str|None, sound: bool(소리 말이 있는가), notes: [한국어 안내]}.
    max_seconds 를 넘는 길이는 그 길이로 맞추고 안내를 남긴다(소리 없는 영상 5초, 소리 포함 20초, 긴 영상 3분). 안내는 결과 뒤에 한 줄로 보인다."""
    notes = []
    seconds = None
    v = asked_seconds(text)
    if v is not None:
        if v > max_seconds:
            seconds = max_seconds
            lab = long_label(max_seconds)
            notes.append(f"한 번에 {lab}까지라 {lab}{_ro(lab)} 만들었습니다")
        elif v >= MIN_SECONDS:
            seconds = v
    aspect = next((name for name, rx in ASPECT_RES if rx.search(text or "")), None)
    sound = bool(SOUND_RE.search(text or ""))
    return {"seconds": seconds, "aspect": aspect, "sound": sound, "notes": notes}


def asked_seconds(text: str) -> float | None:
    """글에 쓴 길이(초). '3분'·'1분 30초'·'1분 반'·'90초'. 없으면 None.
    길이로 읽을 '분'이 있으면 그것을 쓴다 ('처음 10초는 천천히, 전체 3분 영상' 은 3분). 길이가 아닌 '분'(MINUTES_RE 의 제외 말,
    시각)만 있으면 초를 본다 ('10초짜리 영상, 5분 거리 공원' 은 10초)"""
    text = text or ""
    for m in MINUTES_RE.finditer(text):
        if CLOCK_BEFORE_RE.search(text[:m.start()]):
            continue
        v = float(m.group(1)) * 60
        if m.group(2):
            v += float(m.group(2))
        elif m.group(3):
            v += 30
        return v
    m = SECONDS_RE.search(text)
    return float(m.group(1)) if m else None


def is_long(seconds) -> bool:
    """한 장면(20초)보다 길어 여러 장면을 이어 붙이는 긴 영상인가"""
    try:
        return float(seconds or 0) > MAX_SECONDS_SOUND
    except (TypeError, ValueError):
        return False


def long_label(seconds) -> str:
    """180 → '3분', 90 → '1분 30초', 45 → '45초'"""
    s = int(round(float(seconds or 0)))
    m, r = divmod(s, 60)
    if not m:
        return f"{r}초"
    return f"{m}분" + (f" {r}초" if r else "")


def _ro(word: str) -> str:
    """조사 '로/으로': 받침이 있으면(ㄹ 받침 빼고) '으로' ('3분으로', '20초로')"""
    ch = (word or " ")[-1]
    code = ord(ch) - 0xAC00
    if 0 <= code < 11172 and code % 28 not in (0, 8):
        return "으로"
    return "로"


# 5초 넘는 길이는 소리 포함 영상만. 20초 넘는 길이(30초~3분)는 긴 영상(여러 장면)이다
SECONDS_CHOICES = (2.0, 3.0, 4.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 30.0, 60.0, 90.0, 120.0, 180.0)
ASPECT_CHOICES = ("portrait", "landscape", "square")


def clean_options(options) -> dict:
    """화면의 길이·비율 칸 (D17). {"seconds": 2~5 중 하나, "aspect": portrait|landscape|square} 중 고른 것만 돌려준다.
    자동(없음·null·"auto")은 뺀다. 모르는 값은 InputError. 글에 쓴 지정보다 칸이 우선한다"""
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise InputError("bad_request", "요청 형식이 올바르지 않습니다.")
    out = {}
    sec = options.get("seconds")
    if sec not in (None, "", "auto"):
        try:
            v = float(sec)
        except (TypeError, ValueError, OverflowError):
            raise InputError("bad_request", "요청 형식이 올바르지 않습니다.") from None
        if isinstance(sec, bool) or v not in SECONDS_CHOICES:
            raise InputError("bad_request", "길이는 2초~3분 중에서 고를 수 있습니다.")
        out["seconds"] = v
    asp = options.get("aspect")
    if asp not in (None, "", "auto"):
        if asp not in ASPECT_CHOICES:
            raise InputError("bad_request", "비율은 가로·세로·정사각 중에서 고를 수 있습니다.")
        out["aspect"] = asp
    q = options.get("quality")
    if q not in (None, "", "auto", "fast"):
        if q != "high":
            raise InputError("bad_request", "화질은 빠름·높음 중에서 고를 수 있습니다.")
        out["quality"] = "high"
    snd = options.get("sound")
    if snd not in (None, "", "auto"):
        if snd not in ("on", "off"):
            raise InputError("bad_request", "소리는 자동·포함·없음 중에서 고를 수 있습니다.")
        out["sound"] = snd
    return out


def apply_options(spec: dict, options: dict) -> dict:
    """글에서 뽑은 spec 에 칸에서 고른 값을 덮어쓴다 (칸이 우선). 글의 '5초 넘음' 안내는 칸이 덮으면 뺀다"""
    s = dict(spec or {})
    if "seconds" in options:
        s["seconds"] = options["seconds"]
        s["notes"] = [n for n in s.get("notes") or [] if "까지라" not in n]
    if "aspect" in options:
        s["aspect"] = options["aspect"]
    if options.get("quality") == "high":
        s["quality"] = "high"
    return s


def i2v_notes(spec: dict, aspect_note: bool = True) -> list[str]:
    """i2v 는 사진 비율을 따르므로 비율 지정이 있으면 알린다 (글→영상을 장면 이미지로 만든 경우는 비율을 지켰으니 알리지 않는다)"""
    notes = list(spec.get("notes") or [])
    if spec.get("aspect") and aspect_note:
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


# 승인 대기 중에 이 말만 오면 계획 수정이 아니라 승인이다 ("응", "네 진행해줘", "ok")
APPROVE_RE = re.compile(r"^\s*(?:응|네|예|넵|좋아|좋아요|승인|진행|시작|오케이|ok|okay|ㅇㅇ)(?:[\s,.!~]*(?:진행|시작)?(?:해줘|해주세요|해)?)[\s.!~]*$", re.I)


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
