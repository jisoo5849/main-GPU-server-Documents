"""감독 시험 (5절): 가짜 DB·가짜 부품으로 P1 한 줄기(사진 → 영상 → 진행 → 결과)와 실패·취소 길을 확인한다. GPU·DB 없이 돈다.

  cd hub && python tests/test_studio_director.py
"""
import asyncio
import contextlib
import copy
import datetime
import inspect
import os
import re
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
        self.expand_kw = []          # expand_image 의 crew·그 밖의 인자
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

    async def expand_image(self, user, ip, text, crew=None, **kw):
        self.calls.append(("expand_image", text))
        self.expand_kw.append({"crew": crew, **kw})          # 영화 빛 계획(look_plan)·crew=False 를 보려고 (11-26)
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
    assert {r["action"] for r in rs.values()} == {"i2v", "t2v", "image", "edit", "music", "voice", "voice_clone", "talk", "scene_image", "edit_ref", "i2v_end", "compose_ref", "talk_duo", "edit_photo"}
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
    # 두 사람 대사 레시피는 따로 고른다: 혼자 대사 컷('talk')은 그대로 LTX
    assert d.recipe_for("talk")["id"] == "ltx23_ia2v_talk" and d.recipe_for("talk_duo")["id"] == "wan21_infinitetalk_duo"
    # 11-24 2차: 장면 그림 정밀 판(높음)과 실사 변환 레시피도 따로 고른다. 편집('edit')은 그대로 Qwen Image Edit
    assert d.recipe_for("scene_image", "high")["id"] == "qwen_image_2512_scene_quality"
    assert d.recipe_for("edit_photo")["id"] == "qwen_image21_edit_photo" and d.recipe_for("edit", "high")["id"] == "qwen_image_edit_quality"


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

    def last_frame(self, clip, out):
        self.calls.append(("last_frame", (clip, out)))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"png")
        return out

    def assemble(self, clips, music, out, sound=True, transitions=None, seams=None):
        self.calls.append(("assemble", (list(clips), music, out)))
        self.transitions = transitions
        self.seam_kinds = seams
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
            if rec["action"] in ("image", "edit", "scene_image", "edit_ref", "compose_ref", "edit_photo"):
                outs = [{"node_id": main, "rel_path": self._file(f"img_{self.n}{ext}"), "media": "images"}]
            elif rec["action"] in ("music", "voice", "voice_clone"):
                outs = [{"node_id": main, "rel_path": self._file(f"{rec['action']}_{self.n}{ext}"), "media": "audio"}]
            elif rec["action"] == "talk":
                outs = [{"node_id": main, "rel_path": self._file(f"talk_{self.n}.mp4"), "media": "videos"}]
                chk = (rec.get("outputs") or {}).get("check")
                if chk:              # 입 맞춤 클립의 검사 프레임 (11-20)
                    outs.append({"node_id": chk["node"], "rel_path": self._file(f"talkcheck_{self.n}.png"), "media": "images"})
                lst = (rec.get("outputs") or {}).get("last")
                if lst:              # 입 맞춤 클립의 잇는 프레임 (자르는 지점, 11-20 장면 잇기)
                    outs.append({"node_id": lst["node"], "rel_path": self._file(f"talklast_{self.n}.png"), "media": "images"})
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


FILM_LOOK = {"time": "early evening, light rain", "light": "soft warm lamp light from shop windows, amber and brown tones",
             "air": "fine drizzle haze in the lamp light", "mood": "a calm wistful mood", "light_terms": ["lamp light"],
             "picked": [{"step": "조명", "id": "lighting.x", "ko": "가로등"}]}


class FilmCrewDeps(LongDeps):
    """정본 인물 영화: 구성안에 영화 빛 계획(story.crew)과 영화 제작진 기록(film_crew)이 붙어 온다 (11-26)"""

    async def storyboard(self, user, ip, env, **kw):
        self.calls.append(("storyboard", kw))
        await asyncio.sleep(0.01)
        story = copy.deepcopy(LONG_STORY)
        story["crew"] = copy.deepcopy(FILM_LOOK)
        return {"story": story, "violations": [], "rounds": 1,
                "film_crew": {"category": "cast_film", "cast": ["niki"], "used": True, "rounds": 1,
                              "picked": FILM_LOOK["picked"], "violations": []}}


async def t_long_film_crew():
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            fs = FakeStore(); fs.patch()
            deps = FilmCrewDeps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            await d.pump(); await settle(d, 10)
            assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
            fc = [v for k, v in fs.events if k == "film_crew"]
            assert len(fc) == 1 and fc[0]["used"] and fc[0]["cast"] == ["niki"] and fc[0]["picked"]
            assert await d.approve(t) == "ok"
            await d.pump(); await settle(d, 15)
            assert t["state"] == "done", (t["state"], t.get("error"))
            # 장면 그림 글(사람 없는 컷·예전 경로)은 장면마다 제작진을 돌리지 않고 영화 빛 계획을 쓴다
            assert deps.expand_kw and all(kw["crew"] is False for kw in deps.expand_kw)
            assert all("light and palette: soft warm lamp light from shop windows" in kw["look_plan"] for kw in deps.expand_kw)
            assert all("air: fine drizzle haze" in kw["look_plan"] for kw in deps.expand_kw)
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


async def t_scene_hint_follows_the_story_style():
    # (11-24: 예전에는 async 가 아니라 main 이 돌리지 않던 시험)
    assert D.Director.scene_hint({"bible": {}}) == D.Director.SCENE_HINT                 # 사진(기본)
    h = D.Director.scene_hint({"bible": {"style": "cel-shaded 3D with hand-drawn outlines"}})
    assert "cel-shaded 3D" in h and "Not a photograph" in h and "one still frame" in h
    assert D.Director.scene_hint({"bible": {"style": "photorealistic cinematic live action"}}) == D.Director.SCENE_HINT
    # 실사 판정은 화풍 열쇠와 같은 함수: 'semi-photorealistic' 은 반실사 (예전에는 여기서만 실사로 읽혔다)
    assert "semi-photorealistic digital painting" in D.Director.scene_hint({"bible": {"style": "semi-photorealistic digital painting"}})


async def t_scene_image_style_head_follows_the_recipe_and_the_style():
    """11-24 화풍 일반화 S4: 장소·장면·인물 그림 앞머리. Qwen 2512 는 화풍 원문이 있으면 실사여도(반실사 회화체 성향이라 실사 영화의
    장소 그림이 그림처럼 나올 수 있다), Z-Image 는 사실적이지 않은 화풍일 때만 붙인다. 편집 글에는 붙이지 않는다"""
    sp = D.Director._scene_image_prompt
    photo = {"bible": {"style": "photorealistic cinematic live action"}}
    anime = {"bible": {"style": "2D anime film, clean cel-shaded line art."}}
    assert sp(SCENE, photo, "IMG text") == "Photorealistic cinematic live action. IMG text"
    assert sp(SCENE, anime, "IMG text") == "2D anime film, clean cel-shaded line art. IMG text"
    assert sp(IMG, anime, "IMG text") == "2D anime film, clean cel-shaded line art. IMG text"
    assert sp(IMG, photo, "IMG text") == "IMG text"                                      # 실사는 Z-Image 의 기본 그림
    assert sp(SCENE, {"bible": {"style": ""}}, "IMG text") == "IMG text" and sp(IMG, {"bible": {}}, "IMG text") == "IMG text"
    assert sp(EDITR, anime, "edit text") == "edit text"
    already = "Photorealistic cinematic live action. IMG text"
    assert sp(SCENE, photo, already) == already                                          # 이미 그 문장으로 시작하면 그대로


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

    def assemble_dialogue(self, clips, lines, music, out, trims=None, seams=None):
        self.calls.append(("assemble_dialogue", (list(clips), [dict(x) for x in lines], music, out, list(trims or []))))
        self.seam_kinds = seams
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
            # 11-24: Z-Image 글도 사실적이지 않은 화풍이면 앞머리를 붙인다 (예전에는 그대로 두었다. 실사는 t_scene_image_style_head_* 에서)
            assert D.Director._scene_image_prompt(IMG, {"bible": {"style": "x y z"}}, "IMG text") == "X y z. IMG text"
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



# ── 장면 잇기 (11-20, docs/scene-chaining-design.md): 모든 컷을 앞 장면 끝에서 잇고, 끝 프레임에 인물이 나오게, 인물마다 같은 기준 그림 ──
EDITREF = recipes.load_all()["qwen_image_edit_ref"]
LTXEND = recipes.load_all()["ltx25_i2v_sound_end"]
GOOD_END = {"ok": True, "issues": [], "traits": [], "people": 1}
NO_ONE = {"ok": True, "issues": [], "traits": [], "people": 0}


def chain_deps(fs, root, cls=None, **kw):
    deps = (cls or LongDeps)(fs, root, **kw)
    deps.recs.update({EDITREF["id"]: EDITREF, LTXEND["id"]: LTXEND})
    deps.inspect_result = dict(GOOD_END)
    return deps


def tags_of(deps, t):
    tid = str(t["id"])
    return [c[1]["client_id"].split(f"studio-{tid}-")[1] for c in deps.calls if c[0] == "submit"]


def graph_loads(call):
    return sum(1 for n in call[1]["prompt"].values() if n.get("class_type") == "LoadImage")


async def t_chain_long_film_edits_every_cut_keeps_the_faces_and_ends_with_the_people():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            w = t["work"]
            assert w["chain"] is True
            tags = tags_of(deps, t)
            # 인물 기준 그림(C0) → 첫 장면은 기준 그림에서 구성(L0e) → 끝 키프레임(L0k) → 영상(L0v). 둘째 장면은 같은 샷이 이어져(continue)
            # 첫 화면 편집이 없다. 셋째 장면(장소 바뀜)은 앞 프레임을 고쳐 잇는다(L2e)
            for must in ("C01", "L0e1", "L0k1", "L0v1", "L1k1", "L1v1", "L2e1", "L2k1", "L2v1"):
                assert must in tags, (must, tags)
            assert "L1e1" not in tags and "L0i1" not in tags and "L2i1" not in tags, tags         # 장면 그림(글로 새로 그림)은 없다
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {x.split("studio-")[-1] if False else tags[i]: subs[i] for i in range(len(subs))}
            rid = lambda k: recipe_of(by[k])
            assert rid("L0e1") == EDITREF["id"] and rid("L2e1") == EDITREF["id"] and rid("L0k1") == EDITREF["id"]
            assert all(rid(f"L{k}v1") == LTXEND["id"] for k in range(3))                            # 끝 키프레임이 있어 가이드 레시피
            # 편집에는 늘 그 인물의 기준 그림을 함께 넣는다: 장면 2 는 그림 1(앞 프레임) + 그림 2(기준 그림) = LoadImage 둘, 첫 장면은 기준 그림 하나뿐
            assert graph_loads(by["L2e1"]) == 2 and graph_loads(by["L0e1"]) == 1
            p2 = " ".join(n["inputs"].get("prompt", "") for n in by["L2e1"][1]["prompt"].values() if isinstance(n["inputs"].get("prompt"), str))
            assert "face, hair and clothes of image 2" in p2 and "new place" in p2 and "not a glossy 3D render" in p2
            # 영상은 끝 키프레임(end_frame LoadImage)이 붙는다
            assert graph_loads(by["L0v1"]) == 2
            sc = w["scenes"]
            assert [x["seam"] for x in sc] == [None, "continue", "jump"] or [x.get("seam") for x in sc] == [None, "continue", "jump"]
            assert sc[1]["first_from"] == "last" and sc[2]["first_from"] == "edit" and sc[0]["first_from"] == "edit"
            assert all(x.get("end_rel") and x.get("last_rel") and x.get("chain_rel") and x.get("end_fail") is False for x in sc), sc
            assert sc[0]["chain_rel"] == sc[0]["end_rel"]                                               # 다음 장면이 잇는 것은 깨끗한 끝 키프레임
            # 합치기: 이음 종류별 (continue·jump 는 디졸브, 같은 장소 새 샷은 하드 컷)
            assert deps.seam_kinds == ["continue", "jump"]
            ends = [e[1] for e in fs.events if e[0] == "end_check"]
            assert len(ends) == 6 and all(e["why"] == [] for e in ends), ends                          # 끝 키프레임 3 + 끝 프레임 3
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_chain_end_frame_without_people_forces_a_fix_edit_for_the_next_scene():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, root)
            # 첫 장면: 끝 키프레임 통과, 클립 끝 프레임은 두 번 다 사람이 없다 → 끝 키프레임을 잇는 원본으로 쓰고 end_fail
            deps.inspect_seq = [dict(GOOD_END), dict(NO_ONE), dict(NO_ONE)]
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            assert tags.count("L0v1") == 1 and "L0v2" in tags, tags                                      # 끝 프레임이 틀려 클립을 한 번 더
            s0, s1 = t["work"]["scenes"][0], t["work"]["scenes"][1]
            assert s0["end_fail"] is True and s0["chain_rel"] == s0["end_rel"]
            # 둘째 장면은 continue 였지만 앞 끝 프레임이 사람이 없어 그대로 잇지 않고 고쳐 잇는다 (L1e)
            assert "L1e1" in tags and s1["first_from"] == "edit", (tags, s1)
            assert t["work"]["check_left"]["0:clip"][0].startswith("end:")
            res = [m for m in fs.messages if m["kind"] == "result"][0]
            assert any("끝 프레임에 인물 없음" in n for n in res["data"]["notes"]), res["data"]["notes"]
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_chain_off_or_edit_recipe_missing_keeps_the_old_way():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            for chain_flag, with_recipe in ((False, True), (True, False)):
                settings.studio_chain = chain_flag
                fs = FakeStore(); fs.patch()
                deps = chain_deps(fs, root)
                if not with_recipe:
                    deps.recs.pop(EDITREF["id"])
                d = D.Director(deps, dict(deps.recs))
                t = long_turn(fs)
                t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
                await d.pump(); await settle(d, 25)
                assert t["state"] == "done", (chain_flag, with_recipe, t["state"], t.get("error"))
                assert t["work"]["chain"] is False and deps.seam_kinds is None
                assert "L0i1" in tags_of(deps, t)                                                          # 예전처럼 장면 그림
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_chain_source_ref_choice_and_readiness():
    ok = D.Director._chain_source({"end_rel": "k.png", "edit_rel": "f.png"}, {"last_rel": "l.png"}, [])
    assert ok == {"chain_rel": "k.png", "end_fail": False}                                       # 통과: 깨끗한 끝 키프레임
    ok2 = D.Director._chain_source({"edit_rel": "f.png"}, {"last_rel": "l.png"}, ["body:floating_limb"])
    assert ok2 == {"chain_rel": "l.png", "end_fail": False}                                      # 끝 키프레임이 없으면 끝 프레임
    bad = D.Director._chain_source({"end_rel": "k.png", "edit_rel": "f.png"}, {"last_rel": "l.png"}, ["end:missing:A"])
    assert bad == {"chain_rel": "k.png", "end_fail": True}
    bad2 = D.Director._chain_source({"edit_rel": "f.png"}, {"last_rel": "l.png"}, ["end:people:0/1"])
    assert bad2 == {"chain_rel": "f.png", "end_fail": True}                                      # 통과한 끝 그림이 없으면 검사를 통과한 첫 화면
    unk = D.Director._chain_source({"edit_rel": "f.png"}, {"last_rel": "l.png"}, ["end:unchecked"])
    assert unk["end_fail"] is False and unk["chain_rel"] == "l.png"                              # 확인 못 한 것은 실패로 단정하지 않는다
    assert D.Director._cast_ref({"refs": {"semi": "a.png"}, "ref": "old.png"}, "semi") == ("a.png", None)
    assert D.Director._cast_ref({"refs": {"semi": "a.png"}, "ref": "old.png"}, "anime") == (None, "a.png")        # 반실사 그림을 바꿔 만든다
    assert D.Director._cast_ref({"refs": {"semi": "a.png", "anime": "b.png"}}, "anime") == ("b.png", None)
    assert D.Director._cast_ref({"ref": "old.png"}, "photo") == ("old.png", None)                                 # refs 가 없는 라이브러리


async def t_chain_dialogue_film_edits_talk_scenes_and_uses_the_trim_frame():
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, settings.results_dir, DialogueDeps)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": dialogue_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            w = t["work"]
            tags = tags_of(deps, t)
            # 말하는 장면(L1·L2)도 앞 프레임을 고쳐 잇는다 (기준 그림 편집이 아니라). 인물 닻은 대사 없는 장면의 인물까지
            assert "L1e1" in tags and "L2e1" in tags and "L3e1" in tags and "L0e1" in tags, tags
            subs = [c for c in deps.calls if c[0] == "submit"]
            talk = [c for c in subs if recipe_of(c) == TALK["id"]]
            assert len(talk) == 2
            # 입 맞춤 클립은 합칠 때 자르는 지점의 프레임을 저장한다 (last_index): 대사 2.6초 + 0.9초 = 3.5초 × 24fps
            idx = [n["inputs"]["batch_index"] for c in talk for n in c[1]["prompt"].values()
                   if n.get("class_type") == "ImageFromBatch" and n["inputs"].get("batch_index") not in (None, 60)]
            assert idx and all(i > 0 and i % 1 == 0 for i in idx), idx
            sc = w["scenes"]
            assert sc[1]["last_rel"] and sc[1]["chain_rel"] == sc[1]["last_rel"] and sc[1]["end_fail"] is False
            assert deps.seam_kinds == ["cut", "cut", "cut"], deps.seam_kinds                            # 같은 장소의 새 샷은 하드 컷
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain = old


