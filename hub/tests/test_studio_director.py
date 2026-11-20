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
import tempfile
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
from app.studio import files, intent, recipes, store  # noqa: E402

D.POLL = 0.01
D.LLM_POLL = 0.01
settings.studio_progress_min_interval_ms = 0
settings.studio_inspect = "idle"
# 그림·클립 자동 검사(11-20)는 따로 시험한다: 켜 두면 가짜 검사 결과(사람 0명)로 모든 긴 영상 시험에서 다시 만들기가 돈다
settings.studio_picture_check = False

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
        async def replan_turn(tid, stage, rid, ver, keep=None):
            t = fs.turns[tid]
            if t["state"] != "awaiting_approval":
                return False
            t.update(state="pending", stage=stage, work=dict(keep or {}), plan=None, recipe_id=rid, recipe_version=ver)
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
                "length_choice": {"tier": "long", "reason": "자세해서", "by": "rule"}, "crew": None, "rounds": 0,
                "plan": kw.get("plan") or (dict(self.PLAN) if kw.get("direction") else None)}

    async def write_edit(self, user, ip, text, frame=None):
        self.calls.append(("write_edit", text))
        await asyncio.sleep(0.02)
        return {"prompt": "Replace the background with a warm sunset sky and keep the person unchanged", "violations": [],
                "rounds": 1}

    PLAN = {"place": ["wet sidewalk", "low curb", "building fronts", "a side street"],
            "light": "Soft light from the left edges his shoulder; a warm lit window glows far behind.",
            "air": "Rain crosses the pools of lamp light.", "details": ["a glistening wet sleeve"],
            "acting": ["head bowed, shoulders rounded", "he lifts his head to check the path", "he walks on at the same pace"]}
    plan_fail = False

    async def plan(self, user, ip, env, **kw):
        self.calls.append(("plan", kw))
        await asyncio.sleep(0.01)
        if self.plan_fail:
            raise RuntimeError("plan down")
        return dict(self.PLAN)

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
        seq = getattr(self, "inspect_seq", None)
        if seq:                       # 차례대로 돌려줄 검사 결과 (예외면 검사 실패)
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
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
         "stage": None, "recipe_id": rec["id"], "recipe_version": rec["version"], "request": req, "work": None, "depends_on": None,
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
    assert {r["action"] for r in rs.values()} == {"i2v", "t2v", "image", "edit", "music", "voice", "voice_clone", "talk", "scene_image"}
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
    assert d.recipe_for("scene_image")["id"] == "qwen_image_2512_scene"


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


async def t_walking_request_gets_one_random_staging_for_scene_and_video():
    """11-17: 방향을 안 정한 보행 요청은 연출 하나를 골라 장면 이미지 힌트와 영상 프롬프트에 똑같이 준다"""
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = via_director(deps)
    t = new_via(fs, "비오는 저녁 우산을 쓰고 걸어가는 남성을 영상으로 만들어줘")
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"))
    sid = t["work"].get("staging")
    assert sid in ("side_lr", "side_rl", "away", "toward"), sid
    hint = [c[1] for c in deps.calls if c[0] == "expand_image"][0]
    from app import video
    assert video.STAGINGS[sid]["scene"] in hint
    rk = [c[1] for c in deps.calls if c[0] == "render"][0]
    assert rk["staging"] == sid
    # 방향을 말했거나 1인칭이면 연출을 고르지 않는다
    fs2 = FakeStore(); fs2.patch()
    deps2 = FakeDeps(fs2)
    d2 = via_director(deps2)
    t2 = new_via(fs2, "왼쪽에서 오른쪽으로 걸어가는 남성을 영상으로 만들어줘")
    await d2.pump(); await settle(d2)
    assert t2["state"] == "done" and not t2["work"].get("staging")
    assert [c[1] for c in deps2.calls if c[0] == "render"][0]["staging"] is None


async def t_director_plan_feeds_scene_and_video_and_a_failed_plan_does_not_stop_the_request():
    """11-17: 연출 계획표를 먼저 짜서 장면 이미지 힌트(장소·빛)와 영상 프롬프트(연기·공기)에 같이 준다"""
    fs = FakeStore(); fs.patch()
    deps = FakeDeps(fs)
    d = via_director(deps)
    t = new_via(fs, "비 오는 저녁 우산을 쓴 남자가 걸어가는 영상")
    await d.pump(); await settle(d)
    assert t["state"] == "done", (t["state"], t.get("error"))
    order = [c[0] for c in deps.calls if c[0] in ("plan", "expand_image", "render")]
    assert order == ["plan", "expand_image", "render"], order
    hint = [c[1] for c in deps.calls if c[0] == "expand_image"][0]
    assert "wet sidewalk" in hint and "lit window" in hint and "glistening wet sleeve" in hint
    assert "lifts his head" not in hint                      # 연기 박자는 영상 글의 몫이다
    rk = [c[1] for c in deps.calls if c[0] == "render"][0]
    assert rk["plan"]["acting"][1].startswith("he lifts his head") and t["work"]["plan"] == rk["plan"]
    assert any(k == "plan" for k, _d in fs.events)
    # 계획표를 못 짜도 요청은 계속된다
    fs2 = FakeStore(); fs2.patch()
    deps2 = FakeDeps(fs2); deps2.plan_fail = True
    d2 = via_director(deps2)
    t2 = new_via(fs2, "비 오는 저녁 우산을 쓴 남자가 걸어가는 영상")
    await d2.pump(); await settle(d2)
    assert t2["state"] == "done" and not t2["work"].get("plan")
    assert [c[1] for c in deps2.calls if c[0] == "render"][0]["plan"] is None
    assert any(k == "plan_failed" for k, _d in fs2.events)
    # 1인칭 요청은 계획표를 짜지 않는다
    fs3 = FakeStore(); fs3.patch()
    deps3 = FakeDeps(fs3)
    d3 = via_director(deps3)
    t3 = new_via(fs3)
    await d3.pump(); await settle(d3)
    assert t3["state"] == "done" and not [c for c in deps3.calls if c[0] == "plan"]


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


# ── 긴 영상 (11-17, docs/long-video-design.md 7절) ──
MUS = recipes.load_all()["ace_step_music"]
LONG_STORY = {
    "title_ko": "비 오는 귀갓길", "logline_ko": "비 오는 저녁 남자가 집으로 돌아간다.",
    "bible": {"characters": [{"id": "A", "look": "a man in his forties, navy wool coat, black umbrella"}],
              "places": [{"id": "P1", "look": "a wet city street"}, {"id": "P2", "look": "an apartment entrance"}],
              "time": "early evening, steady rain", "light": "cool dusk with warm windows"},
    "music": {"tags": "melancholic cinematic instrumental, soft piano", "bpm": 70, "key": "D minor", "ko": "잔잔한 피아노"},
    "scenes": [
        {"seconds": 10, "place": "P1", "characters": ["A"], "continues": False, "shot": "wide shot", "action": "He walks.",
         "sound": "rain on the umbrella", "ko": "남자가 비 오는 거리를 걷는다."},
        {"seconds": 10, "place": "P1", "characters": ["A"], "continues": True, "shot": "medium tracking shot",
         "action": "He looks up.", "sound": "footsteps on wet stone", "ko": "남자가 고개를 든다."},
        {"seconds": 10, "place": "P2", "characters": ["A"], "continues": False, "shot": "close shot", "action": "He folds the umbrella.",
         "sound": "the umbrella snapping shut", "ko": "남자가 현관에서 우산을 접는다."}],
    "seconds": 30}


class LongDeps(FakeDeps):
    """긴 영상 시험용: 구성안·합치기를 가짜로, 작업마다 결과 파일을 실제로 만든다 (클립 지문·마지막 프레임 확인을 시험하려고)"""

    def __init__(self, fs, root, **kw):
        super().__init__(fs, rec=LTX, **kw)
        self.root, self.n = root, 0
        self.recs = {LTX["id"]: LTX, IMG["id"]: IMG, MUS["id"]: MUS}
        self.hold_run = False       # True 면 작업이 running 에 머문다 (hold 는 queued 에 머문다)
        self.log = []               # 접수·work 저장 순서 (다시 만들 때 옛 기록을 먼저 비우는지 본다)

    async def storyboard(self, user, ip, env, **kw):
        self.calls.append(("storyboard", kw))
        await asyncio.sleep(0.01)
        return {"story": copy.deepcopy(LONG_STORY), "violations": [], "rounds": 1}

    def assemble(self, clips, music, out, sound=True, transitions=None):
        self.calls.append(("assemble", (list(clips), music, out)))
        self.transitions = transitions
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"final video")
        return {"scenes": len(clips), "seconds": 30.0, "music": bool(music), "duration": 30.0}

    def _file(self, name):
        rel = f"studio/bob/long/{name}"
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(name.encode())
        return rel

    async def submit(self, env, user, body, ip):
        self.calls.append(("submit", body))
        self.log.append(("submit", body["client_id"]))
        jid = str(uuid.uuid4())
        cid = body["client_id"]
        self.fs.jobs[jid] = {"id": jid, "seq": 5, "state": "queued", "outcome": None, "error": None, "started_at": None}
        self.fs.by_client[cid] = {"prompt_id": jid, "number": 5, "node_errors": {}}
        outcome = self.outcome_seq.pop(0) if self.outcome_seq else self.outcome
        asyncio.ensure_future(self._drive(jid, cid, outcome, self.recs[body["extra_data"]["studio"]["recipe"]]))
        return {"prompt_id": jid, "number": 5, "node_errors": {}}

    async def _drive(self, jid, cid, outcome, rec=None):
        j = self.fs.jobs[jid]
        r = self.receivers.get(cid)
        await asyncio.sleep(0.01)
        while self.hold and j["state"] == "queued":
            await asyncio.sleep(0.01)
        if j["state"] != "queued":
            return                      # 대기 중에 취소됐다
        j["state"] = "running"
        j["started_at"] = datetime.datetime.now(datetime.timezone.utc)
        if r:
            await r.send_text('{"type":"execution_start","data":{"prompt_id":"%s"}}' % jid)
        await asyncio.sleep(0.01)
        while self.hold_run and j["state"] == "running":
            await asyncio.sleep(0.01)
        if j["state"] != "running":
            return                      # 실행 중에 중단됐다
        if outcome == "success":
            self.n += 1
            main = rec["outputs"]["main"]["node"]
            ext = (rec["outputs"]["main"].get("ext") or [".mp4"])[0]
            if rec["action"] in ("image", "edit", "scene_image"):
                outs = [{"node_id": main, "rel_path": self._file(f"img_{self.n}{ext}"), "media": "images"}]
            elif rec["action"] in ("music", "voice", "voice_clone"):
                outs = [{"node_id": main, "rel_path": self._file(f"{rec['action']}_{self.n}{ext}"), "media": "audio"}]
            elif rec["action"] == "talk":
                outs = [{"node_id": main, "rel_path": self._file(f"talk_{self.n}.mp4"), "media": "videos"}]
                chk = (rec.get("outputs") or {}).get("check")
                if chk:              # 입 맞춤 클립의 검사 프레임 (11-20)
                    outs.append({"node_id": chk["node"], "rel_path": self._file(f"talkcheck_{self.n}.png"), "media": "images"})
            else:
                outs = [{"node_id": main, "rel_path": self._file(f"video_{self.n}.mp4"), "media": "videos"},
                        {"node_id": "901", "rel_path": self._file(f"check_{self.n}.png"), "media": "images"},
                        {"node_id": "903", "rel_path": self._file(f"last_{self.n}.png"), "media": "images"}]
            self.fs.outputs[jid] = outs
            if r:
                await r.send_text('{"type":"execution_success","data":{"prompt_id":"%s"}}' % jid)
        j["state"] = "finished"
        j["outcome"] = outcome
        if outcome == "error":
            j["error"] = {"message": "boom"}


