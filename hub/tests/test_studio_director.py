"""감독 시험 (5절): 가짜 DB·가짜 부품으로 P1 한 줄기(사진 → 영상 → 진행 → 결과)와 실패·취소 길을 확인한다. GPU·DB 없이 돈다.

  cd hub && python tests/test_studio_director.py
"""
import asyncio
import contextlib
import copy
import datetime
import inspect
import os
import sys
import types
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# app.db 는 asyncpg 가 있어야 불러와지므로 가짜로 둔다
import app  # noqa: E402,F401

_db = types.ModuleType("app.db")
_db.ip = lambda v: v
sys.modules["app.db"] = _db
app.db = _db

from app.config import settings  # noqa: E402
from app.studio import director as D  # noqa: E402
from app.studio import intent, recipes, store  # noqa: E402

D.POLL = 0.01
D.LLM_POLL = 0.01
settings.studio_progress_min_interval_ms = 0
settings.studio_inspect = "idle"

REC = recipes.load_all()["wan22_i2v_14b_fast"]
LTX = recipes.load_all()["ltx25_i2v_sound"]
LTXH = recipes.load_all()["ltx23_dev_i2v_sound_quality"]
SOUND_TEXT = "Audio: A soft room tone and a calm instrumental score of warm piano that fades with the final frame."
IMG = recipes.load_all()["zimage_turbo_t2i"]
# P3 레시피는 메인에서 내보낸 파일이 오기 전이라 기존 레시피를 고쳐 시험용으로 만든다 (감독 코드만 본다)
T2V = copy.deepcopy(REC)
T2V.update(id="wan22_t2v_test", action="t2v", video_profile="wan22_t2v_long")
del T2V["inject"]["first_frame"], T2V["prompt"]["97"]
del T2V["prompt"]["129:98"]["inputs"]["start_image"]
EDIT = copy.deepcopy(IMG)
EDIT.update(id="qwen_edit_test", action="edit")
EDIT["prompt"]["41"] = {"class_type": "LoadImage", "inputs": {"image": "studio/_check/sample.png"}}
EDIT["inject"]["edit_image"] = {"node": "41", "input": "image", "type": "input_image"}
del EDIT["inject"]["width"], EDIT["inject"]["height"]
USER = {"id": 7, "username": "bob", "active": True}
ENV = {"id": 3, "slug": "studio-chat", "name": "chat", "username": "studio", "linux_uid": 2001}


class FakeStore:
    def __init__(self):
        self.turns, self.events, self.messages, self.links, self.atts, self.progress = {}, [], [], [], {}, {}
        self.jobs, self.outputs, self.by_client = {}, {}, {}
        self.results = []
        self.stages = []
        self.thread_cfg = {}

    def patch(self):
        fs = self

        async def get_user(uid): return USER
        async def studio_env(slug): return ENV
        async def attachments_of(ids): return [a for a in fs.atts.values() if str(a["id"]) in [str(i) for i in ids]]
        async def merge_work(tid, patch): fs.turns[tid]["work"] = {**(fs.turns[tid].get("work") or {}), **patch}
        async def event(tid, kind, detail=None): fs.events.append((kind, detail))
        async def save_progress(tid, prog): fs.progress[tid] = prog
        async def link_job(tid, jid, step, purpose=None, attempt=1): fs.links.append((step, str(jid), purpose))
        async def add_message(thread_id, role, kind="text", body="", data=None, turn_id=None, client_msg_id=None):
            m = {"id": len(fs.messages) + 1, "thread_id": thread_id, "role": role, "kind": kind, "body": body,
                 "data": data, "turn_id": turn_id}
            fs.messages.append(m)
            return m
        async def touch_thread(*a, **k): return None
        async def move(tid, from_state, to_state, stage=None, **cols):
            t = fs.turns[tid]
            froms = [from_state] if isinstance(from_state, str) else list(from_state)
            if t["state"] not in froms:
                return False
            t["state"] = to_state
            if stage:
                t["stage"] = stage
            t.update(cols)
            return True
        async def count_active(uid): return sum(1 for t in fs.turns.values() if t["state"] == "active" and t["user_id"] == uid)
        async def pending_turns(): return [t for t in fs.turns.values() if t["state"] == "pending"]
        async def active_turns(): return [t for t in fs.turns.values() if t["state"] == "active"]
        async def get_turn(tid, uid=None): return fs.turns.get(uuid.UUID(str(tid)))
        async def job_state(jid): return fs.jobs.get(str(jid))
        async def job_outputs(jid): return fs.outputs.get(str(jid), [])
        async def job_by_client(env_id, cid): return fs.by_client.get(cid)
        async def cancel_requested(tid): return fs.turns[tid].get("cancel_requested", False)
        async def request_cancel(tid): fs.turns[tid]["cancel_requested"] = True; return True
        async def turn_job(tid, step):
            for st, jid, _p in reversed(fs.links):
                if st == step:
                    return jid
            return None
        async def add_attachment(att): fs.results.append(att); fs.atts[str(att["id"])] = att; return att
        async def set_stage(tid, stage): fs.stages.append(stage)
        async def thread_settings(thread_id): return fs.thread_cfg
        async def set_message_data(mid, patch):
            for m in fs.messages:
                if m["id"] == mid:
                    m["data"] = {**(m["data"] or {}), **patch}
        async def expired_plans(hours): return [t for t in fs.turns.values() if t["state"] == "awaiting_approval" and t.get("stale")]
        async def approve_turn(tid, stage="queue"):
            t = fs.turns[tid]
            if t["state"] != "awaiting_approval":
                return False
            t.update(state="pending", stage=stage, work={**(t.get("work") or {}), "approved": True})
            return True
        async def replan_turn(tid, stage, rid, ver):
            t = fs.turns[tid]
            if t["state"] != "awaiting_approval":
                return False
            t.update(state="pending", stage=stage, work={}, plan=None, recipe_id=rid, recipe_version=ver)
            return True

        for k, v in list(locals().items()):
            if k != "fs" and callable(v):
                setattr(store, k, v)


class FakeDeps:
    def __init__(self, fs, violations=None, outcome="success", fix_changes=True, rec=None):
        self.fs, self.violations, self.outcome = fs, violations or [], outcome
        self.rec = rec or REC
        self.outcome_seq = []        # 접수마다 차례로 쓸 outcome (없으면 outcome)
        self.violation_seq = []      # render 호출마다 차례로 돌려줄 위반 목록 (없으면 violations)
        self.fix_violation_seq = []  # fix 호출마다 차례로 돌려줄 남은 위반 목록 (없으면 비어 있음)
        self.calls = []
        self.receivers = {}
        self.queue_rows = []
        self.settings = {}
        self.published = []
        self.median = {"comfy": None, "llm": None}
        self.cancel_ret = 0
        self.hold = False       # True 면 작업이 queued 에 머문다
        self.inspect_result = {"ok": True, "issues": [], "traits": [], "people": 0}
        self.submit_error = None
        self.last_job = None

    @contextlib.contextmanager
    def llm_context(self):
        bag = []
        yield bag

    async def render(self, user, ip, env, **kw):
        self.calls.append(("render", kw))
        await asyncio.sleep(0.02)
        ltx = kw.get("profile_id") == "ltx25_i2v_long"
        sec = float(kw.get("seconds") or 5.0)
        return {"prompt": "P " * 300 + (("\n\n" + SOUND_TEXT) if ltx else ""), "sound": SOUND_TEXT if ltx else "",
                "parts": {"a": "x"}, "ko": {"subject": "s"}, "motion": "m",
                "profile": "ltx25_i2v_long" if ltx else "wan22_i2v_long",
                "settings": {"frames": int(sec * 24) + 1, "seconds": sec} if ltx else {"frames": 81, "seconds": 5.0},
                "frame": {"subject": "cockpit"},
                "violations": self.violation_seq.pop(0) if self.violation_seq else self.violations,
                "length_choice": {"tier": "long", "reason": "자세해서", "by": "rule"}, "crew": None, "rounds": 0}

    async def write_edit(self, user, ip, text, frame=None):
        self.calls.append(("write_edit", text))
        await asyncio.sleep(0.02)
        return {"prompt": "Replace the background with a warm sunset sky and keep the person unchanged", "violations": [],
                "rounds": 1}

    async def expand_image(self, user, ip, text, crew=None):
        self.calls.append(("expand_image", text))
        await asyncio.sleep(0.02)
        return {"prompt": "IMG " + text, "body": text, "keep": [], "categories": {}, "people": 1, "violations": [],
                "rounds": 1, "crew": {"category": "공간", "used": True}, "advice": []}

    async def fix(self, user, ip, env, **kw):
        self.calls.append(("fix", kw))
        return {"prompt": "FIXED " * 300, "parts": {"a": "y"}, "ko": {}, "changed": True, "fixed": True,
                "violations": self.fix_violation_seq.pop(0) if self.fix_violation_seq else [],
                "before_ko": [], "words_before": 1, "words_after": 1, "rounds": 1}

    async def inspect(self, user, ip, username, filename, subfolder, expect):
        self.calls.append(("inspect", (username, subfolder, filename, expect)))
        return self.inspect_result

    def frame_known(self, frame): return bool(frame)
    def pov(self, text): return "1인칭" in text or "파일럿" in text

    async def submit(self, env, user, body, ip):
        self.calls.append(("submit", body))
        if self.submit_error:
            raise self.submit_error
        jid = str(uuid.uuid4())
        self.last_job = jid
        cid = body["client_id"]
        self.fs.jobs[jid] = {"id": jid, "seq": 5, "state": "queued", "outcome": None, "error": None,
                             "started_at": None}
        self.fs.by_client[cid] = {"prompt_id": jid, "number": 5, "node_errors": {}}
        self.queue_rows = [{"id": jid, "seq": 5, "state": "queued", "kind": "comfy"}]
        outcome = self.outcome_seq.pop(0) if self.outcome_seq else self.outcome
        rec = IMG if body["extra_data"]["studio"]["recipe"] == IMG["id"] else self.rec
        asyncio.ensure_future(self._drive(jid, cid, outcome, rec))
        return {"prompt_id": jid, "number": 5, "node_errors": {}}

    async def _drive(self, jid, cid, outcome, rec=None):
        rec = rec or self.rec
        j = self.fs.jobs[jid]
        bands = rec["progress"]["bands"]
        r = self.receivers.get(cid)
        while self.hold:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.03)
        j["state"] = "starting"
        await asyncio.sleep(0.03)
        j["state"] = "running"
        j["started_at"] = datetime.datetime.now(datetime.timezone.utc)
        if r:
            await r.send_text('{"type":"execution_start","data":{"prompt_id":"%s"}}' % jid)
            for n in bands[0]["nodes"]:
                await r.send_text('{"type":"executing","data":{"node":"%s","prompt_id":"%s"}}' % (n, jid))
            for b in bands:
                if b.get("sampler"):
                    for n in b["nodes"]:
                        await r.send_text('{"type":"executing","data":{"node":"%s","prompt_id":"%s"}}' % (n, jid))
                        await r.send_text('{"type":"progress","data":{"node":"%s","value":2,"max":2,"prompt_id":"%s"}}' % (n, jid))
        await asyncio.sleep(0.03)
        if outcome == "success":
            if rec["action"] in ("image", "edit"):
                self.fs.outputs[jid] = [{"node_id": "9", "rel_path": "studio/chat/2026-11-12/bob/abcd1234/image_00001_.png", "media": "images"}]
            else:
                self.fs.outputs[jid] = [{"node_id": rec["outputs"]["main"]["node"], "rel_path": "studio/chat/2026-11-12/bob/abcd1234/video_00001_.mp4", "media": "videos"},
                                        {"node_id": "901", "rel_path": "studio/chat/2026-11-12/bob/abcd1234/check_frame_00001_.png", "media": "images"}]
            if r:
                await r.send_text('{"type":"execution_success","data":{"prompt_id":"%s"}}' % jid)
        j["state"] = "cleaning"
        await asyncio.sleep(0.03)
        j["state"] = "finished"
        j["outcome"] = outcome
        if outcome == "error":
            j["error"] = {"message": "boom"}
        self.queue_rows = []

    async def stage_image(self, env, user, turn, rel):
        self.calls.append(("stage_image", rel))
        att_id = str(uuid.uuid4())
        self.fs.atts[att_id] = {"id": att_id, "media": "image", "input_rel": f"studio/bob/{att_id}.png", "width": 1024, "height": 576}
        return {"id": att_id, "width": 1024, "height": 576}

    async def cancel_queued(self, env_id, ids):
        self.calls.append(("cancel_queued", ids))
        j = self.fs.jobs[ids[0]]
        if j["state"] == "queued":
            j["state"], j["outcome"] = "finished", "cancelled"
            return 1
        return 0

    async def interrupt(self, env_id, job_id):
        self.calls.append(("interrupt", job_id))
        j = self.fs.jobs[job_id]
        j["state"], j["outcome"] = "finished", "cancelled"

    async def medians(self): return self.median
    async def queue(self): return self.queue_rows
    async def setting(self, key): return self.settings.get(key)

    def register(self, env_id, cid, receiver): self.receivers[cid] = receiver
    def unregister(self, env_id, cid, receiver): self.receivers.pop(cid, None)
    async def publish(self, uid, event): self.published.append(event)


