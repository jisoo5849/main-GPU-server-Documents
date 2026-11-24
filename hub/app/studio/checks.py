"""그림·클립 자동 검사 (11-20): 시각 모델(promptx.inspect) 결과로 장면 그림·편집 그림·클립의 검사 프레임을 다시 만들지 정한다.

11-20 메인 재시험(요청 4a31da67)에서 기준 그림을 편집한 첫 화면에 푸토르가 둘로 나왔다(편집 오류). 같은 씨앗으로 재현했고,
메인의 시각 모델 검사(qwen3-vl 8B)는 그 그림을 사람 2명으로, 한 명짜리 그림을 1명으로, 니키·푸토르가 걷는 넓은 장면을 2명으로 셌다
(한 장 9~19초). 사용자: 입 모양은 중요하지 않고 물리적인 오류·편집 오류·화풍 고정만 본다.

사람 수 기준 (11-20 검토에서 고침):
- 한 사람 그림(인물 기준 그림·기준 그림을 고친 첫 화면·입 맞춤 클립)은 정확히 1명. 두 사람 대사 컷(11-24)의 클립은 정확히 2명.
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
# 가슴 아래가 잘린 클로즈업의 '팔다리 떠 있음'은 오탐이다 (11-24 메인: 베스 클로즈업 첫 화면을 다섯 번 다시 그렸다). 입 맞춤 클립도 같다
CLOSE_SKIP = frozenset({"floating_limb"})
TALK_SKIP = frozenset({"face"}) | CLOSE_SKIP
# 두 사람 대사 컷(미디엄 투샷, 11-24): 얼굴 이상은 입 맞춤과 같이 보지 않고, 클로즈업 오탐 예외(CLOSE_SKIP)는 해당 없다 (허리까지 보인다)
DUO_TALK_SKIP = frozenset({"face"})
# 장면 글에 이런 말이 있으면 설정집 인물 말고도 사람이 나오는 장면이다 (행인·상인·군중)
EXTRAS_RE = re.compile(
    r"\b(?:crowds?|people|passers?-?by|pedestrians?|shoppers?|customers?|vendors?|merchants?|villagers?|townsfolk|students?|"
    r"audience|onlookers?|spectators?|tourists?|travell?ers?|workers?|strangers?|others|bystanders?|families|children|kids|"
    r"bakers?|shopkeepers?|waiters?|waitress(?:es)?|guards?|soldiers?|dancers?|musicians?|players?|riders?|drivers?|"
    r"passengers?|commuters?|fishermen|farmers?|monks?|neighbou?rs?|"
    r"man|men|woman|women|boys?|girls?|lady|ladies|child|baby|friends?|mother|father|grand\w+|elder|sailors?|"
    r"cook|chef|clerk|teacher|doctor|nurse|police|officers?)\b", re.I)
# 무리(행인·군중)를 가리키는 말만. EXTRAS_RE 의 mother·woman·girl 같은 말은 설정집 인물 자신을 가리키는 일이 많아, 새로 그린 첫 화면의
# 사람 수를 느슨하게 하면 안 된다 (11-21 메인: 베스 혼자 장면에 2명이 나왔는데 'mother' 때문에 사람 수 오류로 잡히지 않았다)
# 여러 사람을 가리키는 복수 낱말(people·children …)과 crowded·bustling 은 남긴다: 시장 장면의 'full of people' 처럼 실제 군중이다
CROWD_RE = re.compile(
    r"\b(?:crowds?|crowded|bustling|people|children|kids|families|students|travell?ers|dancers|musicians|players|others|"
    r"passers?-?by|pedestrians?|shoppers?|customers?|vendors?|merchants?|villagers?|townsfolk|audience|onlookers?|"
    r"spectators?|tourists?|bystanders?|commuters?|passengers?|strangers?|guards?|soldiers?|workers?)\b", re.I)
# 얼굴 없는 형상(구도 그림)은 얼굴 이상·머리 없음으로 잡히기 쉽다
LAYOUT_SKIP = frozenset({"face", "missing_head", "detached_head"})
TRAIT_MAX = 200            # promptx.inspect 가 받는 특징 한 줄의 최대 길이
CROWD_COUNT = 3           # 첫 화면의 사람이 이보다 많으면 클립에서 사람 수를 세지 않는다 (군중은 셀 때마다 수가 달라진다)


def style_trait(style: str) -> str:
    """검사 모델에게 볼 화풍 한 줄 (사실적이지 않은 화풍만 준다. 실사 영화는 감독이 style_negative('photo') 를 따로 넣는다, 11-24)"""
    style = str(style or "").strip().rstrip(".")
    return f"The whole picture is in this art style: {style}" if style else ""


def style_negative(style_key: str) -> str:
    """화풍 열쇠(cast_lib.style_key)별로 '아닌 것'을 함께 말하는 검사 문장 (영어). style_trait 의 화풍 글만으로는 검사 모델이
    3D 렌더·사진·만화를 가려내지 못해서(11-20 실측 S6: 실사로 바꾼 그림에 그림 느낌이 남아도 통과) 아닌 쪽을 못 박는다"""
    return {
        "semi": "The picture is a soft painted semi-realistic illustration, not a glossy 3D render, not a flat cartoon, not cel-shaded",
        "anime": "The picture is a 2D anime-style illustration with clean cel-shaded line art, not a 3D render, not a photograph",
        "photo": "The picture looks like a real photograph or live-action film still, not a painting, not a cartoon, not a 3D render",
    }.get(str(style_key or ""), "")


def cast_trait(look: str, name: str = "") -> str:
    """끝 프레임 검사용: 이 외형의 인물이 화면 안에 있고 얼굴이 보이는가 (검사 모델에게 주는 '기대' 한 줄, 영어).
    외형은 첫 14낱말까지만 쓰고(쉼표·공백 경계, 끝 문장부호 제거) 전체가 200자(promptx 의 특징 한 개 한도)를 넘지 않게 자른다.
    name 은 검사 모델이 모르는 이름이라 글에 넣지 않는다(호출 쪽 시그니처 맞춤용)"""
    # '몸 전체가 화면 안'이 아니라 '얼굴이 화면 안'을 묻는다: 대사 클로즈업은 가슴에서 잘려도 맞는 그림이다
    # (11-21 메인: 얼굴이 정면으로 보이는 니키 끝 프레임을 '없음'으로 답해 클립을 헛되이 다시 만들었다)
    head = ("A person who looks like this is in the picture with the whole face inside the frame, clearly visible "
            "from the front or three-quarter: ")
    words = " ".join(str(look or "").split()[:14]).strip(" ,;:.")
    room = TRAIT_MAX - len(head)
    if len(words) > room:
        cut = words[:room]
        words = (cut.rsplit(" ", 1)[0] if " " in cut else cut).strip(" ,;:.")
    return head + words


SIGN_ITEMS = 2            # 인물마다 검사에 쓰는 알아볼 소품 수: 라이브러리 signature 앞쪽(머리·모자·윗옷)은 가까운 샷에도 보인다.
                          # 뒤쪽(장화·브로치)은 앉은 장면·작은 물건이라 안 보여도 틀린 그림이 아니다


def sign_trait(look: str, signature=None) -> str:
    """새로 그린 첫 화면 검사용: 이 인물이 그림에 있고 알아볼 소품이 보이는가 (영어 한 줄, TRAIT_MAX 이내).
    signature 가 없으면 외형 앞부분(cast_trait)으로"""
    items = [str(x).strip().rstrip(".") for x in (signature or []) if str(x).strip()][:SIGN_ITEMS]
    if not items:
        return cast_trait(look)
    out = "A person with " + " and ".join(items) + " is in the picture with the face visible"
    return out[:TRAIT_MAX]


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


def clip_rule(first_people: int | None, animals: int, talk: bool = False, duo: bool = False, crowd: bool = False) -> dict:
    """클립 가운데 프레임의 기준. first_people 은 그 클립의 첫 화면에서 센 사람 수 (모르면 None).
    입 맞춤 클립은 한 사람이 그대로 있어야 한다 (첫 화면이 이미 한 사람으로 검사됐다).
    두 사람 대사 컷(talk·duo)은 두 사람 아래로는 안 된다: 듣는 사람이 사라져도 다시 만든다 (fewer_ok 거짓, 11-24).
    crowd 는 장면 글에 무리 말이 있을 때: 첫 화면 기준(compose_rule)처럼 더 많아도 된다 (배경 행인이 첫 화면부터 있어 다시 만들어도 같다)"""
    if talk and duo:
        return rule(2, more_ok=crowd, skip=DUO_TALK_SKIP | (ANIMAL_SKIP if animals else frozenset()))
    skip = (ANIMAL_SKIP if animals else frozenset()) | (TALK_SKIP if talk else frozenset())
    if talk and not animals:
        if isinstance(first_people, int) and not isinstance(first_people, bool) and 1 < first_people <= CROWD_COUNT:
            return rule(first_people, fewer_ok=True, skip=skip)   # 첫 화면에 둘이 남았어도 클립이 더 늘리지만 않으면 된다
        return rule(1, skip=skip)
    if animals or not isinstance(first_people, int) or isinstance(first_people, bool) or first_people > CROWD_COUNT:
        return rule(None, skip=skip)
    return rule(first_people, fewer_ok=True, skip=skip)


def cut_rule(humans: int, animals: int, extras: bool) -> dict:
    """앞 장면의 마지막 프레임을 고쳐 만든 장면의 첫 화면 기준 (장면 잇기). 샷 크기와 상관없이 그 장면 인물 수를 센다:
    고치는 편집이 사람을 복제하거나 빠뜨리기 쉽다. 장면 글에 행인 말이 있으면 더 많아도 되고, 동물이 있으면 사람으로 세어질 수 있어
    더 많아도 되지만 사람 수 아래로는 안 된다(want 를 None 으로 두지 않는다). 동물이 있으면 팔다리 검사는 뺀다"""
    return rule(humans, more_ok=bool(extras) or bool(animals), skip=ANIMAL_SKIP if animals else frozenset())


def compose_rule(humans: int, animals: int, crowd: bool, close: bool = False) -> dict:
    """새로 그린 첫 화면(기준 그림·구도 그림에서 그린 것)의 사람 수 기준: 장면 인물 수 정확히. 장면 글에 무리 말(CROWD_RE)이 있거나
    동물이 있을 때만 더 많아도 된다. cut_rule 과 같은 짜임이지만 EXTRAS_RE 대신 무리 말만 본다.
    close(클로즈업·미디엄 클로즈업)이면 '팔다리 떠 있음'은 보지 않는다"""
    skip = (ANIMAL_SKIP if animals else frozenset()) | (CLOSE_SKIP if close else frozenset())
    return rule(humans, more_ok=bool(crowd) or bool(animals), skip=skip)


def layout_trait(n: int) -> str:
    """구도 그림 검사용: 얼굴 없는 형상이 정확히 n 개 (검사 모델이 형상을 사람으로 세지 않을 수 있어 특징 줄로 묻는다)"""
    # 몸이 아니라 머리가 화면 안인지 묻는다: 클로즈업·미디엄 구도는 형상이 가슴·허리에서 잘리는 것이 맞다 (11-21 검토)
    return f"Exactly {n} faceless mannequin figure{'s' if n != 1 else ''} with blank heads, every head fully inside the frame"[:TRAIT_MAX]


def layout_problems(insp, n: int, animals: int = 0, style: str = "") -> list[str]:
    """구도 그림 검사 결과에서 다시 그릴 까닭. traits[0] = layout_trait(n), (style 이면) 마지막 줄이 화풍.
    - layout:unchecked: 검사 결과 없음 (다시 그리지 않는다 — 다음 단계의 인물 검사가 사람 수를 다시 본다)
    - layout:figures: 형상 수 줄이 '아님'
    - people:<센 수>/<n>: 사람을 형상보다 많이 셌다 (형상을 사람으로 안 세서 적게 세는 것은 보지 않는다). 동물이 있으면 보지 않는다
    - body:… (얼굴 이상·머리 없음 제외), style"""
    if not isinstance(insp, dict):
        return ["layout:unchecked"]
    why = []
    traits = insp.get("traits") if isinstance(insp.get("traits"), list) else []
    t0 = traits[0] if traits and isinstance(traits[0], dict) else {}
    if t0.get("present") is False:
        why.append("layout:figures")
    p = insp.get("people")
    if isinstance(p, int) and not isinstance(p, bool) and p > n and not animals:
        why.append(f"people:{p}/{n}")
    why += _body_reasons(insp, rule(None, skip=LAYOUT_SKIP | (ANIMAL_SKIP if animals else frozenset())))
    last = len(traits) - 1
    if style and last >= 1:
        t = traits[last]
        if isinstance(t, dict) and t.get("present") is False and str(t.get("note") or "").strip():
            why.append("style")
    return why


def end_rule(humans: int, animals: int, extras: bool) -> dict:
    """사람이 나오는 장면의 클립 마지막 프레임 기준: 다음 장면이 이 프레임에서 시작하므로 그 장면 인물이 모두 있어야 한다.
    아래쪽만 본다(사람 수 >= humans, more_ok 항상 참: 행인·군중·동물이 더 있어도 끝 프레임을 버리지 않는다).
    사람 없는 장면(humans 0, 행인 말도 동물도 없음)은 정확히 0명이어야 해서 want=None 에 zero=True 로 적는다
    (다음 장면이 지어낸 사람을 이어받지 않게). 동물이 있으면 동물이 사람으로 세어질 수 있어 0명을 요구하지 않는다.
    몸의 이상은 클립 기준과 같고(동물이면 팔다리 검사 뺌) 입 맞춤 예외(TALK_SKIP)는 없다"""
    humans = max(int(humans or 0), 0)
    r = rule(humans or None, more_ok=True, skip=ANIMAL_SKIP if animals else frozenset())
    r["zero"] = humans == 0 and not extras and not animals
    r["end"] = True
    return r


def _people_reason(insp: dict, r: dict) -> list[str]:
    """사람 수 기준(r 의 want·more_ok·fewer_ok)에 어긋나면 ['people:<센 수>/<기준>']"""
    n, want = insp.get("people"), r.get("want")
    if want is not None and isinstance(n, int) and not isinstance(n, bool):
        if (n > int(want) and not r.get("more_ok")) or (n < int(want) and not r.get("fewer_ok")):
            return [f"people:{n}/{int(want)}"]
    return []


def _body_reasons(insp: dict, r: dict) -> list[str]:
    look = BODY_ISSUES - frozenset(r.get("skip") or ())
    bad = sorted({str(i.get("type")) for i in insp.get("issues") or [] if isinstance(i, dict) and i.get("type") in look})
    return ["body:" + ",".join(bad)] if bad else []


def end_problems(insp, rule: dict, cast_ids: list[str], first_trait_index: int = 0, style: str = "") -> list[str]:
    """끝 프레임 검사 결과에서 다시 만들 까닭 (짧은 영어 코드). 빈 목록이면 통과.
    - end:unchecked: 검사 결과가 없다. 통과로 보지 않고 감독이 정한다 (다음 장면의 원본이 되는 프레임이라)
    - end:people:<n>/<최소>: 사람이 모자람 / end:extra:<n>: 사람 없는 장면인데 사람이 보임
    - end:missing:<id>: 그 인물의 cast_trait(traits[first_trait_index + i], cast_ids 와 같은 차례)가 '없음'. 검사 모델이 모르겠다고
      (None) 답했어도 사람이 한 명도 안 보이면 없는 것으로 본다
    - body:…: problems 와 같음
    - style: 화풍 특징을 마지막 특징으로 줬고(style 글이 있고 인물 특징 뒤에 있는 마지막 것) 이유를 붙여 '아님'이라고 답했을 때
    rule 은 end_rule() 결과(아니면 사람 수 기준 없이 몸의 이상만 본다)"""
    if not isinstance(insp, dict):
        return ["end:unchecked"]
    r = rule if isinstance(rule, dict) else {}
    why = []
    n = insp.get("people")
    n = n if isinstance(n, int) and not isinstance(n, bool) else None
    want = r.get("want")
    if n is not None:
        if want is not None and n < int(want):
            why.append(f"end:people:{n}/{int(want)}")
        elif r.get("zero") and n > 0:
            why.append(f"end:extra:{n}")
    traits = insp.get("traits") if isinstance(insp.get("traits"), list) else []
    for i, cid in enumerate(cast_ids or []):
        j = first_trait_index + i
        t = traits[j] if 0 <= j < len(traits) and isinstance(traits[j], dict) else {}
        if t.get("present") is False or (t.get("present") is None and n == 0):
            why.append(f"end:missing:{cid}")
    why += _body_reasons(insp, r)
    last = len(traits) - 1
    if style and last >= first_trait_index + len(cast_ids or []):
        t = traits[last]
        if isinstance(t, dict) and t.get("present") is False and str(t.get("note") or "").strip():
            why.append("style")
    return why


def cast_problems(insp, rule: dict, cast_ids: list[str], style: str = "") -> list[str]:
    """새로 그린 첫 화면(compose_ref) 검사 결과에서 다시 그릴 까닭. traits 는 cast_ids 차례의 sign_trait 다음에 (style 이면) 화풍 한 줄.
    - cast:unchecked: 검사 결과가 없다 (통과로 보지 않고 결과에 알린다. 다시 그려도 검사가 없으니 다시 그리지는 않는다)
    - people:<n>/<want>: problems 와 같은 사람 수 기준(cut_rule)
    - cast:missing:<id>: 그 인물의 특징 줄이 '없음' (얼굴·머리·옷이 기준 그림과 다르게 그려졌거나 빠졌다)
    - body:…, style: problems·end_problems 와 같음"""
    if not isinstance(insp, dict):
        return ["cast:unchecked"]
    r = rule if isinstance(rule, dict) else {}
    why = _people_reason(insp, r)
    traits = insp.get("traits") if isinstance(insp.get("traits"), list) else []
    for j, cid in enumerate(cast_ids or []):
        t = traits[j] if j < len(traits) and isinstance(traits[j], dict) else {}
        if t.get("present") is False:
            why.append(f"cast:missing:{cid}")
    why += _body_reasons(insp, r)
    last = len(traits) - 1
    if style and last >= len(cast_ids or []):
        t = traits[last]
        if isinstance(t, dict) and t.get("present") is False and str(t.get("note") or "").strip():
            why.append("style")
    return why


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
    why = _people_reason(insp, r)
    why += _body_reasons(insp, r)
    if style and any(isinstance(t, dict) and t.get("present") is False and str(t.get("note") or "").strip()
                     for t in insp.get("traits") or []):
        why.append("style")
    return why


def describe(why) -> str:
    """사용자에게 보일 한국어 (다시 만들어도 남은 문제를 결과에 한 줄로 알린다)"""
    parts = []
    for w in why or []:
        w = str(w)
        if w == "end:unchecked":
            parts.append("끝 프레임 확인 못 함")
        elif w.startswith(("end:missing", "end:people", "end:extra")):
            parts.append("끝 프레임에 인물 없음")
        elif w == "layout:figures":
            parts.append("구도 그림의 형상 수")
        elif w == "layout:unchecked":
            parts.append("구도 그림 확인 못 함")
        elif w.startswith("cast:missing"):
            parts.append("인물 모습(기준 그림과 다름)")
        elif w == "cast:unchecked":
            parts.append("인물 모습 확인 못 함")
        elif w.startswith("people:"):
            parts.append("사람 수")
        elif w.startswith("body:"):
            parts.append("몸 모양")
        elif w == "style":
            parts.append("화풍")
    return "·".join(dict.fromkeys(parts))


def _rank(why) -> tuple[int, int]:
    """(끝 프레임·인물 모습 까닭 수, 전체 까닭 수): 끝 프레임 실패(다음 장면이 그 프레임에서 시작한다)와 인물이 기준 그림과 다른 것
    (일관성이 가장 중요하다, 11-20 사용자)은 다른 문제보다 나쁘다"""
    why = [str(w) for w in why or []]
    return sum(w.startswith(("end:", "cast:missing", "layout:figures")) for w in why), len(why)


def better(a: list[str] | None, b: list[str]) -> bool:
    """b 가 a 보다 나은 결과인가. 끝 프레임 까닭(end:*)이 적을수록, 같으면 남은 까닭이 적을수록. 처음(a 가 None)이면 늘 낫다"""
    return a is None or _rank(b) < _rank(a)