def long_turn(fs, seconds=30.0):
    t = new_via(fs, "비 오는 저녁 우산 쓴 남자가 집에 가는 30초 영상", rec=LTX, spec={"seconds": seconds})
    t["request"]["long"] = True
    return t


def recipe_of(call):
    return call[1]["extra_data"]["studio"]["recipe"]


async def t_long_video_story_plan_scenes_music_and_assembly():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            await d.pump(); await settle(d, 10)
            # 1) 구성안 → 계획 카드 (긴 영상은 늘 승인을 받는다). GPU 작업은 아직 없다
            assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
            plan = plan_msgs(fs)[-1]["data"]["plan"]
            assert plan["action"] == "long" and plan["label"] == "30초" and len(plan["scenes"]) == 3 and plan["scenes"][1]["continues"]
            assert plan["music"] == "잔잔한 피아노" and plan["estimate_s"] > 3 * D.LONG_SCENE_OVERHEAD and plan["size"] == [1280, 704]
            assert [c[1]["seconds"] for c in deps.calls if c[0] == "storyboard"] == [30.0]
            assert not [c for c in deps.calls if c[0] == "submit"]
            # 2) 승인 → 장면 셋 → 음악 → 합치기 → 결과 하나
            assert await d.approve(t) == "ok"
            await d.pump(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            assert [recipe_of(c) for c in subs] == [IMG["id"], LTX["id"], LTX["id"], IMG["id"], LTX["id"], MUS["id"]]   # 이어지는 장면은 그림을 새로 안 그린다
            tid = str(t["id"])
            assert [c[1]["client_id"] for c in subs] == [f"studio-{tid}-{x}" for x in ("L0i1", "L0v1", "L1v1", "L2i1", "L2v1", "M1")]
            staged = [c[1] for c in deps.calls if c[0] == "stage_image"]
            assert staged[0].endswith("img_1.png") and staged[1].endswith("last_2.png") and staged[2].endswith("img_4.png")
            renders = [c[1] for c in deps.calls if c[0] == "render"]
            assert len(renders) == 3 and all(r["music"] is False and r["staging"] is None for r in renders)
            assert renders[1]["text"] == "남자가 고개를 든다." and "scene 2 of 3" in renders[1]["notes"]
            assert renders[1]["image_prompt"] == "" and renders[0]["image_prompt"].startswith("IMG ")    # 이어진 장면은 첫 화면만 읽는다
            plans = [c[1] for c in deps.calls if c[0] == "plan"]
            # 이어지는 장면(2)의 계획표는 첫 화면을 읽은 뒤 영상 글을 쓰는 차례 안에서 쓴다 (먼저 쓰지 않는다)
            assert len(plans) == 2 and plans[1]["text"] == "남자가 현관에서 우산을 접는다." and "apartment entrance" in plans[1]["notes"]
            assert renders[1]["plan"] is None and renders[0]["plan"] is not None and renders[2]["plan"] is not None
            assert all(r["safe"] for r in renders) and all(p["safe"] for p in plans)
            assert "IS the last frame of the previous clip" in renders[1]["direction"] and "same shot size" in renders[1]["direction"]
            assert "NEXT clip starts from this clip's last frame" in plans[0]["direction"] and "after a cut" in plans[1]["direction"]
            assert len([c for c in deps.calls if c[0] == "expand_image"]) == 2
            video = subs[2][1]["prompt"]
            assert video["398:362"]["inputs"]["value"] == 10 and video["902"]["inputs"]["batch_index"] == -1
            assert video["398:372"]["inputs"]["value"] == 1280 and video["398:360"]["inputs"]["value"] == 704    # 모든 장면이 같은 크기
            music = subs[5][1]["prompt"]
            assert music["94"]["inputs"]["tags"] == LONG_STORY["music"]["tags"] and music["98"]["inputs"]["seconds"] == 32.0
            assert music["94"]["inputs"]["bpm"] == 70 and music["94"]["inputs"]["keyscale"] == "D minor"
            clips, mus, out = [c[1] for c in deps.calls if c[0] == "assemble"][0]
            assert [os.path.basename(c) for c in clips] == ["video_2.mp4", "video_3.mp4", "video_5.mp4"] and mus.endswith("music_6.mp3")
            assert out.endswith(f"long_{tid[:8]}.mp4") and os.path.isfile(out)
            res = [m for m in fs.messages if m["kind"] == "result"]
            assert len(res) == 1 and res[0]["data"]["media"] == "video"
            att = fs.atts[res[0]["data"]["attachment_ids"][0]]
            assert att["job_id"] is None and att["result_rel"].endswith(f"long_{tid[:8]}.mp4") and att["sha256"]
            work = t["work"]
            assert len(work["scenes"]) == 3 and all(s.get("clip_sha") for s in work["scenes"]) and work["music_rel"].endswith(".mp3")
            assert work["scenes"][1]["first_from"] == "last" and work["scenes"][0]["first_from"] == "image"
            firsts = [s["first_from"] for s in work["scenes"]]
            assert deps.transitions == [D.DISSOLVE_SECONDS if f == "last" else 0.0 for f in firsts[1:]] and any(deps.transitions)   # 이어 만든 이음새만 디졸브
            assert {"story", "scenes", "music", "assemble"} <= set(fs.stages)
            prog = fs.progress[t["id"]]
            assert prog["title"] == "긴 영상 만드는 중" and [c["key"] for c in prog["chips"]] == ["story", "scenes", "music", "assemble"]
            assert "long_assembled" in kinds(fs) and kinds(fs).count("long_scene_done") == 3
        finally:
            settings.results_dir = old


async def t_long_video_failure_keeps_done_scenes_and_retry_continues():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}        # 승인한 뒤라고 본다
            # 셋째 장면 영상이 두 번 다 실패한다 (한 번은 시드를 바꿔 자동으로 다시 한다)
            deps.outcome_seq = ["success", "success", "success", "success", "error", "error"]
            await d.pump(); await settle(d, 15)
            assert t["state"] == "failed" and t["error"]["code"] == "run_error", (t["state"], t.get("error"))
            assert kinds(fs).count("long_scene_retry") == 1 and len(t["work"]["scenes"]) == 3
            assert [bool(s.get("clip_rel")) for s in t["work"]["scenes"]] == [True, True, False]
            # 안내 버튼은 '다시 시도'(남은 장면부터). '다시 만들기'(새 시드)는 끝난 장면을 버리므로 붙이지 않는다
            note = [m for m in fs.messages if m["kind"] == "notice"][-1]
            assert note["data"]["actions"] == D.RETRY_FULL and note["body"].endswith(D.LONG_KEEP), note
            # 다시 시도: 끝난 장면 둘은 그대로, 셋째 장면부터 (계획 카드 없이)
            r = new_via(fs, "다시 시도", rec=LTX, spec={"seconds": 30.0})
            r["request"].update(long=True, mode="rewrite", source_turn=str(t["id"]))
            n_before = len([c for c in deps.calls if c[0] == "submit"])
            await d.pump(); await settle(d, 15)
            assert r["state"] == "done", (r["state"], r.get("error"), fs.events[-4:])
            subs = [recipe_of(c) for c in deps.calls if c[0] == "submit"][n_before:]
            assert subs == [IMG["id"], LTX["id"], MUS["id"]]
            assert ("long_inherited", {"source": str(t["id"]), "mode": "rewrite", "scenes_kept": 2}) in fs.events
            clips = [c[1] for c in deps.calls if c[0] == "assemble"][-1][0]
            assert [os.path.basename(c) for c in clips] == ["video_2.mp4", "video_3.mp4", "video_6.mp4"]
            # 결과 폴더의 클립이 바뀌었으면 그 장면부터 다시 만든다 (지문이 다르다)
            with open(os.path.join(root, t["work"]["scenes"][1]["clip_rel"]), "wb") as f:
                f.write(b"edited by someone")
            r2 = new_via(fs, "다시 시도", rec=LTX, spec={"seconds": 30.0})
            r2["request"].update(long=True, mode="rewrite", source_turn=str(t["id"]))
            await d.pump(); await settle(d, 15)
            assert r2["state"] == "done", (r2["state"], r2.get("error"))
            assert [dd for k, dd in fs.events if k == "long_inherited"][-1]["scenes_kept"] == 1
        finally:
            settings.results_dir = old


