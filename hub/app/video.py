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

from . import cast_lib, crew_lib, promptx
from . import crew as crew_mod
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


# ── 소리: 영상과 소리를 함께 만드는 모델(LTX-2.5)용. 영상 프롬프트 뒤에 붙는 'Audio:' 단락을 따로 쓴다 ──
SOUND_SYSTEM = """You are the sound designer of a small film crew. You write the sound for one short video clip; the video model creates the audio together with the picture from your text.
Clip: about {seconds} seconds, one continuous shot.
Return JSON only:
- "sound": one English paragraph of {lo} to {hi} words that starts with "Audio:".
- "ko": one Korean sentence that tells the user what they will hear.
Rules:
- Order: first the ambient bed of the place (room tone, wind, a distant hum), then the sounds that the visible actions make, in the order of the picture (name what makes each sound, its material and its distance, for example a heel on wet stone, fabric brushing, a motor humming), then the music, then how the sound settles with the final frame.
- Sound only what the picture shows or implies, and never add a sound source that is not in the video text. When a thing makes no sound (space, a still lake), let a quiet bed and the music carry it instead of inventing noise.
- Music: {music_rule}
- Speech: {speech_rule}
- Level over time: every sound you name is already audible in the first second and keeps the same steady level to the last second. Never write that a sound starts later, builds, swells, rises, grows louder, fades in or out, or draws nearer; the ambient bed, the footsteps and the music all sound the same from start to end.
- Describe only what is heard; never mention light, colour, bokeh, reflections or anything that is only seen.
- Positive phrasing only: describe what is heard; never write what is absent (no "no", "not", "without").
- Never name songs, bands, artists, brands or real people, and never write lyrics."""
MUSIC_ON = ("add a quiet instrumental score under the sounds: name its mood, its tempo (slow, steady or driving) and two or three "
            "instruments or synthesizer textures that fit the picture; keep it clearly below the sound effects and at one steady level "
            "from the first second to the last, with no swell and no fade.")
MUSIC_OFF = "the user asked for no music: write no music at all, only the ambient bed and the sound effects."
# 긴 영상의 장면: 배경음악은 합칠 때 한 곡으로 따로 깐다 (docs/long-video-design.md 7절). 장면 소리에 음악이 있으면 두 음악이 겹친다
MUSIC_LATER = ("the film's background music is laid over all scenes later as one track, so write no music at all: "
               "only the ambient bed and the sounds of the action.")
MUSIC_WORD_RE = re.compile(r"\b(?:music|musical|score|soundtrack|melod(?:y|ies|ic)|piano|violin|cello|synth(?:esizer)?s?|"
                           r"orchestra(?:l)?|chords?|instrumental)\b", re.I)
SPEECH_OFF = "the clip has no spoken words: write no speech, singing or narration."
SPEECH_ON = ("the user gave these spoken lines: {lines}. Put each line in double quotes, word for word and in its original language, "
             "say which visible person speaks it, and describe the voice in a few words. Add no other speech.")
NO_MUSIC_RE = re.compile(r"음악\s*(?:은|도|을)?\s*(?:없이|빼|넣지\s*마|필요\s*없)|배경\s*음악\s*(?:은\s*)?(?:없|빼)|무\s*음악"
                         r"|\bno (?:background )?music\b|without (?:any )?music", re.I)
QUOTE_RE = re.compile(r'["“”「」『』]\s*([^"“”「」『』]{2,200}?)\s*["“”「」『』]')
# 따옴표가 대사인지는 말하는 말이 함께 있을 때만 본다 ('"니키"라는 고양이' 의 이름을 대사로 읽지 않는다)
SPEECH_CUE_RE = re.compile(r"말(?:한|해|하|을|이)|라고\s*(?:말|외|속삭|인사|노래|한다|해)|대사|외치|속삭|인사|\bsays?\b|\bsaid\b|speak|whisper|shout|talk", re.I)
QUOTED_OUT_RE = re.compile(r'["“”][^"“”]*["“”]')
SOUND_NEGATION_RE = re.compile(r"\b(?:no|not|without)\b", re.I)
# 소리 단락은 들리는 것만: 11-17 '...fading gently into the background bokeh lights' (소리가 보케 불빛으로 사라진다)
# 11-17 메인 실측: 소리가 5~8초부터 갑자기 커진다 (초당 크기 2 2 2 2 2 9 32 72 133 189). 시간에 따라 변하는 말은 쓰지 않는다
SOUND_DRIFT_RE = re.compile(
    r"\b(?:fad(?:e|es|ed|ing)|swell(?:s|ed|ing)?|crescendo|builds?|building|grow(?:s|ing)? (?:louder|stronger|fuller)|louder|"
    r"ris(?:es|ing) (?:slowly|gradually|steadily|to|in)|intensif\w+|gradually|(?:begins?|starts?|starting|beginning) to|"
    r"dies? (?:down|away)|draws? (?:near|closer)|nearer|approach\w*|crescendos?|ramps? up|slowly emerg\w+|emerg(?:es|ing))\b", re.I)
SOUND_SEEN_RE = re.compile(r"\b(?:bokeh|reflections?|reflective|shadows?|silhouettes?|haze|hazy|blurry|blurred)\b", re.I)


def spoken_lines(said: str) -> list[str]:
    """사용자가 따옴표로 준 대사. 영상 안에서 그대로 말하게 한다"""
    if not SPEECH_CUE_RE.search(said or ""):
        return []
    return [m.strip() for m in QUOTE_RE.findall(said or "") if m.strip()][:4]


def sound_problems(data, said: str, profile: dict, music: bool | None = None) -> list[str]:
    """music=False 면 음악이 없어야 한다 (긴 영상의 장면: 배경음악은 따로 깐다)"""
    if not isinstance(data, dict):
        return ["Return a JSON object with sound and ko."]
    lo, hi = profile.get("sound_words") or [35, 120]
    snd = str(data.get("sound") or "").strip()
    problems = []
    n = len(snd.split())
    if not lo <= n <= hi:
        problems.append(f'"sound" has {n} words; write {lo} to {hi} words.')
    if HANGUL_RE.search(QUOTED_OUT_RE.sub("", snd)):
        problems.append('"sound" must be English; only a spoken line in double quotes may keep its own language.')
    if not HANGUL_RE.search(str(data.get("ko") or "")):
        problems.append('"ko" must be a Korean sentence.')
    lines = spoken_lines(said)
    if QUOTED_OUT_RE.sub("", snd) != snd and not lines:
        problems.append("No one speaks in this clip: remove the quoted words.")
    for line in lines:
        if line not in snd:
            problems.append(f'Say the user\'s spoken line word for word in double quotes: "{line}".')
    if SOUND_NEGATION_RE.search(QUOTED_OUT_RE.sub("", snd)):
        problems.append('Use positive phrasing only: describe what is heard, never write "no", "not" or "without".')
    drift = SOUND_DRIFT_RE.search(QUOTED_OUT_RE.sub("", snd))
    if drift:
        problems.append(f'Every sound must keep one steady level from the first second to the last, but the text says it changes over '
                        f'time ("{drift.group(0)}"): write each sound as already present and constant, with no start, swell or fade.')
    seen = SOUND_SEEN_RE.search(QUOTED_OUT_RE.sub("", snd))
    if seen:
        problems.append(f'The sound paragraph must describe only what is heard, but it mentions something only seen ("{seen.group(0)}"): '
                        "name the sound it makes or drop it.")
    mus = music_words(QUOTED_OUT_RE.sub("", snd), said) if music is False else []
    if mus:
        problems.append(f'This clip must have no music (the film\'s music is added later), but the text names "{mus[0]}": '
                        "remove every music word and keep only the ambient bed and the sounds of the action.")
    return problems


def music_words(text: str, allowed: str = "") -> list[str]:
    """음악 낱말. 장면에서 실제로 연주하는 악기('피아노 치는 소녀'의 piano)처럼 요청·장면 글(allowed)에 있는 말은 뺀다"""
    allow = (allowed or "").lower()
    return [m.group(0) for m in MUSIC_WORD_RE.finditer(text or "") if m.group(0).lower() not in allow]


def music_rule(said: str, music: bool | None = None) -> str:
    if music is False:
        return MUSIC_LATER
    return MUSIC_OFF if NO_MUSIC_RE.search(said or "") else MUSIC_ON


async def write_sound(call, text: str, notes: str, video_prompt: str, seconds: float, profile: dict,
                      rounds: int = 2, music: bool | None = None) -> dict:
    """영상 프롬프트를 본 뒤 소리 단락을 쓴다 (같은 LLM 차례 안). {'sound': 'Audio: ...', 'ko': '...', 'rounds': n, 'left': [...]}.
    music=False 는 음악 없이 (긴 영상의 장면)"""
    said = f"{text} {notes}".strip()
    lo, hi = profile.get("sound_words") or [35, 120]
    lines = spoken_lines(said)
    system = SOUND_SYSTEM.format(
        seconds=seconds, lo=lo, hi=hi, music_rule=music_rule(said, music),
        speech_rule=SPEECH_ON.format(lines=" | ".join(f'"{x}"' for x in lines)) if lines else SPEECH_OFF)
    user = f"Request (Korean): {text}\n" + (f"Must-keep details from the user: {notes}\n" if notes else "") \
        + f"VIDEO TEXT (what the picture and the motion show):\n{video_prompt}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    schema = {"type": "object", "properties": {"sound": {"type": "string"}, "ko": {"type": "string"}}, "required": ["sound", "ko"]}
    best, data, problems = None, {}, ["no answer"]
    for rnd in range(1, rounds + 1):
        reply = await call({"messages": messages, "format": schema, "think": False, "options": {"temperature": 0.6}})
        content = (reply.get("message") or {}).get("content", "")
        try:
            data = parse_json_reply(content)
        except PromptxError as e:
            data, problems = {}, [str(e)]
        else:
            problems = sound_problems(data, said, profile, music)
        if isinstance(data, dict) and str(data.get("sound") or "").strip() and (best is None or len(problems) < len(best[1])):
            best = (data, problems)
        if not problems:
            break
        messages = messages + [{"role": "assistant", "content": content}, repair_message(problems)]
    data, left = best if best else ({}, problems)
    snd = re.sub(r"\s+", " ", str(data.get("sound") or "")).strip()
    if snd and not snd.lower().startswith("audio:"):
        snd = "Audio: " + snd
    return {"sound": snd, "ko": str(data.get("ko") or "").strip()[:300], "rounds": rnd, "left": left}


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


def part_min(profile: dict) -> int:
    """단락 최소 낱말 수. 보통 프로필은 단락이 짧아 더 작게 둔다."""
    return int(profile.get("part_min_words") or long_spec()["part_min_words"])


def aim_words(profile: dict) -> int:
    """모델에게 알리는 목표 낱말 수. 보통은 범위의 가운데이고, 프로필이 정하면 그 값이다.

    27B 는 목표보다 25~60% 길게 쓴다(11-12·11-16 메인). T2V 장문은 상한을 600 으로 넓히되 목표는 410 에 두어
    넓어진 상한이 '더 길게 쓰라' 는 뜻이 되지 않게 한다.
    """
    lo, hi = profile["words"]
    return int(profile.get("target") or (lo + hi) // 2)


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


LENGTH_SYSTEM = """You decide how detailed the video prompt for one short clip should be. Return JSON only:
{{"tier": "long" or "mid", "reason": "one short Korean sentence that tells the user why"}}
- long: four paragraphs of about 100 words. Choose it when the request is rich in specifics (several subjects, camera or mood details, a setting or a small story), has people or a first-person view, or is a complex scene, or when it says little and the whole look has to be invented.
- mid: four compact paragraphs of about 45 words. Choose it when the request is one simple, clear idea that is easy to picture and a focused prompt will do.
{mode_note}In doubt choose long."""
LENGTH_MODE_NOTE = {
    "i2v": "The first frame already fixes the look, so a simple request needs only a compact prompt.\n",
    "t2v": "There is no first frame, so even a short request needs a rich invented look; choose mid only for a truly trivial one-object idea.\n",
}
LENGTH_REASON = {"long": "요청에 맞춰 자세히(장문) 쓰기로 했습니다.", "mid": "요청이 단순해 보통 길이로 쓰기로 했습니다."}


def request_is_detailed(text: str, notes: str = "") -> bool:
    """3줄 이상이거나 공백을 뺀 글자가 120자 이상이거나 '꼭 지킬 특징' 이 있으면 자세한 요청이다."""
    t = str(text or "")
    lines = [ln for ln in t.split("\n") if ln.strip()]
    return len(lines) >= 3 or len(re.sub(r"\s+", "", t)) >= 120 or bool(str(notes or "").strip())


async def decide_length(call, text: str, notes: str, mode: str, frame: dict | None = None, scene: str = "") -> dict:
    """장문(long)으로 쓸지 보통(mid)으로 쓸지 정한다. {tier, reason(한국어), by: rule|llm}.

    자세한 요청은 규칙으로 바로 장문이다(모델을 부르지 않는다). 짧은 요청만 같은 GPU 차례 안에서 LLM 에게 묻고,
    못 정하면 요청 길이 규칙(짧으면 보통)을 따른다. 판단이 애매하면 long 이 기본이다.
    """
    if request_is_detailed(text, notes):
        return {"tier": "long", "reason": "요청이 자세해서 자세히(장문)로 쓰기로 했습니다.", "by": "rule"}
    lines = [f"Mode: {'image to video' if mode == 'i2v' else 'text to video'}", f"Request: {text}"]
    if notes:
        lines.append(f"Must keep: {notes}")
    if frame_known(frame):
        lines.append(f"First frame: {frame.get('subject') or ''}; {frame.get('setting') or ''}")
    elif scene:
        lines.append(f"Look reference: {scene[:300]}")
    schema = {"type": "object", "required": ["tier", "reason"], "additionalProperties": False,
              "properties": {"tier": {"type": "string", "enum": ["long", "mid"]}, "reason": {"type": "string"}}}
    try:
        data = await call({"messages": [{"role": "system", "content": LENGTH_SYSTEM.format(mode_note=LENGTH_MODE_NOTE[mode])},
                                        {"role": "user", "content": "\n".join(lines)}],
                           "format": schema, "think": False, "options": {"temperature": 0.2}})
        got = parse_json_reply((data.get("message") or {}).get("content", ""))
        tier = got.get("tier") if isinstance(got, dict) else None
        if tier in ("long", "mid"):
            reason = str(got.get("reason") or "").strip()[:200]
            return {"tier": tier, "reason": reason if HANGUL_RE.search(reason) else LENGTH_REASON[tier], "by": "llm"}
    except (PromptxError, RuntimeError, ValueError, KeyError, AttributeError):
        pass
    return {"tier": "mid", "reason": "자동 판단을 못 해 요청 길이로 보통 길이를 골랐습니다.", "by": "rule"}


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
    return [{"id": k, "title": p["title"], "mode": p["mode"], "long": is_long(p), "mid": bool(p.get("mid")), "default_seconds": p["default_seconds"],
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
    if profile.get("whole_seconds"):       # LTX 는 길이를 정수 초로 받는다: 2.5초는 3초로, 1초는 최소 길이로 맞춘다
        sec = float(max(int(profile.get("min_seconds") or 1), int(round(sec))))
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


# ── 보행 장면 연출 무작위 (11-17 러다이트 비교): 방향을 안 정한 보행 요청은 옆 추적(좌→우·우→좌)·멀어짐·다가옴을 고르게 섞는다 ──
# 장면 이미지가 영상의 첫 화면이 되므로 고른 연출을 장면 이미지 힌트와 영상 프롬프트에 똑같이 준다.
WALK_KO_RE = re.compile(r"걸어|걷[는고다지]|걸으|걸음|산책|거닐|배회|행진")
WALKER_KO_RE = re.compile(r"남성|여성|남자|여자|사람|소년|소녀|아이|노인|할아버지|할머니|청년|인물|사내|신사|숙녀|아저씨|아주머니")
DIRECTION_KO_RE = re.compile(r"왼쪽|오른쪽|좌에서|우에서|방향|카메라 ?(?:쪽|를 ?향|앞)|정면|옆모습|옆에서|측면|뒷모습|뒤에서|뒤를|따라|추적|고정|다가|멀어|앞으로|뒤로|클로즈|줌|팬|트래킹|드론|항공|위에서|아래에서|회전|돌아")

STAGINGS = {
    "side_lr": {
        "scene": ("Compose it as a side-on view: the walker seen in full-body profile, facing right and walking left to right, "
                  "placed in the left half of the frame with open space ahead of them on the right."),
        "video": ("The walker travels from left to right in side profile. The camera tracks alongside at the same pace and the "
                  "same distance, so the walker keeps the same place in the frame while the background slides past from right to left."),
    },
    "side_rl": {
        "scene": ("Compose it as a side-on view: the walker seen in full-body profile, facing left and walking right to left, "
                  "placed in the right half of the frame with open space ahead of them on the left."),
        "video": ("The walker travels from right to left in side profile. The camera tracks alongside at the same pace and the "
                  "same distance, so the walker keeps the same place in the frame while the background slides past from left to right."),
    },
    "away": {
        "scene": ("Compose it from behind: the walker seen from the back at some distance, walking away from the camera along the "
                  "street, centered, with the way ahead open in front of them."),
        "video": ("The walker moves away from the camera, seen from behind. The camera follows behind at the same pace and the "
                  "same distance, moving forward, so the walker keeps about the same size in the frame while the street opens ahead."),
    },
    "toward": {
        "scene": ("Compose it from the front: the walker seen facing the camera at a medium-wide distance, walking toward it, so "
                  "there is room for the figure to come closer and still fit the frame."),
        "video": ("The walker moves toward the camera. The camera is locked off, so the figure grows larger in the frame as they "
                  "approach; they start at a medium-wide distance and stay fully inside the frame until the end of the clip."),
    },
}


def pick_staging(said: str, rng=None) -> str | None:
    """방향·카메라를 정하지 않은 '사람이 걷는' 요청이면 연출 id 를 무작위(세 갈래 균등, 옆 추적은 좌우 반반)로 고른다.
    1인칭, 사용자가 방향·카메라를 말한 요청, 사람이 아닌 대상은 None."""
    said = said or ""
    if pov_wanted(said) or not WALK_KO_RE.search(said) or not WALKER_KO_RE.search(said) or DIRECTION_KO_RE.search(said):
        return None
    r = rng or random.SystemRandom()
    kind = r.choice(["side", "away", "toward"])
    return r.choice(["side_lr", "side_rl"]) if kind == "side" else kind


def staging_of(sid) -> dict | None:
    return STAGINGS.get(sid) if isinstance(sid, str) else None


def staging_line(sid) -> str:
    st = staging_of(sid)
    if not st:
        return ""
    return ("STAGING (decided for this clip and already used to compose the first frame; keep it exactly, in every paragraph, "
            "and never change the direction or the camera relationship): " + st["video"])


# ── 연출 계획표 (11-17): 러다이트처럼 장소·기분·빛·비 를 하나하나 짚는 연출을 모든 요청에서 ──
# 프롬프트를 쓰기 전에 감독이 계획표를 먼저 짠다. 장소·빛은 장면 이미지에 그려지고(첫 화면), 구간별 연기·공기·디테일은 영상 글에 들어간다.
# 규칙 문장만으로는 27B 가 일부만 지켰다(11-17 메인: 따뜻한 포인트 조명·기분·장소 사물이 빠짐). 그래서 계획표를 만들고 빠진 항목을 검사한다.
PLAN_SYSTEM = """You are the director of a small film crew. Before anything is shot you write the production plan for one short video clip of about {seconds} seconds, one continuous shot.
Return JSON only:
- "place": 3 or 4 short phrases, each a concrete, visible thing that tells where the shot is (for a street: the sidewalk, the curb, the building fronts, a side street; for a room: the furniture and surfaces; for a landscape: the ground cover, the horizon line, the nearest rock or tree).
- "light": one sentence: where the main light comes from, how it separates the subject from the background (an edge of light, a lit face, a lit outline), and ONE small accent light in the far background (a lit window, a lamp, a glow on water) whose colour differs from the main light.
- "air": one sentence on weather, mist, dust, steam or any particles in three depth layers: near the lens, in the middle, far away, and how each layer is seen and touches the surfaces and light (rain streaking against dark walls near the lens, droplets stippling the ground in the middle, haze softening the far buildings). Write an empty string when the scene has none.
- "details": 2 or 3 short phrases for small signature details of the subject or its material that the camera can see (a glistening wet sleeve, a chipped glaze, a scuffed boot).
- "ending": one sentence: the exact last frame of the clip, because a later shot may start from it: the subject fully in frame (its size and position), standing or walking, the camera settled; when a person is in the shot and the film context below asks for it, the face is visible (front or three-quarter view).
- "acting": exactly 3 phrases, one per third of the clip: the opening moment, the one main action unfolding (with ONE small living detail that does not change the action, such as a lifted glance, a gust, a ripple), and how the same action carries on at the end. For a person show the mood through the body (head, shoulders, gaze, posture, steps), never a mood word alone; for an animal the paws, wings or body; for an object or vehicle how its material, wheels or surfaces respond; for a landscape or weather how its own elements move.
Rules:
- Honour every word of the request first; it is the top priority. Invent only what fits the place and the request, and nothing that contradicts it.
- Write short English phrases, each at most 20 words. Name no brands, songs, artists or real people, and write no text, signs or logos.
- Positive phrasing only: never write what is absent (no "no", "not", "without").
- A person's fixed look (hair, glasses, coat colour, build) comes from the must-keep details; repeat it in "details" exactly and never add or change anything (no new glasses, hats, scarves or beards).
- The video model draws touching and handling wrongly (bent fingers, doors that open twice). Never plan a close-up of hands or an action where hands grip, touch, pick up, hand over, open, close, push, pull, put on, take off, fold or adjust anything (doors, umbrellas, coats, keys, phones, cups). A person may keep carrying an object already in the hand.
- A person must not be added when the request has none."""
PLAN_KEYS = ("place", "light", "air", "details", "acting")


def _clean_items(v, lo: int, hi: int) -> list[str] | None:
    if not isinstance(v, list):
        return None
    out = [re.sub(r"\s+", " ", str(x or "")).strip(" .;") for x in v]
    out = [x for x in out if x]
    return out[:hi] if len(out) >= lo else None


def clean_plan(data, safe: bool = False) -> dict | None:
    """모델이 돌려준 계획표를 다듬는다. 모양이 틀리면 None (계획표 없이 기존 방식으로 간다).
    safe 이면 손·접촉 동작(PHYSICS_RE)이 들어간 계획표도 버린다 (긴 영상의 장면: 11-18 3분 실측의 물리 오류)"""
    if not isinstance(data, dict):
        return None
    place = _clean_items(data.get("place"), 3, 4)
    acting = _clean_items(data.get("acting"), 3, 3)
    details = _clean_items(data.get("details"), 1, 3)
    light = re.sub(r"\s+", " ", str(data.get("light") or "")).strip()
    air = re.sub(r"\s+", " ", str(data.get("air") or "")).strip()
    if not (place and acting and details and light):
        return None
    plan = {"place": place, "light": light, "air": air, "details": details, "acting": acting}
    ending = re.sub(r"\s+", " ", str(data.get("ending") or "")).strip()
    if ending:
        plan["ending"] = ending
    flat = " ".join(place + acting + details + [light, air, ending])
    if HANGUL_RE.search(flat) or SOUND_NEGATION_RE.search(flat):
        return None
    if safe and PHYSICS_RE.search(flat):
        return None
    return plan


async def write_plan(call, text: str, notes: str, profile: dict, seconds: float, frame: dict | None = None,
                     scene: str = "", staging: str | None = None, direction: str = "", safe: bool = False) -> dict | None:
    """연출 계획표 하나를 같은 LLM 차례 안에서 쓴다. 실패하면 None (영상은 계획표 없이도 만들어진다)."""
    if not is_long(profile):
        return None
    system = PLAN_SYSTEM.format(seconds=int(float(seconds) + 0.5) if seconds else 5)
    user = f"Request (Korean): {text}\n"
    if notes:
        user += f"Must-keep details from the user: {notes}\n"
    st = staging_of(staging)
    if st:
        user += f"Direction already decided (keep it; the acting beats must agree with it): {st['video']}\n"
    if frame and frame_known(frame):
        user += ("FIRST FRAME already exists (read by a vision model). Name only place items, lights and details that this frame "
                 "shows; the acting beats are what happens next:\n" + frame_text(frame) + "\n")
    elif scene:
        user += f"Look reference (an image prompt for this shot; stay consistent with it): {scene[:1500]}\n"
    if direction:
        user += f"Film context and continuity (obey it): {direction[:1800]}\n"
    schema = {"type": "object", "required": list(PLAN_KEYS), "properties": {
        "place": {"type": "array", "items": {"type": "string"}}, "light": {"type": "string"}, "air": {"type": "string"},
        "details": {"type": "array", "items": {"type": "string"}}, "acting": {"type": "array", "items": {"type": "string"}},
        "ending": {"type": "string"}}}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for _ in range(2):
        reply = await call({"messages": messages, "format": schema, "think": False, "options": {"temperature": 0.7}})
        content = (reply.get("message") or {}).get("content", "")
        try:
            plan = clean_plan(json.loads(content), safe)
        except (ValueError, TypeError):
            plan = None
        if plan:
            return plan
        messages = messages + [{"role": "assistant", "content": content or "{}"},
                               {"role": "user", "content": "Rewrite the plan: place 3-4 phrases, one light sentence, the air sentence "
                                "(empty if none), 1-3 details, exactly 3 acting phrases and the ending sentence; English only, positive "
                                "phrasing only" + ("; plan no hands, no touching or handling of doors, umbrellas, coats or objects" if safe else "") + "."}]
    return None


def plan_scene_text(plan: dict | None) -> str:
    """장면 이미지(영상의 첫 화면)에 그려 둘 것: 장소 사물, 빛, 공기, 디테일. 연기 박자는 영상 글의 몫이다."""
    if not plan:
        return ""
    air = f" Air: {plan['air']}." if plan.get("air") else ""
    return ("Director's plan for this picture (show all of it, in your own words): the place has " + "; ".join(plan["place"])
            + f". Light: {plan['light']}.{air} Small details: " + "; ".join(plan["details"]) + ".")


def plan_block(plan: dict | None, timeline: bool) -> str:
    if not plan:
        return ""
    beats = " ".join(f"({i}) {b}." for i, b in enumerate(plan["acting"], 1))
    how = ("the three timed beats of the \"action\" paragraph, one per beat" if timeline
           else "three successive stages of the \"action\" paragraph")
    air = f"\n- Air (put it in \"camera_light\" or \"action\"): {plan['air']}" if plan.get("air") else ""
    return ("DIRECTOR'S PLAN (decided for this clip before shooting; the first frame was made from the place, light and details; "
            "use EVERY item below in your own words, keeping its key nouns, and add nothing that contradicts it):\n"
            f"- Place (name these in \"shot\"): {'; '.join(plan['place'])}\n"
            f"- Light (write it in \"camera_light\"): {plan['light']}{air}\n"
            f"- Details (show them in \"shot\" or \"action\"): {'; '.join(plan['details'])}\n"
            f"- Acting, as {how}: {beats}"
            + (f"\n- Last frame (write it in \"ending\"): {plan['ending']}" if plan.get("ending") else ""))


_PLAN_STOP = frozenset("the and with from into onto over under across near far its his her their this that while small slight "
                       "slightly soft gentle gently each than then them they have been being very more some same also "
                       "light lights lighting left right behind".split())


def _plan_tokens(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _PLAN_STOP}


def plan_problems(result, plan: dict | None) -> list[str]:
    """계획표 항목이 프롬프트에 들어갔는지 낱말 겹침으로 본다 (표현은 바꿔도 되고 핵심 명사가 남아야 한다)."""
    if not plan or not isinstance(result, dict):
        return []
    whole = _plan_tokens(" ".join(str(result.get(k) or "") for k in ("shot", "camera_light", "action", "ending")))
    action = _plan_tokens(str(result.get("action") or ""))
    miss: list[str] = []
    place_missing = [p for p in plan["place"] if not (_plan_tokens(p) & whole)]
    if len(place_missing) > 1:      # 장소 사물은 하나까지 빠져도 된다
        miss += [f'place "{p}"' for p in place_missing]
    if not (_plan_tokens(plan["light"]) & whole):
        miss.append(f'light "{plan["light"]}"')
    if plan.get("air") and not (_plan_tokens(plan["air"]) & whole):
        miss.append(f'air "{plan["air"]}"')
    det_missing = [d for d in plan["details"] if not (_plan_tokens(d) & whole)]
    if len(det_missing) == len(plan["details"]):
        miss += [f'detail "{d}"' for d in det_missing]
    for i, b in enumerate(plan["acting"], 1):
        if not (_plan_tokens(b) & action):
            miss.append(f'acting beat {i} "{b}"')
    if plan.get("ending") and not (_plan_tokens(plan["ending"]) & _plan_tokens(str(result.get("ending") or ""))):
        miss.append(f'last frame "{plan["ending"]}"')
    if not miss:
        return []
    return ["The director's plan items missing from the prompt: " + "; ".join(miss[:5])
            + ". Work each into the paragraph named in the plan, in your own words, keeping the plan's key nouns."]


# ── 긴 영상 구성안 (11-17, docs/long-video-design.md 7절) ──
# 영상 모델은 한 번에 5~20초 한 컷이라, 긴 요청은 장면표로 나눠 한 장면씩 만들고 컷으로 잇는다.
# 설정집(인물·장소의 고정된 모습)을 한 번 정해 모든 장면의 글에 그대로 넣는다 (장면 사이 모습을 맞추려고).
STORY_SCENE_MIN, STORY_SCENE_MAX = 5, 20
STORY_MIN_SECONDS, STORY_MAX_SECONDS = 25, 180
# ACE-Step 1.5 TextEncode 의 keyscale 값 (메인 object_info, 11-17)
MUSIC_KEYS = tuple(f"{n} {m}" for m in ("major", "minor")
                   for n in ("C", "C#", "Db", "D", "D#", "Eb", "E", "F", "F#", "Gb", "G", "G#", "Ab", "A", "A#", "Bb", "B"))
STORY_SYSTEM = """You are the director and screenwriter of a small film crew. The user wants one film of about {seconds} seconds. The video model shoots one continuous shot of 5 to 20 seconds at a time, so you split the film into scenes that are shot one by one and joined with hard cuts.
Return JSON only:
- "title_ko": a short Korean title.
- "logline_ko": one Korean sentence: what the whole film shows.
- "bible": the fixed look of everything that appears in more than one scene; it is copied word for word into every scene so the scenes match:
  - "characters": for each recurring person or animal, {{"id": "A", "look": "..."}}: age range, build, hair, face, clothing with exact colours and materials, and one signature item, in English, at most 45 words. An empty list when the film has no people or animals.
  - "places": for each place, {{"id": "P1", "look": "..."}}: the ground, the main surfaces and objects and their colours, in English, at most 45 words.
  - "time": time of day and weather in English, the same in every scene unless the request asks for a change.
  - "light": the light and colour palette of the whole film in English.
  - "style_said": the user's own words for the look of the film, copied exactly from the request (Korean or English), for example "애니메이션", "수채화 느낌으로", "3D 픽사 스타일", "실사", "pixel art". Copy only words that say how the film should look (an art style, a medium, animation or live action, the look of a kind of film or of a studio); never things inside the story (a felt doll a character makes, a pencil sketch she draws, a comic book he reads, a song, a topic). If the request turns one look down for another ("반실사 말고 애니메이션으로"), copy only the wanted look ("애니메이션"). If it only turns a look down without naming another ("반실사 말고 다른 그림체로"), copy those words as they are, the refused look included, and choose a different look yourself for "style". The same goes for "원래 그림체 말고 다른 그림체로", which turns the known characters' own look down, and for "지금 그림체 말고 다른 스타일로", which turns down the look the user sees now: the look of the film made from the earlier message when the request holds one, otherwise the known characters' own look; "style" is never the refused look. Both are the opposite of "원래 그림체 그대로", which keeps the characters' own look. Words that only keep the known characters' own look ("원래 그림체 그대로") or only give a mood ("따뜻한 느낌으로") are not a look. If the request holds an earlier message and a newer one, follow the newer one. A newer message that says nothing about the look keeps the earlier words for the look. "" when the request does not ask for a look.
  - "style": the visual style of the whole film in one English sentence, copied into every scene: when "style_said" is not empty, the film is drawn in exactly that look (for example animation, anime, 2D, 3D, cel-shaded, hand-drawn, watercolour, clay or comic): describe exactly that look (line, shading, texture, colour); when "style_said" is "", write "photorealistic cinematic live action".
- "music": the background music laid under the whole film as one track: "tags" (English: genre, mood, three or four instruments, and the word instrumental), "bpm" (a whole number from 50 to 180), "key" (one of: {keys}), "ko" (one Korean sentence).
- "scenes": about {count} scenes ({n_min} to {n_max}). Each scene: "seconds" (a whole number from 5 to 20; most scenes about 10), "place" (a place id), "characters" (the character ids in the shot), "continues" (true only when the shot follows the previous scene in the same place with no time jump, so its first frame is the previous scene's last frame), "shot" (shot size and one camera move, in English), "action" (the one main action of the shot in English, at most 35 words; show mood through the body), "sound" (the ambient sound and the sounds of the action in English; never music), "ko" (one Korean sentence for the user).
Rules:
- Honour every word of the request first; it is the top priority. Build a clear arc: an opening that sets the place, a development, and a closing image.
- The scene seconds add up to {seconds} (within {tolerance}).
- Vary the shot sizes (wide, medium, close) and keep the screen direction of a moving subject the same from scene to scene.
- A shot that the next scene continues must end with the main subject still fully in frame, because the next scene starts from its last frame: never let the subject walk out of frame, cross to the edge or disappear in such a shot; keep a moving subject in frame with a tracking camera, or let it walk toward or away from the camera.
- The video model draws touching and handling wrongly (bent fingers, doors that open twice, objects that pass through hands). Never write a close-up of hands, and never write an action where hands grip, touch, pick up, hand over, open, close, push, pull, put on, take off, fold, unfold or adjust anything (doors, gates, umbrellas, coats, keys, phones, cups). A person may hold an object that was already in the hand at the start (an umbrella while walking), but the shot must not show the hand or the handling. Show mood through the whole body, the walk, the face and the gaze, the weather and the place.
- A scene that continues the previous scene starts from that scene's last frame, so it must open in the same shot size and the same camera height; it may change the size only slowly during the shot (a slow push in or pull back), never jump from a close-up to a wide shot. Write the camera as height or angle, move and speed (for example "eye-level, slow dolly back at walking pace").
- The bible look of a character lists everything worn or carried that the audience sees (hair, face, glasses or none, coat, bag, umbrella). A scene must not add anything to a character that the bible look does not list (glasses, hats, scarves, beards, masks).
- Every scene can be shot from one still frame: one place, one main action, no cut inside it, no flashback, and no text, signs or logos.
- Name no brands, songs, artists or real people. English everywhere except "title_ko", "logline_ko", "ko" and bible "style_said", which copies the user's own words exactly (a studio or artist name in them included).
- A person must not be added when the request has none.
- Every scene that shows people must end with all of its listed characters still fully in frame and their faces visible (front or three-quarter view), because the next scene is built from this scene's last frame and takes the people's faces from it. Never end a shot on a back view, on a character walking out of frame or away into the distance, or on an empty place; show a departure by cutting to the next scene. Never let a character walk into a shot: every listed character is already in frame in the first moment."""
# bible.style_said (11-25 사용자 결정 "방식 바꾸기", 3차 명세 2절): 사용자가 화풍을 말한 말을 구성안 모델이 요청에서 그대로 옮겨 적는다
# ("" = 말하지 않음). 코드는 요청 글을 낱말로 짐작하지 않고 이 칸으로 화풍을 정한다 (검사 story_problems, 라이브러리 인물 cast_lib.apply_to_story).
# 화풍은 대사가 있든 없든 같은 기능이라 대사 영상 칸(DIALOGUE_SCHEMA·DIALOGUE_SCHEMA_SOLO)도 이 칸을 그대로 베낀다
STORY_SCHEMA = {"type": "object", "required": ["title_ko", "logline_ko", "bible", "music", "scenes"], "properties": {
    "title_ko": {"type": "string"}, "logline_ko": {"type": "string"},
    "bible": {"type": "object", "required": ["characters", "places", "time", "light", "style_said"], "properties": {
        "style_said": {"type": "string"},
        "style": {"type": "string"},
        "characters": {"type": "array", "items": {"type": "object", "required": ["id", "look"],
                                                  "properties": {"id": {"type": "string"}, "look": {"type": "string"}}}},
        "places": {"type": "array", "items": {"type": "object", "required": ["id", "look"],
                                              "properties": {"id": {"type": "string"}, "look": {"type": "string"}}}},
        "time": {"type": "string"}, "light": {"type": "string"}}},
    "music": {"type": "object", "required": ["tags", "bpm", "key", "ko"], "properties": {
        "tags": {"type": "string"}, "bpm": {"type": "integer"}, "key": {"type": "string"}, "ko": {"type": "string"}}},
    "scenes": {"type": "array", "items": {"type": "object",
                                          "required": ["seconds", "place", "characters", "continues", "shot", "action", "sound", "ko"],
                                          "properties": {"seconds": {"type": "integer"}, "place": {"type": "string"},
                                                         "characters": {"type": "array", "items": {"type": "string"}},
                                                         "continues": {"type": "boolean"}, "shot": {"type": "string"},
                                                         "action": {"type": "string"}, "sound": {"type": "string"},
                                                         "ko": {"type": "string"}}}}}}
CHAIN_MAX = 3     # 마지막 프레임으로 이어 쓰기는 연속 세 번까지 (색·선명도 변화가 쌓이는 것을 막는 [추정] 값)
# 요청에 사람이 있는지 (구성안 검사용). crew.PERSON_WORDS 에 없는 '남성'·'여성' 등을 더한다: 11-17 메인 첫 긴 영상에서
# "…걸어가는 남성을 30초 영상으로" 가 '사람 없는 요청'으로 읽혀 구성안을 세 번 고쳐 쓰고도 거짓 위반이 남았다.
# (crew 쪽 목록은 '남성 향수' 같은 제품 요청 때문에 그대로 둔다)
STORY_PERSON_KO_RE = re.compile(r"남성|여성|남자|여자|사람|인물|소년|소녀|어린이|아기|노인|할머니|할아버지|엄마|아빠|어머니|아버지"
                                r"|학생|커플|연인|부부|가족|친구|청년|주인공|군인|경찰|의사|선수|댄서|가수|배우|요리사|직원|손님|그녀")


# 대상이 화면 밖으로 나가는 말. 다음 장면이 이 장면의 마지막 프레임에서 시작하면 빈 화면에서 시작하게 된다
# (11-17 메인 첫 긴 영상: "static wide shot, walks left to right across the frame" → 마지막 프레임이 빈 거리 → 다음 장면이 사람 없이 시작)
EXIT_RE = re.compile(r"\b(?:exits?|exiting|leaves? (?:the )?(?:frame|shot|screen)|out of (?:the )?(?:frame|shot|view)|off[- ]?(?:screen|frame)"
                     r"|disappears?|vanish(?:es)?|across the (?:whole )?frame|to the (?:right|left|far) edge|reach(?:es)? the (?:right|left) edge)\b", re.I)


# 영상 모델이 물리를 자주 틀리는 접촉 동작. 11-18 3분 실측: 우산 손잡이를 쥔 손 클로즈업의 손가락, 두 문이 동시에 열린 채 문을 잡는 장면.
# 손으로 물건을 쥐거나 만지거나 주고받는 동작, 문·우산·옷을 여닫고 입고 벗는 동작은 구성안에서 쓰지 않는다 (몸과 얼굴, 걸음, 시선으로 감정을 보인다)
PHYSICS_RE = re.compile(
    r"\b(?:hands?|fingers?|palms?|knuckles?|grips?|gripping|grasps?|grasping|clutch(?:es|ing)?|fists?|wrists?)\b"
    r"|\b(?:opens?|opening|closes?|closing|shuts?|pushes?|pushing|pulls?|pulling|unlocks?|locks?|knocks?)\b[^.;]{0,25}\b(?:doors?|gates?|umbrella|window|zipper|coat|jacket|buttons?)\b"
    r"|\b(?:doors?|gates?)\b[^.;]{0,20}\b(?:opens?|opening|swings?|closes?|closing)\b"
    r"|\b(?:picks? up|pick(?:ing)? up|puts? down|putting down|hands? over|hold(?:s|ing) out|reach(?:es|ing) (?:out|for|into)|takes? (?:out|off)|puts? on|puts? away|pulls? out|unfold(?:s|ing)?|fold(?:s|ing)?|adjusts?|tightens?|buttons?|zips?)\b", re.I)


# 샷 크기 (작은 쪽부터). 이어지는 장면은 앞 장면 마지막 프레임에서 시작하므로 크기가 한 단계 넘게 뛰면 안 된다.
# 11-18 3분 실측: 얼굴 클로즈업 → 이어서 '미디엄 와이드' 장면이라 디졸브 구간에서 큰 점프가 보였다. 긴 이름을 먼저 찾는다.
# 투샷·그룹샷(두세 사람 대사 컷, 11-24)은 미디엄과 같은 크기. 'wide two-shot' 은 넓은 샷이라 wide 뒤에 둔다
SHOT_SIZES = (("extreme close", 0), ("extreme wide", 5), ("medium close", 1), ("medium wide", 3), ("medium long", 3), ("close-up", 0),
              ("close up", 0), ("closeup", 0), ("close shot", 0), ("full shot", 3), ("long shot", 4), ("wide", 4),
              ("two-shot", 2), ("two shot", 2), ("group shot", 2), ("group-shot", 2), ("medium", 2))
ACCESSORY_RE = re.compile(r"\b(?:glasses|spectacles|sunglasses|hats?|caps?|scarf|scarves|beards?|masks?|earrings?|necklaces?|gloves)\b", re.I)


def shot_rank(shot: str) -> int | None:
    t = (shot or "").lower()
    for name, rank in SHOT_SIZES:
        if name in t:
            return rank
    return None


def story_counts(seconds: float) -> tuple[int, int, int]:
    """(권장 장면 수, 최소, 최대). 장면은 대개 10초, 5~20초"""
    s = float(seconds)
    n_min = max(2, math.ceil(s / 15))
    n_max = max(n_min, math.ceil(s / 7))
    return max(n_min, min(n_max, round(s / 10))), n_min, n_max


def story_tolerance(seconds: float) -> int:
    return max(3, round(float(seconds) * 0.1))


def _s(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _sec(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and float(v).is_integer():
        return int(v)
    return None


def story_problems(data, seconds: float, person_ok: bool = True, library_style: str = "", now_style: str | None = None) -> list[str]:
    """구성안 검사. 고칠 말을 영어로 돌려준다 (모델에게 다시 보낸다). library_style 은 요청에 이름이 나온 라이브러리 인물의 그림체(없으면 ""):
    화풍 구절이 인물의 원래 그림체를 거절했을 때('원래 그림체 말고 다른 스타일로') 무엇을 거절했는지 안다 (fixC, cast_lib._phrase_refused).
    now_style 은 '지금 그림체' 가 가리키는 화풍: None 이면 요청이 한 메시지라 인물의 그림체, 글이면 고치기 차례에 앞 메시지로 만든 영상의 화풍
    ("" 은 모름, fixD)"""
    if not isinstance(data, dict):
        return ["Return one JSON object with title_ko, logline_ko, bible, music and scenes."]
    p: list[str] = []
    bible = data.get("bible") if isinstance(data.get("bible"), dict) else {}
    chars = {_s(c.get("id")): c for c in bible.get("characters") or [] if isinstance(c, dict) and _s(c.get("id"))}
    places = {_s(c.get("id")): c for c in bible.get("places") or [] if isinstance(c, dict) and _s(c.get("id"))}
    if not places:
        p.append('bible.places needs at least one place with an "id" and a "look".')
    for kind, group in (("character", chars), ("place", places)):
        for cid, c in group.items():
            look = _s(c.get("look"))
            if not look or HANGUL_RE.search(look):
                p.append(f'The {kind} "{cid}" needs an English "look".')
            elif len(look.split()) > 60:
                p.append(f'The {kind} "{cid}" look has {len(look.split())} words; keep it under 45.')
    if _s(bible.get("style")) and HANGUL_RE.search(_s(bible.get("style"))):
        p.append("bible.style must be an English sentence.")
    # 사용자의 화풍 구절(style_said)은 사용자 말 그대로(한국어도)라 영어 검사를 하지 않는다. 칸끼리만 본다 (3차 명세 2.4b): 구절이 말한 화풍을
    # bible.style 이 그리지 않았으면 고쳐 쓰게 한다. 칸이 없거나 "" 이면 보지 않는다 (요청 글은 읽지 않는다)
    # fixA: 다듬기와 같은 값(자리 채움 글 'none'·'없음' 은 "", 120자까지)을 본다 — 화풍 말이 120자 뒤에 있으면 검사와 덮어쓰기가 서로 다른 열쇠를
    # 보았다. "" 인데 bible.style 이 특정 화풍(애니·3D·custom)이면 칸끼리 어긋난다: 결과 뒤 고치기에서 앞 화풍을 지운 "" 가 라이브러리 화풍으로 덮였다
    said = bible.get("style_said")
    if isinstance(said, str):
        sd = cast_lib.said_text(said)
        if len(sd) > STYLE_SAID_MAX:
            p.append(f"bible.style_said has {len(sd)} characters: copy only the user's words for the look (at most {STYLE_SAID_MAX} characters).")
        sd = sd[:STYLE_SAID_MAX]
        st = _s(bible.get("style"))
        if sd and not cast_lib.style_said_fits(sd, st, library_style, now_style):
            p.append(f'bible.style must describe the look in bible.style_said ("{sd}"). '
                     + cast_lib.style_said_hint(sd, st, library_style, now_style))
        elif not sd and st and cast_lib.style_key(st) in ("anime", "toon3d", cast_lib.CUSTOM_KEY):
            # fixC: 거절만 한 요청('반실사 말고 다른 그림체로'·'지금 그림체 말고 다른 스타일로')도 앞 갈래다 — 예전 문구는 거절을 지켜 다른 화풍을 고른
            # 구성안을 '규칙이 주는 화풍'(라이브러리 반실사, 거절한 화풍)으로 돌려보냈다
            # fixD: 사용자의 가장 새 말로 한정한다 — 앞 메시지의 거절('반실사 말고 다른 그림체로') 뒤에 새 메시지가 '원래 그림체 그대로' 면 옮길 말이 없다
            p.append(f'bible.style_said is "", but bible.style describes a particular look ("{st[:80]}"): if the user\'s newest words about the '
                     'look (the newer message, when the request holds an earlier one) ask for this look or turn a look down (for example '
                     '"반실사 말고 다른 그림체로"), copy those words into bible.style_said; otherwise write the style the rules give when '
                     'bible.style_said is "".')
    for k in ("time", "light"):
        if not _s(bible.get(k)) or HANGUL_RE.search(_s(bible.get(k))):
            p.append(f'bible.{k} must be an English sentence.')
    for k in ("title_ko", "logline_ko"):
        if not HANGUL_RE.search(_s(data.get(k))):
            p.append(f'"{k}" must be Korean.')
    music = data.get("music") if isinstance(data.get("music"), dict) else {}
    tags = _s(music.get("tags"))
    if not tags or HANGUL_RE.search(tags):
        p.append('music.tags must be English genre, mood and instrument words.')
    bpm = _sec(music.get("bpm"))
    if bpm is None or not 50 <= bpm <= 180:
        p.append("music.bpm must be a whole number from 50 to 180.")
    if _s(music.get("key")) not in MUSIC_KEYS:
        p.append(f'music.key must be one of: {", ".join(MUSIC_KEYS)}.')
    scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
    _, n_min, n_max = story_counts(seconds)
    if not n_min <= len(scenes) <= n_max:
        p.append(f"Write {n_min} to {n_max} scenes (you wrote {len(scenes)}).")
    total, prev_place = 0, None
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            p.append(f"Scene {i} must be an object.")
            continue
        sec = _sec(sc.get("seconds"))
        if sec is None or not STORY_SCENE_MIN <= sec <= STORY_SCENE_MAX:
            p.append(f'Scene {i}: "seconds" must be a whole number from {STORY_SCENE_MIN} to {STORY_SCENE_MAX}.')
        total += sec or 0
        place = _s(sc.get("place"))
        if place not in places:
            p.append(f'Scene {i}: place "{place}" is not in bible.places.')
        for c in sc.get("characters") or []:
            if _s(c) not in chars:
                p.append(f'Scene {i}: character "{_s(c)}" is not in bible.characters.')
        if sc.get("continues") is True and (i == 1 or place != prev_place):
            p.append(f"Scene {i} cannot continue: " + ("it is the first scene." if i == 1 else "it is in a different place from the previous scene."))
        nxt = scenes[i] if i < len(scenes) else None          # i 는 1부터라 scenes[i] 가 다음 장면이다
        if isinstance(nxt, dict) and nxt.get("continues") is True and EXIT_RE.search(_s(sc.get("action")) + " " + _s(sc.get("shot"))):
            p.append(f"Scene {i + 1} continues scene {i}, so scene {i} must end with the subject still in frame, but its action "
                     f'or shot lets it leave ("{EXIT_RE.search(_s(sc.get("action")) + " " + _s(sc.get("shot"))).group(0)}"): '
                     "keep the subject in frame with a tracking camera or a walk toward or away from the camera.")
        if sc.get("continues") is True and i > 1 and isinstance(scenes[i - 2], dict):
            a, b = shot_rank(_s(scenes[i - 2].get("shot"))), shot_rank(_s(sc.get("shot")))
            if a is not None and b is not None and abs(a - b) > 1:
                p.append(f"Scene {i} continues scene {i - 1}, so it starts from that last frame, but its shot size jumps "
                         f'from "{_s(scenes[i - 2].get("shot"))}" to "{_s(sc.get("shot"))}". Open scene {i} in the same shot size as scene {i - 1} '
                         "(or one step away) and change the size only with a slow camera move, or make scene " + str(i) + " a new scene (continues false).")
        seen_looks = " ".join(_s(chars[c].get("look")) for c in (sc.get("characters") or []) if _s(c) in chars).lower()
        add = sorted({m.group(0).lower() for m in ACCESSORY_RE.finditer(_s(sc.get("action")) + " " + _s(sc.get("shot")))
                      if m.group(0).lower() not in seen_looks and m.group(0).lower().rstrip("s") not in seen_looks})
        if add and sc.get("characters"):
            p.append(f"Scene {i} gives the character {', '.join(add)}, which the bible look does not list; add it to the character's look "
                     "in the bible (so every scene has it) or remove it from the scene.")
        mt = PHYSICS_RE.search(_s(sc.get("action")) + " " + _s(sc.get("shot")))
        if mt:
            p.append(f'Scene {i}: "{mt.group(0)}" is a hand or handling action the video model draws wrongly. Rewrite the shot so no hands, '
                     "doors, umbrellas, coats or objects are touched, opened, closed or moved; show the mood with the whole body, the walk, the face or the place.")
        for k in ("shot", "action", "sound"):
            v = _s(sc.get(k))
            if not v:
                p.append(f'Scene {i}: "{k}" is empty.')
            elif HANGUL_RE.search(v):
                p.append(f'Scene {i}: "{k}" must be English.')
        if len(_s(sc.get("action")).split()) > 45:
            p.append(f'Scene {i}: "action" is too long; keep it under 35 words.')
        if music_words(_s(sc.get("sound")), _s(sc.get("action")) + " " + _s(sc.get("shot"))):
            p.append(f'Scene {i}: "sound" must not name music; the music is one separate track '
                     "(an instrument played on screen is fine when the action shows it).")
        if not HANGUL_RE.search(_s(sc.get("ko"))):
            p.append(f'Scene {i}: "ko" must be a Korean sentence.')
        if not person_ok and PERSON_NOTE_RE.search(_s(sc.get("action")) + " " + _s(sc.get("shot"))):
            p.append(f"Scene {i} adds a person, but the request has none.")
        prev_place = place
    if not person_ok and any(PERSON_NOTE_RE.search(_s(c.get("look"))) for c in chars.values()):
        p.append("The bible adds a person, but the request has none.")
    tol = story_tolerance(seconds)
    if scenes and abs(total - float(seconds)) > tol:
        p.append(f"The scene seconds add up to {total}; make them add up to {int(seconds)} (within {tol}).")
    return p[:14]


STYLE_SAID_MAX = 120     # 사용자 화풍 구절(bible.style_said)을 남기는 글자 수 (다듬기·검사가 같은 값을 본다)
# 구성안이 화풍을 비웠을 때의 화풍 (STORY_SYSTEM 의 기본값과 같다): 정지 그림 지시문(style_key 는 빈 글을 반실사로 읽는다)과 기준 그림·영상 글
# (story_style 은 빈 글을 실사로 쓴다)이 부딪히지 않게 다듬을 때 채운다 (11-24 3차 검토)
DEFAULT_STYLE = "photorealistic cinematic live action"


def clean_story(data, seconds: float) -> dict | None:
    """구성안을 다듬는다. 길이 합이 조금 어긋나면 장면 길이를 고르게 고친다. 쓸 수 없는 모양이면 None"""
    if not isinstance(data, dict):
        return None
    bible = data.get("bible") if isinstance(data.get("bible"), dict) else {}
    chars = [{"id": _s(c.get("id")), "look": _s(c.get("look"))} for c in bible.get("characters") or []
             if isinstance(c, dict) and _s(c.get("id")) and _s(c.get("look"))]
    places = [{"id": _s(c.get("id")), "look": _s(c.get("look"))} for c in bible.get("places") or []
              if isinstance(c, dict) and _s(c.get("id")) and _s(c.get("look"))]
    if not places:
        return None
    char_ids, place_ids = {c["id"] for c in chars}, {c["id"] for c in places}
    scenes, prev = [], None
    for sc in data.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        sec = _sec(sc.get("seconds")) or 10
        item = {"seconds": max(STORY_SCENE_MIN, min(STORY_SCENE_MAX, sec)),
                "place": _s(sc.get("place")) if _s(sc.get("place")) in place_ids else places[0]["id"],
                "characters": [_s(c) for c in sc.get("characters") or [] if _s(c) in char_ids],
                "continues": False, "shot": _s(sc.get("shot")), "action": _s(sc.get("action")),
                "sound": _s(sc.get("sound")), "ko": _s(sc.get("ko"))}
        if not (item["shot"] and item["action"] and item["ko"]) or HANGUL_RE.search(item["shot"] + item["action"] + item["sound"]):
            return None
        item["continues"] = bool(sc.get("continues") is True and prev is not None and prev["place"] == item["place"]
                                 and not EXIT_RE.search(prev["action"] + " " + prev["shot"]))    # 앞 장면이 대상을 내보내면 잇지 않는다
        scenes.append(item)
        prev = item
    if len(scenes) < 2:
        return None
    _fit_seconds(scenes, seconds)
    music = data.get("music") if isinstance(data.get("music"), dict) else {}
    key = _s(music.get("key"))
    bpm = _sec(music.get("bpm"))
    story = {"title_ko": _s(data.get("title_ko"))[:60], "logline_ko": _s(data.get("logline_ko"))[:300],
             "bible": {"characters": chars, "places": places, "time": _s(bible.get("time")), "light": _s(bible.get("light")),
                       "style": _s(bible.get("style"))[:240] or DEFAULT_STYLE},
             "music": {"tags": _s(music.get("tags"))[:400] or "calm cinematic instrumental, soft piano, warm strings",
                       "bpm": bpm if bpm and 50 <= bpm <= 180 else 90, "key": key if key in MUSIC_KEYS else "C major",
                       "ko": _s(music.get("ko"))[:200]},
             "scenes": scenes}
    if isinstance(bible.get("style_said"), str):
        # 사용자의 화풍 구절 (3차 명세 2.3): 칸이 없는 구성안(예전 경로, 요청 글을 읽는다)과 ""(화풍을 말하지 않음)를 가르려고 칸이 있을 때만 남긴다.
        # 자리 채움 글('none'·'없음'·'N/A')은 "" (fixA, cast_lib.said_text — 검사와 같은 값)
        story["bible"]["style_said"] = cast_lib.said_text(bible["style_said"])[:STYLE_SAID_MAX]
    story["seconds"] = sum(s["seconds"] for s in scenes)
    _mark_chains(scenes)
    return story


def _fit_seconds(scenes: list[dict], seconds: float):
    """장면 길이 합을 목표에 맞춘다: 차이를 장면마다 1초씩 나눠 더하거나 뺀다 (5~20초 안에서)"""
    target = int(round(float(seconds)))
    for _ in range(400):
        diff = target - sum(s["seconds"] for s in scenes)
        if diff == 0:
            return
        step = 1 if diff > 0 else -1
        moved = False
        for s in sorted(scenes, key=lambda x: x["seconds"], reverse=step < 0):
            if STORY_SCENE_MIN <= s["seconds"] + step <= STORY_SCENE_MAX:
                s["seconds"] += step
                moved = True
                break
        if not moved:
            return


def _mark_chains(scenes: list[dict]):
    """이어 쓰기가 CHAIN_MAX 번 연속되면 그 장면은 앞 프레임을 같은 구도로 다시 그려 시작한다 (refresh, 11-20 설계: 색·선명도가 쌓이는 것을 되돌린다)"""
    run = 0
    for s in scenes:
        if s["continues"]:
            run += 1
            if run > CHAIN_MAX:
                s["continues"] = False
                s["refresh"] = True
                run = 0
        else:
            run = 0


# 장면 이음 종류 (11-20 설계, docs/scene-chaining-design.md 2·7절): 모든 장면은 앞 장면의 마지막 프레임에서 시작한다
SEAM_CONTINUE, SEAM_CUT, SEAM_JUMP, SEAM_REFRESH = "continue", "cut", "jump", "refresh"
DISSOLVE_SEAMS = (SEAM_CONTINUE, SEAM_JUMP, SEAM_REFRESH)      # 하드 컷은 cut 하나뿐이다 (사용자 11-20: 장소·시간이 바뀌는 컷은 디졸브)


def scene_seam(story: dict, i: int) -> str | None:
    """장면 i 가 앞 장면에 이어지는 방식. 첫 장면은 None.
    continue = 같은 샷이 그대로 이어짐(끝 프레임이 곧 첫 화면), refresh = 이어짐이 CHAIN_MAX 를 넘어 같은 구도를 다시 그림,
    jump = 장소나 시간이 바뀜, cut = 같은 장소·시간의 새 카메라 샷"""
    if i <= 0:
        return None
    sc, prev = story["scenes"][i], story["scenes"][i - 1]
    if sc.get("continues"):
        return SEAM_CONTINUE
    if sc.get("refresh") and sc.get("place") == prev.get("place"):
        return SEAM_REFRESH
    if sc.get("place") != prev.get("place") or (sc.get("time") and sc.get("time") != prev.get("time")):
        return SEAM_JUMP
    return SEAM_CUT


def scene_seams(story: dict) -> list[str]:
    """이음 종류 목록 (장면 수 - 1 개). 합치기(assemble)가 이음마다 디졸브나 하드 컷을 정한다"""
    return [scene_seam(story, i) for i in range(1, len(story["scenes"]))]


def story_style(story: dict) -> str:
    """사실적이지 않은 화풍 한 줄 (애니메이션 등). 사진·실사면 빈 글: 기존 영상 글은 그대로 쓴다.
    실사 판정은 화풍 열쇠와 같은 함수(cast_lib.is_photo_style)로 한다 (11-24: 정규식이 둘이라 서로 어긋났다)"""
    st = _s(((story or {}).get("bible") or {}).get("style"))
    return "" if not st or cast_lib.is_photo_style(st) else st


# custom 화풍인데 사진 쪽인 글 (누아르·흑백 무성영화·realistic …): custom 은 무엇이 아닌지 모르므로(C2) '사진 아님'·'그린 그림' 문장을 붙이지 않는다
# (11-24 3차 검토: 실사 요청을 따른 'hyper-realistic cinematic look'·흑백 무성영화에 'never photographic realism' 이 붙었다).
# 4차 검토: 낱말 경계가 없어 'lively'·'olive'·'surrealist' 가, 부정 구절을 지우지 않아 'not photorealistic'·'not a photograph' 가 손그림 화풍을 사진
# 쪽으로 만들었다 → '아닌 것' 구절을 지우고 낱말 경계로 본다
_PHOTO_LIKE_RE = re.compile(r"\b(?:noir|silent\s+(?:film|movie)s?|black[\s-]*and[\s-]*white\s+(?:film|movie|photo\w*|footage)|footage"
                            r"|live[\s-]*action|cinematograph\w*|photograph\w*|photo|35\s*mm)\b"
                            r"|(?<!semi-)(?<!semi )\b(?:realism|realistic)\b|흑백|무성\s*영화|누아르|실사", re.I)


def photo_like_text(style: str) -> bool:
    """화풍 글이 custom 이고 사진 쪽 말이 든 글인가 ('아닌 것' 구절은 지우고 본다)"""
    st = _s(style)
    return bool(st) and cast_lib.style_key(st) == "custom" and bool(_PHOTO_LIKE_RE.search(cast_lib._NOT_RE.sub(" ", st)))


def photo_like(story: dict) -> bool:
    """영화 화풍이 custom 이고 사진 쪽 말이 든 글인가 (그러면 '사진 아님' 문장을 붙이지 않는다)"""
    return photo_like_text(story_style(story))


def scene_notes(story: dict, i: int) -> str:
    """장면 하나의 '지킬 것' (영어): 설정집 + 이 장면의 카메라·동작·소리. 영상 렌더러·연출 계획표에 넘긴다.
    앞뒤 장면은 넣지 않는다: 이 글은 '반드시 지킬 말' 검사(checked_demands)에도 쓰여, 실내 고정 장면에 앞 장면의 비·다음 장면의
    빠른 줌을 요구하게 된다 (11-17 검토)"""
    sc = story["scenes"][i]
    n = len(story["scenes"])
    b = story["bible"]
    looks = {c["id"]: c["look"] for c in b["characters"]}
    place = next((p["look"] for p in b["places"] if p["id"] == sc["place"]), "")
    lines = [f"This shot is scene {i + 1} of {n} in one film of about {story['seconds']} seconds, joined to the others with hard cuts. "
             "Keep these fixed looks exactly:"]
    for cid in sc["characters"]:
        if cid in looks:
            lines.append(f"- {cid}: {looks[cid]}")
    lines.append(f"- Place: {place}")
    lines.append(f"- Time: {b['time']} Light: {b['light']}")
    if story_style(story):
        lines.append(f"- Style: {story_style(story)} Keep this look in every frame" + (", never photographic realism." if not photo_like(story) else "."))
    lines.append(f"This shot: {sc['shot']}. Action: {sc['action']}")
    if sc.get("sound"):
        lines.append(f"Sound: {sc['sound']}")
    if i + 1 < n and story["scenes"][i + 1].get("continues"):
        # 다음 장면이 이 장면의 마지막 프레임에서 시작한다: 끝에 대상이 화면 안에 있어야 한다 (11-17 메인)
        lines.append("The next shot begins from this shot's last frame: keep the main subject fully visible in frame until "
                     "the very last frame, with the camera following it if it moves.")
    return "\n".join(lines)[:1990]


def scene_direction(story: dict, i: int, prev: dict | None = None) -> str:
    """연출 계획표를 짜는 모델에게 주는 앞뒤 장면·이음새 정보 (영어). 영상 렌더러의 notes 에는 넣지 않는다
    ('반드시 지킬 말' 검사에 쓰여 이웃 장면의 말이 요구가 되기 때문). prev 는 앞 장면의 계획표(마지막 프레임 글을 가져온다)"""
    scenes = story["scenes"]
    sc = scenes[i]
    lines = []
    if sc.get("continues") and i > 0:
        last = ((prev or {}).get("ending") or ((prev or {}).get("acting") or [""])[-1] or "").strip()
        lines.append(f"This clip's first frame IS the last frame of the previous clip (scene {i}: {scenes[i - 1]['action']}"
                     + (f" It ended with: {last}." if last else "") + "). Continue the same moment: the opening beat keeps the same shot size, "
                     "camera height, subject position and pose as that frame; change the size only with a slow camera move.")
    elif i > 0:
        lines.append(f"This clip starts a new scene after a cut. The previous scene was: {scenes[i - 1]['action']} (do not repeat its action).")
    if i + 1 < len(scenes):
        nxt = scenes[i + 1]
        if nxt.get("continues"):
            lines.append("The NEXT clip starts from this clip's last frame, so \"ending\" must show the main subject fully in frame, "
                         "standing or walking (never sitting or lying), the camera settled, and the face visible (front or three-quarter) "
                         f"when a person is in the shot. The next scene is: {nxt['action']}")
        else:
            lines.append(f"The next scene (after a cut) is: {nxt['action']}")
    lines.append(f"Scene {i + 1} of {len(scenes)}; the camera keeps one steady height and speed unless the shot says otherwise.")
    return " ".join(lines)[:1800]


def scene_has_person(story: dict, i: int) -> bool:
    """이 장면에 설정집의 사람(동물이 아닌)이 나오는가. 이어 받은 첫 화면에 사람이 없으면 새 장면 이미지로 바꾸는 데 쓴다"""
    looks = {c["id"]: c["look"] for c in story["bible"]["characters"]}
    return any(PERSON_NOTE_RE.search(looks.get(c, "")) for c in story["scenes"][i].get("characters") or [])


def scene_image_text(story: dict, i: int, plan: dict | None = None, limit: int = 1750) -> str:
    """장면 이미지(첫 화면)용 글: 장면 한 줄 + 설정집 + 카메라·동작의 첫 순간 + 연출 계획표의 장소·빛.
    이미지 확장 입력은 2000자까지라, 감독이 뒤에 붙이는 장면 힌트(약 200자) 자리를 남겨 limit 안으로 줄인다"""
    sc = story["scenes"][i]
    b = story["bible"]
    looks = {c["id"]: c["look"] for c in b["characters"]}
    place = next((p["look"] for p in b["places"] if p["id"] == sc["place"]), "")
    who = " ".join(f"{looks[c]}." for c in sc["characters"] if c in looks)
    style = story_style(story)
    base = (f"{sc['ko']}\nThe first instant of this shot as one still {'frame' if style else 'photograph'}: {sc['shot']}. {sc['action']} "
            f"{who} Place: {place} Time: {b['time']} Light: {b['light']}" + (f" Style: {style}" if style else ""))
    extra = (" " + plan_scene_text(plan)) if plan else ""
    text = base + extra
    return text if len(text) <= limit else base[:limit]


def film_look(story) -> str:
    """앞 메시지로 만든 긴 영상의 화풍 글 (고치기 차례의 '지금 그림체', fixD): 구성안의 bible.style, 비었으면 기본 실사 글. 구성안이 없으면(짧은
    영상·아직 못 씀) "" — 모른다"""
    if not isinstance(story, dict) or not story.get("scenes"):
        return ""
    bible = story.get("bible") if isinstance(story.get("bible"), dict) else {}
    return _s(bible.get("style"))[:300] or "photorealistic cinematic live action"


def now_look_note(now_style: str | None) -> str:
    """구성안 요청 글에 붙이는 앞 영상의 화풍 한 줄 (고치기 차례이고 그 화풍을 알 때만, fixD): 새 메시지의 '지금 그림체' 가 무엇인지 알려 준다"""
    if not now_style:
        return ""
    return (f"The film made from the earlier message has this look: {_s(now_style)[:300]}. Words in the newer message such as "
            '"지금 그림체" or "현재 스타일" mean that look.\n')


def _known_person(known: list | None) -> bool:
    """요청에 이름이 나온 라이브러리 인물(known) 가운데 사람이 있는가: '니키가 …' 처럼 사람 낱말 없이 이름만 쓴 요청도 사람이 나오는 요청이다
    (fixA: 니키를 빼지 않고는 고칠 수 없는 'The bible adds a person' 위반이 고쳐 쓰기 차례를 헛돌렸다)"""
    return any(isinstance(e, dict) and not is_animal_look(_s(e.get("look"))) for e in known or [])


# 구성안 차례의 문맥 길이 (fixD, 11-25 좁은 확인): 고쳐 쓰기 차례는 지시문·요청 + 앞 답(빈칸 없는 JSON) + 그 답의 문제 + 이번 답이다. 3분 대사
# 구성안은 장면이 22~30개가 흔해(대사 15~22줄, 대사 장면 초 = 글자/6+1) 장면 글이 보통 길이여도 이 합이 12.9K~16.5K 로 HELPER_NUM_CTX(12288)를
# 넘었다 (fixC 는 장면 글이 짧은 18장면으로 쟀다). 같은 GPU 차례 안의 호출은 같은 num_ctx 를 써야 모델을 다시 올리지 않으므로, 차례를 시작할 때
# 그 요청이 쓸 수 있는 가장 긴 구성안(장면 수 상한 n_max, 장면 글이 낱말 상한 가까이)으로 한 번 정해 그 차례의 모든 호출에 준다.
# 값은 Qwen 토크나이저 근사로 잰 것(fixD 탐침 calib.py)에 여유를 둔 것이다
STORY_CTX_SCENE_OUT = 260     # 이번 답(들여쓴 JSON)의 대사 영상 장면 하나: 실측 보통 길이 213, 낱말 상한 가까이 255
STORY_CTX_SCENE_IN = 200      # 앞 답(빈칸 없는 JSON)의 대사 영상 장면 하나: 실측 153 / 195
STORY_CTX_PLAIN_OUT = 170     # 대사 없는 영상 장면 하나 (대사·듣는 사람·present 칸이 없다): 실측 138 / 159
STORY_CTX_PLAIN_IN = 130      # 같은 것, 빈칸 없음: 실측 103 / 124
STORY_CTX_FRAME_OUT = 1200    # 장면 밖(title·logline·bible·music), 들여씀: 인물 3명 실측 708 — 인물이 더 많아도 들게
STORY_CTX_FRAME_IN = 900      # 같은 것, 빈칸 없음: 실측 574
STORY_CTX_PROBLEMS = 2600     # 고쳐 쓰기 문구 (16개까지, 가장 긴 문구 실측 123 → 16 × 160)
STORY_CTX_STEP = 1024


def approx_tokens(text) -> int:
    """Qwen 토큰 수를 넉넉히 어림한다: ASCII 3글자에 1토큰, 그 밖의 글자(한글 등)는 1글자에 1토큰 (근사 토크나이저로 잰 구성안 지시문보다
    약 1.4배, 구성안 JSON 보다 약 1.2배)"""
    t = str(text or "")
    a = sum(1 for ch in t if ord(ch) < 128)
    return math.ceil(a / 3) + (len(t) - a)


def story_num_ctx(base: list, n_max: int, dialogue: bool = True, extra: int = 0) -> int | None:
    """구성안 차례(write_story·write_dialogue_story)의 num_ctx: 처음 messages(base) + 앞 답(빈칸 없음) + 문구 + 이번 답(들여씀), 장면 n_max 개 기준,
    1024 단위로 올린다 (fixD). dialogue 가 거짓이면 대사 없는 영상의 장면 어림. HELPER_NUM_CTX 보다 작지 않다. HELPER_NUM_CTX 가 0 이면
    (Ollama 기본값을 쓰라는 설정) None — 주지 않는다. extra 는 base 에 아직 없는 글의 예약분 (영화 빛 계획, LOOK_PLAN_RESERVE)"""
    if settings.helper_num_ctx <= 0:
        return None
    n = max(1, int(n_max))
    s_in, s_out = (STORY_CTX_SCENE_IN, STORY_CTX_SCENE_OUT) if dialogue else (STORY_CTX_PLAIN_IN, STORY_CTX_PLAIN_OUT)
    need = (sum(approx_tokens(m.get("content")) + 4 for m in base) + 2 * 4 + max(0, int(extra))
            + STORY_CTX_FRAME_IN + n * s_in + STORY_CTX_PROBLEMS + STORY_CTX_FRAME_OUT + n * s_out)
    return max(settings.helper_num_ctx, -(-need // STORY_CTX_STEP) * STORY_CTX_STEP)


def story_options(base: list, n_max: int, dialogue: bool = True, num_ctx: int | None = None) -> dict:
    """구성안 차례의 모든 호출에 같은 값으로 주는 options (온도 0.7 + story_num_ctx). num_ctx 를 주면 그 값 (영화 제작진 초안과
    같은 차례에서 같은 값을 쓰려고 storyboard 가 미리 정한 값, 11-26)"""
    ctx = num_ctx or story_num_ctx(base, n_max, dialogue)
    return {"temperature": 0.7, **({"num_ctx": ctx} if ctx else {})}


# 영화 빛 계획(정본 장면 제작진, 11-26)을 구성안 요청 글에 붙일 때의 예약 토큰. 계획은 crew.LOOK_PLAN_MAX(600자) 안이라 넘지 않는다
LOOK_PLAN_RESERVE = 300


def look_plan_note(look_plan: str) -> str:
    """구성안 요청 글에 붙이는 영화 빛 계획. 'Must-keep details from the user' 에는 넣지 않는다: 모델이 style_said 로 옮기면
    apply_to_story 가 라이브러리 화풍을 바꾼다"""
    if not look_plan:
        return ""
    return ("Look plan from the crew (not the user's words; never copy it into style_said; the user's words and the known "
            f"characters come first): {look_plan}. Write bible.time and bible.light from it.\n")


def repair_turn(base: list, content: str, problems: list[str], data=None) -> list:
    """구성안 고쳐 쓰기 차례에 보낼 messages: 처음 messages(지시문·요청) + 마지막 답 + 그 답의 문제 (fixC). 앞 차례의 답과 문구를 모두 쌓으면
    3분 대사 구성안의 세 번째 차례가 계속 길어졌다. 모델은 마지막 구성안과 그 구성안의 지금 문제를 모두 본다 (앞 문제는 고쳤거나 지금 문제에
    다시 나온다). 마지막 답을 읽었으면(data) 같은 내용을 빈칸 없는 JSON 으로 보낸다 (들여쓴 답보다 장면마다 약 60토큰 적다).
    이것만으로는 3분 대사 구성안(22~30장면)이 HELPER_NUM_CTX(12288)에 들지 않아, 그 차례의 문맥 길이는 story_num_ctx 로 따로 정한다 (fixD)"""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if isinstance(data, (dict, list)) else (content or "{}")
    return list(base) + [{"role": "assistant", "content": text}, repair_message(problems)]


def story_base(text: str, notes: str, seconds: float, known: list | None = None, now_style: str | None = None,
               look_plan: str = "") -> tuple[list, int]:
    """대사 없는 영상 구성안의 처음 messages 와 장면 수 상한 (write_story 와 storyboard 의 문맥 길이 어림이 같은 글을 본다)"""
    count, n_min, n_max = story_counts(seconds)
    said = f"{text or ''} {notes or ''}"
    system = STORY_SYSTEM.format(seconds=int(seconds), keys=", ".join(MUSIC_KEYS), count=count, n_min=n_min, n_max=n_max,
                                 tolerance=story_tolerance(seconds)) + cast_lib.prompt_block(known, speech=False, by_id=True, text=said)
    user = (f"Request (Korean): {text}\n" + (f"Must-keep details from the user: {notes}\n" if notes else "") + now_look_note(now_style)
            + look_plan_note(look_plan))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], n_max


async def write_story(call, text: str, notes: str, seconds: float, rounds: int = 3,
                      known: list | None = None, now_style: str | None = None, look_plan: str = "",
                      num_ctx: int | None = None) -> tuple[dict | None, list[str], int]:
    """구성안을 같은 LLM 차례 안에서 쓴다. (구성안, 남은 위반, 쓴 횟수). 위반이 남아도 다듬어 쓸 수 있으면 돌려준다.
    known 은 요청에 이름이 나온 라이브러리 인물: 대사가 없는 영상도 같은 모습·그림체로 쓰게 한다 (11-20, 말투는 뺀다).
    now_style 은 고치기 차례의 앞 영상 화풍 (None 이면 한 메시지 요청, fixD): 요청 글에 알려 주고 화풍 구절 검사가 '지금 그림체' 로 쓴다.
    look_plan 은 영화 빛 계획(정본 장면 제작진), num_ctx 는 storyboard 가 같은 차례의 제작진 초안과 맞춘 문맥 길이 (11-26)"""
    said = f"{text or ''} {notes or ''}"
    person_ok = bool(PERSON_REQ_RE.search(said)) or bool(STORY_PERSON_KO_RE.search(said)) or _known_person(known)
    lib_style = cast_lib.library_style(known)
    base, n_max = story_base(text, notes, seconds, known, now_style, look_plan)
    messages = base
    options = story_options(base, n_max, dialogue=False, num_ctx=num_ctx)          # 한 차례 안의 호출은 같은 num_ctx (fixD)
    best, problems, rnd = None, ["no answer"], 0
    for rnd in range(1, rounds + 1):
        reply = await call({"messages": messages, "format": STORY_SCHEMA, "think": False, "options": dict(options)})
        content = (reply.get("message") or {}).get("content", "")
        try:
            data = parse_json_reply(content)
        except PromptxError as e:
            data, problems = None, [str(e)]
        else:
            problems = story_problems(data, seconds, person_ok, lib_style, now_style)
        story = clean_story(data, seconds) if data is not None else None
        if story is not None and (best is None or len(problems) < len(best[1])):
            best = (story, problems)
        if not problems:
            break
        messages = repair_turn(base, content, problems, data)
    if best is None:
        return None, problems, rnd
    return best[0], best[1], rnd


async def storyboard(user, ip: str, username: str, slug: str, text: str, notes: str = "", seconds=None,
                     dialogue: bool = False, duo: bool | None = None, now_style: str | None = None) -> dict:
    """긴 영상 구성안 (감독이 계획 카드 전에 한 번). dialogue 면 대사 영상 구성안. 못 쓰면 PromptxError.
    duo 는 감독이 정한 '이 요청에서 두세 사람 대사 컷을 만들 수 있는가' (대사 영상만, None 이면 설정 STUDIO_DUO_TALK).
    now_style 은 고치기 차례(앞 요청 + 이번 말)에 앞 메시지로 만든 영상의 화풍 (film_look, "" 은 모름). None 이면 요청이 한 메시지다 (fixD:
    고치기 차례의 '지금 그림체' 는 방금 본 영상의 화풍인데 늘 라이브러리 그림체로 읽었다)"""
    from . import llm

    text = clean_input(text)
    notes = str(notes or "").strip()[:2000]
    sec = max(STORY_MIN_SECONDS, min(STORY_MAX_SECONDS, float(seconds or 60)))
    # 요청에 이름이 나온 정해진 인물 (니키·푸토르 등). 11-20: 대사가 있든 없든 같은 모습·그림체여야 해서 대사 없는 긴 영상에도 쓴다
    known = cast_lib.known_in(f"{text} {notes}")
    # 정본 인물 영화의 빛·시간·색·대기·분위기 초안 (11-26, CREW_CAST_MODE=safe). 같은 차례 안에서 구성안보다 먼저 받고, 문맥 길이는
    # 구성안과 같은 값을 쓴다 (Ollama 는 num_ctx 가 바뀌면 모델을 다시 올린다)
    film_on = crew_mod.film_crew_on(text, notes)
    ctx = None
    if film_on:
        base, n_max = (dialogue_base(text, notes, sec, known, duo, now_style) if dialogue
                       else story_base(text, notes, sec, known, now_style))
        ctx = story_num_ctx(base, n_max, dialogue, extra=LOOK_PLAN_RESERVE)
    film = None
    try:
        async with llm.llm_turn(user, ip, settings.expand_model, "video_story") as call:
            if film_on:
                film = await crew_mod.draft_film(call, text, notes, num_ctx=ctx)
            plan = crew_mod.look_plan_text((film or {}).get("look"))
            if dialogue:
                story, left, rounds = await write_dialogue_story(call, text, notes, sec, known=known, duo=duo, now_style=now_style,
                                                                 look_plan=plan, num_ctx=ctx)
            else:
                story, left, rounds = await write_story(call, text, notes, sec, known=known, now_style=now_style,
                                                        look_plan=plan, num_ctx=ctx)
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    if story is None:
        raise PromptxError("구성안을 만들지 못했습니다: " + "; ".join(left[:3]), 502)
    cast = cast_lib.apply_to_story(story, known, f"{text} {notes}", keep_style=bool(story_style(story)), now_style=now_style)
    if film and film.get("look"):
        add_crew_look(story, film["look"])
    return {"story": story, "violations": left, "rounds": rounds, "model": settings.expand_model, "cast": cast,
            "film_crew": {k: film.get(k) for k in ("category", "cast", "used", "rounds", "picked", "violations")} if film else None}


FILM_LIGHT_MAX = 320     # 영화 빛 계획을 더한 bible.light 상한: 정지 그림 지시문이 넘치면 compose 끝 문장(손·얼굴)부터 잘린다


def add_crew_look(story: dict, look: dict) -> None:
    """영화 빛 계획을 구성안에 남긴다 (story.crew: 장소 그림의 Air·사람 없는 컷의 빛 계획). 모델이 bible.light 에 고른 조명을
    옮기지 않았으면 조명 문장을 덧붙인다 (FILM_LIGHT_MAX 안에서, 덧붙인 문장이 잘리지 않게 앞 글을 줄인다)"""
    story["crew"] = look
    b = story.get("bible") or {}
    light = _s(b.get("light"))
    add = _s(look.get("light"))
    if add and not crew_lib.has_term(light, look.get("light_terms") or []):
        room = FILM_LIGHT_MAX - len(add) - 2
        light = (light[:max(0, room)].rstrip(" ,;.") + ", " + add) if light and room > 20 else add
    b["light"] = light[:FILM_LIGHT_MAX]


# ── 대사 영상 구성안 (11-19, docs/short-film-dialogue-design.md 12절) ──
# 숲길 단편(11-19)에서 손으로 확인한 방식: 목소리를 먼저 녹음하고(인물마다 목소리 하나로 고정), 말하는 장면은 그 사람만 화면에 두고
# 대사 소리로 입을 맞추고(LTX-2.3 IA2V), 나머지 장면은 긴 영상과 같이 만든 뒤 디졸브·대사·자막·음악으로 합친다.
DIALOGUE_EXTRA = """
This film has spoken dialogue in Korean. The voices are recorded first, one fixed voice per character, and each speaking shot is lip-synced to its recorded line. Follow these extra rules:
- Every character who speaks also has "name_ko" (the Korean name or role word, for example "니키" or "할아버지"), "voice" (an English description for a voice designer: age, gender, pitch, timbre, pace and manner, at most 40 words; never name a real person, actor or famous voice), "voice_ko" (a short Korean description of the voice) and "sample_ko" (two short Korean sentences, 20 to 40 characters in all, that this character would really say, in the character's own manner of speaking and age; it is recorded once to fix the voice).
- Each scene also has "kind", "speaker", "line_ko" and "emotion":
  - "talk": one character speaks one line to someone off-screen. "characters" is exactly that one character and "speaker" is its id. The shot is a close-up or medium close-up of the speaker alone in the frame, the face in three-quarter view toward the listener, with a still or very slowly pushing camera. "action" describes the face, the eyes and small head movements while speaking. Never two people in a talk scene.
  - "shot": every other scene (the place, walking together, a listening face, an insert, a reveal). It may carry a voice-over line, heard while the speaker is far away, seen from behind or off-screen: then "speaker" is that character and "line_ko" is the line; otherwise both are "".
  - "line_ko": natural spoken Korean, one or two short sentences, at most 60 Korean characters. Never put the speaker's name in front of the line. Lines are spoken aloud, never read from a page: use the character's own manner of speaking, and let feeling show in the words, with interjections (허허, 어머, 아, 글쎄), a pause written as "...", a question mark or an exclamation mark where a person would use one. Vary the sentence length and the feeling from line to line; do not write every line in the same calm, explanatory tone.
  - "emotion": how the line is said, in English (for example "calm and wise, slow"); "" when the scene has no line.
- Write about one line for every 8 to 12 seconds of film, in the order of the conversation. Let the speakers take turns naturally, and now and then cut to the listener's face (a "shot" close-up with no line).
- The "seconds" of a talk scene is your guess of how long the line takes (the voices speak about 6 Korean characters per second, so about 1 second per 6 characters plus 1); the film follows the recorded voice later.
- A talk scene never continues the previous scene and is never continued: "continues" is false for a talk scene and for the scene right after it."""
# 두세 사람 대화 장면 (11-24 메인 실측 X1·X6·X11~X13, STUDIO_DUO_TALK). 사용자 확정 규칙(11-24, 이 규칙이 무엇보다 앞선다):
# "대화 할 때는 대화를 주고받는 이들이 나와야 하고, 독백 씬에는 클로즈업이 나와야 해" + "셋 모두 나오게".
# 대사마다 누구에게 하는 말인지(listener: 듣는 사람 id / "everyone" / "nobody")를 적게 한다. 누군가에게·모두에게 하는 말(대화)은 그 자리의
# 사람 모두(최대 3명)를 한 화면(두 사람 미디엄 투샷, 세 사람 미디엄 그룹샷)에 두고 InfiniteTalk 로 말하는 사람 입만 맞춘다(듣는 사람은
# 입을 다물고 들었다). 독백(혼잣말·생각·전화·관객에게)은 같은 장소에 다른 사람이 있어도 말하는 사람 혼자 클로즈업(LTX).
# 카메라는 고정: 대사 영상 글의 삼각대 문장과 부딪히던 'very slowly pushing camera' 는 뺐다. 듣는 얼굴은 같은 화면에 보여 따로 컷하지 않는다.
# 11-25 사용자 결정 "방식 바꾸기": 장면마다 그 자리에 있는 사람(present)을 모델이 적는다. 대화 컷의 characters 는 present 에서 고르고,
# 예전에 괄호로 적던 짐작 설명(앞 장면의 사람은 떠나거나 없다고 적히기 전까지 그 자리에 있다)은 present 규칙으로 옮겼다
DIALOGUE_EXTRA_DUO = """
This film has spoken dialogue in Korean. The voices are recorded first, one fixed voice per character, and each speaking shot is lip-synced to its recorded line. Follow these extra rules:
- Every character who speaks also has "name_ko" (the Korean name or role word, for example "니키" or "할아버지"), "voice" (an English description for a voice designer: age, gender, pitch, timbre, pace and manner, at most 40 words; never name a real person, actor or famous voice), "voice_ko" (a short Korean description of the voice) and "sample_ko" (two short Korean sentences, 20 to 40 characters in all, that this character would really say, in the character's own manner of speaking and age; it is recorded once to fix the voice).
- Each scene also has "kind", "speaker", "listener", "line_ko" and "emotion":
  - "talk": one character speaks one line, seen speaking on screen. "speaker" is the speaker's id. "listener" says whom the line is spoken to: the id of the person spoken to, "everyone" when the speaker talks to everyone in the place at once, or "nobody" for a monologue (thinking aloud, talking to oneself, a phone call, speaking to the audience, calling to someone who is not in the place, such as a voice behind a door or someone who has just left: nobody in the place is spoken to). Only someone who is in the place (in "present"), a person or an animal, can be "listener"; a line spoken to an animal names that animal.
    - A line spoken to someone or to everyone is a conversation, so the people talking together are all seen: "characters" is the speaker first, then the person spoken to, then the others in "present", at most three people. With two people the shot is a "medium two-shot"; with three people it is a "medium group shot": every upper body fully in frame, side by side or seated around a table, every face in three-quarter view toward the camera, and a still camera. The others listen with their mouths closed.
    - A monologue is a close-up of the speaker alone, even when other people are in the place: "characters" is exactly [speaker], "listener" is "nobody", and the shot is a close-up or a medium close-up of the speaker (never a medium or wider shot), the face in three-quarter view, with a still camera. Its "action" describes only the speaker and never mentions the other people (no other names, no "glances at her mother", no "looks at him"), because anyone it mentions is drawn into the close-up. Its "shot" frames only the speaker too: nobody else blurred in the background or seen over a shoulder.
    - "action" describes the faces, the eyes and small head movements while speaking. Never more than three people in a talk scene.
  - "shot": every other scene (the place, walking together, an insert, a reveal). It may carry a voice-over line, heard while the speaker is far away, seen from behind or off-screen: then "speaker" is that character and "line_ko" is the line; otherwise both are "". "listener" is "" in a shot scene. A line from someone who is not seen (a voice behind a door, from another room, on the phone) is always such a "shot", never a "talk" scene.
  - "line_ko": natural spoken Korean, one or two short sentences, at most 60 Korean characters. Never put the speaker's name in front of the line. Lines are spoken aloud, never read from a page: use the character's own manner of speaking, and let feeling show in the words, with interjections (허허, 어머, 아, 글쎄), a pause written as "...", a question mark or an exclamation mark where a person would use one. Vary the sentence length and the feeling from line to line; do not write every line in the same calm, explanatory tone.
  - "emotion": how the line is said, in English (for example "calm and wise, slow"); "" when the scene has no line.
- Every scene also has "present": the ids of everyone who is in this place during this scene, whether the shot shows them or not (people and animals). "characters" (who is seen) is always part of "present".
  - Someone stays present in the later scenes at the same place until a scene shows them leave or says they are away (people from the scenes before in the same place are still there unless they left or a scene says they are away, such as "Beth and James are alone now"). Someone who leaves during a scene is present in that scene and not in the scenes after it; someone who arrives is present from that scene on. A new place or a jump in time starts again from the people really there.
  - The speaker of a "talk" scene is always present. Someone who is not in the place (a voice behind a door, from another room, on the phone) is never present and speaks only in a "shot" scene. Such a voice needs no "talk" scene of its own, even in a conversation.
  - A "listener" id is always someone in "present"; someone you call or answer who is not in the place (behind a door, in another room, on the phone, or who has just left) is never the listener: write "nobody". "everyone" means everyone in "present".
- Write about one line for every 8 to 12 seconds of film, in the order of the conversation. Let the speakers take turns naturally; the listeners' faces are seen in the same shot, so no separate listening shot is needed.
- The "seconds" of a talk scene is your guess of how long the line takes (the voices speak about 6 Korean characters per second, so about 1 second per 6 characters plus 1); the film follows the recorded voice later.
- A talk scene may continue the talk scene right before it only when both are conversation lines with the same people in the same place (the conversation goes on in the same shot). A monologue never continues and is never continued. Otherwise "continues" is false for a talk scene and for the scene right after a talk scene."""
DIALOGUE_LINE_MAX = 80          # 대사 한 줄의 글자 수 상한 (규칙은 60, 넘어도 80 까지는 쓴다)
# 대사 줄 수와 대화 (11-19 메인 첫 시험: "니키와 푸토르가 대화" 40초 요청의 구성안에 대사가 푸토르 한 줄뿐, 니키는 한마디도 없었다.
# 규칙문에는 '8~12초마다 한 줄, 번갈아'를 적었지만 검사가 강제하지 않아 모델이 최소한만 썼다)
DIALOGUE_SECONDS_PER_LINE = 13      # 이 초마다 한 줄 이상 (최소 2줄)
DIALOGUE_RUN_MAX = 2                # 같은 사람이 이어서 말하는 줄 수 상한 (대화 요청)
CONVERSATION_RE = re.compile(r"대화|이야기를\s*나누|주고\s*받|묻고|물어|대답|수다|인터뷰|토론")


def dialogue_min_lines(seconds: float) -> int:
    """영화 길이에 맞는 최소 대사 줄 수: 약 13초에 한 줄, 적어도 두 줄"""
    return max(2, int(float(seconds) // DIALOGUE_SECONDS_PER_LINE))


def wants_exchange(text: str) -> bool:
    """요청이 인물들이 서로 말을 주고받는 대화인가 (대화·묻고 대답·수다·인터뷰·토론 말). 이면 두 인물 이상이 번갈아 말해야 한다"""
    return bool(CONVERSATION_RE.search(text or ""))
DIALOGUE_SCHEMA = json.loads(json.dumps(STORY_SCHEMA))
DIALOGUE_SCHEMA["properties"]["bible"]["properties"]["characters"]["items"]["properties"].update(
    {"name_ko": {"type": "string"}, "voice": {"type": "string"}, "voice_ko": {"type": "string"}, "sample_ko": {"type": "string"}})
DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["required"] += ["kind", "speaker", "line_ko", "emotion"]
DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["properties"].update(
    {"kind": {"type": "string", "enum": ["talk", "shot"]}, "speaker": {"type": "string"}, "line_ko": {"type": "string"},
     "emotion": {"type": "string"}})
# 두세 사람 대화 장면을 끈 때(STUDIO_DUO_TALK=false)는 예전 칸 그대로 (듣는 사람 칸 없음)
DIALOGUE_SCHEMA_SOLO = json.loads(json.dumps(DIALOGUE_SCHEMA))
DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["required"] += ["listener"]
DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["properties"]["listener"] = {"type": "string"}
# 그 자리에 있는 사람 칸 (11-25 사용자 결정 "방식 바꾸기"): 장면 글의 떠남·들어옴·없음 말을 코드가 짐작하지 않고 구성안 모델이 장면마다 적는다.
# 두세 사람 규칙에만 둔다 (꺼짐은 1차 그대로, 대사 없는 영상의 STORY_SCHEMA 에도 없다)
DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["required"] += ["present"]
DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["properties"]["present"] = {"type": "array", "items": {"type": "string"}}


def duo_rules(duo: bool | None = None) -> bool:
    """구성안을 두세 사람 대화 장면 규칙으로 쓰는가. duo 는 감독이 요청마다 정한 값: 두세 사람 대사 컷을 실제로 만들 수 있는가
    (설정·장면 잇기·두 화자 레시피·가로 영상, director._duo_ready). 못 만드는 요청을 두 사람 규칙으로 쓰면 투샷 첫 화면을 LTX 로
    입 맞추거나(11-24 X2: 말하는 입이 거의 안 움직였다) 한 사람 첫 화면에 투샷 글이 붙는다 (11-24 검토). None 이면 설정(STUDIO_DUO_TALK)만 본다"""
    return settings.studio_duo_talk if duo is None else bool(duo)


def dialogue_extra(duo: bool | None = None) -> str:
    """대사 영상 구성안 지시문 (두세 사람 대화 장면 규칙 / 예전 혼자 클로즈업 규칙, duo_rules)"""
    return DIALOGUE_EXTRA_DUO if duo_rules(duo) else DIALOGUE_EXTRA


def dialogue_schema(duo: bool | None = None) -> dict:
    return DIALOGUE_SCHEMA if duo_rules(duo) else DIALOGUE_SCHEMA_SOLO


REAL_VOICE_RE = re.compile(r"\b(?:like|sounds? like|in the style of|imitat\w*|impersonat\w*)\s+(?:[A-Z][a-z]+\s?){1,3}\b")


GROUP_MAX = 3            # 대사 컷 한 화면에 두는 사람 수 상한 (사용자 결정 11-24 "셋 모두 나오게", 메인 X12·X13 세 사람 통과)
GROUP_SHOT = {2: "medium two-shot", 3: "medium group shot"}
MONO_SHOT = "medium close-up"      # 독백 컷의 샷 글이 클로즈업보다 넓을 때 (사용자 확정 규칙 11-24 "독백 씬에는 클로즈업")
TWO_SHOT_RE = re.compile(r"\btwo[\s-]?shot\b", re.I)
GROUP_SHOT_RE = re.compile(r"\bgroup[\s-]?shot\b", re.I)
MULTI_SHOT_RE = re.compile(r"\b(?:two|group)[\s-]?shot\b", re.I)


# 샷 글이 한 사람을 가리키는 꼴 ('medium shot of Beth at the table'·'medium shot of her'·'medium shot of the girl')
_ONE_PERSON_SHOT_RE = re.compile(r"\bof\s+(?:[A-Z][a-z]+\b(?!\s+(?:and|&)\b)|(?:her|him|his)\b|the\s+(?:girl|boy|woman|man|child|kid|lady"
                                 r"|old\s+(?:man|woman)|grand(?:father|mother|pa|ma)|mother|father|mom|dad)\b)")


def group_shot(shot: str, n: int, cast: dict | None = None) -> str:
    """두세 사람 대사 컷의 샷 글을 사람 수에 맞춘다: 세 사람인데 'two-shot' 이면 'group shot'(두 사람인데 'group shot' 이면 'two-shot'),
    미디엄 크기(2)가 아니면 — 가까운 샷(클로즈업·미디엄 클로즈업)·크기 말이 없음·미디엄보다 넓음(풀샷·미디엄 와이드 …) — 미디엄
    투샷/그룹샷. 혼자 말하던 컷에 같은 자리의 사람을 이어 받으면 '클로즈업'에 두세 사람이 들어가 구도 그림의 크기 지시와 부딪힌다
    (clean_dialogue_story). 사용자 확정 규칙(G0-1)은 미디엄 투샷·그룹샷이고 메인 X11~X13 도 미디엄 크기로만 통과했다 (11-24 2차 검토:
    풀샷이 그대로 남았다). 미디엄이어도 한 사람을 가리키는 샷 글('medium shot of Beth at the table', cast 를 주면 인물 이름이 든 글도)은
    미디엄 투샷/그룹샷 (3차 검토: 구도 글이 'medium shot of Beth' 에 세 형상을, 영상 글이 'Medium shot of Beth … of three people' 을 썼다).
    한 사람이면 그대로"""
    shot = str(shot or "")
    if n < 2:
        return shot
    shot = TWO_SHOT_RE.sub("group shot", shot) if n >= 3 else GROUP_SHOT_RE.sub("two-shot", shot)
    one = bool(_ONE_PERSON_SHOT_RE.search(shot)) or bool(cast and any(rx is not None and rx.search(shot) for rx in cast["names"].values()))
    return shot if shot_rank(shot) == 2 and not one else GROUP_SHOT[min(n, GROUP_MAX)]


def close_shot(shot: str) -> str:
    """독백 컷의 샷 글: 클로즈업·미디엄 클로즈업이면 그대로, 투샷·그룹샷이거나 더 넓거나 크기 말이 없으면 미디엄 클로즈업
    (사용자 확정 규칙 11-24 "독백 씬에는 클로즈업이 나와야 해": 미디엄 샷도 아니다)"""
    shot = str(shot or "")
    rank = shot_rank(shot)
    return shot if rank is not None and rank <= 1 and not MULTI_SHOT_RE.search(shot) else MONO_SHOT


# 어깨 너머 샷: 앞에 다른 사람의 어깨가 걸린다 (독백 컷은 혼자)
_OTS_RE = re.compile(r"\bover[\s-]+(?:the|her|his|their|[A-Za-z]+['’]s)[\s-]+shoulders?\b|\bOTS\b", re.I)
_SHOT_CUT_RE = re.compile(r"\s*[,;(]|\s+(?:with|as|while|and|over|behind|beside|next\s+to|in\s+front\s+of|near|facing|across|by|from|between"
                          r"|among|watched|framed|surrounded)\s+", re.I)
_SIZE_WORD_RE = re.compile(r"\b(?:extreme|medium)?[\s-]*close[\s-]*up\b", re.I)


def _shot_others(shot: str, who: str, cast: dict) -> str:
    """샷 글에서 말하는 사람 말고 다른 사람을 가리키는 말 (_other_mention·_stranger, 어깨 너머 샷). 없으면 빈 글"""
    m = _OTS_RE.search(str(shot or ""))
    return _other_mention(shot, who, cast) or _stranger(shot, who, cast) or (m.group(0) if m else "")


def _mono_shot(shot: str, who: str, cast: dict) -> str:
    """독백 컷의 샷 글: 크기는 클로즈업까지(close_shot), 다른 사람을 가리키는 말('with Beth and James out of focus behind her'·'her parents softly
    blurred in the background'·"over her father's shoulder")이 있으면 그 앞(크기 말과 말하는 사람)까지만, 그래도 남으면 크기 말만 (5차 검토: 다른
    사람 이름이 혼자 대사 영상 글·구도 그림 글·기준 그림 고치기 글의 첫머리에 그대로 들어가 그 사람을 그려 넣었다 — 11-21 R3)"""
    shot = close_shot(shot)
    if not _shot_others(shot, who, cast):
        return shot
    cut = _SHOT_CUT_RE.split(shot, maxsplit=1)[0].strip()
    if cut and shot_rank(cut) is not None and not _shot_others(cut, who, cast):
        return cut
    m = _SIZE_WORD_RE.search(_OTS_RE.sub(" ", shot))
    return re.sub(r"\s+", " ", m.group(0)).strip(" -").lower() if m else MONO_SHOT


def _shown(sc: dict, looks: dict) -> list[str]:
    """장면 화면에 있는 사람 id (동물 아님, 적힌 차례). 대사 장면은 말하는 사람도 넣는다 (말 없는 장면의 화면 밖 목소리는 넣지 않는다. 대사 장면도
    글이 문 밖·전화 같은 화면 밖 목소리라고 말하면 넣지 않는다 — _offscreen_voice, 4차 검토)"""
    ids = list(dict.fromkeys(_s(c) for c in sc.get("characters") or []))
    who = _s(sc.get("speaker"))
    if _s(sc.get("kind")) == "talk" and who and who not in ids and not _offscreen_voice(sc):
        ids.insert(0, who)
    return [c for c in ids if c in looks and not is_animal_look(looks[c])]


def _names_re(words, exact=()) -> re.Pattern | None:
    """사람을 가리키는 말(이름·id) 목록 → 정규식. 영어는 낱말 경계로, 한글은 조사가 붙으므로 부분 일치로. exact 는 대소문자까지 맞아야 하는
    말 (장면 글에서 배운 영어 이름: 'Tears' 같은 낱말을 'tears' 에서 찾지 않게). 없으면 None"""
    en = sorted({w for w in words if w and not HANGUL_RE.search(w)}, key=len, reverse=True)
    ko = sorted({w for w in words if w and HANGUL_RE.search(w)}, key=len, reverse=True)
    ex = sorted({w for w in exact if w} - set(en), key=len, reverse=True)
    parts = ([r"(?<![A-Za-z])(?:" + "|".join(re.escape(w) for w in en) + r")(?![A-Za-z])"] if en else []) \
        + (["(?:" + "|".join(re.escape(w) for w in ko) + ")"] if ko else []) \
        + ([r"(?-i:(?<![A-Za-z])(?:" + "|".join(re.escape(w) for w in ex) + r")(?![A-Za-z]))"] if ex else [])
    return re.compile("|".join(parts), re.I) if parts else None


# 장면 글의 고유명사 후보 (대문자로 시작하는 낱말)와 고유명사가 아닌 흔한 첫 낱말
_NAME_WORD_RE = re.compile(r"\b([A-Z][a-z]{1,15})(?:['’]s)?\b")
# 관사·소유격 바로 뒤의 대문자 낱말 무리 ('the Compass'·'her Compass'·'the Strange Compass'): 이야기의 물건 이름이지 사람 이름이 아니다 (4차 검토).
# this·that 은 넣지 않는다 (5차 검토: 'Suji realizes that Minho is upset' 의 접속사 that 뒤 Minho 를 물건으로 보아 이름을 배우지 못했다)
_ARTICLE = r"\b(?i:the|a|an|her|his|their|its|my|your|our)\s+"
_ARTICLED_RE = re.compile(_ARTICLE + r"((?:[A-Z][a-z]{1,15}\s+)*[A-Z][a-z]{1,15})\b")
_ARTICLED_BEFORE = re.compile(_ARTICLE + r"(?:[A-Z][a-z]{1,15}\s+)*$")
_NOT_NAME = frozenset((
    "The", "A", "An", "She", "He", "They", "It", "Her", "His", "Their", "Its", "Then", "As", "With", "While", "When", "In", "On", "At",
    "From", "To", "Of", "And", "But", "Or", "So", "Now", "Still", "Later", "Meanwhile", "Behind", "Outside", "Inside", "Close", "Medium",
    "Wide", "Camera", "Morning", "Evening", "Night", "Today", "Everyone", "Everybody", "Someone", "Somebody", "Nobody", "Both", "All",
    "Each", "One", "Two", "Three", "This", "That", "These", "Those", "There", "Here", "Together", "Finally", "Again", "Just", "Only",
    "Even", "Very", "Long", "Soft", "Warm", "Bright", "Dark", "Light", "Little", "Small", "Big", "Old", "Young", "After", "Before",
    "Under", "Over", "Near", "Beside", "Across", "Through", "Back", "Front", "Side", "Mom", "Mum", "Dad", "Mother", "Father", "Grandma",
    "Grandpa", "No", "Not", "Yes", "Oh", "Ah", "Eye", "Low", "High", "Over", "Extreme", "Full", "Static", "Slow", "Fast", "Quick"))


_ROLE_NAME = frozenset(("Mom", "Mum", "Dad", "Mother", "Father", "Grandma", "Grandpa"))      # 장면 글의 주어로 쓰는 역할 말


def _text_names(scenes, chars: dict) -> dict[str, set[str]]:
    """구성안 장면 글(영어)에서 인물의 영어 이름을 배운다 (11-24 3차 검토: 설정집 id 가 'A'·'B' 이고 라이브러리 밖 인물이면 'James'·'Minho' 를
    사람 이름으로 몰라, 독백 컷의 'Niki glances at James' 가 검사를 지나 영상 글에 그대로 들어갔다). 말하는 장면의 장면 글은 말하는 사람
    이름으로 시작하고(대사 장면 규칙), 한 사람만 나오는 장면의 장면 글은 그 사람 이름으로 시작하는 일이 많다: 장면 글 앞 네 낱말 안의 첫
    고유명사 후보를 그 사람에게 한 표. 글 가운데에도 대문자로 나오거나 두 번 이상 표를 받은 낱말만 이름으로 보고('Tears roll down …' 의
    'Tears' 는 아니다), 표가 가장 많은 사람의 이름으로 한다 (비기면 모른다). 이야기 어디선가 관사·소유격 뒤에 온 낱말('the Compass')은 물건
    이름이라 건너뛰고 그다음 후보를 본다 (4차 검토: 'Holding the Compass, Niki …' 에서 나침반을 니키 이름으로 배워 푸토르 독백의 'the Compass' 를
    다른 사람으로 잡았다). {인물 id: 이름들}"""
    votes: dict[str, dict[str, int]] = {}
    inner: set[str] = set()
    articled = {w for sc in scenes or [] if isinstance(sc, dict) for field in ("action", "shot", "sound", "ko")
                for m in _ARTICLED_RE.finditer(_s(sc.get(field))) for w in m.group(1).split()}
    for sc in scenes or []:
        if not isinstance(sc, dict):
            continue
        for field in ("action", "shot", "sound"):
            for sent in re.split(r"[.!?;:]+", _s(sc.get(field))):
                for m in _NAME_WORD_RE.finditer(sent):
                    if sent[:m.start()].strip(" ,\"'(“”"):
                        inner.add(m.group(1))           # 문장 첫 낱말이 아닌데 대문자
        act = _s(sc.get("action"))
        who = _s(sc.get("speaker")) if _s(sc.get("kind")) == "talk" else ""
        ppl = [c for c in dict.fromkeys(_s(x) for x in sc.get("characters") or [])
               if c in chars and not is_animal_look(_s((chars.get(c) or {}).get("look")))]
        target = who if who in chars else (ppl[0] if len(ppl) == 1 else "")
        if not target or not act:
            continue
        head = " ".join(act.split()[:4])
        for m in _NAME_WORD_RE.finditer(head):
            w = m.group(1)
            if w in _ROLE_NAME:
                if re.match(r"\s+[A-Z][a-z]", head[m.end():]):
                    continue        # 'Grandpa Joe smiles …': 역할 말 + 이름 — 바로 뒤 이름에 표를 준다 (fixB: 이름을 못 배워 'close-up of Grandpa Joe' 를 잘랐다)
                break               # 'Mother smiles at Jiwoo': 역할 말이 주어다 — 다음 대문자 낱말(듣는 사람)에 표를 주지 않는다 (fixA)
            if w in _NOT_NAME or w.endswith(("ly", "ing")) or w in articled:
                continue
            votes.setdefault(w, {}).setdefault(target, 0)
            votes[w][target] += 1
            break
    out: dict[str, set[str]] = {}
    for w, cnt in votes.items():
        ranked = sorted(cnt.items(), key=lambda kv: -kv[1])
        if (len(ranked) > 1 and ranked[0][1] == ranked[1][1]) or not (w in inner or ranked[0][1] >= 2):
            continue
        out.setdefault(ranked[0][0], set()).add(w)
    return out


def _cast_view(chars: dict, known: list | None = None, scenes: list | None = None, places: dict | None = None) -> dict:
    """대사 장면 정리·검사·영상 글이 보는 인물 정보 (11-24 2차 검토).
    - looks: 외형
    - person: 같은 사람 묶음 열쇠. 인물 라이브러리의 같은 사람 다른 모습(variant_of: 제임스 ↔ 안경 쓴 제임스)은 한 사람이다 — 자리 이어
      받기가 안경 쓴 제임스의 클로즈업 뒤 대화 컷에 안경 안 쓴 제임스를 더해 한 화면에 제임스가 둘이었다. 구성안 단계는 요청에 이름이 나온
      라이브러리 인물(known)로 맞추고(cast_lib.match_character), 라이브러리로 덮어쓴 이야기는 설정집의 library·variant_of 칸을 본다
    - names: 장면 글에서 그 사람을 가리키는 말의 정규식 (이름 같은 id, 한글 이름, 같은 사람의 라이브러리 이름들, scenes 를 주면 장면 글에서
      배운 영어 이름 _text_names — 3차 검토)
    - refs: 움직임의 주어를 찾는 말 (사람은 names, 동물은 이름과 'the puppy' 같은 외형 머리 낱말 — 4차 검토: 동물이 떠나는 말을 사람에게 붙였다)
    - places: {장소 id: 외형 글} (움직임의 목적지가 지금 장소인지 본다, 4차 검토)"""
    entries = list(known or [])
    by_id = {e["id"]: e for e in entries}
    if not entries and any(isinstance(c, dict) and c.get("library") for c in chars.values()):
        by_id = {e["id"]: e for e in cast_lib.library()}
    looks, person, words, ko = {}, {}, {}, {}
    for cid, c in chars.items():
        c = c if isinstance(c, dict) else {}
        looks[cid] = _s(c.get("look"))
        ko[cid] = _s(c.get("name_ko"))
        if entries:
            e = cast_lib.match_character(c, entries)
            base = (e.get("variant_of") or e["id"]) if e else ""
        else:
            base = _s(c.get("variant_of")) or _s(c.get("library"))
        person[cid] = f"lib:{base}" if base else cid
        words[cid] = ([cid] if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]+", cid) else []) + [_s(c.get("name_ko"))]
        if base:
            words[cid] += [n for x in by_id.values() if (x.get("variant_of") or x["id"]) == base for n in x["names"]]
    # 한글 이름이 역할 말인 인물('엄마'·'할머니')은 영어 장면 글이 그 역할 말로 부른다 ('Grandma waves goodbye and leaves', 5차 검토: 떠남을 그 사람에게
    # 붙이지 못해 떠난 할머니가 다음 대화 컷에 남았다). 그 말을 가진 인물이 하나일 때만 이름으로 더한다
    role_of: dict[str, list[str]] = {}
    for cid in chars:
        for w in _ROLE_WORDS.get(ko[cid], "").split():
            role_of.setdefault(w, []).append(cid)
    for w, ids in role_of.items():
        if len(ids) == 1:
            words[ids[0]].append(w)
    known_re = {cid: _names_re(w) for cid, w in words.items()}
    learned = _text_names(scenes, chars) if scenes else {}
    names = {}
    for cid in chars:
        # 배운 이름이 다른 사람의 아는 이름이면 버린다 (같은 사람의 다른 모습은 괜찮다)
        mine = {w for w in learned.get(cid, ()) if not any(rx is not None and rx.fullmatch(w) and person[o] != person[cid]
                                                           for o, rx in known_re.items())}
        names[cid] = _names_re(words[cid], mine) if mine else known_re[cid]
    refs = dict(names)
    for cid, look in looks.items():
        if is_animal_look(look):
            noun = (re.findall(r"[a-z]+", _head_phrase(look).lower()) or [""])[-1]
            parts = ([names[cid].pattern] if names.get(cid) is not None else []) + (
                [rf"\b(?:the|her|his|their|its|a|an)\s+(?:[a-z'’-]+\s+){{0,2}}?{re.escape(noun)}s?\b"] if noun else [])
            refs[cid] = re.compile("|".join(parts), re.I) if parts else None
    return {"looks": looks, "person": person, "names": names, "ko": ko, "refs": refs, "places": dict(places or {})}


def _story_cast(story: dict) -> dict:
    """라이브러리로 덮어쓴(또는 구성안 그대로의) 이야기의 인물 정보 (_cast_view, 장면 글에서 배운 영어 이름까지)"""
    return _cast_view({c["id"]: c for c in story["bible"]["characters"]}, scenes=story.get("scenes"),
                      places={p["id"]: p["look"] for p in story["bible"].get("places") or []})


def _persons(story: dict) -> dict[str, str]:
    """설정집 인물 id → 같은 사람 묶음 열쇠 (설정집의 library·variant_of 칸: 같은 사람의 다른 모습은 한 사람, 라이브러리 밖 인물은 제 id)"""
    out = {}
    for c in story["bible"]["characters"]:
        base = _s(c.get("variant_of")) or _s(c.get("library"))
        out[c["id"]] = f"lib:{base}" if base else c["id"]
    return out


def _one_look(ids, cast: dict) -> list[str]:
    """같은 사람의 다른 모습이 여럿 적혔으면 처음 것만 남긴다 (차례는 그대로: 그 장면이 적은 말하는 사람·듣는 사람이 앞에 있다)"""
    out, seen = [], set()
    for c in ids:
        p = cast["person"].get(c, c)
        if p not in seen:
            seen.add(p)
            out.append(c)
    return out


# 그 장소를 떠나는 말·들어오는 말 (11-24 2차·3차·4차 검토): 자리 사람 이어 받기(_presence)는 앞 장면에서 그 장소를 떠난 사람을 다음 대화 컷에
# 다시 넣지 않고(학교에 가려고 부엌문으로 뛰어나간 니키가 부모의 다음 대화 컷에 다시 앉아 있었다), 듣는 사람이 그 자리에 있는가(_here)는 그
# 사람이 처음 보이는 장면이 들어오는 장면인지도 본다. 3차 검토: 떠남 판정이 넓어 그 자리에 그대로 있는 사람을 다음 대화 컷에서 뺐다 — 'goes off'
# (알람)·'heads off to the stove'·'slips off her chair'·'run out of milk'·'looks out the kitchen door'·'wants to go out'·'she will leave for
# work'·'지나간다'·'앞으로 나간다'·'이야기를 이어 나간다'·'나가 놀고 싶어'. 그래서 사람이 장소를 떠나는 꼴만 보고(움직이는 말 + out of the 방·문,
# away, '밖으로·문을·정원으로 나가다', '집을 나서다', '뛰쳐나가다', '떠나다'), 바람·계획·부정·전하는 말은 빼고, 누가 떠나는지는 그 말의 주어로 본다.
# 4차 검토: 목적지를 보지 않아 정원 장면의 'Niki runs out into the garden'·부엌 장면의 'runs downstairs into the kitchen' 처럼 그 장소에 막 들어온
# 사람을 떠난 사람으로 셌다 → 움직임의 목적지·출발지가 지금 장소(장소 외형 글)인지 본다 (_move_dir). 흔한 떠남('gets up and leaves'·'goes to
# school'·'sets off'·'disappears'·'인사하고 나간다'·'학교에 간다'·'사라진다')과 들어옴('comes down the stairs'·'appears in the doorway'·'joins
# them'·'내려온다')도 더했다
_LEAVE_GO = (r"(?:runs?|running|ran|walks?|walking|walked|go|goes|going|went|heads?|heading|headed|steps?|stepping|stepped"
             r"|rush(?:es|ing|ed)?|hurr(?:y|ies|ying|ied)|dash(?:es|ing|ed)?|storms?|storming|stormed|slips?|slipping|slipped|sneaks?"
             r"|sneaking|sneaked|snuck|wanders?|wandering|wandered|strides?|striding|strode|march(?:es|ing|ed)?|drives?|driving|drove"
             r"|races?|racing|raced|darts?|darting|darted|bolts?|bolting|bolted|climbs?|climbing|climbed|hops?|hopping|hopped|skips?"
             r"|skipping|skipped|stomps?|stomping|stomped|tiptoes?|tiptoeing|tiptoed)")
_LEAVE_OFF = (r"(?:walks?|walking|walked|runs?|running|ran|drives?|driving|drove|storms?|storming|stormed|march(?:es|ing|ed)?"
              r"|strides?|striding|strode|wanders?|wandering|wandered|hurries|hurrying|hurried|rush(?:es|ing|ed)|dash(?:es|ing|ed)"
              r"|races?|racing|raced|stomps?|stomping|stomped)")
_LEAVE_PLACE = (r"(?:room|kitchen|house|home|door(?:way)?|attic|cottage|hall(?:way)?|building|shop|store|class(?:room)?|office|caf[eé]"
                r"|tent|barn|cabin|gate|garden|yard|bedroom|library|church|school|castle|cave|window|car|bus|train|party|forest|woods"
                r"|village|town|scene)")
LEAVE_RE = re.compile(
    rf"\b{_LEAVE_GO}\s+out\b(?:\s+(?:of\s+(?:the|her|his|their|its|a|an)\s+(?:\w+\s+)?{_LEAVE_PLACE}\b"
    rf"|(?:through\s+)?the\s+(?:\w+\s+)?(?:door|gate|window)\b|(?:into|onto|to|toward|towards)\b|of\s+sight\b)"
    rf"|(?=\s*(?:$|[,:]|and\b|with\b|as\b|while\b|without\b)))"
    rf"|\b{_LEAVE_OFF}\s+off\b(?!\s+(?:her|his|their|the|a)\s+(?:chair|seat|bed|sofa|couch|stool|bench|swing|lap)\b)"
    rf"|\b{_LEAVE_GO}\s+(?:back\s+)?(?:away|outside|upstairs|downstairs|home)\b"
    rf"|\b{_LEAVE_GO}\s+(?:back\s+)?(?:up|down)\s+the\s+(?:stairs|staircase)\b"
    rf"|\b{_LEAVE_GO}\s+(?:back\s+)?(?:off\s+)?(?:to|for)\s+(?:school|work|bed|class|kindergarten|(?:the\s+)?(?:office|station|bus\s+stop)"
    rf"|(?:her|his|their|my)\s+(?:room|bedroom))\b"
    rf"|\b{_LEAVE_GO}\s+to\s+the\s+(?:\w+\s+)?(?:door|gate)\s+and\s+out\b"
    rf"|\b(?:follows?|following|followed)\s+(?:[\w'’-]+\s+){{0,3}}?out\b(?!\s+(?:the|a|an)?\s*window)"
    rf"|\b(?:leaves?|leaving)\s+(?:the\s+|her\s+|his\s+|their\s+)?{_LEAVE_PLACE}\b(?!\s+(?:open|ajar|unlocked|wide|half))"
    rf"|\b(?:leaves?|leaving)\s+(?:for|without)\b"
    rf"|\bleaves(?=\s*(?:$|[,:;]|and\b))"
    rf"|\b(?:sets?|setting)\s+off\b(?!\s+(?:the|a|an|her|his|their|its|fireworks|sparks|alarms?)\b)"
    rf"|\b(?:disappears?|disappearing|disappeared|vanish(?:es|ed|ing)?)\b"
    rf"|\b(?:has|have|had)\s+(?:already\s+|just\s+)?left\b(?=\s*(?:$|[,.;:!?]|for\b|to\b|with\b|without\b|early\b|for\s+good\b"
    rf"|(?:the\s+|her\s+|his\s+|their\s+){_LEAVE_PLACE}\b(?!\s+(?:open|ajar|unlocked|wide|half))))"
    rf"|\b(?:has|have|had)\s+(?:already\s+|just\s+)?gone\b(?!\s+(?:quiet|silent|still|pale|red|white|cold|dark|numb|soft|stiff|wrong|well|bad|dry"
    rf"|flat|stale))"
    rf"|\b(?:is|are|was|were)\s+(?:already\s+|now\s+|long\s+)?gone\b"
    rf"|\b(?:exits?|exiting|exited|departs?|departing|departed)\b", re.I)
ENTER_RE = re.compile(
    rf"\b(?:{_LEAVE_GO}|comes?|coming|came|bursts?|bursting|peeks?|peeking|peeked|strolls?|strolling|strolled)\s+(?:back\s+)?(?:in|into|inside)\b"
    r"|\b(?:enters?|entering|entered|arrives?|arriving|arrived|returns?|returning|returned|reappears?|reappearing|reappeared)\b"
    r"|\b(?:comes?|coming|came|gets?|got)\s+back\b"
    r"|\b(?:follows?|following|followed)\s+(?:[\w'’-]+\s+){0,3}?(?:in|into|inside)\b"
    r"|\b(?:comes?|coming|came)\s+(?:back\s+)?(?:down|up|over|out|running|rushing|hurrying|walking|dashing|skipping|bounding|home)\b"
    r"|\b(?:bursts?|bursting|comes?|coming|came)\s+(?:in\s+)?through\s+the\s+(?:\w+\s+)?(?:door|doorway|gate)\b"
    r"|\bappears?\s+(?:in|at)\s+the\s+(?:\w+\s+)?(?:door(?:way)?|entrance|gate|top\s+of\s+the\s+stairs)\b"
    r"|\bjoins?\s+(?:them|the\s+(?:family|others|group)|everyone|her\s+(?:parents|family)|his\s+(?:parents|family))\b"
    r"|\bjoins?\s+(?:\w+\s+){1,3}at\s+the\s+(?:\w+\s+)?table\b"
    r"|\b(?:sits?|sitting|sat)\s+down\s+at\s+the\s+(?:\w+\s+)?table\b|\btakes?\s+(?:her|his|their)\s+(?:seat|place)\s+at\b"
    r"|\bpulls?\s+up\s+a\s+chair\b", re.I)
# '문을 닫고 나간다' 같은 약한 떠남 ('closes the door behind her'): 같은 장면에서 그 사람이 들어오면 들어온 뒤 문을 닫은 것이다
_WEAK_LEAVE_RE = re.compile(r"\bcloses?\s+the\s+(?:\w+\s+)?door\s+behind\s+(?:her|him|them)\b", re.I)
# 한국어: '뛰쳐나가다'·'뛰어/달려/걸어 나가다'는 늘, 맨 '나가다'는 앞에 장소 말('밖으로·문을·집을·정원으로')이나 이어지는 동작('인사하고·문을
# 열고·가방을 챙겨 나간다', 4차 검토)이 있을 때만 ('앞으로·뒤로·옆으로'·'지나가다'·'이어 나가다' 는 아니다), '나서다'는 '집을 나서다' 꼴,
# '떠나다'·'자리를 뜨다'·'퇴장'. 목적지 + 가는 말('학교에 간다'·'방으로 올라간다'·'학교로 향한다')과 '등교·출근·외출·사라지다'도 (4차 검토)
_KO_LEAVE_VERB = re.compile(r"(?P<pre>뛰쳐\s*|(?:뛰어|달려|걸어|빠져|튀어|서둘러|급히|몰래)\s*)?나(?:가|간|갔|감|선|서|섰)|떠나|떠난|떠났"
                            r"|자리를\s*(?:뜨|뜬|떴)|퇴장")
_KO_GO_TO = re.compile(r"(?P<dest>[가-힣0-9]+?)\s*(?:에|으로|로)\s+(?:[가-힣]+(?:히|게|리|러)\s+)?(?:혼자\s+|먼저\s+|다시\s+)?"
                       r"(?:(?:(?:뛰어|달려|걸어|서둘러|급히)\s*)?(?:(?:올라|내려|들어)\s*)?(?:간다|갔다|갔어|갑니다|가요|가는|가고|가며|가서|가버|가\s+버)"
                       r"|향한다|향하는|향해|향했|향하며|향하고)")
# 5차 검토: 낱말만 보아 '등교 준비를 하는 니키'·'외출복을 입은 베스'·'하교한 니키'(집에 막 돌아온 사람)를 떠난 사람으로 셌다 → 등교·출근·외출은
# 동사 꼴('등교한다·등교했다·등교하러·등굣길·외출 중')만, 하교·퇴근·귀가는 돌아오는 말이라 지금 장소가 학교·회사가 아니면 들어옴 (_KO_HOME_VERB)
_KO_GONE_VERB = re.compile(r"(?:등교|출근|외출)(?=\s*(?:한다|했|합니|해요|해서|하러|하고|하며|하면서|하자|한\s|한$|하는\s*길|길|중))|등굣길|출근길"
                           r"|사라지|사라진|사라졌|사라져")
_KO_HOME_VERB = re.compile(r"(?:하교|퇴근|귀가)(?=\s*(?:한다|했|합니|해요|해서|하고|하며|하면서|하자|한\s|한$))")
_KO_OWNER_BEFORE = re.compile(r"(?:친구|친척|할머니|할아버지|이웃|삼촌|이모|고모|선생님|[가-힣]+네)(?:의)?\s*$")
_KO_ENTER_VERB = re.compile(r"들어(?:오|온|왔|와|서|선|섰)|돌아(?:오|온|왔|와)|내려(?:오|온|왔|와)|도착|등장|나타나|나타난|나타났|합류")
_KO_PLACE_BEFORE = re.compile(r"(?:(?:밖|바깥)(?:으로|에)?|문\s*(?:을|으로|밖으로|밖)?|(?:집|방|부엌|교실|가게|마당|학교)\s*(?:을|에서|밖으로|밖)"
                              r"|(?<![앞뒤옆위안속])(?<![앞뒤옆위안속]으)(?:으로|로))(?:\s+[가-힣]*(?:히|게|이))?\s*$")
_KO_AUX_BEFORE = re.compile(r"(?:지|이어\s*|해\s*|만들어\s*|풀어\s*|헤쳐\s*|살아\s*|키워\s*|이겨\s*|극복해\s*)$")
_KO_CONN_BEFORE = re.compile(r"(?:고|며|면서|서|챙겨|안아|업어|끌어|데려)\s+$")
# 바람·계획·부정 ('나가 놀고 싶어'·'나가려다'·'떠나지 않는다'·'나갈 거야'·'wants to go out'·'she will leave'·'doesn't leave')
_KO_WISH_AFTER = re.compile(r"\s*(?:[가-힣]*\s*)?(?:싶|려|겠|지\s*(?:않|못|마)|기\s*(?:전|싫)|야\s*(?:해|한|하|겠))")
_EN_WISH_BEFORE = re.compile(r"\b(?:will|would|could|should|can|cannot|may|might|must|shall|to|has|have|had|not|never|longer|about"
                             r"|refuses?|refused)\s+(?:\w+ly\s+)?$|(?:'ll|'d|n't|n’t)\s+(?:\w+ly\s+)?$", re.I)
# 전하는 말·생각 ('Beth tells James she will leave'·'Niki says she goes out'·'Beth remembers how James walked out')
_EN_REPORT_BEFORE = re.compile(r"\b(?:says?|said|tells?|told|asks?|asked|promises?|promised|announces?|announced|explains?|explained"
                               r"|warns?|warned|reminds?|reminded|thinks?|thought|imagines?|imagined|dreams?|dreamed|dreamt|remembers?"
                               r"|remembered|wonders?|wondered|wish(?:es|ed)?|hopes?|hoped|decides?|decided|plans?|planned|pretends?)\b", re.I)
_SUBJECT_PRONOUN_RE = re.compile(r"\b(?:she|he)\b", re.I)
# 떠나는(들어오는) 사람이 '모두'인 말 (그 말이 움직이는 말의 주어일 때만: 가장 가까운 앞 사람 말일 때)
_ALL_WHO_RE = re.compile(r"\b(?:they|everyone|everybody|all of them|both of them|the family|the others)\b", re.I)
_ALL_WHO_KO_RE = re.compile(r"모두|다\s*같이|다\s*함께|가족(?:이|은|들)")
# 설정집 밖의 사람이 주어인 말 ('Beth smiles as someone walks out'·'a stranger leaves'): 그 움직임은 설정집 인물의 것이 아니다
_OTHER_SUBJECT_RE = re.compile(r"\b(?:someone|somebody|a\s+(?:stranger|customer|waiter|waitress|neighbou?r|guest|passer-?by)"
                               r"|the\s+(?:stranger|customer|waiter|waitress|neighbou?r|guest|mailman|postman))\b", re.I)
_AND_RE = re.compile(r"^\s*(?:,|and|&|와|과|하고|이랑|랑)?\s*$", re.I)
# 맨 'leaves' 바로 앞 낱말이 이음말이면 떠남이지만('gets up and leaves'), 자연물이 이어지면 나뭇잎이다 ('covered in moss and leaves')
_NATURE_BEFORE_AND = re.compile(r"\b(?:moss|grass|twigs?|branches|flowers?|petals?|acorns?|berries|sticks?|stones?|mud|dirt|bark|apples?|seeds?"
                                r"|vines?|roots?|ferns?|weeds?|needles|pine|dust|snow|sand|rocks?|pebbles?|mushrooms?|fruits?|nuts?|feathers?)\s+and\s+$",
                                re.I)

# ── 장소 말: 움직임의 목적지·출발지가 지금 장소인가 (4차 검토) ──
_PLACE_STOP = frozenset((
    "a an the of with and in on at by to into onto from for her his their its my your our this that these those some small big large little tiny "
    "old new sunny quiet cozy cosy bright dark warm cold wooden round narrow wide long front back side main open lovely pretty beautiful simple "
    "soft green white red blue yellow stone brick tall low shady quiet empty busy").split())
_OUTDOOR_FAMILIES = (frozenset("garden gardens yard backyard lawn grass patio porch terrace courtyard flowerbed".split()),
                     frozenset("street road lane alley sidewalk square market town village".split()),
                     frozenset("forest woods trail path clearing".split()),
                     frozenset("field fields meadow farm orchard hill hills valley mountain mountains cliff".split()),
                     frozenset("beach shore sea seaside river riverbank lake pond bridge".split()),
                     frozenset("park playground".split()))
_HOME_WORDS = frozenset(("home house cottage kitchen living dining bedroom attic hall hallway apartment flat cabin loft nursery bathroom study parlor "
                         "parlour basement cellar foyer entrance room inside indoors").split())
_HOME_ANY = frozenset("home house cottage inside indoors".split())          # 집 안 어디든: 지금 장소가 집 안이면 지금 장소
_UP_WORDS = frozenset("attic bedroom loft upstairs nursery".split())
_DOWN_WORDS = frozenset("kitchen living dining hall hallway basement cellar foyer parlor parlour entrance downstairs".split())
_PASS_WORDS = frozenset("room door doorway gate window scene frame building".split())     # 나가는 쪽이면 지금 장소의 문·방
_ELSEWHERE_WORDS = frozenset(("school work office class classroom shop store cafe bed station car bus train taxi van truck carriage boat cart "
                              "church library hospital bakery elsewhere").split())
_KO_PLACE_WORDS = (("다락방", "attic"), ("아래층", "downstairs"), ("바닷가", "beach"), ("과수원", "orchard"), ("놀이터", "playground"),
                   ("정원", "garden"), ("마당", "yard"), ("잔디", "lawn"), ("공원", "park"), ("거리", "street"), ("해변", "beach"), ("강가", "riverbank"),
                   ("언덕", "hill"), ("농장", "farm"), ("바깥", "outside"), ("야외", "outside"), ("부엌", "kitchen"), ("주방", "kitchen"), ("거실", "living"),
                   ("침실", "bedroom"), ("복도", "hallway"), ("현관", "entrance"), ("학교", "school"), ("교실", "classroom"), ("회사", "office"),
                   ("가게", "shop"), ("카페", "cafe"), ("위층", "upstairs"), ("윗층", "upstairs"), ("2층", "upstairs"), ("이층", "upstairs"),
                   ("1층", "downstairs"), ("다락", "attic"), ("뜰", "garden"), ("숲", "forest"), ("밖", "outside"), ("집", "home"), ("방", "room"))


_PLACE_VIEW_RE = re.compile(r"\b(?:windows?|views?|doors?|doorways?|gates?)\s+(?:to|onto|into|of|over|toward|towards|out\s+(?:to|onto|over))\s+"
                            r"(?:the\s+|a\s+|an\s+)?[a-z]+(?:\s+[a-z]+)?|\b(?:overlooking|looking\s+out\s+(?:on|onto|to|over))\s+(?:the\s+|a\s+|an\s+)?"
                            r"[a-z]+(?:\s+[a-z]+)?", re.I)


# 옆 장소 (5차 검토: 'a bright cottage kitchen that opens onto the garden'·'a farmhouse kitchen next to the barn'·'a small garden behind the house' 의
# 정원·헛간·집을 지금 장소로 보아 그쪽으로 나가는 말을 들어옴으로 읽었다): 관계 말과 그 목적어는 지금 장소가 아니다 ('with an apple tree' 는 그 장소 안)
_PLACE_NEAR_RE = re.compile(r"\b(?:next\s+to|beside|behind|near|by|across\s+from|opposite|outside|beyond|facing|leading\s+(?:to|into|onto|out\s+to)"
                            r"|(?:that\s+|which\s+)?opens?\s+(?:onto|into|to|out\s+(?:to|onto))|opening\s+(?:onto|into|to))\s+"
                            r"(?:the\s+|a\s+|an\s+)?(?:(?!(?:with|and|where|which|that)\b)[a-z]+(?:\s+|$)){1,3}", re.I)


def _place_words(look: str) -> frozenset:
    """장소 외형 글의 낱말 (소문자). 창·문 너머로 보이는 곳('a kitchen with a window to the garden')과 옆 장소('next to the barn'·'opens onto the
    garden')는 그 장소가 아니라 뺀다"""
    return frozenset(re.findall(r"[a-z]+", _PLACE_NEAR_RE.sub(" ", _PLACE_VIEW_RE.sub(" ", str(look or ""))).lower()))


def _is_here(words, here: frozenset, origin: bool = False) -> bool | None:
    """목적지(origin 이면 출발지) 낱말들(영어, 앞에서부터)이 지금 장소인가. here 는 지금 장소 외형 글의 낱말(_place_words).
    같은 낱말이 외형에 있으면 그렇다('the garden'·'the apple tree' ↔ 'a small garden with an apple tree'), 밖·바깥 말은 장소가 바깥이면, 집·집 안
    말은 장소가 집 안이면, 위층·아래층은 장소가 위층(다락·침실)·아래층(부엌·거실·복도)이면, 바깥끼리는 같은 갈래(정원·잔디·마당)면, 다른 방
    이름('kitchen' ↔ 다락방)·학교·차는 아니다. 출발지의 문·방('out of the room'·'the door')은 지금 장소다. 모르면 None"""
    if not here:
        return None
    fams = [f for f in _OUTDOOR_FAMILIES if here & f]
    indoor = bool(here & _HOME_WORDS) and not fams
    for w in words:
        if w in _PLACE_STOP or len(w) < 2:
            continue
        if w in _PASS_WORDS:
            if w == "room":
                return indoor
            if origin:
                return True
            return None
        if w in here:
            return True
        if w in ("outside", "outdoors"):
            return bool(fams)
        if w == "upstairs":
            return bool(here & _UP_WORDS)
        if w == "downstairs":
            return bool(here & _DOWN_WORDS)
        if w == "bed":
            return bool(here & {"bedroom", "attic", "nursery", "loft"})
        if w in _HOME_ANY:
            return indoor
        if w in _HOME_WORDS:
            return False
        fam = next((f for f in _OUTDOOR_FAMILIES if w in f), None)
        if fam is not None:
            return fam in fams
        if w in _ELSEWHERE_WORDS:
            return False
        return None
    return None


def _ko_place_word(word: str) -> str | None:
    """한국어 장소 낱말 → 영어 장소 낱말 ('부엌' → kitchen, '다락방' → attic). '부엌문'·'현관문' 처럼 문이면 door, 모르면 None"""
    if re.search(r"문$", word):
        return "door"
    return next((en for ko, en in _KO_PLACE_WORDS if word.endswith(ko)), None)


_COME_BEFORE_RE = re.compile(r"\b(?:comes?|coming|came)\s+(?:\w+ly\s+)?$", re.I)
_COME_RE = re.compile(r"(?:comes?|coming|came)\b", re.I)
_DEST_RE = re.compile(r"\b(?:into|onto|to|toward|towards|inside)\s+(?:(?P<det>the|her|his|their|its|a|an|my)\s+)?"
                      r"(?P<w>[a-z][\w'’-]*(?:\s+[a-z][\w'’-]*){0,2})", re.I)
_DEST_ADV_RE = re.compile(r"\b(?:(?P<w>outside|outdoors|home|upstairs|downstairs)|(?P<ud>up|down)\s+the\s+(?:stairs|staircase))\b", re.I)
_ORIGIN_RE = re.compile(r"\b(?:out\s+of|leaves?|leaving|left|exits?|exiting|exited|from|through)\s+(?P<det>the|her|his|their|its|a|an|my)\s+"
                        r"(?P<w>[a-z][\w'’-]*(?:\s+[a-z][\w'’-]*){0,2})", re.I)
_CLAUSE_END_RE = re.compile(r"[,;:]|\b(?:and|then|while|as|but|before|after|when|until|so)\b", re.I)


def _dest_words(det, w) -> list[str]:
    words = re.findall(r"[a-z]+", str(w or "").lower())
    if str(det or "").lower() in ("her", "his", "their", "my") and words[:1] == ["room"]:
        return ["bedroom"]            # 'her room' 은 그 사람의 방
    return words


def _move_dir(sent: str, ms: int, me: int, here: frozenset, kind: str) -> str:
    """움직이는 말 하나(sent[ms:me], kind 는 그 말의 꼴 'leave'·'enter')가 지금 장소(here)를 떠나는가('leave')·들어오는가('enter').
    - 'comes …' 는 늘 들어옴 ('comes running home'·'comes down the stairs')
    - 목적지(into·onto·to the X, outside, home, upstairs, downstairs)가 지금 장소면 들어옴, 다른 곳이면 떠남 (정원 장면의 'runs out into the
      garden' 은 들어옴, 부엌 장면의 'runs out into the garden' 은 떠남, 정원 장면의 'runs into the house' 는 떠남)
    - 목적지가 없고 출발지(out of the X)가 다른 곳이면 들어옴 (정원 장면의 'runs out of the house'·'steps out of the car')
    장소를 모르면 그 말의 꼴 그대로"""
    if kind == "enter":
        if not here or _COME_RE.match(sent, ms):
            return "enter"
        m = _DEST_RE.search(sent[ms:me] + _CLAUSE_END_RE.split(sent[me:], maxsplit=1)[0])
        return "leave" if m and _is_here(_dest_words(m.group("det"), m.group("w")), here) is False else "enter"
    if _COME_BEFORE_RE.search(sent[:ms]):
        return "enter"
    if not here:
        return "leave"
    scope = sent[ms:me] + _CLAUSE_END_RE.split(sent[me:], maxsplit=1)[0]
    for m in _DEST_RE.finditer(scope):
        d = _is_here(_dest_words(m.group("det"), m.group("w")), here)
        if d is not None:
            return "enter" if d else "leave"
    m = _DEST_ADV_RE.search(scope)
    if m:
        d = _is_here([m.group("w").lower() if m.group("w") else ("upstairs" if m.group("ud").lower() == "up" else "downstairs")], here)
        if d is not None:
            return "enter" if d else "leave"
    m = _ORIGIN_RE.search(scope)
    if m and _is_here(_dest_words(m.group("det"), m.group("w")), here, origin=True) is False:
        return "enter"
    return "leave"


def _en_events(sent: str, here: frozenset, cast: dict) -> list[tuple[int, int, str, bool]]:
    """영어 문장의 움직임 [(시작, 끝, 'leave'·'enter', 약한 떠남인가)]. 바람·계획·부정·전하는 말 안의 말은 뺀다. 문장 끝의 맨 'leaves' 는 바로 앞이
    사람 이름·she·he·they·이음말(and·then)·꾸밈말(-ly)일 때만 ('James leaves.'·'gets up and leaves' 는 떠남, 'walks through golden leaves'·
    'covered in moss and leaves' 는 아니다)"""
    out = []
    for rx, kind in ((LEAVE_RE, "leave"), (ENTER_RE, "enter"), (_WEAK_LEAVE_RE, "weak")):
        for m in rx.finditer(sent):
            before = sent[:m.start()]
            if _EN_WISH_BEFORE.search(before) or _EN_REPORT_BEFORE.search(before):
                continue
            if m.group(0).lower() == "leaves":
                prev = re.search(r"([\w’'-]+)\s+$", before)
                word = prev.group(1) if prev else ""
                if not (re.fullmatch(r"she|he|they|and|then|\w+ly", word, re.I) or (not word and before.rstrip().endswith(","))
                        or any(rx2 is not None and rx2.fullmatch(word) for rx2 in cast["names"].values())):
                    continue
                if word.lower() == "and" and _NATURE_BEFORE_AND.search(before):
                    continue
            if kind == "weak":
                out.append((m.start(), m.end(), "leave", True))
            else:
                out.append((m.start(), m.end(), _move_dir(sent, m.start(), m.end(), here, kind), False))
    return out


def _ko_events(sent: str, here: frozenset) -> list[tuple[int, int, str, bool]]:
    """한국어 문장의 움직임 [(시작, 끝, 'leave'·'enter', 주어를 엄격히 볼 말인가)]. 바람·계획·부정은 뺀다. 목적지('정원으로 나간다'·'부엌으로
    들어간다')가 지금 장소면 들어옴, 출발지('집을 나선다')가 다른 곳이면 들어옴. '사라지다' 는 주어가 바로 앞에 분명할 때만 ('니키의 미소가 사라진다'
    는 아니다)"""
    out = []
    for m in _KO_LEAVE_VERB.finditer(sent):
        s, e = m.span()
        before = sent[max(0, s - 16):s]
        word = m.group(0)
        if word.endswith(("나가", "나간", "나갔", "나감")) and not m.group("pre"):
            if _KO_AUX_BEFORE.search(before) or not (_KO_PLACE_BEFORE.search(before) or _KO_CONN_BEFORE.search(before)):
                continue              # '지나가다'·'이어 나가다'·'앞으로 나가다', 장소 말·이어지는 동작 없는 맨 '나가다'
        elif word[-1] in "서선섰" and not m.group("pre") and not re.search(r"(?:을|를)\s*$", before):
            continue                  # '나서다'는 '집을 나서다' 꼴만
        if _KO_WISH_AFTER.match(sent, e):
            continue
        d = "leave"
        p = re.search(r"([가-힣0-9]+?)\s*(으로|로|을|를|에서)\s*(?:[가-힣]*(?:히|게|이)\s+)?$", before)
        eng = _ko_place_word(p.group(1)) if p and here else None
        if eng and eng != "door":
            if p.group(2) in ("으로", "로"):
                d = "enter" if _is_here(["bedroom" if eng == "room" else eng], here) else "leave"
            elif _is_here([eng], here, origin=True) is False:
                d = "enter"
        out.append((s, e, d, False))
    for m in _KO_GO_TO.finditer(sent):
        eng = _ko_place_word(m.group("dest"))
        if eng is None or eng == "door" or _KO_WISH_AFTER.match(sent, m.end()):
            continue
        # 목적지의 '방' 은 그 사람의 방이다 ('방으로 올라간다'): 출발지의 '방'('방을 나간다')만 지금 방. '친구 집에 간다' 의 집은 남의 집
        if eng == "home" and _KO_OWNER_BEFORE.search(sent, 0, m.start("dest")):
            eng = "elsewhere"
        out.append((m.start("dest"), m.end(), "enter" if _is_here(["bedroom" if eng == "room" else eng], here) else "leave", False))
    for m in _KO_GONE_VERB.finditer(sent):
        if not _KO_WISH_AFTER.match(sent, m.end()):
            out.append((m.start(), m.end(), "leave", m.group(0).startswith("사라")))
    for m in _KO_HOME_VERB.finditer(sent):
        if not _KO_WISH_AFTER.match(sent, m.end()):
            out.append((m.start(), m.end(), "leave" if here & {"school", "classroom", "class", "office", "work"} else "enter", False))
    for m in _KO_ENTER_VERB.finditer(sent):
        if not _KO_WISH_AFTER.match(sent, m.end()):
            out.append((m.start(), m.end(), "enter", False))
    return out


def _en_subject(head: str) -> set[str]:
    """아는 이름이 앞에 없는 영어 움직임 말의 주어: 주어가 없으면('Quietly slips out of the room') 화면의 한 사람("+"), 문장 첫 낱말이 모르는
    고유명사면('Niki grabs her bag and runs out', 이름을 아직 모르는 인물) 역시 화면의 한 사람, 사람이 아닌 주어('The candle goes out')나 문장
    가운데의 모르는 이름('Beth smiles as Niki runs out' 의 'Niki' — 화면 밖 사람일 수 있다)·소유격('Niki's smile disappears')이면 아무도 아니다.
    쉼표로 끝나는 이끄는 마디 바로 뒤의 모르는 이름('With a quick goodbye, Niki rushes out', 4차 검토)은 문장 첫 이름처럼 본다"""
    words = [m for m in _NAME_WORD_RE.finditer(head) if m.group(1) not in _NOT_NAME and not m.group(1).endswith(("ly", "ing"))]
    if words:
        last = words[-1]
        lead = head[:last.start()].strip(" \"'(“”")
        if last.group(0).endswith(("'s", "’s")):
            return set()
        return {"+"} if not lead.strip(",") or (len(words) == 1 and lead.endswith(",")) else set()
    return set() if re.search(r"[A-Za-z]", re.sub(r"\b(?:\w+ly|then|now|finally|suddenly|soon|and|but|so)\b", " ", head, flags=re.I)) else {"+"}


def _subjects(sent: str, ms: int, me: int, lang: str, strict: bool, shown: list[str], cast: dict) -> set[str]:
    """움직임 말 sent[ms:me] 의 주어 (인물 id). 가장 가까운 앞 사람 말: 이름(화면 밖 사람도 — 'Beth watches Niki run out of the kitchen door' 는
    니키, 동물은 이름이나 'the puppy' 같은 머리 낱말), 'they'·'모두'(화면의 모두), 'X and Y' 처럼 이어진 이름은 함께, she·he 는 화면에서 그 성별인
    한 사람, 'someone' 은 아무도 아님. 소유격('Niki's smile'·'니키의 미소')은 주어가 아니다. 한국어는 앞에 없으면 가장 가까운 뒤 이름('뛰쳐나가는
    니키'), 그것도 없으면 생략된 주어(화면에 한 사람뿐일 때 그 사람). strict 면(사라지다) 이·가·은·는·도 가 붙은 앞 이름만, 그 사이에 다른 주어가
    없을 때만"""
    refs = [(h.start(), h.end(), c) for c, rx in cast.get("refs", cast["names"]).items() if rx is not None for h in rx.finditer(sent)]
    refs += [(h.start(), h.end(), "*") for h in (_ALL_WHO_RE if lang == "en" else _ALL_WHO_KO_RE).finditer(sent)]
    if lang == "en":
        refs += [(h.start(), h.end(), "+" + h.group(0).lower()) for h in _SUBJECT_PRONOUN_RE.finditer(sent)]
        refs += [(h.start(), h.end(), "-") for h in _OTHER_SUBJECT_RE.finditer(sent)]
    refs = sorted(r for r in refs if not re.match(r"['’]s\b" if lang == "en" else r"의(?:\s|$)", sent[r[1]:]))
    before = [r for r in refs if r[1] <= ms]
    after = [r for r in refs if r[0] >= me]
    if strict:
        if not before or not re.match(r"(?:이|가|은|는|도)\s", sent[before[-1][1]:]) or re.search(r"[가-힣]+(?:이|가)\s", sent[before[-1][1] + 2:ms]):
            return set()
        before = before[-1:]
    if before:
        run = [before[-1]]
        for r in reversed(before[:-1]):          # 'Beth and James walk away': 이어진 이름은 함께 움직인다
            if not _AND_RE.match(sent[r[1]:run[-1][0]]):
                break
            run.append(r)
        who = {r[2] for r in run}
    elif lang == "ko" and after:
        who = {after[0][2]}
    elif lang == "ko":
        who = {"+"}                               # 생략된 주어
    else:
        who = _en_subject(sent[:ms])
    out: set[str] = set()
    for w in who:
        if w == "*":
            out |= set(shown)
        elif w.startswith("+"):
            sex = {"+she": _FEMALE_RE, "+he": _MALE_RE}.get(w)
            cand = [c for c in shown if sex is None or sex.search(_head_phrase(cast["looks"].get(c, "")))]
            if len(cand) == 1:
                out.add(cand[0])
        elif w != "-":
            out.add(w)
    return out


def _moves(sc: dict, shown: list[str], cast: dict) -> dict[str, list[str]]:
    """장면 sc 에서 움직이는 사람(동물도)과 그 움직임들 {id: ['enter'·'leave', … 글 차례대로]} (영어 action·shot, 없으면 한국어 ko). 한 사람의
    마지막 움직임이 그 장면 뒤의 자리를 정한다 ('rushes in, grabs her hat and runs out' 은 떠남). 약한 떠남('closes the door behind her')은 같은
    장면에서 그 사람이 들어오면 세지 않는다. 영어 글에 움직임이 있는 사람은 영어 글을 따른다"""
    here = _place_words((cast.get("places") or {}).get(_s(sc.get("place")), ""))
    found: dict[str, dict[str, list[tuple[str, bool]]]] = {"en": {}, "ko": {}}
    for text, lang in ((_s(sc.get("action")) + ". " + _s(sc.get("shot")), "en"), (_s(sc.get("ko")), "ko")):
        for sent in re.split(r"[.!?;]+", text):
            events = _en_events(sent, here, cast) if lang == "en" else _ko_events(sent, here)
            for ms, me, d, flag in sorted(events):
                for c in _subjects(sent, ms, me, lang, flag and lang == "ko", shown, cast):
                    found[lang].setdefault(c, []).append((d, flag and lang == "en"))
    out: dict[str, list[str]] = {}
    for lang in ("ko", "en"):
        for c, evs in found[lang].items():
            entered = any(d == "enter" for d, _w in evs)
            out[c] = [d for d, weak in evs if not (weak and entered)] or out.get(c, [])
    return {c: v for c, v in out.items() if v}


def _movers(sc: dict, shown: list[str], cast: dict, kind: str = "leave") -> set[str]:
    """장면 sc 에서 그 장소를 떠나는(kind 'leave': 마지막 움직임이 떠남) 사람·들어오는(kind 'enter': 들어오는 움직임이 있는) 사람 (_moves).
    알 수 없으면 아무도 아니다. 시험·진단용: 앱은 _moves 를 바로 쓴다 (_run_state·_first_seen_here·_listed_gone, 5차 검토)"""
    moves = _moves(sc, shown, cast)
    if kind == "leave":
        return {c for c, d in moves.items() if d[-1] == "leave"}
    return {c for c, d in moves.items() if "enter" in d}


def _leavers(sc: dict, shown: list[str], cast: dict) -> set[str]:
    """장면 sc 에서 그 장소를 떠나는 사람 (_movers). 화면 밖에서 떠난 사람도 들어 있을 수 있다. 시험·진단용 (앱은 _moves 를 바로 쓴다)"""
    return _movers(sc, shown, cast, "leave")


# 그 자리에 없음을 말하는 글 (4차 검토: '둘만 남았다'·"Niki's chair is empty"·'with Niki at school'·'while Niki sleeps upstairs'·'After Niki has
# left' 를 보지 않아 앞 장면의 니키를 다음 대화 컷에 다시 앉혔다). 'alone' 은 말 없는 장면에서만 본다 (독백의 'alone with her thoughts' 는 아니다)
_ALONE_RE = re.compile(r"(?<!feel )(?<!feels )(?<!feeling )(?<!felt )(?<!look )(?<!looks )(?<!seem )(?<!seems )(?<!let )(?<!leave )(?<!leaves )"
                       r"\b(?:all\s+)?alone\b(?!\s+(?:with|in)\s+(?:her|his|their)\s+thoughts)"
                       r"|\bby\s+themselves\b|\bon\s+their\s+own\b|\b(?:just|only)\s+the\s+(?:two|three)\s+of\s+them\b|\bthe\s+(?:two|three)\s+of\s+them\s+alone\b"
                       # 5차 검토: 'Only Beth and James remain'·'Only the parents remain'·'Now it is just Beth and James' (이름은 대문자로 시작할 때만)
                       r"|\b(?:only|just)\s+(?:(?-i:[A-Z][a-z]+)(?:\s+(?:and|&)\s+(?-i:[A-Z][a-z]+))?|the\s+(?:two|three|parents|couple|adults|grown-?ups))"
                       r"\s+(?:remains?|are\s+left|is\s+left)\b|\bit\s+is\s+(?:now\s+)?(?:just|only)\s+(?-i:[A-Z][a-z]+)\b", re.I)
_KO_ALONE_RE = re.compile(r"둘만|단둘이|둘이서만|두\s*사람만|부부만|셋만|[가-힣]+만\s*남(?:았|은|아|고|는|겨)"
                          r"|혼자(?:서)?\s*(?:남|앉|서\s|서\s*있|있|기다|먹|지내)|홀로")
_ALONE_OTHERS_RE = re.compile(r"\b(?:parents|family|others|everyone|everybody|mother|father|mom|mum|dad|brother|sister|friends?)\b|부모|가족|엄마|아빠",
                              re.I)
_ABSENT_AFTER_RE = re.compile(
    r"['’]s\s+(?:empty\s+(?:chair|seat|place|spot|bed)|(?:chair|seat|place|spot|bed)\s+(?:is|sits|stays|remains|lies|stands)\s+(?:now\s+|still\s+)?empty)"
    r"|\s+(?:has|had)\s+(?:already\s+|just\s+|long\s+)?(?:left|gone)\b(?!\s+(?:quiet|silent|still|pale|red|white|cold|dark|numb|soft|stiff|wrong"
    r"|well|bad))"
    r"|\s+(?:is|was)\s+(?:already\s+|now\s+|long\s+|still\s+)?(?:gone|away|out|at\s+(?:school|work|kindergarten|class|camp|practice)"
    r"|asleep\s+(?:upstairs|in\s+(?:her|his|their)\s+room)|upstairs|not\s+(?:here|there|home|around|back))\b"
    r"|\s+(?:sleeps|is\s+sleeping)\s+(?:upstairs|in\s+(?:her|his|their)\s+room)\b", re.I)
_ABSENT_CTX_BEFORE = re.compile(r"\b(?:with|while|after|now\s+that|since|until)\s+$", re.I)
_ABSENT_CTX_AFTER = re.compile(r"\s+(?:is\s+|was\s+|has\s+|had\s+)?(?:at\s+(?:school|work|kindergarten|class|camp|practice)|away|asleep|sleeps?"
                               r"|sleeping|gone|left|leaves|went|goes\s+(?:to|off|out)|upstairs|outside|out\b|plays?\s+outside|playing\s+outside"
                               r"|in\s+(?:her|his|their)\s+room)", re.I)
_KO_ABSENT_AFTER = re.compile(r"(?:이|가|은|는|도)?\s*(?:없(?:는|이|다|어|고|자|었)|학교에\s*(?:가|갔|간|있)|회사에\s*(?:가|갔|간|있)"
                              r"|위층에서\s*자|(?:자기\s*)?방에서\s*자|자는\s*동안|잠든\s*(?:사이|동안)|(?:떠난|나간|학교에\s*간)\s*(?:뒤|후|사이))"
                              r"|(?:의)?\s*빈\s*(?:의자|자리)|(?:의)?\s*(?:자리|의자)(?:가|는|도)?\s*(?:비었|비어|텅\s*비)")


def _screen_roles(shown: list[str], cast: dict) -> str:
    """화면 사람을 부를 수 있는 가족 말 (5차 검토: '부모님만 남은 식탁'·'Mom and Dad are alone' 의 부모·엄마·아빠를 화면에 없는 사람으로 보아 없음
    글을 버렸다): 화면에 어른 여자가 있으면 엄마 말, 어른 남자가 있으면 아빠 말, 둘 다 있으면 부모·부부 말"""
    heads = " ".join(_head_phrase(cast["looks"].get(c, "")) for c in shown)
    woman = bool(re.search(r"\b(?:woman|lady|mother|wife|grandmother|grandma)\b", heads, re.I))
    man = bool(re.search(r"\b(?:man|gentleman|father|husband|grandfather|grandpa)\b", heads, re.I))
    return " ".join((["mother mom mum 엄마 어머니"] if woman else []) + (["father dad 아빠 아버지"] if man else [])
                    + (["parents couple 부모 부부"] if woman and man else []))


def _absent(sc: dict, shown: list[str], cast: dict, alone: bool = True) -> set[str]:
    """장면 sc 의 글이 그 자리에 없다고 말하는 사람 (화면에 나오지 않는 사람만, 같은 사람의 다른 모습도): 'Niki's chair is empty'·'with Niki at
    school'·'while Niki sleeps upstairs'·'After Niki has left'·'니키가 없는'·'니키의 빈 의자'. alone 이면 말 없는 장면이 'alone'·'둘만 남았다'·
    'just the two of them' 이라고 말할 때 화면에 나오지 않는 사람 모두"""
    person = cast["person"]
    mine = {person.get(c, c) for c in shown}
    people = [c for c, look in cast["looks"].items() if not is_animal_look(look) and person.get(c, c) not in mine]
    if not people:
        return set()
    en = _s(sc.get("action")) + ". " + _s(sc.get("shot"))
    ko = _s(sc.get("ko"))
    if alone and shown and _s(sc.get("kind")) != "talk":
        others = [cast["names"][c] for c in people if cast["names"].get(c) is not None]
        mine_words = (" ".join(f'{cast["looks"].get(c, "")} {_ROLE_WORDS.get((cast.get("ko") or {}).get(c, ""), "")} '
                               f'{(cast.get("ko") or {}).get(c, "")}' for c in shown) + " " + _screen_roles(shown, cast)).lower()
        for sent, rx in [(x, _ALONE_RE) for x in re.split(r"[.!?;]+", en)] + [(x, _KO_ALONE_RE) for x in re.split(r"[.!?;]+", ko)]:
            # 그 문장이 화면에 없는 사람(이름·부모·가족 말)을 함께 적으면 그 사람은 그 자리에 있을 수 있다 ('while her parents chat at the table').
            # 화면의 사람을 부르는 말('Mom and Dad are alone' 의 엄마·아빠)은 다른 사람이 아니다
            if rx.search(sent) and not any(o.search(sent) for o in others) and not any(
                    m.group(0).lower() not in mine_words for m in _ALONE_OTHERS_RE.finditer(sent)):
                return set(people)
    out: set[str] = set()
    for c in people:
        rx = cast["names"].get(c)
        if rx is None:
            continue
        if any(_ABSENT_AFTER_RE.match(en, h.end()) or (_ABSENT_CTX_BEFORE.search(en, 0, h.start()) and _ABSENT_CTX_AFTER.match(en, h.end()))
               for h in rx.finditer(en)) or any(_KO_ABSENT_AFTER.match(ko, h.end()) for h in rx.finditer(ko)):
            out.add(c)
    return out


# 화면 밖 목소리 (4차 검토: 문 밖에서 부르는 엄마를 talk 장면(characters 비움)으로 쓴 구성안을 다듬기가 다락방의 투샷으로 만들었다).
# 5차 검토: sound 칸의 배경 소리('birds chirping from outside'·'soft rain can be heard on the window')에도 걸려, characters 에 말하는 사람을 빠뜨린
# 보통 대사 장면이 목소리만 얹은 shot 이 되었다(말하는 사람이 자기 대사 컷에서 사라졌다) → 화면 밖 말(from outside·behind the door …)은 말하는
# 목소리 말(voice·calls·shouts·says …)과 같은 마디에 있을 때만 센다. 장면 글(action·shot)과 한국어 글(ko)을 보고, sound 는 'voice' 가 든 마디만.
# 전화('on the phone')는 목소리 말이 없어도 화면 밖이다 (말하는 사람이 적히지 않은 장면에서만: 적힌 사람의 'talks on the phone' 은 화면 속 통화)
_VOICE_WORD_RE = re.compile(r"\b(?:voices?|calls?|calling|called|shouts?|shouting|shouted|yells?|yelling|yelled|says?|saying|said|speaks?"
                            r"|speaking|spoke|talks?|talking|talked|whispers?|whispering|whispered|answers?|answering|answered|replies|replying"
                            r"|replied|asks?|asking|asked|hollers?|hollering|hollered|cries\s+out|cried\s+out|crying\s+out)\b", re.I)
_OFF_PLACE = (r"\boff[\s-]*screen\b|\bout\s+of\s+(?:frame|shot|view|sight)\b|\bfrom\s+(?:outside|behind|beyond|downstairs|upstairs|another\s+room"
              r"|the\s+(?:next|other)\s+room|afar|a\s+distance|the\s+other\s+side)\b|\bbehind\s+the\s+(?:closed\s+)?(?:door|wall|curtain)\b"
              r"|\bunseen\b|\bvoice[\s-]*over\b|\bvoice\s+only\b")
_OFF_MARK_RE = re.compile(_OFF_PLACE + r"|\b(?:outside|through)\s+the\s+(?:closed\s+)?(?:door|wall|window)\b|\b(?:can\s+be|is)\s+heard\b", re.I)
# 말하는 사람이 characters 에 적힌 장면은 방향이 분명한 말만 ('calls through the window' 는 안에서 밖으로 부르는 것일 수도 있다)
_OFF_STRICT_RE = re.compile(_OFF_PLACE + r"|\bthrough\s+the\s+closed\s+(?:door|wall)\b", re.I)
_PHONE_RE = re.compile(r"\b(?:on|over|through|into)\s+the\s+(?:phone|telephone|intercom|radio)\b", re.I)
_OFF_CLAUSE_RE = re.compile(r"[,;:]|\b(?:while|as|but|when|before|after|until|whereas)\b", re.I)
_OFF_LEAD_RE = re.compile(r"\s*(?:from|behind|through|outside|off)\b", re.I)
_KO_OFF_PLACE = r"문\s*밖|화면\s*밖|아래층에서|위층에서|문\s*너머|벽\s*너머|다른\s*방에서|멀리서"
_KO_OFF_MARK_RE = re.compile(_KO_OFF_PLACE + r"|밖에서|건너편")
_KO_OFF_STRICT_RE = re.compile(_KO_OFF_PLACE)
_KO_VOICE_RE = re.compile(r"목소리|부르|부른|불러|외치|외친|외쳐|소리치|소리친|소리쳐|고함|말한|말하|말해|대답|묻|물어|속삭|통화")
_KO_VOICE_OFF_RE = re.compile(r"목소리만|부르는\s*목소리|목소리가\s*들")
_KO_PHONE_RE = re.compile(r"전화(?:로|기|\s*너머|\s*속|\s*통화)|수화기")


def _clauses(text: str) -> list[str]:
    """글을 마디로 (문장 → 쉼표·while·as …). 문장 앞의 화면 밖 말 마디('From behind the door, Beth calls')는 다음 마디와 한 마디로 본다"""
    out = []
    for sent in re.split(r"[.!?]+", str(text or "")):
        parts = [p for p in _OFF_CLAUSE_RE.split(sent) if p and p.strip()]
        if len(parts) > 1 and _OFF_LEAD_RE.match(parts[0]) and not _VOICE_WORD_RE.search(parts[0]):
            parts = [parts[0] + " " + parts[1]] + parts[2:]
        out += parts
    return out


def _voice_off_text(sc: dict, strict: bool = False) -> bool:
    """장면 글이 말하는 목소리가 화면 밖(문 밖·벽 너머·다른 방·전화)에서 온다고 말하는가 (5차 검토). strict 면(말하는 사람이 characters 에 적힌
    장면) 방향이 분명한 말만 보고, 전화는 'voice' 가 함께일 때만 ("Grandma's voice comes over the phone" — 'talks on the phone' 은 화면 속 통화)"""
    off = _OFF_STRICT_RE if strict else _OFF_MARK_RE
    for cl in _clauses(_s(sc.get("action")) + ". " + _s(sc.get("shot"))):
        if _PHONE_RE.search(cl) and (not strict or re.search(r"\bvoices?\b", cl, re.I)):
            return True
        if off.search(cl) and _VOICE_WORD_RE.search(cl):
            return True
    for cl in _clauses(_s(sc.get("sound"))):
        if re.search(r"\bvoices?\b", cl, re.I) and (off.search(cl) or _PHONE_RE.search(cl)):
            return True
    for sent in re.split(r"[.!?]+", _s(sc.get("ko"))):
        if _KO_VOICE_OFF_RE.search(sent) or (_KO_PHONE_RE.search(sent) and (not strict or "목소리" in sent)):
            return True
        if (_KO_OFF_STRICT_RE if strict else _KO_OFF_MARK_RE).search(sent) and _KO_VOICE_RE.search(sent):
            return True
    return False


def _offscreen_voice(sc: dict) -> bool:
    """대사가 있는 장면인데 말하는 사람이 characters 에 없고 장면 글이 화면 밖 목소리라고 말하는가 (문 밖·벽 너머·다른 방·전화, _voice_off_text).
    말하는 사람이 적힌 장면은 앞 장면도 보고 정한다 (_listed_voice_off)"""
    who = _s(sc.get("speaker"))
    if not who or not _s(sc.get("line_ko")) or who in {_s(c) for c in sc.get("characters") or []}:
        return False
    return _voice_off_text(sc)


def _same_run(a, b) -> bool:
    """두 장면이 같은 장소·같은 시간인가 (자리 사람을 이어 받는 구간)"""
    return (isinstance(a, dict) and isinstance(b, dict) and _s(a.get("place")) == _s(b.get("place"))
            and _s(a.get("time")) == _s(b.get("time")))


def _run_start(scenes: list, i: int) -> int:
    """장면 i 가 든 같은 장소·시간 연속 구간의 첫 장면"""
    start = i
    while start > 0 and _same_run(scenes[start - 1], scenes[i]):
        start -= 1
    return start


def _run_state(scenes: list, i: int, cast: dict, why: dict | None = None) -> tuple[list[str], set]:
    """장면 i 가 끝날 때 그 장소의 사람: (있는 사람 id, 가장 나중에 나온 사람부터 / 떠났거나 없다고 적힌 사람 열쇠). 같은 장소의 연속 구간(장소·
    시간이 바뀌기 전까지)을 거슬러 올라가며 화면에 나온 사람 모두 — 화면에서 빠진 사람은 그 자리에서 나간 것이 아니다 (11-24 3차 검토). 그
    장소를 떠난 사람(_moves 의 마지막 움직임이 떠남, 화면 밖에서 떠난 사람도)과 없다고 적힌 사람(_absent, 4차 검토)은 다시 나오기 전까지 넣지
    않고, 화면 밖에서 들어온 사람('Beth looks up as Niki walks in')은 넣는다. 같은 사람의 다른 모습(cast person)은 한 사람으로 세어 가장 나중에
    나온 모습만 남긴다. 동물은 세지 않는다. why 를 주면 없는 사람마다 까닭을 적는다 {사람 열쇠: ("left"·"away", 장면 번호)} (검사 문구, 5차 검토)"""
    out: list[str] = []
    gone: set = set()
    if not (0 <= i < len(scenes)) or not isinstance(scenes[i], dict):
        return out, gone
    why = {} if why is None else why
    person = cast["person"]
    looks = cast["looks"]
    seen: set = set()
    k = i
    while k >= 0 and _same_run(scenes[k], scenes[i]):
        sc = scenes[k]
        shown = _shown(sc, looks)
        last = {c: d[-1] for c, d in _moves(sc, shown, cast).items() if c in looks and not is_animal_look(looks[c])}
        for c, d in last.items():
            p = person.get(c, c)
            if d == "leave" and c not in shown and p not in seen:
                gone.add(p)                  # 화면 밖에서 떠났다
                why.setdefault(p, ("left", k))
        for c in _absent(sc, shown, cast):
            if person.get(c, c) not in seen:
                gone.add(person.get(c, c))   # 그 자리에 없다고 적혔다
                why.setdefault(person.get(c, c), ("away", k))
        for c in shown:
            p = person.get(c, c)
            if p in seen or p in gone:
                continue
            if last.get(c) == "leave":
                gone.add(p)                  # 이 장면에서 떠났다: 더 앞 장면에 있었어도 이제 그 자리에 없다
                why.setdefault(p, ("left", k))
            else:
                seen.add(p)
                out.append(c)
        for c, d in last.items():
            p = person.get(c, c)
            if d == "enter" and c not in shown and p not in seen and p not in gone:
                seen.add(p)                  # 화면 밖에서 들어왔다
                out.append(c)
        k -= 1
    return out, gone


def _presence(scenes: list, i: int, cast: dict) -> list[str]:
    """장면 i 가 끝날 때 그 장소에 있는 사람 (다음 대화 컷이 이어 받는다, G1): _run_state 의 있는 사람"""
    return _run_state(scenes, i, cast)[0]


def _first_seen_here(scenes: list, i: int, x: str, cast: dict, near: set | None = None) -> bool:
    """구간 앞에서 나온 적이 없는 x 가 장면 i 의 순간 이미 그 자리에 있는가: 같은 장소의 뒤 장면을 차례로 보아, 처음 보이는 장면이 그 사람이
    들어오는 장면이 아니면 있다 (near 를 주면 그 장면에 near 의 사람 — 말하는 사람·듣는 사람 — 도 함께 나와야 한다). 화면에 나오기 전에 목소리로만
    나오거나, 없다고 적히거나, 화면 밖에서 들어오면 없다. 화면 밖에서 떠나면 그때까지 있었다"""
    person = cast["person"]
    px = person.get(x, x)
    looks = cast["looks"]
    same = lambda c: person.get(_s(c), _s(c)) == px
    k = i + 1
    while k < len(scenes) and _same_run(scenes[k], scenes[i]):
        s2 = scenes[k]
        shown = _shown(s2, looks)
        moves = _moves(s2, shown, cast)
        me = next((c for c in shown if same(c)), None)
        if me:
            if "enter" in moves.get(me, ()):
                return False
            return near is None or any(person.get(c, c) in near for c in shown if c != me)
        if same(s2.get("speaker")) and HANGUL_RE.search(_s(s2.get("line_ko"))):
            return False                          # 화면 밖 목소리
        if any(same(c) for c in _absent(s2, shown, cast)):
            return False
        mine = next((d for c, d in moves.items() if same(c)), None)
        if mine:
            return mine[-1] == "leave" and near is None
        k += 1
    return False


# 자리에 앉는 말 ('sits down at the table'·'takes her seat at'·'pulls up a chair'·'joins them at the table'): 그 방 안에서 자리에 앉는 것일 수도 있다
_SEAT_ENTER_RE = re.compile(r"\b(?:sits?|sitting|sat)\s+down\s+at\s+the\s+(?:\w+\s+)?table\b|\btakes?\s+(?:her|his|their)\s+(?:seat|place)\s+at\b"
                            r"|\bpulls?\s+up\s+a\s+chair\b|\bjoins?\s+(?:\w+\s+){1,3}at\s+the\s+(?:\w+\s+)?table\b", re.I)


def _enters_at(scenes: list, i: int, x: str, cast: dict) -> int:
    """x(같은 사람의 다른 모습도)가 장면 i 뒤 같은 장소 구간에서 처음 보이거나 움직이는 장면이 그 자리에 들어오는 장면이면 그 번호, 아니면 -1
    ('니키야, 내려와!' 뒤의 'Niki comes down the stairs'). 구성안이 적은 사람을 없다고 보는 증거라, 자리에 앉는 말만 있는 장면('Niki sits down at the
    table' — 그 방 안에서 앉는 것일 수 있다)은 들어옴으로 치지 않는다"""
    person = cast["person"]
    px = person.get(x, x)
    k = i + 1
    while k < len(scenes) and _same_run(scenes[k], scenes[i]):
        s2 = scenes[k]
        shown = _shown(s2, cast["looks"])
        mine = next((d for c, d in _moves(s2, shown, cast).items() if person.get(c, c) == px), None)
        if mine or any(person.get(c, c) == px for c in shown):
            en = _s(s2.get("action")) + ". " + _s(s2.get("shot"))
            seated = [m for m in ENTER_RE.finditer(en)] and all(_SEAT_ENTER_RE.match(en, m.start()) for m in ENTER_RE.finditer(en))
            return k if mine and "enter" in mine and not (seated and not _KO_ENTER_VERB.search(_s(s2.get("ko")))) else -1
        k += 1
    return -1


def _listed_gone(scenes: list, i: int, x: str, cast: dict) -> tuple[str, int]:
    """구성안이 장면 i 의 characters 에 적은 사람 x(같은 사람의 다른 모습도)가 그 순간 그 자리에 없다는 증거와 그 장면 (5차 검토: 적힌 사람은
    장면 글이 무엇이라 하든 그 자리에 있는 것으로 보아, 방금 뛰어나간 니키·아직 내려오지 않은 니키·'둘만 남았다' 뒤의 니키·문 밖이나 전화 속
    목소리를 대화 컷에 그렸고 검사도 조용했다). ("left", k) 그 장소를 떠났다 / ("away", k) 없다고 적혔다(이 장면 글도) / ("voice", k) 화면 밖
    목소리로만 나왔다 / ("later", k) 뒤 장면에서 들어온다 — 아직 없다 / ("", -1) 증거가 없다 (적힌 대로 있다). 이 장면에서 들어오면 있다"""
    person = cast["person"]
    px = person.get(x, x)
    same = lambda c: person.get(_s(c), _s(c)) == px
    sc = scenes[i]
    shown = _shown(sc, cast["looks"])
    if any(same(c) and "enter" in d for c, d in _moves(sc, shown, cast).items()):
        return "", -1
    if any(same(c) for c in _absent(sc, [c for c in shown if not same(c)], cast, alone=False)):
        return "away", i
    start = _run_start(scenes, i)
    if i > start:
        why: dict = {}
        out, gone = _run_state(scenes, i - 1, cast, why)
        if any(same(c) for c in out):
            return "", -1
        if px in gone:
            return why.get(px, ("left", i - 1))
    for k in range(start, i):
        if same(scenes[k].get("speaker")) and HANGUL_RE.search(_s(scenes[k].get("line_ko"))):
            return "voice", k                     # 앞에서 화면 밖 목소리로만 나왔다
    k = _enters_at(scenes, i, x, cast)
    return ("later", k) if k >= 0 else ("", -1)


def _gone_text(reason: str, k: int) -> str:
    """_listed_gone 의 까닭을 검사 문구로 (장면 번호는 1부터)"""
    if reason == "left" and k >= 0:
        return f"left the place in scene {k + 1}"
    if reason == "away" and k >= 0:
        return f"scene {k + 1} says they are not there"
    if reason == "voice" and k >= 0:
        return f"only heard off-screen in scene {k + 1}"
    if reason == "later" and k >= 0:
        return f"comes into the place only in scene {k + 1}"
    return "left the place, or is only heard off-screen"


def _listed_voice_off(scenes: list, i: int, cast: dict) -> bool:
    """대사 장면 i 의 말하는 사람이 characters 에 적혔는데 장면 글이 그 사람의 목소리가 화면 밖에서 온다고 분명히 말하고(_voice_off_text strict:
    'calls through the closed door'·'calls from downstairs'·"Grandma's voice comes over the phone"·'문 밖에서 엄마가 부른다'), 그 사람이 같은 장소
    구간의 앞 장면에서 그 자리에 있지 않았는가 (5차 검토: 구성안이 문 밖의 엄마·전화 속 할머니를 적어 다락방·부엌의 투샷으로 그렸다). 이 장면에서
    들어오면 화면 속이다"""
    sc = scenes[i]
    who = _s(sc.get("speaker"))
    if (_s(sc.get("kind")) != "talk" or not who or not HANGUL_RE.search(_s(sc.get("line_ko")))
            or who not in {_s(c) for c in sc.get("characters") or []} or not _voice_off_text(sc, strict=True)):
        return False
    person = cast["person"]
    me = person.get(who, who)
    if "enter" in _moves(sc, _shown(sc, cast["looks"]), cast).get(who, ()):
        return False
    if i > _run_start(scenes, i):
        out, _gone = _run_state(scenes, i - 1, cast)
        if any(person.get(c, c) == me for c in out):
            return False
    return True


def _animal_here(scenes: list, i: int, x: str, cast: dict) -> bool:
    """동물 x 가 대사 장면 i 의 그 장소에 있는가 (4차 검토: 자리 판정이 사람만 세어, 그 자리의 강아지에게 하는 말이 독백이 됐다): 같은 장소 구간의
    앞 장면에 적혔고 그 뒤에 떠나지 않았으면('the puppy runs off') 있다. 앞에 없으면 뒤에서 처음 적힌 장면이 들어오는 장면이 아니면 있다"""
    looks = cast["looks"]
    start = _run_start(scenes, i)
    for k in range(i - 1, start - 1, -1):
        s = scenes[k]
        d = _moves(s, _shown(s, looks), cast).get(x)
        if d:
            return d[-1] == "enter"
        if x in {_s(c) for c in s.get("characters") or []}:
            return True
    k = i + 1
    while k < len(scenes) and _same_run(scenes[k], scenes[i]):
        s = scenes[k]
        d = _moves(s, _shown(s, looks), cast).get(x) or []
        if x in {_s(c) for c in s.get("characters") or []}:
            return "enter" not in d
        if d:
            return d[-1] == "leave"
        k += 1
    return False


def _here(scenes: list, i: int, x: str, cast: dict) -> bool:
    """듣는 사람 x 가 대사 장면 i 의 그 장소에 있는가 (사용자 확정 규칙 G0: '그 자리에 있는 사람에게 하는 말'). 11-24 3차 검토: 문 밖에서 부르는
    엄마·방금 뛰어나간 니키를 그 사람에게 하는 말이라고 대화 컷에 넣었다.
    - 이 장면이 적은 사람(characters)이면 그 자리에 없다는 증거(_listed_gone: 떠남·없음 글·화면 밖 목소리·뒤에서 들어옴)가 없을 때 있다 (5차 검토).
      동물은 동물 판정(_animal_here, 4차 검토)
    - 이 장면 글이 없다고 적었으면('while Niki sleeps upstairs') 없다 (4차 검토)
    - 같은 장소 구간의 앞 장면에서 화면에 나왔고 떠나지 않았으면(_run_state) 있다. 떠났거나 없다고 적혔거나('둘만 남았다'), 화면 밖 목소리로만
      나왔으면 없다
    - 앞에 나온 적이 없으면 같은 장소의 뒤 장면을 본다 (_first_seen_here: 구성안이 대화의 첫 줄에서 상대를 characters 에 빠뜨렸다)
    같은 사람의 다른 모습은 같은 사람이다"""
    person = cast["person"]
    px = person.get(x, x)
    same = lambda c: person.get(_s(c), _s(c)) == px
    sc = scenes[i]
    looks = cast["looks"]
    if any(same(c) for c in sc.get("characters") or []):
        return is_animal_look(looks.get(x, "")) or not _listed_gone(scenes, i, x, cast)[0]
    if is_animal_look(looks.get(x, "")):
        return _animal_here(scenes, i, x, cast)
    if any(same(c) for c in _absent(sc, _shown(sc, looks), cast, alone=False)):
        return False
    start = _run_start(scenes, i)
    if i > start:
        out, gone = _run_state(scenes, i - 1, cast)
        if any(same(c) for c in out):
            return True
        if px in gone:
            return False
    for k in range(start, i):
        if any(same(c) for c in _shown(scenes[k], looks)) or same(scenes[k].get("speaker")):
            return False                          # 화면 밖 목소리로만 나왔다
    return _first_seen_here(scenes, i, x, cast)


def _place_people(scenes: list, i: int, cast: dict, who: str = "", lis: str = "", to_all: bool = False, ahead: bool = True) -> list[str]:
    """대사 장면 i 의 순간 그 장소에 있는 사람 (말하는 사람 빼고, 사람만, 같은 사람의 다른 모습은 하나): 같은 장소 구간의 앞 장면들에서 이어 온
    사람(_run_state — 떠났거나 없다고 적힌 사람과 이 장면 글이 없다고 적은 사람은 뺀다)과, ahead 면 구간 앞에서 나온 적이 없는 사람 가운데 구간
    뒤에서 처음 보이는 장면이 들어오는 장면이 아니고 그 장면에 말하는 사람이나 듣는 사람도 함께 나오는 사람 — 모두에게 하는 말은 함께 나오지
    않아도 (_first_seen_here). 4차 검토: 구간 첫 장면의 모두에게 하는 말('다녀왔습니다!')이 혼자 하는 말이 되고 검사도 조용했다"""
    sc = scenes[i]
    person = cast["person"]
    looks = cast["looks"]
    start = _run_start(scenes, i)
    out, gone = _run_state(scenes, i - 1, cast) if i > start else ([], set())
    absent = {person.get(c, c) for c in _absent(sc, _shown(sc, looks), cast, alone=False)}
    me = person.get(who, who)
    res = [c for c in out if person.get(c, c) not in absent and person.get(c, c) != me]
    known = {person.get(c, c) for c in out} | gone | absent | {me}
    for k in range(start, i):                # 구간 앞에서 화면에 나왔거나 목소리로만 나온 사람은 뒤를 보지 않는다
        sp = _s(scenes[k].get("speaker"))
        known |= {person.get(c, c) for c in _shown(scenes[k], looks)} | ({person.get(sp, sp)} if sp else set())
    near = None if to_all else {me} | ({person.get(lis, lis)} if lis else set())
    for c, look in (looks.items() if ahead else ()):
        p = person.get(c, c)
        if is_animal_look(look) or p in known:
            continue
        if _first_seen_here(scenes, i, c, cast, near):
            res.append(c)
            known.add(p)
    return res


def _look_here(scenes: list, i: int, x: str, cast: dict) -> str:
    """듣는 사람 x 를 그 장면이 보이는 모습의 id 로 (11-24 3차 검토: 안경 쓴 제임스가 화면에 있는데 listener 를 'james' 로 적으면 안경 안 쓴
    제임스로 바뀌어 같은 식탁에서 모습이 바뀌고 다음 컷 잇기도 끊겼다): 이 장면이 적은 모습, 없으면 그 자리에 가장 나중에 보인 모습, 그 밖에는 x"""
    person = cast["person"]
    px = person.get(x, x)
    for c in scenes[i].get("characters") or []:
        if person.get(_s(c), _s(c)) == px:
            return _s(c)
    if i > 0 and _same_run(scenes[i - 1], scenes[i]):
        for c in _presence(scenes, i - 1, cast):
            if person.get(c, c) == px:
                return c
    return x


def _people_of(ids, looks: dict) -> list[str]:
    """id 가운데 사람(동물 아님), 차례대로 한 번씩"""
    return [c for c in dict.fromkeys(ids) if c in looks and not is_animal_look(looks[c])]


# 독백 컷의 장면 글에서 다른 사람을 가리키는 말 (11-24 2차 검토): 독백은 같은 장소에 다른 사람이 있어도 혼자 클로즈업이라(G0) 'glances at her
# mother and father' 같은 말이 화면 밖 사람을 그려 넣는다 (11-21 실험 R3: 장면 글의 화면 밖 이름이 사람을 하나 더 만들었다)
# 4차 검토: 사람을 가리키는 보통 명사('the girl'·'the woman beside him'·'his little girl'·'the kid'·'the two of them')도 (말하는 사람 외형에 든 말은 뺀다)
_OTHER_PEOPLE_RE = re.compile(r"\b(?:mother|mom|mum|mommy|mama|father|dad|daddy|papa|parents?|family|brothers?|sisters?|siblings?|sons?|"
                              r"daughters?|husband|wife|grand(?:mother|ma|father|pa|parents?)|granny|uncles?|aunts?|cousins?|friends?|"
                              r"teachers?|others|everyone|everybody|someone|somebody|each other|one another|girls?|boys?|wom[ae]n|m[ae]n"
                              r"|child(?:ren)?|kids?|lady|ladies|gentlem[ae]n|bab(?:y|ies)|toddlers?|teen(?:ager)?s?|people|persons?"
                              r"|the\s+(?:two|three|four|others)\s+of\s+them"
                              # 5차 검토: 독백은 말하는 사람 한 명이라 사람 동작의 they·them 은 다른 사람이다 ('watches them laugh'·'glances at them'·'While
                              # they chat'·'the two adults'·'from one to the other'). 물건의 them('smells them'·'looks at them')은 아니다
                              r"|the\s+(?:two|three|four)\s+(?:adults|grown-?ups|grownups|others)|(?:from\s+)?one\s+to\s+the\s+other"
                              r"|they\s+(?:\w+ly\s+)?(?:chat|talk|laugh|argu|whisper|eat|smil|giggl|bicker|discuss|jok|sing|hug|kiss|danc|play|leav|look"
                              r"|watch|nod|speak|say|teas|wav|toast|cheer|clap|grin)\w*"
                              r"|them\s+(?:laugh|talk|chat|argu|whisper|eat|smil|giggl|bicker|hug|kiss|danc|play|leav|go|walk|sing|jok)\w*"
                              r"|(?:glanc|smil|grin|wink|nodd?|wav|peek|beam|frown)\w*\s+at\s+them|watch(?:es|ed|ing)?\s+them)\b", re.I)
# 한글 이름이 역할 말인 인물('엄마'·'할아버지')은 장면 글이 그 역할 말로 그 사람을 부른다 ('Mom smiles to herself' 는 엄마 자신)
_ROLE_WORDS = {"엄마": "mother mom mum mommy mama", "어머니": "mother mom mum", "아빠": "father dad daddy papa", "아버지": "father dad",
               "할머니": "grandmother grandma granny", "할아버지": "grandfather grandpa", "언니": "sister", "누나": "sister", "오빠": "brother",
               "형": "brother", "선생님": "teacher"}
_SHE_RE = re.compile(r"\b(?:she|her|hers|herself)\b", re.I)
_HE_RE = re.compile(r"\b(?:he|him|his|himself)\b", re.I)
_FEMALE_RE = re.compile(r"\b(?:woman|girl|lady|mother|grandmother|grandma|wife|daughter|sister|queen|princess|witch)\b", re.I)
_MALE_RE = re.compile(r"\b(?:man|boy|gentleman|father|grandfather|grandpa|husband|son|brother|king|prince|dwarf|wizard)\b", re.I)


# they·them 꼴 (5차 검토 9번이 넣은 사람 동작의 they·them): 같은 문장에서 그 앞의 가장 가까운 복수 명사가 물건·동물이면 그것을 가리킨다 ('watches
# the paper lanterns rise and smiles at them'·'gazes at fireflies as they dance' — fixA: 올바른 독백 컷이 검사에 걸리고 영상 글에서 동작 문장이 빠졌다).
# fixB: 관사가 붙은 명사만 보아 'fallen leaves'·'fireflies' 를 놓쳤고, 앞에 물건이 하나라도 있으면(any) 걸러 'wipes her tears and smiles at them'·
# 'puts down the cups, glances at the adults and nods at them' 의 그 자리 사람을 놓쳤다 → 가장 가까운 복수 명사 하나만 보고, 몸 낱말(eyes·tears·
# hands …)은 세지 않고, 사람만 하는 동작('watches them laugh'·'as they talk')은 앞에 물건이 있어도 사람이다
_THEY_THEM_RE = re.compile(r"^(?:they|them)\b|(?<!of )\bthem$", re.I)        # 'the two of them' 은 사람이다
_PLURAL_PEOPLE = re.compile(r"(?:guests|neighbou?rs|villagers|customers|students|classmates|visitors|strangers|adults|grown-?ups|elders|players"
                            r"|dancers|musicians|workers|soldiers|travell?ers|passengers|members|twins|crowds|others|parents|friends|brothers|sisters"
                            r"|siblings|sons|daughters|cousins|uncles|aunts|teachers|girls|boys|kids|ladies|babies|teens|teenagers|persons|grandparents"
                            r"|people|folks|guys|elves|dwarves|fairies|ghosts)", re.I)
# 몸·안경 낱말: 'smiles at them' 의 them 이 되는 일이 드물어 가장 가까운 명사로 세지 않는다
_BODY_PLURAL = frozenset(("eyes", "hands", "tears", "cheeks", "lips", "fingers", "arms", "shoulders", "knees", "feet", "legs", "ears", "braids",
                          "curls", "pigtails", "palms", "fists", "toes", "teeth", "brows", "eyebrows", "lashes", "eyelashes", "wrists", "elbows",
                          "glasses", "spectacles", "sleeves"))
# -s 로 끝나지만 복수 명사가 아닌 낱말 (-ss·-us·-is 는 따로 뺀다)
_NOT_PLURAL = frozenset(("as", "has", "was", "his", "hers", "its", "yes", "always", "perhaps", "sometimes", "besides", "towards", "afterwards",
                         "sideways", "upwards", "downwards", "backwards", "forwards", "onwards", "outwards", "inwards", "nowadays", "whereas",
                         "ours", "yours", "theirs", "news", "lens"))
# 복수 명사 바로 앞에 오는 말 (관사·수량·소유격·전치사): 이 뒤의 -s 낱말은 명사다
_NOUN_BEFORE = frozenset(("the", "a", "her", "his", "its", "their", "my", "your", "our", "some", "these", "those", "many", "few", "several", "all",
                          "both", "two", "three", "four", "five", "countless", "dozens", "lots", "hundreds", "of", "at", "with", "into", "onto",
                          "on", "in", "among", "from", "for", "toward", "towards", "through", "over", "under", "around", "about", "between", "past",
                          "beside", "by", "near", "like", "up", "down", "more", "no"))
# -s 낱말 바로 앞이 주어·이음말이면 그 낱말은 동사다 ('Niki gathers'·'she rubs'·'then looks'·'and smiles'·', glances')
_VERB_BEFORE = frozenset(("she", "he", "it", "they", "who", "which", "that", "and", "or", "then", "but", "also", "still", "just", "now", "finally",
                          "again", "soon", "suddenly"))
# 사람만 하는 동작 (they·them 뒤): 앞에 물건이 있어도 다른 사람이다. 'dance'·'swirl'·'look like' 는 물건도 한다
_PEOPLE_ACT_RE = re.compile(r"(?:laugh|talk|chat|argu|whisper|giggl|bicker|jok|gossip|cheer|discuss|quarrel|speak|say|smil|grin|hug|kiss|toast)\w*",
                            re.I)


def _people_act(text: str, m: re.Match) -> bool:
    """they·them 꼴(m)이 사람만 하는 동작인가 ('they talk'·'watches them laugh')"""
    g = m.group(0)
    verb = re.match(r"(?:they|them)\s+(?:\w+ly\s+)?(\w+)", g, re.I)
    if verb and _PEOPLE_ACT_RE.fullmatch(verb.group(1)):
        return True
    after = re.match(r"\s+(\w+)", text[m.end():]) if re.search(r"\bthem$", g, re.I) else None
    return bool(after and _PEOPLE_ACT_RE.fullmatch(after.group(1)))


def _thing_before(text: str, at: int) -> bool:
    """글 text 의 at 자리 앞, 같은 문장 안에서 가장 가까운 복수 명사가 사람이 아닌 것('paper lanterns'·'fallen leaves'·'the photos')인가.
    몸 낱말(eyes·tears …)은 건너뛴다. -s 낱말이 동사인지는 바로 앞 낱말로 가른다: 관사·수량·소유격·전치사 뒤면 명사, 이름(대문자)·대명사·
    사람 말·이음말·쉼표·-ly 뒤면 동사 ('Niki gathers fallen leaves' 의 gathers 는 동사, leaves 는 명사)"""
    start = max(text.rfind(c, 0, at) for c in ".!?") + 1      # ';' 는 넘는다 ('looks up at the lanterns; they look like tiny stars')
    near, person = "", False
    prev = None
    toks = list(re.finditer(r"[A-Za-z][A-Za-z'’-]*|[,;:]", text[start:at]))
    for n, tok in enumerate(toks):
        w = tok.group(0)
        lw = w.lower()
        if (len(lw) >= 4 and lw.endswith("s") and not lw.endswith(("ss", "us", "is", "'s", "’s")) and lw not in _NOT_PLURAL
                and lw not in _BODY_PLURAL):
            p = (prev or "").lower()
            verb = prev is not None and p not in _NOUN_BEFORE and (
                prev in ",;:" or p in _VERB_BEFORE or p.endswith("ly") or prev[0].isupper() or bool(_OTHER_PEOPLE_RE.fullmatch(p)))
            if not verb:
                # 바로 뒤가 사람만 하는 동작이면 사람 무리다 ('watches the teammates cheer and grins at them')
                nxt = toks[n + 1].group(0) if n + 1 < len(toks) else ""
                near, person = lw, bool(_PLURAL_PEOPLE.fullmatch(lw) or _PEOPLE_ACT_RE.fullmatch(nxt))
        prev = w
    return bool(near) and not person


def _other_mention(text: str, who: str, cast: dict) -> str:
    """글(독백 컷의 action)에서 말하는 사람 말고 다른 사람을 가리키는 말: 다른 인물의 이름, 가족·친구 같은 사람 말(말하는 사람 외형에 있는 말과
    말하는 사람의 역할 이름 — 한글 이름 '엄마' 면 mother·mom — 은 뺀다), 말하는 사람과 다른 성별의 대명사(외형 머리 구절로 성별을 안다).
    없으면 빈 글"""
    text = str(text or "")
    me = cast["person"].get(who, who)
    for cid, rx in cast["names"].items():
        if rx is None or cid == who or cast["person"].get(cid, cid) == me:
            continue
        m = rx.search(text)
        if m:
            return m.group(0)
    own = cast["looks"].get(who, "") + " " + _ROLE_WORDS.get((cast.get("ko") or {}).get(who, ""), "")
    for m in _OTHER_PEOPLE_RE.finditer(text):
        if _THEY_THEM_RE.search(m.group(0)) and not _people_act(text, m) and _thing_before(text, m.start()):
            continue
        if not re.search(rf"\b{re.escape(m.group(0))}\b", own, re.I):
            return m.group(0)
    head = _head_phrase(own)
    female, male = bool(_FEMALE_RE.search(head)), bool(_MALE_RE.search(head))
    rx = _HE_RE if female and not male else (_SHE_RE if male and not female else None)
    m = rx.search(text) if rx else None
    return m.group(0) if m else ""


def _stranger(sent: str, who: str, cast: dict) -> str:
    """문장 가운데의 모르는 고유명사 (말하는 사람의 이름이 아닌, 문장 첫 낱말이 아닌 대문자 낱말). 화면 밖 사람일 수 있다 (11-24 3차 검토:
    라이브러리 밖 인물의 'Suji glances at Minho' 가 독백 영상 글에 들어갔다). 관사·소유격 뒤의 대문자 낱말('the Compass')은 물건 이름이라
    사람이 아니다 (4차 검토: 'Putor stares at the Compass' 를 독백 영상 글에서 뺐다). 없으면 빈 글"""
    rx = cast["names"].get(who)
    for m in _NAME_WORD_RE.finditer(sent):
        if not sent[:m.start()].strip(" ,\"'(“”"):
            continue
        w = m.group(1)
        if w in _NOT_NAME or (rx is not None and rx.fullmatch(w)) or _ARTICLED_BEFORE.search(sent, 0, m.start()):
            continue
        return w
    return ""


def _speaker_only(text: str, who: str, cast: dict) -> str:
    """글에서 다른 사람을 가리키는 문장(_other_mention: 다른 인물의 이름·가족 말·다른 성별 대명사)과 모르는 고유명사가 든 문장(_stranger)을
    뺀다 (독백 컷의 영상 글·고쳐 잇기 글: 화면 밖 사람을 그려 넣지 않게). 남는 문장이 없으면 빈 글"""
    keep = [s.strip() for s in re.findall(r"[^.!?]+[.!?]*", str(text or ""))
            if s.strip() and not _other_mention(s, who, cast) and not _stranger(s, who, cast)]
    return " ".join(keep)


def _cap_people(ids, looks: dict, limit: int = GROUP_MAX) -> list[str]:
    """적힌 차례대로 사람은 limit 명까지만 남긴다 (말하는 사람·듣는 사람이 앞에 있다). 동물은 사람 수에 넣지 않고 그대로 둔다"""
    out, n = [], 0
    for c in ids:
        if c in out:
            continue
        person = c in looks and not is_animal_look(looks[c])
        if person and n >= limit:
            continue
        out.append(c)
        n += 1 if person else 0
    return out


def _line_partner(scenes: list, i: int, who: str, chars, step: int, cast: dict | None = None) -> str:
    """같은 장소에서 장면 i 앞(step -1)이나 뒤(+1)로 가장 가까운, 다른 사람이 화면에 보이며 말한 대사의 화자 (G0-6 (b)).
    같은 사람이 이어 말한 줄은 건너뛴다(같은 상대에게 이어 말한다 — 같은 사람의 다른 모습도, 5차 검토: 안경 쓴 제임스의 줄이 제임스를 듣는 사람으로
    골랐다). 다른 사람의 대사가 화면 밖 목소리(그 장면 characters 에 없는 화자,
    화자가 안 보이는 shot 장면)면 듣는 사람으로 삼지 않고 거기서 멈춘다 — 그 사람은 그 자리에 없다 (11-24 2차 검토: 문 밖에서 부르는
    제임스를 다락방의 독백 컷에 듣는 사람으로 넣었다). 그 화자가 장면 i 의 그 자리에 없으면(떠났다, _here) 역시 멈춘다 (3차 검토: 방금 뛰어나간
    니키를 엄마의 혼잣말의 듣는 사람으로 골랐다). 장소가 바뀌면 멈춘다. 없으면 빈 글"""
    place = _s(scenes[i].get("place"))
    k = i + step
    while 0 <= k < len(scenes):
        sc = scenes[k]
        if not isinstance(sc, dict) or _s(sc.get("place")) != place:
            return ""
        sp = _s(sc.get("speaker"))
        if sp and HANGUL_RE.search(_s(sc.get("line_ko"))):
            if sp != who and (cast is None or cast["person"].get(sp, sp) != cast["person"].get(who, who)):
                seen = (_s(sc.get("kind")) == "talk" and not _offscreen_voice(sc)) or sp in {_s(c) for c in sc.get("characters") or []}
                return sp if sp in chars and seen and (cast is None or _here(scenes, i, sp, cast)) else ""
        k += step
    return ""


# 구성안의 listener 칸 (사용자 확정 규칙 11-24 G0): 듣는 사람 id, 그 자리의 모두에게 하는 말, 독백. 모델이 쓰는 비슷한 말도 받는다
# (3차 검토: 'themselves'·'monologue'·'N/A'·'아무도'·'on the phone' 을 모르는 값으로 보고 앞뒤 대사로 추정해 독백이 그룹샷 대화가 되었다)
LISTEN_ALL, LISTEN_NOBODY = "everyone", "nobody"
_LISTEN_ALL_RE = re.compile(r"^(?:to\s+)?(?:(?:everyone|everybody|all|all\s+of\s+them|the\s+others|others|them|both|both\s+of\s+them"
                            r"|the\s+(?:whole\s+)?family|the\s+group)\b|모두|다들|다\s*같이|여러분|가족)", re.I)
_LISTEN_NOBODY_RE = re.compile(
    r"^(?:to\s+)?(?:(?:nobody|no\s*one|no-one|none|n/?a|null|nil|self|oneself|himself|herself|themselves|themself|itself|myself|yourself"
    r"|(?:a\s+)?monologue|self[\s-]*talk|talking\s+to\s+(?:oneself|himself|herself|themselves)|thinking\s+aloud|thinks?\s+aloud"
    r"|thoughts?|inner\s+(?:voice|monologue)|aside|(?:the\s+)?audience|(?:the\s+)?viewers?|(?:the\s+)?camera"
    r"|(?:on\s+)?(?:the\s+)?(?:phone|telephone)|voice[\s-]*over)\b|아무도|아무에게도|없음|혼잣말|혼자|독백|자신|관객|시청자|전화)", re.I)
# 값 가운데 어디에 있어도 독백·통화인 말 (5차 검토: 'internal monologue'·'thinking to herself'·'no listener'·'니키 자신'·'Grandma (on the phone)'·
# '할머니(전화)' 를 모르는 값으로 보고 앞뒤 대사로 추정해 혼자 클로즈업이어야 할 줄이 그룹샷 대화가 되었다). 줄표·점만 있는 값('-'·'—')도 독백
_LISTEN_NOBODY_ANY_RE = re.compile(r"monologue|(?:him|her|them|one|my|your)sel(?:f|ves)\b|\bthinking\b|\bthoughts?\b|\baloud\b|\bphone\b"
                                   r"|telephone|\bdiary\b|\bmirror\b|\bno\s+(?:one|body|listener)\b|전화|자신|혼자|혼잣말|독백|일기|거울", re.I)
# 모르는 listener 값의 장면 글이 독백·통화라고 말하는 꼴 ('Niki smiles to herself'·'James talks on the phone'·'thinking'): 앞뒤 대사로 추정하지 않는다
_MONO_TEXT_RE = re.compile(r"\bto\s+(?:her|him|them)sel(?:f|ves)\b|\b(?:on|over)\s+the\s+(?:phone|telephone)\b|\bthinking\b|\bthinks\s+aloud\b"
                           r"|혼잣말|혼자\s*(?:중얼|말)|전화|생각에\s*잠", re.I)


def _read_listener(sc: dict, chars, cast: dict) -> tuple[str, str]:
    """대사 장면의 listener 칸이 뜻하는 것: ("one", 듣는 사람 id) / ("all", "") / ("mono", "") 독백 / ("self", "") 말하는 사람 자신(혼잣말) /
    ("", "") 빈 칸 / ("?", "") 모르는 값. id 대신 이름으로 적은 듣는 사람('Beth'·'베스')도 받는다 (3차 검토)"""
    who = _s(sc.get("speaker"))
    v = re.sub(r"^[\s\"'(\[{“]+|[\s\"')\]}.,!?”]+$", "", _s(sc.get("listener")))
    if not v:
        return "", ""
    person = cast["person"]
    me = person.get(who, who)
    if v in chars:
        return ("self", "") if person.get(v, v) == me else ("one", v)
    if _LISTEN_NOBODY_RE.match(v) or _LISTEN_NOBODY_ANY_RE.search(v) or not re.search(r"\w", v):
        return "mono", ""
    if _LISTEN_ALL_RE.match(v):
        return "all", ""
    hits = [c for c, rx in cast["names"].items() if c in chars and rx is not None and rx.fullmatch(v)]
    if hits and all(person.get(c, c) == me for c in hits):
        return "self", ""
    hits = [c for c in hits if person.get(c, c) != me]
    if hits and len({person.get(c, c) for c in hits}) == 1:
        return "one", hits[0]
    return "?", ""


def line_target(scenes: list, i: int, chars, exchange: bool = False, cast: dict | None = None) -> tuple[str, str]:
    """대사 장면 i 의 말이 누구에게 가는가 (사용자 확정 규칙 11-24 G0: "대화 할 때는 대화를 주고받는 이들이 나와야 하고, 독백 씬에는
    클로즈업이 나와야 해"). ("one", 듣는 사람 id) = 그 자리의 한 사람에게(대화), ("all", "") = 그 자리의 모두에게(대화),
    ("mono", "") = 독백(같은 장소에 다른 사람이 있어도 혼자 클로즈업). chars 는 설정집 인물 id (사람·동물), cast 는 인물 정보(_cast_view, 없으면
    chars 로 만든다).
    - 듣는 사람(listener 칸, 이름으로 적어도)이 그 자리에 없으면(_here: 문 밖·화면 밖 목소리·방금 떠난 사람) 독백이다 — 그 자리의 누구에게도
      하는 말이 아니다 (G0-3, 3차 검토). 같은 사람의 다른 모습으로 적었으면 화면에 보이는 모습으로 돌려준다 (_look_here)
    - 말하는 사람 자신·독백을 뜻하는 말(nobody·herself·themselves·monologue·N/A·아무도·전화 …)은 독백
    - 비었거나 모르는 값이면(예전 구성안·모르는 id) 추정한다: (a) characters 에 다른 인물이 있으면 대화로 보고 그 첫 사람을 듣는 사람으로(1차 동작),
      (b) 요청이 대화(exchange)이고 같은 장소의 앞이나 뒤 대사의 화자가 화면에 보이며 그 자리에 있는 다른 사람이면 그 사람 (_line_partner,
      앞 먼저), (c) 그 밖에는 독백. "nobody"(독백)라고 분명히 적은 장면은 추정하지 않는다. (a) 의 다른 인물은 그 자리에 없다는 증거가 없는
      사람만(_listed_gone, 5차 검토), 모르는 값인데 장면 글이 독백·통화라고 말하면('to herself'·'on the phone') (b) 대신 독백 (5차 검토)
    scenes 의 장면은 kind·speaker·line_ko·place·characters·listener 칸과 떠남을 보는 action·shot·ko 칸을 본다"""
    sc = scenes[i] if 0 <= i < len(scenes) and isinstance(scenes[i], dict) else {}
    cast = cast or _cast_view(chars if isinstance(chars, dict) else {c: {} for c in chars})
    who = _s(sc.get("speaker"))
    kind, lis = _read_listener(sc, chars, cast) if sc else ("", "")
    if kind == "one":
        return ("one", _look_here(scenes, i, lis, cast)) if _here(scenes, i, lis, cast) else ("mono", "")
    if kind in ("all", "mono", "self"):
        return ("all" if kind == "all" else "mono"), ""
    person = cast["person"]
    me = person.get(who, who)
    others = [c for c in dict.fromkeys(_s(x) for x in sc.get("characters") or []) if c in chars and person.get(c, c) != me
              and (is_animal_look(cast["looks"].get(c, "")) or not _listed_gone(scenes, i, c, cast)[0])]
    if others:
        return "one", others[0]
    if kind == "?" and _MONO_TEXT_RE.search(" ".join(_s(sc.get(f)) for f in ("action", "shot", "ko"))):
        return "mono", ""
    if exchange and sc:
        for step in (-1, 1):
            other = _line_partner(scenes, i, who, chars, step, cast)
            look = _look_here(scenes, i, other, cast) if other else ""
            if look and person.get(look, look) != me:
                return "one", look
    return "mono", ""


# ── 그 자리에 있는 사람 칸 present (11-25 사용자 결정 "방식 바꾸기", docs/short-film-dialogue-design.md 13절) ──
# 2차에서는 코드가 장면 글(영어)의 떠남·들어옴·없음 말로 '지금 그 장소에 누가 있는가'를 짐작했다. 네 번 고치고 네 번 확인했지만 지적이
# 14 → 17 → 23 → 28건으로 줄지 않았고 고칠 때마다 흔한 문장이 새로 깨졌다. 그래서 구성안 모델이 장면마다 그 자리에 있는 사람(present)을
# 적고, 코드는 그 칸만 보고 규칙(G0)을 지킨다. 장면 하나라도 present 가 리스트면 그 구성안 전체를 이 방식으로 다듬고 검사하며, 이때는 장면
# 글로 그 자리 여부를 짐작하는 도우미(_run_state·_presence·_here·_place_people·_listed_gone·_first_seen_here·_listed_voice_off·
# _offscreen_voice·_animal_here·_absent·_moves)를 부르지 않는다. 칸이 없는 구성안(예전 저장본·1차·모델이 빠뜨림)은 그 도우미를 지금
# 그대로 쓴다 — 넓히지 않는다
def has_present(scenes) -> bool:
    """구성안(또는 다듬은 이야기)의 장면 하나라도 그 자리에 있는 사람 칸(present)이 리스트인가 (새 방식)"""
    return any(isinstance(sc, dict) and isinstance(sc.get("present"), list) for sc in scenes or [])


def _read_present(v, chars, cast: dict) -> tuple[str, str]:
    """present 칸의 한 항목을 읽는다 — 듣는 사람 칸(_read_listener)과 같은 방식 (fixC: id 글자가 정확히 같아야만 받아, '베스'·'Beth' 로 적은 구성안의
    대화 줄이 모두 혼자 클로즈업이 됐다). 칸을 읽는 것이지 장면 글을 짐작하는 것이 아니다.
    (id, "") = 읽음: id 그대로, 대소문자만 다른 id(한 사람), 한 사람에게만 맞는 이름(cast names 전체 일치: 한글 이름·라이브러리 이름 — 같은
    사람의 두 모습이면 처음 모습, 모습은 _scene_present 가 이 장면의 speaker·characters·listener 로 고른다).
    ("", 고칠 id) = 못 읽었지만 이름이 한 사람에게만 들어 있다 ('베스(엄마)'). ("", "") = 모르는 값"""
    v = re.sub(r"^[\s\"'(\[{“]+|[\s\"')\]}.,!?”]+$", "", _s(v))
    if not v:
        return "", ""
    if v in chars:
        return v, ""
    person = cast["person"]
    low = [c for c in chars if c.lower() == v.lower()]
    if low and len({person.get(c, c) for c in low}) == 1:
        return low[0], ""
    hits = [c for c, rx in cast["names"].items() if c in chars and rx is not None and rx.fullmatch(v)]
    if hits and len({person.get(c, c) for c in hits}) == 1:
        return hits[0], ""
    near = [c for c, rx in cast["names"].items() if c in chars and rx is not None and rx.search(v)]
    if near and len({person.get(c, c) for c in near}) == 1:
        return "", near[0]
    return "", ""


def _written_present(rs: dict, chars, cast: dict) -> list[str] | None:
    """구성안 장면이 쓴 present 를 읽은 id (적은 차례, 한 번씩, _read_present). 칸이 리스트가 아니면 None (present 칸이 없는 장면).
    못 읽었지만 이름이 한 사람에게만 들어 있는 항목('베스(엄마)')은 고칠 id 로 넣는다 (fixD): 칸 검사(_present_field_problems)는 'write the id
    "beth"' 를 내며 그 사람을 그 자리에 있다고 보는데, P 와 대사 검사가 없다고 보아 같은 판에 '듣는 사람이 present 에 없다(독백)' 가 함께 나왔고,
    따르면 같은 방의 엄마에게 하는 말이 혼자 클로즈업이 됐다 (11-25 좁은 확인). 다듬기도 같은 P 를 써, 고치지 못한 구성안에서도 엄마가 대화 컷에 든다"""
    if not isinstance(rs.get("present"), list):
        return None
    return list(dict.fromkeys(c for c in (r[0] or r[1] for r in (_read_present(x, chars, cast) for x in rs["present"])) if c))


def _scene_present(written: list, listed: list, cast: dict, who: str = "", lis: str = "", prefer: str = "") -> list[str]:
    """새 방식 장면의 그 자리에 있는 사람 P (id 차례: 쓴 present, 그다음 나머지). 장면 글은 보지 않는다:
    쓴 present(설정집 인물만) ∪ 이 장면 characters(listed) ∪ 말하는 사람 who(talk 이고 화면 밖 목소리가 아닐 때) ∪ 듣는 사람 lis(부르는 쪽이
    present 칸이 없는 장면에만 준다 — present 를 쓴 장면은 present·characters 로만 정한다). present 칸이 없는 장면은 written 이 비어 characters·말하는
    사람·듣는 사람만 (짐작하지 않는다). 같은 사람의 두 모습(james / james_glasses)은 하나만: 이 장면의 speaker·characters·listener 차례로 쓴
    모습을 먼저(fixA: 말하는 사람을 james_glasses, characters 에 james 로 적으면 P 와 컷의 모습이 달라 talk_group 이 말하는 사람을 빼고 두 사람
    컷을 '화면에 혼자' 글로 만들었다. 대화 컷 _present_cut 의 듣는 사람 id 도 P 의 모습을 따른다), 없으면 먼저 쓴 모습.
    prefer 는 모습 고르기에만 쓰는 듣는 사람 id 다 — P 에 넣지 않는다 (fixC: present 를 쓴 장면에 lis 를 주지 않자 모습 고르기에서도 듣는 사람이
    빠져, present 에 james·james_glasses 가 함께 있으면 listener 칸의 안경 쓴 제임스 대신 먼저 쓴 james 가 남아 잇기가 끊겼다)"""
    person = cast["person"]
    # 말하는 사람만 speaker 칸의 모습을 먼저, 다른 사람은 화면에 적은 모습(characters)을 듣는 사람 칸보다 먼저 (fixB: listener 에 'james' 만 적어도
    # 화면의 안경 쓴 제임스를 이겨 대화 컷 사이에 안경 없는 제임스가 끼고 잇기가 끊겼다)
    first = ([who] if who else []) + list(listed) + ([lis] if lis else [])
    pick: dict = {}
    for c in first + ([prefer] if prefer else []) + list(written):
        pick.setdefault(person.get(c, c), c)
    out, seen = [], set()
    for c in list(written) + first:
        p = person.get(c, c)
        if p not in seen:
            seen.add(p)
            out.append(pick[p])
    return out


def _present_view(scenes: list, chars, cast: dict, places: list | None = None) -> tuple[list, list, list]:
    """새 방식 구성안의 장면마다 (그 자리 사람 P, 화면 밖 목소리인가, 듣는 사람을 정할 때 보는 장면). 다듬기와 검사가 같은 값을 쓴다.
    쓴 present 는 듣는 사람 칸과 같은 방식으로 읽는다 (_written_present·_read_present: id·대소문자만 다른 id·한 사람에게만 맞는 이름, fixC)
    - 화면 밖 목소리: talk 인데 말하는 사람이 쓴 present 에도 characters 에도 없다 (같은 사람의 다른 모습도 같은 사람). 목소리를 얹은 shot 으로
      바꾸고 그 사람은 P 에 넣지 않는다. present 칸이 없는 장면은 화면 밖 목소리로 보지 않는다 (말하는 사람이 P 에 든다)
    - 보는 장면: 말하는 사람·대사(올바를 때만), 장소(places 를 주면 그 값 — 다듬은 장소), kind(화면 밖 목소리는 shot), 적은 characters, listener"""
    person = cast["person"]
    pres, offs, view = [], [], []
    for n, rs in enumerate(scenes):
        rs = rs if isinstance(rs, dict) else {}
        who, line = _s(rs.get("speaker")), _s(rs.get("line_ko"))
        ok = who in chars and bool(HANGUL_RE.search(line))
        kind = _s(rs.get("kind"))
        listed = [c for c in dict.fromkeys(_s(x) for x in rs.get("characters") or []) if c in chars]
        written = _written_present(rs, chars, cast)
        off = ok and kind == "talk" and written is not None and person.get(who, who) not in {person.get(c, c) for c in written + listed}
        talk = ok and kind == "talk" and not off
        said, named = _read_listener(rs, chars, cast) if ok and kind == "talk" else ("", "")
        # 듣는 사람은 present 칸이 없는 장면에서만 P 에 넣는다. present 를 쓴 장면에서 듣는 사람이 present 에도 characters 에도 없으면 그 자리에 없는
        # 사람이다 (화면 밖 목소리와 같은 기준) — 그 사람에게 하는 말은 독백이다 (_present_target, G0: 문 밖·전화 너머 사람에게 하는 말을 대화 컷으로
        # 그리면 다락방에 없는 엄마가 그려졌다, 11-25 최종 확인). 듣는 사람 칸의 모습은 present 를 쓴 장면에서도 모습 고르기에 쓴다 (prefer, fixC)
        one = named if said == "one" else ""
        pres.append(_scene_present(written or [], listed, cast, who if talk else "", one if written is None else "", one))
        offs.append(off)
        view.append({"speaker": who if ok else "", "line_ko": line if ok else "",
                     "place": places[n] if places is not None else _s(rs.get("place")), "kind": "talk" if talk else "shot",
                     "characters": listed, "listener": rs.get("listener")})
    return pres, offs, view


def _present_partner(scenes: list, i: int, who: str, step: int, here: dict, cast: dict) -> str:
    """듣는 사람 추정 (b) (새 방식): 같은 장소에서 장면 i 앞(step -1)이나 뒤(+1)로 가장 가까운, 다른 사람이 말한 대사의 화자가 그 자리(here:
    {사람 열쇠: P 의 모습})에 있으면 그 모습, 없으면 빈 글 — 그 너머는 보지 않는다. 같은 사람(다른 모습도)이 이어 말한 줄은 건너뛴다.
    장소가 바뀌면 멈춘다"""
    person = cast["person"]
    me = person.get(who, who)
    place = _s(scenes[i].get("place"))
    k = i + step
    while 0 <= k < len(scenes):
        sc = scenes[k]
        if not isinstance(sc, dict) or _s(sc.get("place")) != place:
            return ""
        sp = _s(sc.get("speaker"))
        if sp and HANGUL_RE.search(_s(sc.get("line_ko"))) and person.get(sp, sp) != me:
            return here.get(person.get(sp, sp), "")
        k += step
    return ""


def _present_target(scenes: list, i: int, chars, pres: list, cast: dict) -> tuple[str, str]:
    """새 방식의 line_target: 대사 장면 i 의 말이 누구에게 가는가. ("one", 듣는 사람 id) / ("all", "") / ("mono", ""). 그 자리 여부는
    pres[i](P)만 본다 — 장면 글을 읽지 않는다. scenes 는 _present_view 의 보는 장면.
    - 듣는 사람(id·이름)이 P 에 있으면 그 사람에게: 돌려주는 id 는 P 에 있는 그 사람의 모습. P 에 없으면(present 를 쓴 장면에서 present 에도
      characters 에도 없음) 그 자리에 없는 사람에게 하는 말이라 독백 (11-25 최종 확인: 예전에는 믿고 P 에 넣어 문 밖의 엄마를 다락방에 그렸다)
    - "everyone"·비슷한 말은 모두에게, "nobody"·독백 말은 독백 (추정하지 않는다)
    - 말하는 사람 자신(자기 id·자기 이름)은 독백이다: 규칙 글이 혼잣말('talking to oneself')을 nobody 라고 하고, 예전 경로(line_target)·고침 2 의
      결정과 같다 (fixA: 추정하면 'whispers to herself' 클로즈업이 세 사람 대화 컷이 되고 검사 문구 둘이 서로 반대로 시켰다 — G0 '독백은 클로즈업')
    - 비었거나 모르는 값이면 추정한다 (3차 명세 1.3): (a) characters 의 다른 인물, (b) 같은 장소 구간에서 가장 가까운,
      다른 사람이 말한 대사의 화자 — 그 사람이 P 에 있을 때만 (앞 먼저), (c) 그 밖에는 독백. 예전 짐작의 '대화 요청일 때만'(G0-6 (b))은 두지
      않는다: 그 자리 여부를 P 가 정하므로 그 자리에 없는 사람을 듣는 사람으로 고를 일이 없다"""
    sc = scenes[i]
    person = cast["person"]
    here = {person.get(c, c): c for c in pres[i]}
    who = _s(sc.get("speaker"))
    me = person.get(who, who)
    said, lis = _read_listener(sc, chars, cast)
    if said == "one":
        # 그 자리(P)에 없는 사람에게 하는 말(문 밖·다른 방·전화 너머)은 독백이다 (G0, 예전 경로 test_r3_a 와 같다)
        key = person.get(lis, lis)
        return ("one", here[key]) if key in here else ("mono", "")
    if said in ("all", "mono", "self"):
        return ("all" if said == "all" else "mono"), ""
    others = [c for c in dict.fromkeys(_s(x) for x in sc.get("characters") or []) if c in chars and person.get(c, c) != me]
    if others:
        return "one", here.get(person.get(others[0], others[0]), others[0])
    for step in (-1, 1):
        other = _present_partner(scenes, i, who, step, here, cast)
        if other:
            return "one", other
    return "mono", ""


def _present_seats(scenes: list, i: int, cast: dict) -> list:
    """새 방식 다듬기의 자리 차례 (사람 열쇠, 왼쪽→오른쪽): 장면 i 가 든 같은 장소 구간의 characters(앞 장면은 다듬은 값, 이 장면과 뒤 장면은
    적은 값)에서 사람이 둘 이상인 장면을 차례로 합친다 (자리 지도 _run_seats 와 같은 _merge_seats, 적은 차례를 좌우로 본다). 그 자리에 넷
    이상이 있을 때 대화 컷에 넣을 사람을 고르는 데만 쓴다 ("말하는 사람 + 자리 차례로 둘")"""
    looks, person = cast["looks"], cast["person"]
    place = scenes[i].get("place")
    start = end = i
    while start > 0 and scenes[start - 1].get("place") == place:
        start -= 1
    while end + 1 < len(scenes) and scenes[end + 1].get("place") == place:
        end += 1
    seats: list = []
    for k in range(start, end + 1):
        keys = list(dict.fromkeys(person.get(c, c) for c in scenes[k].get("characters") or [] if c in looks and not is_animal_look(looks[c])))
        if len(keys) >= 2:
            seats = _merge_seats(seats, keys)
    return seats


def _present_cut(scenes: list, i: int, who: str, lis: str, listed: list, here: list, cast: dict) -> list[str]:
    """새 방식의 대화 컷 사람: [말하는 사람, 듣는 사람(한 사람에게)] + 구성안이 characters 에 적은 나머지(동물도 — 듣는 동물은 남는다) + 그 자리
    (here = P)의 다른 사람(사람만, 자리 차례 _present_seats 로). 사람은 3명까지, 같은 사람은 한 모습 (사용자 결정 "셋 모두 나오게", G0).
    scenes 는 다듬는 중인 이야기 장면"""
    looks, person = cast["looks"], cast["person"]
    own = [who] + ([lis] if lis else []) + [c for c in listed if c not in (who, lis)]
    have = {person.get(c, c) for c in own}
    seats = _present_seats(scenes, i, cast)
    rest = [c for c in here if c in looks and not is_animal_look(looks[c]) and person.get(c, c) not in have]
    rest.sort(key=lambda c: (seats.index(person.get(c, c)) if person.get(c, c) in seats else len(seats), here.index(c)))
    return _cap_people(_one_look(own + rest, cast), looks)


def _present_field_problems(sc: dict, i: int, chars: dict, cast: dict) -> list[str]:
    """새 방식 구성안 장면 i(1부터)의 칸끼리 맞는가: present 가 있는가·설정집 인물인가(이름·대소문자 다른 id 는 읽고, 이름이 한 사람에게만 들어
    있으면 고칠 id 를 알려 준다 — _read_present), 같은 사람의 두 모습을 함께 적었는가, 화면의 사람·말하는 사람이 present 에 있는가.
    듣는 사람이 그 자리(P)에 없는 줄은 대사 검사(_present_talk_problems)가 한 문구로 말한다 (fixC: 여기서도 'present 에 넣거나 nobody' 를 내 같은
    판에 독백 문구와 반대로 시켰다). 장면 글은 보지 않는다. 같은 사람의 다른 모습은 같은 사람으로 본다"""
    if not isinstance(sc.get("present"), list):
        return [f'Scene {i}: write "present", the ids of everyone who is in this place during this scene (people and animals), '
                "whether the shot shows them or not."]
    person = cast["person"]
    p, read, fixed = [], [], []
    for c in dict.fromkeys(_s(x) for x in sc["present"]):
        cid, fix = _read_present(c, chars, cast)
        if cid:
            read.append(cid)
        elif fix:
            fixed.append(fix)
            p.append(f'Scene {i}: "present" has "{c}", which is not an id: write the id "{fix}" (the "id" in bible.characters).')
        else:
            p.append(f'Scene {i}: "present" has "{c}", which is not in bible.characters.')
    # 고칠 id 를 알려 준 사람은 아래 칸 검사에서 그 자리에 있는 것으로 본다: '말하는 사람이 present 에 없다(목소리면 shot 으로)' 를 함께 내면 id 를 고치는
    # 대신 대사를 shot 으로 바꾸게 할 수 있다. 장면의 P(_written_present)와 대사 검사도 같은 기준이다 (fixD)
    here = {person.get(c, c) for c in read + fixed}
    who = _s(sc.get("speaker"))
    talk = _s(sc.get("kind")) == "talk" and who in chars
    me = person.get(who, who)
    listed = [c for c in dict.fromkeys(_s(x) for x in sc.get("characters") or []) if c in chars]
    # 같은 사람의 두 모습을 present 에 함께 (규칙 글 'never put both entries in one scene', fixC): 이 장면이 보이는 모습 하나만 남기게 한다
    # (모습은 다듬기 _scene_present 와 같은 차례: speaker, characters, listener)
    said, named = _read_listener(sc, chars, cast) if talk else ("", "")
    shows = ([who] if talk else []) + listed + ([named] if said == "one" else [])
    by_person: dict = {}
    for c in dict.fromkeys(read):
        by_person.setdefault(person.get(c, c), []).append(c)
    for ids in by_person.values():
        if len(ids) > 1:
            keep = next((c for c in shows if c in ids), "")
            names = " and ".join(f'"{c}"' for c in ids)
            p.append(f'Scene {i}: "present" has {names}, the same person in two looks; a scene has only one of them: '
                     + (f'keep only "{keep}", the look this scene shows.' if keep else
                        'keep only the look this person has in this scene.'))
    for c in listed:
        if person.get(c, c) not in here and not (talk and person.get(c, c) == me):
            if said == "one" and person.get(c, c) == person.get(named, named):
                # 화면에 적은 듣는 사람: 빼는 길이면 그 줄은 독백이다 — 그 길을 한 문구에 끝까지 적는다 (fixC: 빼기만 따르면 다음 판에 듣는 사람이 그 자리에
                # 없다는 문구가 또 나와 한 차례가 더 들었다)
                p.append(f'Scene {i}: the listener "{c}" is in "characters" but not in "present": add "{c}" to "present" if "{c}" is in the '
                         f'place; if not, nobody in the place is spoken to: remove "{c}" from "characters", set "listener" to "{LISTEN_NOBODY}" '
                         f'and make it a close-up of "{who}" alone.')
                continue
            p.append(f'Scene {i}: "{c}" is in "characters" but not in "present": add "{c}" to "present", or remove "{c}" from "characters".')
    if talk and me not in here:
        p.append(f'Scene {i}: the speaker must be in "present" ("{who}" is not); a voice from elsewhere (behind a door, from another '
                 'room, on the phone) is a "shot" scene with the line as a voice-over.')
    return p


def _listener_away_problem(sc: dict, i: int, who: str, named: str, here: list, cast: dict) -> str:
    """새 방식: 듣는 사람 named 가 그 자리(here = P)에 없는 대사 장면 i(1부터)의 문구 하나 (fixC, 11-25 최종 확인 지적 06#1·05#2).
    독백 길(nobody, 혼자 클로즈업)을 먼저 적는다 — 문 너머·다른 방·전화 너머 사람에게 하는 대답은 독백이다. present 에 넣는 길은 '장면들이 그
    사람을 이 장소에 보일 때만'이고, 그때의 화면(말하는 사람, 듣는 사람, present 의 다른 사람, 사람 3명까지)과 샷을 바로 적어 한 번에 맞게 한다.
    독백 길의 장면 글·샷 글이 다른 사람을 가리키면 그것도 같은 문구에 적는다 (따로 내면 한 차례가 더 든다)"""
    looks = cast["looks"]
    other = _other_mention(_s(sc.get("action")), who, cast) or _shot_others(_s(sc.get("shot")), who, cast)
    mention = f', with an "action" and a "shot" that do not mention "{other}"' if other else ""
    cut = _cap_people(_one_look([who, named] + [c for c in here if c in looks and not is_animal_look(looks[c])], cast), looks)
    n = len(_people_of(cut, looks))
    shot = f' in a {GROUP_SHOT[n]}' if n in GROUP_SHOT else ""
    return (f'Scene {i}: the listener "{named}" is not in "present", so nobody in the place is spoken to: set "listener" to "{LISTEN_NOBODY}" '
            f'and make it a close-up of "{who}" alone ("characters" ["{who}"]{mention}); a reply to a voice behind a door, from another room '
            f'or on the phone is such a monologue. Only if the scenes show "{named}" in this place, add "{named}" to "present" and make '
            '"characters" [' + ", ".join(f'"{c}"' for c in cut) + f']{shot} instead.')


def _present_talk_problems(view: list, said: list, k: int, chars: dict, cast: dict, pres: list, raw: dict) -> list[str]:
    """새 방식 구성안의 대사 장면 k(0부터) 검사. 사용자 확정 규칙 G0 을 그 자리 사람(pres[k] = P)으로 계산한다 — _group_talk_problems 와 같은
    뜻이지만 장면 글로 그 자리 여부를 정하지 않는다:
    - 대화(한 사람에게·모두에게): characters 는 말하는 사람 + 듣는 사람 + present 의 다른 사람 (사람 3명까지)
    - 독백: [말하는 사람] 혼자, 클로즈업까지, 장면 글·샷 글에 다른 사람 없음 (지시문 정리, 지금 그대로)
    - 듣는 사람으로 적은 사람이 그 자리(P)에 없으면 독백 길을 먼저 적은 문구 하나만 (_listener_away_problem, fixC)
    - 화면 밖 목소리(말하는 사람이 present·characters 에 없음)는 present 칸 검사(_present_field_problems)가 shot 으로 고쳐 쓰게 한다
    view 는 화면 밖 목소리를 shot(_off)으로 바꾼 구성안 장면, said 는 _present_view 의 보는 장면, raw 는 이 장면의 구성안 글 그대로"""
    sc = raw
    i = k + 1
    if view[k].get("_off"):
        return []
    prev = view[k - 1] if k > 0 and isinstance(view[k - 1], dict) else None
    who = _s(sc.get("speaker"))
    looks, person = cast["looks"], cast["person"]
    here = pres[k]
    p = []
    got = [_s(c) for c in sc.get("characters") or []]
    people = _one_look(_people_of(got, looks), cast)
    said_kind, _named = _read_listener(sc, chars, cast)
    lis = _s(sc.get("listener"))
    if said_kind == "?":
        p.append(f'Scene {i}: "listener" "{lis}" is not in bible.characters (write the id of the person spoken to, '
                 f'"{LISTEN_ALL}" or "{LISTEN_NOBODY}").')
    elif said_kind == "self":
        p.append(f'Scene {i}: "listener" is the speaker "{who}" (talking to oneself): write the id of the person in the place who is spoken '
                 f'to, or "{LISTEN_NOBODY}" for a monologue, which is a close-up of "{who}" alone.')
    kind, target = _present_target(said, k, chars, pres, cast)
    if len(people) > GROUP_MAX:
        p.append(f'Scene {i} is a talk scene with {len(people)} people in "characters": show at most three people '
                 "(the speaker, the person spoken to and one more person in the place).")
    if not got or got[0] != who or len(set(got)) != len(got):
        p.append(f'Scene {i} is a talk scene: "characters" must start with the speaker "{who}", then the person spoken to, then the others '
                 "in the place (each id once).")
    rank = shot_rank(_s(sc.get("shot")))
    if kind == "mono" and said_kind == "one":
        # 듣는 사람이 그 자리(P)에 없다: 두 길을 함께 적은 문구 하나만 (예전 경로 _group_talk_problems 와 같은 꼴, fixC). 일반 독백 문구나 'present 에
        # 넣어라' 칸 문구를 같은 판에 함께 내면 서로 반대로 시켰고, present 에 넣는 길을 먼저 적어 문 너머 엄마를 다락방 투샷에 들였다 — nobody 를 먼저
        p.append(_listener_away_problem(sc, i, who, _named, here, cast))
    elif kind == "mono" and said_kind != "self":
        if len(set(got)) > 1:
            p.append(f'Scene {i} is a monologue ("{who}" speaks to nobody in the place): a monologue is a close-up of the speaker alone '
                     f'even when others are there, so "characters" must be exactly ["{who}"].')
        if rank is None or rank > 1:
            p.append(f"Scene {i} is a monologue: the shot must be a close-up or a medium close-up of the speaker alone, "
                     "not a medium or wider shot.")
        other = _other_mention(_s(sc.get("action")), who, cast)
        if other:
            p.append(f'Scene {i} is a monologue: its "action" must describe only the speaker "{who}" (the face, the eyes, small head '
                     f'movements), but it mentions someone else ("{other}"), who would be drawn into the close-up.')
        other = _shot_others(_s(sc.get("shot")), who, cast)
        if other:
            p.append(f'Scene {i} is a monologue: its "shot" must frame only the speaker "{who}" (no other people in the background or over a '
                     f'shoulder), but it mentions "{other}".')
    elif kind != "mono":
        shown = {person.get(c, c) for c in got}
        if kind == "one" and person.get(target, target) not in shown and said_kind not in ("?", "self"):
            p.append(f'Scene {i}: the person spoken to must be in the shot: put "{target}" in "characters" right after the speaker '
                     f'(or write "{LISTEN_NOBODY}" in "listener" if the line is a monologue).')
        elif kind == "one" and got[:1] == [who] and target in got and got.index(target) != 1:
            p.append(f'Scene {i}: the person spoken to, "{target}", must come second in "characters", right after the speaker "{who}".')
        if len(people) < GROUP_MAX and said_kind != "?":
            # 모르는 듣는 사람이면 추정한 사람을 더하라고 하지 않는다 (고침 2 와 같은 뜻, fixA). 더할 사람은 남은 자리(사람 3명 − 이미 정한 사람)까지:
            # 넷 이상이 있을 때 빠진 사람을 모두 더하라고 하면 그대로 따른 답이 '사람 4명' 위반이 된다 (fixA)
            have = shown | {person.get(who, who)} | ({person.get(target, target)} if kind == "one" else set())
            missing = [c for c in here if c in looks and not is_animal_look(looks[c]) and person.get(c, c) not in have]
            room = GROUP_MAX - len(have & {person.get(c, c) for c in looks if not is_animal_look(looks[c])})
            if missing and room > 0:
                names = ", ".join(f'"{c}"' for c in missing)
                add = (f"add {names}" if len(missing) <= room
                       else f'add {"one more person" if room == 1 else f"{room} more people"} from "present" ({names})')
                if kind == "all":
                    p.append(f'Scene {i}: "{who}" speaks to everyone in the place, so everyone there must be in the shot: {add} to "characters" '
                             "(at most three people).")
                else:
                    p.append(f'Scene {i} is a conversation, so everyone in "present" is seen: {add} to "characters" after the speaker and the person '
                             "spoken to (at most three people).")
        if len(people) >= 2 and (rank is None or rank > 3):
            p.append(f"Scene {i} is a conversation with {min(len(people), GROUP_MAX)} people: the shot must be a medium two-shot (two people) "
                     "or a medium group shot (three people) that shows every upper body.")
        elif len(people) < 2 and (rank is None or rank > 2):
            p.append(f"Scene {i} is a talk scene: the shot must be a close-up or a medium close-up of the speaker "
                     "(or a medium two-shot or group shot when the people spoken to are in the shot).")
    group = kind != "mono" and len(set(got)) >= 2
    if sc.get("continues") is True and not (group and prev is not None and _same_talk_group(
            dict(prev, present=pres[k - 1]), dict(sc, present=here), set(got), cast)):
        p.append(f"Scene {i} is a talk scene, so it cannot continue the previous scene unless both are conversation lines "
                 "with the same people in the same place.")
    return p


def _same_talk_group(prev, sc, people: set, cast: dict | None = None) -> bool:
    """앞 장면이 같은 사람들(차례는 상관없음)·같은 장소의 대화 장면인가 (두세 사람 대사 컷이 앞 컷을 같은 화면으로 이어 갈 수 있는 조건:
    첫 화면이 앞 클립의 끝 프레임이라 사람이 늘거나 줄면 안 된다). 독백 컷은 잇지도 이어지지도 않는다 (혼자 클로즈업).
    화면의 사람만 본다: 그 자리 사람 칸(present)이 있어도 화면 밖 사람(넷 이상일 때 컷에 못 든 사람)이 그 사이에 나가거나 들어온 것은 끝 프레임을
    바꾸지 않는다 (fixA: present 까지 같아야 이어 붙여 규칙 글의 조건 '같은 사람·같은 장소'를 지킨 구성안을 까닭 없이 거절했다 — 화면의 사람이
    바뀌는 경우는 G0 컷이 characters 를 바꾸므로 여기서 걸린다). cast 는 예전 호출과 맞추려고 받기만 한다"""
    if not isinstance(prev, dict) or _s(prev.get("kind")) != "talk" or prev.get("monologue") is True or len(people) < 2:
        return False
    return {_s(c) for c in prev.get("characters") or []} == people and _s(prev.get("place")) == _s(sc.get("place"))


def _group_talk_problems(scenes: list, k: int, chars: dict, exchange: bool, cast: dict | None = None, raw: dict | None = None) -> list[str]:
    """두세 사람 대화 장면 규칙(STUDIO_DUO_TALK)의 대사 장면 k(0부터) 검사. 사용자 확정 규칙(11-24 G0)을 따른다 (line_target):
    - 대화(그 자리의 누군가에게·모두에게 하는 말): 말하는 사람과 듣는 사람이 화면에 나온다("the person spoken to must be in the shot").
      characters 는 첫째 = 말하는 사람, 둘째 = 듣는 사람, 사람 3명까지. 모두에게 하는 말이면 그 자리에 있는 사람(같은 장소 구간의 앞 장면들,
      떠났거나 없다고 적힌 사람은 빼고 같은 사람의 다른 모습은 한 사람으로, 구간 첫 장면이면 뒤 장면에서 이미 있던 사람: _place_people)도 빠지면
      안 된다 (말하는 쪽이 동물이어도, 4차 검토).
      혼자(듣는 쪽이 동물뿐)면 미디엄까지(크기 2), 두세 사람이면 미디엄 투샷/그룹샷(크기 3)까지
    - 독백: 같은 장소에 다른 사람이 있어도 [말하는 사람] 혼자, 클로즈업·미디엄 클로즈업(크기 1)까지 (미디엄 샷 2 는 위반). 장면 글(action)은
      말하는 사람만 쓴다: 다른 사람을 가리키면 그 사람을 클로즈업에 그려 넣는다 (11-24 2차 검토)
    - 듣는 사람으로 적은 사람이 그 자리에 없으면(문 밖 목소리·떠난 사람) 독백으로 고쳐 쓰게 한다. 모르는 값·말하는 사람 자신이면 그것만 말하고
      추정한 사람을 넣으라고 하지 않는다 (3차 검토)
    - 말하는 사람이 characters 에 없고 장면 글이 화면 밖 목소리(문 밖·전화)면 talk 가 아니라 목소리를 얹은 shot 으로 고쳐 쓰게 한다 (4차 검토).
      말하는 사람을 적었어도 그 사람이 그 자리에 없었고 글이 그 목소리가 화면 밖에서 온다고 분명히 말하면 같다 (_listed_voice_off, 5차 검토)
    - 적힌 사람이 그 자리에 없다는 증거(_listed_gone: 떠남·없음 글·화면 밖 목소리·뒤에서 들어옴)가 있으면 빼라고 한다 — 그 사람을 더하라고 하지
      않는다 (5차 검토). 독백 컷은 샷 글(shot)도 말하는 사람만 잡는다 (5차 검토: 'close-up of Niki with Beth and James out of focus behind her')
    앞 컷을 잇는 것은 같은 사람들·같은 장소의 대화 컷일 때만. cast 는 인물 정보(_cast_view, 없으면 설정집으로 만든다).
    scenes 는 앞 장면들을 다듬은 차례의 장면들(dialogue_problems 의 view: 화면 밖 목소리 대사는 shot, 검사한 대사 장면은 그 자리에 있는 사람만),
    raw 는 이 장면의 구성안 글 그대로 (없으면 scenes[k])"""
    sc = raw if raw is not None else scenes[k]
    i = k + 1
    prev = scenes[k - 1] if k > 0 and isinstance(scenes[k - 1], dict) else None
    who = _s(sc.get("speaker"))
    cast = cast or _cast_view(chars, scenes=scenes)
    if scenes[k].get("_off") or _offscreen_voice(sc) or _listed_voice_off(scenes, k, cast):
        listed = who in {_s(c) for c in sc.get("characters") or []}
        return [f'Scene {i}: "{who}" is only heard off-screen (outside the place, behind a door or on the phone), so this is not a talk scene: '
                f'write "kind": "shot" with the line as a voice-over (the shot shows the place or the people who hear it'
                + (f', without "{who}" in "characters"' if listed else "") + f'), or show "{who}" speaking in the place.']
    looks = cast["looks"]
    person = cast["person"]
    p = []
    got = [_s(c) for c in sc.get("characters") or []]
    people = _one_look(_people_of(got, looks), cast)
    said, named = _read_listener(sc, chars, cast)
    lis = _s(sc.get("listener"))
    if said == "?":
        p.append(f'Scene {i}: "listener" "{lis}" is not in bible.characters (write the id of the person spoken to, '
                 f'"{LISTEN_ALL}" or "{LISTEN_NOBODY}").')
    elif said == "self":
        p.append(f'Scene {i}: "listener" is the speaker "{who}" (talking to oneself): write the id of the person in the place who is spoken '
                 f'to, or "{LISTEN_NOBODY}" for a monologue, which is a close-up of "{who}" alone.')
    kind, target = line_target(scenes, k, chars, exchange, cast)
    if len(people) > GROUP_MAX:
        p.append(f'Scene {i} is a talk scene with {len(people)} people in "characters": show at most three people '
                 "(the speaker, the person spoken to and one more person in the place).")
    if not got or got[0] != who or len(set(got)) != len(got):
        p.append(f'Scene {i} is a talk scene: "characters" must start with the speaker "{who}", then the person spoken to, then the others '
                 "in the place (each id once).")
    rank = shot_rank(_s(sc.get("shot")))
    if kind == "mono" and said == "one":
        gone = _gone_text(*_listed_gone(scenes, k, named, cast))
        listed = named in got
        p.append(f'Scene {i}: "{named}" is not in the place at this moment ({gone}), so nobody in the place is spoken to: write '
                 f'"{LISTEN_NOBODY}" in "listener"' + (f', remove "{named}" from "characters"' if listed else "")
                 + f' and make it a close-up of "{who}" alone, or show "{named}" in the place first.')
    elif kind == "mono" and said != "self":
        if len(set(got)) > 1:
            p.append(f'Scene {i} is a monologue ("{who}" speaks to nobody in the place): a monologue is a close-up of the speaker alone '
                     f'even when others are there, so "characters" must be exactly ["{who}"].')
        if rank is None or rank > 1:
            p.append(f"Scene {i} is a monologue: the shot must be a close-up or a medium close-up of the speaker alone, "
                     "not a medium or wider shot.")
        other = _other_mention(_s(sc.get("action")), who, cast)
        if other:
            p.append(f'Scene {i} is a monologue: its "action" must describe only the speaker "{who}" (the face, the eyes, small head '
                     f'movements), but it mentions someone else ("{other}"), who would be drawn into the close-up.')
        other = _shot_others(_s(sc.get("shot")), who, cast)
        if other:
            p.append(f'Scene {i} is a monologue: its "shot" must frame only the speaker "{who}" (no other people in the background or over a '
                     f'shoulder), but it mentions "{other}".')
    elif kind != "mono":
        for c in dict.fromkeys(got):
            if c == who or c == target or c not in looks or is_animal_look(looks[c]) or cast["person"].get(c, c) == cast["person"].get(who, who):
                continue
            gone = _listed_gone(scenes, k, c, cast)
            if gone[0]:
                p.append(f'Scene {i}: "{c}" is not in the place at this moment ({_gone_text(*gone)}): remove "{c}" from "characters" '
                         f'(list only the people who are there), or show "{c}" coming back first.')
        if kind == "one" and target not in got and said != "?":
            p.append(f'Scene {i}: the person spoken to must be in the shot: put "{target}" in "characters" right after the speaker '
                     f'(or write "{LISTEN_NOBODY}" in "listener" if the line is a monologue).')
        elif kind == "one" and got[:1] == [who] and target in got and got.index(target) != 1:
            p.append(f'Scene {i}: the person spoken to, "{target}", must come second in "characters", right after the speaker "{who}".')
        if kind == "all" and len(people) < GROUP_MAX:
            have = {person.get(c, c) for c in got + [who]}
            missing = [c for c in _place_people(scenes, k, cast, who, "", True) if person.get(c, c) not in have]
            if missing:
                p.append(f'Scene {i}: "{who}" speaks to everyone in the place, so everyone there must be in the shot: add '
                         + ", ".join(f'"{c}"' for c in missing[:GROUP_MAX]) + ' to "characters" (at most three people).')
        if len(people) >= 2 and (rank is None or rank > 3):
            p.append(f"Scene {i} is a conversation with {min(len(people), GROUP_MAX)} people: the shot must be a medium two-shot (two people) "
                     "or a medium group shot (three people) that shows every upper body.")
        elif len(people) < 2 and (rank is None or rank > 2):
            p.append(f"Scene {i} is a talk scene: the shot must be a close-up or a medium close-up of the speaker "
                     "(or a medium two-shot or group shot when the people spoken to are in the shot).")
    group = kind != "mono" and len(set(got)) >= 2
    if sc.get("continues") is True and not (group and _same_talk_group(prev, sc, set(got))):
        p.append(f"Scene {i} is a talk scene, so it cannot continue the previous scene unless both are conversation lines "
                 "with the same people in the same place.")
    return p


def dialogue_problems(data, seconds: float, person_ok: bool = True, exchange: bool = False, duo: bool | None = None,
                      known: list | None = None, now_style: str | None = None) -> list[str]:
    """대사 영상 구성안 검사: 긴 영상 검사 + 대사·목소리 규칙. 고칠 말을 영어로 돌려준다.
    exchange 는 요청이 인물끼리의 대화일 때: 두 인물 이상이 번갈아 말하고, 말하는 인물마다 얼굴이 보이는 말하기 장면이 있어야 한다
    (두세 사람 규칙에서는 듣는 사람 칸이 빈 대사를 앞뒤 대사로 추정하는 데도 쓴다, line_target).
    duo 는 두세 사람 대화 장면 규칙으로 보는가 (duo_rules). known 은 요청에 이름이 나온 라이브러리 인물: 같은 사람의 다른 모습
    (제임스 ↔ 안경 쓴 제임스)을 한 사람으로 센다 — 두세 사람 규칙에서는 한 장면에 둘 다 두면 위반 (11-24 2차 검토).
    두세 사람 규칙에서 장면 하나라도 present 칸을 쓴 구성안(새 방식, 11-25 "방식 바꾸기")은 그 자리 여부를 present 로만 정한다: 칸이 없는 장면·
    모르는 id·present 에 없는 화면의 사람·말하는 사람·듣는 사람을 고쳐 쓰게 하고, G0 검사도 present 로 계산한다 (_present_field_problems·
    _present_talk_problems). 어떤 검사도 장면 글을 읽어 그 자리 여부를 정하지 않는다. 칸이 없는 구성안은 지금 그대로.
    사용자의 화풍 구절 칸끼리의 검사(bible.style_said ↔ bible.style, 3차 명세 2.4b)는 긴 영상 검사(story_problems)에 있어 여기서도 한 번 돈다
    (now_style: 고치기 차례의 '지금 그림체', fixD)"""
    p = story_problems(data, seconds, person_ok, cast_lib.library_style(known), now_style)
    if not isinstance(data, dict):
        return p
    duo_on = duo_rules(duo)
    bible = data.get("bible") if isinstance(data.get("bible"), dict) else {}
    chars = {_s(c.get("id")): c for c in bible.get("characters") or [] if isinstance(c, dict) and _s(c.get("id"))}
    scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
    cast = _cast_view(chars, known, scenes, {_s(x.get("id")): _s(x.get("look")) for x in bible.get("places") or [] if isinstance(x, dict)})
    # 그 자리 사람을 정할 때 보는 장면들 (clean_dialogue_story 의 said 와 같은 차례, 5차 검토): 화면 밖 목소리 대사 장면은 목소리를 얹은 shot
    # (말하는 사람은 그 자리에 없다), 검사한 대사 장면은 적힌 사람 가운데 그 자리에 있는 사람만 — 떠난 사람을 적은 줄 하나가 뒤 컷으로 번지지 않게
    view = [dict(sc) if isinstance(sc, dict) else sc for sc in scenes]
    # 새 방식(11-25 "방식 바꾸기"): 장면 하나라도 present 를 썼으면 그 자리 여부는 present 로만 정하고 칸끼리 맞는지만 본다 (장면 글을 읽지 않는다)
    new = duo_on and has_present(scenes)
    pres, said = [], []
    if new:
        pres, offs, said = _present_view(scenes, chars, cast)
        for n, sc in enumerate(view):
            if offs[n]:
                view[n] = dict(sc, kind="shot", characters=[c for c in sc.get("characters") or [] if _s(c) != _s(sc.get("speaker"))], _off=True)
    elif duo_on:
        for n, sc in enumerate(view):
            if (isinstance(sc, dict) and _s(sc.get("kind")) == "talk" and _s(sc.get("speaker")) in chars and HANGUL_RE.search(_s(sc.get("line_ko")))
                    and (_offscreen_voice(sc) or _listed_voice_off(view, n, cast))):
                view[n] = dict(sc, kind="shot", characters=[c for c in sc.get("characters") or [] if _s(c) != _s(sc.get("speaker"))], _off=True)
    speakers, talks, lines = [], 0, 0
    order: list[tuple[int, str]] = []          # 대사가 있는 장면 (번호, 말하는 사람) — 장면 차례대로
    seen_in_scenes: list[str] = []             # 장면에 나오는 인물 (처음 나오는 차례대로)
    talk_by: set[str] = set()                  # 얼굴이 보이는 말하기 장면을 가진 인물
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            continue
        for c in sc.get("characters") or []:
            if _s(c) in chars and _s(c) not in seen_in_scenes:
                seen_in_scenes.append(_s(c))
        kind, who, line = _s(sc.get("kind")), _s(sc.get("speaker")), _s(sc.get("line_ko"))
        if kind not in ("talk", "shot"):
            p.append(f'Scene {i}: "kind" must be "talk" or "shot".')
            continue
        if line or kind == "talk":
            if who not in chars:
                p.append(f'Scene {i}: "speaker" "{who}" is not in bible.characters.')
            if not HANGUL_RE.search(line):
                p.append(f'Scene {i}: "line_ko" must be a spoken Korean line.')
            elif len(line) > DIALOGUE_LINE_MAX:
                p.append(f'Scene {i}: the line has {len(line)} characters; keep it under 60 (split it over two scenes).')
            if not _s(sc.get("emotion")) or HANGUL_RE.search(_s(sc.get("emotion"))):
                p.append(f'Scene {i}: "emotion" must say in English how the line is spoken.')
            if who in chars and who not in speakers:
                speakers.append(who)
            if who in chars:
                order.append((i, who))
            lines += 1
        prev = scenes[i - 2] if i > 1 and isinstance(scenes[i - 2], dict) else None
        if duo_on:
            # 같은 사람의 두 모습을 한 장면에 (인물 라이브러리: '한 장면에 둘을 넣지 말 것', 11-24 2차 검토: 자리 이어 받기가 넣기도 했다)
            ids = [_s(c) for c in sc.get("characters") or [] if _s(c) in chars] + ([who] if kind == "talk" and who in chars else [])
            ids = list(dict.fromkeys(ids))
            twin = next(((a, b) for n, a in enumerate(ids) for b in ids[n + 1:]
                         if cast["person"].get(a, a) == cast["person"].get(b, b)), None)
            if twin:
                p.append(f'Scene {i}: "{twin[0]}" and "{twin[1]}" are the same person in two looks; a scene shows only one of them '
                         "(use the other look in other scenes).")
            if new:
                p += _present_field_problems(sc, i, chars, cast)
        if kind == "talk" and duo_on and new:
            talks += 1
            if not view[i - 1].get("_off"):
                talk_by.add(who)             # 화면 밖 목소리는 얼굴이 보이는 말하기 장면이 아니다
            p += _present_talk_problems(view, said, i - 1, chars, cast, pres, raw=sc)
        elif kind == "talk" and duo_on:
            talks += 1
            off = bool(view[i - 1].get("_off"))
            if not off:
                talk_by.add(who)             # 화면 밖 목소리는 얼굴이 보이는 말하기 장면이 아니다
            p += _group_talk_problems(view, i - 1, chars, exchange, cast, raw=sc)
            if not off:
                view[i - 1] = dict(view[i - 1], characters=[
                    c for c in sc.get("characters") or [] if _s(c) == who or _s(c) not in cast["looks"] or is_animal_look(cast["looks"][_s(c)])
                    or not _listed_gone(view, i - 1, _s(c), cast)[0]])
        elif kind == "talk":
            talks += 1
            talk_by.add(who)
            if [_s(c) for c in sc.get("characters") or []] != [who]:
                p.append(f'Scene {i} is a talk scene: "characters" must be exactly ["{who}"] (the speaker alone in the frame).')
            rank = shot_rank(_s(sc.get("shot")))
            if rank is None or rank > 2:
                p.append(f'Scene {i} is a talk scene: the shot must be a close-up or a medium close-up of the speaker.')
            if sc.get("continues") is True:
                p.append(f"Scene {i} is a talk scene, so it cannot continue the previous scene.")
        if prev is not None and _s(prev.get("kind")) == "talk" and sc.get("continues") is True and not (duo_on and kind == "talk"):
            p.append(f"Scene {i} comes right after a talk scene, so it cannot continue it.")
    if not talks:
        p.append('The film needs talk scenes: write at least one "talk" scene with a spoken line.')
    rules: list[str] = []
    need = dialogue_min_lines(seconds)
    if lines < need:
        rules.append(f"The film runs {int(seconds)} seconds, so write at least {need} spoken lines (about one every 10 to 13 seconds); "
                     f"you wrote {lines}. Turn silent shots into talk scenes or give a shot a voice-over line instead of leaving long stretches without speech.")
    if exchange:
        silent = [c for c in seen_in_scenes if c not in speakers]
        if len(seen_in_scenes) >= 2 and len(speakers) < 2:
            rules.append(f'The request is a conversation between the characters, but only "{speakers[0] if speakers else "nobody"}" speaks. '
                         f'Let "{(silent or seen_in_scenes)[0]}" speak too (a question, an answer, a reaction in words), '
                         "and take turns: a line, an answer, a line.")
        run, prev_who = 0, None
        for i, who in order:
            run = run + 1 if who == prev_who else 1
            prev_who = who
            if run > DIALOGUE_RUN_MAX:
                rules.append(f'"{who}" speaks {run} lines in a row (up to scene {i}). Let the other character answer in between, '
                             "so the conversation goes back and forth.")
                break
        # 새 방식: 말한 줄이 모두 그 장면의 그 자리 사람(P) 밖에서 나온 화자(목소리 shot·화면 밖 talk)는 목소리로만 나오는 사람이다 (문 밖·다른 방·
        # 전화 — 규칙 글대로 shot 으로만 말하고 talk 장면이 필요 없다). 얼굴이 보이는 말하기 장면을 요구하면 그 사람을 방 안 투샷에 들이게 된다
        # (fixA). fixC: 예전에는 '어느 장면의 P 에도 없는 화자'만 보아, 부엌에서 말없이 한 번 보인 엄마가 다락방에 목소리로만 말하면 면제가 풀려
        # 문구를 따르면 엄마를 다락방 present 에 넣게 됐다 — 줄마다 그 장면의 P 만 본다
        inside = set()
        if new:
            for n, sc in enumerate(scenes):
                w = _s(sc.get("speaker")) if isinstance(sc, dict) else ""
                if (w in chars and HANGUL_RE.search(_s(sc.get("line_ko")))
                        and cast["person"].get(w, w) in {cast["person"].get(x, x) for x in pres[n]}):
                    inside.add(w)
        for cid in speakers:
            if cid not in talk_by and (not new or cid in inside) and len(speakers) >= 2:
                rules.append(f'"{cid}" speaks only as a voice-over, though "{cid}" is in "present" when speaking: make at least one of those '
                             f'lines a "talk" scene, so the audience sees the face speak.' if new else
                             f'"{cid}" speaks only as a voice-over. Give "{cid}" at least one "talk" scene, so the audience sees the face speak.')
                break
    for cid in speakers:
        c = chars[cid]
        v = _s(c.get("voice"))
        if not _s(c.get("name_ko")) or not HANGUL_RE.search(_s(c.get("name_ko"))):
            p.append(f'Character "{cid}" speaks, so it needs a Korean "name_ko".')
        if not v or HANGUL_RE.search(v):
            p.append(f'Character "{cid}" speaks, so it needs an English "voice" description.')
        elif len(v.split()) > 55:
            p.append(f'The voice of "{cid}" has {len(v.split())} words; keep it under 40.')
        elif REAL_VOICE_RE.search(v):
            p.append(f'The voice of "{cid}" refers to a real or famous voice; describe the voice itself instead.')
    return (rules + p)[:16]          # 줄 수·대화 규칙을 앞에 둔다: 다른 위반이 많아도 잘리지 않게


def clean_dialogue_story(data, seconds: float, duo: bool | None = None, exchange: bool = False,
                         known: list | None = None) -> dict | None:
    """대사 영상 구성안을 다듬는다: 긴 영상 다듬기 + 장면의 말하는 사람·대사, 인물의 이름·목소리. 대사가 하나도 없으면 None.
    duo 는 두세 사람 대화 장면 규칙으로 다듬는가 (duo_rules): 아니면 대사 장면은 말하는 사람 혼자 (예전 그대로).
    exchange 는 요청이 대화인가 (wants_exchange): 듣는 사람 칸이 빈 대사를 앞뒤 대사로 추정한다 (line_target).
    known 은 요청에 이름이 나온 라이브러리 인물: 같은 사람의 다른 모습(제임스 ↔ 안경 쓴 제임스)을 한 사람으로 센다 (_cast_view).
    두세 사람 규칙의 대사 장면은 누구에게 하는 말인지(line_target)로 다듬는다 (사용자 확정 규칙 11-24 G0, 검사는 고쳐 쓰게 하고 정리는 뜻을 살린다):
    - 대화(한 사람에게·모두에게): characters = 말하는 사람, 듣는 사람, 그 자리의 나머지 (사람 3명까지). 바로 앞 장면이 같은 장소면 그 자리에
      있던 사람(_presence: 그 장소를 떠난 사람은 빼고, 같은 사람의 다른 모습은 이 장면이 적은 모습 하나만)을 뒤에 더하고(사용자 결정
      "셋 모두 나오게"), 샷 글을 사람 수에 맞춘다(group_shot: 미디엄 투샷·그룹샷). 앞 대화 컷과 사람들·장소가 같을 때만 잇는다.
      모두에게 하는 말인데 그 자리에 아무도 없으면 혼자 하는 말(독백)
    - 독백: 같은 장소에 다른 사람이 있어도 [말하는 사람] 혼자, 샷은 클로즈업까지(close_shot), 잇지 않는다. 사람을 더하지 않는다
    - 말하는 사람이 characters 에 없고 글이 화면 밖 목소리(문 밖·전화)인 대사 장면은 목소리를 얹은 shot 으로 바꾼다: 그 사람을 그 장소에 그리지
      않고(offscreen 표시: scene_people 이 넣지 않는다 — 화면에 적히지 않은 사람의 목소리를 얹은 shot 도 같다), 대답하는 컷에서도 그 사람은 그
      자리에 없다 (4차 검토). 말하는 사람을 적었어도 그 사람이 그 자리에 없었고 글이 목소리가 화면 밖에서 온다고 분명히 말하면 같다(characters 에서
      뺀다, _listed_voice_off, 5차 검토)
    - 구성안이 적은 사람도 그 자리에 없다는 증거(_listed_gone: 앞에서 떠남·없음 글·화면 밖 목소리로만 나옴·뒤에서 들어옴)가 있으면 넣지 않는다.
      그 사람이 듣는 사람이면 그 줄은 독백이다 (5차 검토: 방금 뛰어나간 니키를 적은 부르는 줄이 그룹샷 대화가 되고 뒤 컷에도 이어졌다)
    - 독백의 샷 글은 말하는 사람만 잡는다: 다른 사람을 가리키는 말('with Beth and James out of focus behind her')이 있으면 크기 말까지만 (_mono_shot)
    모든 장면에 listener(듣는 사람 id, 그 밖에는 ""), 두세 사람 규칙의 대사 장면에 monologue·to_all 표시, 두세 사람 규칙으로 다듬은
    이야기에 duo_rules 표시(자리 차례 group_order 를 이 이야기에만 쓴다, 꺼짐은 1차 그대로)를 둔다.
    새 방식(11-25 사용자 결정 "방식 바꾸기"): 두세 사람 규칙에서 장면 하나라도 present 칸이 리스트면 위의 떠남·없음·화면 밖 목소리 짐작 대신
    그 칸으로 다듬는다 — 그 자리 사람 P = 쓴 present(이름·대소문자만 다른 id 도 읽는다, _read_present) ∪ characters ∪ 말하는 사람(화면 밖 목소리가
    아닐 때). 듣는 사람은 present 칸이 없는 장면에서만 P 에 더한다: present 를 쓴 장면에서 듣는 사람이 present 에도 characters 에도 없으면 그 자리에
    없는 사람이라 그 줄은 독백이다(_present_view·_present_target, 11-25 최종 확인). 말하는 사람이 present·characters 에 없는 talk 는 화면 밖
    목소리(shot), 듣는 사람 추정은 P 안에서(_present_target), 대화 컷은 P 에서 고른다(_present_cut). 다듬은 장면마다 P 를 present 로 남긴다
    (뒤 단계가 쓴다). 칸이 없는 구성안은 지금 그대로"""
    story = clean_story(data, seconds)
    if story is None:
        return None
    raw = [sc for sc in data.get("scenes") or [] if isinstance(sc, dict)]
    rawc = {_s(c.get("id")): c for c in (data.get("bible") or {}).get("characters") or [] if isinstance(c, dict)}
    chars = {c["id"]: c for c in story["bible"]["characters"]}
    looks = {c["id"]: c["look"] for c in story["bible"]["characters"]}
    for c in story["bible"]["characters"]:
        rc = rawc.get(c["id"]) or {}
        voice = _s(rc.get("voice"))
        c["name_ko"] = _s(rc.get("name_ko"))[:20] or c["id"]
        c["voice"] = "" if HANGUL_RE.search(voice) else voice[:400]
        c["voice_ko"] = _s(rc.get("voice_ko"))[:80]
        sample = _s(rc.get("sample_ko"))        # 기준 소리가 말할 글: 한글이고 짧을 때만 쓴다 (아니면 voice_sample 이 기본 글을 쓴다)
        c["sample_ko"] = sample if HANGUL_RE.search(sample) and len(sample) <= SAMPLE_MAX else ""
    lines = 0
    duo_on = duo_rules(duo)
    cast = _cast_view(chars, known, raw, {p["id"]: p["look"] for p in story["bible"]["places"]})
    # 새 방식(11-25 사용자 결정 "방식 바꾸기"): 장면 하나라도 present 를 썼으면 구성안 전체의 그 자리 사람·화면 밖 목소리·듣는 사람 추정을
    # present 로만 정한다 (_present_view·_present_target·_present_cut). 장면 글로 그 자리 여부를 짐작하는 도우미는 부르지 않는다
    new = duo_on and has_present(raw)
    pres: list = []
    if new:
        pres, offs, said = _present_view(raw, chars, cast, [sc["place"] for sc in story["scenes"]])
    else:
        # 누구에게 하는 말인지 정할 때 보는 장면들 (앞뒤 대사를 보므로 먼저 모은다): 말하는 사람·대사는 다듬은 값, kind·listener·characters 는 쓴
        # 그대로 (화면 밖 목소리 talk 는 shot). 다듬은 장면은 그 자리에서 다듬은 사람들로 바꿔 둔다: 뒤 장면이 보는 그 자리 사람이 다듬은 화면과 같게
        said, offs = [], []
        for i, (sc, rs) in enumerate(zip(story["scenes"], raw)):
            who, line = _s(rs.get("speaker")), _s(rs.get("line_ko"))
            ok = who in chars and bool(HANGUL_RE.search(line))
            said.append({"speaker": who if ok else "", "line_ko": line if ok else "", "place": sc["place"],
                         "kind": _s(rs.get("kind")), "characters": rs.get("characters") or [], "listener": rs.get("listener"),
                         "action": sc["action"], "shot": sc["shot"], "ko": sc["ko"], "sound": sc.get("sound", "")})
            off = duo_on and ok and _s(rs.get("kind")) == "talk" and (_offscreen_voice(rs) or _listed_voice_off(said, i, cast))
            if off:
                said[i].update(kind="shot", characters=[c for c in said[i]["characters"] if _s(c) != who])
            offs.append(off)
    for i, (sc, rs) in enumerate(zip(story["scenes"], raw)):
        who, line = _s(rs.get("speaker")), _s(rs.get("line_ko"))[:DIALOGUE_LINE_MAX]
        ok = who in chars and bool(HANGUL_RE.search(line))
        off = offs[i]
        talk = ok and _s(rs.get("kind")) == "talk" and not off
        sc.update(kind="talk" if talk else "shot", speaker=who if ok else "", line_ko=line if ok else "",
                  emotion=_s(rs.get("emotion"))[:120] if ok and not HANGUL_RE.search(_s(rs.get("emotion"))) else "",
                  listener="")
        if new:
            sc["present"] = list(pres[i])     # 그 자리에 있는 사람 (뒤 단계: scene_people·talk_group·_same_talk_group 이 본다)
        if off:
            sc["characters"] = [c for c in sc["characters"] if c != who]
        if duo_on and ok and not talk and who not in sc["characters"]:
            # 목소리만 얹은 장면(화면 밖 목소리로 쓴 talk 도, 화면에 적히지 않은 사람의 목소리도): 말하는 사람을 이 장면에 그리지 않는다
            # (scene_people — 닫힌 다락방 문 클로즈업에 문 밖의 엄마를 그렸다, 4차 검토)
            sc["offscreen"] = True
        prev = story["scenes"][i - 1] if i > 0 else None
        if talk and duo_on:
            target, lis = _present_target(said, i, chars, pres, cast) if new else line_target(said, i, chars, exchange, cast)
            own = [who]
            animal = is_animal_look(looks.get(who, ""))
            if target != "mono" and new:
                # 새 방식: 대화 컷 사람을 그 자리 사람 P(present)로 정한다. 말하는 사람 + 듣는 사람 + 구성안이 적은 나머지 + P 의 다른 사람(자리 차례),
                # 사람 3명까지. 모두에게 하는 말인데 P 에 다른 사람이 없으면 독백. 독백에는 사람을 더하지 않는다 (G0)
                listed = [c for c in dict.fromkeys(sc["characters"]) if c in chars and c != who]
                own = _present_cut(story["scenes"], i, who, lis if target == "one" else "", listed, pres[i], cast)
                if target == "all" and not _people_of([c for c in own if c != who], looks):
                    target, own = "mono", [who]
            elif target != "mono":
                # 대화: 말하는 사람, 듣는 사람, 구성안이 적은 나머지, 그리고 그 순간 그 장소에 있는 사람(_place_people: 같은 장소 구간에서 화면에
                # 나왔고 떠나지 않았거나 없다고 적히지 않은 사람, 구간 첫 장면이면 뒤 장면에 이미 있던 사람)을 뒤에 더한다 (사람 3명까지, 동물 제외 —
                # 말하는 순간 같은 자리의 사람이 사라지지 않게, 사용자 결정 11-24 "셋 모두 나오게"). 독백에는 절대 더하지 않는다 (G0). 앞 대화 컷을
                # 잇는 대화 컷은 이것으로 같은 사람들이 된다. 같은 사람의 다른 모습은 한 사람이다: 이 장면이 적은 모습만 남긴다 (11-24 2차 검토).
                # 말하는 쪽이 동물이어도 더한다 (4차 검토: G0 '모두에게 하는 말은 그 자리 모두' — 동물 대사 글은 사람들이 입을 다물고 듣게 쓴다).
                # 구간 뒤 장면에서 이미 있던 사람을 찾는 것은 모두에게 하는 말과, 구성안이 말하는 사람만 적은 대화뿐이다: 대화하는 사람들을 적은
                # 컷(정원의 [베스, 니키])에 나중 넓은 장면의 사람을 끌어오지 않는다
                listed = [c for c in dict.fromkeys(_s(x) for x in rs.get("characters") or []) if c in chars and c != who
                          and (is_animal_look(looks.get(c, "")) or not _listed_gone(said, i, c, cast)[0])]
                own += ([lis] if lis else []) + [c for c in listed if c != lis]
                own += [c for c in _place_people(said, i, cast, who, lis, target == "all", ahead=target == "all" or not listed)
                        if c not in own]
                own = _cap_people(_one_look(own, cast), looks)
                if target == "all" and not _people_of([c for c in own if c != who], looks):
                    target, own = "mono", [who]          # 모두에게 하는 말인데 그 자리에 아무도 없다: 혼자 하는 말
            sc.update(listener=lis if target == "one" else "", monologue=target == "mono", to_all=target == "all", characters=own)
            if target == "mono":
                sc["shot"] = _mono_shot(sc["shot"], who, cast)
                sc["continues"] = False
            else:
                # 동물이 말하는 컷은 동물도 화면의 한 몫이다 (여우 + 니키 = 투샷)
                sc["shot"] = group_shot(sc["shot"], len(_people_of(own, looks)) + (1 if animal else 0), cast)
                sc["continues"] = bool(sc["continues"] and _same_talk_group(prev, sc, set(own), cast))
        elif talk:
            sc["characters"] = [who]
            sc["continues"] = False
            if i + 1 < len(story["scenes"]):
                story["scenes"][i + 1]["continues"] = False
        elif duo_on and prev is not None and prev.get("kind") == "talk":
            sc["continues"] = False          # 대사 컷 바로 다음의 말 없는 장면은 잇지 않는다 (대사 컷 끝은 자르는 지점)
        said[i].update(kind=sc["kind"], characters=list(sc["characters"]))
        lines += 1 if ok else 0
    if not lines:
        return None
    for c in story["bible"]["characters"]:
        if not c["voice"] and any(s["speaker"] == c["id"] for s in story["scenes"]):
            c["voice"] = "A natural, clear Korean speaking voice that suits the character, calm and steady."
    story["dialogue"] = True
    if duo_on:
        story["duo_rules"] = True
    return story


def dialogue_count_rules(seconds: float, exchange: bool, duo: bool = False) -> str:
    """구성안 지시문 끝에 붙이는 줄 수·대화 규칙 (숫자로 적는다: 모델은 '약 8~12초마다' 만으로는 최소한만 쓴다).
    duo 는 두세 사람 규칙(present 칸)으로 쓰는가: 그러면 '말하는 사람마다 talk 장면' 은 그 자리에 있는 사람만이다 — 다른 방·전화 너머 목소리는
    talk 장면이 필요 없다 (규칙 글 DIALOGUE_EXTRA_DUO 와 같은 말, fixC: 두 글이 반대로 말해 문 너머 엄마를 다락방 투샷에 들이게 했다)"""
    need = dialogue_min_lines(seconds)
    out = (f"\n- This film is about {int(seconds)} seconds long, so write at least {need} spoken lines in total (about one every 10 to 13 seconds). "
           "A film with one or two lines and long silent shots is wrong.")
    if exchange:
        every = ('every speaker who is in the place gets at least one "talk" scene so the audience sees that face speak (a voice from another '
                 'room or on the phone needs none). ') if duo else 'every speaker gets at least one "talk" scene so the audience sees that face speak. '
        out += (f'\n- The request is a conversation: the characters speak to each other. At least two characters must speak and take turns '
                f"(a line, an answer, a line...), never more than {DIALOGUE_RUN_MAX} lines in a row from the same character, "
                + every + "A listener answers in words, not only with a nod.")
    return out


def dialogue_base(text: str, notes: str, seconds: float, known: list | None = None, duo: bool | None = None,
                  now_style: str | None = None, look_plan: str = "") -> tuple[list, int]:
    """대사 영상 구성안의 처음 messages 와 장면 수 상한 (write_dialogue_story 와 storyboard 의 문맥 길이 어림이 같은 글을 본다)"""
    count, n_min, n_max = story_counts(seconds)
    n_max = max(n_max, math.ceil(float(seconds) / 5))      # 말하는 장면은 짧다: 장면이 더 많아도 된다
    said = f"{text or ''} {notes or ''}"
    exchange = wants_exchange(said)
    duo = duo_rules(duo)
    system = STORY_SYSTEM.format(seconds=int(seconds), keys=", ".join(MUSIC_KEYS), count=count, n_min=n_min, n_max=n_max,
                                 tolerance=story_tolerance(seconds)) + dialogue_extra(duo) + dialogue_count_rules(seconds, exchange, duo) \
        + cast_lib.prompt_block(known, text=said)
    user = (f"Request (Korean): {text}\n" + (f"Must-keep details from the user: {notes}\n" if notes else "") + now_look_note(now_style)
            + look_plan_note(look_plan))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], n_max


async def write_dialogue_story(call, text: str, notes: str, seconds: float, rounds: int = 3,
                               known: list | None = None, duo: bool | None = None,
                               now_style: str | None = None, look_plan: str = "",
                               num_ctx: int | None = None) -> tuple[dict | None, list[str], int]:
    """대사 영상 구성안을 같은 LLM 차례 안에서 쓴다. (구성안, 남은 위반, 쓴 횟수). known 은 요청에 이름이 나온 라이브러리 인물.
    duo 는 감독이 정한 '두세 사람 대사 컷을 만들 수 있는가' (None 이면 설정): 지시문·칸·검사·다듬기가 모두 같은 규칙을 쓴다.
    now_style 은 고치기 차례의 앞 영상 화풍 (None 이면 한 메시지 요청, fixD, write_story 와 같다).
    look_plan·num_ctx 는 영화 빛 계획과 storyboard 가 맞춘 문맥 길이 (11-26, write_story 와 같다)"""
    said = f"{text or ''} {notes or ''}"
    exchange = wants_exchange(said)
    duo = duo_rules(duo)
    person_ok = True if PERSON_REQ_RE.search(said) or STORY_PERSON_KO_RE.search(said) or _known_person(known) else bool(re.search(r"대화|대사|말하", said))
    base, n_max = dialogue_base(text, notes, seconds, known, duo, now_style, look_plan)
    messages = base
    options = story_options(base, n_max, num_ctx=num_ctx)    # 한 차례 안의 호출은 같은 num_ctx: 3분 대사 구성안은 12288 을 넘는다 (fixD)
    best, problems, rnd = None, ["no answer"], 0
    for rnd in range(1, rounds + 1):
        reply = await call({"messages": messages, "format": dialogue_schema(duo), "think": False, "options": dict(options)})
        content = (reply.get("message") or {}).get("content", "")
        try:
            data = parse_json_reply(content)
        except PromptxError as e:
            data, problems = None, [str(e)]
        else:
            problems = [x for x in dialogue_problems(data, seconds, person_ok, exchange, duo=duo, known=known, now_style=now_style)
                        if not (x.startswith("Write ") and "scenes (you wrote" in x)]     # 장면 수는 대사 수를 따른다
        story = clean_dialogue_story(data, seconds, duo=duo, exchange=exchange, known=known) if data is not None else None
        if story is not None and (best is None or len(problems) < len(best[1])):
            best = (story, problems)
        if not problems:
            break
        messages = repair_turn(base, content, problems, data)          # 마지막 답과 그 문제만 (fixC: 3분 대사 구성안의 문맥 길이)
    if best is None:
        return None, problems, rnd
    return best[0], best[1], rnd


STRETCH_WIDE_MAX = 16     # 말 없는 넓은 장면(풍경·함께 걷기)을 늘릴 수 있는 상한(초)
STRETCH_CLOSE_MAX = 10    # 말 없는 가까운 장면(듣는 얼굴)의 상한(초): 얼굴만 오래 잡으면 움직임이 흐트러진다


def stretch_scenes(story: dict, target: float, overlap: float = 0.5, talk_loss: float = 0.5) -> bool:
    """대사 길이에 맞춰 말하는 장면이 줄어 영화가 요청 길이보다 짧아지면, 말하지 않는 장면을 1초씩 돌려 가며 늘려 요청 길이에 가깝게 맞춘다.
    11-19 메인 시험: 1분 요청이 대사 4줄 × 5초로 줄어 약 40초가 됐다. 넓은 장면부터 늘리고, 말하는 장면은 늘려도 소리가 없어 쓸모없다
    (끝이 어두워져 잘라 쓴다). 장면 사이 디졸브(overlap)로 줄어드는 만큼과, 말하는 장면을 대사 끝 + 0.9초에서 자를 때 잃는 만큼(talk_loss,
    장면마다 평균 0.5초)도 더한다. 바뀌었으면 True"""
    scenes = story.get("scenes") or []
    if not scenes or not target:
        return False
    talks = sum(1 for s in scenes if s.get("kind") == "talk")
    need = float(target) + overlap * max(0, len(scenes) - 1) + talk_loss * talks - sum(int(s["seconds"]) for s in scenes)
    free = [i for i, s in enumerate(scenes) if s.get("kind") != "talk"]
    free.sort(key=lambda i: (cast_scene(story, i) is not None, i))      # 넓은 장면 먼저
    changed = False
    while need >= 1 and free:
        moved = False
        for i in free:
            cap = STRETCH_CLOSE_MAX if cast_scene(story, i) is not None else STRETCH_WIDE_MAX
            if need >= 1 and int(scenes[i]["seconds"]) < cap:
                scenes[i]["seconds"] = int(scenes[i]["seconds"]) + 1
                need -= 1
                moved = changed = True
        if not moved:
            break
    if changed:
        story["seconds"] = sum(int(s["seconds"]) for s in scenes)
    return changed


def speakers_of(story: dict) -> list[str]:
    """대사가 있는 인물 id (처음 말하는 차례대로)"""
    out = []
    for sc in story.get("scenes") or []:
        if sc.get("line_ko") and sc.get("speaker") and sc["speaker"] not in out:
            out.append(sc["speaker"])
    return out


def speaker_side(story: dict, cid: str) -> str:
    """말하는 사람이 바라보는 화면 쪽. 대화하는 두 사람이 서로 마주 보게(180도 규칙) 첫 번째는 오른쪽, 두 번째는 왼쪽을 본다"""
    order = speakers_of(story)
    return "right" if (order.index(cid) if cid in order else 0) % 2 == 0 else "left"


def _opposite(side: str) -> str:
    return "left" if side == "right" else "right"


def talk_group(story: dict, i: int) -> list[str] | None:
    """대사 장면 i 가 두세 사람 대사 컷(대화)이면 [말하는 사람, 듣는 사람, 나머지(장면에 적힌 차례)], 아니면 None (혼자·네 사람 이상).
    독백은 다듬을 때 [말하는 사람] 혼자가 되므로 None 이다 (G0). 설정과 상관없이 장면 모양만 본다: 구성안이 두세 사람으로 짠 컷은
    첫 화면도 그 사람들이라, 설정을 나중에 꺼도 영상 글·검사는 그 사람 수 기준이어야 하고, 이 칸이 없는 예전 구성안(1차·그 전)도
    그대로 읽힌다. 동물은 세지 않는다: 듣는 쪽이 동물뿐이면 사람 한 명의 대사 컷이다.
    그 자리 사람 칸(present, 새 방식)이 있는 장면은 그 칸에 있는 사람만 세고, 독백 표시가 있으면 None (11-25: 그 자리 여부는 present 로 정한다)"""
    if not story.get("dialogue"):
        return None
    sc = story["scenes"][i]
    sp = sc.get("speaker")
    if sc.get("kind") != "talk" or not sp:
        return None
    people = scene_people(story, i)
    if isinstance(sc.get("present"), list):
        if sc.get("monologue") is True:
            return None
        people = [c for c in people if c in sc["present"]]
    if not 2 <= len(people) <= GROUP_MAX or sp not in people:
        return None
    lis = sc.get("listener")
    rest = [c for c in people if c != sp]
    return [sp] + sorted(rest, key=lambda c: 0 if c == lis else 1)


def talk_pair(story: dict, i: int) -> list[str] | None:
    """대사 장면 i 가 두 사람 대사 컷이면 [말하는 사람, 듣는 사람], 아니면 None (세 사람 컷은 talk_group). 두 사람은 서로 마주 본다(pair_facing)"""
    group = talk_group(story, i)
    return group if group and len(group) == 2 else None


def duo_talk(story: dict, i: int) -> bool:
    """두세 사람 대사 컷을 InfiniteTalk 레시피(talk_duo)로 만드는가: 대사 영상의 talk 장면이고 사람이 둘이나 셋이고(talk_group, 대화)
    설정(STUDIO_DUO_TALK)이 켜짐. 독백은 혼자 LTX 대사. 이름은 1차(두 사람)대로 두고 뜻을 세 사람까지 넓혔다 (11-24 "셋 모두 나오게")"""
    return bool(settings.studio_duo_talk and talk_group(story, i))


def _place_run(story: dict, i: int) -> tuple[int, int]:
    """장면 i 가 든 같은 장소의 연속 구간 (첫 장면, 끝 장면)"""
    scenes = story.get("scenes") or []
    place = scenes[i].get("place")
    start = end = i
    while start > 0 and scenes[start - 1].get("place") == place:
        start -= 1
    while end + 1 < len(scenes) and scenes[end + 1].get("place") == place:
        end += 1
    return start, end


def _seated(story: dict) -> bool:
    """자리 차례(group_order)와 그 자리에서 정하는 두 사람·한 사람 컷의 좌우·시선을 쓰는 이야기인가: 두세 사람 대화 장면 규칙으로 다듬은
    대사 영상 (clean_dialogue_story 의 duo_rules 표시). 꺼짐(STUDIO_DUO_TALK=false)·대사 없는 영상·1차 이야기는 1차 그대로 (G0-9, 11-24 2차 검토)"""
    return bool(story.get("dialogue") and story.get("duo_rules"))


def group_order(story: dict, i: int, ids: list[str]) -> list[str]:
    """여러 사람의 화면 자리 차례(왼쪽→오른쪽)가 대화 내내 같게: 장면 i 가 든 같은 장소 연속 구간의 자리 지도(_run_seats)에서의 차례.
    그 사람들이 모두 앉은 자리 지도가 없으면 이야기에 처음 나온 차례.
    가족 식탁의 넓은 장면 다음 대사 컷들은 말하는 사람이 바뀌어도 자리가 그대로다 (180도 규칙, 11-24 사용자 결정 "셋 모두 나오게").
    같은 사람의 다른 모습(인물 라이브러리 variant_of: 넓은 장면의 제임스 ↔ 대사 컷의 안경 쓴 제임스)은 같은 사람으로 맞추고, 돌려주는 id 는
    ids 에 적힌 것 그대로다 (11-24 2차 검토: 모습이 다르면 넓은 장면을 못 찾아 좌우가 뒤집혔다).
    감독은 이 차례에서 말하는 사람의 칸을 정해 InfiniteTalk 마스크 1 로 쓴다. 그 자리 사람 칸(present)이 있는 이야기는 자리 지도가 그 칸으로
    정한 장면 사람(scene_people)으로 만들어진다 (11-25)"""
    want = list(dict.fromkeys(ids))
    scenes = story.get("scenes") or []
    if len(want) < 2 or not 0 <= i < len(scenes):
        return want
    person = _persons(story)
    by = {person.get(c, c): c for c in want}
    if len(by) == len(want):
        seats = _run_seats(story, i)
        if set(by) <= set(seats):
            return [by[p] for p in seats if p in by]
    first = [person.get(c, c) for c in anchor_ids(story)]
    return sorted(want, key=lambda c: (first.index(person.get(c, c)) if person.get(c, c) in first else len(first), want.index(c)))


def _natural_order(story: dict, k: int, ids: list[str]) -> list[str]:
    """자리 지도가 없을 때의 좌우: 두 사람은 1차 규칙(두 사람 대사 컷을 함께하면 서로 마주 보는 pair_facing, 아니면 말한 차례 — 같은 쪽이면
    적힌 차례), 셋 이상은 적힌 차례 (그 장면의 scene_people 차례)"""
    if len(ids) == 2:
        if talk_pair(story, k) or _talks_together(story, ids):
            face_of = pair_facing(story, ids[0], ids[1])
            return sorted(ids, key=lambda c: 0 if face_of[c] == "right" else 1)
        order = sorted(ids, key=lambda c: 0 if speaker_side(story, c) == "right" else 1)
        return list(ids) if speaker_side(story, order[0]) == speaker_side(story, order[1]) else order
    return list(ids)


def _merge_seats(seats: list, order: list) -> list:
    """자리 지도(seats, 왼쪽→오른쪽)에 한 장면의 차례(order)를 합친다: 이미 앉은 사람의 좌우는 그대로, 새 사람은 그 장면에서 자기보다 왼쪽에
    있던 사람들의 가장 오른쪽 바로 다음에 (없으면 오른쪽에 있던 사람들의 가장 왼쪽 바로 앞에) 앉힌다"""
    out = list(seats)
    for n, p in enumerate(order):
        if p in out:
            continue
        left = [out.index(q) for q in order[:n] if q in out]
        right = [out.index(q) for q in order[n + 1:] if q in out]
        out.insert(max(left) + 1 if left else (min(right) if right else len(out)), p)
    return out


def _run_seats(story: dict, i: int) -> list[str]:
    """같은 장소 연속 구간의 자리 지도 (왼쪽→오른쪽, 사람 열쇠 _persons). 구간의 여럿 장면(사람 둘 이상 — 대사 컷도 말 없는 넓은 장면도)을
    장면 차례대로 보며, 처음 함께 나온 장면의 좌우(_natural_order)를 정하고 뒤 장면은 이미 앉은 사람의 좌우를 지키며 새 사람만 끼워 넣는다
    (11-24 3차 검토: 세 사람 자리를 세 사람 대화 컷에서만 정해, 넓은 장면 [제임스, 베스, 니키] 다음 베스→제임스 투샷의 좌우가 뒤집히고 넓은 장면
    오른쪽 니키의 독백 클로즈업이 식탁 반대쪽을 봤다. 반대로 정원 투샷 [니키, 베스] 다음 같은 정원의 넓은 장면이 베스를 왼쪽에 두었다).
    장면의 사람은 scene_people 이라 그 자리 사람 칸(present)이 있으면 그 칸을 따른다 (그 자리에 없는 사람의 목소리는 자리에 앉히지 않는다, 11-25)"""
    scenes = story.get("scenes") or []
    if not 0 <= i < len(scenes):
        return []
    person = _persons(story)
    start, end = _place_run(story, i)
    seats: list = []
    for k in range(start, end + 1):
        ppl = scene_people(story, k)
        keys = [person.get(c, c) for c in ppl]
        if len(keys) < 2 or len(set(keys)) != len(keys):
            continue
        seats = _merge_seats(seats, [person.get(c, c) for c in _natural_order(story, k, ppl)])
    return seats


def _seat_order(story: dict, i: int, ids: list[str]) -> list[str]:
    """두세 사람 ids 의 좌우 차례: 장면 i 가 든 구간의 자리 지도(_run_seats). 지도에 없는 사람은 그 사람들의 기본 좌우(_natural_order)로 끼워 넣는다.
    같은 사람이 두 모습으로 함께 적혔으면(검사가 막는 경우) 기본 좌우"""
    person = _persons(story)
    keys = [person.get(c, c) for c in ids]
    if len(set(keys)) != len(keys):
        return _natural_order(story, i, ids)
    seats = _merge_seats(_run_seats(story, i), [person.get(c, c) for c in _natural_order(story, i, ids)])
    return sorted(ids, key=lambda c: seats.index(person.get(c, c)))


def pair_facing(story: dict, a: str, b: str) -> dict[str, str]:
    """대화하는 두 사람이 바라보는 화면 쪽. 같은 두 사람은 장면이 바뀌어도 좌우가 같아야 한다(180도 규칙, 오른쪽을 보는 사람이 왼쪽 자리).
    말한 차례(speaker_side)가 서로 다르면 그대로. 같은 쪽이 되면(한 번도 말하지 않는 듣는 사람은 첫 화자와 같은 'right' 를 받고,
    화자가 셋 이상이면 1번과 3번이 같다) 두 사람이 처음 함께 나오는 대사 컷의 말하는 사람이 제 쪽을 보고 상대는 반대쪽을 본다
    (11-24 검토 C3: 등장 차례로 되돌아가 오른쪽 자리의 화자에게 '오른쪽으로 말한다'가 갈 수 있었다)"""
    sa, sb = speaker_side(story, a), speaker_side(story, b)
    if sa != sb:
        return {a: sa, b: sb}
    for k in range(len(story.get("scenes") or [])):
        pr = talk_pair(story, k)
        if pr and set(pr) == {a, b}:
            side = speaker_side(story, pr[0])
            return {pr[0]: side, pr[1]: _opposite(side)}
    return {a: sa, b: _opposite(sa)}


def _pair_sides(story: dict, i: int, pair: list[str]) -> dict[str, str]:
    """두 사람 대사 컷 i 의 두 사람이 바라보는 쪽: 구도 그림의 자리(layout_slots)에서 왼쪽 사람은 오른쪽, 오른쪽 사람은 왼쪽. 고쳐 잇기·
    바로 그리기 글이 구도 그림·말하는 사람 칸과 같은 좌우를 쓴다 (같은 장소 구간의 자리 지도를 따르는 좌우도 같게, _seat_order)"""
    slots = layout_slots(story, i, pair)
    return {slots[0]["id"]: "right", slots[-1]["id"]: "left"}


def _talks_together(story: dict, ids: list[str]) -> bool:
    """이 두 사람이 영화 안에서 두 사람 대사 컷으로 함께 나오는가 (그러면 다른 두 사람 장면도 같은 좌우로 둔다)"""
    want = set(ids)
    return len(want) == 2 and any(set(talk_pair(story, k) or ()) == want for k in range(len(story.get("scenes") or [])))


def _seat_facing(story: dict, k: int, cid: str) -> str | None:
    """여럿이 나오는 장면 k(대사 컷이든 말 없는 넓은 장면이든)의 자리에서 cid(같은 사람의 다른 모습도)가 바라보는 화면 쪽: 왼쪽 자리는 오른쪽,
    오른쪽 자리는 왼쪽, 세 사람의 가운데는 대사 컷에서 말하는 사람이면 듣는 사람 쪽, 듣는 사람이면 말하는 사람 쪽. 그 장면에 없거나, 모두에게
    말하는 가운데 사람이거나, 말 없는 장면의 가운데 사람이면 None (11-24 3차 검토: 넓은 장면의 자리도 본다)"""
    people = scene_people(story, k)
    if len(people) < 2:
        return None
    person = _persons(story)
    me = next((c for c in people if person.get(c, c) == person.get(cid, cid)), None)
    if me is None:
        return None
    seats = [s["id"] for s in layout_slots(story, k, people)]
    pos = seats.index(me)
    if pos == 0:
        return "right"
    if pos == len(seats) - 1:
        return "left"
    if not talk_group(story, k):
        return None
    sc = story["scenes"][k]
    toward = sc.get("listener") if me == sc.get("speaker") else sc.get("speaker")
    if toward in seats and toward != me:
        return "right" if seats.index(toward) > pos else "left"
    return None


def facing_of(story: dict, i: int | None, cid: str) -> str:
    """한 사람 컷(클로즈업·혼자 대사·기준 그림을 고친 첫 화면)에서 이 사람이 바라보는 화면 쪽. 두 사람 대사 컷에 나오는 사람은 그 쌍의
    바라보는 쪽(pair_facing)을 따른다 — 장면 i 에서 가장 가까운 앞 투샷, 없으면 가장 가까운 뒤 투샷 (180도 규칙. 11-24 검토: 한 번도
    말하지 않는 듣는 사람은 투샷에서 왼쪽을 보는데 바로 다음 클로즈업에서 speaker_side 대로 오른쪽을 봐 시선이 튀었다).
    투샷에 나오지 않으면 speaker_side. i 가 None 이면(인물 기준 그림) 처음 나오는 투샷.
    두세 사람 규칙으로 다듬은 이야기(_seated)는 여럿이 나오는 장면의 자리를 본다 (11-24 2차 검토: 식탁 오른쪽 자리의 니키가 그룹샷에서는
    왼쪽을 보는데 그 사이 독백 클로즈업에서는 말한 차례대로 오른쪽을 봐 시선이 튀었다. 3차 검토: 대화 컷이 없는 장소에서는 넓은 장면의
    자리도 본다): 같은 장소의 연속 구간에서 이 사람이 나오는 가장 가까운 여럿 장면(앞 먼저)의 자리(_seat_facing), 없으면 다른 곳의 가장
    가까운 여럿 장면, 그다음이 speaker_side. 여럿 장면의 자리는 scene_people·talk_group 으로 정하므로 그 자리 사람 칸(present)이 있으면 그 칸을
    따른다 (11-25)"""
    n = len(story.get("scenes") or [])
    order = range(n) if i is None else list(range(min(i, n - 1), -1, -1)) + list(range(i + 1, n))
    if _seated(story) and n:
        near = list(order)
        if i is not None:
            start, end = _place_run(story, min(i, n - 1))
            near = [k for k in near if start <= k <= end] + [k for k in near if not start <= k <= end]
        for k in near:
            side = _seat_facing(story, k, cid)
            if side:
                return side
        return speaker_side(story, cid)
    for k in order:
        pr = talk_pair(story, k)
        if pr and cid in pr:
            return pair_facing(story, pr[0], pr[1])[cid]
    return speaker_side(story, cid)


def cast_scene(story: dict, i: int) -> str | None:
    """장면 i 의 첫 화면을 인물 기준 그림에서 고쳐 만들 인물 id. 말하는 장면은 그 사람, 대사 없는 장면도 한 사람만 가깝게(미디엄 이하)
    나오면 그 사람 (듣는 얼굴 등). 여럿이 나오거나 멀리 잡은 장면은 None: 긴 영상과 같이 장면 이미지를 새로 그린다"""
    sc = story["scenes"][i]
    if not story.get("dialogue"):
        return None
    if sc.get("kind") == "talk":
        return sc.get("speaker") or None
    chars = sc.get("characters") or []
    rank = shot_rank(sc.get("shot") or "")
    return chars[0] if len(chars) == 1 and rank is not None and rank <= 2 else None


def cast_ids(story: dict) -> list[str]:
    """기준 그림이 필요한 인물 id (처음 나오는 차례대로)"""
    out = []
    for i in range(len(story.get("scenes") or [])):
        cid = cast_scene(story, i)
        if cid and cid not in out:
            out.append(cid)
    return out


def _look(story: dict, cid: str) -> str:
    look = next((c["look"] for c in story["bible"]["characters"] if c["id"] == cid), "").strip()
    return look if not look or look.endswith((".", "!", "?")) else look + "."


def _style_line(story: dict) -> str:
    st = story_style(story)
    return f" Style: {st}" if st else " Photorealistic cinematic look."


def style_lock(story: dict) -> str:
    """영상 글 끝에 붙이는 화풍 고정 문장. 말하는 장면(LTX-2.3)과 말 없는 장면(LTX-2.5)의 영상 글이 같은 말로 첫 화면의 화풍을 지키게 한다
    (11-20 사용자: 화풍은 대사가 나올 때나 안 나올 때나 항상 같아야 한다). 사실적이지 않은 화풍일 때만 (사진 화풍은 영상 글을 그대로 둔다)"""
    if not story_style(story):
        return ""
    return "Keep exactly the look of the first frame in every frame: the same art style, the same colours and textures, never restyled."


# 기준 목소리: 대사는 이 소리의 나이·말투·감정을 따라 하며(맥락 복제) 녹음되므로 기준 소리가 모든 대사의 연기를 정한다.
# 11-19 메인 시험 2: 인사말로 설계한 기준 소리의 '목소리 특징만' 복제했더니 80세 푸토르가 젊은 남자 목소리로, 감정 없이 책 읽는 투로 나왔다.
# 설계 지시문 끝에 붙인 "Natural Korean speech, clear pronunciation. Emotion: calm and friendly, relaxed." 와 인사말이 늙은 설명을 눌렀다.
# 손으로 승인받은 방식(숲길 단편)은 '목소리 설명 + Emotion: …' 한 줄과 인물다운 말이었다.
VOICE_REF_TEXT = "안녕하세요. 오늘은 바람이 참 좋네요."     # 인물의 샘플 글(sample_ko)이 없을 때만 쓰는 기본 글
VOICE_REF_TOKENS = 256         # 약 20초 상한: 푸토르 설명의 '아주 느린 말'이 세 문장 기준 문장을 38.96초·5분으로 늘린 적이 있다 (11-19)
VOICE_REF_EMOTION = ("warm and expressive, speaking naturally to someone close, with natural rises and falls in pitch "
                     "and real feeling in the words")
SAMPLE_MAX = 70                # 인물의 샘플 글 상한 (글자 수): 길면 기준 소리가 늘어진다


def voice_instruct(story: dict, cid: str) -> str:
    """목소리 설계 지시문 (인물마다 한 번, 이 목소리를 모든 대사에 복제해 쓴다)"""
    c = next((c for c in story["bible"]["characters"] if c["id"] == cid), {})
    voice = c.get("voice") or "A natural, clear Korean speaking voice."
    return f"{voice.rstrip('.')}. Emotion: {VOICE_REF_EMOTION}."


def voice_sample(story: dict, cid: str) -> str:
    """기준 소리가 말할 글: 이 인물이 실제로 할 법한 말(sample_ko). 없거나 한글이 아니면 기본 글. 맥락 복제는 이 글을 기준 소리의 글로 쓴다"""
    c = next((c for c in story["bible"]["characters"] if c["id"] == cid), {})
    s = _s(c.get("sample_ko"))
    return s if s and HANGUL_RE.search(s) and len(s) <= SAMPLE_MAX else VOICE_REF_TEXT


def voice_ref_max_seconds(text: str) -> float:
    """기준 소리가 이보다 길면 늘어진 것으로 보고 시드를 바꿔 한 번 더 설계한다. 맥락 복제는 기준 소리와 글이 맞아야 해서
    소리를 잘라 쓰지 못한다 (한국어 느린 말투 약 초당 2.5~4글자 + 여유)"""
    return 4.0 + len(text or "") * 0.5


def cast_prompt(story: dict, cid: str) -> str:
    """인물 기준 그림 (Z-Image): 말하는 장면·혼자 나오는 가까운 장면은 모두 이 그림을 고쳐 만들어 얼굴을 맞춘다"""
    side = facing_of(story, None, cid)
    return (f"Character reference portrait of one person: {_look(story, cid)} Head and shoulders, centered in a wide frame, "
            f"the face in three-quarter view turned toward the {side} side of the frame, a calm natural expression, mouth closed, "
            "hands not visible, a plain softly lit neutral background, soft even light, sharp detail on the face and clothing. "
            + ONE_PERSON + " No text." + _style_line(story))


# 그림에 한 사람만 나오게 하는 문장 (11-20 메인 재시험: 기준 그림을 고친 첫 화면에 푸토르가 둘로 나왔다. 편집 글의
# "looking at someone off-screen" 이 그 '누군가'를 그려 넣게 했다 — 같은 씨앗으로 재현했고 이 문장으로 바꾸자 한 명이 됐다)
ONE_PERSON = ("The picture shows exactly one person, alone, with empty background beside the person: "
              "no second person, no reflection, no copy or mirror image of the person.")


def cast_edit_prompt(story: dict, i: int) -> str:
    """기준 그림을 장면 i 의 첫 화면으로 고치는 편집 지시 (Qwen 편집). 얼굴·옷은 그대로, 구도·표정·배경만 바꾼다.
    바라보는 쪽은 '화면 밖의 누군가'가 아니라 화면 가장자리 너머로 쓴다 (11-20: '누군가'를 그려 넣어 사람이 둘이 됐다).
    그림은 늘 한 사람이라 투샷 글은 미디엄 클로즈업으로 (solo_shot: 두 사람 규칙으로 짠 대사 장면을 장면 잇기 없이 만들 때)"""
    sc = story["scenes"][i]
    b = story["bible"]
    cid = sc.get("speaker") if sc.get("kind") == "talk" else (sc.get("characters") or [""])[0]
    side = facing_of(story, i, cid)
    place = next((p["look"] for p in b["places"] if p["id"] == sc["place"]), "")
    mood = sc.get("emotion") or "attentive and natural"
    talking = "lips slightly parted as if about to speak" if sc.get("kind") == "talk" else "listening quietly, lips closed"
    return (f"Keep the same person from the image exactly: the same face, age, hair, clothing and colours. {solo_shot(sc['shot'])}. "
            f"The head is turned in three-quarter view toward the {side} side of the frame, the eyes looking past the edge of "
            f"the frame at nothing visible, a {mood} expression, {talking}. The background is now this place, softly out of focus: "
            f"{place} Time: {b['time']} Light: {b['light']} The hands are not visible. {ONE_PERSON}"
            + _style_line(story))[:1990]


# ── 장면 잇기 (11-20 설계, docs/scene-chaining-design.md): 모든 장면의 첫 화면은 앞 장면 마지막 프레임을 고쳐 만든다 ──
# 얼굴·머리·옷은 늘 고정 기준 그림에서 (11-19 손으로 만든 시범이 일관됐던 까닭: 키프레임마다 같은 기준 그림에서 새로 편집했다)
STYLE_KEEP = {
    "semi": ("Keep the exact rendering style of the reference images: a soft painted semi-realistic illustration with visible painterly brush "
             "texture, matte soft shading and natural proportions, the same level of painted detail on faces at every distance. "
             "It is not a glossy 3D render, not a cartoon, not cel-shaded."),
    "anime": ("Keep the exact rendering style of the reference images: a 2D anime-style illustration with clean cel-shaded line art and flat "
              "colours with soft shading, the same line weight on every face. It is not a 3D render, not a photograph."),
    "photo": ("Keep the exact look of the reference images: a real photograph or live-action film still with real skin, hair and fabric and "
              "natural light. It is not a painting, not a cartoon, not a 3D render."),
    # 3D 애니메이션 (11-24 2차): 픽사풍 3D 요청이 애니 열쇠로 묶여 애니 문장의 'not a 3D render' 와 부딪혔다
    "toon3d": ("Keep the exact rendering style of the reference images: a stylized 3D animated feature film look with smooth soft shading, "
               "rounded stylized proportions and clean materials, the same on every face. It is not a photograph, not a flat 2D drawing."),
    # 그 밖의 화풍 (수채·클레이·픽셀 …, 11-24 2차): 앞에 붙는 화풍 원문(style_keep)이 화풍을 정한다. 무엇이 아닌지 모르므로 '아닌 것'
    # 문장은 넣지 않는다 (예전에는 반실사 문장의 'not a cartoon, not cel-shaded' 가 수채·코믹 요청과 부딪혔다)
    "custom": ("Keep exactly the art style of the reference images in every part of the picture: the same medium, lines, textures, colours "
               "and shading on every face and object."),
}


STILL_PROMPT_MAX = 1990  # 그림 편집·새로 그리기 지시문 상한 (이미지 확장 입력 2000자)
STYLE_RAW_MAX = 200      # 정지 그림 지시문에 싣는 영화 화풍 원문(bible.style) 상한(자)
STYLE_RAW_MIN = 80       # 지시문이 길면 화풍 원문을 이 길이까지 줄인다 (화풍 종류·선·채색은 앞쪽에 온다)


def style_key(story: dict) -> str:
    """영화 화풍 열쇠: semi(반실사)·anime(애니)·toon3d(3D 애니메이션)·photo(실사)·custom(그 밖에 분명히 말한 화풍, 원문을 그대로 쓴다).
    화풍 글에서 정한다 (cast_lib.style_key)"""
    return cast_lib.style_key(_s(((story or {}).get("bible") or {}).get("style")))


def style_raw(story: dict, limit: int = STYLE_RAW_MAX) -> str:
    """영화 화풍 원문(bible.style)을 limit 자 이내로. 자를 때는 뒤쪽 절반 안의 쉼표 경계, 없으면 낱말 경계에서 (끝 문장부호는 뺀다).
    비었으면 빈 글"""
    return trim_style(((story or {}).get("bible") or {}).get("style"), limit)


def trim_style(text, limit: int = STYLE_RAW_MAX) -> str:
    """화풍 글 하나를 limit 자 이내로 (style_raw 와 같은 자르기: 뒤쪽 절반 안의 쉼표 경계, 없으면 낱말 경계, 끝 문장부호는 뺀다)"""
    st = " ".join(_s(text).split()).rstrip(" .")
    if len(st) > limit:
        cut = st[:limit]
        comma = max(cut.rfind(","), cut.rfind(";"))
        st = (cut[:comma] if comma >= limit // 2 else (cut.rsplit(" ", 1)[0] if " " in cut else cut)).rstrip(" ,;:.")
    return st


def style_keep(story: dict, raw_limit: int = STYLE_RAW_MAX) -> str:
    """정지 그림(구도·인물 입히기·새로 그리기·끝 키프레임·고쳐 잇기) 지시문의 화풍 문장 = 영화 화풍 원문 + 열쇠 문장(STYLE_KEEP).
    11-24: 열쇠 문장 셋만 실으니 수채·점토·3D 같은 화풍도 첫 화면 단계에서 반실사 문장으로 모였다 → 원문을 앞에 싣는다. 원문이 비면 열쇠 문장만.
    모르는 열쇠는 반실사가 아니라 custom 문장('아닌 것'이 없다)으로 (11-24 2차)"""
    raw = style_raw(story, raw_limit)
    key = STYLE_KEEP.get(style_key(story)) or STYLE_KEEP["custom"]
    return (f"The art style of the whole film: {raw}. " if raw else "") + key


def _with_style(parts: list[str], story: dict, tail: str, limit: int = STILL_PROMPT_MAX) -> str:
    """앞 글(parts) + 화풍 문장(style_keep) + 끝 문장(정확한 사람·형상 수)을 limit 자 안에 넣는다. 넘치면 화풍 원문을 STYLE_RAW_MIN 까지
    먼저 줄이고, 그래도 넘치면 앞 글의 끝을 문장 경계에서 자른다: 끝 문장이 잘리면 복제·지어낸 사람을 막지 못한다 (11-24, 원문을 실어 길어졌다)"""
    head = " ".join(p for p in parts if p)
    keep = style_keep(story)
    over = len(head) + len(keep) + len(tail) + 2 - limit
    raw = style_raw(story)
    if over > 0 and raw:
        keep = style_keep(story, max(STYLE_RAW_MIN, len(raw) - over))
        over = len(head) + len(keep) + len(tail) + 2 - limit
    if over > 0:
        cut = head[:max(0, len(head) - over)]
        end = cut.rfind(". ")
        head = cut[:end + 1] if end >= len(cut) // 2 else (cut.rsplit(" ", 1)[0] if " " in cut else cut)
    return " ".join(x for x in (head, keep, tail) if x)[:limit]


def people_sentence(n: int) -> str:
    """그림 속 사람 수 문장: 한 사람이면 ONE_PERSON, 여럿이면 정확한 수 (복제·지어낸 사람을 막는다)"""
    if n <= 0:
        return "There is no person in the picture."
    if n == 1:
        return ONE_PERSON
    return f"Exactly {n} people in the picture, no other person, no reflection, no copy or mirror image of anyone."


def anchor_ids(story: dict) -> list[str]:
    """고정 기준 그림이 필요한 인물 id: 어느 장면에든 나오는 사람(동물 아님), 처음 나오는 차례대로. 대사가 없어도 모든 긴 영상에 둔다"""
    looks = {c["id"]: c["look"] for c in story["bible"]["characters"]}
    out = []
    for sc in story["scenes"]:
        for cid in list(sc.get("characters") or []) + ([sc["speaker"]] if sc.get("speaker") else []):
            if cid in looks and cid not in out and not is_animal_look(looks[cid]):
                out.append(cid)
    return out


def scene_people(story: dict, i: int) -> list[str]:
    """장면 i 에 나오는 사람 id (동물 아님). 말하는 사람이 characters 에 없어도 넣는다 (화면 밖 목소리로 다듬은 장면 — offscreen — 은 넣지 않는다:
    문 밖에서 부르는 엄마를 다락방에 그렸다, 4차 검토). 그 자리 사람 칸(present, 새 방식)이 있는 장면은 그 칸에 있는 말하는 사람만 넣는다 —
    그 자리에 없는 사람의 목소리를 그 장소에 그리지 않는다 (11-25). 자리 지도·자리 차례·바라보는 쪽(_run_seats·group_order·layout_slots·
    facing_of)과 감독의 대사 컷 경로가 이 사람들로 정해진다"""
    looks = {c["id"]: c["look"] for c in story["bible"]["characters"]}
    sc = story["scenes"][i]
    ids = list(dict.fromkeys(sc.get("characters") or []))       # 같은 id 가 두 번 적혀도 한 번 (구도 그림의 형상이 둘이 되지 않게)
    here = sc.get("present") if isinstance(sc.get("present"), list) else None
    if sc.get("speaker") and sc["speaker"] not in ids and not sc.get("offscreen") and (here is None or sc["speaker"] in here):
        ids.append(sc["speaker"])
    return [c for c in ids if c in looks and not is_animal_look(looks[c])]


def pick_refs(story: dict, i: int, have: list[str], limit: int = 2) -> list[str]:
    """편집 그림 2·3 자리에 넣을 인물 id (고정 기준 그림이 있는 사람만). 말하는 사람이 먼저, 그다음 나오는 차례.
    그림이 limit 장을 넘으면 넘친 인물은 글로만 설명한다"""
    people = [c for c in scene_people(story, i) if c in have]
    sp = story["scenes"][i].get("speaker")
    people.sort(key=lambda c: 0 if c == sp else 1)
    return people[:limit]


def _who(story: dict, cid: str) -> str:
    return ((next((c["look"] for c in story["bible"]["characters"] if c["id"] == cid), "") or "").split(",")[0]).strip().rstrip(".")


def cut_edit_prompt(story: dict, i: int, ref_ids: list[str], seam: str | None = None) -> str:
    """앞 장면 마지막 프레임(그림 1)을 장면 i 의 첫 화면으로 고치는 편집 지시 (Qwen 편집, 그림 2·3 = ref_ids 순서의 인물 기준 그림).
    그림 1 은 구도·빛·장소·그림체, 얼굴·머리·옷은 늘 그 인물의 기준 그림에서 (일관성이 가장 중요하다, 11-20 사용자).
    seam: cut(같은 장소·시간의 새 샷), jump(장소·시간 바뀜: 그림 1 의 빛·색 문장을 뺀다), refresh(같은 구도를 다시 그림)"""
    sc = story["scenes"][i]
    b = story["bible"]
    looks = {c["id"]: c["look"] for c in b["characters"]}
    place = next((p["look"] for p in b["places"] if p["id"] == sc["place"]), "")
    people = scene_people(story, i)
    parts = []
    if seam == SEAM_REFRESH:
        parts.append("Redraw the same camera shot as image 1, the same moment and composition, with the same people in the same places, "
                     "with clean sharp detail on every face and the original light and colours.")
    elif seam == SEAM_JUMP:
        parts.append("Cut to a new shot in a new place, built from image 1 only for the art style and the people.")
    else:
        parts.append("Cut to a new camera shot of the same moment in the same place, built from image 1.")
    # 독백 컷은 다른 사람을 가리키는 문장을 뺀다 (혼자 클로즈업인데 그 사람을 그려 넣는다, 11-24 2차 검토)
    act = _speaker_only(sc["action"], sc.get("speaker"), _story_cast(story)) if sc.get("monologue") is True else sc["action"]
    parts.append(f"New shot: {sc['shot']}." + (f" What happens: {act}" if act else ""))
    for n, cid in enumerate(ref_ids, start=2):
        parts.append(f"The person {_who(story, cid)} has exactly the face, hair and clothes of image {n}, not the face of image 1; "
                     "they appear once in the frame.")
    for cid in people:
        if cid not in ref_ids:
            parts.append(f"The person {looks.get(cid, '')} looks exactly as in image 1.")
    pair = talk_pair(story, i)
    if pair:
        # 두 사람 대사 컷: 구도 그림·소리 자리와 같은 좌우 (오른쪽을 보는 사람이 왼쪽 자리), 듣는 사람은 입을 다문다
        face_of = _pair_sides(story, i, pair)
        sp_pos = "left" if face_of[pair[0]] == "right" else "right"
        parts.append(f"The person {_who(story, pair[0])} is on the {sp_pos}, turned toward the {face_of[pair[0]]} side of the frame, "
                     f"speaking; the person {_who(story, pair[1])} is on the {_opposite(sp_pos)}, turned toward the speaker, listening "
                     f"with {LISTEN_FACE}; both faces in three-quarter view toward the camera.")
    elif talk_group(story, i):
        # 세 사람 대사 컷: 구도 그림·말하는 사람 칸(InfiniteTalk 마스크 1)과 같은 자리 차례 (group_order)
        slots = layout_slots(story, i, people)
        parts.append("The three people are side by side: " + ", ".join(f"the person {_who(story, s['id'])} {s['pos']}" for s in slots)
                     + "; every face in three-quarter view toward the camera.")
        parts.append(_act_sentence(story, i, slots))
    if seam == SEAM_JUMP:
        parts.append(f"The place is now: {place} Time: {sc.get('time') or b['time']} Light: {b['light']}")
    else:
        parts.append(f"The place: {place} Light: {b['light']} The same light and colours as image 1.")
    parts.append("All the people are fully in frame with their faces visible (front or three-quarter) and all are already in frame in the "
                 "first moment. The hands are not visible or relaxed at the sides.")
    return _with_style(parts, story, people_sentence(len(people)))


def end_keyframe_prompt(story: dict, i: int, ending: str = "") -> str:
    """장면 i 의 첫 화면(그림 1)에서 '이 샷이 끝나는 순간' 그림을 만드는 편집 지시. 같은 샷·같은 장소 안에서 자세·표정·작은 카메라 움직임만 바꾼다.
    인물은 모두 화면 안, 얼굴이 정면이나 3/4 로 보인다 (R2: 다음 장면은 이 끝에서 시작한다)"""
    people = scene_people(story, i)
    end = ending.strip().rstrip(".") or "everyone calm in the same frame, the camera settled"
    return _with_style([f"The same camera shot a few seconds later, at the very end of this shot: {end}. Keep the same place, the same light "
                        "and the same people with exactly the same faces, hair and clothes as image 1; only the pose, the expression and a small "
                        "camera move change. All the people stay fully in frame, standing or sitting calmly, faces visible (front or "
                        "three-quarter), none walking out of frame. The hands are relaxed."], story, people_sentence(len(people)))


def signature_of(story: dict, cid: str) -> list[str]:
    """설정집 인물의 알아볼 소품(라이브러리 signature, 예: 니키의 와인색 둥근 챙 모자). 없으면 빈 목록"""
    c = next((c for c in story["bible"]["characters"] if c["id"] == cid), {})
    return [str(x).strip() for x in (c.get("signature") or []) if str(x).strip()][:4]


def scene_place(story: dict, i: int) -> str | None:
    """장면 i 의 장소 id (설정집에 있는 장소만)"""
    pid = story["scenes"][i].get("place")
    return pid if any(p["id"] == pid for p in story["bible"]["places"]) else None


def compose_prompt(story: dict, i: int, ref_ids: list[str], place_image: int | None = None) -> str:
    """장면 i 의 첫 화면을 빈 화면에서 새로 그리는 지시 (compose_ref). 그림 1~ = ref_ids 차례의 인물 기준 그림, place_image = 장소 그림의
    번호(넣었으면). 앞 장면의 프레임은 넣지 않는다: 앞 프레임을 그림 1로 고치면 그 사람·배치가 남는다(11-20 요청 9089e3c7).
    짜임은 11-20 사용자가 승인한 메인 시험 N1~N3 과 같다: 그림마다 '그 그림의 얼굴·머리·옷 그대로' + 알아볼 소품, 장소, 화풍, 정확한 사람 수"""
    sc = story["scenes"][i]
    b = story["bible"]
    looks = {c["id"]: c["look"] for c in b["characters"]}
    place = next((p["look"] for p in b["places"] if p["id"] == sc["place"]), "")
    people = scene_people(story, i)
    talk = sc.get("kind") == "talk" and sc.get("speaker") in ref_ids
    # 대사 컷은 장면 글을 넣지 않는다: 화면 밖 상대(예: '니키를 보며')를 그려 넣어 사람이 둘이 됐다 (11-21 실험 R3, 3/3)
    parts = [f"A new picture: {sc['shot']}." if talk else f"A new picture: {sc['shot']}. What happens: {sc['action']}"]
    for n, cid in enumerate(ref_ids, start=1):
        sig = signature_of(story, cid)
        parts.append(f"The person from image {n} is {_who(story, cid)}, with exactly the face, hair and clothes of image {n}"
                     + (f", including the {', the '.join(sig)}" if sig else "") + "; this person appears once.")
    for cid in people:
        if cid not in ref_ids:
            parts.append(f"The person {looks.get(cid, '')}")
    sp = sc.get("speaker")
    pair = talk_pair(story, i)
    if talk and pair:
        # 두 사람 대사 컷 (구도 그림 없이 바로 그릴 때): 자리는 구도 그림·소리 자리와 같은 좌우 (오른쪽을 보는 사람이 왼쪽)
        face_of = _pair_sides(story, i, pair)
        lis = pair[1]
        sp_pos = "left" if face_of[sp] == "right" else "right"
        who_lis = f"the person from image {ref_ids.index(lis) + 1}" if lis in ref_ids else f"the person {_who(story, lis)}"
        parts.append(f"The person from image {ref_ids.index(sp) + 1} is on the {sp_pos} and speaks toward the {face_of[sp]} side of the "
                     f"frame with a {sc.get('emotion') or 'natural'} expression, the face in three-quarter view, lips slightly parted.")
        parts.append(f"{who_lis[:1].upper()}{who_lis[1:]} is on the {_opposite(sp_pos)}, turned toward the speaker, and listens with "
                     f"{LISTEN_FACE}. Both faces are in three-quarter view toward the camera.")
    elif talk and talk_group(story, i):
        # 세 사람 대사 컷 (구도 그림 없이 바로 그릴 때): 구도 그림·말하는 사람 칸(InfiniteTalk 마스크 1)과 같은 자리 차례 (group_order)
        slots = layout_slots(story, i, people)
        names = {c: (f"the person from image {ref_ids.index(c) + 1}" if c in ref_ids else f"the person {_who(story, c)}")
                 for c in people}
        parts.append("The three people are side by side: " + ", ".join(f"{names[s['id']]} {s['pos']}" for s in slots)
                     + "; every face in three-quarter view toward the camera.")
        parts.append(_act_sentence(story, i, slots))
    elif talk:
        parts.append(f"The person from image {ref_ids.index(sp) + 1} speaks toward the {facing_of(story, i, sp)} side of the frame "
                     f"with a {sc.get('emotion') or 'natural'} expression, the face in three-quarter view, lips slightly parted.")
    if place_image:
        parts.append(f"The place is the place shown in image {place_image}, with nobody else in it: {place} "
                     f"Time: {sc.get('time') or b['time']} Light: {b['light']}")
    else:
        parts.append(f"The place: {place} Time: {sc.get('time') or b['time']} Light: {b['light']}")
    parts.append("All the people are fully in frame with their faces visible (front or three-quarter). The hands are not visible or relaxed.")
    return _with_style(parts, story, people_sentence(len(people)))


def place_card_prompt(story: dict, pid: str) -> str:
    """장소 그림: 사람이 없는 그 장소 한 장 (장면 그림 레시피). 새로 그리는 컷마다 자리가 남으면 함께 넣어 배경을 맞춘다
    (11-20 승인 시험 N1·N2 는 부엌 그림을 함께 넣었다)"""
    b = story["bible"]
    place = next((p["look"] for p in b["places"] if p["id"] == pid), "")
    # 정본 장면 제작진의 영화 빛 계획이 있으면 대기(안개·먼지·김 …)도 싣는다: 사람 없는 그림이라 제작진 본래 설계와 맞고,
    # 그 장소의 모든 컷 그림 1 이 된다 (11-26)
    air = _s((story.get("crew") or {}).get("air"))
    return (f"An empty establishing view of this place with nobody in it: {place} Time: {b['time']} Light: {b['light']} "
            + (f"Air: {air}. " if air else "")
            + "Eye-level camera, the whole space visible, calm and still. There is no person, no animal and no figure in the picture. No text.")


# ── 구도 그림에서 그리기 (11-21 메인 실험, docs/reference-system-design.md 11절) ──
# ① 장소 그림 + 구도 글 → 얼굴 없는 형상이 정확한 수·자리에 있는 구도 그림, ② 그 그림을 바탕(Picture 1)으로 인물 기준 그림(Picture 2·3)을
# 입힌다, 세 사람 이상이면 ③ 한 명씩. 구도(사람 수·자리·카메라)는 ①이, 얼굴·옷은 기준 그림이 정한다
_AGE_CHILD_RE = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|[1-9]|1[0-3])"
                           r"[- ]years?[- ]old\b", re.I)
_CHILD_NOUN_RE = re.compile(r"\b(?:girl|boy|child|kid|toddler|baby|schoolgirl|schoolboy)\b", re.I)
_SHORT_RE = re.compile(r"\b(?:dwarf|dwarven|short|small|tiny|little)\b", re.I)
# 외형의 머리 구절(사람 낱말·나이·체격)이 끝나는 곳: 첫 쉼표나 with·in·wearing·holding 앞. 그 뒤의 'short hair'·'tiny glasses'·
# 'baby blue'·'holding her baby' 는 키가 아니다 (11-21 검토)
_HEAD_CUT_RE = re.compile(r"[,;]|\b(?:with|in|wearing|holding|carrying|who|and)\b", re.I)
_POSE_RES = (("sitting", re.compile(r"\b(?:sits?|sitting|seated)\b", re.I)),
             ("walking", re.compile(r"\b(?:walks?|walking|strolls?|strolling|wanders?|wandering)\b", re.I)),
             ("running", re.compile(r"\b(?:runs?|running|dashes|dashing|hurries|hurrying)\b", re.I)),
             ("standing", re.compile(r"\b(?:stands?|standing|stood)\b", re.I)),
             ("kneeling", re.compile(r"\b(?:kneels?|kneeling|crouch(?:es|ing)?)\b", re.I)),
             ("lying down", re.compile(r"\b(?:lies|lying)\b", re.I)))
_SIT_PLACE_RE = re.compile(r"\bat the (?:breakfast |dinner |kitchen |wooden )?(?:table|desk)\b|\bon (?:a|the) (?:bench|couch|sofa|chair|bed)\b",
                           re.I)
_STATIC_POSES = ("sitting", "standing", "kneeling", "lying down")


def _head_phrase(look: str) -> str:
    t = " ".join(str(look or "").split()[:14])
    m = _HEAD_CUT_RE.search(t)
    return t[:m.start()] if m else t


def figure_size(look: str) -> str:
    """구도 그림의 형상 키: 나이(열세 살 이하)나 머리 구절의 아이 낱말이면 아이, 머리 구절에 작은 키 낱말(dwarf·short …)이면 작은 사람"""
    if _AGE_CHILD_RE.search(" ".join(str(look or "").split()[:14])) or _CHILD_NOUN_RE.search(_head_phrase(look)):
        return "child-sized"
    if _SHORT_RE.search(_head_phrase(look)):
        return "short"
    return "adult-sized"


def _pose_cue(text: str) -> str | None:
    """글에서 가장 먼저 나오는 자세 동사 (첫 화면은 그 동작의 시작: '걸어와 앉는다'는 걷기). 동사가 없으면 '식탁에·의자에' 같은 말로 앉음"""
    best = None
    for pose, rx in _POSE_RES:
        m = rx.search(text or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), pose)
    if best:
        return best[1]
    return "sitting" if _SIT_PLACE_RE.search(text or "") else None


def scene_pose(story: dict, i: int) -> str:
    """구도 그림의 자세. 이 장면 글(동작·샷)에 자세 단서가 있으면 그것, 없으면 같은 장소가 이어지는 앞 장면 가운데 이 장면 사람이 나온
    가장 가까운 장면의 멈춘 자세(앉음·섬 등 — 식탁 장면의 대사 컷이 갑자기 서 있지 않게). 걷기·달리기는 순간 동작이라 물려받지 않고,
    장소가 바뀌면 거기서 멈춘다. 그 밖에는 서 있음"""
    scenes = story["scenes"]
    sc = scenes[i]
    cue = _pose_cue(" ".join([sc.get("action") or "", sc.get("shot") or ""]))
    if cue:
        return cue
    people = set(scene_people(story, i))
    for j in range(i - 1, -1, -1):
        pv = scenes[j]
        if pv.get("place") != sc.get("place"):
            break
        if people & set(scene_people(story, j)):
            pc = _pose_cue(" ".join([pv.get("action") or "", pv.get("shot") or ""]))
            if pc in _STATIC_POSES:
                return pc
    return "standing"


def layout_slots(story: dict, i: int, ids: list[str]) -> list[dict]:
    """구도 그림의 형상 차례(왼쪽→오른쪽)와 자리·크기·방향. 두 사람은 오른쪽을 보는 사람(speaker_side)을 왼쪽에 두어 마주 보게
    (두 사람 대사 컷을 함께하는 두 사람은 pair_facing: 듣는 사람은 말하는 사람의 반대쪽을 본다). 세 사람은 group_order: 같은 장소의
    연속 구간에서 셋이 처음 함께 나온 장면의 차례라, 말하는 사람이 바뀌어도 자리가 그대로다 (11-24). 넷 이상은 장면에 나오는 차례대로
    (두세 사람 규칙의 이야기는 넷 이상도 자리 지도, 5차 검토).
    두세 사람 규칙으로 다듬은 이야기(_seated)만 자리 차례를 쓴다: 꺼짐·대사 없는 영상·1차 이야기의 세 사람은 1차처럼 적힌 차례 (G0-9).
    그런 이야기의 두세 사람은 같은 장소 구간의 자리 지도(_seat_order: 넓은 장면·대사 컷 가운데 처음 함께 나온 장면의 좌우를 지킨다)를 따른다
    (11-24 2차·3차 검토).
    감독이 두세 사람 대사 컷의 말하는 사람 칸(InfiniteTalk 마스크 1)도 이 차례로 정한다. 그 자리 사람 칸(present)이 있는 이야기는 자리 지도·
    대사 컷 판정(scene_people·talk_group)이 그 칸을 따른다 (11-25). 돌려주는 값: [{"id", "pos", "size", "facing"}]"""
    looks = {c["id"]: c["look"] for c in story["bible"]["characters"]}
    ids = [c for c in ids if c in looks]
    n = len(ids)
    if n == 1:
        # 한 사람: 두 사람 대사 컷에 나오는 사람이면 그 투샷과 같은 쪽을 본다 (facing_of, 180도 규칙)
        return [{"id": ids[0], "pos": "in the centre", "size": figure_size(looks[ids[0]]),
                 "facing": f"turned slightly toward the {facing_of(story, i, ids[0])}"}]
    if n == 2 and _seated(story):
        # 두세 사람 규칙의 두 사람: 같은 장소 구간의 자리 지도 그대로 (넓은 장면·세 사람 컷에서 앉은 좌우), 서로 마주 본다
        order = _seat_order(story, i, ids)
        pos, face = ["on the left", "on the right"], ["turned toward the right", "turned toward the left"]
    elif n == 2 and (talk_pair(story, i) or _talks_together(story, ids)):
        # 두 사람 대사 컷을 함께하는 두 사람: 좌우를 둘의 바라보는 쪽(pair_facing)으로 정해 장면마다 같게 (180도 규칙)
        face_of = pair_facing(story, ids[0], ids[1])
        order = sorted(ids, key=lambda c: 0 if face_of[c] == "right" else 1)
        pos, face = ["on the left", "on the right"], ["turned toward the right", "turned toward the left"]
    elif n == 2:
        order = sorted(ids, key=lambda c: 0 if speaker_side(story, c) == "right" else 1)
        if speaker_side(story, order[0]) == speaker_side(story, order[1]):
            order = list(ids)
        pos, face = ["on the left", "on the right"], ["turned toward the right", "turned toward the left"]
    elif n == 3:
        order = _seat_order(story, i, ids) if _seated(story) else list(ids)
        pos = ["on the left", "in the middle", "on the right"]
        face = ["turned toward the right", "facing the camera", "turned toward the left"]
    else:
        # 넷 이상(넓은 장면)도 두세 사람 규칙의 이야기면 같은 장소 구간의 자리 지도를 따른다 (5차 검토: 적힌 차례 그대로 써서 대화 컷의 좌우와
        # 뒤집혔다 — 180도 규칙). 방향은 모두 서로를 향한다
        order = _seat_order(story, i, ids) if _seated(story) else list(ids)
        nth = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh"]
        pos = [f"{nth[k] if k < len(nth) else str(k + 1) + 'th'} from the left" for k in range(n)]
        face = ["turned toward the others"] * n
    return [{"id": c, "pos": pos[k], "size": figure_size(looks[c]), "facing": face[k]} for k, c in enumerate(order)]


def _frame_hint(rank: int | None, n: int, duo: bool = False) -> str:
    """샷 크기별 형상 크기 지시. 11-21 실험: 'medium close-up' 만으로는 미디엄 샷이 나와 얼굴이 작았다.
    duo(두세 사람 대사 컷)의 미디엄 투샷·그룹샷도 머리 위 여유를 둔다 (11-24: 머리가 화면 맨 위에 붙은 첫 화면은 대사 영상에서 머리가 잘렸다)"""
    if duo and rank == 2 and n == 2:
        return ("Both heads and upper bodies are fully in frame, side by side in the middle part of the picture, with clear empty space "
                "above the hair (about one tenth of the frame height); no head touches the top edge.")
    if duo and rank == 2 and n == 3:
        return ("All three heads and upper bodies are fully in frame, side by side in the middle part of the picture, with clear empty "
                "space above the hair (about one tenth of the frame height); no head touches the top edge.")
    if rank is not None and rank <= 1:
        if n == 1:
            # 11-24 메인: 머리가 화면 맨 위에 붙은 첫 화면은 입 맞춤 영상에서 카메라가 내려가며 머리가 잘렸다 (7초 중 4초 얼굴 없음)
            return ("The head and shoulders are large, but the whole head stays well inside the frame: clear empty space above the hair "
                    "(about one tenth of the frame height), the eyes about one third of the way down from the top edge, "
                    "the face in the middle part of the picture, the chest cut by the bottom edge.")
        return "The heads and shoulders are large, every head fully inside the frame with clear space above the hair."
    if rank == 2:
        return "The heads and upper bodies are fully in frame, in the middle part of the picture."
    return "Every figure is fully in frame, apart from the others."


_BACK_VIEW_RE = re.compile(r",?\s*\b(?:from behind|from the back|back view|rear view)\b", re.I)


def _layout_shot(shot: str) -> str:
    """구도 그림·첫 화면 설명에 쓰는 샷 글: '뒤에서' 같은 말을 뺀다 (얼굴이 보여야 기준 그림의 얼굴을 입히고 검사할 수 있다, 11-21 검토)"""
    t = _BACK_VIEW_RE.sub("", str(shot or "")).strip(" ,")
    return re.sub(r"\s{2,}", " ", t) or "medium shot"


def _place_of(story: dict, i: int) -> str:
    sc = story["scenes"][i]
    return next((p["look"] for p in story["bible"]["places"] if p["id"] == sc.get("place")), "")


def layout_prompt(story: dict, i: int, slots: list[dict]) -> str:
    """① 구도 그림: Picture 1 = 장소 그림. 장면 글(동작)은 넣지 않는다 — 화면 밖 인물의 이름이 들어 있으면 그 사람을 그려 넣는다
    (11-21 실험 R3: 혼자 대사 컷에 '니키를 보며'가 있자 3/3 베스가 둘). 형상 수·자리·방향·크기는 코드가 정한다"""
    sc = story["scenes"][i]
    b = story["bible"]
    looks = {c["id"]: c["look"] for c in b["characters"]}
    n = len(slots)
    pose = scene_pose(story, i)
    place = _place_of(story, i).strip()
    parts = [f"Picture 1 shows the place. Keep its architecture, furniture and props, seen from a slightly different camera angle: "
             f"{place}{'' if place.endswith(('.', '!', '?')) else '.'}",
             f"A new picture of this place: {_layout_shot(sc['shot'])}."]
    group = talk_group(story, i)
    duo = bool(group) and n == len(group)
    if n == 1:
        s = slots[0]
        parts.append(f"One {s['size']} faceless mannequin figure, {pose}, {s['facing']}, {s['pos']} of the frame.")
    elif duo and n == 2:
        # 두 사람 대사 컷: 서로 마주 보되 두 얼굴 모두 카메라 쪽 3/4 (입 맞춤이 두 입을 다 봐야 하고, 검사가 두 얼굴을 센다)
        parts.append(f"2 faceless mannequin figures, {pose}, turned toward each other, both faces in three-quarter view toward the camera: "
                     + "; ".join(f"{s['pos']} a {s['size']} figure {s['facing']}" for s in slots) + ".")
    elif duo:
        # 세 사람 대사 컷 (11-24 X12·X13): 나란히, 모든 머리가 카메라 쪽 3/4, 가운데는 카메라를 본다 (InfiniteTalk 마스크가 화면을
        # 세 칸으로 나누므로 자리가 겹치지 않게)
        parts.append(f"3 faceless mannequin figures, {pose}, side by side, every head in three-quarter view toward the camera and the middle "
                     "figure looking at the camera: " + "; ".join(f"{s['pos']} a {s['size']} figure {s['facing']}" for s in slots) + ".")
    else:
        parts.append(f"{n} faceless mannequin figures, {pose}: " + "; ".join(f"{s['pos']} a {s['size']} figure {s['facing']}" for s in slots) + ".")
    parts.append("Each figure has a smooth blank head with no face and no hair, and plain matte light grey clothes, like an artist's mannequin.")
    parts.append("Every head is turned toward the camera or in three-quarter view; no figure is seen from behind.")
    parts.append(_frame_hint(shot_rank(sc.get("shot") or ""), n, duo=duo))
    for cid in dict.fromkeys(sc.get("characters") or []):
        if cid in looks and is_animal_look(looks[cid]):
            parts.append(f"Also in the picture: {looks[cid]}")
    parts.append(f"Time: {sc.get('time') or b['time']} Light: {b['light']}")
    return _with_style(parts, story, f"Exactly {n} figure{'s' if n != 1 else ''}, no other figure or person.")


def background_sentence(story: dict, i: int) -> str:
    """② ③ 에 꼭 넣는 배경 문장. 빼면 기준 그림(단색 배경의 초상화)이 바탕 그림을 이긴다 (11-21 실험 R3: 3/3 단색 배경, 넣으면 4/4)"""
    place = " ".join(_place_of(story, i).split()[:28]).rstrip(" ,;.")
    return f"The background stays exactly the place of Picture 1 ({place}); it is not a plain studio background."


def _put_sentence(story: dict, slot: dict, n: int) -> str:
    sig = signature_of(story, slot["id"])
    return (f"Replace the faceless figure {slot['pos']} with the person in Picture {n}, {_who(story, slot['id'])}: take only the face, hair, "
            f"body and clothes of Picture {n}" + (f", including the {', the '.join(sig)}" if sig else "") + ".")


# 듣는 사람의 표정 (11-24 X13: 듣는 베스·제임스가 첫 그림의 활짝 웃는 표정(이가 보임)을 이어 가 입이 벌어져 보였다 → 입을 다문 미소를
# 분명히). 첫 화면 인물 입히기·고쳐 잇기·새로 그리기·영상 글의 듣는 사람 문장이 모두 이 말을 쓴다
LISTEN_FACE = "lips closed and a gentle closed-mouth smile"


def _speak_sentence(story: dict, i: int, slots: list[dict], slot: dict) -> str:
    """두세 사람 대사 컷의 말하는 사람 문장: 실제 자리에서 듣는 사람 쪽으로 말한다 (두 사람은 상대 쪽, 세 사람은 듣는 사람이 있는 쪽,
    듣는 사람이 없으면 가장자리 자리는 안쪽으로, 가운데는 모두에게 카메라 쪽으로)"""
    sc = story["scenes"][i]
    mood = sc.get("emotion") or "natural"
    k = slots.index(slot)
    lis = next((s for s in slots if s["id"] == sc.get("listener") and s is not slot), None)
    if len(slots) == 2 or lis is None and k in (0, len(slots) - 1):
        toward = "right" if k == 0 else "left"
    elif lis is not None:
        toward = "right" if slots.index(lis) > k else "left"
    else:
        return (f"The person {slot['pos']} speaks to the others with a {mood} expression, the face turned toward the camera, "
                "lips slightly parted.")
    return (f"The person {slot['pos']} speaks toward the {toward} side of the frame with a {mood} expression, "
            "the face in three-quarter view, lips slightly parted.")


def _act_sentence(story: dict, i: int, slots: list[dict], only: list[str] | None = None) -> str:
    """표정·동작. 대사 컷은 말하는 방향과 감정만 (장면 글에 화면 밖 인물이 나와 그 사람을 그려 넣지 않게), 그 밖의 장면은 장면 글.
    only 는 세 사람 대사 컷을 한 명씩 입히는 단계(②·③)에서 이번에 입히는 사람: 그 사람의 표정만 쓰고, 말하는 사람이 아직 얼굴 없는
    형상이면 '사람'이라 부르지 않고 화면 쪽으로만 쓴다 (아직 얼굴 없는 형상에게 표정을 주면 그 형상을 사람으로 그린다)"""
    sc = story["scenes"][i]
    sp = sc.get("speaker")
    slot = next((s for s in slots if s["id"] == sp), None)
    group = talk_group(story, i) if sc.get("kind") == "talk" else None
    if slot and group and len(slots) == len(group):
        # 두세 사람 대사 컷: 말하는 방향은 실제 자리에서 듣는 사람 쪽, 듣는 사람은 입을 다물고 듣는다 (11-24 X1·X6·X12·X13)
        others = [s for s in slots if s is not slot]
        if only is not None:
            k = slots.index(slot)
            return " ".join(_speak_sentence(story, i, slots, s) if s is slot else
                            f"The person {s['pos']} listens with {LISTEN_FACE}, looking toward the "
                            f"{'right' if k > slots.index(s) else 'left'} side of the frame."
                            for s in slots if s["id"] in only)
        speak = _speak_sentence(story, i, slots, slot)
        if len(others) == 1:
            return f"{speak} The person {others[0]['pos']} listens with {LISTEN_FACE}, looking at the speaker."
        return (f"{speak} The others, {others[0]['pos']} and {others[1]['pos']}, listen with {LISTEN_FACE}, "
                "looking at the speaker.")
    if sc.get("kind") == "talk" and slot:
        mood = sc.get("emotion") or "natural"
        return (f"The person {slot['pos']} speaks toward the {facing_of(story, i, sp)} side of the frame with a {mood} expression, "
                "the face in three-quarter view, lips slightly parted.")
    return f"What happens: {sc['action']}"


def identity_prompt(story: dict, i: int, slots: list[dict], put: list[str], final: bool) -> str:
    """② 구도 그림(Picture 1)의 형상에 인물을 입힌다. put = 이번에 입힐 인물 id (Picture 2·3 차례, 2명까지).
    final 이 아니면 남은 형상은 그대로 두라고 쓴다 (③ 이 한 명씩 입힌다)"""
    by = {s["id"]: s for s in slots}
    parts = ["Picture 1 is the layout. Keep exactly its camera, framing, the place, the light, the art style, and every figure's "
             "position, size and pose."]
    for n, cid in enumerate(put, start=2):
        parts.append(_put_sentence(story, by[cid], n))
    if not final:
        for s in slots:
            if s["id"] not in put:
                parts.append(f"Keep the faceless figure {s['pos']} exactly as it is.")
    parts.append("Take nothing else from " + ("Picture 2 or Picture 3." if len(put) > 1 else "Picture 2."))
    parts.append(background_sentence(story, i))
    parts.append(_act_sentence(story, i, slots, only=None if final else put))
    parts.append("Every face is visible, front or three-quarter. The hands are not visible or relaxed.")
    rank = shot_rank(story["scenes"][i].get("shot") or "")
    if rank is not None and rank <= 1:
        parts.append("Keep the head where it is in Picture 1, with clear empty space above the hair; do not move the head to the top edge "
                     "and do not enlarge the figure.")
    elif _group_heads(story, i, slots):
        parts.append(_group_heads(story, i, slots))
    return _with_style(parts, story, people_sentence(len(slots)) if final else f"Exactly {len(slots)} figures in total, no other person.")


def _group_heads(story: dict, i: int, slots: list[dict]) -> str:
    """두세 사람 대사 컷의 머리 자리 문장 (11-24: 머리가 화면 맨 위에 붙은 첫 화면은 대사 영상에서 머리가 잘렸다). 아니면 빈 글"""
    group = talk_group(story, i)
    if not group or len(slots) != len(group):
        return ""
    heads = "both heads" if len(slots) == 2 else "all three heads"
    return f"Keep {heads} where they are in Picture 1, with clear empty space above the hair; do not enlarge the figures."


def finish_prompt(story: dict, i: int, slots: list[dict], cid: str, done: list[str], final: bool) -> str:
    """③ 세 번째 사람부터 한 명씩: Picture 1 = 지금까지 그린 그림, Picture 2 = 그 사람의 기준 그림. done = 이미 입힌 인물 id"""
    by = {s["id"]: s for s in slots}
    parts = ["Picture 1 is the picture to finish. Keep everything in Picture 1 exactly: the camera, the place, the light, the art "
             "style and the people already in it.", "Replace only one figure. " + _put_sentence(story, by[cid], 2),
             "Take nothing else from Picture 2."]
    if not final:
        for s in slots:
            if s["id"] not in done and s["id"] != cid:
                parts.append(f"Keep the faceless figure {s['pos']} exactly as it is.")
    parts.append(background_sentence(story, i))
    heads = _group_heads(story, i, slots)
    if heads:
        # 세 사람 대사 컷의 세 번째 사람: 말하는 사람이면 말하는 표정, 듣는 사람이면 입을 다문 미소 (11-24 X13: 첫 화면의 표정을 영상이 이어 간다)
        parts.append(_act_sentence(story, i, slots, only=[cid]))
    parts.append("Every face is visible, front or three-quarter.")
    if heads:
        parts.append(heads)
    return _with_style(parts, story, people_sentence(len(slots)) if final else f"Exactly {len(slots)} figures in total, no other person.")


def layout_description(story: dict, i: int, slots: list[dict]) -> str:
    """구도 그림에서 그린 첫 화면의 설명. 영상 글을 쓰는 LLM 이 '첫 화면을 만든 글'로 읽으므로, 그림 번호·형상 같은 중간 단계 말 없이
    샷·자리별 인물·동작·장소만 쓴다 (세 사람 장면에 ② 의 '형상은 그대로' 글이 넘어가던 문제, 11-21 검토)"""
    sc = story["scenes"][i]
    b = story["bible"]
    looks = {c["id"]: c["look"] for c in b["characters"]}
    who = "; ".join(f"{s['pos']}: {str(looks.get(s['id'], '')).rstrip(' .')}" for s in slots)
    return (f"{_layout_shot(sc['shot'])}. {who}. What happens: {sc['action']} The place: {_place_of(story, i)} "
            f"Time: {sc.get('time') or b['time']} Light: {b['light']}")[:1990]


def style_convert_prompt(key: str, who: str, style: str = "") -> str:
    """인물 기준 그림을 영화 화풍으로 바꾸는 편집 지시 (11-20 메인 실측 S3~S8). 얼굴·머리·옷·소품은 그대로.
    - photo: 실사 변환 전용 레시피(Qwen-Image 2.1 편집)에서 통한 문장 모양 (11-24 메인 P1: 진짜 사진 질감에 같은 사람으로 보이고
      가르마·앞치마·호박 브로치가 남았다). 높음 편집으로 내려가도 같은 글
    - toon3d: 3D 애니메이션 문장 ('not a 3D render' 를 넣지 않는다)
    - custom: 열쇠 문장이 없어 영화 화풍 원문(style, 200자 이내)으로 그리게 한다. 무엇이 아닌지는 말하지 않는다 (11-24 2차).
      모르는 열쇠도 custom 문장으로. 사진 쪽 custom(누아르·흑백 무성영화, photo_like_text)은 '그린 그림'이 아니라 그 화풍의 실사 영화 한 컷으로
      (4차 검토: 'a character drawn in this art style: black-and-white film noir …' 이었다)"""
    who = who or "person"
    raw = trim_style(style)
    if key == "custom" and raw and photo_like_text(raw):
        return (f"Turn this portrait into a live-action film still of the same person, {who}, in this style: {raw}: real human skin, real hair "
                "and real fabric. Keep exactly the same face shape, eyes, hairstyle, hair colour, clothes and accessories, the same "
                "head-and-shoulders framing, centered, and the same plain background, the hands out of the frame. " + ONE_PERSON)
    if key == "photo":
        return (f"Turn this illustrated portrait into a real photograph of the same person, {who}: a live-action film still with real human "
                "skin and pores, real hair strands, real fabric and natural soft light. Keep exactly the same face shape, eyes, hairstyle, "
                "hair colour, clothes and accessories, the same head-and-shoulders framing, centered, and the same plain background, the "
                "hands out of the frame. It is not a painting, not an illustration, not a 3D render. " + ONE_PERSON)
    base = ("Redraw this exact {who} as {kind}: the same face shape, hairstyle, hair colour, eye colour and the same clothes and "
            "accessories, the same head-and-shoulders framing, centered, the hands out of the frame, a plain background. ")
    kinds = {"anime": "a character in a 2D anime feature film: clean cel-shaded line art, flat colours with soft shading. Not a photograph, not a 3D render",
             "toon3d": "a character in a stylized 3D animated feature film: smooth soft shading, rounded stylized proportions. Not a photograph, not a flat 2D drawing",
             "semi": "a calm, sharp head-and-shoulders portrait in a soft painted semi-realistic illustration style",
             "custom": f"a character drawn in this art style: {raw}" if raw else "a character drawn in the art style of the film"}
    return base.format(who=who, kind=kinds.get(key) or kinds["custom"]) + ONE_PERSON


# 장소 그림·사람 없는 장면 그림의 정밀 판(Qwen 2512 가속 끔 40스텝 cfg 4)에 넣는 화풍별 부정 글 (11-24 메인 P3a~f: cfg 4 라 실제로 적용되고,
# 빠른 판에 섞이던 반실사 질감이 빠졌다). 반실사는 빠른 판이라 없고, custom 은 무엇이 아닌지 몰라 레시피의 기본 부정 글을 그대로 쓴다
QUALITY_NEGATIVE = {
    "anime": "semi-realistic, photorealistic, photograph, 3D render, realistic lighting, skin pores, low quality, blurry, text, watermark",
    "photo": "painting, illustration, drawing, cartoon, anime, 3D render, low quality, blurry, text, watermark",
    "toon3d": "photograph, flat 2D drawing, low quality, blurry, text, watermark",
}


def quality_negative(story: dict) -> str:
    """이 영화 화풍의 정밀 판 부정 글 (QUALITY_NEGATIVE). 없으면 빈 글: 감독이 레시피의 기본 부정 글을 넣는다"""
    return QUALITY_NEGATIVE.get(style_key(story), "")


# 대사 컷 샷 글의 카메라 움직임 말 (영상 글 끝의 삼각대 문장과 부딪힌다, 11-24)
CAMERA_MOVE_WORD_RE = re.compile(r"\b(?:push(?:es|ed|ing)?|dolly|dollies|dollying|zoom(?:s|ed|ing)?|pan(?:s|ned|ning)?"
                                 r"|tilt(?:s|ed|ing)?|track(?:s|ed|ing)?)\b", re.I)
# 움직임 구: 앞의 이음말(with·as·while·and·or)과 꾸밈말('the camera slowly …'), 뒤의 방향 말까지 한 덩어리로 지운다
_CAMERA_MOVE_PHRASE_RE = re.compile(r"(?:\b(?:with|as|while|and|or)\s+)?"
                                    r"(?:\b(?:a|an|the|very|slow|slowly|gentle|gently|subtle|slight|steady|smooth|camera)\s+)*"
                                    + CAMERA_MOVE_WORD_RE.pattern
                                    + r"(?:[- ](?:in|out|forward|back|up|down|left|right|closer|camera|move|movement|shot))*", re.I)
# 움직임 구를 지운 뒤 끝에 매달린 말 ('… with a still or' 의 'with a still or', '… as the camera' 의 'as the camera')
_DANGLING_TAIL_RE = re.compile(r"(?:\s+(?:with|as|while|and|or|to|into|toward|towards|a|an|the|camera|still|very|slow|slowly|"
                               r"gentle|gently|subtle|slight|steady|smooth))+\s*$", re.I)


def still_shot(shot: str) -> str:
    """대사 컷의 샷 글에서 카메라 움직임(push in·pushing·dolly·zoom·pan·tilt·track)을 지운다. 쉼표로 나뉜 마디마다 움직임 구를 지우고,
    끝에 매달린 이음말·관사를 다듬는다 (11-24 검토: 'Medium close-up of Beth with a still or very slowly pushing camera' 가
    '… with a still or' 로 남았다). 움직임이 있던 마디는 샷 크기 말이 남을 때만 둔다('slow push-in toward her face' → 뺌).
    남는 것이 없으면 빈 글"""
    out = []
    for p in re.split(r"[,;]", str(shot or "")):
        p = p.strip()
        if p and CAMERA_MOVE_WORD_RE.search(p):
            p = re.sub(r"\s{2,}", " ", _CAMERA_MOVE_PHRASE_RE.sub(" ", p)).strip(" -,")
            p = _DANGLING_TAIL_RE.sub("", " " + p).strip(" -,")
            if shot_rank(p) is None:
                p = ""
        if p:
            out.append(p)
    return ", ".join(out)


def solo_shot(shot: str) -> str:
    """한 사람만 그리는 컷(기준 그림을 고친 첫 화면·혼자 대사 영상 글)의 샷 글: 투샷·그룹샷 글이면 말하는 사람 혼자의 미디엄 클로즈업으로.
    두세 사람 규칙으로 짠 대사 장면을 한 사람으로 만들 때(장면 잇기가 꺼짐·듣는 쪽이 동물) 'Medium two-shot … exactly one person'·
    'Medium two-shot of one person alone' 처럼 어긋난 글이 되던 것을 막는다 (11-24 검토)"""
    shot = str(shot or "")
    return "Medium close-up" if MULTI_SHOT_RE.search(shot) else shot


def talk_prompt(story: dict, i: int, solo: bool = False) -> str:
    """말하는 장면 영상 글 (LTX-2.3 IA2V). 다른 사람을 지어내지 않게 '화면에 혼자' 를 꼭 넣는다 (11-19 시범: 빼면 화면 밖 상대를 만들어 냄).
    샷 글의 카메라 움직임 말은 지운다 (11-24: 'very slowly pushing camera' 가 끝의 삼각대 문장과 부딪혔다).
    두세 사람 규칙으로 짠 대사 장면을 한 사람 첫 화면으로 만들 때(장면 잇기가 꺼짐)는 투샷 글을 혼자 크기로 바꾸고 장면 글(action)을 뺀다:
    'She smiles at him' 처럼 화면 밖 상대를 가리켜 그 사람을 지어낸다 (11-19·11-21 R3, 11-24 검토).
    독백 컷(monologue)은 같은 장소에 다른 사람이 있어도 혼자 클로즈업이라, 장면 글에서 다른 사람을 가리키는 문장을 뺀다 (_speaker_only,
    11-24 2차 검토: 'glances at her mother and father across the table' 이 그대로 들어갔다).
    사람이 그 자리의 동물에게 말하는 컷([니키, 강아지])은 첫 화면에 동물이 함께 있어 사람과 동물을 함께 쓴다 (_pet_talk_prompt, 5차 검토: '화면에
    혼자 있는 한 사람' 글을 썼다). solo 는 첫 화면이 기준 그림 한 장을 고친 한 사람 그림인가 (장면 잇기 꺼짐): 그러면 동물을 쓰지 않는다"""
    sc = story["scenes"][i]
    b = story["bible"]
    cid = sc.get("speaker") or (sc.get("characters") or [""])[0]
    place = next((p["look"] for p in b["places"] if p["id"] == sc["place"]), "")
    shot = solo_shot(still_shot(sc["shot"])) or "Close-up"
    pets = [c for c in dict.fromkeys(sc.get("characters") or []) if c != cid and is_animal_look(_look(story, c))]
    if pets and not solo and not is_animal_look(_look(story, cid)) and not talk_group(story, i) and sc.get("monologue") is not True:
        return _pet_talk_prompt(story, i, cid, pets, place)
    if is_animal_look(_look(story, cid)) and scene_people(story, i):
        # 동물과 사람이 함께 보이는 첫 화면: 샷 글은 다듬은 그대로(투샷·그룹샷), 가까운 크기면 미디엄 샷 (4차 검토: 'Medium close-up' 으로 바뀌었다)
        ashot = still_shot(sc["shot"])
        rank = shot_rank(ashot)
        return _animal_talk_prompt(story, i, cid, ashot if rank is not None and rank >= 2 else "medium shot", place)
    if talk_group(story, i):
        act = ""
    elif sc.get("monologue") is True:
        act = _speaker_only(sc["action"], cid, _story_cast(story))
        act = f"{act} " if act else ""
    else:
        act = f"{sc['action']} "
    return (f"{shot} of one person alone in the frame, no other people: {_look(story, cid)} {act}"
            f"The person speaks toward the {facing_of(story, i, cid)} edge of the frame, {sc.get('emotion') or 'natural'}, "
            "and no one else ever enters the frame; "
            "the lips and jaw move clearly with the words, natural blinking, small head movements and calm breathing. "
            f"Behind, softly out of focus: {place} Time: {b['time']} Light: {b['light']}"
            + _style_line(story) + (" " + style_lock(story) if style_lock(story) else "")
            + " The camera is locked on a tripod and never moves: no tilt, no pan, no zoom, no push in. The person stays seated or "
              "standing in the same place and the face stays in the same position inside the frame the whole time.")[:3000]


def _short(story: dict, cid: str) -> str:
    """외형 글의 머리 구절에서 관사를 뺀 짧은 이름 ('a small red fox with a blue scarf' → 'small red fox')"""
    head = _head_phrase(next((c["look"] for c in story["bible"]["characters"] if c["id"] == cid), "")).strip(" ,.")
    return re.sub(r"^(?:a|an|the)\s+", "", head, flags=re.I)


def _animal_talk_prompt(story: dict, i: int, cid: str, shot: str, place: str) -> str:
    """동물이 말하고 그 자리의 사람들이 듣는 컷의 영상 글 (LTX 대사). 11-24 3차 검토: 사람이 있는 첫 화면에 '화면에 혼자 있는 한 사람' 글을 썼다.
    말하는 것은 동물 하나, 사람들은 입을 다문 미소로 듣는다"""
    sc = story["scenes"][i]
    b = story["bible"]
    people = scene_people(story, i)
    who = _short(story, cid) or "animal"
    listen = (f"The person {_look(story, people[0])} only listens with {LISTEN_FACE}: the mouth stays closed." if len(people) == 1 else
              f"The {len(people)} people only listen with {LISTEN_FACE}, mouths closed: " + "; ".join(_look(story, c).rstrip(".") for c in people) + ".")
    return (f"{shot[:1].upper()}{shot[1:]}: {_look(story, cid)} Only the {who} speaks, {sc.get('emotion') or 'natural'}: its mouth moves clearly "
            f"with the words, natural blinking and small head movements. {listen} No one else enters the frame. "
            f"Behind, softly out of focus: {place} Time: {b['time']} Light: {b['light']}"
            + _style_line(story) + (" " + style_lock(story) if style_lock(story) else "")
            + " The camera is locked on a tripod and never moves: no tilt, no pan, no zoom, no push in.")[:3000]


def _pet_talk_prompt(story: dict, i: int, cid: str, pets: list[str], place: str) -> str:
    """사람 한 명이 그 자리의 동물에게 말하는 컷의 영상 글 (LTX 대사, _animal_talk_prompt 를 뒤집은 것). 샷 글은 다듬은 그대로(투샷·그룹샷 말은
    사람과 동물의 수에 맞춘다), 가까운 크기면 미디엄 샷 (동물도 화면의 한 몫이다, 4차 검토의 동물 화자와 같다). 장면 글은 사람을 가리키는 문장만
    빼고 둔다(_speaker_only). 말하는 것은 사람 하나, 동물은 소리 없이 듣는다"""
    sc = story["scenes"][i]
    b = story["bible"]
    ashot = still_shot(sc["shot"])
    rank = shot_rank(ashot)
    shot = ashot if rank is not None and rank >= 2 else "medium shot"
    shot = TWO_SHOT_RE.sub("group shot", shot) if len(pets) >= 2 else GROUP_SHOT_RE.sub("two-shot", shot)
    lis = sc.get("listener") if sc.get("listener") in pets else (pets[0] if len(pets) == 1 else "")
    to = f"the {_short(story, lis) or 'animal'}" if lis else "the animals"
    act = _speaker_only(sc.get("action"), cid, _story_cast(story))
    return (f"{shot[:1].upper()}{shot[1:]}: {_look(story, cid)} Also in the frame: " + "; ".join(_look(story, c).rstrip(".") for c in pets) + ". "
            + (f"{act} " if act else "")
            + f"The person speaks to {to}, {sc.get('emotion') or 'natural'}: the lips and jaw move clearly with the words, natural blinking, small "
            f"head movements and calm breathing. The {'animal makes no sound and only listens' if len(pets) == 1 else 'animals make no sound and only listen'}. "
            "No one else enters the frame. "
            f"Behind, softly out of focus: {place} Time: {b['time']} Light: {b['light']}"
            + _style_line(story) + (" " + style_lock(story) if style_lock(story) else "")
            + " The camera is locked on a tripod and never moves: no tilt, no pan, no zoom, no push in.")[:3000]


def duo_talk_prompt(story: dict, i: int, slots: list[dict]) -> str:
    """두세 사람 대사 컷 영상 글 (InfiniteTalk, 11-24 메인 X1·X6·X12·X13). 모두를 자리와 외형으로 쓰고, 누가 누구에게 말하는지도 자리로
    쓴다. 말하는 사람은 한 명, 나머지는 입을 다문 미소로 듣기만 한다 (X13: 듣는 사람이 첫 그림의 활짝 웃는 표정을 이어 가 입이 벌어져
    보였다). 장면의 action 원문은 넣지 않는다: 화면 밖 인물 이름이 섞여 그 사람을 지어냈다 (11-21 실험 R3).
    slots 는 layout_slots 차례(왼쪽→오른쪽) — 감독이 말하는 사람 칸(마스크 1)을 정한 것과 같은 차례. LTX 로 내려갈 때도 이 글을 쓴다"""
    sc = story["scenes"][i]
    b = story["bible"]
    n = len(slots)
    sp = next((s for s in slots if s["id"] == sc.get("speaker")), slots[0])
    others = [s for s in slots if s is not sp]
    looks = {c["id"]: c["look"] for c in b["characters"]}
    animals = [c for c in dict.fromkeys(sc.get("characters") or []) if c in looks and is_animal_look(looks[c])]
    pet = sc.get("listener") if sc.get("listener") in animals else None
    lis = None if pet else next((s for s in others if s["id"] == sc.get("listener")), others[0] if len(others) == 1 else None)
    shot = still_shot(_layout_shot(sc.get("shot") or "")) or GROUP_SHOT.get(n, "medium shot")
    if n >= 3:
        shot = TWO_SHOT_RE.sub("group shot", shot)
    place = _place_of(story, i).strip()
    count = {2: "two", 3: "three"}.get(n, str(n))
    parts = [f"{shot[:1].upper()}{shot[1:]} of {count} people in a conversation, {'both' if n == 2 else 'all'} fully in frame the whole time.",
             " ".join(f"{s['pos'][:1].upper()}{s['pos'][1:]}: {_look(story, s['id'])}" for s in slots)]
    if animals:
        # 화면의 동물 (3차 검토: 사람이 여우에게 말하는 컷의 영상 글에 여우가 없고 '사람에게 말한다'고 썼다)
        parts.append("Also in the frame: " + "; ".join(_look(story, c).rstrip(".") for c in animals) + ".")
    to = f"the {_short(story, pet)}" if pet else ("the person " + lis["pos"] if lis else "the others")
    parts.append(f"The person {sp['pos']} speaks to {to}, {sc.get('emotion') or 'natural'}: the lips and jaw move clearly with the words.")
    if len(others) == 1:
        parts.append(f"The person {others[0]['pos']} only listens with {LISTEN_FACE}: the mouth stays closed and the head nods softly.")
    else:
        parts.append(f"The people {' and '.join(s['pos'] for s in others)} only listen with {LISTEN_FACE}: their mouths stay closed and "
                     "their heads nod softly.")
    parts += ["Natural blinking and small head movements.",
              "The camera stays completely still.",
              f"The place: {place}{'' if place.endswith(('.', '!', '?')) else '.'} Time: {sc.get('time') or b['time']} Light: {b['light']}"]
    return (" ".join(parts) + _style_line(story) + (" " + style_lock(story) if style_lock(story) else ""))[:3000]


def line_tokens(line: str) -> int:
    """목소리 복제의 토큰 상한 (256 단위, 약 12토큰/초). 짧은 대사가 끝나지 않고 늘어지는 것을 막는다 (11-19)"""
    return 256 if len(line or "") <= 24 else 512


def line_max_seconds(line: str) -> float:
    """대사 소리가 이보다 길면 늘어진 것으로 보고 시드를 바꿔 한 번 더 녹음한다 (한국어 느린 말투 약 초당 3글자 + 여유)"""
    return 4.0 + len(line or "") * 0.45


def pov_line() -> str:
    return ("The user asked for a first-person view: the camera is the viewer's eyes and the viewer never appears as a "
            "figure. Open \"shot\" with \"First-person POV from\" and the position, show only what lies ahead, and show the "
            "viewer's body only as their own hands or forearms at the bottom or side edge of the frame. Never write that "
            "the viewer sits, wears or looks, never describe the viewer's clothing, headset, helmet, head, hair, face, "
            "shoulders or back, and never frame the shot over a shoulder or from behind the viewer. Other people in the "
            "scene are described normally.")


# 1인칭으로 무언가를 조종·운전하는 요청 (드론 FPV 처럼 조종자의 손이 보이지 않는 것은 뺀다)
OPERATE_RE = re.compile(r"운항|조종|운전|핸들|스티어링|조종간|조종석|운전석|파일럿|조종사|기장|운전자")
FPV_RE = re.compile(r"드론|FPV|fpv")


def operating_pov(said: str) -> bool:
    return pov_wanted(said) and bool(OPERATE_RE.search(said or "")) and not FPV_RE.search(said or "") and not hides_hands(said)


def operate_line() -> str:
    return ("The viewer is operating something (flying, driving, steering): show the viewer's own hands or forearms on the "
            "real controls of that vehicle (yoke, wheel, handlebars) at the bottom edge of the frame as a small supporting "
            "detail, about a tenth of the frame, never the main subject. The hands come last in the visual hierarchy: the "
            "view ahead and the whole interior read first and second. The one main action is the view in motion (clouds or "
            "road passing, light shifting, displays glowing), not the hands; the hands may move once, slightly. Do not "
            "describe turning or steering motions unless the user asks. Show no other person.")


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

def timeline_ranges(seconds) -> list[tuple[int, int, int]] | None:
    """영상 길이를 세 구간(약 30%·40%·30%)으로: 10초면 (0,3)(3,7)(7,10). 5초 미만은 나누지 않는다 (러다이트의 구간 방식)."""
    try:
        n = int(float(seconds) + 0.5)
    except (TypeError, ValueError, OverflowError):
        return None
    if n < 5:
        return None
    a, b = int(n * 0.3 + 0.5), int(n * 0.7 + 0.5)
    return [(0, a, 1), (a, b, 2), (b, n, 3)]


TIMELINE_LINE = ('TIMELINE (write the "action" paragraph as three timed beats, one sentence group each, opening each with these exact '
                 'ranges): {ranges}. Beat 1 is the opening moment, beat 2 is the one main action unfolding, beat 3 is how it ends '
                 "(the subject keeps its pace if it travels). Keep every beat about the same one action; add no new events.")


def timeline_text(seconds) -> str | None:
    r = timeline_ranges(seconds)
    if not r:
        return None
    return TIMELINE_LINE.format(ranges="; ".join(f'"From {a} to {b} seconds, ..."' for a, b, _ in r))


def build_messages(text: str, notes: str, profile: dict, seconds: float, frame: dict | None = None, scene: str = "",
                   cat: str | None = None, menus: dict | None = None, rng=None, style: str = "",
                   staging: str | None = None, plan: dict | None = None) -> list[dict]:
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
        ex = spec.get("example") if mode == "t2v" and not profile.get("mid") else None
        example = ("\n\nEXAMPLE of the length, density and kind of detail wanted (a different, live-action request: never "
                   "copy its subject, objects, setting, sentences or film finish, and never use its nouns "
                   f"({', '.join(spec.get('example_words') or [])[:120]}) unless the request itself names them; "
                   "take the medium and finish from the request, the LOOK REFERENCE and the STYLE line):\n"
                   + "\n".join(f'- "{p["key"]}": {ex[p["key"]]}' for p in spec["parts"])) if ex else ""
        target = aim_words(profile)
        system = SYSTEM_LONG.format(
            model=model_name(profile), seconds=seconds, mode_rule=spec["modes"][mode],
            rules="\n".join(f"- {r}" for r in kn["global_rules"] + spec["rules"]), field=field,
            choices_line=choices_line, parts="\n".join(f'- "{p["key"]}": one English paragraph: {part_spec(p, mode)}.'
                                                       for p in spec["parts"]),
            lo=lo, hi=hi, target=target, part_target=target // len(spec["parts"]), part_min=part_min(profile),
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
        if operating_pov(said):
            parts.append(operate_line())
    if not people_ok(said, frame, scene, frame is not None or profile["mode"] == "t2v"):
        parts.append("No person appears in the request, the scene or the first frame; keep every human out of the clip.")
    if hides_hands(said):
        parts.append("The hands stay hidden: never write hand, finger, palm, fist, holding, grip, grasp or clutching.")
    sl = staging_line(staging) if long else ""
    if sl:
        parts.append(sl)
    pb = plan_block(plan, bool(profile.get("timeline"))) if long else ""
    if pb:
        parts.append(pb)
    tl = timeline_text(seconds) if profile.get("timeline") and long else None
    if tl:
        parts.append(tl)
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


STOP_WORDS = frozenset((
    "about above after again against along around because before behind below beside between camera clear clearly close "
    "closer could every exact exactly fixed frame framing from gentle gently ground hold holds holding keeps later light lights "
    "lighting moment moves moving never other over place position positions reads remain remains scene shape shapes shot "
    "slow slowly small soft softly stays still steady surface surfaces their there these those through toward under until "
    "where which while whole within without would above first second third final calm same fine film look finish grain "
    "detail details edge edges across along behind another motion pace light's one two three that this with into onto "
    "from have been than then they them what when also each just like more most much only some such very").split())


# 장면이 달라도 겹치기 쉬운 꾸밈말 (겹쳐도 같은 장면의 증거가 아니다)
DECOR_WORDS = frozenset(("bright glowing warm cool large small thin thick pale dark deep faint smooth sunlit golden "
                         "continuous background foreground midground visual hierarchy perspective soft gentle quiet "
                         "early morning window glass surface reads first second third").split())


def _alpha_words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", str(text or "").lower())


# 요청에 없는 다른 비행기·사람이 글에 들어갔는지 (11-16: 조종석 요청에 부기장 실루엣, 창밖에 다른 비행기)
EXTRA_AIRCRAFT_RE = re.compile(r"\b(?:another|other|second|distant|nearby|passing|approaching|formation of)\s+"
                               r"(?:aircraft|airplanes?|planes?|jets?|jetliners?|airliners?|helicopters?)\b|\bcontrails?\b", re.I)
EXTRA_CREW_RE = re.compile(r"\b(?:co-?pilots?|passengers?|crew members?|flight attendants?|second pilot|another pilot)\b", re.I)
ALLOW_AIRCRAFT_RE = re.compile(r"다른 비행기|옆 비행기|편대|지나가는|전투기|헬리콥터|여객기가|비행운")
ALLOW_CREW_RE = re.compile(r"부기장|승객|승무원|동료|co-?pilot|함께|같이")


HANDS_FIRST_RE = re.compile(r"\b(?:hands?|forearms?|yoke)\b[^.]{0,70}\bread(?:s)? first\b", re.I)
HAND_FOCUS_RE = re.compile(r"\b(?:hands?|fingers?|forearms?|grip\w*|yoke|steering)\b", re.I)


def hand_focus_problems(result: dict, said: str) -> list[str]:
    """조종·운전 1인칭 요청에서 손이 주인공이 되면 디테일이 손으로 쏠려 장면이 뭉개지고 핸들처럼 나온다
    (11-16 파일럿 요청: 'the hands read first' + 움직임 단락 전체가 손). 손은 조연이어야 한다"""
    if not operating_pov(said):
        return []
    problems = []
    if HANDS_FIRST_RE.search(" ".join(str(result.get(k) or "") for k in ("shot", "camera_light"))):
        problems.append("The hands must not read first: in the visual hierarchy the view ahead and the interior come first and "
                        "second, and the hands come last as a small detail at the bottom edge.")
    if len(HAND_FOCUS_RE.findall(str(result.get("action") or ""))) >= 7:
        problems.append('"action" is mostly about the hands. Make the main action the view in motion (clouds or road passing, '
                        "light shifting, displays glowing) and mention the hands at most twice, with one slight movement.")
    return problems


def extra_subject_problems(prompt: str, said: str) -> list[str]:
    """요청이 이름 붙이지 않은 다른 비행기(창밖)나 다른 사람(조종 1인칭 요청)이 글에 있으면 위반"""
    found = []
    if not ALLOW_AIRCRAFT_RE.search(said or ""):
        found += [m.group(0).lower() for m in EXTRA_AIRCRAFT_RE.finditer(prompt)]
    if operating_pov(said) and not ALLOW_CREW_RE.search(said or ""):
        found += [m.group(0).lower() for m in EXTRA_CREW_RE.finditer(prompt)]
    if not found:
        return []
    uniq = ", ".join(sorted(set(found)))
    return [f"The request names no such subject, so remove: {uniq}. Say what lies beyond the windshield or window and that it holds "
            "only what the request names (for example only clouds and open sky), and show only the viewer's own hands."]


# ── 카메라와 대상의 동선 (11-16 글→영상 장문 비교 평가) ──
# T2V 장문이 "a man ... walks away from the camera" 와 "The camera moves backward ... maintaining a constant distance" 를
# 함께 썼다. 둘 다 서로 멀어지는 방향이라 거리를 지킬 수 없고, 영상 모델이 어느 쪽을 따를지 모른다 [추정: 뒷걸음질 같은 결과].
# 대상은 사람·탈것의 이동 동사로만 본다 (mist·steam·light 의 drift·move 는 동선이 아니다. 명사 'head' 도 뺀다)
_GO = (r"(?:walk(?:s|ing|ed)?|strid(?:e|es|ing)|strode|run(?:s|ning)?|ran|jog(?:s|ging)?|march(?:es|ing)?|stroll(?:s|ing)?"
       r"|wander(?:s|ing)?|heads|heading|hurr(?:ies|ying)|sprint(?:s|ing)?|trudg(?:es|ing)|step(?:s|ping)?|driv(?:e|es|ing)"
       r"|rid(?:e|es|ing)|cycl(?:e|es|ing)|skat(?:e|es|ing)|fl(?:y|ies|ying)|swim(?:s|ming)?)")
_EYE = r"(?:the |our )?(?:camera|viewer|lens|us)\b"
SUBJECT_AWAY_RE = re.compile(
    rf"\b{_GO}\b(?:\s+[\w'’-]+){{0,3}}?\s+away from {_EYE}"
    rf"|\b{_GO}\b(?:\s+[\w'’-]+){{0,3}}?\s+(?:deeper |further |farther )?into the (?:far )?distance\b"
    rf"|\b{_GO}\b(?:\s+[\w'’-]+){{0,3}}?\s+(?:toward|towards) the (?:horizon|vanishing point|far end)\b", re.I)
SUBJECT_TOWARD_RE = re.compile(
    rf"\b{_GO}\b(?:\s+[\w'’-]+){{0,3}}?\s+(?:toward|towards|straight at|right at|up to|closer to) {_EYE}"
    rf"|\bapproach(?:es|ing)? {_EYE}|\b(?:comes?|coming)\b(?:\s+[\w'’-]+){{0,2}}?\s+(?:toward|towards|closer to) {_EYE}", re.I)
# 카메라가 어느 쪽으로 가는지. 주어가 카메라일 때만 본다(_CAM: 'as he moves away' 의 he 는 카메라가 아니다).
# 'back to its starting position'(루프로 돌아오기)과 'moves in sync'(속도 맞추기)는 방향이 아니다
_TRAVEL_VERB = (r"(?:mov(?:e|es|ing)|travel(?:s|l?ing)?|glid(?:e|es|ing)|doll(?:y|ies|ying)|track(?:s|ing)?|pull(?:s|ing)?"
                r"|push(?:es|ing)?|retreat(?:s|ing)?|back(?:s|ing)|drift(?:s|ing)?|eas(?:e|es|ing)|slid(?:e|es|ing)"
                r"|creep(?:s|ing)?|advanc(?:e|es|ing)|float(?:s|ing)?|crawl(?:s|ing)?)\b(?:\s+[\w-]+){0,2}?\s+")
CAMERA_BACK_RE = re.compile(
    _CAM + _TRAVEL_VERB + r"(?:backwards?|away|back(?!\s+(?:to|into|toward|towards|at)\b))\b"
    r"|\bpull(?:s|ing)?[- ]back\b|\bdolly[- ](?:out|back)\b|\bbackward (?:dolly|pull|push|track(?:ing)?|move|travel|glide)\b", re.I)
CAMERA_FORWARD_RE = re.compile(
    _CAM + _TRAVEL_VERB + r"(?:forwards?|closer|toward(?:s)?(?! (?:the )?(?:left|right)\b)"
    r"|in(?=\s*(?:[,.;]|toward|towards|on\b|closer|slowly|gently|steadily|smoothly|$)))"
    r"|" + _CAM + r"follow(?:s|ing)?\b(?:\s+[\w-]+){0,2}?\s+(?:behind|after)\b"
    r"|\bpush(?:es|ing)?[- ]in\b|\bdolly[- ]in\b|\bforward (?:dolly|push|track(?:ing)?|move|travel|glide)\b", re.I)
CAMERA_SIDEWAYS_RE = re.compile(
    _CAM + r"(?:track(?:s|ing)?|truck(?:s|ing)?|mov(?:e|es|ing)|glid(?:e|es|ing)|slid(?:e|es|ing)|travel(?:s|l?ing)?"
    r"|drift(?:s|ing)?|doll(?:y|ies|ying))\b(?:\s+[\w-]+){0,2}?\s+(?:sideways|laterally|alongside|beside|parallel|to the (?:left|right))\b"
    r"|\b(?:lateral|sideways|side-on|crab) (?:track(?:ing)?|dolly|move|travel|glide|shot)\b|\btrucks? (?:left|right)\b", re.I)
CAMERA_STILL_RE = re.compile(
    r"\b(?:locked[- ]off|static (?:shot|camera|frame|framing)|stationary camera|fixed camera)\b"
    r"|\bcamera\b(?:\s+[\w-]+){0,2}?\s+(?:stays|remains|holds|sits)\s+(?:perfectly |completely |entirely )?"
    r"(?:still|static|locked|fixed|stationary|motionless|in place)\b", re.I)
# 카메라가 대상과 거리를 지킨다는 말. 'same size'(안정성 문장 'keeps the same size and shape' 에 흔하다)는 보지 않는다
KEEP_DISTANCE_RE = re.compile(
    r"\b(?:constant|same|steady|fixed|even|consistent|unchanging|unchanged) distance\b"
    r"|\bkeep(?:s|ing)? (?:up )?pace\b|\bmatched pace\b"
    r"|\bin (?:sync|step|time|lockstep) with (?:his|her|their|its|the [\w-]+(?:['’]s)?) (?:walking )?(?:pace|stride|steps|speed|walk|movement)\b"
    r"|\bmatch(?:es|ing|ed)? (?:his|her|their|its|the [\w-]+(?:['’]s)?) (?:walking )?(?:pace|stride|speed|steps)\b", re.I)


def blocking_problems(prompt: str) -> list[str]:
    """카메라가 대상과 거리를 지킨다고 쓰면서 둘이 서로 멀어지거나 가까워지는 방향으로 움직이면 위반이다.

    대상이 멀어지는데 카메라가 뒤로 물러나기, 대상이 다가오는데 카메라가 앞으로 가기, 카메라가 서 있거나 옆으로만 가는데
    다가오거나 멀어지는 대상과 거리가 같다고 쓰기. 거리를 지킨다는 말이 없으면 크기가 바뀌는 연출일 수 있어 보지 않는다.
    """
    text = re.sub(r"\s+", " ", prompt or "")
    dist = KEEP_DISTANCE_RE.search(text)
    away, toward = SUBJECT_AWAY_RE.search(text), SUBJECT_TOWARD_RE.search(text)
    if not dist or not (away or toward):
        return []
    fwd, back = CAMERA_FORWARD_RE.search(text), CAMERA_BACK_RE.search(text)
    head = "Camera and subject directions disagree: "
    fix = ("Make them agree in every part: either the camera moves forward behind the subject at its pace while it walks away, "
           "or the subject walks toward the camera while the camera moves backward ahead of it at the same pace.")
    if away and back:
        return [head + f'the subject moves away from the camera ("{away.group(0)}") while the camera moves backward '
                f'("{back.group(0)}") and keeps the same distance ("{dist.group(0)}"); moving apart, they cannot keep that '
                "distance. " + fix]
    if toward and fwd:
        return [head + f'the subject moves toward the camera ("{toward.group(0)}") while the camera moves forward '
                f'("{fwd.group(0)}") and keeps the same distance ("{dist.group(0)}"); moving toward each other, they cannot '
                "keep that distance. " + fix]
    if fwd or back:
        return []
    still = CAMERA_STILL_RE.search(text)
    cam = still or CAMERA_SIDEWAYS_RE.search(text)
    if not cam:
        return []
    way = away or toward
    return [head + f'the camera {"stays in place" if still else "travels sideways"} ("{cam.group(0)}") yet keeps the same '
            f'distance ("{dist.group(0)}") from a subject that moves {"away from" if away else "toward"} it ("{way.group(0)}"). '
            "Either move the camera with the subject at its pace (forward behind it, or backward ahead of it), or keep the "
            f"camera as it is and show the subject growing {'smaller' if away else 'larger'} in the frame."]


def lateral_camera_problems(prompt: str, said: str = "") -> list[str]:
    """11-17 메인 실측: '카메라가 남자 앞에서 뒤로 물러난다' + '남자가 왼쪽에서 오른쪽으로 걷는다' 를 함께 썼더니 영상에서 남자가
    카메라 쪽으로 걸어와 5초에 화면 밖으로 나갔다. 옆으로 가는 대상은 카메라가 나란히 따라가거나 고정이어야 하고,
    앞뒤(깊이)로 움직이는 카메라와는 짝이 맞지 않는다. 1인칭 요청은 보지 않는다."""
    if pov_wanted(said):
        return []
    text = re.sub(r"\s+", " ", prompt or "")
    side = next((m for sent in _sentences(text) if _travels(sent) for m in [LATERAL_RE.search(sent)] if m), None)
    if not side or SUBJECT_AWAY_RE.search(text) or SUBJECT_TOWARD_RE.search(text):
        return []                                   # 깊이로도 가는 글이면 blocking_problems 가 본다
    cam = CAMERA_BACK_RE.search(text) or CAMERA_FORWARD_RE.search(text)
    if not cam or CAMERA_SIDEWAYS_RE.search(text):
        return []
    return [f'The subject travels sideways ("{side.group(0)}") but the camera moves in depth ("{cam.group(0)}"); the two do not '
            "match and the video model will pick one. For sideways travel, either track alongside the subject at its pace "
            "(camera moves left to right with it) or lock the camera off and let the subject cross the frame; keep the subject "
            "inside the frame for the whole clip."]


# 걷기·달리기·주행은 짧은 클립 내내 같은 속도로 (11-16: "Then he begins to stride forward", "the man's stride slows").
# 멈춰 있다 출발하거나 느려져 멈추는 동작은 짧은 클립에서 어색해지기 쉽다 [추정]. 사용자가 출발·멈춤을 말했으면 보지 않는다
_TRAVEL = (r"(?:walk(?:s|ing)?|strid(?:e|es|ing)|run(?:s|ning)?|jog(?:s|ging)?|march(?:es|ing)?|stroll(?:s|ing)?"
           r"|driv(?:e|es|ing)|rid(?:e|es|ing)|cycl(?:e|es|ing)|skat(?:e|es|ing)|sprint(?:s|ing)?)")
TRAVEL_START_RE = re.compile(
    rf"\b(?:begins?|began|starts?|started|beginning|starting) (?:to )?{_TRAVEL}\b"
    r"|\btakes? (?:his|her|their|its|a) first steps?\b"
    r"|\bsets? (?:off|out) (?:down|along|across|toward|towards|into|on|through)\b"
    r"|\b(?:breaks?|bursts?) into a (?:walk|run|jog|sprint|stride)\b", re.I)
TRAVEL_STOP_RE = re.compile(
    rf"\b(?:stops?|stopping|halts?|halting|pauses?|pausing|ceases?) {_TRAVEL}\b"
    r"|\b(?:stride|strides|steps|gait|footsteps|walk)\b(?:\s+[\w-]+){0,2}?\s+(?:slows?|slowing|shortens?|shortening)\b"
    r"|\bslows? (?:his|her|their|its) (?:pace|stride|steps|walk|gait)\b"
    r"|\b(?:he|she|they|it|the (?:man|woman|figure|walker|runner|rider|driver|cyclist|person|girl|boy|child|car|vehicle|horse"
    r"|dog|cat|deer|fox|wolf|bird|animal|boat|ship|train|bus|truck|bike|bicycle|motorcycle|scooter|drone|aircraft|plane))\b"
    r"(?:\s+[\w-]+){0,2}?\s+(?:stops|halts|pauses|comes to (?:a )?(?:stop|halt|standstill|rest)"
    r"|slows (?:down|to (?:a )?(?:walk|halt|stop|standstill)))\b", re.I)
TRAVEL_CHANGE_ASKED_RE = re.compile(r"멈추|멈춰|멈춘|멈춤|멈칫|멈출|멈췄|정지|서다|선다|섰다|세우|세운|속도를 줄|느려|늦추|출발|시작"
                                    r"|발을 떼|일어나|\b(?:stop|stops|halt|start|starts|begin|begins|pause)\b", re.I)


def travel_problems(prompt: str, said: str, mode: str) -> list[str]:
    """이동(걷기·주행)이 클립 중간에 시작하거나 느려지거나 멈추면 위반이다.

    시작은 T2V 만 본다: I2V 는 첫 화면이 서 있는 사람일 수 있어 걷기 시작하는 것이 자연스럽다.
    """
    if TRAVEL_CHANGE_ASKED_RE.search(said or ""):
        return []
    text = re.sub(r"\s+", " ", prompt or "")
    start = TRAVEL_START_RE.search(text) if mode == "t2v" else None
    stop = TRAVEL_STOP_RE.search(text)
    if not (start or stop):
        return []
    need = []
    if start:
        need.append("write the subject already walking, running or driving at a steady pace in the opening moment")
    if stop:
        need.append("keep it moving at the same pace in the final frame and let the calm ending come from the camera settling")
    found = "; ".join(m.group(0) for m in (start, stop) if m)
    return [f'Travel keeps one steady pace for the whole clip ("{found}"): ' + ", and ".join(need) + "."]


# ── 구체성: 카메라 항목표, 깊이별 날씨, 걸음의 물리 묘사 (11-16: 러다이트 1번 글 수준) ──
# 러다이트 1번은 카메라 높이·이동 방식·속도·거리·인물 크기·수평선·시차를 모두 적고, 무엇을 유지하는지까지 정했다.
# 우리 3번은 '네온·비·사이버펑크' 같은 분위기 말이 많고 카메라와 걸음은 한 줄이었다. 짧은 보통 프로필은 단락이 짧아 보지 않는다.
CAMERA_HEIGHT_RE = re.compile(
    r"\b(?:eye[- ]?level|eye[- ]?height|eye position|eye line|(?:chest|waist|hip|knee|shoulder|table|counter|head|sill|street|ground)"
    r"[- ]?(?:level|height)|low[- ]angle|high[- ]angle|overhead|top[- ]down|bird['’]?s[- ]eye|worm['’]?s[- ]eye|dutch angle"
    r"|(?:pilot|driver|rider|viewer)['’]?s eye)\b", re.I)
CAMERA_KEEP_RE = re.compile(
    r"\b(?:keep(?:s|ing)?|maintain(?:s|ing)?|hold(?:s|ing)?|stay(?:s|ing)?|remain(?:s|ing)?)\b[^.;]{0,70}"
    r"\b(?:height|horizon|distance|framing|size|level|angle|position|pace)\b", re.I)


def camera_sheet_problems(result: dict) -> list[str]:
    """camera_light 단락이 카메라 높이·각도를 밝히고(shot 단락에 있어도 된다) 카메라가 무엇을 유지하는지 말해야 한다."""
    cam = str(result.get("camera_light") or "")
    if not cam.strip():
        return []   # 빈 단락은 길이 검사가 알린다
    out = []
    if not CAMERA_HEIGHT_RE.search(cam) and not CAMERA_HEIGHT_RE.search(str(result.get("shot") or "")):
        out.append('"camera_light" must name the camera height or angle (for example eye-level, low-angle, table height).')
    if not CAMERA_KEEP_RE.search(cam):
        out.append('"camera_light" must say what the camera keeps constant, such as its distance to the subject, the subject\'s size '
                   "in frame, a level horizon or its framing, in positive words.")
    return out


# 비·눈은 깊이별로 나뉘어 다른 것들과 이어져야 한다 (3번: 'Heavy rain falls in visible streaks' 한 줄)
PRECIP_RE = re.compile(r"\b(?:rain(?:y|fall|drops?|ing)?|drizzle|downpour|snow(?:fall|flakes?|ing)?|sleet|hail|fog(?:gy)?)\b", re.I)
_DEPTH_NEAR = re.compile(r"\b(?:near|nearer|nearest|close|closer|foreground|front)\b", re.I)
_DEPTH_FAR = re.compile(r"\b(?:far|farther|further|distant|distance|background|haze|hazy)\b", re.I)
_DEPTH_MID = re.compile(r"\b(?:mid|middle|midground|midway)\b", re.I)
_DEPTH_WORD = re.compile(r"\b(?:(?:different|varying|several|multiple|separate|distinct|many|three|two)(?: \w+)? (?:depths?|layers?)|at (?:\w+ )?depths?"
                         r"|(?:layers?|layered) of (?:rain|snow|drizzle|mist)|depth layers?)\b", re.I)


def weather_depth_problems(prompt: str) -> list[str]:
    """비·눈이 나오는 글은 어느 한 문장에서 깊이별(가까이·중간·멀리, 또는 '다른 깊이')로 나눠 써야 한다."""
    # 세미콜론에서 자르지 않는다: 'Rain falls in distinct layers: near the lens, …; in the middle, …; and far away, …' 는 한 문장이다
    # (메인 I2V 시험 11-16: 모델이 세 층으로 정확히 썼는데 앞 토막만 보고 '배경 효과' 로 걸렸다)
    sents = re.split(r"(?<=[.!?])\s+", prompt or "")
    hit = [s for s in sents if PRECIP_RE.search(s)]
    if not hit:
        return []
    for s in hit:
        layers = sum(bool(rx.search(s)) for rx in (_DEPTH_NEAR, _DEPTH_MID, _DEPTH_FAR))
        if layers >= 2 or _DEPTH_WORD.search(s):
            return []
    return ["The weather is a flat background effect. Layer it by depth in one sentence (near the lens, in the middle, far away), "
            "say how each layer is seen (streaks against dark surfaces, droplets stippling the ground, haze softening distant "
            "shapes) and connect it to the surfaces, reflections and light sources around it."]


# 걷는 사람은 발의 착지·체중 이동·다음 발의 전진·팔과 몸통으로 나눠 쓴다 ('walks' 한 단어로 끝내지 않는다)
WALKER_RE = re.compile(r"\b(?:man|woman|person|figure|walker|runner|girl|boy|child|adult|pedestrian|he|she|they)\b"
                       r"(?:\s+[\w'’,-]+){0,6}?\s+(?:walk|strid|stroll|jog|run|ran|march|trudg|wander)\w*", re.I)
_GAIT_BUCKETS = (re.compile(r"\b(?:foot|feet|shoes?|heels?|toes?|boots?|soles?)\b", re.I),
                 re.compile(r"\b(?:weight|balance)\b", re.I),
                 re.compile(r"\b(?:plants?|planted|planting|lands?|landing|releases?|swings?|swinging|advances?|advancing)\b", re.I),
                 re.compile(r"\b(?:arms?|torso|shoulders?|hips?|knees?|legs?)\b", re.I))


CARRIED_RE = re.compile(r"\b(?:umbrella|parasol|handbag|bag|briefcase|suitcase|backpack|basket|bouquet|cane|walking stick|box|cup|mug"
                        r"|book|phone|lantern|balloons?|flag|leash|bucket|tray)\b", re.I)
HAND_GRIP_RE = re.compile(r"\b(?:hands?|fingers?|grips?|gripping|grasp\w*|handle|strap|fist|palm|wrist)\b", re.I)


def gait_problems(prompt: str, said: str = "") -> list[str]:
    """걷는·달리는 사람이 나오면 발·체중·착지와 전진·팔과 몸통 중 세 가지 이상의 물리 묘사가 있어야 한다. 1인칭 걷기는 보지 않는다.
    우산·가방처럼 들고 걷는 것이 있으면 어느 손이 어떻게 쥐는지도 써야 한다 (손 숨김 요청은 손 말을 쓰지 않으니 보지 않는다)."""
    if pov_wanted(said) or not WALKER_RE.search(prompt or ""):
        return []
    out = []
    have = sum(bool(rx.search(prompt)) for rx in _GAIT_BUCKETS)
    if have < 3:
        out.append('Describe the walk physically in "action" instead of just saying the person walks: the foot landing and taking '
                   "the weight, the other leg swinging forward so the next foot advances, the arms counter-swinging and the torso "
                   "rising and falling slightly, with the clothing following.")
    held = CARRIED_RE.search(prompt)
    if held and not hides_hands(said) and not HAND_GRIP_RE.search(prompt):
        item = held.group(0).lower()
        out.append(f'The person carries {"an" if item[0] in "aeiou" else "a"} {item}: say which hand holds it and how (for example one hand keeps a '
                   "relaxed, steady grip on the handle) and what the other arm does while walking.")
    return out


# ── 11-17 러다이트와 비교(비 오는 저녁 우산 쓴 남자)에서 나온 검사: 이동 방향, 고정 카메라의 크기, 감정을 몸으로 ──
DIRECTION_RE = re.compile(
    r"\b(?:left[- ]to[- ]right|right[- ]to[- ]left|(?:from|toward|towards|to|off|into) the (?:left|right)(?: edge| side)?"
    r"|(?:head(?:s|ing)?|walk(?:s|ing)?|mov(?:es|ing)|travel(?:s|ing)?|go(?:es|ing)?|run(?:s|ning)?|jog(?:s|ging)?|drive(?:s|ing)?) (?:to the )?(?:left|right)\b"
    r"|(?:toward|towards|at|approach(?:es|ing)?|coming to|up to|closer to|nearer to) (?:the )?(?:camera|lens|viewer|us)\b"
    r"|away from (?:the )?(?:camera|lens|viewer|us)\b|(?:into|toward|towards) the (?:distance|horizon|background)\b"
    r"|(?:walk|strid|stroll|jog|run|ran|march|trudg|wander|drive|ride|cycl|skat|sprint|glid|fly|swim)\w* (?:\w+ ){0,2}?away\b"
    r"|across (?:the )?(?:frame|shot|screen|street|road)\b|from the (?:distance|background|horizon|foreground|side)\b"
    r"|into (?:the )?(?:frame|shot|camera view)\b|(?:off|out of) (?:the )?(?:frame|screen|shot)\b)", re.I)
# 옆으로 가로지르는 이동. 'right hand'·'light from the left' 의 left·right 는 이동 방향이 아니다
LATERAL_RE = re.compile(
    r"\b(?:left[- ]to[- ]right|right[- ]to[- ]left|(?:from|toward|towards|to|off|into) the (?:left|right) (?:edge|side)\b"
    r"|across (?:the )?(?:frame|shot|screen)\b|sideways|laterally|(?:walk|strid|stroll|jog|run|march|drive|ride|glid)\w* (?:to the )?(?:left|right)\b)", re.I)


def _travels(text: str) -> bool:
    return bool(WALKER_RE.search(text) or ANIMAL_MOVER_RE.search(text) or VEHICLE_MOVER_RE.search(text))


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or ""))


def direction_problems(prompt: str, said: str = "") -> list[str]:
    """걷는·달리는 사람·동물이나 달리는 탈것이 나오면 화면에서 어느 쪽으로 가는지 써야 한다 (러다이트: 오른쪽에서 왼쪽, 앞쪽 여백).
    이동하는 대상이 나온 문장에 방향이 있어야 한다 (조명의 'from the left' 는 방향이 아니다). 1인칭 요청은 보지 않는다."""
    if pov_wanted(said):
        return []
    moving = [sent for sent in _sentences(prompt) if _travels(sent)]
    if not moving or any(DIRECTION_RE.search(sent) for sent in moving):
        return []
    return ['Name which way the moving subject travels relative to the frame (left to right, right to left, toward the camera or '
            'away from the camera) in the sentence where it moves; a subject that travels sideways sits slightly off-center '
            "with open space ahead of it."]


STILL_SIZE_CLAIM_RE = re.compile(
    r"\b(?:size|scale)\b(?:\s+[\w'’-]+){0,4}?\s+in (?:the )?(?:frame|shot|picture|view)\b[^.;]{0,40}\b(?:stays?|remains?|is|kept|held|holds?)\b"
    r"[^.;]{0,16}\b(?:same|constant|unchanged|fixed|steady|unchanging)\b"
    r"|\b(?:same|constant|unchanged|fixed|steady|unchanging) (?:apparent |on-screen )?(?:size|scale) in (?:the )?(?:frame|shot|picture|view)\b"
    r"|\bunchanged in (?:size|scale)\b"
    r"|\b(?:keep(?:s|ing)|hold(?:s|ing)|maintain(?:s|ing)|stays?|remains?)\s+(?:the |his |her |their |its )?(?:[\w'’-]+['’]s )?"
    r"(?:size|scale|distance)(?:\s+(?:and|or)\s+(?:position|framing|scale|size))?\s+(?:constant|the same|unchanged|steady|fixed)\b", re.I)


TIME_RANGE_RE = re.compile(r"\b(?:from )?(\d+)(?::\d\d)?\s*(?:to|[-–—])\s*(\d+)(?::\d\d)?\s*(?:s\b|secs?\b|seconds?\b)", re.I)


def timeline_problems(result: dict, seconds, profile: dict) -> list[str]:
    """시간 구간을 쓰는 프로필(LTX)은 action 단락에 정해 준 세 구간('From 0 to 3 seconds')이 모두 있어야 한다."""
    r = timeline_ranges(seconds) if profile.get("timeline") else None
    if not r:
        return []
    action = str((result or {}).get("action") or "")
    found = {(int(a), int(b)) for a, b in TIME_RANGE_RE.findall(action)}
    miss = [(a, b) for a, b, _ in r if (a, b) not in found]
    if not miss:
        return []
    want = ", ".join(f'"From {a} to {b} seconds, ..."' for a, b, _ in r)
    return [f'Write the "action" paragraph as three timed beats that open with exactly these ranges: {want}.']


TRAVELER_REF_RE = re.compile(r"\b(?:his|her|their|its|he|she|they|the (?:subject|figure|man|woman|walker|runner|rider|driver|dog|horse|car|vehicle))\b", re.I)


def still_size_problems(prompt: str) -> list[str]:
    """고정 카메라(locked off)에서 다가오거나 멀어지는 대상은 화면에서 커지거나 작아진다. 크기·거리가 그대로라고 쓰면 위반이다.
    옆으로 가로지르는 이동(left·right·across)이면 거리가 같으니 보지 않는다."""
    text = re.sub(r"\s+", " ", prompt or "")
    if not CAMERA_STILL_RE.search(text) or not _travels(text) or LATERAL_RE.search(text):
        return []
    m = None
    for sent in _sentences(text):
        found = STILL_SIZE_CLAIM_RE.search(sent)
        if found and TRAVELER_REF_RE.search(sent):      # 가만히 선 물체(병·탑)의 크기가 그대로인 것은 맞는 말이다
            m = found
            break
    if not m:
        return []
    return [f'With a locked-off camera, a subject that walks toward or away from it changes size in the frame, yet the text says '
            f'"{m.group(0)}". Write that the figure grows larger (or smaller) as it moves instead; keep "same distance" or '
            "\"same size\" only for sideways travel across the frame."]


EMOTION_RE = re.compile(
    r"\b(?:dejected|dejection|despondent|sad(?:ness)?|sorrow\w*|melanchol\w+|lonely|loneliness|despair\w*|depress\w+|joy(?:ful|ous)?"
    r"|happ(?:y|iness)|angry|anger|anxious|anxiety|nervous\w*|excite(?:d|ment)|weary|exhausted|defeated|hopeless\w*)\b", re.I)
BODY_SHOWN_RE = re.compile(
    r"\b(?:head|shoulders?|chest|spine|posture|steps?|stride|gait|gaze|eyes?|brows?|face|jaw|lips?|mouth|hands?|arms?|fingers?"
    r"|knees?|slump\w*|hunch\w*|bowed|droop\w*|sag\w*|lowered|lifted|tilts?|trembl\w*|breath\w*|sigh\w*|express\w*|smil\w*|frown\w*|tears?)\b", re.I)
PERSON_RE = re.compile(r"\b(?:man|woman|person|figure|girl|boy|child|adult|walker|runner|he|she|his|her|him)\b", re.I)


EMOTION_PLACE_RE = re.compile(
    EMOTION_RE.pattern + r"(?:\s+[\w'’-]+){0,2}?\s+(?:street|road|plaza|square|room|house|hall|city|town|village|beach|shore|sky|"
    r"lights?|glow|atmosphere|evening|night|morning|afternoon|day|scene|space|corridor|alley|garden|field|forest|lake|sea|ocean|air)\b",
    re.I)


def emotion_body_problems(prompt: str, said: str = "") -> list[str]:
    """사람이 나오는 글에서 감정 낱말(dejected, sad …)이 몸·시선 말 없이 혼자 쓰인 문장은 위반이다. 몸으로 보여 줘야 한다.
    장소·빛을 꾸미는 말('lonely street')과 따옴표 안의 말은 보지 않는다. 1인칭 요청도 보지 않는다."""
    text = re.sub(r"\s+", " ", prompt or "")
    if pov_wanted(said) or not PERSON_RE.search(text):
        return []
    for sent in re.split(r"(?<=[.!?])\s+", QUOTED_OUT_RE.sub("", text)):
        sent_mood = EMOTION_RE.sub(lambda m: m.group(0) if not EMOTION_PLACE_RE.match(sent, m.start()) else "", sent)
        if EMOTION_RE.search(sent_mood) and PERSON_RE.search(sent) and not BODY_SHOWN_RE.search(sent):   # 사람 이야기인 문장만 (gloomy atmosphere 같은 빛 묘사는 아니다)
            return ['Show the mood through the body and what the camera sees (head, shoulders, steps, gaze, hands), not with a '
                    f'mood word alone: "{sent[:140]}"']
    return []


# 사람이 아닌 대상도 같다: 동물은 발·날개와 체중, 몸의 따름을, 탈것은 바퀴·선체와 차체의 반응, 뒤에 남는 물보라·먼지를 쓴다.
# 정규식으로 알 수 있는 흔한 대상(사람·동물·탈것)만 검사한다. 액체·천·불꽃·기계 같은 나머지는 지시문 규칙이 맡는다
_ANIMAL = (r"(?:dog|puppy|cat|kitten|horse|pony|deer|stag|fox|wolf|bear|lion|tiger|leopard|cheetah|rabbit|hare|elephant|cow|sheep"
           r"|goat|bird|eagle|hawk|owl|crow|gull|duck|swan|goose|butterfly|bee|dragonfly|fish|whale|dolphin|shark|turtle|lizard"
           r"|snake|animal|squirrel|mouse|rat|hamster|hedgehog|raccoon|otter|badger|panda|monkey|pig|chicken|hen|frog|toad|penguin"
           r"|parrot|dragon|unicorn|bunny|lamb|donkey|creature|beast|chick|crab|octopus|seal|camel|giraffe|zebra|koala)(?:s|es)?"
           r"|puppies|ponies|butterflies|dragonflies|mice")
_MOVE = (r"(?:walk|trot|gallop|run|sprint|prowl|lope|bound|leap|jump|hop|soar|glide|swim|dart|crawl|slither|trudg|pad|"
         r"stride|fl(?:y|ies|ew|ying))(?:s|es|ing|ed)?|ran|flies|flying|flew")
ANIMAL_MOVER_RE = re.compile(rf"\b(?:{_ANIMAL})\b(?:\s+[\w'’,-]+){{0,6}}?\s+(?:{_MOVE})\b", re.I)
# 설정집 외형 글이 동물인가: 첫 몇 낱말에 동물 이름이 있다 ('a small white puppy with…'). 사람 외형 뒤쪽의 소품('a cat-shaped bag')은
# 잡지 않는다. 그림 검사(studio/checks.py)가 동물을 사람 수에 넣지 않으려고 쓴다 (11-20 검토)
ANIMAL_LOOK_RE = re.compile(rf"^\W*(?:[\w'’-]+\W+){{0,5}}?(?:{_ANIMAL})\b", re.I)


def is_animal_look(look: str) -> bool:
    return bool(ANIMAL_LOOK_RE.search(look or ""))
_ANIMAL_BUCKETS = (
    re.compile(r"\b(?:paws?|hoofs?|hooves|legs?|wings?|fins?|claws?|talons?|feet|flippers?|forelegs?|hindlegs?)\b", re.I),
    re.compile(r"\b(?:weight|balance|momentum|thrust|pushes? off)\b", re.I),
    re.compile(r"\b(?:plants?|planted|lands?|lifts?|swings?|extends?|flaps?|beats?|strokes?|sweeps?|reach(?:es)?|folds?|unfolds?"
               r"|kicks?|undulat\w+)\b", re.I),
    re.compile(r"\b(?:spine|tail|head|neck|ears?|mane|body|shoulders?|flanks?|ribs?|feathers?|fur|coat|torso|hips?|whiskers?)\b", re.I))
_VEHICLE = (r"(?:car|truck|van|bus|taxi|train|tram|motorcycle|motorbike|bicycle|bike|scooter|boat|ship|sailboat|yacht|canoe|kayak"
            r"|ferry|aircraft|airplane|plane|jet|helicopter|drone|rover|tractor|sled|carriage|cart)(?:s|es)?")
_VMOVE = (r"(?:drive|drives|driving|ride|rides|riding|race|races|racing|speed|speeds|speeding|cruise|cruises|cruising|roll|rolls"
          r"|rolling|sail|sails|sailing|glide|glides|gliding|fly|flies|flying|pedal|pedals|pedaling|row|rows|rowing|paddle"
          r"|paddles|paddling|travel|travels|traveling|zoom|zooms|zooming|moves|moving|passes|passing)")
VEHICLE_MOVER_RE = re.compile(rf"\b{_VEHICLE}\b(?:\s+[\w'’,-]+){{0,6}}?\s+{_VMOVE}\b", re.I)
_VEHICLE_BUCKETS = (
    re.compile(r"\b(?:wheels?|tyres?|tires?|rotors?|propellers?|props?|hull|bow|keel|sails?|oars?|paddles?|pedals?|handlebars?"
               r"|engine|exhaust|wings?|blades?|runners?)\b", re.I),
    re.compile(r"\b(?:suspension|chassis|body|frame|lean(?:s|ing)?|banks?|banking|pitch(?:es)?|sways?|rocks?|tilts?|bounces?"
               r"|settles?|dips?|surges?|heel(?:s|ing)?)\b", re.I),
    re.compile(r"\b(?:spray|mist|dust|wake|foam|ripples?|smoke|vapou?r|trail|reflections?|skid|puddles?|splash(?:es)?"
               r"|turbulence|sparks?|gravel|waves?)\b", re.I))


def mover_problems(prompt: str, said: str = "") -> list[str]:
    """걷는 사람 말고도 움직이는 동물·탈것은 움직이는 부분과 몸·차체의 반응을 나눠 써야 한다. 1인칭 요청은 보지 않는다.

    동물은 발·날개, 체중, 착지·들기·휘젓기 같은 단계, 몸(척추·꼬리·머리)의 따름 중 세 가지 이상.
    탈것은 바퀴·선체·날개, 차체의 반응, 뒤에 남는 물보라·먼지·물결 중 두 가지 이상.
    """
    if pov_wanted(said):
        return []
    out = []
    if ANIMAL_MOVER_RE.search(prompt or "") and sum(bool(rx.search(prompt)) for rx in _ANIMAL_BUCKETS) < 3:
        out.append('Describe the animal\'s motion physically in "action" instead of just saying it moves: the paw, hoof or wing that '
                   "plants or beats and takes the weight, how the leg or wing swings through to the next stride or beat, and how the "
                   "spine, shoulders, head and tail answer it.")
    if VEHICLE_MOVER_RE.search(prompt or "") and sum(bool(rx.search(prompt)) for rx in _VEHICLE_BUCKETS) < 2:
        out.append('Describe the vehicle\'s motion physically in "action" instead of just saying it moves: the wheels, rotors or hull '
                   "doing the work, how the body settles or leans on its suspension or in the air, and what trails behind it "
                   "(spray, dust, wake, ripples).")
    return out


# 비·눈·안개는 깊이별로 나눠 쓰고(weather_depth_problems), 김·연기·먼지·불똥·꽃잎·물보라 같은 주변 입자는 빛이나 표면과 이어서 쓴다.
# '미세한 먼지가 떠다닌다' 만 쓰면 배경 효과일 뿐이다 (1번 글: 'Rain moves at different apparent depths, visible against shadowed building surfaces')
AMBIENT_RE = re.compile(r"\b(?:mist|steam|smoke|dust|embers?|sparks?|petals?|pollen|fireflies|spray|bubbles?|ash|vapou?r)\b", re.I)
AMBIENT_LINK_RE = re.compile(
    r"\b(?:light|lights|lit|beam|glow(?:s|ing)?|backlight|highlights?|reflect\w*|shadows?|surfaces?|glass|water|ground|pavement|wall|"
    r"walls|facades?|windows?|sky|sun|sunlight|lamp|rim|edge|edges|skin|fabric|leaf|leaves|branches|table|floor|ceiling|roof|"
    r"buildings?|trees?|hills?|peaks?|rim|cup|bottle|lid|rim|stone|wood|metal|steel|bark|horizon|distance|distant)\b", re.I)


def ambient_link_problems(prompt: str) -> list[str]:
    """주변 입자(김·연기·먼지·불똥·꽃잎·물보라)가 나오면 어느 한 문장에서 빛이나 표면과 이어져야 한다."""
    sents = [s for s in re.split(r"(?<=[.!?])\s+", prompt or "") if AMBIENT_RE.search(s)]
    if not sents or any(AMBIENT_LINK_RE.search(s) for s in sents):
        return []
    return ["The atmosphere (mist, steam, smoke, dust, sparks, petals, spray) floats unconnected. Tie it to the light and the surfaces "
            "in the same sentence: where it catches the light, what it drifts past, what it settles on."]


def example_copy_problems(result: dict, said: str, scene: str) -> list[str]:
    """T2V 장문은 길이 예시(찻잔 장면)를 받는다. 모델이 그것을 베껴 요청과 상관없는 찻잔·김·창가가 영상에 나온다
    (11-16 파일럿 1인칭 요청: 앞 두 단락은 조종석, 뒤 두 단락은 찻잔). 예시 낱말이나 문장이 요청·장면에 없는데 나오면 알린다"""
    spec = long_spec()
    ex = spec.get("example") or {}
    ex_words = set(spec.get("example_words") or [])
    allowed = set(_alpha_words(scene)) | set(_alpha_words(said))
    ex_grams = set()
    for part in ex.values():
        w = _alpha_words(part)
        ex_grams |= {tuple(w[i:i + 12]) for i in range(len(w) - 11)}
    problems = []
    for p in spec["parts"]:
        text = str(result.get(p["key"]) or "")
        w = _alpha_words(text)
        # 'teacup'·'windowsill' 처럼 합성어로 장면에 있으면 'tea'·'cup'·'sill' 도 허용한다
        leaked = sorted(x for x in set(w) & ex_words if not any(x in a for a in allowed))
        copied = any(tuple(w[i:i + 12]) in ex_grams for i in range(len(w) - 11))
        if leaked or copied:
            what = ", ".join(leaked[:6]) if leaked else "whole sentences"
            problems.append(f'"{p["key"]}" copies the EXAMPLE ({what}). The EXAMPLE is a different request: rewrite "{p["key"]}" '
                            "about the user's own subject, objects and setting only, and never reuse its subject, objects, "
                            "setting or sentences.")
    return problems


def part_consistency_problems(result: dict) -> list[str]:
    """네 단락은 한 장면이다. action·ending 이 shot 에서 말한 피사체·배경과 겹치는 낱말이 거의 없으면 다른 장면을 쓴 것이다"""
    def content(key):
        return {x for x in _alpha_words(result.get(key)) if len(x) >= 5 and x not in STOP_WORDS}
    shot = content("shot")
    if len(shot) < 6:
        return []
    problems = []
    for key in ("action", "ending"):
        part = content(key)
        # 정상 글은 shot 의 피사체·배경 낱말을 1개 이상 다시 쓴다(짧은 보통 프로필 포함). 0개면 다른 장면이다.
        # 잘못 나온 11-16 파일럿 글은 action 이 shot 과 겹치는 낱말이 모두 꾸밈말(bright·glowing)이었다
        if part and not ((part & shot) - DECOR_WORDS):
            main = ", ".join(sorted(shot, key=lambda x: -len(x))[:6])
            problems.append(f'"{key}" describes a different scene from "shot": it must move and end the subject, objects and '
                            f'setting that "shot" names (for example: {main}).')
    return problems


def check_video(result, profile: dict, said: str, frame: dict | None = None, scene: str = "",
                cat: str | None = None, menus: dict | None = None, seconds=None, plan: dict | None = None) -> list[str]:
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
        need = part_min(profile)
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
        msg = f"The prompt has {n} words; write {lo} to {hi} words."
        if long:
            # 모델은 낱말 수를 잘 세지 못해 '줄여라' 만으로는 못 줄인다. 단락마다 지금과 목표를 알려 준다
            aim = aim_words(profile)
            now = ", ".join(f'"{p["key"]}" {len(str(result.get(p["key"]) or "").split())}' for p in long_spec()["parts"])
            msg += (f" {'Cut' if n > hi else 'Expand'} it to about {aim} words in total, about {aim // 4} words per part "
                    f"(now: {now}); keep every part's key details and drop only repetition and filler.")
        problems.append(msg)
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
        if profile["mode"] == "t2v" and not profile.get("mid"):
            problems += example_copy_problems(result, said, scene)
        problems += part_consistency_problems(result)
        problems += extra_subject_problems(prompt, said)
        problems += hand_focus_problems(result, said)
        problems += blocking_problems(prompt)
        problems += lateral_camera_problems(prompt, said)
        problems += travel_problems(prompt, said, profile["mode"])
        if not profile.get("mid"):   # 보통 프로필은 단락이 짧아 항목표·깊이·걸음 묘사까지 요구하지 않는다
            problems += camera_sheet_problems(result)
            problems += weather_depth_problems(prompt)
            problems += gait_problems(prompt, said)
            problems += timeline_problems(result, seconds, profile)
            problems += plan_problems(result, plan)
            problems += direction_problems(prompt, said)
            problems += still_size_problems(prompt)
            problems += emotion_body_problems(prompt, said)
            problems += mover_problems(prompt, said)
            problems += ambient_link_problems(prompt)
        if operating_pov(said) and not (HAND_WORDS_RE.search(prompt) and re.search(r"\b(?:hands?|forearms?|gloves?)\b", prompt, re.I)):
            problems.append("The user asked for a first-person view of operating a vehicle: show the viewer's own hands or "
                            "forearms on the real controls (yoke, wheel, handlebars) at the bottom edge of the frame, and "
                            "describe their small movements in \"action\".")
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
    (re.compile(r"^Camera and subject directions disagree"),
     lambda m: "카메라와 인물의 방향이 서로 맞지 않습니다(예: 인물은 멀어지는데 카메라가 뒤로 물러나며 같은 거리를 유지). "
               "영상 모델이 어느 쪽을 따를지 몰라 뒷걸음질 같은 결과가 나올 수 있습니다."),
    (re.compile(r"^Travel keeps one steady pace"),
     lambda m: "걷기·주행이 클립 중간에 시작하거나 느려지다 멈춥니다. 처음부터 끝까지 같은 속도로 움직이게 써야 합니다."),
    (re.compile(r'^"camera_light" must name the camera height'),
     lambda m: "카메라 높이·각도(예: 눈높이)가 글에 없습니다. 카메라를 항목표처럼 구체적으로 써야 합니다."),
    (re.compile(r'^"camera_light" must say what the camera keeps constant'),
     lambda m: "카메라가 무엇을 유지하는지(인물과의 거리·인물 크기·수평선·구도)가 없습니다. 움직임뿐 아니라 유지할 것까지 정해야 합니다."),
    (re.compile(r"^The weather is a flat background effect"),
     lambda m: "비·눈이 배경 효과로만 쓰였습니다. 가까이·중간·멀리 깊이별로 나눠 쓰고 건물·바닥 반사·빛과 이어서 묘사해야 합니다."),
    (re.compile(r"^Describe the walk physically"),
     lambda m: "걷는다고만 쓰였습니다. 발의 착지, 체중 이동, 다음 발의 전진, 팔과 몸통의 움직임으로 나눠 써야 합니다."),
    (re.compile(r"^Describe the animal's motion physically"),
     lambda m: "동물이 움직인다고만 쓰였습니다. 발·날개가 딛고 치는 순간, 체중, 다음 걸음·날갯짓, 척추·꼬리·머리의 따름으로 나눠 써야 합니다."),
    (re.compile(r"^Describe the vehicle's motion physically"),
     lambda m: "탈것이 움직인다고만 쓰였습니다. 바퀴·선체의 움직임, 차체의 기울어짐·출렁임, 뒤에 남는 물보라·먼지·물결로 나눠 써야 합니다."),
    (re.compile(r"^The atmosphere \(mist"),
     lambda m: "김·연기·먼지 같은 입자가 빛이나 표면과 이어지지 않고 떠 있습니다. 빛을 받는 곳, 지나가는 곳, 내려앉는 곳을 함께 써야 합니다."),
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
                rng=None, style: str = "", start: tuple[str, list[str]] | None = None, staging: str | None = None,
                plan: dict | None = None):
    """한 GPU 차례(llm_turn) 안에서 움직임 초안을 받는다. (결과, 회차, 남은 위반) 을 돌려준다.

    start=(앞서 쓴 답의 JSON 글, 그 위반 목록) 이면 처음 쓰기 없이 그 답을 고쳐 쓰는 것부터 시작한다(자동 수정)."""
    rounds_max = settings.video_repair_rounds if repair_rounds is None else repair_rounds
    said = f"{text} {notes}".strip()
    long = is_long(profile)
    messages = await asyncio.to_thread(build_messages, text, notes, profile, seconds, frame, scene, cat, menus, rng,
                                       style, staging, plan)
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
            problems = check_video(result, profile, said, frame, scene, cat, menus, seconds, plan)
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
                 crew=None, read_frame_on: bool = True, rng=None, auto_length: bool = False,
                 frame: dict | None = None, staging: str | None = None, plan: dict | None = None,
                 music: bool | None = None, direction: str = "", safe: bool = False) -> dict:
    """요청 → (첫 화면 읽기) → (T2V 장면) → 움직임 초안 → 최종 영상 프롬프트와 설정 제안.

    frame 은 이미 읽은 첫 화면 결과다. 주면 첫 화면을 다시 읽지 않는다(채팅의 영상 고치기). 패널은 넘기지 않는다.
    music=False 면 소리 단락에 음악을 쓰지 않는다 (긴 영상의 장면: 배경음악은 합칠 때 한 곡으로 깐다).

    auto_length 이면 장문 프로필을 받아도 요청에 맞춰 장문·보통 중 하나로 정한다(decide_length). 사용자가 고르지 않는다."""
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
    frame_in = parse_frame(frame) if isinstance(frame, dict) else None
    if frame_in is not None:
        pass      # 읽어 둔 결과를 쓴다
    elif mode == "i2v" and isinstance(image, dict) and image.get("filename") and read_frame_on:
        try:   # 첫 화면을 못 찾거나 못 열어도 글만 보고 계속 쓴다
            b64 = await asyncio.to_thread(frame_b64, frame_file(username, slug, image))
        except (PromptxError, OSError) as e:
            advice.append(f"첫 화면을 읽지 못해 요청 글만 보고 움직임을 썼습니다 ({e}).")
    elif mode == "i2v" and not (isinstance(image, dict) and image.get("filename")):
        advice.append("첫 화면 이미지를 찾지 못해 요청 글만 보고 움직임을 썼습니다. LoadImage 노드가 있으면 첫 화면을 읽습니다.")
    # 영상 메뉴(video_knowledge)에는 정본 장면 분야가 없다: 라이브러리 인물 클립도 영상 제작진 단계는 지금처럼 건너뛴다 (11-26)
    rt, crew_advice = promptx.crew_route(text, notes, crew, cast=False)
    advice += [a.replace("제작진 모드를", "영상 제작진 단계를") for a in crew_advice]
    cat = rt["category"] if rt else None
    model, frame_model = settings.expand_model, settings.video_frame_model
    frame, scene_info, crew_info, menus = frame_in, None, None, None
    length = None
    sound_info = None
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
            if auto_length and is_long(profile) and not profile.get("mid") and profile.get("mid_id"):
                length = await decide_length(call, text, notes, mode, frame, scene)
                if length["tier"] == "mid":
                    pid, profile = profile_of(profile["mid_id"])
            if mode == "t2v" and not scene:
                # T2V 는 첫 화면이 없으므로 같은 차례 안에서 이미지 확장(제작진·도우미)으로 장면부터 쓴다
                scene_info = await promptx.expand_in_turn(call, text, style, notes, crew)
                scene = scene_info["body"]
                advice += [a for a in scene_info.get("advice") or [] if a not in advice]
            if plan is None and is_long(profile):
                # 감독이 미리 짠 계획표가 없으면(패널·사진으로 만드는 영상) 같은 차례 안에서 지금 짠다. 실패하면 계획표 없이 쓴다
                plan = await write_plan(call, text, notes, profile, suggested["seconds"], frame, scene, staging, direction, safe)
            if cat:
                menus = await asyncio.to_thread(build_menus, said, cat, rng)
                result, rounds, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene,
                                                   cat, menus, rng=rng, style=style, staging=staging, plan=plan)
                crew_info = {"category": cat, "title": load_knowledge()["categories"][cat]["title"], "rounds": rounds,
                             "used": not left, "violations": left}
                if left:
                    advice.append("영상 제작진 초안이 규칙을 통과하지 못해 단계 메뉴 없이 다시 썼습니다.")
                else:
                    crew_info.update(describe(result, cat))
            if not cat or left:
                result, more, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene, rng=rng,
                                                 style=style, staging=staging, plan=plan)
                rounds += more
            if profile.get("sound") and str(result.get("prompt") or "").strip():
                sound_info = await write_sound(call, text, notes, str(result["prompt"]), suggested["seconds"], profile,
                                               music=music)
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    long = is_long(profile)
    motion = str(result.get("prompt") or "").strip()
    ko = result.get("ko") if isinstance(result.get("ko"), dict) else {}
    parts = {p["key"]: str(result.get(p["key"]) or "").strip() for p in long_spec()["parts"]} if long else None
    sound = (sound_info or {}).get("sound") or ""
    ko_out = {k: str(ko.get(k) or "") for k in KO_KEYS}
    if sound and (sound_info or {}).get("ko"):
        ko_out["sound"] = sound_info["ko"]
    body = compose(mode, scene, motion, style, long)
    return {"prompt": f"{body}\n\n{sound}" if sound and body else body, "sound": sound,
            "sound_left": (sound_info or {}).get("left") or [], "motion": motion, "long": long, "parts": parts,
            "scene": scene if mode == "t2v" else "", "profile": pid, "profile_title": profile["title"], "mode": mode,
            "settings": suggested, "frame": frame, "ko": ko_out,
            "crew": crew_info, "scene_crew": (scene_info or {}).get("crew"),
            "scene_violations": (scene_info or {}).get("violations") or [],
            "length_choice": length, "violations": left, "violations_ko": explain_ko(left), "rounds": rounds, "model": model,
            "plan": plan,
            "frame_model": frame_model if frame else None, "advice": advice}


async def plan(user, ip: str, username: str, slug: str, text: str, notes: str = "", profile_id: str | None = None,
               seconds=None, staging: str | None = None, direction: str = "", safe: bool = False) -> dict | None:
    """연출 계획표 (감독이 장면 이미지를 그리기 전에 한 번). 실패해도 예외 없이 None."""
    from . import llm

    text = clean_input(text)
    notes = str(notes or "").strip()[:2000]
    pid, profile = profile_of(profile_id)
    suggested = suggest_settings(profile, seconds)
    try:
        async with llm.llm_turn(user, ip, settings.expand_model, "video_plan") as call:
            return await write_plan(call, text, notes, profile, suggested["seconds"], None, "", staging, direction, safe)
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None


async def fix(user, ip: str, username: str, slug: str, text: str, notes: str = "", style: str = "",
              profile_id: str | None = None, seconds=None, current=None, frame=None, scene: str = "",
              staging: str | None = None, plan: dict | None = None) -> dict:
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
    before = check_video(result, profile, said, frame, scene, seconds=suggest_settings(profile, seconds)["seconds"], plan=plan)
    after, rounds, left = result, 0, []
    if before:
        model = settings.expand_model
        start = (json.dumps({k: v for k, v in result.items() if k != "prompt"}, ensure_ascii=False), before)
        try:
            async with llm.llm_turn(user, ip, model, "video_fix") as call:
                after, rounds, left = await draft(call, text, notes, profile, suggested["seconds"], frame, scene,
                                                  repair_rounds=2, style=style, start=start, staging=staging, plan=plan)
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
