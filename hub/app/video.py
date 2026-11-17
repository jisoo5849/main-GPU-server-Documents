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
- "air": one sentence on weather, mist, dust, steam or any particles and how they cross the light and touch surfaces (rain crossing pools of light and dripping from an edge; dust turning in a beam). Write an empty string when the scene has none.
- "details": 2 or 3 short phrases for small signature details of the subject or its material that the camera can see (a glistening wet sleeve, a chipped glaze, a scuffed boot).
- "acting": exactly 3 phrases, one per third of the clip: the opening moment, the one main action unfolding (with ONE small living detail that does not change the action, such as a lifted glance, a gust, a ripple), and how the same action carries on at the end. For a person show the mood through the body (head, shoulders, steps, hands), never a mood word alone; for an animal the paws, wings or body; for an object or vehicle how its material, wheels or surfaces respond; for a landscape or weather how its own elements move.
Rules:
- Honour every word of the request first; it is the top priority. Invent only what fits the place and the request, and nothing that contradicts it.
- Write short English phrases, each at most 20 words. Name no brands, songs, artists or real people, and write no text, signs or logos.
- Positive phrasing only: never write what is absent (no "no", "not", "without").
- A person must not be added when the request has none."""
PLAN_KEYS = ("place", "light", "air", "details", "acting")


def _clean_items(v, lo: int, hi: int) -> list[str] | None:
    if not isinstance(v, list):
        return None
    out = [re.sub(r"\s+", " ", str(x or "")).strip(" .;") for x in v]
    out = [x for x in out if x]
    return out[:hi] if len(out) >= lo else None


def clean_plan(data) -> dict | None:
    """모델이 돌려준 계획표를 다듬는다. 모양이 틀리면 None (계획표 없이 기존 방식으로 간다)."""
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
    flat = " ".join(place + acting + details + [light, air])
    if HANGUL_RE.search(flat) or SOUND_NEGATION_RE.search(flat):
        return None
    return plan


async def write_plan(call, text: str, notes: str, profile: dict, seconds: float, frame: dict | None = None,
                     scene: str = "", staging: str | None = None) -> dict | None:
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
    schema = {"type": "object", "required": list(PLAN_KEYS), "properties": {
        "place": {"type": "array", "items": {"type": "string"}}, "light": {"type": "string"}, "air": {"type": "string"},
        "details": {"type": "array", "items": {"type": "string"}}, "acting": {"type": "array", "items": {"type": "string"}}}}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for _ in range(2):
        reply = await call({"messages": messages, "format": schema, "think": False, "options": {"temperature": 0.7}})
        content = (reply.get("message") or {}).get("content", "")
        try:
            plan = clean_plan(json.loads(content))
        except (ValueError, TypeError):
            plan = None
        if plan:
            return plan
        messages = messages + [{"role": "assistant", "content": content or "{}"},
                               {"role": "user", "content": "Rewrite the plan: place 3-4 phrases, one light sentence, the air sentence "
                                "(empty if none), 1-3 details, exactly 3 acting phrases; English only, positive phrasing only."}]
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
            f"- Acting, as {how}: {beats}")


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
- "music": the background music laid under the whole film as one track: "tags" (English: genre, mood, three or four instruments, and the word instrumental), "bpm" (a whole number from 50 to 180), "key" (one of: {keys}), "ko" (one Korean sentence).
- "scenes": about {count} scenes ({n_min} to {n_max}). Each scene: "seconds" (a whole number from 5 to 20; most scenes about 10), "place" (a place id), "characters" (the character ids in the shot), "continues" (true only when the shot follows the previous scene in the same place with no time jump, so its first frame is the previous scene's last frame), "shot" (shot size and one camera move, in English), "action" (the one main action of the shot in English, at most 35 words; show mood through the body), "sound" (the ambient sound and the sounds of the action in English; never music), "ko" (one Korean sentence for the user).
Rules:
- Honour every word of the request first; it is the top priority. Build a clear arc: an opening that sets the place, a development, and a closing image.
- The scene seconds add up to {seconds} (within {tolerance}).
- Vary the shot sizes (wide, medium, close) and keep the screen direction of a moving subject the same from scene to scene.
- A shot that the next scene continues must end with the main subject still fully in frame, because the next scene starts from its last frame: never let the subject walk out of frame, cross to the edge or disappear in such a shot; keep a moving subject in frame with a tracking camera, or let it walk toward or away from the camera.
- Every scene can be shot from one still frame: one place, one main action, no cut inside it, no flashback, and no text, signs or logos.
- Name no brands, songs, artists or real people. English everywhere except "title_ko", "logline_ko" and "ko".
- A person must not be added when the request has none."""
STORY_SCHEMA = {"type": "object", "required": ["title_ko", "logline_ko", "bible", "music", "scenes"], "properties": {
    "title_ko": {"type": "string"}, "logline_ko": {"type": "string"},
    "bible": {"type": "object", "required": ["characters", "places", "time", "light"], "properties": {
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


def story_problems(data, seconds: float, person_ok: bool = True) -> list[str]:
    """구성안 검사. 고칠 말을 영어로 돌려준다 (모델에게 다시 보낸다)"""
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
             "bible": {"characters": chars, "places": places, "time": _s(bible.get("time")), "light": _s(bible.get("light"))},
             "music": {"tags": _s(music.get("tags"))[:400] or "calm cinematic instrumental, soft piano, warm strings",
                       "bpm": bpm if bpm and 50 <= bpm <= 180 else 90, "key": key if key in MUSIC_KEYS else "C major",
                       "ko": _s(music.get("ko"))[:200]},
             "scenes": scenes}
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
    """이어 쓰기가 CHAIN_MAX 번 연속되면 다음 장면은 새 장면 이미지로 시작한다"""
    run = 0
    for s in scenes:
        if s["continues"]:
            run += 1
            if run > CHAIN_MAX:
                s["continues"] = False
                run = 0
        else:
            run = 0


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
    lines.append(f"This shot: {sc['shot']}. Action: {sc['action']}")
    if sc.get("sound"):
        lines.append(f"Sound: {sc['sound']}")
    if i + 1 < n and story["scenes"][i + 1].get("continues"):
        # 다음 장면이 이 장면의 마지막 프레임에서 시작한다: 끝에 대상이 화면 안에 있어야 한다 (11-17 메인)
        lines.append("The next shot begins from this shot's last frame: keep the main subject fully visible in frame until "
                     "the very last frame, with the camera following it if it moves.")
    return "\n".join(lines)[:1990]


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
    base = (f"{sc['ko']}\nThe first instant of this shot as one still photograph: {sc['shot']}. {sc['action']} "
            f"{who} Place: {place} Time: {b['time']} Light: {b['light']}")
    extra = (" " + plan_scene_text(plan)) if plan else ""
    text = base + extra
    return text if len(text) <= limit else base[:limit]


async def write_story(call, text: str, notes: str, seconds: float, rounds: int = 3) -> tuple[dict | None, list[str], int]:
    """구성안을 같은 LLM 차례 안에서 쓴다. (구성안, 남은 위반, 쓴 횟수). 위반이 남아도 다듬어 쓸 수 있으면 돌려준다"""
    count, n_min, n_max = story_counts(seconds)
    system = STORY_SYSTEM.format(seconds=int(seconds), keys=", ".join(MUSIC_KEYS), count=count, n_min=n_min, n_max=n_max,
                                 tolerance=story_tolerance(seconds))
    user = f"Request (Korean): {text}\n" + (f"Must-keep details from the user: {notes}\n" if notes else "")
    said = f"{text or ''} {notes or ''}"
    person_ok = bool(PERSON_REQ_RE.search(said)) or bool(STORY_PERSON_KO_RE.search(said))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    best, problems, rnd = None, ["no answer"], 0
    for rnd in range(1, rounds + 1):
        reply = await call({"messages": messages, "format": STORY_SCHEMA, "think": False, "options": {"temperature": 0.7}})
        content = (reply.get("message") or {}).get("content", "")
        try:
            data = parse_json_reply(content)
        except PromptxError as e:
            data, problems = None, [str(e)]
        else:
            problems = story_problems(data, seconds, person_ok)
        story = clean_story(data, seconds) if data is not None else None
        if story is not None and (best is None or len(problems) < len(best[1])):
            best = (story, problems)
        if not problems:
            break
        messages = messages + [{"role": "assistant", "content": content or "{}"}, repair_message(problems)]
    if best is None:
        return None, problems, rnd
    return best[0], best[1], rnd


async def storyboard(user, ip: str, username: str, slug: str, text: str, notes: str = "", seconds=None) -> dict:
    """긴 영상 구성안 (감독이 계획 카드 전에 한 번). 못 쓰면 PromptxError"""
    from . import llm

    text = clean_input(text)
    notes = str(notes or "").strip()[:2000]
    sec = max(STORY_MIN_SECONDS, min(STORY_MAX_SECONDS, float(seconds or 60)))
    try:
        async with llm.llm_turn(user, ip, settings.expand_model, "video_story") as call:
            story, left, rounds = await write_story(call, text, notes, sec)
    except llm.HelperBusy as e:
        raise PromptxError(str(e), 503) from None
    except RuntimeError as e:
        raise PromptxError(str(e), 502) from None
    if story is None:
        raise PromptxError("구성안을 만들지 못했습니다: " + "; ".join(left[:3]), 502)
    return {"story": story, "violations": left, "rounds": rounds, "model": settings.expand_model}


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
           r"|snake|animal)(?:s|es)?|puppies|ponies|butterflies|dragonflies")
_MOVE = (r"(?:walk|trot|gallop|run|sprint|prowl|lope|bound|leap|jump|hop|soar|glide|swim|dart|crawl|slither|trudg|pad|"
         r"stride|fl(?:y|ies|ew|ying))(?:s|es|ing|ed)?|ran|flies|flying|flew")
ANIMAL_MOVER_RE = re.compile(rf"\b(?:{_ANIMAL})\b(?:\s+[\w'’,-]+){{0,6}}?\s+(?:{_MOVE})\b", re.I)
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
                 music: bool | None = None) -> dict:
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
    rt, crew_advice = promptx.crew_route(text, notes, crew)
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
                plan = await write_plan(call, text, notes, profile, suggested["seconds"], frame, scene, staging)
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
               seconds=None, staging: str | None = None) -> dict | None:
    """연출 계획표 (감독이 장면 이미지를 그리기 전에 한 번). 실패해도 예외 없이 None."""
    from . import llm

    text = clean_input(text)
    notes = str(notes or "").strip()[:2000]
    pid, profile = profile_of(profile_id)
    suggested = suggest_settings(profile, seconds)
    try:
        async with llm.llm_turn(user, ip, settings.expand_model, "video_plan") as call:
            return await write_plan(call, text, notes, profile, suggested["seconds"], None, "", staging)
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