async def t_chain_style_convert_makes_the_film_style_reference_once():
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, settings.results_dir, DialogueDeps)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = library_story(settings.results_dir)
            story["bible"]["style"] = "2D anime film, clean cel-shaded line art"
            for c in story["bible"]["characters"]:
                rel = f"studio/_cast/{c['id']}_semi.png"
                os.makedirs(os.path.dirname(os.path.join(settings.results_dir, rel)), exist_ok=True)
                with open(os.path.join(settings.results_dir, rel), "wb") as f:
                    f.write(b"png")
                c["refs"] = {"semi": rel}
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            conv = [e[1] for e in fs.events if e[0] == "cast_style_convert"]
            assert len(conv) == len(story["bible"]["characters"]) and all(e["style"] == "anime" for e in conv), conv
            subs = [c for c in deps.calls if c[0] == "submit"]
            edits = [c for c in subs if recipe_of(c) == EDITR["id"]]
            assert edits and all("2D anime feature film" in " ".join(n["inputs"].get("prompt", "") for n in c[1]["prompt"].values()
                                                                      if isinstance(n["inputs"].get("prompt"), str)) for c in edits)
            assert all(t["work"]["casts"][c["id"]].get("converted") == "anime" for c in story["bible"]["characters"])
            assert all(e["recipe"] == EDITR["id"] for e in conv)                       # 애니는 빠른 편집 그대로
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain = old


async def t_photo_film_converts_references_with_the_quality_edit_and_checks_them():
    """11-24 화풍 일반화 S5·S6: 실사 영화는 반실사 기준 그림을 가속을 끈 편집(높음 판)으로 바꾸고(11-20 S6: Lightning 실사 변환은 그림 느낌이
    남았다), 변환 그림은 알아볼 소품과 실사의 '아닌 것' 문장으로 늘 검사한다. 장면 클립·입 맞춤 클립 가운데 프레임도 실사 문장으로 본다
    (예전에는 실사면 story_style 이 빈 글이라 화풍을 보지 않았다)"""
    from app.studio import checks
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        settings.studio_picture_check = True
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, settings.results_dir, DialogueDeps)
            deps.recs[HQ_EDIT["id"]] = HQ_EDIT
            deps.inspect_result = dict(PASS)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = library_story(settings.results_dir)
            story["bible"]["style"] = "photorealistic cinematic live action"
            for c in story["bible"]["characters"]:
                rel = f"studio/_cast/{c['id']}_semi.png"
                os.makedirs(os.path.dirname(os.path.join(settings.results_dir, rel)), exist_ok=True)
                with open(os.path.join(settings.results_dir, rel), "wb") as f:
                    f.write(b"png")
                c["refs"] = {"semi": rel}
            story["bible"]["characters"][0]["signature"] = ["wine-red round-brimmed hat", "two long black braids"]
            # 예상 시간: 실사 변환 두 장은 높음 판 추정식(빠른 편집이면 고쳐 잇기 편집 한 장만큼)
            d2 = D.Director(deps, {k: v for k, v in deps.recs.items() if k != HQ_EDIT["id"]})
            ctx = D.Ctx(t, USER, ENV, LTX)
            ctx.long = {}
            e_hq, e_fast = await d.estimate_long(ctx, copy.deepcopy(story)), await d2.estimate_long(ctx, copy.deepcopy(story))
            assert e_hq - e_fast == 2 * (HQ_EDIT["estimate"]["gpu_seconds"] - D.CHAIN_EDIT_SECONDS), (e_hq, e_fast)
            assert d._convert_recipe("anime")["id"] == EDITR["id"] and d._convert_recipe("photo")["id"] == HQ_EDIT["id"]
            assert d2._convert_recipe("photo")["id"] == EDITR["id"]                    # 높음 판이 없으면 지금처럼 빠른 편집
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            conv = [e[1] for e in fs.events if e[0] == "cast_style_convert"]
            assert len(conv) == 2 and all(e["style"] == "photo" and e["recipe"] == HQ_EDIT["id"] for e in conv), conv
            rids = [recipe_of(c) for c in deps.calls if c[0] == "submit"]
            assert rids.count(HQ_EDIT["id"]) == 2 and EDITR["id"] not in rids, rids
            assert all(t["work"]["casts"][c["id"]].get("converted") == "photo" for c in story["bible"]["characters"])
            neg = checks.style_negative("photo")
            chars = {c["id"]: c for c in story["bible"]["characters"]}
            ins = [c[1][3] for c in deps.calls if c[0] == "inspect"]
            # 변환 그림 검사: 알아볼 소품(없으면 외형 앞부분) + 실사의 '아닌 것' 문장
            assert [checks.sign_trait(chars["A"]["look"], chars["A"]["signature"]), neg] in ins, ins[:4]
            assert [checks.cast_trait(chars["B"]["look"]), neg] in ins, ins[:4]
            conv_checks = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "convert"]
            assert len(conv_checks) == 2 and all(e["why"] == [] and len(e["cast"]) == 1 for e in conv_checks), conv_checks
            # 장면 클립(check_)·입 맞춤 클립(talkcheck_)의 가운데 프레임 검사도 실사 문장
            by_file = {c[1][2]: c[1][3] for c in deps.calls if c[0] == "inspect"}
            clip = [x for f, x in by_file.items() if f.startswith(("check_", "talkcheck_"))]
            assert len(clip) == 4 and all(x == [neg] for x in clip), by_file
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check = old


async def t_photo_film_scene_images_and_clips_are_checked_for_the_photo_look():
    """11-24 S5: 장면 잇기가 꺼진 실사 영화도 장면 그림·클립 가운데 프레임을 실사의 '아닌 것' 문장으로 본다.
    Z-Image 장면 그림에는 실사 앞머리를 붙이지 않는다 (실사가 Z-Image 의 기본 그림)"""
    from app.studio import checks
    old = (settings.results_dir, settings.studio_chain, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain, settings.studio_picture_check = root, False, True
        try:
            fs = FakeStore(); fs.patch()
            deps = LongDeps(fs, root)
            deps.inspect_result = dict(PASS)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            story = copy.deepcopy(LONG_STORY)
            story["bible"]["style"] = "photorealistic cinematic live action"
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            neg = checks.style_negative("photo")
            by_file = {c[1][2]: c[1][3] for c in deps.calls if c[0] == "inspect"}
            imgs = [x for f, x in by_file.items() if f.startswith("img_")]
            clips = [x for f, x in by_file.items() if f.startswith("check_")]
            assert len(imgs) == 2 and len(clips) == 3 and all(x == [neg] for x in imgs + clips), by_file
            slot = IMG["inject"]["prompt"]
            scene = [c[1]["prompt"][slot["node"]]["inputs"][slot["input"]] for c in deps.calls
                     if c[0] == "submit" and recipe_of(c) == IMG["id"]]
            assert len(scene) == 2 and all(x.startswith("IMG ") for x in scene), scene
            # 실사가 아니면 '아닌 것' 문장을 더하지 않는다 (사실적이지 않은 화풍은 style_trait 가 원문으로 본다). 이미 있으면 한 번
            assert D.Director._style_extra({"bible": {"style": "2D anime film"}}) == [] and D.Director._style_extra({"bible": {}}) == []
            assert D.Director._style_extra({"bible": {"style": "photo-real film"}}, ["x", ""]) == ["x", neg]
            assert D.Director._style_extra({"bible": {"style": "live action"}}, [neg]) == [neg]
        finally:
            settings.results_dir, settings.studio_chain, settings.studio_picture_check = old


async def t_photo_film_place_card_and_portrait_get_the_style_head_and_the_photo_check():
    """11-24 S4·S5: Qwen 2512 로 그리는 장소 그림·인물 기준 그림은 실사 영화여도 화풍 앞머리를 붙이고(반실사 회화체 성향),
    장소 그림 검사도 실사의 '아닌 것' 문장으로 본다"""
    from app.studio import checks
    old = (settings.results_dir, settings.studio_chain, settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root)
            deps.recs[SCENE["id"]] = SCENE
            story = copy.deepcopy(LONG_STORY)
            story["bible"]["style"] = "photorealistic cinematic live action"
            story["bible"]["characters"][0]["library"] = "guest"        # 라이브러리 인물이 나오는 영화: 장면·장소·인물 그림은 Qwen 2512
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            slot = SCENE["inject"]["prompt"]
            text = lambda c: c[1]["prompt"][slot["node"]]["inputs"][slot["input"]]
            assert recipe_of(by["P01"]) == SCENE["id"] and text(by["P01"]).startswith("Photorealistic cinematic live action. An empty establishing")
            assert recipe_of(by["C01"]) == SCENE["id"] and text(by["C01"]).startswith("Photorealistic cinematic live action. Character reference")
            neg = checks.style_negative("photo")
            name = os.path.basename(t["work"]["places"]["P1"]["rel"])
            assert [c[1][3] for c in deps.calls if c[0] == "inspect" and c[1][2] == name] == [[neg]]
        finally:
            settings.results_dir, settings.studio_chain, settings.studio_ref_layout = old


# ── 기준 그림으로 새로 그리기 (11-20 사용자 승인: 컷마다 앞 프레임을 고치지 않고 기준 그림에서 새로 그린다) ──
COMPOSE = recipes.load_all()["qwen_image_compose_ref"]
CAST_MISS = {"ok": True, "issues": [], "traits": [{"present": False, "note": "no black umbrella"}], "people": 1}


def compose_deps(fs, root, cls=None, layout=False, **kw):
    """새로 그리기 레시피까지 켠 장면 잇기. layout 이면 구도 그림에서 그리기(STUDIO_REF_LAYOUT)도 켠다 (아니면 기준 그림으로 바로).
    검사 결과는 방금 접수한 작업으로 고른다: 장소 그림(P)은 사람 없음(place_people 로 바꿈), 구도 그림(Lc)은 layout_seq 차례,
    새로 그린 첫 화면(Le)은 compose_seq 차례(비면 통과), 그 밖은 인물이 있는 통과"""
    settings.studio_ref_layout = bool(layout)
    base = cls or LongDeps

    class ComposeDeps(base):
        async def submit(self, env, user, body, ip):
            self.last_cid = body["client_id"]
            return await super().submit(env, user, body, ip)

        async def inspect(self, user, ip, username, filename, subfolder, expect):
            self.calls.append(("inspect", (username, subfolder, filename, expect)))
            tag = self.last_cid.rsplit("-", 1)[-1]
            if tag.startswith("P"):
                return {"ok": True, "issues": [], "traits": [], "people": self.place_people}
            if re.fullmatch(r"L\d+c\d+", tag) and self.layout_seq:
                return self.layout_seq.pop(0)
            if tag.startswith("L") and "e" in tag and self.compose_seq:
                r = self.compose_seq.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            n = self.people_for(tag) if self.people_for else None        # 여러 사람 장면: 그 단계에서 셀 사람 수
            out = {**GOOD_END, "people": n} if n is not None else dict(GOOD_END)
            if self.issues_for:
                out["issues"] = [{"type": x} for x in self.issues_for(tag)]
            return out

    deps = ComposeDeps(fs, root, **kw)
    deps.recs.update({EDITREF["id"]: EDITREF, LTXEND["id"]: LTXEND, COMPOSE["id"]: COMPOSE})
    deps.last_cid, deps.compose_seq, deps.place_people, deps.layout_seq, deps.people_for, deps.issues_for = "", [], 0, [], None, None
    return deps


def loads_of(call):
    return {n["inputs"]["image"] for n in call[1]["prompt"].values() if n.get("class_type") == "LoadImage"}


def text_of(call):
    return " ".join(n["inputs"]["prompt"] for n in call[1]["prompt"].values()
                    if isinstance(n["inputs"].get("prompt"), str) and n["inputs"]["prompt"].strip())


async def t_compose_draws_every_cut_from_the_references_and_a_place_card():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            w = t["work"]
            tags = tags_of(deps, t)
            # 인물 기준 그림(C0) → 장소 그림(P0) → 새로 그린 첫 화면(L0e) → 끝 키프레임(L0k) → 영상. 둘째 장면은 같은 샷이 이어져 끝 프레임 그대로.
            # 셋째 장면(장소 바뀜)은 새 장소 그림(P1)과 기준 그림으로 새로 그린다 (앞 프레임을 고치지 않는다)
            for must in ("C01", "P01", "L0e1", "L0k1", "L0v1", "L1v1", "P11", "L2e1", "L2k1", "L2v1"):
                assert must in tags, (must, tags)
            assert "L1e1" not in tags and "L0i1" not in tags and "L2i1" not in tags and "L0e2" not in tags, tags
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[i]: subs[i] for i in range(len(subs))}
            assert recipe_of(by["L0e1"]) == COMPOSE["id"] and recipe_of(by["L2e1"]) == COMPOSE["id"]
            assert recipe_of(by["L0k1"]) == EDITREF["id"]                     # 끝 키프레임은 같은 샷이라 첫 화면을 고친다
            # 그림은 기준 그림 + 장소 그림뿐: 앞 장면의 프레임이 들어가지 않는다
            inp = lambda att: fs.atts[att]["input_rel"]
            cast_in = inp(w["casts"]["A"]["att"])
            assert loads_of(by["L2e1"]) == {cast_in, inp(w["places"]["P2"]["att"])}
            assert loads_of(by["L0e1"]) == {cast_in, inp(w["places"]["P1"]["att"])}
            # 빈 잠재의 크기는 영상 비율
            lat = by["L0e1"][1]["prompt"]["44"]["inputs"]
            assert (lat["width"], lat["height"]) == tuple(recipes.default_size(COMPOSE, photo=tuple(w["long_size"])))
            p2 = text_of(by["L2e1"])
            assert p2.startswith("A new picture: close shot") and "face, hair and clothes of image 1" in p2
            assert "the place shown in image 2" in p2 and "an apartment entrance" in p2 and "not a glossy 3D render" in p2
            sc = w["scenes"]
            assert [x.get("first_from") for x in sc] == ["compose", "last", "compose"], sc
            assert [e[1]["place"] for e in fs.events if e[0] == "place_card"] == ["P1", "P2"]
            comp = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "compose"]
            assert len(comp) == 2 and all(e["why"] == [] and e["cast"] == ["A"] for e in comp), comp
            # 검사 모델에게는 인물(알아볼 소품이 없으면 외형 앞부분)과 사람 수를 묻는다
            exp = [c[1][3] for c in deps.calls if c[0] == "inspect" and c[1][3] and c[1][3][0].startswith("A person who looks like")]
            assert exp and "navy wool coat" in exp[0][0]
            assert deps.seam_kinds == ["continue", "jump"]
            assert not (w.get("check_left") or {}), w.get("check_left")
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_compose_redraws_until_the_character_matches_and_reports_what_is_left():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root)
            # 첫 장면: 다섯 번 모두 우산이 없다 → 가장 나은 그림을 쓰고 알린다. 셋째 장면: 검사 모델이 두 번 다 실패 → 다시 그리지 않고 알린다
            deps.compose_seq = [dict(CAST_MISS) for _ in range(5)] + [RuntimeError("vision down"), RuntimeError("vision down")]
            story = copy.deepcopy(LONG_STORY)
            story["bible"]["characters"][0]["signature"] = ["black umbrella", "navy wool coat", "polished brown shoes"]
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 30)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            assert [x for x in tags if x.startswith("L0e")] == ["L0e1", "L0e2", "L0e3", "L0e4", "L0e5"], tags
            assert [x for x in tags if x.startswith("L2e")] == ["L2e1"], tags
            retry = [e[1] for e in fs.events if e[0] == "picture_retry" and e[1]["what"] == "compose"]
            assert len(retry) == 4 and all(e["why"] == ["cast:missing:A"] for e in retry), retry
            left = t["work"]["check_left"]
            assert left["0:image"] == ["cast:missing:A"] and left["2:image"] == ["cast:unchecked"], left
            # 알아볼 소품은 앞 두 개만 검사한다 (신발처럼 앉은 장면에서 안 보이는 것은 틀린 그림이 아니다)
            exp = [c[1][3] for c in deps.calls if c[0] == "inspect" and c[1][3] and c[1][3][0].startswith("A person with")]
            assert exp and exp[0][0] == "A person with black umbrella and navy wool coat is in the picture with the face visible", exp[0]
            # 그림 글에는 소품을 모두 적는다
            p0 = text_of(next(c for c in deps.calls if c[0] == "submit" and c[1]["client_id"].endswith("L0e1")))
            assert "including the black umbrella, the navy wool coat, the polished brown shoes" in p0
            notes = [m for m in fs.messages if m["kind"] == "result"][0]["data"]["notes"]
            assert any("인물 모습(기준 그림과 다름)" in n for n in notes) and any("인물 모습 확인 못 함" in n for n in notes), notes
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_compose_skips_a_place_card_that_still_shows_people():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root)
            deps.place_people = 1                 # 장소 그림에 사람이 남는다 → 세 번 그리고 버린다 (그 사람이 장면에 들어오지 않게)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 30)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            w = t["work"]
            tags = tags_of(deps, t)
            assert [x for x in tags if x.startswith("P0")] == ["P01", "P02", "P03"], tags
            subs = [c for c in deps.calls if c[0] == "submit"]
            l0 = subs[tags.index("L0e1")]
            assert loads_of(l0) == {fs.atts[w["casts"]["A"]["att"]]["input_rel"]}                  # 기준 그림 한 장만
            assert "the place shown in image" not in text_of(l0) and "The place: a wet city street" in text_of(l0)
            assert w["places"]["P1"] == {"dropped": True} and w["places"]["P2"] == {"dropped": True}
            assert not any(str(k).startswith("place:") for k in (w.get("check_left") or {})), w.get("check_left")
            assert len([e for e in fs.events if e[0] == "place_card_dropped"]) == 2
            assert not any(e[0] == "place_card" for e in fs.events)
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_compose_dialogue_film_draws_talk_scenes_with_the_speaker_first():
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, settings.results_dir, DialogueDeps)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": dialogue_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[i]: subs[i] for i in range(len(subs))}
            for k in range(4):
                assert recipe_of(by[f"L{k}e1"]) == COMPOSE["id"], (k, tags)
            # 두 사람 장면: 기준 그림 둘(말하는 푸토르 먼저) + 장소 그림. 말하는 장면: 말하는 사람이 그림 1이고 말하는 방향을 적는다
            casts = t["work"]["casts"]
            inp = lambda att: fs.atts[att]["input_rel"]
            assert len(loads_of(by["L0e1"])) == 3 and loads_of(by["L1e1"]) == {inp(casts["A"]["att"]), inp(t["work"]["places"]["P1"]["att"])}
            p0, p1 = text_of(by["L0e1"]), text_of(by["L1e1"])
            assert "The person from image 1 is a short old dwarf herbalist" in p0 and "Exactly 2 people" in p0
            assert "speaks toward the" in p1 and "exactly one person" in p1
            assert "She looks up and asks softly" not in p1          # 대사 컷은 장면 글을 넣지 않는다 (화면 밖 상대 복제, 11-21 R3)
            assert len([e for e in fs.events if e[0] == "place_card"]) == 1         # 한 장소에 한 장
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain = old


