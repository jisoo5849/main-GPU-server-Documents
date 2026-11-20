"""그림·클립 자동 검사 (11-20): 시각 모델(promptx.inspect) 결과로 장면 그림·편집 그림·클립의 검사 프레임을 다시 만들지 정한다.

11-20 메인 재시험(요청 4a31da67)에서 기준 그림을 편집한 첫 화면에 푸토르가 둘로 나왔다(편집 오류). 같은 씨앗으로 재현했고,
메인의 시각 모델 검사(qwen3-vl 8B)는 그 그림을 사람 2명으로, 한 명짜리 그림을 1명으로, 니키·푸토르가 걷는 넓은 장면을 2명으로 셌다
(한 장 9~19초). 사용자: 입 모양은 중요하지 않고 물리적인 오류·편집 오류·화풍 고정만 본다.

사람 수 기준 (11-20 검토에서 고침):
- 한 사람 그림(인물 기준 그림·기준 그림을 고친 첫 화면·입 맞춤 클립)은 정확히 1명.
- 긴 영상의 장면 그림은 설정집 인물(되풀이해 나오는 사람)만 알아서, 한 장면에만 나오는 행인·상인·군중이 빠져 있다. 그래서 가까운
  장면만 정확히, 미디엄은 빠진·겹친 사람만, 넓은 장면은 세지 않는다. 동물 인물이 있으면 사람 수와 팔다리 수를 보지 않는다
  (동물을 사람으로 셀지 정해져 있지 않고, 다리 넷이 '팔다리 더함'으로 잡힌다).
- 클립은 첫 화면의 사람을 그대로 이어 가므로 새 씨앗으로 사람 수를 고칠 수 없다. 첫 화면에서 센 수보다 많아졌을 때(클립 안에서
  지어낸 사람·복제)만 다시 만든다. 화면 밖으로 나간 사람은 괜찮다.

순수 함수만 둔다(감독이 검사를 부르고, 여기서는 기준을 정하고 결과를 읽기만 한다)."""
import re

# 다시 만들 몸의 이상. 손가락·안 보이는 팔다리(missing_limb·extra_fingers)는 소매에 숨긴 손이나 화면 밖으로 잘린 몸 때문에
# 잘못 잡히기 쉬워 뺀다. 글자(text_artifact)는 몸의 오류가 아니다
BODY_ISSUES = frozenset({"extra_head", "missing_head", "detached_head", "merged_bodies", "extra_limb", "floating_limb", "face"})
# 동물이 있는 장면에서는 빼는 몸의 이상 (부위 나열이 동물 다리 넷을 팔다리 더함으로, 동물 머리를 머리 없음으로 잡는다)
ANIMAL_SKIP = frozenset({"extra_limb", "floating_limb", "missing_head"})
# 입 맞춤 클립에서는 빼는 몸의 이상: 말하는 입이 흔들린 얼굴을 '뭉개진 얼굴'로 잡을 수 있고, 입 모양은 보지 않기로 했다 (11-20 사용자)
TALK_SKIP = frozenset({"face"})
# 장면 글에 이런 말이 있으면 설정집 인물 말고도 사람이 나오는 장면이다 (행인·상인·군중)
EXTRAS_RE = re.compile(
    r"\b(?:crowds?|people|passers?-?by|pedestrians?|shoppers?|customers?|vendors?|merchants?|villagers?|townsfolk|students?|"
    r"audience|onlookers?|spectators?|tourists?|travell?ers?|workers?|strangers?|others|bystanders?|families|children|kids|"
    r"bakers?|shopkeepers?|waiters?|waitress(?:es)?|guards?|soldiers?|dancers?|musicians?|players?|riders?|drivers?|"
    r"passengers?|commuters?|fishermen|farmers?|monks?|neighbou?rs?|"
    r"man|men|woman|women|boys?|girls?|lady|ladies|child|baby|friends?|mother|father|grand\w+|elder|sailors?|"
    r"cook|chef|clerk|teacher|doctor|nurse|police|officers?)\b", re.I)
CROWD_COUNT = 3            # 첫 화면의 사람이 이보다 많으면 클립에서 사람 수를 세지 않는다 (군중은 셀 때마다 수가 달라진다)


def style_trait(style: str) -> str:
    """검사 모델에게 볼 화풍 한 줄 (사실적이지 않은 화풍만 준다: 사진 화풍은 따로 보지 않는다)"""
    style = str(style or "").strip().rstrip(".")
    return f"The whole picture is in this art style: {style}" if style else ""


def rule(want: int | None = None, more_ok: bool = False, fewer_ok: bool = False, skip=frozenset()) -> dict:
    """사람 수 기준과 보지 않을 몸의 이상. want 가 None 이면 사람 수를 세지 않는다"""
    return {"want": want, "more_ok": bool(more_ok), "fewer_ok": bool(fewer_ok), "skip": frozenset(skip)}