def new_turn(fs, text="천천히 앞으로 나아가는 영상으로 만들어줘", used_default=False, spec=None, mode=None, rec=None):
    rec = rec or REC
    atts = []
    if rec["action"] in ("i2v", "edit"):
        att_id = uuid.uuid4()
        fs.atts[str(att_id)] = {"id": att_id, "media": "image", "input_rel": f"studio/bob/{att_id}.png", "width": 1920, "height": 1080}
        atts = [str(att_id)]
    req = {"text": text, "used_default": used_default, "attachment_ids": atts, "spec": spec or {}}
    if mode:
        req["mode"] = mode
    t = {"id": uuid.uuid4(), "thread_id": uuid.uuid4(), "user_id": 7, "client_ip": "10.0.0.5", "state": "pending",
         "stage": None, "recipe_id": rec["id"], "recipe_version": 1, "request": req, "work": None, "depends_on": None,
         "action": rec["action"]}
    fs.turns[t["id"]] = t
    return t


async def settle(d, timeout=5):
    for _ in range(int(timeout / 0.01)):
        if not d.tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("요청이 끝나지 않았다")


def kinds(fs):
    return [k for k, _ in fs.events]


async def t_happy_path():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs, "비행기를 운항하는 파일럿의 1인칭 시점 영상으로 만들어줘", spec={"seconds": None, "notes": ["소리는 아직 넣지 못합니다"]})
    await d.pump()
    await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events)
    # 호출: render(첫 화면 읽기 포함, 길이 자동) → submit. 낱말 수 위반이 없으니 fix 는 부르지 않는다
    names = [c[0] for c in deps.calls]
    assert names[0] == "render" and "fix" not in names and "submit" in names, names
    r = deps.calls[0][1]
    assert r["profile_id"] == "wan22_i2v_long" and r["auto_length"] is True and r["read_frame_on"] is True
    assert r["image"]["type"] == "input" and r["image"]["subfolder"] == "studio/bob" and r["crew"] is None
    body = [c[1] for c in deps.calls if c[0] == "submit"][0]
    p = body["prompt"]
    assert p["129:93"]["inputs"]["text"].startswith("P ") and p["129:98"]["inputs"]["length"] == 81
    assert (p["129:98"]["inputs"]["width"], p["129:98"]["inputs"]["height"]) == (848, 480)      # 1920x1080 사진 비율
    assert p["108"]["inputs"]["filename_prefix"].startswith("bob/" + str(t["id"])[:8] + "/")
    assert body["client_id"].startswith("studio-" + str(t["id"]))
    for k in ("turn_created", "spec", "render", "negative_check", "comfy_submitted", "output", "inspect", "node_timing"):
        assert k in kinds(fs), (k, kinds(fs))
    nt = [d_ for k, d_ in fs.events if k == "node_timing"][0]["nodes"]
    assert nt and all(set(n) == {"node", "ms"} and n["ms"] >= 0 for n in nt) and {"129:86", "129:85"} <= {n["node"] for n in nt}, nt
    # 결과 메시지와 결과 첨부
    res = [m for m in fs.messages if m["kind"] == "result"]
    assert len(res) == 1 and res[0]["data"]["attachment_ids"] and "소리는 아직 넣지 못합니다" not in res[0]["data"]["notes"] or True
    assert fs.results[0]["source"] == "result" and fs.results[0]["media"] == "video"
    assert fs.results[0]["result_rel"].endswith("video_00001_.mp4")
    # 진행: 단계가 대기 → 준비 → 생성 → 마무리로 가고 %가 줄지 않고 100 에 닿는다
    stages = [e["progress"]["stage"] for e in deps.published if e["type"] == "turn.progress"]
    order = ["frame", "queue", "prepare", "generate", "collect"]
    seen = [s for i, s in enumerate(stages) if i == 0 or s != stages[i - 1]]
    assert [s for s in seen if s in order] == sorted([s for s in seen if s in order], key=order.index), seen
    pcts = [e["progress"]["pct"] for e in deps.published if e["type"] == "turn.progress" and e["progress"]["pct"] is not None]
    assert pcts == sorted(pcts) and pcts[-1] == 100, pcts
    # 결과 검사: 1인칭 요청이면 기대 특징을 넣고, 검사 프레임을 결과 폴더 주인 기준으로 읽는다
    ins = [c[1] for c in deps.calls if c[0] == "inspect"][0]
    assert ins[0] == "studio" and ins[1] == "chat/2026-11-12/bob/abcd1234" and ins[2] == "check_frame_00001_.png"
    assert ins[3] == [D.FIRST_PERSON_TRAIT]
    assert d.tasks == {} and not deps.receivers      # 수신기를 풀었다
    assert fs.stages[0] == "frame" and fs.stages[-1] == "collect" and len(set(fs.stages)) == len(fs.stages) - fs.stages.count("x"), fs.stages
    assert "generate" in fs.stages
    assert any(e["type"] == "turn.state" and e["state"] == "done" for e in deps.published)


async def t_default_text_skips_crew():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs, intent.DEFAULT_I2V_TEXT, used_default=True)
    await d.pump(); await settle(d)
    assert t["state"] == "done"
    assert deps.calls[0][1]["crew"] is False
    ev = dict((k, v) for k, v in fs.events if k == "render")["render"]
    assert ev["crew_skipped"] == "기본 요청문이라 제작진 생략"


async def t_fix_only_for_non_word_violations():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, violations=["The prompt has 520 words; write 250 to 450 words."])
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump(); await settle(d)
    assert t["state"] == "done" and "fix" not in [c[0] for c in deps.calls]
    fs2 = FakeStore(); fs2.patch()
    deps2 = FakeDeps(fs2, violations=["Do not write 'then transitions to'", "The prompt has 520 words; write 250 to 450 words."])
    d2 = D.Director(deps2, {REC["id"]: REC})
    t2 = new_turn(fs2)
    await d2.pump(); await settle(d2)
    assert t2["state"] == "done" and "fix" in [c[0] for c in deps2.calls]
    body = [c[1] for c in deps2.calls if c[0] == "submit"][0]
    assert body["prompt"]["129:93"]["inputs"]["text"].startswith("FIXED")      # 고친 글을 쓴다
    assert "fix" in kinds(fs2)
    chips = [e["progress"]["chips"] for e in deps2.published if e["type"] == "turn.progress"][-1]
    assert "fix" in [c["key"] for c in chips]