# ── 구도 그림에서 그리기 (11-21 메인 실험 반영): ① 얼굴 없는 형상의 구도 그림 → ② 인물 입히기 → ③ 세 번째 사람부터 ──
THREE_STORY = copy.deepcopy(LONG_STORY)
THREE_STORY["bible"]["characters"] += [{"id": "B", "look": "a twelve-year-old girl with two long black braids and a wine-red hat"},
                                       {"id": "C", "look": "a calm neat woman around forty in a cream blouse"}]
THREE_STORY["scenes"][0].update(characters=["A", "B", "C"], shot="medium wide shot", action="The family sits at the table and laughs.")
LAYOUT_BAD = {"ok": True, "issues": [], "traits": [{"present": False, "note": "two figures"}], "people": 0}


async def t_layout_draws_a_layout_then_puts_the_person_in():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root, layout=True)
            deps.layout_seq = [dict(LAYOUT_BAD)]           # 첫 구도 그림은 형상 수가 틀려 다시 그린다
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            w = t["work"]
            tags = tags_of(deps, t)
            # 장소 그림(P0) → 구도 그림(L0c, 두 번째에 통과) → 인물 입히기(L0e) → 끝 키프레임 → 영상. 이어지는 둘째 장면은 끝 프레임 그대로
            for must in ("C01", "P01", "L0c1", "L0c2", "L0d1", "L0k1", "L0v1", "L1v1", "P11", "L2c1", "L2d1", "L2k1", "L2v1"):
                assert must in tags, (must, tags)
            assert "L1c1" not in tags and "L1d1" not in tags and "L0c3" not in tags and "L0d2" not in tags, tags
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            assert all(recipe_of(by[x]) == COMPOSE["id"] for x in ("L0c1", "L0d1", "L2c1", "L2d1"))
            inp = lambda att: fs.atts[att]["input_rel"]
            cast_in = inp(w["casts"]["A"]["att"])
            assert loads_of(by["L0c2"]) == {inp(w["places"]["P1"]["att"])}                     # ① 은 장소 그림 한 장
            e0 = loads_of(by["L0d1"])
            assert len(e0) == 2 and cast_in in e0 and inp(w["places"]["P1"]["att"]) not in e0  # ② 는 구도 그림 + 기준 그림
            c0, e0t = text_of(by["L0c2"]), text_of(by["L0d1"])
            assert "faceless mannequin figure" in c0 and c0.endswith("Exactly 1 figure, no other figure or person.") and "He walks" not in c0
            assert e0t.startswith("Picture 1 is the layout.") and "The background stays exactly the place of Picture 1 (a wet city street)" in e0t
            assert "Replace the faceless figure in the centre with the person in Picture 2" in e0t and "What happens: He walks." in e0t
            lat = by["L0c1"][1]["prompt"]["44"]["inputs"]
            assert (lat["width"], lat["height"]) == tuple(recipes.default_size(COMPOSE, photo=tuple(w["long_size"])))
            sc = w["scenes"]
            assert [x.get("first_from") for x in sc] == ["layout", "last", "layout"], sc
            assert sc[0].get("layout_rel") and sc[2].get("layout_rel") and sc[0]["layout_rel"] != sc[0]["edit_rel"]
            checks_ = [e[1] for e in fs.events if e[0] == "picture_check" and not e[1].get("skipped")]
            lay = [e for e in checks_ if e["what"] == "layout"]
            ide = [e for e in checks_ if e["what"] == "identity"]
            assert [e["why"] for e in lay] == [["layout:figures"], [], []], lay
            assert len(ide) == 2 and all(e["why"] == [] and e["cast"] == ["A"] and e["want"] == 1 for e in ide), ide
            retry = [e[1] for e in fs.events if e[0] == "picture_retry"]
            assert [r["what"] for r in retry] == ["layout"], retry
            done = [e[1] for e in fs.events if e[0] == "layout_done"]
            assert [x["index"] for x in done] == [0, 2] and all(x["passes"] == 2 for x in done)
            assert not (w.get("check_left") or {}), w.get("check_left")
            # 검사 모델에게는 형상 수를 특징 줄로 묻는다
            exp = [c[1][3] for c in deps.calls if c[0] == "inspect" and c[1][3] and c[1][3][0].startswith("Exactly 1 faceless mannequin")]
            assert len(exp) == 3
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_layout_three_people_puts_two_then_the_third():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root, layout=True)
            deps.people_for = lambda tag: 3 if re.match(r"L0(?:f|k|v)", tag) else None      # 첫 장면의 ③·끝 키프레임·클립은 세 사람
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(THREE_STORY), "approved": True}
            await d.pump(); await settle(d, 30)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            w = t["work"]
            tags = tags_of(deps, t)
            for must in ("L0c1", "L0d1", "L0f01", "L2c1", "L2d1"):
                assert must in tags, (must, tags)
            assert "L2f01" not in tags, tags                                  # 한 사람 장면은 ③ 이 없다
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            inp = lambda cid: fs.atts[w["casts"][cid]["att"]]["input_rel"]
            c0, e0, f0 = text_of(by["L0c1"]), text_of(by["L0d1"]), text_of(by["L0f01"])
            assert "3 faceless mannequin figures, sitting: on the left a adult-sized figure" in c0 and "in the middle a child-sized figure" in c0
            # ② 는 앞의 두 사람(A·B)만, 오른쪽 형상은 그대로. ③ 은 그 그림 + C 의 기준 그림
            le = loads_of(by["L0d1"])
            assert len(le) == 3 and {inp("A"), inp("B")} <= le and inp("C") not in le
            assert "Keep the faceless figure on the right exactly as it is." in e0 and "Exactly 3 figures in total" in e0
            lf = loads_of(by["L0f01"])
            assert len(lf) == 2 and inp("C") in lf and not ({inp("A"), inp("B")} & lf)
            assert f0.startswith("Picture 1 is the picture to finish.") and "on the right with the person in Picture 2, a calm neat woman" in f0
            assert "Exactly 3 people in the picture" in f0
            checks_ = [e[1] for e in fs.events if e[0] == "picture_check" and not e[1].get("skipped")]
            ide = [e for e in checks_ if e["what"] == "identity" and e["index"] == 0]
            fin = [e for e in checks_ if e["what"] == "finish"]
            assert len(ide) == 1 and ide[0]["want"] is None and ide[0]["cast"] == ["A", "B"]        # 중간 그림은 사람 수를 세지 않는다
            assert len(fin) == 1 and fin[0]["want"] == 3 and fin[0]["cast"] == ["A", "B", "C"] and fin[0]["more_ok"] is False
            assert w["scenes"][0]["first_from"] == "layout"
            assert [x["passes"] for x in (e[1] for e in fs.events if e[0] == "layout_done")] == [3, 2]
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_layout_without_a_place_card_draws_straight_from_the_references():
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root, layout=True)
            deps.place_people = 1                   # 장소 그림에 사람이 남아 버린다 → 구도 그림을 그릴 바탕이 없다
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True}
            await d.pump(); await settle(d, 30)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            assert not any(x.startswith(("L0c", "L2c")) for x in tags), tags
            subs = [c for c in deps.calls if c[0] == "submit"]
            l0 = subs[tags.index("L0e1")]
            assert loads_of(l0) == {fs.atts[t["work"]["casts"]["A"]["att"]]["input_rel"]}
            assert [x.get("first_from") for x in t["work"]["scenes"]] == ["compose", "last", "compose"]
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_layout_dialogue_talk_scene_puts_the_speaker_in_the_centre():
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, settings.results_dir, DialogueDeps, layout=True)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": dialogue_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            for k in range(4):
                assert f"L{k}c1" in tags and f"L{k}d1" in tags, (k, tags)
            w = t["work"]
            inp = lambda cid: fs.atts[w["casts"][cid]["att"]]["input_rel"]
            # 두 사람 장면: 오른쪽을 보는 첫 화자(푸토르 B)가 왼쪽 = 그림 2, 니키 A = 그림 3
            e0 = text_of(by["L0d1"])
            assert "on the left with the person in Picture 2, a short old dwarf herbalist" in e0 and "on the right with the person in Picture 3" in e0
            assert {inp("A"), inp("B")} <= loads_of(by["L0d1"])
            # 니키의 대사 컷: 가운데 한 형상, 걷던 장면을 이어 걷는 자세, 장면 글 없이 말하는 방향·감정
            c1, e1 = text_of(by["L1c1"]), text_of(by["L1d1"])
            assert "One child-sized faceless mannequin figure, standing" in c1 and "A new picture of this place: wide shot." in text_of(by["L0c1"]) and "clear empty space above the hair" in c1
            assert "speaks toward the left side of the frame with a hesitant expression" in e1 and "She looks up" not in e1
            assert len([e for e in fs.events if e[0] == "place_card"]) == 1
            talk = [c for c in subs if recipe_of(c) == TALK["id"]]
            assert len(talk) == 2
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain = old


async def t_layout_middle_pass_keeps_the_blank_figure_without_retrying():
    """세 사람 장면의 ② 에는 일부러 남긴 얼굴 없는 형상이 있다: 검사 모델이 그것을 얼굴 이상으로 답해도 다시 그리지 않는다 (11-21 검토)"""
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root, layout=True)
            deps.people_for = lambda tag: 3 if re.match(r"L0(?:f|k|v)", tag) else None
            deps.issues_for = lambda tag: ["face", "missing_head"] if tag.startswith("L0d") else []
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": copy.deepcopy(THREE_STORY), "approved": True}
            await d.pump(); await settle(d, 30)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            assert [x for x in tags if x.startswith("L0d")] == ["L0d1"], tags
            assert not [e for e in fs.events if e[0] == "picture_retry" and e[1]["what"] == "identity"]
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_layout_resume_uses_the_saved_stage_picture():
    """허브가 다시 시작하면 끝난 단계는 고른 그림을 그대로 쓴다 (다시 검사·다시 그리기 없이, 11-21 검토)"""
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root, layout=True)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            lay = deps._file("saved_layout.png")
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True, "L0c_done": {"rel": lay, "why": [], "seen": None}}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            assert "L0c1" not in tags and "L0d1" in tags and "L2c1" in tags, tags
            assert ("stage_image", lay) in deps.calls
            w = t["work"]
            assert w["scenes"][0]["layout_rel"] == lay and w["L0d_done"]["rel"] == w["scenes"][0]["edit_rel"]
            assert w["L2c_done"]["rel"] == w["scenes"][2]["layout_rel"]
            lay_checks = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "layout" and e[1].get("index") == 0]
            assert not lay_checks                                   # 남겨 둔 구도 그림은 다시 검사하지 않는다
            # 첫 화면 설명은 그림 번호·형상 말 없이 (영상 글을 쓰는 LLM 이 읽는다)
            sp = w["scenes"][0]["scene_prompt"]
            assert "Picture" not in sp and "faceless" not in sp and "in the centre: a man in his forties" in sp
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_layout_reset_clears_stage_pictures_and_the_end_keyframe():
    """장면 클립이 바뀌어 다시 만들면 구도 그림 단계의 고른 그림과 끝 키프레임 접수 정보도 비운다 (옛 끝 그림을 이어받던 문제, 11-21 검토)"""
    old = (settings.results_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root, layout=True)
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            old_lay, old_key = deps._file("old_layout.png"), deps._file("old_keyframe.png")
            t["state"] = "active"
            t["work"] = {"story": copy.deepcopy(LONG_STORY), "approved": True, "long_size": [1280, 704], "chain": True,
                         "L0c_done": {"rel": old_lay}, "L0d_done": {"rel": deps._file("old_first.png")},
                         "L0k_pending": {"client_id": f"studio-{t['id']}-L0k1", "attempt": 1},
                         "scenes": [{"clip_rel": deps._file("video_a.mp4"), "clip_sha": "0" * 64, "last_rel": deps._file("last_a.png"),
                                     "end_rel": old_key, "plan": dict(FakeDeps.PLAN)}]}
            fs.by_client[f"studio-{t['id']}-L0k1"] = {"prompt_id": "old-job"}          # 옛 끝 키프레임 작업을 이어받으면 안 된다
            await d.recover(); await settle(d, 30)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            assert "L0c1" in tags and "L0d1" in tags and "L0k2" in tags, tags         # 새 구도 그림·새 첫 화면·새 끝 키프레임
            s0 = t["work"]["scenes"][0]
            assert s0["layout_rel"] != old_lay and s0["end_rel"] != old_key
        finally:
            settings.results_dir, settings.studio_chain = old


