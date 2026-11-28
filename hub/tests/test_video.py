"""영상 렌더러(video) 단위 테스트. GPU·DB 없이 돈다.

  cd hub && python tests/test_video.py

검사 예시는 영상 시험에서 나온 문제다: 이미지 프롬프트에 같은 문장을 덧붙인 영상은 움직임 지시가 약했고,
제작진 단계에 카메라 움직임·속도·마무리 칸이 없었다 (docs/video-renderer.md).
"""
import asyncio
import contextlib
import copy
import json
import os
import random
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crew, crew_lib, promptx, video  # noqa: E402
from app.config import settings  # noqa: E402

# 11-26: 기존 구성안·렌더 시험의 가짜 모델은 구성안 답만 준다. 정본 인물 영화의 제작진 초안(CREW_CAST_MODE=safe)은
# test_storyboard_film_crew_same_ctx 등 cast 시험이 설정을 켜고 따로 본다
settings.crew_cast_mode = "off"

I2V = video.profile_of("wan22_i2v")[1]
T2V = video.profile_of("wan22_t2v")[1]
FRAME = {"subject": "a glass perfume bottle", "where": "center", "setting": "a marble table", "people": 0,
         "movable": ["light on the glass", "fine mist"], "shot": "close-up at eye level", "text_visible": False}
SCENE = ("a single elegant glass perfume bottle with a gold cap standing centered on a smooth white marble surface, "
         "soft diffused studio lighting from the upper left")
MOTION = ("The camera pushes in slowly toward the bottle while a soft band of light glides across the glass, "
          "fine mist drifts around it, and the motion eases into a calm final hold on the bottle.")
KO = {"camera": "카메라가 병 쪽으로 천천히 다가갑니다.", "motion": "빛이 유리 위를 훑고 안개가 흐릅니다.",
      "timing": "느긋하게 움직이다 마지막에 멈춥니다."}
def _png() -> bytes:
    """올바른 1x1 PNG (Pillow 가 있는 환경에서도 열리게 CRC 까지 맞춘다)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b"")


PNG = _png()


def fit(reply, payload):
    """가짜 LLM: 받은 메뉴(스키마 enum)에서 단계마다 서로 다른 첫 항목을 고른다."""
    reply = copy.deepcopy(reply)
    if isinstance(reply, dict) and reply.pop("fit_menu", False) and isinstance(payload.get("format"), dict):
        props = (payload["format"]["properties"].get("choices") or {}).get("properties") or {}
        used, choices = set(), {}
        for k, v in props.items():
            pick = next(i for i in v["enum"] if i not in used)
            used.add(pick)
            choices[k] = pick
        reply["choices"] = choices
    return reply


GOOD = {"fit_menu": True, "ko": KO, "prompt": MOTION}


# ── 지식 파일·프로필 ──────────────────────────────────────────────
def test_knowledge_profiles_and_steps():
    kn = video.load_knowledge()
    assert set(kn["modes"]) == {"i2v", "t2v"} and kn["global_rules"]
    lib = crew_lib.load_library()
    for cat, spec in kn["categories"].items():
        assert spec["title"] and spec["rule"] and cat in crew.load_knowledge()["categories"]
        keys = [st["key"] for st in spec["steps"]]
        assert keys == ["camera", "subject", "environment", "pacing", "ending"], cat
        for st in spec["steps"]:
            pool = [i for i in lib["by_facet"]["video_motion"] if crew_lib.fits(lib["entries"][i], cat)
                    and lib["entries"][i]["group"].startswith(tuple(st["groups"]))]
            assert len(pool) >= 8, (cat, st["key"], len(pool))
    for pid, p in kn["profiles"].items():
        assert p["mode"] in kn["modes"] and p["fps"] > 0 and (p["max_frames"] - 1) % p["frame_step"] == 0, pid
        assert len(p["words"]) == 2 and p["words"][0] < p["words"][1] and p["models"], pid
    assert [p["id"] for p in video.profile_list()] == list(kn["profiles"])
    try:
        video.profile_of("nope")
        raise AssertionError("unknown profile accepted")
    except promptx.PromptxError:
        pass


def test_suggest_settings_frames_are_4n_plus_1():
    assert video.suggest_settings(I2V, 5) == {"frames": 81, "fps": 16, "seconds": 5.0, "capped": False}
    assert video.suggest_settings(I2V, 3)["frames"] == 49
    s = video.suggest_settings(I2V, 2.3)
    assert (s["frames"] - 1) % 4 == 0 and s["seconds"] <= 2.3
    assert video.suggest_settings(I2V, 12) == {"frames": 81, "fps": 16, "seconds": 5.0, "capped": True}
    assert video.suggest_settings(I2V, None)["frames"] == video.suggest_settings(I2V, "x")["frames"] == 81


# ── 사용자 영상 말 ───────────────────────────────────────────────
def test_video_user_words():
    q = crew_lib.query("향수병이 천천히 회전하는 광고 영상, 루프로")
    assert {d["tag"] for d in q.video_demands} >= {"천천히", "회전하는", "루프로"} or \
        {d["tag"] for d in q.video_demands} >= {"천천히", "회전하는", "루프"}, q.video_demands
    assert all(d["ids"] for d in q.video_demands)
    tags = {d["tag"] for d in crew_lib.query("드론으로 바다 위, 카메라 고정은 아님").video_demands}
    assert "드론으로" in tags or "드론" in tags
    # 이미지 확장과 이미지 제작진은 영상 말을 요구로 보지 않는다
    q = crew_lib.query("향수 광고, 천천히 다가가게")
    assert all(d["facet"] in crew_lib.ENFORCED_FACETS for d in q.demands)
    assert {d["tag"] for d in video.checked_demands("눈 오는 날 거리, 천천히")} == {"눈 오는", "천천히"}


def test_menus_follow_step_groups_and_user_words():
    said = "눈 오는 밤 거리 풍경, 드론으로 천천히"
    menus = video.build_menus(said, "landscape", random.Random(3))
    for st in video.steps_of("landscape"):
        ents = menus[st["key"]]
        assert len(ents) >= 6, st["key"]
        assert all(e["group"].startswith(tuple(st["groups"])) for e in ents), st["key"]
        assert not any(crew.TEXT_NOTE_RE.search(e["en"]) for e in ents)            # 라벨·간판 항목 없음
        assert not any(crew_lib.PEOPLE_EN_RE.search(e["en"]) for e in ents)        # 요청에 사람이 없다
    assert any(e["group"] == video.STILL_GROUP for e in menus["environment"])       # '배경은 고요하게' 선택지
    drone = next(d for d in crew_lib.query(said).video_demands if d["tag"].startswith("드론"))
    assert {e["id"] for e in menus["camera"]} & set(drone["ids"])                    # 사용자 말 항목이 메뉴에 있다
    a = [e["id"] for e in video.build_menus("향수 광고 영상", "product", random.Random(1))["camera"]]
    b = [e["id"] for e in video.build_menus("향수 광고 영상", "product", random.Random(2))["camera"]]
    assert a != b                                                                    # 매번 다른 메뉴


def test_schema_uses_menus():
    menus = video.build_menus("향수 광고 영상", "product", random.Random(0))
    s = video.build_schema("product", menus)
    assert s["required"][0] == "choices" and set(s["properties"]["choices"]["required"]) == set(menus)
    assert s["properties"]["choices"]["properties"]["camera"]["enum"] == [e["id"] for e in menus["camera"]]
    assert "choices" not in video.build_schema()["properties"]


# ── 초안 검사 ───────────────────────────────────────────────────
def menus_and_choice(said="향수 광고 영상", cat="product", seed=0):
    menus = video.build_menus(said, cat, random.Random(seed))
    picked, used = {}, set()
    for st in video.steps_of(cat):
        pick = next(e["id"] for e in menus[st["key"]] if e["id"] not in used)
        used.add(pick)
        picked[st["key"]] = pick
    return menus, picked


def test_check_good_draft_passes():
    menus, picked = menus_and_choice()
    good = {"choices": picked, "ko": KO, "prompt": MOTION}
    assert video.check_video(good, I2V, "향수 광고 영상", FRAME, SCENE, "product", menus) == []
    assert video.check_video({"ko": KO, "prompt": MOTION}, I2V, "향수 광고 영상", FRAME, SCENE) == []


def test_check_rules():
    def probs(prompt, said="향수 광고 영상", frame=FRAME, scene=SCENE, profile=I2V):
        return video.check_video({"ko": KO, "prompt": prompt}, profile, said, frame, scene)

    pad = " The light glides across the glass and the mist drifts around it before a calm final hold."
    assert any("restate" in p for p in probs("A single elegant glass perfume bottle with a gold cap standing still." + pad))
    assert any("one main camera move" in p
               for p in probs("The camera pushes in toward the bottle and then orbits around it slowly." + pad))
    assert any("continuous shot" in p for p in probs("The camera pushes in slowly, then cuts to a wide view of the room." + pad))
    assert any("Negative wording" in p for p in probs("The camera pushes in slowly without any shake." + pad))
    assert any("text, letters" in p for p in probs("The camera pushes in slowly toward the label on the bottle." + pad))
    assert any("No person" in p for p in probs("A woman reaches in as the camera pushes in slowly toward the bottle." + pad))
    assert not any("No person" in p for p in probs("A woman reaches in as the camera pushes in slowly." + pad,
                                                   frame=dict(FRAME, people=1)))   # 첫 화면에 사람이 있으면 된다
    assert any("words" in p for p in probs("The camera pushes in."))
    hidden = probs("The camera pushes in slowly as her hands hold the bottle." + pad, said="향수 광고 영상, 손은 소매 속에 숨김",
                   frame=dict(FRAME, people=1))
    assert any("hands must stay hidden" in p for p in hidden)
    assert any("English only" in p for p in probs(MOTION + " 천천히"))
    assert any("ko.camera" in p for p in video.check_video({"ko": {}, "prompt": MOTION}, I2V, "향수 광고 영상"))
    assert video.check_video([], I2V, "x") and video.check_video({"ko": KO}, I2V, "x")[-1] == '"prompt" is empty.'


def test_user_words_must_show_in_choice_and_text():
    said = "향수 광고 영상, 드론으로 루프"
    menus, picked = menus_and_choice(said, "product", 4)
    demands = crew_lib.query(said).video_demands
    loop = next(d for d in demands if d["tag"].startswith("루프"))
    offered_loop = [e["id"] for e in menus["ending"] if e["id"] in loop["ids"]]
    assert offered_loop                                          # 루프 항목이 메뉴에 들어간다
    picked["ending"] = next(e["id"] for e in menus["ending"] if e["id"] not in loop["ids"])
    probs = video.check_video({"choices": picked, "ko": KO, "prompt": MOTION}, I2V, said, FRAME, SCENE, "product", menus)
    assert any("'루프" in p and "must use an id" in p for p in probs), probs
    assert any("'루프" in p and "clearly show" in p for p in probs), probs
    picked["ending"] = offered_loop[0]
    ok_text = MOTION.replace("calm final hold on the bottle.", "seamless loop back to the start.")
    probs = video.check_video({"choices": picked, "ko": KO, "prompt": ok_text}, I2V, said, FRAME, SCENE, "product", menus)
    assert not any("루프" in p for p in probs), probs


def test_weather_words_and_t2v_scene():
    said = "눈 오는 날 거리"
    p = video.check_video({"ko": KO, "prompt": MOTION}, I2V, said, None, "")
    assert any("'눈 오는'" in x for x in p)
    snowy = ("The camera glides slowly forward along the street while snow keeps falling past the lamps, "
             "light flickers softly in the windows, and the shot settles into a calm hold.")
    assert video.check_video({"ko": KO, "prompt": snowy}, I2V, said, None, "") == []
    # T2V 는 장면 + 움직임을 함께 본다 (장면에 이미 있는 눈은 다시 쓰지 않아도 된다)
    scene = "a quiet city street under falling snow at dusk with warm lamps"
    motion = ("The camera glides slowly forward along the street, flakes drift past the lamps, light flickers softly "
              "in the windows and the shot settles into a calm hold.")
    assert video.check_video({"ko": KO, "prompt": motion}, T2V, said, None, scene) == []


def test_subject_and_environment_differ():
    menus, picked = menus_and_choice()
    shared = [e["id"] for e in menus["subject"] if e["id"] in {x["id"] for x in menus["environment"]}]
    if shared:
        picked["subject"] = picked["environment"] = shared[0]
        probs = video.check_video({"choices": picked, "ko": KO, "prompt": MOTION}, I2V, "향수 광고 영상", FRAME, SCENE,
                                  "product", menus)
        assert any("different ids" in p for p in probs)
    menus["camera"] = menus["camera"][:1]
    picked["camera"] = "video_motion.nope"
    probs = video.check_video({"choices": picked, "ko": KO, "prompt": MOTION}, I2V, "향수 광고 영상", FRAME, SCENE,
                              "product", menus)
    assert any('Step "camera"' in p for p in probs)


# ── 첫 화면 ───────────────────────────────────────────────────
def test_parse_frame_is_robust():
    f = video.parse_frame({"subject": "bottle", "people": "2", "movable": ["mist", "", 3], "text_visible": "yes"})
    assert f["people"] == 2 and f["movable"] == ["mist", "3"] and f["text_visible"] is False
    f = video.parse_frame("broken")
    assert f["subject"] == "" and f["people"] == 0 and f["movable"] == []
    text = video.frame_text(FRAME)
    assert "perfume bottle" in text and "fine mist" in text


def test_frame_file_stays_inside_folders():
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as tmp:
        settings.results_dir, settings.envs_dir = os.path.join(tmp, "results"), os.path.join(tmp, "envs")
        try:
            inp = os.path.join(settings.state_path("alice-main"), "input", "pasted")
            out = os.path.join(settings.results_dir, "alice", "day1")
            os.makedirs(inp)
            os.makedirs(out)
            for d, n in ((inp, "first.png"), (out, "ComfyUI_00001_.png")):
                with open(os.path.join(d, n), "wb") as f:
                    f.write(PNG)
            p = video.frame_file("alice", "alice-main", {"filename": "pasted/first.png", "type": "input"})
            assert p.endswith(os.path.join("pasted", "first.png"))
            p = video.frame_file("alice", "alice-main", {"filename": "ComfyUI_00001_.png [output]", "subfolder": "day1"})
            assert p.endswith("ComfyUI_00001_.png")
            for bad in ({"filename": "../../x.png"}, {"filename": "first.png", "subfolder": "../.."},
                        {"filename": "first.txt", "subfolder": "pasted"}, {"filename": "first.png", "type": "temp"}):
                try:
                    video.frame_file("alice", "alice-main", bad)
                    raise AssertionError(bad)
                except promptx.PromptxError:
                    pass
            assert video.frame_b64(os.path.join(inp, "first.png"))
        finally:
            settings.results_dir, settings.envs_dir = old


def test_compose():
    assert video.compose("i2v", SCENE, MOTION, "STYLE X") == MOTION                  # I2V 는 움직임만
    t = video.compose("t2v", "a quiet street.", "The camera glides forward.", "STYLE X")
    assert t == "a quiet street. The camera glides forward. STYLE X"
    assert video.compose("t2v", "", "The camera glides forward.") == "The camera glides forward."


# ── 초안 받기와 렌더 (가짜 llm) ─────────────────────────────────────
class Call:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def __call__(self, payload):
        self.calls.append(payload)
        return {"message": {"content": json.dumps(fit(self.replies.pop(0), payload))}}


def test_draft_repairs_once_then_gives_up():
    menus = video.build_menus("향수 광고 영상", "product", random.Random(0))
    bad = dict(GOOD, prompt="The camera pushes in and then orbits around the bottle while mist drifts and light glides "
                            "across the glass before the motion settles into a calm final hold on the bottle.")
    call = Call([bad, GOOD])
    _, rounds, left = asyncio.run(video.draft(call, "향수 광고 영상", "", I2V, 5.0, FRAME, SCENE, "product", menus,
                                              repair_rounds=1))
    assert left == [] and rounds == 2 and call.calls[1]["messages"][-1]["content"].startswith("Your JSON broke these rules")
    stubborn = Call([bad, bad])
    _, rounds, left = asyncio.run(video.draft(stubborn, "향수 광고 영상", "", I2V, 5.0, FRAME, SCENE, "product", menus,
                                              repair_rounds=1))
    assert left and rounds == 2


PLAN_PREFIX = "You are the director of a small film crew. Before anything is shot"


def run_render(replies, frame_model=None, plan_reply=None, **kw):
    """plan_reply 가 없으면 연출 계획표 호출은 빈 답({})을 받아 계획표 없이 간다 (기존 시험의 답 순서를 지킨다).
    계획표 호출은 calls 에 넣지 않고 run_render.plan_calls 에 둔다"""
    calls, turns = [], []
    run_render.plan_calls = plan_calls = []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        turns.append((model, purpose))

        async def call(payload):
            if str(payload["messages"][0]["content"]).startswith(PLAN_PREFIX):
                plan_calls.append(payload)
                return {"message": {"content": json.dumps(plan_reply or {})}}
            calls.append(payload)
            return {"message": {"content": json.dumps(fit(replies.pop(0), payload))}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    saved_model = settings.video_frame_model
    sys.modules["app.llm"] = fake
    if frame_model:
        settings.video_frame_model = frame_model
    try:
        result = asyncio.run(video.render({"id": 1}, "127.0.0.1", **kw))
    finally:
        settings.video_frame_model = saved_model
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved
    return result, calls, turns


def with_frame_folder(fn):
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as tmp:
        settings.results_dir, settings.envs_dir = os.path.join(tmp, "results"), os.path.join(tmp, "envs")
        d = os.path.join(settings.state_path("alice-main"), "input")
        os.makedirs(d)
        with open(os.path.join(d, "first.png"), "wb") as f:
            f.write(PNG)
        try:
            return fn()
        finally:
            settings.results_dir, settings.envs_dir = old


def test_render_i2v_reads_the_first_frame_then_crew_draft():
    def go():
        return run_render([FRAME, GOOD], frame_model="vision-model", username="alice", slug="alice-main",
                          text="향수 광고 영상", profile_id="wan22_i2v", seconds=5, image={"filename": "first.png"},
                          image_prompt=SCENE, crew=True, rng=random.Random(0))
    result, calls, turns = with_frame_folder(go)
    assert turns == [("vision-model", "video_frame"), (settings.expand_model, "video_expand")]   # 모델이 달라 차례 2번
    assert calls[0]["messages"][-1]["images"]                                         # 첫 화면 이미지를 보냈다
    assert "FIRST FRAME" in calls[1]["messages"][-1]["content"] and "fine mist" in calls[1]["messages"][-1]["content"]
    assert result["crew"]["used"] and result["crew"]["category"] == "product" and len(result["crew"]["picked"]) == 5
    assert result["prompt"] == MOTION and result["violations"] == [] and result["frame"]["subject"] == FRAME["subject"]
    assert result["settings"]["frames"] == 81 and result["ko"]["camera"]


def test_render_same_model_reads_frame_in_one_turn():
    def go():
        return run_render([FRAME, GOOD], frame_model=settings.expand_model, username="alice", slug="alice-main",
                          text="향수 광고 영상", profile_id="wan22_i2v", image={"filename": "first.png"}, crew=True,
                          rng=random.Random(0))
    _, calls, turns = with_frame_folder(go)
    assert turns == [(settings.expand_model, "video_expand")] and len(calls) == 2


def test_render_without_frame_and_general_path():
    result, calls, turns = run_render([{"ko": KO, "prompt": MOTION}], username="alice", slug="alice-main",
                                      text="고양이가 창가에서 하품하는 영상", profile_id="wan22_i2v", crew=True)
    assert turns == [(settings.expand_model, "video_expand")] and len(calls) == 1
    assert result["crew"] is None and "choices" not in calls[0]["format"]["properties"]
    assert "Optional ideas" in calls[0]["messages"][-1]["content"]
    assert any("첫 화면 이미지를 찾지 못해" in a for a in result["advice"])


def test_render_falls_back_when_crew_keeps_failing():
    bad = dict(GOOD, prompt="The camera pushes in and then orbits around the bottle while mist drifts and light glides "
                            "across the glass before the motion settles into a calm final hold on the bottle.")
    result, calls, _ = run_render([bad, bad, {"ko": KO, "prompt": MOTION}], username="alice", slug="alice-main",
                                  text="향수 광고 영상", profile_id="wan22_i2v", image_prompt=SCENE, crew=True,
                                  rng=random.Random(0))
    assert len(calls) == 3 and result["crew"]["used"] is False and result["violations"] == []
    assert "choices" not in calls[2]["format"]["properties"]
    assert any("단계 메뉴 없이" in a for a in result["advice"])


def test_render_t2v_writes_the_scene_in_the_same_turn():
    expanded = {"prompt": ("a single luxury perfume bottle on a white marble table, soft window light from the left, "
                           "close-up shot at eye level with a 100mm lens, a pale cream backdrop, polished glass with crisp "
                           "reflections and brushed metal texture, calm elegant mood, fine dust floating in the light beam"),
                "keep": ["perfume bottle"],
                "categories": {"lighting": "soft window light from the left",
                               "camera": "close-up shot at eye level with a 100mm lens",
                               "environment": "a pale cream backdrop",
                               "texture": "polished glass with crisp reflections and brushed metal texture"}, "people": 0}
    result, calls, turns = run_render([expanded, {"ko": KO, "prompt": MOTION}], username="alice", slug="alice-main",
                                      text="향수 광고 영상", style="STYLE X", profile_id="wan22_t2v", crew=False)
    assert turns == [(settings.expand_model, "video_expand")] and len(calls) == 2      # 장면과 움직임이 한 차례
    assert result["scene"] == expanded["prompt"] and result["prompt"].startswith("a single luxury perfume bottle")
    assert result["prompt"].endswith("STYLE X") and MOTION.rstrip(".") in result["prompt"]
    assert "SCENE (fixed" in calls[1]["messages"][-1]["content"]


def test_render_caps_long_clips():
    result, _, _ = run_render([{"ko": KO, "prompt": MOTION}], username="alice", slug="alice-main", text="고양이 영상",
                              profile_id="wan22_i2v", seconds=12, crew=False)
    assert result["settings"]["capped"] and any("클립을 나눠" in a for a in result["advice"])


# ── 검토에서 나온 문제 (11-11) ───────────────────────────────────────
def test_camera_move_counter_needs_a_camera_subject():
    assert video.camera_moves("The camera moves in a small arc around the bottle.") == ["orbit"]     # 전치사 in 은 다가가기가 아니다
    assert video.camera_moves("Light pans across the glass while a truck passes and steam rises.") == []
    assert sorted(video.camera_moves("The camera pushes in and then orbits around the bottle.")) == ["orbit", "push"]
    assert video.allowed_moves("향수 광고 영상") == 1 and video.allowed_moves("향수 영상, 오빗으로 돌면서 줌인") == 2


def test_two_camera_words_can_pass():
    said = "바다 풍경 영상, 드론으로 줌인"
    menus, picked = menus_and_choice(said, "landscape", 2)
    ds = crew_lib.query(said).video_demands
    drone = next(d for d in ds if d["tag"].startswith("드론"))
    picked["camera"] = next(e["id"] for e in menus["camera"] if e["id"] in drone["ids"])
    text = ("The drone glides slowly forward and the camera zooms in on the waves as foam drifts toward the shore, "
            "light shimmers on the water and the shot settles into a calm hold.")
    probs = video.check_video({"choices": picked, "ko": KO, "prompt": text}, I2V, said, None, "", "landscape", menus)
    assert not any("must use an id" in p for p in probs), probs     # 카메라 칸은 드론이 썼으므로 줌인은 글로 지킨다


def test_people_and_hidden_hands_rules():
    person = "A woman turns her head slowly as the camera pushes in and her hair drifts in the breeze before a calm hold."
    assert not any("No person" in p for p in video.check_video({"ko": KO, "prompt": person + " The light shifts softly."},
                                                              T2V, "향수 광고 영상", None, "a woman holding a perfume bottle"))
    assert not any("No person" in p for p in video.check_video({"ko": KO, "prompt": person + " The light shifts softly."},
                                                              I2V, "향수 광고 영상", None, ""))   # 첫 화면을 안 읽었으면 모른다
    hold = MOTION   # 'a calm final hold' 는 손 낱말이 아니다
    assert not any("hidden" in p for p in video.check_video({"ko": KO, "prompt": hold}, I2V, "향수 광고 영상, 손은 소매 속에 숨김",
                                                            dict(FRAME, people=1), SCENE))


def test_bad_seconds_and_frame_failures_do_not_break():
    for bad in ("inf", "nan", 1e400, 0, -3):
        assert video.suggest_settings(I2V, bad)["frames"] == 81, bad

    def go():
        return run_render([GOOD], frame_model="vision-model", username="alice", slug="alice-main", text="향수 광고 영상",
                          profile_id="wan22_i2v", image={"filename": "missing.png"}, crew=True, rng=random.Random(0))
    result, calls, turns = with_frame_folder(go)
    assert len(turns) == 1 and result["prompt"] and any("첫 화면을 읽지 못해" in a for a in result["advice"])


def test_schema_puts_choices_first():
    menus = video.build_menus("향수 광고 영상", "product", random.Random(0))
    assert list(video.build_schema("product", menus)["properties"]) == ["choices", "ko", "prompt"]


# ── 장문 프로필 (러다이트식, 11-11) ─────────────────────────────────────
# 러다이트 Agent 는 한 줄 요청을 구도 → 카메라·빛 → 시간 순서 → 마무리 네 단락의 영어 상세 프롬프트로 바꿨다.
# 그런데 '1인칭' 프롬프트로 만든 영상이 어깨너머 샷으로 나왔다 (Directing_follow_for_comfyUI 02번 문서 E1-c).
I2V_LONG = video.profile_of("wan22_i2v_long")[1]
T2V_LONG = video.profile_of("wan22_t2v_long")[1]
PARTS = ("shot", "camera_light", "action", "ending")
LONG = {"ko": KO,
        "shot": ("Live-action, cinematic close-up product shot at eye level, composed as one continuous shot. A single glass "
                 "perfume bottle with a faceted body and a brushed gold cap stands slightly left of centre on a smooth white "
                 "marble table, its clear liquid holding a pale amber tint. Behind it, a pale cream backdrop falls softly out "
                 "of focus, and a thin beam of window light crosses the table from the left. A 100mm macro perspective keeps "
                 "the bottle and the veining of the marble crisp while the background melts into creamy tones, so the bottle "
                 "reads first, the beam of light second and the backdrop third."),
        "camera_light": ("The camera pushes in slowly and steadily toward the bottle for the whole clip, keeping the same "
                         "height and a level horizon. Soft morning daylight enters from a tall window on the left, wrapping the "
                         "faceted glass in a clean highlight, drawing a bright rim along the gold cap and laying a long, soft "
                         "shadow to the right across the marble. A faint warm bounce from the right lifts the shadow side of the "
                         "bottle. A restrained film look with gentle highlight roll-off, fine grain, soft tonal separation and a "
                         "warm, neutral colour grade keeps detail in both the bright glass and the deep amber liquid."),
        "action": ("At first the bottle sits perfectly still while fine dust motes drift slowly through the beam of light and "
                   "the liquid holds a small, steady reflection of the window. Then a faint mist rises from the base of the "
                   "bottle, curls gently upward along the glass and catches the backlight as a pale, glowing veil, moving "
                   "slowly enough that each curl stays readable. As the camera comes closer, a soft band of reflected light "
                   "glides across the facets from left to right, lighting each edge in turn, and the amber liquid glows a "
                   "little warmer where the light passes through it."),
        "ending": ("Finally the mist thins into a faint veil around the base, the band of light comes to rest on the gold cap, "
                   "and the camera settles into a calm, closer framing of the bottle against the softly glowing backdrop. The "
                   "dust keeps drifting quietly through the beam. The bottle keeps its exact shape, proportions, facets and "
                   "polished glass surface throughout, the gold cap and the marble veining stay fixed in place, and every "
                   "reflection stays attached to the glass. The final frame holds the same calm, premium feeling, with the "
                   "light a touch warmer than at the start.")}
LONG_I2V = {"ko": KO,
            "shot": ("A close-up shot at eye level, composed as one continuous shot, holds the glass perfume bottle at the "
                     "centre of the frame on the marble table exactly as the first frame shows it. The softly blurred backdrop "
                     "sits behind it, the bottle reads first and the gentle beam of light second, and the same lens "
                     "perspective and depth of field stay in place for the whole clip."),
            "camera_light": ("The camera pushes in slowly and steadily toward the bottle for the whole clip, keeping the same "
                             "height and a level horizon. The soft light from the left stays steady, holding the clean "
                             "highlight on the glass, the bright rim along the cap and the long, soft shadow on the table, "
                             "with the calm, warm colour and the fine texture of the first frame."),
            "action": ("At first the bottle sits perfectly still while fine mist hangs in the air around it and a small "
                       "reflection rests on the glass. Then the mist drifts slowly upward along the glass, curls once near the "
                       "shoulder of the bottle and catches the light as a pale veil. As the push continues, a soft band of "
                       "reflected light glides across the curved glass from left to right, lighting each edge in turn."),
            "ending": ("Finally the mist thins into a faint veil around the base and the camera settles into a calm, closer "
                       "framing of the bottle. The bottle keeps its exact shape, proportions and glass surface, the cap and "
                       "the table stay fixed, every reflection stays on the glass, and the final frame holds the same calm, "
                       "warm feeling as the first frame.")}
PILOT = "비행기를 운항하는 파일럿의 1인칭시점을 영상으로 만들어줘"
POV_KO = {"camera": "카메라가 조종석 눈높이에 고정됩니다.", "motion": "손이 요크를 살짝 돌려 기체가 기웁니다.",
          "timing": "처음엔 고요하다가 마지막에 다시 수평으로 돌아옵니다."}
POV = {"ko": POV_KO,
       "shot": ("First-person POV from the pilot seat at the eye position of the person flying, live-action and cinematic, "
                "composed as one continuous shot. The broad windshield of a small generic aircraft fills most of the frame "
                "with open sky and a calm, distant horizon, while the curved windshield frame and the upper edge of a "
                "restrained generic instrument panel define the sides and the bottom. Through the glass, a soft layer of "
                "clouds lies far below. A natural wide perspective keeps the near cockpit and the far horizon clear together, "
                "so the view ahead reads first, the cockpit second and the controls third."),
       "camera_light": ("The camera stays locked at the same seated eye level for the whole clip, a static first-person "
                        "frame that moves only with the aircraft. Soft afternoon daylight pours through the windshield from "
                        "ahead and slightly right, laying gentle highlights along the glazing and the frame while the matte "
                        "panel keeps a deep, even tone. A clean, restrained film look with soft highlight roll-off, gentle "
                        "tonal separation and a cool, neutral grade keeps detail in both the bright sky and the dark cockpit, "
                        "and a faint atmospheric haze softens the horizon."),
       "action": ("At first the aircraft flies steady and level as the clouds drift slowly past far below and the horizon "
                  "rests still against the windshield frame. Then the pilot's own gloved hands enter from the bottom edge of "
                  "the frame, settle on a generic control yoke and ease it gently to the left in one small, deliberate "
                  "correction. The yoke follows the input, the aircraft answers with a smooth, shallow bank, and the horizon "
                  "tilts gradually across the windshield while the cockpit stays steady relative to the viewer, giving a "
                  "clear, readable reaction to the movement of the hands."),
       "ending": ("Finally the hands ease the yoke back toward centre in one continuous recovery, the bank fades, and the "
                  "horizon settles level again in a slightly new direction. The clip ends on a calm first-person view of the "
                  "open sky ahead, with the clouds still drifting quietly far below. The windshield frame, the panel and the "
                  "hands keep their shape, size and position throughout, the reflections stay on the glass, and the final "
                  "frame holds the same quiet, attentive feeling as the start, with the small correction complete.")}


def test_long_profiles_come_first_and_are_flagged():
    ps = video.profile_list()
    assert [p["id"] for p in ps[:2]] == ["wan22_i2v_long", "wan22_t2v_long"] and all(p["long"] for p in ps[:2])
    assert not any(p["long"] for p in ps if p["id"] in ("wan22_i2v", "wan22_t2v"))      # 예전 방식은 그대로 남는다
    assert video.profile_of(None)[0] == "wan22_i2v_long"                                    # 프로필을 안 고르면 장문
    spec = video.long_spec()
    assert [p["key"] for p in spec["parts"]] == list(PARTS) and set(spec["modes"]) == {"i2v", "t2v"} and spec["rules"]
    for p in (I2V_LONG, T2V_LONG):   # 장문 길이도 단락 최소 길이 네 개보다 길어야 한다
        assert p["words"][0] >= 4 * spec["part_min_words"]


def test_long_schema_takes_four_parts_in_order():
    s = video.build_schema(long=True)
    assert list(s["properties"]) == ["ko", *PARTS] and "prompt" not in s["properties"]
    assert s["required"] == ["ko", *PARTS]
    menus = video.build_menus("향수 광고 영상", "product", random.Random(0))
    assert list(video.build_schema("product", menus, long=True)["properties"]) == ["choices", "ko", *PARTS]
    joined = video.assemble(dict(LONG, shot="A close-up\nshot  at eye level."))
    assert joined.split("\n\n")[0] == "A close-up shot at eye level." and joined.count("\n\n") == 3   # 단락 순서·줄바꿈 정리
    assert video.assemble("broken") == "" and video.assemble({"ko": KO}) == ""


def test_long_messages_carry_the_four_part_plan():
    sys_msg, user_msg = video.build_messages("향수 광고 영상", "", T2V_LONG, 5.0, scene=SCENE, style="STYLE X")
    for key in PARTS:
        assert f'"{key}"' in sys_msg["content"]
    assert "320 to 600 words" in sys_msg["content"] and "aim for about 410" in sys_msg["content"] and "quality buzzwords" in sys_msg["content"]
    assert "EXAMPLE of the length" in sys_msg["content"] and "ceramic teacup" in sys_msg["content"]   # 러다이트 밀도 예시
    assert "Model: Wan 2.2 T2V 14B." in sys_msg["content"] and "러다이트" not in sys_msg["content"]  # Luddite 로 읽히지 않게
    assert "LOOK REFERENCE" in user_msg["content"] and "do not restate" not in user_msg["content"]
    assert "STYLE (appended after your text" in user_msg["content"] and "STYLE X" in user_msg["content"]
    sys_i2v, user_msg = video.build_messages("향수 광고 영상", "", I2V_LONG, 5.0, FRAME, SCENE, style="STYLE X")
    assert "never contradict" in user_msg["content"] and "FIRST FRAME" in user_msg["content"]
    assert "EXAMPLE of the length" not in sys_i2v["content"] and "STYLE (" not in user_msg["content"]
    assert "FIRST FRAME notes" in sys_i2v["content"] and "250 to 450 words" in sys_i2v["content"]
    # 예전 프로필의 지시문 구조는 그대로다
    sys_old, user_old = video.build_messages("향수 광고 영상", "", I2V, 5.0, FRAME, SCENE)
    assert '"prompt": one English paragraph of 25 to 90 words' in sys_old["content"] and "do not restate" in user_old["content"]


def test_long_check_passes_and_may_reuse_the_look():
    assert video.check_video(LONG, T2V_LONG, "향수 광고 영상", None, SCENE) == []
    reuse = dict(LONG, shot="A single elegant glass perfume bottle with a gold cap standing centered on a smooth white marble "
                            "surface, seen in a live-action, cinematic close-up product shot at eye level and composed as one "
                            "continuous shot, with a pale cream backdrop falling softly out of focus behind it, so the bottle "
                            "reads first, the beam of window light second and the backdrop third.")
    assert video.check_video(reuse, T2V_LONG, "향수 광고 영상", None, SCENE) == []          # 장문은 장면 글을 다시 써도 된다
    assert video.check_video(LONG_I2V, I2V_LONG, "향수 광고 영상", FRAME, SCENE) == []
    assert 300 <= len(video.assemble(LONG).split()) <= 450                                # 러다이트처럼 단락마다 약 100낱말


def test_long_check_rules():
    def probs(**change):
        return video.check_video(dict(LONG, **change), T2V_LONG, "향수 광고 영상", None, SCENE)

    assert any('"ending" has 4 words' in p for p in probs(ending="The clip ends calmly."))
    assert any("quality buzzwords" in p and "8k" in p and "masterpiece" in p
               for p in probs(camera_light=LONG["camera_light"] + " Masterpiece, 8k, ultra detailed."))
    assert any("one main camera move" in p
               for p in probs(action=LONG["action"] + " Then the camera orbits around the bottle in a wide arc."))
    assert any("Negative wording" in p for p in probs(ending=LONG["ending"] + " The glass stays clean without any smudge."))
    assert any("text, letters" in p for p in probs(shot=LONG["shot"] + " A small label faces the camera."))
    long_ish = dict(LONG, action=LONG["action"] + " " + LONG["action"])                 # 450낱말을 넘기면 줄이라고 한다
    over = [p for p in video.check_video(long_ish, I2V_LONG, "향수 광고 영상", FRAME, SCENE) if "words; write 250 to 450" in p]
    assert over and "Cut it to about 350 words in total, about 87 words per part" in over[0] and '"action" ' in over[0]
    assert not any("words; write" in p for p in video.check_video(LONG, I2V_LONG, "향수 광고 영상", FRAME, SCENE))   # 410낱말은 통과
    missing = video.check_video({"ko": KO, "prompt": MOTION}, T2V_LONG, "향수 광고 영상", None, SCENE)
    assert sum('has 0 words' in p for p in missing) == 4 and missing[-1] == '"prompt" is empty.'
    assert video.check_video([], T2V_LONG, "x") == ["Return a JSON object with ko and the four parts."]
    # 장문에는 prompt 칸이 없으므로 고칠 단락 이름을 알려 준다
    assert '"action" must be English only.' in probs(action=LONG["action"] + " 천천히")
    # 사용자가 직접 쓴 4K 는 막지 않고, 하이픈 유행어는 잡는다
    assert not any("buzzwords" in p for p in video.check_video(
        dict(LONG, camera_light=LONG["camera_light"] + " The image stays crisp in 4K."), T2V_LONG, "향수 광고 영상 4K", None, SCENE))
    assert any("best-quality" in p for p in probs(camera_light=LONG["camera_light"] + " A best-quality finish."))


def test_pov_request_is_a_user_word_and_allows_own_hands():
    pov = [d for d in crew_lib.query(PILOT).video_demands if video.POV_TERM in d["terms"]]
    assert pov and video.pov_wanted(PILOT) and video.pov_wanted("비행기를 운항하는 파일럿의 1인칭 시점")
    assert video.pov_wanted("자전거 POV 영상") and not video.pov_wanted("그 시점에 비가 왔다")
    assert video.people_ok(PILOT)                     # '파일럿' 은 사람 말 사전에 없지만 1인칭이면 보는 사람이 있다
    user = video.build_messages(PILOT, "", T2V_LONG, 5.0)[1]["content"]
    assert "first-person view" in user and "No person appears" not in user
    assert video.check_video(POV, T2V_LONG, PILOT, None, "") == []
    # 예전(짧은) 프로필에서도 1인칭 말과 자기 손은 지켜진다
    short = ("The camera stays locked at eye level in a first-person view as the pilot's hands ease the yoke left, the "
             "horizon tilts gently across the windshield and settles level again in a calm hold.")
    assert video.check_video({"ko": POV_KO, "prompt": short}, T2V, PILOT, None, "a cockpit seen from the pilot seat") == []


def test_pov_blocks_over_the_shoulder_and_needs_the_word():
    def probs(**change):
        return video.check_video(dict(POV, **change), T2V_LONG, PILOT, None, "")

    shoulder = ("An over-the-shoulder view behind the pilot shows the broad windshield of a small generic aircraft with "
                "open sky and a distant horizon filling most of the frame above a generic instrument panel.")
    assert any("first-person view" in p and "over-the-shoulder" in p for p in probs(shot=shoulder))
    head = POV["shot"] + " The pilot's head and headset sit at the left edge of the frame."
    assert any("first-person view" in p for p in probs(shot=head))
    plain = {k: v.replace("First-person POV from", "A view from").replace("first-person ", "")
             for k, v in POV.items() if k in PARTS}
    assert any("clearly show" in p and "first-person" in p for p in probs(**plain))
    # 1인칭 요청이 아니면 어깨너머 구도도 괜찮다
    assert not any("first-person" in p
                   for p in video.check_video(dict(POV, shot=shoulder), T2V_LONG, "비행기 조종석 영상", None, ""))


def test_compose_long_keeps_paragraphs():
    body = video.assemble(LONG)
    assert video.compose("t2v", SCENE, body, "STYLE X", long=True) == body + "\n\nSTYLE X"   # 장면을 앞에 다시 붙이지 않는다
    assert video.compose("i2v", SCENE, body, "STYLE X", long=True) == body                  # 그림체는 첫 화면이 정한다
    assert video.compose("t2v", SCENE, "", "STYLE X", long=True) == ""                       # 빈 결과에 그림체만 남기지 않는다


def test_draft_long_assembles_and_repairs():
    short = dict(LONG, ending="The clip ends calmly.")
    call = Call([short, LONG])
    result, rounds, left = asyncio.run(video.draft(call, "향수 광고 영상", "", T2V_LONG, 5.0, None, SCENE, repair_rounds=1))
    assert left == [] and rounds == 2 and result["prompt"] == video.assemble(LONG)
    assert '"ending" has 4 words' in call.calls[1]["messages"][-1]["content"]
    assert set(PARTS) <= set(call.calls[0]["format"]["properties"])


def test_render_long_t2v_uses_the_scene_as_look_reference():
    expanded = {"prompt": ("a single luxury perfume bottle on a white marble table, soft window light from the left, "
                           "close-up shot at eye level with a 100mm lens, a pale cream backdrop, polished glass with crisp "
                           "reflections and brushed metal texture, calm elegant mood, fine dust floating in the light beam"),
                "keep": ["perfume bottle"],
                "categories": {"lighting": "soft window light from the left",
                               "camera": "close-up shot at eye level with a 100mm lens",
                               "environment": "a pale cream backdrop",
                               "texture": "polished glass with crisp reflections and brushed metal texture"}, "people": 0}
    result, calls, turns = run_render([expanded, LONG], username="alice", slug="alice-main", text="향수 광고 영상",
                                      style="STYLE X", profile_id="wan22_t2v_long", crew=False)
    assert turns == [(settings.expand_model, "video_expand")] and len(calls) == 2         # 장면과 장문이 한 차례
    assert "LOOK REFERENCE" in calls[1]["messages"][-1]["content"]
    assert result["long"] and result["violations"] == [] and result["parts"]["shot"] == LONG["shot"]
    assert result["prompt"] == video.assemble(LONG) + "\n\nSTYLE X"
    assert result["prompt"].count("\n\n") == 4 and not result["prompt"].startswith(SCENE)


def test_render_long_i2v_reads_frame_and_uses_crew_menus():
    def go():
        return run_render([FRAME, dict(LONG_I2V, fit_menu=True)], frame_model="vision-model", username="alice",
                          slug="alice-main", text="향수 광고 영상", style="STYLE X", profile_id="wan22_i2v_long",
                          image={"filename": "first.png"}, image_prompt=SCENE, crew=True, rng=random.Random(0))
    result, calls, turns = with_frame_folder(go)
    assert turns == [("vision-model", "video_frame"), (settings.expand_model, "video_expand")]
    assert "FIRST FRAME" in calls[1]["messages"][-1]["content"] and "never contradict" in calls[1]["messages"][-1]["content"]
    assert list(calls[1]["format"]["properties"]) == ["choices", "ko", *PARTS]
    assert result["crew"]["used"] and result["violations"] == [] and result["long"]
    assert result["prompt"] == video.assemble(LONG_I2V)                                    # 그림체 문장을 붙이지 않는다


# ── 장문 검토에서 나온 문제 (11-11, 검토자 4명 + 검증자 16명) ─────────────────────────
def test_t2v_long_enforces_the_users_look_words():
    # T2V 장문은 장면 글을 앞에 붙이지 않으므로 사용자의 장르·색·조명·분위기 말이 장문 안에 있어야 한다
    said = "네온 조명의 사이버펑크 거리, 비 오는 밤, 천천히"
    tags = {d["tag"] for d in crew_lib.query(said).demands}
    assert "사이버펑크" in tags
    user = video.build_messages(said, "", T2V_LONG, 5.0, scene="a cyberpunk street")[1]["content"]
    assert "'사이버펑크'" in user and "look already follows" not in user
    drift = video.check_video(LONG, T2V_LONG, said, None, "a cyberpunk street at night in the rain")
    assert any("'사이버펑크'" in p for p in drift)
    # 예전 T2V 는 장면 글이 앞에 붙으므로 그대로 (장르 말을 움직임 글에서 묻지 않는다)
    old = video.check_video({"ko": KO, "prompt": MOTION}, T2V, said, None, "a cyberpunk street at night in the rain")
    assert not any("'사이버펑크'" in p for p in old)
    assert "look already follows" in video.build_messages(said, "", I2V_LONG, 5.0, FRAME, SCENE)[1]["content"]


def test_i2v_long_falls_back_to_motion_only_without_frame_or_reference():
    result, calls, _ = run_render([{"ko": KO, "prompt": MOTION}], username="alice", slug="alice-main",
                                  text="고양이가 창가에서 하품하는 영상", crew=False)   # 프로필을 안 고르면 장문 I2V
    assert result["profile"] == "wan22_i2v" and result["long"] is False and "prompt" in calls[0]["format"]["properties"]
    assert any("장문 대신 움직임만" in a for a in result["advice"])
    # 도우미 결과(참고 글)가 있으면 첫 화면을 못 읽어도 장문으로 쓴다
    result, calls, _ = run_render([LONG_I2V], username="alice", slug="alice-main", text="향수 광고 영상",
                                  image_prompt=SCENE, crew=False)
    assert result["profile"] == "wan22_i2v_long" and result["long"] and result["prompt"] == video.assemble(LONG_I2V)
    # 첫 화면을 읽으면 빛·색도 메모로 넘긴다 (장문이 지어내지 않게)
    f = video.parse_frame(dict(FRAME, light="soft window light from the left", colours="warm cream and gold"))
    text = video.frame_text(f)
    assert "- light: soft window light from the left" in text and "- colours: warm cream and gold" in text
    assert video.frame_known(f) and not video.frame_known(video.parse_frame({})) and not video.frame_known(None)


def test_pov_detection_covers_common_phrasings():
    for said in ("1인칭뷰로 자전거 타는 영상", "조종사 시점으로 비행하는 영상", "1인칭으로만 찍어줘", "운전석 시점 드라이브",
                 "point-of-view shot of a skier", "내 시점에서 보는 바다"):
        assert video.pov_wanted(said), said
    for said in ("일주일 이내 시점의 뉴스", "그 시점에 비가 왔다", "실내 시점 조명", "비행기 조종석 영상"):
        assert not video.pov_wanted(said), said


# ── 11-16 파일럿 1인칭 요청: 앞 두 단락은 조종석, 뒤 두 단락은 길이 예시의 찻잔을 베낀 영상이 나왔다 ──
PILOT_SAID = "비행기를 운항하는 파일럿의 1인칭 시점 영상으로 만들어줘"
PILOT_SCENE = "A first-person view from an airliner cockpit looking out the windshield at clouds, instruments and the control yoke"
PILOT_KO = {"camera": "카메라는 조종석 시점에서 수평으로 천천히 앞으로 나아갑니다.", "motion": "조종사의 손이 조종간을 잡고 작게 움직입니다.",
            "timing": "5초 동안 일정한 속도로 진행됩니다."}
PILOT_GOOD = {
    "ko": PILOT_KO,
    "shot": ("Live-action, cinematic first-person POV from the pilot's seat inside an airliner cockpit, composed as one "
             "continuous shot. The view looks straight ahead through the wide curved windshield at a bright horizon above a "
             "sea of white clouds, while the dark glare shield and the glowing instrument displays fill the lower frame. The "
             "viewer's own hands in thin black gloves rest on the control yoke at the bottom edge of the frame, the leather "
             "of the gloves and the matte plastic of the yoke clearly visible. A 35mm perspective keeps the yoke and the "
             "nearest displays crisp while the clouds fall into a soft blur, so the horizon reads first, the displays second "
             "and the hands on the yoke third."),
    "camera_light": ("The camera holds the pilot's eye position and eases forward in one slow, steady push toward the "
                     "windshield, keeping the same height and a level horizon. Warm sunrise light enters from the right of the "
                     "windshield, drawing a thin golden rim along the top of the yoke and the knuckles of the gloves and laying "
                     "soft, low shadows across the glare shield, while cool blue light from the displays lifts the shadow side "
                     "of the panel. A restrained film look with gentle highlight roll-off, fine grain and a warm-to-cool "
                     "neutral grade keeps detail in both the bright clouds and the dark instrument panel."),
    "action": ("At first the airliner flies level and almost still: the horizon stays straight and the clouds below drift "
               "very slowly toward the windshield. Then the gloved hands tighten on the yoke and ease it a fraction forward, "
               "and the horizon tilts by a degree while the cloud tops slide past the lower edge of the glass. As the push "
               "continues, a thin thread of sunlight glides across the glare shield and the displays pulse softly, and the "
               "hands settle back into a steady grip as the aircraft levels again."),
    "ending": ("Finally the small movements fade, the horizon returns to a perfectly level line and the camera comes to rest "
               "on a calm, slightly closer view through the windshield, with the clouds still drifting faintly below. The "
               "yoke keeps its exact shape, the gloves keep their position at the bottom edge, the glare shield and the "
               "instrument displays stay fixed in place, and the light on the cloud tops stays unchanged. The final frame "
               "holds the same calm, high-altitude feeling, the light a touch warmer than at the start.")}
# 손 낱말을 모두 뺀 같은 글
PILOT_NO_HANDS = dict(PILOT_GOOD)
PILOT_NO_HANDS["shot"] = PILOT_GOOD["shot"].replace(
    "The viewer's own hands in thin black gloves rest on the control yoke at the bottom edge of the frame, the leather of the "
    "gloves and the matte plastic of the yoke clearly visible.", "The control yoke sits at the bottom edge of the frame in matte black plastic.")
PILOT_NO_HANDS["shot"] = PILOT_NO_HANDS["shot"].replace(" and the hands on the yoke third", " and the yoke third")
PILOT_NO_HANDS["camera_light"] = PILOT_GOOD["camera_light"].replace(" and the knuckles of the gloves", "")
PILOT_NO_HANDS["action"] = PILOT_GOOD["action"].replace(
    "Then the gloved hands tighten on the yoke and ease it a fraction forward, and the horizon tilts", "Then the horizon tilts").replace(
    ", and the hands settle back into a steady grip as the aircraft levels again", ", and the aircraft levels again")
PILOT_NO_HANDS["ending"] = PILOT_GOOD["ending"].replace("the gloves keep their position at the bottom edge, ", "")
# 잘못 나온 글: 조종석(앞)과 길이 예시의 찻잔(뒤)
PILOT_BAD = dict(PILOT_GOOD)
EX = video.long_spec()["example"]
PILOT_BAD["camera_light"] = PILOT_GOOD["camera_light"].split(". ")[0] + ". " + EX["camera_light"].split(". ", 1)[1]
PILOT_BAD["action"] = EX["action"]
PILOT_BAD["ending"] = EX["ending"]


def test_t2v_long_rejects_a_copy_of_the_length_example():
    probs = video.check_video(PILOT_BAD, T2V_LONG, PILOT_SAID, None, PILOT_SCENE)
    for key in ("camera_light", "action", "ending"):
        assert any(f'"{key}" copies the EXAMPLE' in p for p in probs), (key, probs)
    assert any('"action" describes a different scene from "shot"' in p for p in probs)
    assert any('"ending" describes a different scene from "shot"' in p for p in probs)
    # 요청이 정말 찻잔·김이면 같은 낱말도 허용한다 (장면 글에 있다)
    def reworded(text):   # 문장을 그대로 베끼지 않은 같은 장면 (낱말 12개 연속 일치를 끊는다)
        out = []
        for i, w in enumerate(text.split()):
            out.append(w)
            if i % 6 == 5:
                out.append("really")
        return " ".join(out)
    tea_parts = {**PILOT_BAD, **{k: reworded(PILOT_BAD[k]) for k in ("camera_light", "action", "ending")}}
    teacup = video.check_video(tea_parts, T2V_LONG, "창가 찻잔에서 김이 오르는 영상",
                               None, "A teacup on a windowsill with steam rising and branches outside the window")
    assert not any("copies the EXAMPLE" in p for p in teacup), teacup
    # 같은 글을 조종석 요청에서 보면 낱말이 요청에 없으니 걸린다
    assert any("copies the EXAMPLE" in p for p in video.check_video(tea_parts, T2V_LONG, PILOT_SAID, None, PILOT_SCENE))
    # 올바른 조종석 글, 구름·골목 같은 정상 글은 걸리지 않는다
    assert video.check_video(PILOT_GOOD, T2V_LONG, PILOT_SAID, None, PILOT_SCENE) == []
    assert not video.part_consistency_problems(LONG) and not video.example_copy_problems(LONG, "향수 광고 영상", SCENE)
    # 단락이 짧은 보통 프로필도 겹치는 낱말이 있으면 통과한다 (예시를 주지 않는 프로필은 베끼기 검사도 하지 않는다)
    mid = {"ko": KO, **{k: ". ".join(LONG[k].split(". ")[:2]).rstrip(".") + "." for k in PARTS}}
    assert video.part_consistency_problems(mid) == []
    # 예시 머리말은 예시의 명사를 쓰지 말라고 알려 준다
    user_msgs = video.build_messages(PILOT_SAID, "", T2V_LONG, 5.0, scene=PILOT_SCENE)
    assert "teacup" in user_msgs[0]["content"] and "never use its nouns" in user_msgs[0]["content"]


def test_pov_operating_request_shows_the_viewers_hands_on_the_controls():
    assert video.operating_pov(PILOT_SAID) and video.operating_pov("자동차 운전석 1인칭 시점 주행 영상")
    # 드론 FPV·그냥 걷는 1인칭·손 숨김 요청은 해당하지 않는다
    assert not video.operating_pov("1인칭 드론 비행 영상") and not video.operating_pov("1인칭으로 걷는 영상")
    assert not video.operating_pov("파일럿 1인칭 시점, 손은 보이지 않게") and not video.operating_pov("비행기 조종석 영상")
    msgs = video.build_messages(PILOT_SAID, "", T2V_LONG, 5.0, scene=PILOT_SCENE)
    assert "operating something" in msgs[1]["content"] and "yoke, wheel, handlebars" in msgs[1]["content"]
    assert "operating something" not in video.build_messages("1인칭으로 걷는 영상", "", T2V_LONG, 5.0)[1]["content"]
    probs = video.check_video(PILOT_NO_HANDS, T2V_LONG, PILOT_SAID, None, PILOT_SCENE)
    assert any("first-person view of operating" in p for p in probs), probs
    assert not any("first-person view of operating" in p for p in video.check_video(PILOT_GOOD, T2V_LONG, PILOT_SAID, None, PILOT_SCENE))
    # 조종하지 않는 1인칭 요청에는 손을 요구하지 않는다
    walk = video.check_video(PILOT_NO_HANDS, T2V_LONG, "1인칭 시점으로 숲길을 걷는 영상", None, PILOT_SCENE)
    assert not any("first-person view of operating" in p for p in walk)


def test_extra_aircraft_and_crew_are_flagged_when_the_request_names_none():
    # 11-16 재시험 a7ddf778 후속 80fdf0b6: 글에 'distant co-pilot's silhouette' 가 들어 있었고 영상 창밖에는 비행기가 한 대 더 나왔다
    said = "비행기를 운항하는 파일럿의 1인칭 시점 영상으로 만들어줘"
    p = video.extra_subject_problems("keeping the control surfaces and the distant co-pilot’s silhouette sharp", said)
    assert p and "co-pilot" in p[0] and "only clouds and open sky" in p[0]
    assert video.extra_subject_problems("another aircraft passes far off beyond the windshield", said)
    assert video.extra_subject_problems("a thin contrail crosses the sky", said)
    # 요청이 그것을 이름 붙였으면 허용한다
    assert not video.extra_subject_problems("another aircraft passes far off", "다른 비행기가 지나가는 영상")
    assert not video.extra_subject_problems("a co-pilot sits beside", "부기장과 함께 비행하는 파일럿 1인칭 시점")
    # 조종하는 1인칭이 아니면 부기장·승객을 막지 않는다 (승객 이야기일 수 있다)
    assert not video.extra_subject_problems("a passenger looks out of the window", "비행기 안 풍경 영상")
    # 정상 조종석 글은 걸리지 않고, check_video 에도 들어간다
    assert not video.extra_subject_problems(video.assemble(PILOT_GOOD), said)
    bad = dict(PILOT_GOOD, shot=PILOT_GOOD["shot"].replace("The view looks straight ahead", "The distant co-pilot is a silhouette as the view looks straight ahead"))
    assert any("co-pilot" in x for x in video.check_video(bad, T2V_LONG, said, None, PILOT_SCENE))
    # 규칙은 모델에게도 알려 준다: 창 너머에 보이는 것은 그것뿐이라고 긍정 문장으로 쓴다
    sys_msg = video.build_messages(said, "", T2V_LONG, 5.0, scene=PILOT_SCENE)[0]["content"]
    assert "holds only those things" in sys_msg and "Never add vehicles, aircraft" in sys_msg


# ── 11-16 글→영상 장문 비교 평가: 'walks away from the camera' 와 'camera moves backward ... constant distance' 가 한 글에 있었다 ──
WALK_BAD = ("A solitary man in a dark trench coat walks away from the camera down a rain-slicked street. The camera moves "
            "backward in one steady, continuous pull, keeping the same height and a level horizon while maintaining a constant "
            "distance from the subject. Then he begins to stride forward, his feet splashing through puddles. Finally the man's "
            "stride slows slightly as he moves deeper into the distance, and the camera eases to a gentle stop.")
WALK_FIXED = ("A solitary man in a dark trench coat walks away from the camera down a rain-slicked street. The camera follows "
              "behind him, moving forward in one steady dolly at his walking pace, keeping the same height, a level horizon and "
              "a constant distance. From the first frame he is already mid-stride, his feet splashing through puddles. Finally "
              "the camera settles while he keeps walking at the same pace, the rain falling steadily.")


def test_camera_and_subject_directions_must_agree():
    # 인물은 멀어지는데 카메라도 뒤로 물러나며 같은 거리를 지킨다는 글은 위반이다
    p = video.blocking_problems(WALK_BAD)
    assert len(p) == 1 and "walks away from the camera" in p[0] and "camera moves backward" in p[0] and "constant distance" in p[0]
    assert "follows behind" in p[0] or "forward behind the subject" in p[0]
    assert video.blocking_problems(WALK_FIXED) == []                                   # 카메라가 뒤에서 앞으로 따라가면 맞다
    # 다가오는 인물 + 카메라가 앞으로, 서 있거나 옆으로만 가는 카메라 + 같은 거리도 위반이다
    assert video.blocking_problems("A girl walks toward the camera while the camera pushes forward, keeping a constant distance.")
    assert not video.blocking_problems("A girl walks toward the camera while the camera moves backward at the same pace, keeping a constant distance.")
    assert video.blocking_problems("A man walks away from the camera. The camera stays locked off, keeping a constant distance from him.")
    # 거리를 지킨다는 말이 없으면 크기가 바뀌는 연출이라 보지 않는다
    assert not video.blocking_problems("A man walks away from the camera and grows smaller in the frame. The camera stays locked off.")
    # 러다이트 1번처럼 옆으로 나란히 따라가는 글, 안개·김의 drift 는 걸리지 않는다
    side = ("A solitary man walks from screen-left toward screen-right. The camera makes a smooth lateral tracking shot at a "
            "matched pace, maintaining a constant distance, stable figure size and level horizon.")
    assert video.blocking_problems(side) == []
    assert video.blocking_problems("The camera pushes in slowly. Thin mist drifts away from the bottle and steam moves toward "
                                   "the window, keeping a constant distance between the cups.") == []


def test_travel_keeps_one_steady_pace():
    p = video.travel_problems(WALK_BAD, "비 오는 거리를 걷는 남자", "t2v")
    assert len(p) == 1 and "begins to stride" in p[0] and "stride slows" in p[0]
    assert video.travel_problems(WALK_FIXED, "비 오는 거리를 걷는 남자", "t2v") == []
    # I2V 는 서 있는 첫 화면에서 걷기 시작해도 되므로 시작은 보지 않는다. 느려져 멈추는 것은 본다
    assert video.travel_problems("Then he begins to walk down the street.", "", "i2v") == []
    assert video.travel_problems("Then he begins to walk. Finally his stride slows.", "", "i2v")
    # 사용자가 출발이나 멈춤을 말했으면 막지 않는다
    assert video.travel_problems(WALK_BAD, "걷다가 멈춰서 위를 보는 남자", "t2v") == []
    assert video.travel_problems("The man stops at the crossing.", "", "t2v")
    assert video.travel_problems("The camera eases to a gentle stop while the rain keeps falling.", "", "t2v") == []


def test_direction_and_travel_rules_are_in_check_video_and_the_prompt():
    bad = dict(LONG, action=WALK_BAD)
    probs = video.check_video(bad, T2V_LONG, "비 오는 거리를 걷는 남자", None, SCENE)
    assert any(x.startswith("Camera and subject directions disagree") for x in probs)
    assert any(x.startswith("Travel keeps one steady pace") for x in probs)
    assert not any(x.startswith(("Camera and subject", "Travel keeps")) for x in video.check_video(LONG, T2V_LONG, "향수 광고 영상", None, SCENE))
    ko = video.explain_ko([x for x in probs if x.startswith(("Camera and subject", "Travel keeps"))])
    assert "방향이 서로 맞지 않습니다" in ko[0] and "같은 속도" in ko[1]
    rules = video.build_messages("남자가 걷는 영상", "", T2V_LONG, 5.0, scene=SCENE)[0]["content"]
    assert "Never combine a subject walking away with a camera moving backward" in rules and "never write that it begins to move" in rules


# ── 11-16 구체성: 러다이트 1번 글은 카메라 항목, 깊이별 비, 걸음의 물리 묘사를 모두 적었고 우리 3번 글은 한 줄이었다 ──
LUDYTE_CAMERA = ("The camera makes a smooth, eye-level lateral Tracking Shot at a matched pace, maintaining a constant distance, "
                 "stable figure size, and level horizon as the facades pass in natural parallax.")
LUDYTE_GAIT = ("Each shoe plants, takes his weight, and releases as the other leg swings forward; his torso follows with a small "
               "natural rise and fall, and his arms counter-swing modestly. The dark coat's hem trails slightly, then settles.")
LUDYTE_RAIN = ("Rain moves at different apparent depths, visible against shadowed building surfaces, while droplets stipple the "
               "pavement and make small localized ripples.")
FLAT_RAIN = ("Heavy rain falls in visible streaks, creating a glossy sheen on the ground. The rain continues to fall in visible, "
             "diagonal streaks, blurring the background slightly.")


def test_camera_sheet_needs_height_and_what_stays_constant():
    assert video.camera_sheet_problems({"camera_light": LUDYTE_CAMERA}) == []
    # 높이는 shot 단락에 있어도 된다 (기존 글: 'close-up shot at eye level' + 'keeping the same height and a level horizon')
    assert video.camera_sheet_problems(LONG) == [] and video.camera_sheet_problems(PILOT_GOOD) == []
    bare = {"shot": "A man walks down a street.", "camera_light": "The camera moves backward in one steady pull at a gentle pace."}
    p = video.camera_sheet_problems(bare)
    assert len(p) == 2 and "height or angle" in p[0] and "keeps constant" in p[1]
    # 높이만 있고 유지할 것이 없으면 하나만 걸린다
    only = video.camera_sheet_problems({"camera_light": "An eye-level camera tracks the man with a smooth, slow glide."})
    assert len(only) == 1 and "keeps constant" in only[0]
    assert video.camera_sheet_problems({"camera_light": ""}) == []


def test_weather_must_be_layered_by_depth():
    assert video.weather_depth_problems(LUDYTE_RAIN) == []
    assert video.weather_depth_problems("Rain streaks near the lens against the dark facades while a fine drizzle hazes the distant towers.") == []
    assert len(video.weather_depth_problems(FLAT_RAIN)) == 1                           # 3번의 '비는 줄무늬로 내린다' 는 배경 효과일 뿐이다
    assert video.weather_depth_problems("A man walks along a dry street under a clear sky.") == []   # 비가 없으면 묻지 않는다
    assert video.weather_depth_problems(video.assemble(LONG)) == []


def test_walking_person_needs_a_physical_gait():
    assert video.gait_problems(LUDYTE_GAIT + " A man walks along the street.") == []
    assert len(video.gait_problems("A solitary man walks away from the camera down a rain-slicked street, his feet splashing "
                                   "through puddles.")) == 1
    assert video.gait_problems("A solitary man walks along the street.", "") != []
    assert video.gait_problems(video.assemble(LONG), "향수 광고 영상") == []              # 걷는 사람이 없으면 묻지 않는다
    assert video.gait_problems("The viewer walks along a forest path.", "1인칭 시점으로 숲길을 걷는 영상") == []   # 1인칭은 보지 않는다


# ── 사람이 아닌 요청에도 같은 구체성 규칙이 적용된다 (동물·탈것·김·연기·안개) ──
def test_animals_and_vehicles_need_physical_motion_too():
    dog = ("A golden dog trots across the meadow; each front paw plants and takes the weight, the leg swings through to the next "
           "stride, and the spine, shoulders and tail answer the rhythm.")
    eagle = ("An eagle flies over the valley as its wings rise, sweep down and fold, the body lifting with each beat, the tail steering.")
    car = ("A red car drives along a wet road: the tires bite, the body settles on its suspension into the curve, and spray trails behind it.")
    assert video.mover_problems(dog) == [] and video.mover_problems(eagle) == [] and video.mover_problems(car) == []
    for flat, head in (("A golden dog runs across a meadow toward the camera.", "Describe the animal's motion"),
                       ("An eagle soars over the valley.", "Describe the animal's motion"),
                       ("A red car drives along a coastal road at dusk.", "Describe the vehicle's motion")):
        p = video.mover_problems(flat)
        assert len(p) == 1 and p[0].startswith(head), (flat, p)
    assert video.mover_problems("A cat sleeps on the windowsill and its tail flicks once.") == []    # 이동하지 않는 동물은 묻지 않는다
    assert video.mover_problems("An eagle soars over the valley.", "1인칭 시점으로 날아가는 영상") == []   # 1인칭은 보지 않는다
    # 걷는 동물·탈것의 방향 모순과 출발·정지도 사람과 같이 본다
    assert video.blocking_problems("A dog runs away from the camera while the camera moves backward, keeping a constant distance.")
    assert video.travel_problems("The dog stops at the gate.", "", "t2v") and video.travel_problems("The boat slows to a halt.", "", "t2v")
    assert video.travel_problems("The boat glides on at the same pace while the camera settles.", "", "t2v") == []


def test_t2v_long_cap_is_600_but_the_aim_stays_410():
    # 11-16 메인: 개 예시가 542낱말이라 상한 500 을 넘었다. 상한은 600 으로 넓히고 목표는 410 에 둔다 (넓힌 상한이 '더 길게' 가 아니게)
    assert T2V_LONG["words"] == [320, 600] and video.aim_words(T2V_LONG) == 410
    assert video.aim_words(I2V_LONG) == 350 and video.aim_words(video.profile_of("wan22_t2v_mid")[1]) == 215   # 목표가 없으면 범위 가운데
    over = dict(LONG, action=LONG["action"] + " " + LONG["action"] + " " + LONG["action"])
    n = len(video.assemble(over).split())
    assert 600 < n
    msg = [p for p in video.check_video(over, T2V_LONG, "향수 광고 영상", None, SCENE) if "words; write 320 to 600" in p]
    assert msg and "Cut it to about 410 words in total, about 102 words per part" in msg[0]
    mid_len = dict(LONG, action=LONG["action"] + " " + LONG["action"])                       # 500 을 넘고 600 이하는 이제 통과한다
    assert 500 < len(video.assemble(mid_len).split()) <= 600
    assert not any("words; write" in p for p in video.check_video(mid_len, T2V_LONG, "향수 광고 영상", None, SCENE))


def test_layered_rain_in_one_sentence_with_semicolons_passes():
    # 11-16 메인 I2V 시험: 모델이 세 층으로 쓴 한 문장이 세미콜론에서 잘려 '배경 효과' 로 걸렸다
    layered = ("Rain falls in distinct layers: near the lens, streaks blur against the dark coat; in the middle, droplets stipple "
               "the asphalt, rippling the reflections; and far away, a fine mist softens the distant towers.")
    assert video.weather_depth_problems(layered) == []
    assert video.weather_depth_problems("Rain falls in three layers over the street.") == []
    assert len(video.weather_depth_problems(FLAT_RAIN)) == 1                               # 한 층뿐인 비는 여전히 걸린다
    assert video.ambient_link_problems("Steam rises slowly; it curls through the beam of window light.") == []


def test_ambient_particles_and_fog_need_light_surfaces_or_depth():
    assert video.ambient_link_problems("Steam rises from the cup and curls through the beam of window light, catching the backlight.") == []
    assert video.ambient_link_problems(video.assemble(LONG)) == []                                  # 향수 병 글의 안개·먼지는 빛과 이어져 있다
    for flat in ("Steam rises slowly and fine dust drifts around.", "Smoke drifts across the clearing."):
        assert len(video.ambient_link_problems(flat)) == 1 and "floats unconnected" in video.ambient_link_problems(flat)[0]
    assert video.ambient_link_problems("A man walks down a clear street.") == []
    # 안개는 비·눈처럼 깊이별로 나눠 쓴다
    assert len(video.weather_depth_problems("A thick fog fills the valley.")) == 1
    assert video.weather_depth_problems("Fog lies thick near the lens and thins into pale layers across the middle valley, fading "
                                        "the far ridges into haze.") == []
    ko = video.explain_ko(video.mover_problems("A red car drives along a coastal road.") + video.ambient_link_problems("Smoke drifts."))
    assert "탈것" in ko[0] and "빛이나 표면" in ko[1]


def test_detail_rules_apply_to_long_but_not_to_the_compact_profile():
    flat = dict(LONG,
                shot="Live-action cinematic shot of a man who walks down a street while heavy rain falls in visible streaks.",
                camera_light="The camera moves backward in one steady pull. " + " ".join(["Soft light falls across the street."] * 12),
                action=" ".join(["The man walks down the street."] * 12))
    probs = video.check_video(flat, T2V_LONG, "비 오는 거리를 걷는 남자", None, SCENE)
    for head in ('"camera_light" must name the camera height', '"camera_light" must say what the camera keeps constant',
                 "The weather is a flat background effect", "Describe the walk physically"):
        assert any(x.startswith(head) for x in probs), (head, probs)
    ko = video.explain_ko([x for x in probs if x.startswith(('"camera_light"', "The weather", "Describe the walk"))])
    assert any("높이" in k for k in ko) and any("유지" in k for k in ko) and any("깊이별" in k for k in ko) and any("체중" in k for k in ko)
    # 러다이트 1번처럼 쓴 글은 걸리지 않는다
    good = dict(LONG, shot=LONG["shot"].replace("perfume bottle", "man") , camera_light=LUDYTE_CAMERA + " " + LONG["camera_light"],
                action=LUDYTE_GAIT + " " + LUDYTE_RAIN + " " + LONG["action"])
    assert not any(x.startswith(('"camera_light"', "The weather", "Describe the walk")) for x in
                   video.check_video(good, T2V_LONG, "비 오는 거리를 걷는 남자", None, SCENE))
    # 보통 프로필은 단락이 짧아 이 세 검사를 하지 않는다
    mid = video.profile_of("wan22_t2v_mid")[1]
    assert not any(x.startswith(('"camera_light" must', "The weather", "Describe the walk")) for x in
                   video.check_video(flat, mid, "비 오는 거리를 걷는 남자", None, SCENE))
    # 지시문에도 항목표·깊이별 날씨·물리 묘사가 들어간다
    rules = video.build_messages("남자가 걷는 영상", "", T2V_LONG, 5.0, scene=SCENE)[0]["content"]
    for key in ("Camera sheet:", "Weather and atmosphere in depth:", "Subject motion in physical terms:", "what the camera keeps constant"):
        assert key in rules, key


def test_pov_operating_keeps_the_hands_a_supporting_detail():
    # 11-16 60cc823e: 'the hands read first' + 움직임 단락 전체가 손(조종간을 돌림) → 디테일이 손으로 쏠리고 조종간이 자동차 핸들처럼 나왔다
    said = "비행기를 운항하는 파일럿의 1인칭 시점 영상으로 만들어줘"
    real = {"shot": ("First-person POV from the pilot's seat inside a commercial airplane cockpit, composed as one continuous "
                     "shot. The leather seat and metal dashboard textures are crisp, creating a clean, professional aviation "
                     "atmosphere where the hands read first, the instruments second, and the clouds third."),
            "camera_light": "The camera remains locked off and steady.",
            "action": ("At first, the scene is calm with the hands resting lightly on the yoke. Then, the viewer's hands make "
                       "small, natural adjustments, turning the yoke slightly to the left and then back to center. The motion "
                       "is smooth and controlled, with the fingers gripping the textured rubber firmly. As the hands move, the "
                       "instrument displays remain steady. Finally, the hands settle back into a neutral position on the yoke.")}
    probs = video.hand_focus_problems(real, said)
    assert any("must not read first" in p for p in probs) and any('"action" is mostly about the hands' in p for p in probs), probs
    assert any("must not read first" in p for p in video.check_video(dict(PILOT_GOOD, shot=real["shot"]), T2V_LONG, said, None, PILOT_SCENE))
    # 올바른 글(손은 마지막 셋째, 움직임은 구름·빛)과 조종하지 않는 1인칭은 걸리지 않는다
    assert video.hand_focus_problems(PILOT_GOOD, said) == []
    assert video.hand_focus_problems(real, "1인칭으로 걷는 영상") == [] and video.hand_focus_problems(real, "1인칭 드론 비행 영상") == []
    # 모델에게도 손은 화면의 10분의 1 조연이고 마지막이며 핸들 돌리기를 쓰지 말라고 알린다
    line = video.operate_line()
    assert "small supporting detail" in line and "come last" in line and "not the hands" in line and "turning or steering" in line
    assert "yoke, wheel, handlebars" in line


def test_long_rules_name_things_by_shape_and_give_vehicle_interiors_identifying_parts():
    rules = " ".join(video.long_spec()["rules"])
    assert "U-shaped control yoke" in rules and "mistaken for another object" in rules and "generic control yoke" not in rules
    assert "at least five parts" in rules and "rudder pedals" in rules and "no single object may take over the frame" in rules
    sys_msg = video.build_messages("비행기를 운항하는 파일럿의 1인칭 시점 영상으로 만들어줘", "", T2V_LONG, 5.0)[0]["content"]
    assert "at least five parts that identify it" in sys_msg


def test_pov_check_targets_only_the_viewer():
    rx = video.SELF_BODY_RE
    for bad in ("an over-the-shoulder view", "the pilot's headset", "the pilot’s head", "a view of a seated pilot",
                "The camera follows close behind them", "framed from behind them", "seen from just behind the pilot"):
        assert rx.search(bad), bad
    # 화면 속 다른 사람과 배경은 1인칭 위반이 아니다
    for ok in ("her hair drifts in the wind as she turns", "the sun sets behind her", "snow-capped peaks rising behind them",
               "her face lights up with a smile", "the woman's face", "close behind the skier ahead", "the pilot's own gloved hands"):
        assert not rx.search(ok), ok


def test_camera_counter_ignores_other_subjects():
    cm = video.camera_moves
    assert cm("As the drone moves forward, the mist lifts from the water.") == ["push"]
    assert cm("Soft light from camera left slides across the table as the camera pushes in.") == ["push"]
    assert cm("A wide shot of a misty lake at dawn, rising mist drifting over the water.") == []
    assert cm("The camera holds steady as the steam rises from the cup.") == []
    assert cm("Through a 50mm lens, the rising sun fills the frame with a warm glow.") == []
    # 진짜 두 이동은 계속 잡는다
    assert sorted(cm("The camera glides forward as it rises over the ridge.")) == ["crane", "push"]
    assert sorted(cm("The camera orbits the bottle while rising slowly.")) == ["crane", "orbit"]
    assert cm("The shot slowly pushes in toward the cup.") == ["push"]
    probs = video.check_video(dict(LONG, action=LONG["action"] + " Then the camera orbits around the bottle in a wide arc."),
                              T2V_LONG, "향수 광고 영상", None, SCENE)
    assert any('orbit ("' in p and 'push ("' in p for p in probs)                       # 고칠 구절을 보여 준다


def test_long_reference_softens_signs_and_cut_words_are_narrow():
    scene = "a rainy night street with glowing neon signs, shop signage and wet asphalt"
    user = video.build_messages("비 오는 밤 거리", "", T2V_LONG, 5.0, scene=scene)[1]["content"]
    assert "glowing neon lights" in user and "signage" not in user and "neon signs" not in user
    old = video.build_messages("비 오는 밤 거리", "", T2V, 5.0, scene=scene)[1]["content"]
    assert "neon signs" in old                                                           # 예전 프로필은 그대로
    assert not video.CUT_RE.search("the cool light slowly transitions to warm gold")
    assert video.CUT_RE.search("then transitions to a wide view of the room")
    assert not crew.TEXT_NOTE_RE.search("faint signs of wear on a brand-new leather strap")
    assert crew.TEXT_NOTE_RE.search("glowing neon signs")
    assert not crew.PERSON_NOTE_RE.search("light falls on the barista counter") and crew.PERSON_NOTE_RE.search("a barista pours")
    assert crew_lib.has_term("a slow, steady pull-back from the cup", ["pull back"])          # 하이픈 표기도 확인어로 본다
    assert not crew_lib.has_term("fine film grain", ["rain"])


def test_pov_look_reference_treats_the_person_as_the_viewer():
    user = video.build_messages(PILOT, "", T2V_LONG, 5.0, scene="a pilot in a headset seated in a small cockpit")[1]["content"]
    assert "the person it shows is the viewer" in user and "keep its subject" not in user


def test_second_review_cut_and_camera_rules():
    # 부사가 낀 전환·명사형 전환·video/footage 는 컷이고, 빛의 변화는 컷이 아니다 (2차 검토, 11-11)
    for t in ("The scene seamlessly transitions to a sunset over the ocean.",
              "A smooth transition to a close-up of the bottle ends the clip.",
              "and transitions into an overhead shot of the dish", "The footage transitions to the same street at night."):
        assert video.CUT_RE.search(t), t
    for t in ("the glow gradually transitions into a deep amber", "the cool light slowly transitions to warm gold"):
        assert not video.CUT_RE.search(t), t
    cm = video.camera_moves
    # 관사 없는 다른 주어의 절(thin mist, steam)은 카메라 이동이 아니다
    for s in ("As the drone moves forward, thin mist lifts off the water.",
              "The camera pushes in slowly as steam rises from the cup.",
              "The camera slowly pushes in and steam rises from the cup."):
        assert cm(s) == ["push"], s
    # 카메라 자신의 다음 이동은 쉼표·and·as·while 뒤에서도 센다
    for s in ("The camera glides forward, the view rising above the rooftops.",
              "The camera pushes in toward the bottle. The shot then rises above the table.",
              "The camera pushes in, then tilts up toward the window.", "The camera pushes in and tilts up to the ceiling.",
              "The camera dollies in while tilting up toward the peak.",
              "The drone glides forward, rises over the treeline and tilts down.",
              "The drone flies forward, as it rises over the ridge.", "The camera pushes in, while it slowly rises."):
        assert sorted(cm(s)) == ["crane", "push"], s


def test_second_review_signs_people_and_pov_words():
    for s in ("neon signs of nearby bars", "the signs of the old shops", "a neon sign of a ramen shop"):
        assert crew.TEXT_NOTE_RE.search(s), s
    for s in ("As the first signs of morning light appear", "signs of heavy wear", "faint signs of wear"):
        assert not crew.TEXT_NOTE_RE.search(s), s
    for s in ("a man-made stone pier", "the slender figure of the bottle", "at human eye level"):
        assert not crew.PERSON_NOTE_RE.search(s), s
    for s in ("a man walks past", "a lone figure walks", "a figure of a woman"):
        assert crew.PERSON_NOTE_RE.search(s), s
    assert not video.pov_wanted("영상 내 시점 변화 없이 고정된 카메라로 바다를 보여줘")      # 이 '내' 는 '안' 이다
    assert video.pov_wanted("비행기를 운항하는 내 시점으로") and video.pov_wanted("승객 시점 기차 창밖")
    rx = video.SELF_BODY_RE
    assert rx.search("a seated pilot in a headset")
    for ok in ("a clear view of the pilot's own hands", "a clear view of the rider ahead", "from the seated pilot's eye position"):
        assert not rx.search(ok), ok
    # 간판은 글자를 부르는 말만 바꾸고 물건은 그대로 둔다 (도로 표지를 가로등으로 바꾸지 않는다)
    assert video.unsign("a street sign and a lit sign") == "a plain street panel and a lit panel"
    assert video.unsign("Neon signs over a wooden shop sign") == "Neon lights over a wooden shopfront board"


def test_second_review_look_rules_follow_style_and_first_frame():
    # T2V 장문의 그림체: 첫머리에 매체를 밝히고 필름 말을 쓰지 않게 하고, 예시는 실사 예시라고 밝힌다
    sys_msg, user_msg = video.build_messages("비 오는 밤 거리를 걷는 소녀", "", T2V_LONG, 5.0, scene=SCENE,
                                             style="soft watercolor illustration style")
    assert 'open "shot" by naming this medium' in user_msg["content"] and "do not repeat it" not in user_msg["content"]
    assert "and the film look." not in sys_msg["content"] and "the finish of the medium" in sys_msg["content"]
    assert "a different, live-action request" in sys_msg["content"]
    # I2V 장문: 렌즈·전경·중경·배경은 첫 화면 메모나 참고 글에 있는 것만
    sys_i2v = video.build_messages("커피잔 김", "", I2V_LONG, 5.0, FRAME, "")[0]["content"]
    assert "when there is a first frame, name only the ones the FIRST FRAME notes" in sys_i2v
    assert "(when there is a first frame, only with things the FIRST FRAME notes" in sys_i2v
    # 루프: 끝에서 멈추거나 다가가지 않고 처음 구도·빛으로 돌아간다
    assert "except in a loop" in sys_msg["content"] and "the opening framing and light again" in sys_msg["content"]


def run_fix(replies, **kw):
    """video.fix 를 가짜 LLM 으로 돌린다 (run_render 와 같은 방식). (결과, 호출 목록, 차례 목록)"""
    calls, turns = [], []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        turns.append((model, purpose))

        async def call(payload):
            calls.append(payload)
            return {"message": {"content": json.dumps(fit(replies.pop(0), payload))}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        result = asyncio.run(video.fix({"id": 1}, "127.0.0.1", "alice", "alice-main", **kw))
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved
    return result, calls, turns


def current_of(result):
    return {"ko": result["ko"], "parts": {k: result[k] for k in PARTS}}


def test_explain_ko_turns_check_messages_into_korean():
    out = video.explain_ko([
        "The prompt has 423 words; write 220 to 380 words.",
        '"ending" has 12 words; write at least 50 words: how it ends.',
        'Negative wording "without" in the prompt; state what should be seen instead.',
        "Do not mention text, letters, logos, labels, signs or quotation marks in the prompt.",
        "Keep one continuous shot: do not write cuts, scene changes or transitions.",
        'Use one main camera move for the whole clip; the prompt combines: push ("camera pushes in"); orbit ("camera orbits").',
        "Replace quality buzzwords with visible details (light, lens, colour, texture): masterpiece, 8k.",
        "something the rules never listed",
    ])
    assert "423" in out[0] and "줄여야" in out[0] and "마무리" in out[1] and "12낱말" in out[1]
    assert "without" in out[2] and "글자" in out[3] and "한 컷" in out[4]
    assert "push" in out[5] and "orbit" in out[5] and "masterpiece" in out[6]
    assert out[7] == "something the rules never listed"                      # 모르는 문구는 그대로 보여 준다
    assert "늘려야" in video.explain_ko(["The prompt has 100 words; write 300 to 450 words."])[0]


def test_fix_rewrites_only_what_broke_and_reports_in_korean():
    bad = dict(LONG, action=LONG["action"] + " The scene is a masterpiece of calm.")
    problems = video.check_video(dict(bad, prompt=video.assemble(bad)), T2V_LONG, "향수 광고 영상", None, SCENE)
    assert any("buzzwords" in p for p in problems)
    result, calls, turns = run_fix([LONG], text="향수 광고 영상", profile_id="wan22_t2v_long", scene=SCENE,
                                   current=current_of(bad), style="STYLE X")
    assert turns == [(settings.expand_model, "video_fix")] and len(calls) == 1          # 한 차례, 처음 쓰기 없이 고치기부터
    msgs = calls[0]["messages"]
    assert msgs[-2]["role"] == "assistant" and "masterpiece" in msgs[-2]["content"]       # 앞서 쓴 글을 보여 주고
    assert "quality buzzwords" in msgs[-1]["content"]                                     # 위반을 알려 준다
    assert result["fixed"] and result["changed"] and result["violations"] == [] and result["violations_ko"] == []
    assert result["parts"]["action"] == LONG["action"] and result["prompt"] == video.assemble(LONG) + "\n\nSTYLE X"
    assert any("masterpiece" in line and "유행어" in line for line in result["before_ko"])
    assert result["words_before"] > result["words_after"]


def test_fix_does_nothing_when_clean_and_keeps_the_original_when_worse():
    result, calls, turns = run_fix([], text="향수 광고 영상", profile_id="wan22_t2v_long", scene=SCENE,
                                   current=current_of(LONG))
    assert calls == [] and turns == [] and not result["changed"] and not result["fixed"]    # 검사를 통과하면 모델을 부르지 않는다
    assert result["prompt"] == video.assemble(LONG)
    bad = dict(LONG, action=LONG["action"] + " The scene is a masterpiece of calm.")
    worse = dict(bad, ending=LONG["ending"] + " Never use 8k.")
    result, calls, _ = run_fix([worse, worse, worse], text="향수 광고 영상", profile_id="wan22_t2v_long", scene=SCENE,
                               current=current_of(bad))
    assert len(calls) == 3 and not result["changed"] and not result["fixed"]
    assert result["parts"]["action"] == bad["action"] and result["violations_ko"]           # 더 나빠지면 원래 글을 지키고 이유를 남긴다


def test_fix_distrusts_the_panel_and_handles_short_profiles_and_garbage():
    # 패널이 보낸 위반은 믿지 않고 다시 검사한다 (current 만 받는다). 빈·깨진 입력도 죽지 않는다
    result, calls, _ = run_fix([LONG], text="향수 광고 영상", profile_id="wan22_t2v_long", scene=SCENE, current="garbage")
    assert len(calls) == 1 and result["fixed"] and result["parts"]["shot"] == LONG["shot"]
    neg = MOTION.rstrip(".") + " without any clutter in the frame."
    result, calls, _ = run_fix([{"ko": KO, "prompt": MOTION}], text="향수 광고 영상", profile_id="wan22_t2v", scene=SCENE,
                               current={"ko": KO, "motion": neg}, style="STYLE X")
    assert len(calls) == 1 and result["parts"] is None and result["long"] is False and result["fixed"]
    assert result["prompt"].endswith("STYLE X") and result["motion"] == MOTION


def test_mid_profiles_are_compact_versions_of_the_long_one():
    ids = [p["id"] for p in video.profile_list()]
    assert ids[:4] == ["wan22_i2v_long", "wan22_t2v_long", "wan22_i2v_mid", "wan22_t2v_mid"]   # 프로필을 안 고르면 장문 I2V
    assert [p["mid"] for p in video.profile_list()[:4]] == [False, False, True, True]
    t2v_mid, i2v_mid = video.profile_of("wan22_t2v_mid")[1], video.profile_of("wan22_i2v_mid")[1]
    assert video.is_long(t2v_mid) and video.part_min(t2v_mid) == 30 and video.part_min(T2V_LONG) == 50
    sys_mid = video.build_messages("향수 광고 영상", "", t2v_mid, 5.0, scene=SCENE)[0]["content"]
    assert "170 to 260 words" in sys_mid and "at least 30 words" in sys_mid and "EXAMPLE of the length" not in sys_mid
    assert "EXAMPLE of the length" in video.build_messages("향수 광고 영상", "", T2V_LONG, 5.0, scene=SCENE)[0]["content"]
    mid = {"ko": KO, **{k: ". ".join(LONG[k].split(". ")[:2]).rstrip(".") + "." for k in PARTS}}
    n = len(video.assemble(mid).split())
    assert 170 <= n <= 260, n
    assert video.check_video(mid, t2v_mid, "향수 광고 영상", None, SCENE) == []
    assert any("words; write 320 to 600" in p for p in video.check_video(mid, T2V_LONG, "향수 광고 영상", None, SCENE))
    # 첫 화면도 참고 글도 모르면 보통 I2V 도 움직임만으로 바뀐다
    result, _, _ = run_render([{"ko": KO, "prompt": MOTION}], username="alice", slug="alice-main",
                              text="고양이가 창가에서 하품하는 영상", profile_id="wan22_i2v_mid", crew=False)
    assert result["profile"] == "wan22_i2v" and result["long"] is False


def test_auto_length_is_decided_by_rule_then_llm_and_shown_to_the_user():
    assert video.request_is_detailed("가" * 130) and video.request_is_detailed("비 오는 밤\n네온 거리\n걷는 소녀")
    assert video.request_is_detailed("짧은 요청", "손은 항상 소매 속에") and not video.request_is_detailed("한 줄 요청\n\n  \n두 줄")
    # 자세한 요청은 모델을 부르지 않고 장문이다
    call = Call([])
    got = asyncio.run(video.decide_length(call, "비 오는 밤\n네온 거리\n소녀가 걷는다", "", "t2v"))
    assert got["tier"] == "long" and got["by"] == "rule" and call.calls == []
    # 짧은 요청은 같은 차례 안에서 LLM 에게 묻고, 한국어 이유를 화면에 쓴다
    call = Call([{"tier": "mid", "reason": "단순한 한 장면이라 보통 길이로 충분합니다."}])
    got = asyncio.run(video.decide_length(call, "찻잔에서 김이 오른다", "", "i2v", FRAME))
    assert got == {"tier": "mid", "reason": "단순한 한 장면이라 보통 길이로 충분합니다.", "by": "llm"}
    sent = call.calls[0]
    assert sent["format"]["properties"]["tier"]["enum"] == ["long", "mid"]
    assert "first frame already fixes the look" in sent["messages"][0]["content"] and "찻잔" in sent["messages"][1]["content"]
    t2v = Call([{"tier": "long", "reason": "처음부터 모습을 만들어야 해서 자세히 씁니다."}])
    asyncio.run(video.decide_length(t2v, "고양이", "", "t2v"))
    assert "no first frame" in t2v.calls[0]["messages"][0]["content"]
    # 한국어가 아닌 이유는 기본 문구로, 이상한 답·오류는 요청 길이 규칙(짧으면 보통)으로 정한다
    odd = asyncio.run(video.decide_length(Call([{"tier": "long", "reason": "because"}]), "고양이", "", "t2v"))
    assert odd["tier"] == "long" and "자세히" in odd["reason"] and odd["by"] == "llm"
    bad = asyncio.run(video.decide_length(Call([{"tier": "huge", "reason": "x"}]), "고양이", "", "t2v"))
    assert bad["tier"] == "mid" and bad["by"] == "rule"

    class Broken:
        async def __call__(self, payload):
            raise RuntimeError("ollama down")
    assert asyncio.run(video.decide_length(Broken(), "고양이", "", "t2v"))["by"] == "rule"
    # render: auto_length 이면 보통 프로필로 바꿔 쓰고 결과에 이유를 돌려준다. 끄면 그대로 장문
    mid_i2v = {"ko": KO, **{k: " ".join(LONG_I2V[k].split()[:45]).rstrip(".,;") + "." for k in PARTS}}   # 단락마다 약 45낱말
    assert 140 <= len(video.assemble(mid_i2v).split()) <= 230
    reply = {"tier": "mid", "reason": "단순한 요청이라 보통 길이로 씁니다."}
    result, calls, turns = run_render([reply, mid_i2v], username="alice", slug="alice-main", text="찻잔에서 김이 오른다",
                                      profile_id="wan22_i2v_long", image_prompt=SCENE, crew=False, auto_length=True)
    assert result["profile"] == "wan22_i2v_mid" and result["long"] and result["violations"] == []
    assert result["length_choice"]["tier"] == "mid" and turns == [(settings.expand_model, "video_expand")]
    assert "tier" in calls[0]["format"]["properties"] and set(PARTS) <= set(calls[1]["format"]["properties"])
    result, calls, _ = run_render([LONG_I2V], username="alice", slug="alice-main", text="찻잔에서 김이 오른다",
                                  profile_id="wan22_i2v_long", image_prompt=SCENE, crew=False)
    assert result["profile"] == "wan22_i2v_long" and result["length_choice"] is None and len(calls) == 1


def test_proper_names_are_described_not_named():
    """영화·캐릭터·브랜드·실존 인물 이름은 영상 모델이 모른다: 이름 대신 눈에 보이는 묘사로 풀게 하는 규칙이 모든 프로필에 간다"""
    from app import promptx
    text = "영화 스타쉽트루퍼스에 나오는 공격적인 외형의 외계벌레가 공격하는 영상"
    for prof, mode in (("wan22_i2v_long", "i2v"), ("wan22_t2v_long", "t2v"), ("wan22_i2v_mid", "i2v"), ("wan22_t2v_mid", "t2v")):
        sys_text = video.build_messages(text, "", video.load_knowledge()["profiles"][prof], 5.0)[0]["content"]
        assert "Proper names" in sys_text and "never a likeness" in sys_text, prof
    assert "Proper names" in " ".join(video.load_knowledge()["global_rules"])
    assert "creatures, brands or real people" in promptx.META_RULES and "never a likeness" in promptx.META_RULES


# LTX 프로필(시간 구간을 쓴다)용: 5초 영상이면 action 이 0-2, 2-4, 4-5초 세 구간으로 열려야 한다
LTX_LONG = dict(LONG_I2V, action="From 0 to 2 seconds, " + LONG_I2V["action"]
                + " From 2 to 4 seconds, the same motion carries on at the same steady pace. From 4 to 5 seconds, it keeps that pace as the clip ends.")

SOUND_OK = {"sound": ("Audio: A soft, steady room tone fills the quiet space, with a gentle breath of air moving past the glass. "
                      "The slow push-in carries a faint, smooth whisper from the camera mount, and a light touch on the marble "
                      "sounds once, clearly. A calm instrumental score of warm piano and a low synthesizer pad holds one steady "
                      "level through the whole clip."),
            "ko": "방 안의 잔잔한 소리와 부드러운 피아노 음악이 깔립니다."}


def test_ltx_profile_settings_and_default_profile_stay_wan():
    assert video.profile_of(None)[0] == "wan22_i2v_long"                     # 프로필을 안 고르면 예전처럼 Wan (소리 없음)
    ltx = video.profile_of("ltx25_i2v_long")[1]
    assert ltx["sound"] and ltx["fps"] == 24 and ltx["frame_step"] == 8 and is_long_profile(ltx)
    assert video.suggest_settings(ltx, 5) == {"frames": 121, "fps": 24, "seconds": 5.0, "capped": False}
    assert video.suggest_settings(ltx, 20)["frames"] == 481 and video.suggest_settings(ltx, 10)["frames"] == 241
    assert video.suggest_settings(ltx, 30)["capped"] and video.suggest_settings(ltx, 30)["seconds"] == 20.0
    # 길이는 정수 초: 계획·추정·결과가 실제로 만들어지는 길이와 같다 (레시피의 초 칸이 정수라서)
    for sec, want in ((2.5, 2), (7.5, 8), (1, 2), (0.5, 2), (9.4, 9)):
        st = video.suggest_settings(ltx, sec)
        assert st["seconds"] == float(want) and st["frames"] == want * 24 + 1, (sec, st)
    assert all((f - 1) % 8 == 0 for f in (video.suggest_settings(ltx, s)["frames"] for s in (2, 3, 4, 7.5, 12, 15)))
    assert next(p for p in video.profile_list() if p["id"] == "ltx25_i2v_long")["max_seconds"] == 20.0


def is_long_profile(p):
    return video.is_long(p)


def test_sound_paragraph_is_written_after_the_video_text_and_kept_separate():
    result, calls, _ = run_render([LTX_LONG, SOUND_OK], username="alice", slug="alice-main", text="향수 광고 영상",
                                  image_prompt=SCENE, profile_id="ltx25_i2v_long", crew=False)
    assert result["profile"] == "ltx25_i2v_long" and result["long"] and len(calls) == 2
    assert result["sound"] == SOUND_OK["sound"] and result["sound_left"] == []
    assert result["prompt"] == video.assemble(LTX_LONG) + chr(10) * 2 + SOUND_OK["sound"]       # 영상 글 뒤에 소리 단락
    assert result["ko"]["sound"] == SOUND_OK["ko"] and set(result["ko"]) == {"camera", "motion", "timing", "sound"}
    sysmsg, usermsg = calls[1]["messages"][0]["content"], calls[1]["messages"][1]["content"]
    assert "sound designer" in sysmsg and "quiet instrumental score" in sysmsg and "no spoken words" in sysmsg
    assert "VIDEO TEXT" in usermsg and "close-up shot" in usermsg
    assert set(calls[1]["format"]["properties"]) == {"sound", "ko"}
    # Wan 프로필은 소리 단락이 없고 호출도 늘지 않는다
    r2, calls2, _ = run_render([LONG_I2V], username="alice", slug="alice-main", text="향수 광고 영상", image_prompt=SCENE,
                               profile_id="wan22_i2v_long", crew=False)
    assert r2["sound"] == "" and len(calls2) == 1 and "sound" not in r2["ko"] and r2["prompt"] == video.assemble(LONG_I2V)


def test_sound_follows_speech_and_music_requests_and_repairs_bad_answers():
    said = '여자가 "안녕하세요, 반가워요" 라고 말하는 영상, 음악 없이'
    speech = {"sound": ('Audio: A calm room tone with a faint clock tick. The young woman at the desk says "안녕하세요, 반가워요" '
                        'in a warm, low voice, and her soft breath follows each phrase. The room settles quietly with the final frame.'),
              "ko": "차분한 방 소리와 여자의 한국어 대사가 들립니다."}
    result, calls, _ = run_render([LTX_LONG, speech], username="alice", slug="alice-main", text=said, image_prompt=SCENE,
                                  profile_id="ltx25_i2v_long", crew=False)
    sysmsg = calls[1]["messages"][0]["content"]
    assert video.MUSIC_OFF in sysmsg and '"안녕하세요, 반가워요"' in sysmsg and "word for word" in sysmsg
    assert result["sound"] == speech["sound"] and result["sound_left"] == []            # 따옴표 안의 한국어 대사는 그대로 둔다
    # 나쁜 답(없는 대사를 따옴표로, 부정어, 한글)은 한 번 고쳐 쓴다
    bad = {"sound": 'Audio: Someone says "hello there" and there is no music at all, 조용합니다.', "ko": "소리"}
    result, calls, _ = run_render([LTX_LONG, bad, SOUND_OK], username="alice", slug="alice-main", text="향수 광고 영상",
                                  image_prompt=SCENE, profile_id="ltx25_i2v_long", crew=False)
    assert len(calls) == 3 and calls[2]["messages"][-1]["content"].startswith("Your JSON broke these rules")
    assert result["sound"] == SOUND_OK["sound"] and result["sound_left"] == []
    # 끝내 못 고치면 가장 나은 답을 'Audio:' 로 시작하게 하고 남은 문제를 알린다
    weak = {"sound": "A short hum", "ko": "소리가 납니다."}
    result, calls, _ = run_render([LTX_LONG, weak, weak], username="alice", slug="alice-main", text="향수 광고 영상",
                                  image_prompt=SCENE, profile_id="ltx25_i2v_long", crew=False)
    assert result["sound"] == "Audio: A short hum" and result["sound_left"] and len(calls) == 3


def test_sound_problem_checks_and_spoken_lines():
    ltx = video.profile_of("ltx25_i2v_long")[1]
    assert video.sound_problems(SOUND_OK, "향수 광고", ltx) == []
    assert video.spoken_lines('"안녕하세요, 반가워요" 라고 말해') == ["안녕하세요, 반가워요"] and video.spoken_lines("향수 광고") == []
    assert video.spoken_lines("“오늘은 맑아요” 라고 말하고 『내일도 맑아요』 라고 말해") == ["오늘은 맑아요", "내일도 맑아요"]
    assert video.spoken_lines('"니키"라는 이름의 고양이가 걷는 영상') == []              # 말하는 말이 없으면 따옴표는 대사가 아니다
    assert video.spoken_lines('"안녕하세요" 라는 간판이 걸린 가게') == []
    # 요청한 대사가 소리 단락에 없으면 고치게 한다
    assert any("word for word" in p for p in video.sound_problems(SOUND_OK, '"안녕하세요" 라고 말해', ltx))
    assert any("words" in p for p in video.sound_problems({"sound": "Audio: hum", "ko": "소리"}, "", ltx))
    assert any("quoted" in p for p in video.sound_problems(dict(SOUND_OK, sound=SOUND_OK["sound"] + ' She says "hi".'), "", ltx))
    assert not any("quoted" in p for p in video.sound_problems(dict(SOUND_OK, sound=SOUND_OK["sound"] + ' She says "hi".'), '"hi" 라고 말해', ltx))
    assert any("positive" in p for p in video.sound_problems(dict(SOUND_OK, sound=SOUND_OK["sound"] + " It is not loud."), "", ltx))
    assert any("English" in p for p in video.sound_problems(dict(SOUND_OK, sound=SOUND_OK["sound"] + " 조용하다"), "", ltx))
    assert any("Korean" in p for p in video.sound_problems(dict(SOUND_OK, ko="quiet"), "", ltx))
    assert video.sound_problems("x", "", ltx)
    for t in ("음악 없이 만들어줘", "배경음악은 빼고", "무음악", "no music please"):
        assert video.NO_MUSIC_RE.search(t), t
    assert not video.NO_MUSIC_RE.search("잔잔한 음악이 흐르는 영상")


RAINY_CAM = ("The camera is locked off at eye level, keeping the man’s upper body centered. The distance remains constant, so his "
             "size in the frame stays unchanged. The man walks slowly forward with a heavy, slumped posture, a black umbrella over him.")


def test_moving_subjects_get_a_direction_and_a_locked_camera_does_not_claim_a_constant_size():
    """11-17 러다이트 비교: 이동 방향이 없고, 고정 카메라인데 '크기가 그대로'라고 썼다"""
    assert video.direction_problems("A man walks slowly forward on a rainy street.", "비 오는 거리의 남자")
    for ok in ("A man walks from right to left along a rainy street.", "A man walks toward the camera down the street.",
               "A woman strides away from the camera into the distance.", "A man walks across the frame past parked cars.",
               "The still lake holds a perfect reflection."):
        assert not video.direction_problems(ok, "x"), ok
    assert not video.direction_problems("A man walks slowly forward.", "1인칭 시점으로 걸어가는 영상")        # 1인칭은 보지 않는다
    probs = video.still_size_problems(RAINY_CAM)
    assert probs and "grows larger" in probs[0] and "stays unchanged" in probs[0]
    assert not video.still_size_problems(RAINY_CAM.replace("The distance remains constant, so his size in the frame stays unchanged.",
                                                             "He grows larger in the frame as he walks toward the lens."))
    assert not video.still_size_problems(RAINY_CAM.replace("walks slowly forward", "walks slowly from left to right"))   # 옆으로 가로지르면 거리가 같다
    assert not video.still_size_problems(RAINY_CAM.replace("locked off", "tracking alongside him"))            # 고정 카메라가 아니면 보지 않는다
    assert not video.still_size_problems("The camera is locked off. The vase keeps the same size and shape throughout.")   # 안정성 문장은 괜찮다


def test_a_walker_who_carries_something_needs_a_hand_and_a_mood_needs_the_body():
    base = ("The man walks, each foot landing and taking his weight as the other leg swings forward, his arms swinging and torso "
            "rising and falling. He carries a black umbrella over his head.")
    assert any("which hand holds" in p for p in video.gait_problems(base, "우산을 쓴 남자"))
    assert not video.gait_problems(base + " One hand keeps a relaxed grip on the umbrella handle.", "우산을 쓴 남자")
    assert not video.gait_problems(base, "손은 소매 속에 숨기고 우산을 쓴 남자")                               # 손 숨김 요청은 손 말을 쓰지 않는다
    sad = "The scene settles into a calm, quiet sadness, with the man’s figure staying centered."
    probs = video.emotion_body_problems(sad)
    assert probs and "mood word alone" in probs[0]
    assert not video.emotion_body_problems("The man walks with his head bowed in dejection and his shoulders rounded.")
    assert not video.emotion_body_problems("A serene sadness hangs over the empty plaza and the quiet fountain.")        # 사람이 없으면 보지 않는다


def test_the_sound_paragraph_names_only_heard_things():
    ltx = video.profile_of("ltx25_i2v_long")[1]
    bad = dict(SOUND_OK, sound=SOUND_OK["sound"].replace("holds one steady level through the whole clip.",
                                                         "fading gently into the background bokeh lights."))
    probs = video.sound_problems(bad, "", ltx)
    assert any("only seen" in p and "bokeh" in p for p in probs)
    assert not any("only seen" in p for p in video.sound_problems(SOUND_OK, "", ltx))


def test_sound_paragraph_keeps_one_steady_level_from_first_to_last_second():
    """11-17 메인 실측: 소리가 5~8초부터 갑자기 커진다 → 소리 글에 시작·고조·사라짐 표현을 쓰지 않는다"""
    ltx = video.profile_of("ltx25_i2v_long")[1]
    assert not any("steady level" in p for p in video.sound_problems(SOUND_OK, "", ltx))
    for drift in ("then fades with the final frame.", "that slowly builds toward the end.", "that grows louder as he approaches.",
                  "that begins to swell near the end.", "that rises slowly and then fades."):
        bad = dict(SOUND_OK, sound=SOUND_OK["sound"].replace("holds one steady level through the whole clip.", drift))
        assert any("steady level" in p for p in video.sound_problems(bad, "", ltx)), drift
    system = video.SOUND_SYSTEM.format(seconds=10, lo=35, hi=120, music_rule=video.MUSIC_ON, speech_rule=video.SPEECH_OFF)
    assert "already audible in the first second" in system and "no swell and no fade" in system
    assert "fade with the ending" not in system


def test_timeline_beats_scale_with_the_clip_and_only_the_ltx_profile_asks_for_them():
    """러다이트의 0-3, 3-7, 7-10 구간 방식 (11-17 사용자 선택)"""
    assert video.timeline_ranges(10) == [(0, 3, 1), (3, 7, 2), (7, 10, 3)]
    assert video.timeline_ranges(20) == [(0, 6, 1), (6, 14, 2), (14, 20, 3)]
    assert video.timeline_ranges(5) == [(0, 2, 1), (2, 4, 2), (4, 5, 3)] and video.timeline_ranges(3) is None
    assert video.timeline_ranges(None) is None and video.timeline_ranges("abc") is None
    ltx, wan = video.profile_of("ltx25_i2v_long")[1], video.profile_of("wan22_i2v_long")[1]
    msgs = video.build_messages("비 오는 거리의 남자", "", ltx, 10.0, FRAME, SCENE)
    user = msgs[1]["content"]
    assert "TIMELINE" in user and '"From 3 to 7 seconds, ..."' in user and "unless a TIMELINE line" in msgs[0]["content"]
    assert "TIMELINE" not in video.build_messages("비 오는 거리의 남자", "", wan, 5.0, FRAME, SCENE)[1]["content"]       # Wan 은 구간을 쓰지 않는다
    assert "TIMELINE" not in video.build_messages("비 오는 거리의 남자", "", ltx, 3.0, FRAME, SCENE)[1]["content"]       # 5초 미만도 나누지 않는다
    res = {"action": "The man walks. From 0 to 3 seconds, he steps. From 3 to 7 seconds, he keeps walking. From 7 to 10 seconds, he walks on."}
    assert video.timeline_problems(res, 10, ltx) == []
    assert video.timeline_problems({"action": "He walks steadily."}, 10, ltx)
    assert video.timeline_problems({"action": "From 0 to 3 seconds, a. From 3 to 8 seconds, b. From 8 to 10 seconds, c."}, 10, ltx)
    assert video.timeline_problems({"action": "He walks."}, 10, wan) == [] and video.timeline_problems({"action": "x"}, 3, ltx) == []
    # 규칙 문장이 지식 파일에 들어 있다
    rules = " ".join(video.long_spec()["rules"])
    for key in ("Direction on screen", "locked-off camera", "mood with the body", "carries or holds something", "Light separates the subject"):
        assert key in rules, key


def test_image_rules_keep_the_hidden_hands_example_for_hidden_hands_requests_only():
    from app import promptx
    sys_text = promptx.META_RULES
    assert "cuffs closed and drooping" not in sys_text and "accent light" in sys_text and "open space ahead" in sys_text
    plain = promptx.build_expand_messages("비오는 저녁 우산을 쓰고 풀이 죽은 채 걸어가는 남성", ideas=False)
    assert "HIDDEN HANDS" not in plain[1]["content"] and "cuffs closed and drooping" not in plain[0]["content"] + plain[1]["content"]
    hid = promptx.build_expand_messages("손은 소매 속에 숨기고 우산을 든 소녀", ideas=False)
    assert "HIDDEN HANDS" in hid[1]["content"] and "cuffs closed and drooping" in hid[1]["content"]


def test_review_fixes_for_direction_size_mood_and_sound_checks():
    """독립 검토에서 나온 오탐·미탐을 고정"""
    # 조명 방향('from the left')·오른손은 이동 방향이 아니다
    assert video.direction_problems("A man walks slowly. Warm light spills from the left across the wet road.", "x")
    assert not video.direction_problems("A man walks closer to the camera along the road.", "x")
    assert not video.direction_problems("A man walks toward the right edge of the frame.", "x")
    assert not video.still_size_problems("The camera is locked off. The man walks with his right hand on the umbrella; "
                                         "the bottle on the table keeps the same size throughout.")
    # 가만히 선 병의 크기 문장은 괜찮고, 걷는 사람의 크기가 그대로라는 문장만 걸린다
    assert not video.still_size_problems("The camera is locked off. A man walks forward. The bottle keeps the same size in the frame.")
    # 감정: 장소·빛을 꾸미는 말과 1인칭은 괜찮다
    assert not video.emotion_body_problems("He walks with slow steps along the lonely street.")
    assert not video.emotion_body_problems("He sits in a melancholy room lit by a single lamp.", "")
    assert not video.emotion_body_problems("The man feels sad.", "1인칭 시점으로 걷는 영상")
    # 소리: 음악 형용사는 괜찮다
    ltx = video.profile_of("ltx25_i2v_long")[1]
    glow = dict(SOUND_OK, sound=SOUND_OK["sound"].replace("a low synthesizer pad", "a glowing synth pad"))
    assert not any("only seen" in p for p in video.sound_problems(glow, "", ltx))


def test_ltx_draft_repairs_a_missing_timeline_and_passes_the_clip_length():
    ltx = video.profile_of("ltx25_i2v_long")[1]
    n = len(video.check_video(LTX_LONG, ltx, "x", FRAME, SCENE, seconds=5.0))
    assert not any("timed beats" in p for p in video.check_video(LTX_LONG, ltx, "x", FRAME, SCENE, seconds=5.0)), n
    assert any("timed beats" in p for p in video.check_video(LONG_I2V, ltx, "x", FRAME, SCENE, seconds=5.0))


def test_walking_staging_is_random_uniform_and_skipped_when_the_user_decides():
    import collections
    import random
    r = random.Random(7)
    c = collections.Counter(video.pick_staging("비오는 저녁 우산을 쓰고 풀이 죽은 채 걸어가는 남성", r) for _ in range(3000))
    assert set(c) == {"side_lr", "side_rl", "away", "toward"}
    assert abs(c["away"] - 1000) < 120 and abs(c["toward"] - 1000) < 120 and abs(c["side_lr"] + c["side_rl"] - 1000) < 120
    assert abs(c["side_lr"] - c["side_rl"]) < 120
    for said in ("왼쪽에서 오른쪽으로 걸어가는 남성", "1인칭 시점으로 걷는 영상", "카메라가 따라가며 걷는 여자", "앞으로 걸어가는 남자",
                 "거대로봇이 걸어다니는 영상", "고양이가 걷는 영상", "비 오는 거리 영상"):
        assert video.pick_staging(said, r) is None, said
    ltx = video.profile_of("ltx25_i2v_long")[1]
    wan = video.profile_of("wan22_i2v_long")[1]
    user = video.build_messages("남성이 걸어가는 영상", "", ltx, 10.0, FRAME, SCENE, staging="side_rl")[1]["content"]
    assert "STAGING" in user and "from right to left" in user and "tracks alongside" in user
    assert "STAGING" not in video.build_messages("남성이 걸어가는 영상", "", ltx, 10.0, FRAME, SCENE)[1]["content"]
    # 고른 연출의 영상 글은 방향·카메라 검사를 통과하는 쌍이다
    for sid, st in video.STAGINGS.items():
        assert not video.direction_problems(st["video"], "x"), sid
        assert not video.lateral_camera_problems(st["video"], "x"), sid
        assert not video.blocking_problems(st["video"]), sid


RAIN_PLAN = {"place": ["wet sidewalk", "low curb", "building fronts", "a side street"],
             "light": "Soft light from the left outlines his shoulder; a warm lit window glows far behind.",
             "air": "Rain crosses the pools of lamp light and drips from the umbrella edge.",
             "details": ["a glistening wet sleeve", "scuffed boots"],
             "acting": ["head bowed with rounded shoulders", "he lifts his head to check the path", "he walks on at the same pace"]}


def test_director_plan_is_cleaned_asked_for_and_fed_into_the_prompt():
    """11-17: 프롬프트 전에 연출 계획표(장소·빛·공기·디테일·구간별 연기)를 짜고 프롬프트에 모두 쓰게 한다"""
    assert video.clean_plan(dict(RAIN_PLAN)) == RAIN_PLAN
    assert video.clean_plan({**RAIN_PLAN, "place": ["one", "two"]}) is None                     # 장소는 3~4개
    assert video.clean_plan({**RAIN_PLAN, "acting": ["a", "b"]}) is None                         # 연기는 정확히 3개
    assert video.clean_plan({**RAIN_PLAN, "light": "빛이 비춘다"}) is None                        # 영어만
    assert video.clean_plan({**RAIN_PLAN, "details": ["a sleeve without a tear"]}) is None       # 긍정 표현만
    assert video.clean_plan("x") is None and video.clean_plan({}) is None
    ltx = video.profile_of("ltx25_i2v_long")[1]
    wan = video.profile_of("wan22_i2v_mid")[1]
    call = Call([RAIN_PLAN])
    plan = asyncio.run(video.write_plan(call, "비 오는 저녁 걷는 남자", "", ltx, 10.0, FRAME, SCENE, "side_lr"))
    assert plan == RAIN_PLAN
    sent = call.calls[0]["messages"]
    assert sent[0]["content"].startswith("You are the director of a small film crew. Before anything is shot")
    assert "left to right" in sent[1]["content"] and "FIRST FRAME" in sent[1]["content"]
    # 두 번 틀리면 None (계획표 없이 간다)
    assert asyncio.run(video.write_plan(Call([{}, {}]), "x", "", ltx, 5.0)) is None
    # 영상 프롬프트 요청에 계획표가 들어간다. 시간 구간 프로필은 '세 구간', 아니면 '세 단계'
    user = video.build_messages("남자가 걷는 영상", "", ltx, 10.0, FRAME, SCENE, plan=RAIN_PLAN)[1]["content"]
    assert "DIRECTOR'S PLAN" in user and "wet sidewalk" in user and "three timed beats" in user and "lit window" in user
    user = video.build_messages("남자가 걷는 영상", "", wan, 5.0, FRAME, SCENE, plan=RAIN_PLAN)[1]["content"]
    assert "three successive stages" in user
    assert "DIRECTOR'S PLAN" not in video.build_messages("남자가 걷는 영상", "", ltx, 10.0, FRAME, SCENE)[1]["content"]
    assert video.plan_scene_text(RAIN_PLAN).startswith("Director's plan for this picture") and "lifts his head" not in video.plan_scene_text(RAIN_PLAN)
    assert video.plan_scene_text(None) == ""


def test_plan_items_must_show_in_the_prompt():
    full = {"shot": "A man stands on a wet sidewalk beside a low curb, building fronts behind, a side street opening to one side, a glistening wet sleeve and scuffed boots.",
            "camera_light": "Soft light from the left outlines his shoulder; a warm lit window glows far behind while rain crosses the pools of lamp light.",
            "action": "From 0 to 3 seconds, his head bowed with rounded shoulders. From 3 to 7 seconds, he lifts his head to check the path. "
                      "From 7 to 10 seconds, he walks on at the same pace.", "ending": "He keeps walking."}
    assert video.plan_problems(full, RAIN_PLAN) == []
    assert video.plan_problems(full, None) == []
    miss = dict(full, camera_light="A cool grey light falls evenly.")
    p = video.plan_problems(miss, RAIN_PLAN)
    assert p and "light" in p[0] and "air" in p[0]
    nobeat = dict(full, action="From 0 to 3 seconds, he walks. From 3 to 7 seconds, he walks. From 7 to 10 seconds, he walks.")
    p = video.plan_problems(nobeat, RAIN_PLAN)
    assert p and "acting beat 2" in p[0]
    one_place = dict(full, shot=full["shot"].replace("a low curb, ", ""))
    assert video.plan_problems(one_place, RAIN_PLAN) == []               # 장소 사물은 하나까지 빠져도 된다
    two_places = dict(full, shot="A man stands in a quiet scene with a glistening wet sleeve.")
    assert any("place" in x for x in video.plan_problems(two_places, RAIN_PLAN))


def test_render_writes_the_plan_in_the_same_turn_when_none_was_given_and_checks_it():
    ltx_long = LTX_LONG
    replies = [ltx_long] * 3 + [SOUND_OK]                                    # 계획표 항목이 빠져 고쳐 쓰기를 두 번 하고, 소리 단락을 쓴다
    result, calls, turns = run_render(replies, username="alice", slug="alice-main", text="비 오는 저녁 걷는 남자", seconds=5,
                                      profile_id="ltx25_i2v_long", image_prompt=SCENE, crew=False, plan_reply=RAIN_PLAN)
    assert result["plan"] == RAIN_PLAN and len(run_render.plan_calls) == 1
    assert turns == [(settings.expand_model, "video_expand")]                  # 계획표도 같은 LLM 차례 안
    assert "DIRECTOR'S PLAN" in calls[0]["messages"][1]["content"]
    assert any("director's plan items missing" in v for v in result["violations"])   # 이 글에는 계획표 항목이 없다
    given, calls, _ = run_render([ltx_long] * 3 + [SOUND_OK], username="alice", slug="alice-main", text="x", seconds=5,
                                 profile_id="ltx25_i2v_long", image_prompt=SCENE, crew=False, plan=RAIN_PLAN)
    assert given["plan"] == RAIN_PLAN and run_render.plan_calls == []          # 감독이 준 계획표는 다시 짜지 않는다


def test_ltx_profile_allows_the_longer_detailed_prompt():
    ltx = video.profile_of("ltx25_i2v_long")[1]
    assert ltx["words"] == [260, 450] and video.aim_words(ltx) == 360 and video.part_min(ltx) == 45


def test_locked_camera_size_claim_without_the_word_frame_is_caught():
    """11-17 두 번째 메인 실측: 'keeping the subject's size and position constant' + 'moving closer to the camera'"""
    t = ("The camera is locked off at a low angle, keeping the subject's size and position constant. The man walks forward at a "
         "slow pace, then continues walking toward the camera.")
    assert video.still_size_problems(t)
    assert not video.still_size_problems("The camera is locked off. The vase keeps the same size. A man walks forward.")
    assert not video.still_size_problems("The camera is locked off, keeping the subject's size constant. The man walks from left to right.")


def test_sideways_travel_does_not_pair_with_a_camera_that_moves_in_depth():
    """11-17 메인 실측: 카메라 후퇴 + 남자 left to right → 영상에서 남자가 카메라 쪽으로 오다 5초에 화면 밖으로"""
    bad = "The camera moves backward ahead of the man at the same pace. From 0 to 3 seconds, the man walks from left to right slowly."
    probs = video.lateral_camera_problems(bad, "x")
    assert probs and "moves in depth" in probs[0] and "sideways" in probs[0]
    assert not video.lateral_camera_problems("The camera tracks alongside the man at his pace. The man walks from left to right.", "x")
    assert not video.lateral_camera_problems("The camera is locked off. The man walks from left to right.", "x")
    assert not video.lateral_camera_problems("The camera pushes in toward the cup. A man walks toward the camera.", "x")   # 깊이 이동은 다른 검사
    assert not video.lateral_camera_problems(bad, "1인칭 시점으로 걷는 영상")
    ltx = video.profile_of("ltx25_i2v_long")[1]
    res = dict(LTX_LONG, camera_light=bad)
    assert any("moves in depth" in p for p in video.check_video(res, ltx, "x", FRAME, SCENE, seconds=5.0))


# ── 긴 영상 구성안 (11-17, docs/long-video-design.md 7절) ──
STORY_OK = {
    "title_ko": "비 오는 귀갓길", "logline_ko": "비 오는 저녁, 우산을 쓴 남자가 집으로 돌아간다.",
    "bible": {"characters": [{"id": "A", "look": "a man in his forties, short black hair, navy wool coat, black umbrella"}],
              "places": [{"id": "P1", "look": "a narrow city street with wet asphalt, shop fronts and warm windows"},
                         {"id": "P2", "look": "a small apartment entrance with a glass door and a brass handle"}],
              "time": "early evening, steady rain", "light": "cool blue dusk with warm window light"},
    "music": {"tags": "melancholic cinematic instrumental, soft piano, warm strings, low synth pad", "bpm": 70, "key": "D minor",
              "ko": "잔잔한 피아노와 현악기가 깔립니다."},
    "scenes": [
        {"seconds": 10, "place": "P1", "characters": ["A"], "continues": False, "shot": "wide shot, slow push-in",
         "action": "The man walks along the wet street under his umbrella, shoulders slightly hunched.",
         "sound": "steady rain on the umbrella and asphalt, distant traffic", "ko": "남자가 비 오는 거리를 걷는다."},
        {"seconds": 10, "place": "P1", "characters": ["A"], "continues": True, "shot": "medium wide tracking shot from the side",
         "action": "He keeps walking past the shop windows and lifts his gaze to the road ahead.",
         "sound": "rain on the umbrella, footsteps on wet stone", "ko": "남자가 상점 앞을 지나며 고개를 든다."},
        {"seconds": 10, "place": "P2", "characters": ["A"], "continues": False, "shot": "close shot, static",
         "action": "He stops under the entrance light, shakes the rain off his shoulders and breathes out.",
         "sound": "rain dripping, the umbrella snapping shut", "ko": "남자가 현관에서 우산을 접는다."}]}


def _story(**over):
    s = copy.deepcopy(STORY_OK)
    s.update(over)
    return s


def test_story_counts_follow_ten_second_scenes():
    assert video.story_counts(180) == (18, 12, 26) and video.story_tolerance(180) == 18
    assert video.story_counts(30) == (3, 2, 5) and video.story_tolerance(30) == 3
    assert video.story_counts(60)[0] == 6


def test_story_problems_reject_hand_and_door_handling():
    """11-18 3분 실측: 손잡이를 쥔 손 클로즈업·문 여닫기를 영상 모델이 물리에 맞지 않게 그렸다. 구성안에서 막는다"""
    def with_action(text, shot="medium wide shot"):
        s = _story()
        s["scenes"][1].update(action=text, shot=shot)
        return video.story_problems(s, 30)
    for bad in ("The man's hand grips the umbrella handle tightly, knuckles pale.",
                "The man's fingers touch the cold door handle.", "The man turns the handle and the door creaks open.",
                "The man steps inside, the door closing behind him.", "He picks up the keys and puts on his coat.",
                "He folds the umbrella and shakes off the water."):
        probs = with_action(bad)
        assert any("handling action" in p for p in probs), bad
    assert any("handling action" in p for p in with_action("He walks on.", shot="close-up of his hands"))
    for ok in ("The man walks slowly toward the camera, head down, umbrella tilted against the rain.",
               "He keeps walking and lifts his gaze to the road ahead.", "Rain streaks down the brick as his eyes close briefly.",
               "The man stands in the doorway, umbrella dripping, warm light enveloping him."):
        assert with_action(ok) == [], ok


def test_story_problems_keep_continued_shots_and_looks_steady():
    """11-18 3분 실측: 클로즈업 뒤 이어진 장면이 미디엄 와이드로 뛰었고, 설정집에 없는 안경이 장면 3에 나왔다"""
    def scenes_of(a, b):
        s = _story()
        s["scenes"][0].update(shot=a)
        s["scenes"][1].update(shot=b)
        return video.story_problems(s, 30)
    assert any("shot size jumps" in p for p in scenes_of("close-up, static", "medium wide, panning right"))
    assert any("shot size jumps" in p for p in scenes_of("wide shot, static", "medium shot, static"))
    assert scenes_of("wide shot, static", "medium wide, tracking") == []         # 한 단계는 허용
    assert scenes_of("close-up, static", "medium close-up, slow pull back") == []
    assert video.shot_rank("Extreme close-up") == 0 and video.shot_rank("medium wide shot") == 3 and video.shot_rank("establishing") is None
    s = _story()
    s["scenes"][1].update(action="Raindrops cling to his glasses as he keeps walking.")
    assert any("glasses" in p and "bible look" in p for p in video.story_problems(s, 30))
    s["bible"]["characters"][0]["look"] += ", round glasses"
    assert not any("bible look does not list" in p for p in video.story_problems(s, 30))


def test_scene_direction_tells_the_planner_about_neighbours_and_the_seam():
    story = clean_story_for_direction()
    d1 = video.scene_direction(story, 1, {"ending": "He stands mid-frame facing the camera, umbrella above him."})
    assert "IS the last frame of the previous clip" in d1 and "facing the camera" in d1 and "same shot size" in d1
    assert "NEXT clip starts" not in d1 and "after a cut" in d1                    # 다음 장면이 새 장면이면 컷이라고만 알린다
    story["scenes"][2]["continues"] = True
    d1 = video.scene_direction(story, 1)
    assert "NEXT clip starts from this clip's last frame" in d1 and "face visible" in d1 and "never sitting or lying" in d1
    story["scenes"][2]["continues"] = False
    d0 = video.scene_direction(story, 0)
    assert "previous" not in d0.lower() and "NEXT clip starts" in d0
    d2 = video.scene_direction(story, 2)
    assert "new scene after a cut" in d2 and "NEXT clip" not in d2


def clean_story_for_direction():
    s = copy.deepcopy(STORY_OK)
    s["seconds"] = 30
    return s


def test_plan_asks_for_last_frame_depth_air_and_no_handling():
    assert "ending" in video.PLAN_SYSTEM and "three depth layers" in video.PLAN_SYSTEM and "hands" not in video.PLAN_SYSTEM.split("Rules:")[0].split('"acting"')[1].split("; for an animal")[0]
    base = {"place": ["wet street", "shop fronts", "kerb"], "light": "Cool light from the left.", "air": "rain",
            "details": ["wet coat"], "acting": ["He walks.", "He walks on.", "He keeps walking."], "ending": "He stands mid-frame, face visible."}
    assert video.clean_plan(base)["ending"] == "He stands mid-frame, face visible."
    assert video.clean_plan(dict(base, acting=["His hand grips the handle.", "He walks on.", "He keeps walking."]), safe=True) is None
    assert video.clean_plan(dict(base, acting=["His hand grips the handle.", "He walks on.", "He keeps walking."])) is not None   # 패널 영상은 그대로
    plan = video.clean_plan(base)
    assert "Last frame" in video.plan_block(plan, True)
    ok = {"shot": "wet street shop fronts kerb", "camera_light": "Cool light left rain", "action": "He walks. He walks on. He keeps walking wet coat",
          "ending": "He stands mid-frame, face visible."}
    assert video.plan_problems(ok, plan) == []
    assert any("last frame" in p for p in video.plan_problems(dict(ok, ending="Fog drifts over empty ground."), plan))


def test_story_style_reaches_every_scene_text_and_stays_out_when_photoreal():
    """11-18 애니메이션 시험: 요청의 화풍(2D+3D)이 설정집에 안 들어가 사실적인 아이가 나올 뻔했다. 설정집 style 이 모든 장면 글에 간다"""
    assert "style" in video.STORY_SYSTEM and "photorealistic cinematic live action" in video.STORY_SYSTEM
    anim = _story()
    anim["bible"]["style"] = "2D hand-drawn lines on cel-shaded 3D characters, soft painted backgrounds, bright pastel colours"
    assert video.story_style(anim).startswith("2D hand-drawn")
    assert video.story_style(STORY_OK) == ""                                         # 화풍이 없으면 기존 글 그대로
    photo = _story()
    photo["bible"]["style"] = "photorealistic cinematic live action"
    assert video.story_style(photo) == ""
    notes = video.scene_notes(dict(anim, seconds=30), 0)
    assert "- Style: 2D hand-drawn" in notes and "never photographic realism" in notes
    assert "Style:" not in video.scene_notes(dict(STORY_OK, seconds=30), 0)
    img = video.scene_image_text(dict(anim, seconds=30), 0)
    assert "one still frame" in img and "Style: 2D hand-drawn" in img and "one still photograph" not in img
    assert "one still photograph" in video.scene_image_text(dict(STORY_OK, seconds=30), 0)
    cleaned = video.clean_story(anim, 30)
    assert cleaned["bible"]["style"].startswith("2D hand-drawn")
    bad = _story()
    bad["bible"]["style"] = "손그림 느낌"
    assert any("bible.style must be an English sentence" in p for p in video.story_problems(bad, 30))


def test_story_problems_catch_bad_storyboards():
    assert video.story_problems(STORY_OK, 30) == []

    def scene(i, **kw):
        s = _story()
        s["scenes"][i].update(kw)
        return s
    checks = [
        (scene(0, seconds=25), "whole number from 5 to 20"),
        (scene(2, place="P9"), 'place "P9"'),
        (scene(2, continues=True), "different place"),
        (scene(0, continues=True), "first scene"),
        (scene(1, action="남자가 걷는다"), "must be English"),
        (scene(1, ko="He walks"), "Korean sentence"),
        (scene(1, sound="rain and a soft piano melody"), "must not name music"),
        (scene(0, characters=["Z"]), 'character "Z"'),
        (_story(music=dict(STORY_OK["music"], key="H major")), "music.key"),
        (_story(music=dict(STORY_OK["music"], bpm=300)), "music.bpm"),
        (_story(scenes=[dict(s, seconds=5) for s in STORY_OK["scenes"]]), "add up to 15"),
        (_story(scenes=STORY_OK["scenes"][:1]), "Write 2 to 5 scenes"),
        (_story(title_ko="Rainy way home"), '"title_ko" must be Korean'),
    ]
    for data, needle in checks:
        probs = video.story_problems(data, 30)
        assert any(needle in p for p in probs), (needle, probs)
    # 요청에 사람이 없으면 설정집·장면에 사람을 더하지 않는다
    assert any("adds a person" in p for p in video.story_problems(STORY_OK, 30, person_ok=False))
    assert video.story_problems("x", 30) and video.story_problems({}, 30)


def test_clean_story_fits_total_and_breaks_long_chains():
    short = _story(scenes=[dict(s, seconds=9) for s in STORY_OK["scenes"]])
    st = video.clean_story(short, 30)
    assert st["seconds"] == 30 and sorted(s["seconds"] for s in st["scenes"]) == [10, 10, 10]     # 27초 → 30초로 고르게
    first = _story()
    first["scenes"][0]["continues"] = True
    first["scenes"][2].update(continues=True)          # 장소가 바뀌는 이어짐은 이어짐이 아니다
    st = video.clean_story(first, 30)
    assert [s["continues"] for s in st["scenes"]] == [False, True, False]
    st = video.clean_story(_story(scenes=[dict(STORY_OK["scenes"][0], place="P1", continues=i > 0) for i in range(6)]), 60)
    assert [s["continues"] for s in st["scenes"]] == [False, True, True, True, False, True]     # 세 번 이으면 새 장면 이미지
    assert video.clean_story(_story(scenes=[dict(STORY_OK["scenes"][0], place="P9")] * 2), 20)["scenes"][0]["place"] == "P1"
    assert video.clean_story(_story(scenes=[dict(s, shot="와이드 샷") for s in STORY_OK["scenes"]]), 30) is None
    assert video.clean_story(_story(scenes=STORY_OK["scenes"][:1]), 30) is None
    assert video.clean_story(_story(music={"tags": "", "bpm": 999, "key": "Q"}), 30)["music"]["key"] == "C major"


def test_a_shot_that_the_next_scene_continues_keeps_the_subject_in_frame():
    """11-17 메인 첫 긴 영상: 1장면이 'static wide shot, walks left to right across the frame' 이라 사람이 화면 밖으로 나간 채 끝났고,
    그 마지막 프레임에서 시작한 2장면은 빈 거리에서 시작했다"""
    s = _story()
    s["scenes"][0]["action"] = "The man walks from left to right across the frame under his umbrella."
    probs = video.story_problems(s, 30)
    assert any("must end with the subject still in frame" in p and "across the frame" in p for p in probs), probs
    st = video.clean_story(s, 30)
    assert [x["continues"] for x in st["scenes"]] == [False, False, False]        # 고치지 못하면 잇지 않고 새 장면 이미지로
    assert not any("still in frame" in p for p in video.story_problems(STORY_OK, 30))
    st = video.clean_story(STORY_OK, 30)
    assert "The next shot begins from this shot's last frame" in video.scene_notes(st, 0)      # 2장면이 1장면을 잇는다
    assert "last frame" not in video.scene_notes(st, 1) and "last frame" not in video.scene_notes(st, 2)
    assert "end with the main subject still fully in frame" in video.STORY_SYSTEM
    assert video.scene_has_person(st, 0)
    cat = _story(bible=dict(STORY_OK["bible"], characters=[{"id": "A", "look": "a small orange tabby cat with white paws"}]))
    assert not video.scene_has_person(video.clean_story(cat, 30), 0)               # 동물은 사람 검사에서 뺀다


def test_scene_notes_and_image_text_carry_the_bible():
    st = video.clean_story(STORY_OK, 30)
    notes = video.scene_notes(st, 1)
    assert "scene 2 of 3" in notes and "navy wool coat" in notes and "wet asphalt" in notes
    assert "medium wide tracking shot" in notes and "footsteps on wet stone" in notes
    # 앞뒤 장면은 넣지 않는다: '반드시 지킬 말' 검사가 다른 장면의 날씨·카메라를 이 장면에 요구하게 된다 (11-17 검토)
    assert "Previous scene" not in notes and "Next scene" not in notes and "현관" not in notes
    plan = {"place": ["the sidewalk", "the curb", "the shop fronts"], "light": "warm window light from the left", "air": "rain",
            "details": ["a glistening wet sleeve"], "acting": ["a", "b", "c"]}
    img = video.scene_image_text(st, 0, plan)
    assert img.startswith("남자가 비 오는 거리를 걷는다.") and "wide shot" in img and "Director's plan" in img and "navy wool coat" in img
    huge = dict(plan, place=["x" * 600] * 4)
    assert len(video.scene_image_text(st, 0, huge)) <= 1750                     # 이미지 확장 2000자 안 (힌트 자리를 남긴다)


def test_write_story_repairs_then_accepts():
    bad = _story(scenes=[dict(s, seconds=5) for s in STORY_OK["scenes"]])
    call = Call([bad, STORY_OK])
    story, left, rounds = asyncio.run(video.write_story(call, "비 오는 저녁 우산 쓴 남자가 집에 가는 30초 영상", "", 30))
    assert rounds == 2 and left == [] and story["seconds"] == 30 and len(story["scenes"]) == 3
    system = call.calls[0]["messages"][0]["content"]
    assert "about 3 scenes (2 to 5)" in system and "D minor" in system and call.calls[0]["format"] is video.STORY_SCHEMA
    assert call.calls[1]["messages"][-1]["content"].startswith("Your JSON broke these rules")
    # 끝내 고치지 못해도 다듬어 쓸 수 있으면 돌려준다 (길이 합은 다듬을 때 맞춘다)
    story, left, rounds = asyncio.run(video.write_story(Call([bad, bad, bad]), "남자 영상", "", 30))
    assert story is not None and story["seconds"] == 30 and left and rounds == 3
    # 쓸 수 없는 모양이면 None
    story, left, _ = asyncio.run(video.write_story(Call(["x", "x", "x"]), "남자 영상", "", 30))
    assert story is None and left
    # '남성'·'여성' 도 사람이 있는 요청이다 (11-17 메인: '남성' 요청이 사람 없는 요청으로 읽혀 세 번 고쳐 쓰고도 거짓 위반이 남았다)
    for req in ("비 오는 저녁 우산을 쓴 남성이 걷는 30초 영상", "여성이 카페에서 책을 읽는 30초 영상"):
        story, left, rounds = asyncio.run(video.write_story(Call([STORY_OK]), req, "", 30))
        assert left == [] and rounds == 1, (req, left)
    story, left, rounds = asyncio.run(video.write_story(Call([STORY_OK, STORY_OK, STORY_OK]), "비 오는 골목 풍경 30초 영상", "", 30))
    assert any("adds a person" in p for p in left)                  # 사람이 없는 요청은 그대로 검사한다


def test_storyboard_runs_in_one_llm_turn_and_clamps_length():
    turns = []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        turns.append(purpose)

        async def call(payload):
            turns.append(payload["messages"][0]["content"])
            return {"message": {"content": json.dumps(STORY_OK)}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "비 오는 저녁 남자 영상", seconds=30))
        assert turns[0] == "video_story" and "screenwriter" in turns[1] and r["story"]["seconds"] == 30
        assert r["violations"] == [] and r["rounds"] == 1
        turns.clear()
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "남자 영상", seconds=900))
        assert "about 18 scenes (12 to 26)" in turns[1] and "add up to 180" in turns[1]      # 3분으로 맞춰 쓴다
        assert r["violations"] and r["rounds"] == 3              # 3장면짜리 답은 3분을 채우지 못해 위반이 남는다
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved


SOUND_NO_MUSIC = {"sound": ("Audio: A steady rain bed covers the narrow street, with water ticking on the umbrella canopy close to "
                            "the camera. His leather shoes press on the wet stones in an even rhythm, a car hisses past far behind, "
                            "and the rain keeps the same soft level through the whole clip."),
                  "ko": "빗소리와 발소리가 고르게 들립니다."}


def test_scene_sound_has_no_music_when_the_film_adds_one_track():
    ltx = video.profile_of("ltx25_i2v_long")[1]
    assert video.music_rule("", False) == video.MUSIC_LATER and video.music_rule("음악 없이", None) == video.MUSIC_OFF
    assert video.music_rule("", None) == video.MUSIC_ON
    assert any("no music" in p for p in video.sound_problems(SOUND_OK, "", ltx, music=False))      # 피아노 음악이 들어 있다
    assert video.sound_problems(SOUND_NO_MUSIC, "", ltx, music=False) == []
    assert not any("no music" in p for p in video.sound_problems(SOUND_OK, "", ltx))               # 짧은 영상은 지금처럼 음악 포함
    # 화면 속에서 실제로 연주하는 악기 소리는 음악 낱말이어도 된다 (장면 글에 그 악기가 있을 때)
    played = dict(SOUND_NO_MUSIC, sound=SOUND_NO_MUSIC["sound"] + " Her fingers press the piano keys softly.")
    assert any("no music" in p for p in video.sound_problems(played, "비 오는 거리", ltx, music=False))
    assert not any("no music" in p for p in video.sound_problems(played, "소녀가 피아노를 친다 Action: A girl plays the piano.",
                                                                  ltx, music=False))
    assert video.music_words("soft piano and a warm score", "she plays the piano") == ["score"]
    s = _story()
    s["scenes"][0].update(action="A girl plays the piano by the window.", sound="the piano notes she plays, rain on the glass")
    assert not any("must not name music" in p for p in video.story_problems(s, 30))
    result, calls, _ = run_render([LTX_LONG, SOUND_OK, SOUND_NO_MUSIC], username="alice", slug="alice-main",
                                  text="향수 광고 영상", image_prompt=SCENE, profile_id="ltx25_i2v_long",
                                  crew=False, music=False)
    assert video.MUSIC_LATER in calls[1]["messages"][0]["content"]
    assert result["sound"] == SOUND_NO_MUSIC["sound"] and result["sound_left"] == []     # 음악이 든 첫 답은 고쳐 쓴다


# ── 대사 영상 구성안 (11-19) ──
DLG_OK = {
    "title_ko": "숲길 끝의 화원", "logline_ko": "니키와 푸토르가 숲길을 걸으며 아픔을 이야기한다.",
    "bible": {"characters": [
        {"id": "A", "look": "a twelve-year-old girl with two long black braids, wine-red hat and cape", "name_ko": "니키",
         "voice": "A twelve-year-old Korean girl, clear and natural, gentle and a little shy.", "voice_ko": "맑은 열두 살 여자아이"},
        {"id": "B", "look": "a short old dwarf herbalist with a long white beard, round glasses and a golden-ochre coat",
         "name_ko": "푸토르", "voice": "A kind Korean grandpa around eighty, low warm gravelly voice, slow and gentle.",
         "voice_ko": "굵고 따뜻한 할아버지", "sample_ko": "허허, 얘야. 이 할아비가 옛날얘기 하나 해 줄까?"}],
        "places": [{"id": "P1", "look": "a winding forest path with mossy trees"},
                   {"id": "P2", "look": "a sunlit flower garden with an arch"}],
        "time": "early morning, clear", "light": "soft golden sunbeams", "style": "semi-realistic painterly 2.5D fantasy film look"},
    "music": {"tags": "gentle cinematic fantasy instrumental, piano, strings", "bpm": 70, "key": "D major", "ko": "잔잔한 피아노"},
    "scenes": [
        {"seconds": 8, "place": "P1", "characters": [], "continues": False, "shot": "wide shot, slow dolly forward",
         "action": "Morning light falls on the empty winding path.", "sound": "birds and leaves", "ko": "아침 숲길",
         "kind": "shot", "speaker": "", "line_ko": "", "emotion": ""},
        {"seconds": 10, "place": "P1", "characters": ["A", "B"], "continues": False, "shot": "wide shot from behind, slow follow",
         "action": "The girl and the old dwarf walk side by side along the path.", "sound": "footsteps on soft earth",
         "ko": "둘이 함께 걷는다", "kind": "shot", "speaker": "B", "line_ko": "얘야, 굽은 길은 천천히 보게 해 준단다.",
         "emotion": "calm and wise, slow"},
        {"seconds": 6, "place": "P1", "characters": ["A"], "continues": False, "shot": "close-up",
         "action": "She looks up with hesitant eyes.", "sound": "soft breeze", "ko": "니키가 묻는다", "kind": "talk",
         "speaker": "A", "line_ko": "할아버지, 아픈 건 언제 없어져요?", "emotion": "hesitant and a little sad"},
        {"seconds": 9, "place": "P1", "characters": ["B"], "continues": True, "shot": "medium close-up",
         "action": "He smiles warmly and nods slowly.", "sound": "birds", "ko": "푸토르가 답한다", "kind": "talk", "speaker": "B",
         "line_ko": "아픔은 없애는 게 아니란다. 함께 걸어가는 법을 배우는 게지.", "emotion": "warm and wise"},
        {"seconds": 6, "place": "P1", "characters": ["A"], "continues": False, "shot": "close-up",
         "action": "She listens quietly, eyes lowered.", "sound": "birds", "ko": "니키가 생각한다", "kind": "shot",
         "speaker": "", "line_ko": "", "emotion": ""},
        {"seconds": 9, "place": "P2", "characters": ["A", "B"], "continues": False, "shot": "wide shot from behind",
         "action": "They stand at the end of the path before the bright garden.", "sound": "bees and wind", "ko": "화원이 펼쳐진다",
         "kind": "shot", "speaker": "", "line_ko": "", "emotion": ""}]}


def test_dialogue_story_rules_and_cleaning():
    import copy as _copy
    left = video.dialogue_problems(DLG_OK, 48, True)
    assert any("Scene 4 is a talk scene, so it cannot continue" in x for x in left), left    # 말하는 장면은 잇지 않는다
    s = video.clean_dialogue_story(DLG_OK, 48)
    assert s["dialogue"] and [x["kind"] for x in s["scenes"]] == ["shot", "shot", "talk", "talk", "shot", "shot"]
    assert not any(x["continues"] for x in s["scenes"]) and s["scenes"][2]["characters"] == ["A"]
    assert s["scenes"][1]["speaker"] == "B" and s["scenes"][1]["line_ko"].startswith("얘야")      # 걷는 장면의 화면 밖 목소리
    a, b = s["bible"]["characters"]
    assert a["name_ko"] == "니키" and "girl" in a["voice"] and b["voice_ko"] == "굵고 따뜻한 할아버지"
    # 말하는 사람·기준 그림·바라보는 쪽 (두 사람이 서로 마주 보게)
    assert video.speakers_of(s) == ["B", "A"] and {video.speaker_side(s, "A"), video.speaker_side(s, "B")} == {"left", "right"}
    assert [video.cast_scene(s, i) for i in range(6)] == [None, None, "A", "B", "A", None] and video.cast_ids(s) == ["A", "B"]
    # 잘못된 구성안: 말하는 장면에 두 사람(예전 혼자 클로즈업 규칙), 먼 샷, 실제 사람 목소리 흉내, 대사 없는 영화
    bad = _copy.deepcopy(DLG_OK)
    bad["scenes"][2].update(characters=["A", "B"], shot="wide shot")
    bad["bible"]["characters"][0]["voice"] = "sounds like Taylor Swift"
    with duo_setting(False):
        left = video.dialogue_problems(bad, 48, True)
        assert any('must be exactly ["A"]' in x for x in left) and any("close-up or a medium close-up" in x for x in left)
        assert any("real or famous voice" in x for x in left)
    with duo_setting(True):          # 두세 사람 규칙: 두 사람은 대화(듣는 사람 추정 (a))라 맞고, 먼 샷만 틀렸다
        left = video.dialogue_problems(bad, 48, True)
        assert any("Scene 3 is a conversation with 2 people: the shot must be a medium two-shot" in x for x in left), left
        assert not any('must be exactly ["A"]' in x for x in left) and any("real or famous voice" in x for x in left)
    none = _copy.deepcopy(DLG_OK)
    for sc in none["scenes"]:
        sc.update(kind="shot", speaker="", line_ko="", emotion="")
    assert any("needs talk scenes" in x for x in video.dialogue_problems(none, 48, True))
    assert video.clean_dialogue_story(none, 48) is None


def test_dialogue_prompts_keep_one_person_and_the_look():
    s = video.clean_dialogue_story(DLG_OK, 48)
    talk = video.talk_prompt(s, 3)
    assert "alone in the frame, no other people" in talk and "golden-ochre coat." in talk and "warm and wise" in talk
    # 11-20: '화면 밖의 누군가에게 말한다'는 그 누군가를 그려 넣게 할 수 있다 → 화면 가장자리 쪽으로 말하고 아무도 들어오지 않는다
    assert "someone off-screen" not in talk and "speaks toward the" in talk and "no one else ever enters the frame" in talk
    assert "Style: semi-realistic painterly" in talk and "lips and jaw move" in talk
    # 11-24: 카메라는 고정(밀고 들어가지 않는다), 얼굴은 화면 안 같은 자리 (베스 컷 얼굴이 카메라와 함께 아래로 빠졌다)
    assert "locked on a tripod and never moves" in talk and "push in" in talk and "very slow push in" not in talk
    assert "face stays in the same position inside the frame" in talk
    edit = video.cast_edit_prompt(s, 2)
    assert edit.startswith("Keep the same person from the image exactly") and video.ONE_PERSON in edit
    # 11-20 메인 재시험: "looking at someone off-screen" 이 그 '누군가'를 그려 넣어 푸토르가 둘이 됐다 (같은 씨앗으로 재현, 이 글로 한 명)
    assert "someone off-screen" not in edit and "looking past the edge of the frame at nothing visible" in edit
    assert "no copy or mirror image" in video.ONE_PERSON and "no second person" in video.ONE_PERSON
    assert "winding forest path" in edit and "about to speak" in edit
    assert "listening quietly" in video.cast_edit_prompt(s, 4)                 # 듣는 얼굴은 입을 다문다
    cast = video.cast_prompt(s, "A")
    assert "two long black braids" in cast and video.ONE_PERSON in cast and "hands not visible" in cast and "No text." in cast
    vi = video.voice_instruct(s, "B")
    assert vi.startswith("A kind Korean grandpa") and vi.endswith(video.VOICE_REF_EMOTION + ".") and "Emotion:" in vi
    # 11-19 메인 시험 2: 설명 끝의 "Natural Korean speech, clear pronunciation. Emotion: calm and friendly, relaxed." 가 80세 푸토르를
    # 맑은 젊은 목소리로 눌렀다. 그 문구는 쓰지 않는다
    assert "calm and friendly" not in vi and "clear pronunciation" not in vi and "relaxed" not in vi
    # 기준 소리가 말할 글: 인물의 샘플 글(sample_ko), 없으면 기본 인사말
    assert video.voice_sample(s, "B").startswith("허허, 얘야") and video.voice_sample(s, "A") == video.VOICE_REF_TEXT
    assert video.line_tokens("예뻐요.") == 256 and video.line_tokens("가" * 40) == 512
    assert video.line_max_seconds("가" * 20) == 13.0


def test_write_dialogue_story_uses_the_dialogue_schema_and_repairs():
    import copy as _copy
    bad = _copy.deepcopy(DLG_OK)
    bad["scenes"][2]["characters"] = ["A", "B"]
    good = _copy.deepcopy(DLG_OK)
    good["scenes"][3]["continues"] = False
    with duo_setting(False):          # 예전 혼자 클로즈업 규칙 (두세 사람 규칙의 고쳐 쓰기는 test_g0_write_dialogue_story_repairs_a_missing_listener)
        call = Call([bad, good])
        story, left, rounds = asyncio.run(video.write_dialogue_story(call, "니키와 푸토르가 숲속을 걸어가며 대화하는 영상", "", 48))
        assert rounds == 2 and left == [] and story["dialogue"], left
        system = call.calls[0]["messages"][0]["content"]
        assert "spoken dialogue in Korean" in system and call.calls[0]["format"] is video.dialogue_schema()      # 두 사람 대화 설정에 따라
    assert "line_ko" in video.DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["required"]
    assert "line_ko" in video.DIALOGUE_SCHEMA_SOLO["properties"]["scenes"]["items"]["required"]


def _dlg_good():
    import copy as _copy
    g = _copy.deepcopy(DLG_OK)
    g["scenes"][3]["continues"] = False          # 말하는 장면은 앞 장면을 잇지 않는다
    return g


def _dlg_duo_good():
    """_dlg_good 을 두세 사람 규칙(사용자 확정 11-24)에 맞춘 판: 서로에게 하는 두 대사는 듣는 사람을 적고 두 사람을 투샷에 둔다"""
    g = _dlg_good()
    g["scenes"][2].update(characters=["A", "B"], listener="B", shot="medium two-shot")
    g["scenes"][3].update(characters=["B", "A"], listener="A", shot="medium two-shot")
    for sc in g["scenes"]:
        sc.setdefault("listener", "")
    return g


def test_dialogue_wants_exchange_words():
    # 인물끼리의 대화 말만 대화로 본다: 한 사람의 설명·따옴표 대사·풍경 요청은 아니다
    for yes in ("니키와 푸토르가 숲속을 걸어가며 대화를 하는데", "두 사람이 서로 묻고 대답하는 영상", "친구들이 수다를 떤다",
                "기자가 인터뷰를 한다", "두 사람이 토론한다", "이야기를 나누며 걷는다"):
        assert video.wants_exchange(yes), yes
    for no in ("푸토르가 \"안녕\" 이라고 말한다", "할아버지가 고통을 이기는 방법을 설명해 준다", "비 오는 거리 풍경 영상"):
        assert not video.wants_exchange(no), no
    assert [video.dialogue_min_lines(x) for x in (25, 30, 40, 60, 95, 180)] == [2, 2, 3, 4, 7, 13]


def test_dialogue_rules_want_enough_lines_and_a_real_exchange():
    import copy as _copy
    # 대사 셋·두 인물·번갈아 말하고 둘 다 얼굴이 보이는 장면이 있는 구성안은 대화 요청에서도 통과한다 (예전 혼자 클로즈업 규칙).
    # 두세 사람 규칙(사용자 확정 11-24)에서는 듣는 사람 칸이 빈 대사를 앞뒤 대사로 추정해, 상대가 화면에 없다는 위반만 남는다
    with duo_setting(False):
        assert video.dialogue_problems(_dlg_good(), 48, True, exchange=True) == []
    with duo_setting(True):
        left = video.dialogue_problems(_dlg_good(), 48, True, exchange=True)
        assert left == ['Scene 3: the person spoken to must be in the shot: put "B" in "characters" right after the speaker '
                        '(or write "nobody" in "listener" if the line is a monologue).',
                        'Scene 4: the person spoken to must be in the shot: put "A" in "characters" right after the speaker '
                        '(or write "nobody" in "listener" if the line is a monologue).'], left
    # 11-19 메인 첫 시험의 구성안: 푸토르 한 줄뿐, 니키는 말이 없다 → 줄이 모자라고 한 사람만 말한다
    thin = _dlg_good()
    for sc in thin["scenes"]:
        sc.update(kind="shot", speaker="", line_ko="", emotion="")
    thin["scenes"][1].update(kind="shot", speaker="B", line_ko="고통은 바람처럼 지나가.", emotion="calm and wise")
    thin["scenes"][3].update(kind="talk", speaker="B", line_ko="숨을 고르고, 마음속 꽃을 바라봐.", emotion="warm")
    left = video.dialogue_problems(thin, 48, True, exchange=True)
    assert any("at least 3 spoken lines" in x and "you wrote 2" in x for x in left), left
    assert any('only "B" speaks' in x and 'Let "A" speak too' in x for x in left), left
    assert video.dialogue_problems(thin, 48, True) != [] and not any("only" in x and "speaks" in x for x in video.dialogue_problems(thin, 48, True))
    # 대화 요청이 아니면 한 사람의 이야기도 된다 (줄 수는 여전히 본다)
    one = _dlg_good()
    # (장면 글도 말하는 푸토르의 것으로: 독백 컷의 장면 글이 다른 사람('She')을 가리키면 위반이다, 11-24 2차 검토)
    one["scenes"][2].update(kind="talk", speaker="B", characters=["B"], line_ko="천천히 걸어 보거라.", emotion="calm",
                            action="He looks ahead with calm eyes.")
    assert video.dialogue_problems(one, 48, True) == []
    assert any('only "B" speaks' in x for x in video.dialogue_problems(one, 48, True, exchange=True))
    # 같은 사람이 세 줄 연속
    run = _dlg_good()
    run["scenes"][2].update(speaker="B", characters=["B"], line_ko="괜찮단다.", emotion="soft")
    run["scenes"][4].update(kind="talk", speaker="A", characters=["A"], shot="close-up", line_ko="네, 할아버지.", emotion="quiet")
    left = video.dialogue_problems(run, 48, True, exchange=True)
    assert any('"B" speaks 3 lines in a row' in x for x in left), left
    # 한 인물이 화면 밖 목소리로만 말한다 (얼굴이 보이는 말하기 장면이 없다)
    voice_only = _dlg_good()
    voice_only["scenes"][3].update(speaker="A", characters=["A"], line_ko="저도 해 볼게요.", emotion="hopeful")
    left = video.dialogue_problems(voice_only, 48, True, exchange=True)
    assert any('"B" speaks only as a voice-over' in x for x in left), left
    # 긴 영화는 줄 수를 늘려야 한다 (90초면 6줄)
    left = video.dialogue_problems(_dlg_good(), 90, True)
    assert any("at least 6 spoken lines" in x for x in left), left
    # 줄 수·대화 규칙은 다른 위반이 많아도 잘리지 않게 앞에 놓인다
    messy = _copy.deepcopy(thin)
    messy["scenes"][0]["shot"] = "wide"
    left = video.dialogue_problems(messy, 48, True, exchange=True)
    assert len(left) <= 16 and "spoken lines" in left[0]


def test_dialogue_story_prompt_states_the_line_and_conversation_rules():
    with duo_setting(True):          # 두세 사람 규칙에 맞는 답 (듣는 사람이 화면에 있다)
        call = Call([_dlg_duo_good()])
        asyncio.run(video.write_dialogue_story(call, "니키와 푸토르가 숲속을 걸어가며 대화하는 40초 영상", "", 48))
        system = call.calls[0]["messages"][0]["content"]
        assert "at least 3 spoken lines in total" in system and "The request is a conversation" in system
        assert "never more than 2 lines in a row" in system and "A listener answers in words" in system
        # 대화가 아닌 요청은 줄 수만 말하고 번갈아 말하기는 요구하지 않는다
        call = Call([_dlg_good()])
        asyncio.run(video.write_dialogue_story(call, "할아버지가 \"고통은 지나간다\" 라고 말하는 영상", "", 48))
        system = call.calls[0]["messages"][0]["content"]
        assert "at least 3 spoken lines in total" in system and "The request is a conversation" not in system
    # 한 사람만 말하는 구성안은 대화 요청에서 고쳐 쓰게 한다: 첫 답이 모자라면 다시 쓰라는 말이 규칙을 그대로 담는다
    # (예전 혼자 클로즈업 규칙에서는 _dlg_good, 두세 사람 규칙에서는 듣는 사람을 화면에 둔 _dlg_duo_good 이 고친 답이다)
    thin = _dlg_good()
    for sc in thin["scenes"]:
        sc.update(kind="shot", speaker="", line_ko="", emotion="")
    thin["scenes"][3].update(kind="talk", speaker="B", line_ko="숨을 고르거라.", emotion="warm")
    for duo, fixed in ((False, _dlg_good()), (True, _dlg_duo_good())):
        with duo_setting(duo):
            call = Call([thin, fixed])
            story, left, rounds = asyncio.run(video.write_dialogue_story(call, "두 사람이 대화하는 영상", "", 48))
            assert rounds == 2 and left == [] and len([x for x in story["scenes"] if x["line_ko"]]) == 3, (duo, left)
            repair = call.calls[1]["messages"][-1]["content"]
            assert "at least 3 spoken lines" in repair and 'only "B" speaks' in repair


# ── 인물 라이브러리 (11-19): 니키·푸토르는 정해진 모습·목소리·기준 그림·그림체로 고정한다 ──
def test_cast_library_file_is_valid_and_fits_the_story_checks():
    from app import cast_lib
    lib = cast_lib.library()
    assert [e["id"] for e in lib] == ["niki", "putor", "beth", "james", "james_glasses"]
    for e in lib:
        assert len(e["look"].split()) <= 45, e["id"]                 # 구성안 검사는 외형 글이 60낱말을 넘으면 고치게 한다
        assert not video.HANGUL_RE.search(e["look"] + e["voice"] + e["style"])
        assert video.story_style({"bible": {"style": e["style"]}}), "라이브러리 그림체가 실사로 읽히면 안 된다"
        assert e["ref"].endswith(".png") and not e["ref"].startswith("/") and ".." not in e["ref"]
        assert e["voice_ko"] and video.HANGUL_RE.search(e["names"][0])
        # 승인된 기준 소리: 영상마다 목소리를 새로 설계하지 않고 이 소리를 따라 대사를 녹음한다 (11-19 메인 시험 2: 새로 설계한
        # 인사말 기준 소리에서 80세 푸토르가 젊은 목소리로 나왔다). 소리와 글이 맞아야 해서 글은 소리 파일이 말하는 그대로다
        assert e["voice_ref"].endswith((".flac", ".mp3", ".wav")) and not e["voice_ref"].startswith("/") and ".." not in e["voice_ref"]
        assert video.HANGUL_RE.search(e["voice_ref_text"]) and len(e["voice_ref_text"]) <= 120
    assert lib[1]["voice_fx"] == "bass" and "voice_fx" not in lib[0]
    # 외형 글에 쓰는 소품이 구성안 검사가 막는 소품 단어와 맞는다: 모자(니키)·수염·안경(푸토르)이 외형에 있다
    assert "hat" in lib[0]["look"] and "beard" in lib[1]["look"] and "glasses" in lib[1]["look"]
    # 니키의 부모 베스·제임스 그린 (11-20 사용자): 확정한 반실사 컨셉아트(니키_반실사화_수정본)의 외형 — 베스는 호박 브로치·앞치마, 제임스는 파란 카디건
    assert "brooch" in lib[2]["look"] and "apron" in lib[2]["look"] and "cardigan" in lib[3]["look"]
    assert [e["id"] for e in cast_lib.known_in("제임스 그린과 베스 그린, 니키가 아침을 먹는다")] == ["james", "james_glasses", "beth", "niki"]
    assert [e["id"] for e in cast_lib.known_in("엄마(베스)와 아빠(제임스)")] == ["beth", "james", "james_glasses"]


def test_cast_library_variant_is_the_same_person_in_another_look():
    """제임스는 무언가에 집중할 때만 안경을 쓴다 (11-20): 변형 항목은 기본 인물을 따라 나오고, 목소리·말투는 이어받고, 이름이 겹쳐도 바르게 맞는다"""
    from app import cast_lib
    lib = {e["id"]: e for e in cast_lib.library()}
    j, g = lib["james"], lib["james_glasses"]
    assert g["variant_of"] == "james" and "glasses" in g["look"] and "glasses" not in j["look"]
    assert all(g[k] == j[k] for k in ("voice", "voice_ko", "voice_ref", "voice_ref_text", "speech", "style")), "목소리·말투·그림체는 기본 인물 것"
    assert g["ref"] != j["ref"] and g["look"] != j["look"]
    # 변형은 이름으로 찾지 않고 기본 인물이 나오면 바로 뒤에 따라 나온다. 기본 인물이 없으면 나오지 않는다
    assert [e["id"] for e in cast_lib.known_in("제임스가 책을 읽는다")] == ["james", "james_glasses"]
    assert [e["id"] for e in cast_lib.known_in("안경 쓴 니키")] == ["niki"]
    # 설정집 인물 맞추기: id 가 그대로면 그 항목, 이름이 같으면 그 항목, 부분 일치는 가장 긴 이름 (변형 이름은 기본 인물 이름을 품는다)
    entries = cast_lib.known_in("제임스")
    m = lambda c: cast_lib.match_character(c, entries)["id"]
    assert m({"id": "james_glasses", "name_ko": ""}) == "james_glasses"
    assert m({"id": "A", "name_ko": "제임스 그린(안경)"}) == "james_glasses"
    assert m({"id": "B", "name_ko": "제임스 그린 (안경)"}) == "james_glasses"
    assert m({"id": "C", "name_ko": "제임스"}) == "james" and m({"id": "D", "name_ko": "아빠 제임스"}) == "james"
    # 구성안 지시문: 변형은 '집중할 때만, 한 장면에 둘이 함께는 안 된다'를 알린다. 기본 인물 줄에는 그 말이 없다
    block = cast_lib.prompt_block(entries)
    assert block.count("same person") == 1 and "concentrating on something" in block and "never put both entries in one scene" in block
    story = {"bible": {"characters": [{"id": "A", "name_ko": "제임스 그린(안경)", "look": "x"}, {"id": "B", "name_ko": "제임스", "look": "y"}], "style": ""},
             "scenes": []}
    got = cast_lib.apply_to_story(story, entries, "")
    chars = {c["id"]: c for c in story["bible"]["characters"]}
    assert got == {"A": "james_glasses", "B": "james"} and chars["A"]["ref"] == g["ref"] and chars["B"]["ref"] == j["ref"]
    assert chars["A"]["voice_ref"] == chars["B"]["voice_ref"]


def test_dialogue_stretch_fills_the_requested_length_with_silent_scenes():
    import copy as _copy
    story = video.clean_dialogue_story(DLG_OK, 48)
    # 대사가 짧아 말하는 장면이 5초로 줄었다 (11-19 메인 시험: 1분 요청이 40초로 줄었다)
    for i in (2, 3):
        story["scenes"][i]["seconds"] = 5
    before = [s["seconds"] for s in story["scenes"]]
    assert before == [8, 10, 5, 5, 6, 9] and story["scenes"][2]["kind"] == story["scenes"][3]["kind"] == "talk"
    assert video.stretch_scenes(story, 60.0) is True
    after = [s["seconds"] for s in story["scenes"]]
    assert after[2:4] == [5, 5]                                              # 말하는 장면은 늘리지 않는다 (끝이 어두워져 잘라 쓴다)
    # 필요한 길이 = 60 + 디졸브 0.5 × 5 + 자르기 손실 0.5 × 2 - 43 = 20.5초 → 20초를 늘린다
    assert sum(after) == 63 and story["seconds"] == 63
    assert all(after[i] <= video.STRETCH_WIDE_MAX for i in (0, 1, 5)) and after[4] == video.STRETCH_CLOSE_MAX    # 듣는 얼굴은 상한 10초까지만
    assert after[0] > before[0] and after[1] > before[1] and after[5] > before[5]    # 넓은 장면이 먼저, 고르게 늘어난다
    # 이미 충분히 길면 그대로, 말 없는 장면이 없으면 그대로
    long_enough = video.clean_dialogue_story(DLG_OK, 48)
    assert video.stretch_scenes(long_enough, 40.0) is False
    only_talk = _copy.deepcopy(story)
    for sc in only_talk["scenes"]:
        sc["kind"] = "talk"
    assert video.stretch_scenes(only_talk, 120.0) is False
    assert video.stretch_scenes(story, 0) is False and video.stretch_scenes({"scenes": []}, 60.0) is False


def test_dialogue_reference_voice_is_in_character_short_and_capped():
    assert len(video.VOICE_REF_TEXT) < 25 and video.VOICE_REF_TEXT.count(".") <= 2     # 기본 글은 짧은 두 문장 이하 (이전은 세 문장 31자)
    assert video.VOICE_REF_TOKENS == 256
    assert "6 Korean characters per second" in video.DIALOGUE_EXTRA
    # 11-19 메인 시험 2: 인사말 기준 소리를 목소리 특징만 복제하니 늙은 인물이 젊은 목소리로, 책 읽는 투로 나왔다.
    # 구성안이 인물마다 '그 인물이 할 법한 말'(sample_ko)과 '말하듯 감정이 드러나는 대사'를 쓰게 한다
    assert '"sample_ko"' in video.DIALOGUE_EXTRA and "never read from a page" in video.DIALOGUE_EXTRA
    assert "interjections" in video.DIALOGUE_EXTRA and '"..."' in video.DIALOGUE_EXTRA
    assert "sample_ko" in video.DIALOGUE_SCHEMA["properties"]["bible"]["properties"]["characters"]["items"]["properties"]
    # 샘플 글은 한글이고 짧을 때만 쓴다 (맥락 복제는 기준 소리를 자르지 못해서 늘어지면 안 된다)
    import copy as _copy
    raw = _copy.deepcopy(DLG_OK)
    raw["bible"]["characters"][0]["sample_ko"] = "Hello there, I am Niki."                  # 한글이 아님 → 기본 글
    raw["bible"]["characters"][1]["sample_ko"] = "허" * (video.SAMPLE_MAX + 1)               # 너무 김 → 기본 글
    story = video.clean_dialogue_story(raw, 48)
    assert [c["sample_ko"] for c in story["bible"]["characters"]] == ["", ""]
    assert video.voice_sample(story, "A") == video.voice_sample(story, "B") == video.VOICE_REF_TEXT
    # 기준 소리가 글 길이에 비해 늘어졌는지 보는 한도: 기본 글 약 15초, 긴 샘플 글은 그만큼 더 길다
    assert 14 <= video.voice_ref_max_seconds(video.VOICE_REF_TEXT) <= 15 and video.voice_ref_max_seconds("가" * 40) == 24.0


def test_cast_library_has_style_refs_and_signature_and_style_key_picks_the_film_style():
    """11-20 장면 잇기: 인물마다 화풍별 기준 그림(refs)·알아볼 소품(signature), 화풍 글 → 열쇠, 예전 ref 는 그대로"""
    from app import cast_lib
    lib = {e["id"]: e for e in cast_lib.library()}
    for e in lib.values():
        assert isinstance(e["refs"], dict) and set(e["refs"]) <= set(cast_lib.STYLE_KEYS), e["id"]
        assert e["refs"]["semi"].endswith(".png") and not e["refs"]["semi"].startswith("/") and ".." not in e["refs"]["semi"]
        assert 2 <= len(e["signature"]) <= 5 and not any(video.HANGUL_RE.search(s) for s in e["signature"]), e["id"]
        assert cast_lib.ref_for(e, "semi") == e["refs"]["semi"] and cast_lib.ref_for(e, "legacy") == e["ref"]
        assert cast_lib.ref_for(e, "photo") is None and cast_lib.ref_for(e, "anime") is None     # 다른 화풍에 예전 그림을 대신 내주지 않는다
    assert lib["niki"]["refs"]["semi"] == "admin/main/2026-11-20/chaintest/S1_niki_semi_00001_.png"
    assert lib["putor"]["refs"]["semi"] == "admin/main/2026-11-20/chaintest/S2_putor_semi_00001_.png"
    assert lib["niki"]["ref"] == "admin/main/2026-11-19/cast/niki_00001_.png"                     # 예전 기준 그림은 그대로
    assert lib["beth"]["refs"]["semi"] == lib["beth"]["ref"] and lib["james"]["refs"]["semi"] == lib["james"]["ref"]
    assert "wine-red round-brimmed hat" in lib["niki"]["signature"]
    # 변형은 기본 인물의 그림·소품을 이어받지 않는다 (자기 것이 있다)
    assert lib["james_glasses"]["refs"]["semi"] == lib["james_glasses"]["ref"] != lib["james"]["refs"]["semi"]
    assert lib["james_glasses"]["signature"] != lib["james"]["signature"]
    assert cast_lib.ref_for({}, "semi") is None and cast_lib.ref_for({"refs": {"semi": ""}}, "semi") is None
    assert cast_lib.ref_for({"ref": "a.png"}, "semi") is None and cast_lib.ref_for({"ref": "a.png"}, "legacy") == "a.png"
    # 화풍 글 → 열쇠
    key = cast_lib.style_key
    assert key(lib["niki"]["style"]) == "semi" and key("") == "semi" and key(None) == "semi"
    assert key("semi-realistic digital painting") == "semi" and key("반실사 컨셉아트") == "semi"
    assert key("photorealistic live-action film still") == "photo" and key("실사 영화") == "photo" and key("a cinematic photograph") == "photo"
    assert key("2D anime style") == "anime" and key("cel-shaded cartoon") == "anime" and key("애니메이션") == "anime" and key("만화 느낌") == "anime"
    # 11-24 2차: 비어 있지 않은데 아무 말에도 안 맞는 화풍 글은 custom (원문을 그대로 쓴다), 빈 글만 반실사
    assert key("2.5D concept art") == "semi" and key("something unknown") == "custom"
    # 이야기 설정집에 refs·signature 가 복사되고 ref 도 그대로 간다
    story = {"bible": {"characters": [{"id": "A", "name_ko": "니키", "look": "x"}, {"id": "B", "name_ko": "행인", "look": "y"}]}}
    cast_lib.apply_to_story(story, cast_lib.library())
    c = story["bible"]["characters"][0]
    assert c["refs"] == lib["niki"]["refs"] and c["signature"] == lib["niki"]["signature"] and c["ref"] == lib["niki"]["ref"]
    assert "refs" not in story["bible"]["characters"][1]


def test_cast_library_finds_names_in_the_request_and_matches_story_characters():
    from app import cast_lib
    assert [e["id"] for e in cast_lib.known_in("니키와 푸토르가 숲속을 걸어가며 대화")] == ["niki", "putor"]
    assert [e["id"] for e in cast_lib.known_in("푸토르 할아버지가 니키에게 설명한다")] == ["putor", "niki"]    # 글에 나온 차례
    assert [e["id"] for e in cast_lib.known_in("NIKI 와 아이가 걷는다")] == ["niki"]
    assert cast_lib.known_in("Nikita 가 걷는다") == [] and cast_lib.known_in("비 오는 거리") == [] and cast_lib.known_in("") == []
    lib = cast_lib.library()
    assert cast_lib.match_character({"id": "B", "name_ko": "푸토르 할아버지"}, lib)["id"] == "putor"
    assert cast_lib.match_character({"id": "putor", "name_ko": ""}, lib)["id"] == "putor"
    assert cast_lib.match_character({"id": "C", "name_ko": "이웃"}, lib) is None
    block = cast_lib.prompt_block(lib)
    assert 'name_ko "니키"' in block and 'name_ko "푸토르"' in block and "golden-ochre coat" in block
    # 화풍 줄은 한 가지 (3차 명세 2.2): 요청이 화풍을 말하지 않았으면 라이브러리 화풍을 베낀다
    assert "you copy this into bible.style" in block and "voice" not in block.lower().replace("visual style", "")
    # 말투: 푸토르는 "얘야, ~란다" 할아버지 말투, 니키는 "할아버지"라고 부르는 수줍은 존댓말 (모델이 푸토르를 "선생님" 존댓말로 쓴 적이 있다)
    assert "Speech: a warm old grandfather" in block and "-란다" in block and "얘야" in block
    assert "Speech: shy and polite" in block and "할아버지" in block and "in the given speech manner" in block
    assert "11-19" not in block and "메인 시험" not in block                          # 설명용 메모는 지시문에 들어가면 안 된다
    assert cast_lib.prompt_block([]) == "" and cast_lib.prompt_block(None) == ""


def test_cast_library_overrides_the_story_bible_and_the_style():
    import copy as _copy
    from app import cast_lib
    lib = cast_lib.library()
    story = video.clean_dialogue_story(DLG_OK, 48)
    chars = {c["name_ko"]: c for c in story["bible"]["characters"]}
    assert chars["니키"]["look"] != lib[0]["look"]
    matched = cast_lib.apply_to_story(story, lib, "니키와 푸토르가 대화", keep_style=video.story_style(story) != "")
    assert matched == {"A": "niki", "B": "putor"}
    a, b = story["bible"]["characters"]
    assert a["look"] == lib[0]["look"] and b["look"] == lib[1]["look"] and a["library"] == "niki"
    assert a["voice"] == lib[0]["voice"] and b["voice_fx"] == "bass" and "voice_fx" not in a
    assert a["ref"] == lib[0]["ref"] and b["ref"] == lib[1]["ref"] and a["name_ko"] == "니키"
    # 구성안의 그림체: 사용자가 말한 화풍(비사실)은 지킨다
    assert story["bible"]["style"] == "semi-realistic painterly 2.5D fantasy film look"
    # 모델이 실사로 정했거나 그림체를 안 정했으면 라이브러리 그림체로, 사용자가 실사를 말했으면 그대로 둔다
    real = _copy.deepcopy(story)
    real["bible"]["style"] = "photorealistic cinematic live action"
    assert cast_lib.apply_to_story(real, lib, "니키와 푸토르가 대화", keep_style=False)
    assert real["bible"]["style"] == lib[0]["style"]
    real["bible"]["style"] = "photorealistic cinematic live action"
    cast_lib.apply_to_story(real, lib, "니키와 푸토르가 실사 영화처럼 대화", keep_style=False)
    assert real["bible"]["style"] == "photorealistic cinematic live action"
    # 라이브러리에 없는 인물만 있으면 아무것도 바꾸지 않는다
    other = _copy.deepcopy(story)
    other["bible"]["characters"][0].update(name_ko="이웃", id="Z", look="a tall man in a grey coat and a black scarf")
    other["bible"]["characters"] = other["bible"]["characters"][:1]
    before = _copy.deepcopy(other)
    assert cast_lib.apply_to_story(other, lib, "이웃이 걷는다") == {} and other == before
    # 이름도 id 도 다르지만 외형 글이 라이브러리 외형을 그대로 옮긴 인물은 같은 인물이다 (대사 없는 영상의 구성안에는 name_ko 칸이 없고
    # 모델이 id 를 'A' 로 쓸 수 있다). 외형이 다르면 같은 인물이 아니다
    same = {"id": "A", "look": lib[1]["look"].replace("wild white hair", "wild  white hair,")}
    assert cast_lib.match_character(same, lib)["id"] == "putor"
    assert cast_lib.match_character({"id": "A", "look": "an old man with a white beard and a brown hat"}, lib) is None
    assert cast_lib.match_character({"id": "putor", "look": "anything"}, lib)["id"] == "putor"           # id 가 라이브러리 id 와 같다


def test_style_key_order_one_photo_detector_and_said_style():
    """11-24 화풍 일반화(2차): 열쇠 순서(분명한 반실사 → 애니(3D 와 함께면 3D 애니) → 3D 애니 → 실사 → custom 말 → 그림 말 → 빈 글은 반실사,
    나머지는 custom), 실사 판정은 한 함수, 요청 글이 화풍을 말했는가"""
    from app import cast_lib
    key = cast_lib.style_key
    lib_style = cast_lib.library()[0]["style"]
    # ① 분명한 반실사 말이 먼저 (라이브러리 화풍 글의 'semi-realistic' 이 실사 낱말에 걸리지 않게). 한글이 붙은 '2.5D 느낌'도
    for s in (lib_style, "semi-photorealistic digital painting", "반실사 컨셉아트", "2.5D 느낌의 판타지", "2.5D concept art"):
        assert key(s) == "semi" and not cast_lib.is_photo_style(s), s
    # ② 애니 말은 그림 말보다 먼저 (11-18 애니 시험 문장이 'painted' 때문에 반실사로 읽혔다)
    for s in ("2D anime film with painterly watercolor backgrounds", "cel shading, flat colours", "웹툰 그림체", "2D로 그린 만화",
              "cel-shaded cartoon"):
        assert key(s) == "anime", s
    # ② 의 단서 (2차): 3D 와 함께면 3D 애니(애니 문장의 'not a 3D render' 가 부딪힌다), 2D 와 3D 를 함께 말한 섞인 화풍은 custom
    for s in ("Pixar-like 3D animated feature", "stylized 3D CG animation"):
        assert key(s) == "toon3d", s
    assert key("2D hand-drawn lines on cel-shaded 3D characters, soft painted backgrounds, bright pastel colours") == "custom"
    # ③ 실사 말
    for s in ("photorealistic cinematic live action", "live-action film still", "a realistic photo look", "a cinematic photograph",
              "사진처럼 생생하게", "사진 같은 화면", "실사 영화"):
        assert key(s) == "photo" and cast_lib.is_photo_style(s), s
    # 수채·유화·점토·3D 렌더는 2차부터 custom (원문을 그대로 쓴다). 빈 글만 반실사
    for s in ("watercolour storybook illustration", "oil painting with thick strokes", "수채화 동화책", "유화 느낌",
              "3D render, octane", "claymation", "something unknown", "hyper-realistic oil painting", "watercolour with realistic lighting"):
        assert key(s) == "custom", s
    # 사실적인 영화 말은 실사 (3차 검토: 실사 요청을 따른 구성안 화풍이 custom 으로 읽혀 실사 영화에 '사진 아님' 문장이 붙었다). 다른 화풍 말과
    # 함께면 그 화풍의 꾸밈말이다 (위 'hyper-realistic oil painting')
    for s in ("realistic cinematic film", "realistic cinematic film look with natural light", "hyper-realistic cinematic look",
              "cinematic realism, natural lighting", "lifelike cinematic style", "naturalistic film look"):
        assert key(s) == "photo" and cast_lib.is_photo_style(s), s
    for s in ("", None, "   "):
        assert key(s) == "semi", s
    # '아닌 것' 구절은 그 화풍으로 읽지 않는다 (구성안 화풍 글의 "not a cartoon"·"non-photorealistic")
    assert key("photorealistic live action, not a cartoon, not a painting") == "photo"
    assert key("hand-drawn anime, never photographic") == "anime" and key("non-photorealistic watercolour") == "custom"
    assert key("excellent cello celebration") == "custom" and key("animal documentary, photorealistic") == "photo"      # 'cel' 은 낱말로만
    # 'non-' 은 바로 붙은 낱말만 부정한다: 같은 구절의 애니 말은 남는다 (11-24 검토: 'non-realistic 2D animation' 이 반실사로 읽혔다).
    # not 은 쉼표까지 ('not a cel-shaded cartoon' 은 구절 전체가 아닌 것)
    assert key("non-realistic 2D animation, soft colors") == "anime" and key("Non-photorealistic anime look, flat colors") == "anime"
    assert key("photorealistic, not a cel-shaded cartoon") == "photo" and key("nonphotorealistic watercolour") == "custom"
    # ② 애니 말은 ④ 실사 말보다 먼저 (3D 애니는 3D 애니 열쇠, 실사 배경 위의 애니 인물은 애니 열쇠)
    assert key("photorealistic 3D animated feature") == "toon3d"
    assert key("anime characters over live-action photographed backgrounds") == "anime"
    # 실사 판정은 한 함수: story_style(사실적이지 않은 화풍만 돌려준다)과 열쇠가 서로 어긋나지 않는다
    for s in ("semi-photorealistic digital painting", "photorealistic cinematic live action", "2D anime", "realistic cinematic film",
              "a cinematic photograph", lib_style, "watercolour, not photorealistic", "cinematic live-action drama"):
        assert (video.story_style({"bible": {"style": s}}) == "") == (key(s) == "photo"), s
    # 요청 글이 화풍을 말했는가: 열쇠 말 + 3D·픽사·클레이·코믹스·동화·그림체·화풍. '코믹한'·'운동화'·정해진 모습을 쓰라는 말은 아니다
    said = cast_lib.said_style
    for t in ("니키와 푸토르가 대화하는 애니메이션", "실사 영화처럼", "수채화 느낌의 숲", "3D 픽사 느낌으로", "클레이 애니", "코믹스 그림체",
              "동화풍 숲속", "지브리 화풍", "따뜻한 화풍으로", "반실사로", "웹툰처럼", "사진처럼 찍은", "hand-drawn look"):
        assert said(t), t
    # '동화 같은 숲속' 은 분위기 말이다 (화풍 표지가 없다, 11-24 2차 검토: 동화 화풍으로 읽어 라이브러리 화풍을 잃었다)
    for t in ("니키와 푸토르가 숲길을 걸어가는 영상", "코믹한 대화", "운동화를 신은 니키", "자동화 공장", "니키 원래 그림체 그대로",
              "그림체는 유지해 줘", "사진을 찍는 제임스", "동화 같은 숲속", "", None):
        assert not said(t), t
    assert cast_lib.said_photo("실사 영화처럼") and not cast_lib.said_photo("반실사로") and not cast_lib.said_photo("애니메이션")


def test_library_style_is_not_forced_when_the_request_names_a_style():
    """11-24: 니키가 나오는 애니 요청에 '라이브러리 화풍(반실사)을 bible.style 에 베껴라' 줄이 같이 가서 반실사가 됐다.
    요청이 화풍을 말하면 그 줄 대신 요청 화풍을 쓰게 하고, 구성안 화풍을 라이브러리 화풍으로 덮어쓰지 않는다.
    11-25 3차 명세 2.2: 지시문의 화풍 줄은 요청 글과 상관없이 한 가지가 됐다 — '요청이 화풍을 말했으면 그 말을 bible.style_said 에, 그 화풍을
    bible.style 에, 아니면 라이브러리 화풍을 베낀다'. 아래 덮어쓰기는 style_said 칸이 없는 구성안의 예전 경로다 (요청 글을 읽는다)"""
    import copy as _copy
    from app import cast_lib
    lib = cast_lib.library()
    lib_style = lib[0]["style"]
    plain = cast_lib.prompt_block(lib, text="니키와 푸토르가 대화")
    assert "you copy this into bible.style" in plain and lib_style in plain
    anime = cast_lib.prompt_block(lib, text="니키와 푸토르가 대화하는 2D 애니메이션")
    # fixC: 화풍 줄이 '화풍에 대해 무엇이든 말했으면(청했거나 거절했으면)' 으로 바뀌었다 (11-25 최종 확인 05#1)
    assert anime == plain and "write the user's words into bible.style_said and write into bible.style the look asked for" in anime
    assert "golden-ochre coat" in anime                                                            # 인물 외형은 그대로
    silent = cast_lib.prompt_block(lib, speech=False, by_id=True, text="니키가 걷는 실사 영화")
    assert silent == cast_lib.prompt_block(lib, speech=False, by_id=True) and "bible.style_said" in silent
    assert lib_style in cast_lib.prompt_block(lib, text="니키 원래 그림체 그대로")              # 정해진 모습을 쓰라는 말은 화풍이 아니다
    base = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)

    def applied(style, text, keep=None):
        st = _copy.deepcopy(base)
        st["bible"]["style"] = style
        cast_lib.apply_to_story(st, lib, text, keep_style=bool(video.story_style(st)) if keep is None else keep)
        return st["bible"]["style"]
    # 사용자가 말한 화풍은 지킨다 (모델이 그 화풍으로 썼다)
    assert applied("2D anime film, clean cel-shaded line art", "니키와 푸토르가 대화하는 애니메이션") == "2D anime film, clean cel-shaded line art"
    assert applied("soft watercolour storybook illustration", "수채화 느낌으로 니키가 걷는다", keep=False) == "soft watercolour storybook illustration"
    assert applied("photorealistic cinematic live action", "니키와 푸토르가 실사 영화처럼 대화") == "photorealistic cinematic live action"
    # 말하지 않았으면 지금 규칙: 실사·빈 화풍은 라이브러리 화풍, 사실적이지 않은 구성안 화풍(반실사·애니·3D 애니로 읽히는 글)은 그대로
    assert applied("photorealistic cinematic live action", "니키와 푸토르가 대화") == lib_style
    assert applied("", "니키와 푸토르가 대화") == lib_style
    assert applied("warm painterly fantasy look", "니키와 푸토르가 대화") == "warm painterly fantasy look"
    # 2차 검토: 화풍을 말하지 않은 요청에서 구성안 화풍이 custom 으로 읽히는 글이면 라이브러리 화풍 (custom 은 사용자가 말한 화풍에만).
    # 그래서 열쇠가 반실사라 기준 그림을 바꾸지 않고(refs.semi 그대로) 장소 그림도 빠른 판이다
    for style in ("whimsical fantasy concept art with soft light", "warm painterly storybook look"):
        got = applied(style, "니키와 푸토르가 숲길을 걸으며 대화하는 영상")
        assert got == lib_style and video.style_key({"bible": {"style": got}}) == "semi", (style, got)
    # 화풍을 말했는데 구성안이 따르지 않았으면(비었거나 애니 요청에 실사) 요청 화풍의 기본 글로 채운다 (11-24 검토: 애니 요청이 반실사로
    # 덮였다). 반실사·그림 말이면 라이브러리 화풍(반실사)이 그 화풍이다
    anime_text = cast_lib.SAID_STYLE_TEXT["anime"]
    assert applied("", "애니메이션으로") == anime_text and video.style_key({"bible": {"style": anime_text}}) == "anime"
    assert applied("photorealistic cinematic live action", "니키와 푸토르가 대화하는 애니메이션") == anime_text
    assert applied("", "니키와 푸토르가 실사 영화처럼") == cast_lib.SAID_STYLE_TEXT["photo"]
    assert video.style_key({"bible": {"style": cast_lib.SAID_STYLE_TEXT["photo"]}}) == "photo"
    assert applied("photorealistic film", "반실사로") == lib_style
    # 2차: 수채화는 custom 화풍이라 반실사 라이브러리 화풍으로 덮지 않고 요청의 화풍 말을 영어로 옮겨 채운다. 3D 애니는 그 열쇠의 기본 글
    assert applied("", "수채화 느낌으로") == "watercolour painting style" and video.style_key({"bible": {"style": applied("", "수채화 느낌으로")}}) == "custom"
    assert applied("photorealistic cinematic live action", "니키가 나오는 픽사 같은 3D 애니") == cast_lib.SAID_STYLE_TEXT["toon3d"]
    assert video.style_key({"bible": {"style": cast_lib.SAID_STYLE_TEXT["toon3d"]}}) == "toon3d"
    assert applied("", "니키가 나오는 몽환적인 화풍") == lib_style                     # custom 이지만 옮길 화풍 말이 없으면 라이브러리 화풍
    # '만화책을 읽는' 같은 물건 말은 화풍을 말한 것이 아니다: 지금 규칙(라이브러리 화풍), 구성안 지시문에도 라이브러리 화풍 줄
    assert applied("photorealistic cinematic live action", "니키가 만화책을 읽는다") == lib_style
    assert not cast_lib.said_style("니키가 만화책을 읽는다") and cast_lib.said_style("만화 그림체로") and not cast_lib.said_style("reading a comic book")
    assert lib_style in cast_lib.prompt_block(lib, text="니키가 만화책을 읽는다")
    assert cast_lib.said_key("니키가 만화책을 읽는다") is None and cast_lib.said_key("애니메이션으로") == "anime"
    assert cast_lib.said_key("실사 영화처럼") == "photo" and cast_lib.said_key("반실사로") == "semi"
    assert cast_lib.keeps_said_style("애니메이션으로", "2D anime") and not cast_lib.keeps_said_style("애니메이션으로", "photorealistic film")
    assert cast_lib.keeps_said_style("실사로", "photorealistic film") and not cast_lib.keeps_said_style("대화", "2D anime")
    assert not cast_lib.keeps_said_style("실사로", "") and not cast_lib.keeps_said_style("애니메이션으로", "")


def test_storyboard_with_library_characters_keeps_the_requested_style():
    """11-24: '니키와 푸토르가 대화하는 애니메이션' — 모델이 쓴 애니 화풍이 그대로 남는다.
    모델이 요청 화풍을 놓치고 실사로 쓰면 요청 화풍(애니)의 기본 글로 채운다 (예전에는 라이브러리의 반실사로 덮였다, 11-24 검토).
    11-25 3차 명세 2.2: 구성안 지시문의 화풍 줄은 한 가지다 (요청 화풍을 말했으면 style_said·style 에, 아니면 라이브러리 화풍). 여기 가짜 모델은
    style_said 칸을 쓰지 않아 덮어쓰기는 예전 경로(요청 글)다"""
    from app import cast_lib
    turns, want = [], {"style": ""}
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    silent_story = {"title_ko": "숲길", "logline_ko": "두 사람이 숲길을 걷는다.",
                    "bible": {"characters": [{"id": "A", "look": "a tall woman in a grey coat"}, {"id": "putor", "look": "an old man"}],
                              "places": [{"id": "P1", "look": "a winding forest path"}], "time": "morning", "light": "soft"},
                    "music": {"tags": "calm piano", "bpm": 70, "key": "D major", "ko": "잔잔한 피아노"},
                    "scenes": [{"seconds": 14, "place": "P1", "characters": ["A", "putor"], "continues": False, "shot": "wide shot",
                                "action": "They walk along the path.", "sound": "footsteps", "ko": "걷는다"},
                               {"seconds": 14, "place": "P1", "characters": ["putor"], "continues": False, "shot": "medium shot",
                                "action": "He stops and looks ahead.", "sound": "birds", "ko": "멈춘다"}]}

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        async def call(payload):
            turns.append(payload["messages"][0]["content"])
            story = json.loads(json.dumps(DLG_OK if want.get("dialogue", True) else silent_story))
            if want.get("dialogue", True):
                story["scenes"][3]["continues"] = False
            story["bible"]["style"] = want["style"]
            return {"message": {"content": json.dumps(story)}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        lib_style = cast_lib.library()[0]["style"]
        ask = "니키와 푸토르가 숲속에서 대화하는 애니메이션"
        want["style"] = "2D anime film with clean cel-shaded line art and flat bright colours"
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", ask, seconds=48, dialogue=True))
        assert turns and "write the user's words into bible.style_said and write into bible.style the look asked for" in turns[0]   # fixC 화풍 줄
        assert r["story"]["bible"]["style"] == want["style"] and r["cast"] == {"A": "niki", "B": "putor"}
        assert video.style_key(r["story"]) == "anime" and video.story_style(r["story"]) == want["style"]
        turns.clear()
        want["style"] = "photorealistic cinematic live action"
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", ask, seconds=48, dialogue=True))
        assert r["story"]["bible"]["style"] == cast_lib.SAID_STYLE_TEXT["anime"] and video.style_key(r["story"]) == "anime"
        # 대사 없는 영상도 같은 규칙 (구성안 지시문은 id 칸): 실사를 말했으면 실사 그대로, 화풍을 말하지 않았으면 라이브러리 화풍
        turns.clear()
        want.update(style="photorealistic cinematic live action", dialogue=False)
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키와 푸토르가 숲길을 걷는 실사 영화", seconds=28))
        assert "bible.style_said" in turns[0] and '- id "putor" (푸토르)' in turns[0] and r["cast"] == {"putor": "putor"}
        assert r["story"]["bible"]["style"] == "photorealistic cinematic live action"
        turns.clear()
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키와 푸토르가 숲길을 걷는 영상", seconds=28))
        assert lib_style in turns[0] and r["story"]["bible"]["style"] == lib_style
        # 2차 검토: 화풍을 말하지 않은 요청에서 모델이 라이브러리 화풍을 다른 말로 바꿔 써 custom 으로 읽히면 라이브러리 화풍으로 (열쇠 반실사:
        # 승인된 반실사 기준 그림을 그대로 쓰고 장소 그림도 빠른 판). custom 은 사용자가 말한 화풍에만 쓴다
        turns.clear()
        want.update(style="whimsical fantasy concept art with soft light", dialogue=True)
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키와 푸토르가 숲길을 걸으며 대화하는 영상",
                                         seconds=48, dialogue=True))
        assert lib_style in turns[0] and r["story"]["bible"]["style"] == lib_style and video.style_key(r["story"]) == "semi"
        assert all(c.get("refs", {}).get("semi") for c in r["story"]["bible"]["characters"])
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved


def test_known_characters_are_used_in_films_without_dialogue_too():
    """11-20 사용자: 화풍은 대사가 나올 때나 안 나올 때나 항상 같아야 한다. 대사 없는 긴 영상도 요청에 니키·푸토르가 있으면 같은 모습·그림체로"""
    from app import cast_lib
    lib = cast_lib.library()
    silent = cast_lib.prompt_block(lib, speech=False, by_id=True)
    assert 'with the given "id" (shown first)' in silent and '- id "niki" (니키): a twelve-year-old girl' in silent
    assert '- id "putor" (푸토르): a short stocky old dwarf' in silent
    assert "Speech:" not in silent and "spoken lines" not in silent and "name_ko" not in silent      # 대사 없는 영상에는 말투도 이름 칸도 없다
    assert "you copy this into bible.style" in silent and "golden-ochre coat" in silent          # 화풍 줄은 한 가지 (3차 명세 2.2)
    # 대사 영상의 글은 그대로다 (name_ko 칸, 말투)
    talk = cast_lib.prompt_block(lib)
    assert 'name_ko "니키"' in talk and "Speech: a warm old grandfather" in talk and "in the given speech manner" in talk
    turns = []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        async def call(payload):
            turns.append(payload["messages"][0]["content"])
            story = {"title_ko": "숲길", "logline_ko": "두 사람이 숲길을 걷는다.",
                     "bible": {"characters": [{"id": "A", "look": "a tall woman in a grey coat"}, {"id": "putor", "look": "an old man"}],
                               "places": [{"id": "P1", "look": "a winding forest path"}], "time": "morning", "light": "soft",
                               "style": "photorealistic cinematic live action"},
                     "music": {"tags": "calm piano", "bpm": 70, "key": "D major", "ko": "잔잔한 피아노"},
                     "scenes": [{"seconds": 14, "place": "P1", "characters": ["A", "putor"], "continues": False, "shot": "wide shot",
                                 "action": "They walk along the path.", "sound": "footsteps", "ko": "걷는다"},
                                {"seconds": 14, "place": "P1", "characters": ["putor"], "continues": False, "shot": "medium shot",
                                 "action": "He stops and looks ahead.", "sound": "birds", "ko": "멈춘다"}]}
            return {"message": {"content": json.dumps(story)}}
        yield call

    fake.llm_turn, fake.HelperBusy = llm_turn, HelperBusy
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        out = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키와 푸토르가 숲길을 걸어가는 영상", seconds=28))
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved
    system = turns[0]
    assert 'with the given "id"' in system and '- id "niki"' in system and '- id "putor"' in system and "spoken lines" not in system
    story = out["story"]
    # 모델이 id 를 'A' 로 쓰고 외형을 다르게 써도 이름·id 가 아니라 외형이 같은 'putor' 는 id 로 맞고, 'A' 는 아무것도 안 맞아 그대로다
    chars = {c["id"]: c for c in story["bible"]["characters"]}
    assert chars["putor"]["library"] == "putor" and chars["putor"]["look"] == cast_lib.library()[1]["look"]
    assert "library" not in chars["A"] and out["cast"] == {"putor": "putor"}
    assert story["bible"]["style"] == cast_lib.library()[0]["style"]            # 실사로 정한 그림체는 라이브러리 그림체로
    assert not story.get("dialogue")                                           # 대사 영상이 아니다


def test_dialogue_storyboard_tells_the_model_the_known_characters_and_then_applies_them():
    from app import cast_lib
    turns = []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        turns.append(purpose)

        async def call(payload):
            turns.append(payload["messages"][0]["content"])
            story = json.loads(json.dumps(DLG_OK))
            story["scenes"][3]["continues"] = False
            story["bible"]["style"] = "photorealistic cinematic live action"      # 모델이 실사로 정했다
            story["bible"]["characters"][0]["look"] = "a tall woman in a grey coat"      # 모델이 외형을 다르게 썼다
            return {"message": {"content": json.dumps(story)}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat",
                                         "니키와 푸토르가 숲속을 걸어가며 대화하는 40초 영상", seconds=48, dialogue=True))
        system = turns[1]
        assert "Known characters" in system and 'name_ko "니키"' in system and "golden-ochre coat" in system
        lib = cast_lib.library()
        a, b = r["story"]["bible"]["characters"]
        assert a["look"] == lib[0]["look"] and b["look"] == lib[1]["look"] and a["ref"] == lib[0]["ref"]
        assert r["story"]["bible"]["style"] == lib[0]["style"] and r["cast"] == {"A": "niki", "B": "putor"}
        # 이름이 없는 요청은 라이브러리를 건드리지 않는다
        turns.clear()
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat",
                                         "두 사람이 숲속을 걸어가며 대화하는 40초 영상", seconds=48, dialogue=True))
        assert "Known characters" not in turns[1] and r["cast"] == {}
        turns.clear()
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "두 사람이 걷는 30초 영상", seconds=30))
        assert "Known characters" not in turns[1] and r["cast"] == {}
        # 대사 없는 긴 영상도 이름이 나오면 라이브러리를 쓴다 (11-20: 화풍은 대사가 나올 때나 안 나올 때나 같아야 한다). 말투는 뺀다
        turns.clear()
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키가 걷는 30초 영상", seconds=30))
        assert "Known characters" in turns[1] and '- id "niki" (니키)' in turns[1] and "Speech:" not in turns[1] and "cast" in r
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved



# ── 장면 잇기 (11-20 설계): 이음 종류, 편집 글, 닻 ──
def test_scene_seams_continue_cut_jump_refresh():
    import copy as _copy
    st = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    sc = st["scenes"]
    sc[1]["continues"] = True           # 같은 장소에서 동작이 이어짐
    sc[5]["place"] = "P2"               # 장소가 바뀜
    sc[4]["place"] = "P1"
    assert video.scene_seam(st, 0) is None
    assert video.scene_seam(st, 1) == "continue" and video.scene_seam(st, 2) == "cut"
    assert video.scene_seam(st, 5) == "jump"
    sc[3]["refresh"] = True             # 이어 쓰기가 많아 같은 구도를 다시 그린다
    assert video.scene_seam(st, 3) == "refresh"
    sc[4]["time"] = "dusk"              # 같은 장소라도 시간이 바뀌면 jump
    assert video.scene_seam(st, 4) == "jump"
    seams = video.scene_seams(st)
    assert len(seams) == len(sc) - 1 and seams[0] == "continue"
    assert [k for k in seams if k not in video.DISSOLVE_SEAMS] == ["cut"]       # 하드 컷은 cut 뿐 (사용자 11-20)


def test_chain_mark_chains_refresh_instead_of_fresh_image():
    scenes = [{"continues": i > 0, "place": "P1"} for i in range(6)]
    video._mark_chains(scenes)
    assert [bool(x.get("refresh")) for x in scenes] == [False, False, False, False, True, False]
    assert scenes[4]["continues"] is False


def test_chain_anchor_ids_and_pick_refs():
    import copy as _copy
    st = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    assert video.anchor_ids(st) == ["B", "A"] or video.anchor_ids(st) == ["A", "B"]
    ids = video.scene_people(st, 1)
    assert set(ids) == {"A", "B"}
    # 말하는 사람이 먼저, 자리는 2장까지
    assert video.pick_refs(st, 1, ["A", "B"])[0] == "B" and len(video.pick_refs(st, 1, ["A", "B"])) == 2
    assert video.pick_refs(st, 1, ["A"]) == ["A"] and video.pick_refs(st, 0, ["A", "B"]) == []
    st["bible"]["characters"].append({"id": "D", "look": "a small white puppy with floppy ears"})
    st["scenes"][1]["characters"].append("D")
    assert "D" not in video.scene_people(st, 1) and "D" not in video.anchor_ids(st)        # 동물은 사람 기준 그림이 없다


def test_chain_cut_edit_prompt_uses_the_reference_for_faces():
    import copy as _copy
    st = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    pr = video.cut_edit_prompt(st, 1, ["B", "A"], "cut")
    assert "image 1" in pr and "image 2" in pr and "image 3" in pr
    assert "has exactly the face, hair and clothes of image 2" in pr and "not the face of image 1" in pr
    assert "Exactly 2 people" in pr and "faces visible" in pr and "not a glossy 3D render" in pr
    assert "same light and colours as image 1" in pr and "no Korean" or not video.HANGUL_RE.search(pr)
    assert not video.HANGUL_RE.search(pr) and len(pr) <= 1990
    # 장소가 바뀌는 컷은 그림 1 의 빛·색 문장을 빼고 새 장소·시간을 쓴다
    pj = video.cut_edit_prompt(st, 5, ["A"], "jump")
    assert "new place" in pj and "same light and colours as image 1" not in pj and "The place is now:" in pj
    pr2 = video.cut_edit_prompt(st, 4, [], "refresh")
    assert "Redraw the same camera shot as image 1" in pr2 and "Exactly 1" not in pr2 and "exactly one person" in pr2
    # 끝 키프레임·첫 장면 구성·화풍 변환
    ek = video.end_keyframe_prompt(st, 1, "they stop and look at each other.")
    assert "a few seconds later" in ek and "faces visible" in ek and "none walking out of frame" in ek and "Exactly 2 people" in ek
    cp = video.compose_prompt(st, 1, ["A", "B"])
    assert "face, hair and clothes of image 1" in cp and "of image 2" in cp and "Exactly 2 people" in cp
    assert cp.startswith("A new picture:") and "the place shown in image" not in cp
    # 새로 그리기(11-20 승인): 알아볼 소품을 그림 글에 적고, 장소 그림을 넣었으면 그 번호를 적는다
    st2 = _copy.deepcopy(st)
    st2["bible"]["characters"][0]["signature"] = ["wine-red hat", "two black braids"]
    cp2 = video.compose_prompt(st2, 1, ["A"], place_image=2)
    assert "including the wine-red hat, the two black braids; this person appears once." in cp2
    assert "The place is the place shown in image 2, with nobody else in it:" in cp2
    assert video.signature_of(st2, "A") == ["wine-red hat", "two black braids"] and video.signature_of(st2, "B") == []
    pid = st["scenes"][1]["place"]
    assert video.scene_place(st, 1) == pid
    pc = video.place_card_prompt(st, pid)
    assert "nobody in it" in pc and "no person, no animal" in pc
    assert "anime" in video.style_convert_prompt("anime", "girl") and "live-action" in video.style_convert_prompt("photo", "man")
    assert "semi-realistic" in video.style_convert_prompt("semi", "girl")


def test_layout_slots_sizes_poses_and_prompts():
    import copy as _copy
    st = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    assert video.figure_size("a twelve-year-old girl with two long black braids") == "child-sized"
    assert video.figure_size("a short old dwarf herbalist with a long white beard") == "short"
    assert video.figure_size("a calm neat woman around forty with long straight black hair") == "adult-sized"
    assert video.figure_size("a tall lean man in his early forties") == "adult-sized"
    # 자세: 이 장면 글에 단서가 없으면 같은 장소의 앞 장면 (숲길을 걷던 장면 뒤의 클로즈업은 걷는 중)
    # 걷기는 순간 동작이라 물려받지 않는다 (숲길을 걷던 장면 뒤의 클로즈업은 서 있음)
    assert video.scene_pose(st, 1) == "walking" and video.scene_pose(st, 2) == "standing" and video.scene_pose(st, 5) == "standing"
    st_sit = _copy.deepcopy(st)
    st_sit["scenes"][1]["action"] = "The girl and the old dwarf sit at the wooden table."
    assert video.scene_pose(st_sit, 2) == "sitting"
    # 두 사람: 오른쪽을 보는 첫 화자(푸토르)가 왼쪽, 서로 마주 본다. 한 사람은 가운데
    sl = video.layout_slots(st, 1, ["A", "B"])
    assert [s["id"] for s in sl] == ["B", "A"] and [s["pos"] for s in sl] == ["on the left", "on the right"]
    assert [s["facing"] for s in sl] == ["turned toward the right", "turned toward the left"] and [s["size"] for s in sl] == ["short", "child-sized"]
    one = video.layout_slots(st, 2, ["A"])
    assert one == [{"id": "A", "pos": "in the centre", "size": "child-sized", "facing": "turned slightly toward the left"}]
    # ① 구도 글: 장면 글(화면 밖 인물 이름이 섞일 수 있다)은 넣지 않고 형상 수·자리·크기만. 클로즈업은 얼굴을 크게
    lp = video.layout_prompt(st, 2, one)
    assert lp.startswith("Picture 1 shows the place.") and "a winding forest path with mossy trees" in lp
    assert "One child-sized faceless mannequin figure, standing, turned slightly toward the left, in the centre of the frame." in lp
    assert "no figure is seen from behind" in lp
    assert "clear empty space above the hair" in lp and "one third of the way down" in lp and lp.endswith("Exactly 1 figure, no other figure or person.")
    assert "She looks up" not in lp and "hesitant" not in lp
    lp2 = video.layout_prompt(st, 1, sl)
    assert "2 faceless mannequin figures, walking: on the left a short figure turned toward the right; on the right a child-sized figure turned toward the left." in lp2
    # ② 인물 입히기: 배경 문장 필수, 대사 컷은 말하는 방향·감정만 (장면 글 없음)
    ip = video.identity_prompt(st, 2, one, ["A"], final=True)
    assert ip.startswith("Picture 1 is the layout.") and "Replace the faceless figure in the centre with the person in Picture 2, a twelve-year-old girl" in ip
    assert "The background stays exactly the place of Picture 1 (a winding forest path with mossy trees); it is not a plain studio background." in ip
    assert "speaks toward the left side of the frame with a hesitant and a little sad expression" in ip and "She looks up" not in ip
    assert video.ONE_PERSON in ip and "Take nothing else from Picture 2." in ip
    ip2 = video.identity_prompt(st, 1, sl, ["B"], final=False)
    assert "Keep the faceless figure on the right exactly as it is." in ip2 and ip2.endswith("Exactly 2 figures in total, no other person.")
    assert "What happens: The girl and the old dwarf walk side by side" in ip2          # 대사 컷이 아니면 장면 글
    both = video.identity_prompt(st, 1, sl, ["B", "A"], final=True)
    assert "on the left with the person in Picture 2, a short old dwarf herbalist" in both and "on the right with the person in Picture 3" in both
    assert "Take nothing else from Picture 2 or Picture 3." in both and "Exactly 2 people" in both
    # ③ 한 명씩
    fp = video.finish_prompt(st, 1, sl, "A", ["B"], final=True)
    assert fp.startswith("Picture 1 is the picture to finish.") and "Replace the faceless figure on the right with the person in Picture 2" in fp
    assert "Exactly 2 people" in fp and "The background stays exactly the place of Picture 1" in fp
    # 세 사람: 왼쪽·가운데·오른쪽, 나오는 차례
    st3 = _copy.deepcopy(st)
    st3["bible"]["characters"].append({"id": "C", "look": "a calm neat woman around forty with long straight black hair"})
    st3["scenes"][5]["characters"] = ["A", "B", "C"]
    s3 = video.layout_slots(st3, 5, ["A", "B", "C"])
    assert [s["id"] for s in s3] == ["A", "B", "C"] and [s["pos"] for s in s3] == ["on the left", "in the middle", "on the right"]
    lp3 = video.layout_prompt(st3, 5, s3)
    assert "3 faceless mannequin figures, standing: on the left a child-sized figure turned toward the right" in lp3 and "Exactly 3 figures" in lp3
    fp3 = video.finish_prompt(st3, 5, s3, "B", ["A"], final=False)
    assert "Keep the faceless figure on the right exactly as it is." in fp3 and "Exactly 3 figures in total" in fp3
    # 11-21 검토: 키 판정은 머리 구절만, 자세는 가장 먼저 나오는 동사·같은 사람이 나온 앞 장면의 멈춘 자세, 뒤에서 찍은 샷 말 빼기
    for look, size in (("a tall man in his forties with short dark hair", "adult-sized"), ("an old man with tiny round spectacles", "adult-sized"),
                       ("a young woman in her twenties wearing a baby blue cardigan", "adult-sized"),
                       ("a young mother holding her baby, in a blue dress", "adult-sized"), ("Mina, ten years old, in a raincoat", "child-sized"),
                       ("a man of short stature", "short"), ("a 40-year-old man in a grey suit", "adult-sized")):
        assert video.figure_size(look) == size, (look, video.figure_size(look))
    sp = _copy.deepcopy(st)
    sp["scenes"][1]["action"] = "The old dwarf stands at the kitchen counter while the girl sits nearby."
    assert video.scene_pose(sp, 1) == "standing"                                   # 가장 먼저 나오는 동사
    sp["scenes"][1]["action"] = "The girl walks across the room and sits down at the table."
    assert video.scene_pose(sp, 1) == "walking"                                    # 첫 화면은 동작의 시작
    sp["scenes"][1]["action"] = "The girl and the old dwarf sit at the table."
    sp["scenes"][3]["action"] = "He smiles."                                       # B 의 대사 컷: 같은 장소의 앞 장면에서 B 가 앉아 있었다
    assert video.scene_pose(sp, 3) == "sitting"
    sp["scenes"][5]["characters"] = ["A"]
    sp["scenes"][5]["action"] = "Bright light."                                    # 장소가 바뀌면 물려받지 않는다
    assert video.scene_pose(sp, 5) == "standing"
    assert video._layout_shot("wide shot from behind, slow follow") == "wide shot, slow follow"
    desc = video.layout_description(st, 1, sl)
    assert "Picture" not in desc and "faceless" not in desc and desc.startswith("wide shot, slow follow. on the left: a short old dwarf herbalist")
    assert "Every face is visible, front or three-quarter." in ip and "Every face is visible" in fp
    assert "Keep the head where it is in Picture 1, with clear empty space above the hair" in ip      # 대사 클로즈업
    assert "Keep the head where it is" not in video.identity_prompt(st, 1, sl, ["B", "A"], final=True)   # 와이드는 아님
    dup = _copy.deepcopy(st)
    dup["scenes"][1]["characters"] = ["A", "A", "B"]
    assert video.scene_people(dup, 1) == ["A", "B"]                               # 두 번 적힌 인물은 한 번
    # 예전 길(기준 그림으로 바로)도 대사 컷은 장면 글 대신 감정만
    cp = video.compose_prompt(st, 2, ["A"])
    assert "She looks up" not in cp and "with a hesitant and a little sad expression" in cp


def test_chain_style_keep_follows_the_film_style():
    import copy as _copy
    st = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    assert video.style_key(st) == "semi" and "not cel-shaded" in video.style_keep(st)
    # 11-24: 정지 그림 지시문의 화풍 문장 = 영화 화풍 원문 + 열쇠 문장 (열쇠 문장만이면 수채·점토 같은 화풍도 반실사 문장으로 모였다)
    assert video.style_keep(st) == ("The art style of the whole film: semi-realistic painterly 2.5D fantasy film look. "
                                    + video.STYLE_KEEP["semi"])
    st["bible"]["style"] = "2D anime film, clean cel-shaded line art"
    assert video.style_key(st) == "anime" and "2D anime-style" in video.style_keep(st)
    assert video.style_keep(st) == "The art style of the whole film: 2D anime film, clean cel-shaded line art. " + video.STYLE_KEEP["anime"]
    st["bible"]["style"] = "photorealistic cinematic live action"
    assert video.style_key(st) == "photo" and "live-action" in video.style_keep(st)
    assert video.style_keep(st).startswith("The art style of the whole film: photorealistic cinematic live action. Keep the exact look")
    st["bible"]["style"] = "soft watercolour storybook illustration with visible paper texture."
    keep = video.style_keep(st)
    # 2차: 수채는 custom 열쇠라 '아닌 것' 없는 custom 문장 (예전에는 반실사 문장의 'not a cartoon, not cel-shaded')
    assert keep == ("The art style of the whole film: soft watercolour storybook illustration with visible paper texture. "   # 마침표는 한 번
                    + video.STYLE_KEEP["custom"])
    st["bible"]["style"] = ""
    assert video.style_keep(st) == video.STYLE_KEEP["semi"]                                  # 원문이 없으면 열쇠 문장만
    # 원문은 200자까지: 뒤쪽 절반 안의 쉼표 경계에서 자른다
    st["bible"]["style"] = "2D anime feature film with clean cel-shaded line art, " + "soft painted watercolour backgrounds, " * 8
    raw = video.style_raw(st)
    assert len(raw) <= video.STYLE_RAW_MAX and raw.startswith("2D anime feature film") and raw.endswith("watercolour backgrounds")
    assert video.style_raw(st, 80) == "2D anime feature film with clean cel-shaded line art"
    assert "There is no person" in video.people_sentence(0) and video.people_sentence(1) == video.ONE_PERSON


def test_still_prompts_say_art_style_and_keep_the_people_sentence_with_a_long_film_style():
    """11-24: 고정 문구 'the painting style' 은 실사·애니 영화와 부딪혀 'the art style' 로. 화풍 원문을 실어 지시문이 길어져도
    끝의 정확한 사람 수 문장은 잘리지 않는다 (화풍 원문을 먼저 줄이고, 그래도 넘치면 앞 글 끝을 문장 경계에서 자른다)"""
    import copy as _copy
    st = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    sl = video.layout_slots(st, 1, ["A", "B"])
    one = video.layout_slots(st, 2, ["A"])
    ip = video.identity_prompt(st, 2, one, ["A"], final=True)
    fp = video.finish_prompt(st, 1, sl, "A", ["B"], final=True)
    pj = video.cut_edit_prompt(st, 5, ["A"], "jump")
    assert "the light, the art style, and every figure's" in ip and "the light, the art style and the people already in it" in fp
    assert "built from image 1 only for the art style and the people" in pj
    assert not any("painting style" in p for p in (ip, fp, pj))
    # 긴 외형·알아볼 소품·장소와 긴 화풍 글 (외형은 구성안 상한 45낱말 가까이)
    big = _copy.deepcopy(st)
    a, b = big["bible"]["characters"]
    a["look"] = ("a twelve-year-old girl with two long black braids and thin straight bangs, pale face, dark blue eyes, a wine-red round-brimmed "
                 "hat, a wine-red cape with small embroidered flowers over a wine-red blouse with a big bow at the neck, dark brown boots")
    b["look"] = ("a short stocky old dwarf herbalist with wild white hair, a long white beard, small round glasses, a golden-ochre coat with a "
                 "green herb badge, brown trousers, sturdy boots, a leather belt with many small pouches and a carved walking stick")
    a["signature"] = ["wine-red round-brimmed hat", "two long black braids", "wine-red cape with embroidered flowers"]
    b["signature"] = ["wild white hair", "long white beard", "small round glasses", "golden-ochre coat with a green herb badge"]
    big["bible"]["characters"].append({
        "id": "C", "look": ("a calm neat woman around forty with long straight black hair parted in the middle, a cream blouse with a "
                            "stand-up collar, a beige linen apron with an amber brooch, a long brown skirt and soft leather shoes"),
        "signature": ["long straight black hair parted in the middle", "cream blouse with a stand-up collar",
                      "beige linen apron with an amber brooch"]})
    big["scenes"][1]["characters"] = ["A", "B", "C"]
    big["scenes"][1]["action"] = ("The girl, the old dwarf and the woman walk side by side along the path in the early light, talking quietly, "
                                  "the girl skipping a little ahead while the dwarf leans on his stick and the woman smiles at them both, "
                                  "and birds lift from the ferns as they pass the stream")
    big["bible"]["places"][0]["look"] = ("a winding forest path with mossy trees, ferns and old roots, a wooden fence along one side, wild "
                                         "flowers, a small stream with flat stones, soft moss on the rocks and birch trunks far behind")
    big["bible"]["style"] = ("2D hand-drawn anime feature film with clean cel-shaded line art, flat bright colours, soft painted watercolour "
                             "backgrounds, gentle film grain and warm golden light on every face and surface")
    full = video.style_raw(big)
    s3 = video.layout_slots(big, 1, ["A", "B", "C"])
    for p, tail in ((video.compose_prompt(big, 1, ["A", "B", "C"]), video.people_sentence(3)),
                    (video.cut_edit_prompt(big, 1, ["B", "A"], "cut"), video.people_sentence(3)),
                    (video.identity_prompt(big, 1, s3, ["A", "B"], final=False), "Exactly 3 figures in total, no other person."),
                    (video.finish_prompt(big, 1, s3, "C", ["A", "B"], final=True), video.people_sentence(3)),
                    (video.layout_prompt(big, 1, s3), "Exactly 3 figures, no other figure or person."),
                    (video.end_keyframe_prompt(big, 1, "they stop and smile"), video.people_sentence(3))):
        assert len(p) <= video.STILL_PROMPT_MAX and p.endswith(tail), (len(p), p[-160:])
        assert "The art style of the whole film: 2D hand-drawn anime feature film with clean cel-shaded line art" in p
        assert video.STYLE_KEEP["anime"] in p
    cp = video.compose_prompt(big, 1, ["A", "B", "C"])
    assert full not in cp and "What happens: The girl, the old dwarf and the woman" in cp     # 넘쳐서 화풍 원문을 줄였다 (앞 글은 남는다)
    assert full in video.layout_prompt(big, 1, s3)                                           # 넘치지 않으면 원문 그대로
    # 앞 글이 아주 길면 앞 글 끝을 자르고 화풍 문장·끝 문장은 남긴다
    out = video._with_style(["One sentence here. " * 120], big, "Exactly 2 people.")
    assert len(out) <= video.STILL_PROMPT_MAX and out.endswith(video.STYLE_KEEP["anime"] + " Exactly 2 people.")
    assert "The art style of the whole film: 2D hand-drawn anime" in out
    # 짧으면 그대로 이어 붙인다 (예전과 같은 글)
    assert video._with_style(["A.", "B."], st, "C.") == "A. B. " + video.style_keep(st) + " C."


# ── 어떤 화풍 요청에도 (11-24 2차 Part C): 3D 애니메이션(toon3d)·그 밖의 화풍(custom) 열쇠 ──
# 열쇠마다 한국어·영어·애매한 글 (애매한 글은 왜 그 열쇠인지 test_style_key_table_* 에 적는다)
STYLE_TABLE = {
    "semi": ("semi-realistic digital painting, 2.5D concept art for a fantasy feature film", "반실사 판타지 컨셉아트", "2.5D 느낌의 동화",
             "softly painted digital painting with warm light", "a painterly look", "", None),
    "anime": ("2D anime feature film with clean cel-shaded line art", "cel-shaded cartoon", "애니메이션", "일본 애니 느낌", "웹툰 그림체",
              "hand-drawn animation", "watercolor anime", "2D anime with bright pastel colours"),
    "toon3d": ("Pixar-like 3D animated feature", "Disney 3D", "CG animation with soft lighting", "3D cartoon", "stylized 3D characters",
               "3D 애니메이션", "픽사 느낌", "photorealistic 3D animated feature"),
    "photo": ("photorealistic cinematic live action", "실사 영화", "사진처럼 생생하게", "a cinematic photograph",
              "live-action film still, not a cartoon", "photorealistic film noir"),
    "custom": ("soft watercolour storybook illustration", "수채화 동화책", "gouache painting", "유화 느낌", "claymation",
               "stop-motion animation with felt puppets", "클레이 애니", "paper cut-out animation", "pixel art", "픽셀 아트",
               "comic book style", "그래픽 노블", "ink wash sumi-e", "수묵화", "ukiyo-e woodblock print", "판화", "stained glass art style",
               "low poly", "film noir", "soft pastel drawing", "crayon drawing", "coloured pencil drawing", "pencil sketch",
               "something unknown", "3D render, octane", "2D hand-drawn lines on cel-shaded 3D characters"),
}


def test_style_key_table_for_every_key_korean_english_and_ambiguous():
    """C1·C8: 화풍 글 → 열쇠 표 (열쇠마다 5개 이상, 한국어·영어, 애매한 글)"""
    from app import cast_lib
    for want, texts in STYLE_TABLE.items():
        assert len([t for t in texts if t]) >= 5, want
        for s in texts:
            assert cast_lib.style_key(s) == want, (s, cast_lib.style_key(s), want)
    assert cast_lib.STYLE_KEYS == ("photo", "semi", "anime", "toon3d") and cast_lib.CUSTOM_KEY == "custom"
    key = cast_lib.style_key
    # 애매한 글: 애니 말은 custom 말보다 먼저('watercolor anime'), '움직이는 그림' 말에 custom 말이 붙으면 그 화풍('stop-motion animation'),
    # 3D 와 애니 말이 함께면 3D 애니(실사 말보다 먼저), 2D 와 3D 를 함께 말한 섞인 화풍은 custom, 실사 말은 custom 말보다 먼저('film noir' 실사)
    assert key("watercolor anime") == "anime" and key("stop-motion animation") == "custom" and key("clay animation") == "custom"
    assert key("3D 애니") == "toon3d" and key("클레이 애니메이션") == "custom" and key("2D anime with 3D backgrounds") == "custom"
    assert key("photorealistic film noir") == "photo" and key("black and white film noir") == "custom"
    # custom 말(⑤)은 그림 말(⑥ painted → 반실사)보다 먼저: 손으로 칠한 수채·유화는 반실사 문장이 아니라 원문으로
    assert key("hand-painted watercolour storybook") == "custom" and key("oil painted portrait look") == "custom"
    assert key("hand-painted fantasy look") == "semi"
    # 색 이름으로 쓴 파스텔은 화풍(파스텔화)이 아니다
    assert key("anime, bright pastel colours") == "anime" and key("pastel colours") == "custom"     # 둘째는 열쇠 말이 없어 custom(원문)
    # '아닌 것' 만 적은 글은 비어 있지 않으므로 custom (원문을 그대로 쓴다)
    assert key("non-photorealistic") == "custom" and not cast_lib.is_photo_style("non-photorealistic")
    # 2차 검토: 'cel-shaded comic' 은 코믹(custom) — 'cel' 애니 말에 먼저 걸리지 않게. 한국어 '디즈니 3D'·'픽사 스타일' 은 3D 애니
    assert key("cel-shaded comic") == "custom" and key("bold cel-shaded comics") == "custom" and key("cel-shaded cartoon") == "anime"
    assert key("디즈니 3D") == key("디즈니 3D 애니메이션") == key("픽사 스타일") == "toon3d"


def test_said_style_knows_every_style_word_and_skips_objects():
    """C1: said_style 은 3D 애니·custom 말을 모두 안다. 물건으로 쓴 말('연필로 편지를 쓰는')은 화풍이 아니다 (화풍을 말하지 않은 요청이
    라이브러리 화풍을 잃지 않게). 요청이 말한 화풍을 구성안이 따르지 않으면 그 화풍의 글로 채운다"""
    from app import cast_lib
    said = cast_lib.said_style
    # 화풍 표지(풍·화풍·느낌·스타일·그림체·로 그린·처럼·애니메이션·인형극 …, style·look·art·illustration·animation …)가 붙은 말과
    # 그 자체로 화풍인 말(claymation·stop-motion·pixel art·ukiyo-e·sumi-e·low poly·cel-shaded comic)
    for t in ("수채화로", "수채화풍으로", "클레이 애니메이션으로", "in watercolor style", "pixel art style", "과슈로 그린", "유화 느낌", "파스텔화 느낌",
              "크레파스로 그린 듯", "색연필 그림체로", "연필 스케치 느낌으로", "수묵화풍", "판화 느낌", "그림책 같은 그림체", "동화 같은 그림체",
              "클레이 애니", "점토 인형극", "스톱모션으로", "종이 오리기 느낌으로", "펠트 인형극", "픽셀 아트", "코믹북 스타일", "그래픽 노블처럼",
              "누아르 느낌", "누아르 영화처럼 찍어 줘", "스테인드글라스 화풍", "로우 폴리", "3D 애니로", "픽사처럼", "디즈니 3D", "watercolor style",
              "gouache painting look", "pastel drawing style", "colored pencil look", "pencil sketch style", "ink wash style", "sumi-e",
              "woodblock print style", "ukiyo-e", "picture book illustration", "claymation", "stop-motion", "paper cut-out animation",
              "felt puppet animation", "pixel art", "graphic novel art", "noir style", "stained glass art", "low poly", "cel-shaded comic",
              "watercolor and storybook style", "Pixar-like", "Disney 3D", "CG animation", "3D cartoon", "stylized 3D"):
        assert said(t), t
    lib = cast_lib.library()
    lib_style = lib[0]["style"]
    # 물건·활동·분위기로 쓴 말은 화풍 요청이 아니다: 라이브러리 화풍 그대로 (11-24 2차 검토 — 1차와 같다)
    objects = ("니키가 펠트 인형을 만드는 영상", "니키가 연필 스케치를 하는 영상", "니키가 판화 수업을 듣는 영상", "니키가 픽셀이 깨진 게임을 한다",
               "니키가 수묵화를 본다", "니키가 클레이로 인형을 만드는 영상", "니키가 수채화 물감으로 그림을 그린다", "니키가 유화를 감상한다",
               "니키가 종이 인형을 만드는 영상", "니키가 코믹북을 읽는다", "Niki plays with clay", "니키와 푸토르의 동화 같은 하루",
               "니키와 푸토르의 코믹 스토리", "니키가 누아르 영화를 본다", "점토 인형", "펠트 모자를 쓴 푸토르",
               "니키가 연필로 편지를 쓰는 영상", "종이 비행기를 날리는 니키", "파스텔톤 옷을 입은 베스", "색연필을 든 니키", "크레파스를 쥔 아이",
               "니키가 동화책을 읽는다", "니키가 그림책을 펼친다", "she felt sad in the rain", "코믹한 대화", "니키와 푸토르가 숲길을 걷는 영상",
               "니키가 스테인드글라스 창문 앞에 서 있다", "light through a stained glass window", "니키가 수채화 풍경을 그린다")
    for t in objects:
        assert not said(t) and cast_lib.said_key(t) is None and cast_lib.custom_style_text(t) == "", t
        assert cast_lib.said_style_fill(t, lib_style) == lib_style and lib_style in cast_lib.prompt_block(lib, text=t), t
        st = {"bible": {"characters": [{"id": "A", "name_ko": "니키", "look": "x"}], "style": "photorealistic cinematic live action"}}
        cast_lib.apply_to_story(st, lib, t)
        assert st["bible"]["style"] == lib_style, t
    # 표지 없는 맨 낱말은 일부러 화풍으로 세지 않는다 (요청 글에서 물건으로도 쓰인다). 구성안의 화풍 칸(style_key)은 넓게 읽는다
    for t in ("watercolor", "gouache", "felt puppets", "comic book", "graphic novel", "noir", "stained glass", "pencil sketch", "pastel drawing",
              "picture book", "paper cut-out", "woodblock", "ink wash", "colored pencil", "펠트", "파스텔", "색연필", "연필", "종이", "코믹",
              "스테인드글라스"):
        assert not said(t), t
    assert cast_lib.style_key("pastel drawing") == "custom" and cast_lib.style_key("film noir") == "custom"
    # 물건 말과 화풍 말이 함께면 화풍 말만 옮긴다. 화풍으로 쓰지 않은 custom 말은 열쇠도 정하지 않는다 ('점토 인형을 만드는 애니메이션' 은 애니)
    assert cast_lib.custom_style_text("수채화풍으로, 니키가 점토 인형을 만드는 영상") == "watercolour painting style"
    assert cast_lib.said_key("니키가 점토 인형을 만드는 애니메이션") == "anime"
    assert cast_lib.said_style_fill("니키가 점토 인형을 만드는 애니메이션", lib_style) == cast_lib.SAID_STYLE_TEXT["anime"]
    for t, fill in (("수채화풍으로", "watercolour painting style"), ("클레이 애니메이션으로", "claymation style"),
                    ("in watercolor style", "watercolour painting style"), ("pixel art style", "pixel art style")):
        assert cast_lib.said_key(t) == "custom" and cast_lib.said_style_fill(t, lib_style) == fill, t
    assert cast_lib.said_key("수채화 동화풍으로") == "custom" and cast_lib.said_key("3D 픽사 느낌으로") == "toon3d"
    assert cast_lib.said_key("클레이 애니메이션으로") == "custom" and cast_lib.said_key("애니메이션으로") == "anime"
    assert cast_lib.said_key("니키가 연필로 편지를 쓰는 영상") is None
    assert cast_lib.custom_style_text("수채화 동화풍으로") == "watercolour painting and storybook illustration style"
    assert cast_lib.custom_style_text("클레이 애니") == "claymation style" and cast_lib.custom_style_text("애니메이션") == ""
    assert cast_lib.said_style_fill("수채화 동화풍으로", "LIB") == "watercolour painting and storybook illustration style"
    assert cast_lib.said_style_fill("3D 픽사 느낌으로", "LIB") == cast_lib.SAID_STYLE_TEXT["toon3d"]
    assert cast_lib.said_style_fill("반실사로", "LIB") == "LIB" and cast_lib.said_style_fill("몽환적인 화풍", "LIB") == "LIB"
    for k in ("anime", "photo", "toon3d"):                  # 채우는 글은 그 열쇠로 읽힌다
        assert cast_lib.style_key(cast_lib.SAID_STYLE_TEXT[k]) == k, k
    # 라이브러리 refs 칸: toon3d 는 받고 custom 은 받지 않는다 (글마다 달라 저장하지 않는다)
    e = cast_lib._clean_refs({"id": "x", "refs": {"semi": "a.png", "toon3d": "b.png", "custom": "c.png"}})
    assert e["refs"] == {"semi": "a.png", "toon3d": "b.png"}
    assert cast_lib.ref_for(e, "toon3d") == "b.png" and cast_lib.ref_for(e, "custom") is None


# custom 영화에 들어가면 원문 화풍과 부딪히는 '아닌 것' 문장 (예전에는 반실사·애니 문장이 수채·점토·3D 요청에 붙었다)
STYLE_CONFLICTS = ("not a cartoon", "not cel-shaded", "not a 3d render", "not a glossy 3d render", "not a painting", "not a flat 2d drawing",
                   "not an illustration", "not a flat cartoon", "not a photograph")


def _style_prompts(st: dict) -> dict:
    """한 대사 영상(DLG_OK 꼴)으로 만드는 화풍이 실리는 지시문 모두: 정지 그림·기준 그림·변환·영상 글·장면 글"""
    sl = video.layout_slots(st, 1, ["A", "B"])
    one = video.layout_slots(st, 2, ["A"])
    return {"keep": video.style_keep(st), "layout": video.layout_prompt(st, 1, sl),
            "identity": video.identity_prompt(st, 2, one, ["A"], final=True), "finish": video.finish_prompt(st, 1, sl, "A", ["B"], final=True),
            "cut": video.cut_edit_prompt(st, 5, ["A"], "jump"), "compose": video.compose_prompt(st, 1, ["A", "B"]),
            "end": video.end_keyframe_prompt(st, 1, "they smile"), "cast_edit": video.cast_edit_prompt(st, 2), "cast": video.cast_prompt(st, "A"),
            "convert": video.style_convert_prompt(video.style_key(st), "girl", video.style_raw(st)), "talk": video.talk_prompt(st, 2),
            "notes": video.scene_notes(st, 1), "image": video.scene_image_text(st, 1)}


def test_custom_and_toon3d_prompts_carry_the_style_without_conflicting_negations():
    """C2·C6·C8: custom 영화의 어떤 지시문에도 'not a cartoon' 같은 부딪히는 문장이 없고 원문 화풍이 실린다. 3D 애니 영화에는
    'not a 3D render' 가 없다. 영상 글은 실사만 따로, 3D 애니·custom 은 원문 + 첫 화면 화풍 고정"""
    import copy as _copy
    base = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    for style in ("soft watercolour storybook illustration with visible paper grain", "claymation stop-motion with felt textures",
                  "pixel art", "bold comic book art with halftone dots", "2D hand-drawn lines on cel-shaded 3D characters"):
        st = _copy.deepcopy(base)
        st["bible"]["style"] = style
        assert video.style_key(st) == "custom", style
        assert video.style_keep(st) == f"The art style of the whole film: {style}. " + video.STYLE_KEEP["custom"]
        for name, p in _style_prompts(st).items():
            low = p.lower()
            assert not [w for w in STYLE_CONFLICTS if w in low], (style, name, [w for w in STYLE_CONFLICTS if w in low])
            assert style.lower()[:20] in low, (style, name)                   # 원문 화풍이 실린다
        assert video._style_line(st) == f" Style: {style}" and video.style_lock(st)
        assert "never photographic realism" in video.scene_notes(st, 1)           # 사실적이지 않은 화풍의 장면 글 (custom 은 실사가 아니다)
    for style in ("Pixar-like 3D animated feature film with soft lighting", "stylized 3D CG animation", "3D 애니메이션 느낌"):
        st = _copy.deepcopy(base)
        st["bible"]["style"] = style
        assert video.style_key(st) == "toon3d", style
        assert video.style_keep(st).endswith(video.STYLE_KEEP["toon3d"]) and "stylized 3D animated feature film" in video.STYLE_KEEP["toon3d"]
        for name, p in _style_prompts(st).items():
            assert "3d render" not in p.lower(), (style, name)
        assert video._style_line(st) == f" Style: {style}" and video.style_lock(st)
    # 실사만 따로: 영상 글은 실사 문장, 화풍 고정 문장 없음
    st = _copy.deepcopy(base)
    st["bible"]["style"] = "photorealistic cinematic live action"
    assert video._style_line(st) == " Photorealistic cinematic look." and video.style_lock(st) == ""
    # 모르는 열쇠는 반실사가 아니라 custom 문장으로 (style_keep)
    old = video.cast_lib.style_key
    try:
        video.cast_lib.style_key = lambda s: "mystery"
        assert video.style_keep(st).endswith(video.STYLE_KEEP["custom"])
    finally:
        video.cast_lib.style_key = old


def test_style_convert_prompt_and_quality_negative_for_every_key():
    """C4·C7: 기준 그림 변환 지시문. 실사는 메인 P1 에서 통한 문장 모양, 3D 애니는 3D 문장('not a 3D render' 없음), custom 은 원문(200자 이내)으로
    그리고 '아닌 것'을 말하지 않는다. C5b: 정밀 판의 화풍별 부정 글 (반실사·custom 은 없음 = 레시피 기본 부정 글)"""
    pc = video.style_convert_prompt("photo", "a calm woman", "photorealistic film")
    assert pc.startswith("Turn this illustrated portrait into a real photograph of the same person, a calm woman:"), pc
    assert "a live-action film still with real human skin and pores, real hair strands, real fabric and natural soft light" in pc
    assert "Keep exactly the same face shape, eyes, hairstyle, hair colour, clothes and accessories" in pc and "plain background" in pc
    assert "It is not a painting, not an illustration, not a 3D render." in pc and pc.endswith(video.ONE_PERSON)
    t3 = video.style_convert_prompt("toon3d", "girl")
    assert "a character in a stylized 3D animated feature film" in t3 and "Not a photograph, not a flat 2D drawing" in t3
    assert "3D render" not in t3 and "the same clothes and accessories" in t3
    raw = "soft watercolour storybook illustration with visible paper grain, " + "gentle washes of colour, " * 12
    cu = video.style_convert_prompt("custom", "girl", raw)
    trimmed = video.trim_style(raw)
    assert len(trimmed) <= video.STYLE_RAW_MAX and f"a character drawn in this art style: {trimmed}:" in cu and raw.strip(" ,") not in cu
    assert "the same face shape, hairstyle, hair colour, eye colour and the same clothes and accessories" in cu
    assert not any(w in cu.lower() for w in ("not a ", "semi-realistic", "anime", "photograph", "3d"))
    assert video.style_convert_prompt("nope", "girl", "pixel art") == video.style_convert_prompt("custom", "girl", "pixel art")
    assert "drawn in the art style of the film" in video.style_convert_prompt("custom", "girl", "")
    assert "semi-realistic" in video.style_convert_prompt("semi", "girl") and "2D anime feature film" in video.style_convert_prompt("anime", "girl")
    assert video.trim_style("a, b.  ") == "a, b" and video.trim_style(None) == ""
    # 정밀 판의 부정 글
    st = lambda s: {"bible": {"style": s}}
    anime, photo, toon = (video.quality_negative(st(s)) for s in ("2D anime film", "photorealistic live action", "Pixar-like 3D animated"))
    assert anime == video.QUALITY_NEGATIVE["anime"] and anime.startswith("semi-realistic, photorealistic, photograph, 3D render")
    assert photo.startswith("painting, illustration, drawing, cartoon, anime, 3D render") and "watermark" in photo
    assert toon == "photograph, flat 2D drawing, low quality, blurry, text, watermark" and "3D render" not in toon
    assert video.quality_negative(st("soft watercolour storybook")) == "" and video.quality_negative(st("semi-realistic painting")) == ""


# ── 두 사람 대화 장면 (11-24 메인 X1·X6, STUDIO_DUO_TALK): 듣는 상대가 같은 장소면 두 사람을 미디엄 투샷에 둔다 ──
DUO_OK = {
    "title_ko": "니키의 생일 아침", "logline_ko": "베스와 제임스가 아침 식탁에서 니키의 생일을 이야기한다.",
    "bible": {"characters": [
        {"id": "A", "look": "a calm neat woman around forty with long straight black hair, a cream blouse and an apron with an amber brooch",
         "name_ko": "베스", "voice": "A warm Korean woman around forty, gentle and clear.", "voice_ko": "따뜻한 엄마"},
        {"id": "B", "look": "a tall lean man in his early forties with short brown hair and a blue cardigan", "name_ko": "제임스",
         "voice": "A calm Korean man in his forties, low and friendly.", "voice_ko": "차분한 아빠"},
        {"id": "C", "look": "a twelve-year-old girl with two long black braids and a wine-red hat", "name_ko": "니키"}],
        "places": [{"id": "P1", "look": "a sunny cottage kitchen with a wooden breakfast table"},
                   {"id": "P2", "look": "a small garden with an apple tree"}],
        "time": "morning, clear", "light": "soft warm window light", "style": "semi-realistic painterly 2.5D film look"},
    "music": {"tags": "gentle cinematic instrumental, piano", "bpm": 72, "key": "D major", "ko": "잔잔한 피아노"},
    "scenes": [
        {"seconds": 10, "place": "P1", "characters": ["A", "B"], "continues": False, "shot": "wide shot, slow dolly forward",
         "action": "Beth and James sit at the breakfast table in the morning light.", "sound": "birds outside, a kettle",
         "ko": "부엌의 아침", "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
        {"seconds": 5, "place": "P1", "characters": ["A", "B"], "continues": False, "shot": "medium two-shot",
         "action": "Beth smiles and speaks softly to James about Niki.", "sound": "soft room tone", "ko": "베스가 말한다",
         "kind": "talk", "speaker": "A", "listener": "B", "line_ko": "여보, 오늘 니키 생일이에요.", "emotion": "warm and excited"},
        {"seconds": 5, "place": "P1", "characters": ["B", "A"], "continues": True, "shot": "medium two-shot",
         "action": "James raises his eyebrows and answers.", "sound": "soft room tone", "ko": "제임스가 답한다",
         "kind": "talk", "speaker": "B", "listener": "A", "line_ko": "그래요? 케이크를 사 올까요?", "emotion": "surprised"},
        {"seconds": 5, "place": "P1", "characters": ["A", "C"], "continues": False, "shot": "medium two-shot",
         "action": "Beth turns to Niki with a gentle smile.", "sound": "soft room tone", "ko": "베스가 니키에게 말한다",
         "kind": "talk", "speaker": "A", "listener": "C", "line_ko": "니키야, 생일 축하해.", "emotion": "gentle"},
        {"seconds": 8, "place": "P1", "characters": ["C"], "continues": False, "shot": "close-up",
         "action": "Niki beams with joy.", "sound": "soft room tone", "ko": "니키가 웃는다",
         "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
        {"seconds": 15, "place": "P2", "characters": ["A", "B", "C"], "continues": False, "shot": "wide shot",
         "action": "The family stands together under the apple tree.", "sound": "wind in the leaves", "ko": "정원의 가족",
         "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""}]}


@contextlib.contextmanager
def duo_setting(on: bool):
    old = settings.studio_duo_talk
    settings.studio_duo_talk = on
    try:
        yield
    finally:
        settings.studio_duo_talk = old


def _duo_pairs():
    """DUO_OK 에서 베스와 니키의 장면(3·4)을 정원(P2)으로 옮긴 판: 장소가 바뀌어 자리 이어 받기가 없으므로 장면 3 이 두 사람 대사 컷이다
    (DUO_OK 그대로면 같은 식탁의 제임스가 이어 들어와 세 사람 컷이 된다, 2차 G1)"""
    import copy as _copy
    d = _copy.deepcopy(DUO_OK)
    for k in (3, 4):
        d["scenes"][k]["place"] = "P2"
    return d


def test_duo_talk_story_rules_and_cleaning():
    import copy as _copy
    with duo_setting(True):
        assert video.shot_rank("medium two-shot") == 2 and video.shot_rank("two-shot") == 2 and video.shot_rank("a two shot") == 2
        assert video.shot_rank("wide two-shot") == 4 and video.shot_rank("medium close-up") == 1         # 넓은 투샷은 넓은 샷
        assert video.shot_rank("medium group shot") == 2 and video.shot_rank("group shot") == 2
        assert video.dialogue_problems(DUO_OK, 48, True, exchange=True) == []
        s = video.clean_dialogue_story(DUO_OK, 48)
        assert [x["kind"] for x in s["scenes"]] == ["shot", "talk", "talk", "talk", "shot", "shot"]
        assert [x["listener"] for x in s["scenes"]] == ["", "B", "A", "C", "", ""]                     # 모든 장면에 listener
        assert s["scenes"][1]["characters"] == ["A", "B"] and s["scenes"][2]["characters"] == ["B", "A"]
        # 베스가 니키에게 말하는 장면 3: 같은 식탁의 제임스(앞 장면)가 사라지지 않는다 (자리 이어 받기, 사용자 결정 "셋 모두 나오게")
        assert s["scenes"][3]["characters"] == ["A", "C", "B"] and s["scenes"][3]["shot"] == "medium group shot"
        # 같은 사람들·같은 장소의 대사 컷은 앞 대사 컷을 같은 화면으로 잇는다. 사람이 달라진 대사 컷, 대사 컷 다음 장면은 잇지 않는다
        assert [x["continues"] for x in s["scenes"]] == [False, False, True, False, False, False]
        assert [video.talk_pair(s, k) for k in range(6)] == [None, ["A", "B"], ["B", "A"], None, None, None]
        assert video.talk_group(s, 3) == ["A", "C", "B"]
        assert [video.duo_talk(s, k) for k in range(6)] == [False, True, True, True, False, False]
        # 1차 D2(11-24 검토)가 그대로: 한 사람 컷도 그 사람이 나온 대화 컷과 같은 쪽을 본다. 장면 3 은 이어 받기로 세 사람이 되었고(가운데
        # 니키가 왼쪽의 베스를 보며 듣는다) 바로 다음 장면 4 의 니키 클로즈업도 왼쪽 (2차 검토: 세 사람 자리를 보지 않아 오른쪽으로 튀었다)
        assert [x["id"] for x in video.layout_slots(s, 3, video.scene_people(s, 3))] == ["A", "C", "B"]
        assert video.facing_of(s, 4, "C") == "left" and video.layout_slots(s, 4, ["C"])[0]["facing"] == "turned slightly toward the left"
        assert video.scene_people(s, 2) == ["B", "A"] and video.pick_refs(s, 2, ["A", "B", "C"]) == ["B", "A"]     # 말하는 사람 먼저
        assert "C" in video.anchor_ids(s) and video.cast_scene(s, 3) == "A"
        assert video.scene_seam(s, 2) == video.SEAM_CONTINUE and video.scene_seam(s, 3) == video.SEAM_CUT
        # 위반: 혼잣말(듣는 사람 칸이 빈 혼자 컷 = 독백)을 잇는 대사 컷, 대사 컷을 잇는 말 없는 장면, 모르는 듣는 사람
        bad = _copy.deepcopy(DUO_OK)
        bad["scenes"][1].update(characters=["A"], listener="")
        bad["scenes"][4]["continues"] = True
        bad["scenes"][3].update(listener="Z")
        left = video.dialogue_problems(bad, 48, True)
        assert any("Scene 3 is a talk scene, so it cannot continue" in x and "same people in the same place" in x for x in left), left
        assert any("Scene 5 comes right after a talk scene, so it cannot continue it." in x for x in left), left
        assert any('"listener" "Z" is not in bible.characters' in x for x in left), left
        assert any("Scene 2 is a monologue: the shot must be a close-up" in x for x in left), left          # 독백인데 투샷 크기
        cb = video.clean_dialogue_story(bad, 48)
        assert cb["scenes"][1]["characters"] == ["A"] and cb["scenes"][1]["monologue"] is True and cb["scenes"][1]["shot"] == "medium close-up"
        assert cb["scenes"][2]["continues"] is False and cb["scenes"][4]["continues"] is False
        assert cb["scenes"][3]["listener"] == "C" and cb["scenes"][3]["characters"] == ["A", "C", "B"]  # 틀린 듣는 사람 → characters 의 상대
        # 샷 크기의 경계: 두세 사람 대화 컷은 크기 3(풀샷·미디엄 와이드)까지, 독백 컷은 1(미디엄 클로즈업)까지 (미디엄 샷은 위반)
        for shot, ok in (("full shot", True), ("medium wide shot", True), ("long shot", False), ("wide two-shot", False)):
            pb = _copy.deepcopy(DUO_OK)
            pb["scenes"][1]["shot"] = shot
            msg = any("Scene 2 is a conversation with 2 people: the shot must be" in x for x in video.dialogue_problems(pb, 48, True))
            assert msg is not ok, (shot, msg)
        for shot, ok in (("close-up", True), ("medium close-up", True), ("medium shot", False), ("full shot", False),
                         ("medium wide shot", False), ("medium two-shot", False)):
            sb = _copy.deepcopy(DUO_OK)
            sb["scenes"][3].update(characters=["A"], listener="nobody", shot=shot)
            msg = any("Scene 4 is a monologue: the shot must be a close-up" in x for x in video.dialogue_problems(sb, 48, True))
            assert msg is not ok, (shot, msg)
        wide = _copy.deepcopy(DUO_OK)
        wide["scenes"][1]["shot"] = "wide shot"
        wide["scenes"][3].update(listener="")                          # characters 는 [A, C] 인데 듣는 사람 칸이 비었다 → C 에게 하는 말 (G0 (a))
        wide["scenes"][2].update(listener="B")                          # 말하는 사람과 같은 듣는 사람
        left = video.dialogue_problems(wide, 48, True)
        assert any("Scene 2 is a conversation with 2 people: the shot must be a medium two-shot" in x for x in left), left
        assert not any(x.startswith("Scene 4") for x in left), left
        assert any('Scene 3: "listener" is the speaker "B" (talking to oneself)' in x for x in left), left
        assert not any(x.startswith("Scene 3 is a monologue") for x in left), left        # 자기 id 는 그 한 줄로 (사람 수·샷 위반을 겹쳐 내지 않는다)
        cw = video.clean_dialogue_story(wide, 48)
        # 정리는 뜻을 살린다: 빈 칸은 characters 의 상대(C), 말하는 사람 자신은 혼잣말이라 독백 (3차 검토: 'herself' 는 독백인데 자기 id 는 대화였다)
        assert cw["scenes"][3]["listener"] == "C" and cw["scenes"][2]["listener"] == ""
        assert cw["scenes"][2]["monologue"] is True and cw["scenes"][2]["characters"] == ["B"] and cw["scenes"][2]["continues"] is False
        # 혼자 말하는 컷(혼잣말·전화)은 독백: 클로즈업까지
        solo = _copy.deepcopy(DUO_OK)
        solo["scenes"][3].update(characters=["A"], listener="", shot="close-up")
        # 독백의 장면 글이 다른 사람을 가리키면 위반: 'Niki' 는 장면 글에서 배운 니키(C)의 영어 이름이다 (3차 검토: id 가 A·B·C 면 영어 이름을 몰랐다)
        assert any('mentions someone else ("Niki")' in x for x in video.dialogue_problems(solo, 48, True))
        solo["scenes"][3]["action"] = "Beth smiles softly to herself."
        assert video.dialogue_problems(solo, 48, True) == []
        cs = video.clean_dialogue_story(solo, 48)
        assert video.talk_pair(cs, 3) is None and not video.duo_talk(cs, 3) and cs["scenes"][3]["characters"] == ["A"]
        assert cs["scenes"][3]["monologue"] is True
        # 듣는 쪽이 동물이면 사람 한 명의 대사 컷이다 (두 화자 레시피를 쓰지 않는다)
        pet = _copy.deepcopy(s)
        pet["bible"]["characters"].append({"id": "D", "look": "a small white puppy with floppy ears"})
        pet["scenes"][3].update(characters=["A", "D"], listener="D")
        assert video.talk_pair(pet, 3) is None and not video.duo_talk(pet, 3)


def test_duo_talk_off_keeps_the_old_rules_and_prompts():
    import copy as _copy
    with duo_setting(False):
        assert video.dialogue_extra() is video.DIALOGUE_EXTRA and video.dialogue_schema() is video.DIALOGUE_SCHEMA_SOLO
        assert "listener" not in video.DIALOGUE_SCHEMA_SOLO["properties"]["scenes"]["items"]["properties"]
        left = video.dialogue_problems(DUO_OK, 48, True)
        assert any('must be exactly ["A"] (the speaker alone in the frame)' in x for x in left), left
        assert any(x == "Scene 3 is a talk scene, so it cannot continue the previous scene." for x in left), left
        # 예전 규칙의 샷 크기: 넓은 샷의 혼자 대사 컷은 위반 (11-24 검토: 이 분기를 보는 시험이 없었다)
        wide = _copy.deepcopy(DUO_OK)
        wide["scenes"][3].update(characters=["A"], listener="", shot="wide shot")
        assert "Scene 4 is a talk scene: the shot must be a close-up or a medium close-up of the speaker." in video.dialogue_problems(wide, 48, True)
        wide["scenes"][3]["shot"] = "medium shot"
        assert not any("Scene 4 is a talk scene: the shot" in x for x in video.dialogue_problems(wide, 48, True))
        s = video.clean_dialogue_story(DUO_OK, 48)
        assert [x["characters"] for x in s["scenes"] if x["kind"] == "talk"] == [["A"], ["B"], ["A"]]
        assert not any(x["continues"] for x in s["scenes"]) and all(x["listener"] == "" for x in s["scenes"])
        assert not any(video.duo_talk(s, k) or video.talk_pair(s, k) for k in range(6))
        # 예전 두 사람 자리: 말한 차례로 마주 보게, 같은 쪽이면 장면에 적힌 차례 (니키는 말하지 않아 첫 화자와 같은 쪽)
        assert [x["id"] for x in video.layout_slots(s, 0, ["A", "B"])] == ["A", "B"]
        assert [x["id"] for x in video.layout_slots(s, 4, ["C", "A"])] == ["C", "A"]
        lp = video.layout_prompt(s, 3, video.layout_slots(s, 3, ["A"]))
        assert "One adult-sized faceless mannequin figure" in lp and "turned toward each other" not in lp
        talk = video.talk_prompt(s, 1)
        assert "alone in the frame, no other people" in talk
        call = Call([DUO_OK] * 3)
        asyncio.run(video.write_dialogue_story(call, "베스와 제임스가 아침 식탁에서 대화하는 영상", "", 48))
        assert call.calls[0]["format"] is video.DIALOGUE_SCHEMA_SOLO
        system = call.calls[0]["messages"][0]["content"]
        assert "to someone off-screen" in system and "medium two-shot" not in system and "very slowly pushing camera" in system
    with duo_setting(True):
        assert video.dialogue_extra() is video.DIALOGUE_EXTRA_DUO and video.dialogue_schema() is video.DIALOGUE_SCHEMA
        items = video.DIALOGUE_SCHEMA["properties"]["scenes"]["items"]
        assert "listener" in items["required"] and items["properties"]["listener"] == {"type": "string"}
        ex = video.DIALOGUE_EXTRA_DUO
        assert '"listener"' in ex and "medium two-shot" in ex and "three-quarter view toward the camera" in ex
        assert "very slowly pushing camera" not in ex and "cut to the listener's face" not in ex
        assert "6 Korean characters per second" in ex and '"sample_ko"' in ex and "never read from a page" in ex
        assert "same people in the same place" in ex and "A monologue never continues" in ex
        # 사용자 확정 규칙(11-24): 대사마다 누구에게 하는 말인지(id / "everyone" / "nobody"), 독백은 다른 사람이 있어도 혼자 클로즈업
        assert '"everyone" when the speaker talks to everyone in the place' in ex and '"nobody" for a monologue' in ex
        assert "A monologue is a close-up of the speaker alone, even when other people are in the place" in ex
        assert "medium group shot" in ex and "at most three people" in ex
        assert '"listener" is "" when' not in ex and "Only when nobody else is in the place" not in ex       # 2차 진행 중 글의 어긋난 규칙
        call = Call([DUO_OK])
        story, left, _r = asyncio.run(video.write_dialogue_story(call, "베스와 제임스가 아침 식탁에서 대화하는 영상", "", 48))
        assert call.calls[0]["format"] is video.DIALOGUE_SCHEMA and left == [] and story["scenes"][2]["continues"] is True
        assert "medium two-shot" in call.calls[0]["messages"][0]["content"]
    # 감독이 요청마다 정한 값(duo)이 설정보다 앞선다: 두 사람 컷을 만들 수 없는 요청(세로 영상·장면 잇기 꺼짐·두 화자 레시피 꺼짐)은
    # 설정이 켜져 있어도 예전 혼자 규칙으로 쓰고 검사·다듬는다 (11-24 검토). 그 반대도 같다
    with duo_setting(True):
        assert video.dialogue_extra(False) is video.DIALOGUE_EXTRA and video.dialogue_schema(False) is video.DIALOGUE_SCHEMA_SOLO
        assert not video.duo_rules(False) and video.duo_rules(None) and video.duo_rules()
        left = video.dialogue_problems(DUO_OK, 48, True, duo=False)
        assert any('must be exactly ["A"] (the speaker alone in the frame)' in x for x in left), left
        so = video.clean_dialogue_story(DUO_OK, 48, duo=False)
        assert [x["characters"] for x in so["scenes"] if x["kind"] == "talk"] == [["A"], ["B"], ["A"]]
        assert not any(x["continues"] for x in so["scenes"])
        call = Call([DUO_OK] * 3)
        st, _l, _r = asyncio.run(video.write_dialogue_story(call, "베스와 제임스가 아침 식탁에서 대화하는 영상", "", 48, duo=False))
        assert call.calls[0]["format"] is video.DIALOGUE_SCHEMA_SOLO and "to someone off-screen" in call.calls[0]["messages"][0]["content"]
        assert not any(video.talk_pair(st, k) for k in range(len(st["scenes"])))
    with duo_setting(False):
        assert video.dialogue_extra(True) is video.DIALOGUE_EXTRA_DUO and video.dialogue_problems(DUO_OK, 48, True, exchange=True, duo=True) == []
        call = Call([DUO_OK])
        st, left, _r = asyncio.run(video.write_dialogue_story(call, "베스와 제임스가 아침 식탁에서 대화하는 영상", "", 48, duo=True))
        assert call.calls[0]["format"] is video.DIALOGUE_SCHEMA and left == [] and video.talk_pair(st, 1) == ["A", "B"]


def test_storyboard_follows_the_duo_decision_of_the_director():
    """11-24 검토: 구성안의 두 사람 규칙은 감독이 정한 값(storyboard duo)을 따른다. 설정만 보면 세로 영상·장면 잇기 꺼짐에서도
    투샷 대사 컷을 써서, 투샷 첫 화면이 LTX 로 가거나(X2: 입이 거의 안 움직임) 한 사람 첫 화면에 투샷 글이 붙었다"""
    seen = []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        async def call(payload):
            seen.append((payload["messages"][0]["content"], payload["format"]))
            return {"message": {"content": json.dumps(DUO_OK)}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    ask = "두 사람이 아침 식탁에서 대화하는 영상"           # 라이브러리 인물 이름이 없는 요청
    try:
        with duo_setting(True):
            r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", ask, seconds=48, dialogue=True, duo=False))
            assert seen[0][1] is video.DIALOGUE_SCHEMA_SOLO and "to someone off-screen" in seen[0][0]
            assert not any(video.talk_pair(r["story"], k) for k in range(len(r["story"]["scenes"])))
            seen.clear()
            r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", ask, seconds=48, dialogue=True))
            assert seen[0][1] is video.DIALOGUE_SCHEMA and video.talk_pair(r["story"], 1) == ["A", "B"]       # 값이 없으면 설정
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved


def test_duo_sides_stay_the_same_and_the_listener_faces_the_speaker():
    with duo_setting(True):
        s = video.clean_dialogue_story(_duo_pairs(), 48)
        assert video.talk_pair(s, 3) == ["A", "C"]
        # 베스(A)가 첫 화자라 오른쪽을 본다 → 왼쪽 자리. 장면이 바뀌어도(말하는 사람이 바뀌어도) 좌우가 같다 (180도 규칙)
        order = lambda k, ids: [x["id"] for x in video.layout_slots(s, k, ids)]
        assert order(1, video.scene_people(s, 1)) == order(2, video.scene_people(s, 2)) == order(0, ["A", "B"]) == ["A", "B"]
        # 한 번도 말하지 않는 니키(C)는 첫 화자와 같은 쪽이 되지만, 두 사람 대사 컷에서는 말하는 사람의 반대쪽을 본다 (C3)
        assert video.speaker_side(s, "C") == video.speaker_side(s, "A") == "right"
        assert video.pair_facing(s, "A", "C") == {"A": "right", "C": "left"}
        assert order(3, ["C", "A"]) == ["A", "C"] and order(4, ["C", "A"]) == ["A", "C"]         # 같은 두 사람의 말 없는 장면도 같은 좌우
        sl = video.layout_slots(s, 3, ["A", "C"])
        assert [x["facing"] for x in sl] == ["turned toward the right", "turned toward the left"]
        # 화자 셋: 1번(A)과 3번(C)이 같은 쪽 → 둘의 첫 대사 컷의 말하는 사람이 제 쪽, 상대는 반대. 뒤의 반대 방향 대사 컷도 같은 좌우
        three = _duo_pairs()
        three["bible"]["characters"][2].update(voice="A bright Korean girl, clear.", voice_ko="맑은 아이")
        three["scenes"][3].update(characters=["C", "A"], speaker="C", listener="A", line_ko="엄마, 고마워요!", emotion="happy")
        three["scenes"][4].update(kind="talk", characters=["A", "C"], speaker="A", listener="C", shot="medium two-shot",
                                  line_ko="사랑한단다.", emotion="tender")
        t3 = video.clean_dialogue_story(three, 48)
        assert video.speakers_of(t3) == ["A", "B", "C"] and video.speaker_side(t3, "C") == "right"
        assert video.pair_facing(t3, "A", "C") == {"C": "right", "A": "left"}
        o3 = [x["id"] for x in video.layout_slots(t3, 3, video.scene_people(t3, 3))]
        o4 = [x["id"] for x in video.layout_slots(t3, 4, video.scene_people(t3, 4))]
        assert o3 == o4 == ["C", "A"], (o3, o4)
        # ① 구도 글: 서로 마주 보되 두 얼굴 모두 카메라 쪽 3/4, 미디엄 투샷도 머리 위 여유. 장면 글은 넣지 않는다
        slots = video.layout_slots(s, 2, video.scene_people(s, 2))
        lp = video.layout_prompt(s, 2, slots)
        assert ("2 faceless mannequin figures, sitting, turned toward each other, both faces in three-quarter view toward the camera: "
                "on the left a adult-sized figure turned toward the right; on the right a adult-sized figure turned toward the left.") in lp
        assert "clear empty space above the hair (about one tenth of the frame height); no head touches the top edge" in lp
        assert "James raises" not in lp and lp.endswith("Exactly 2 figures, no other figure or person.")
        assert video._frame_hint(2, 2) == "The heads and upper bodies are fully in frame, in the middle part of the picture."   # 두 사람 장면(대사 아님)은 그대로
        # ② 인물 입히기: 말하는 방향은 실제 자리에서 상대 쪽, 듣는 사람은 입을 다문 미소로 듣는다 (11-24 X13: 이가 보이는 웃음은 입이 벌어져 보였다)
        ip = video.identity_prompt(s, 2, slots, ["B", "A"], final=True)
        assert "The person on the right speaks toward the left side of the frame with a surprised expression" in ip
        assert "The person on the left listens with lips closed and a gentle closed-mouth smile, looking at the speaker." in ip
        assert "Keep both heads where they are in Picture 1" in ip and "Exactly 2 people" in ip and "James raises" not in ip
        ip1 = video.identity_prompt(s, 1, video.layout_slots(s, 1, ["A", "B"]), ["A", "B"], final=True)
        assert "The person on the left speaks toward the right side of the frame with a warm and excited expression" in ip1
        assert "The person on the right listens with lips closed" in ip1
        # 구도 그림 없이 바로 그릴 때·앞 프레임을 고칠 때도 같은 좌우
        cp = video.compose_prompt(s, 2, ["B", "A"], place_image=3)
        assert "The person from image 1 is on the right and speaks toward the left side of the frame" in cp
        assert "The person from image 2 is on the left, turned toward the speaker, and listens with lips closed" in cp
        assert "James raises" not in cp and "Exactly 2 people" in cp
        ce = video.cut_edit_prompt(s, 3, ["A", "C"], "cut")
        assert "is on the left, turned toward the right side of the frame, speaking" in ce and "listening with lips closed" in ce
        # 한 사람 컷도 그 사람이 나오는 투샷과 같은 쪽을 본다 (facing_of, 11-24 검토 D2): 한 번도 말하지 않는 니키(C)는 장면 3 투샷에서
        # 왼쪽을 보므로 바로 다음 장면 4 클로즈업도 왼쪽 (speaker_side 로는 오른쪽이라 시선이 튀었다)
        assert video.speaker_side(s, "C") == "right" and video.facing_of(s, 4, "C") == "left"
        assert video.layout_slots(s, 4, ["C"])[0]["facing"] == "turned slightly toward the left"
        assert "toward the left side of the frame" in video.cast_edit_prompt(s, 4)
        assert "turned toward the left side of the frame" in video.cast_prompt(s, "C")          # 기준 그림은 처음 나오는 투샷
        assert video.facing_of(s, 0, "C") == "left"                                          # 앞에 투샷이 없으면 뒤의 투샷
        assert video.facing_of(s, 5, "A") == "right" and video.facing_of(s, 5, "B") == "left"
        assert video.facing_of(t3, 5, "A") == "left" and video.facing_of(t3, 1, "A") == "right"   # 가장 가까운 앞 투샷의 쌍을 따른다
    with duo_setting(False):
        so = video.clean_dialogue_story(DUO_OK, 48)                  # 투샷이 없으면 예전 그대로 (말한 차례)
        assert all(video.facing_of(so, k, c) == video.speaker_side(so, c) for k in range(6) for c in ("A", "B", "C"))


def test_duo_talk_prompt_and_still_camera_shot():
    import copy as _copy
    with duo_setting(True):
        s = video.clean_dialogue_story(DUO_OK, 48)
        slots = video.layout_slots(s, 2, video.scene_people(s, 2))
        dp = video.duo_talk_prompt(s, 2, slots)
        assert dp.startswith("Medium two-shot of two people in a conversation, both fully in frame the whole time.")
        assert "On the left: a calm neat woman around forty" in dp and "On the right: a tall lean man in his early forties" in dp
        assert "The person on the right speaks to the person on the left, surprised: the lips and jaw move clearly with the words." in dp
        assert ("The person on the left only listens with lips closed and a gentle closed-mouth smile: the mouth stays closed and the head "
                "nods softly.") in dp
        assert "Natural blinking and small head movements." in dp and "The camera stays completely still." in dp
        assert "sunny cottage kitchen" in dp and "Style: semi-realistic painterly" in dp and video.style_lock(s) in dp
        assert "James raises" not in dp and "Niki" not in dp and len(dp) <= 3000          # 장면 글(화면 밖 이름)은 넣지 않는다 (R3)
        dp1 = video.duo_talk_prompt(s, 1, video.layout_slots(s, 1, ["A", "B"]))
        assert "The person on the left speaks to the person on the right, warm and excited" in dp1
        # 혼자 대사 영상 글: 샷 글의 카메라 움직임 말을 지운다 (삼각대 문장과 부딪힘)
        for shot, want in (("medium close-up, very slowly pushing camera", "medium close-up"), ("close-up, slow push in", "close-up"),
                           ("slow push-in close-up", "close-up"), ("medium shot; slow dolly forward", "medium shot"),
                           ("close-up, gentle zoom in, slight tilt down", "close-up"), ("medium two-shot", "medium two-shot"),
                           ("wide shot from behind, slow follow", "wide shot from behind, slow follow"), ("panning shot", "")):
            assert video.still_shot(shot) == want, (shot, video.still_shot(shot))
        # 움직임 말이 크기 말과 같은 마디에 있어도 앞뒤 이음말이 매달리지 않는다 (11-24 검토: '… with a still or' 로 남았다).
        # 움직임이 있던 마디는 크기 말이 남을 때만 둔다
        for shot, want in (("Medium close-up of Beth with a still or very slowly pushing camera", "Medium close-up of Beth"),
                           ("Close-up of Niki's face as the camera slowly pushes in", "Close-up of Niki's face"),
                           ("Extreme close-up with a slow zoom in", "Extreme close-up"),
                           ("close-up with a slow push in, warm light", "close-up, warm light"),
                           ("Medium close-up, slow push-in toward her face", "Medium close-up"),
                           ("close-up while the camera tracks left", "close-up")):
            assert video.still_shot(shot) == want, (shot, video.still_shot(shot))
        st = video.clean_dialogue_story(DLG_OK, 48)
        st["scenes"][3]["shot"] = "medium close-up, very slowly pushing camera"
        tp = video.talk_prompt(st, 3)
        assert tp.startswith("medium close-up of one person alone in the frame") and "pushing camera" not in tp
        assert "locked on a tripod and never moves" in tp
        st["scenes"][3]["shot"] = "slow pan"
        assert video.talk_prompt(st, 3).startswith("Close-up of one person alone")
        st["scenes"][3]["shot"] = "Medium close-up of Beth with a still or very slowly pushing camera"
        tp = video.talk_prompt(st, 3)
        assert tp.startswith("Medium close-up of Beth of one person alone") and "with a still or" not in tp
        # 두 사람 규칙으로 짠 대사 장면을 한 사람 첫 화면으로 만들 때(장면 잇기 꺼짐): 투샷 글은 혼자 크기로, 화면 밖 상대를 가리키는
        # 장면 글('speaks softly to James')은 뺀다 (11-24 검토: 'Medium two-shot of one person alone' + 'She smiles at him')
        tp1 = video.talk_prompt(s, 1)
        assert tp1.startswith("Medium close-up of one person alone in the frame, no other people:") and "two-shot" not in tp1
        assert "speaks softly to James" not in tp1 and "toward the right edge of the frame" in tp1
        ce1 = video.cast_edit_prompt(s, 1)
        assert "Medium close-up." in ce1 and "two-shot" not in ce1 and video.ONE_PERSON in ce1
        assert video.solo_shot("medium two-shot") == "Medium close-up" and video.solo_shot("close-up") == "close-up"
        # 혼자 말하는 컷(혼잣말)은 장면 글 그대로
        solo = _copy.deepcopy(s)
        solo["scenes"][3].update(characters=["A"], listener="", shot="close-up", action="Beth whispers to herself.")
        assert "Beth whispers to herself." in video.talk_prompt(solo, 3)
        # 듣는 쪽이 동물이면 동물은 첫 화면에 있다(화면 밖 사람이 아니다): 장면 글은 두고, 영상 글은 사람과 동물을 함께 쓴다 (5차 검토: '화면에
        # 혼자 있는 한 사람' 글이 첫 화면의 강아지와 어긋났다 — 4차 검토의 동물 화자처럼 동물도 화면의 한 몫, 투샷 크기)
        pet = _copy.deepcopy(s)
        pet["bible"]["characters"].append({"id": "D", "look": "a small white puppy with floppy ears"})
        pet["scenes"][3].update(characters=["A", "D"], listener="D", action="Beth smiles at the puppy.")
        tpp = video.talk_prompt(pet, 3)
        assert tpp.startswith("Medium two-shot: a calm neat woman around forty") and "Also in the frame: a small white puppy with floppy ears." in tpp
        assert "Beth smiles at the puppy." in tpp and "speaks to the small white puppy" in tpp and "alone in the frame" not in tpp, tpp
        assert "only listens" in tpp and "locked on a tripod" in tpp
        # 첫 화면이 기준 그림 한 장을 고친 한 사람 그림이면(장면 잇기 꺼짐, solo) 예전처럼 한 사람 글
        tps = video.talk_prompt(pet, 3, solo=True)
        assert tps.startswith("Medium close-up of one person alone") and "Beth smiles at the puppy." in tps


# ── 두세 사람 대사 장면 2차 (11-24): 사용자 확정 규칙 G0 "대화 할 때는 대화를 주고받는 이들이 나와야 하고, 독백 씬에는 클로즈업이
#    나와야 해" + "셋 모두 나오게" — 가족 세 사람의 아침 식탁 ──
FAMILY = {
    "title_ko": "니키의 생일 아침", "logline_ko": "가족이 아침 식탁에서 니키의 생일을 이야기한다.",
    "bible": {"characters": [
        {"id": "A", "look": "a calm neat woman around forty with long straight black hair, a cream blouse and an apron",
         "name_ko": "베스", "voice": "A warm Korean woman around forty, gentle and clear.", "voice_ko": "따뜻한 엄마"},
        {"id": "B", "look": "a tall lean man in his early forties with short brown hair and a blue cardigan", "name_ko": "제임스",
         "voice": "A calm Korean man in his forties, low and friendly.", "voice_ko": "차분한 아빠"},
        {"id": "C", "look": "a twelve-year-old girl with two long black braids and a wine-red cape", "name_ko": "니키",
         "voice": "A bright twelve-year-old Korean girl, clear and lively.", "voice_ko": "맑은 아이"}],
        "places": [{"id": "P1", "look": "a sunny cottage kitchen with a wooden breakfast table"},
                   {"id": "P2", "look": "a small garden with an apple tree"},
                   {"id": "P3", "look": "a quiet attic bedroom with a round window"}],
        "time": "morning, clear", "light": "soft warm window light", "style": "semi-realistic painterly 2.5D film look"},
    "music": {"tags": "gentle cinematic instrumental, strings", "bpm": 72, "key": "D major", "ko": "잔잔한 현악"},
    "scenes": [
        {"seconds": 16, "place": "P1", "characters": ["A", "B", "C"], "continues": False, "shot": "wide shot",
         "action": "The family sits around the breakfast table in the morning light.", "sound": "birds outside, a kettle",
         "ko": "가족의 아침 식탁", "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
        {"seconds": 6, "place": "P1", "characters": ["A", "B", "C"], "continues": False, "shot": "medium group shot",
         "action": "Beth leans toward James with a warm smile.", "sound": "soft room tone", "ko": "베스가 제임스에게 말한다",
         "kind": "talk", "speaker": "A", "listener": "B", "line_ko": "여보, 오늘 니키 생일이에요.", "emotion": "warm and excited"},
        {"seconds": 6, "place": "P1", "characters": ["B", "A", "C"], "continues": True, "shot": "medium group shot",
         "action": "James looks around the table and laughs.", "sound": "soft room tone", "ko": "제임스가 모두에게 말한다",
         "kind": "talk", "speaker": "B", "listener": "everyone", "line_ko": "자, 다 같이 축하하자!", "emotion": "cheerful"},
        {"seconds": 5, "place": "P1", "characters": ["C"], "continues": False, "shot": "close-up",
         "action": "Niki looks down at the table, thinking.", "sound": "soft room tone", "ko": "니키의 혼잣말",
         "kind": "talk", "speaker": "C", "listener": "nobody", "line_ko": "다들 기억하고 있었구나...", "emotion": "quiet and moved"},
        {"seconds": 6, "place": "P1", "characters": ["C", "A"], "continues": False, "shot": "medium two-shot",
         "action": "Niki beams at her mother.", "sound": "soft room tone", "ko": "니키가 엄마에게 말한다",
         "kind": "talk", "speaker": "C", "listener": "A", "line_ko": "엄마, 고마워요!", "emotion": "happy"},
        {"seconds": 10, "place": "P2", "characters": ["A", "C"], "continues": False, "shot": "wide shot",
         "action": "Beth and Niki stand under the apple tree.", "sound": "wind in the leaves", "ko": "정원의 두 사람",
         "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
        {"seconds": 11, "place": "P3", "characters": ["A"], "continues": False, "shot": "close-up",
         "action": "Beth sits alone by the round window, smiling softly.", "sound": "a clock ticking", "ko": "베스의 혼잣말",
         "kind": "talk", "speaker": "A", "listener": "nobody", "line_ko": "벌써 열두 살이라니...", "emotion": "tender, a little wistful"}]}
FAMILY_IDS = {"A", "B", "C"}


def test_g0_family_table_conversation_shows_everyone_and_monologue_is_a_close_up():
    import copy as _copy
    with duo_setting(True):
        assert video.dialogue_problems(FAMILY, 60, True, exchange=True) == []
        s = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        sc = s["scenes"]
        # 베스가 제임스에게 하는 말(대화) → 식탁의 세 사람 모두 그룹샷. 제임스가 모두에게 → 세 사람, 같은 사람들이라 앞 컷을 잇는다
        assert sc[1]["characters"] == ["A", "B", "C"] and sc[1]["listener"] == "B" and sc[1]["shot"] == "medium group shot"
        assert sc[1]["monologue"] is False and sc[1]["to_all"] is False
        assert sc[2]["characters"] == ["B", "A", "C"] and sc[2]["listener"] == "" and sc[2]["to_all"] is True
        assert sc[2]["continues"] is True and video.scene_seam(s, 2) == video.SEAM_CONTINUE
        # 니키의 혼잣말(독백): 같은 식탁에 둘이 있어도 니키 혼자 클로즈업. 사람을 더하지 않고, 잇지도 이어지지도 않는다
        assert sc[3]["characters"] == ["C"] and sc[3]["monologue"] is True and sc[3]["shot"] == "close-up"
        assert sc[3]["continues"] is False and sc[4]["continues"] is False
        # 독백 다음의 대화 컷(니키가 엄마에게): 독백 클로즈업은 다른 사람을 화면에서 뺄 뿐이라 식탁의 제임스도 다시 나온다
        assert sc[4]["characters"] == ["C", "A", "B"] and sc[4]["listener"] == "A" and sc[4]["shot"] == "medium group shot"
        # 혼자 있는 다락방의 독백
        assert sc[6]["characters"] == ["A"] and sc[6]["monologue"] is True and sc[6]["listener"] == "" and sc[6]["shot"] == "close-up"
        assert "monologue" not in sc[0] and sc[0]["listener"] == ""                           # 말 없는 장면에는 표시가 없다
        assert [video.talk_group(s, k) for k in range(7)] == [None, ["A", "B", "C"], ["B", "A", "C"], None, ["C", "A", "B"], None, None]
        assert [video.duo_talk(s, k) for k in range(7)] == [False, True, True, False, True, False, False]
        # 독백은 혼자 LTX 대사 글 (혼자, 장면 글 그대로). 대화 컷의 영상 글은 세 사람 (test_g3_three_people_prompts)
        tp = video.talk_prompt(s, 3)
        assert "close-up of one person alone in the frame" in tp and "Niki looks down at the table, thinking." in tp
        # 샷 글 다듬기
        assert video.group_shot("close-up", 2) == "medium two-shot" and video.group_shot("medium two-shot", 3) == "medium group shot"
        assert video.group_shot("long shot", 2) == "medium two-shot" and video.group_shot("close-up", 1) == "close-up"
        # 미디엄보다 넓은 대화 컷(풀샷·미디엄 와이드)도 미디엄 투샷·그룹샷으로 (G0-1, 메인 X11~X13 은 미디엄 크기로만 통과, 11-24 2차 검토)
        assert video.group_shot("medium group shot", 2) == "medium two-shot" and video.group_shot("full shot", 3) == "medium group shot"
        assert video.group_shot("medium wide shot", 2) == "medium two-shot" and video.group_shot("medium shot of the table", 3) == "medium shot of the table"
        wide = _copy.deepcopy(FAMILY)
        wide["scenes"][1]["shot"] = "full shot"
        wide["scenes"][4]["shot"] = "medium wide shot"
        assert video.dialogue_problems(wide, 60, True, exchange=True) == []          # 검사는 크기 3 까지 받고, 정리가 미디엄으로 맞춘다
        cw = video.clean_dialogue_story(wide, 60, exchange=True)
        assert cw["scenes"][1]["shot"] == cw["scenes"][4]["shot"] == "medium group shot"
        assert video.close_shot("medium shot") == "medium close-up" and video.close_shot("close-up of Niki") == "close-up of Niki"
        assert video.close_shot("medium two-shot") == "medium close-up" and video.close_shot("") == "medium close-up"
    with duo_setting(False):        # 꺼짐(STUDIO_DUO_TALK=false): 예전 그대로 모든 대사가 말하는 사람 혼자
        off = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        assert [x["characters"] for x in off["scenes"] if x["kind"] == "talk"] == [["A"], ["B"], ["C"], ["C"], ["A"]]
        assert not any(x.get("monologue") or x.get("to_all") for x in off["scenes"]) and not any(x["continues"] for x in off["scenes"])
        assert not any(video.talk_group(off, k) for k in range(7)) and all(x["listener"] == "" for x in off["scenes"])
        assert any('must be exactly ["A"] (the speaker alone in the frame)' in x for x in video.dialogue_problems(FAMILY, 60, True))


def test_g0_listener_inference_and_checks():
    import copy as _copy
    with duo_setting(True):
        # (a) 듣는 사람 칸이 비었어도 characters 에 다른 사람이 있으면 대화: 말하는 사람 다음 사람이 듣는 사람 (1차 동작)
        a = _copy.deepcopy(FAMILY)
        a["scenes"][1]["listener"] = ""
        assert video.line_target(a["scenes"], 1, FAMILY_IDS) == ("one", "B")
        assert video.dialogue_problems(a, 60, True, exchange=True) == []
        assert video.clean_dialogue_story(a, 60)["scenes"][1]["listener"] == "B"
        # (b) 대화 요청이면 같은 장소의 앞뒤 다른 사람 대사의 화자가 듣는 사람. 그 사람이 화면에 없으면 위반, 정리는 넣는다
        b = _copy.deepcopy(FAMILY)
        b["scenes"][1].update(characters=["A"], listener="")
        assert video.line_target(b["scenes"], 1, FAMILY_IDS, exchange=True) == ("one", "B")          # 앞 대사가 없어 뒤 대사(제임스)
        left = video.dialogue_problems(b, 60, True, exchange=True)
        assert ('Scene 2: the person spoken to must be in the shot: put "B" in "characters" right after the speaker '
                '(or write "nobody" in "listener" if the line is a monologue).') in left, left
        cb = video.clean_dialogue_story(b, 60, exchange=True)
        assert cb["scenes"][1]["listener"] == "B" and cb["scenes"][1]["characters"] == ["A", "B", "C"]
        assert cb["scenes"][1]["shot"] == "medium group shot"
        # (c) 대화 요청이 아니면 독백: 혼자 클로즈업 (그룹샷 글은 미디엄 클로즈업으로)
        assert video.line_target(b["scenes"], 1, FAMILY_IDS) == ("mono", "")
        cc = video.clean_dialogue_story(b, 60)
        assert cc["scenes"][1]["characters"] == ["A"] and cc["scenes"][1]["monologue"] is True and cc["scenes"][1]["shot"] == "medium close-up"
        assert any("Scene 2 is a monologue: the shot must be a close-up" in x for x in video.dialogue_problems(b, 60, True))
        # (b) 의 앞뒤 대사: 같은 사람이 이어 말한 줄은 건너뛰고, 장소가 바뀐 줄은 보지 않는다
        seq = [{"kind": "talk", "speaker": "B", "line_ko": "안녕?", "place": "P1"}, {"kind": "talk", "speaker": "A", "line_ko": "응.", "place": "P1"},
               {"speaker": "A", "line_ko": "있잖아.", "place": "P1", "characters": ["A"]},
               {"kind": "talk", "speaker": "C", "line_ko": "뭐?", "place": "P2"}]
        assert video.line_target(seq, 2, FAMILY_IDS, exchange=True) == ("one", "B")
        seq[0]["place"] = "P0"
        assert video.line_target(seq, 2, FAMILY_IDS, exchange=True) == ("mono", "")
        # 앞 대사 먼저: [B 대사, A 대사(characters [A]), C 대사] 의 A 는 B 에게 하는 말 (11-24 2차 검토: 차례를 고정하는 시험이 없었다)
        both = [{"kind": "talk", "speaker": "B", "line_ko": "안녕?", "place": "P1", "characters": ["B"]},
                {"kind": "talk", "speaker": "A", "line_ko": "응, 왔니?", "place": "P1", "characters": ["A"]},
                {"kind": "talk", "speaker": "C", "line_ko": "나도 왔어!", "place": "P1", "characters": ["C"]}]
        assert video.line_target(both, 1, FAMILY_IDS, exchange=True) == ("one", "B")
        # 화면 밖 목소리(그 장면 characters 에 없는 화자의 shot 장면 대사)는 듣는 사람이 아니다: 뒤의 보이는 화자(니키)에게 하는 말 (11-24 2차 검토)
        both[0].update(kind="shot", characters=[])
        assert video.line_target(both, 1, FAMILY_IDS, exchange=True) == ("one", "C")
        both[0]["characters"] = ["B"]                    # 보이는 사람의 목소리(멀리·뒤에서)는 그 자리에 있다
        assert video.line_target(both, 1, FAMILY_IDS, exchange=True) == ("one", "B")
        # 화면 밖 목소리에서 멈춘다: 그 너머의 앞 대사(베스)까지 거슬러 가지 않는다
        far = [{"kind": "talk", "speaker": "A", "line_ko": "얘들아!", "place": "P1", "characters": ["A"]},
               {"kind": "shot", "speaker": "B", "line_ko": "나 여기 있어!", "place": "P1", "characters": []},
               {"kind": "talk", "speaker": "C", "line_ko": "아빠 목소리다...", "place": "P1", "characters": ["C"]}]
        assert video.line_target(far, 2, FAMILY_IDS, exchange=True) == ("mono", "")
        # 다락방에 혼자인 베스(듣는 사람 칸이 빈 대사) 다음에, 사람 없이 닫힌 문만 보이고 문 밖 제임스의 목소리가 들린다: 베스는 독백이다
        attic = _copy.deepcopy(FAMILY)
        attic["scenes"][6]["listener"] = ""
        attic["scenes"].append({"seconds": 5, "place": "P3", "characters": [], "continues": False, "shot": "close-up of the closed door",
                                "action": "The attic door stays closed.", "sound": "a voice behind the door", "ko": "닫힌 다락방 문",
                                "kind": "shot", "speaker": "B", "listener": "", "line_ko": "여보, 내려와요!", "emotion": "calling"})
        assert video.line_target(attic["scenes"], 6, FAMILY_IDS, exchange=True) == ("mono", "")
        ca = video.clean_dialogue_story(attic, 65, exchange=True)
        assert ca["scenes"][6]["characters"] == ["A"] and ca["scenes"][6]["monologue"] is True
        assert not any("put \"B\" in \"characters\"" in x for x in video.dialogue_problems(attic, 65, True, exchange=True))
        # "nobody"(독백)라고 적은 장면은 추정하지 않는다: characters 에 다른 사람이 있고 대화 요청이어도 독백
        n = _copy.deepcopy(FAMILY)
        n["scenes"][3]["characters"] = ["C", "A"]
        assert video.line_target(n["scenes"], 3, FAMILY_IDS, exchange=True) == ("mono", "")
        left = video.dialogue_problems(n, 60, True, exchange=True)
        assert ('Scene 4 is a monologue ("C" speaks to nobody in the place): a monologue is a close-up of the speaker alone even when '
                'others are there, so "characters" must be exactly ["C"].') in left, left
        assert video.clean_dialogue_story(n, 60, exchange=True)["scenes"][3]["characters"] == ["C"]
        # 독백의 샷 크기 2(미디엄)는 위반, 정리는 미디엄 클로즈업
        m = _copy.deepcopy(FAMILY)
        m["scenes"][3]["shot"] = "medium shot"
        assert ("Scene 4 is a monologue: the shot must be a close-up or a medium close-up of the speaker alone, not a medium or wider shot."
                in video.dialogue_problems(m, 60, True, exchange=True))
        assert video.clean_dialogue_story(m, 60, exchange=True)["scenes"][3]["shot"] == "medium close-up"
        # 모두에게 하는 말인데 앞 장면(같은 장소)에 있던 사람이 빠졌다: 위반, 정리는 그 자리 사람을 넣는다
        e = _copy.deepcopy(FAMILY)
        e["scenes"][2]["characters"] = ["B"]
        left = video.dialogue_problems(e, 60, True, exchange=True)
        assert ('Scene 3: "B" speaks to everyone in the place, so everyone there must be in the shot: add "A", "C" to "characters" '
                '(at most three people).') in left, left
        ce = video.clean_dialogue_story(e, 60, exchange=True)
        assert ce["scenes"][2]["characters"] == ["B", "A", "C"] and ce["scenes"][2]["to_all"] is True
        # 모두에게 하는 말인데 그 자리에 아무도 없으면 혼자 하는 말(독백)
        alone = _copy.deepcopy(FAMILY)
        alone["scenes"][6]["listener"] = "everyone"
        ca = video.clean_dialogue_story(alone, 60, exchange=True)
        assert ca["scenes"][6]["characters"] == ["A"] and ca["scenes"][6]["monologue"] is True and ca["scenes"][6]["to_all"] is False
        # 검사도 다른 장소(정원)의 앞 장면 사람을 그 자리 사람으로 보지 않는다 (11-24 2차 검토: 같은 장소 조건을 고정하는 시험이 없었다)
        assert not any("speaks to everyone" in x for x in video.dialogue_problems(alone, 60, True, exchange=True))
        # 적은 듣는 사람이 화면에 없는 대화 대사, 말하는 사람 자신을 듣는 사람으로 쓴 대사
        o = _copy.deepcopy(FAMILY)
        o["scenes"][4]["characters"] = ["C"]
        assert any('Scene 5: the person spoken to must be in the shot: put "A"' in x for x in video.dialogue_problems(o, 60, True, exchange=True))
        o["scenes"][4]["listener"] = "C"
        assert any('Scene 5: "listener" is the speaker "C" (talking to oneself)' in x for x in video.dialogue_problems(o, 60, True, exchange=True))
        # 비슷한 말도 받는다
        assert video.line_target([{"speaker": "A", "listener": "Everybody"}], 0, {"A"}) == ("all", "")
        assert video.line_target([{"speaker": "A", "listener": "no one", "characters": ["A", "B"]}], 0, {"A", "B"}) == ("mono", "")


def test_g1_presence_is_carried_to_conversation_lines_only():
    import copy as _copy
    with duo_setting(True):
        # 구성안이 식탁의 니키를 빠뜨린 대화 컷: 앞 장면(같은 장소)의 사람을 뒤에 더한다 → 이어지는 모두에게 하는 말과 같은 사람들
        f = _copy.deepcopy(FAMILY)
        f["scenes"][1].update(characters=["A", "B"], shot="medium two-shot")
        s = video.clean_dialogue_story(f, 60, exchange=True)
        assert s["scenes"][1]["characters"] == ["A", "B", "C"] and s["scenes"][1]["shot"] == "medium group shot"
        assert s["scenes"][2]["continues"] is True
        # 검사는 쓴 그대로 본다: 두 사람 컷을 세 사람 컷이 잇는 것은 위반 (고쳐 쓰게 한다)
        assert any("Scene 3 is a talk scene, so it cannot continue" in x for x in video.dialogue_problems(f, 60, True, exchange=True))
        # 장소가 바뀌면 이어 받지 않는다: 정원에서 베스가 니키에게 하는 말은 둘만 (부엌의 제임스는 없다)
        g = _copy.deepcopy(FAMILY)
        g["scenes"][5].update(kind="talk", speaker="A", listener="C", line_ko="니키야, 이리 와 보렴.", emotion="gentle",
                              shot="medium two-shot")
        sg = video.clean_dialogue_story(g, 60, exchange=True)
        assert sg["scenes"][5]["characters"] == ["A", "C"] and video.talk_pair(sg, 5) == ["A", "C"]
        # 넷이 있는 자리: 대사 컷은 말하는 사람·듣는 사람 먼저 세 사람까지
        four = _copy.deepcopy(FAMILY)
        four["bible"]["characters"].append({"id": "E", "look": "a kind grandmother in her seventies with silver hair and a lilac shawl"})
        four["scenes"][0]["characters"] = ["A", "B", "C", "E"]
        assert video.clean_dialogue_story(four, 60, exchange=True)["scenes"][1]["characters"] == ["A", "B", "C"]
        four["scenes"][1]["characters"] = ["A", "E", "B", "C"]
        left = video.dialogue_problems(four, 60, True, exchange=True)
        assert any('Scene 2 is a talk scene with 4 people in "characters": show at most three people' in x for x in left), left
        assert any('the person spoken to, "B", must come second' in x for x in left), left
        assert video.clean_dialogue_story(four, 60, exchange=True)["scenes"][1]["characters"] == ["A", "B", "E"]
        # 동물은 이어 받지 않는다 (사람만)
        pet = _copy.deepcopy(FAMILY)
        pet["bible"]["characters"].append({"id": "D", "look": "a small white puppy with floppy ears"})
        pet["scenes"][0]["characters"] = ["A", "B", "C", "D"]
        assert video.clean_dialogue_story(pet, 60, exchange=True)["scenes"][1]["characters"] == ["A", "B", "C"]
        # 독백에는 절대 더하지 않는다 (같은 장소에 둘이 있어도)
        assert video.clean_dialogue_story(f, 60, exchange=True)["scenes"][3]["characters"] == ["C"]


def test_g2_group_order_stays_the_same_through_the_conversation():
    import copy as _copy
    with duo_setting(True):
        s = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        # 넓은 가족 장면(16초) 다음 베스·제임스·니키의 대사 컷: 말하는 사람이 바뀌어도 좌우 차례가 같다
        orders = {k: [x["id"] for x in video.layout_slots(s, k, video.scene_people(s, k))] for k in (0, 1, 2, 4)}
        assert orders == {0: ["A", "B", "C"], 1: ["A", "B", "C"], 2: ["A", "B", "C"], 4: ["A", "B", "C"]}, orders
        assert [video.group_order(s, k, video.scene_people(s, k)) for k in (1, 2, 4)] == [["A", "B", "C"]] * 3
        sl = video.layout_slots(s, 4, ["C", "A", "B"])
        assert [x["id"] for x in sl] == ["A", "B", "C"] and [x["pos"] for x in sl] == ["on the left", "in the middle", "on the right"]
        assert [x["facing"] for x in sl] == ["turned toward the right", "facing the camera", "turned toward the left"]
        # 그 사람들이 모두 나온 장면이 같은 구간에 없으면 이야기에 처음 나온 차례
        assert video.group_order(s, 6, ["C", "B", "A"]) == ["A", "B", "C"]
        # 넓은 장면이 다른 차례로 적혀 있으면 대사 컷도 그 차례
        f = _copy.deepcopy(FAMILY)
        f["scenes"][0]["characters"] = ["C", "A", "B"]
        t = video.clean_dialogue_story(f, 60, exchange=True)
        assert all([x["id"] for x in video.layout_slots(t, k, video.scene_people(t, k))] == ["C", "A", "B"] for k in (0, 1, 2, 4))


def test_g3_three_people_prompts():
    with duo_setting(True):
        s = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        sl = video.layout_slots(s, 4, video.scene_people(s, 4))            # 왼쪽 베스, 가운데 제임스, 오른쪽 니키(말하는 사람)
        lp = video.layout_prompt(s, 4, sl)
        assert ("3 faceless mannequin figures, sitting, side by side, every head in three-quarter view toward the camera and the middle figure "
                "looking at the camera: on the left a adult-sized figure turned toward the right; in the middle a adult-sized figure facing "
                "the camera; on the right a child-sized figure turned toward the left.") in lp
        assert "All three heads and upper bodies are fully in frame" in lp and "clear empty space above the hair" in lp
        assert "Niki beams" not in lp and lp.endswith("Exactly 3 figures, no other figure or person.")
        # ② 앞의 두 사람(듣는 사람)만 입힐 때: 그 둘의 입을 다문 미소만 쓰고, 아직 얼굴 없는 오른쪽 형상은 '사람'이라 부르지 않는다
        ip = video.identity_prompt(s, 4, sl, ["A", "B"], final=False)
        assert "The person on the left listens with lips closed and a gentle closed-mouth smile, looking toward the right side of the frame." in ip
        assert "The person in the middle listens with lips closed and a gentle closed-mouth smile" in ip
        assert "speaks" not in ip and "Keep the faceless figure on the right exactly as it is." in ip and "Keep all three heads" in ip
        # ③ 세 번째 사람(말하는 니키)
        fp = video.finish_prompt(s, 4, sl, "C", ["A", "B"], final=True)
        assert "The person on the right speaks toward the left side of the frame with a happy expression" in fp and "Exactly 3 people" in fp
        # 영상 글: 세 사람을 자리·외형으로, 말하는 사람은 하나, 나머지는 입을 다문 미소로 듣는다. 장면 글(action)은 넣지 않는다
        dp = video.duo_talk_prompt(s, 4, sl)
        assert dp.startswith("Medium group shot of three people in a conversation, all fully in frame the whole time.")
        assert "On the left: a calm neat woman" in dp and "In the middle: a tall lean man" in dp and "On the right: a twelve-year-old girl" in dp
        assert "The person on the right speaks to the person on the left, happy: the lips and jaw move clearly with the words." in dp
        assert ("The people on the left and in the middle only listen with lips closed and a gentle closed-mouth smile: their mouths stay "
                "closed") in dp
        assert "Niki beams" not in dp and "The camera stays completely still." in dp and len(dp) <= 3000
        # 모두에게 하는 말: 가운데 제임스가 모두에게 (카메라 쪽으로), 양쪽은 입을 다문다
        s2 = video.layout_slots(s, 2, video.scene_people(s, 2))
        assert "The person in the middle speaks to the others, cheerful" in video.duo_talk_prompt(s, 2, s2)
        assert video._act_sentence(s, 2, s2) == (
            "The person in the middle speaks to the others with a cheerful expression, the face turned toward the camera, lips slightly "
            "parted. The others, on the left and on the right, listen with lips closed and a gentle closed-mouth smile, looking at the speaker.")
        # 구도 그림 없이 바로 그릴 때·앞 프레임을 고칠 때도 같은 자리 차례
        cp = video.compose_prompt(s, 4, ["C", "A", "B"])
        assert ("The three people are side by side: the person from image 2 on the left, the person from image 3 in the middle, "
                "the person from image 1 on the right") in cp
        ce = video.cut_edit_prompt(s, 1, ["A", "B"], "cut")
        assert "The three people are side by side: the person a calm neat woman around forty" in ce
        assert "listen with lips closed and a gentle closed-mouth smile" in ce and "Exactly 3 people" in ce


def _variant_story():
    """인물 라이브러리의 베스·제임스(안경 쓴 제임스는 같은 사람의 다른 모습)·니키의 아침 식탁 (11-24 2차 검토의 시나리오):
    넓은 장면 [베스, 제임스] → 신문을 읽는 제임스 클로즈업 [안경 쓴 제임스] → 베스가 안경 쓴 제임스에게 → 같은 화면에서 제임스가 답한다"""
    return {
        "title_ko": "아침 식탁", "logline_ko": "베스와 제임스의 아침.",
        "bible": {"characters": [
            {"id": "beth", "name_ko": "베스", "look": "a calm neat woman around forty", "voice": "A warm Korean woman.", "voice_ko": "엄마"},
            {"id": "james", "name_ko": "제임스", "look": "a tall lean man in his early forties", "voice": "A calm Korean man.", "voice_ko": "아빠"},
            {"id": "james_glasses", "name_ko": "제임스 그린(안경)", "look": "a tall lean man with round glasses", "voice": "A calm Korean man.",
             "voice_ko": "아빠"},
            {"id": "niki", "name_ko": "니키", "look": "a twelve-year-old girl", "voice": "A bright Korean girl.", "voice_ko": "아이"}],
            "places": [{"id": "P1", "look": "a sunny cottage kitchen with a wooden breakfast table"}],
            "time": "morning", "light": "soft warm window light", "style": "semi-realistic painterly 2.5D film look"},
        "music": {"tags": "gentle instrumental, piano", "bpm": 72, "key": "D major", "ko": "잔잔한 피아노"},
        "scenes": [
            {"seconds": 12, "place": "P1", "characters": ["beth", "james"], "continues": False, "shot": "wide shot",
             "action": "Beth and James sit at the breakfast table.", "sound": "birds", "ko": "아침 식탁", "kind": "shot", "speaker": "",
             "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 8, "place": "P1", "characters": ["james_glasses"], "continues": False, "shot": "close-up",
             "action": "James reads the newspaper through his glasses.", "sound": "paper rustling", "ko": "신문을 읽는 제임스", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 6, "place": "P1", "characters": ["beth", "james_glasses"], "continues": False, "shot": "medium two-shot",
             "action": "Beth smiles at James.", "sound": "room tone", "ko": "베스가 말한다", "kind": "talk", "speaker": "beth",
             "listener": "james_glasses", "line_ko": "여보, 커피 더 줄까요?", "emotion": "warm"},
            {"seconds": 6, "place": "P1", "characters": ["james_glasses", "beth"], "continues": True, "shot": "medium two-shot",
             "action": "James looks up and nods.", "sound": "room tone", "ko": "제임스가 답한다", "kind": "talk", "speaker": "james_glasses",
             "listener": "beth", "line_ko": "고마워요, 여보.", "emotion": "calm"}]}


def test_g1_same_person_in_two_looks_is_one_person():
    """11-24 2차 검토(높음): 자리 이어 받기가 라이브러리의 같은 사람 다른 모습(제임스 ↔ 안경 쓴 제임스)을 다른 사람으로 보고 대화 컷에 더해
    한 화면에 제임스가 둘이었다. 이어 받기·모두에게 하는 말 검사·세 명 자르기·자리 차례는 같은 사람으로 세고, 그 장면이 적은 모습을 남긴다.
    검사는 한 장면에 두 모습을 함께 둔 구성안을 고쳐 쓰게 한다"""
    import copy as _copy
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    assert {"beth", "james", "james_glasses", "niki"} <= {e["id"] for e in known}
    with duo_setting(True):
        raw = _variant_story()
        cast = video._cast_view({c["id"]: c for c in raw["bible"]["characters"]}, known)
        assert cast["person"]["james"] == cast["person"]["james_glasses"] != cast["person"]["beth"]
        assert video._presence(raw["scenes"], 1, cast) == ["james_glasses", "beth"]       # 가장 나중의 모습 하나만
        assert video.dialogue_problems(raw, 32, True, exchange=True, known=known) == []
        s = video.clean_dialogue_story(raw, 32, exchange=True, known=known)
        assert s["scenes"][2]["characters"] == ["beth", "james_glasses"] and s["scenes"][2]["shot"] == "medium two-shot"
        assert s["scenes"][3]["characters"] == ["james_glasses", "beth"] and s["scenes"][3]["continues"] is True
        assert video.talk_pair(s, 2) == ["beth", "james_glasses"]
        # 모두에게 하는 말도 같은 사람을 더하라고 하지 않는다
        to_all = _copy.deepcopy(raw)
        to_all["scenes"][2]["listener"] = "everyone"
        assert not any("speaks to everyone" in x for x in video.dialogue_problems(to_all, 32, True, exchange=True, known=known))
        assert video.clean_dialogue_story(to_all, 32, exchange=True, known=known)["scenes"][2]["characters"] == ["beth", "james_glasses"]
        swap = _copy.deepcopy(to_all)                    # 앞 컷은 안경 안 쓴 모습, 이 컷은 안경 쓴 모습: 그래도 같은 사람이다
        swap["scenes"][1].update(characters=["james"], action="James reads the newspaper.")
        assert not any("speaks to everyone" in x for x in video.dialogue_problems(swap, 32, True, exchange=True, known=known))
        assert video.clean_dialogue_story(swap, 32, exchange=True, known=known)["scenes"][2]["characters"] == ["beth", "james_glasses"]
        # 한 장면에 두 모습을 함께 적으면 위반, 정리는 앞에 적힌 모습 하나만 남긴다
        twin = _copy.deepcopy(raw)
        twin["scenes"][2]["characters"] = ["beth", "james_glasses", "james"]
        left = video.dialogue_problems(twin, 32, True, exchange=True, known=known)
        assert any('"james_glasses" and "james" are the same person in two looks' in x for x in left), left
        assert video.clean_dialogue_story(twin, 32, exchange=True, known=known)["scenes"][2]["characters"] == ["beth", "james_glasses"]
        # 라이브러리를 모르면(known 없음) 예전처럼 다른 사람으로 센다
        assert video.clean_dialogue_story(raw, 32, exchange=True)["scenes"][2]["characters"] == ["beth", "james_glasses", "james"]
        # 넓은 장면 [베스, 제임스, 니키] 다음에 니키 → 안경 쓴 제임스 대화: 이어 받기는 셋(제임스는 한 번), 자리는 넓은 장면 차례 그대로
        # (베스 왼쪽, 니키 오른쪽 — 모습이 다르면 넓은 장면을 못 찾아 좌우가 뒤집혔다)
        mirror = _copy.deepcopy(raw)
        mirror["scenes"][0]["characters"] = ["beth", "james", "niki"]
        mirror["scenes"][2].update(characters=["niki", "james_glasses"], speaker="niki", listener="james_glasses", line_ko="아빠, 뭐 읽어요?",
                                   action="Niki leans toward her father.", emotion="curious")
        mirror["scenes"][3].update(characters=["james_glasses", "niki"], listener="niki")
        sm = video.clean_dialogue_story(mirror, 32, exchange=True, known=known)
        assert sm["scenes"][2]["characters"] == ["niki", "james_glasses", "beth"]
        cast_lib.apply_to_story(sm, known, "베스와 제임스, 니키의 아침 식탁")
        chars = {c["id"]: c for c in sm["bible"]["characters"]}
        assert chars["james_glasses"]["variant_of"] == "james" and "variant_of" not in chars["james"]
        assert video.group_order(sm, 2, ["niki", "james_glasses", "beth"]) == ["beth", "james_glasses", "niki"]
        slots = video.layout_slots(sm, 2, video.scene_people(sm, 2))
        assert [x["id"] for x in slots] == ["beth", "james_glasses", "niki"] and slots[0]["pos"] == "on the left"


def test_g1_people_who_left_the_place_are_not_carried():
    """11-24 2차 검토: 앞 장면에서 그 장소를 떠난 사람(학교에 가려고 부엌문으로 뛰어나간 니키)은 다음 대화 컷에 이어 받지 않는다.
    다시 나오면 다시 이어 받는다. 모두에게 하는 말의 검사도 같은 기준이다"""
    import copy as _copy
    with duo_setting(True):
        f = _copy.deepcopy(FAMILY)
        f["scenes"][3] = {"seconds": 5, "place": "P1", "characters": ["C"], "continues": False, "shot": "medium shot",
                          "action": "Niki grabs her school bag and runs out of the kitchen door.", "sound": "a door closes",
                          "ko": "니키가 학교에 간다", "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""}
        f["scenes"][4].update(characters=["A", "B"], speaker="A", listener="B", line_ko="우리 딸 다 컸네요.", emotion="warm",
                              action="Beth smiles at James.")
        s = video.clean_dialogue_story(f, 60, exchange=True)
        assert s["scenes"][4]["characters"] == ["A", "B"] and s["scenes"][4]["shot"] == "medium two-shot" and video.talk_pair(s, 4) == ["A", "B"]
        assert video.dialogue_problems(f, 60, True, exchange=True) == []
        e = _copy.deepcopy(f)
        e["scenes"][4]["listener"] = "everyone"
        assert not any("speaks to everyone" in x for x in video.dialogue_problems(e, 60, True, exchange=True))
        assert video.clean_dialogue_story(e, 60, exchange=True)["scenes"][4]["characters"] == ["A", "B"]
        # 여럿이 나오는 장면에서는 떠나는 말에 가까운 이름이 떠난다 (한국어 장면 글): 니키만 떠나고 부모는 남는다
        g = _copy.deepcopy(f)
        g["scenes"][3].update(characters=["A", "B", "C"], shot="wide shot", action="The parents wave goodbye at the kitchen door.",
                              ko="베스와 제임스가 손을 흔들고 니키가 집을 나선다")
        g["scenes"][4]["characters"] = ["A"]
        assert video.clean_dialogue_story(g, 60, exchange=True)["scenes"][4]["characters"] == ["A", "B"]
        # 이어진 이름은 함께 떠난다: 부모가 정원으로 나가고 니키만 남아 혼잣말을 하다가 … (부모가 다시 나오지 않으면 이어 받지 않는다)
        cast = video._cast_view({c["id"]: c for c in FAMILY["bible"]["characters"]})
        both = {"characters": ["A", "B", "C"], "action": "Beth and James stand up from the table.", "shot": "wide shot",
                "ko": "베스와 제임스가 정원으로 나간다"}
        assert video._leavers(both, ["A", "B", "C"], cast) == {"A", "B"}
        assert video._leavers(dict(both, action="They step out into the garden."), ["A", "B", "C"], cast) == {"A", "B", "C"}
        assert video._leavers(dict(both, action="They watch as Niki runs out of the kitchen door.", ko="배웅"), ["A", "B", "C"],
                              video._cast_view({c["id"]: dict(c, library={"A": "beth", "B": "james", "C": "niki"}[c["id"]])
                                                for c in FAMILY["bible"]["characters"]})) == {"C"}
        assert video._leavers({"action": "Leaves fall from the apple tree.", "shot": "wide shot", "ko": "낙엽이 진다"}, ["A", "B"], cast) == set()
        assert video._leavers({"action": "She walks away from the window.", "shot": "medium shot", "ko": "창가"}, ["C"], cast) == {"C"}
        # 다시 나오면 다시 이어 받는다: 뛰어나간 니키가 돌아오는 장면 뒤의 대화 컷에는 니키도 나온다
        back = _copy.deepcopy(f)
        back["scenes"].insert(4, {"seconds": 5, "place": "P1", "characters": ["C"], "continues": False, "shot": "medium shot",
                                  "action": "Niki rushes back in for her forgotten hat.", "sound": "footsteps", "ko": "니키가 돌아온다",
                                  "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""})
        assert video.clean_dialogue_story(back, 65, exchange=True)["scenes"][5]["characters"] == ["A", "B", "C"]
        # 같은 장소의 세 사람 대화 컷에 함께 나온 두 사람은 그 자리의 좌우를 따른다 (넓은 장면이 제임스·베스·니키 차례):
        # 니키가 떠난 뒤의 베스 → 제임스 두 사람 컷도 제임스 왼쪽·베스 오른쪽, 말하는 베스는 왼쪽(제임스 쪽)을 보며 말한다
        w = _copy.deepcopy(f)
        w["scenes"][0]["characters"] = ["B", "A", "C"]
        sw = video.clean_dialogue_story(w, 60, exchange=True)
        assert [x["id"] for x in video.layout_slots(sw, 1, video.scene_people(sw, 1))] == ["B", "A", "C"]
        assert sw["scenes"][4]["characters"] == ["A", "B"]
        pair = video.layout_slots(sw, 4, video.scene_people(sw, 4))
        assert [x["id"] for x in pair] == ["B", "A"] and [x["facing"] for x in pair] == ["turned toward the right", "turned toward the left"]
        assert video._act_sentence(sw, 4, pair).startswith("The person on the right speaks toward the left side of the frame")
        assert "The person from image 1 is on the right and speaks toward the left side" in video.compose_prompt(sw, 4, ["A", "B"])
        assert "is on the right, turned toward the left side of the frame, speaking" in video.cut_edit_prompt(sw, 4, ["A", "B"], "cut")


def test_g2_one_and_two_person_shots_follow_the_group_seats():
    """11-24 2차 검토: 세 사람 자리(왼쪽 turned right, 가운데, 오른쪽 turned left)가 같은 자리의 한 사람 컷(독백 클로즈업·듣는 얼굴)으로
    이어진다. 오른쪽 자리의 니키는 그룹샷에서 왼쪽을 보므로 그 사이 독백 클로즈업도 왼쪽을 본다 (말한 차례로는 오른쪽이라 시선이 튀었다).
    가운데 사람은 말하는 사람이면 듣는 사람 쪽, 듣는 사람이면 말하는 사람 쪽"""
    with duo_setting(True):
        s = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        assert video.speaker_side(s, "C") == "right"                     # 말한 차례로는 오른쪽
        assert video.facing_of(s, 3, "C") == "left" and "toward the left edge" in video.talk_prompt(s, 3)
        assert video.layout_slots(s, 3, ["C"])[0]["facing"] == "turned slightly toward the left"
        assert video.facing_of(s, 3, "A") == "right"                     # 왼쪽 자리의 베스
        # 가운데 제임스: 베스(왼쪽)의 말을 들을 때는 왼쪽, 모두에게 말할 때는 정하지 않고, 니키(오른쪽)의 말을 들을 때는 오른쪽
        assert [video._seat_facing(s, k, "B") for k in (1, 2, 4)] == ["left", None, "right"]
        assert video.facing_of(s, 3, "B") == "left"                       # 같은 식탁에서 가장 가까운 앞 컷(장면 2)은 정하지 않아 장면 1
        # 다른 장소(다락방)의 베스 독백은 다락방에 대화 컷이 없어 다른 곳의 가장 가까운 대화 컷(식탁 왼쪽 자리)을 따른다
        assert video.facing_of(s, 6, "A") == "right"
    with duo_setting(False):                                              # 꺼짐: 1차 그대로 말한 차례
        off = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        assert all(video.facing_of(off, k, c) == video.speaker_side(off, c) for k in range(7) for c in ("A", "B", "C"))


def test_g2_off_mode_and_films_without_dialogue_keep_the_written_order():
    """G0-9 (11-24 2차 검토): 자리 차례(group_order)는 두세 사람 규칙으로 다듬은 대사 영상에만 쓴다. 꺼짐(STUDIO_DUO_TALK=false)과
    대사 없는 영상의 세 사람 장면은 1차처럼 적힌 차례대로 둔다"""
    import copy as _copy
    with duo_setting(True):
        on = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        assert on.get("duo_rules") is True
        assert [x["id"] for x in video.layout_slots(on, 0, ["B", "C", "A"])] == ["A", "B", "C"]
    with duo_setting(False):
        off = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        assert "duo_rules" not in off
        assert [x["id"] for x in video.layout_slots(off, 0, ["B", "C", "A"])] == ["B", "C", "A"]
    silent = _copy.deepcopy(FAMILY)
    for sc in silent["scenes"]:
        for k in ("kind", "speaker", "listener", "line_ko", "emotion"):
            sc.pop(k, None)
    plain = video.clean_story(silent, 60)
    assert not plain.get("dialogue") and [x["id"] for x in video.layout_slots(plain, 0, ["B", "C", "A"])] == ["B", "C", "A"]


def test_g0_monologue_prompt_never_shows_anyone_else():
    """11-24 2차 검토: 같은 식탁에 다른 사람이 있는 독백(혼자 클로즈업)의 장면 글이 그 사람을 가리키면 영상 글에서 그 문장을 빼고
    (화면 밖 사람을 그려 넣는다, 11-21 R3), 검사는 고쳐 쓰게 하고, 구성안 규칙 글도 독백의 장면 글은 말하는 사람만 쓰라고 한다"""
    import copy as _copy
    assert "Its \"action\" describes only the speaker and never mentions the other people" in video.DIALOGUE_EXTRA_DUO
    with duo_setting(True):
        m = _copy.deepcopy(FAMILY)
        m["scenes"][3]["action"] = "Niki glances at her mother and father across the table and whispers to herself."
        left = video.dialogue_problems(m, 60, True, exchange=True)
        assert any('Scene 4 is a monologue: its "action" must describe only the speaker "C"' in x and '("mother")' in x for x in left), left
        s = video.clean_dialogue_story(m, 60, exchange=True)
        tp = video.talk_prompt(s, 3)
        assert "close-up of one person alone in the frame" in tp and "mother" not in tp and "father" not in tp
        assert "Niki glances" not in video.cut_edit_prompt(s, 3, ["C"], "cut")
        # 말하는 사람만 쓴 문장은 남는다
        s["scenes"][3]["action"] = "Niki looks down at the table, thinking. She glances at her mother and father."
        tp = video.talk_prompt(s, 3)
        assert "Niki looks down at the table, thinking." in tp and "mother" not in tp
        # 다른 성별의 대명사는 다른 사람이다 (제 몸을 가리키는 her·his 는 아니다)
        cast = video._story_cast(s)
        assert video._other_mention("She whispers to herself, her eyes lowered.", "C", cast) == ""
        assert video._other_mention("She smiles at him.", "A", cast) == "him"
        assert video._other_mention("He glances at her across the table.", "B", cast) == "her"
        assert video._other_mention("Beth thinks of Niki.", "A", video._cast_view(
            {"A": {"look": "a woman", "name_ko": "베스"}, "C": {"id": "niki", "look": "a girl", "name_ko": "니키", "library": "niki"}})) == "Niki"
        # 한글 이름이 역할 말('엄마')인 사람의 독백에서 'Mom' 은 그 사람 자신이다 (다른 사람의 독백에서는 다른 사람)
        roles = video._cast_view({"M": {"look": "a kind woman in an apron", "name_ko": "엄마"}, "K": {"look": "a small boy", "name_ko": "아이"}})
        assert video._other_mention("Mom smiles softly to herself.", "M", roles) == ""
        assert video._other_mention("He thinks of his mom.", "K", roles) == "mom"
        # 대화 컷은 그대로 (장면 글을 넣지 않는다), 꺼짐의 혼자 대사 컷도 1차 그대로 장면 글을 쓴다
    with duo_setting(False):
        off = video.clean_dialogue_story(m, 60, exchange=True)
        assert "Niki glances at her mother and father" in video.talk_prompt(off, 3)


def test_g3_edge_seat_speaking_to_everyone_turns_inward():
    """세 사람 컷의 가장자리 자리 화자가 모두에게 말하면 안쪽(나머지 사람들 쪽)으로 말한다. 가운데 화자는 카메라 쪽 (11-24 2차 검토:
    이 갈래를 고정하는 시험이 없었다)"""
    import copy as _copy
    with duo_setting(True):
        f = _copy.deepcopy(FAMILY)
        f["scenes"][2].update(speaker="A", characters=["A", "B", "C"], listener="everyone", line_ko="자, 다 같이 축하해요!")
        s = video.clean_dialogue_story(f, 60, exchange=True)
        slots = video.layout_slots(s, 2, video.scene_people(s, 2))
        assert [x["id"] for x in slots] == ["A", "B", "C"] and s["scenes"][2]["to_all"] is True
        assert video._act_sentence(s, 2, slots).startswith("The person on the left speaks toward the right side of the frame with a cheerful expression")
        g = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        g2 = video.layout_slots(g, 2, video.scene_people(g, 2))
        assert video._act_sentence(g, 2, g2).startswith("The person in the middle speaks to the others with a cheerful expression")


def test_g0_old_saved_plans_still_render():
    """listener·monologue·to_all 칸이 없는 예전 구성안(1차 전·1차)도 깨지지 않는다: 사람 수로 대화 컷·혼자 컷을 가린다"""
    with duo_setting(True):
        old0 = video.clean_dialogue_story(DLG_OK, 48, duo=False)          # 1차 전 모양: talk 은 모두 혼자, listener 칸 없음
        for sc in old0["scenes"]:
            sc.pop("listener", None)
        assert not any(video.talk_group(old0, k) or video.duo_talk(old0, k) for k in range(6))
        assert "alone in the frame" in video.talk_prompt(old0, 2) and video.layout_slots(old0, 2, ["A"])[0]["pos"] == "in the centre"
        # 1차 모양: 두 사람 대사 컷은 [말하는 사람, 듣는 사람] + listener, 혼자 컷은 [말하는 사람] + listener "" (monologue·to_all 없음)
        old1 = video.clean_dialogue_story(_duo_pairs(), 48)
        old1.pop("duo_rules", None)                                       # 1차 이야기에는 2차의 표시가 없다
        for sc in old1["scenes"]:
            sc.pop("monologue", None)
            sc.pop("to_all", None)
        assert [video.talk_pair(old1, k) for k in range(6)] == [None, ["A", "B"], ["B", "A"], ["A", "C"], None, None]
        sl = video.layout_slots(old1, 2, video.scene_people(old1, 2))
        assert "The person on the right speaks to the person on the left" in video.duo_talk_prompt(old1, 2, sl)
        assert "The person on the left listens with lips closed" in video.identity_prompt(old1, 2, sl, ["B", "A"], final=True)
        old1["scenes"][3].update(characters=["A"], listener="")             # 1차의 혼자 대사 컷
        assert video.talk_group(old1, 3) is None and "alone in the frame" in video.talk_prompt(old1, 3)
        # 예전 구성안 글(listener 칸이 없음)을 다시 다듬어도 된다: 빈 칸은 추정한다 (대화 요청이 아니면 독백)
        again = video.clean_dialogue_story(DLG_OK, 48)
        assert [x["characters"] for x in again["scenes"] if x["kind"] == "talk"] == [["A"], ["B"]]


def test_g0_write_dialogue_story_repairs_a_missing_listener():
    """두세 사람 규칙: 대화 요청에서 상대를 화면에 두지 않은 대사(듣는 사람 칸이 빈 혼자 컷)는 고쳐 쓰게 하고, 끝내 고치지 않으면
    정리가 앞뒤 대사로 듣는 사람을 넣는다"""
    with duo_setting(True):
        call = Call([_dlg_good(), _dlg_duo_good()])
        story, left, rounds = asyncio.run(video.write_dialogue_story(call, "니키와 푸토르가 숲속을 걸어가며 대화하는 영상", "", 48))
        assert rounds == 2 and left == [], left
        assert call.calls[0]["format"] is video.DIALOGUE_SCHEMA
        assert 'the person spoken to must be in the shot: put "B"' in call.calls[1]["messages"][-1]["content"]
        assert story["scenes"][2]["characters"] == ["A", "B"] and story["scenes"][3]["characters"] == ["B", "A"]
        call = Call([_dlg_good()] * 3)
        story, left, rounds = asyncio.run(video.write_dialogue_story(call, "니키와 푸토르가 숲속을 걸어가며 대화하는 영상", "", 48))
        assert rounds == 3 and left
        assert story["scenes"][2]["characters"] == ["A", "B"] and story["scenes"][2]["listener"] == "B"
        assert story["scenes"][3]["characters"] == ["B", "A"] and story["scenes"][3]["listener"] == "A"
        assert story["scenes"][2]["shot"] == "medium two-shot"


# ── 2차 3차 검토 (11-25): 그 자리에 있는 사람·자리 지도·떠남 판정·독백 글의 다른 사람·화풍 요청 ──
def _r3_story(chars, scenes, style="semi-realistic painterly 2.5D film look"):
    import copy as _copy
    return {"title_ko": "검증", "logline_ko": "검증용 이야기.",
            "bible": {"characters": _copy.deepcopy(chars), "places": _copy.deepcopy(FAMILY["bible"]["places"]), "time": "morning, clear",
                      "light": "soft warm window light", "style": style},
            "music": {"tags": "gentle cinematic instrumental", "bpm": 72, "key": "D major", "ko": "잔잔한 음악"}, "scenes": scenes}


def _r3_shot(place, chars, shot, action, ko="장면", seconds=8, speaker="", line="", emotion=""):
    return {"seconds": seconds, "place": place, "characters": list(chars), "continues": False, "shot": shot, "action": action,
            "sound": "soft room tone", "ko": ko, "kind": "shot", "speaker": speaker, "listener": "", "line_ko": line, "emotion": emotion}


def _r3_talk(place, chars, speaker, listener, line, shot="medium two-shot", action="They talk.", cont=False, seconds=6):
    return {"seconds": seconds, "place": place, "characters": list(chars), "continues": cont, "shot": shot, "action": action,
            "sound": "soft room tone", "ko": "대사", "kind": "talk", "speaker": speaker, "listener": listener, "line_ko": line, "emotion": "warm"}


_R3_FAM = FAMILY["bible"]["characters"]
_R3_WIDE = _r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the breakfast table.", "가족의 아침 식탁", 12)


def _r3_clean(scenes, chars=None, exchange=True, known=None):
    data = _r3_story(chars or _R3_FAM, scenes)
    secs = sum(s["seconds"] for s in scenes)
    with duo_setting(True):
        return video.clean_dialogue_story(data, secs, exchange=exchange, known=known), video.dialogue_problems(data, secs, True, exchange=exchange,
                                                                                                                 known=known)


def test_r3_a_line_to_someone_not_in_the_place_is_a_monologue():
    """G0 '그 자리에 있는 사람에게 하는 말' (3차 검토 1): 문 밖의 목소리·방금 뛰어나간 사람에게 하는 말은 그 자리의 누구에게도 하는 말이 아니라
    독백(혼자 클로즈업)이다. 그 사람을 대화 컷에 넣지 않고, 검사는 그 사람을 넣으라고 하지 않고 'nobody' 로 고쳐 쓰게 한다"""
    # 다락방의 니키 → 문 밖에서 부르는 엄마(화면 밖 목소리) → 니키가 문 쪽에 대답 (listener 는 엄마)
    s, left = _r3_clean([_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed by the round window.", "다락방의 니키"),
                         _r3_shot("P3", [], "close-up of the attic door", "The attic door stays still.", "닫힌 문", 5, speaker="A",
                                  line="니키야, 밥 먹어라!", emotion="calling warmly"),
                         _r3_talk("P3", ["C"], "C", "A", "엄마, 조금만요!", shot="close-up", action="Niki calls toward the door.")],
                        chars=[_R3_FAM[0], _R3_FAM[2]])
    assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True and s["scenes"][2]["listener"] == ""
    assert any('Scene 3: "A" is not in the place at this moment' in x and 'write "nobody"' in x for x in left), left
    assert not any('put "A" in "characters"' in x for x in left), left
    # 목소리가 뒤에 와도 같다 (엄마는 끝내 화면에 나오지 않는다)
    s, _l = _r3_clean([_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed.", "다락방의 니키"),
                       _r3_talk("P3", ["C"], "C", "A", "엄마!", shot="close-up", action="Niki calls toward the door."),
                       _r3_shot("P3", [], "close-up of the attic door", "The door stays shut.", "닫힌 문", 5, speaker="A", line="왜 그러니?",
                                emotion="calling")], chars=[_R3_FAM[0], _R3_FAM[2]])
    assert s["scenes"][1]["characters"] == ["C"] and s["scenes"][1]["monologue"] is True
    # 방금 부엌문으로 뛰어나간 니키를 부르는 엄마: 엄마 혼자 클로즈업, 다음 대화 컷에도 니키는 없다
    s, left = _r3_clean([_R3_WIDE, _r3_shot("P1", ["C"], "medium shot", "Niki grabs her bag and runs out of the kitchen door.", "니키가 나간다", 6),
                         _r3_talk("P1", ["A", "B"], "A", "C", "니키야, 도시락 가져가!", action="Beth calls toward the door."),
                         _r3_talk("P1", ["B", "A"], "B", "A", "벌써 갔어요.", action="James chuckles.")])
    assert s["scenes"][2]["characters"] == ["A"] and s["scenes"][2]["monologue"] is True and s["scenes"][2]["shot"] == "medium close-up"
    assert any('Scene 3: "C" is not in the place at this moment' in x for x in left), left
    assert s["scenes"][3]["characters"] == ["B", "A"]
    # 빈 listener 추정(b)은 떠난 사람에서 멈춘다: 앞 대사의 니키는 떠났으니 듣는 사람이 아니고, 뒤에서 대답하는 제임스가 듣는 사람
    s, _l = _r3_clean([_R3_WIDE, _r3_talk("P1", ["C", "A", "B"], "C", "everyone", "다녀오겠습니다!", shot="medium group shot"),
                       _r3_shot("P1", ["C"], "medium shot", "Niki grabs her school bag and runs out of the kitchen door.", "니키가 학교에 간다", 6),
                       _r3_talk("P1", ["A"], "A", "", "벌써 다 컸네...", shot="close-up", action="Beth watches the door, smiling."),
                       _r3_talk("P1", ["B", "A"], "B", "A", "그러게 말이야.", action="James smiles at Beth.")])
    assert s["scenes"][3]["characters"] == ["A", "B"] and s["scenes"][3]["listener"] == "B"
    assert s["scenes"][4]["characters"] == ["B", "A"]
    raw = _r3_story(_R3_FAM, [_R3_WIDE, _r3_talk("P1", ["C", "A", "B"], "C", "everyone", "다녀오겠습니다!", shot="medium group shot"),
                              _r3_shot("P1", ["C"], "medium shot", "Niki runs out of the kitchen door.", "니키가 나간다", 6),
                              _r3_talk("P1", ["A"], "A", "", "벌써 다 컸네...", shot="close-up")])
    cast = video._cast_view({c["id"]: c for c in _R3_FAM}, scenes=raw["scenes"])
    assert video._line_partner(raw["scenes"], 3, "A", FAMILY_IDS, -1, cast) == "" and video._line_partner(raw["scenes"], 3, "A", FAMILY_IDS, -1) == "C"
    assert video.line_target(raw["scenes"], 3, FAMILY_IDS, exchange=True, cast=cast) == ("mono", "")
    # 새 장소의 첫 대사에서 상대를 characters 에 빠뜨렸고 그 사람이 바로 대답하면(들어오는 장면이 아니면) 이미 그 자리에 있다
    s, left = _r3_clean([_r3_talk("P2", ["A"], "A", "C", "니키야, 사과가 익었네.", shot="close-up"),
                         _r3_talk("P2", ["C", "A"], "C", "A", "정말요?", shot="medium two-shot")])
    assert s["scenes"][0]["characters"] == ["A", "C"] and any('Scene 1: the person spoken to must be in the shot: put "C"' in x for x in left)
    # 그 사람이 뒤에서 들어오면 말하는 순간에는 그 자리에 없다: 부르는 말은 독백
    s, _l = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 10),
                       _r3_talk("P1", ["A", "B"], "A", "C", "니키야, 일어나렴!", shot="medium two-shot", action="Beth calls upstairs."),
                       _r3_shot("P1", ["C"], "medium shot", "Niki walks into the kitchen, rubbing her eyes.", "니키가 들어온다", 6),
                       _r3_talk("P1", ["C", "A", "B"], "C", "everyone", "좋은 아침!", shot="medium group shot")])
    assert s["scenes"][1]["characters"] == ["A"] and s["scenes"][1]["monologue"] is True
    assert s["scenes"][3]["characters"] == ["C", "A", "B"] and s["scenes"][3]["to_all"] is True


def test_r3_people_stay_in_the_place_until_they_leave():
    """G1 자리 사람 이어 받기 (3차 검토 2): 같은 장소 구간을 장소가 바뀔 때까지 거슬러 본다. 한 사람 미디엄 컷·말 없는 두 사람 컷이 끼어도 그 자리에
    있던 사람은 대화 컷에서 빠지지 않고, 모두에게 하는 말은 그 자리 모두의 그룹샷이다 (검사도 같은 기준). 떠난 사람만 빠지고, 돌아오면 다시 나온다"""
    # 들어온 니키가 모두에게 인사 (구성안은 니키 혼자 미디엄 샷): 식탁의 부모도 나오는 그룹샷, 검사는 부모를 더하라고 한다
    s, left = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 10),
                         _r3_shot("P1", ["C"], "medium shot", "Niki walks into the kitchen, rubbing her eyes.", "니키가 들어온다", 6),
                         _r3_talk("P1", ["C"], "C", "everyone", "좋은 아침!", shot="medium shot", action="Niki waves sleepily."),
                         _r3_talk("P1", ["A", "C"], "A", "C", "잘 잤니?", action="Beth smiles at Niki.")])
    assert s["scenes"][2]["characters"] == ["C", "A", "B"] and s["scenes"][2]["to_all"] is True and video.shot_rank(s["scenes"][2]["shot"]) == 2
    assert ('Scene 3: "C" speaks to everyone in the place, so everyone there must be in the shot: add "A", "B" to "characters" '
            '(at most three people).') in left, left
    assert s["scenes"][3]["characters"] == ["A", "C", "B"]
    # 넓은 장면 다음의 한 사람 미디엄 컷·말 없는 두 사람 컷 뒤의 대화 컷에도 식탁의 셋 (끼인 컷이 없을 때와 같다)
    for mid in (_r3_shot("P1", ["A"], "medium shot", "Beth pours tea into the cups.", "차를 따르는 베스", 6),
                _r3_shot("P1", ["A", "B"], "medium two-shot", "Beth and James share a quiet look over their cups.", "부모의 눈빛", 6)):
        s, _l = _r3_clean([_R3_WIDE, mid, _r3_talk("P1", ["A", "B"], "A", "B", "니키가 오늘 좀 조용하네요.")])
        assert s["scenes"][2]["characters"] == ["A", "B", "C"] and s["scenes"][2]["shot"] == "medium group shot", mid["action"]
    # 나갔다가 돌아온 니키가 모두에게: 셋 모두 (2차 검토의 떠남 고침은 그대로)
    s, _l = _r3_clean([_R3_WIDE, _r3_shot("P1", ["C"], "medium shot", "Niki runs out of the kitchen door.", "니키가 나간다", 6),
                       _r3_shot("P1", ["C"], "medium shot", "Niki rushes back in for her forgotten hat.", "니키가 돌아온다", 6),
                       _r3_talk("P1", ["C"], "C", "everyone", "모자를 깜빡했어요!", shot="medium shot")])
    assert s["scenes"][3]["characters"] == ["C", "A", "B"] and s["scenes"][3]["to_all"] is True
    # 넓은 장면 → 니키가 방을 빠져나가는 클로즈업 → 베스가 제임스에게: 떠난 니키는 이어 받지 않는다 (2차 검토 변이: gone 확인을 지워도 통과했다)
    s, _l = _r3_clean([_R3_WIDE, _r3_shot("P1", ["C"], "close-up", "Niki quietly slips out of the room.", "니키가 조용히 방을 빠져나간다", 5),
                       _r3_talk("P1", ["A", "B"], "A", "B", "쟤 어디 가요?", action="Beth looks at James.")])
    assert s["scenes"][2]["characters"] == ["A", "B"]
    cast = video._cast_view({c["id"]: c for c in _R3_FAM})
    scenes = [_R3_WIDE, _r3_shot("P1", ["A"], "medium shot", "Beth pours tea.", "차", 6), _r3_shot("P2", ["C"], "wide shot", "Niki in the garden.", "정원", 8)]
    assert video._presence(scenes, 1, cast) == ["A", "B", "C"] and video._presence(scenes, 2, cast) == ["C"]     # 장소가 바뀌면 거기서 멈춘다


def test_r3_leaving_is_read_only_from_real_departures():
    """떠남 판정 (3차 검토 3·17): 그 자리에 그대로 있는 사람을 떠난 사람으로 보지 않는다 (알람·부엌 안의 움직임·의자에서 내려옴·우유가 떨어짐·
    문밖을 봄·바람·계획·전하는 말·'지나가다'·'앞으로 나가다'·'이어 나가다'). 누가 떠나는지는 주어로 본다 — 화면 밖 사람이 주어면 그 사람이다"""
    from app import cast_lib
    cast = video._cast_view({c["id"]: c for c in FAMILY["bible"]["characters"]}, scenes=FAMILY["scenes"])
    sc = lambda act, ko="자리": {"action": act, "shot": "wide shot", "ko": ko}
    for act in ("The oven timer goes off and Beth laughs.", "Beth heads off to the stove to get more tea.", "James's phone goes off on the table.",
                "Niki slips off her chair to hug her father.", "Beth realizes they have run out of milk.", "The birthday candle goes out and Niki giggles.",
                "Niki looks out the kitchen door at the garden.", "Niki says she wants to go out and play later.",
                "Beth tells James she will leave for work soon.", "Niki walks through golden leaves.", "Niki doesn't leave the table.",
                "The candle goes out.", "Beth realizes they have run out.", "Beth remembers the day James stormed out.",
                "Beth thinks about how James walked away."):
        assert video._leavers(sc(act), ["A", "B", "C"], cast) == set(), act
    for ko in ("니키와 제임스가 숲길을 지나간다", "니키가 한 걸음 앞으로 나간다", "제임스가 이야기를 이어 나간다", "니키가 창밖으로 지나가는 새를 본다",
               "니키의 얼굴에 미소가 지나간다", "니키가 나가 놀고 싶어 한다", "가족이 아침을 먹는 동안 창밖으로 구름이 지나간다", "니키가 나가려다 멈춘다",
               "베스는 떠나지 않는다"):
        assert video._leavers(sc("They sit.", ko), ["A", "B", "C"], cast) == set(), ko
    for act, ko, want in (("Niki grabs her bag and runs out of the kitchen door.", "자리", {"C"}), ("James storms out, slamming the door.", "자리", {"B"}),
                          ("Beth leaves the kitchen.", "자리", {"A"}), ("James leaves.", "자리", {"B"}), ("Niki runs out.", "자리", {"C"}),
                          ("James walks out the front door.", "자리", {"B"}), ("Niki runs upstairs.", "자리", {"C"}), ("Beth goes home.", "자리", {"A"}),
                          ("They sit.", "가족이 정원으로 나간다", {"A", "B", "C"}), ("They sit.", "니키가 부엌문으로 뛰어나간다", {"C"}),
                          ("They sit.", "베스가 집을 나선다", {"A"}), ("They sit.", "뛰쳐나가는 니키", {"C"}), ("They sit.", "제임스가 자리를 뜬다", {"B"})):
        assert video._leavers(sc(act, ko), ["A", "B", "C"], cast) == want, (act, ko)
    # 화면 밖 사람이 주어: 베스 클로즈업의 'Beth watches Niki run out …' 은 니키가 떠난다. 다음 모두에게 하는 말은 베스·제임스 (니키 아님)
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    lib = {e["id"]: e for e in known}
    libc = [{"id": cid, "look": lib[cid]["look"], "name_ko": n, "voice": "A natural Korean voice.", "voice_ko": "목소리"}
            for cid, n in (("beth", "베스"), ("james", "제임스"), ("niki", "니키"))]
    s, left = _r3_clean([_r3_shot("P1", ["beth", "james", "niki"], "wide shot", "Beth, James and Niki sit around the breakfast table.", "식탁", 12),
                         _r3_shot("P1", ["beth"], "close-up", "Beth watches Niki run out of the kitchen door.", "니키를 배웅하는 베스", 6),
                         _r3_talk("P1", ["james"], "james", "everyone", "자, 이제 커피나 마실까!", shot="medium shot", action="James stretches.")],
                        chars=libc, known=known)
    assert s["scenes"][2]["characters"] == ["james", "beth"], s["scenes"][2]["characters"]
    assert any('add "beth"' in x for x in left) and not any('"niki"' in x for x in left), left
    # 그 자리에 그대로 있는 사람은 다음 대화 컷에 남는다 (예전에는 'goes off'·'heads off to' 로 베스·제임스를 뺐다)
    for act, rest in (("The oven timer goes off and Beth laughs.", ["james", "niki"]), ("Beth heads off to the stove to get more tea.", ["james", "niki"])):
        s, _l = _r3_clean([_r3_shot("P1", ["beth", "james", "niki"], "wide shot", "Beth, James and Niki sit at the table.", "식탁", 12),
                           _r3_shot("P1", ["beth", "james", "niki"], "wide shot", act, "식탁", 8),
                           _r3_talk("P1", rest, rest[0], rest[1], "그거 정말이야?")], chars=libc, known=known)
        assert set(s["scenes"][2]["characters"]) == {"beth", "james", "niki"}, act
    # 넓은 장면이 '창밖으로 구름이 지나간다'여도 모두에게 하는 말은 그 자리 모두의 그룹샷 (예전에는 모두 떠난 것으로 읽어 혼자 클로즈업)
    wide = dict(_R3_WIDE, ko="가족이 아침을 먹는 동안 창밖으로 구름이 지나간다")
    s, _l = _r3_clean([wide, _r3_talk("P1", ["A"], "A", "everyone", "자, 먹자!", shot="medium shot")])
    assert s["scenes"][1]["characters"] == ["A", "B", "C"] and s["scenes"][1]["to_all"] is True
    # 들어오는 말
    for act, who in (("Niki walks into the kitchen, rubbing her eyes.", {"C"}), ("Niki rushes back in for her forgotten hat.", {"C"}),
                     ("Niki appears tired.", set())):
        assert video._movers(sc(act), ["C"], cast, "enter") == who, act


def test_r3_monologue_prompt_never_names_anyone_else_even_with_letter_ids():
    """G0-3 독백 글 (3차 검토 4): 설정집 id 가 'A'·'B' 꼴이고 라이브러리 밖 인물이어도 장면 글에서 영어 이름을 배워(_text_names), 독백 장면 글의
    다른 사람 이름을 검사가 잡고 영상 글에서 뺀다. 모르는 고유명사가 든 문장도 영상 글에서 뺀다 (화면 밖 사람일 수 있다)"""
    import copy as _copy
    with duo_setting(True):
        for act in ("Niki glances at James across the table and whispers to herself.", "Niki looks at Beth, then down at her plate."):
            m = _copy.deepcopy(FAMILY)
            m["scenes"][3]["action"] = act
            left = video.dialogue_problems(m, 60, True, exchange=True)
            assert any("Scene 4 is a monologue: its \"action\" must describe only the speaker" in x for x in left), (act, left)
            tp = video.talk_prompt(video.clean_dialogue_story(m, 60, exchange=True), 3)
            assert "James" not in tp and "Beth" not in tp and "alone in the frame" in tp, tp
        names = video._text_names(FAMILY["scenes"], {c["id"]: c for c in FAMILY["bible"]["characters"]})
        assert names == {"A": {"Beth"}, "B": {"James"}, "C": {"Niki"}}, names
        # 라이브러리 밖 수지(A)·민호(B)의 카페
        cafe = _r3_story([{"id": "A", "look": "a young woman in her twenties with a short bob and a mustard sweater", "name_ko": "수지",
                           "voice": "A bright young Korean woman.", "voice_ko": "밝은 목소리"},
                          {"id": "B", "look": "a young man in his twenties with round glasses and a navy coat", "name_ko": "민호",
                           "voice": "A calm young Korean man.", "voice_ko": "차분한 목소리"}],
                         [_r3_shot("P1", ["A", "B"], "wide shot", "Suji and Minho sit by the cafe window.", "카페", 10),
                          _r3_talk("P1", ["A", "B"], "A", "B", "오늘 좀 늦었네?", action="Suji tilts her head at Minho."),
                          _r3_talk("P1", ["B", "A"], "B", "A", "미안, 버스를 놓쳤어.", action="Minho rubs his neck.", cont=True),
                          _r3_talk("P1", ["A"], "A", "nobody", "또 거짓말...", shot="close-up", action="Suji glances at Minho and bites her lip.")])
        left = video.dialogue_problems(cafe, 30, True, exchange=True)
        assert any('mentions someone else ("Minho")' in x for x in left), left
        sc = video.clean_dialogue_story(cafe, 30, exchange=True)
        assert "Minho" not in video.talk_prompt(sc, 3)
        # 모르는 고유명사가 든 문장은 영상 글에서 뺀다 (검사는 하지 않는다), 말하는 사람만 쓴 문장은 남긴다
        sc["scenes"][3]["action"] = "Suji sighs quietly. She thinks of Tom."
        tp = video.talk_prompt(sc, 3)
        assert "Suji sighs quietly." in tp and "Tom" not in tp
        # 문장 첫머리의 흔한 낱말은 이름이 아니다 ('Tears' 를 이름으로 배우면 다른 사람 글의 'tears' 를 사람으로 셌다)
        tears = _r3_story(_R3_FAM, [_r3_shot("P1", ["C"], "close-up", "Tears roll down her cheeks.", "눈물", 6),
                                   _r3_talk("P1", ["C"], "C", "nobody", "왜 아무도...", shot="close-up", action="Niki wipes away her tears.")])
        assert "Tears" not in str(video._text_names(tears["scenes"], {c["id"]: c for c in _R3_FAM}))
        # 안경 쓴 제임스의 독백에서 'James' 는 그 사람 자신이다 (같은 사람의 다른 모습)
        from app import cast_lib
        v = _variant_story()
        v["scenes"][3].update(characters=["james_glasses"], listener="nobody", continues=False, shot="close-up",
                              action="James adjusts his glasses.")
        known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
        sv = video.clean_dialogue_story(v, 32, exchange=True, known=known)
        cast_lib.apply_to_story(sv, known, "베스와 제임스, 니키의 아침 식탁")
        assert sv["scenes"][3]["monologue"] is True and "James adjusts his glasses." in video.talk_prompt(sv, 3)


def test_r3_one_seat_map_per_place_including_wide_shots():
    """G2 자리 (3차 검토 5): 같은 장소 구간의 자리 지도(_run_seats)는 넓은 장면·대사 컷 가운데 처음 함께 나온 장면의 좌우를 지킨다. 두 사람 컷의
    좌우·한 사람 컷의 시선이 넓은 장면의 자리를 따르고, 앞선 투샷 다음의 넓은 장면도 그 좌우를 따른다"""
    from app import cast_lib
    # 넓은 장면 [제임스, 베스, 니키] → 니키가 나간 뒤 베스→제임스 투샷: 제임스 왼쪽, 베스 오른쪽 (베스는 왼쪽을 보며 말한다)
    s, _l = _r3_clean([_r3_shot("P1", ["B", "A", "C"], "wide shot", "James, Beth and Niki sit around the breakfast table.", "식탁", 12),
                       _r3_shot("P1", ["C"], "medium shot", "Niki grabs her bag and runs out of the kitchen door.", "니키가 나간다", 6),
                       _r3_talk("P1", ["A", "B"], "A", "B", "우리 딸 다 컸네요.", action="Beth smiles at James.")])
    with duo_setting(True):
        assert [x["id"] for x in video.layout_slots(s, 0, video.scene_people(s, 0))] == ["B", "A", "C"]
        pair = video.layout_slots(s, 2, video.scene_people(s, 2))
        assert [x["id"] for x in pair] == ["B", "A"] and video._act_sentence(s, 2, pair).startswith("The person on the right speaks toward the left")
    # 넓은 장면 오른쪽의 니키가 그 장소에 대화 컷이 없는 독백을 하면 식탁 쪽(왼쪽)을 본다
    s, _l = _r3_clean([_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family eats breakfast in silence.", "조용한 아침 식탁", 12),
                       _r3_talk("P1", ["C"], "C", "nobody", "아무도 내 생일을 모르나 봐...", shot="close-up", action="Niki stares at her plate."),
                       _r3_talk("P2", ["A", "B"], "A", "B", "준비 다 됐어요?"), _r3_talk("P2", ["B", "A"], "B", "A", "그럼요.", cont=True)])
    with duo_setting(True):
        assert video.layout_slots(s, 1, ["C"])[0]["facing"] == "turned slightly toward the left" and "toward the left edge" in video.talk_prompt(s, 1)
        assert video._seat_facing(s, 0, "B") is None and video._seat_facing(s, 0, "A") == "right"      # 말 없는 장면의 가운데는 정하지 않는다
    # 말 없는 넓은 장면에 화면에 보이는 사람의 목소리가 깔려도 가운데 사람의 시선은 정하지 않는다 (대사 컷만 말하는 쪽을 본다)
    vo = _r3_clean([_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits at the table.", "식탁", 12, speaker="A", line="자, 먹자!",
                             emotion="cheerful"), _r3_talk("P1", ["C"], "C", "nobody", "배고파...", shot="close-up")])[0]
    with duo_setting(True):
        assert vo["scenes"][0]["speaker"] == "A" and video._seat_facing(vo, 0, "B") is None
    # 정원 투샷 [니키, 베스] 다음 같은 정원의 넓은 장면 [베스, 제임스, 니키]: 니키가 베스 왼쪽인 좌우를 지킨다 (예전에는 넓은 장면이 뒤집혔다)
    t3 = _r3_clean([_r3_talk("P2", ["C", "A"], "C", "A", "엄마, 고마워요!"), _r3_talk("P2", ["A", "C"], "A", "C", "사랑한단다."),
                    _r3_shot("P2", ["A", "B", "C"], "wide shot", "The family stands together under the apple tree.", "정원의 가족", 12)])[0]
    with duo_setting(True):
        two = [x["id"] for x in video.layout_slots(t3, 0, video.scene_people(t3, 0))]
        wide = [x["id"] for x in video.layout_slots(t3, 2, video.scene_people(t3, 2))]
        assert wide.index(two[0]) < wide.index(two[1]), (two, wide)
    assert video._merge_seats(["A", "B"], ["A", "C", "B"]) == ["A", "C", "B"] and video._merge_seats(["B", "A"], ["A", "B", "C"]) == ["B", "A", "C"]
    assert video._merge_seats(["A", "B"], ["C", "A"]) == ["C", "A", "B"] and video._merge_seats([], ["B", "A"]) == ["B", "A"]
    # 세 사람 대화 컷 [베스, 제임스, 니키] 다음 같은 식탁의 [베스, 안경 쓴 제임스] 투샷: 라이브러리로 덮어쓴 뒤에도 세 사람 자리의 좌우 그대로
    # (2차 검토 변이: 같은 사람 묶음을 무시해도 통과했다)
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    v = _variant_story()
    v["scenes"][0]["characters"] = ["james", "beth", "niki"]
    v["scenes"][1].update(kind="talk", characters=["beth", "james", "niki"], speaker="beth", listener="james", line_ko="여보, 신문 그만 봐요.",
                          emotion="teasing", shot="medium group shot", action="Beth smiles.")
    v["scenes"][2].update(characters=["beth", "james_glasses"], speaker="beth", listener="james_glasses", continues=False)
    v["scenes"][3].update(characters=["james_glasses", "beth"], speaker="james_glasses", listener="beth", continues=True)
    with duo_setting(True):
        sv = video.clean_dialogue_story(v, 32, exchange=True, known=known)
        cast_lib.apply_to_story(sv, known, "베스와 제임스, 니키의 아침 식탁")
        seats = [x["id"] for x in video.layout_slots(sv, 1, video.scene_people(sv, 1))]
        pair = [x["id"] for x in video.layout_slots(sv, 2, ["beth", "james_glasses"])]
        assert seats.index("james") < seats.index("beth") and pair == ["james_glasses", "beth"], (seats, pair)
    # 가운데 자리 화자가 한 사람에게 말하면 그 사람 쪽을 본다 (2차 검토 변이: 가운데 화자 시선을 지워도 통과했다)
    with duo_setting(True):
        g = video.clean_dialogue_story(FAMILY, 60, exchange=True)
        g["scenes"][2].update(speaker="B", listener="C")
        assert video._seat_facing(g, 2, "B") == "right"
        g["scenes"][2].update(listener="A")
        assert video._seat_facing(g, 2, "B") == "left"
        # 다른 장소의 대화 컷이 더 가까워도 같은 장소의 여럿 장면을 먼저 따른다 (2차 검토 변이: 같은 장소 먼저를 지워도 통과했다)
        f = _r3_clean([_r3_talk("P2", ["C", "A"], "C", "A", "엄마, 이리 와요!"),
                       _r3_shot("P1", ["C"], "close-up", "Niki smiles at her plate.", "니키", 6),
                       _r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits at the table.", "식탁", 12)])[0]
        assert [x["id"] for x in video.layout_slots(f, 0, ["C", "A"])] == ["C", "A"]          # 정원에서는 니키가 왼쪽 (오른쪽을 본다)
        assert video.facing_of(f, 1, "C") == "left"                     # 바로 앞은 정원 투샷이지만, 식탁(같은 장소)에서는 오른쪽 자리라 왼쪽을 본다


def test_r3_listener_spellings_names_and_other_looks():
    """listener 칸 (3차 검토 6·8): 독백을 뜻하는 말·말하는 사람 자신은 독백, 모두를 뜻하는 말은 모두에게, 이름으로 적은 듣는 사람은 그 사람.
    모르는 값·자기 id 는 그것만 말하고 추정한 사람을 넣으라고 하지 않는다. 같은 사람의 다른 모습으로 적은 듣는 사람은 화면의 모습으로 읽는다"""
    from app import cast_lib
    base = [dict(_R3_WIDE, action="Beth, James and Niki sit around the breakfast table."),
            _r3_talk("P1", ["A", "B", "C"], "A", "B", "여보, 커피?", shot="medium group shot", action="Beth offers coffee to James.")]
    for lis in ("C", "themselves", "monologue", "N/A", "아무도", "nobody (thinking aloud)", "on the phone", "the camera", "viewers", "혼잣말"):
        s, left = _r3_clean(base + [_r3_talk("P1", ["C"], "C", lis, "오늘은 정말 좋은 날이야.", shot="close-up", action="Niki smiles to herself.")])
        assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True, lis
        assert not any('put "' in x for x in left), (lis, left)
    for lis in ("Everyone", "everybody", "모두", "both of them", "the family"):
        s, _l = _r3_clean(base + [_r3_talk("P1", ["C"], "C", lis, "다들 고마워요!", shot="medium shot")])
        assert s["scenes"][2]["to_all"] is True and s["scenes"][2]["characters"] == ["C", "A", "B"], lis
    for lis in ("Beth", "베스"):
        s, left = _r3_clean(base + [_r3_talk("P1", ["C", "A"], "C", lis, "엄마, 고마워요!", action="Niki smiles at Beth.")])
        assert s["scenes"][2]["listener"] == "A" and s["scenes"][2]["characters"] == ["C", "A", "B"], lis
        assert not any('is not in bible.characters' in x for x in left), left
    s, left = _r3_clean(base + [_r3_talk("P1", ["C"], "C", "Z", "음...", shot="close-up")])
    assert any('"listener" "Z" is not in bible.characters' in x for x in left) and not any('put "' in x for x in left), left
    # 안경 쓴 제임스가 화면에 있는데 listener 를 'james' 로 적었다: 검사는 'james' 를 넣으라고 하지 않고, 정리는 안경 쓴 모습 그대로, 다음 컷 잇기도
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    v = _variant_story()
    v["scenes"][1] = dict(v["scenes"][0], characters=["beth", "james_glasses"], shot="wide shot", action="Beth and James sit; James reads.")
    v["scenes"][2]["listener"] = "james"
    with duo_setting(True):
        left = video.dialogue_problems(v, 32, True, exchange=True, known=known)
        assert not any('put "james"' in x for x in left), left
        sv = video.clean_dialogue_story(v, 32, exchange=True, known=known)
        assert sv["scenes"][2]["characters"] == ["beth", "james_glasses"] and sv["scenes"][2]["listener"] == "james_glasses"
        assert sv["scenes"][3]["continues"] is True


def test_r3_animal_speaker_and_animal_listener_prompts():
    """동물이 말하거나 듣는 대사 컷 (3차 검토 7): 사람이 있으면 그 사람들이 듣는 글을 쓴다. 사람이 동물에게 말하는 두세 사람 컷의 영상 글은
    그 동물에게 말한다고 쓰고 화면의 동물도 적는다. 4차 검토 G2: 동물 화자 컷도 그 자리 사람을 이어 받는다 (G0 '그 자리 모두')"""
    fox = {"id": "F", "look": "a small red fox with a blue scarf", "name_ko": "여우", "voice": "A sly young Korean voice.", "voice_ko": "여우"}
    dog = {"id": "D", "look": "a small white puppy with floppy ears", "name_ko": "뭉치"}
    s, left = _r3_clean([_r3_shot("P1", ["A", "B", "C", "F"], "wide shot", "The family sits at the table; a small fox sits on a chair.", "식탁의 여우", 12),
                         _r3_talk("P1", ["F", "C"], "F", "C", "니키야, 나침반을 잃어버렸어!", shot="medium shot", action="The fox looks up at Niki."),
                         _r3_talk("P1", ["C", "F"], "C", "F", "정말? 같이 찾아볼게!", shot="medium shot", action="Niki leans toward the fox."),
                         _r3_talk("P1", ["F"], "F", "everyone", "다들 도와줘!", shot="medium shot", action="The fox hops onto the table.")],
                        chars=_R3_FAM + [fox])
    with duo_setting(True):
        assert s["scenes"][1]["characters"] == ["F", "C", "A", "B"]
        tp = video.talk_prompt(s, 1)
        assert "small red fox" in tp and "Only the small red fox speaks" in tp and "alone in the frame" not in tp and "lips closed" in tp, tp
        assert s["scenes"][2]["characters"] == ["C", "F", "A", "B"] and video.talk_group(s, 2) == ["C", "A", "B"]
        dp = video.duo_talk_prompt(s, 2, video.layout_slots(s, 2, video.scene_people(s, 2)))
        assert "speaks to the small red fox" in dp and "Also in the frame: a small red fox with a blue scarf." in dp, dp
        assert any('Scene 4: "F" speaks to everyone in the place' in x for x in left), left     # 동물 화자도 그 자리 모두에게 (4차 검토 G2)
    s, _l = _r3_clean([_r3_shot("P1", ["A", "C", "D"], "wide shot", "Beth and Niki sit with the puppy.", "강아지", 10),
                       _r3_talk("P1", ["C", "D", "A"], "C", "D", "뭉치야, 이리 와!", shot="medium two-shot")], chars=_R3_FAM + [dog])
    with duo_setting(True):
        dp = video.duo_talk_prompt(s, 1, video.layout_slots(s, 1, video.scene_people(s, 1)))
        assert "speaks to the small white puppy" in dp and "speaks to the person" not in dp, dp


def test_r3_one_person_shot_text_becomes_a_group_shot():
    """사람을 더한 대화 컷의 샷 글 (3차 검토 9): 한 사람을 가리키는 샷 글('medium shot of Beth at the table')은 미디엄 투샷·그룹샷으로"""
    assert video.group_shot("medium shot of Beth at the table", 3) == "medium group shot"
    assert video.group_shot("medium shot of her", 2) == "medium two-shot" and video.group_shot("medium shot of the girl", 2) == "medium two-shot"
    assert video.group_shot("medium shot of the table", 3) == "medium shot of the table"         # 한 사람이 아니다 (그대로)
    s, _l = _r3_clean([_R3_WIDE, _r3_talk("P1", ["A"], "A", "B", "여보, 오늘 니키 생일이에요.", shot="medium shot of Beth at the table")])
    assert s["scenes"][1]["characters"] == ["A", "B", "C"] and s["scenes"][1]["shot"] == "medium group shot"
    with duo_setting(True):
        assert video.duo_talk_prompt(s, 1, video.layout_slots(s, 1, video.scene_people(s, 1))).startswith("Medium group shot of three people")
    cast = video._cast_view({"beth": {"look": "a woman", "name_ko": "베스"}, "niki": {"look": "a girl", "name_ko": "니키"}})
    assert video.group_shot("medium shot, beth in focus", 2, cast) == "medium two-shot"


def test_r3_silent_scene_two_looks_and_listener_after_a_voiceless_speaker():
    """2차 검토가 고정하지 못한 갈래 (3차 검토 20 ⑦⑧): 말 없는 장면에 같은 사람의 두 모습을 함께 두면 위반. characters 에 말하는 사람을 적지 않은
    talk 장면도 그 사람이 보이며 말한 대사라, 다음 줄의 듣는 사람 추정에 쓰인다"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    v = _variant_story()
    v["scenes"][0]["characters"] = ["beth", "james", "james_glasses"]
    with duo_setting(True):
        left = video.dialogue_problems(v, 32, True, exchange=True, known=known)
        assert any('Scene 1: "james" and "james_glasses" are the same person in two looks' in x for x in left), left
    # 장면 1 은 talk 인데 characters 가 비었다 (말하는 사람 B 가 보이며 말한다): 장면 2 의 빈 listener 는 B
    seq = [{"kind": "talk", "speaker": "B", "line_ko": "안녕?", "place": "P1", "characters": []},
           {"kind": "talk", "speaker": "A", "line_ko": "응, 왔니?", "place": "P1", "characters": ["A"]}]
    assert video.line_target(seq, 1, FAMILY_IDS, exchange=True) == ("one", "B")
    s, _l = _r3_clean([_r3_talk("P1", [], "B", "", "안녕?", shot="close-up"), _r3_talk("P1", ["A"], "A", "", "응, 왔니?", shot="close-up")])
    assert s["scenes"][1]["listener"] == "B" and s["scenes"][1]["characters"] == ["A", "B"]


def test_r3_request_style_sentence_forms_objects_and_unlisted_styles():
    """요청 글의 화풍 (3차 검토 10·11·13·14·15·18·19): 흔한 문장 꼴의 화풍 요청('수채화로 …'·'수채화처럼 …'·'in the style of a watercolor painting'·
    'as an oil painting'·'목판화 느낌')은 화풍 요청이고, 애니·실사·3D 말도 물건·활동·관용 표현이면 화풍이 아니다. 목록 밖 화풍('반 고흐 스타일로')은
    구성안 모델에게 판단을 맡기고 모델이 쓴 화풍(실사 말고)을 덮지 않는다"""
    from app import cast_lib
    lib = cast_lib.library()
    lib_style = lib[0]["style"]

    def applied(style, text):
        st = {"bible": {"characters": [{"id": "A", "name_ko": "니키", "look": "x"}], "style": style}}
        cast_lib.apply_to_story(st, lib, text, keep_style=bool(style) and not cast_lib.is_photo_style(style))
        return st["bible"]["style"]
    sure = (("수채화로 니키와 푸토르가 숲을 걷는 영상", "custom"), ("유화로 니키와 베스가 대화하는 장면", "custom"),
            ("크레파스로 니키가 뛰노는 영상", "custom"), ("수채화처럼 니키와 푸토르가 숲을 걷는 영상", "custom"),
            ("그림책처럼 니키와 푸토르가 걷는 영상", "custom"), ("그래픽 노블처럼 푸토르의 이야기", "custom"),
            ("수채화 영상으로 니키와 푸토르가 숲을 걷는 장면", "custom"), ("수채화 같은 그림으로 니키와 푸토르를 보여줘", "custom"),
            ("니키와 푸토르, 수채화 기법으로", "custom"), ("목판화 느낌으로 니키와 푸토르", "custom"), ("동판화 스타일로 니키", "custom"),
            ("전래동화 느낌으로 니키와 푸토르", "custom"), ("A watercolor short film of Niki and Putor in the forest", "custom"),
            ("Niki and Putor in watercolor, walking in the forest", "custom"), ("Niki and Putor in the style of a watercolor painting", "custom"),
            ("Make Niki and Putor look like a watercolor painting", "custom"), ("Niki and Beth as an oil painting", "custom"),
            ("Niki and Putor like a children's picture book", "custom"), ("Niki and Putor, done in watercolour", "custom"),
            ("Niki walking through a flower field, painted in watercolor", "custom"), ("a watercolor painting of Niki and Putor", "custom"),
            ("니키와 푸토르, 8비트 픽셀 게임 느낌으로", "custom"), ("니키와 푸토르를 수채 물감 느낌으로", "custom"),
            ("니키와 푸토르, 수채화처럼 예쁜 영상", "custom"), ("니키와 푸토르, 유화 같은 분위기로", "custom"), ("니키와 푸토르를 동화책 삽화처럼", "custom"),
            ("니키와 푸토르를 클레이 인형으로 만든 영상", "custom"), ("점토 인형 애니메이션으로 니키와 푸토르", "custom"),
            ("목탄화 느낌으로 니키와 푸토르", "custom"), ("펜화 스타일로 푸토르가 약초를 고르는 영상", "custom"), ("흑백 무성영화처럼 제임스가 걷는 영상", "custom"),
            ("종이접기 스타일로 니키", "custom"), ("니키와 푸토르를 모자이크 스타일로", "custom"),
            ("지브리 느낌으로 니키와 푸토르가 숲을 걷는 영상", "anime"), ("카툰 스타일로 니키와 제임스가 노는 영상", "anime"), ("손그림 느낌으로 니키", "anime"),
            ("Niki and Putor walking in the forest, manga style", "anime"), ("Niki and Putor walking in the forest, webtoon style", "anime"),
            ("니키와 푸토르가 대화하는 애니메이션을 만들어 줘", "anime"), ("3D로 니키와 푸토르가 노는 영상", "toon3d"), ("CG 느낌으로 니키", "toon3d"),
            ("반실사 수채화로 니키", "semi"))
    for text, key in sure:
        assert cast_lib.said_level(text) == "sure" and cast_lib.said_key(text) == key, (text, cast_lib.said_level(text), cast_lib.said_key(text))
        assert cast_lib.prompt_block(lib, text=text) == cast_lib.prompt_block(lib), text           # 지시문 화풍 줄은 한 가지 (3차 명세 2.2)
    assert cast_lib.said_style_fill("점토 인형 애니메이션으로 니키와 푸토르", lib_style) == "claymation style"
    assert cast_lib.said_style_fill("지브리 느낌으로", lib_style) == cast_lib.SAID_STYLE_TEXT["anime"]
    assert cast_lib.said_style_fill("3D로 니키와 푸토르가 노는 영상", lib_style) == cast_lib.SAID_STYLE_TEXT["toon3d"]
    # 구성안이 요청을 따랐으면 그대로 (예전에는 '말하지 않음 + custom' 으로 읽혀 라이브러리 반실사로 덮였다)
    for text, plan in (("수채화로 니키와 푸토르가 숲을 걷는 영상", "soft watercolor painting with visible paper grain"),
                       ("Niki and Putor in the style of a woodblock print", "woodblock print with bold carved lines"),
                       ("니키와 푸토르를 클레이 인형으로 만든 영상", "claymation stop-motion"), ("Niki and Putor, manga style", "black and white manga illustration")):
        assert applied(plan, text) == plan, (text, plan)
    # 요청 화풍 말이 없는 custom 구성안·다른 열쇠 구성안은 요청 화풍으로 채운다
    assert applied("pixel art", "수채화로 니키와 푸토르가 숲을 걷는 영상") == "watercolour painting style"
    assert applied("semi-realistic digital painting, 2.5D concept art", "니키와 베스가 부엌에서 대화하는 장면을 애니메이션으로") == cast_lib.SAID_STYLE_TEXT["anime"]
    # 물건·활동·관용 표현의 애니·실사·3D 말: 화풍 요청이 아니다 (라이브러리 화풍 그대로)
    for text in ("니키가 거실에서 만화를 그리는 영상", "니키와 푸토르가 TV로 애니메이션을 보는 영상", "니키가 웹툰을 보며 웃는 영상", "니키의 만화 같은 하루",
                 "니키와 푸토르의 만화 같은 모험", "만화가 베스가 니키에게 그림을 보여주는 영상", "니키가 고양이 애니와 노는 영상",
                 "니키가 애니메이션 감독이 꿈이라고 말하는 영상", "Niki watches cartoons with Putor on a rainy day",
                 "Niki and Putor have an animated conversation in the forest", "Putor tells Niki an old story in an animated voice",
                 "Niki is drawing anime characters in her sketchbook", "Niki takes a photograph of Putor in the garden",
                 "Beth shows Niki an old photograph of James", "Niki photographs the flowers in Putor's garden", "니키가 사진 같은 풍경을 바라보는 영상",
                 "니키가 사진처럼 선명한 꿈을 꾸는 영상", "니키와 제임스가 픽사 영화를 보는 영상", "니키가 픽사 인형을 들고 노는 영상",
                 "니키가 3D 프린터로 나침반을 만드는 영상", "니키와 제임스가 3D 안경을 쓰고 영화를 보는 영상", "Niki makes a clay pot with Putor",
                 "Niki plays in the clay", "일러스트레이터 베스가 그림을 그리는 영상", "Niki and James put on 3D glasses to watch a movie",
                 "Niki and Putor visit an anime museum", "Niki talks to the anime club", "Niki and Putor at the anime festival",
                 "Niki and Putor at a cartoon exhibition"):
        assert cast_lib.said_level(text) is None and cast_lib.said_key(text) is None, (text, cast_lib._req_scan(text))
        assert lib_style in cast_lib.prompt_block(lib, text=text) and applied("photorealistic cinematic live action", text) == lib_style, text
    # 목록 밖 화풍: 판단은 구성안 모델에게, 모델이 쓴 화풍(실사 말고)은 둔다. 비었거나 실사면 라이브러리 화풍
    for text, plan in (("반 고흐 스타일로 니키와 푸토르가 숲길을 걷는 영상", "Van Gogh inspired post-impressionist oil painting with swirling brushstrokes"),
                       ("니키와 푸토르가 대화하는 영상, 레고 스타일로", "LEGO brick figure style toy world"),
                       ("사이버펑크 느낌으로 니키가 도시를 걷는 영상", "cyberpunk neon-lit digital illustration"),
                       ("Niki and Putor walking in the forest, Van Gogh style", "Van Gogh inspired post-impressionist painting"),
                       ("니키와 푸토르, 아르데코 풍으로", "art deco poster style"), ("니키와 푸토르, 디즈니처럼", "classic hand-drawn animation"),
                       ("디즈니 스타일로 니키와 푸토르가 춤추는 영상", "stylized 3D animated feature film look")):
        assert cast_lib.said_style(text), text
        assert applied(plan, text) == plan and applied("photorealistic film", text) == cast_lib.said_style_fill(text, lib_style), (text, plan)
        assert applied("", text) == (lib_style if cast_lib.said_level(text) == "maybe" else cast_lib.said_style_fill(text, lib_style)), text
        if cast_lib.said_level(text) == "maybe":
            block = cast_lib.prompt_block(lib, text=text)
            assert "if the request says anything about the look" in block and lib_style in block, text      # 한 가지 줄 (3차 명세 2.2, fixC 문구)
    assert cast_lib.said_level("니키와 푸토르가 대화하는 영상, 레고 스타일로") == "sure" and cast_lib.said_level("디즈니 스타일로 니키") == "maybe"
    for text in ("따뜻한 느낌으로 니키가 걷는 영상", "봄 느낌으로 니키가 걷는 영상", "유럽풍 마을에서 니키가 걷는 영상", "니키의 패션 스타일",
                 "니키와 푸토르가 숲길을 걸으며 대화하는 영상"):
        assert cast_lib.said_level(text) is None, text
    assert applied("whimsical fantasy concept art with soft light", "니키와 푸토르가 숲길을 걸으며 대화하는 영상") == lib_style
    # 영어 manga·webtoon·manhwa 는 애니 열쇠 (구성안 화풍 글)
    for style in ("Korean webtoon style digital illustration with clean line art and flat colours", "manga style illustration", "Korean manhwa style"):
        assert cast_lib.style_key(style) == "anime", style


def test_r3_photo_requests_keep_only_photo_plans_and_photo_like_custom_has_no_negation():
    """실사 요청 (3차 검토 12): 구성안 화풍이 실사로 읽혀야 그대로 둔다 (사실적인 영화 말도 실사로 읽는다). custom 화풍인데 사진 쪽이면(누아르·흑백
    무성영화) 장면 글에 '사진 아님' 을 붙이지 않는다"""
    from app import cast_lib
    ask = "실사 영화처럼 베스와 제임스가 대화하는 영상"
    for plan in ("hyper-realistic cinematic look", "realistic cinematic film look with natural light", "cinematic realism, natural lighting",
                 "lifelike cinematic style", "naturalistic film look", "photorealistic cinematic live action"):
        assert cast_lib.keeps_said_style(ask, plan) and cast_lib.style_key(plan) == "photo", plan
    for plan in ("semi-realistic digital painting, 2.5D concept art", "soft painterly storybook look", "2D anime"):
        assert not cast_lib.keeps_said_style(ask, plan), plan
    assert not cast_lib.keeps_said_style("니키와 베스가 부엌에서 대화하는 장면을 애니메이션으로", "semi-realistic digital painting, 2.5D concept art")
    base = video.clean_dialogue_story(DLG_OK, 48)
    for style, neg in (("black-and-white film noir with hard shadows", False), ("black-and-white silent film look", False),
                       ("soft watercolour storybook illustration", True)):
        st = dict(base, bible=dict(base["bible"], style=style))
        assert ("never photographic realism" in video.scene_notes(st, 1)) is neg and video.photo_like(st) is not neg, style


def test_r3_empty_style_is_the_photorealistic_default():
    """빈 화풍 (3차 검토 16): 다듬을 때 STORY_SYSTEM 기본값(실사)으로 채워 정지 그림 지시문(반실사 문장)과 기준 그림·영상 글(실사)이 부딪히지
    않는다. 라이브러리 인물 영화는 요청이 실사를 말하지 않았으면 라이브러리 화풍으로 바뀐다"""
    import copy as _copy
    from app import cast_lib
    d = _copy.deepcopy(DLG_OK)
    d["bible"]["style"] = ""
    s = video.clean_dialogue_story(d, 48)
    assert s["bible"]["style"] == video.DEFAULT_STYLE and video.style_key(s) == "photo" and video.story_style(s) == ""
    keep = video.style_keep(s)
    assert keep.endswith(video.STYLE_KEEP["photo"]) and "semi-realistic" not in keep
    assert video._style_line(s) == " Photorealistic cinematic look."
    lib = cast_lib.library()
    cast_lib.apply_to_story(s, lib, "니키와 푸토르가 대화", keep_style=bool(video.story_style(s)))
    assert s["bible"]["style"] == lib[0]["style"]


# ── 2차 4차 검토 (11-25): 들어옴·없음·흔한 떠남·그 자리의 동물·구간 첫 대사·화면 밖 목소리·독백의 보통 명사·화풍 요청의 비유·활동·실사 말 ──
def _r4_lib_fam():
    """인물 라이브러리의 베스·제임스·니키 외형 (id A/B/C)"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스와 니키")
    look = {x["id"]: x["look"] for x in known}
    fam = [dict(_R3_FAM[0], look=look["beth"]), dict(_R3_FAM[1], look=look["james"]), dict(_R3_FAM[2], look=look["niki"])]
    return fam, known


def test_r4_arriving_in_the_place_is_not_leaving():
    """4차 검토 1·6: 움직임의 목적지·출발지가 지금 장소인지 본다. 정원 장면의 'runs out into the garden'·부엌 장면의 'runs downstairs into the
    kitchen' 은 그 장소에 들어온 것이라, 그 사람에게 하는 말은 대화(그 자리 모두)이고 모두에게 하는 말에도 나온다. 부르는 말 뒤에 내려오는 사람
    ('comes down the stairs'·'appears in the doorway'·'joins them')은 부르는 순간 아직 없다 (독백)"""
    fam, known = _r4_lib_fam()
    garden = ("Niki runs out into the garden.", "Niki steps outside into the garden.", "Niki runs out of the house into the garden.",
              "Niki dashes out to the apple tree.", "Niki walks out onto the lawn.", "Niki comes running home.")
    for act in garden:
        s, left = _r3_clean([_r3_shot("P2", ["A", "B"], "wide shot", "Beth and James stand under the apple tree.", "정원", 8),
                             _r3_shot("P2", ["C"], "medium shot", act, "니키가 정원에 나온다", 6),
                             _r3_talk("P2", ["A"], "A", "C", "니키야, 이리 와 보렴.", shot="close-up", action="Beth smiles warmly."),
                             _r3_talk("P2", ["B"], "B", "everyone", "자, 다 같이 사과 따자!", shot="close-up", action="James laughs.")],
                            chars=fam, known=known)
        assert s["scenes"][2]["characters"] == ["A", "C", "B"] and s["scenes"][2]["monologue"] is False, act
        assert s["scenes"][3]["characters"] == ["B", "A", "C"] and s["scenes"][3]["to_all"] is True, act
        assert not any("is not in the place" in x for x in left) and any('add "A", "C"' in x for x in left), (act, left)
    for act in ("Niki runs downstairs into the kitchen.", "Niki hurries downstairs.", "Niki runs home and bursts through the door."):
        s, _l = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부엌", 8),
                           _r3_shot("P1", ["C"], "medium shot", act, "니키가 부엌으로 내려온다", 6),
                           _r3_talk("P1", ["A"], "A", "C", "잘 잤니?", shot="close-up"), _r3_talk("P1", ["B"], "B", "everyone", "자, 다 같이 먹자!")],
                          chars=fam, known=known)
        assert s["scenes"][2]["characters"] == ["A", "C", "B"] and s["scenes"][3]["characters"] == ["B", "A", "C"], act
    # 같은 말도 장소가 다르면 떠남: 부엌 장면의 'runs out into the garden'·다락방 장면의 'hurries downstairs'
    cast = video._cast_view({c["id"]: c for c in FAMILY["bible"]["characters"]}, scenes=FAMILY["scenes"],
                            places={p["id"]: p["look"] for p in FAMILY["bible"]["places"]})
    mv = lambda act, place, ko="자리": {c: d[-1] for c, d in video._moves({"action": act, "shot": "medium shot", "ko": ko, "place": place},
                                                                         ["C"], cast).items()}
    for act, place, want in (("Niki runs out into the garden.", "P1", "leave"), ("Niki runs out into the garden.", "P2", "enter"),
                             ("Niki hurries downstairs.", "P3", "leave"), ("Niki hurries downstairs.", "P1", "enter"),
                             ("Niki runs into the house.", "P2", "leave"), ("Niki returns to her room.", "P1", "leave"),
                             ("Niki runs out of the kitchen door.", "P1", "leave"), ("Niki quietly slips out of the room.", "P1", "leave"),
                             ("Niki steps out of the car.", "P2", "enter"), ("Niki comes running out into the garden.", "P2", "enter")):
        assert mv(act, place) == {"C": want}, (act, place)
    assert mv("They sit.", "P2", "니키가 정원으로 나간다") == {"C": "enter"} and mv("They sit.", "P1", "니키가 정원으로 나간다") == {"C": "leave"}
    assert mv("They sit.", "P1", "니키가 부엌으로 들어간다") == {"C": "enter"} and mv("They sit.", "P1", "니키가 방으로 올라간다") == {"C": "leave"}
    assert mv("Niki runs out into the garden.", "") == {"C": "leave"}          # 장소를 모르면 예전 그대로 떠남
    assert video._is_here(["lawn"], video._place_words("a small garden with an apple tree")) is True
    assert video._is_here(["street"], video._place_words("a small garden with an apple tree")) is False
    assert video._is_here(["kitchen"], video._place_words("a quiet attic bedroom with a round window")) is False
    assert video._is_here(["garden"], video._place_words("a sunny kitchen with a window to the garden")) is False     # 창밖은 그 장소가 아니다
    assert video._is_here(["bed"], video._place_words("a quiet attic bedroom with a round window")) is True
    assert mv("Niki walks down the steps.", "P2") == {} and mv("Niki goes to bed.", "P1") == {"C": "leave"}
    assert mv("Beth follows Niki into the kitchen.", "P1") == {"A": "enter"} and mv("Beth follows Niki out of the kitchen.", "P1") == {"A": "leave"}
    assert mv("They sit.", "P1", "니키가 계단을 내려온다") == {"C": "enter"} and mv("Niki joins the family.", "P1") == {"C": "enter"}
    # 화면 밖에서 들어온 사람('Beth looks up as Niki walks in')은 다음 대사 컷에서 그 자리에 있다
    s, _l = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부엌", 8),
                       _r3_shot("P1", ["A"], "close-up", "Beth looks up as Niki walks in.", "베스가 고개를 든다", 5),
                       _r3_talk("P1", ["A"], "A", "C", "잘 잤니?", shot="close-up")], chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["A", "C", "B"] and s["scenes"][2]["monologue"] is False
    # 부르는 말 뒤에 들어오는 사람: 부르는 순간은 혼자 하는 말, 내려온 뒤 대화
    for act in ("Niki comes down the stairs, yawning.", "Niki appears in the doorway.", "Niki joins them at the table.",
                "Niki sits down at the table.", "Beth smiles as Niki walks in."):
        s, left = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부엌", 8),
                             _r3_talk("P1", ["A"], "A", "C", "니키야, 내려와!", shot="close-up", action="Beth calls toward the stairs."),
                             _r3_shot("P1", ["C"] if not act.startswith("Beth") else ["A"], "medium shot", act, "니키가 온다", 6),
                             _r3_talk("P1", ["C", "A"], "C", "A", "좋은 아침이에요!")], chars=fam, known=known)
        assert s["scenes"][1]["characters"] == ["A"] and s["scenes"][1]["monologue"] is True, act
        assert s["scenes"][3]["characters"] == ["C", "A", "B"], act
        assert any('Scene 2: "C" is not in the place at this moment' in x for x in left), (act, left)


def test_r4_absence_words_keep_the_absent_person_out():
    """4차 검토 2: '둘만 남았다'·"Niki's chair is empty"·'with Niki at school'·'while Niki sleeps upstairs'·'After Niki has left' 처럼 그 사람이
    이제 없다고 적은 장면 뒤의 대화 컷에 그 사람을 다시 앉히지 않는다 (모두에게 하는 말 검사도 그 사람을 더하라고 하지 않는다). 독백의 'alone with
    her thoughts' 는 없음이 아니다"""
    import asyncio as _asyncio
    import json as _json
    fam, known = _r4_lib_fam()
    for act, ko in (("Beth and James are alone in the kitchen now.", "부엌에 둘만 남았다"),
                    ("Niki's chair is empty; Beth and James sit across from each other.", "빈 의자"),
                    ("Later, with Niki at school, Beth and James sit alone at the table.", "부부의 식탁"),
                    ("That evening, Beth and James talk quietly at the table while Niki sleeps upstairs.", "부부의 식탁"),
                    ("After Niki has left for school, Beth pours more tea for James.", "부부의 식탁"),
                    ("Beth and James sit at the table.", "부엌에 둘만 남았다")):
        scenes = [_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the breakfast table in the morning light.", "식탁", 8),
                  _r3_shot("P1", ["A", "B"], "medium two-shot", act, ko, 6),
                  _r3_talk("P1", ["A", "B"], "A", "B", "그 애가 요즘 좀 힘든가 봐요.", action="Beth sighs and looks at James."),
                  _r3_talk("P1", ["B"], "B", "everyone", "우리 둘이 잘 지켜보자.", shot="close-up", action="James nods slowly."),
                  _r3_talk("P1", ["A"], "A", "C", "니키야...", shot="close-up", action="Beth looks at the stairs.")]
        s, left = _r3_clean(scenes, chars=fam, known=known)
        assert s["scenes"][2]["characters"] == ["A", "B"] and s["scenes"][2]["shot"] == "medium two-shot", act
        assert s["scenes"][3]["characters"] == ["B", "A"] and s["scenes"][3]["to_all"] is True, act
        assert s["scenes"][4]["characters"] == ["A"] and s["scenes"][4]["monologue"] is True, act
        assert any('Scene 4: "B" speaks to everyone' in x and 'add "A" ' in x for x in left) and not any('"C"' in x and "add" in x for x in left), left
    assert "unless they left or a scene says they are away" in video.DIALOGUE_EXTRA_DUO        # 구성안 규칙 글도
    # 이 장면 글이 그 사람이 없다고 적으면 이어 받지 않는다 (대사 장면이어도)
    for act, ko in (("Beth whispers to James while Niki sleeps upstairs.", "대사"), ("Beth smiles at James, with Niki at school.", "대사"),
                    ("Beth pours tea for James while Niki plays outside.", "대사"), ("Beth smiles at James.", "니키가 없는 식탁에서 베스가 말한다")):
        talk = dict(_r3_talk("P1", ["A", "B"], "A", "B", "조용하네요.", action=act), ko=ko)
        s, _l = _r3_clean([_R3_WIDE, talk], chars=fam, known=known)
        assert s["scenes"][1]["characters"] == ["A", "B"], (act, ko)
    s, _l = _r3_clean([_R3_WIDE, _r3_shot("P1", ["A", "B"], "medium two-shot", "Beth and James sit at the table.", "니키의 빈 의자", 6),
                       _r3_talk("P1", ["A", "B"], "A", "B", "조용하네요.")], chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["A", "B"]
    # 'alone' 문장이 화면에 없는 사람을 함께 적으면 그 사람은 그 자리에 있을 수 있다 (없음이 아니다)
    s, _l = _r3_clean([_R3_WIDE, _r3_shot("P1", ["C"], "medium shot", "Niki sits alone in the corner while her parents chat at the table.", "니키", 6),
                       _r3_talk("P1", ["C"], "C", "A", "엄마, 저 여기 있어요.", shot="close-up")], chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["C", "A", "B"] and s["scenes"][2]["monologue"] is False
    # 독백·말 없는 한 사람 장면의 'alone with her thoughts' 는 없음이 아니다 (그 자리 모두가 다음 대화 컷에 나온다)
    s, _l = _r3_clean([_R3_WIDE, _r3_shot("P1", ["C"], "close-up", "Niki stares at her plate, alone with her thoughts.", "니키", 5),
                       _r3_talk("P1", ["C"], "C", "A", "엄마, 저 다 먹었어요.", shot="close-up")], chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["C", "A", "B"] and s["scenes"][2]["monologue"] is False
    # 듣는 사람으로 적은 사람을 이 대사 장면 글이 없다고 적으면('with Niki at school') 그 자리의 누구에게도 하는 말이 아니다 (독백)
    s, left = _r3_clean([_R3_WIDE, _r3_talk("P1", ["A"], "A", "C", "니키야, 보고 싶다...", shot="close-up",
                                            action="Beth smiles at the empty chair, with Niki at school.")], chars=fam, known=known)
    assert s["scenes"][1]["characters"] == ["A"] and s["scenes"][1]["monologue"] is True
    assert any('Scene 2: "C" is not in the place at this moment' in x for x in left), left
    # 구성안을 쓰는 같은 LLM 차례: 검사가 위반을 내지 않고 다듬기도 두 사람 그대로 (예전에는 위반 없이 세 사람 그룹샷으로 바꿨다)
    raw = _r3_story(fam, [_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the breakfast table in the morning light.", "식탁", 8),
                          _r3_shot("P1", ["A", "B"], "medium two-shot", "Beth and James are alone in the kitchen now.", "부엌에 둘만 남았다", 6),
                          _r3_talk("P1", ["A", "B"], "A", "B", "그 애가 요즘 좀 힘든가 봐요.", action="Beth sighs and looks at James."),
                          _r3_talk("P1", ["B", "A"], "B", "A", "그러게 말이야.", action="James nods slowly.", cont=True),
                          _r3_talk("P1", ["A", "B"], "A", "B", "오늘 저녁에 얘기해 봐요.", action="Beth smiles.")])

    async def call(payload):
        return {"message": {"content": _json.dumps(raw, ensure_ascii=False)}}
    with duo_setting(True):
        story, left, rounds = _asyncio.run(video.write_dialogue_story(call, "베스와 제임스와 니키의 아침 대화", "", 32, known=known, duo=True))
    assert rounds == 1 and left == [], left
    assert [x["characters"] for x in story["scenes"][2:]] == [["A", "B"], ["B", "A"], ["A", "B"]]


def test_r4_common_leaving_forms_and_what_is_not_leaving():
    """4차 검토 3: 흔한 떠남('gets up and leaves'·'goes to school'·'sets off'·'disappears'·'closes the door behind her'·'인사하고 나간다'·'학교에
    간다'·'사라진다'·'등교한다')을 읽는다. 나뭇잎('covered in moss and leaves')·소유격('Niki's smile disappears'·'니키의 미소가 사라진다')·물건
    ('has left her bag')·바람('학교에 가고 싶어')은 떠남이 아니다"""
    fam, known = _r4_lib_fam()
    cast = video._cast_view({c["id"]: c for c in FAMILY["bible"]["characters"]}, scenes=FAMILY["scenes"],
                            places={p["id"]: p["look"] for p in FAMILY["bible"]["places"]})
    sc = lambda act, ko="자리": {"action": act, "shot": "medium shot", "ko": ko, "place": "P1"}
    for act, ko in (("Niki gets up and leaves.", "자리"), ("Niki picks up her bag and goes to school.", "자리"), ("Niki heads to school.", "자리"),
                    ("Niki sets off for school.", "자리"), ("Niki disappears down the hallway.", "자리"), ("Niki then leaves.", "자리"),
                    ("Niki leaves the table and goes to her room.", "자리"), ("Niki waves goodbye and closes the door behind her.", "자리"),
                    ("Niki runs to the front door and out.", "자리"), ("Niki has left for school.", "자리"), ("With a quick goodbye, Niki rushes out.", "자리"),
                    ("Niki stands up.", "니키가 인사하고 나간다"), ("Niki stands up.", "니키가 문을 열고 나간다"), ("Niki stands up.", "니키가 가방을 챙겨 나간다"),
                    ("Niki stands up.", "니키가 학교에 간다"), ("Niki stands up.", "니키가 사라진다"), ("Niki stands up.", "니키가 학교로 향한다"),
                    ("Niki stands up.", "니키가 등교한다"), ("Niki stands up.", "니키가 방으로 올라간다"), ("Niki stands up.", "니키가 학교로 뛰어간다"),
                    ("Niki stands up.", "니키가 친구 집에 놀러 간다")):
        assert video._leavers(sc(act, ko), ["C"], cast) == {"C"}, (act, ko)
    for act, ko in (("The path is covered in moss and leaves.", "자리"), ("Niki walks along a path covered in moss and leaves.", "자리"),
                    ("Niki's smile disappears.", "자리"), ("The sun disappears behind a cloud.", "자리"),
                    ("Niki has left her bag on the table.", "자리"), ("Niki has gone quiet.", "자리"), ("Niki sets off the alarm by accident.", "자리"),
                    ("Niki gets up and leaves the table.", "자리"), ("Niki comes back in and leaves the door open.", "자리"),
                    ("Niki comes in and closes the door behind her.", "자리"), ("Beth smiles as someone walks out.", "자리"),
                    ("Niki smiles.", "니키의 미소가 사라진다"), ("Niki smiles.", "니키 얼굴에서 미소가 사라진다"), ("Niki smiles.", "니키가 학교에 가고 싶어 한다"),
                    ("Niki smiles.", "니키가 한 걸음 앞으로 나간다"), ("Niki smiles.", "제임스가 이야기를 이어 나간다")):
        assert video._leavers(sc(act, ko), ["C"], cast) == set(), (act, ko)
    plain = video._cast_view({c["id"]: c for c in FAMILY["bible"]["characters"]}, places={"P1": FAMILY["bible"]["places"][0]["look"]})
    assert video._leavers(sc("With a quick goodbye, Niki rushes out."), ["C"], plain) == {"C"}
    assert video._leavers(sc("Beth looks up, Niki runs out."), ["A"], plain) == set()
    # 떠난 니키는 다음 대화 컷에 다시 앉지 않고, 니키에게 하는 말은 혼자 하는 말이다
    for act, ko in (("Niki gets up and leaves.", "니키가 일어난다"), ("Niki picks up her bag and goes to school.", "니키가 학교에 간다"),
                    ("Niki stands up with her school bag.", "니키가 인사하고 나간다"), ("Niki disappears down the hallway.", "니키가 사라진다")):
        s, left = _r3_clean([_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the breakfast table in the morning light.", "식탁", 8),
                             _r3_shot("P1", ["C"], "medium shot", act, ko, 6),
                             _r3_talk("P1", ["A", "B"], "A", "B", "그 애가 요즘 좀 힘든가 봐요.", action="Beth sighs and looks at James."),
                             _r3_talk("P1", ["A"], "A", "C", "도시락 가져가!", shot="close-up", action="Beth calls after her.")], chars=fam, known=known)
        assert s["scenes"][2]["characters"] == ["A", "B"] and s["scenes"][3]["characters"] == ["A"] and s["scenes"][3]["monologue"] is True, act
        assert not any('put "C"' in x for x in left), (act, left)


def test_r4_an_animal_in_the_place_is_spoken_to():
    """4차 검토 4: 그 자리에 있는 동물(앞 장면에 적혔고 떠나지 않은 강아지)에게 하는 말은 대화다 ([니키, 강아지]). 강아지가 떠난 뒤에 부르는 말은
    혼자 하는 말이다"""
    dog = {"id": "D", "look": "a small white puppy with floppy ears", "name_ko": "뭉치"}
    chars = [_R3_FAM[2], dog]
    s, left = _r3_clean([_r3_shot("P2", ["C", "D"], "wide shot", "Niki sits on the grass with her puppy.", "정원", 8),
                         _r3_talk("P2", ["C", "D"], "C", "D", "착하지, 우리 강아지.", shot="medium shot", action="Niki smiles down at the puppy."),
                         _r3_talk("P2", ["C"], "C", "D", "오늘은 산책 가자!", shot="close-up", action="Niki laughs.")], chars=chars)
    assert s["scenes"][1]["characters"] == ["C", "D"] and s["scenes"][2]["characters"] == ["C", "D"] and s["scenes"][2]["monologue"] is False
    assert any('Scene 3: the person spoken to must be in the shot: put "D"' in x for x in left) and not any("is not in the place" in x for x in left)
    s, left = _r3_clean([_r3_shot("P2", ["C", "D"], "wide shot", "Niki sits on the grass with her puppy.", "정원", 8),
                         _r3_shot("P2", ["C"], "medium shot", "The puppy runs off after a butterfly.", "강아지가 달아난다", 5),
                         _r3_talk("P2", ["C"], "C", "D", "뭉치야, 돌아와!", shot="close-up", action="Niki calls out.")], chars=chars)
    assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True


def test_r4_first_line_of_a_place_to_everyone_shows_everyone_there():
    """4차 검토 5: 장소 구간의 첫 장면이 모두에게 하는 말이면 뒤 장면에 이미 있던(들어오는 장면이 아닌) 사람이 그 자리 모두다. 구성안이 말하는
    사람만 적은 대화도 그렇다. 대화하는 사람들을 적은 컷(정원의 [베스, 니키])에는 나중 넓은 장면의 사람을 끌어오지 않는다"""
    s, left = _r3_clean([_r3_talk("P1", ["C"], "C", "everyone", "다녀왔습니다!", shot="medium shot", action="Niki waves at the table with a big smile."),
                         _r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the dinner table.", "저녁 식탁", 8),
                         _r3_talk("P1", ["A", "B", "C"], "A", "C", "어서 와, 배고프지?", shot="medium group shot", action="Beth smiles at Niki.")])
    assert s["scenes"][0]["characters"] == ["C", "A", "B"] and s["scenes"][0]["to_all"] is True and s["scenes"][0]["monologue"] is False
    assert any('Scene 1: "C" speaks to everyone in the place, so everyone there must be in the shot: add "A", "B"' in x for x in left), left
    s, _l = _r3_clean([_r3_talk("P1", ["C"], "C", "A", "다녀왔습니다!", shot="medium shot"),
                       _r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the dinner table.", "저녁 식탁", 8)])
    assert s["scenes"][0]["characters"] == ["C", "A", "B"]
    s, _l = _r3_clean([_r3_talk("P1", ["C"], "C", "A", "다녀왔습니다!", shot="medium shot"),
                       _r3_shot("P1", ["A", "C"], "medium two-shot", "Beth hugs Niki at the door.", "포옹", 6),
                       _r3_shot("P1", ["B"], "medium shot", "James reads the newspaper at the table.", "신문", 6)])
    assert s["scenes"][0]["characters"] == ["C", "A"]
    # 뒤 넓은 장면이 들어오는 장면이면 그 사람은 아직 없다 (혼자 하는 말)
    fam, known = _r4_lib_fam()
    s, _l = _r3_clean([_r3_talk("P1", ["C"], "C", "everyone", "아무도 없어요?", shot="medium shot"),
                       _r3_shot("P1", ["A", "B", "C"], "wide shot", "Beth and James walk into the kitchen.", "부모가 들어온다", 8)],
                      chars=fam, known=known)
    assert s["scenes"][0]["characters"] == ["C"] and s["scenes"][0]["monologue"] is True
    with duo_setting(True):
        assert video.clean_dialogue_story(_duo_pairs(), 48)["scenes"][3]["characters"] == ["A", "C"]


def test_r4_monologue_text_never_points_at_another_person_by_a_common_noun():
    """4차 검토 7: 독백 장면 글의 'the girl'·'the woman beside him'·'the two of them'·'his little girl'·'the kid' 도 다른 사람이다: 검사가 고쳐
    쓰게 하고 영상 글에서 뺀다. 말하는 사람 외형에 든 말(니키의 'girl')은 그 사람 자신이다"""
    for act in ("James glances at the girl and smiles to himself.", "James looks at the woman beside him and sighs.",
                "James looks at the two of them and sighs happily.", "James looks over at his little girl, thinking.", "James watches the kid and laughs softly."):
        raw = _r3_story(_R3_FAM, [_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the breakfast table.", "식탁", 8),
                                  _r3_talk("P1", ["A", "B", "C"], "A", "B", "오늘 날씨 좋네요.", shot="medium group shot", action="Beth smiles at James."),
                                  _r3_talk("P1", ["B"], "B", "nobody", "우리 딸이 벌써 이렇게 컸구나...", shot="close-up", action=act)])
        with duo_setting(True):
            left = video.dialogue_problems(raw, 20, True, exchange=True)
            assert any(x.startswith('Scene 3 is a monologue: its "action" must describe only the speaker') for x in left), (act, left)
            tp = video.talk_prompt(video.clean_dialogue_story(raw, 20, exchange=True), 2)
        assert act not in tp and "alone in the frame" in tp, (act, tp)
    cast = video._cast_view({c["id"]: c for c in _R3_FAM})
    assert video._other_mention("Niki smiles like a happy girl.", "C", cast) == "" and video._other_mention("Beth smiles at the girl.", "A", cast) == "girl"


def test_r4_offscreen_voice_written_as_talk_is_a_voice_over():
    """4차 검토 8: 문 밖에서 부르는 목소리를 talk 장면(characters 비움, 'calls from outside')으로 쓴 구성안은 목소리를 얹은 shot 으로 다듬는다:
    그 사람을 다락방에 그리지 않고(offscreen, scene_people), 대답하는 니키의 말은 혼자 하는 말이다. 검사는 kind shot 으로 고쳐 쓰게 한다.
    화면에 적히지 않은 사람의 목소리만 얹은 shot 도 그 사람을 그리지 않는다"""
    chars = [_R3_FAM[0], _R3_FAM[2]]
    s, left = _r3_clean([_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed by the round window.", "다락방의 니키", 8),
                         _r3_talk("P3", [], "A", "C", "니키야, 밥 먹어라!", shot="close-up of the attic door",
                                  action="The attic door stays still while Beth calls from outside."),
                         _r3_talk("P3", ["C"], "C", "A", "엄마, 조금만요!", shot="close-up", action="Niki looks toward the door.")], chars=chars)
    one = s["scenes"][1]
    assert one["kind"] == "shot" and one["speaker"] == "A" and one["line_ko"] == "니키야, 밥 먹어라!" and one["offscreen"] is True
    with duo_setting(True):
        assert video.scene_people(s, 1) == [] and video.talk_group(s, 1) is None
    assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True
    assert any('Scene 2: "A" is only heard off-screen' in x and '"kind": "shot"' in x for x in left), left
    assert not any("Scene 2 is a talk scene" in x or 'Scene 2: the person spoken to' in x for x in left), left
    assert not any('put "A"' in x for x in left), left
    assert any('"A" speaks only as a voice-over' in x for x in left), left          # 화면 밖 목소리는 얼굴이 보이는 말하기 장면이 아니다
    assert 'is always such a "shot", never a "talk" scene' in video.DIALOGUE_EXTRA_DUO       # 구성안 규칙 글도
    # 목소리만 얹은 shot (글에 화면 밖 말이 없어도): 말하는 사람을 그리지 않는다. 대사 컷의 말하는 사람은 지금처럼 넣는다
    s, _l = _r3_clean([_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed.", "다락방의 니키", 8),
                       _r3_shot("P3", [], "close-up of the attic door", "The attic door stays closed.", "닫힌 문", 5, speaker="A",
                                line="니키야, 밥 먹어라!", emotion="calling"),
                       _r3_talk("P3", [], "C", "nobody", "아, 귀찮아...", shot="close-up")], chars=chars)
    with duo_setting(True):
        assert s["scenes"][1]["offscreen"] is True and video.scene_people(s, 1) == [] and video.scene_people(s, 2) == ["C"]
    # 꺼짐(G0-9)은 예전 그대로: 대사 장면은 말하는 사람 혼자
    with duo_setting(False):
        raw = _r3_story(chars, [_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed.", "다락방의 니키", 8),
                                _r3_talk("P3", [], "A", "C", "니키야!", shot="close-up", action="Beth calls from outside."),
                                _r3_talk("P3", ["C"], "C", "A", "네!", shot="close-up")])
        so = video.clean_dialogue_story(raw, 19)
        assert so["scenes"][1]["kind"] == "talk" and so["scenes"][1]["characters"] == ["A"] and "offscreen" not in so["scenes"][1]


def test_r4_animal_speaker_carries_the_people_in_the_place():
    """4차 검토 G2·G5: 동물이 그 자리의 모두에게 하는 말은 그 자리 사람 모두가 나오는 대화다 (혼자 하는 말이 아니다), 검사도 그 사람들을 더하게
    한다. 동물이 한 사람에게 하는 말에도 그 자리의 다른 사람이 나온다. 영상 글의 샷 말은 사람이 함께 보이는 크기다"""
    fox = {"id": "F", "look": "a small red fox with a blue scarf", "name_ko": "여우", "voice": "A sly young Korean voice.", "voice_ko": "여우"}
    wide = _r3_shot("P1", ["A", "B", "C", "F"], "wide shot", "The family sits at the table; a small fox sits on a chair.", "식탁의 여우", 12)
    s, left = _r3_clean([wide, _r3_talk("P1", ["F"], "F", "everyone", "다들 도와줘!", shot="medium shot", action="The fox hops onto the table.")],
                        chars=_R3_FAM + [fox])
    assert s["scenes"][1]["characters"] == ["F", "A", "B", "C"] and s["scenes"][1]["to_all"] is True and s["scenes"][1]["monologue"] is False
    assert any('Scene 2: "F" speaks to everyone in the place, so everyone there must be in the shot: add "A", "B", "C"' in x for x in left), left
    s, _l = _r3_clean([wide, _r3_talk("P1", ["F", "C"], "F", "C", "니키야, 도와줘!", shot="medium two-shot", action="The fox looks up.")],
                      chars=_R3_FAM + [fox])
    assert s["scenes"][1]["characters"] == ["F", "C", "A", "B"] and s["scenes"][1]["shot"] == "medium group shot"
    with duo_setting(True):
        tp = video.talk_prompt(s, 1)
    assert tp.startswith("Medium group shot: a small red fox") and "The 3 people only listen" in tp and "Medium close-up" not in tp, tp
    s, _l = _r3_clean([_r3_shot("P2", ["C", "F"], "wide shot", "Niki meets a small fox in the garden.", "정원의 여우", 10),
                       _r3_talk("P2", ["F", "C"], "F", "C", "안녕, 니키!", shot="close-up", action="The fox tilts its head.")], chars=_R3_FAM + [fox])
    assert s["scenes"][1]["characters"] == ["F", "C"] and s["scenes"][1]["shot"] == "medium two-shot"
    with duo_setting(True):
        assert video.talk_prompt(s, 1).startswith("Medium two-shot: a small red fox")
    s, _l = _r3_clean([_r3_shot("P2", ["C", "F"], "wide shot", "Niki meets a small fox in the garden.", "정원의 여우", 10),
                       _r3_talk("P2", ["F"], "F", "everyone", "다들 안녕!", shot="medium shot", action="The fox waves its tail.")], chars=_R3_FAM + [fox])
    assert s["scenes"][1]["characters"] == ["F", "C"] and s["scenes"][1]["to_all"] is True and s["scenes"][1]["monologue"] is False


def test_r4_text_names_skip_object_names_after_an_article():
    """4차 검토 G4: 'Holding the Compass, Niki …' 에서 이야기의 물건 'the Compass' 를 니키 이름으로 배우지 않는다: 푸토르 독백의 'the Compass' 를
    다른 사람으로 잡지 않고 영상 글에서도 빼지 않는다"""
    chars = [{"id": "A", "look": "a twelve-year-old girl with two long black braids and a wine-red cape", "name_ko": "니키",
              "voice": "A bright girl.", "voice_ko": "아이"},
             {"id": "B", "look": "an old dwarf with a long white beard and a green hood", "name_ko": "푸토르", "voice": "An old man.", "voice_ko": "노인"}]
    for lead in ("Holding the Compass, Niki looks up at Putor.", "Compass in hand, Niki shows it to Putor."):
        sc = [_r3_shot("P2", ["A", "B"], "wide shot", "Niki and Putor walk along the forest path; the Strange Compass glows.", "숲길", 10),
              _r3_talk("P2", ["A", "B"], "A", "B", "푸토르, 이 나침반 좀 봐요!", action=lead),
              _r3_talk("P2", ["B", "A"], "B", "A", "허허, 바늘이 떨리는구나.", action="Putor leans closer and chuckles.", cont=True),
              _r3_talk("P2", ["B"], "B", "nobody", "이 나침반이 왜 지금...", shot="close-up", action="Putor frowns at the Compass, deep in thought.")]
        assert video._text_names(sc, {c["id"]: c for c in chars}) == {"A": {"Niki"}, "B": {"Putor"}}, lead
        data = _r3_story(chars, sc)
        with duo_setting(True):
            assert not any(x.startswith("Scene 4 is a monologue: its") for x in video.dialogue_problems(data, 30, True, exchange=True)), lead
            assert "Putor frowns at the Compass, deep in thought." in video.talk_prompt(video.clean_dialogue_story(data, 30, exchange=True), 3)


def test_r4_request_style_similes_activities_realism_cg_and_materials():
    """4차 검토 화풍: 비유 'like a …'·'look like a …' 는 화풍 요청이고, 'X로 …하며'·'sketches the herbs in pencil'·'plays a pixel art game' 같은
    활동은 화풍이 아니다. 사실적·리얼·실제 사람·realistic 은 실사 요청, 'CG 애니메이션'·'드림웍스' 는 3D 애니, '3D 클레이 애니메이션' 은 점토(custom),
    '디즈니 애니메이션처럼' 은 판단을 구성안에 맡긴다. '영화처럼'·'광고처럼'·'cinematic style' 은 화풍이 아니다"""
    from app import cast_lib
    lib = cast_lib.library()
    lib_style = lib[0]["style"]

    def final(text, planner):
        known = cast_lib.known_in(text)
        story = {"bible": {"characters": [{"id": e["id"], "name_ko": e["names"][0], "look": "x"} for e in known if not e.get("variant_of")],
                           "style": planner}}
        cast_lib.apply_to_story(story, known, f"{text} ", keep_style=bool(video.story_style(story)))
        return story["bible"]["style"]
    for text, planner, key in (("Niki and Putor talking in the forest, make it look like a watercolor painting", "soft watercolor painting with paper grain", "custom"),
                               ("Niki and Putor talking, like a claymation film", "claymation with visible fingerprints", "custom"),
                               ("Beth and James talking in the kitchen, like a Pixar movie", lib_style, "toon3d"),
                               ("Niki and Putor talking in the forest, like a Ghibli film", lib_style, "anime"),
                               ("Beth and James talking, like a real movie with real actors", "photorealistic cinematic live action", "photo"),
                               ("사실적인 느낌으로 베스와 제임스가 부엌에서 대화하는 영상", "photorealistic cinematic live action", "photo"),
                               ("리얼하게 베스와 제임스가 대화하는 영상 만들어 줘", "photorealistic cinematic live action", "photo"),
                               ("진짜 사람이 연기하는 것처럼 니키와 베스가 대화하는 영상", "photorealistic cinematic live action", "photo"),
                               ("A realistic video of Beth and James talking in the kitchen", "photorealistic cinematic live action", "photo"),
                               ("Beth and James talking in the kitchen, realistic style", "photorealistic cinematic live action", "photo"),
                               ("리얼한 영화처럼 제임스와 베스가 대화하는 영상", "realistic cinematic live-action film", "photo"),
                               ("CG 애니메이션 느낌으로 베스와 제임스가 대화하는 영상", "CG animated feature film look with soft shading", "toon3d"),
                               ("CGI 애니메이션처럼 니키와 푸토르가 대화하는 영상", "CGI animated feature film", "toon3d"),
                               ("드림웍스 애니메이션처럼 니키와 푸토르가 대화하는 영상", "DreamWorks-style 3D animated feature film", "toon3d"),
                               ("3D 클레이 애니메이션으로 니키와 푸토르가 대화하는 영상", "3D claymation with visible fingerprints", "custom"),
                               ("수묵 담채화로 니키와 푸토르가 산을 오르며 대화하는 영상", "ink wash painting with light colour", "custom"),
                               ("니키와 푸토르가 대화하는 영상, 픽셀 그래픽으로 만들어 줘", "pixel art with a limited palette", "custom")):
        f = final(text, planner)
        assert cast_lib.style_key(f) == key and (f == planner or cast_lib.style_key(planner) != key), (text, f)
    assert final("로우폴리 3D 애니메이션으로 니키와 푸토르가 대화하는 영상", lib_style) == "low poly style"
    for style in ("low poly 3D animation with flat shaded facets", "3D claymation with visible fingerprints", "Niki in a 3D felt puppet animation"):
        assert cast_lib.style_key(style) == "custom", style
    assert cast_lib.style_key("stylized 3D animated feature film with a storybook feel") == "toon3d"
    assert cast_lib.style_key("Pixar-style claymation figures") == "custom" and cast_lib.style_key("CG 애니메이션 느낌") == "toon3d"
    assert "smooth soft shading" not in video.style_keep({"bible": {"style": "low poly 3D animation with flat shaded facets"}})
    assert cast_lib.said_level("디즈니 애니메이션처럼 니키와 푸토르가 대화하는 영상") == "maybe"
    assert final("디즈니 애니메이션처럼 니키와 푸토르가 대화하는 영상", "stylized 3D animated feature film look") == "stylized 3D animated feature film look"
    # 활동·물건: 화풍 요청이 아니다 (라이브러리 화풍 그대로)
    for text in ("푸토르가 연필로 약초를 스케치하며 니키에게 설명하는 영상", "니키가 크레파스로 낙서하며 노는 영상", "니키가 색연필로 엄마 얼굴을 그려 주는 영상",
                 "니키가 크레용으로 그림 그리며 베스와 이야기하는 영상", "제임스가 목탄으로 니키 얼굴을 스케치하는 영상", "니키가 레고로 성을 쌓으며 제임스와 대화하는 영상",
                 "니키가 클레이로 놀면서 푸토르와 대화하는 영상", "니키가 픽셀로 된 게임을 하는 영상", "클레이로 만든 인형을 들고 있는 니키", "연필로 편지를 쓰는 니키",
                 "Putor sketches the herbs in pencil and explains them to Niki", "Niki draws Putor in crayon while they chat",
                 "Beth paints the garden in watercolor while James talks to her", "Niki plays a pixel art game while Putor watches",
                 "Niki shows James her pixel art drawing", "I like anime and Niki talks with Putor", "니키가 사실적인 그림을 그리는 영상",
                 "니키가 수채화로 그린 그림을 보여주는 영상", "픽셀로 된 게임을 하는 니키", "Niki and Putor talk about a pixel art game",
                 "Niki plays a claymation video game with Putor"):
        assert cast_lib.said_level(text) is None, (text, cast_lib._req_scan(text))
        assert final(text, lib_style) == lib_style and final(text, "pencil sketch style") == lib_style, text
    # 화풍 꼴은 그대로 화풍이다 ('X로 그린'·'X로 만들어 줘'·뒤 글이 주어로 시작·'…처럼')
    for text in ("니키와 푸토르를 수채화로 그려 줘", "니키와 푸토르가 대화하는 장면을 수채화로 만들어 줘", "수채화로 숲을 걷는 니키와 푸토르",
                 "니키와 푸토르를 클레이로 만든 인형처럼", "과슈로 그린", "Niki and Putor, looks like a storybook", "a pixel art drawing of Niki and Putor",
                 "니키와 푸토르가 나오는 픽셀로 된 영상"):
        assert cast_lib.said_level(text) == "sure", text
    # '영화처럼'·'광고처럼'·'cinematic style' 은 완성도 말: 모델이 쓴 영화 글이 라이브러리 화풍을 덮지 않는다
    for text in ("니키와 푸토르가 숲에서 대화하는 장면, 영화 같은 분위기로", "니키와 푸토르가 숲에서 대화하는 장면을 영화처럼 멋지게",
                 "Niki and Putor talk in the forest, cinematic style", "니키와 푸토르가 숲에서 대화하는 장면을 광고처럼 감각적으로",
                 "니키와 푸토르가 숲에서 대화하는 장면을 영화처럼 만들어 줘"):
        assert cast_lib.said_level(text) is None, text
        for plan in ("cinematic film look with warm golden light and shallow depth of field", "epic cinematic fantasy film look",
                     "moody cinematic look, teal and orange grading", "cinematic fantasy look"):
            assert final(text, plan) == lib_style, (text, plan)
    # 화풍일 수도 있는 말(maybe)에서 구성안의 custom 글은 화풍 말이 있어야 둔다
    assert final("니키와 푸토르, 디즈니처럼", "cinematic fantasy look") == lib_style
    assert final("니키와 푸토르, 디즈니처럼", "dreamy fantasy mood with soft glow") == lib_style       # custom 인데 화풍 말이 없다
    assert final("니키와 푸토르, 디즈니처럼", "classic hand-drawn animation") == "classic hand-drawn animation"


def test_r4_photo_film_looks_and_photo_like_custom():
    """4차 검토 화풍: 사진 말 없는 실사 영화 글('cinematic film look'·'35mm film look'·'documentary style')은 실사다(그림 말이 있으면 아니다).
    사진 쪽 custom 판정은 '아닌 것' 구절을 지우고 낱말 경계로 본다 ('not photorealistic'·'lively'·'olive'·'surrealist' 는 손그림에 '사진 아님'
    문장을 남긴다). 사진 쪽 custom(누아르) 기준 그림 변환은 '그린 그림'이 아니라 실사 영화의 한 컷이다"""
    from app import cast_lib
    for style in ("cinematic film look with dramatic lighting", "35mm film look with natural light", "documentary style handheld look",
                  "realistic, cinematic film look", "realistic style"):
        assert cast_lib.style_key(style) == "photo", style
    for style in ("fantasy concept art with a cinematic look", "soft watercolor storybook, cinematic look", "2D anime with a cinematic film look"):
        assert cast_lib.style_key(style) != "photo", style
    base = video.clean_dialogue_story(DLG_OK, 48)
    for style in ("soft watercolor storybook illustration, not photorealistic", "hand-painted watercolour picture-book illustration, not a photograph",
                  "whimsical storybook illustration with lively colours", "gouache painting with olive and ochre tones",
                  "surrealist oil painting with dreamlike colours", "claymation stop-motion look, never photorealistic"):
        st = dict(base, bible=dict(base["bible"], style=style))
        assert video.photo_like(st) is False and "never photographic realism" in video.scene_notes(st, 1), style
    noir = dict(base, bible=dict(base["bible"], style="black-and-white film noir with hard shadows"))
    assert video.photo_like(noir) is True and "never photographic realism" not in video.scene_notes(noir, 1)
    p = video.style_convert_prompt("custom", "a tall lean man", "black-and-white film noir with hard shadows")
    assert p.startswith("Turn this portrait into a live-action film still of the same person") and "drawn" not in p and "film noir" in p
    w = video.style_convert_prompt("custom", "girl", "soft watercolour storybook illustration")
    assert "drawn in this art style: soft watercolour storybook illustration" in w


# ── 2차 5차 검토 (11-25, 확인 3회차): 적힌 사람의 그 자리 여부·화면 밖 판정·독백 샷 글·없음 글·역할 이름·listener 값·같은 사람 두 모습·넷 이상
#    자리·독백의 they/them·등교·옆 장소·'that Minho' / 화풍: 주어 + 'X로'·명령·부정·가장 새 말·소재 명사·공손한 영어·실사 꼴·캐릭터·도트·흑백·2D ──
_R5_WIDE = _r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the breakfast table in the morning light.", "가족의 아침 식탁", 10)
_R5_LEAVE = _r3_shot("P1", ["C"], "medium shot", "Niki grabs her school bag and runs out of the kitchen door.", "니키가 가방을 챙겨 부엌문으로 뛰어나간다", 6)


def _r5_talk(place, chars, speaker, listener, line, shot="medium two-shot", action="They talk.", ko="대사", cont=False):
    return dict(_r3_talk(place, chars, speaker, listener, line, shot=shot, action=action, cont=cont), ko=ko)


def test_r5_listed_people_who_are_not_there_are_not_drawn():
    """5차 검토 1: 구성안이 characters 에 적은 사람도 그 자리에 없다는 증거(앞에서 떠남·없음 글·화면 밖 목소리·뒤에서 들어옴)가 있으면 대화 컷에
    그리지 않는다. 그 사람에게 하는 말은 독백이고, 다듬은 장면이 뒤 컷으로 번지지 않는다. 검사는 빼라고 하고 더하라고 하지 않는다.
    말하는 사람을 적었어도 그 사람이 그 자리에 없었고 글이 목소리가 화면 밖에서 온다고 분명히 말하면 목소리를 얹은 shot 이다"""
    import asyncio as _asyncio
    import json as _json
    fam, known = _r4_lib_fam()
    # (1) 방금 뛰어나간 니키를 적은 부르는 말 → 혼자 클로즈업, 뒤 대화 컷에도 니키가 다시 앉지 않는다
    scenes = [_R5_WIDE, _R5_LEAVE, _r5_talk("P1", ["A", "C"], "A", "C", "니키야, 도시락 가져가!", action="Beth calls after Niki."),
              _r5_talk("P1", ["B", "A"], "B", "A", "벌써 갔어요.", action="James chuckles."),
              _r5_talk("P1", ["A", "B"], "A", "B", "그러게요.", action="Beth laughs.", cont=True)]
    s, left = _r3_clean(scenes, chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["A"] and s["scenes"][2]["monologue"] is True and video.shot_rank(s["scenes"][2]["shot"]) <= 1
    assert s["scenes"][3]["characters"] == ["B", "A"] and s["scenes"][4]["characters"] == ["A", "B"]
    assert any('Scene 3: "C" is not in the place at this moment (left the place in scene 2)' in x and 'remove "C" from "characters"' in x
               for x in left), left
    to_all = scenes[:3] + [_r5_talk("P1", ["B", "A"], "B", "everyone", "자, 우리도 출근 준비하자!", action="James stands up and stretches.")]
    _s, left = _r3_clean(to_all, chars=fam, known=known)
    assert not any("add" in x and '"C"' in x for x in left), left
    # 구성안을 쓰는 같은 LLM 차례: 검사가 알리고, 다듬은 컷은 혼자 클로즈업
    raw = _r3_story(fam, scenes)

    async def call(payload):
        return {"message": {"content": _json.dumps(raw, ensure_ascii=False)}}
    with duo_setting(True):
        story, left, rounds = _asyncio.run(video.write_dialogue_story(call, "베스와 제임스와 니키의 아침 대화", "", 33, known=known, duo=True))
    assert story["scenes"][2]["characters"] == ["A"] and any('"C" is not in the place' in x for x in left), left
    # (2) 아직 내려오지 않은 니키를 적은 부르는 말 → 독백, 내려온 뒤에는 세 사람
    s, left = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 8),
                         _r5_talk("P1", ["A", "C"], "A", "C", "니키야, 내려와! 밥 먹자!", action="Beth calls toward the stairs."),
                         _r3_shot("P1", ["C"], "medium shot", "Niki comes down the stairs, yawning.", "니키가 하품하며 내려온다", 6),
                         _r5_talk("P1", ["C", "A", "B"], "C", "everyone", "좋은 아침!", shot="medium group shot", action="Niki waves sleepily.")],
                        chars=fam, known=known)
    assert s["scenes"][1]["characters"] == ["A"] and s["scenes"][1]["monologue"] is True and s["scenes"][3]["characters"] == ["C", "A", "B"]
    assert any('Scene 2: "C" is not in the place at this moment (comes into the place only in scene 3)' in x for x in left), left
    # 자리에 앉는 말만 있는 뒤 장면('sits down at the table'·'joins them at the table')은 적힌 사람을 없다고 볼 증거가 아니다 (그 방 안에서 앉을 수 있다)
    for act in ("Niki sits down at the table.", "Niki joins them at the table."):
        s, left = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 8),
                             _r5_talk("P1", ["A", "C"], "A", "C", "니키야, 이것 좀 먹어 봐.", action="Beth hands Niki a plate."),
                             _r3_shot("P1", ["C"], "medium shot", act, "니키", 6)], chars=fam, known=known)
        assert s["scenes"][1]["characters"] == ["A", "C", "B"] and s["scenes"][1]["monologue"] is False, act
        assert not any("is not in the place" in x for x in left), (act, left)
    # (3) 문 밖의 엄마를 적은 다락방 장면 → 목소리를 얹은 shot (엄마를 그리지 않는다), 대답은 독백. 엄마만 적어도 같다
    for chars, action in ((["A", "C"], "Beth calls through the closed door."), (["A"], "Beth calls from behind the closed attic door.")):
        s, left = _r3_clean([_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed by the round window, reading.", "다락방의 니키", 8),
                             _r5_talk("P3", chars, "A", "C", "니키야, 밥 먹어라!", action=action, ko="문 밖에서 엄마가 부른다"),
                             _r5_talk("P3", ["C", "A"], "C", "A", "엄마, 조금만요!", action="Niki calls toward the door.")], chars=fam, known=known)
        one = s["scenes"][1]
        with duo_setting(True):
            assert one["kind"] == "shot" and one["offscreen"] is True and "A" not in video.scene_people(s, 1), (chars, one)
        assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True, chars
        assert any('Scene 2: "A" is only heard off-screen' in x and 'without "A" in "characters"' in x for x in left), left
        assert not any('put "C"' in x or 'put "A"' in x for x in left), left
    # (4) 전화 속 할머니 목소리를 적은 장면 → 목소리 shot, 다음 컷은 부엌의 두 사람
    gran = {"id": "G", "look": "a kind grandmother in her seventies with silver hair in a bun and a lilac shawl", "name_ko": "할머니",
            "voice": "A gentle grandmother.", "voice_ko": "할머니"}
    s, left = _r3_clean([_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 8),
                         _r5_talk("P1", ["G"], "G", "B", "아들, 니키 생일 잊지 마라!", shot="close-up", action="Grandma's voice comes over the phone.",
                                  ko="전화 속 할머니 목소리"),
                         _r5_talk("P1", ["B", "A"], "B", "A", "어머니가 니키 생일 잊지 말래요.", action="James smiles at Beth.")], chars=fam + [gran], known=known)
    assert s["scenes"][1]["kind"] == "shot" and s["scenes"][2]["characters"] == ["B", "A"]
    assert any('"G" is only heard off-screen' in x for x in left) and not any('put "B"' in x for x in left), left
    # (5) '둘만 남았다' 뒤에 니키를 적은 대화 컷 → 두 사람
    s, left = _r3_clean([_R5_WIDE, _r3_shot("P1", ["A", "B"], "medium two-shot", "Beth and James are alone in the kitchen now.", "부엌에 둘만 남았다", 6),
                         _r5_talk("P1", ["A", "B", "C"], "A", "B", "그 애가 요즘 좀 힘든가 봐요.", shot="medium group shot", action="Beth sighs."),
                         _r5_talk("P1", ["B", "A"], "B", "A", "그러게 말이야.", action="James nods.")], chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["A", "B"] and s["scenes"][3]["characters"] == ["B", "A"]
    assert any('Scene 3: "C" is not in the place at this moment (scene 2 says they are not there)' in x for x in left), left
    # 바르게 적은 컷은 그대로이고, 화면 속 사람의 통화('talks on the phone')는 목소리 shot 이 아니라 혼자 클로즈업이다
    s, left = _r3_clean([_R5_WIDE, _r5_talk("P1", ["A", "B", "C"], "A", "B", "여보, 오늘 니키 생일이에요.", shot="medium group shot"),
                         _r5_talk("P1", ["C", "A"], "C", "A", "엄마, 고마워요!"),
                         _r5_talk("P1", ["A"], "A", "nobody", "네, 어머니. 오늘 저녁에 갈게요.", shot="close-up", action="Beth talks on the phone, smiling.")],
                        chars=fam, known=known)
    assert s["scenes"][1]["characters"] == ["A", "B", "C"] and s["scenes"][2]["characters"] == ["C", "A", "B"]
    assert s["scenes"][3]["kind"] == "talk" and s["scenes"][3]["characters"] == ["A"] and not any("not in the place" in x for x in left), left
    cast = video._cast_view({c["id"]: c for c in fam}, known, [], {p["id"]: p["look"] for p in FAMILY["bible"]["places"]})
    assert video._listed_gone([_R5_WIDE, _R5_LEAVE, _r5_talk("P1", ["A", "C"], "A", "C", "니키야!")], 2, "C", cast) == ("left", 1)
    assert video._listed_gone([_R5_WIDE, _r5_talk("P1", ["A", "C"], "A", "C", "니키야!")], 1, "C", cast) == ("", -1)


def test_r5_offscreen_voice_needs_a_speaking_word():
    """5차 검토 2: 화면 밖 판정은 말하는 목소리 말과 같은 마디에 있을 때만 (sound 의 'birds chirping from outside'·'rain can be heard' 는 배경
    소리라, 말하는 사람을 빠뜨린 보통 대사 장면은 지금처럼 말하는 사람을 넣은 대화 컷이다)"""
    fam, known = _r4_lib_fam()
    for chars, sound in ((["C"], "birds chirping from outside, a kettle"), (["C"], "soft rain can be heard on the window"),
                         ([], "morning birdsong from outside")):
        t = _r5_talk("P1", chars, "A", "C", "니키야, 오늘 생일 축하해!", action="Beth smiles warmly at Niki.")
        t["sound"] = sound
        s, left = _r3_clean([_R5_WIDE, t, _r5_talk("P1", ["C", "A"], "C", "A", "고마워요, 엄마!")], chars=fam, known=known)
        assert s["scenes"][1]["kind"] == "talk" and s["scenes"][1]["characters"][:2] == ["A", "C"], (sound, s["scenes"][1])
        assert not any("only heard off-screen" in x for x in left), (sound, left)
    off = lambda act, ko="대사", sound="soft room tone": video._offscreen_voice(dict(_r5_talk("P3", ["C"], "A", "C", "니키야!", action=act, ko=ko), sound=sound))
    assert off("The attic door stays still while Beth calls from outside.") and off("Niki looks up as her mother's voice comes from downstairs.")
    assert off("From behind the door, Beth calls softly.") and off("Niki listens.", "문 밖에서 엄마가 부른다")
    assert off("Niki reads a book.", sound="a muffled voice calls from behind the door") and off("James listens to Grandma on the phone.")
    assert not off("Niki smiles as birds chirp outside the window.", "창밖에서 새가 지저귄다")
    assert not off("Beth smiles warmly at Niki.", "밖에서 빗소리가 들린다", "rain can be heard on the window")
    assert not off("Rain falls outside the window, Beth says softly.")      # 앞 마디의 배경 소리는 말하는 목소리와 다른 마디다
    assert not video._voice_off_text({"action": "Beth talks on the phone, smiling.", "shot": "close-up", "ko": "통화"}, strict=True)
    assert video._voice_off_text({"action": "Grandma's voice comes over the phone.", "shot": "close-up", "ko": ""}, strict=True)


def test_r5_monologue_shot_frames_only_the_speaker():
    """5차 검토 3: 독백 컷의 샷 글이 다른 사람을 가리키면('with Beth and James out of focus behind her'·"over her father's shoulder") 크기 말과
    말하는 사람까지만 남긴다. 검사도 고쳐 쓰게 하고, 혼자 대사 영상 글·구도 그림 글·기준 그림 고치기 글에 다른 사람 이름이 없다"""
    fam, known = _r4_lib_fam()
    g1 = _r5_talk("P1", ["A", "B", "C"], "A", "B", "여보, 오늘 니키 생일이에요.", shot="medium group shot", action="Beth leans toward James.")
    for shot, want in (("close-up of Niki, her parents softly blurred in the background", "close-up of Niki"),
                       ("close-up of Niki with Beth and James out of focus behind her", "close-up of Niki"),
                       ("medium close-up of Niki over her father's shoulder", "medium close-up of Niki"),
                       ("close-up of Niki, the family chatting behind her", "close-up of Niki"),
                       ("close-up of Niki as her mother watches from the side", "close-up of Niki"),
                       ("over-the-shoulder close-up of Niki", "close-up")):
        scenes = [_R5_WIDE, g1, _r5_talk("P1", ["C"], "C", "nobody", "다들 기억하고 있었구나...", shot=shot, action="Niki looks down at her plate, thinking.")]
        s, left = _r3_clean(scenes, chars=fam, known=known)
        assert s["scenes"][2]["shot"] == want, (shot, s["scenes"][2]["shot"])
        assert any(x.startswith('Scene 3 is a monologue: its "shot" must frame only the speaker') for x in left), (shot, left)
        with duo_setting(True):
            texts = [video.talk_prompt(s, 2), video.layout_prompt(s, 2, video.layout_slots(s, 2, video.scene_people(s, 2))), video.cast_edit_prompt(s, 2)]
        assert not any(w in x for x in texts for w in ("Beth", "James", "parents", "father", "mother", "family", "over-the-shoulder")), (shot, texts)
    s, left = _r3_clean([_R5_WIDE, g1, _r5_talk("P1", ["C"], "C", "nobody", "다들 기억하고 있었구나...", shot="close-up of Niki")], chars=fam, known=known)
    assert s["scenes"][2]["shot"] == "close-up of Niki" and not any('its "shot"' in x for x in left)
    assert 'Its "shot" frames only the speaker too' in video.DIALOGUE_EXTRA_DUO


def test_r5_more_absence_forms():
    """5차 검토 4: '부모님만 남은 식탁'·'엄마 아빠만 남았다'·'Mom and Dad are alone'·'Only Beth and James remain'·'Now it is just Beth and James'·
    'Only the parents remain'·'The parents are left by themselves'·"Niki's seat is now empty"·'니키의 자리가 비었다' 뒤에는 니키가 다시 앉지 않는다.
    화면 사람을 부르는 부모·엄마·아빠는 화면에 없는 사람이 아니다. 화면에 없는 부모를 함께 적은 'alone' 은 여전히 없음이 아니다"""
    fam, known = _r4_lib_fam()
    for act, ko in (("Beth and James sit quietly at the table.", "부모님만 남은 식탁"), ("Beth and James sit quietly at the table.", "엄마 아빠만 남았다"),
                    ("Mom and Dad are alone at the table now.", "식탁의 두 사람"), ("Only Beth and James remain at the table.", "식탁의 두 사람"),
                    ("Now it is just Beth and James at the table.", "식탁의 두 사람"), ("Only the parents remain at the table.", "식탁의 두 사람"),
                    ("The parents are left by themselves at the table.", "식탁의 두 사람"), ("Niki's seat is now empty.", "니키의 자리가 비었다"),
                    ("Beth and James sit at the table.", "니키 자리가 비어 있다")):
        s, _l = _r3_clean([_R5_WIDE, _r3_shot("P1", ["A", "B"], "medium two-shot", act, ko, 6),
                           _r5_talk("P1", ["A", "B"], "A", "B", "그 애가 요즘 좀 힘든가 봐요.", action="Beth sighs.")], chars=fam, known=known)
        assert s["scenes"][2]["characters"] == ["A", "B"], (act, ko, s["scenes"][2]["characters"])
    s, _l = _r3_clean([_R5_WIDE, _r3_shot("P1", ["C"], "medium shot", "Niki sits alone in the corner while her parents chat at the table.", "니키", 6),
                       _r5_talk("P1", ["C"], "C", "A", "엄마, 저 여기 있어요.", shot="close-up")], chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["C", "A", "B"]
    s, _l = _r3_clean([_R5_WIDE, _r3_shot("P1", ["A", "B"], "medium two-shot", "Only crumbs remain on the table.", "식탁", 6),
                       _r5_talk("P1", ["A", "B"], "A", "B", "니키는 벌써 다 먹었네.")], chars=fam, known=known)
    assert s["scenes"][2]["characters"] == ["A", "B", "C"]          # 이름이 아닌 'Only crumbs remain' 은 없음이 아니다


def test_r5_role_named_people_leave_by_their_english_role_word():
    """5차 검토 5: 한글 이름이 역할 말인 인물('엄마'·'할머니')은 영어 장면 글의 'Mom'·'Grandma' 가 그 사람이다 (그 역할의 인물이 하나일 때만)"""
    mom = {"id": "M", "look": "a warm woman around forty with a short bob and a green apron", "name_ko": "엄마", "voice": "v", "voice_ko": "v"}
    kid = {"id": "K", "look": "a nine-year-old boy with short black hair and a yellow t-shirt", "name_ko": "민준", "voice": "v", "voice_ko": "v"}
    gran = {"id": "G", "look": "a kind grandmother in her seventies with silver hair in a bun", "name_ko": "할머니", "voice": "v", "voice_ko": "v"}
    for act, ko, pair, want in (("Grandma waves goodbye and leaves.", "할머니의 작별 인사", ("M", "K"), ["M", "K"]),
                                ("Grandma kisses Minjun's head and walks out the front door.", "할머니가 손자에게 인사한다", ("M", "K"), ["M", "K"]),
                                ("Mom grabs her coat and hurries out the door.", "엄마의 바쁜 아침", ("G", "K"), ["G", "K"])):
        who = "G" if act.startswith("Grandma") else "M"
        s, _l = _r3_clean([_r3_shot("P1", ["G", "M", "K"], "wide shot", "Grandma, Mom and Minjun sit at the breakfast table.", "아침 식탁", 8),
                           _r3_shot("P1", [who], "medium shot", act, ko, 6),
                           _r5_talk("P1", list(pair), pair[0], pair[1], "이제 우리 둘이네.", action="They smile.")], chars=[mom, kid, gran])
        assert s["scenes"][2]["characters"] == want, (act, s["scenes"][2]["characters"])
    cast = video._cast_view({c["id"]: c for c in (mom, kid, gran)})
    assert cast["names"]["G"].search("Grandma waves") and cast["names"]["M"].search("Mom smiles") and not cast["names"]["K"].search("Mom")
    two = video._cast_view({"M": mom, "M2": dict(mom, id="M2")})
    assert not two["names"]["M"].search("Mom smiles")                # 엄마가 둘이면 'Mom' 이 누구인지 모른다


def test_r5_listener_values_and_monologue_text():
    """5차 검토 6: listener 값 가운데 어디에 있어도 독백·통화인 말('internal monologue'·'thinking to herself'·'no listener'·'니키 자신'·'-'·
    'Grandma (on the phone)'·'할머니(전화)')은 독백이다. 모르는 값인데 장면 글이 'to herself'·'on the phone' 이면 앞뒤 대사로 추정하지 않는다"""
    fam, known = _r4_lib_fam()
    g1 = _r5_talk("P1", ["A", "B", "C"], "A", "B", "여보, 오늘 니키 생일이에요.", shot="medium group shot", action="Beth smiles.")
    for val in ("internal monologue", "inner thoughts", "thinking to herself", "speaking to herself", "-", "—", "no listener", "니키 자신",
                "her diary", "the mirror"):
        s, _l = _r3_clean([_R5_WIDE, g1, _r5_talk("P1", ["C"], "C", val, "다들 기억하고 있었구나...", shot="close-up",
                                                  action="Niki smiles to herself, eyes on her plate."),
                           _r5_talk("P1", ["B", "A"], "B", "A", "자, 먹자.")], chars=fam, known=known)
        assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True, (val, s["scenes"][2]["characters"])
    for val in ("Grandma (on the phone)", "her mother on the phone", "Grandma", "할머니(전화)"):
        s, _l = _r3_clean([_R5_WIDE, g1, _r5_talk("P1", ["B"], "B", val, "네, 어머니. 곧 갈게요.", shot="close-up", action="James talks on the phone, smiling."),
                           _r5_talk("P1", ["A", "B"], "A", "B", "누구예요?")], chars=fam, known=known)
        assert s["scenes"][2]["characters"] == ["B"] and s["scenes"][2]["monologue"] is True, val
    # 모르는 값이어도 장면 글에 독백 말이 없으면 지금처럼 앞뒤 대사로 추정한다 (G0-6 b)
    s, _l = _r3_clean([_R5_WIDE, g1, _r5_talk("P1", ["B"], "B", "Zed", "그래, 좋아.", shot="close-up", action="James nods."),
                       _r5_talk("P1", ["A", "B"], "A", "B", "그럼 됐어요.")], chars=fam, known=known)
    assert s["scenes"][2]["monologue"] is False and s["scenes"][2]["characters"][:2] == ["B", "A"]


def test_r5_two_looks_of_one_person_are_not_each_others_listener():
    """5차 검토 7: 같은 사람의 두 모습(제임스 ↔ 안경 쓴 제임스)이 이어 말하면 앞 줄의 화자를 듣는 사람으로 고르지 않는다"""
    from app import cast_lib
    kn = cast_lib.known_in("베스와 제임스와 니키")
    if not any(x["id"] == "james_glasses" for x in kn):
        return
    chars = [{"id": e["id"], "look": e["look"], "name_ko": e["names"][0], "voice": "v", "voice_ko": "v"} for e in kn
             if e["id"] in ("beth", "james", "james_glasses", "niki")]
    s, left = _r3_clean([_r3_shot("P1", ["beth", "james", "niki"], "wide shot", "The family sits at the table.", "식탁", 8),
                         _r5_talk("P1", ["james", "beth", "niki"], "james", "beth", "오늘 신문에 재밌는 기사가 났어.", shot="medium group shot"),
                         _r5_talk("P1", ["james_glasses"], "james_glasses", "", "음... 여기 보자.", shot="close-up",
                                  action="James reads the newspaper through his glasses."),
                         _r5_talk("P1", ["beth", "james_glasses"], "beth", "james_glasses", "뭔데요?")], chars=chars, known=kn)
    x = s["scenes"][2]
    assert x["listener"] not in ("james", "james_glasses") and "james" not in x["characters"], x
    assert not any('the person spoken to, "james_glasses", must come second' in y for y in left), left


def test_r5_four_people_wide_scenes_follow_the_seat_map():
    """5차 검토 8: 넷 이상이 나오는 넓은 장면도 같은 장소 구간의 자리 지도를 따른다 (적힌 차례가 달라도 좌우가 뒤집히지 않는다, 180도 규칙)"""
    gran = {"id": "G", "look": "a kind grandmother in her seventies with silver hair in a bun and a lilac shawl", "name_ko": "할머니",
            "voice": "v", "voice_ko": "v"}
    s, _l = _r3_clean([_R5_WIDE, _r5_talk("P1", ["A", "B", "C"], "A", "B", "여보, 커피 줄까요?", shot="medium group shot"),
                       _r3_shot("P1", ["G"], "medium shot", "Grandma walks into the kitchen with a birthday cake.", "할머니가 들어온다", 6),
                       _r5_talk("P1", ["G", "C"], "G", "C", "우리 니키, 생일 축하한다!"), _r5_talk("P1", ["C", "G"], "C", "G", "할머니!", cont=True),
                       _r3_shot("P1", ["A", "B", "C", "G"], "wide shot", "The whole family gathers around the table.", "온 가족", 8),
                       _r3_shot("P1", ["G", "A", "C", "B"], "wide shot", "Everyone laughs together at the table.", "웃음", 8)], chars=_R3_FAM + [gran])
    with duo_setting(True):
        orders = {k: [x["id"] for x in video.layout_slots(s, k, video.scene_people(s, k))] for k in range(len(s["scenes"]))
                  if len(video.scene_people(s, k)) >= 2}
    side = {}
    for k, order in orders.items():
        for a in range(len(order)):
            for b in range(a + 1, len(order)):
                pair = tuple(sorted((order[a], order[b])))
                assert side.setdefault(pair, (order[a], order[b])) == (order[a], order[b]), (k, order, side[pair])
    assert orders[5] == orders[6] and len(orders[5]) == 4
    with duo_setting(False):
        off = video.clean_dialogue_story(_r3_story(_R3_FAM + [gran], [_r3_shot("P1", ["G", "A", "C", "B"], "wide shot", "Everyone laughs.", "웃음", 8),
                                                                      _r5_talk("P1", ["A"], "A", "", "생일 축하해!", shot="close-up")]), 14)
        assert [x["id"] for x in video.layout_slots(off, 0, video.scene_people(off, 0))] == ["G", "A", "C", "B"]     # 꺼짐은 적힌 차례 그대로


def test_r5_monologue_they_and_them():
    """5차 검토 9: 독백 장면 글의 사람 동작 they·them('watches them laugh'·'glances at them'·'While they chat')·'the two adults'·'from one to the
    other' 는 다른 사람이다. 물건의 them('smells them'·'looks at them')은 아니다"""
    fam, known = _r4_lib_fam()
    g1 = _r5_talk("P1", ["A", "B", "C"], "A", "B", "여보, 오늘 니키 생일이에요.", shot="medium group shot")
    for act in ("Niki watches them laugh and smiles quietly to herself.", "Niki glances at them, then down at her plate.",
                "Niki looks up at the two adults and smiles to herself.", "Niki looks from one to the other, thinking.",
                "While they chat, Niki whispers to herself."):
        s, left = _r3_clean([_R5_WIDE, g1, _r5_talk("P1", ["C"], "C", "nobody", "다들 기억하고 있었구나...", shot="close-up", action=act)],
                            chars=fam, known=known)
        assert any(x.startswith('Scene 3 is a monologue: its "action"') for x in left), (act, left)
        with duo_setting(True):
            assert act not in video.talk_prompt(s, 2), act
    for act in ("Niki holds the flowers and smells them.", "Niki picks up her crayons and looks at them, smiling."):
        _s, left = _r3_clean([_R5_WIDE, g1, _r5_talk("P1", ["C"], "C", "nobody", "예쁘다...", shot="close-up", action=act)], chars=fam, known=known)
        assert not any('its "action"' in x for x in left), (act, left)


def test_r5_person_speaking_to_an_animal_in_a_cleaned_story():
    """5차 검토 10: 다듬은 이야기에서 사람이 그 자리의 강아지에게 말하는 컷은 사람과 강아지를 함께 쓴 영상 글이다 ('화면에 혼자' 가 아니다)"""
    dog = {"id": "D", "look": "a small white puppy with floppy ears", "name_ko": "뭉치"}
    s, _l = _r3_clean([_r3_shot("P2", ["C", "D"], "wide shot", "Niki sits on the grass with her puppy.", "정원", 8),
                       _r5_talk("P2", ["C", "D"], "C", "D", "착하지, 우리 뭉치.", shot="close-up", action="Niki smiles down at the puppy.")],
                      chars=[_R3_FAM[2], dog])
    with duo_setting(True):
        tp = video.talk_prompt(s, 1)
    assert tp.startswith("Medium shot: a twelve-year-old girl") and "Also in the frame: a small white puppy with floppy ears." in tp, tp
    assert "alone in the frame" not in tp and "Niki smiles down at the puppy." in tp and "speaks to the small white puppy" in tp


def test_r5_school_and_work_words_and_neighbouring_places():
    """5차 검토 20·21: '등교 준비를 하는'·'외출복을 입은' 은 떠남이 아니고 '하교한'·'퇴근한' 은 집에 들어옴이다 ('등교한다'·'출근했다'·'외출 중'
    은 떠남). 장소 외형의 옆 장소('opens onto the garden'·'next to the barn'·'behind the house')는 지금 장소가 아니라 그쪽으로 나가면 떠남이다"""
    fam, known = _r4_lib_fam()
    for act, ko, sp, lis, line in (("Niki packs her schoolbag at the table.", "등교 준비를 하는 니키", "A", "C", "니키야, 도시락 챙겼니?"),
                                   ("Niki puts her bag down by the kitchen door.", "하교한 니키가 부엌 문 옆에 가방을 내려놓는다", "A", "C", "왔니? 손 씻고 와."),
                                   ("Beth looks in the mirror.", "외출복을 입은 베스가 거울을 본다", "B", "A", "여보, 어디 가?")):
        who = lis
        s, left = _r3_clean([_R5_WIDE, _r3_shot("P1", [who], "medium shot", act, ko, 6), _r5_talk("P1", [sp], sp, lis, line, shot="close-up")],
                            chars=fam, known=known)
        assert s["scenes"][2]["monologue"] is False and lis in s["scenes"][2]["characters"], (ko, s["scenes"][2]["characters"])
        assert not any("is not in the place" in x for x in left), (ko, left)
    cast = video._cast_view({c["id"]: c for c in fam}, known, [], {p["id"]: p["look"] for p in FAMILY["bible"]["places"]})
    mv = lambda ko, who: video._moves({"action": "They sit.", "shot": "medium shot", "ko": ko, "place": "P1"}, [who], cast)
    assert mv("니키가 등교 준비를 한다", "C") == {} and mv("외출복을 입은 베스가 거울을 본다", "A") == {}
    assert mv("퇴근한 제임스가 소파에 앉는다", "B") == {"B": ["enter"]} and mv("니키가 등교한다", "C") == {"C": ["leave"]}
    assert mv("제임스가 출근했다", "B") == {"B": ["leave"]} and mv("엄마는 외출 중이다", "A") == {"A": ["leave"]}
    for look, act in (("a bright cottage kitchen that opens onto the garden", "Niki runs out into the garden."),
                      ("a farmhouse kitchen next to the barn", "Niki runs out to the barn."),
                      ("a bright cottage kitchen with French doors that open onto the garden", "Niki dashes out into the garden."),
                      ("a small garden behind the house with an apple tree", "Niki runs into the house."),
                      ("a cottage garden next to the kitchen door", "Niki runs into the kitchen.")):
        story = _r3_story(fam, [_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family is together.", "가족", 8),
                                _r3_shot("P1", ["C"], "medium shot", act, "니키", 6),
                                _r5_talk("P1", ["A", "B"], "A", "B", "여보, 니키가 요즘 좀 이상해요.")])
        story["bible"]["places"] = [{"id": "P1", "look": look}]
        with duo_setting(True):
            s = video.clean_dialogue_story(story, 20, exchange=True, known=known)
        assert s["scenes"][2]["characters"] == ["A", "B"], (look, act, s["scenes"][2]["characters"])
    assert video._is_here(["apple", "tree"], video._place_words("a small garden behind the house with an apple tree")) is True
    assert video._is_here(["garden"], video._place_words("a bright cottage kitchen that opens onto the garden")) is False


def test_r5_text_names_after_that_and_mutant_inputs():
    """5차 검토 25·26: 'Suji realizes that Minho is upset' 의 that 은 관사가 아니라 Minho 를 이름으로 배운다 (떠난 민호를 다음 컷에 넣지 않는다).
    4차 고침의 살아남은 변이 다섯이 바꾸는 입력을 단언으로 둔다"""
    from app import cast_lib
    suji = {"id": "A", "look": "a young woman with a ponytail and a denim jacket", "name_ko": "수지", "voice": "v", "voice_ko": "v"}
    minho = {"id": "B", "look": "a young man with curly hair and a green hoodie", "name_ko": "민호", "voice": "v", "voice_ko": "v"}
    scenes = [_r3_shot("P1", ["A", "B"], "medium two-shot", "Suji and Minho sit at the kitchen table.", "식탁", 6),
              _r5_talk("P1", ["A", "B"], "A", "B", "괜찮아?", action="Suji smiles. Suji realizes that Minho is upset."),
              _r5_talk("P1", ["B", "A"], "B", "A", "응, 괜찮아.", action="Minho nods."),
              _r3_shot("P1", ["A"], "close-up", "Suji watches as Minho walks out of the kitchen.", "민호가 나간다", 5),
              _r5_talk("P1", ["A"], "A", "everyone", "아, 정말...", shot="close-up", action="Suji sighs.")]
    assert video._text_names(scenes, {"A": suji, "B": minho}).get("B") == {"Minho"}
    s, _l = _r3_clean(scenes, chars=[suji, minho])
    assert s["scenes"][4]["characters"] == ["A"] and s["scenes"][4]["monologue"] is True
    fam, known = _r4_lib_fam()
    cast = video._cast_view({c["id"]: c for c in fam}, known, [], {"P1": FAMILY["bible"]["places"][0]["look"]})
    sc = lambda act: {"seconds": 6, "place": "P1", "characters": ["C"], "continues": False, "shot": "medium shot", "action": act, "sound": "",
                      "ko": "장면", "kind": "shot", "speaker": "", "listener": "", "line_ko": "", "emotion": ""}
    assert video._moves(sc("Niki runs into her mother's arms."), ["C"], cast) == {"C": ["enter"]}           # M1: 목적지를 모르는 들어옴
    assert video._absent(sc("Niki feels alone at the crowded table."), ["C"], cast) == set()                 # M4: 'feels alone'
    assert cast_lib.said_level("Niki draws a picture of Putor in crayon while they chat") is None            # M6: 낱말 다섯까지
    assert cast_lib.keeps_said_style("니키와 푸토르, 디즈니처럼", "soft watercolour storybook look") is True      # M10: maybe + custom 재료 말
    s, _l = _r3_clean([_r5_talk("P1", ["A"], "A", "B", "여보, 커피 마실래요?", shot="close-up"),
                       _r3_shot("P1", ["A"], "medium shot", "Beth watches as Niki runs out the door.", "배웅", 6),
                       _r5_talk("P1", ["B", "A"], "B", "A", "좋지.", shot="close-up")], chars=fam, known=known)
    assert s["scenes"][0]["characters"] == ["A", "B"]                                                         # M3: 뒤에서 떠나는 사람


def test_r5_request_style_subject_forms_commands_negation_and_latest_line():
    """5차 검토 화풍 11·12·14·20·23: '니키와 푸토르가 수채화로 대화하는 영상'·'베스와 제임스가 실사로 나오는 영상' 처럼 주어 뒤 'X로' 도 첫 동작이
    도구를 쓰는 말이 아니면 화풍이다. 'Draw …'·'Can you draw …'·'Can you make …'·"I'd like a … video" 는 요청 자체다. 부정한 말('실사 말고'·
    'not anime')은 빼고, 고치기 글(앞 요청 + 이번 말)은 화풍을 말한 가장 나중 줄을 본다. 활동('푸토르가 연필로 약초를 스케치하며')은 그대로"""
    from app import cast_lib
    lib_style = cast_lib.library()[0]["style"]
    for text, key in (("니키와 푸토르가 수채화로 대화하는 영상", "custom"), ("베스와 제임스가 애니메이션으로 대화하는 영상", "anime"),
                      ("베스와 제임스가 실사로 나오는 영상", "photo"), ("니키와 베스가 반실사로 대화하는 영상", "semi"),
                      ("니키와 푸토르가 3D로 대화하는 1분 영상", "toon3d"), ("니키가 웹툰으로 나오는 짧은 영상", "anime"), ("니키가 만화로 그려진 영상", "anime"),
                      ("니키와 푸토르가 동화책 그림으로 대화하는 영상", "custom"), ("니키가 수채화로 나와서 푸토르와 이야기하는 영상", "custom"),
                      ("베스와 제임스가 실사로 나와 요리를 만드는 영상", "photo"),
                      ("Draw Niki and Putor talking in watercolor", "custom"), ("Can you draw Niki and Putor in watercolor?", "custom"),
                      ("Sketch Niki and Putor talking in pencil", "custom"), ("Draw Niki and Putor talking in 3D animation", "toon3d"),
                      ("Paint the scene in watercolor: Niki and Putor talk in the forest", "custom"),
                      ("Can you make an anime video of Niki and Putor talking?", "anime"), ("I want to make an anime video of Niki and Putor talking", "anime"),
                      ("Could you make a watercolor video of Niki and Putor talking in the forest?", "custom"),
                      ("We'd love a claymation short of Niki and Putor talking", "custom"), ("I'd like a watercolor video of Niki and Putor talking", "custom"),
                      ("I would like a Pixar style video of Niki and Putor talking", "toon3d"),
                      ("실사 말고 애니메이션으로 니키와 푸토르가 대화하는 영상", "anime"), ("3D 말고 2D 애니메이션으로 니키와 푸토르가 대화하는 영상", "anime"),
                      ("픽사 말고 지브리 느낌으로 니키와 푸토르가 대화하는 영상", "anime"), ("Beth and James talking in the kitchen, realistic, not anime", "photo"),
                      ("Niki and Putor talking, 2D animation, not 3D", "anime"),
                      ("실사로 베스와 제임스가 거실에서 대화하는 1분 영상\n애니메이션으로 바꿔 줘", "anime"),
                      ("실사로 베스와 제임스가 거실에서 대화하는 1분 영상\n수채화 느낌으로 바꿔 줘", "custom"),
                      ("실사로 베스와 제임스가 거실에서 대화하는 1분 영상\n반실사로 다시 해 줘", "semi"),
                      ("3D 애니메이션으로 니키와 푸토르가 대화하는 영상\n2D로 바꿔 줘", "anime"),
                      ("실사로 베스와 제임스가 대화하는 영상\n더 길게 해 줘", "photo")):
        assert cast_lib.said_level(text) == "sure" and cast_lib.said_key(text) == key, (text, cast_lib.said_level(text), cast_lib.said_key(text))
    for text in ("실사 느낌은 빼고 니키와 푸토르가 숲에서 대화하는 영상", "애니 말고 원래 그림체로 니키와 푸토르가 대화하는 영상",
                 "실사로 베스와 제임스가 대화하는 영상\n실사 느낌은 빼 줘",
                 "푸토르가 연필로 약초를 스케치하며 니키에게 설명하는 영상", "니키가 수채화로 그린 그림을 보여주는 영상", "니키가 레고로 성을 쌓으며 제임스와 대화하는 영상",
                 "Niki draws Putor in crayon while they chat", "Niki makes a clay pot with Putor", "I like anime and Niki talks with Putor",
                 "I would like a clay pot for Niki"):
        assert cast_lib.said_level(text) is None, (text, cast_lib._req_scan(text))
    revise = "실사로 베스와 제임스가 거실에서 대화하는 1분 영상\n애니메이션으로 바꿔 줘"
    assert cast_lib.keeps_said_style(revise, "2D anime feature film with clean line art") and not cast_lib.keeps_said_style(revise, "photorealistic film")
    assert cast_lib.said_style_fill(revise, lib_style) == cast_lib.SAID_STYLE_TEXT["anime"]


def test_r5_request_style_topics_characters_realism_forms_and_dots():
    """5차 검토 화풍 13·16·22·24: 이야기 소재로 쓴 열쇠 말('애니메이션 주제가'·'지브리 영화 속 장면'·'3D 모델링'·'about her favourite Pixar movie'·
    '실사 영화 촬영장')은 화풍이 아니다. '애니메이션 캐릭터로 만들어 줘'·'픽사 캐릭터처럼 …'·'as Pixar characters' 는 화풍이다. 사실·리얼·
    현실적은 화풍 꼴일 때만 실사 요청이고('사실적인 대화'·'리얼하게 다투는'·'real-life story' 는 아니다), '현실적인 느낌으로'·'실제 사람이 나오는'·
    '사진처럼 생생하게'·'…, real people'·'like a real movie' 는 실사다. '도트 그래픽으로' 는 픽셀 아트, '고흐 그림처럼' 은 화풍일 수도 있는 말"""
    from app import cast_lib
    for text in ("니키와 베스가 애니메이션 주제가를 부르며 대화하는 영상", "니키가 애니 OST를 흥얼거리며 베스와 대화하는 영상",
                 "니키가 지브리 영화 속 장면을 떠올리며 푸토르와 대화하는 영상", "니키가 웹툰 속 주인공 이야기를 베스에게 하는 영상",
                 "니키가 만화 주인공처럼 씩씩하게 말하는 영상", "Niki hums an anime theme song while talking with Beth", "제임스가 3D 모델링을 하며 니키와 대화하는 영상",
                 "니키가 좋아하는 픽사 영화 이야기를 하며 제임스와 대화하는 영상", "Niki tells Putor about her favourite Pixar movie",
                 "제임스가 실사 영화 촬영장에서 일했던 이야기를 니키에게 하는 영상", "Niki talks like a cartoon hero to Putor",
                 "자연스럽고 사실적인 대화를 나누는 베스와 제임스", "니키와 푸토르가 리얼한 표정으로 대화하는 영상", "베스와 제임스가 리얼하게 다투는 장면",
                 "니키가 리얼하고 감동적으로 우는 장면", "니키와 푸토르가 사실적인 고민을 나누는 영상", "Niki tells Putor a real-life story in the forest",
                 "실제 인물을 바탕으로 니키와 베스가 대화하는 영상", "니키와 베스가 실제 인물처럼 생생하게 대화하는 영상", "Niki meets real people in the city",
                 "니키가 애니메이션 캐릭터 피규어를 모으는 영상", "Niki is drawing anime characters in her sketchbook", "니키가 사진처럼 선명한 꿈을 꾸는 영상"):
        assert cast_lib.said_level(text) is None, (text, cast_lib._req_scan(text))
    for text, key in (("니키와 푸토르를 애니메이션 캐릭터로 만들어 줘", "anime"), ("픽사 캐릭터처럼 니키와 푸토르가 대화하는 영상", "toon3d"),
                      ("니키와 푸토르가 3D 캐릭터로 나오는 영상", "toon3d"), ("애니 캐릭터처럼 니키와 푸토르가 대화하는 영상", "anime"),
                      ("Niki and Putor as Pixar characters talking in the forest", "toon3d"), ("Make Niki and Putor anime characters talking in the forest", "anime"),
                      ("현실적인 느낌으로 베스와 제임스가 대화하는 영상", "photo"), ("현실적으로 베스와 제임스가 대화하는 영상", "photo"),
                      ("실제 사람이 나오는 것처럼 니키와 베스가 대화하는 영상", "photo"), ("실제 배우가 나오는 영상으로 베스와 제임스가 대화하는 장면", "photo"),
                      ("사진처럼 생생하게 베스와 제임스가 대화하는 영상", "photo"), ("Beth and James talking in the kitchen, real people", "photo"),
                      ("Beth and James talking, like a real movie", "photo"), ("리얼하게 베스와 제임스가 대화하는 영상 만들어 줘", "photo"),
                      ("사실적인 느낌으로 베스와 제임스가 부엌에서 대화하는 영상", "photo"), ("도트 그래픽으로 니키와 푸토르가 대화하는 영상", "custom"),
                      ("도트로 니키와 푸토르가 대화하는 영상", "custom")):
        assert cast_lib.said_level(text) == "sure" and cast_lib.said_key(text) == key, (text, cast_lib.said_level(text), cast_lib.said_key(text))
    assert cast_lib.custom_style_text("도트 그래픽으로 니키와 푸토르가 대화하는 영상") == "pixel art style"
    for text in ("고흐 그림처럼 니키와 푸토르가 대화하는 영상", "모네 그림 같은 느낌으로 니키와 푸토르가 대화하는 영상"):
        assert cast_lib.said_level(text) == "maybe", text
    assert cast_lib.said_level("니키가 도트 무늬 원피스를 입고 푸토르와 대화하는 영상") is None       # 무늬의 도트는 화풍이 아니다


def test_r5_black_and_white_material_and_2d_material_styles():
    """5차 검토 화풍 17·18: '흑백 연필화' 의 흑백은 재료의 꾸밈말이라 채움 글이 사진 쪽이 아니다 ('그린 그림' 문장이 남는다). 화풍 글의 애니 말이
    '2D' 하나뿐이고 재료 말이 함께면 그 재료 화풍(custom)이다 ('2D anime'·'watercolor anime style' 은 애니)"""
    from app import cast_lib
    for text, want in (("흑백 연필화로 니키와 푸토르가 대화하는 영상", "black-and-white pencil sketch style"),
                       ("흑백 수채화 느낌으로 니키와 베스가 대화하는 영상", "black-and-white watercolour painting style"),
                       ("Niki and Putor talking, black and white pencil sketch style", "black-and-white pencil sketch style")):
        fill = cast_lib.said_style_fill(text, "semi")
        assert fill == want and not video.photo_like({"bible": {"style": fill}}), (text, fill)
    assert cast_lib.custom_style_text("흑백 무성영화처럼 제임스가 걷는 영상") == "black-and-white film style"
    for style, key in (("2D pixel art animation with a limited palette", "custom"), ("soft watercolor-style 2D animation with gentle washes", "custom"),
                       ("pencil-sketch style 2D animation with rough graphite lines", "custom"), ("2D anime with soft shading", "anime"),
                       ("watercolor anime style", "anime"), ("2D animation", "anime"), ("2D storybook animation", "anime")):
        assert cast_lib.style_key(style) == key, (style, cast_lib.style_key(style))


def test_r5_tail_style_tags_follow_the_bare_word_decision():
    """5차 검토 화풍 15 (결정 유지): 1차 검토 고침의 결정 '표지 없는 맨 낱말(watercolor·noir·펠트 …)은 일부러 화풍 요청으로 세지 않는다' 를 끝
    꼬리표 자리에도 그대로 둔다 — 바꾸려면 사용자 판단이 필요하다. 표지가 있는 꼬리표(', watercolor illustration'·', claymation')와 실사 꼬리표
    (', realistic'·', real people' — 열쇠 말)는 지금처럼 화풍이다"""
    from app import cast_lib
    for text in ("Niki and Putor talking in the forest, oil painting", "Niki and Putor talking, pencil sketch", "니키와 푸토르가 숲에서 대화하는 영상, 수채화",
                 "Beth and James talking in the kitchen, film noir"):
        assert cast_lib.said_level(text) is None, text
    for text in ("Niki and Putor talking, watercolor illustration", "Niki and Putor talking, claymation", "Beth and James talking, realistic"):
        assert cast_lib.said_level(text) == "sure", text


# ── 3차 명세 (11-25, 사용자 결정 "방식 바꾸기"): 구성안이 장면마다 그 자리에 있는 사람(present)을 적고, 코드는 그 칸만 보고 G0 을 지킨다.
#    낱말 짐작은 칸이 없는 구성안의 예비로만 남는다 (넓히지 않는다). 시험 번호는 명세 4절의 번호 ──
def _pr(scene, present):
    """장면에 그 자리에 있는 사람 칸(present)을 붙인 사본"""
    return dict(scene, present=list(present))


def _scene_msgs(left):
    """검사 문구 가운데 장면 문구만 (줄 수·대화 차례 같은 영화 전체 규칙은 뺀다)"""
    return [x for x in left if x.startswith("Scene ")]


def _family_p():
    """FAMILY 에 장면마다 그 자리에 있는 사람(present)을 적은 판. 니키가 엄마에게 하는 말(장면 5)은 식탁의 제임스도 적었다
    (대화 컷에는 present 의 사람이 모두 나온다)"""
    f = copy.deepcopy(FAMILY)
    for sc, here in zip(f["scenes"], (["A", "B", "C"],) * 5 + (["A", "C"], ["A"])):
        sc["present"] = list(here)
    f["scenes"][4].update(characters=["C", "A", "B"], shot="medium group shot")
    return f


def _leave_plan(act, ko="니키"):
    """가족 식탁 → 모두에게 하는 말 → 니키 혼자 장면(act) 동안 떠남(present 로만) → 부모의 대화 두 줄"""
    return [_pr(_R5_WIDE, ["A", "B", "C"]),
            _pr(_r5_talk("P1", ["A", "B", "C"], "A", "everyone", "자, 아침 먹자!", shot="medium group shot", action="Beth smiles."), ["A", "B", "C"]),
            _pr(_r3_shot("P1", ["C"], "medium shot", act, ko, 6), ["A", "B", "C"]),
            _pr(_r5_talk("P1", ["A", "B"], "A", "B", "우리 딸 다 컸네요.", action="Beth smiles at James."), ["A", "B"]),
            _pr(_r5_talk("P1", ["B", "A"], "B", "A", "그러게 말이야.", action="James nods.", cont=True), ["A", "B"])]


def test_s3_01_leaving_written_only_in_present():
    """4절 1: 떠남을 present 로만 적는다 — 장면 글이 낱말 목록에 없는 말('waves goodbye')이어도 뒤 장면 present 에 니키가 없으면 부모의 대화 줄은
    투샷이다. 명세의 예 'Niki heads off to school' 도 같다. present 가 없으면 예전 낱말 짐작이라 목록 밖 떠남은 모른다 (넓히지 않았다)"""
    for act, ko in (("Niki grabs her school bag and waves goodbye.", "니키"), ("Niki heads off to school.", "니키"),
                    ("Niki smiles.", "니키가 손을 흔든다")):
        s, left = _r3_clean(_leave_plan(act, ko), exchange=False)
        assert _scene_msgs(left) == [], (act, left)
        sc = s["scenes"]
        assert sc[1]["characters"] == ["A", "B", "C"] and sc[1]["to_all"] is True and sc[1]["present"] == ["A", "B", "C"], act
        assert sc[2]["present"] == ["A", "B", "C"] and sc[3]["present"] == ["A", "B"], act        # 떠나는 장면까지는 그 자리에 있다
        assert sc[3]["characters"] == ["A", "B"] and sc[3]["shot"] == "medium two-shot" and sc[3]["listener"] == "B", act
        assert sc[4]["characters"] == ["B", "A"] and sc[4]["continues"] is True, act
        with duo_setting(True):
            assert video.talk_group(s, 1) == ["A", "B", "C"] and video.talk_pair(s, 3) == ["A", "B"] and video.talk_pair(s, 4) == ["B", "A"]
    # 모두에게 하는 말도 present 의 사람만: 떠난 니키는 넣지 않는다
    plan = _leave_plan("Niki grabs her school bag and waves goodbye.")
    plan[3] = _pr(_r5_talk("P1", ["A"], "A", "everyone", "자, 우리도 준비하자!", shot="close-up"), ["A", "B"])
    s, left = _r3_clean(plan, exchange=False)
    assert s["scenes"][3]["characters"] == ["A", "B"] and s["scenes"][3]["to_all"] is True
    assert ('Scene 4: "A" speaks to everyone in the place, so everyone there must be in the shot: add "B" to "characters" '
            '(at most three people).') in left, left
    # present 가 없으면 예전 짐작 그대로: 목록 밖 떠남('waves goodbye')은 모르므로 셋, 목록의 떠남('heads off to school')은 둘
    for act, want in (("Niki grabs her school bag and waves goodbye.", ["A", "B", "C"]), ("Niki heads off to school.", ["A", "B"])):
        old = [{k: v for k, v in x.items() if k != "present"} for x in _leave_plan(act)]
        s0, _l = _r3_clean(old, exchange=False)
        assert s0["scenes"][3]["characters"] == want and not any("present" in x for x in s0["scenes"]), act


def test_s3_02_present_keeps_a_person_whatever_the_text_says():
    """4절 2: '등교 준비를 하는 니키' 장면 뒤에도 present 에 니키가 있으면 셋 모두 나온다. 장면 글이 예전 낱말 목록의 떠남('heads off to school'·
    '학교에 간다')이어도 present 가 이긴다 — 그 자리 여부는 장면 글로 정하지 않는다"""
    for act, ko in (("Niki gets ready for school.", "등교 준비를 하는 니키"), ("Niki heads off to school.", "니키가 학교에 간다"),
                    ("Niki grabs her school bag and runs out of the kitchen door.", "니키가 부엌문으로 뛰어나간다")):
        scenes = [_pr(_R5_WIDE, ["A", "B", "C"]), _pr(_r3_shot("P1", ["C"], "medium shot", act, ko, 6), ["A", "B", "C"]),
                  _pr(_r5_talk("P1", ["A", "B", "C"], "A", "B", "니키 준비 다 했니?", shot="medium group shot"), ["A", "B", "C"])]
        s, left = _r3_clean(scenes, exchange=False)
        assert s["scenes"][2]["characters"] == ["A", "B", "C"] and s["scenes"][2]["shot"] == "medium group shot", act
        assert _scene_msgs(left) == [], (act, left)
        # 구성안이 니키를 화면에서 빠뜨려도(present 에는 있다) 다듬기가 넣고, 검사는 넣으라고 한다
        two = scenes[:2] + [_pr(_r5_talk("P1", ["A", "B"], "A", "B", "니키 준비 다 했니?"), ["A", "B", "C"])]
        s, left = _r3_clean(two, exchange=False)
        assert s["scenes"][2]["characters"] == ["A", "B", "C"] and s["scenes"][2]["shot"] == "medium group shot", act
        assert ('Scene 3 is a conversation, so everyone in "present" is seen: add "C" to "characters" after the speaker and the person '
                'spoken to (at most three people).') in left, left
        assert not any("not in the place" in x or "left the place" in x for x in left), left
    # present 가 없으면 예전 짐작 그대로: 'heads off to school' 은 떠남이라 둘
    old = [_R5_WIDE, _r3_shot("P1", ["C"], "medium shot", "Niki heads off to school.", "니키가 학교에 간다", 6),
           _r5_talk("P1", ["A", "B"], "A", "B", "니키 준비 다 했니?")]
    assert _r3_clean(old, exchange=False)[0]["scenes"][2]["characters"] == ["A", "B"]


def test_s3_03_absence_and_arrival_written_only_in_present():
    """4절 3: 없음('Beth and James are alone now')과 들어옴을 present 로만 적는다. 장면 글이 '둘만 남았다'고 해도 present 에 니키가 있으면 니키는
    그 자리에 있다"""
    # 없음: 장면 글은 아무 말도 하지 않고 present 에서만 니키가 빠진다
    scenes = [_pr(_R5_WIDE, ["A", "B", "C"]),
              _pr(_r3_shot("P1", ["A", "B"], "medium two-shot", "Beth and James sit at the table.", "부모", 6), ["A", "B"]),
              _pr(_r5_talk("P1", ["A", "B"], "A", "B", "조용하네요."), ["A", "B"]),
              _pr(_r5_talk("P1", ["B"], "B", "everyone", "우리 둘이 잘 지켜보자.", shot="close-up"), ["A", "B"])]
    s, left = _r3_clean(scenes, exchange=False)
    assert s["scenes"][2]["characters"] == ["A", "B"] and s["scenes"][3]["characters"] == ["B", "A"] and s["scenes"][3]["to_all"] is True
    assert _scene_msgs(left) == ['Scene 4: "B" speaks to everyone in the place, so everyone there must be in the shot: add "A" to "characters" '
                                 '(at most three people).'], left
    # 장면 글이 '둘만 남았다'고 해도 present 에 니키가 있으면 셋 (글을 읽지 않는다)
    alone = [scenes[0], _pr(_r3_shot("P1", ["A", "B"], "medium two-shot", "Beth and James are alone in the kitchen now.", "부엌에 둘만 남았다", 6),
                            ["A", "B", "C"]),
             _pr(_r5_talk("P1", ["A", "B", "C"], "A", "B", "조용하네요.", shot="medium group shot"), ["A", "B", "C"])]
    s, left = _r3_clean(alone, exchange=False)
    assert s["scenes"][2]["characters"] == ["A", "B", "C"] and _scene_msgs(left) == [], left
    # 들어옴: 장면 글에 들어오는 말이 없어도 present 에 니키가 들어오면 그 장면부터 그 자리에 있다
    arrive = [_pr(_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 10), ["A", "B"]),
              _pr(_r5_talk("P1", ["A", "B"], "A", "B", "니키는 아직 자나 봐요."), ["A", "B"]),
              _pr(_r3_shot("P1", ["C"], "medium shot", "Niki stands by the table, smiling sleepily.", "니키", 6), ["A", "B", "C"]),
              _pr(_r5_talk("P1", ["C"], "C", "everyone", "좋은 아침!", shot="medium shot"), ["A", "B", "C"])]
    s, left = _r3_clean(arrive, exchange=False)
    assert s["scenes"][1]["characters"] == ["A", "B"] and s["scenes"][1]["present"] == ["A", "B"]
    assert s["scenes"][3]["characters"] == ["C", "A", "B"] and s["scenes"][3]["to_all"] is True and video.shot_rank(s["scenes"][3]["shot"]) == 2
    assert ('Scene 4: "C" speaks to everyone in the place, so everyone there must be in the shot: add "A", "B" to "characters" '
            '(at most three people).') in left, left


def test_s3_04_a_voice_from_elsewhere_is_a_voice_over_shot():
    """4절 4: talk 인데 말하는 사람이 present·characters 에 없으면 목소리를 얹은 shot 이 되고 그 사람을 그리지 않는다 (장면 글이 문 밖이라고 하지
    않아도 — 예전 짐작은 글에 화면 밖 말이 없으면 다락방에 엄마를 그렸다). 대답 줄은 그 자리 사람에게 하는 말이 아니면 독백이다"""
    fam2 = [_R3_FAM[0], _R3_FAM[2]]
    scenes = [_pr(_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed by the round window.", "다락방의 니키"), ["C"]),
              _pr(_r5_talk("P3", [], "A", "C", "니키야, 밥 먹어라!", shot="close-up of the attic door", action="The attic door stays still."), ["C"]),
              _pr(_r5_talk("P3", ["C"], "C", "", "엄마, 조금만요!", shot="close-up", action="Niki looks up from her book."), ["C"])]
    s, left = _r3_clean(scenes, chars=fam2)
    one = s["scenes"][1]
    assert one["kind"] == "shot" and one["offscreen"] is True and one["speaker"] == "A" and one["line_ko"] and one["present"] == ["C"]
    with duo_setting(True):
        assert video.scene_people(s, 1) == [] and video.cast_scene(s, 1) is None
    # 대답(듣는 사람 칸이 빔, 대화 요청): 앞 대사의 화자(엄마)는 그 자리(present)에 없어 듣는 사람이 아니다 → 혼자 클로즈업
    assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True and s["scenes"][2]["listener"] == ""
    assert ('Scene 2: the speaker must be in "present" ("A" is not); a voice from elsewhere (behind a door, from another room, on the phone) '
            'is a "shot" scene with the line as a voice-over.') in left, left
    assert not any('put "A"' in x or "is only heard off-screen" in x for x in left), left
    # "nobody" 로 적어도 같다. 엄마를 characters 에만 적었으면(present 에는 없다) 화면 속 대화로 보고 검사가 present 를 고치게 한다
    scenes[2] = _pr(_r5_talk("P3", ["C"], "C", "nobody", "엄마, 조금만요!", shot="close-up"), ["C"])
    assert _r3_clean(scenes, chars=fam2)[0]["scenes"][2]["characters"] == ["C"]
    scenes[1] = _pr(_r5_talk("P3", ["A", "C"], "A", "C", "니키야, 밥 먹어라!"), ["C"])
    s, left = _r3_clean(scenes, chars=fam2)
    assert s["scenes"][1]["kind"] == "talk" and s["scenes"][1]["characters"] == ["A", "C"] and s["scenes"][1]["present"] == ["C", "A"]
    assert any(x.startswith('Scene 2: the speaker must be in "present" ("A" is not)') for x in left), left
    assert not any('"A" is in "characters" but not in "present"' in x for x in left), left       # 말하는 사람은 위 문구 하나로
    # 예전 짐작(present 없음)은 글에 화면 밖 말이 없으면 엄마를 다락방의 대화 컷에 그렸다
    old = [{k: v for k, v in x.items() if k != "present"} for x in
           [scenes[0], _r5_talk("P3", [], "A", "C", "니키야, 밥 먹어라!", shot="close-up of the attic door", action="The attic door stays still.")]]
    assert _r3_clean(old, chars=fam2)[0]["scenes"][1]["characters"] == ["A", "C"]


def test_s3_05_a_phone_call_in_the_place_is_a_solo_close_up():
    """4절 5: 화면 속 통화(말하는 사람은 present, listener nobody) → 같은 자리에 다른 사람이 있어도 혼자 클로즈업"""
    for chars in (["B"], ["B", "A"]):
        scenes = [_pr(_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 10), ["A", "B"]),
                  _pr(_r5_talk("P1", chars, "B", "nobody", "네, 어머니. 오늘 저녁에 갈게요.", shot="medium shot",
                               action="James talks on the phone, smiling."), ["A", "B"])]
        s, left = _r3_clean(scenes, exchange=False)
        sc = s["scenes"][1]
        assert sc["kind"] == "talk" and sc["characters"] == ["B"] and sc["monologue"] is True and sc["shot"] == "medium close-up", chars
        assert sc["present"] == ["A", "B"] and sc["listener"] == "" and sc["continues"] is False
        assert ("Scene 2 is a monologue: the shot must be a close-up or a medium close-up of the speaker alone, not a medium or wider shot."
                in left), left
        assert any(x.startswith('Scene 2 is a monologue ("B" speaks to nobody in the place)') for x in left) is (len(chars) > 1), left
        with duo_setting(True):
            assert video.talk_group(s, 1) is None and not video.duo_talk(s, 1)
            assert "alone in the frame, no other people" in video.talk_prompt(s, 1)


def test_s3_06_a_listener_missing_from_present_is_not_in_the_place():
    """4절 6 (11-25 최종 확인에서 바꿈): present 를 쓴 장면에서 듣는 사람이 present 에도 characters 에도 없으면 그 자리에 없는 사람이다 (화면 밖
    목소리와 같은 기준). 그 사람에게 하는 말은 독백(혼자 클로즈업)이고, 검사는 present 에 넣거나 nobody 로 고쳐 쓰게 한다. 이름으로 적은 듣는
    사람('니키')도 같다. 예전에는 듣는 사람을 믿고 P 에 넣어, 문 밖에서 부른 엄마에게 하는 대답에 다락방에 없는 엄마를 그렸다"""
    for lis in ("C", "니키"):
        scenes = [_pr(_r3_shot("P2", ["A"], "wide shot", "Beth waters the apple tree.", "정원의 베스", 10), ["A"]),
                  _pr(_r5_talk("P2", ["A"], "A", lis, "니키야, 이리 와 보렴.", shot="close-up", action="Beth calls out warmly."), ["A"])]
        s, left = _r3_clean(scenes, exchange=False)
        sc = s["scenes"][1]
        assert sc["characters"] == ["A"] and sc["listener"] == "" and sc["present"] == ["A"] and sc["monologue"] is True, lis
        assert video.shot_rank(sc["shot"]) <= 1, sc["shot"]
        # fixC (11-25 최종 확인 06#1·05#2): 문구 하나만 — "nobody"(혼자 클로즈업) 길을 먼저, present 에 넣는 길은 '장면들이 그 사람을 이 장소에 보일
        # 때만' 뒤에 (예전 칸 문구 'add "C" to "present" if … or set "listener" to "nobody"' 를 바꿨다)
        assert [x for x in _scene_msgs(left) if x.startswith("Scene 2")] == [
            'Scene 2: the listener "C" is not in "present", so nobody in the place is spoken to: set "listener" to "nobody" and make it a '
            'close-up of "A" alone ("characters" ["A"]); a reply to a voice behind a door, from another room or on the phone is such a '
            'monologue. Only if the scenes show "C" in this place, add "C" to "present" and make "characters" ["A", "C"] in a medium '
            'two-shot instead.'], left
    # 문 밖에서 부른 엄마(화면 밖 목소리)에게 하는 대답, 전화 너머 엄마에게 하는 말: 다락방에는 니키 혼자
    for act in ("Niki calls toward the door.", "Niki talks into her phone."):
        attic = [_pr(_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed.", "다락방의 니키"), ["C"]),
                 _pr(_r5_talk("P3", ["C"], "A", "C", "니키야, 밥 먹어라!", shot="close-up"), ["C"]),
                 _pr(_r5_talk("P3", ["C"], "C", "A", "네, 갈게요!", shot="close-up", action=act), ["C"])]
        s, left = _r3_clean(attic, chars=[_R3_FAM[0], _R3_FAM[2]], exchange=False)
        assert s["scenes"][1]["kind"] == "shot" and s["scenes"][1].get("offscreen") is True, act
        assert s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True and s["scenes"][2]["present"] == ["C"], act
        with duo_setting(True):
            assert video.talk_group(s, 2) is None and video.scene_people(s, 2) == ["C"], act
    # 화면에 적은 듣는 사람은 그 자리에 있다 (characters 는 present 의 일부): 대화 컷, 검사는 present 에 넣으라고 한다
    s, left = _r3_clean([_pr(_r3_shot("P2", ["A"], "wide shot", "Beth waters the apple tree.", "정원의 베스", 10), ["A"]),
                         _pr(_r5_talk("P2", ["A", "C"], "A", "C", "니키야, 물 좀 줄래?"), ["A"])], exchange=False)
    assert s["scenes"][1]["characters"] == ["A", "C"] and s["scenes"][1]["present"] == ["A", "C"] and s["scenes"][1]["monologue"] is False
    # fixC (바꾼 단언): 화면에 적은 사람이 듣는 사람이면 빼는 길을 끝까지(독백: nobody, 혼자 클로즈업) 적는다 — 빼기만 따르면 다음 판에 '듣는 사람이
    # 그 자리에 없다' 문구가 또 나와 한 차례가 더 들었다. 같은 뜻의 칸 문구(present 에 넣거나 nobody)는 따로 내지 않는다
    assert [x for x in _scene_msgs(left) if x.startswith("Scene 2")] == [
        'Scene 2: the listener "C" is in "characters" but not in "present": add "C" to "present" if "C" is in the place; if not, nobody in '
        'the place is spoken to: remove "C" from "characters", set "listener" to "nobody" and make it a close-up of "A" alone.'], left


def test_s3_06b_field_checks_and_listener_inference_inside_present():
    """3차 명세 1.3·1.4: 듣는 사람 칸이 비었거나 모르는 값이면 (a) characters 의 다른 사람으로 추정하고("nobody" 는 추정하지 않는다), 모두에게
    하는 말인데 그 자리에 다른 사람이 없으면 독백이다. 말하는 사람 자신(혼잣말)은 독백이다 (fixA: 규칙 글 'talking to oneself' = nobody, 예전
    경로·고침 2 와 같다 — G0 '독백은 클로즈업'). 칸끼리의 검사: present 의 모르는 id, present 에 없는 화면의 사람. 구성안이 적은 동물은 대화 컷에 남는다"""
    wide = _pr(_r3_shot("P1", ["A", "B"], "wide shot", "Beth and James sit at the breakfast table.", "부모의 아침", 10), ["A", "B"])
    for lis, msg in (("", None), ("Z", '"listener" "Z" is not in bible.characters'), ("A", '"listener" is the speaker "A" (talking to oneself)')):
        s, left = _r3_clean([wide, _pr(_r5_talk("P1", ["A", "B"], "A", lis, "여보, 커피 더 줄까요?"), ["A", "B"])], exchange=False)
        if lis == "A":
            assert s["scenes"][1]["characters"] == ["A"] and s["scenes"][1]["listener"] == "" and s["scenes"][1]["monologue"] is True, lis
            assert s["scenes"][1]["shot"] == "medium close-up", s["scenes"][1]["shot"]
            assert [x for x in _scene_msgs(left) if x.startswith("Scene 2")] == [
                'Scene 2: "listener" is the speaker "A" (talking to oneself): write the id of the person in the place who is spoken to, or "nobody" '
                'for a monologue, which is a close-up of "A" alone.'], left
            continue
        assert s["scenes"][1]["characters"] == ["A", "B"] and s["scenes"][1]["listener"] == "B" and s["scenes"][1]["monologue"] is False, lis
        assert (msg is None and _scene_msgs(left) == []) or any(msg in x for x in left), (lis, left)
    s, _l = _r3_clean([wide, _pr(_r5_talk("P1", ["A", "B"], "A", "nobody", "커피가 식었네...", shot="close-up"), ["A", "B"])], exchange=False)
    assert s["scenes"][1]["characters"] == ["A"] and s["scenes"][1]["monologue"] is True
    # (b) 구성안이 말하는 사람만 적었으면 같은 장소에서 가장 가까운 다른 화자 — 그 사람이 그 자리(present)에 있을 때만. 대화 요청이 아니어도 같다
    # (그 자리 여부를 present 가 정한다). 그 사람이 present 에 없으면 독백
    for here, want in ((["A", "B"], (["A", "B"], "B")), (["A"], (["A"], ""))):
        s, _l = _r3_clean([wide, _pr(_r5_talk("P1", ["A"], "A", "", "여보, 일어났어요?", shot="close-up"), here),
                           _pr(_r5_talk("P1", ["B", "A"], "B", "A", "응, 방금.", shot="medium two-shot"), ["A", "B"])], exchange=False)
        assert (s["scenes"][1]["characters"], s["scenes"][1]["listener"]) == want, (here, s["scenes"][1]["characters"])
    # 모두에게 하는 말인데 다락방에 혼자: 독백 (to_all 이 아니다)
    attic = [_pr(_r3_shot("P3", ["A"], "medium shot", "Beth sits by the round window.", "다락방의 베스", 10), ["A"]),
             _pr(_r5_talk("P3", ["A"], "A", "everyone", "다들 어디 갔을까?", shot="close-up"), ["A"])]
    s, _l = _r3_clean(attic, exchange=False)
    assert s["scenes"][1]["characters"] == ["A"] and s["scenes"][1]["monologue"] is True and s["scenes"][1]["to_all"] is False
    # present 의 모르는 id, present 에 없는 화면의 사람 (다듬기는 화면의 사람을 그 자리 사람으로 둔다)
    s, left = _r3_clean([_pr(_r3_shot("P1", ["A", "C"], "wide shot", "Beth and Niki sit at the table.", "아침", 10), ["A", "Z"]),
                         _pr(_r5_talk("P1", ["A", "C"], "A", "C", "니키야, 잘 잤니?"), ["A", "C"])], exchange=False)
    assert s["scenes"][0]["present"] == ["A", "C"] and s["scenes"][1]["characters"] == ["A", "C"]
    assert 'Scene 1: "present" has "Z", which is not in bible.characters.' in left, left
    assert 'Scene 1: "C" is in "characters" but not in "present": add "C" to "present", or remove "C" from "characters".' in left, left
    # 구성안이 화면에 적은 동물은 사람끼리의 대화 컷에도 남는다 (사람 3명에 세지 않는다)
    pup = {"id": "D", "look": "a small white puppy with floppy ears", "name_ko": "강아지"}
    s, left = _r3_clean([_pr(_r3_shot("P2", ["A", "C", "D"], "wide shot", "Beth and Niki sit on the grass with the puppy.", "정원", 10), ["A", "C", "D"]),
                         _pr(_r5_talk("P2", ["A", "C", "D"], "A", "C", "니키야, 강아지가 졸린가 봐."), ["A", "C", "D"])], chars=_R3_FAM + [pup], exchange=False)
    assert s["scenes"][1]["characters"] == ["A", "C", "D"] and _scene_msgs(left) == [], left
    with duo_setting(True):
        assert video.talk_group(s, 1) == ["A", "C"] and "Also in the frame: a small white puppy with floppy ears." in video.duo_talk_prompt(
            s, 1, video.layout_slots(s, 1, video.scene_people(s, 1)))


def test_s3_07_a_line_to_everyone_shows_up_to_three_people_in_seat_order():
    """4절 7: 모두에게 하는 말은 present 의 사람 모두 — 셋이면 셋, 넷이면 말하는 사람 + 자리 차례(같은 장소 구간의 자리 지도)로 둘"""
    s, left = _r3_clean([_pr(_R5_WIDE, ["A", "B", "C"]),
                         _pr(_r5_talk("P1", ["B"], "B", "everyone", "자, 다 같이 축하하자!", shot="close-up"), ["C", "B", "A"])], exchange=False)
    assert s["scenes"][1]["characters"] == ["B", "A", "C"] and s["scenes"][1]["to_all"] is True and s["scenes"][1]["shot"] == "medium group shot"
    assert ('Scene 2: "B" speaks to everyone in the place, so everyone there must be in the shot: add "C", "A" to "characters" '
            '(at most three people).') in left, left
    gran = {"id": "E", "look": "a kind grandmother in her seventies with silver hair and a lilac shawl", "name_ko": "할머니",
            "voice": "A gentle grandmother.", "voice_ko": "할머니"}
    for seats, want, order in ((["A", "B", "C", "E"], ["B", "A", "C"], ["A", "B", "C"]), (["E", "C", "B", "A"], ["B", "E", "C"], ["E", "C", "B"])):
        four = [_pr(_r3_shot("P1", seats, "wide shot", "The family sits around the breakfast table.", "가족의 아침 식탁", 10), ["A", "B", "C", "E"]),
                _pr(_r5_talk("P1", ["B"], "B", "everyone", "자, 다 같이 축하하자!", shot="close-up"), ["E", "C", "B", "A"])]
        s, left = _r3_clean(four, chars=_R3_FAM + [gran], exchange=False)
        assert s["scenes"][1]["characters"] == want and s["scenes"][1]["present"] == ["E", "C", "B", "A"], (seats, s["scenes"][1]["characters"])
        with duo_setting(True):
            assert [x["id"] for x in video.layout_slots(s, 1, video.scene_people(s, 1))] == order, seats
            assert video.talk_group(s, 1)[0] == "B" and video.duo_talk(s, 1)
        # 구성안이 고른 셋은 그대로 (사람 3명까지)
        four[1] = _pr(_r5_talk("P1", ["B", "E", "A"], "B", "everyone", "자, 다 같이 축하하자!", shot="medium group shot"), ["A", "B", "C", "E"])
        s, left = _r3_clean(four, chars=_R3_FAM + [gran], exchange=False)
        assert s["scenes"][1]["characters"] == ["B", "E", "A"] and not any("speaks to everyone" in x for x in left), (seats, left)


def test_s3_08_two_looks_of_one_person_in_present_keep_one():
    """4절 8: james 와 james_glasses 가 present 에 함께 있으면 한 모습만 남는다 — 이 장면의 characters·speaker·listener 에 쓴 모습을 먼저,
    없으면 먼저 쓴 모습. 같은 사람으로 세어 대화 컷·잇기·검사가 그대로다"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    raw = _variant_story()
    for sc, here in zip(raw["scenes"], (["beth", "james", "james_glasses"], ["beth", "james_glasses", "james"],
                                        ["james", "beth", "james_glasses"], ["james", "beth"])):
        sc["present"] = here
    with duo_setting(True):
        s = video.clean_dialogue_story(raw, 32, exchange=True, known=known)
        left = video.dialogue_problems(raw, 32, True, exchange=True, known=known)
    assert [x["present"] for x in s["scenes"]] == [["beth", "james"], ["beth", "james_glasses"], ["james_glasses", "beth"],
                                                   ["james_glasses", "beth"]]
    assert s["scenes"][2]["characters"] == ["beth", "james_glasses"] and s["scenes"][2]["shot"] == "medium two-shot"
    assert s["scenes"][3]["characters"] == ["james_glasses", "beth"] and s["scenes"][3]["continues"] is True
    # fixC (11-25 최종 확인 06#2, 바꾼 단언 — 예전에는 left == []): present 에 두 모습을 함께 적으면 규칙 글('never put both entries in one scene')대로
    # 이 장면이 보이는 모습 하나만 남기게 한다. 남기라는 모습은 다듬기가 고른 모습과 같다
    assert left == ['Scene 1: "present" has "james" and "james_glasses", the same person in two looks; a scene has only one of them: keep only '
                    '"james", the look this scene shows.',
                    'Scene 2: "present" has "james_glasses" and "james", the same person in two looks; a scene has only one of them: keep only '
                    '"james_glasses", the look this scene shows.',
                    'Scene 3: "present" has "james" and "james_glasses", the same person in two looks; a scene has only one of them: keep only '
                    '"james_glasses", the look this scene shows.'], left
    # 모두에게 하는 말도 같은 사람을 둘로 넣지 않는다
    raw["scenes"][2]["listener"] = "everyone"
    with duo_setting(True):
        assert video.clean_dialogue_story(raw, 32, exchange=True, known=known)["scenes"][2]["characters"] == ["beth", "james_glasses"]


def test_s3_09_an_animal_in_present_and_an_animal_listener():
    """4절 9: 동물도 present 에 적는다. 동물에게 하는 말은 그 동물이 화면에 남고(사람 3명에 세지 않음) 그 자리의 다른 사람도 나온다. 동물이 듣는
    사람이 아니면 사람끼리의 대화 컷에 더하지 않는다 (지금 규칙 그대로)"""
    pup = {"id": "D", "look": "a small white puppy with floppy ears", "name_ko": "강아지"}
    chars = _R3_FAM + [pup]
    wide = _r3_shot("P2", ["C", "D"], "wide shot", "Niki plays with the puppy under the apple tree.", "정원의 니키와 강아지", 10)
    s, left = _r3_clean([_pr(wide, ["C", "D"]), _pr(_r5_talk("P2", ["C", "D"], "C", "D", "이리 와, 몽이야!", shot="medium shot"), ["C", "D"])],
                        chars=chars, exchange=False)
    assert s["scenes"][1]["characters"] == ["C", "D"] and s["scenes"][1]["listener"] == "D" and _scene_msgs(left) == [], left
    with duo_setting(True):
        assert video.talk_group(s, 1) is None and not video.duo_talk(s, 1)
        assert "Also in the frame: a small white puppy with floppy ears." in video.talk_prompt(s, 1)
    # 베스도 정원에 있으면(present) 강아지에게 하는 말에도 베스가 나온다: [니키, 강아지, 베스] — 두 사람 대사 컷, 검사는 베스를 넣게 한다
    s, left = _r3_clean([_pr(wide, ["A", "C", "D"]), _pr(_r5_talk("P2", ["C", "D"], "C", "D", "이리 와, 몽이야!", shot="medium shot"), ["A", "C", "D"])],
                        chars=chars, exchange=False)
    assert s["scenes"][1]["characters"] == ["C", "D", "A"] and s["scenes"][1]["listener"] == "D"
    with duo_setting(True):
        assert video.talk_group(s, 1) == ["C", "A"] and video.duo_talk(s, 1)
    assert any(x.startswith('Scene 2 is a conversation, so everyone in "present" is seen: add "A"') for x in left), left
    # 강아지가 그 자리에 있어도 사람끼리의 대화 컷에는 더하지 않는다 (동물은 이어 받지 않는다)
    s, left = _r3_clean([_pr(wide, ["A", "C", "D"]), _pr(_r5_talk("P2", ["A", "C"], "A", "C", "니키야, 강아지 밥 줬니?"), ["A", "C", "D"])],
                        chars=chars, exchange=False)
    assert s["scenes"][1]["characters"] == ["A", "C"] and s["scenes"][1]["present"] == ["A", "C", "D"] and _scene_msgs(left) == [], left


def test_s3_10_a_plan_with_present_in_some_scenes_only():
    """4절 10: present 가 있는 장면과 없는 장면이 섞인 구성안은 전체를 새 방식으로 다듬는다. 칸이 없는 장면의 그 자리 사람은 characters·말하는
    사람·듣는 사람뿐이고(짐작하지 않는다), 검사는 그 장면에 present 를 쓰라고 한다"""
    f = _family_p()
    for k in (0, 3):
        f["scenes"][k].pop("present")
    with duo_setting(True):
        s = video.clean_dialogue_story(f, 60, exchange=True)
        left = video.dialogue_problems(f, 60, True, exchange=True)
    assert all(isinstance(x.get("present"), list) for x in s["scenes"])
    assert s["scenes"][0]["present"] == ["A", "B", "C"] and s["scenes"][3]["present"] == ["C"]
    assert [x["characters"] for x in s["scenes"]] == [["A", "B", "C"], ["A", "B", "C"], ["B", "A", "C"], ["C"], ["C", "A", "B"], ["A", "C"], ["A"]]
    assert s["scenes"][3]["monologue"] is True and s["scenes"][2]["continues"] is True
    assert left == ['Scene 1: write "present", the ids of everyone who is in this place during this scene (people and animals), whether the '
                    'shot shows them or not.',
                    'Scene 4: write "present", the ids of everyone who is in this place during this scene (people and animals), whether the '
                    'shot shows them or not.'], left
    # 칸이 없는 장면의 글이 떠남을 말해도 짐작하지 않는다: 뒤 장면 present 가 정한다 (예전 짐작이면 둘)
    plan = [_pr(_R5_WIDE, ["A", "B", "C"]), _R5_LEAVE, _pr(_r5_talk("P1", ["A", "B", "C"], "A", "B", "니키 어디 가니?", shot="medium group shot"),
                                                          ["A", "B", "C"])]
    s, left = _r3_clean(plan, exchange=False)
    assert s["scenes"][2]["characters"] == ["A", "B", "C"] and s["scenes"][1]["present"] == ["C"]
    assert _scene_msgs(left) == ['Scene 2: write "present", the ids of everyone who is in this place during this scene (people and animals), '
                                 'whether the shot shows them or not.'], left
    old = [{k: v for k, v in x.items() if k != "present"} for x in plan]
    assert _r3_clean(old, exchange=False)[0]["scenes"][2]["characters"] == ["A", "B"]


def test_s3_10b_present_plans_never_call_the_word_guessing_helpers():
    """3차 명세 1.3·1.4: present 를 쓴 구성안에서는 장면 글로 그 자리 여부를 짐작하는 도우미를 부르지 않는다 (다듬기·검사 모두). 도우미를 부르면
    실패하게 바꿔 두고, 떠남·없음·화면 밖 목소리·통화·동물·두 모습이 모두 든 구성안을 다듬고 검사한다. 글을 다듬는 도우미(_mono_shot·
    _other_mention)는 지시문 정리라 그대로 쓴다"""
    names = ("_run_state", "_presence", "_here", "_place_people", "_listed_gone", "_first_seen_here", "_listed_voice_off", "_offscreen_voice",
             "_animal_here", "_absent", "_moves", "_movers", "_leavers", "_enters_at", "_voice_off_text", "_shown", "_line_partner", "_look_here",
             "line_target")
    saved = {n: getattr(video, n) for n in names}

    def guard(name):
        def fail(*a, **k):
            raise AssertionError(f"{name} 를 불렀다 (present 구성안)")
        return fail
    pup = {"id": "D", "look": "a small white puppy with floppy ears", "name_ko": "강아지"}
    plan = _leave_plan("Niki grabs her school bag and runs out of the kitchen door.") + [
        _pr(_r5_talk("P1", [], "C", "A", "엄마, 도시락!", shot="close-up of the door", action="A voice calls from outside the kitchen door."), ["A", "B"]),
        _pr(_r5_talk("P1", ["A"], "A", "", "어머, 깜빡했네.", shot="close-up", action="Beth laughs to herself."), ["A", "B", "D"]),
        _pr(_r5_talk("P1", ["B", "D"], "B", "D", "너도 배고프니?", shot="medium shot", action="James talks to the puppy."), ["A", "B", "D"]),
        _pr(_r5_talk("P1", ["B"], "B", "nobody", "네, 어머니.", shot="close-up", action="James talks on the phone."), ["A", "B", "D"])]
    try:
        for n in names:
            setattr(video, n, guard(n))
        s, left = _r3_clean(plan, chars=_R3_FAM + [pup])
        with duo_setting(True):
            later = [(video.talk_group(s, k), video.scene_people(s, k), [x["id"] for x in video.layout_slots(s, k, video.scene_people(s, k))])
                     for k in range(len(s["scenes"]))]
    finally:
        for n, fn in saved.items():
            setattr(video, n, fn)
    sc = s["scenes"]
    assert sc[5]["kind"] == "shot" and sc[5]["offscreen"] is True and later[5][1] == []          # 문 밖의 니키 목소리
    assert sc[6]["characters"] == ["A", "B"] and sc[6]["listener"] == "B"                          # 빈 칸 → 뒤 대사의 제임스 (그 자리)
    assert sc[7]["characters"] == ["B", "D", "A"] and later[7][0] == ["B", "A"]                     # 강아지에게 + 그 자리의 베스
    assert sc[8]["characters"] == ["B"] and sc[8]["monologue"] is True                             # 화면 속 통화
    assert any(x.startswith('Scene 6: the speaker must be in "present" ("C" is not)') for x in left), left


def test_s3_11_plans_without_present_are_cleaned_as_before():
    """4절 11: present 가 하나도 없는 구성안의 결과는 고침 4 뒤 트리(작업보관_2차_20261124\\확인3회차\\작업트리_고침4후)와 같다. 아래 값은 그 트리의
    코드로 만든 것이다 (구현 때 두 트리에 옛 시험 파일 전체와 무작위 구성안 1,500개를 함께 돌려 맨 위 호출 결과가 모두 같음도 확인했다).
    다듬은 장면에 present 칸을 더하지 않고, 예전 낱말 짐작(떠남·문 밖 목소리)이 그대로 돈다"""
    from app import cast_lib

    def rows(s):
        return [(x["kind"], x["characters"], x["listener"], x.get("monologue"), x.get("to_all"), x["continues"], x.get("offscreen"), x["shot"])
                for x in s["scenes"]]
    fam, known = _r4_lib_fam()
    leave = [_R5_WIDE, _R5_LEAVE, _r5_talk("P1", ["A", "C"], "A", "C", "니키야, 도시락 가져가!", action="Beth calls after Niki."),
             _r5_talk("P1", ["B", "A"], "B", "A", "벌써 갔어요.", action="James chuckles."),
             _r5_talk("P1", ["A", "B"], "A", "B", "그러게요.", action="Beth laughs.", cont=True)]
    door = [_r3_shot("P3", ["C"], "medium shot", "Niki sits alone on her bed by the round window, reading.", "다락방의 니키", 8),
            _r5_talk("P3", ["A", "C"], "A", "C", "니키야, 밥 먹어라!", action="Beth calls through the closed door.", ko="문 밖에서 엄마가 부른다"),
            _r5_talk("P3", ["C", "A"], "C", "A", "엄마, 조금만요!", action="Niki calls toward the door.")]
    with duo_setting(True):
        got = {"FAMILY": (video.clean_dialogue_story(FAMILY, 60, exchange=True), video.dialogue_problems(FAMILY, 60, True, exchange=True)),
               "DUO_OK": (video.clean_dialogue_story(DUO_OK, 48), video.dialogue_problems(DUO_OK, 48, True)),
               "DLG_OK": (video.clean_dialogue_story(DLG_OK, 48), video.dialogue_problems(DLG_OK, 48, True)),
               "VARIANT": (video.clean_dialogue_story(_variant_story(), 32, exchange=True, known=cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")),
                           video.dialogue_problems(_variant_story(), 32, True, exchange=True, known=cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")))}
    got["LEAVE"] = _r3_clean(leave, chars=fam, known=known)
    got["DOOR"] = _r3_clean(door, chars=fam, known=known)
    want = {
        "FAMILY": ([("shot", ["A", "B", "C"], "", None, None, False, None, "wide shot"),
                    ("talk", ["A", "B", "C"], "B", False, False, False, None, "medium group shot"),
                    ("talk", ["B", "A", "C"], "", False, True, True, None, "medium group shot"),
                    ("talk", ["C"], "", True, False, False, None, "close-up"),
                    ("talk", ["C", "A", "B"], "A", False, False, False, None, "medium group shot"),
                    ("shot", ["A", "C"], "", None, None, False, None, "wide shot"),
                    ("talk", ["A"], "", True, False, False, None, "close-up")], []),
        "DUO_OK": ([("shot", ["A", "B"], "", None, None, False, None, "wide shot, slow dolly forward"),
                    ("talk", ["A", "B"], "B", False, False, False, None, "medium two-shot"),
                    ("talk", ["B", "A"], "A", False, False, True, None, "medium two-shot"),
                    ("talk", ["A", "C", "B"], "C", False, False, False, None, "medium group shot"),
                    ("shot", ["C"], "", None, None, False, None, "close-up"),
                    ("shot", ["A", "B", "C"], "", None, None, False, None, "wide shot")], []),
        "DLG_OK": ([("shot", [], "", None, None, False, None, "wide shot, slow dolly forward"),
                    ("shot", ["A", "B"], "", None, None, False, None, "wide shot from behind, slow follow"),
                    ("talk", ["A"], "", True, False, False, None, "close-up"),
                    ("talk", ["B"], "", True, False, False, None, "medium close-up"),
                    ("shot", ["A"], "", None, None, False, None, "close-up"),
                    ("shot", ["A", "B"], "", None, None, False, None, "wide shot from behind")],
                   ["Scene 4 is a talk scene, so it cannot continue the previous scene unless both are conversation lines with the same people "
                    "in the same place."]),
        "VARIANT": ([("shot", ["beth", "james"], "", None, None, False, None, "wide shot"),
                     ("shot", ["james_glasses"], "", None, None, False, None, "close-up"),
                     ("talk", ["beth", "james_glasses"], "james_glasses", False, False, False, None, "medium two-shot"),
                     ("talk", ["james_glasses", "beth"], "beth", False, False, True, None, "medium two-shot")], []),
        "LEAVE": ([("shot", ["A", "B", "C"], "", None, None, False, None, "wide shot"),
                   ("shot", ["C"], "", None, None, False, None, "medium shot"),
                   ("talk", ["A"], "", True, False, False, None, "medium close-up"),
                   ("talk", ["B", "A"], "A", False, False, False, None, "medium two-shot"),
                   ("talk", ["A", "B"], "B", False, False, True, None, "medium two-shot")],
                  ['Scene 3: "C" is not in the place at this moment (left the place in scene 2), so nobody in the place is spoken to: write '
                   '"nobody" in "listener", remove "C" from "characters" and make it a close-up of "A" alone, or show "C" in the place first.']),
        "DOOR": ([("shot", ["C"], "", None, None, False, None, "medium shot"),
                  ("shot", ["C"], "", None, None, False, True, "medium two-shot"),
                  ("talk", ["C"], "", True, False, False, None, "medium close-up")],
                 ['"A" speaks only as a voice-over. Give "A" at least one "talk" scene, so the audience sees the face speak.',
                  'Scene 2: "A" is only heard off-screen (outside the place, behind a door or on the phone), so this is not a talk scene: write '
                  '"kind": "shot" with the line as a voice-over (the shot shows the place or the people who hear it, without "A" in "characters"), '
                  'or show "A" speaking in the place.',
                  'Scene 3: "A" is not in the place at this moment (scene 1 says they are not there), so nobody in the place is spoken to: write '
                  '"nobody" in "listener", remove "A" from "characters" and make it a close-up of "C" alone, or show "A" in the place first.'])}
    for name, (s, left) in got.items():
        assert rows(s) == want[name][0], (name, rows(s))
        assert left == want[name][1], (name, left)
        assert not any("present" in x for x in s["scenes"]) and not video.has_present(s["scenes"]), name


def test_s3_12_off_mode_ignores_present():
    """4절 12: 꺼짐(STUDIO_DUO_TALK=false, SOLO 칸)은 1차 그대로 — present 를 써도 보지 않고, 다듬은 장면에 남기지 않고, 검사도 present 를 묻지 않는다"""
    f = _family_p()
    plain = copy.deepcopy(f)
    for sc in plain["scenes"]:
        sc.pop("present")
    with duo_setting(False):
        a, b = video.clean_dialogue_story(f, 60, exchange=True), video.clean_dialogue_story(plain, 60, exchange=True)
        assert a == b and [x["characters"] for x in a["scenes"] if x["kind"] == "talk"] == [["A"], ["B"], ["C"], ["C"], ["A"]]
        assert not any("present" in x for x in a["scenes"]) and "duo_rules" not in a
        assert video.dialogue_problems(f, 60, True, exchange=True) == video.dialogue_problems(plain, 60, True, exchange=True)
        assert not any("present" in x for x in video.dialogue_problems(f, 60, True, exchange=True))
        assert video.dialogue_schema() is video.DIALOGUE_SCHEMA_SOLO
    with duo_setting(True):                       # 감독이 이 요청에서 두세 사람 컷을 못 만든다고 정한 때(duo=False)도 같다
        assert video.clean_dialogue_story(f, 60, duo=False, exchange=True) == b


def test_s3_13_schema_asks_for_present_only_in_the_group_rules():
    """4절 13 (present 부분): 두세 사람 규칙 칸(DIALOGUE_SCHEMA)은 장면마다 present(문자열 목록)를 요구하고, 꺼짐 칸(SOLO)과 대사 없는 영상 칸
    (STORY_SCHEMA)에는 없다. (STORY_SCHEMA 의 style_said 는 test_s3_13b 에서 본다)"""
    items = video.DIALOGUE_SCHEMA["properties"]["scenes"]["items"]
    assert "present" in items["required"] and items["properties"]["present"] == {"type": "array", "items": {"type": "string"}}
    assert "listener" in items["required"]
    for schema in (video.DIALOGUE_SCHEMA_SOLO, video.STORY_SCHEMA):
        sitems = schema["properties"]["scenes"]["items"]
        assert "present" not in sitems["required"] and "present" not in sitems["properties"]
    with duo_setting(True):
        call = Call([_family_p()])
        story, left, _r = asyncio.run(video.write_dialogue_story(call, "가족이 아침 식탁에서 대화하는 영상", "", 60))
        assert call.calls[0]["format"] is video.DIALOGUE_SCHEMA and "present" in story["scenes"][0] and _scene_msgs(left) == [], left


def test_s3_14_rule_text_has_the_present_rules():
    """4절 14 (present 부분): 구성안 규칙 글(DIALOGUE_EXTRA_DUO)에 present 규칙과, present 에서 고르는 대화 컷 규칙이 있다. 꺼짐 규칙 글에는 없다.
    (STORY_SYSTEM 의 style_said 규칙·prompt_block 화풍 줄은 test_s3_14b 에서 본다)"""
    ex = video.DIALOGUE_EXTRA_DUO
    for want in ('Every scene also has "present": the ids of everyone who is in this place during this scene, whether the shot shows them or '
                 "not (people and animals).", '"characters" (who is seen) is always part of "present".',
                 "Someone stays present in the later scenes at the same place until a scene shows them leave or says they are away",
                 "Someone who leaves during a scene is present in that scene and not in the scenes after it; someone who arrives is present from "
                 "that scene on.", "A new place or a jump in time starts again from the people really there.",
                 'The speaker of a "talk" scene is always present.', 'is never present and speaks only in a "shot" scene.',
                 # fixC (11-25 최종 확인 05#2): 예전 줄 'The person spoken to ("listener") is always present' 는 그 자리에 없는 사람을 present 에 넣게 했다
                 'A "listener" id is always someone in "present"; someone you call or answer who is not in the place (behind a door, in another '
                 'room, on the phone, or who has just left) is never the listener: write "nobody". "everyone" means everyone in "present".',
                 '"characters" is the speaker first, then the person spoken to, then the others in "present", at most three people.'):
        assert want in ex, want
    assert '"characters" lists everyone who is in that place at that moment' not in ex          # 예전 짐작 설명은 present 규칙으로 옮겼다
    assert '"present"' not in video.DIALOGUE_EXTRA and '"present"' not in video.STORY_SYSTEM


def test_s3_later_stages_follow_present():
    """3차 명세 1.5: 다듬은 장면에 present 가 있으면 뒤 단계(scene_people·talk_group·_same_talk_group 과 이것으로 정하는 자리 지도·group_order·
    layout_slots·facing_of)가 그 칸으로 그 자리를 정한다. 없으면 지금 그대로"""
    s, _l = _r3_clean(_leave_plan("Niki grabs her school bag and waves goodbye."), exchange=False)
    with duo_setting(True):
        # 넓은 장면 [베스, 제임스, 니키]의 자리가 니키가 떠난 뒤의 투샷에도 이어진다 (베스 왼쪽·제임스 오른쪽), 니키 혼자 컷은 식탁 오른쪽 자리에서 왼쪽을 본다
        assert [x["id"] for x in video.layout_slots(s, 3, video.scene_people(s, 3))] == ["A", "B"]
        assert [x["id"] for x in video.layout_slots(s, 4, video.scene_people(s, 4))] == ["A", "B"]
        assert video.group_order(s, 1, ["C", "B", "A"]) == ["A", "B", "C"] and video.facing_of(s, 2, "C") == "left"
        # 그 자리에 없는 말하는 사람은 그 장소에 그리지 않는다 (손으로 고친 이야기: 말 없는 니키 장면에 베스의 목소리)
        t = copy.deepcopy(s)
        t["scenes"][2].update(speaker="A", line_ko="니키야, 잘 다녀와!", emotion="warm")
        t["scenes"][2]["present"] = ["B", "C"]
        assert video.scene_people(t, 2) == ["C"]
        t["scenes"][2]["present"] = ["A", "B", "C"]
        assert video.scene_people(t, 2) == ["C", "A"]
        del t["scenes"][2]["present"]
        assert video.scene_people(t, 2) == ["C", "A"]                     # 칸이 없으면 예전 그대로
        # talk_group: 독백 표시가 있거나 present 밖의 사람은 세지 않는다
        t["scenes"][3]["present"] = ["A"]
        assert video.talk_group(t, 3) is None
        t["scenes"][3]["present"] = ["A", "B"]
        t["scenes"][3]["monologue"] = True
        assert video.talk_group(t, 3) is None
        t["scenes"][3]["monologue"] = False
        assert video.talk_group(t, 3) == ["A", "B"]
    # _same_talk_group: 화면의 사람·장소만 본다 — 화면 밖 사람이 그 사이에 나가거나 들어와도 끝 프레임은 그대로라 잇는다 (fixA: 규칙 글의
    # '같은 사람·같은 장소'를 지킨 구성안을 까닭 없이 거절했다). 화면의 사람이 바뀌면 잇지 않는다
    cast = video._story_cast(s)
    prev, cur = dict(s["scenes"][3]), dict(s["scenes"][4])
    assert video._same_talk_group(prev, cur, {"A", "B"}, cast)
    assert video._same_talk_group(prev, dict(cur, present=["A", "B", "C"]), {"A", "B"}, cast)
    assert not video._same_talk_group(prev, dict(cur, characters=["A", "B", "C"]), {"A", "B", "C"}, cast)
    pcast = video._cast_view({**{c["id"]: c for c in s["bible"]["characters"]}, "D": {"look": "a small white puppy with floppy ears"}})
    assert video._same_talk_group(prev, dict(cur, present=["A", "B", "D"]), {"A", "B"}, pcast)
    assert video._same_talk_group({k: v for k, v in prev.items() if k != "present"}, dict(cur, present=["A", "B", "C"]), {"A", "B"}, cast)


# ── 3차 명세 2절 (11-25 "방식 바꾸기"): 사용자가 말한 화풍 칸 bible.style_said — 구성안 모델이 요청에서 사용자 말을 그대로 옮겨 적고, 코드는 그
#    구절을 열쇠 하나로만 바꾼다 (요청 글을 낱말로 짐작하지 않는다). 칸이 없는 구성안은 예전 요청 글 경로(said_level) 그대로 ──
_PHOTO_PLAN = "photorealistic cinematic live action"


def _said_applied(style, said, text=""):
    """라이브러리 인물(니키) 하나의 설정집에 apply_to_story 를 한 뒤의 화풍. said 가 None 이면 style_said 칸이 없다 (예전 구성안)"""
    from app import cast_lib
    bible = {"characters": [{"id": "A", "name_ko": "니키", "look": "x"}], "style": style}
    if said is not None:
        bible["style_said"] = said
    st = {"bible": bible}
    cast_lib.apply_to_story(st, cast_lib.library(), text, keep_style=bool(style) and not cast_lib.is_photo_style(style))
    return st["bible"]["style"]


@contextlib.contextmanager
def _fake_story_llm(story):
    """구성안 모델 대신 story 를 돌려주는 가짜 app.llm. 받은 지시문(payload)을 모은다"""
    turns = []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        async def call(payload):
            turns.append(payload)
            return {"message": {"content": json.dumps(story)}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        yield turns
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved


def test_s3_13b_schema_asks_for_style_said_in_every_story_schema():
    """4절 13 (style_said 부분): STORY_SCHEMA 의 bible 은 style_said(문자열)를 요구한다. 대사 영상 칸(DIALOGUE_SCHEMA·꺼짐 SOLO)은 이 칸을 베껴
    같다 (화풍은 대사가 있든 없든 같은 기능이다). 사용자 말을 먼저 적고 그 화풍을 쓰게 style 바로 앞에 둔다. present 는 여전히 두세 사람 규칙
    칸에만 있다"""
    for schema in (video.STORY_SCHEMA, video.DIALOGUE_SCHEMA, video.DIALOGUE_SCHEMA_SOLO):
        b = schema["properties"]["bible"]
        assert "style_said" in b["required"] and b["properties"]["style_said"] == {"type": "string"}
        keys = list(b["properties"])
        assert keys.index("style_said") == keys.index("style") - 1, keys
    assert "present" not in video.DIALOGUE_SCHEMA_SOLO["properties"]["scenes"]["items"]["properties"]
    assert "present" in video.DIALOGUE_SCHEMA["properties"]["scenes"]["items"]["required"]
    # 구성안을 쓰는 길마다 이 칸을 보낸다 (대사 없는 영상, 대사 영상 꺼짐·켜짐)
    call = Call([STORY_OK])
    asyncio.run(video.write_story(call, "비 오는 저녁 우산 쓴 남자가 집에 가는 30초 영상", "", 30))
    assert "style_said" in call.calls[0]["format"]["properties"]["bible"]["required"]
    for on in (False, True):
        with duo_setting(on):
            call = Call([DLG_OK] * 3)
            asyncio.run(video.write_dialogue_story(call, "니키와 푸토르가 숲길에서 대화", "", 48))
            assert "style_said" in call.calls[0]["format"]["properties"]["bible"]["required"], on


def test_s3_14b_rule_text_has_the_style_said_rules_and_one_library_style_line():
    """4절 14 (style_said 부분): STORY_SYSTEM 에 style_said 규칙(사용자 말 그대로·화풍 말만·이야기 속 물건은 아님·거절한 화풍은 빼고·나중 말을
    따름·말하지 않으면 "")이 style 줄 바로 앞에 있고, style 줄은 style_said 의 화풍을 그린다. 영어 규칙의 예외에 style_said 가 있다.
    라이브러리 인물이 있을 때 prompt_block 의 화풍 줄은 요청 글과 상관없이 한 가지다"""
    from app import cast_lib
    sysm = video.STORY_SYSTEM
    for want in ('"style_said": the user\'s own words for the look of the film, copied exactly from the request (Korean or English)',
                 '"애니메이션", "수채화 느낌으로", "3D 픽사 스타일", "실사", "pixel art"',
                 "Copy only words that say how the film should look (an art style, a medium, animation or live action, the look of a kind of "
                 "film or of a studio)",
                 "never things inside the story (a felt doll a character makes, a pencil sketch she draws, a comic book he reads, a song, a topic)",
                 'If the request turns one look down for another ("반실사 말고 애니메이션으로"), copy only the wanted look ("애니메이션").',
                 "If the request holds an earlier message and a newer one, follow the newer one.",
                 '"" when the request does not ask for a look.',
                 'when "style_said" is not empty, the film is drawn in exactly that look',
                 'when "style_said" is "", write "photorealistic cinematic live action".',
                 'English everywhere except "title_ko", "logline_ko", "ko" and bible "style_said"'):
        assert want in sysm, want
    assert sysm.index('  - "light"') < sysm.index('  - "style_said"') < sysm.index('  - "style":')
    assert '"present"' not in sysm
    sysm.format(seconds=60, keys="C major", count=6, n_min=4, n_max=9, tolerance=6)          # 중괄호가 깨지지 않는다
    lib = cast_lib.library()
    lib_style = lib[0]["style"]
    # fixC (11-25 최종 확인 05#1): 거절만 한 요청('반실사 말고 다른 그림체로'·'지금 그림체 말고 다른 스타일로')도 화풍을 말한 것이다 — 예전 줄은
    # '특정 화풍을 청했으면 …, 아니면 "" 와 라이브러리 화풍' 이라 거절한 반실사를 베끼게 했다. fixD: 예문은 인물 그림체 거절 '원래 그림체 말고'
    # ('지금 그림체' 는 고치기 차례에 앞 영상의 화풍이다)
    line = ("Visual style of the whole film: if the request says anything about the look (asks for a look, or turns a look down, for example "
            "\"반실사 말고 다른 그림체로\" or \"원래 그림체 말고 다른 스타일로\"), write the user's words into bible.style_said and write into "
            "bible.style the look asked for, or a look you choose that is not the refused one; only when the request says nothing about the "
            "look or only keeps the characters' own look (\"원래 그림체 그대로\") is bible.style_said \"\" and you copy this into bible.style: "
            + lib_style)
    texts = ("", "니키와 푸토르가 대화", "니키와 푸토르가 대화하는 2D 애니메이션", "니키가 걷는 실사 영화", "수채화 느낌으로 니키", "반 고흐 스타일로 니키",
             "디즈니처럼 니키", "니키가 만화책을 읽는다", "니키 원래 그림체 그대로")
    for speech, by_id in ((True, False), (False, True)):
        blocks = {cast_lib.prompt_block(lib, speech=speech, by_id=by_id, text=t) for t in texts}
        assert len(blocks) == 1, (speech, by_id)
        assert [x for x in blocks.pop().split("\n") if x.startswith("Visual style")] == [line]
    # 라이브러리 화풍이 없는 인물만 있으면 베낄 화풍 없이 같은 줄
    assert cast_lib.prompt_block([dict(lib[0], style="")]).split("\n")[-1] == line.split(" and you copy this")[0] + "."


def test_s3_15_style_said_phrase_to_key_and_fill_table():
    """4절 15: 사용자의 화풍 구절 → 열쇠(said_phrase_key)·채울 글(style_said_fill) 표. 구절 전체를 화풍 말로 읽는다 — 표지 말이 없어도 되고
    ('유화'·'도트'·'실사'), 물건·활동 거르기와 부정 처리는 하지 않는다(구성안 모델이 했다). 모르는 말·애매한 말('고흐 그림처럼'·'디즈니
    애니메이션처럼')은 None 이라 구성안이 정한 화풍(실사·반실사 말고)을 둔다. 채운 글은 그 열쇠로 읽힌다"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    a, p, t = (cast_lib.SAID_STYLE_TEXT[k] for k in ("anime", "photo", "toon3d"))
    table = (
        ("애니메이션", "anime", a), ("애니", "anime", a), ("웹툰 스타일", "anime", a), ("지브리 느낌", "anime", a), ("2D 애니메이션", "anime", a),
        ("만화 그림체", "anime", a), ("손그림 느낌", "anime", a), ("카툰 스타일", "anime", a), ("anime", "anime", a), ("manga style", "anime", a),
        ("3D 애니", "toon3d", t), ("픽사 스타일", "toon3d", t), ("3D 픽사 스타일", "toon3d", t), ("CG 애니메이션", "toon3d", t),
        ("드림웍스 느낌", "toon3d", t), ("디즈니 3D", "toon3d", t), ("3D로", "toon3d", t), ("Pixar style", "toon3d", t),
        ("실사", "photo", p), ("실제 배우", "photo", p), ("사실적으로", "photo", p), ("실사 영화처럼", "photo", p), ("사진 느낌", "photo", p),
        ("live action", "photo", p), ("realistic", "photo", p),
        ("반실사", "semi", lib), ("2.5D", "semi", lib), ("반실사 수채화", "semi", lib),
        ("수채화 느낌으로", "custom", "watercolour painting style"), ("유화", "custom", "oil painting style"),
        ("클레이 애니메이션", "custom", "claymation style"), ("픽셀 아트", "custom", "pixel art style"), ("도트", "custom", "pixel art style"),
        ("8비트", "custom", "pixel art style"), ("수묵 담채화", "custom", "ink wash painting style"), ("pixel art", "custom", "pixel art style"),
        ("watercolor", "custom", "watercolour painting style"), ("3D 클레이", "custom", "claymation style"),
        ("흑백 연필 스케치", "custom", "black-and-white pencil sketch style"),
        ("수채화 동화풍", "custom", "watercolour painting and storybook illustration style"), ("스톱모션", "custom", "stop-motion style"),
        ("고흐 그림처럼", None, lib), ("디즈니 애니메이션처럼", None, lib), ("디즈니 스타일", None, lib), ("사이버펑크 느낌", None, lib),
        ("Disney animation", None, lib))
    assert len(table) >= 25
    for phrase, key, fill in table:
        assert cast_lib.said_phrase_key(phrase) == key, (phrase, cast_lib.said_phrase_key(phrase))
        assert cast_lib.style_said_fill(phrase, _PHOTO_PLAN, lib) == fill, (phrase, cast_lib.style_said_fill(phrase, _PHOTO_PLAN, lib))
        # 라이브러리 인물 영화에서 구성안이 실사로 썼으면 그 구절의 화풍 글로 (실사 구절이면 구성안 그대로)
        assert _said_applied(_PHOTO_PLAN, phrase) == (_PHOTO_PLAN if key == "photo" else fill), phrase
        if key in ("anime", "photo", "toon3d", "custom"):
            assert cast_lib.style_key(fill) == key, (phrase, fill)
    assert cast_lib.said_phrase_key("") is None and cast_lib.said_phrase_key(None) is None
    assert cast_lib.custom_style_text("유화") == "" and cast_lib.custom_style_text("유화", phrase=True) == "oil painting style"    # 요청 글 경로는 그대로
    # 구성안이 그 구절을 따랐으면 그대로 둔다: 열쇠가 같고, custom 이면 구절의 재료 말을 담고, None 이면 실사·반실사가 아닌 글
    for phrase, plan in (("애니메이션", "2D anime feature film with clean line art"), ("픽사 스타일", "stylized 3D animated feature film with soft shading"),
                         ("실사", _PHOTO_PLAN), ("반실사", "semi-realistic painterly 2.5D film look"),
                         ("수채화 느낌으로", "soft watercolor painting with visible paper grain"), ("3D 클레이", "3D claymation with handmade clay textures"),
                         ("도트", "16-bit pixel art with a limited palette"), ("흑백 연필 스케치", "black-and-white pencil sketch with soft graphite shading"),
                         ("고흐 그림처럼", "Van Gogh inspired post-impressionist oil painting with swirling brushstrokes"),
                         ("디즈니 애니메이션처럼", "classic hand-drawn 2D animation"), ("디즈니 애니메이션처럼", "stylized 3D animated feature film look")):
        assert cast_lib.keeps_style_said(phrase, plan) and _said_applied(plan, phrase) == plan, (phrase, plan)
    # 따르지 않았으면 채운다: 재료 말이 다른 custom, 반실사인데 None, 빈 화풍
    assert _said_applied("16-bit pixel art", "수채화 느낌으로") == "watercolour painting style"
    assert _said_applied("semi-realistic digital painting", "고흐 그림처럼") == lib
    assert _said_applied("", "애니메이션") == a and _said_applied("", "고흐 그림처럼") == lib
    assert not cast_lib.keeps_style_said("", "2D anime") and not cast_lib.keeps_style_said("애니메이션", "")


def test_s3_16_empty_style_said_with_library_people_uses_the_library_style():
    """4절 16: style_said 가 "" (사용자가 화풍을 말하지 않음) 이고 라이브러리 인물이 있으면 구성안이 애니·수채·3D 를 썼어도 라이브러리 화풍
    (승인된 인물 그림체). 칸이 있으면 요청 글을 낱말로 짐작하지 않는다. 라이브러리 인물이 없는 영화는 구성안 화풍 그대로"""
    from app import cast_lib
    lib_style = cast_lib.library()[0]["style"]
    for plan in ("2D anime feature film with clean cel-shaded line art", _PHOTO_PLAN, "soft watercolour storybook illustration",
                 "stylized 3D animated feature film", "whimsical fantasy concept art", ""):
        for text in ("니키와 푸토르가 대화", "니키와 푸토르가 대화하는 애니메이션", "수채화 느낌으로 니키와 푸토르"):
            assert _said_applied(plan, "", text) == lib_style, (plan, text)
    other = {"bible": {"characters": [{"id": "Z", "name_ko": "이웃", "look": "a tall man in a grey coat"}], "style": "2D anime", "style_said": ""}}
    assert cast_lib.apply_to_story(other, cast_lib.library(), "이웃이 걷는다") == {} and other["bible"]["style"] == "2D anime"
    # 구성안 길 전체 (storyboard): 모델이 style_said "" 와 애니 화풍을 쓰면 라이브러리 화풍, 사용자 말을 적으면 그 화풍
    story = copy.deepcopy(DLG_OK)
    story["scenes"][3]["continues"] = False
    for said, style, want in (("", "2D anime film with clean cel-shaded line art", lib_style),
                              ("애니메이션", _PHOTO_PLAN, cast_lib.SAID_STYLE_TEXT["anime"]),
                              ("애니메이션", "2D anime film with clean cel-shaded line art", "2D anime film with clean cel-shaded line art")):
        story["bible"].update(style_said=said, style=style)
        with _fake_story_llm(story) as turns:
            r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키와 푸토르가 숲속에서 대화하는 영상",
                                             seconds=48, dialogue=True))
        assert r["story"]["bible"]["style_said"] == said and r["story"]["bible"]["style"] == want, (said, style, r["story"]["bible"]["style"])
        assert r["cast"] == {"A": "niki", "B": "putor"} and "bible.style_said" in turns[0]["messages"][0]["content"]
    # 대사 없는 영상도 같다 (지시문은 id 칸)
    silent = {"title_ko": "숲길", "logline_ko": "두 사람이 숲길을 걷는다.",
              "bible": {"characters": [{"id": "putor", "look": "an old man"}], "places": [{"id": "P1", "look": "a winding forest path"}],
                        "time": "morning", "light": "soft", "style_said": "", "style": "2D anime film"},
              "music": {"tags": "calm piano", "bpm": 70, "key": "D major", "ko": "잔잔한 피아노"},
              "scenes": [{"seconds": 14, "place": "P1", "characters": ["putor"], "continues": False, "shot": "wide shot",
                          "action": "He walks along the path.", "sound": "footsteps", "ko": "걷는다"},
                         {"seconds": 14, "place": "P1", "characters": ["putor"], "continues": False, "shot": "medium shot",
                          "action": "He stops and looks ahead.", "sound": "birds", "ko": "멈춘다"}]}
    with _fake_story_llm(silent):
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "푸토르가 숲길을 걷는 애니메이션", seconds=28))
    assert r["cast"] == {"putor": "putor"} and r["story"]["bible"]["style"] == lib_style


def test_s3_17_plans_without_style_said_take_the_old_request_path():
    """4절 17: style_said 칸이 없는 구성안(예전에 저장된 것, 모델이 빠뜨린 것)은 예전 경로 그대로 요청 글을 읽는다 (said_level·keeps_said_style·
    said_style_fill — 기존 시험이 그대로 통과한다). 다듬기는 칸이 없는 것과 ""(말하지 않음)를 가른다: 칸이 문자열일 때만 남긴다(120자까지)"""
    from app import cast_lib
    lib_style = cast_lib.library()[0]["style"]
    anime = cast_lib.SAID_STYLE_TEXT["anime"]
    raw = copy.deepcopy(DLG_OK)
    assert "style_said" not in video.clean_story(raw, 48)["bible"] and "style_said" not in video.clean_dialogue_story(raw, 48)["bible"]
    for bad in (None, 3, ["애니메이션"], {"said": "애니메이션"}):
        raw["bible"]["style_said"] = bad
        assert "style_said" not in video.clean_story(raw, 48)["bible"], bad
        assert not any("style_said" in x for x in video.story_problems(raw, 48)), bad
    for said, kept in (("", ""), ("  수채화   느낌으로 ", "수채화 느낌으로"), ("애" * 200, "애" * 120)):
        raw["bible"]["style_said"] = said
        assert video.clean_story(raw, 48)["bible"]["style_said"] == kept and video.clean_dialogue_story(raw, 48)["bible"]["style_said"] == kept
    # 같은 구성안·같은 요청 글: 칸이 없으면 요청 글을 읽고(애니 요청 → 애니 글), "" 이면 라이브러리 화풍, 구절이 있으면 그 구절
    ask = "니키와 푸토르가 대화하는 애니메이션"
    assert _said_applied(_PHOTO_PLAN, None, ask) == anime
    assert _said_applied(_PHOTO_PLAN, "", ask) == lib_style
    assert _said_applied(_PHOTO_PLAN, "수채화 느낌으로", ask) == "watercolour painting style"
    plain = "니키와 푸토르가 대화"
    assert _said_applied(_PHOTO_PLAN, None, plain) == lib_style and _said_applied("2D anime film", None, plain) == "2D anime film"
    assert _said_applied("2D anime film", "", plain) == lib_style and _said_applied("2D anime film", "애니메이션", plain) == "2D anime film"
    # 예전 경로의 화풍일 수도 있는 말(maybe)도 그대로: 구성안이 쓴 화풍(실사 말고)을 두고, 실사면 라이브러리 화풍
    assert _said_applied("art deco poster style", None, "니키와 푸토르, 아르데코 풍으로") == "art deco poster style"
    assert _said_applied(_PHOTO_PLAN, None, "니키와 푸토르, 아르데코 풍으로") == lib_style


def test_s3_18_style_said_and_style_must_agree():
    """4절 18 (3차 명세 2.4b): 칸끼리만 본다 — style_said 가 열쇠를 주는데 bible.style 의 열쇠가 다르면, 열쇠가 None 인데 bible.style 이 실사거나
    비었으면 'bible.style must describe the look in bible.style_said ("…")'. 대사 없는 영상(story_problems)·대사 영상(dialogue_problems) 모두
    한 번씩. style_said 는 사용자 말 그대로라 영어 검사를 하지 않는다. 칸이 없거나 "" 이면 보지 않는다 (요청 글은 읽지 않는다)"""
    def msgs(said, style, dialogue=False):
        d = copy.deepcopy(DLG_OK if dialogue else STORY_OK)
        d["bible"]["style"] = style
        if said is not None:
            d["bible"]["style_said"] = said
        left = video.dialogue_problems(d, 48, True) if dialogue else video.story_problems(d, 30)
        return [x for x in left if "bible.style" in x]

    from app import cast_lib

    def bad(said, style):
        # 둘째 문장은 걸린 까닭(구성안 화풍 글)에 맞춘다 (fixB)
        return [f'bible.style must describe the look in bible.style_said ("{said}"). ' + cast_lib.style_said_hint(said, style)]

    def empty(style):
        # fixC (11-25 최종 확인 05#1): 첫 갈래가 화풍을 거절한 요청도 덮는다 (예전 문구는 거절을 지킨 구성안을 라이브러리 반실사로 돌려보냈다).
        # fixD: 사용자의 가장 새 말로 한정한다 (앞 메시지의 거절 뒤 새 메시지가 '원래 그림체 그대로' 면 옮길 말이 없다)
        return [f'bible.style_said is "", but bible.style describes a particular look ("{style}"): if the user' "'" 's newest words about the '
                'look (the newer message, when the request holds an earlier one) ask for this look or turn a look down (for example '
                '"반실사 말고 다른 그림체로"), copy those words into bible.style_said; otherwise write the style the rules give when '
                'bible.style_said is "".']
    for dialogue in (False, True):
        for said, style, want in (("애니메이션", _PHOTO_PLAN, bad("애니메이션", _PHOTO_PLAN)),
                                  ("애니메이션", "2D anime feature film with clean line art", []),
                                  ("수채화 느낌으로", "soft watercolor painting", []), ("수채화 느낌으로", "2D anime film", bad("수채화 느낌으로", "2D anime film")),
                                  ("실사", _PHOTO_PLAN, []),
                                  ("실사", "semi-realistic painterly 2.5D film look", bad("실사", "semi-realistic painterly 2.5D film look")),
                                  ("반실사", "semi-realistic digital painting", []), ("반실사", "", bad("반실사", "")),
                                  ("3D 픽사 스타일", "stylized 3D animated feature film", []), ("3D 픽사 스타일", "2D anime", bad("3D 픽사 스타일", "2D anime")),
                                  ("고흐 그림처럼", "post-impressionist oil painting with swirling brushstrokes", []),
                                  ("고흐 그림처럼", "semi-realistic digital painting", bad("고흐 그림처럼", "semi-realistic digital painting")),
                                  ("고흐 그림처럼", _PHOTO_PLAN, bad("고흐 그림처럼", _PHOTO_PLAN)), ("고흐 그림처럼", "", bad("고흐 그림처럼", "")),
                                  ("", _PHOTO_PLAN, []), ("", "2D anime", empty("2D anime")), ("", "semi-realistic digital painting", []),
                                  (None, _PHOTO_PLAN, []), (None, "2D anime", []), (None, "", [])):
            assert msgs(said, style, dialogue) == want, (dialogue, said, style, msgs(said, style, dialogue))
    assert cast_lib.style_said_fits("", _PHOTO_PLAN) and cast_lib.style_said_fits("  ", "") and cast_lib.style_said_fits(None, "2D anime")
    # 한국어 구절은 영어 검사에 걸리지 않는다. bible.style 의 한국어는 영어 검사가 그대로 잡는다 (열쇠는 같아 칸끼리 검사는 통과)
    assert video.story_problems(dict(STORY_OK, bible=dict(STORY_OK["bible"], style_said="애니메이션", style="2D anime film")), 30) == []
    assert msgs("수채화 느낌으로", "수채화 느낌") == ["bible.style must be an English sentence."]
    # 구성안 고쳐 쓰기: 첫 답이 어긋나면 고칠 말로 보내고, 고친 답을 받는다
    first, fixed = copy.deepcopy(STORY_OK), copy.deepcopy(STORY_OK)
    first["bible"].update(style_said="애니메이션", style=_PHOTO_PLAN)
    fixed["bible"].update(style_said="애니메이션", style="2D anime feature film with clean line art")
    call = Call([first, fixed])
    story, left, rounds = asyncio.run(video.write_story(call, "비 오는 저녁 우산 쓴 남자가 집에 가는 30초 애니메이션", "", 30))
    assert rounds == 2 and left == [] and story["bible"]["style_said"] == "애니메이션" and story["bible"]["style"].startswith("2D anime")
    assert 'bible.style must describe the look in bible.style_said ("애니메이션").' in call.calls[1]["messages"][-1]["content"]


def test_s3_19_intent_r6b_is_unchanged():
    """4절 19 (3차 명세 2.5): 의도 규칙 R6b 는 그대로다 — 채팅 말에서 '다시 만들어'를 그대로 반복할지(R6b) 고칠지(R6) 정하는 갈림은 구성안 모델을
    부르기 전이라 style_said 가 없고, 지금처럼 채팅 말의 화풍 말(said_level 'sure')로 정한다 (고침 4의 변경 포함)"""
    from app.studio import intent
    vid = {"media": "video", "action": "t2v"}
    # 화풍일 수도 있는 말(maybe: '반 고흐 스타일로')만 있으면 지금처럼 그대로 반복이다
    for t in ("다시 만들어 줘", "한 번 더 만들어 줘", "하나 더", "니키가 웃는 장면으로 다시 만들어 줘", "만화책을 읽는 니키로 다시 만들어 줘",
              "반 고흐 스타일로 다시 만들어 줘"):
        r = intent.decide(t, 0, vid)
        assert (r["rule"], r["carry"]) == ("R6b", "repeat"), (t, r["rule"])
    for t in ("애니메이션으로 다시 만들어 줘", "수채화 느낌으로 다시 만들어 줘", "픽사 스타일로 다시 만들어 줘", "실사로 다시 만들어 줘"):
        r = intent.decide(t, 0, vid)
        assert (r["rule"], r["action"], r["carry"]) == ("R6", "t2v", "revise"), (t, r["rule"])
    assert intent.decide("하나 더", 0, {"media": "image", "action": "image"})["rule"] == "R6b"


# ── fixA (11-25): 3차 명세 구현 검증 지적 고침. 새 방식(present·style_said) 안에서 고치고 낱말 짐작 예비는 넓히지 않는다 ──
def test_fa_01_a_self_listener_is_a_monologue_and_an_unknown_listener_adds_nobody():
    """fixA 1: 새 방식에서 듣는 사람이 말하는 사람 자신(자기 id·자기 이름)이면 독백이다 — 추정하면 'whispers to herself' 클로즈업이 세 사람 대화 컷이
    되고 검사 문구 둘('talking to oneself → nobody' 와 'add B')이 서로 반대로 시켰다 (규칙 글·예전 경로·고침 2 와 같게, G0 '독백은 클로즈업').
    모르는 듣는 사람('Niki')이면 그것만 말하고 추정한 사람을 더하라고 하지 않는다"""
    abc = ["A", "B", "C"]
    plan = [_pr(_r3_shot("P1", abc, "wide shot", "The family sits around the breakfast table.", "아침 식탁", 10), abc),
            _pr(_r5_talk("P1", abc, "A", "B", "여보, 니키 생일 선물 준비했어요?", shot="medium group shot", action="Beth leans toward James."), abc),
            None]
    for lis in ("C", "니키"):
        plan[2] = _pr(_r5_talk("P1", ["C"], "C", lis, "다들 내 생일 잊은 줄 알았는데...", shot="close-up", action="Niki whispers to herself."), abc)
        s, left = _r3_clean(plan, exchange=False)
        sc = s["scenes"][2]
        assert sc["characters"] == ["C"] and sc["monologue"] is True and sc["shot"] == "close-up" and sc["present"] == abc, (lis, sc)
        assert [x for x in left if x.startswith("Scene 3")] == [
            'Scene 3: "listener" is the speaker "C" (talking to oneself): write the id of the person in the place who is spoken to, or "nobody" '
            'for a monologue, which is a close-up of "C" alone.'], left
        with duo_setting(True):
            assert video.talk_group(s, 2) is None and not video.duo_talk(s, 2)
    plan[2] = _pr(_r5_talk("P1", ["C"], "C", "Niki", "다들 내 생일 잊은 줄 알았는데...", shot="close-up", action="Niki whispers to herself."), abc)
    _s, left = _r3_clean(plan, exchange=False)
    mine = [x for x in left if x.startswith("Scene 3")]
    assert any('"listener" "Niki" is not in bible.characters' in x for x in mine) and not any(" add " in x or 'put "' in x for x in mine), mine


def test_fa_02_two_looks_keep_one_look_in_present_and_in_the_cut():
    """fixA 2: 말하는 사람을 안경 쓴 제임스로, characters 에는 제임스로 적어도(같은 사람의 두 모습) 다듬은 present 와 대화 컷이 같은 모습을 쓴다 —
    예전에는 present 에 james, 컷에 james_glasses 가 남아 talk_group 이 말하는 사람을 빼고 두 사람 컷을 '화면에 혼자' LTX 글로 만들었다"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    for present in (["beth", "james"], ["beth", "james_glasses"]):
        raw = _variant_story()
        raw["scenes"] = [_pr(_r3_shot("P1", ["beth", "james"], "wide shot", "Beth and James sit at the breakfast table.", "아침 식탁", 12),
                             ["beth", "james"]),
                         _pr(_r5_talk("P1", ["james", "beth"], "james_glasses", "beth", "오늘 신문에 재미있는 기사가 났어."), present),
                         _pr(_r5_talk("P1", ["beth", "james_glasses"], "beth", "james_glasses", "어머, 무슨 기사요?", cont=True),
                             ["beth", "james_glasses"])]
        with duo_setting(True):
            s = video.clean_dialogue_story(raw, 24, exchange=True, known=known)
            left = video.dialogue_problems(raw, 24, True, exchange=True, known=known)
            assert s["scenes"][1]["characters"] == ["james_glasses", "beth"] and s["scenes"][1]["present"] == ["beth", "james_glasses"], present
            assert video.talk_group(s, 1) == ["james_glasses", "beth"] and video.duo_talk(s, 1), present
            cast_lib.apply_to_story(s, known, "베스와 제임스, 니키의 아침 식탁")
            assert video.talk_group(s, 1) == ["james_glasses", "beth"] and video.duo_talk(s, 1), present
        assert any('"james" and "james_glasses" are the same person in two looks' in x for x in left), left      # 검사는 그대로 고치게 한다


def test_fa_03_four_in_the_place_messages_fit_the_seats_left_and_an_unseen_leaving_still_continues():
    """fixA 3: 그 자리에 넷이 있으면 검사 문구가 남은 자리(사람 3명 − 이미 정한 사람)만큼만 더하라고 한다 — 빠진 사람을 모두 더하라는 문구를 따른 답이
    '사람 4명' 위반이 됐다. 화면에 없던 넷째 사람이 그 사이에 나가도 같은 셋·같은 장소의 대화 줄은 잇는다 (_same_talk_group 은 화면의 사람만 본다)"""
    gran = {"id": "E", "look": "a kind grandmother in her seventies with silver hair and a lilac shawl", "name_ko": "할머니",
            "voice": "A gentle grandmother.", "voice_ko": "할머니"}
    chars, four = _R3_FAM + [gran], ["A", "B", "C", "E"]
    wide = _pr(_r3_shot("P1", ["E", "A", "B", "C"], "wide shot", "The family sits around the breakfast table.", "가족의 아침 식탁", 10), four)
    plan = [wide, _pr(_r5_talk("P1", ["B", "A"], "B", "A", "여보, 커피 더 줄까요?"), four),
            _pr(_r5_talk("P1", ["E"], "E", "everyone", "얘들아, 밥 먹자!", shot="close-up"), four)]
    _s, left = _r3_clean(plan, chars=chars, exchange=False)
    assert ('Scene 2 is a conversation, so everyone in "present" is seen: add one more person from "present" ("C", "E") to "characters" after '
            'the speaker and the person spoken to (at most three people).') in left, left
    assert ('Scene 3: "E" speaks to everyone in the place, so everyone there must be in the shot: add 2 more people from "present" ("A", "B", "C") '
            'to "characters" (at most three people).') in left, left
    # 문구대로 고친 답은 더 걸리지 않는다
    plan[1] = _pr(_r5_talk("P1", ["B", "A", "C"], "B", "A", "여보, 커피 더 줄까요?", shot="medium group shot"), four)
    plan[2] = _pr(_r5_talk("P1", ["E", "A", "B"], "E", "everyone", "얘들아, 밥 먹자!", shot="medium group shot"), four)
    _s, left = _r3_clean(plan, chars=chars, exchange=False)
    assert not [x for x in left if x.startswith(("Scene 2", "Scene 3"))], left
    # 셋이 남으면 빠진 사람 모두 (지금처럼)
    three = [_pr(_R5_WIDE, ["A", "B", "C"]), _pr(_r5_talk("P1", ["B"], "B", "everyone", "자, 다 같이 축하하자!", shot="close-up"), ["C", "B", "A"])]
    assert ('Scene 2: "B" speaks to everyone in the place, so everyone there must be in the shot: add "C", "A" to "characters" '
            '(at most three people).') in _r3_clean(three, exchange=False)[1]
    # 화면에 없던 할머니가 장면 2 동안 나갔다: 같은 셋·같은 장소라 잇는다
    cont = [wide, _pr(_r5_talk("P1", ["B", "A", "C"], "B", "A", "여보, 커피 더 줄까요?", shot="medium group shot"), four),
            _pr(_r5_talk("P1", ["A", "B", "C"], "A", "B", "응, 고마워요.", shot="medium group shot", cont=True), ["A", "B", "C"])]
    s, left = _r3_clean(cont, chars=chars, exchange=False)
    assert s["scenes"][2]["continues"] is True and not any("cannot continue" in x for x in left), left


def test_fa_04_a_voice_from_elsewhere_needs_no_talk_scene_in_a_conversation():
    """fixA 4: 새 방식에서 어느 장면의 present 에도 없는 화자(문 밖·전화의 목소리)는 대화 요청이어도 얼굴이 보이는 말하기 장면을 요구하지 않는다 —
    따르려면 그 사람을 방 안 투샷에 들여야 했다. 규칙 글도 그렇게 말한다. present 가 없는 구성안은 지금 그대로"""
    fam2 = [_R3_FAM[0], _R3_FAM[2]]
    door = "The attic door stays still."
    scenes = [_pr(_r3_shot("P3", ["C"], "medium shot", "Niki sits on her bed by the round window.", "다락방의 니키"), ["C"]),
              _pr(_r3_shot("P3", ["C"], "close-up of the attic door", door, "문", speaker="A", line="니키야, 밥 먹어라!", emotion="calling"), ["C"]),
              _pr(_r5_talk("P3", ["C"], "C", "nobody", "엄마, 조금만요!", shot="close-up", action="Niki looks up from her book."), ["C"]),
              _pr(_r3_shot("P3", ["C"], "close-up of the attic door", door, "문", speaker="A", line="얼른 내려와!", emotion="firm"), ["C"]),
              _pr(_r5_talk("P3", ["C"], "C", "nobody", "네, 지금 가요!", shot="close-up", action="Niki closes her eyes and smiles."), ["C"])]
    _s, left = _r3_clean(scenes, chars=fam2, exchange=True)
    assert not any("speaks only as a voice-over" in x for x in left), left
    old = [{k: v for k, v in x.items() if k != "present"} for x in scenes]
    assert any('"A" speaks only as a voice-over' in x for x in _r3_clean(old, chars=fam2, exchange=True)[1])
    ex = video.DIALOGUE_EXTRA_DUO
    for want in ('Only someone who is in the place (in "present"), a person or an animal, can be "listener"; a line spoken to an animal names '
                 "that animal.", 'Such a voice needs no "talk" scene of its own, even in a conversation.'):
        assert want in ex and want not in video.DIALOGUE_EXTRA, want
    assert 'Only a person who is in the place' not in ex


def test_fa_05_a_request_that_names_a_known_person_has_a_person():
    """fixA 5 (전부터 있던 문제): 사람 낱말 없이 라이브러리 인물 이름만 쓴 요청('니키가 …')도 사람이 나오는 요청이다 — 니키를 빼지 않고는 고칠 수
    없는 'The bible adds a person' 위반이 고쳐 쓰기 차례를 헛돌렸다. 사람 없는 요청은 그대로"""
    from app import cast_lib
    known = cast_lib.known_in("니키가 정원에서 꽃을 바라보는 30초 영상")
    assert video._known_person(known) and not video._known_person([]) and not video._known_person(None)
    assert not video._known_person([{"id": "bori", "look": "a small white puppy with floppy ears"}])
    call = Call([STORY_OK])
    _story, left, _r = asyncio.run(video.write_story(call, "니키가 정원에서 꽃을 바라보는 30초 영상", "", 30, known=known))
    assert not any("adds a person" in x for x in left), left
    call = Call([STORY_OK] * 3)
    _story, left, _r = asyncio.run(video.write_story(call, "정원에서 꽃이 바람에 흔들리는 30초 영상", "", 30))
    assert any("adds a person" in x for x in left), left


def test_fa_06_they_and_them_for_things_are_not_other_people():
    """fixA 6: 독백 글의 they·them(5차 검토 9번)은 같은 문장 앞에 물건·동물의 복수 명사가 있으면 그것을 가리킨다 — 올바른 독백 컷이 검사에 걸리고
    혼자 대사 영상 글에서 동작 문장이 빠졌다. 사람 동작의 they·them 과 사람 복수 명사 뒤의 them 은 그대로 다른 사람이다"""
    cast = video._cast_view({"C": {"look": "a twelve-year-old girl with two long black braids", "name_ko": "니키"},
                             "A": {"look": "a calm neat woman around forty", "name_ko": "베스"}})
    for text in ("Niki watches the paper lanterns rise and smiles at them, her eyes shining.",
                 "Niki gathers the fallen leaves and watches them swirl in the wind.",
                 "Niki gazes at the fireflies as they dance around her, her face lit with wonder.",
                 "Niki looks up at the lanterns; they look like tiny stars to her.", "Niki picks up the seashells and grins at them.",
                 "Niki stares at the photos, then nods at them slowly."):
        assert video._other_mention(text, "C", cast) == "", text
        assert video._speaker_only(text, "C", cast) == text, text
    for text, want in (("Niki glances at them and sighs.", "glances at them"), ("While they chat, Niki sighs.", "they chat"),
                       ("Niki watches the guests and smiles at them.", "smiles at them"), ("Niki watches the two of them argue.", "the two of them"),
                       ("Niki watches her parents and smiles at them.", "parents")):
        assert video._other_mention(text, "C", cast) == want, (text, video._other_mention(text, "C", cast))


def test_fa_07_a_role_word_subject_does_not_take_the_speakers_name():
    """fixA 7: 라이브러리 밖 인물의 영어 이름을 장면 글에서 배울 때, 역할 말로 시작하는 줄('Mother smiles at Jiwoo')은 다음 대문자 낱말(듣는 사람)에
    표를 주지 않는다 — 표가 갈려 이름을 배우지 못해 독백 샷 'medium close-up of Jiwoo' 를 다른 사람으로 잡고 샷 글을 잘랐다"""
    chars = [{"id": "C", "look": "a ten-year-old girl with a short bob and a yellow raincoat", "name_ko": "지우", "voice": "A bright girl.",
              "voice_ko": "아이"},
             {"id": "A", "look": "a gentle woman around forty with a low bun and a green cardigan", "name_ko": "엄마", "voice": "A warm woman.",
              "voice_ko": "엄마"}]
    for with_present in (True, False):
        scenes = [_r3_shot("P1", ["C", "A"], "wide shot", "Jiwoo and her mother walk by the river.", "강가", 10),
                  _r5_talk("P1", ["C"], "C", "nobody", "오늘 하늘 진짜 예쁘다...", shot="medium close-up of Jiwoo, eye level, still camera",
                           action="Jiwoo looks up at the sky with a small smile."),
                  _r5_talk("P1", ["A", "C"], "A", "C", "우리 지우 기분 좋구나.", action="Mother smiles at Jiwoo.")]
        if with_present:
            scenes = [_pr(x, ["C", "A"]) for x in scenes]
        s, left = _r3_clean(scenes, chars=chars, exchange=False)
        assert s["scenes"][1]["shot"] == "medium close-up of Jiwoo, eye level, still camera", (with_present, s["scenes"][1]["shot"])
        assert not [x for x in left if x.startswith("Scene 2")], (with_present, left)
    assert video._text_names(scenes, {c["id"]: c for c in chars}) == {"C": {"Jiwoo"}}


def test_fa_08_style_said_phrase_keys():
    """fixA 8: 화풍 구절 열쇠 — 실사 영화로 읽는 영어 구절은 photo(화풍 글 표와 같게), 영화 종류·분위기 말은 None, 자리 채움 글('none'·'없음'·'N/A')은
    "" (말하지 않음), 디즈니 + 꾸밈말 + 움직이는 그림 말은 애매한 말(None), 색·분위기로 쓴 파스텔·코믹은 화풍이 아니고, 거절한 말('X 말고 Y')은 빼고
    읽는다. 르누아르는 누아르가 아니다"""
    from app import cast_lib
    # fixB: 화풍 글 표로 실사 영화로 읽는 영어 구절도 None 이다 (같은 뜻의 한국어 구절 '다큐멘터리처럼' 과 결과를 맞춘다, test_fb_07)
    for phrase, key in (("cinematic film look", None), ("35mm film look", None), ("documentary style", None),
                        ("다큐멘터리처럼", None), ("공포 영화처럼", None), ("판타지 영화처럼", None),
                        ("디즈니풍 애니메이션으로", None), ("Disney-style animation", None), ("Disney style animated film", None),
                        ("디즈니 스타일 애니메이션", None), ("디즈니 느낌의 애니메이션", None), ("디즈니 공주 애니메이션 느낌", None),
                        ("디즈니 3D 애니메이션", "toon3d"), ("파스텔 톤으로", None), ("파스텔 색감으로", None), ("pastel colors", None),
                        ("soft pastel palette", None), ("파스텔화", "custom"), ("코믹한 느낌으로", None), ("코믹 분위기로", None), ("comic mood", None),
                        ("코믹 느낌", "custom"), ("comic book style", "custom"), ("애니메이션 말고 실사로", "photo"), ("애니메이션이 아니라 실사로", "photo"),
                        ("3D 말고 2D 애니메이션으로", "anime"), ("픽사 말고 지브리 느낌", "anime"), ("반실사 말고 다른 그림체로", None),
                        ("반실사 말고 애니메이션으로", "anime"), ("not anime, live action", "photo"), ("르누아르 그림처럼", None), ("필름 누아르", "custom"),
                        ("none", None), ("없음", None), ("N/A", None), ("-", None)):
        assert cast_lib.said_phrase_key(phrase) == key, (phrase, cast_lib.said_phrase_key(phrase))
    for v in ("none", "None", "없음", "N/A", "n/a", "-", "  —  ", "해당 없음", '"none"', "", None):
        assert cast_lib.said_text(v) == "", v
    assert cast_lib.said_text("  수채화   느낌으로 ") == "수채화 느낌으로" and cast_lib.said_text("노을") == "노을"
    assert cast_lib.custom_style_text("파스텔 톤으로", phrase=True) == "" and cast_lib.custom_style_text("르누아르 그림처럼", phrase=True) == ""
    assert cast_lib.custom_style_text("애니메이션 말고 수채화로", phrase=True) == "watercolour painting style"


_FA_LIB_SEMI = "semi-realistic digital painting, 2.5D concept art"


def test_fa_09_style_said_check_and_library_override_agree():
    """fixA 9: 2.4b 검사(style_said_fits)와 라이브러리 덮어쓰기(keeps_style_said)가 같은 기준을 쓴다.
    - 열쇠가 달라도 같은 화풍: 지브리 구절 + 'hand-drawn 2D animation with soft watercolour backgrounds', 3D 구절 + 'clay-like textures', 흑백 누아르
      구절 + 실사 누아르 글, '만화'·'웹툰' 구절 + 코믹 글, '동화 같은 애니메이션' + 동화풍 2D 애니
    - 열쇠가 None 인 구절: 영화 종류·분위기 말은 실사 글도 맞다(라이브러리 영화는 예전처럼 라이브러리 화풍), 그림·화가 말인데 실사·라이브러리 같은
      반실사 글이면 틀리다, 'painted'·'painterly' 글은 둔다(모네·고흐), 재료 없는 장르 글은 검사는 지나고 라이브러리 영화에서만 라이브러리 화풍
    - custom 구절은 검사도 그 재료 말을 본다 (수채 구절 + 픽셀 글은 틀리다)"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    # fixB 로 바뀐 줄: 영어 필름 룩 구절은 None 이라 라이브러리 영화는 라이브러리 화풍(한국어 구절과 같게), None 구절의 반실사 글('판타지 영화처럼' +
    # 라이브러리 화풍·'파스텔 톤으로'·'painted' 글)은 검사가 고쳐 쓰게 한다(칸끼리, test_fb_05), 흑백·누아르 구절의 실사 글은 틀리고 사진 쪽 custom 으로
    # 채운다(test_fb_04), 반실사를 거절한 구절은 거절한 반실사로 채우지 않는다(test_fb_06)
    rows = (  # (구절, 구성안 화풍, 검사 통과, 라이브러리 영화의 결과 — "KEEP" 은 구성안 화풍 그대로)
        ("cinematic film look", "cinematic film look with moody rain light", True, lib),
        ("35mm film look", "35mm film look, live-action, soft grain", True, lib),
        ("다큐멘터리처럼", "documentary-style handheld live-action footage with natural light", True, lib),
        ("공포 영화처럼", _PHOTO_PLAN, True, lib),
        ("판타지 영화처럼", "epic high-fantasy look with glowing magical light", True, lib),
        ("레트로 느낌으로", "retro 1970s look with warm faded colours", True, lib),
        ("판타지 영화처럼", lib, False, lib),
        ("반 고흐 스타일로", lib + ", swirling Van Gogh-like sky", False, lib),
        ("고흐 그림처럼", _PHOTO_PLAN, False, lib),
        ("디즈니 애니메이션처럼", _PHOTO_PLAN, False, lib),
        ("모네 느낌으로", "soft impressionist painted look with dappled light and loose brushwork", False, "KEEP"),
        ("고흐 그림처럼", "expressive painterly style with swirling brushstrokes, thick paint and vivid colours", False, "KEEP"),
        ("사이버펑크 느낌", "neon cyberpunk illustration with glowing signs", True, "KEEP"),
        ("디즈니풍 애니메이션으로", "stylized 3D animated feature film with soft shading", True, "KEEP"),
        ("파스텔 톤으로", lib + ", soft pastel tones", False, lib),
        ("애니메이션 말고 실사로", _PHOTO_PLAN, True, "KEEP"),
        ("애니메이션 말고 다른 느낌으로", _PHOTO_PLAN, True, lib),           # 거절한 말은 그림 말로 세지 않는다
        ("반실사 말고 다른 그림체로", _PHOTO_PLAN, False, cast_lib.SAID_STYLE_TEXT["anime"]),
        ("반실사 말고 다른 그림체로", "2D anime feature film with clean cel-shaded line art", True, "KEEP"),
        ("지브리 느낌으로", "gentle hand-drawn 2D animation with lush painted watercolor backgrounds and soft natural colours", True, "KEEP"),
        ("지브리 느낌으로", "Japanese hand-drawn animation with lush gouache backgrounds", True, "KEEP"),
        ("2D 애니메이션", "2D animated feature look with crayon-textured backgrounds", True, "KEEP"),
        ("3D 애니메이션", "3D animated feature film with soft clay-like textures and warm lighting", True, "KEEP"),
        ("3D 픽사 스타일로", "stylized 3D render with soft clay-like materials", True, "KEEP"),
        ("흑백 누아르 영화처럼", "black-and-white film noir live action with hard shadows", False, "black-and-white film noir style"),
        ("흑백 영화처럼", "black-and-white live-action film with soft film grain", False, "black-and-white film style"),
        ("만화처럼", "comic book illustration style with bold ink outlines", True, "KEEP"),
        ("웹툰 스타일", "clean digital comic illustration with bold outlines", True, "KEEP"),
        ("동화 같은 애니메이션", "storybook-like 2D animation with soft colours", True, "KEEP"),
        ("르누아르 그림처럼", "warm impressionist painting with soft feathery brushstrokes and glowing colours", True, "KEEP"),
        # 지금 그대로여야 하는 것
        ("애니메이션", _PHOTO_PLAN, False, cast_lib.SAID_STYLE_TEXT["anime"]),
        ("애니메이션", "stop-motion felt puppet animation", False, cast_lib.SAID_STYLE_TEXT["anime"]),
        ("지브리 느낌으로", "stylized 3D animated feature film with watercolour textures", False, cast_lib.SAID_STYLE_TEXT["anime"]),
        ("수채화 느낌으로", "16-bit pixel art", False, "watercolour painting style"),
        ("흑백 연필 스케치", "black-and-white live-action film", False, "black-and-white pencil sketch style"),
        ("실사", _FA_LIB_SEMI, False, cast_lib.SAID_STYLE_TEXT["photo"]),
        ("고흐 그림처럼", "Van Gogh inspired post-impressionist oil painting with swirling brushstrokes", True, "KEEP"),
        ("디즈니 애니메이션처럼", "stylized 3D animated feature film look", True, "KEEP"))
    for said, style, fits, want in rows:
        assert cast_lib.style_said_fits(said, style) is fits, (said, style)
        assert _said_applied(style, said) == (style if want == "KEEP" else want), (said, style, _said_applied(style, said))
        assert cast_lib.keeps_style_said(said, style) is (want == "KEEP"), (said, style)
    # 라이브러리 인물이 없는 영화는 검사를 지나면 구성안 화풍 그대로 (덮어쓰지 않는다)
    other = {"bible": {"characters": [{"id": "Z", "look": "a tall man in a grey coat"}], "style": "epic high-fantasy look with glowing magical light",
                       "style_said": "판타지 영화처럼"}}
    assert cast_lib.apply_to_story(other, cast_lib.library(), "") == {} and other["bible"]["style"] == "epic high-fantasy look with glowing magical light"


def test_fa_10_style_said_check_messages_length_and_placeholders():
    """fixA 10: 2.4b 문구의 둘째 문장은 기대한 화풍과 구절 쪽 실수를 고치는 길을 말한다. 검사와 다듬기가 같은 값(120자까지, 자리 채움 글은 "")을
    본다 — 120자 뒤의 화풍 말을 검사만 보았다. 구절이 너무 길면 화풍 말만 옮기라고 한다. 규칙 글: 거절만 한 요청, 새 말이 화풍을 말하지 않으면
    앞 말의 화풍을 지킨다"""
    from app import cast_lib
    assert cast_lib.style_said_hint("애니메이션") == ("Write a 2D anime look (anime, 2D, cel-shaded or hand-drawn animation) in bible.style; if "
                                                 "bible.style_said holds more than the user's words for the look, copy only those words.")
    assert cast_lib.style_said_hint("수채화 동화풍").startswith("Write a watercolour painting and storybook illustration look in bible.style;")
    assert cast_lib.style_said_hint("고흐 그림처럼").startswith("Write that drawn or painted look itself, not a photograph")
    head = "니키와 푸토르가 비 내리는 숲속 오두막의 창가에 나란히 앉아 할아버지가 오래된 약초 책을 펼쳐 니키에게 하나하나 가르쳐 주고 니키가 공책에 열심히 받아 적으며 "
    long = head * 2 + "부드러운 수채화 느낌으로"
    assert len(long) > 120 and "수채" not in long[:120]
    d = copy.deepcopy(STORY_OK)
    d["bible"].update(style_said=long, style=_PHOTO_PLAN)
    lines = [x for x in video.story_problems(d, 30) if "style" in x]
    assert lines == [f"bible.style_said has {len(long)} characters: copy only the user's words for the look (at most 120 characters)."], lines
    assert video.clean_story(d, 30)["bible"]["style_said"] == long[:120]
    for v in ("none", "없음", "N/A", "-"):
        d["bible"].update(style_said=v, style=_PHOTO_PLAN)
        assert not [x for x in video.story_problems(d, 30) if "style" in x] and video.clean_story(d, 30)["bible"]["style_said"] == "", v
        assert _said_applied("2D anime film", v) == cast_lib.library()[0]["style"], v
    # 지브리 구절 + 흔한 지브리 묘사: 첫 답을 받는다 (예전에는 세 번 다 거절)
    d["bible"].update(style_said="지브리 느낌으로", style="gentle hand-drawn 2D animation with lush painted watercolor backgrounds and soft natural colours")
    call = Call([d])
    story, left, rounds = asyncio.run(video.write_story(call, "비 오는 저녁 우산 쓴 남자가 집에 가는 30초 영상, 지브리 느낌으로", "", 30))
    assert rounds == 1 and left == [] and story["bible"]["style"] == d["bible"]["style"], left
    sysm = video.STORY_SYSTEM
    # fixB: 거절만 한 요청은 거절한 말까지 그대로 옮긴다 (코드가 무엇을 거절했는지 알아야 거절한 반실사로 채우지 않는다, test_fb_06)
    for want in ('If it only turns a look down without naming another ("반실사 말고 다른 그림체로"), copy those words as they are, the refused '
                 'look included, and choose a different look yourself for "style".',
                 "A newer message that says nothing about the look keeps the earlier words for the look."):
        assert want in sysm, want


def test_fa_11_renoir_is_not_film_noir_in_the_old_path_either():
    """fixA 11 (전부터 있던 문제): '르누아르'(Renoir)의 '누아르' 를 film noir 로 읽지 않는다 — 구절 길·요청 글 예비 길 모두 (넓히지 않고 잘못 읽던
    것만 뺀다). '필름 누아르'·'누아르 영화처럼' 은 그대로 누아르다"""
    from app import cast_lib
    text = "니키가 정원에서 꽃을 바라보는 30초 영상을 르누아르 그림처럼"
    painting = "warm impressionist painting with soft feathery brushstrokes and glowing colours"
    assert cast_lib.said_level(text) == "maybe" and cast_lib.custom_style_text(text) == ""
    assert _said_applied(painting, None, text) == painting and _said_applied(painting, "르누아르 그림처럼") == painting
    assert cast_lib.custom_style_text("누아르 영화처럼 제임스가 걷는 영상") == "film noir style" and cast_lib.style_key("film noir style") == "custom"
    assert cast_lib.custom_style_text("르누아르풍 그림", phrase=True) == "" and cast_lib.said_phrase_key("필름 누아르 느낌") == "custom"


# ── fixB (11-25): fixA 다시 확인 지적 고침. 새 방식(present·style_said) 안에서 칸끼리 보고, 낱말 짐작 예비는 넓히지 않는다 ──
_FB_CAST = {"C": {"look": "a twelve-year-old girl with two long black braids", "name_ko": "니키"},
            "A": {"look": "a calm neat woman around forty", "name_ko": "베스"}}


def test_fb_01_they_and_them_look_at_the_nearest_plural_noun():
    """fixB 1·8: 독백 글의 they·them 은 같은 문장에서 가장 가까운 복수 명사가 물건·동물일 때만 그것을 가리킨다 — 관사 없는 'fallen leaves'·
    'fireflies' 도 물건이고(fixA 는 관사가 붙은 명사만 보아 올바른 독백 컷이 걸렸다), 몸 낱말(her tears·her eyes)은 세지 않고, 더 가까운 사람 명사
    ('the adults')가 앞의 물건('the cups')을 이기고, 사람만 하는 동작('watches them laugh'·'as they talk'·'the teammates cheer')은 앞에 물건이 있어도
    사람이다 (fixA 의 any 거르기가 그 자리 사람을 놓쳐 독백 검사와 혼자 대사 영상 글을 지나쳤다). present 가 있든 없든 같다"""
    cast = video._cast_view(_FB_CAST)
    for text in ("Niki gathers fallen leaves and watches them swirl in the wind.",
                 "Niki gazes at fireflies as they dance around her, her face lit with wonder.",
                 "Niki watches paper lanterns rise into the sky and smiles at them.",
                 "Niki looks up at countless stars; they look like tiny jewels to her.",
                 "Niki watches dozens of lanterns rise and smiles at them.",
                 "Niki gathers the leaves in her hands and watches them swirl.",
                 "Lanterns drift over the river, and she smiles at them."):
        assert video._other_mention(text, "C", cast) == "", text
        assert video._speaker_only(text, "C", cast) == text, text
    for text, want in (("Niki wipes her tears and smiles at them.", "smiles at them"),
                       ("Niki rubs her eyes, then looks up and grins at them.", "grins at them"),
                       ("Niki folds her hands on the table and nods at them.", "nods at them"),
                       ("Niki puts down her chopsticks and watches them laugh.", "watches them"),
                       ("Niki lowers her eyes as they talk about her birthday.", "they talk"),
                       ("Niki puts down the cups, glances at the adults and nods at them.", "nods at them"),
                       ("Niki watches the teammates cheer and grins at them.", "grins at them")):
        assert video._other_mention(text, "C", cast) == want, (text, video._other_mention(text, "C", cast))
        assert video._speaker_only(text, "C", cast) == "", text
    abc = ["A", "B", "C"]
    for with_present in (True, False):
        plan = [_r3_shot("P1", abc, "wide shot", "The family sits around the breakfast table.", "아침 식탁", 10),
                _r5_talk("P1", abc, "A", "B", "여보, 니키 생일 선물 준비했어요?", shot="medium group shot", action="Beth leans toward James."),
                _r5_talk("P1", ["C"], "C", "nobody", "다들 내 생일 잊은 줄 알았는데...", shot="close-up",
                         action="Niki wipes her tears and smiles at them.")]
        if with_present:
            plan = [_pr(x, abc) for x in plan]
        s, left = _r3_clean(plan, exchange=False)
        assert any(x.startswith("Scene 3 is a monologue") and '"smiles at them"' in x for x in left), (with_present, left)
        with duo_setting(True):
            assert "wipes her tears" not in video.talk_prompt(s, 2), with_present


def test_fb_02_a_role_word_before_a_name_still_teaches_the_name():
    """fixB 7: 'Grandpa Joe'·'Mom Sora' 처럼 역할 말 + 이름으로 쓴 라이브러리 밖 인물은 그 이름을 배운다 — fixA 의 역할 말 멈춤이 바로 뒤 이름까지
    버려 독백 샷 'close-up of Grandpa Joe' 의 Joe 를 다른 사람으로 잡아 샷 글을 자르고 혼자 대사 영상 글에서 동작 문장을 뺐다. 역할 말이 주어면
    ('Mother smiles at Jiwoo') 그대로 멈춘다 (test_fa_07)"""
    for role, name, ko, look in (("Grandpa", "Joe", "할아버지", "an old man around seventy with a white beard and a flat cap"),
                                 ("Mom", "Sora", "엄마", "a woman around forty with a low ponytail and a beige cardigan"),
                                 ("Grandma", "Bokja", "할머니", "an old woman around seventy with short grey permed hair"),
                                 ("Dad", "Minho", "아빠", "a man around forty with short black hair and glasses")):
        chars = [{"id": "A", "look": look, "name_ko": ko, "voice": "A warm voice.", "voice_ko": ko},
                 {"id": "C", "look": "a ten-year-old girl with a short black bob", "name_ko": "수아", "voice": "A bright girl.", "voice_ko": "아이"}]
        who = f"{role} {name}"
        scenes = [_r3_shot("P1", ["A", "C"], "wide shot", f"{who} and Sua sit on the porch at sunset.", "마루", 10),
                  _r5_talk("P1", ["A", "C"], "A", "C", "저기 별 보이니?", action=f"{who} smiles at Sua and points at the sky."),
                  _r5_talk("P1", ["C", "A"], "C", "A", "네, 정말 반짝여요!", action="Sua nods and looks up, eyes wide."),
                  _r5_talk("P1", ["A"], "A", "nobody", "별이 참 많구나.", shot=f"close-up of {who}, eye level, still camera",
                           action=f"{who} looks up at the stars, eyes glistening.")]
        assert name in video._text_names(scenes, {c["id"]: c for c in chars}).get("A", set()), who
        for with_present in (True, False):
            plan = [_pr(x, ["A", "C"]) for x in scenes] if with_present else scenes
            s, left = _r3_clean(plan, chars=chars, exchange=True)
            assert s["scenes"][3]["shot"] == f"close-up of {who}, eye level, still camera", (who, with_present, s["scenes"][3]["shot"])
            assert not [x for x in left if x.startswith("Scene 4")], (who, with_present, left)
            with duo_setting(True):
                assert "looks up at the stars" in video.talk_prompt(s, 3), (who, with_present)


def test_fb_03_a_listener_written_in_another_look_follows_the_look_on_screen():
    """fixB 11: 듣는 사람 칸에만 같은 사람의 다른 모습('james')을 적으면 화면에 적은 모습(characters 의 'james_glasses')을 따른다 — fixA 는 듣는 사람
    칸의 모습이 이겨 안경 쓴 제임스 장면 사이에 안경 없는 제임스 대화 컷이 끼고 다음 대화 줄의 잇기가 끊겼다 (검사 문구도 없었다). 말하는 사람은
    speaker 칸의 모습을 먼저 쓴다 (test_fa_02)"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    both = ["beth", "james_glasses"]
    raw = _variant_story()
    raw["scenes"] = [_pr(_r3_shot("P1", both, "wide shot", "Beth and James sit at the breakfast table.", "아침 식탁", 12), both),
                     _pr(_r5_talk("P1", both, "beth", "james", "여보, 커피 더 줄까요?"), both),
                     _pr(_r5_talk("P1", ["james_glasses", "beth"], "james_glasses", "beth", "고마워요, 여보.", cont=True), both)]
    with duo_setting(True):
        s = video.clean_dialogue_story(raw, 24, exchange=True, known=known)
        sc = s["scenes"][1]
        assert sc["characters"] == both and sc["listener"] == "james_glasses" and sc["present"] == both, sc
        assert s["scenes"][2]["continues"] is True and video.talk_group(s, 1) == both


def test_fb_04_black_and_white_or_noir_words_need_a_custom_look_not_live_action():
    """fixB 2·12: 흑백·누아르 구절에 실사 글('black-and-white … live-action film')은 맞지 않는다 — 실사 열쇠는 뒤 단계(story_style·실사 변환)가 화풍
    원문을 버려 흑백이 사라졌다 (fixA 는 받았다). 검사는 'live action' 말 없이 쓰라고 까닭과 함께 고쳐 쓰게 하고, 라이브러리 영화는 사진 쪽 custom
    글('black-and-white film style', 실사 영화 한 컷으로 그린다)로 채운다. 'live action' 없는 흑백 글은 그대로 둔다. '흑백 애니메이션' 의 실사 글도
    틀리고, 채울 때 애니 말을 잃지 않는다"""
    from app import cast_lib
    for said, plan, fill in (("옛날 흑백 영화처럼", "black-and-white 1940s live-action film look with soft grain", "black-and-white film style"),
                             ("흑백 누아르 영화처럼", "black-and-white film noir live action with hard shadows", "black-and-white film noir style"),
                             ("누아르 느낌으로", "film noir live-action look with hard shadows and rain", "film noir style")):
        assert not cast_lib.style_said_fits(said, plan) and not cast_lib.keeps_style_said(said, plan), said
        assert "without the words live action, photorealistic or photograph" in cast_lib.style_said_hint(said, plan), said
        assert _said_applied(plan, said) == fill and cast_lib.style_key(fill) == "custom" and video.photo_like_text(fill), said
        assert video.style_convert_prompt("custom", "man", fill).startswith("Turn this portrait into a live-action film still"), said
    for said, plan in (("옛날 흑백 영화처럼", "black-and-white film look with soft grain"),
                       ("흑백 누아르 영화처럼", "black-and-white film noir style with hard shadows"),
                       ("흑백 애니메이션", "black-and-white 2D anime with clean line art")):
        assert cast_lib.style_said_fits(said, plan) and cast_lib.keeps_style_said(said, plan) and _said_applied(plan, said) == plan, said
    for said in ("흑백 애니메이션", "누아르 애니메이션", "film noir animation"):
        plan = "black-and-white live-action film with soft film grain"
        assert not cast_lib.style_said_fits(said, plan) and "in animation (2D or 3D), not live action" in cast_lib.style_said_hint(said, plan), said
        fill = _said_applied(plan, said)
        assert fill.startswith(cast_lib.SAID_STYLE_TEXT["anime"]) and cast_lib.style_key(fill) == "anime" and cast_lib.style_said_fits(said, fill), fill
    # 라이브러리 밖 영화: 고쳐 쓰기 문구를 받고 고친 답을 받는다 (흑백 글이 영상 글에 남는다)
    first, fixed = copy.deepcopy(STORY_OK), copy.deepcopy(STORY_OK)
    first["bible"].update(style_said="옛날 흑백 영화처럼", style="black-and-white 1940s live-action film look with soft grain")
    fixed["bible"].update(style_said="옛날 흑백 영화처럼", style="black-and-white film look with soft grain")
    call = Call([first, fixed])
    story, left, rounds = asyncio.run(video.write_story(call, "옛날 흑백 영화처럼 할아버지가 옛 동네를 걷는 30초 영상", "", 30))
    assert rounds == 2 and left == [] and video.story_style(story) == "black-and-white film look with soft grain", left
    assert "without the words live action, photorealistic or photograph" in call.calls[1]["messages"][-1]["content"]


def test_fb_05_a_look_phrase_with_the_default_semi_look_is_checked_field_to_field():
    """fixB 3·4·10: 열쇠가 None 인 구절에 반실사 글(라이브러리 화풍을 베낀 글·'painted' 글)이 오면 화풍 말 목록으로 가르지 않고 칸끼리 고쳐 쓰게 한다:
    원래 그림체·분위기면 style_said 를 "", 다른 화풍이면 그 재료를 적게 (fixA 는 그림 말 목록으로 갈라 목록 밖 '동양화 느낌으로'·'명화처럼' 이 소리
    없이 반실사가 되고, 'painted' 모네 글은 검사를 지나 반실사로 그려졌다). 원래 그림체를 지키라는 말뿐인 구절은 "" 로 읽어 라이브러리 화풍을
    받는다 (fixA 는 거절해 그림 화풍으로 바꾸게 했다). 실사 글은 그림·회화 말 구절에서만 틀리다 (회화 말을 더했다)"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    head = "bible.style reads as the default semi-realistic painted look: if bible.style_said only asks to keep the characters' own look"
    for said in ("동양화 느낌으로", "한국화 느낌으로", "민화 스타일로", "명화 느낌으로", "르네상스 회화처럼", "스케치 느낌으로", "그래피티 스타일",
                 "고흐 그림처럼", "일러스트 느낌으로", "keep the original art style", "판타지 영화처럼"):
        assert cast_lib.said_phrase_key(said) is None, said
        for plan in (lib, lib + ", with soft East Asian brush touches"):
            hint = cast_lib.style_said_hint(said, plan)
            assert not cast_lib.style_said_fits(said, plan) and hint.startswith(head), (said, plan)
            assert 'set bible.style_said to ""' in hint and "name its medium" in hint
            assert _said_applied(plan, said) == lib, (said, plan)
    # 'painted' 글: 재료를 적게 고쳐 쓰게 하고, 세 번 안에 못 고치면 둔다 (fixA 그대로). 재료를 적은 답은 custom 이라 인물을 그 화풍으로 다시 그린다
    for said, plan in (("모네 느낌으로", "soft impressionist painted look with dappled light and loose brushwork"),
                       ("고흐 그림처럼", "expressive painterly style with swirling brushstrokes, thick paint and vivid colours")):
        assert cast_lib.style_key(plan) == "semi" and not cast_lib.style_said_fits(said, plan), said
        assert "name its medium" in cast_lib.style_said_hint(said, plan) and cast_lib.keeps_style_said(said, plan), said
    fixed = "impressionist oil painting with dappled light and loose brushwork"
    assert cast_lib.style_said_fits("모네 느낌으로", fixed) and _said_applied(fixed, "모네 느낌으로") == fixed and cast_lib.style_key(fixed) == "custom"
    assert video.style_convert_prompt("custom", "girl", fixed).startswith(
        "Redraw this exact girl as a character drawn in this art style: impressionist oil painting")
    # 원래 그림체를 지키라는 말뿐인 구절은 "" (말하지 않음): 라이브러리 화풍을 받고, 다른 화풍은 "" 와 어긋난다는 문구로 고쳐 쓰게 한다
    d = copy.deepcopy(STORY_OK)
    for said in ("원래 그림체 그대로", "기존 그림체 유지", "지금 화풍 그대로", "니키 그림체 그대로"):
        assert cast_lib.said_text(said) == "" and cast_lib.style_said_fits(said, lib), said
        assert _said_applied("hand-drawn storybook illustration style with soft painted textures", said) == lib, said
        d["bible"].update(style_said=said, style=lib)
        assert not [x for x in video.story_problems(d, 30) if "style" in x] and video.clean_story(d, 30)["bible"]["style_said"] == "", said
        d["bible"].update(style="hand-drawn storybook illustration style with soft painted textures")
        assert any(x.startswith('bible.style_said is "", but bible.style describes a particular look') for x in video.story_problems(d, 30)), said
    for said in ("원래 그림체 말고 수채화로", "원래 그림체 말고"):
        assert cast_lib.said_text(said) == said, said
    # 실사 글: 그림·회화 구절이면 틀리고, 영화 종류는 맞다. 거절한 화풍이 따로 있으면('실사 말고 …') 라이브러리 반실사도 맞다
    for said in ("동양화 느낌으로", "명화처럼", "벽화 느낌으로", "고흐 그림처럼"):
        assert not cast_lib.style_said_fits(said, _PHOTO_PLAN), said
        assert "not live action" in cast_lib.style_said_hint(said, _PHOTO_PLAN), said
    for said in ("다큐멘터리처럼", "공포 영화처럼", "cinematic film look", "판타지 영화처럼"):
        assert cast_lib.style_said_fits(said, _PHOTO_PLAN), said
    assert cast_lib.style_said_fits("실사 말고 다른 느낌으로", lib) and _said_applied(lib, "실사 말고 다른 느낌으로") == lib
    assert ("Words that only keep the known characters' own look (\"원래 그림체 그대로\") or only give a mood (\"따뜻한 느낌으로\") are not a look."
            in video.STORY_SYSTEM)


def test_fb_06_a_refusal_only_phrase_keeps_what_was_refused():
    """fixB 5: 거절만 한 구절('반실사 말고 다른 스타일로'·'반실사는 빼고'·'anything but semi-realistic')은 무엇을 거절했는지 기억한다 — fixA 는 거절한
    말을 지운 뒤 잊어, 구성안이 실사를 고르면 라이브러리 반실사(거절한 화풍)로 채우고 라이브러리 반실사를 베껴도 통과시켰다. 거절한 화풍의 글은
    고쳐 쓰게 하고, 채울 때도 거절한 반실사 대신 애니 글. 규칙 글은 거절만 한 요청이면 거절한 말까지 그대로 옮기게 한다"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    anime = cast_lib.SAID_STYLE_TEXT["anime"]
    for said in ("반실사 말고 다른 스타일로", "반실사는 빼고", "anything but semi-realistic", "반실사 말고 다른 느낌으로"):
        assert cast_lib.said_phrase_key(said) is None and set(cast_lib._phrase_refused(said)) == {"semi"}, said
        for plan in (_PHOTO_PLAN, "2D anime feature film with clean cel-shaded line art", "stylized 3D animated feature film with smooth soft shading"):
            assert cast_lib.style_said_fits(said, plan) and cast_lib.keeps_style_said(said, plan) and _said_applied(plan, said) == plan, (said, plan)
        assert not cast_lib.style_said_fits(said, lib) and cast_lib.style_said_hint(said, lib).startswith('The user turned down the look in "'), said
        assert _said_applied(lib, said) == anime and _said_applied("", said) == anime, said
    # 그림 말이 남는 구절의 실사 글은 그대로 틀리고, 거절하지 않은 애니 글로 채운다
    assert not cast_lib.style_said_fits("반실사 말고 다른 그림체로", _PHOTO_PLAN) and _said_applied(_PHOTO_PLAN, "반실사 말고 다른 그림체로") == anime
    # 반실사가 아닌 것을 거절했으면 라이브러리 화풍이 그대로 맞고, 거절한 애니 글은 틀리다
    assert _said_applied(_PHOTO_PLAN, "애니메이션 말고 다른 느낌으로") == lib
    assert not cast_lib.style_said_fits("애니메이션 말고 다른 느낌으로", "2D anime feature film with clean line art")
    # 거절과 바라는 화풍을 함께 옮긴 구절은 바라는 화풍의 열쇠 (fixA 그대로)
    assert cast_lib.said_phrase_key("애니메이션 말고 실사로") == "photo" and cast_lib._phrase_refused("애니메이션 말고 실사로") == {"anime": "애니메이션"}
    d = copy.deepcopy(STORY_OK)
    d["bible"].update(style_said="반실사는 빼고", style=lib)
    assert [x for x in video.story_problems(d, 30) if "style" in x] == [
        'bible.style must describe the look in bible.style_said ("반실사는 빼고"). The user turned down the look in "반실사": write a different '
        "look in bible.style (for example 2D animation, 3D animation, live action); if bible.style_said holds more than the user's words for the "
        "look, copy only those words."]
    assert "copy those words as they are, the refused look included" in video.STORY_SYSTEM


def test_fb_07_film_look_phrases_agree_in_korean_and_english():
    """fixB 6·9: 영화 종류·필름 룩 구절은 한국어든 영어든 열쇠가 None — 구성안이 정한 화풍을 믿는다. fixA 의 photo 예비는 화풍 글 표가 실사로
    읽는 영어 구절만 photo 로 보아, 라이브러리 영화에서 'documentary style' 은 인물을 실사로 바꾸고 '다큐멘터리처럼' 은 라이브러리 반실사였고,
    'a Disney film look' 같은 애니 스튜디오 구절이 실사가 되어 맞는 3D 구성안을 거절했다"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    for ko, en, plan in (("다큐멘터리처럼", "documentary style", "documentary-style handheld live-action footage with natural light"),
                         ("공포 영화처럼", "horror film look", _PHOTO_PLAN),
                         ("시네마틱한 느낌으로", "cinematic look", "cinematic film look with moody light"),
                         ("필름 카메라 느낌", "35mm film look", "35mm film look, live-action, soft grain")):
        for said in (ko, en):
            assert cast_lib.said_phrase_key(said) is None and cast_lib.style_said_fits(said, plan), (said, plan)
            assert _said_applied(plan, said) == lib, (said, plan)
    t3 = "stylized 3D animated feature film with soft shading and warm light"
    for said in ("a Disney film look", "classic Disney film look", "Disney cinematic look"):
        assert cast_lib.said_phrase_key(said) is None and cast_lib.style_said_fits(said, t3) and _said_applied(t3, said) == t3, said
        assert not cast_lib.style_said_fits(said, _PHOTO_PLAN), said
    # 라이브러리 밖 영화: 영어 필름 룩 구절 + 실사 글을 첫 답에 받는다 (fixA 4 그대로)
    d = copy.deepcopy(STORY_OK)
    d["bible"].update(style_said="cinematic film look", style="cinematic film look with moody rain light")
    assert not [x for x in video.story_problems(d, 30) if "style" in x]


# ── fixC (11-25 최종 확인 2라운드의 좁은 확인 9건) ──
def _fc_follow(scenes, msgs, branch):
    """검사 문구를 글자 그대로 따르는 구성안 모델 (fixC 문구만): branch "nobody" 는 독백 길, "add" 는 present 에 넣는 길"""
    import re
    out = copy.deepcopy(scenes)
    for m in msgs:
        g = re.match(r"Scene (\d+)", m)
        if not g:
            continue
        sc = out[int(g.group(1)) - 1]
        q = re.findall(r'"([^"]+)"', m)
        who = sc["speaker"]
        if "so nobody in the place is spoken to: set" in m or (m.split(": ", 1)[1].startswith("the listener") and "is in \"characters\"" in m):
            if branch == "add":
                sc["present"] = list(dict.fromkeys(sc["present"] + [q[0]]))
                want = re.search(r'make "characters" (\[[^\]]*\])', m)
                sc["characters"] = re.findall(r'"([^"]+)"', want.group(1)) if want else sc["characters"]
                sc["shot"] = "medium two-shot" if len(sc["characters"]) == 2 else "medium group shot"
            else:
                sc.update(listener="nobody", characters=[who], shot="close-up", action="She speaks softly, eyes lowered.")
        elif "is in \"characters\" but not in \"present\"" in m:
            sc["present"].append(q[0])
        elif "everyone in \"present\" is seen: add" in m:
            body = m.split(": add", 1)[1].split('to "characters"')[0]
            sc["characters"] += [x for x in re.findall(r'"([^"]+)"', body) if x not in sc["characters"]]
    return out


def test_fc_01_a_listener_not_in_the_place_gets_one_message_nobody_first():
    """fixC 1 (06#1·05#2): present 를 쓴 장면에서 듣는 사람이 그 자리(P: present ∪ characters ∪ 말하는 사람)에 없으면 문구는 하나다 — "nobody"
    (혼자 클로즈업) 길을 먼저, '장면들이 그 사람을 이 장소에 보일 때만' present 에 넣는 길(화면·샷까지)을 뒤에. 일반 독백 문구('must be exactly
    [A]'·'샷은 클로즈업')와 예전 칸 문구('add … to present if … or set listener to nobody')를 같은 판에 함께 내지 않는다 (서로 반대로 시켰고, present
    에 넣는 길을 먼저 적어 문 너머 엄마를 다락방 투샷에 들였다). 문구를 글자 그대로 따르면 두 길 모두 고쳐 쓰기 한 번에 통과한다.
    규칙 글도 '듣는 사람은 늘 present' 대신 그 자리에 없는 사람을 부르거나 대답하면 "nobody" 라고 적는다"""
    wide = _pr(_r3_shot("P1", ["A", "B", "C"], "wide shot", "The family sits around the breakfast table.", "가족의 아침 식탁", 10), ["A", "B", "C"])
    reply = _pr(_r5_talk("P1", ["B", "A", "C"], "B", "A", "그래, 맛있겠다.", shot="medium group shot", action="James nods."), ["A", "B", "C"])
    cases = {"1c": _pr(_r5_talk("P1", ["A", "B"], "A", "C", "니키야, 우유 마셔.", shot="medium two-shot", action="Beth smiles."), ["A", "B"]),
             "1b": _pr(_r5_talk("P1", ["A"], "A", "C", "니키야, 우유 마셔.", shot="close-up", action="Beth smiles."), ["A", "B"])}
    want = ('Scene 2: the listener "C" is not in "present", so nobody in the place is spoken to: set "listener" to "nobody" and make it a '
            'close-up of "A" alone ("characters" ["A"]); a reply to a voice behind a door, from another room or on the phone is such a '
            'monologue. Only if the scenes show "C" in this place, add "C" to "present" and make "characters" ["A", "C", "B"] in a medium '
            'group shot instead.')
    for name, line in cases.items():
        scenes = [wide, line, reply]
        s, left = _r3_clean(scenes, exchange=False)
        assert s["scenes"][1]["monologue"] is True and s["scenes"][1]["characters"] == ["A"], name
        assert [x for x in _scene_msgs(left) if x.startswith("Scene 2")] == [want], (name, left)
        assert not any("must be exactly" in x or "is not in \"present\": add" in x for x in left), (name, left)
        for branch, chars in (("nobody", ["A"]), ("add", ["A", "C", "B"])):
            fixed = _fc_follow(scenes, _scene_msgs(left), branch)
            s2, left2 = _r3_clean(fixed, exchange=False)
            assert _scene_msgs(left2) == [], (name, branch, left2)
            assert s2["scenes"][1]["characters"] == chars and s2["scenes"][1]["monologue"] is (branch == "nobody"), (name, branch)
    # 문 너머에서 부른 엄마에게 하는 대답 (listener 엄마, 다락방 present 에는 니키뿐): 독백, 문구는 nobody 먼저
    attic = [_pr(_r3_shot("P3", ["C"], "medium shot", "Niki reads a book on her bed.", "다락방의 니키", 10), ["C"]),
             _pr(_r3_shot("P3", ["C"], "close-up", "Niki looks up from her book.", "아래층에서 부르는 엄마", 6, speaker="A",
                          line="니키야, 저녁 먹어라!", emotion="calling"), ["C"]),
             _pr(_r5_talk("P3", ["C"], "C", "A", "네, 갈게요!", shot="close-up", action="Niki calls toward the door."), ["C"])]
    s, left = _r3_clean(attic, chars=[_R3_FAM[0], _R3_FAM[2]], exchange=False)
    msgs = [x for x in _scene_msgs(left) if x.startswith("Scene 3")]
    assert len(msgs) == 1 and msgs[0].index('set "listener" to "nobody"') < msgs[0].index('add "A" to "present"'), left
    assert "a reply to a voice behind a door" in msgs[0] and s["scenes"][2]["characters"] == ["C"] and s["scenes"][2]["monologue"] is True
    s2, left2 = _r3_clean(_fc_follow(attic, msgs, "nobody"), chars=[_R3_FAM[0], _R3_FAM[2]], exchange=False)
    assert _scene_msgs(left2) == [] and not any("A" in x["characters"] for x in s2["scenes"]), left2
    # 독백 길의 장면 글이 다른 사람을 가리키면 같은 문구에 적는다 (라이브러리 id 라 이름을 안다)
    from app import cast_lib
    known = cast_lib.known_in("베스와 니키")
    fam = [{"id": "beth", "name_ko": "베스", "look": "a calm neat woman around forty", "voice": "A warm Korean woman.", "voice_ko": "엄마"},
           {"id": "niki", "name_ko": "니키", "look": "a twelve-year-old girl", "voice": "A bright Korean girl.", "voice_ko": "아이"}]
    lib = [_pr(_r3_shot("P2", ["beth"], "wide shot", "Beth waters the apple tree.", "정원의 베스", 10), ["beth"]),
           _pr(_r5_talk("P2", ["beth"], "beth", "niki", "니키야, 이리 와 보렴.", shot="close-up", action="Beth smiles at Niki."), ["beth"])]
    _s, left = _r3_clean(lib, chars=fam, exchange=False, known=known)
    assert [x for x in _scene_msgs(left) if x.startswith("Scene 2")] == [
        'Scene 2: the listener "niki" is not in "present", so nobody in the place is spoken to: set "listener" to "nobody" and make it a '
        'close-up of "beth" alone ("characters" ["beth"], with an "action" and a "shot" that do not mention "Niki"); a reply to a voice behind '
        'a door, from another room or on the phone is such a monologue. Only if the scenes show "niki" in this place, add "niki" to "present" '
        'and make "characters" ["beth", "niki"] in a medium two-shot instead.'], left
    ex = video.DIALOGUE_EXTRA_DUO
    assert 'The person spoken to ("listener") is always present' not in ex
    assert ('someone you call or answer who is not in the place (behind a door, in another room, on the phone, or who has just left) is never '
            'the listener: write "nobody".') in ex


def test_fc_02_a_listener_drawn_but_not_in_present_gets_the_whole_monologue_path():
    """fixC 1 보충: 화면(characters)에 적었지만 present 에 없는 듣는 사람은 대화 컷이다(characters 는 present 의 일부). 문구는 'present 에 넣거나,
    아니면 빼고 nobody·혼자 클로즈업' 하나로 끝까지 적는다 — 빼기만 따르면 다음 판에 '듣는 사람이 그 자리에 없다' 가 또 나왔다. 두 길 모두
    고쳐 쓰기 한 번에 통과한다"""
    wide = _pr(_r3_shot("P2", ["A"], "wide shot", "Beth waters the apple tree.", "정원의 베스", 10), ["A"])
    scenes = [wide, _pr(_r5_talk("P2", ["A", "C"], "A", "C", "니키야, 물 좀 줄래?"), ["A"])]
    s, left = _r3_clean(scenes, exchange=False)
    assert s["scenes"][1]["characters"] == ["A", "C"] and s["scenes"][1]["monologue"] is False
    msgs = [x for x in _scene_msgs(left) if x.startswith("Scene 2")]
    assert msgs == ['Scene 2: the listener "C" is in "characters" but not in "present": add "C" to "present" if "C" is in the place; if not, '
                    'nobody in the place is spoken to: remove "C" from "characters", set "listener" to "nobody" and make it a close-up of "A" '
                    'alone.'], left
    for branch, chars in (("add", ["A", "C"]), ("nobody", ["A"])):
        s2, left2 = _r3_clean(_fc_follow(scenes, msgs, branch), exchange=False)
        assert _scene_msgs(left2) == [] and s2["scenes"][1]["characters"] == chars, (branch, left2)


def test_fc_03_a_voice_only_speaker_is_decided_line_by_line_from_present():
    """fixC 2 (05#3): 말한 줄이 모두 그 장면의 P 밖(목소리 shot·화면 밖 talk)에서 나온 화자는 대화 요청이어도 talk 장면이 필요 없다 — 다른 장면에
    말없이 보여도 그렇다 (예전에는 '어느 장면의 present 에도 없는 화자'만 보아 부엌에서 한 번 보인 엄마에게 talk 장면을 요구했고, 따르면 엄마를 다락방
    present 에 넣게 됐다). 그 자리에 있으면서 목소리로만 말한 사람에게는 그 줄 가운데 하나를 talk 로 바꾸게 한다(바른 길). 지시문의 줄 수 규칙도
    두세 사람 규칙에서는 '그 장소에 있는 말하는 사람마다' 라고 DUO 규칙과 같은 말을 한다. present 가 없는 구성안의 문구는 그대로다"""
    kitchen = _pr(_r3_shot("P1", ["A"], "medium shot", "Beth stirs a pot at the stove.", "부엌의 베스", 8), ["A"])
    attic = [_pr(_r3_shot("P3", ["C"], "medium shot", "Niki reads a book on her bed.", "다락방의 니키", 8), ["C"]),
             _pr(_r3_shot("P3", ["C"], "close-up", "Niki looks up from her book.", "엄마 목소리", 6, speaker="A", line="니키야, 저녁 먹어라!",
                          emotion="calling"), ["C"]),
             _pr(_r5_talk("P3", ["C"], "C", "nobody", "네, 갈게요!", shot="close-up", action="Niki calls toward the door."), ["C"]),
             _pr(_r3_shot("P3", ["C"], "close-up", "Niki smiles.", "엄마 목소리", 6, speaker="A", line="빨리 내려오렴!", emotion="calling"), ["C"]),
             _pr(_r5_talk("P3", ["C"], "C", "nobody", "알았어요, 지금 가요!", shot="close-up", action="Niki grins."), ["C"])]
    _s, left = _r3_clean([kitchen] + attic, exchange=True)
    assert not any("voice-over" in x and "talk" in x for x in left), left
    # 같은 다락방에 있으면서(present) 목소리로만 말한 엄마: 그 줄 가운데 하나를 talk 로
    here = [_pr(sc, ["C", "A"]) for sc in attic]
    _s, left = _r3_clean(here, exchange=True)
    assert ('"A" speaks only as a voice-over, though "A" is in "present" when speaking: make at least one of those lines a "talk" scene, so '
            'the audience sees the face speak.') in left, left
    # present 가 없는 구성안은 예전 문구 그대로 (test_s3_11 의 DOOR 판도 본다)
    old = [{k: v for k, v in sc.items() if k != "present"} for sc in [kitchen] + attic]
    _s, left = _r3_clean(old, exchange=True)
    assert '"A" speaks only as a voice-over. Give "A" at least one "talk" scene, so the audience sees the face speak.' in left, left
    duo_rules = video.dialogue_count_rules(60, True, duo=True)
    assert ('every speaker who is in the place gets at least one "talk" scene so the audience sees that face speak (a voice from another '
            'room or on the phone needs none).') in duo_rules
    assert 'every speaker gets at least one "talk" scene' in video.dialogue_count_rules(60, True)
    for duo, want in ((True, "every speaker who is in the place"), (False, 'every speaker gets at least one "talk" scene')):
        call = Call([DLG_OK, DLG_OK, DLG_OK])
        with duo_setting(duo):
            asyncio.run(video.write_dialogue_story(call, "니키와 푸토르가 숲속을 걸어가며 대화하는 영상", "", 48))
        assert want in call.calls[0]["messages"][0]["content"], duo


def test_fc_04_present_entries_are_read_like_the_listener():
    """fixC 3 (06#3): present 항목은 듣는 사람 칸과 같은 방식으로 읽는다 — id, 대소문자만 다른 id, 한 사람에게만 맞는 이름('베스'·'Beth').
    예전에는 id 글자가 같아야만 받아, 이름으로 적은 구성안의 대화 줄이 모두 혼자 클로즈업이 됐다. 이름이 한 사람에게만 들어 있지만 그대로는 못
    읽는 항목('베스(엄마)')에는 고칠 id 를 알려 주고, 같은 판에 '말하는 사람이 present 에 없다(목소리면 shot)' 를 내지 않는다. 모르는 값은 예전 문구"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    fam = [c for c in _variant_story()["bible"]["characters"] if c["id"] != "james_glasses"]
    for pres in (["베스", "제임스", "니키"], ["Beth", "James", "Niki"], ["BETH", "james", "Niki"]):
        scenes = [_pr(_r3_shot("P1", ["beth", "james", "niki"], "wide shot", "The family sits at the breakfast table.", "아침", 10), pres),
                  _pr(_r5_talk("P1", ["beth", "niki", "james"], "beth", "niki", "니키야, 우유 마셔.", shot="medium group shot"), pres),
                  _pr(_r5_talk("P1", ["james", "beth", "niki"], "james", "everyone", "자, 다들 먹자.", shot="medium group shot"), pres)]
        s, left = _r3_clean(scenes, chars=fam, exchange=False, known=known)
        assert [x["present"] for x in s["scenes"]] == [["beth", "james", "niki"]] * 3, pres
        assert s["scenes"][1]["characters"] == ["beth", "niki", "james"] and s["scenes"][1]["monologue"] is False, pres
        assert s["scenes"][2]["to_all"] is True and _scene_msgs(left) == [], (pres, left)
    scenes = [_pr(_r3_shot("P1", ["beth", "niki"], "wide shot", "Beth and Niki sit at the table.", "아침", 10), ["베스(엄마)", "niki", "Z"]),
              _pr(_r5_talk("P1", ["beth", "niki"], "beth", "niki", "니키야, 잘 잤니?"), ["베스(엄마)", "niki"])]
    _s, left = _r3_clean(scenes, chars=fam, exchange=False, known=known)
    assert 'Scene 1: "present" has "베스(엄마)", which is not an id: write the id "beth" (the "id" in bible.characters).' in left, left
    assert 'Scene 2: "present" has "베스(엄마)", which is not an id: write the id "beth" (the "id" in bible.characters).' in left, left
    assert 'Scene 1: "present" has "Z", which is not in bible.characters.' in left, left
    assert not any("the speaker must be in" in x or '"beth" is in "characters" but not' in x for x in left), left
    assert video._read_present("Beth", {"beth": {}, "james": {}}, video._cast_view({"beth": {}, "james": {}})) == ("beth", "")


def test_fc_05_the_listener_look_wins_and_two_looks_in_present_are_flagged():
    """fixC 4 (06#2): present 를 쓴 장면에서도 듣는 사람 칸이 적은 모습(안경 쓴 제임스)으로 같은 사람의 모습을 고른다 — P 에 넣지는 않는다.
    예전에는 모습 고르기에서도 듣는 사람이 빠져 present 에 먼저 쓴 안경 없는 제임스가 남아 잇기가 끊겼다. present 에 두 모습을 함께 적으면
    이 장면이 보이는 모습 하나만 남기게 한다 (규칙 글 'never put both entries in one scene')"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    raw = _variant_story()
    allp = ["beth", "james", "james_glasses", "niki"]
    raw["scenes"][0]["characters"] = ["beth", "james", "niki"]
    raw["scenes"][2].update(characters=["beth"], shot="close-up")
    for sc in raw["scenes"]:
        sc["present"] = list(allp)
    with duo_setting(True):
        s = video.clean_dialogue_story(raw, 32, exchange=True, known=known)
        left = video.dialogue_problems(raw, 32, True, exchange=True, known=known)
    assert s["scenes"][2]["characters"] == ["beth", "james_glasses", "niki"] and s["scenes"][2]["listener"] == "james_glasses"
    assert s["scenes"][2]["present"] == ["beth", "james_glasses", "niki"]
    assert ('Scene 3: "present" has "james" and "james_glasses", the same person in two looks; a scene has only one of them: keep only '
            '"james_glasses", the look this scene shows.') in left, left
    assert ('Scene 1: "present" has "james" and "james_glasses", the same person in two looks; a scene has only one of them: keep only '
            '"james", the look this scene shows.') in left, left
    # 듣는 사람이 다른 곳에 있으면 모습 고르기에만 쓰고 P 에 넣지 않는다 (11-25 최종 확인 그대로)
    look = video._scene_present(["beth", "james", "james_glasses"], ["beth"], video._cast_view(
        {c["id"]: c for c in raw["bible"]["characters"]}, known), "beth", "", "james_glasses")
    assert look == ["beth", "james_glasses"]
    assert video._scene_present(["beth"], ["beth"], video._cast_view({c["id"]: c for c in raw["bible"]["characters"]}, known),
                                "beth", "", "niki") == ["beth"]


def test_fc_06_a_repair_round_sends_only_the_last_answer():
    """fixC 5 (05#5): 구성안 고쳐 쓰기 차례는 처음 messages(지시문·요청) + 마지막 답(빈칸 없는 JSON) + 그 답의 문제만 보낸다. 앞 차례의 답을 모두
    쌓으면 3분 대사 구성안의 세 번째 차례가 HELPER_NUM_CTX(12288)를 넘었다 (근사 13.3K~15.1K → 9.3K~10.3K). 한 GPU 차례 안의 호출은 같은
    num_ctx 를 써야 하므로 문맥 길이는 그대로다. 대사 영상·대사 없는 영상 모두"""
    first, second = _dlg_good(), _dlg_good()
    first["music"]["bpm"], second["music"]["key"] = 10, "H major"          # 차례마다 다른 위반 하나
    for writer, answers, args in ((video.write_dialogue_story, [first, second, _dlg_good()], ("니키와 푸토르가 숲속을 걸어가며 대화하는 영상", "", 48)),
                                  (video.write_story, [_story(scenes=[dict(s, seconds=5) for s in STORY_OK["scenes"]]),
                                                       _story(scenes=[dict(s, seconds=6) for s in STORY_OK["scenes"]]), STORY_OK],
                                   ("비 오는 저녁 우산 쓴 남자가 집에 가는 30초 영상", "", 30))):
        call = Call(answers)
        with duo_setting(False):
            story, left, rounds = asyncio.run(writer(call, *args))
        assert rounds == 3 and left == [] and story is not None, writer.__name__
        third = call.calls[2]["messages"]
        assert len(call.calls[1]["messages"]) == len(third) == 4, writer.__name__
        assert third[:2] == call.calls[0]["messages"] and third[2]["role"] == "assistant", writer.__name__
        assert third[2]["content"] == json.dumps(fit(answers[1], call.calls[1]), ensure_ascii=False, separators=(",", ":")), writer.__name__
        assert third[3]["content"].startswith("Your JSON broke these rules")
        assert [m["role"] for m in third] == ["system", "user", "assistant", "user"], writer.__name__          # 첫 답은 보내지 않는다
        assert call.calls[1]["messages"][3]["content"] != third[3]["content"], writer.__name__                 # 문구는 마지막 답의 것
    assert video.repair_turn([{"role": "system", "content": "s"}], "not json", ["x"])[1]["content"] == "not json"


def test_fc_07_a_request_that_only_turns_a_look_down_never_gets_the_refused_look():
    """fixC 6 (05#1): 거절만 한 요청('반실사 말고 다른 그림체로'·'지금 그림체 말고 다른 스타일로'·'원래 그림체 말고 다른 그림체로')이 거절한 반실사로
    나오지 않는다. 지시문 두 곳(prompt_block 화풍 줄·STORY_SYSTEM)이 같은 말을 하고(거절도 화풍을 말한 것: 그 말을 style_said 에, 거절하지 않은
    화풍을 style 에), 고쳐 쓰기 문구('"" 인데 특정 화풍')의 첫 갈래가 거절한 요청도 덮는다. 코드는 '원래/지금 그림체 말고' 를 라이브러리 그림체를
    거절한 것으로 읽는다(라이브러리 화풍을 알 때): 실사 글은 그대로 두고, 반실사 글은 거절한 화풍이라 고쳐 쓰게 하고 채울 때 애니 글.
    '원래 그림체 그대로' 는 그대로 "" 이고 '반실사 말고 애니메이션으로' 는 애니다"""
    from app import cast_lib
    lib = cast_lib.library()
    lib_style = lib[0]["style"]
    anime = cast_lib.SAID_STYLE_TEXT["anime"]
    block = cast_lib.prompt_block(lib)
    assert ('if the request says anything about the look (asks for a look, or turns a look down, for example "반실사 말고 다른 그림체로" or '
            '"원래 그림체 말고 다른 스타일로"), write the user\'s words into bible.style_said') in block          # fixD: 예문을 인물 그림체 거절로
    assert 'only when the request says nothing about the look or only keeps the characters\' own look ("원래 그림체 그대로") is bible.style_said ""' in block
    assert "if the request asks for a particular look" not in block
    sysm = video.STORY_SYSTEM
    # fixD: '지금 그림체' 는 앞 메시지로 만든 영상의 화풍이고, 앞 메시지가 없을 때만 인물의 그림체다
    assert ('The same goes for "원래 그림체 말고 다른 그림체로", which turns the known characters\' own look down, and for "지금 그림체 말고 다른 '
            '스타일로", which turns down the look the user sees now: the look of the film made from the earlier message when the request holds '
            'one, otherwise the known characters\' own look; "style" is never the refused look.') in sysm
    d = copy.deepcopy(STORY_OK)
    d["bible"].update(style_said="", style="2D anime feature film with clean line art")
    assert [x for x in video.story_problems(d, 30) if "style" in x] == [          # fixD: 사용자의 가장 새 말로 한정한다
        'bible.style_said is "", but bible.style describes a particular look ("2D anime feature film with clean line art"): if the user\'s '
        'newest words about the look (the newer message, when the request holds an earlier one) ask for this look or turn a look down (for '
        'example "반실사 말고 다른 그림체로"), copy those words into bible.style_said; otherwise write the style the rules give when '
        'bible.style_said is "".']
    assert cast_lib.said_text("원래 그림체 그대로") == "" and _said_applied(anime, "원래 그림체 그대로") == lib_style
    assert cast_lib.said_phrase_key("반실사 말고 애니메이션으로") == "anime" and _said_applied(lib_style, "반실사 말고 애니메이션으로") == anime
    for said in ("지금 그림체 말고 다른 스타일로", "원래 그림체 말고 다른 그림체로", "원래 그림체 말고", "기존 화풍은 빼고 다른 느낌으로"):
        assert cast_lib._phrase_refused(said, lib_style) == {"semi": said.split(" 말고")[0].split("은 빼고")[0]}, said
        # 라이브러리 화풍을 모르면 무엇을 거절했는지 모른다: '모름' 표시만 (fixD, 검사·덮어쓰기가 구성안이 고른 화풍을 믿는다)
        assert set(cast_lib._phrase_refused(said)) == {cast_lib.SOME_LOOK}, said
        assert not cast_lib.style_said_fits(said, lib_style, lib_style), said
        assert cast_lib.style_said_hint(said, lib_style, lib_style).startswith("The user turned down the look in"), said
        for plan in (lib_style, ""):
            assert _said_applied(plan, said) == anime, (said, plan)               # 세 번 안에 못 고쳐도 거절한 반실사가 아니다
        assert _said_applied("stylized 3D animated feature film with soft shading", said) == "stylized 3D animated feature film with soft shading"
        assert _said_applied("watercolour painting style with soft washes", said) == "watercolour painting style with soft washes"
        # 검사도 라이브러리 화풍을 받는다 (write_story·dialogue_problems 가 넘긴다): 반실사 글은 거절한 화풍이다
        d["bible"].update(style_said=said, style=lib_style)
        assert any('The user turned down the look in "' in x for x in video.story_problems(d, 30, True, lib_style)), said
        dlg = copy.deepcopy(DLG_OK)
        dlg["bible"].update(style_said=said, style=lib_style)
        assert any('The user turned down the look in "' in x for x in video.dialogue_problems(dlg, 48, True, known=lib[:2])), said
    for said in ("지금 그림체 말고 다른 스타일로", "원래 그림체 말고"):
        assert cast_lib.style_said_fits(said, _PHOTO_PLAN, lib_style) and _said_applied(_PHOTO_PLAN, said) == _PHOTO_PLAN, said
    # '다른 그림체로' 는 그림을 청한 말이라 실사 글은 그림 화풍으로 고치게 하고, "" 로 비우라고 하지 않는다 ("" 는 거절한 라이브러리 화풍)
    hint = cast_lib.style_said_hint("원래 그림체 말고 다른 그림체로", _PHOTO_PLAN, lib_style)
    assert not cast_lib.style_said_fits("원래 그림체 말고 다른 그림체로", _PHOTO_PLAN, lib_style) and 'set bible.style_said to ""' not in hint, hint
    assert _said_applied(_PHOTO_PLAN, "원래 그림체 말고 다른 그림체로") == anime
    assert 'set bible.style_said to ""' not in cast_lib.style_said_hint("반실사 말고 다른 그림체로", _PHOTO_PLAN)
    # 화풍 말 바로 뒤의 '그림체' 는 그 화풍 말의 것이다
    assert cast_lib._phrase_refused("반실사 그림체 말고 다른 걸로", lib_style) == {"semi": "반실사"}
    assert cast_lib._phrase_refused("애니 그림체 말고", lib_style) == {"anime": "애니"}
    # 라이브러리 인물이 나오는 구성안 쓰기: 검사가 라이브러리 화풍을 받아 거절한 반실사 답을 고쳐 쓰게 한다
    first, fixed = copy.deepcopy(STORY_OK), copy.deepcopy(STORY_OK)
    for x in (first, fixed):
        x["bible"]["characters"] = [{"id": "niki", "look": lib[0]["look"]}]
        for sc in x["scenes"]:
            sc["characters"] = ["niki"]
    first["bible"].update(style_said="지금 그림체 말고 다른 스타일로", style=lib_style)
    fixed["bible"].update(style_said="지금 그림체 말고 다른 스타일로", style="stylized 3D animated feature film with soft shading")
    call = Call([first, fixed])
    _st, left, rounds = asyncio.run(video.write_story(call, "니키가 정원을 걷는 30초 영상, 지금 그림체 말고 다른 스타일로", "", 30,
                                                       known=cast_lib.known_in("니키")))
    assert rounds == 2 and not [x for x in left if "style" in x], left
    assert 'The user turned down the look in "지금 그림체"' in call.calls[1]["messages"][-1]["content"]


def test_fc_08_a_refusal_hint_names_the_wanted_look_when_the_phrase_has_one():
    """fixC 7 (05#4): 거절한 화풍 글의 문구는, 구절이 바라는 화풍도 말했으면('반실사 말고 수채화로') 다른 기본 화풍 예시 대신 그 화풍을 적는다
    (예시대로 고친 답이 다음 차례에 '수채화를 써라' 로 다시 걸려 한 차례가 헛돌았다). 바라는 화풍이 없을 때만 예시를 든다"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    tail = "; if bible.style_said holds more than the user's words for the look, copy only those words."
    assert cast_lib.style_said_hint("반실사 말고 수채화로", lib) == (
        'The user turned down the look in "반실사" and asked for a watercolour painting look: write that look in bible.style' + tail)
    assert cast_lib.style_said_hint("실사 말고 3D 애니메이션으로", _PHOTO_PLAN) == (
        'The user turned down the look in "실사" and asked for a 3D animated look: write that look in bible.style' + tail)
    assert cast_lib.style_said_hint("애니메이션 말고 실사로", "2D anime film with clean line art").startswith(
        'The user turned down the look in "애니메이션" and asked for a photorealistic live-action look')
    assert cast_lib.style_said_hint("반실사 말고 다른 그림체로", lib) == (
        'The user turned down the look in "반실사": write a different look in bible.style (for example 2D animation, 3D animation, live action)'
        + tail)


def test_fc_09_docs_and_docstring_state_the_current_listener_rule():
    """fixC 8 (04#1): 문서 13.5 다듬기 첫 줄과 clean_dialogue_story 독스트링이 지금 규칙을 적는다 — 듣는 사람은 present 칸이 없는 장면에서만 P 에
    더한다 (예전 글: 'P = 쓴 present ∪ characters ∪ 듣는 사람 ∪ 말하는 사람')"""
    here = os.path.dirname(os.path.abspath(__file__))
    doc = open(os.path.join(here, "..", "..", "docs", "short-film-dialogue-design.md"), encoding="utf-8").read()
    sec = doc[doc.index("**다듬기 (`video.clean_dialogue_story`)**"):doc.index("**검사 (`video.dialogue_problems`")]
    assert "듣는 사람은 present 칸이 없는 장면에서만 더한다" in sec and "∪ 듣는 사람(올바른 id" not in sec
    ds = video.clean_dialogue_story.__doc__
    assert "듣는 사람은 present 칸이 없는 장면에서만 P 에 더한다" in ds and "characters ∪ 듣는 사람 ∪ 말하는 사람" not in ds

# ── fixD (11-25 fixC 뒤 좁은 확인) ──
def _fd_family_plan(n, long=False, dialogue=True):
    """3분 구성안 (라이브러리 베스·제임스·니키의 아침 식탁): n 장면, 장면 글 보통 길이(action 25낱말) 또는 낱말 상한 가까이(long, 34낱말).
    dialogue 가 거짓이면 대사 없는 영상의 칸만 (대사·듣는 사람·present·목소리 칸이 없다)"""
    from app import cast_lib
    lib = {e["id"]: e for e in cast_lib.library()}
    ids = ("beth", "james", "niki")
    act = ("Niki leans forward over the table, eyes bright and eyebrows raised, talking quickly, while James turns his head toward her "
           "to listen closely and Beth smiles warmly with her chin resting on one hand in the light").split()
    lines = ["오늘 발표 준비는 다 했니? 어젯밤 늦게까지 연습하던데.", "네! 어젯밤에 다 외웠어요. 근데 조금 떨려요...",
             "하하, 우리 딸 최고다. 떨지 말고, 평소처럼만 해!", "아빠도 올 거죠? 맨 앞줄에 앉아 줘요."]
    pairs = [("beth", "niki"), ("niki", "beth"), ("james", "niki"), ("niki", "james")]
    scenes = []
    for k in range(n):
        sc = {"seconds": 6, "place": "P1", "characters": list(ids), "continues": False,
              "shot": "medium group shot, eye-level, still camera, all three at the table" + (" in warm morning light" if long else ""),
              "action": " ".join(act[:34 if long else 25]) + ".",
              "sound": "soft room tone, birdsong outside the window, a kettle humming quietly on the stove" + (" and a chair creaking" if long else ""),
              "ko": "가족이 아침 식탁에서 이야기를 나눈다"}
        if dialogue:
            sc.update(kind="shot", speaker="", listener="", line_ko="", emotion="", present=list(ids))
            if k % 4:
                sp, li = pairs[k % 4]
                sc.update(kind="talk", speaker=sp, listener=li, line_ko=lines[k % 4], characters=[sp, li] + [c for c in ids if c not in (sp, li)],
                          emotion="warm and natural, a little playful" + (", with a soft proud smile in the voice" if long else ""))
        scenes.append(sc)
    chars = [{"id": c, "look": lib[c]["look"]} for c in ids]
    if dialogue:
        for c, nm in zip(chars, ("베스", "제임스", "니키")):
            c.update(name_ko=nm, voice="A warm, calm and natural Korean voice, clear and steady.", voice_ko="따뜻한 목소리",
                     sample_ko="안녕, 오늘 날씨 참 좋구나. 같이 산책 갈래?")
    return {"title_ko": "발표회 날 아침", "logline_ko": "발표회 날 아침, 가족이 식탁에서 니키를 응원한다.",
            "bible": {"characters": chars, "places": [{"id": "P1", "look": "a sunny cottage kitchen with a round wooden breakfast table"}],
                      "time": "early morning, clear sky", "light": "soft warm natural window light", "style_said": "", "style": lib["niki"]["style"]},
            "music": {"tags": "gentle cinematic instrumental, piano, strings, flute", "bpm": 80, "key": "D major", "ko": "잔잔하고 따뜻한 음악"},
            "scenes": scenes}


class _FdCall:
    """같은 구성안을 들여쓴 JSON 으로 계속 돌려주는 가짜 구성안 모델 (music.bpm 이 틀려 세 차례를 모두 돈다)"""
    def __init__(self, plan):
        self.plan, self.calls = copy.deepcopy(plan), []
        self.plan["music"]["bpm"] = 10

    async def __call__(self, payload):
        self.calls.append(payload)
        return {"message": {"content": json.dumps(self.plan, ensure_ascii=False, indent=2)}}


def test_fd_01_a_story_turn_sets_one_num_ctx_that_holds_its_longest_repair_round():
    """fixD ①-1 (05#5 남은 것): 3분 대사 구성안은 장면이 22~30개가 흔해 고쳐 쓰기 차례(지시문 + 앞 답 + 문구 + 이번 답)가 12.9K~16.5K 로
    HELPER_NUM_CTX(12288)를 넘었다 (fixC 는 짧은 18장면으로 쟀다). 구성안 차례는 시작할 때 그 요청의 가장 긴 구성안(장면 수 상한)으로 num_ctx 를
    한 번 정해 모든 호출에 같은 값을 준다. 여기서는 넉넉한 어림(video.approx_tokens: ASCII 3글자·한글 1글자에 1토큰)으로 센 가장 긴 차례가 그
    값 안에 드는지 본다 — 26장면 보통 길이(지적의 재현)부터 30장면 낱말 상한 가까이·36장면 보통 길이까지, 대사 영상과 대사 없는 영상.
    36장면 낱말 상한 가까이는 이 어림이 JSON 을 약 1.3배로 세어 넘지만, 근사 토크나이저로는 약 22.5K 다 (fixD 탐침 ctx_fd.py, 문서 13.5)"""
    from app import cast_lib
    text = "아침 식탁에서 베스, 제임스, 니키가 니키의 발표회 이야기를 나누는 3분 대화 영상"
    known = cast_lib.known_in(text)
    seen = set()
    for dialogue, sizes in ((True, (18, 22, 26, 30, 36)), (False, (18, 26))):
        for n in sizes:
            for long in (False, True):
                plan = _fd_family_plan(n, long, dialogue)
                call = _FdCall(plan)
                with duo_setting(True):
                    if dialogue:
                        asyncio.run(video.write_dialogue_story(call, text, "", 180, known=known, duo=True))
                    else:
                        asyncio.run(video.write_story(call, text, "", 180, known=known))
                assert len(call.calls) == 3, (dialogue, n, long)
                ctxs = {pl["options"].get("num_ctx") for pl in call.calls}
                assert len(ctxs) == 1 and all(pl["options"]["temperature"] == 0.7 for pl in call.calls), ctxs     # 한 차례 안에서 같은 값
                ctx = ctxs.pop()
                seen.add((dialogue, ctx))
                out = video.approx_tokens(json.dumps(call.plan, ensure_ascii=False, indent=2))
                worst = max(sum(video.approx_tokens(m["content"]) + 4 for m in pl["messages"]) + out for pl in call.calls)
                assert ctx > settings.helper_num_ctx and (worst <= ctx or (n, long) == (36, True)), (dialogue, n, long, worst, ctx)
    # 3분 대사 영상 27,648 / 대사 없는 3분 영상 16,384 (요청 길이와 지시문으로 정해지고 답에 따라 바뀌지 않는다)
    assert seen == {(True, 27648), (False, 16384)}, seen
    # 짧은 영상은 HELPER_NUM_CTX 그대로, HELPER_NUM_CTX 가 0 이면(Ollama 기본값) num_ctx 를 주지 않는다
    call = Call([STORY_OK])
    asyncio.run(video.write_story(call, "비 오는 저녁 우산 쓴 남자가 집에 가는 30초 영상", "", 30))
    assert call.calls[0]["options"] == {"temperature": 0.7, "num_ctx": settings.helper_num_ctx}
    old = settings.helper_num_ctx
    settings.helper_num_ctx = 0
    try:
        call = _FdCall(_fd_family_plan(26))
        with duo_setting(True):
            asyncio.run(video.write_dialogue_story(call, text, "", 180, known=known, duo=True))
        assert all(pl["options"] == {"temperature": 0.7} for pl in call.calls)
    finally:
        settings.helper_num_ctx = old
    # 어림은 근사 토크나이저보다 크다 (한글 1글자 = 1토큰, ASCII 3글자 = 1토큰)
    assert video.approx_tokens("abc") == 1 and video.approx_tokens("니키와") == 3 and video.approx_tokens("") == 0
    # 문서 13.5·13.12 의 수치도 고쳤다 (9.3K~10.3K 는 장면 글이 짧은 18장면 기준이었다)
    here = os.path.dirname(os.path.abspath(__file__))
    doc = open(os.path.join(here, "..", "..", "docs", "short-film-dialogue-design.md"), encoding="utf-8").read()
    assert "13.3K~15.1K → 9.3K~10.3K" not in doc and "근사 계산은 세 번째 차례 9.3K~10.3K" not in doc
    assert "story_num_ctx" in doc and "27,648" in doc and "| 26장면 (177초) | 14,235 | 15,057 |" in doc


def test_fd_02_a_name_form_present_entry_is_in_the_place_for_every_check():
    """fixD ①-2: present 에 이름 꼴로 적은 항목('베스(엄마)')의 고칠 id 는 칸 검사·장면의 P·대사 검사·다듬기가 모두 그 자리에 있는 사람으로 본다.
    예전에는 칸 검사만 그렇게 보아, 그 사람이 듣는 사람이고 characters 에 없으면 같은 판에 'write the id "beth"'(엄마는 있음)와 '듣는 사람이
    present 에 없다 → nobody·혼자 클로즈업'(엄마는 없음)이 함께 나왔고, 독백 길을 따르면 같은 방의 엄마에게 하는 말이 혼자 클로즈업이 되어 다음
    판에 그대로 통과했다 (G0 위반)"""
    from app import cast_lib
    known = cast_lib.known_in("베스와 제임스, 니키의 아침 식탁")
    fam = [c for c in _variant_story()["bible"]["characters"] if c["id"] != "james_glasses"]
    scenes = [_pr(_r3_shot("P3", ["niki", "beth"], "medium shot", "Niki and Beth sit on the attic bed.", "다락방", 8), ["niki", "beth"]),
              _pr(_r5_talk("P3", ["niki"], "niki", "beth", "엄마, 이 나침반 좀 봐요!", shot="close-up", action="Niki holds up the compass, eyes bright."),
                  ["niki", "베스(엄마)"]),
              _pr(_r5_talk("P3", ["beth", "niki"], "beth", "niki", "어머, 정말 신기하구나.", action="Beth leans in, smiling."), ["niki", "beth"])]
    s, left = _r3_clean(scenes, chars=fam, exchange=True, known=known)
    two = [x for x in left if x.startswith("Scene 2")]
    assert 'Scene 2: "present" has "베스(엄마)", which is not an id: write the id "beth" (the "id" in bible.characters).' in two, two
    assert not any('is not in "present", so nobody in the place is spoken to' in x for x in two), two
    assert any('the person spoken to must be in the shot: put "beth" in "characters" right after the speaker' in x for x in two), two
    # 고치지 못한 구성안도 엄마가 대화 컷에 든다 (P 에 고칠 id)
    x = s["scenes"][1]
    assert x["characters"][:2] == ["niki", "beth"] and x["monologue"] is False and x["present"] == ["niki", "beth"], x
    # 말하는 사람을 이름 꼴로만 적어도 화면 밖 목소리(shot)가 아니다
    scenes2 = [scenes[0], _pr(_r5_talk("P3", ["niki", "beth"], "niki", "beth", "엄마, 이 나침반 좀 봐요!"), ["니키(딸)", "beth"]), scenes[2]]
    s2, left2 = _r3_clean(scenes2, chars=fam, exchange=True, known=known)
    assert s2["scenes"][1]["kind"] == "talk" and s2["scenes"][1]["characters"][:2] == ["niki", "beth"], s2["scenes"][1]
    assert not any("the speaker must be in" in m for m in left2), left2
    # 읽은 값(id·이름)과 모르는 값은 예전 그대로
    cast = video._cast_view({c["id"]: c for c in fam}, known)
    assert video._written_present({"present": ["베스(엄마)", "Niki", "Z"]}, {c["id"]: c for c in fam}, cast) == ["beth", "niki"]
    assert video._written_present({}, {c["id"]: c for c in fam}, cast) is None


_FD_ANIME = "2D anime feature film with clean cel-shaded line art and flat colours with soft shading"


def test_fd_03_a_revise_turn_reads_now_look_as_the_film_just_shown():
    """fixD ②-(b) 높음: 고치기 차례(앞 요청 + 이번 말)의 '지금 그림체 말고 다른 스타일로' 는 방금 본 영상의 화풍을 거절한 말이다. 예전에는 늘
    라이브러리 반실사로 읽어, 애니로 만든 영상 뒤의 이 말에 바르게 고른 반실사 구성안을 거절한 애니로 바꿨다. 이제 api 가 앞 결과(긴 영상) 구성안의
    화풍을 now_style 로 넘기고(film_look), 감독·storyboard·구성안 쓰기·검사·덮어쓰기가 그 값을 '지금 그림체' 로 쓴다. 한 메시지 요청(None)은
    인물의 그림체, 화풍을 모르면("") 무엇을 거절했는지 모르니 구성안이 고른 화풍을 믿는다. '원래 그림체'·'니키 그림체' 는 늘 인물의 그림체"""
    from app import cast_lib
    lib = cast_lib.library()
    lib_style = lib[0]["style"]
    said = "지금 그림체 말고 다른 스타일로"
    # 앞 영상의 화풍: 구성안 화풍 글, 비었으면 기본 실사 글, 구성안이 없으면 모름
    assert video.film_look({"scenes": [{}], "bible": {"style": _FD_ANIME}}) == _FD_ANIME
    assert video.film_look({"scenes": [{}], "bible": {"style": ""}}) == _PHOTO_PLAN
    assert video.film_look(None) == "" and video.film_look({"bible": {"style": _FD_ANIME}}) == ""
    assert cast_lib._phrase_refused(said, lib_style) == {"semi": "지금 그림체"}
    assert cast_lib._phrase_refused(said, lib_style, _FD_ANIME) == {"anime": "지금 그림체"}
    assert cast_lib._phrase_refused(said, lib_style, "") == {cast_lib.SOME_LOOK: "지금 그림체"}
    assert cast_lib._phrase_refused("원래 그림체 말고 다른 스타일로", lib_style, _FD_ANIME) == {"semi": "원래 그림체"}
    assert cast_lib._phrase_refused("니키 그림체 말고 다른 그림체로", lib_style, _FD_ANIME) == {"semi": "니키 그림체"}

    def applied(style, now):
        st = {"bible": {"characters": [{"id": "A", "name_ko": "니키", "look": "x"}], "style": style, "style_said": said}}
        cast_lib.apply_to_story(st, lib, "", keep_style=True, now_style=now)
        return st["bible"]["style"]
    # 앞 영상이 애니: 반실사·실사·3D 는 그대로, 거절한 애니는 고쳐 쓰게 하고 채울 때도 애니가 아니다
    assert cast_lib.style_said_fits(said, lib_style, lib_style, _FD_ANIME) and applied(lib_style, _FD_ANIME) == lib_style
    assert cast_lib.style_said_fits(said, _PHOTO_PLAN, lib_style, _FD_ANIME) and applied(_PHOTO_PLAN, _FD_ANIME) == _PHOTO_PLAN
    assert not cast_lib.style_said_fits(said, _FD_ANIME, lib_style, _FD_ANIME)
    assert cast_lib.style_key(applied(_FD_ANIME, _FD_ANIME)) != "anime"
    d = copy.deepcopy(STORY_OK)
    d["bible"].update(style_said=said, style=lib_style)
    assert not [x for x in video.story_problems(d, 30, True, lib_style, _FD_ANIME) if "style" in x]
    d["bible"]["style"] = _FD_ANIME
    assert any('The user turned down the look in "지금 그림체"' in x for x in video.story_problems(d, 30, True, lib_style, _FD_ANIME))
    dlg = copy.deepcopy(DLG_OK)
    dlg["bible"].update(style_said=said, style=lib_style)
    assert not [x for x in video.dialogue_problems(dlg, 48, True, known=lib[:2], now_style=_FD_ANIME) if "bible.style" in x]
    # 한 메시지 요청은 예전처럼 인물의 그림체 (반실사 글이 거절한 화풍)
    assert not cast_lib.style_said_fits(said, lib_style, lib_style) and cast_lib.style_key(applied(lib_style, None)) == "anime"
    # 앞 영상의 화풍을 모르면 구성안이 고른 화풍을 믿는다
    for plan in (lib_style, _FD_ANIME, _PHOTO_PLAN):
        assert cast_lib.style_said_fits(said, plan, lib_style, "") and applied(plan, "") == plan, plan
    # 구성안 쓰기: 앞 영상의 화풍을 요청 글에 알려 주고(알 때만), 지시문은 '지금 그림체' 의 뜻을 적는다
    for writer, args in ((video.write_story, ()), (video.write_dialogue_story, ())):
        for now, has in ((None, False), ("", False), (_FD_ANIME, True)):
            call = Call([STORY_OK])
            asyncio.run(writer(call, "니키가 숲길을 걷는 1분 영상, 애니메이션 그림체로\n지금 그림체 말고 다른 스타일로 바꿔줘", "", 60,
                               known=cast_lib.known_in("니키"), rounds=1, now_style=now))
            user = call.calls[0]["messages"][1]["content"]
            assert (f"The film made from the earlier message has this look: {_FD_ANIME}." in user) is has, (writer.__name__, now)
    assert ('"지금 그림체 말고 다른 스타일로", which turns down the look the user sees now: the look of the film made from the earlier message '
            'when the request holds one, otherwise the known characters\' own look') in video.STORY_SYSTEM
    # storyboard 가 now_style 을 구성안 쓰기·덮어쓰기에 넘긴다: 거절한 애니 구성안은 애니로 남지 않는다
    plan = copy.deepcopy(STORY_OK)
    plan["bible"]["characters"] = [{"id": "niki", "look": lib[0]["look"]}]
    for sc in plan["scenes"]:
        sc["characters"] = ["niki"]
    plan["bible"].update(style_said=said, style=_FD_ANIME)
    with _fake_story_llm(plan) as turns:
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키가 정원을 걷는 30초 영상\n" + said, seconds=30,
                                         now_style=_FD_ANIME))
    assert cast_lib.style_key(r["story"]["bible"]["style"]) != "anime" and _FD_ANIME in turns[0]["messages"][1]["content"]
    assert any('The user turned down the look in "지금 그림체"' in v for v in r["violations"]), r["violations"]      # 검사도 now_style 을 받는다
    dlg = copy.deepcopy(DLG_OK)
    dlg["bible"].update(style_said=said, style=_FD_ANIME)
    call = Call([dlg, dlg, dlg])
    with duo_setting(False):
        _st, left, _r = asyncio.run(video.write_dialogue_story(call, "니키와 푸토르가 숲속을 걸어가며 대화하는 영상\n" + said, "", 48,
                                                               known=lib[:2], now_style=_FD_ANIME))
    assert any('The user turned down the look in "지금 그림체"' in x for x in left), left
    with _fake_story_llm(plan):
        r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키가 정원을 걷는 30초 영상, " + said, seconds=30))
    assert r["story"]["bible"]["style"] == _FD_ANIME            # 한 메시지 요청: 거절한 것은 인물의 반실사 — 애니는 맞다


def test_fd_04_look_words_inside_other_words_and_keep_phrases_are_not_refusals():
    """fixD ②-(b) 중간: 인물 그림체·지금 화풍을 가리키는 말은 앞에 한글이 붙지 않아야 하고('만화풍'·'동화풍'·'동양화풍' 의 '화풍' 이 아니다), 화풍 말에
    붙거나 겹친 그림 말은 그 화풍 말의 것이다. 앞말 없는 그림 말은 뒤가 '다른' 일 때만 거절이고, 앞말이 있어도 '말고' 뒤가 바꿀 다른 대상
    ('배경만 바꿔줘')이면 그림체를 두라는 말이다. 예전에는 이런 구절이 라이브러리 반실사를 거절한 것으로 읽혀 맞는 반실사 구성안이 3D·애니로 덮였다"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    for said, want in (("만화풍 말고 다른 걸로", {"anime": "만화"}), ("동화풍 말고 다른 스타일로", {"custom:storybook illustration": "동화"}),
                       ("동양화풍 말고 다른 스타일로", {}), ("유화풍 말고 다른 그림체로", {"custom:oil painting": "유화"}),
                       ("수채화풍 말고 다른 스타일로", {"custom:watercolour painting": "수채화"}), ("만화풍 말고 반실사로", {"anime": "만화"}),
                       ("그림체는 말고 대사만 바꿔줘", {}), ("그림체 말고 배경만 바꿔줘", {}), ("지금 그림체 말고 배경만 바꿔줘", {}),
                       ("그림체 말고 다른 스타일로", {"semi": "그림체"}), ("이 스타일 말고 다른 스타일로", {"semi": "이 스타일"}),
                       ("애니 스타일 말고 다른 걸로", {"anime": "애니"}), ("지금 그림체 말고", {"semi": "지금 그림체"}),
                       ("원래 그림체 말고 바꿔줘", {"semi": "원래 그림체"}), ("그림체 말고 애니메이션으로", {}),
                       ("손 그림체 말고 다른 걸로", {}),          # 화풍 말('손 그림')과 겹친 '그림체' 는 인물 그림체가 아니다
                       ("레트로 스타일 말고 다른 걸로", {}), ("고양이 스타일 말고 다른 걸로", {}),     # 앞 낱말이 꾸미는 그림 말은 그 화풍이다
                       ("니키와 푸토르 영상, 그림체 말고 다른 걸로", {"semi": "그림체"}), ("그 그림체 말고 다른 걸로", {"semi": "그 그림체"})):
        assert cast_lib._phrase_refused(said, lib) == want, (said, cast_lib._phrase_refused(said, lib))
    # 반실사 구성안은 그대로 맞고 반실사로 남는다 (예전: '"화풍" 을 거절했다' 문구와 3D·애니)
    for said in ("만화풍 말고 다른 걸로", "동화풍 말고 다른 스타일로", "동양화풍 말고 다른 스타일로", "만화풍 말고 반실사로", "레트로 스타일 말고 다른 걸로"):
        assert cast_lib.style_said_fits(said, lib, lib) and _said_applied(lib, said) == lib, said
    # '그림체(는) 말고 …만 바꿔줘' 는 그림체를 지키는 말: 반실사 글에는 "" 로 비우는 길을 알려 주고, 채우면 라이브러리 화풍
    for said in ("그림체는 말고 대사만 바꿔줘", "그림체 말고 배경만 바꿔줘"):
        assert 'set bible.style_said to ""' in cast_lib.style_said_hint(said, lib, lib), said
        for plan in (lib, _PHOTO_PLAN):
            assert _said_applied(plan, said) == lib, (said, plan)
    # 코드가 모르는 그림 말을 거절한 구절('동양화풍 말고')은 그림 말 구절이 아니다: 실사 글도 맞다
    assert cast_lib.style_said_fits("동양화풍 말고 다른 스타일로", _PHOTO_PLAN, lib) and _said_applied(_PHOTO_PLAN, "동양화풍 말고 다른 스타일로") == _PHOTO_PLAN


def test_fd_05_style_is_a_look_word_and_a_refusal_never_gets_the_empty_path():
    """fixD ②-(a) 중간: '지금 스타일 말고 다른 스타일로'·'원래 스타일 말고 다른 스타일로' 도 인물 그림체 거절이다 (예전 refused 는 {} 라 실사
    구성안이 검사를 지나 덮어쓰기에서 소리 없이 거절한 반실사가 됐고, 반실사 구성안에는 "" 로 비우는 길을 권했다). 거절한 대상을 코드가 모르는
    거절 구절('이런 거 말고 다른 스타일로')은 반실사 글을 믿고, 문구가 나가더라도 "" 길을 내지 않는다"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    for said in ("지금 스타일 말고 다른 스타일로", "원래 스타일 말고 다른 스타일로"):
        assert cast_lib._phrase_refused(said, lib) == {"semi": said.split(" 말고")[0]}, said
        assert cast_lib.style_said_fits(said, _PHOTO_PLAN, lib) and _said_applied(_PHOTO_PLAN, said) == _PHOTO_PLAN, said
        assert not cast_lib.style_said_fits(said, lib, lib), said
        hint = cast_lib.style_said_hint(said, lib, lib)
        assert hint.startswith(f'The user turned down the look in "{said.split(" 말고")[0]}"') and 'style_said to ""' not in hint, hint
        assert cast_lib.style_key(_said_applied(lib, said)) == "anime", said
    said = "이런 거 말고 다른 스타일로"
    assert cast_lib._phrase_refused(said, lib) == {} and cast_lib.style_said_fits(said, lib, lib)
    hint = cast_lib.style_said_hint(said, lib, lib)
    assert 'style_said to ""' not in hint and hint.startswith("bible.style reads as the default semi-realistic painted look, but"), hint
    # 거절이 없는 구절은 예전 그대로 "" 길 (원래 그림체·분위기)
    assert 'set bible.style_said to ""' in cast_lib.style_said_hint("따뜻한 느낌으로", lib, lib)


def test_fd_06_custom_refusals_go_by_material_and_the_wanted_words_are_quoted():
    """fixD ②-(a) 낮음: custom 거절은 재료 이름으로 따진다 — '클레이 말고 픽셀 아트로' 의 픽셀 아트 구성안이 맞다 (예전: custom 열쇠째 거절해 맞는
    구성안도 거절하고 끝까지 수렴하지 않았다). '수채화 말고' 의 '화' 도 거절 말의 것이다. 바라는 화풍의 열쇠가 None 이어도('반실사 말고 고흐
    그림처럼') 그 말을 인용한다 (예전: 다른 화풍 예시를 들어 예시대로 고친 답이 고흐를 잃었다)"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    pixel = "16-bit pixel art with a limited palette"
    assert cast_lib._phrase_refused("클레이 말고 픽셀 아트로") == {"custom:claymation": "클레이"}
    assert cast_lib.style_said_fits("클레이 말고 픽셀 아트로", pixel) and _said_applied(pixel, "클레이 말고 픽셀 아트로") == pixel
    assert not cast_lib.style_said_fits("클레이 말고 픽셀 아트로", "claymation with soft clay textures")
    assert cast_lib._phrase_refused("수채화 말고 유화로") == {"custom:watercolour painting": "수채화"}
    oil, water = "rich oil painting with thick visible brush strokes", "soft watercolour painting with visible paper texture"
    assert cast_lib.style_said_fits("수채화 말고 유화로", oil) and not cast_lib.style_said_fits("수채화 말고 유화로", water)
    assert cast_lib.style_said_hint("수채화 말고 유화로", water).startswith(
        'The user turned down the look in "수채화" and asked for an oil painting look: write that look in bible.style')
    assert cast_lib.custom_style_text("수채화 말고 유화로", phrase=True) == "oil painting style"
    tail = "; if bible.style_said holds more than the user's words for the look, copy only those words."
    assert cast_lib.style_said_hint("반실사 말고 고흐 그림처럼", lib) == (
        'The user turned down the look in "반실사" and asked for the look in "고흐 그림처럼": write that look in bible.style and name its medium '
        "(for example oil painting, watercolour, ink, pencil, 2D animation or 3D animation)" + tail)
    # 바라는 말이 '다른 …' 이면 예시 그대로
    assert cast_lib.style_said_hint("반실사 말고 다른 그림체로", lib).startswith(
        'The user turned down the look in "반실사": write a different look in bible.style (for example')


def test_fd_07_revise_turn_passes_the_last_film_look_from_api():
    """fixD ②-(b): api 의 고치기 갈래(t2v)가 앞 결과의 구성안 화풍을 요청의 now_style 로 적는다 (감독이 storyboard 에 넘긴다 —
    test_studio_director t_fd_revise_passes_the_last_film_look_to_the_story). 구성안이 없으면(짧은 영상) "" — 모른다"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "app", "studio", "api.py"), encoding="utf-8").read()
    i = src.index('if dec["carry"] == "revise" and last and dec["action"] in ("image", "i2v", "t2v"):')
    block = src[i:src.index("# 긴 영상(20초 넘게", i)]
    assert 'elif dec["action"] == "t2v":' in block and 'video.film_look(lwork.get("story") if isinstance(lwork, dict) else None)' in block
    assert '"now_style"' in block


def test_fe_01_dislike_words_refuse_a_look_in_the_said_phrase():
    """11-25 최종 확인: '싫어·별로·제외·말구·안 돼' 로 거절한 구절('반실사는 싫어, 애니로')을 청한 말로 읽어, 사용자가 거절한 반실사가 최종 화풍이
    됐다 (어느 구성안이든 반실사로 덮었다). 구절 길만 넓히고 요청 글 예비 길(_negated)은 그대로다"""
    from app import cast_lib
    lib = cast_lib.library()[0]["style"]
    anime = "2D anime style, clean line art, cel shading"
    for said, want in (("반실사는 싫어, 애니로", "anime"), ("반실사 싫고 애니메이션으로", "anime"), ("반실사는 별로야 애니로 해줘", "anime"),
                       ("반실사 제외하고 애니메이션으로", "anime"), ("반실사 말구 애니로", "anime"), ("반실사는 안 돼, 애니메이션으로", "anime"),
                       ("반실사 그림체는 싫어 실사로", "photo"), ("애니메이션은 싫어 실사로", "photo"), ("반실사 별로, 수채화로", "custom")):
        assert cast_lib.said_phrase_key(said) == want, (said, cast_lib.said_phrase_key(said))
        for plan in (lib, anime, _PHOTO_PLAN):
            assert cast_lib.style_key(_said_applied(plan, said)) == want, (said, plan, _said_applied(plan, said))
    assert cast_lib._phrase_refused("반실사는 싫어, 애니로", lib) == {"semi": "반실사"}
    assert cast_lib._wanted_words("반실사는 싫어, 고흐 그림처럼") == "고흐 그림처럼"
    # 인물 그림체·지금 화풍을 싫다고 한 말도 거절이다
    for said in ("원래 그림체 싫어, 다른 걸로", "지금 그림체 싫어 다른 걸로", "반실사는 싫어, 다른 스타일로"):
        assert "semi" in cast_lib._phrase_refused(said, lib), said
        assert cast_lib.style_key(_said_applied(lib, said)) != "semi" and cast_lib.style_key(_said_applied(anime, said)) == "anime", said
    # 그림체를 두라는 말은 여전히 거절이 아니다
    assert cast_lib._phrase_refused("그림체는 별로 안 바꾸고 배경만", lib) == {}
    assert cast_lib.said_text("원래 그림체 그대로") == ""
    # 요청 글 예비 길은 넓히지 않았다
    assert not cast_lib._negated("반실사는 싫어, 애니로", 0, 3)


# ── 정본 인물 장면 제작진 (11-26, CREW_CAST_MODE) ─────────────────────────
@contextlib.contextmanager
def cast_mode(mode):
    saved = settings.crew_cast_mode
    settings.crew_cast_mode = mode
    try:
        yield
    finally:
        settings.crew_cast_mode = saved


CAST_LOOK = {"fit_menu": True, "ko": {"plan": "숲길의 아침입니다.", "direction": "인물을 화면 왼쪽에 둡니다.",
                                      "camera_lighting": "부드러운 아침 햇빛입니다.", "details": "옅은 안개가 낍니다."},
             "notes_en": ["Niki in the left third of the frame on the forest path", "Soft low morning sun through the trees",
                          "Pale mist between the far trunks", "A calm and gentle mood over the whole path"]}


def test_render_keeps_cast_clips_out_of_crew():
    with cast_mode("safe"):
        result, calls, turns = run_render([{"ko": KO, "prompt": MOTION}], username="alice", slug="alice-main",
                                          text="니키가 카페 창가에서 책장을 넘기는 영상", profile_id="wan22_i2v", crew=True)
    assert len(calls) == 1 and result["crew"] is None                     # 영상 메뉴에는 정본 장면 분야가 없다 (KeyError 없이)
    assert "choices" not in calls[0]["format"]["properties"]
    assert any("영상 제작진 단계를" in a and "캐릭터 정본" in a for a in result["advice"])


def test_storyboard_film_crew_same_ctx():
    from app import cast_lib
    payloads, turns = [], []
    fake = types.ModuleType("app.llm")

    class HelperBusy(Exception):
        pass

    story = {"title_ko": "숲길", "logline_ko": "니키가 숲길을 걷는다.",
             "bible": {"characters": [{"id": "niki", "look": "a girl"}], "places": [{"id": "P1", "look": "a winding forest path"}],
                       "time": "morning", "light": "soft daylight", "style": "", "style_said": ""},
             "music": {"tags": "calm piano", "bpm": 70, "key": "D major", "ko": "잔잔한 피아노"},
             "scenes": [{"seconds": 14, "place": "P1", "characters": ["niki"], "continues": False, "shot": "wide shot",
                         "action": "She walks along the path.", "sound": "footsteps", "ko": "걷는다"},
                        {"seconds": 14, "place": "P1", "characters": [], "continues": False, "shot": "wide shot",
                         "action": "The empty path in the morning.", "sound": "birds", "ko": "빈 숲길"}]}

    @contextlib.asynccontextmanager
    async def llm_turn(user, ip, model, purpose, wait_seconds=None):
        turns.append(purpose)

        async def call(payload):
            payloads.append(payload)
            if "choices" in (payload.get("format") or {}).get("properties", {}):
                return {"message": {"content": json.dumps(fit(CAST_LOOK, payload))}}
            return {"message": {"content": json.dumps(story)}}
        yield call

    fake.HelperBusy, fake.llm_turn = HelperBusy, llm_turn
    saved = sys.modules.get("app.llm")
    sys.modules["app.llm"] = fake
    try:
        with cast_mode("safe"):
            r = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키가 숲길을 걷는 영상", seconds=28))
        with cast_mode("off"):
            n = len(payloads)
            off = asyncio.run(video.storyboard({"id": 1}, "127.0.0.1", "studio", "studio-chat", "니키가 숲길을 걷는 영상", seconds=28))
    finally:
        if saved is None:
            sys.modules.pop("app.llm", None)
        else:
            sys.modules["app.llm"] = saved
    assert turns[0] == "video_story" and turns.count("video_story") == 2           # 영화 초안은 같은 차례 안에서 (새 GPU 차례 0회)
    assert payloads[0]["messages"][0]["content"].startswith("You are a small production crew of experts preparing the shared look")
    ctxs = {p["options"].get("num_ctx") for p in payloads[:n]}
    assert len(ctxs) == 1 and None not in ctxs                                    # 초안과 구성안의 num_ctx 가 같다
    assert payloads[0]["options"]["temperature"] == 0.6
    user = payloads[1]["messages"][1]["content"]
    assert "Look plan from the crew (not the user's words; never copy it into style_said" in user
    assert "Must-keep details from the user" not in user
    st = r["story"]
    assert st["bible"]["style_said"] == "" and st["bible"]["style"] == cast_lib.library()[0]["style"]
    look = st["crew"]
    assert look["light"] and look["light"] in st["bible"]["light"] and len(st["bible"]["light"]) <= video.FILM_LIGHT_MAX
    assert r["film_crew"]["used"] and r["film_crew"]["cast"] == ["niki"] and r["film_crew"]["picked"]
    # off: 예전과 같다 (초안 없음, 빛 계획 없음)
    assert off["film_crew"] is None and "crew" not in off["story"] and off["story"]["bible"]["light"] == "soft daylight"
    assert not any("crew of experts" in p["messages"][0]["content"] for p in payloads[n:])
    assert all("num_ctx" not in p["options"] or p["options"]["num_ctx"] == video.story_num_ctx(
        video.story_base("니키가 숲길을 걷는 영상", "", 28, cast_lib.known_in("니키가 숲길을 걷는 영상"))[0],
        video.story_counts(28)[2], dialogue=False) for p in payloads[n:])


def test_place_card_prompt_adds_air():
    st = video.clean_story(STORY_OK, 30)
    plain = video.place_card_prompt(st, "P1")
    assert "Air:" not in plain
    st["crew"] = {"air": "pale morning mist drifting between the trunks"}
    p = video.place_card_prompt(st, "P1")
    assert "Air: pale morning mist drifting between the trunks." in p and "nobody in it" in p and p.endswith("No text.")
    assert "There is no person, no animal and no figure in the picture." in p


def test_still_prompt_keeps_hands_sentence_with_crew_light():
    import copy as _copy
    st = video.clean_dialogue_story(_copy.deepcopy(DLG_OK), 48)
    st["bible"]["light"] = "warm golden window light " * 12
    look = {"light": "soft warm practical lamp light pooling on the table, " * 3, "air": "fine dust in the light",
            "light_terms": ["lamp"]}
    look["light"] = crew._clip(look["light"], crew.LOOK_LIGHT_MAX)
    video.add_crew_look(st, look)
    assert len(st["bible"]["light"]) <= video.FILM_LIGHT_MAX and st["bible"]["light"].endswith(look["light"])
    for i in range(len(st["scenes"])):
        people = video.scene_people(st, i)
        if not people:
            continue
        cp = video.compose_prompt(st, i, [c for c in people][:2])
        assert len(cp) <= video.STILL_PROMPT_MAX
        assert "The hands are not visible or relaxed." in cp and cp.endswith(video.people_sentence(len(people)))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"ok ({len(tests)} tests)")