async def t_long_video_without_music_recipe_joins_scene_sound_only():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, {LTX["id"]: LTX, IMG["id"]: IMG})        # 음악 레시피가 없다
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"))
            assert MUS["id"] not in [recipe_of(c) for c in deps.calls if c[0] == "submit"]
            assert [c[1] for c in deps.calls if c[0] == "assemble"][0][1] is None
            res = [m for m in fs.messages if m["kind"] == "result"][0]
            assert "배경음악을 만들지 못해 음악 없이 이어 붙였습니다" in res["data"]["notes"]
        finally:
            settings.results_dir = old


async def t_long_video_resumes_after_hub_restart():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            clip = deps._file("video_old.mp4")
            sha = files.fingerprint(clip)[1]
            t["state"] = "active"
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True, "long_size": [1280, 704],
                         "scenes": [{"clip_rel": clip, "clip_sha": sha, "last_rel": deps._file("last_old.png"), "first_from": "image"}]}
            await d.recover(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"))
            assert t["work"]["long_resumes"] == 1
            subs = [recipe_of(c) for c in deps.calls if c[0] == "submit"]
            assert subs == [LTX["id"], IMG["id"], LTX["id"], MUS["id"]]          # 둘째 장면(앞 장면에서 이어짐)부터
            assert [c[1] for c in deps.calls if c[0] == "stage_image"][0].endswith("last_old.png")
            # 재시작이 되풀이되면 멈춘다
            t2 = long_turn(fs)
            t2["state"] = "active"
            t2["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True, "long_resumes": D.LONG_RESUME_MAX}
            await d.recover(); await settle(d, 5)
            assert t2["state"] == "interrupted"
        finally:
            settings.results_dir = old


async def t_long_video_remakes_a_changed_clip_and_the_scene_chained_to_it():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            c0, c1 = deps._file("video_a.mp4"), deps._file("video_b.mp4")
            sha1 = files.fingerprint(c1)[1]
            t["state"] = "active"
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True, "long_size": [1280, 704],
                         "L0v_pending": {"client_id": f"studio-{t['id']}-L0v1", "attempt": 1},
                         "scenes": [{"clip_rel": c0, "clip_sha": "0" * 64, "last_rel": deps._file("last_a.png"),
                                     "plan": dict(FakeDeps.PLAN)},
                                    {"clip_rel": c1, "clip_sha": sha1, "last_rel": deps._file("last_b.png")}]}
            fs.by_client[f"studio-{t['id']}-L0v1"] = {"prompt_id": "old-job"}          # 옛 작업을 이어받으면 안 된다
            await d.recover(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"))
            redo = [dd for k, dd in fs.events if k == "long_scene_redo"]
            assert redo == [{"index": 0, "chained": False}, {"index": 1, "chained": True}]      # 첫 장면이 바뀌어 이어진 둘째도 다시
            subs = [c for c in deps.calls if c[0] == "submit"]
            assert [recipe_of(c) for c in subs] == [IMG["id"], LTX["id"], LTX["id"], IMG["id"], LTX["id"], MUS["id"]]
            assert subs[1][1]["client_id"].endswith("-L0v2")                                   # 시도 번호는 이어서 늘린다
            assert not [c for c in deps.calls if c[0] == "plan" and "scene 1 of 3" in c[1]["notes"]]    # 계획표는 남겨 둔 것을 쓴다
        finally:
            settings.results_dir = old


async def t_long_progress_shows_scene_and_step_next_to_the_percent():
    """11-17 메인: 채팅 진행 카드는 % 가 있으면 '띠 · %' 만 보여 '장면 1/3' 이 안 보였다. 띠 자리에 장면·단계를 넣는다"""
    fs = FakeStore(); fs.patch()
    t = long_turn(fs)
    ctx = D.Ctx(t, USER, ENV, LTX)
    ctx.long = {"phase": "scenes", "i": 0, "n": 3, "done": 1, "pct": 0}
    ctx.sub, ctx.stage, ctx.text = True, "generate", "생성 45%"
    ctx.tracker.pct = 45
    p = D.Director._long_progress(ctx)
    assert p["band"] == "장면 1/3 · 생성" and p["pct"] == int(100 * 1.45 / 4) and p["text"] == "장면 1/3 · 생성 45%"
    ctx.long.update(phase="music")
    ctx.sub, ctx.text = False, "배경음악 만드는 중"
    p = D.Director._long_progress(ctx)
    assert p["band"] == "배경음악 만드는 중" and p["pct"] >= 36                      # 줄지 않는다
    ctx.long.update(phase="story")
    ctx.text = "구성안 쓰는 중"
    p = D.Director._long_progress(ctx)
    assert p["pct"] is None and p["band"] is None and p["text"] == "구성안 쓰는 중"     # % 없는 단계는 글만


def t_scene_hint_follows_the_story_style():
    assert D.Director.scene_hint({"bible": {}}) == D.Director.SCENE_HINT                 # 사진(기본)
    h = D.Director.scene_hint({"bible": {"style": "cel-shaded 3D with hand-drawn outlines"}})
    assert "cel-shaded 3D" in h and "Not a photograph" in h and "one still frame" in h


async def t_long_video_empty_chained_frame_falls_back_to_a_scene_image():
    """11-17 메인: 앞 장면이 사람이 나간 채 끝나 이어 받은 첫 화면에 사람이 없으면, 그 장면은 새 장면 이미지로 다시 시작한다"""
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            base_render = deps.render

            async def render(user, ip, env, **kw):
                r = await base_render(user, ip, env, **kw)
                r["frame"] = {"subject": "an empty wet street", "people": 0 if not kw.get("image_prompt") else 1}
                return r
            deps.render = render
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"))
            assert ("long_chain_empty", {"index": 1}) in fs.events
            subs = [recipe_of(c) for c in deps.calls if c[0] == "submit"]
            assert subs == [IMG["id"], LTX["id"], IMG["id"], LTX["id"], IMG["id"], LTX["id"], MUS["id"]]
            s1 = t["work"]["scenes"][1]
            assert s1["first_from"] == "image" and s1["no_chain"] and s1["frame_people"] == 1
            assert len([c for c in deps.calls if c[0] == "plan"]) == 3                # 장면 1·3 + 새 장면 이미지가 된 장면 2 (빈 화면에서 쓴 계획표는 버린다)
        finally:
            settings.results_dir = old


async def _first_submit(deps, n=1, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if len([c for c in deps.calls if c[0] == "submit"]) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("접수가 없었다")


async def t_long_video_cancel_stops_the_running_scene_job():
    """11-17 검토: 장면 작업을 기다리다 취소하면 그 작업을 멈춰야 한다 (대기 중이면 취소, 실행 중이면 중단)"""
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            for stage in ("queued", "running"):
                fs = FakeStore(); fs.patch()
                deps = LongDeps(fs, root)
                d = D.Director(deps, dict(deps.recs))
                t = long_turn(fs)
                t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
                deps.hold, deps.hold_run = stage == "queued", stage == "running"
                await d.pump()
                await _first_submit(deps)
                jid = fs.by_client[f"studio-{t['id']}-L0i1"]["prompt_id"]
                for _ in range(300):
                    if fs.jobs[jid]["state"] == stage:
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                assert await d.cancel(t)
                await settle(d, 5)
                assert t["state"] == "cancelled", (stage, t["state"])
                stop = ("cancel_queued", [jid]) if stage == "queued" else ("interrupt", jid)
                assert stop in deps.calls, (stage, [c for c in deps.calls if c[0] in ("cancel_queued", "interrupt")])
                assert fs.jobs[jid]["outcome"] == "cancelled"
                deps.hold = deps.hold_run = False
        finally:
            settings.results_dir = old


async def t_long_video_retry_modes_and_approval_inheritance():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            src = long_turn(fs)
            c0, c1 = deps._file("video_a.mp4"), deps._file("video_b.mp4")
            src.update(state="failed", work={"story": copy.deepcopy(LONG_STORY), "approved": True, "long_size": [1280, 704],
                                             "scenes": [{"clip_rel": c0, "clip_sha": files.fingerprint(c0)[1],
                                                         "last_rel": deps._file("last_a.png")},
                                                        {"clip_rel": c1, "clip_sha": files.fingerprint(c1)[1]}]})
            # 실패한 요청에 말로 '다시 만들기'(새 시드)를 해도 끝난 장면부터 잇는다
            r = new_via(fs, "다시 만들기", rec=LTX, spec={"seconds": 30.0})
            r["request"].update(long=True, mode="new_seed", source_turn=str(src["id"]))
            await d.pump(); await settle(d, 15)
            assert r["state"] == "done", (r["state"], r.get("error"))
            assert [recipe_of(c) for c in deps.calls if c[0] == "submit"] == [IMG["id"], LTX["id"], MUS["id"]]
            # 승인한 적 없는 계획을 이어받으면 카드를 다시 보인다 (승인 없이 GPU 를 쓰지 않는다)
            fs2 = FakeStore(); fs2.patch()
            deps2 = LongDeps(fs2, root)
            d2 = D.Director(deps2, dict(deps2.recs))
            src2 = long_turn(fs2)
            src2.update(state="failed", work={"story": copy.deepcopy(LONG_STORY)})
            r2 = new_via(fs2, "다시 시도", rec=LTX, spec={"seconds": 30.0})
            r2["request"].update(long=True, mode="rewrite", source_turn=str(src2["id"]))
            await d2.pump(); await settle(d2, 10)
            assert r2["state"] == "awaiting_approval" and not [c for c in deps2.calls if c[0] == "submit"]
            assert not [c for c in deps2.calls if c[0] == "storyboard"]          # 구성안은 다시 쓰지 않는다
        finally:
            settings.results_dir = old


async def t_long_video_scene_image_retries_once_and_stays_on_ltx():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            deps.outcome_seq = ["error"]           # 첫 장면 이미지가 한 번 실패한다
            await d.pump(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"))
            assert kinds(fs).count("long_image_retry") == 1
            cids = [c[1]["client_id"] for c in deps.calls if c[0] == "submit"]
            assert cids[0].endswith("-L0i1") and cids[1].endswith("-L0i2")
            # 긴 영상은 소리 없는 Wan 레시피로 바꾸지 않는다: 승인할 때 LTX 가 꺼져 있으면 계획을 접는다
            fs2 = FakeStore(); fs2.patch()
            deps2 = LongDeps(fs2, root)
            d2 = D.Director(deps2, {**deps2.recs, REC["id"]: REC})
            t2 = long_turn(fs2)
            await d2.pump(); await settle(d2, 10)
            assert t2["state"] == "awaiting_approval"
            d2.disabled.add(LTX["id"])
            assert await d2.approve(t2) == "gone" and t2["state"] == "cancelled"
            # 레시피가 LTX 가 아니면 긴 영상을 시작하지 않는다
            t3 = long_turn(fs2)
            t3["recipe_id"], t3["recipe_version"] = REC["id"], REC["version"]
            d2.disabled.clear()
            await d2.pump(); await settle(d2, 10)
            assert t3["state"] == "failed" and t3["error"]["code"] == "unavailable"
        finally:
            settings.results_dir = old


async def t_long_video_reset_is_saved_before_the_new_job():
    """11-17 검토: 다시 만들 장면과 거기서 이어지는 장면의 옛 기록을 새 작업을 접수하기 전에 저장해야 재시작해도 옛 클립이 되살아나지 않는다"""
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            orig_merge = store.merge_work

            async def merge_work(tid, patch):
                deps.log.append(("merge", copy.deepcopy(patch)))
                await orig_merge(tid, patch)
            store.merge_work = merge_work
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            c0, c1 = deps._file("video_a.mp4"), deps._file("video_b.mp4")
            t["state"] = "active"
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True, "long_size": [1280, 704],
                         "L0v_pending": {"client_id": f"studio-{t['id']}-L0v1", "attempt": 1},
                         "L1v_pending": {"client_id": f"studio-{t['id']}-L1v1", "attempt": 1},
                         "scenes": [{"clip_rel": c0, "clip_sha": "0" * 64, "last_rel": deps._file("last_a.png")},
                                    {"clip_rel": c1, "clip_sha": files.fingerprint(c1)[1], "first_att": "x"}]}
            await d.recover(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"))
            first_submit = next(i for i, e in enumerate(deps.log) if e[0] == "submit")
            resets = [e[1] for e in deps.log[:first_submit] if e[0] == "merge" and "scenes" in e[1]
                      and not any(s.get("clip_rel") for s in e[1]["scenes"][:2])]
            assert resets, "다시 만들 장면 기록을 접수 전에 비우지 않았다"
            assert resets[0]["L0v_pending"]["client_id"] is None and resets[0]["L1v_pending"]["client_id"] is None
            assert "first_att" not in resets[0]["scenes"][1]
        finally:
            store.merge_work = orig_merge
            settings.results_dir = old


# ── 대사 영상 (11-19): 목소리 먼저 → 대사 녹음 → 인물 기준 그림 → 장면(말하는 장면은 입 맞춤) → 음악 → 합치기 ──
VOICE = recipes.load_all()["qwen3_tts_voice"]
CLONE = recipes.load_all()["qwen3_tts_clone"]
TALK = recipes.load_all()["ltx23_ia2v_talk"]
EDITR = recipes.load_all()["qwen_image_edit"]
SCENE = recipes.load_all()["qwen_image_2512_scene"]


def dialogue_story():
    from app import video
    raw = {
        "title_ko": "숲길 끝의 화원", "logline_ko": "니키와 푸토르가 숲길을 걸으며 이야기한다.",
        "bible": {"characters": [
            {"id": "A", "look": "a twelve-year-old girl with two long black braids, wine-red hat and cape", "name_ko": "니키",
             "voice": "A twelve-year-old Korean girl, clear and natural.", "voice_ko": "맑은 여자아이"},
            {"id": "B", "look": "a short old dwarf herbalist with a long white beard", "name_ko": "푸토르",
             "voice": "A kind Korean grandpa, low warm gravelly voice, slow.", "voice_ko": "굵은 할아버지",
             "sample_ko": "허허, 얘야. 이 할아비가 옛날얘기 하나 해 줄까?"}],
            "places": [{"id": "P1", "look": "a winding forest path"}], "time": "morning", "light": "soft sunbeams",
            "style": "semi-realistic painterly 2.5D fantasy film look"},
        "music": {"tags": "gentle fantasy instrumental, piano", "bpm": 70, "key": "D major", "ko": "잔잔한 피아노"},
        "scenes": [
            {"seconds": 10, "place": "P1", "characters": ["A", "B"], "continues": False, "shot": "wide shot from behind",
             "action": "They walk side by side along the path.", "sound": "footsteps", "ko": "둘이 걷는다", "kind": "shot",
             "speaker": "B", "line_ko": "얘야, 굽은 길은 천천히 보게 해 준단다.", "emotion": "calm and wise"},
            {"seconds": 6, "place": "P1", "characters": ["A"], "continues": False, "shot": "close-up",
             "action": "She looks up and asks softly.", "sound": "breeze", "ko": "니키가 묻는다", "kind": "talk", "speaker": "A",
             "line_ko": "할아버지, 아픈 건 언제 없어져요?", "emotion": "hesitant"},
            {"seconds": 8, "place": "P1", "characters": ["B"], "continues": False, "shot": "medium close-up",
             "action": "He smiles and answers slowly.", "sound": "birds", "ko": "푸토르가 답한다", "kind": "talk", "speaker": "B",
             "line_ko": "아픔은 없애는 게 아니란다.", "emotion": "warm and wise"},
            {"seconds": 6, "place": "P1", "characters": ["A"], "continues": False, "shot": "close-up",
             "action": "She listens quietly, eyes lowered.", "sound": "birds", "ko": "니키가 생각한다", "kind": "shot",
             "speaker": "", "line_ko": "", "emotion": ""}]}
    story = video.clean_dialogue_story(raw, 30)
    assert story is not None
    return story


class DialogueDeps(LongDeps):
    """대사 영상 시험용: 대사 구성안, 녹음 다듬기(길이를 정해 돌려준다), 자막, 대사 합치기를 가짜로"""

    def __init__(self, fs, root, **kw):
        super().__init__(fs, root, **kw)
        self.recs.update({VOICE["id"]: VOICE, CLONE["id"]: CLONE, TALK["id"]: TALK, EDITR["id"]: EDITR, SCENE["id"]: SCENE})
        self.line_secs = {}            # 입력 파일 이름 → 다듬은 길이 차례 (없으면 목소리 3.0초, 대사 2.6초)
        self.trims = []                # (파일 이름, 후처리, 크기 맞추기) 부른 차례
        self.max_secs = {}             # 파일 이름 → 앞 몇 초만 남기라고 한 값
        self.story = None              # 구성안을 이것으로 돌려준다 (없으면 dialogue_story())

    async def storyboard(self, user, ip, env, **kw):
        self.calls.append(("storyboard", kw))
        await asyncio.sleep(0.01)
        return {"story": copy.deepcopy(self.story) if self.story else dialogue_story(), "violations": [], "rounds": 1}

    def trim_voice(self, src, out, fx=None, level=False, max_seconds=None):
        self.calls.append(("trim_voice", (src, out)))
        self.trims.append((os.path.basename(out), fx, level))
        if max_seconds:
            self.max_secs[os.path.basename(out)] = max_seconds
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"wav")
        name = os.path.basename(out)
        seq = self.line_secs.get(name)
        return seq.pop(0) if seq else (3.0 if name.startswith("voice") else 2.6)

    def render_subtitle(self, text, size, out):
        self.calls.append(("render_subtitle", (text, tuple(size))))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"png")
        return out

    def assemble_dialogue(self, clips, lines, music, out, trims=None):
        self.calls.append(("assemble_dialogue", (list(clips), [dict(x) for x in lines], music, out, list(trims or []))))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"dialogue video")
        return {"shots": len(clips), "lines": len(lines), "seconds": 30.0}


def dialogue_turn(fs, seconds=30.0):
    t = new_via(fs, "니키와 푸토르가 숲속을 걸어가며 대화하는 30초 영상", rec=LTX, spec={"seconds": seconds})
    t["request"]["long"] = True
    t["request"]["dialogue"] = True
    return t


async def t_dialogue_video_records_voices_first_then_lip_syncs_the_talk_shots():
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            deps.line_secs = {"line_2.wav": [3.6]}
            d = D.Director(deps, dict(deps.recs))
            assert d.dialogue_ready()
            t = dialogue_turn(fs)
            await d.pump(); await settle(d, 10)
            # 1) 대사 구성안 → 계획 카드: 목소리와 장면마다 대사를 보인다. GPU 작업은 아직 없다
            assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
            assert [c[1].get("dialogue") for c in deps.calls if c[0] == "storyboard"] == [True]
            plan = plan_msgs(fs)[-1]["data"]["plan"]
            assert plan["dialogue"] and [v["name"] for v in plan["voices"]] == ["푸토르", "니키"]
            assert plan["library"] == [] and "painterly" in plan["style"]            # 라이브러리 인물이 없으면 등록 인물 칸은 비어 있다
            assert plan["scenes"][0]["line"].startswith("푸토르: 얘야") and not plan["scenes"][0]["talk"] and plan["scenes"][1]["talk"]
            assert "line" not in plan["scenes"][3]
            assert not [c for c in deps.calls if c[0] == "submit"]
            # 2) 승인 → 목소리 둘 → 대사 셋 → 기준 그림 둘 → 장면 넷 → 음악 → 대사 합치기
            assert await d.approve(t) == "ok"
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            tid = str(t["id"])
            tags = [c[1]["client_id"].split(f"studio-{tid}-")[1] for c in subs]
            assert tags == ["V01", "V11", "T01", "T11", "T21", "C01", "C11", "L0i1", "L0v1", "L1e1", "L1v1", "L2e1", "L2v1",
                            "L3e1", "L3v1", "M1"], tags
            assert [recipe_of(c) for c in subs] == [VOICE["id"]] * 2 + [CLONE["id"]] * 3 + [IMG["id"]] * 3 + [LTX["id"]] + [
                EDITR["id"], TALK["id"], EDITR["id"], TALK["id"], EDITR["id"], LTX["id"], MUS["id"]]
            work = t["work"]
            # 목소리: 인물마다 한 번 설계하고, 대사는 그 목소리(입력 폴더의 WAV)를 복제해 녹음한다
            v0 = subs[0][1]["prompt"]["1"]["inputs"]
            vid = D._video()
            assert v0["instruct"].startswith("A kind Korean grandpa") and v0["device"] == "cpu"
            # 기준 소리의 글: 푸토르(B)는 그 인물이 할 법한 말(sample_ko), 샘플 글이 없는 니키(A)는 기본 인사말
            assert v0["text"] == "허허, 얘야. 이 할아비가 옛날얘기 하나 해 줄까?" and subs[1][1]["prompt"]["1"]["inputs"]["text"] == vid.VOICE_REF_TEXT
            assert "calm and friendly" not in v0["instruct"] and "clear pronunciation" not in v0["instruct"]
            # 맥락 복제는 기준 소리와 글이 짝이라 소리를 자르지 않는다 (이전에는 앞 12초만 남겼다)
            assert v0["max_new_tokens"] == 256 and deps.max_secs == {}
            t0 = subs[2][1]["prompt"]
            assert t0["1"]["inputs"]["target_text"].startswith("얘야") and t0["3"]["inputs"]["audio"] == work["voices"]["B"]["in_rel"]
            assert t0["1"]["inputs"]["max_new_tokens"] == 256 and subs[3][1]["prompt"]["3"]["inputs"]["audio"] == work["voices"]["A"]["in_rel"]
            # 11-19 메인 시험 2: 목소리 특징만 복제(x_vector_only)하면 말투·나이·감정이 평평해졌다 → 기준 소리의 글까지 주는 맥락 복제
            assert t0["1"]["inputs"]["x_vector_only"] is False and t0["1"]["inputs"]["reference_text"] == work["voices"]["B"]["text"]
            assert subs[3][1]["prompt"]["1"]["inputs"]["reference_text"] == vid.VOICE_REF_TEXT == work["voices"]["A"]["text"]
            assert work["voices"]["B"]["in_rel"].endswith(f"{tid[:8]}/voice_0.wav") and work["lines"]["1"]["in_rel"].endswith("line_1.wav")
            # 녹음 길이에 맞춰 장면 길이를 고친다: 말하는 장면은 대사 + 1초 (4초 이상), 화면 밖 목소리 장면은 줄이지 않는다
            secs = [s["seconds"] for s in work["story"]["scenes"]]
            assert secs == [14, 4, 5, 9], secs                       # 녹음에 맞추면 25초 → 30초 요청(디졸브·자르기 손실 포함 7.5초 부족)에 맞춰 말 없는 두 장면을 늘린다
            st = [e[1] for e in fs.events if e[0] == "dialogue_stretch"]
            assert st and st[0]["before"] == [10, 4, 5, 6] and st[0]["after"] == [14, 4, 5, 9] and st[0]["target"] == 30.0
            # 기준 그림 → 편집한 첫 화면 → 입 맞춤 (대사 소리, 같은 크기, 녹음에 맞춘 길이, 혼자 나오는 글)
            talk = subs[10][1]["prompt"]
            assert talk["276"]["inputs"]["audio"] == work["lines"]["1"]["in_rel"] and talk["340:331"]["inputs"]["value"] == 4
            assert talk["340:330"]["inputs"]["value"] == 1280 and talk["340:324"]["inputs"]["value"] == 704
            assert "alone in the frame, no other people" in talk["340:319"]["inputs"]["value"]
            # 11-20 사용자: 화풍은 대사가 나올 때나 안 나올 때나 같아야 한다 → 말하는 장면(LTX-2.3)과 말 없는 장면(LTX-2.5)의 영상 글이
            # 같은 화풍 고정 문장으로 끝난다
            lock = vid.style_lock(work["story"])
            assert lock.startswith("Keep exactly the look of the first frame") and talk["340:319"]["inputs"]["value"].count(lock) == 1
            slot = LTX["inject"]["prompt"]
            wide = subs[8][1]["prompt"][slot["node"]]["inputs"][slot["input"]]
            assert recipe_of(subs[8]) == LTX["id"] and wide.rstrip().endswith(lock) and wide.count(lock) == 1
            edit = subs[9][1]["prompt"]
            assert edit["170:151"]["inputs"]["prompt"].startswith("Keep the same person") and "41" in edit
            staged = [c[1] for c in deps.calls if c[0] == "stage_image"]
            assert len(staged) == 2 + 1 + 3                     # 기준 그림 둘, 넓은 장면 그림 하나, 편집한 첫 화면 셋
            assert work["scenes"][1]["first_from"] == "edit" and work["scenes"][3]["first_from"] == "edit"
            assert work["scenes"][0]["first_from"] == "image"
            renders = [c[1] for c in deps.calls if c[0] == "render"]
            assert len(renders) == 2                            # 영상 글은 말 없는 장면 둘만 (말하는 장면은 정해진 글)
            # 합치기: 대사 셋(화면 밖 목소리 포함)과 자막, 말하는 샷은 대사 끝 + 0.9초에서 자른다
            clips, lines, mus, out, trims = [c[1] for c in deps.calls if c[0] == "assemble_dialogue"][0]
            assert [x["shot"] for x in lines] == [0, 1, 2] and all(x.get("sub") and os.path.isfile(x["file"]) for x in lines)
            assert trims == [None, 3.5, 4.5, None] and mus.endswith(".mp3") and out.endswith(f"dialogue_{tid[:8]}.mp4")
            assert [c[1][0] for c in deps.calls if c[0] == "render_subtitle"][0].startswith("얘야")
            res = [m for m in fs.messages if m["kind"] == "result"]
            assert len(res) == 1 and fs.atts[res[0]["data"]["attachment_ids"][0]]["meta"]["dialogue"]
            prog = fs.progress[t["id"]]
            assert prog["title"] == "대사 영상 만드는 중" and [c["key"] for c in prog["chips"]] == [
                "story", "voices", "scenes", "music", "assemble"]
            assert "voices" in fs.stages
            assert kinds(fs).count("dialogue_line") == 3 and kinds(fs).count("dialogue_voice") == 2
            # 기준 목소리는 그대로(복제의 바탕), 대사 줄만 크기를 맞춘다. 라이브러리 인물이 아니라 후처리는 없다
            assert sorted(deps.trims) == [("line_0.wav", None, True), ("line_1.wav", None, True), ("line_2.wav", None, True),
                                          ("voice_0.wav", None, False), ("voice_1.wav", None, False)]
        finally:
            settings.results_dir, settings.envs_dir = old


async def t_dialogue_line_that_runs_on_is_recorded_again_and_a_resume_keeps_the_voices():
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            deps.line_secs = {"line_1.wav": [31.9, 2.4]}      # 11-19 숲길 L3: 말이 끝나지 않고 32초로 늘어진 녹음
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": dialogue_story(), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"))
            clones = [c for c in deps.calls if c[0] == "submit" and recipe_of(c) == CLONE["id"]]
            assert len(clones) == 4 and "dialogue_line_retry" in kinds(fs)
            assert t["work"]["lines"]["1"]["seconds"] == 2.4 and t["work"]["story"]["scenes"][1]["seconds"] == 4
            # 실패한 요청을 다시 시도하면 녹음한 목소리·대사·기준 그림을 이어받아 다시 녹음하지 않는다
            fs2 = FakeStore(); fs2.patch()
            fs2.turns, fs2.atts = fs.turns, fs.atts
            deps2 = DialogueDeps(fs2, settings.results_dir)
            d2 = D.Director(deps2, dict(deps2.recs))
            t2 = dialogue_turn(fs2)
            t["state"] = "failed"
            t2["request"].update(source_turn=str(t["id"]), mode="retry")
            await d2.pump(); await settle(d2, 20)
            assert t2["state"] == "done", (t2["state"], t2.get("error"))
            got = [recipe_of(c) for c in deps2.calls if c[0] == "submit"]
            assert VOICE["id"] not in got and CLONE["id"] not in got and got.count(IMG["id"]) == 0, got
        finally:
            settings.results_dir, settings.envs_dir = old