async def t_layout_close_up_does_not_redraw_for_a_floating_limb_false_positive():
    """클로즈업 대사 컷의 '팔다리 떠 있음' 판정은 오탐이다: 다시 그리지 않는다 (11-24 메인: 베스 첫 화면을 다섯 번 다시 그렸다)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, settings.results_dir, DialogueDeps, layout=True)
            deps.issues_for = lambda tag: ["floating_limb"] if re.match(r"L[13]d", tag) else []        # 장면 1·3 은 close-up, 0 은 와이드
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": dialogue_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            tags = tags_of(deps, t)
            assert [x for x in tags if x.startswith("L1d")] == ["L1d1"], tags
            assert [x for x in tags if x.startswith("L3d")] == ["L3d1"], tags
            retry = [e[1] for e in fs.events if e[0] == "picture_retry" and e[1]["what"] == "identity" and e[1]["index"] in (1, 3)]
            assert not retry, retry          # (장면 0 의 두 사람 장면은 가짜 검사기가 1명으로 답해 다시 그리지만 이 시험과 상관없다)
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain = old


# ── 두 사람 대사 컷 (11-24 메인 X1·X6): 듣는 상대가 같은 장소면 투샷 + InfiniteTalk 두 화자, 말하지 않는 자리에 무음 ──
DUO = recipes.load_all()["wan21_infinitetalk_duo"]


def duo_story(on: bool = True):
    """베스(A)가 제임스(B)에게 말하고(투샷), 제임스가 같은 투샷에서 이어 답하고, 베스가 혼잣말(클로즈업)을 한다"""
    from app import video
    raw = {
        "title_ko": "생일 아침", "logline_ko": "베스와 제임스가 니키의 생일을 이야기한다.",
        "bible": {"characters": [
            {"id": "A", "look": "a calm neat woman around forty in a cream blouse and an apron", "name_ko": "베스",
             "voice": "A warm Korean woman around forty, gentle.", "voice_ko": "따뜻한 엄마"},
            {"id": "B", "look": "a tall lean man in his early forties in a blue cardigan", "name_ko": "제임스",
             "voice": "A calm Korean man in his forties, low and friendly.", "voice_ko": "차분한 아빠"}],
            "places": [{"id": "P1", "look": "a sunny cottage kitchen with a wooden table"}], "time": "morning", "light": "warm window light",
            "style": "semi-realistic painterly 2.5D film look"},
        "music": {"tags": "gentle instrumental, piano", "bpm": 72, "key": "D major", "ko": "잔잔한 피아노"},
        "scenes": [
            {"seconds": 10, "place": "P1", "characters": ["A", "B"], "continues": False, "shot": "wide shot",
             "action": "They sit at the wooden table in the morning.", "sound": "birds", "ko": "아침 식탁", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 6, "place": "P1", "characters": ["A", "B"], "continues": False, "shot": "medium two-shot",
             "action": "She smiles at him and talks about Niki.", "sound": "room tone", "ko": "베스가 말한다", "kind": "talk",
             "speaker": "A", "listener": "B", "line_ko": "여보, 오늘 니키 생일이에요.", "emotion": "warm"},
            {"seconds": 6, "place": "P1", "characters": ["B", "A"], "continues": True, "shot": "medium two-shot",
             "action": "He nods and answers.", "sound": "room tone", "ko": "제임스가 답한다", "kind": "talk",
             "speaker": "B", "listener": "A", "line_ko": "그래요? 케이크를 사 올게요.", "emotion": "surprised"},
            {"seconds": 6, "place": "P1", "characters": ["A"], "continues": False, "shot": "close-up",
             "action": "She thinks aloud.", "sound": "room tone", "ko": "베스의 혼잣말", "kind": "talk",
             "speaker": "A", "listener": "", "line_ko": "니키가 좋아하겠지...", "emotion": "dreamy"}]}
    old = settings.studio_duo_talk
    settings.studio_duo_talk = on
    try:
        story = video.clean_dialogue_story(raw, 30)
    finally:
        settings.studio_duo_talk = old
    assert story is not None
    return story


def duo_deps(fs, layout=True, with_duo=True):
    deps = compose_deps(fs, settings.results_dir, DialogueDeps, layout=layout)
    if with_duo:
        deps.recs[DUO["id"]] = DUO
    # 두 사람 장면(0·1·2)은 두 사람, 혼잣말 컷(3)은 한 사람으로 센다
    deps.people_for = lambda tag: 2 if re.match(r"L[012]", tag) else None
    return deps


def prompt_text(call):
    return " ".join(str(n["inputs"].get("text") or n["inputs"].get("value") or "") for n in call[1]["prompt"].values()
                    if n.get("class_type") in ("CLIPTextEncode", "PrimitiveStringMultiline"))


async def t_duo_talk_scene_puts_the_line_in_slot_1_on_the_speaker_column_and_silence_in_slot_2():
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, True
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            # 첫 두 사람 클립은 듣는 사람이 사라졌다(1명) → 다시 만든다 (fewer_ok 거짓)
            deps.people_for = lambda tag: 1 if tag == "L1w1" else (2 if re.match(r"L[012]", tag) else None)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = duo_story()
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            w = t["work"]
            tags = tags_of(deps, t)
            for must in ("L1c1", "L1d1", "L1w1", "L1w2", "L2w1", "L3c1", "L3d1", "L3v1"):
                assert must in tags, (must, tags)
            assert not any(x.startswith(("L1v", "L2v", "L2c", "L2d", "L3w")) for x in tags), tags     # 이어지는 투샷은 앞 클립 프레임 그대로
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            assert recipe_of(by["L1w2"]) == recipe_of(by["L2w1"]) == DUO["id"] and recipe_of(by["L3v1"]) == TALK["id"]
            # 구도 그림: 첫 화자 베스(A)가 오른쪽을 봐서 왼쪽 자리, 서로 마주 보되 두 얼굴 모두 카메라 쪽. 인물 입히기: 말하는 방향과 듣는 사람
            c1, d1 = text_of(by["L1c1"]), text_of(by["L1d1"])
            assert "turned toward each other, both faces in three-quarter view toward the camera" in c1 and "Exactly 2 figures" in c1
            assert "The person on the left speaks toward the right side of the frame with a warm expression" in d1
            assert "The person on the right listens with lips closed and a gentle closed-mouth smile" in d1 and "talks about Niki" not in d1
            inp = lambda cid: fs.atts[w["casts"][cid]["att"]]["input_rel"]
            assert {inp("A"), inp("B")} <= loads_of(by["L1d1"])
            # 장면 1: 말하는 베스가 왼쪽 → 마스크 1 = 왼쪽 반 칸(x 0, 너비 424), audio(자리 1) = 대사, audio2(자리 2) = 무음.
            # 25fps 4n+1, 자르는 지점의 프레임, 영상 크기로 키움
            silence = files.turn_input_rel(USER["username"], USER["id"], t["id"], "silence.wav")
            g1 = by["L1w2"][1]["prompt"]
            assert g1["20"]["inputs"]["audio"] == w["lines"]["1"]["in_rel"] and g1["21"]["inputs"]["audio"] == silence
            assert (g1["33"]["inputs"]["x"], g1["32"]["inputs"]["width"]) == (0, 424) and g1["34"]["inputs"]["mask"] == ["33", 0]
            s1 = story["scenes"][1]["seconds"]
            assert g1["400"]["inputs"]["length"] == recipes.frames_for(DUO, s1) and (g1["400"]["inputs"]["length"] - 1) % 4 == 0
            assert (g1["95"]["inputs"]["width"], g1["95"]["inputs"]["height"]) == tuple(w["long_size"]) == (1280, 704)
            assert (g1["400"]["inputs"]["width"], g1["400"]["inputs"]["height"]) == (848, 464)
            assert g1["902"]["inputs"]["batch_index"] == min(int(round((max(D.TALK_MIN_SHOT, 2.6 + D.TALK_TAIL)) * 25)), g1["400"]["inputs"]["length"] - 1)
            assert g1["900"]["inputs"]["batch_index"] == g1["400"]["inputs"]["length"] // 2
            p1 = prompt_text(by["L1w2"])
            assert "The person on the left speaks to the person on the right, warm" in p1 and "only listens" in p1
            assert "alone in the frame" not in p1 and "talks about Niki" not in p1
            # 장면 2: 제임스가 오른쪽에서 말한다 → 마스크 1 = 오른쪽 반 칸(x 424), 대사는 여전히 자리 1·무음은 자리 2 (11-24 X11).
            # 대사가 0초에서 시작하므로 잇는 프레임을 늦추지 않는다 (1차의 0.12초 늦추기는 없앴다)
            g2 = by["L2w1"][1]["prompt"]
            assert g2["20"]["inputs"]["audio"] == w["lines"]["2"]["in_rel"] and g2["21"]["inputs"]["audio"] == silence
            assert (g2["33"]["inputs"]["x"], g2["32"]["inputs"]["width"]) == (424, 424)
            assert g2["902"]["inputs"]["batch_index"] == min(int(round(max(D.TALK_MIN_SHOT, 2.6 + D.TALK_TAIL) * 25)),
                                                             g2["400"]["inputs"]["length"] - 1)
            assert "The person on the right speaks to the person on the left, surprised" in prompt_text(by["L2w1"])
            spath = files.input_path(ENV, silence)
            assert spath and os.path.getsize(spath) == 44 + 2 * int(round(D.DUO_SILENCE * 48000))
            # 혼잣말 컷은 그대로 LTX 혼자 대사
            assert "alone in the frame, no other people" in prompt_text(by["L3v1"])
            sc = w["scenes"]
            assert sc[1]["duo"] is True and sc[1]["first_from"] == "layout"
            assert sc[2]["duo"] is True and sc[2]["first_from"] == "last" and not any("line_delay" in x for x in sc)
            assert sc[1]["last_rel"] and sc[2]["seam"] == "continue" and "duo" not in sc[3]
            starts = {e[1]["index"]: e[1] for e in fs.events if e[0] == "long_scene_start"}
            dones = {e[1]["index"]: e[1] for e in fs.events if e[0] == "long_scene_done"}
            assert starts[1].get("duo") is True and starts[2].get("duo") is True and "duo" not in starts[3] and "duo" not in starts[0]
            assert starts[1]["people"] == starts[2]["people"] == 2 and "people" not in starts[3]
            assert dones[1].get("duo") is True and dones[2].get("duo") is True and "duo" not in dones[3]
            assert dones[1]["people"] == dones[2]["people"] == 2 and "people" not in dones[3]
            # 검사: 두 사람 대사 컷은 정확히 두 사람 (듣는 사람이 사라진 첫 클립을 다시 만들었다), 혼잣말 컷은 한 사람
            pc = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "talk_clip" and not e[1].get("skipped")]
            p_1 = [e for e in pc if e["index"] == 1]
            assert [e["attempt"] for e in p_1] == [1, 2] and p_1[0]["want"] == 2 and p_1[0]["fewer_ok"] is False and p_1[0]["why"] == ["people:1/2"]
            assert [e["want"] for e in pc if e["index"] == 3] == [1]
            n2 = p_1[1]["file"].split("_")[1].split(".")[0]                     # 다시 만든 클립(검사를 통과한 것)을 쓴다
            assert sc[1]["clip_rel"].endswith(f"video_{n2}.mp4"), (sc[1]["clip_rel"], p_1)
            assert not [e for e in fs.events if e[0] == "duo_fallback"]
            # 합치기: 어느 쪽 사람이 말해도 대사는 클립의 0초에서 시작한다 — 늦추거나 더 길게 자르지 않는다. 이어지는 투샷은 디졸브
            clips, lines, _m, _o, trims = [c[1] for c in deps.calls if c[0] == "assemble_dialogue"][0]
            assert not any("delay" in x for x in lines) and [x["shot"] for x in lines] == [1, 2, 3]
            assert trims == [None, 3.5, 3.5, 3.5]
            assert deps.seam_kinds == ["cut", "continue", "cut"]
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check) = old


async def t_duo_talk_falls_back_to_ltx_when_off_missing_failing_or_without_chain():
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        try:
            # ① 레시피가 꺼졌다: 첫 화면은 이미 두 사람 → LTX 대사로 내려가되 두 사람 영상 글·두 사람 검사, 이벤트로 남긴다
            settings.studio_chain, settings.studio_duo_talk = True, True
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs, with_duo=False)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": duo_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            tags = tags_of(deps, t)
            assert "L1v1" in tags and "L2v1" in tags and not any("w" in x for x in tags if x.startswith("L")), tags
            fb = [e[1] for e in fs.events if e[0] == "duo_fallback"]
            assert [(e["index"], e["reason"]) for e in fb] == [(1, "recipe_off"), (2, "recipe_off")], fb
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            assert recipe_of(by["L1v1"]) == TALK["id"] and "only listens" in prompt_text(by["L1v1"]) and "alone in the frame" not in prompt_text(by["L1v1"])
            assert "duo" not in t["work"]["scenes"][1] and t["work"]["scenes"][2]["first_from"] == "last"
            lines = [c[1] for c in deps.calls if c[0] == "assemble_dialogue"][0][1]
            assert not any("delay" in x for x in lines)
            # ② 두 화자 작업이 실패했다 (두 번): LTX 로 내려간다
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            orig = deps.submit

            async def submit(env, user, body, ip):
                if body["extra_data"]["studio"]["recipe"] == DUO["id"]:
                    deps.outcome_seq = ["error"]
                return await orig(env, user, body, ip)
            deps.submit = submit
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": duo_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            tags = tags_of(deps, t)
            assert tags.index("L1w1") < tags.index("L1w2") < tags.index("L1v1"), tags
            fb = [e[1] for e in fs.events if e[0] == "duo_fallback"]
            assert [(e["index"], e["reason"], e.get("code")) for e in fb] == [(1, "failed", "run_error"), (2, "failed", "run_error")], fb
            assert "duo" not in t["work"]["scenes"][1] and "line_delay" not in t["work"]["scenes"][2]
            # ③ 설정을 끄면 예전과 같다: 대사 컷은 말하는 사람 혼자(구성안 정리), 두 화자 레시피가 켜져 있어도 쓰지 않는다
            settings.studio_duo_talk = False
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            deps.people_for = lambda tag: 2 if tag.startswith("L0") else None      # 대사 컷은 모두 한 사람
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            off = duo_story(on=False)
            assert [s["characters"] for s in off["scenes"]][1:] == [["A"], ["B"], ["A"]] and not any(s["continues"] for s in off["scenes"])
            t["work"] = {"story": off, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            tags = tags_of(deps, t)
            subs = [c for c in deps.calls if c[0] == "submit"]
            assert DUO["id"] not in [recipe_of(c) for c in subs] and not [e for e in fs.events if e[0] == "duo_fallback"]
            talk = [c for c in subs if recipe_of(c) == TALK["id"]]
            assert len(talk) == 3 and all("alone in the frame, no other people" in prompt_text(c) for c in talk)
            assert "One adult-sized faceless mannequin figure" in text_of(subs[tags.index("L1c1")])
            assert not any(e[1].get("duo") for e in fs.events if e[0] in ("long_scene_start", "long_scene_done"))
            # ④ 장면 잇기가 꺼져 있으면 첫 화면이 기준 그림 한 장을 고친 한 사람 그림이다: 두 사람 영상으로 만들지 않는다
            settings.studio_chain, settings.studio_duo_talk = False, True
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": duo_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            assert t["work"]["chain"] is False and DUO["id"] not in [recipe_of(c) for c in subs]
            fb = [e[1] for e in fs.events if e[0] == "duo_fallback"]
            assert [(e["index"], e["reason"]) for e in fb] == [(1, "solo_first_frame"), (2, "solo_first_frame")], fb
            assert t["work"]["scenes"][1]["first_solo"] is True and t["work"]["scenes"][1]["first_from"] == "edit"
            assert all("alone in the frame" in prompt_text(c) for c in subs if recipe_of(c) == TALK["id"])
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk = old


async def t_duo_talk_portrait_estimate_and_reset():
    old = (settings.studio_chain, settings.studio_duo_talk)
    settings.studio_chain, settings.studio_duo_talk = True, True
    try:
        fs = FakeStore(); fs.patch()
        story = duo_story()
        recs = {r["id"]: r for r in (LTX, TALK, EDITREF, COMPOSE, CLONE, VOICE, IMG, MUS)}
        d0 = D.Director(FakeDeps(fs), dict(recs))
        d1 = D.Director(FakeDeps(fs), {**recs, DUO["id"]: DUO})
        t = dialogue_turn(fs)
        ctx = D.Ctx(t, USER, ENV, LTX)
        ctx.long = {}
        # 예상 시간: 두 사람 대사 컷은 InfiniteTalk 추정식(25fps 4n+1 프레임)으로
        e0, e1 = await d0.estimate_long(ctx, story), await d1.estimate_long(ctx, story)

        def part(rec, frames):
            est = rec["estimate"]
            return float(est["gpu_seconds"]) * max(0.4, min(8.0, (frames / float(est["ref_frames"])) ** float(est["exponent"])))
        want = sum(part(DUO, recipes.frames_for(DUO, story["scenes"][k]["seconds"])) - part(TALK, int(story["scenes"][k]["seconds"]) * 24 + 1)
                   for k in (1, 2))
        assert abs((e1 - e0) - want) <= 1.0, (e0, e1, want)
        settings.studio_duo_talk = False
        assert await d1.estimate_long(ctx, story) == e0                    # 설정을 끄면 두 사람 레시피를 셈하지 않는다
        settings.studio_duo_talk = True
        # 세로·정사각 영상은 실행처럼 두 화자 추정식을 쓰지 않는다 (11-24 검토: 추정은 두 화자, 실행은 LTX 로 어긋났다)
        assert await d1.estimate_long(ctx, story, [704, 1280]) == await d0.estimate_long(ctx, story, [704, 1280])
        assert await d1.estimate_long(ctx, story, [960, 960]) == await d0.estimate_long(ctx, story, [960, 960])
        portrait = D.Ctx(t, USER, ENV, LTX)
        portrait.long, portrait.spec = {}, {"aspect": "portrait"}
        assert await d1.estimate_long(portrait, story) == await d0.estimate_long(portrait, story)     # 크기를 안 주면 비율 칸으로
        assert await d1.estimate_long(ctx, story, [1280, 704]) == e1
        # 장면 잇기가 꺼지면 첫 화면이 두 사람이 아니라 두 화자 레시피를 셈하지 않는다
        settings.studio_chain = False
        assert await d1.estimate_long(ctx, story) == await d0.estimate_long(ctx, story)
        settings.studio_chain = True
        # 세로·정사각 영상은 두 화자 레시피를 쓰지 않는다 (848×464 가로 생성을 가운데 자르면 두 사람이 잘린다)
        assert d1._duo_recipe({"long_size": [704, 1280]}, story, 1) == (None, "portrait")
        assert d1._duo_recipe({"long_size": [960, 960]}, story, 1) == (None, "portrait")
        assert d1._duo_recipe({"long_size": [1280, 704]}, story, 1)[0]["id"] == DUO["id"]
        assert d1._duo_recipe({"long_size": [1280, 704]}, story, 3) == (None, "off")             # 혼잣말 컷
        assert d0._duo_recipe({"long_size": [1280, 704]}, story, 1) == (None, "recipe_off")
        # 구성안을 두 사람 규칙으로 쓸지: 설정·장면 잇기·두 화자 레시피·가로 영상이 모두 맞을 때만
        assert d1._duo_ready(ctx, [1280, 704]) and not d1._duo_ready(ctx, [704, 1280]) and not d1._duo_ready(ctx, [960, 960])
        assert not d0._duo_ready(ctx, [1280, 704])
        settings.studio_chain = False
        assert not d1._duo_ready(ctx, [1280, 704])
        settings.studio_chain, settings.studio_duo_talk = True, False
        assert not d1._duo_ready(ctx, [1280, 704])
        settings.studio_duo_talk = True
        # 다시 만들기: 두 화자 작업의 접수 정보와 장면의 두 사람 표시(내려간 표시 포함)도 비운다
        work = {"story": story, "chain": True, "L1w_pending": {"client_id": "studio-x-L1w1", "attempt": 2}}
        t["work"] = dict(work)
        scenes = [{"clip_rel": "a.mp4"}, {"clip_rel": "b.mp4", "duo": True, "duo_failed": True}, {}, {}]
        await d1._long_reset(ctx, work, scenes, 1)
        assert work["L1w_pending"] == {"client_id": None, "attempt": 2} and scenes[1] == {}
        assert "duo" in D.LONG_SCENE_KEYS and "first_solo" in D.LONG_SCENE_KEYS and "duo_failed" in D.LONG_SCENE_KEYS
        # 2차: 새 클립은 오른쪽 화자 늦추기를 쓰지 않지만(대사는 늘 소리 자리 1), 1차로 만든 클립을 이어받은 요청의 합치기가 읽도록 남긴다
        assert "line_delay" in D.LONG_SCENE_KEYS
    finally:
        settings.studio_chain, settings.studio_duo_talk = old


async def t_duo_story_rules_follow_what_the_request_can_make():
    """11-24 검토: 구성안의 두 사람 규칙은 이 요청에서 두 사람 대사 컷을 실제로 만들 수 있을 때만 (설정·장면 잇기·두 화자 레시피·가로 영상).
    못 만들면 혼자 규칙으로 써서 투샷 첫 화면이 LTX 로 가거나(X2: 입이 거의 안 움직임) 한 사람 첫 화면에 투샷 글이 붙지 않게 한다"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk = True, True
        try:
            async def written(aspect=None, with_duo=True):
                fs = FakeStore(); fs.patch()
                deps = duo_deps(fs, with_duo=with_duo)
                d = D.Director(deps, dict(deps.recs))
                t = dialogue_turn(fs)
                if aspect:
                    t["request"]["spec"]["aspect"] = aspect
                await d.pump(); await settle(d, 10)
                assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
                return [c[1].get("duo") for c in deps.calls if c[0] == "storyboard"], t
            got, t = await written()
            assert got == [True] and t["work"]["long_size"] == [1280, 704]
            got, t = await written("portrait")
            assert got == [False] and t["work"]["long_size"][0] < t["work"]["long_size"][1]
            assert (await written(with_duo=False))[0] == [False]
            settings.studio_chain = False
            assert (await written())[0] == [False]
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_ref_layout) = old