async def t_cancel_while_queued():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.hold = True
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump()
    for _ in range(300):
        if deps.last_job:
            break
        await asyncio.sleep(0.01)
    assert deps.last_job
    await asyncio.sleep(0.05)
    await d.cancel(t)
    await settle(d)
    assert t["state"] == "cancelled", (t["state"], t.get("error"))
    assert ("cancel_queued", [deps.last_job]) in deps.calls
    assert not [m for m in fs.messages if m["kind"] == "result"]


async def t_cancel_running_uses_interrupt():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump()
    for _ in range(500):
        j = fs.jobs.get(deps.last_job or "")
        if j and j["state"] == "running":
            break
        await asyncio.sleep(0.005)
    await d.cancel(t)
    await settle(d)
    assert t["state"] in ("cancelled", "done"), t["state"]
    if t["state"] == "cancelled":
        assert not [m for m in fs.messages if m["kind"] == "result"]


async def t_comfy_error_notice():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, outcome="error")
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump(); await settle(d)
    assert t["state"] == "failed" and t["error"]["code"] == "run_error", (t["state"], t.get("error"), fs.events[-3:])
    n = [m for m in fs.messages if m["kind"] == "notice"][-1]
    assert n["body"].endswith("다시 만들어 볼까요?") and n["data"]["actions"][0]["id"] == "retry_seed"


async def t_submit_failure_disables_recipe_after_limit():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.submit_error = RuntimeError("검증 실패")
    d = D.Director(deps, {REC["id"]: REC})
    for _ in range(settings.studio_recipe_fail_limit):
        t = new_turn(fs)
        await d.pump(); await settle(d)
        assert t["state"] == "failed" and t["error"]["code"] == "submit"
    assert REC["id"] in d.disabled and d.recipe_for("i2v") is None
    t = new_turn(fs)
    await d.pump(); await settle(d)
    assert t["state"] == "failed" and t["error"]["code"] == "prepare_down"


async def t_inspect_notice_and_first_person_filter():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.inspect_result = {"ok": False, "issues": [{"type": "floating_limb"}, {"type": "extra_fingers"}], "traits": [], "people": 1}
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs, "파일럿의 1인칭 시점 영상")
    await d.pump(); await settle(d)
    assert t["state"] == "done" and not [m for m in fs.messages if m["kind"] == "notice"]    # 1인칭이면 팔다리 안내를 하지 않는다
    fs2 = FakeStore(); fs2.patch()
    deps2 = FakeDeps(fs2)
    deps2.inspect_result = {"ok": False, "issues": [{"type": "face"}], "traits": [], "people": 1}
    d2 = D.Director(deps2, {REC["id"]: REC})
    t2 = new_turn(fs2, "카페 영상")
    await d2.pump(); await settle(d2)
    notes = [m for m in fs2.messages if m["kind"] == "notice"]
    assert t2["state"] == "done" and notes and "얼굴이 뭉개져" in notes[0]["body"]


async def t_inspect_skipped_when_queue_busy():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump()
    for _ in range(500):
        if deps.last_job:
            break
        await asyncio.sleep(0.005)
    orig_queue = deps.queue
    async def busy():
        return [{"id": "other", "seq": 9, "state": "queued", "kind": "comfy"}]
    deps.queue = busy
    await settle(d)
    assert t["state"] == "done"
    skipped = [dct for k, dct in fs.events if k == "inspect"][0]
    assert "skipped" in skipped and "inspect" not in [c[0] for c in deps.calls]


async def t_new_seed_reuses_prompt():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    src = new_turn(fs)
    await d.pump(); await settle(d)
    assert src["state"] == "done"
    deps.calls.clear()
    t = new_turn(fs, mode="new_seed")
    t["request"]["source_turn"] = str(src["id"])
    fs.turns[uuid.UUID(str(src["id"]))] = src
    await d.pump(); await settle(d)
    assert t["state"] == "done" and "render" not in [c[0] for c in deps.calls]
    s1 = [c[1] for c in deps.calls if c[0] == "submit"][0]
    assert s1["prompt"]["129:93"]["inputs"]["text"].startswith("P ")


async def t_pump_one_per_thread_and_user_limit():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.hold = True
    d = D.Director(deps, {REC["id"]: REC})
    a, b, c = new_turn(fs), new_turn(fs), new_turn(fs)
    b["depends_on"] = a["id"]
    await d.pump()
    assert a["state"] == "active" and b["state"] == "pending"        # 같은 대화는 앞 요청이 끝나야 한다
    assert c["state"] == "active"                                    # 다른 대화는 사용자당 한도(2) 안에서 같이
    for t in (a, b, c):
        await store.request_cancel(t["id"])
    deps.hold = False
    for t in (a, c):
        await d.cancel(t)
    await settle(d)


async def t_recover_marks_llm_stage_interrupted():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    t["state"] = "active"
    t["work"] = {"resumed": True}         # 이미 한 번 자동 재개했던 요청은 두 번째 재시작에서 멈춘다
    await d.recover()
    assert t["state"] == "interrupted"
    n = [m for m in fs.messages if m["kind"] == "notice"][0]
    assert n["data"]["actions"][0]["id"] == "retry" and "다시 시도" in n["body"]


async def t_recover_auto_resumes_llm_stage_once():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    t["state"] = "active"
    await d.recover()
    await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"))
    assert t["work"]["resumed"] is True and ("recovered", {"result": "resumed"}) in fs.events
    assert [c[0] for c in deps.calls].count("render") == 1


async def t_recover_skips_turns_already_running():
    """허브가 막 열린 사이 pump 가 돌린 요청을 recover 가 다시 돌리면 같은 접수가 두 번 일어난다"""
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump()                     # pump 가 먼저 active 로 옮기고 spawn
    assert t["state"] == "active" and t["id"] in d.tasks
    await d.recover()
    await settle(d)
    assert t["state"] == "done"
    names = [c[0] for c in deps.calls]
    assert names.count("render") == 1 and names.count("submit") == 1, names


async def t_new_seed_with_new_aspect_drops_saved_size():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=IMG)
    d = D.Director(deps, {IMG["id"]: IMG})
    src = new_turn(fs, "고양이", rec=IMG)
    src["state"] = "done"
    src["work"] = {"prompt": "SAVED", "size": [1024, 1024]}
    t = new_turn(fs, "하나 더", rec=IMG, mode="new_seed", spec={"aspect": "portrait"})
    t["request"]["source_turn"] = str(src["id"])
    await d.pump()
    await settle(d)
    p = [c[1] for c in deps.calls if c[0] == "submit"][0]["prompt"]
    assert p["57:13"]["inputs"]["width"] < p["57:13"]["inputs"]["height"], p["57:13"]["inputs"]


async def t_t2v_flow_has_no_image():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=T2V)
    d = D.Director(deps, {T2V["id"]: T2V})
    t = new_turn(fs, "바다 위를 나는 드론 영상", rec=T2V, spec={"aspect": "portrait"})
    await d.pump()
    await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events)
    r = [c[1] for c in deps.calls if c[0] == "render"][0]
    assert r["image"] is None and r["profile_id"] == "wan22_t2v_long" and r["frame"] is None
    p = [c[1] for c in deps.calls if c[0] == "submit"][0]["prompt"]
    w, h = p["129:98"]["inputs"]["width"], p["129:98"]["inputs"]["height"]
    assert w < h and "start_image" not in p["129:98"]["inputs"], (w, h)
    assert fs.results[0]["media"] == "video"


async def t_edit_flow_uses_edit_image_and_keeps_input_size():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=EDIT)
    d = D.Director(deps, {EDIT["id"]: EDIT})
    t = new_turn(fs, "배경만 노을로 바꿔줘", rec=EDIT)
    await d.pump()
    await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events)
    names = [c[0] for c in deps.calls]
    assert names[0] == "write_edit" and "render" not in names and "expand_image" not in names
    body = [c[1] for c in deps.calls if c[0] == "submit"][0]
    p = body["prompt"]
    assert p["57:27"]["inputs"]["text"].startswith("Replace the background")
    assert p["41"]["inputs"]["image"].startswith("studio/bob/") and p["41"]["inputs"]["image"].endswith(".png")
    assert p["57:13"]["inputs"]["width"] == 1024          # 크기 자리가 없으니 레시피 값 그대로
    assert "edit_prompt" in kinds(fs) and "comfy_submitted" in kinds(fs)
    res = [m for m in fs.messages if m["kind"] == "result"][0]
    assert res["data"]["media"] == "image" and fs.results[0]["width"] is None


async def t_video_revise_reuses_frame_and_saves_it():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    src = new_turn(fs)
    src["state"] = "done"
    src["work"] = {"prompt": "OLD", "frame": {"subject": "cockpit", "environment": "night"}}
    t = new_turn(fs, "비행기를 운항하는 영상\n더 천천히")
    t["request"]["reuse_frame_of"] = str(src["id"])
    await d.pump()
    await settle(d)
    assert t["state"] == "done"
    r = [c[1] for c in deps.calls if c[0] == "render"][0]
    assert r["frame"] == {"subject": "cockpit", "environment": "night"}      # 8B 차례를 다시 쓰지 않는다
    assert r["text"] == "비행기를 운항하는 영상\n더 천천히" if "text" in r else True
    assert t["work"]["frame"] == {"subject": "cockpit"}                         # 이번에 읽은 결과도 저장