ONE = rule(1)              # 한 사람 그림 (인물 기준 그림·기준 그림을 고친 첫 화면)


def scene_rule(humans: int, animals: int, rank: int | None, extras: bool) -> dict:
    """긴 영상 장면 그림(첫 화면)의 기준. humans·animals 는 그 장면의 설정집 인물 수, rank 는 video.shot_rank (0 가까움 ~ 5 아주 넓음),
    extras 는 장면 글에 다른 사람(행인·상인·군중) 말이 있는지"""
    if animals:
        return rule(None, skip=ANIMAL_SKIP)
    if rank is not None and rank <= 1:     # 클로즈업·미디엄 클로즈업: 정해진 인물만 (복제·지어낸 사람을 잡는다)
        return rule(humans, more_ok=extras)
    if rank == 2:                          # 미디엄: 빠진 사람·겹친 사람만 (행인이 더 그려지는 것은 괜찮다)
        return rule(humans, more_ok=True)
    return rule(None)                      # 넓은 장면: 작게 그려진 사람·행인 때문에 세지 않는다 (몸의 이상·화풍만 본다)


def clip_rule(first_people: int | None, animals: int, talk: bool = False) -> dict:
    """클립 가운데 프레임의 기준. first_people 은 그 클립의 첫 화면에서 센 사람 수 (모르면 None).
    입 맞춤 클립은 한 사람이 그대로 있어야 한다 (첫 화면이 이미 한 사람으로 검사됐다)"""
    skip = (ANIMAL_SKIP if animals else frozenset()) | (TALK_SKIP if talk else frozenset())
    if talk and not animals:
        if isinstance(first_people, int) and not isinstance(first_people, bool) and 1 < first_people <= CROWD_COUNT:
            return rule(first_people, fewer_ok=True, skip=skip)   # 첫 화면에 둘이 남았어도 클립이 더 늘리지만 않으면 된다
        return rule(1, skip=skip)
    if animals or not isinstance(first_people, int) or isinstance(first_people, bool) or first_people > CROWD_COUNT:
        return rule(None, skip=skip)
    return rule(first_people, fewer_ok=True, skip=skip)


def problems(insp, want_or_rule=None, style: str = "") -> list[str]:
    """검사 결과에서 다시 만들 까닭(짧은 영어 낱말). 빈 목록이면 통과다.
    want_or_rule: rule() 결과, 또는 사람 수(정확히 그 수), 또는 None(세지 않음).
    - 사람 수: 기준보다 많으면(복제·지어낸 사람) / 적으면(빠지거나 겹친 사람) 실패. more_ok·fewer_ok 로 한쪽을 봐준다.
    - 몸의 이상: BODY_ISSUES 에서 skip 을 뺀 것만 본다.
    - 화풍: 화풍을 검사하라고 줬고, 검사 모델이 그 화풍이 아니라고 이유를 붙여 답했을 때만 (답을 빠뜨린 것은 실패로 보지 않는다).
    검사 결과가 없으면(검사를 못 했으면) 통과로 본다: 검사가 결과를 막으면 안 된다"""
    if not isinstance(insp, dict):
        return []
    r = want_or_rule if isinstance(want_or_rule, dict) else rule(want_or_rule)
    why = []
    n, want = insp.get("people"), r.get("want")
    if want is not None and isinstance(n, int) and not isinstance(n, bool):
        if (n > int(want) and not r.get("more_ok")) or (n < int(want) and not r.get("fewer_ok")):
            why.append(f"people:{n}/{int(want)}")
    look = BODY_ISSUES - frozenset(r.get("skip") or ())
    bad = sorted({str(i.get("type")) for i in insp.get("issues") or [] if isinstance(i, dict) and i.get("type") in look})
    if bad:
        why.append("body:" + ",".join(bad))
    if style and any(isinstance(t, dict) and t.get("present") is False and str(t.get("note") or "").strip()
                     for t in insp.get("traits") or []):
        why.append("style")
    return why


def describe(why) -> str:
    """사용자에게 보일 한국어 (다시 만들어도 남은 문제를 결과에 한 줄로 알린다)"""
    parts = []
    for w in why or []:
        w = str(w)
        if w.startswith("people:"):
            parts.append("사람 수")
        elif w.startswith("body:"):
            parts.append("몸 모양")
        elif w == "style":
            parts.append("화풍")
    return "·".join(dict.fromkeys(parts))


def better(a: list[str] | None, b: list[str]) -> bool:
    """b 가 a 보다 나은 결과인가 (남은 까닭이 적을수록). 처음(a 가 None)이면 늘 낫다"""
    return a is None or len(b) < len(a)
