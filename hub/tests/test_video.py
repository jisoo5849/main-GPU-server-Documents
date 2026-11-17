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
                      "sounds once, clearly. A calm instrumental score of warm piano and a low synthesizer pad rises slowly, "
                      "then fades with the final frame."),
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
    bad = dict(SOUND_OK, sound=SOUND_OK["sound"].replace("then fades with the final frame.",
                                                         "fading gently into the background bokeh lights."))
    probs = video.sound_problems(bad, "", ltx)
    assert any("only seen" in p and "bokeh" in p for p in probs)
    assert not any("only seen" in p for p in video.sound_problems(SOUND_OK, "", ltx))


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"ok ({len(tests)} tests)")