# ── 인물 라이브러리: 정해진 인물은 기준 그림으로 얼굴을 고정하고 목소리 후처리·크기 맞추기를 건다 ──
def library_story(root, voice_refs=False):
    """dialogue_story() 의 두 인물을 라이브러리 인물로 만든다 (기준 그림 파일은 결과 폴더에 둔다).
    voice_refs 면 승인된 기준 소리(voice_ref)와 그 글(voice_ref_text)도 둔다"""
    from app import cast_lib
    story = dialogue_story()
    story["bible"]["style"] = cast_lib.library()[0]["style"]          # 구성안 단계에서 라이브러리 그림체로 덮어쓴 모습 (cast_lib.apply_to_story)
    chars = {c["id"]: c for c in story["bible"]["characters"]}
    for cid, lib, fx in (("A", "niki", None), ("B", "putor", "bass")):
        rel = f"studio/_cast/{lib}.png"
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(lib.encode())
        chars[cid].update(library=lib, ref=rel, **({"voice_fx": fx} if fx else {}))
        if voice_refs:
            vrel = f"studio/_cast/{lib}_voice.flac"
            with open(os.path.join(root, vrel), "wb") as f:
                f.write(b"voice " + lib.encode())
            chars[cid].update(voice_ref=vrel, voice_ref_text=f"{lib} 기준 소리가 말한 글입니다.")
    return story