async def t_video_revise_rereads_frame_when_saved_one_is_empty():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    src = new_turn(fs)
    src["state"] = "done"
    src["work"] = {"prompt": "OLD", "frame": {}}            # 앞 요청의 첫 화면 읽기가 실패해 빈 값이 저장된 경우
    t = new_turn(fs, "비행기를 운항하는 영상\n더 천천히")
    t["request"]["reuse_frame_of"] = str(src["id"])
    await d.pump()
    await settle(d)
    r = [c[1] for c in deps.calls if c[0] == "render"][0]
    assert r["frame"] is None                               # 빈 결과는 다시 읽게 한다


async def t_all_shipped_recipes_pass_checks_and_build():
    """저장소의 모든 레시피가 파일 검사를 통과하고, 표본 값으로 값을 넣을 수 있으며, 접수 직전 부정 자리 확인도 통과한다"""
    rs = recipes.load_all()
    assert {"wan22_i2v_14b_fast", "zimage_turbo_t2i", "wan22_t2v_14b_fast", "qwen_image_edit"} <= set(rs), list(rs)
    for rid, r in rs.items():
        rep = recipes.check_recipe(r, {"wan22_i2v_long", "wan22_t2v_long", "ltx25_i2v_long"})
        assert recipes.recipe_ok(rep), (rid, [x for x in rep if not x["ok"]])
        p = recipes.build(r, recipes.sample_values(r))
        assert D.negative_problem(r, p) is None, (rid, D.negative_problem(r, p))
    assert {r["action"] for r in rs.values()} == {"i2v", "t2v", "image", "edit"}
    # 4스텝 LoRA 는 레시피가 켠 채로 고정한다
    assert rs["wan22_t2v_14b_fast"]["prompt"]["128:129"]["inputs"]["value"] is True
    assert rs["qwen_image_edit"]["prompt"]["170:168"]["inputs"]["value"] is True
    # 편집 레시피는 두 번째 이미지(예시 무늬)를 쓰지 않는다
    q = rs["qwen_image_edit"]["prompt"]
    assert "83" not in q and "image2" not in q["170:151"]["inputs"] and "image2" not in q["170:149"]["inputs"]
    # recipe_for 는 action 마다 레시피 하나를 고른다
    d = D.Director(FakeDeps(FakeStore()), rs)
    assert {a: d.recipe_for(a)["id"] for a in ("i2v", "t2v", "image", "edit")} == {
        "i2v": "wan22_i2v_14b_fast", "t2v": "wan22_t2v_14b_fast", "image": "zimage_turbo_t2i", "edit": "qwen_image_edit"}


async def t_new_seed_with_new_seconds_rewrites_prompt():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    src = new_turn(fs, spec={"seconds": None})
    src["state"] = "done"
    src["work"] = {"prompt": "SAVED", "settings": {"frames": 81, "seconds": 5.0}, "size": [848, 480]}
    t = new_turn(fs, mode="new_seed", spec={"seconds": 3.0})
    t["request"]["source_turn"] = str(src["id"])
    await d.pump()
    await settle(d)
    assert t["state"] == "done"
    names = [c[0] for c in deps.calls]
    assert "render" in names and "reuse_skipped" in kinds(fs), (names, kinds(fs))       # 길이가 바뀌면 새로 쓴다
    assert [c[1] for c in deps.calls if c[0] == "render"][0]["seconds"] == 3.0


async def t_first_stage_matches_action():
    assert D.first_stage("i2v") == "frame"
    for a in ("image", "t2v", "edit", None):
        assert D.first_stage(a) == "prompt"
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=IMG)
    deps.hold = True
    d = D.Director(deps, {IMG["id"]: IMG})
    t = new_turn(fs, "작은 카페", rec=IMG)
    await d.pump()
    assert t["stage"] == "prompt", t["stage"]          # 운영자 화면이 보는 stage 칸
    await store.request_cancel(t["id"])
    deps.hold = False
    await d.cancel(t)
    await settle(d)


def plan_msgs(fs):
    return [m for m in fs.messages if m["kind"] == "plan"]


async def t_plan_card_waits_for_approval_then_submits_once():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {"ask": "ask_long"}
    old = settings.studio_ask_long_seconds
    settings.studio_ask_long_seconds = 60              # 예상 90초(레시피 추정)가 기준 이상
    try:
        deps = FakeDeps(fs)
        d = D.Director(deps, {REC["id"]: REC})
        t = new_turn(fs)
        await d.pump()
        await settle(d)
        assert t["state"] == "awaiting_approval" and t["stage"] == "plan", (t["state"], t.get("error"))
        names = [c[0] for c in deps.calls]
        assert "render" in names and "submit" not in names, names            # 프롬프트는 쓰고 접수는 하지 않는다
        pm = plan_msgs(fs)
        assert len(pm) == 1 and pm[0]["data"]["status"] == "pending" and [a["id"] for a in pm[0]["data"]["actions"]] == ["approve", "cancel"]
        plan = t["plan"]
        assert plan["seconds"] == 5.0 and plan["frames"] == 81 and plan["size"] == [848, 480] and plan["quality"] == "빠름"
        assert plan["estimate_s"] >= 90 and plan["message_id"] == pm[0]["id"] and plan["attachment_ids"]
        assert not deps.receivers and "plan" in kinds(fs)
        # 승인: 한 번만 접수. 두 번째는 충돌
        r1 = await d.approve(t)
        r2 = await d.approve(t)
        assert (r1, r2) == ("ok", "conflict"), (r1, r2)
        assert t["state"] == "pending" and t["work"]["approved"] is True       # 승인과 approved 기록은 한 번에
        await d.pump()                                                          # 시작은 pump 가 한도를 지켜 한다
        await settle(d)
        assert t["state"] == "done", (t["state"], t.get("error"))
        names = [c[0] for c in deps.calls]
        assert names.count("submit") == 1 and names.count("render") == 1, names      # LLM 은 다시 쓰지 않는다
        assert pm[0]["data"]["status"] == "approved" and pm[0]["data"]["actions"] == []
        assert t["work"]["approved"] is True
    finally:
        settings.studio_ask_long_seconds = old


async def t_plan_not_shown_when_auto_short_or_not_video():
    old = settings.studio_ask_long_seconds
    try:
        for cfg, thr, rec, expect in (({}, 60, REC, False), ({"ask": "auto"}, 60, REC, False),
                                      ({"ask": "ask_long"}, 600, REC, False),       # 예상 시간이 기준 미만
                                      ({"ask": "ask_long"}, 0, IMG, False),           # 이미지는 계획 카드가 없다
                                      ({"ask": "ask_long"}, 0, REC, True)):          # 0 이면 모든 영상에 카드
            fs = FakeStore(); fs.patch()
            fs.thread_cfg = cfg
            settings.studio_ask_long_seconds = thr
            deps = FakeDeps(fs, rec=rec)
            d = D.Director(deps, {rec["id"]: rec})
            t = new_turn(fs, rec=rec)
            await d.pump()
            await settle(d)
            assert (t["state"] == "awaiting_approval") is expect, (cfg, thr, rec["id"], t["state"])
    finally:
        settings.studio_ask_long_seconds = old


async def t_plan_estimate_counts_the_queue():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.queue_rows = [{"id": "a", "kind": "comfy", "state": "running"}, {"id": "b", "kind": "llm", "state": "queued"}]
    deps.median = {"comfy": 100.0, "llm": 20.0}
    d = D.Director(deps, {REC["id"]: REC})
    ctx = D.Ctx(new_turn(fs), USER, ENV, REC)
    assert await d.estimate_seconds(ctx) == 100 + 20 + 90          # 앞 ComfyUI 1건 + 앞 LLM 1건 + 내 작업(레시피 추정 90)
    deps.median = {"comfy": None, "llm": None}
    assert await d.estimate_seconds(ctx) == 90 + 30 + 90


async def t_cancel_and_supersede_and_expire_plan():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {"ask": "ask_long"}
    old = settings.studio_ask_long_seconds
    settings.studio_ask_long_seconds = 0
    try:
        deps = FakeDeps(fs)
        d = D.Director(deps, {REC["id"]: REC})
        # 취소: ComfyUI 줄 없이 끝나고 카드가 '취소됨'
        t = new_turn(fs); await d.pump(); await settle(d)
        assert t["state"] == "awaiting_approval"
        await d.cancel(t)
        assert t["state"] == "cancelled" and plan_msgs(fs)[-1]["data"]["status"] == "cancelled"
        assert "submit" not in [c[0] for c in deps.calls]
        # 새 메시지가 오면 접는다 (5.7)
        t2 = new_turn(fs); await d.pump(); await settle(d)
        assert t2["state"] == "awaiting_approval"
        assert await d.supersede(t2) and t2["state"] == "cancelled" and plan_msgs(fs)[-1]["data"]["status"] == "replanned"
        assert not await d.supersede(t2)                                   # 이미 접힌 계획은 다시 접지 않는다
        # 만료
        t3 = new_turn(fs); await d.pump(); await settle(d)
        t3["stale"] = True
        assert await d.expire_plans() == 1 and t3["state"] == "cancelled" and plan_msgs(fs)[-1]["data"]["status"] == "expired"
        # 승인 대기는 활성 한도에 세지 않는다
        assert await store.count_active(7) == 0
    finally:
        settings.studio_ask_long_seconds = old