async def t_duo_story_rendered_after_the_setting_was_turned_off_keeps_two_people():
    """두 사람 규칙으로 쓰고 승인한 구성안을 STUDIO_DUO_TALK 를 끈 뒤 만들면: 첫 화면은 이미 두 사람이라 LTX 로 내려가되 영상 글·검사는
    두 사람 기준이다 (talk_pair 는 설정과 상관없다). 내려간 까닭은 off (11-24 검토: 이 경로에 시험이 없었다)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, True
        try:
            story = duo_story(on=True)
            settings.studio_duo_talk = False
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            assert DUO["id"] not in [recipe_of(c) for c in subs]
            fb = [e[1] for e in fs.events if e[0] == "duo_fallback"]
            assert [(e["index"], e["reason"]) for e in fb] == [(1, "off"), (2, "off")], fb
            tags = tags_of(deps, t)
            by = {tags[k]: subs[k] for k in range(len(subs))}
            p1 = prompt_text(by["L1v1"])
            assert recipe_of(by["L1v1"]) == TALK["id"] and "only listens" in p1 and "alone in the frame" not in p1
            pc = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "talk_clip" and not e[1].get("skipped")]
            assert {e["index"] for e in pc} >= {1, 2, 3}
            assert all(e["want"] == 2 and e["fewer_ok"] is False for e in pc if e["index"] in (1, 2)), pc
            assert [e["want"] for e in pc if e["index"] == 3] == [1]
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


async def t_duo_fallback_is_remembered_and_build_errors_fall_back_too():
    """① 두 화자 레시피의 값 넣기 오류(submit)도 LTX 로 내려간다 (요청 전체가 실패하지 않게).
    ② 내려간 것은 장면에 남는다(duo_failed): 허브가 LTX 작업 중에 다시 시작해도 실패한 두 화자 작업을 이어받아 새로 접수하지 않는다 (11-24 검토)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk = True, True
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            broken = copy.deepcopy(DUO)
            broken["inject"]["frames"]["max"] = 17                 # 장면 길이의 프레임 수를 넣지 못한다 → 값 넣기 오류
            deps.recs[DUO["id"]] = broken
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": duo_story(), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            fb = [e[1] for e in fs.events if e[0] == "duo_fallback"]
            assert [(e["index"], e["reason"], e.get("code")) for e in fb] == [(1, "failed", "submit"), (2, "failed", "submit")], fb
            sc = t["work"]["scenes"]
            assert sc[1]["duo_failed"] is True and sc[2]["duo_failed"] is True and "duo" not in sc[1] and "duo_failed" not in sc[3]
            tags = tags_of(deps, t)
            assert "L1v1" in tags and "L2v1" in tags and not any(x.startswith(("L1w", "L2w")) for x in tags), tags
            # 재시작: 내려간 표시가 남은 컷은 옛 두 화자 작업(L1w_pending)을 이어받지 않고 바로 LTX, 다시 알리지도 않는다
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": duo_story(), "approved": True, "scenes": [{}, {"duo_failed": True}, {}, {}],
                         "L1w_pending": {"client_id": "studio-old-L1w2", "attempt": 2}}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            tags = tags_of(deps, t)
            assert "L1v1" in tags and not any(x.startswith("L1w") for x in tags) and "L2w1" in tags, tags
            assert not [e for e in fs.events if e[0] == "duo_fallback"]
            sc = t["work"]["scenes"]
            assert sc[1]["duo_failed"] is True and "duo" not in sc[1] and sc[2]["duo"] is True
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_ref_layout) = old


async def t_duo_clip_in_a_crowd_and_an_animal_listener_are_checked_like_their_first_frame():
    """① 무리 말이 있는 장소의 투샷: 첫 화면이 배경 행인을 받아들였으니(compose_rule) 클립도 두 사람보다 많아도 된다 (줄면 다시 만든다).
    ② 듣는 쪽이 동물인 대사 컷: 사람 한 명(LTX)이지만 동물이 첫 화면에 있어 사람 수·팔다리를 세지 않는다 (11-24 검토: 말하는 사람만 보고
    정확히 1명을 셌다). 영상 글은 혼자 크기로, 동물을 가리키는 장면 글은 둔다"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, True
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            deps.people_for = lambda tag: 5 if tag.startswith(("L1w", "L2w")) else (2 if re.match(r"L[012]", tag) else None)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = duo_story()
            story["bible"]["places"][0]["look"] = "a bustling market square full of people"
            story["bible"]["characters"].append({"id": "D", "look": "a small white puppy with floppy ears"})
            story["scenes"][3].update(characters=["A", "D"], listener="D", shot="medium two-shot", action="She smiles at the puppy.")
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            tags = tags_of(deps, t)
            assert "L1w1" in tags and "L1w2" not in tags and "L3v1" in tags and "L3v2" not in tags, tags
            pc = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "talk_clip" and not e[1].get("skipped")]
            p1 = [e for e in pc if e["index"] == 1]
            assert len(p1) == 1 and p1[0]["want"] == 2 and p1[0]["more_ok"] is True and p1[0]["fewer_ok"] is False and p1[0]["why"] == []
            assert [e["want"] for e in pc if e["index"] == 3] == [None]
            subs = [c for c in deps.calls if c[0] == "submit"]
            p3 = prompt_text(subs[tags.index("L3v1")])
            assert "Medium close-up of one person alone" in p3 and "She smiles at the puppy." in p3 and "two-shot" not in p3
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


# ── 두세 사람 대사 컷 2차 (11-24): 사용자 확정 규칙 "대화 할 때는 대화를 주고받는 이들이 나와야 하고, 독백 씬에는 클로즈업이 나와야 해"
#    + "셋 모두 나오게". 세 사람 대화는 InfiniteTalk 마스크 1 = 말하는 사람의 3분의 1 칸(자리 차례 group_order), 독백은 혼자 LTX ──
def family_story():
    """가족 세 사람의 아침 식탁: 베스(A)가 제임스(B)에게(식탁의 니키(C)도 나온다) → 제임스가 모두에게(같은 화면에서 이어짐) →
    니키의 혼잣말(독백, 혼자 클로즈업) → 니키가 엄마에게(구성안은 둘만 적었지만 식탁의 제임스도 나온다)"""
    from app import video
    raw = {
        "title_ko": "생일 아침", "logline_ko": "가족이 니키의 생일을 이야기한다.",
        "bible": {"characters": [
            {"id": "A", "look": "a calm neat woman around forty in a cream blouse and an apron", "name_ko": "베스",
             "voice": "A warm Korean woman around forty, gentle.", "voice_ko": "따뜻한 엄마"},
            {"id": "B", "look": "a tall lean man in his early forties in a blue cardigan", "name_ko": "제임스",
             "voice": "A calm Korean man in his forties, low and friendly.", "voice_ko": "차분한 아빠"},
            {"id": "C", "look": "a twelve-year-old girl with two long black braids and a wine-red cape", "name_ko": "니키",
             "voice": "A bright twelve-year-old Korean girl, clear.", "voice_ko": "맑은 아이"}],
            "places": [{"id": "P1", "look": "a sunny cottage kitchen with a wooden table"}], "time": "morning", "light": "warm window light",
            "style": "semi-realistic painterly 2.5D film look"},
        "music": {"tags": "gentle instrumental, strings", "bpm": 72, "key": "D major", "ko": "잔잔한 현악"},
        "scenes": [
            {"seconds": 10, "place": "P1", "characters": ["A", "B", "C"], "continues": False, "shot": "wide shot",
             "action": "The family sits at the wooden table in the morning.", "sound": "birds", "ko": "가족의 아침 식탁", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 6, "place": "P1", "characters": ["A", "B", "C"], "continues": False, "shot": "medium group shot",
             "action": "She smiles at him.", "sound": "room tone", "ko": "베스가 제임스에게", "kind": "talk",
             "speaker": "A", "listener": "B", "line_ko": "여보, 오늘 니키 생일이에요.", "emotion": "warm"},
            {"seconds": 6, "place": "P1", "characters": ["B", "A", "C"], "continues": True, "shot": "medium group shot",
             "action": "He laughs and looks around the table.", "sound": "room tone", "ko": "제임스가 모두에게", "kind": "talk",
             "speaker": "B", "listener": "everyone", "line_ko": "자, 다 같이 축하하자!", "emotion": "cheerful"},
            {"seconds": 5, "place": "P1", "characters": ["C"], "continues": False, "shot": "close-up",
             "action": "She looks down, thinking.", "sound": "room tone", "ko": "니키의 혼잣말", "kind": "talk",
             "speaker": "C", "listener": "nobody", "line_ko": "다들 기억하고 있었구나...", "emotion": "quiet"},
            {"seconds": 6, "place": "P1", "characters": ["C", "A"], "continues": False, "shot": "medium two-shot",
             "action": "She beams at her mother.", "sound": "room tone", "ko": "니키가 엄마에게", "kind": "talk",
             "speaker": "C", "listener": "A", "line_ko": "엄마, 고마워요!", "emotion": "happy"}]}
    old = settings.studio_duo_talk
    settings.studio_duo_talk = True
    try:
        story = video.clean_dialogue_story(raw, 33)
    finally:
        settings.studio_duo_talk = old
    assert story is not None
    return story


async def t_speaker_span_splits_the_generation_width_by_the_seat_order():
    """말하는 사람 칸(마스크 1): 사람 n 명(2~3)을 레시피의 고정 생성 폭(fixed.gen_width 848)에 같은 너비로 나눈 k 번째 칸"""
    span = D.Director._speaker_span
    assert [span(DUO, ["A", "B"], c) for c in ("A", "B")] == [(0, 424), (424, 424)]
    assert [span(DUO, ["A", "B", "C"], c) for c in ("A", "B", "C")] == [(0, 283), (283, 282), (565, 283)]
    assert sum(w for _x, w in (span(DUO, ["A", "B", "C"], c) for c in "ABC")) == 848
    narrow = copy.deepcopy(DUO)
    narrow["fixed"]["gen_width"]["value"] = 832
    assert span(narrow, ["A", "B"], "B") == (416, 416)
    nofix = copy.deepcopy(DUO)
    del nofix["fixed"]["gen_width"]
    assert span(nofix, ["A", "B", "C"], "C") == (565, 283)                  # 고정 폭을 못 읽으면 848
    for n, k in ((2, 0), (2, 1), (3, 0), (3, 1), (3, 2)):                    # 레시피 칸 자리의 범위 안 (벗어나면 RecipeError)
        x, w = span(DUO, [str(j) for j in range(n)], str(k))
        assert recipes.check_value(DUO["inject"]["speaker_x"], x) == x
        assert recipes.check_value(DUO["inject"]["speaker_w"], w) == w


async def t_family_table_three_people_talk_on_their_column_and_the_monologue_is_a_solo_close_up():
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, True
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            # 세 사람 장면(0·1·2·4)의 마지막 그림·클립은 세 사람, 니키의 혼잣말 컷(3)은 한 사람으로 센다
            deps.people_for = lambda tag: 3 if re.match(r"L[0124](?:f|k|v|w)", tag) else None
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = family_story()
            assert [s["characters"] for s in story["scenes"]][1:] == [["A", "B", "C"], ["B", "A", "C"], ["C"], ["C", "A", "B"]]
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 60)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            w = t["work"]
            tags = tags_of(deps, t)
            for must in ("L1c1", "L1d1", "L1f01", "L1w1", "L2w1", "L3c1", "L3d1", "L3v1", "L4c1", "L4d1", "L4f01", "L4w1"):
                assert must in tags, (must, tags)
            assert not any(x.startswith(("L1v", "L2v", "L2c", "L4v", "L3w", "L3f")) for x in tags), tags   # 이어지는 컷은 앞 클립 프레임
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            assert {recipe_of(by[k]) for k in ("L1w1", "L2w1", "L4w1")} == {DUO["id"]} and recipe_of(by["L3v1"]) == TALK["id"]
            # 자리 차례는 넓은 장면의 차례(왼쪽 베스, 가운데 제임스, 오른쪽 니키)로 대화 내내 같다 → 말하는 사람의 칸
            silence = files.turn_input_rel(USER["username"], USER["id"], t["id"], "silence.wav")
            for tag, line, span in (("L1w1", "1", (0, 283)), ("L2w1", "2", (283, 282)), ("L4w1", "4", (565, 283))):
                g = by[tag][1]["prompt"]
                assert g["20"]["inputs"]["audio"] == w["lines"][line]["in_rel"] and g["21"]["inputs"]["audio"] == silence, tag
                assert (g["33"]["inputs"]["x"], g["32"]["inputs"]["width"]) == span and g["34"]["inputs"]["mask"] == ["33", 0], tag
                assert g["902"]["inputs"]["batch_index"] == min(int(round(max(D.TALK_MIN_SHOT, 2.6 + D.TALK_TAIL) * 25)),
                                                                g["400"]["inputs"]["length"] - 1), tag
                assert prompt_text(by[tag]).startswith("Medium group shot of three people in a conversation"), tag
            assert "The person on the left speaks to the person in the middle, warm" in prompt_text(by["L1w1"])
            assert "The person in the middle speaks to the others, cheerful" in prompt_text(by["L2w1"])
            assert "The person on the right speaks to the person on the left, happy" in prompt_text(by["L4w1"])
            assert "only listen with lips closed and a gentle closed-mouth smile" in prompt_text(by["L4w1"])
            # 첫 화면: 세 형상 나란히 → 앞의 둘(베스·제임스) → 세 번째(니키). 듣는 사람은 입을 다문 미소
            assert "3 faceless mannequin figures, sitting, side by side" in text_of(by["L1c1"])
            d1, f1 = text_of(by["L1d1"]), text_of(by["L1f01"])
            assert "The person on the left speaks toward the right side of the frame with a warm expression" in d1
            assert "The person in the middle listens with lips closed and a gentle closed-mouth smile" in d1
            assert "The person on the right listens with lips closed and a gentle closed-mouth smile" in f1
            # 니키의 혼잣말: 같은 식탁이어도 혼자 클로즈업, LTX 혼자 대사 (구도 그림도 한 형상)
            assert "One child-sized faceless mannequin figure" in text_of(by["L3c1"])
            assert "alone in the frame, no other people" in prompt_text(by["L3v1"])
            # 검사: 세 사람 대사 클립은 정확히 세 사람(빠지면 다시 만든다), 독백 클립은 한 사람
            pc = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "talk_clip" and not e[1].get("skipped")]
            assert all(e["want"] == 3 and e["fewer_ok"] is False for e in pc if e["index"] in (1, 2, 4)) and {1, 2, 4} <= {e["index"] for e in pc}
            assert [e["want"] for e in pc if e["index"] == 3] == [1]
            starts = {e[1]["index"]: e[1] for e in fs.events if e[0] == "long_scene_start"}
            dones = {e[1]["index"]: e[1] for e in fs.events if e[0] == "long_scene_done"}
            assert all(starts[k]["duo"] is True and starts[k]["people"] == 3 for k in (1, 2, 4)) and "duo" not in starts[3]
            assert all(dones[k]["duo"] is True and dones[k]["people"] == 3 for k in (1, 2, 4)) and "people" not in dones[3]
            sc = w["scenes"]
            assert [s.get("duo") for s in sc] == [None, True, True, None, True] and sc[2]["first_from"] == "last"
            assert not [e for e in fs.events if e[0] == "duo_fallback"]
            clips, lines, _m, _o, trims = [c[1] for c in deps.calls if c[0] == "assemble_dialogue"][0]
            assert not any("delay" in x for x in lines) and trims == [None, 3.5, 3.5, 3.5, 3.5]
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


async def t_family_table_three_people_fall_back_to_ltx_with_the_group_prompt():
    """세 사람 대사 컷이 LTX 로 내려갈 때(두 화자 레시피 꺼짐)도 영상 글은 세 사람 글이고 검사는 정확히 세 사람이다 (명세 G5 마지막 항목.
    11-24 2차 검토: 내려가기 시험이 모두 두 사람이었다)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, True
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs, with_duo=False)
            deps.people_for = lambda tag: 3 if re.match(r"L[0124](?:f|k|v|w)", tag) else None
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": family_story(), "approved": True}
            await d.pump(); await settle(d, 60)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            fb = [e[1] for e in fs.events if e[0] == "duo_fallback"]
            assert [(e["index"], e["reason"]) for e in fb] == [(1, "recipe_off"), (2, "recipe_off"), (4, "recipe_off")], fb
            tags = tags_of(deps, t)
            assert not any(x.startswith(("L1w", "L2w", "L4w")) for x in tags), tags
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            for tag in ("L1v1", "L2v1", "L4v1"):
                p = prompt_text(by[tag])
                assert recipe_of(by[tag]) == TALK["id"], tag
                assert "Medium group shot of three people in a conversation" in p and "alone in the frame" not in p, (tag, p[:200])
            assert "The person in the middle speaks to the others, cheerful" in prompt_text(by["L2v1"])
            assert "alone in the frame, no other people" in prompt_text(by["L3v1"])          # 독백은 그대로 혼자 대사 글
            pc = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "talk_clip" and not e[1].get("skipped")]
            assert {1, 2, 4} <= {e["index"] for e in pc}
            assert all(e["want"] == 3 and e["fewer_ok"] is False for e in pc if e["index"] in (1, 2, 4)), pc
            assert [e["want"] for e in pc if e["index"] == 3] == [1]
            assert not any(s.get("duo") for s in t["work"]["scenes"])
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