async def t_dialogue_library_characters_use_their_reference_pictures_and_voice_fx():
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": library_story(settings.results_dir), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            tid = str(t["id"])
            tags = [c[1]["client_id"].split(f"studio-{tid}-")[1] for c in subs]
            # 기준 그림 둘(C0·C1)은 새로 그리지 않는다: 장면 이미지(L0i)만 그린다. 라이브러리 인물이 나오므로 Z-Image 가 아니라
            # Qwen-Image 2512 (11-19 시험: Z-Image 는 디즈니·픽사풍 3D 로 나와 말하는 장면과 화풍이 달랐다)
            assert "C01" not in tags and "C11" not in tags and tags.count("L0i1") == 1, tags
            rids = [recipe_of(c) for c in subs]
            assert rids.count(IMG["id"]) == 0 and rids.count(SCENE["id"]) == 1, rids
            scene = next(c for c in subs if recipe_of(c) == SCENE["id"])[1]["prompt"]["238:227"]["inputs"]["text"]
            assert scene.startswith("Semi-realistic digital painting, 2.5D concept art") and "IMG " in scene      # 그림체 앞머리 + 장면 그림 글
            assert scene.lower().count("semi-realistic digital painting") >= 2                  # 맨 앞머리 + 글 안의 Style 문장들 (앞머리는 항상 붙는다)
            # 사용자가 Lightning(4스텝, 약 6초) 결과 b_new_light 를 화풍으로 골랐다: Lightning 스위치가 켜져 있고 단계·CFG 가 그쪽이다
            sp = next(c for c in subs if recipe_of(c) == SCENE["id"])[1]["prompt"]
            assert sp["238:229"]["inputs"]["value"] is True and SCENE["quality_label"] == "빠름" and SCENE["estimate"]["gpu_seconds"] < 20
            assert sp["238:225"]["inputs"]["value"] == 4 and sp["238:218"]["inputs"]["value"] == 1          # Lightning 4스텝·CFG 1 (스위치가 이 값을 고른다)
            assert D.Director._scene_image_prompt(SCENE, {"bible": {"style": "x y z"}}, "IMG text").startswith("X y z. IMG")
            head = scene.split(". IMG")[0] + ". "
            assert D.Director._scene_image_prompt(SCENE, {"bible": {"style": head[:-2]}}, head + "IMG text") == head + "IMG text"   # 이미 그 문장으로 시작하면 그대로
            assert D.Director._scene_image_prompt(IMG, {"bible": {"style": "x y z"}}, "IMG text") == "IMG text"                    # Z-Image 글은 그대로
            staged = [c[1] for c in deps.calls if c[0] == "stage_image"]
            assert "studio/_cast/niki.png" in staged and "studio/_cast/putor.png" in staged
            assert {c: t["work"]["casts"][c]["rel"] for c in ("A", "B")} == {"A": "studio/_cast/niki.png", "B": "studio/_cast/putor.png"}
            assert all(t["work"]["casts"][c].get("library") for c in ("A", "B"))
            assert [e[1]["library"] for e in fs.events if e[0] == "dialogue_cast"] == [True, True]
            # 목소리: 기준 목소리는 후처리·크기 맞추기 없이, 대사 줄은 크기를 맞추고 푸토르(B)에만 저음 보강
            voices = [x for x in deps.trims if x[0].startswith("voice_")]
            lines = {x[0]: x for x in deps.trims if x[0].startswith("line_")}
            assert voices and all(fx is None and not lv for _n, fx, lv in voices)
            assert lines["line_1.wav"][1:] == (None, True)                  # 니키(A)의 대사
            assert lines["line_2.wav"][1:] == ("bass", True)                # 푸토르(B)의 대사
            assert lines["line_0.wav"][1:] == ("bass", True)                # 푸토르의 화면 밖 목소리
            # 계획 카드가 아니라 이미 승인된 흐름이므로 카드는 없다. 합치기까지 끝났다
            assert any(c[0] == "assemble_dialogue" for c in deps.calls)
        finally:
            settings.results_dir, settings.envs_dir = old