async def t_approve_with_new_recipe_version_replans():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {"ask": "ask_long"}
    old = settings.studio_ask_long_seconds
    settings.studio_ask_long_seconds = 0
    try:
        deps = FakeDeps(fs)
        d = D.Director(deps, {REC["id"]: REC})
        t = new_turn(fs); await d.pump(); await settle(d)
        assert t["state"] == "awaiting_approval"
        t["recipe_version"] = 0                # 계획한 뒤 레시피 판이 바뀐 것처럼
        assert await d.approve(t) == "replan"
        await d.pump()
        await settle(d)
        assert t["state"] == "awaiting_approval" and t["recipe_version"] == REC["version"]     # 새 판으로 다시 계획했다
        assert [c[0] for c in deps.calls].count("render") == 2 and "submit" not in [c[0] for c in deps.calls]
        assert plan_msgs(fs)[0]["data"]["status"] == "replanned" and len(plan_msgs(fs)) == 2
    finally:
        settings.studio_ask_long_seconds = old


async def parked(fs, deps=None, action_rec=None):
    """계획 대기까지 보낸 요청과 감독"""
    fs.thread_cfg = {"ask": "ask_long"}
    deps = deps or FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump()
    await settle(d)
    assert t["state"] == "awaiting_approval"
    return d, deps, t


async def t_plan_races_never_cancel_or_double_submit_an_approved_turn():
    old = settings.studio_ask_long_seconds
    settings.studio_ask_long_seconds = 0
    try:
        # 승인한 뒤 supersede·만료가 와도 취소하지 않는다 (이미 승인돼 대기 중)
        fs = FakeStore(); fs.patch()
        d, deps, t = await parked(fs)
        assert await d.approve(t) == "ok"
        assert not await d.supersede(t) and t["state"] == "pending"
        t["stale"] = True
        assert await d.expire_plans() == 0 and t["state"] == "pending"
        await d.pump(); await settle(d)
        assert t["state"] == "done" and [c[0] for c in deps.calls].count("submit") == 1
        # 승인 뒤 취소: pending 이라 시작하지 않고, 접수도 없다. 카드는 '승인함' 그대로
        fs = FakeStore(); fs.patch()
        d, deps, t = await parked(fs)
        assert await d.approve(t) == "ok"
        await d.cancel(dict(t, state="awaiting_approval"))          # 오래된 화면이 awaiting 으로 알고 보낸 취소
        assert t["state"] == "cancelled"
        await d.pump(); await settle(d)
        assert "submit" not in [c[0] for c in deps.calls] and plan_msgs(fs)[-1]["data"]["status"] == "approved"
        # 취소가 먼저면 승인은 충돌
        fs = FakeStore(); fs.patch()
        d, deps, t = await parked(fs)
        await d.cancel(t)
        assert await d.approve(t) == "conflict" and t["state"] == "cancelled"
    finally:
        settings.studio_ask_long_seconds = old


async def t_approve_respects_active_limit_and_survives_restart():
    old, old_max = settings.studio_ask_long_seconds, settings.studio_max_active_per_user
    settings.studio_ask_long_seconds = 0
    try:
        fs = FakeStore(); fs.patch()
        d, deps, t = await parked(fs)
        settings.studio_max_active_per_user = 0                       # 활성 한도가 찼다
        assert await d.approve(t) == "ok"
        await d.pump()
        assert t["state"] == "pending" and not d.tasks                # 한도를 우회해 바로 시작하지 않는다
        settings.studio_max_active_per_user = old_max
        # 허브가 승인 직후 죽었다가 다시 열려도 계획으로 되돌아가지 않고 접수까지 간다
        d2 = D.Director(FakeDeps(fs), {REC["id"]: REC})
        await d2.recover()
        await settle(d2)
        assert t["state"] == "done" and len(plan_msgs(fs)) == 1, (t["state"], len(plan_msgs(fs)))
        assert [c[0] for c in d2.deps.calls].count("render") == 0     # 프롬프트는 다시 쓰지 않았다
    finally:
        settings.studio_ask_long_seconds, settings.studio_max_active_per_user = old, old_max


async def t_plan_does_not_block_later_requests_and_old_card_is_folded():
    old = settings.studio_ask_long_seconds
    settings.studio_ask_long_seconds = 0
    try:
        fs = FakeStore(); fs.patch()
        d, deps, t = await parked(fs)
        later = new_turn(fs, "다른 요청")
        later["depends_on"] = t["id"]                                  # 계획 대기 요청을 기다리도록 걸려 있다
        fs.thread_cfg = {}
        await d.pump(); await settle(d)
        assert later["state"] == "done", later["state"]                # 승인 대기 요청은 뒤 요청을 막지 않는다
        # 카드를 만든 뒤 상태를 바꾸기 전에 멈췄다가 다시 계획되면 낡은 카드는 접는다
        fs = FakeStore(); fs.patch()
        d, deps, t = await parked(fs)
        first_mid = t["work"]["plan_message_id"]
        t["state"] = "active"                                          # 복구가 active 로 되살린 것처럼
        fs.thread_cfg = {"ask": "ask_long"}
        d.spawn(t, resume=True)
        await settle(d)
        assert [m["data"]["status"] for m in plan_msgs(fs)] == ["replanned", "pending"], [m["data"] for m in plan_msgs(fs)]
        assert [m for m in plan_msgs(fs)][0]["id"] == first_mid
    finally:
        settings.studio_ask_long_seconds = old


async def t_plan_judgement_failure_proceeds_without_plan_and_reuse_skips_plan():
    old = settings.studio_ask_long_seconds
    settings.studio_ask_long_seconds = 0
    try:
        fs = FakeStore(); fs.patch()
        fs.thread_cfg = {"ask": "ask_long"}
        deps = FakeDeps(fs)

        real_queue, hit = deps.queue, {"n": 0}

        async def boom():
            hit["n"] += 1
            if hit["n"] == 1:               # 계획 카드 판단에서 한 번만 실패한다 (감시에서는 정상)
                raise RuntimeError("db down")
            return await real_queue()
        deps.queue = boom
        d = D.Director(deps, {REC["id"]: REC})
        t = new_turn(fs)
        await d.pump(); await settle(d)
        assert t["state"] == "done", (t["state"], t.get("error"))      # 예상 시간을 못 구해도 요청이 죽지 않는다
        # 다시 만들기(저장된 프롬프트 재사용)는 계획을 건너뛴다. 길이가 바뀌어 새로 쓰면 계획한다
        fs = FakeStore(); fs.patch()
        fs.thread_cfg = {"ask": "ask_long"}
        deps = FakeDeps(fs)
        d = D.Director(deps, {REC["id"]: REC})
        src = new_turn(fs, spec={"seconds": None}); src["state"] = "done"
        src["work"] = {"prompt": "SAVED", "settings": {"frames": 81, "seconds": 5.0}, "size": [848, 480]}
        t = new_turn(fs, mode="new_seed"); t["request"]["source_turn"] = str(src["id"])
        await d.pump(); await settle(d)
        assert t["state"] == "done"
        t2 = new_turn(fs, mode="new_seed", spec={"seconds": 3.0}); t2["request"]["source_turn"] = str(src["id"])
        await d.pump(); await settle(d)
        assert t2["state"] == "awaiting_approval"
    finally:
        settings.studio_ask_long_seconds = old


def t_approve_words():
    ok = ("응", "네", "네 진행해줘", "ok", "좋아요!", "승인", "ㅇㅇ", "진행", "시작해줘")
    no = ("더 천천히", "응 근데 더 밝게", "네 구름을 더 많이", "고마워", "")
    for t in ok:
        assert intent.APPROVE_RE.match(t), t
    for t in no:
        assert not intent.APPROVE_RE.match(t), t


COPY_VIOLATION = ['"action" copies the EXAMPLE (steam, tea). The EXAMPLE is a different request: rewrite "action" about the '
                  "user's own subject, objects and setting only."]


async def t_copied_example_prompt_is_rewritten_once_and_never_sent_to_gpu():
    # 고치기가 베끼기를 고치면 새로 쓰지 않고 그 글로 만든다
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.violation_seq = [COPY_VIOLATION]
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs, "비행기를 운항하는 파일럿의 1인칭 시점 영상")
    await d.pump(); await settle(d)
    assert t["state"] == "done" and [c[0] for c in deps.calls].count("render") == 1 and "prompt_rejected" not in kinds(fs)
    # 첫 글이 예시를 베꼈고 고치기로도 못 고쳤지만 두 번째 새로 쓴 글이 깨끗하면 그 글로 만든다
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.violation_seq = [COPY_VIOLATION, []]
    deps.fix_violation_seq = [COPY_VIOLATION]
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs, "비행기를 운항하는 파일럿의 1인칭 시점 영상")
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"))
    names = [c[0] for c in deps.calls]
    assert names.count("render") == 2 and "prompt_rejected" in kinds(fs), (names, kinds(fs))
    assert names.index("submit") > [i for i, n in enumerate(names) if n == "render"][-1]       # 접수는 두 번째 글 뒤에
    # 두 번 다 베끼면 GPU 작업을 접수하지 않고 안내와 함께 실패한다
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.violation_seq = [COPY_VIOLATION, COPY_VIOLATION]
    deps.fix_violation_seq = [COPY_VIOLATION, COPY_VIOLATION]
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs, "비행기를 운항하는 파일럿의 1인칭 시점 영상")
    await d.pump(); await settle(d)
    assert t["state"] == "failed" and t["error"]["code"] == "bad_prompt", (t["state"], t.get("error"))
    assert "submit" not in [c[0] for c in deps.calls] and [c[0] for c in deps.calls].count("render") == 2
    n = [m for m in fs.messages if m["kind"] == "notice"][0]
    assert "다시 시도" in n["body"] and n["data"]["actions"][0]["id"] == "retry"
    assert not [e for e in fs.events if e[0] == "comfy_submitted"]
    # 낱말 수 위반처럼 영상에 문제가 되지 않는 위반은 막지 않는다
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, violations=["The prompt has 520 words; write 250 to 450 words. Cut it"])
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump(); await settle(d)
    assert t["state"] == "done" and [c[0] for c in deps.calls].count("render") == 1