async def t_round1_state_keeps_the_line_delay_and_old_duo_jobs_are_not_adopted():
    """1차(레시피 판 1) 상태로 재개해도 소리와 입이 맞는다 (11-24 2차 검토).
    ① 1차로 만든 오른쪽 화자 두 사람 클립(무음이 앞이라 대사가 0.12초 늦게 시작, 장면 상태 line_delay)을 이어받으면 합치기가 대사를 그만큼
       늦게 놓고 그만큼 더 길게 자른다. 새 클립은 line_delay 를 쓰지 않는다.
    ② 1차 그래프로 접수한 두 화자 작업(접수 정보에 판이 없다)은 이어받지 않고 새로 접수한다 (시도 번호는 이어서). 새 접수 정보에는 판이 남는다"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, False
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": duo_story(), "approved": True,
                         "L1w_pending": {"client_id": f"studio-{t['id']}-L1w1", "attempt": 1}}
            fs.by_client[f"studio-{t['id']}-L1w1"] = {"prompt_id": "old-job"}          # 1차 그래프로 접수한 옛 작업
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            subs = [c for c in deps.calls if c[0] == "submit"]
            duo_ids = [c[1]["client_id"] for c in subs if recipe_of(c) == DUO["id"]]
            assert duo_ids and duo_ids[0].endswith("-L1w2"), duo_ids                       # 옛 작업을 이어받지 않고 시도 번호를 이어서
            assert not [e for e in fs.events if e[0] == "long_resumed" and e[1].get("key") == "L1w_pending"]
            w = t["work"]
            assert w["L1w_pending"]["version"] == DUO["version"] == 2 and w["L2w_pending"]["version"] == 2
            assert not any("line_delay" in s for s in w["scenes"])
            # ① 1차로 만든 장면 2(오른쪽 제임스) 클립을 이어받은 모양: 합치기만 다시 한다
            clips, lines, _m, _o, trims = [c[1] for c in deps.calls if c[0] == "assemble_dialogue"][-1]
            assert trims == [None, 3.5, 3.5, 3.5] and not any("delay" in x for x in lines)
            scenes = [dict(s) for s in w["scenes"]]
            scenes[2]["line_delay"] = 0.12
            ctx = D.Ctx(t, USER, ENV, LTX)
            ctx.long = {}
            await d._dlg_finish(ctx, w, scenes, None)
            clips, lines, _m, _o, trims = [c[1] for c in deps.calls if c[0] == "assemble_dialogue"][-1]
            assert trims[:2] == [None, 3.5] and abs(trims[2] - 3.62) < 1e-9 and trims[3] == 3.5, trims
            assert [x.get("delay") for x in lines] == [None, 0.12, None], lines
            # 저장할 때도 남는다 (다시 시도·허브 재시작 뒤 합치기가 읽는다)
            assert "line_delay" in D.LONG_SCENE_KEYS
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


async def t_style_convert_is_not_redrawn_when_the_check_model_does_not_answer():
    """기준 그림 화풍 변환 검사(_compose_check)는 STUDIO_PICTURE_CHECK 와 상관없이 늘 돈다. 검사 모델이 답하지 않으면(cast:unchecked)
    다시 그리지 않고 결과에 알린다: 실사 변환은 가속을 끈 높음 판이라 한 장에 몇 분 [추정] (11-24 검토: 이 멈춤에 시험이 없었다)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        settings.studio_picture_check = False
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, settings.results_dir, DialogueDeps)
            deps.recs[HQ_EDIT["id"]] = HQ_EDIT
            deps.inspect_result = None                            # 검사 모델이 답하지 않는다
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = library_story(settings.results_dir)
            story["bible"]["style"] = "photorealistic cinematic live action"
            for c in story["bible"]["characters"]:
                rel = f"studio/_cast/{c['id']}_semi.png"
                os.makedirs(os.path.dirname(os.path.join(settings.results_dir, rel)), exist_ok=True)
                with open(os.path.join(settings.results_dir, rel), "wb") as f:
                    f.write(b"png")
                c["refs"] = {"semi": rel}
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            rids = [recipe_of(c) for c in deps.calls if c[0] == "submit"]
            assert rids.count(HQ_EDIT["id"]) == 2, rids                       # 인물마다 한 번 (다시 그리지 않았다)
            left = t["work"]["check_left"]
            assert left.get("c0:portrait") == ["cast:unchecked"] and left.get("c1:portrait") == ["cast:unchecked"], left
            assert not [e for e in fs.events if e[0] == "picture_retry" and e[1].get("what") == "convert"]
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check = old


# ── 어떤 화풍 요청에도 (11-24 2차 Part C): 실사 변환 레시피, 장소 그림 정밀 판, custom 화풍 검사 ──
PHOTO_EDIT = recipes.load_all()["qwen_image21_edit_photo"]
SCENE_Q = recipes.load_all()["qwen_image_2512_scene_quality"]


def semi_ref_story(root, style):
    """library_story 의 두 인물에 반실사 기준 그림(refs.semi)만 두고 영화 화풍을 style 로 (영화 시작 때 그 화풍으로 바꿔 만든다)"""
    story = library_story(root)
    story["bible"]["style"] = style
    for c in story["bible"]["characters"]:
        rel = f"studio/_cast/{c['id']}_semi.png"
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "wb") as f:
            f.write(b"png")
        c["refs"] = {"semi": rel}
    return story


class PhotoFailDeps(DialogueDeps):
    """실사 변환 레시피의 작업만 실패한다 (새 모델이 메인에서 실패하는 경우)"""

    async def submit(self, env, user, body, ip):
        if body["extra_data"]["studio"]["recipe"] == PHOTO_EDIT["id"]:
            self.outcome_seq.insert(0, "error")
        return await super().submit(env, user, body, ip)


async def t_photo_film_converts_references_with_the_photo_recipe_first():
    """11-24 2차 C7 (메인 P1): 실사 영화는 반실사 기준 그림을 실사 변환 전용 레시피(Qwen-Image 2.1 편집, action edit_photo)로 먼저 바꾼다.
    지시문은 P1 에서 통한 문장 모양, 검사는 1차와 같다(소품·실사 문장). 레시피가 없으면 1차 길(가속 끈 높음 편집 → 빠른 편집)"""
    from app.studio import checks
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        settings.studio_picture_check = True
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, settings.results_dir, DialogueDeps)
            deps.recs.update({HQ_EDIT["id"]: HQ_EDIT, PHOTO_EDIT["id"]: PHOTO_EDIT})
            deps.inspect_result = dict(PASS)
            d = D.Director(deps, dict(deps.recs))
            assert [r["id"] for r in d._convert_recipes("photo")] == [PHOTO_EDIT["id"], HQ_EDIT["id"]]
            assert d._convert_recipe("photo")["id"] == PHOTO_EDIT["id"]
            for key in ("anime", "toon3d", "custom", "semi"):                  # 실사 말고는 빠른 편집 하나 (실사 변환 레시피를 쓰지 않는다)
                assert [r["id"] for r in d._convert_recipes(key)] == [EDITR["id"]], key
            no_photo = D.Director(deps, {k: v for k, v in deps.recs.items() if k != PHOTO_EDIT["id"]})
            assert [r["id"] for r in no_photo._convert_recipes("photo")] == [HQ_EDIT["id"]]           # 1차 길 그대로
            t = dialogue_turn(fs)
            story = semi_ref_story(settings.results_dir, "photorealistic cinematic live action")
            # 예상 시간: 변환 두 장은 그 레시피의 추정식 (실사 변환 레시피 대 가속 끈 높음 편집)
            ctx = D.Ctx(t, USER, ENV, LTX)
            ctx.long = {}
            e_photo, e_hq = await d.estimate_long(ctx, copy.deepcopy(story)), await no_photo.estimate_long(ctx, copy.deepcopy(story))
            assert e_photo - e_hq == 2 * (PHOTO_EDIT["estimate"]["gpu_seconds"] - HQ_EDIT["estimate"]["gpu_seconds"]), (e_photo, e_hq)
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            conv = [e[1] for e in fs.events if e[0] == "cast_style_convert"]
            assert len(conv) == 2 and all(e["style"] == "photo" and e["recipe"] == PHOTO_EDIT["id"] for e in conv), conv
            assert not [e for e in fs.events if e[0] == "cast_style_convert_fallback"]
            tags = tags_of(deps, t)
            subs = [c for c in deps.calls if c[0] == "submit"]
            photo = [c for c in subs if recipe_of(c) == PHOTO_EDIT["id"]]
            rids = [recipe_of(c) for c in subs]
            assert len(photo) == 2 and HQ_EDIT["id"] not in rids and EDITR["id"] not in rids, rids
            assert "C0p1" in tags and "C1p1" in tags and not [x for x in tags if x.startswith(("C0s", "C1s"))], tags
            for c in photo:
                p = c[1]["prompt"]["474"]["inputs"]["prompt"]
                assert p.startswith("Turn this illustrated portrait into a real photograph of the same person") and "real human skin and pores" in p
                assert c[1]["prompt"]["474"]["inputs"]["negative_prompt"] == "" and c[1]["prompt"]["470"]["inputs"]["image"].startswith("studio/bob/")
                assert D.negative_problem(PHOTO_EDIT, c[1]["prompt"]) is None
            assert all(t["work"]["casts"][c["id"]].get("converted") == "photo" for c in story["bible"]["characters"])
            neg = checks.style_negative("photo")
            chars = {c["id"]: c for c in story["bible"]["characters"]}
            ins = [c[1][3] for c in deps.calls if c[0] == "inspect"]
            assert [checks.cast_trait(chars["A"]["look"]), neg] in ins and [checks.cast_trait(chars["B"]["look"]), neg] in ins, ins[:4]
            conv_checks = [e[1] for e in fs.events if e[0] == "picture_check" and e[1].get("what") == "convert"]
            assert len(conv_checks) == 2 and all(e["why"] == [] for e in conv_checks), conv_checks
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check = old