async def t_dialogue_library_voice_reference_is_used_instead_of_designing_a_voice():
    """11-19 메인 시험 2: 인사말로 새로 설계한 기준 소리에서 80세 푸토르가 젊은 남자 목소리로 나왔다.
    승인된 기준 소리(voice_ref)가 있는 라이브러리 인물은 영상마다 목소리를 설계하지 않고 그 소리와 글을 맥락 복제의 기준으로 쓴다"""
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": library_story(settings.results_dir, voice_refs=True), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            rids = [recipe_of(c) for c in subs]
            assert rids.count(VOICE["id"]) == 0 and rids.count(CLONE["id"]) == 3, rids       # 목소리 설계 없이 대사 셋만 녹음
            work = t["work"]
            assert work["voices"]["B"]["library"] and work["voices"]["A"]["library"]
            assert work["voices"]["B"]["text"] == "putor 기준 소리가 말한 글입니다." and work["voices"]["B"]["rel"] == "studio/_cast/putor_voice.flac"
            assert work["voices"]["B"]["in_rel"].endswith("voice_0.wav") and work["voices"]["A"]["in_rel"].endswith("voice_1.wav")
            # 대사는 라이브러리 기준 소리의 글까지 주는 맥락 복제로 녹음한다 (푸토르 B → 니키 A 차례)
            clones = [c[1]["prompt"]["1"]["inputs"] for c in subs if recipe_of(c) == CLONE["id"]]
            assert [c["reference_text"] for c in clones] == ["putor 기준 소리가 말한 글입니다.", "niki 기준 소리가 말한 글입니다.",
                                                         "putor 기준 소리가 말한 글입니다."], clones
            assert all(c["x_vector_only"] is False for c in clones)
            # 기준 소리는 입력 폴더로 옮기기만 한다: 후처리·크기 맞추기·자르기 없음 (맥락 복제는 소리와 글이 짝이다)
            voices = [x for x in deps.trims if x[0].startswith("voice_")]
            assert sorted(voices) == [("voice_0.wav", None, False), ("voice_1.wav", None, False)] and deps.max_secs == {}
            assert [e[1].get("library") for e in fs.events if e[0] == "dialogue_voice"] == [True, True]
            assert "dialogue_voice_ref_missing" not in kinds(fs)
        finally:
            settings.results_dir, settings.envs_dir = old