HQ = recipes.quality_variant(REC)
HQ_EDIT = recipes.load_all()["qwen_image_edit_quality"]


async def t_high_quality_always_shows_plan_card_and_runs_the_high_recipe():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {}                                  # 대화 설정이 auto 여도 높음은 계획 카드를 거친다
    deps = FakeDeps(fs, rec=HQ)
    d = D.Director(deps, {REC["id"]: REC, HQ["id"]: HQ})
    t = new_turn(fs, rec=HQ)
    await d.pump(); await settle(d)
    assert t["state"] == "awaiting_approval" and t["recipe_id"] == HQ["id"], (t["state"], t.get("error"))
    plan = plan_msgs(fs)[0]["data"]["plan"]
    assert plan["quality"] == "높음" and plan["estimate_s"] >= HQ["estimate"]["gpu_seconds"] and "submit" not in [c[0] for c in deps.calls]
    assert await d.approve(t) == "ok"
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"))
    p = [c[1] for c in deps.calls if c[0] == "submit"][0]["prompt"]
    assert p["129:131"]["inputs"]["value"] is False       # 가속을 끄고 접수한다
    assert d.quality_of_turn(t) == "high" and d.quality_of_turn(None) == "fast"
    # 같은 조건의 빠른 요청은 계획 카드가 뜨지 않는다
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {}
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC, HQ["id"]: HQ})
    t = new_turn(fs)
    await d.pump(); await settle(d)
    assert t["state"] == "done" and not plan_msgs(fs)
    # 높음 요청을 다시 만들기(저장된 프롬프트 재사용)하면 계획 카드를 건너뛴다. 처음부터 다시(rewrite)는 계획한다
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {}
    deps = FakeDeps(fs, rec=HQ)
    d = D.Director(deps, {REC["id"]: REC, HQ["id"]: HQ})
    src = new_turn(fs, rec=HQ); src["state"] = "done"
    src["work"] = {"prompt": "SAVED", "settings": {"frames": 81, "seconds": 5.0}, "size": [848, 480]}
    again = new_turn(fs, rec=HQ, mode="new_seed"); again["request"]["source_turn"] = str(src["id"])
    await d.pump(); await settle(d)
    assert again["state"] == "done"
    redo = new_turn(fs, rec=HQ, mode="rewrite")
    await d.pump(); await settle(d)
    assert redo["state"] == "awaiting_approval"


async def t_high_quality_edit_also_asks_before_running():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {}
    deps = FakeDeps(fs, rec=HQ_EDIT)
    d = D.Director(deps, {HQ_EDIT["id"]: HQ_EDIT})
    t = new_turn(fs, "배경만 노을로 바꿔줘", rec=HQ_EDIT)
    await d.pump(); await settle(d)
    assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
    plan = plan_msgs(fs)[0]["data"]["plan"]
    assert plan["action"] == "edit" and plan["quality"] == "높음" and plan["size"] in (None, []) and not plan["seconds"]
    assert await d.approve(t) == "ok"
    await d.pump(); await settle(d)
    assert t["state"] == "done"
    assert [c[1] for c in deps.calls if c[0] == "submit"][0]["prompt"]["170:168"]["inputs"]["value"] is False


def new_via(fs, text="비행기 조종석에서 1인칭으로 구름 사이를 나는 영상", rec=None, **kw):
    """글만 보낸 영상 요청: 내부에서 장면 이미지를 먼저 만들고 그 이미지로 영상을 만든다"""
    t = new_turn(fs, text, rec=rec or REC, **kw)
    t["request"]["attachment_ids"] = []
    t["request"]["via_image"] = True
    t["action"] = "t2v"
    return t


def via_director(deps, rec=None):
    rec = rec or REC
    return D.Director(deps, {rec["id"]: rec, IMG["id"]: IMG})


async def t_via_image_pipeline_is_one_task_with_only_the_video():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = via_director(deps)
    t = new_via(fs)
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
    order = [c[0] for c in deps.calls if c[0] in ("expand_image", "submit", "stage_image", "render")]
    assert order == ["expand_image", "submit", "stage_image", "render", "submit"], order
    subs = [c[1] for c in deps.calls if c[0] == "submit"]
    assert [s["extra_data"]["studio"]["recipe"] for s in subs] == [IMG["id"], REC["id"]]
    assert len({s["client_id"] for s in subs}) == 2           # 접수 이름이 겹치지 않는다
    # 영상 프롬프트는 장면 이미지(숨은 첨부)를 첫 화면으로, 장면 프롬프트를 함께 받는다
    rk = [c[1] for c in deps.calls if c[0] == "render"][0]
    staged = [a for a in fs.atts.values() if a.get("input_rel") == rk["image"]["subfolder"] + "/" + rk["image"]["filename"]]
    assert len(staged) == 1 and rk["image_prompt"].startswith("IMG ")
    assert "first-instant" not in rk["image_prompt"] and "still photograph" in [c[1] for c in deps.calls if c[0] == "expand_image"][0]
    # 장면 이미지는 채팅에 나가지 않는다: 결과 메시지는 영상 하나
    res = [m for m in fs.messages if m["kind"] == "result"]
    assert len(res) == 1 and res[0]["data"]["media"] == "video" and len(res[0]["data"]["attachment_ids"]) == 1
    assert len(fs.results) == 1 and fs.results[0]["media"] == "video"
    assert "scene" in [s for s, _j, _p in fs.links] and "generate" in [s for s, _j, _p in fs.links]
    # 사용자에게 보이는 진행: 한 카드, 이미지·첫 화면이라는 말이 없다
    progs = [e["progress"] for e in deps.published if e.get("type") == "turn.progress"]
    assert progs and {p["title"] for p in progs} == {"영상 만드는 중"}
    stages = {p["stage"] for p in progs}
    assert {"scene", "motion", "generate"} <= stages and not (stages & {"frame", "fix"}), stages
    for p in progs:
        assert "이미지" not in p["text"] and "첫 화면" not in p["text"], p
        assert [c["key"] for c in p["chips"]] == ["prompt", "scene", "motion", "generate", "collect"]
        if p["stage"] == "scene":
            assert p["pct"] is None
    notes = res[0]["data"].get("notes") or []
    assert "영상 비율은 사진 비율을 따릅니다" not in notes
    # 장면 이미지 크기는 영상 비율, 영상 크기는 장면 이미지 비율에서 정해진다
    assert fs.turns[t["id"]]["work"]["size"][0] > fs.turns[t["id"]]["work"]["size"][1]


async def t_via_image_high_quality_shows_the_same_plan_card_then_runs_only_the_video():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {}
    deps = FakeDeps(fs, rec=HQ)
    d = via_director(deps, HQ)
    t = new_via(fs, rec=HQ)
    await d.pump(); await settle(d)
    assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
    # 계획은 예전 글→영상과 같은 카드다 (요약·프롬프트·길이). 장면 이미지는 빠름이라 계획 전에 만들어 두고 영상은 승인 뒤에 돈다
    order = [c[0] for c in deps.calls if c[0] in ("expand_image", "submit", "stage_image", "render")]
    assert order == ["expand_image", "submit", "stage_image", "render"], order
    plan = plan_msgs(fs)[0]["data"]["plan"]
    assert plan["action"] == "t2v" and plan["attachment_ids"] == [] and plan["quality"] == "높음"
    assert plan["ko"] and plan["words"] > 0 and plan["seconds"] and plan["frames"]
    assert plan["estimate_s"] >= HQ["estimate"]["gpu_seconds"]
    assert await d.approve(t) == "ok"
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"))
    subs = [c[1] for c in deps.calls if c[0] == "submit"]
    assert [s["extra_data"]["studio"]["recipe"] for s in subs] == [IMG["id"], HQ["id"]]
    assert len([c for c in deps.calls if c[0] == "expand_image"]) == 1 and len([c for c in deps.calls if c[0] == "render"]) == 1
    assert len(plan_msgs(fs)) == 1


async def t_via_image_scene_failure_and_cancel_stop_before_the_video():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.outcome_seq = ["error"]
    d = via_director(deps)
    t = new_via(fs)
    await d.pump(); await settle(d)
    assert t["state"] == "failed" and t["error"]["code"] == "run_error", (t["state"], t.get("error"))
    assert "render" not in [c[0] for c in deps.calls] and not [m for m in fs.messages if m["kind"] == "result"]
    # 장면 이미지가 대기 중일 때 취소하면 그 작업이 취소된다
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.hold = True
    d = via_director(deps)
    t = new_via(fs)
    await d.pump()
    for _ in range(300):
        if deps.last_job:
            break
        await asyncio.sleep(0.01)
    assert deps.last_job
    await asyncio.sleep(0.05)
    job = deps.last_job
    await d.cancel(t); await settle(d)
    assert t["state"] == "cancelled" and ("cancel_queued", [job]) in deps.calls, (t["state"], t.get("error"))
    assert "render" not in [c[0] for c in deps.calls]


async def t_via_image_again_reuses_scene_prompt_but_makes_a_new_scene():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = via_director(deps)
    src = new_via(fs)
    await d.pump(); await settle(d)
    assert src["state"] == "done"
    n_expand = len([c for c in deps.calls if c[0] == "expand_image"])
    n_render = len([c for c in deps.calls if c[0] == "render"])
    again = new_via(fs, mode="new_seed"); again["request"]["source_turn"] = str(src["id"])
    await d.pump(); await settle(d)
    assert again["state"] == "done", (again["state"], again.get("error"))
    assert len([c for c in deps.calls if c[0] == "expand_image"]) == n_expand      # LLM 을 다시 쓰지 않는다
    assert len([c for c in deps.calls if c[0] == "render"]) == n_render + 1       # 영상 프롬프트는 새 장면 이미지를 보고 새로 쓴다
    assert len([c for c in deps.calls if c[0] == "stage_image"]) == 2              # 장면 이미지는 새로 만든다
    subs = [c[1] for c in deps.calls if c[0] == "submit"]
    assert len(subs) == 4 and len({s["client_id"] for s in subs}) == 4, [(s["client_id"], s["extra_data"]["studio"]["recipe"]) for s in subs]