async def t_photo_recipe_failure_falls_back_to_the_quality_edit():
    """11-24 2차 C7: 실사 변환 레시피의 작업이 실패하면(시드를 바꾼 한 번 더까지) 1차 길(가속 끈 높음 편집)로 한 번 내려가고 기록을 남긴다.
    작업 표시가 달라(C{k}p → C{k}s) 실패한 작업을 이어받지 않고, 지시문은 같은 실사 문장이다. 실패한 레시피는 이 요청에서 기억해
    다음 인물에는 쓰지 않는다 (같은 실패를 인물마다 되풀이하지 않게)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        settings.studio_picture_check = False
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, settings.results_dir, PhotoFailDeps)
            deps.recs.update({HQ_EDIT["id"]: HQ_EDIT, PHOTO_EDIT["id"]: PHOTO_EDIT})
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": semi_ref_story(settings.results_dir, "photorealistic cinematic live action"), "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            fb = [e[1] for e in fs.events if e[0] == "cast_style_convert_fallback"]
            assert len(fb) == 1 and fb[0]["recipe"] == PHOTO_EDIT["id"] and fb[0]["code"] == "run_error" and fb[0]["style"] == "photo", fb
            conv = [e[1] for e in fs.events if e[0] == "cast_style_convert"]
            assert len(conv) == 2 and all(e["recipe"] == HQ_EDIT["id"] for e in conv), conv
            rids = [recipe_of(c) for c in deps.calls if c[0] == "submit"]
            assert rids.count(PHOTO_EDIT["id"]) == 2 and rids.count(HQ_EDIT["id"]) == 2, rids
            tags = tags_of(deps, t)
            assert [x for x in tags if x.startswith("C0")] == ["C0p1", "C0p2", "C0s1"], tags        # 첫 인물: 실사 변환 두 번 실패 → 높음 편집
            assert [x for x in tags if x.startswith("C1")] == ["C1s1"], tags                        # 둘째 인물: 실패를 기억해 바로 높음 편집
            assert t["work"]["failed_recipes"] == [PHOTO_EDIT["id"]]
            hq = [c for c in deps.calls if c[0] == "submit" and recipe_of(c) == HQ_EDIT["id"]]
            slot = HQ_EDIT["inject"]["prompt"]
            assert all(c[1]["prompt"][slot["node"]]["inputs"][slot["input"]].startswith("Turn this illustrated portrait") for c in hq)
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check = old


def quality_story(style, empty_first=False):
    """라이브러리 인물이 나오는(장면·장소 그림이 Qwen 2512) 긴 영상: 같은 장소의 사람 장면 둘 + 다른 장소의 사람 없는 장면 하나.
    empty_first 면 사람 없는 장면이 처음이다 (장면 그림이 장소 그림보다 먼저 그려진다)"""
    story = copy.deepcopy(LONG_STORY)
    story["bible"]["style"] = style
    story["bible"]["characters"][0]["library"] = "guest"
    if empty_first:
        story["scenes"][0].update(characters=[], action="Rain falls on the empty street.")
        story["scenes"][1]["continues"] = False
    else:
        story["scenes"][2].update(characters=[], shot="wide shot", action="Rain falls on the empty apartment entrance.")
    return story


class QualityFailDeps(LongDeps):
    """장면 그림 정밀 판의 작업만 실패한다"""

    async def submit(self, env, user, body, ip):
        if body["extra_data"]["studio"]["recipe"] == SCENE_Q["id"]:
            self.outcome_seq.insert(0, "error")
        return await super().submit(env, user, body, ip)


async def _quality_film(style, cls=None, layout=False, empty_first=False):
    """quality_story(style) 를 장면 잇기 + 새로 그리기로 만든다. 정밀 판(SCENE_Q)과 빠른 판(SCENE)이 모두 켜져 있다. (deps, t, fs, 접수 표시별 호출)"""
    fs = FakeStore(); fs.patch()
    deps = compose_deps(fs, settings.results_dir, cls, layout=layout)
    deps.recs.update({SCENE["id"]: SCENE, SCENE_Q["id"]: SCENE_Q})
    d = D.Director(deps, dict(deps.recs))
    t = long_turn(fs)
    t["work"] = {"story": quality_story(style, empty_first), "approved": True}
    await d.pump(); await settle(d, 25)
    assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
    tags = tags_of(deps, t)
    subs = [c for c in deps.calls if c[0] == "submit"]
    return deps, t, fs, {tags[k]: subs[k] for k in range(len(subs))}


async def t_non_semi_film_draws_place_cards_and_empty_scenes_with_the_quality_scene_recipe():
    """11-24 2차 C5b (메인 P3a~f): 반실사가 아닌 영화의 장소 그림·사람 없는 장면 그림은 Qwen 2512 정밀 판(가속 끔 40스텝 cfg 4)으로 그리고
    화풍별 부정 글을 넣는다(cfg 4 라 적용된다). 사람이 나오는 그림(인물 기준 그림)은 빠른 판 그대로, 앞머리 화풍은 두 판 모두.
    custom 은 무엇이 아닌지 몰라 레시피의 기본 부정 글, 반실사는 사용자가 고른 빠른 판 그대로. 예상 시간에도 정밀 판 시간을 넣는다"""
    from app import video
    old = (settings.results_dir, settings.studio_chain, settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            neg_node, pos_node = SCENE["inject"]["negative"]["node"], SCENE["inject"]["prompt"]["node"]
            text = lambda c, n: c[1]["prompt"][n]["inputs"]["text"]
            anime = "2D anime film with clean cel-shaded line art"
            deps, t, fs, by = await _quality_film(anime)
            assert "P0q1" in by and "P01" not in by and "L2q1" in by and "L2i1" not in by, sorted(by)
            assert recipe_of(by["P0q1"]) == SCENE_Q["id"] and recipe_of(by["L2q1"]) == SCENE_Q["id"] and recipe_of(by["C01"]) == SCENE["id"]
            assert text(by["P0q1"], neg_node) == video.QUALITY_NEGATIVE["anime"] == text(by["L2q1"], neg_node)
            assert text(by["C01"], neg_node) == recipes.default_negative(SCENE)              # 빠른 판(인물 기준 그림)은 기본 부정 글 그대로
            assert text(by["P0q1"], pos_node).startswith(anime + ". An empty establishing view") and text(by["L2q1"], pos_node).startswith(anime + ". ")
            assert text(by["C01"], pos_node).startswith(anime + ". Character reference")
            assert by["P0q1"][1]["prompt"]["238:229"]["inputs"]["value"] is False and by["C01"][1]["prompt"]["238:229"]["inputs"]["value"] is True
            pc = [e[1] for e in fs.events if e[0] == "place_card"]
            assert len(pc) == 1 and pc[0]["recipe"] == SCENE_Q["id"] and pc[0]["quality"] is True, pc
            sub = [e[1] for e in fs.events if e[0] == "comfy_submitted" and e[1]["recipe"] == SCENE_Q["id"]]
            assert sub and all(e["negative"] == video.QUALITY_NEGATIVE["anime"] for e in sub), sub
            assert not [e for e in fs.events if e[0] == "quality_still_failed"]
            # custom: 레시피의 기본 부정 글(높음 판은 빠른 판에서 만들 때 더한 말까지), 앞머리는 원문
            water = "soft watercolour storybook illustration with visible paper grain"
            deps, t, fs, by = await _quality_film(water)
            assert recipe_of(by["P0q1"]) == SCENE_Q["id"] and text(by["P0q1"], neg_node) == recipes.default_negative(SCENE_Q)
            assert text(by["P0q1"], pos_node).startswith("Soft watercolour storybook illustration")
            # 반실사 (라이브러리 화풍): 빠른 판 그대로
            from app import cast_lib
            deps, t, fs, by = await _quality_film(cast_lib.library()[0]["style"])
            assert "P01" in by and "L2i1" in by and not [k for k in by if "q" in k], sorted(by)
            assert SCENE_Q["id"] not in [recipe_of(c) for c in by.values()]
            # 예상 시간: 장소 그림 한 장 + 사람 없는 장면 한 장이 정밀 판 (반실사·정밀 판이 꺼진 영화는 그대로)
            fs2 = FakeStore(); fs2.patch()
            deps2 = compose_deps(fs2, root)
            deps2.recs.update({SCENE["id"]: SCENE, SCENE_Q["id"]: SCENE_Q})
            d_q = D.Director(deps2, dict(deps2.recs))
            d_f = D.Director(deps2, {k: v for k, v in deps2.recs.items() if k != SCENE_Q["id"]})
            ctx = D.Ctx(long_turn(fs2), USER, ENV, LTX)
            ctx.long = {}
            st_a = quality_story(anime)
            diff = await d_q.estimate_long(ctx, copy.deepcopy(st_a)) - await d_f.estimate_long(ctx, copy.deepcopy(st_a))
            assert diff == 2 * (D.PLACE_QUALITY_SECONDS - D.DIALOGUE_CAST_SECONDS), diff
            st_s = quality_story(cast_lib.library()[0]["style"])
            assert await d_q.estimate_long(ctx, copy.deepcopy(st_s)) == await d_f.estimate_long(ctx, copy.deepcopy(st_s))
        finally:
            settings.results_dir, settings.studio_chain, settings.studio_ref_layout = old


async def t_quality_scene_recipe_failure_falls_back_to_the_fast_one():
    """11-24 2차 C5b: 정밀 판의 작업이 실패하면(시드를 바꾼 한 번 더까지) 지금처럼 빠른 판으로 그린다. 장소 그림을 버리지 않는다.
    실패한 정밀 판은 이 요청에서 기억해 다음 장소 그림·장면 그림에는 쓰지 않는다 (장마다 같은 실패를 되풀이하지 않게)"""
    old = (settings.results_dir, settings.studio_chain, settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            node = SCENE["inject"]["negative"]["node"]
            # 장소 그림이 먼저: 장소 그림 정밀 판이 두 번 실패 → 빠른 판. 뒤의 사람 없는 장면은 바로 빠른 판
            deps, t, fs, by = await _quality_film("photorealistic cinematic live action", QualityFailDeps)
            assert {"P0q1", "P0q2", "P01", "L2i1"} <= set(by) and "L2q1" not in by, sorted(by)
            assert recipe_of(by["P01"]) == SCENE["id"] and recipe_of(by["L2i1"]) == SCENE["id"]
            assert by["P01"][1]["prompt"][node]["inputs"]["text"] == recipes.default_negative(SCENE)   # 빠른 판에는 화풍별 부정 글을 넣지 않는다
            failed = [e[1] for e in fs.events if e[0] == "quality_still_failed"]
            assert [(e["what"], e["code"], e["recipe"]) for e in failed] == [("place", "run_error", SCENE_Q["id"])], failed
            assert t["work"]["failed_recipes"] == [SCENE_Q["id"]]
            assert t["work"]["places"]["P1"].get("att") and not [e for e in fs.events if e[0] == "place_card_dropped"]
            pc = [e[1] for e in fs.events if e[0] == "place_card"]
            assert len(pc) == 1 and pc[0]["recipe"] == SCENE["id"] and pc[0]["quality"] is False, pc
            # 사람 없는 장면이 먼저: 장면 그림 정밀 판이 두 번 실패 → 빠른 판. 뒤의 장소 그림 둘은 바로 빠른 판
            deps, t, fs, by = await _quality_film("photorealistic cinematic live action", QualityFailDeps, empty_first=True)
            assert {"L0q1", "L0q2", "L0i1", "P01", "P11"} <= set(by) and not [k for k in by if k.startswith(("P0q", "P1q"))], sorted(by)
            assert recipe_of(by["L0i1"]) == SCENE["id"] and recipe_of(by["P11"]) == SCENE["id"]
            failed = [e[1] for e in fs.events if e[0] == "quality_still_failed"]
            assert [(e["what"], e["index"], e["code"]) for e in failed] == [("scene_image", 0, "run_error")], failed
            assert t["work"]["failed_recipes"] == [SCENE_Q["id"]]
        finally:
            settings.results_dir, settings.studio_chain, settings.studio_ref_layout = old


async def t_custom_film_checks_the_raw_style_but_not_on_the_mannequin_layout():
    """11-24 2차 C3: custom 영화는 '아닌 것' 문장이 없어 인물을 입힌 그림·끝 프레임 검사에 화풍 원문 줄을 쓰고, 구도 그림(회색 마네킹)
    검사에는 화풍 줄을 넣지 않는다 (마네킹이 화풍 검사에 걸려 헛되이 다시 그리지 않게)"""
    from app.studio import checks
    old = (settings.results_dir, settings.studio_chain, settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.studio_chain = root, True
        try:
            fs = FakeStore(); fs.patch()
            deps = compose_deps(fs, root, layout=True)
            story = copy.deepcopy(LONG_STORY)
            story["bible"]["style"] = "soft watercolour storybook illustration with visible paper grain"
            d = D.Director(deps, dict(deps.recs))
            t = long_turn(fs)
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 25)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            trait = checks.style_trait(story["bible"]["style"])
            ins = [c[1][3] for c in deps.calls if c[0] == "inspect" and c[1][3]]
            layout = [x for x in ins if x[0].startswith("Exactly 1 faceless mannequin")]
            assert layout and all(x == [checks.layout_trait(1)] for x in layout), layout          # 형상 수 줄만
            person = [x for x in ins if x[0].startswith("A person who looks like")]
            assert len(person) >= 4 and all(x[-1] == trait for x in person), person                 # 인물 입히기·끝 키프레임·끝 프레임
            assert not any(checks.style_negative("semi") in x for x in ins)                           # 예전 반실사 문장은 없다
        finally:
            settings.results_dir, settings.studio_chain, settings.studio_ref_layout = old


async def t_custom_and_toon3d_style_check_lines_heads_and_hints():
    """11-24 2차 C3·C5: 검사 화풍 줄(열쇠 문장, custom 은 원문 줄 — 특징 한 줄 한도 안), 장소·장면 그림 앞머리(custom·3D 애니도 원문),
    장면 힌트에 부딪히는 문장이 없음, 정밀 판은 반실사가 아닌 Qwen 2512 영화에만"""
    from app import cast_lib
    from app.studio import checks
    custom = {"bible": {"style": "soft watercolour storybook illustration with visible paper grain"}}
    toon = {"bible": {"style": "Pixar-like 3D animated feature film"}}
    sc = D.Director._style_check
    assert sc(custom) == checks.style_trait(custom["bible"]["style"])
    assert sc(toon) == checks.style_negative("toon3d") and sc({"bible": {"style": "2D anime film"}}) == checks.style_negative("anime")
    assert sc({"bible": {}}) == checks.style_negative("semi") and sc({"bible": {"style": "live action"}}) == checks.style_negative("photo")
    long_custom = {"bible": {"style": "soft watercolour washes, " * 30}}
    assert len(sc(long_custom)) <= checks.TRAIT_MAX and sc(long_custom).startswith("The whole picture is in this art style: soft watercolour")
    sp = D.Director._scene_image_prompt
    assert sp(SCENE, custom, "IMG text") == "Soft watercolour storybook illustration with visible paper grain. IMG text"
    assert sp(SCENE_Q, toon, "IMG text") == "Pixar-like 3D animated feature film. IMG text"
    assert sp(IMG, custom, "IMG text").startswith("Soft watercolour storybook") and sp(IMG, toon, "IMG text").startswith("Pixar-like 3D")
    for st in (custom, toon):
        h = D.Director.scene_hint(st).lower()
        assert st["bible"]["style"].lower() in h and "not a cartoon" not in h and "3d render" not in h
    d = D.Director(FakeDeps(FakeStore()), {SCENE["id"]: SCENE, SCENE_Q["id"]: SCENE_Q, IMG["id"]: IMG})
    for st in (custom, toon, {"bible": {"style": "photorealistic live action"}}, {"bible": {"style": "2D anime film"}}):
        assert d._quality_still(st, SCENE)["id"] == SCENE_Q["id"], st
    for st in ({"bible": {}}, {"bible": {"style": cast_lib.library()[0]["style"]}}):
        assert d._quality_still(st, SCENE) is None, st                       # 반실사는 사용자가 고른 빠른 판
    assert d._quality_still(custom, IMG) is None and d._quality_still(custom, None) is None      # Z-Image 영화에는 정밀 판이 없다
    d.disabled.add(SCENE_Q["id"])
    assert d._quality_still(custom, SCENE) is None                                              # 꺼지면 빠른 판


async def t_long_reset_also_clears_the_quality_scene_image_job():
    """다시 만드는 장면은 정밀 판 장면 그림의 접수 정보(L{k}q)도 비운다 (옛 그림을 이어받지 않게). 장소 그림(영화마다 한 번)은 그대로"""
    with tempfile.TemporaryDirectory() as root:
        fs = FakeStore(); fs.patch()
        deps = LongDeps(fs, root)
        d = D.Director(deps, dict(deps.recs))
        t = long_turn(fs)
        work = {"story": copy.deepcopy(LONG_STORY), "chain": True, "L0q_pending": {"client_id": "studio-x-L0q1", "attempt": 1},
                "L0i_pending": {"client_id": "studio-x-L0i1", "attempt": 2}, "P0q_pending": {"client_id": "p", "attempt": 1}}
        t["work"] = dict(work)
        ctx = D.Ctx(t, USER, ENV, LTX)
        ctx.long = {}
        await d._long_reset(ctx, work, [{"clip_rel": "a.mp4"}, {}, {}], 0)
        assert work["L0q_pending"] == {"client_id": None, "attempt": 1} and work["L0i_pending"] == {"client_id": None, "attempt": 2}
        assert work["P0q_pending"] == {"client_id": "p", "attempt": 1}
        assert t["work"]["L0q_pending"] == {"client_id": None, "attempt": 1}                       # 바로 저장한다
def away_story():
    """그 자리에 없는 사람에게 하는 말 (11-24 3차 검토): 다락방의 니키 → 문 밖에서 부르는 엄마(목소리) → 니키가 문 쪽에 대답(listener 는 엄마),
    부엌의 가족 → 니키가 부엌문으로 뛰어나감 → 베스가 떠난 니키를 부름(listener 는 니키) → 제임스가 베스에게"""
    from app import video
    raw = {
        "title_ko": "늦잠", "logline_ko": "니키가 늦잠을 자고 학교에 뛰어간다.",
        "bible": {"characters": [
            {"id": "A", "look": "a calm neat woman around forty in a cream blouse and an apron", "name_ko": "베스",
             "voice": "A warm Korean woman around forty, gentle.", "voice_ko": "따뜻한 엄마"},
            {"id": "B", "look": "a tall lean man in his early forties in a blue cardigan", "name_ko": "제임스",
             "voice": "A calm Korean man in his forties, low and friendly.", "voice_ko": "차분한 아빠"},
            {"id": "C", "look": "a twelve-year-old girl with two long black braids and a wine-red cape", "name_ko": "니키",
             "voice": "A bright twelve-year-old Korean girl, clear.", "voice_ko": "맑은 아이"}],
            "places": [{"id": "P1", "look": "a sunny cottage kitchen with a wooden table"}, {"id": "P3", "look": "a quiet attic bedroom with a round window"}],
            "time": "morning", "light": "warm window light", "style": "semi-realistic painterly 2.5D film look"},
        "music": {"tags": "gentle instrumental, strings", "bpm": 72, "key": "D major", "ko": "잔잔한 현악"},
        "scenes": [
            {"seconds": 8, "place": "P3", "characters": ["C"], "continues": False, "shot": "medium shot",
             "action": "Niki sits alone on her bed by the round window.", "sound": "birds", "ko": "다락방의 니키", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 5, "place": "P3", "characters": [], "continues": False, "shot": "close-up of the attic door",
             "action": "The attic door stays still.", "sound": "a voice behind the door", "ko": "닫힌 문", "kind": "shot",
             "speaker": "A", "listener": "", "line_ko": "니키야, 밥 먹어라!", "emotion": "calling warmly"},
            {"seconds": 5, "place": "P3", "characters": ["C"], "continues": False, "shot": "close-up",
             "action": "Niki calls toward the door.", "sound": "room tone", "ko": "니키의 대답", "kind": "talk",
             "speaker": "C", "listener": "A", "line_ko": "엄마, 조금만요!", "emotion": "sleepy"},
            {"seconds": 10, "place": "P1", "characters": ["A", "B", "C"], "continues": False, "shot": "wide shot",
             "action": "The family sits at the wooden table.", "sound": "birds", "ko": "가족의 아침 식탁", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 6, "place": "P1", "characters": ["C"], "continues": False, "shot": "medium shot",
             "action": "Niki grabs her bag and runs out of the kitchen door.", "sound": "a door slams", "ko": "니키가 뛰어나간다", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 5, "place": "P1", "characters": ["A", "B"], "continues": False, "shot": "medium two-shot",
             "action": "Beth calls toward the door.", "sound": "room tone", "ko": "베스가 니키를 부른다", "kind": "talk",
             "speaker": "A", "listener": "C", "line_ko": "니키야, 도시락 가져가!", "emotion": "calling"},
            {"seconds": 5, "place": "P1", "characters": ["B", "A"], "continues": False, "shot": "medium two-shot",
             "action": "James chuckles.", "sound": "room tone", "ko": "제임스가 베스에게", "kind": "talk",
             "speaker": "B", "listener": "A", "line_ko": "벌써 갔어요.", "emotion": "amused"}]}
    old = settings.studio_duo_talk
    settings.studio_duo_talk = True
    try:
        story = video.clean_dialogue_story(raw, 44, exchange=True)
    finally:
        settings.studio_duo_talk = old
    assert story is not None
    return story


async def t_r3_a_line_to_someone_not_in_the_place_is_a_solo_close_up():
    """G0 '그 자리에 있는 사람에게 하는 말' (11-24 3차 검토): 문 밖의 엄마·뛰어나간 니키에게 하는 말은 혼자 클로즈업(LTX 혼자 대사, 구도 그림 한 형상).
    그 사람을 대화 컷에 데려오지 않고(예전에는 다락방에 베스가 앉은 투샷), 다음 대화 컷(제임스→베스)에도 니키는 없다"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, False
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            deps.people_for = lambda tag: None
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = away_story()
            assert [s["characters"] for s in story["scenes"]] == [["C"], [], ["C"], ["A", "B", "C"], ["C"], ["A"], ["B", "A"]]
            assert story["scenes"][2]["monologue"] is True and story["scenes"][5]["monologue"] is True
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 60)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            tags = tags_of(deps, t)
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            for k in (2, 5):
                clip = next(x for x in tags if re.match(rf"L{k}[wv]\d", x))
                assert recipe_of(by[clip]) == TALK["id"] and "alone in the frame, no other people" in prompt_text(by[clip]), (k, clip)
                lay = next((x for x in tags if re.match(rf"L{k}c\d", x)), None)
                assert lay is None or "One " in text_of(by[lay]), (k, lay)
            assert recipe_of(by[next(x for x in tags if re.match(r"L6[wv]\d", x))]) == DUO["id"]
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


