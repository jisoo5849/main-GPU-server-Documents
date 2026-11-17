"""영상 엔진 고르기: 소리 포함(LTX)·소리 없음(Wan)과 화질을 어느 레시피로 만들지.

api._post_message 가 DB 없이 시험되지 않아서 판단만 따로 뗐다. 순수 함수이고 hub/tests/test_studio_director.py 가 검증한다.

우선순위: 화면의 소리 칸 > 글의 '소리 없이' > 이어 말하기(고치기)가 잇는 앞 요청의 엔진 > 설정 기본(STUDIO_SOUND_DEFAULT).
소리 포함 영상: 화질 높음은 증류 없는 LTX-2.3 dev, 빠름은 LTX-2.5 distilled. 소리 포함 높음 레시피가 없을 때만 높음은 소리 없는 Wan 으로 간다.
"""
from . import intent, recipes


def choose(d, options: dict, text: str, action: str, carry: str, last_turn: dict | None, want_high: bool, high_ok: bool,
           sound_default: bool) -> dict:
    """{"use_sound": bool, "high_ok": bool, "notes": [한국어 안내]}.  영상(i2v·t2v)이 아니면 소리 없음으로 돌려준다"""
    out = {"use_sound": False, "high_ok": high_ok, "notes": []}
    if d is None or action not in ("i2v", "t2v"):
        return out
    notes = out["notes"]
    opt = options.get("sound")
    ltx_high = d.recipe_for("i2v", "high", True)
    ltx_high_ok = ltx_high is not None and recipes.quality_of(ltx_high) == "high"
    use = False
    if opt is not None:
        use = opt == "on"
    elif intent.MUTE_RE.search(text or ""):
        use = False
    elif carry == "revise" and last_turn:
        use = d.engine_of_turn(last_turn) == "ltx"
        if not use and sound_default:
            notes.append("앞 영상이 소리 없는 영상이라 이어서 소리 없이 만들었습니다. 소리를 넣으려면 소리 칸을 '포함'으로 바꿔 주세요")
    elif want_high and not ltx_high_ok:
        notes.append("화질 높음은 소리가 없는 영상으로 만들었습니다. 소리를 넣으려면 소리 칸을 '포함'으로 바꿔 주세요")
    else:
        use = bool(sound_default)
    if use:
        # 쓸 수 있는 소리 포함 레시피가 있는가: 높음이면 높음(없으면 빠름으로 내려간다), 빠름이면 빠름
        have = d.recipe_for("i2v", "high" if want_high else "fast", True)
        if have is None or (action == "t2v" and d.recipe_for("image") is None):
            notes.append("소리 포함 영상은 지금 준비 중이라 소리 없이 만들었습니다")
            use = False
    if use and want_high and not ltx_high_ok:
        notes.append("소리 포함 영상의 화질 높음은 지금 준비 중이라 기본 화질로 만들었습니다")
        out["high_ok"] = False
    out["use_sound"] = use
    return out
