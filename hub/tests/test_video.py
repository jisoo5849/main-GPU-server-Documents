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
    assert "300 to 450 words" in sys_msg["content"] and "quality buzzwords" in sys_msg["content"]
    assert "EXAMPLE of the length" in sys_msg["content"] and "ceramic teacup" in sys_msg["content"]   # 러다이트 밀도 예시
    assert "Model: Wan 2.2 T2V 14B." in sys_msg["content"] and "러다이트" not in sys_msg["content"]  # Luddite 로 읽히지 않게
    assert "LOOK REFERENCE" in user_msg["content"] and "do not restate" not in user_msg["content"]
    assert "STYLE (appended after your text" in user_msg["content"] and "STYLE X" in user_msg["content"]
    sys_i2v, user_msg = video.build_messages("향수 광고 영상", "", I2V_LONG, 5.0, FRAME, SCENE, style="STYLE X")
    assert "never contradict" in user_msg["content"] and "FIRST FRAME" in user_msg["content"]
    assert "EXAMPLE of the length" not in sys_i2v["content"] and "STYLE (" not in user_msg["content"]
    assert "FIRST FRAME notes" in sys_i2v["content"] and "220 to 380 words" in sys_i2v["content"]
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
    assert any("words; write 220 to 380" in p
               for p in video.check_video(LONG, I2V_LONG, "향수 광고 영상", FRAME, SCENE))   # I2V 장문은 조금 짧게
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"ok ({len(tests)} tests)")
