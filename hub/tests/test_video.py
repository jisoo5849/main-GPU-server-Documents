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


def run_render(replies, frame_model=None, **kw):
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"ok ({len(tests)} tests)")