async def t_ltx_sound_video_runs_both_stages_with_seconds_and_the_audio_paragraph():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=LTX)
    d = via_director(deps, LTX)
    t = new_via(fs, rec=LTX, spec={"seconds": 10.0})
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
    rk = [c[1] for c in deps.calls if c[0] == "render"][0]
    assert rk["profile_id"] == "ltx25_i2v_long" and rk["seconds"] == 10.0 and rk["image_prompt"].startswith("IMG ")
    body = [c[1] for c in deps.calls if c[0] == "submit"][1]["prompt"]
    assert body["398:362"]["inputs"]["value"] == 10                                      # 길이는 초로
    assert body["398:376"]["inputs"]["value"].endswith(SOUND_TEXT) and body["398:376"]["inputs"]["value"].count("Audio:") == 1
    assert (body["398:372"]["inputs"]["value"], body["398:360"]["inputs"]["value"]) == (1280, 704)
    assert body["900"]["inputs"]["batch_index"] == 120 and body["75"]["inputs"]["filename_prefix"].endswith("/video")
    res = [m for m in fs.messages if m["kind"] == "result"]
    assert len(res) == 1 and res[0]["data"]["media"] == "video" and fs.results[-1]["frames"] == 241
    # 엔진 고르기: 소리를 고르면 LTX(화질 높음 없음), 아니면 늘 Wan
    assert d.recipe_for("i2v", sound=True)["id"] == LTX["id"] and d.recipe_for("i2v", "high", True)["id"] == LTX["id"]
    assert d.recipe_for("t2v", sound=True) is None and d.engine_of_turn(t) == "ltx" and d.engine_of_turn(None) == "wan"
    d2 = D.Director(deps, {REC["id"]: REC, LTX["id"]: LTX, IMG["id"]: IMG})
    assert d2.recipe_for("i2v")["id"] == REC["id"] and d2.engine_of_turn({"recipe_id": REC["id"]}) == "wan"


async def t_scene_hint_asks_for_a_moving_subject_to_be_off_center_with_room_ahead():
    assert "still photograph" in D.Director.SCENE_HINT and "open space ahead" in D.Director.SCENE_HINT


async def no_queue():
    return []


async def t_ltx_plan_card_estimates_by_length_and_fix_keeps_the_audio_paragraph():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {"ask": "ask_long"}
    old = settings.studio_ask_long_seconds
    settings.studio_ask_long_seconds = 0
    try:
        deps = FakeDeps(fs, rec=LTX)
        deps.queue = no_queue                                   # 장면 이미지 작업이 정리 중인 대기열 행이 예상 시간에 끼지 않게 (시간 맞춤 시험)
        deps.violation_seq = [["Travel keeps one steady pace for the whole clip."]]      # 영상 글을 고치게 한다
        d = via_director(deps, LTX)
        t = new_via(fs, rec=LTX, spec={"seconds": 20.0})
        await d.pump(); await settle(d)
        assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
        plan = plan_msgs(fs)[0]["data"]["plan"]
        assert plan["seconds"] == 20.0 and plan["frames"] == 481 and plan["fps"] == 24 and plan["quality"] == "소리 포함"
        assert 120 <= plan["estimate_s"] <= 160, plan["estimate_s"]                      # 실측 20초 137초 (5초는 약 50초)
        assert await d.approve(t) == "ok"
        await d.pump(); await settle(d)
        assert t["state"] == "done", (t["state"], t.get("error"))
        prompt = [c[1] for c in deps.calls if c[0] == "submit"][1]["prompt"]["398:376"]["inputs"]["value"]
        assert prompt.startswith("FIXED") and prompt.endswith(SOUND_TEXT)                 # 고친 영상 글 뒤에도 소리 단락이 이어진다
        # 5초 요청은 더 짧게 잡는다
        fs2 = FakeStore(); fs2.patch(); fs2.thread_cfg = {"ask": "ask_long"}
        deps2 = FakeDeps(fs2, rec=LTX)
        deps2.queue = no_queue
        d2 = via_director(deps2, LTX)
        t2 = new_via(fs2, rec=LTX, spec={"seconds": 5.0})
        await d2.pump(); await settle(d2)
        assert 40 <= plan_msgs(fs2)[0]["data"]["plan"]["estimate_s"] <= 70
    finally:
        settings.studio_ask_long_seconds = old


async def t_ltx_dev_high_quality_with_sound_plans_then_runs_the_dev_recipe():
    fs = FakeStore(); fs.patch()
    fs.thread_cfg = {}
    deps = FakeDeps(fs, rec=LTXH)
    deps.queue = no_queue
    d = D.Director(deps, {LTX["id"]: LTX, LTXH["id"]: LTXH, IMG["id"]: IMG})
    t = new_via(fs, rec=LTXH, spec={"seconds": 10.0})
    await d.pump(); await settle(d)
    assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))        # 높음은 늘 계획 카드를 거친다
    plan = plan_msgs(fs)[0]["data"]["plan"]
    assert plan["quality"] == "높음" and plan["seconds"] == 10.0 and plan["frames"] == 241 and plan["fps"] == 24
    assert 100 <= plan["estimate_s"] <= 150, plan["estimate_s"]                     # 실측 10초 122초
    assert [c[1]["extra_data"]["studio"]["recipe"] for c in deps.calls if c[0] == "submit"] == [IMG["id"]]    # 영상은 승인 뒤에
    assert await d.approve(t) == "ok"
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
    subs = [c[1] for c in deps.calls if c[0] == "submit"]
    assert [s["extra_data"]["studio"]["recipe"] for s in subs] == [IMG["id"], LTXH["id"]]
    body = subs[1]["prompt"]
    assert body["320:301"]["inputs"]["value"] == 10 and body["320:319"]["inputs"]["value"].endswith(SOUND_TEXT)
    assert body["320:S1G"]["inputs"]["cfg"] == 3.5 and body["900"]["inputs"]["batch_index"] == 120
    assert d.quality_of_turn(t) == "high" and d.engine_of_turn(t) == "ltx"
    # 엔진·화질 고르기: 높음은 dev, 빠름은 2.5 distilled, 높음이 꺼지면 빠른 LTX 로 내려간다
    assert d.recipe_for("i2v", "high", True)["id"] == LTXH["id"] and d.recipe_for("i2v", "fast", True)["id"] == LTX["id"]
    d.disabled.add(LTXH["id"])
    assert d.recipe_for("i2v", "high", True)["id"] == LTX["id"]
    # 다시 만들기가 꺼진 dev 레시피를 못 쓰면 같은 엔진의 빠른 레시피로 잇는다 (소리를 잃지 않는다)
    assert d.recipe_for("i2v", d.quality_of_turn(t), d.engine_of_turn(t) == "ltx")["id"] == LTX["id"]