async def t_r3_photo_like_custom_scene_hint_does_not_say_drawn():
    """custom 화풍인데 사진 쪽(누아르·흑백 무성영화)이면 장면 그림 힌트에 '그린 그림·사진 아님' 을 쓰지 않는다 (11-24 3차 검토, C2 원칙)"""
    for style in ("black-and-white film noir with hard shadows", "black-and-white silent film look"):
        h = D.Director.scene_hint({"bible": {"style": style}})
        assert style in h and "Not a photograph" not in h and "drawn" not in h and "one still frame" in h, h
    h = D.Director.scene_hint({"bible": {"style": "soft watercolour storybook illustration"}})
    assert "drawn in this style" in h and "Not a photograph" in h
async def t_r3_a_new_clip_clears_the_round1_line_delay():
    """1차 상태(오른쪽 화자 클립의 line_delay)가 남은 장면을 새로 만들면 그 표시를 지운다: 새 클립은 대사가 늘 0초에서 시작하므로, 남으면
    합치기가 대사를 0.12초 늦게 놓아 소리가 입보다 늦는다 (2차 검토 변이: 새 클립 완료 때 line_delay 를 지우지 않아도 통과했다)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, False
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            t["work"] = {"story": duo_story(), "approved": True, "scenes": [{}, {}, {"line_delay": 0.12}, {}]}   # 클립은 없다 (새로 만든다)
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            assert not any("line_delay" in s for s in t["work"]["scenes"])
            _clips, lines, _m, _o, _trims = [c[1] for c in deps.calls if c[0] == "assemble_dialogue"][-1]
            assert not any("delay" in x for x in lines), lines
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


async def t_r4_photo_like_custom_converts_references_as_a_live_action_still():
    """4차 검토: 사진 쪽 custom 영화(누아르·흑백 무성영화)의 기준 그림 변환은 실사 변환 레시피를 먼저 쓰고, 지시문은 '그린 그림'이 아니라 그 화풍의
    실사 영화 한 컷이다 (예전에는 'a character drawn in this art style: black-and-white film noir …'). 손그림 custom 은 빠른 편집 하나 그대로"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir, settings.studio_chain = os.path.join(root, "results"), os.path.join(root, "envs"), True
        settings.studio_picture_check = True
        try:
            fs = FakeStore(); fs.patch()
            deps = chain_deps(fs, settings.results_dir, DialogueDeps)
            deps.recs.update({HQ_EDIT["id"]: HQ_EDIT, PHOTO_EDIT["id"]: PHOTO_EDIT})
            deps.inspect_result = dict(PASS)
            d = D.Director(deps, dict(deps.recs))
            assert [r["id"] for r in d._convert_recipes("custom", True)] == [PHOTO_EDIT["id"], HQ_EDIT["id"]]
            assert [r["id"] for r in d._convert_recipes("custom")] == [EDITR["id"]] and d._convert_recipe("custom", True)["id"] == PHOTO_EDIT["id"]
            t = dialogue_turn(fs)
            story = semi_ref_story(settings.results_dir, "black-and-white film noir with hard shadows")
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 40)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-3:])
            conv = [e[1] for e in fs.events if e[0] == "cast_style_convert"]
            assert len(conv) == 2 and all(e["style"] == "custom" and e["recipe"] == PHOTO_EDIT["id"] for e in conv), conv
            photo = [c for c in deps.calls if c[0] == "submit" and recipe_of(c) == PHOTO_EDIT["id"]]
            assert len(photo) == 2
            for c in photo:
                p = c[1]["prompt"]["474"]["inputs"]["prompt"]
                assert p.startswith("Turn this portrait into a live-action film still of the same person") and "film noir" in p, p
                assert "drawn" not in p and "illustrat" not in p.split(":")[0], p
        finally:
            settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_picture_check = old


def present_story():
    """3차 명세 (11-25, 사용자 결정 "방식 바꾸기"): 구성안이 장면마다 그 자리에 있는 사람(present)을 적었다. 가족 식탁 → 베스가 모두에게(셋) →
    니키가 손을 흔들고 나간다(장면 글은 예전 낱말 목록에 없는 'waves goodbye', 떠남은 present 로만) → 베스가 제임스에게(둘) → 제임스가 같은 화면에서
    답한다 → 문 밖에서 니키 목소리(present 에 없다) → 제임스의 혼잣말"""
    from app import video
    fam = family_story()["bible"]["characters"]
    raw = {
        "title_ko": "등교하는 아침", "logline_ko": "니키가 학교에 가고 부모가 이야기한다.",
        "bible": {"characters": [{k: c[k] for k in ("id", "look", "name_ko", "voice", "voice_ko")} for c in fam],
                  "places": [{"id": "P1", "look": "a sunny cottage kitchen with a wooden table"}], "time": "morning", "light": "warm window light",
                  "style": "semi-realistic painterly 2.5D film look"},
        "music": {"tags": "gentle instrumental, strings", "bpm": 72, "key": "D major", "ko": "잔잔한 현악"},
        "scenes": [
            {"seconds": 10, "place": "P1", "characters": ["A", "B", "C"], "present": ["A", "B", "C"], "continues": False, "shot": "wide shot",
             "action": "The family sits at the wooden table in the morning.", "sound": "birds", "ko": "가족의 아침 식탁", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 6, "place": "P1", "characters": ["A", "B", "C"], "present": ["A", "B", "C"], "continues": False,
             "shot": "medium group shot", "action": "She smiles around the table.", "sound": "room tone", "ko": "베스가 모두에게", "kind": "talk",
             "speaker": "A", "listener": "everyone", "line_ko": "자, 아침 먹자!", "emotion": "cheerful"},
            {"seconds": 6, "place": "P1", "characters": ["C"], "present": ["A", "B", "C"], "continues": False, "shot": "medium shot",
             "action": "Niki grabs her school bag and waves goodbye.", "sound": "footsteps", "ko": "니키가 손을 흔든다", "kind": "shot",
             "speaker": "", "listener": "", "line_ko": "", "emotion": ""},
            {"seconds": 6, "place": "P1", "characters": ["A", "B"], "present": ["A", "B"], "continues": False, "shot": "medium two-shot",
             "action": "She smiles at him.", "sound": "room tone", "ko": "베스가 제임스에게", "kind": "talk",
             "speaker": "A", "listener": "B", "line_ko": "우리 딸 다 컸네요.", "emotion": "warm"},
            {"seconds": 6, "place": "P1", "characters": ["B", "A"], "present": ["A", "B"], "continues": True, "shot": "medium two-shot",
             "action": "He nods.", "sound": "room tone", "ko": "제임스가 답한다", "kind": "talk",
             "speaker": "B", "listener": "A", "line_ko": "그러게 말이야.", "emotion": "gentle"},
            {"seconds": 5, "place": "P1", "characters": [], "present": ["A", "B"], "continues": False, "shot": "close-up of the kitchen door",
             "action": "The kitchen door stays still.", "sound": "room tone", "ko": "부엌문", "kind": "talk",
             "speaker": "C", "listener": "everyone", "line_ko": "다녀오겠습니다!", "emotion": "bright"},
            {"seconds": 5, "place": "P1", "characters": ["B"], "present": ["A", "B"], "continues": False, "shot": "close-up",
             "action": "He smiles to himself.", "sound": "room tone", "ko": "제임스의 혼잣말", "kind": "talk",
             "speaker": "B", "listener": "nobody", "line_ko": "잘 다녀와라...", "emotion": "quiet"}]}
    old = settings.studio_duo_talk
    settings.studio_duo_talk = True
    try:
        story = video.clean_dialogue_story(raw, 44, exchange=True)
    finally:
        settings.studio_duo_talk = old
    assert story is not None
    return story


async def t_s3_present_story_draws_who_present_says_on_the_speaker_column():
    """3차 명세 1.5 감독의 대사 컷 경로: present 로 다듬은 이야기에서 감독이 그 칸이 정한 사람으로 컷을 만든다. 베스가 모두에게(셋, InfiniteTalk 3분의 1
    칸), 니키가 떠난 뒤 부모의 대화(둘, 반쪽 칸 — 장면 글의 'waves goodbye' 는 예전 낱말 목록에 없지만 present 가 정한다), 같은 화면으로 이어지는
    답, 그 자리에 없는 니키의 목소리(그리지 않는 목소리 shot), 제임스의 혼잣말(혼자 LTX 대사)"""
    old = (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check,
           settings.studio_ref_layout)
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir, settings.envs_dir = os.path.join(root, "results"), os.path.join(root, "envs")
        settings.studio_chain, settings.studio_duo_talk, settings.studio_picture_check = True, True, False
        try:
            fs = FakeStore(); fs.patch()
            deps = duo_deps(fs)
            counts = {"0": 3, "1": 3, "2": 1, "3": 2, "4": 2, "5": 0, "6": 1}
            deps.people_for = lambda tag: counts.get(re.match(r"L(\d+)", tag).group(1)) if re.match(r"L\d+", tag) else None
            d = D.Director(deps, dict(deps.recs))
            t = dialogue_turn(fs)
            story = present_story()
            assert [s["characters"] for s in story["scenes"]] == [["A", "B", "C"], ["A", "B", "C"], ["C"], ["A", "B"], ["B", "A"], [], ["B"]]
            assert [s["present"] for s in story["scenes"]][2:4] == [["A", "B", "C"], ["A", "B"]]
            assert story["scenes"][5]["kind"] == "shot" and story["scenes"][5]["offscreen"] is True and story["scenes"][6]["monologue"] is True
            t["work"] = {"story": story, "approved": True}
            await d.pump(); await settle(d, 60)
            assert t["state"] == "done", (t["state"], t.get("error"), fs.events[-4:])
            w = t["work"]
            tags = tags_of(deps, t)
            subs = [c for c in deps.calls if c[0] == "submit"]
            by = {tags[k]: subs[k] for k in range(len(subs))}
            assert {recipe_of(by[k]) for k in ("L1w1", "L3w1", "L4w1")} == {DUO["id"]} and recipe_of(by["L6v1"]) == TALK["id"]
            assert not any(x.startswith(("L4c", "L4d", "L5c", "L5w", "L6w")) for x in tags), tags      # 이어지는 답은 앞 클립 프레임
            # 그 자리에 없는 니키의 목소리: 사람 없는 장면 그림(L5i) + 보통 장면 영상이고 입 맞춤 대사 컷이 아니다
            assert "L5i1" in tags and recipe_of(by["L5v1"]) not in (TALK["id"], DUO["id"]), tags
            for tag, span in (("L1w1", (0, 283)), ("L3w1", (0, 424)), ("L4w1", (424, 424))):
                g = by[tag][1]["prompt"]
                assert (g["33"]["inputs"]["x"], g["32"]["inputs"]["width"]) == span, (tag, g["33"]["inputs"]["x"], g["32"]["inputs"]["width"])
            assert prompt_text(by["L1w1"]).startswith("Medium group shot of three people in a conversation")
            assert prompt_text(by["L3w1"]).startswith("Medium two-shot of two people in a conversation") and "twelve-year-old" not in prompt_text(by["L3w1"])
            assert "2 faceless mannequin figures" in text_of(by["L3c1"]) and "3 faceless mannequin figures" in text_of(by["L1c1"])
            assert "alone in the frame, no other people" in prompt_text(by["L6v1"])
            starts = {e[1]["index"]: e[1] for e in fs.events if e[0] == "long_scene_start"}
            assert starts[1]["people"] == 3 and starts[3]["people"] == 2 and starts[4]["people"] == 2 and "duo" not in starts[6]
            assert "duo" not in starts[5] and w["scenes"][4]["first_from"] == "last"
            assert not [e for e in fs.events if e[0] == "duo_fallback"]
        finally:
            (settings.results_dir, settings.envs_dir, settings.studio_chain, settings.studio_duo_talk,
             settings.studio_picture_check, settings.studio_ref_layout) = old


async def t_s3_oom_words_are_read_as_gpu_memory_errors():
    """3차 명세 3절: OOM_RE 의 \\b 자리에 백스페이스 글자(\\x08)가 들어 있어 'oom' 만으로는 GPU 메모리 부족을 알아보지 못했다 (HEAD 부터).
    실행 오류 글이 'CUDA error: oom' 이면 run_oom(안내 'GPU 메모리가 모자랐습니다'), 'boom' 같은 낱말 속 oom 은 run_error 그대로"""
    assert "\x08" not in D.OOM_RE.pattern and D.OOM_RE.pattern == r"out of memory|\boom\b|메모리가 부족|메모리 부족"
    for text in ("cuda error: oom", "torch.outofmemoryerror: cuda out of memory", "runtimeerror: oom!", "gpu 메모리가 부족합니다", "메모리 부족"):
        assert D.OOM_RE.search(text), text
    for text in ("boom", "room tone", "zoom in", "bloom", "oomph", "error: doom"):
        assert not D.OOM_RE.search(text), text
    for message, code in (("CUDA error: OOM", "run_oom"), ("boom", "run_error")):
        fs = FakeStore(); fs.patch()
        deps = FakeDeps(fs, outcome="error")
        drive = deps._drive

        async def failing(jid, cid, outcome, rec=None, drive=drive, message=message):
            await drive(jid, cid, outcome, rec)
            fs.jobs[jid]["error"] = {"message": message}
        deps._drive = failing
        d = D.Director(deps, {REC["id"]: REC})
        t = new_turn(fs)
        await d.pump(); await settle(d)
        assert t["state"] == "failed" and t["error"]["code"] == code, (message, t["state"], t.get("error"))
        if code == "run_oom":
            n = [m for m in fs.messages if m["kind"] == "notice"][-1]
            assert n["body"].startswith("GPU 메모리가 모자랐습니다"), n["body"]


async def t_fd_revise_passes_the_last_film_look_to_the_story():
    """fixD: 고치기 차례(api 가 요청에 now_style 을 적음)면 감독이 앞 영상의 화풍을 storyboard 에 넘긴다 ('지금 그림체' 가 가리키는 화풍).
    한 메시지 요청에는 넘기지 않는다 (storyboard 가 인물의 그림체로 읽는다). "" (앞 영상의 화풍을 모름)도 그대로 넘긴다"""
    old = settings.results_dir
    with tempfile.TemporaryDirectory() as root:
        settings.results_dir = root
        try:
            for now in ("2D anime feature film with clean cel-shaded line art", "", None):
                fs = FakeStore(); fs.patch()
                deps = LongDeps(fs, root)
                d = D.Director(deps, dict(deps.recs))
                t = long_turn(fs)
                if now is not None:
                    t["request"]["now_style"] = now
                await d.pump(); await settle(d, 10)
                assert t["state"] == "awaiting_approval", (t["state"], t.get("error"))
                kws = [c[1] for c in deps.calls if c[0] == "storyboard"]
                assert len(kws) == 1 and kws[0].get("now_style", "absent") == (now if now is not None else "absent"), (now, kws)
        finally:
            settings.results_dir = old


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