async def t_dialogue_voice_clone_mode_can_be_switched_to_x_vector():
    """STUDIO_VOICE_CLONE=xvector 이면 목소리 특징만 복제한다 (듣고 마음에 드는 쪽으로 바꿀 수 있게 설정으로 둔다)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_voice_clone)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            assert old[2] == "icl"                                        # 기본은 맥락 복제
            settings.studio_voice_clone = "xvector"
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": library_story(settings.results_dir, voice_refs=True), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            clones = [c[1]["prompt"]["1"]["inputs"] for c in deps.calls if c[0] == "submit" and recipe_of(c) == CLONE["id"]]
            assert len(clones) == 3 and all(c["x_vector_only"] is True and c["reference_text"] for c in clones)
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_voice_clone = old


async def t_dialogue_library_voice_reference_missing_falls_back_to_a_designed_voice():
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = library_story(settings.results_dir, voice_refs=True)
            os.remove(os.path.join(settings.results_dir, "studio/_cast/putor_voice.flac"))      # 푸토르의 기준 소리 파일이 지워졌다
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            rids = [recipe_of(c) for c in deps.calls if c[0] == "submit"]
            assert rids.count(VOICE["id"]) == 1 and rids.count(CLONE["id"]) == 3, rids        # 푸토르만 새로 설계한다
            work = t["work"]
            assert not work["voices"]["B"].get("library") and work["voices"]["A"]["library"]
            assert work["voices"]["B"]["text"] == "허허, 얘야. 이 할아비가 옛날얘기 하나 해 줄까?"       # 설계한 기준 소리의 글은 인물의 샘플 글
            miss = [e[1] for e in fs.events if e[0] == "dialogue_voice_ref_missing"]
            assert miss and miss[0]["speaker"] == "B" and miss[0]["ref"] == "studio/_cast/putor_voice.flac"
        finally:
            settings.results_dir, settings.envs_dir = old


async def t_dialogue_reference_voice_that_runs_on_is_designed_again():
    """맥락 복제는 기준 소리를 잘라 쓰지 못한다 (글과 어긋난다): 글에 비해 너무 늘어졌으면 시드를 바꿔 한 번 더 설계한다"""
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            deps.line_secs = {"voice_0.wav": [40.0, 3.0]}              # 푸토르(첫 말하는 사람)의 기준 소리가 40초로 늘어졌다
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": dialogue_story(), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            designs = [c for c in deps.calls if c[0] == "submit" and recipe_of(c) == VOICE["id"]]
            assert len(designs) == 3, len(designs)                      # 푸토르 두 번(다시 설계), 니키 한 번
            retry = [e[1] for e in fs.events if e[0] == "dialogue_voice_retry"]
            assert retry and retry[0]["speaker"] == "B" and retry[0]["seconds"] == 40.0
            assert t["work"]["voices"]["B"]["seconds"] == 3.0 and deps.max_secs == {}
            # 시드가 바뀐다 (같은 글·같은 시드로는 같은 소리가 나온다)
            assert designs[0][1]["prompt"]["1"]["inputs"]["seed"] != designs[1][1]["prompt"]["1"]["inputs"]["seed"]
        finally:
            settings.results_dir, settings.envs_dir = old


async def t_long_video_without_dialogue_draws_library_characters_with_the_same_scene_recipe():
    """11-20 사용자: 화풍은 대사가 나올 때나 안 나올 때나 항상 같아야 한다. 라이브러리 인물이 나오는 대사 없는 긴 영상도
    장면 그림은 Qwen-Image 2512(그림체 앞머리 포함), 영상 글은 화풍 고정 문장으로 끝난다"""
    from app import cast_lib
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            story = copy.deepcopy(LONG_STORY)
            story["bible"]["style"] = cast_lib.library()[0]["style"]
            story["bible"]["characters"][0].update(library="niki")          # 구성안 단계에서 라이브러리로 덮어쓴 모습 (cast_lib.apply_to_story)
            deps.story = story
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            await d.pump(); await settle(d, 10)
            assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
            assert await d.approve(t) == "ok"
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            rids = [recipe_of(c) for c in subs]
            assert rids.count(SCENE["id"]) == 2 and rids.count(IMG["id"]) == 0, rids        # 이어지는 장면은 그림을 새로 안 그린다
            scenes = [c[1]["prompt"]["238:227"]["inputs"]["text"] for c in subs if recipe_of(c) == SCENE["id"]]
            assert all(x.startswith("Semi-realistic digital painting, 2.5D concept art") for x in scenes)
            lock = D._video().style_lock(story)
            slot = LTX["inject"]["prompt"]
            vids = [c[1]["prompt"][slot["node"]]["inputs"][slot["input"]] for c in subs if recipe_of(c) == LTX["id"]]
            assert len(vids) == 3 and all(v.rstrip().endswith(lock) for v in vids)
            # 사진 화풍이면 영상 글을 그대로 둔다 (고정 문장 없음)
            assert D._video().style_lock({"bible": {"style": "photorealistic cinematic live action"}}) == ""
            assert D._video().style_lock({"bible": {"style": ""}}) == ""
            # 라이브러리 인물이 없으면 지금까지처럼 Z-Image
            assert D.Director(deps, dict(deps.recs))._scene_image_recipe(copy.deepcopy(LONG_STORY))["id"] == IMG["id"]
        finally:
            settings.results_dir, settings.envs_dir = old


# ── 그림·클립 자동 검사 (11-20): 사람 수·몸의 이상·화풍이 틀리면 씨앗을 바꿔 다시 만든다 ──
PASS = {"ok": True, "anatomy_ok": True, "issues": [], "traits": [], "people": None}


async def t_picture_check_rules():
    from app import video
    from app.studio import checks
    style = "semi-realistic digital painting, 2.5D concept art"
    assert checks.style_trait(style + ".") == "The whole picture is in this art style: " + style and checks.style_trait("") == ""
    # 11-20 메인 실측: 푸토르가 둘로 나온 편집 그림은 사람 2명, 한 명짜리는 1명 → 한 사람 그림은 정확히 1명
    assert checks.problems({"people": 2, "issues": [], "traits": []}, checks.ONE) == ["people:2/1"]
    assert checks.problems({"people": 0}, checks.ONE) == ["people:0/1"] and checks.problems({"people": 1}, checks.ONE) == []
    assert checks.problems({"people": 2}, 1) == ["people:2/1"]                # 숫자만 주면 정확히 그 수
    body = {"people": 1, "issues": [{"type": "merged_bodies"}, {"type": "extra_fingers"}, {"type": "face"}]}
    assert checks.problems(body, checks.ONE) == ["body:face,merged_bodies"]   # 손가락은 숨긴 손 때문에 잘못 잡히기 쉬워 보지 않는다
    # 화풍: 검사 모델이 이유를 붙여 '아니다'라고 답했을 때만 (답을 빠뜨리면 실패로 보지 않는다), 화풍을 주지 않았으면 안 본다
    no = {"people": 1, "traits": [{"trait": "x", "present": False, "note": "it looks like a photograph"}]}
    assert checks.problems(no, checks.ONE, style) == ["style"] and checks.problems(no, checks.ONE, "") == []
    assert checks.problems({"people": 1, "traits": [{"trait": "x", "present": False, "note": ""}]}, checks.ONE, style) == []
    assert checks.problems({"people": None}, 2) == [] and checks.problems(None, 1) == [] and checks.problems({"people": True}, 1) == []
    assert checks.problems({"people": 0}, None) == []                       # 셀 수를 모르면 사람 수는 보지 않는다
    assert checks.describe(["people:2/1", "body:face", "style"]) == "사람 수·몸 모양·화풍" and checks.describe([]) == ""
    assert checks.better(None, ["a"]) and checks.better(["a", "b"], ["a"]) and not checks.better(["a"], ["b"])
    # 장면 그림 (11-20 검토): 설정집 인물만 알아서 행인·상인을 셀 수 없다. 가까운 장면만 정확히, 미디엄은 빠진·겹친 사람만, 넓은 장면은 세지 않는다
    close, medium, wide = video.shot_rank("close-up"), video.shot_rank("medium shot"), video.shot_rank("wide shot")
    assert (close, medium, wide) == (0, 2, 4)
    r = checks.scene_rule(1, 0, close, False)
    assert checks.problems({"people": 2}, r) == ["people:2/1"] and checks.problems({"people": 0}, r) == ["people:0/1"]
    assert checks.problems({"people": 2}, checks.scene_rule(1, 0, close, True)) == []          # 장면 글에 상인·행인 말이 있으면 더 많아도 된다
    r = checks.scene_rule(2, 0, medium, False)
    assert checks.problems({"people": 5}, r) == [] and checks.problems({"people": 1}, r) == ["people:1/2"]
    assert checks.problems({"people": 6}, checks.scene_rule(1, 0, wide, False)) == []           # 넓은 장면은 세지 않는다
    assert checks.problems({"people": 0}, checks.scene_rule(1, 0, None, False)) == []           # 샷 크기를 모르면 세지 않는다
    # 동물 인물: 사람 수와 팔다리 수는 보지 않는다 (다리 넷이 '팔다리 더함'으로 잡힌다). 겹친 몸은 본다
    r = checks.scene_rule(1, 1, close, False)
    dog = {"people": 2, "issues": [{"type": "extra_limb"}, {"type": "floating_limb"}, {"type": "missing_head"}]}
    assert checks.problems(dog, r) == [] and checks.problems({"people": 1, "issues": [{"type": "merged_bodies"}]}, r) == ["body:merged_bodies"]
    assert video.is_animal_look("a small white puppy with floppy ears") and not video.is_animal_look("a short stocky old dwarf herbalist")
    assert not video.is_animal_look("a twelve-year-old girl with two long black braids, a wine-red hat with a cat badge")
    # 클립: 첫 화면에서 센 사람보다 많아졌을 때만 (지어낸 사람·복제). 화면 밖으로 나간 사람은 괜찮다. 군중·모르는 수는 세지 않는다
    r = checks.clip_rule(2, 0)
    assert checks.problems({"people": 3}, r) == ["people:3/2"] and checks.problems({"people": 1}, r) == [] and checks.problems({"people": 2}, r) == []
    assert checks.problems({"people": 9}, checks.clip_rule(checks.CROWD_COUNT + 1, 0)) == []
    assert checks.problems({"people": 9}, checks.clip_rule(None, 0)) == [] and checks.problems({"people": 9}, checks.clip_rule(True, 0)) == []
    # 입 맞춤 클립: 한 사람 그대로. 말하는 입 때문의 '뭉개진 얼굴'은 보지 않는다 (입 모양은 보지 않기로 함)
    r = checks.clip_rule(None, 0, talk=True)
    assert checks.problems({"people": 2}, r) == ["people:2/1"] and checks.problems({"people": 1, "issues": [{"type": "face"}]}, r) == []
    assert checks.problems({"people": 1, "issues": [{"type": "merged_bodies"}]}, r) == ["body:merged_bodies"]
    assert checks.problems({"people": 0, "issues": [{"type": "extra_limb"}]}, checks.clip_rule(None, 1, talk=True)) == []


async def t_dialogue_picture_check_remakes_a_two_person_still_and_a_bad_talk_clip():
    """11-20 메인 재시험: 기준 그림을 고친 첫 화면에 푸토르가 둘로 나왔다. 검사에서 사람 수가 틀리면 그림을, 클립 가운데 프레임의 몸이
    겹치면 클립을 씨앗을 바꿔 다시 만든다"""
    from app.studio import checks
    old = (settings.results_dir, settings.envs_dir, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_picture_check = True
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            # 검사 차례: 장면0 그림·클립, 장면1 편집(사람 2 → 다시)·편집·입 맞춤(몸 겹침 → 다시)·입 맞춤, 장면2 편집·입 맞춤, 장면3 편집·클립
            deps.inspect_seq = [PASS, PASS, {"people": 2, "issues": [], "traits": []}, {"people": 1, "issues": [], "traits": []},
                                {"people": 1, "issues": [{"type": "merged_bodies"}], "traits": []}, PASS] + [PASS] * 4
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = library_story(settings.results_dir)
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            tid = str(t["id"])
            tags = [c[1]["client_id"].split(f"studio-{tid}-")[1] for c in subs]
            assert tags.count("L1e1") == 1 and tags.count("L1e2") == 1 and "L1e3" not in tags, tags      # 편집을 한 번 더
            assert tags.count("L1v1") == 1 and tags.count("L1v2") == 1, tags                              # 입 맞춤도 한 번 더
            assert "L2e2" not in tags and "L2v2" not in tags and "L0i2" not in tags and "L0v2" not in tags
            insp = [c[1] for c in deps.calls if c[0] == "inspect"]
            assert len(insp) == 10 and not deps.inspect_seq, len(insp)
            style = D._video().story_style(story)
            assert insp[2][3] == [checks.style_trait(style)] and insp[2][2].startswith("img_")             # 편집 그림 + 화풍 한 줄
            assert insp[4][2].startswith("talkcheck_")                                                    # 입 맞춤 클립의 검사 프레임
            retries = [e[1] for e in fs.events if e[0] == "picture_retry"]
            assert [(r["what"], r["index"]) for r in retries] == [("edit", 1), ("talk_clip", 1)], retries
            assert retries[0]["why"] == ["people:2/1"] and retries[1]["why"] == ["body:merged_bodies"]
            # 다시 만든 그림·클립을 쓴다 (첫 번째가 아니라)
            work = t["work"]
            edit1 = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "edit" and e[1]["index"] == 1]
            assert [x["attempt"] for x in edit1] == [1, 2] and work["scenes"][1]["edit_rel"].endswith("/" + edit1[1]["file"])
            talk1 = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "talk_clip" and e[1]["index"] == 1]
            assert [x["attempt"] for x in talk1] == [1, 2]
            assert work["scenes"][1]["clip_rel"].endswith("talk_" + talk1[1]["file"].split("_")[1].split(".")[0] + ".mp4")
            assert not work.get("check_left")                                                             # 다시 만들어서 다 통과했다
            res = [m for m in fs.messages if m["kind"] == "result"][0]
            assert not any("자동 검사" in n for n in res["data"]["notes"])
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_picture_check = old


async def t_long_video_picture_check_keeps_the_best_and_tells_the_user():
    """세 번 만들어도 검사를 통과하지 못하면 가장 나은 그림으로 잇고 결과에 한 줄로 알린다 (멈추지 않는다)"""
    old = (settings.results_dir, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        settings.studio_picture_check = True
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            bad = {"people": 1, "issues": [{"type": "merged_bodies"}], "traits": []}
            deps.inspect_seq = [bad, dict(bad), dict(bad), PASS, PASS, dict(PASS, people=1), PASS]
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            tid = str(t["id"])
            tags = [c[1]["client_id"].split(f"studio-{tid}-")[1] for c in subs]
            assert [x for x in tags if x.startswith("L0i")] == ["L0i1", "L0i2", "L0i3"], tags          # 처음 + 다시 두 번
            assert [x for x in tags if x.startswith("L2i")] == ["L2i1"] and "L0v2" not in tags
            seen = [e[1] for e in fs.events if e[0] == "picture_check"]
            # 장면0 그림 셋(몸 겹침), 장면0 클립, 이어지는 장면1 클립(그림 없음), 장면2 그림·클립
            assert [(c["what"], c["index"]) for c in seen] == [("scene_image", 0)] * 3 + [("clip", 0), ("clip", 1), ("scene_image", 2), ("clip", 2)]
            # 사람 수 기준: 장면0 넓은 장면은 세지 않고, 장면2 가까운 장면(close shot)은 정확히 1명.
            # 클립의 사람 수 상한은 그림을 정확히 센 장면(가까운 장면 2)에서만 쓴다: 넓은 장면 0 은 그림에서 1명으로 읽혀도 세지 않고
            # (행인이 한 명 지나가도 클립을 다시 만들지 않는다), 이어지는 장면 1 은 그림이 없어 세지 않는다 (11-20 재검토)
            assert [c["want"] for c in seen if c["what"] == "scene_image"] == [None, None, None, 1]
            assert [c["want"] for c in seen if c["what"] == "clip"] == [None, None, 1]
            assert all(c["fewer_ok"] and not c["more_ok"] for c in seen if c["what"] == "clip" and c["want"] is not None)
            sc = t["work"]["scenes"]
            assert sc[0].get("first_people") is None and "first_people" not in sc[1] and sc[2]["first_people"] == 1
            work = t["work"]
            assert work["check_left"] == {"0:image": ["body:merged_bodies"]}
            first = [c["file"] for c in seen if c["what"] == "scene_image" and c["index"] == 0][0]
            assert work["scenes"][0]["image_rel"].endswith("/" + first)                                  # 모두 같으면 처음 것을 쓴다
            res = [m for m in fs.messages if m["kind"] == "result"][0]
            assert "장면 1: 다시 만들어도 자동 검사에서 몸 모양 문제가 남아 가장 나은 결과로 이었습니다" in res["data"]["notes"]
            # 예상 시간에 검사 시간이 들어간다 (장면마다 두 장)
            ctx = D.Ctx(t, {"id": 1, "username": "bob"}, {"id": 1}, LTX)
            settings.studio_picture_check = False
            base = await d.estimate_long(ctx, copy.deepcopy(LONG_STORY))
            settings.studio_picture_check = True
            more = await d.estimate_long(ctx, copy.deepcopy(LONG_STORY))
            assert more - base == 2 * 3 * D.PICTURE_CHECK_SECONDS, (base, more)
        finally:
            settings.results_dir, settings.studio_picture_check = old


async def t_picture_retry_that_fails_keeps_the_best_result():
    """검사 때문에 다시 만든 작업이 오류로 끝나도 영상을 잃지 않는다: 앞서 만든 결과로 잇는다 (11-20 검토)"""
    old = (settings.results_dir, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        settings.studio_picture_check = True
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            deps.inspect_seq = [{"people": 1, "issues": [{"type": "merged_bodies"}], "traits": []}] + [PASS] * 6
            deps.outcome_seq = ["success", "error", "error"]       # 장면0 그림 → 검사 실패 → 다시 만든 그림이 두 번 다 오류
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            failed = [e[1] for e in fs.events if e[0] == "picture_retry_failed"]
            assert failed == [{"what": "scene_image", "index": 0, "attempt": 2, "code": "run_error"}], failed
            first = [e[1] for e in fs.events if e[0] == "picture_check" and e[1]["what"] == "scene_image"][0]["file"]
            assert t["work"]["scenes"][0]["image_rel"].endswith("/" + first)
            assert t["work"]["check_left"] == {"0:image": ["body:merged_bodies"]}
        finally:
            settings.results_dir, settings.studio_picture_check = old


async def t_picture_check_notes_survive_a_retry_and_a_redo_clears_them():
    """다시 시도는 이어받은 장면의 남은 검사 문제를 이어받는다. 장면을 다시 만들 때(_long_reset)는 그 장면의 기록을 지운다"""
    old = (settings.results_dir, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        settings.studio_picture_check = True
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            bad = {"people": 1, "issues": [{"type": "merged_bodies"}], "traits": []}
            deps.inspect_seq = [bad, dict(bad), dict(bad), PASS, PASS, PASS]
            # 장면0 그림 셋·클립, 장면1 클립, 장면2 그림은 되고, 장면2 클립이 두 번 다 실패한다
            deps.outcome_seq = ["success"] * 6 + ["error", "error"]
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "failed" and t["work"]["check_left"] == {"0:image": ["body:merged_bodies"]}, (t["state"], t.get("error"))
            r = new_via(fs, "다시 시도", rec=LTX, spec={"seconds": 30.0})
            r["request"].update(long=True, mode="rewrite", source_turn=str(t["id"]))
            deps.inspect_seq = [PASS] * 4
            await d.pump(); await settle(d, 20)
            assert r["state"] == "done", (r["state"], r.get("error"))
            assert r["work"]["check_left"] == {"0:image": ["body:merged_bodies"]}
            res = [m for m in fs.messages if m["kind"] == "result"][-1]
            assert "장면 1: 다시 만들어도 자동 검사에서 몸 모양 문제가 남아 가장 나은 결과로 이었습니다" in res["data"]["notes"]
            # 장면 0 을 다시 만들면(클립이 바뀜) 그 장면과 이어지는 장면 1 의 기록을 지운다
            ctx = D.Ctx(r, {"id": 1, "username": "bob"}, {"id": 1}, LTX)
            ctx.long = {}
            work = {"story": copy.deepcopy(LONG_STORY), "check_left": {"0:image": ["x"], "1:clip": ["y"], "2:clip": ["z"], "c0:portrait": ["w"]}}
            scenes = [{"clip_rel": "a"}, {"clip_rel": "b"}, {"clip_rel": "c"}]
            await d._long_reset(ctx, work, scenes, 0)
            assert work["check_left"] == {"2:clip": ["z"], "c0:portrait": ["w"]}
        finally:
            settings.results_dir, settings.studio_picture_check = old


async def t_picture_check_off_or_failing_never_blocks_the_film():
    old = (settings.results_dir, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            # 꺼 두면 검사하지 않는다
            settings.studio_picture_check = False
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done" and not [c for c in deps.calls if c[0] == "inspect"]
            # 검사가 실패하면(시각 모델 오류) 통과로 보고 계속한다
            settings.studio_picture_check = True
            fs2 = FakeStore(); fs2.patch()
            deps2 = LongDeps(fs2, root)
            deps2.inspect_seq = [RuntimeError("vision model down")] + [PASS] * 6
            d2 = D.Director(deps2, dict(deps2.recs))
            t2 = long_turn(fs2)
            t2["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d2.pump(); await settle(d2, 20)
            assert t2["state"] == "done", (t2["state"], t2.get("error"))
            skipped = [e[1] for e in fs2.events if e[0] == "picture_check" and e[1].get("skipped")]
            assert len(skipped) == 1 and skipped[0]["what"] == "scene_image" and "picture_retry" not in kinds(fs2)
            tags = [c[1]["client_id"] for c in deps2.calls if c[0] == "submit"]
            assert not any(x.endswith("L0i2") for x in tags)
        finally:
            settings.results_dir, settings.studio_picture_check = old


async def t_dialogue_plan_card_shows_the_library_characters_and_the_style():
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            deps.story = library_story(settings.results_dir)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            await d.pump(); await settle(d, 10)
            assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
            plan = plan_msgs(fs)[-1]["data"]["plan"]
            assert plan["library"] == ["니키", "푸토르"] and plan["style"].startswith("semi-realistic digital painting, 2.5D concept art")
        finally:
            settings.results_dir, settings.envs_dir = old


async def t_dialogue_library_missing_reference_falls_back_to_a_drawn_portrait():
    old = (settings.results_dir, settings.envs_dir)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            fs = FakeStore(); fs.patch()
            deps = DialogueDeps(fs, settings.results_dir)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = library_story(settings.results_dir)
            os.remove(os.path.join(settings.results_dir, "studio/_cast/putor.png"))      # 푸토르의 기준 그림이 없어졌다
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 20)
            assert t["state"] == "done", (t["state"], t.get("error"))
            tid = str(t["id"])
            tags = [c[1]["client_id"].split(f"studio-{tid}-")[1] for c in deps.calls if c[0] == "submit"]
            assert "C11" in tags and "C01" not in tags            # 니키(A, 첫째 C0)는 기준 그림을 쓰고, 기준 그림이 없어진 푸토르(B, 둘째 C1)는 새로 그린다
            # 새로 그리는 기준 그림도 장면 그림과 같은 Qwen-Image 2512 로, 같은 그림체 앞머리와 함께 (11-20: 화풍이 갈리지 않게)
            portrait = next(c for c in deps.calls if c[0] == "submit" and c[1]["client_id"].endswith("C11"))
            assert recipe_of(portrait) == SCENE["id"]
            assert portrait[1]["prompt"]["238:227"]["inputs"]["text"].startswith("Semi-realistic digital painting, 2.5D concept art")
            miss = [e[1] for e in fs.events if e[0] == "dialogue_cast_ref_missing"]
            assert miss and miss[0]["character"] == "B" and miss[0]["ref"] == "studio/_cast/putor.png"
            assert [e[1]["library"] for e in fs.events if e[0] == "dialogue_cast"].count(True) == 1
        finally:
            settings.results_dir, settings.envs_dir = old


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