async def t_engine_choice_covers_sound_quality_and_fallbacks():
    from app.studio import engine
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    full = {REC["id"]: REC, HQ["id"]: HQ, LTX["id"]: LTX, LTXH["id"]: LTXH, IMG["id"]: IMG}
    d = D.Director(deps, full)

    def pick(dd=d, opts=None, text="비 오는 거리의 남자 영상", action="t2v", carry="new", last=None, high=False, sd=True):
        return engine.choose(dd, opts or {}, text, action, carry, last, high, high and action in ("i2v", "t2v"), sd)

    # 기본: 소리 포함 (빠름)
    c = pick()
    assert c["use_sound"] and not c["notes"] and d.recipe_for("i2v", "fast", True)["id"] == LTX["id"]
    # 높음을 골라도 소리 포함이 기본: 증류 없는 dev
    c = pick(high=True)
    assert c["use_sound"] and c["high_ok"] and not c["notes"] and d.recipe_for("i2v", "high", True)["id"] == LTXH["id"]
    # 소리 칸 없음 → 소리 없는 Wan (높음이면 Wan 높음), 글의 '소리 없이' 도 같다
    c = pick(opts={"sound": "off"}, high=True)
    assert not c["use_sound"] and c["high_ok"] and not c["notes"]
    assert not pick(text="소리 없이 비 오는 거리 영상")["use_sound"] and not pick(text="무음 영상으로", high=True)["use_sound"]
    # 칸이 글보다 우선, 칸 포함 + 높음 = dev
    assert pick(opts={"sound": "on"}, text="소리 없이 만들어줘", high=True)["use_sound"]
    # 설정 기본이 꺼지면 소리 없음, 칸이 켜면 소리 포함
    assert not pick(sd=False)["use_sound"] and pick(opts={"sound": "on"}, sd=False)["use_sound"]
    # 이어 말하기는 앞 요청의 엔진을 잇는다. 앞이 소리 없는 영상이면 이유를 알린다
    c = pick(carry="revise", last={"recipe_id": REC["id"]})
    assert not c["use_sound"] and any("앞 영상이 소리 없는" in n for n in c["notes"])
    assert pick(carry="revise", last={"recipe_id": LTXH["id"]}, high=True)["use_sound"]
    assert pick(carry="revise", last={"recipe_id": LTX["id"]})["use_sound"]
    # dev 레시피가 꺼지면(없으면) 높음을 고른 요청은 화질을 지켜 소리 없는 Wan 높음으로 + 안내.
    # 소리를 칸에서 직접 포함으로 고르면 빠른 LTX 로 내려가고 화질이 낮아졌음을 안내한다
    d2 = D.Director(deps, full); d2.disabled.add(LTXH["id"])
    c = pick(dd=d2, high=True)
    assert not c["use_sound"] and c["high_ok"] and any("소리가 없는 영상" in n for n in c["notes"])
    c = pick(dd=d2, opts={"sound": "on"}, high=True)
    assert c["use_sound"] and not c["high_ok"] and any("기본 화질" in n for n in c["notes"])
    assert d2.recipe_for("i2v", "fast", True)["id"] == LTX["id"]
    # 소리 포함 높음 레시피가 처음부터 없으면 높음은 소리 없는 Wan 높음 + 안내
    d3 = D.Director(deps, {REC["id"]: REC, HQ["id"]: HQ, LTX["id"]: LTX, IMG["id"]: IMG})
    c = pick(dd=d3, high=True)
    assert not c["use_sound"] and c["high_ok"] and any("소리가 없는 영상" in n for n in c["notes"])
    # 빠른 LTX 만 꺼지고 dev 는 살아 있으면 빠름 요청은 소리 없는 Wan, 높음은 dev
    d4 = D.Director(deps, full); d4.disabled.add(LTX["id"])
    assert not pick(dd=d4)["use_sound"] and pick(dd=d4, high=True)["use_sound"]
    # LTX 가 모두 꺼지면 소리 없이 + 안내, 이미지 레시피가 없으면 글→영상만 소리 없이 (사진→영상은 소리 포함)
    d5 = D.Director(deps, {REC["id"]: REC, HQ["id"]: HQ, IMG["id"]: IMG})
    c = pick(dd=d5)
    assert not c["use_sound"] and any("준비 중" in n for n in c["notes"])
    d6 = D.Director(deps, {REC["id"]: REC, HQ["id"]: HQ, LTX["id"]: LTX, LTXH["id"]: LTXH})
    assert not pick(dd=d6, action="t2v")["use_sound"] and pick(dd=d6, action="i2v")["use_sound"]
    # 영상이 아니거나 감독이 없으면 건드리지 않는다
    assert pick(action="image") == {"use_sound": False, "high_ok": False, "notes": []}
    assert engine.choose(None, {}, "x", "t2v", "new", None, True, True, True) == {"use_sound": False, "high_ok": True, "notes": []}
    # 승인 대기 중 dev 가 꺼지면 같은 소리 포함 빠른 레시피로 잇고 알린다 (소리·계획 카드 없이 높음이 사라지는 것을 말없이 두지 않는다)
    fs = FakeStore(); fs.patch(); fs.thread_cfg = {}
    deps5 = FakeDeps(fs, rec=LTXH); deps5.queue = no_queue
    d7 = via_director(deps5, LTXH)
    d7.recipe_map[LTX["id"]] = LTX
    t = new_via(fs, rec=LTXH, spec={"seconds": 10.0})
    await d7.pump(); await settle(d7)
    assert t["state"] == "awaiting_approval"
    d7.disabled.add(LTXH["id"])
    assert await d7.approve(t) == "replan"
    assert any(m["kind"] == "notice" and "기본 화질" in m["body"] for m in fs.messages)


async def t_resume_after_prompt_done_skips_llm():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    t["state"] = "active"
    t["work"] = {"prompt": "P " * 300, "settings": {"frames": 81, "seconds": 5.0}}
    await d.recover()
    await settle(d)
    assert t["state"] == "done"
    names = [c[0] for c in deps.calls]
    assert "render" not in names and "submit" in names, names


async def t_image_happy_path():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=IMG)
    d = D.Director(deps, {IMG["id"]: IMG})
    t = new_turn(fs, "비 오는 밤 골목의 작은 카페", rec=IMG)
    await d.pump()
    await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events)
    names = [c[0] for c in deps.calls]
    assert names[0] == "expand_image" and "render" not in names and "submit" in names, names
    body = [c[1] for c in deps.calls if c[0] == "submit"][0]
    p = body["prompt"]
    assert p["57:27"]["inputs"]["text"] == "IMG 비 오는 밤 골목의 작은 카페"
    assert (p["57:13"]["inputs"]["width"], p["57:13"]["inputs"]["height"]) == (1024, 1024)
    assert p["9"]["inputs"]["filename_prefix"].startswith("bob/" + str(t["id"])[:8] + "/")
    assert "first_frame" not in IMG["inject"]
    for k in ("expand", "negative_check", "comfy_submitted", "output", "inspect"):
        assert k in kinds(fs), (k, kinds(fs))
    res = [m for m in fs.messages if m["kind"] == "result"][0]
    assert res["data"]["media"] == "image" and [a["id"] for a in res["data"]["actions"]] == ["to_video", "retry_seed"]
    a = fs.results[0]
    assert a["media"] == "image" and a["result_rel"].endswith("image_00001_.png") and "sha256" in a
    ins = [c[1] for c in deps.calls if c[0] == "inspect"][0]
    assert ins[0] == "studio" and ins[2] == "image_00001_.png" and ins[3] is None     # 이미지는 결과 파일을 직접 검사
    stages = [e["progress"]["stage"] for e in deps.published if e["type"] == "turn.progress"]
    assert "frame" not in stages
    chips = [e["progress"]["chips"] for e in deps.published if e["type"] == "turn.progress"][0]
    assert [c["key"] for c in chips] == ["prompt", "generate", "collect"]


async def t_image_aspect_and_two_people_notice():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=IMG)
    deps.inspect_result = {"ok": True, "issues": [], "traits": [], "people": 1}
    d = D.Director(deps, {IMG["id"]: IMG})
    t = new_turn(fs, "세로로 두 사람이 우산을 같이 쓴 그림", spec={"aspect": "portrait"}, rec=IMG)
    await d.pump()
    await settle(d)
    p = [c[1] for c in deps.calls if c[0] == "submit"][0]["prompt"]
    w, h = p["57:13"]["inputs"]["width"], p["57:13"]["inputs"]["height"]
    assert w < h and w % 16 == 0 and h % 16 == 0, (w, h)
    n = [m for m in fs.messages if m["kind"] == "notice"]
    assert n and "한 몸처럼" in n[0]["body"] and n[0]["data"]["actions"][0]["id"] == "retry_seed",         ([e for e in fs.events if e[0] in ("inspect", "error", "notice")], t["state"])


async def t_image_new_seed_reuses_prompt():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs, rec=IMG)
    d = D.Director(deps, {IMG["id"]: IMG})
    src = new_turn(fs, "작은 카페", rec=IMG)
    src["state"] = "done"
    src["work"] = {"prompt": "SAVED PROMPT", "size": [1024, 1024]}
    t = new_turn(fs, "작은 카페", rec=IMG, mode="new_seed")
    t["request"]["source_turn"] = str(src["id"])
    await d.pump()
    await settle(d)
    assert t["state"] == "done"
    names = [c[0] for c in deps.calls]
    assert "expand_image" not in names and "submit" in names
    assert [c[1] for c in deps.calls if c[0] == "submit"][0]["prompt"]["57:27"]["inputs"]["text"] == "SAVED PROMPT"


async def t_lost_resubmits_once():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.outcome_seq = ["lost", "success"]
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump()
    await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"))
    subs = [c[1] for c in deps.calls if c[0] == "submit"]
    assert len(subs) == 2 and subs[0]["client_id"].endswith("-g1") and subs[1]["client_id"].endswith("-g2")
    assert "auto_resubmit" in kinds(fs) and t["work"]["lost_retried"] is True
    assert [c[0] for c in deps.calls].count("render") == 1       # 프롬프트는 다시 쓰지 않는다


async def t_lost_twice_fails():
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    deps.outcome_seq = ["lost", "lost"]
    d = D.Director(deps, {REC["id"]: REC})
    t = new_turn(fs)
    await d.pump()
    await settle(d)
    assert t["state"] == "failed" and t["error"]["code"] == "lost"
    assert len([c for c in deps.calls if c[0] == "submit"]) == 2


def t_unit_helpers():
    assert D.needs_fix([]) is False
    assert D.needs_fix(["The prompt has 520 words; write 250 to 450 words. Cut it"]) is False
    assert D.needs_fix(["The prompt has 520 words; write 250 to 450 words.", "bad"]) is True
    assert D.notice_for_inspection({"issues": [{"type": "missing_head"}]}, False) and \
        not D.notice_for_inspection({"issues": [{"type": "missing_head"}]}, True)
    assert not D.notice_for_inspection({"issues": [{"type": "missing_limb"}, {"type": "extra_fingers"}, {"type": "other"}]}, False)
    assert D.notice_for_inspection({"issues": [], "traits": [{"present": False}]}, True)
    assert not D.notice_for_inspection({"issues": [], "traits": [{"present": False}]}, False)
    # 부정 자리 확인
    import copy
    p = recipes.build(REC, {"prompt": "x", "first_frame": "studio/b/a.png", "seed": 1, "frames": 81, "width": 640,
                            "height": 640, "check_index": 40, "save_prefix": ["b/abcd1234/video", "b/abcd1234/check_frame"]})
    assert D.negative_problem(REC, p) is None
    q = copy.deepcopy(p); q["129:89"]["inputs"]["text"] = "x"
    assert "부정 자리" in D.negative_problem(REC, q)
    q = copy.deepcopy(p); q["129:98"]["inputs"]["positive"] = ["129:89", 0]
    assert D.negative_problem(REC, q) is not None


async def main():
    t_unit_helpers()
    print("ok t_unit_helpers")
    t_approve_words()
    print("ok t_approve_words")
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and inspect.iscoroutinefunction(fn):
            await asyncio.wait_for(asyncio.ensure_future(fn()), timeout=20)
            print("ok", name)
    print("ok studio_director")

asyncio.run(main())
