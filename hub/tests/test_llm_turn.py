"""llm_turn 시험 (9.2): 차례를 기다리다 빠져나가는 모든 길에서 waiter.done 이 서고 끝난 이유가 남는다.
DB·Ollama·스케줄러는 가짜로 바꿔 GPU·DB 없이 돈다.

  cd hub && python tests/test_llm_turn.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class Waiter:
    def __init__(self):
        self.go = asyncio.Event()
        self.done = asyncio.Event()
        self.cancelled = False
        self.result = {}


class FakeSched:
    def __init__(self):
        self.llm_waiters = {}
        self.wake = types.SimpleNamespace(set=lambda: None)


sched = FakeSched()
calls = {"cancel_queued": [], "outcome": None, "fail_notify": False}

for name in ("httpx", "starlette", "starlette.applications", "starlette.requests", "starlette.responses",
             "starlette.routing"):
    if name not in sys.modules:
        _stub(name)
sys.modules["httpx"].AsyncClient = lambda **kw: types.SimpleNamespace()
sys.modules["httpx"].Timeout = lambda *a, **k: None
sys.modules["httpx"].HTTPError = type("HTTPError", (Exception,), {})
for n, attrs in (("starlette.applications", ["Starlette"]), ("starlette.requests", ["Request"]),
                 ("starlette.responses", ["JSONResponse", "Response", "StreamingResponse"]),
                 ("starlette.routing", ["Route"])):
    for a in attrs:
        setattr(sys.modules[n], a, object)

import app  # noqa: E402,F401


class FakePool:
    def acquire(self):
        class Ctx:
            async def __aenter__(self_):
                return object()

            async def __aexit__(self_, *a):
                return False
        return Ctx()


async def fetchval(*a):
    return calls["outcome"]


async def insert_job(conn, **kw):
    return 1


async def notify_queue_changed():
    if calls["fail_notify"]:
        raise RuntimeError("notify 실패")


async def cancel_queued(env_id, ids):
    calls["cancel_queued"].append(ids)
    return 1


_stub("app.db", pool=FakePool(), fetchval=fetchval)
_stub("app.jobs", insert_job=insert_job, notify_queue_changed=notify_queue_changed, cancel_queued=cancel_queued)
_stub("app.security")
_stub("app.scheduler", LlmWaiter=Waiter, scheduler=sched)
sys.modules["app"].db = sys.modules["app.db"]
sys.modules["app"].jobs = sys.modules["app.jobs"]
sys.modules["app"].security = sys.modules["app.security"]

from app import llm  # noqa: E402


class FakeClient:
    async def post(self, *a, **k):
        return types.SimpleNamespace(status_code=200, json=lambda: {})


async def _log_call(*a, **k):
    return None


llm._client = FakeClient()
llm._log_call = _log_call
USER = {"id": 7}


def reset():
    sched.llm_waiters.clear()
    calls["cancel_queued"].clear()
    calls["outcome"] = None
    calls["fail_notify"] = False


def only_waiter():
    assert len(sched.llm_waiters) <= 1
    return next(iter(sched.llm_waiters.values()), None)


async def wait_for_waiter():
    for _ in range(300):
        w = only_waiter()
        if w is not None:
            return w
        await asyncio.sleep(0.005)
    raise AssertionError("대기자가 등록되지 않았다")


async def scheduler_gives_turn():
    """스케줄러가 하듯 대기자를 찾아 go 를 세우고 done 을 기다린다. done 이 안 서면 TimeoutError."""
    w = await wait_for_waiter()
    w.go.set()
    await asyncio.wait_for(w.done.wait(), timeout=2)
    return w


async def t_normal():
    reset()
    bag = []
    tok = llm.STUDIO_JOBS.set(bag)
    sch = asyncio.ensure_future(scheduler_gives_turn())
    async with llm.llm_turn(USER, "1.1.1.1", "m", "video_expand"):
        assert llm.IN_TURN.get() is True
    llm.STUDIO_JOBS.reset(tok)
    w = await sch
    assert w.done.is_set() and w.result == {"status": 200}
    assert len(bag) == 1 and bag[0]["end"] == "done" and bag[0]["purpose"] == "video_expand", bag
    assert llm.IN_TURN.get() is False


async def t_panel_unchanged():
    reset()
    assert llm.STUDIO_JOBS.get() is None
    sch = asyncio.ensure_future(scheduler_gives_turn())
    async with llm.llm_turn(USER, "ip", "m", "p"):
        pass
    await sch


async def t_cancel_while_waiting():
    reset()
    bag = []
    llm.STUDIO_JOBS.set(bag)

    async def body():
        async with llm.llm_turn(USER, "ip", "m", "p"):
            pass
    t = asyncio.ensure_future(body())
    w = await wait_for_waiter()
    assert not w.go.is_set()
    t.cancel()
    try:
        await t
        raise AssertionError("취소돼야 한다")
    except asyncio.CancelledError:
        pass
    assert w.done.is_set() and w.cancelled and w.result == {"status": 499}
    assert bag[0]["end"] == "caller_cancelled" and calls["cancel_queued"], bag
    assert not sched.llm_waiters


async def t_cancel_right_after_go():
    """스케줄러가 go 를 세운 직후 취소가 와도 done 이 서서 스케줄러가 멈추지 않는다"""
    reset()
    llm.STUDIO_JOBS.set([])

    async def body():
        async with llm.llm_turn(USER, "ip", "m", "p"):
            pass
    t = asyncio.ensure_future(body())
    w = await wait_for_waiter()
    w.go.set()
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(w.done.wait(), timeout=1)
    assert w.done.is_set()


async def t_timeout():
    reset()
    bag = []
    llm.STUDIO_JOBS.set(bag)
    try:
        async with llm.llm_turn(USER, "ip", "m", "p", wait_seconds=0.05):
            pass
        raise AssertionError("HelperBusy 여야 한다")
    except llm.HelperBusy:
        pass
    assert bag[0]["end"] == "timeout"
    assert calls["cancel_queued"]


async def t_studio_wait_overrides_default():
    reset()
    llm.STUDIO_JOBS.set([])
    tok = llm.STUDIO_WAIT.set(0.05)
    try:
        async with llm.llm_turn(USER, "ip", "m", "p"):
            pass
        raise AssertionError("HelperBusy 여야 한다")
    except llm.HelperBusy:
        pass
    finally:
        llm.STUDIO_WAIT.reset(tok)


async def t_exception_before_wait():
    reset()
    bag = []
    llm.STUDIO_JOBS.set(bag)
    calls["fail_notify"] = True
    try:
        async with llm.llm_turn(USER, "ip", "m", "p"):
            pass
        raise AssertionError("RuntimeError 여야 한다")
    except RuntimeError:
        pass
    calls["fail_notify"] = False
    assert bag[0]["end"] == "error" and not sched.llm_waiters


async def t_cancel_inside_turn():
    reset()
    bag = []
    llm.STUDIO_JOBS.set(bag)
    started = asyncio.Event()

    async def body():
        async with llm.llm_turn(USER, "ip", "m", "p"):
            started.set()
            await asyncio.sleep(10)
    sch = asyncio.ensure_future(scheduler_gives_turn())
    t = asyncio.ensure_future(body())
    await asyncio.wait_for(started.wait(), 2)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    w = await sch
    assert w.done.is_set() and bag[0]["end"] == "caller_cancelled", bag
    assert w.result["status"] >= 400


async def t_error_inside_turn():
    reset()
    bag = []
    llm.STUDIO_JOBS.set(bag)
    sch = asyncio.ensure_future(scheduler_gives_turn())
    try:
        async with llm.llm_turn(USER, "ip", "m", "p"):
            raise ValueError("x")
    except ValueError:
        pass
    w = await sch
    assert w.done.is_set() and bag[0]["end"] == "error"


async def _released(outcome, message):
    reset()
    bag = []
    llm.STUDIO_JOBS.set(bag)

    async def body():
        async with llm.llm_turn(USER, "ip", "m", "p"):
            pass
    t = asyncio.ensure_future(body())
    w = await wait_for_waiter()
    w.cancelled = True
    w.result = {"message": message}
    calls["outcome"] = outcome
    w.go.set()
    try:
        await t
        raise AssertionError("HelperBusy 여야 한다")
    except llm.HelperBusy as e:
        assert message in str(e)
    assert w.done.is_set()
    return bag[0]["end"]


async def t_released_by_admin():
    assert await _released("cancelled", "관리자가 취소했습니다.") == "cancelled"
    assert await _released(None, "관리자가 취소했습니다.") == "cancelled"


async def t_rejected_by_precheck():
    assert await _released("rejected", "비활성 사용자입니다.") == "rejected"


async def t_nested():
    reset()
    sch = asyncio.ensure_future(scheduler_gives_turn())
    async with llm.llm_turn(USER, "ip", "m", "outer"):
        try:
            async with llm.llm_turn(USER, "ip", "m", "inner"):
                raise AssertionError("열리면 안 된다")
        except llm.NestedTurnError:
            pass
    await sch
    assert not issubclass(llm.NestedTurnError, RuntimeError)


async def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("t_"):
            await asyncio.wait_for(asyncio.ensure_future(fn()), timeout=10)   # 시험마다 새 작업(컨텍스트 분리)
            print("ok", name)
    print("ok llm_turn")

asyncio.run(main())
