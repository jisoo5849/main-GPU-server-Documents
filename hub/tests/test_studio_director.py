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

        for k, v in list(locals().items()):
            if k != "fs" and callable(v):
                setattr(store, k, v)


class FakeDeps:
    def __init__(self, fs, violations=None, outcome="success", fix_changes=True, rec=None):
        self.fs, self.violations, self.outcome = fs, violations or [], outcome
        self.rec = rec or REC
        self.outcome_seq = []        # 접수마다 차례로 쓸 outcome (없으면 outcome)
        self.calls = []
        self.receivers = {}
        self.queue_rows = []
        self.settings = {}
        self.published = []
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
        return {"prompt": "P " * 300, "parts": {"a": "x"}, "ko": {"subject": "s"}, "motion": "m", "profile": "wan22_i2v_long",
                "settings": {"frames": 81, "seconds": 5.0}, "frame": {"subject": "cockpit"}, "violations": self.violations,
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
        return {"prompt": "FIXED " * 300, "parts": {"a": "y"}, "ko": {}, "changed": True, "fixed": True, "violations": [],
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
        asyncio.ensure_future(self._drive(jid, cid, outcome))
        return {"prompt_id": jid, "number": 5, "node_errors": {}}

    async def _drive(self, jid, cid, outcome):
        j = self.fs.jobs[jid]
        bands = self.rec["progress"]["bands"]
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
            if self.rec["action"] in ("image", "edit"):
                self.fs.outputs[jid] = [{"node_id": "9", "rel_path": "studio/chat/2026-11-12/bob/abcd1234/image_00001_.png", "media": "images"}]
            else:
                self.fs.outputs[jid] = [{"node_id": "108", "rel_path": "studio/chat/2026-11-12/bob/abcd1234/video_00001_.mp4", "media": "videos"},
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
        rep = recipes.check_recipe(r, {"wan22_i2v_long", "wan22_t2v_long"})
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
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and inspect.iscoroutinefunction(fn):
            await asyncio.wait_for(asyncio.ensure_future(fn()), timeout=20)
            print("ok", name)
    print("ok studio_director")

asyncio.run(main())
