"""허브 대기열 정지 풀기 (11-24, 11-21 사무실 재현 메모): 기준값이 낮게 굳어 53~64MB 차이로 몇 시간씩 정지가 풀리지 않던 문제.
DB·GPU 없이 가짜로: 순수 함수 rebase_ok 와 Scheduler._try_unblock 의 흐름."""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 개발 PC 에 없는 외부 모듈(httpx·docker 등)은 가짜로 끼운다: scheduler 를 불러오려고만. 이 시험은 네트워크·DB·GPU·도커를 쓰지 않는다
class _Dummy:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Dummy()

    def __getattr__(self, name):
        return _Dummy()


class _StubModule(types.ModuleType):
    __path__ = []                        # 하위 모듈(starlette.requests 등)도 가져올 수 있게

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (Exception,) if name.endswith(("Error", "Exception")) else (_Dummy,), {})


def _import_scheduler():
    for _ in range(40):
        try:
            from app import scheduler
            return scheduler
        except ModuleNotFoundError as e:
            name = e.name or ""
            if not name or name.split(".")[0] == "app":
                raise
            sys.modules[name] = _StubModule(name)
            for k in [k for k in sys.modules if k.startswith("app.") and k != "app"]:
                del sys.modules[k]       # 불러오다 만 모듈을 지우고 다시
    raise ImportError("가짜 모듈로도 scheduler 를 불러오지 못했다")


S = _import_scheduler()
from app.config import settings  # noqa: E402


def t_rebase_ok_needs_a_stable_idle_value_a_bit_above_the_baseline():
    ok = lambda s, b0=534, need=8, tol=64, mx=1024: S.rebase_ok(s, b0, need, tol, mx)      # noqa: E731
    assert ok([854] * 8)                                              # 11-21 04:05 기록: 534 → 854
    assert ok([843, 850, 846, 849, 851, 848, 845, 847])               # 오차 안
    assert not ok([854] * 7)                                          # 표본이 모자람
    assert not ok([854, 854, 854, 854, 854, 854, 854, 1000])          # 값이 흔들림
    assert not ok([534] * 8) and not ok([500] * 8)                    # 기준보다 높지 않으면 다시 잡을 일이 아니다
    assert not ok([2000] * 8)                                         # 기준보다 너무 높음 = 누수·남의 점유일 수 있다
    assert ok([1558] * 8) and not ok([1559] * 8)                      # 경계: 기준 + 1024
    assert not S.rebase_ok([854] * 8, None, 8, 64, 1024) and not S.rebase_ok([854] * 8, 534, 0, 64, 1024)
    assert ok([100, 900] + [854] * 8)                                 # 앞의 값은 보지 않고 마지막 need 개만 본다


class FakeDb:
    def __init__(self):
        self.settings = {"blocked": {"since": "x", "job": "j"}}
        self.incidents = []

    async def execute(self, sql, *a):
        if "DELETE FROM settings WHERE key='blocked'" in sql:
            self.settings.pop("blocked", None)

    async def incident(self, kind, detail):
        self.incidents.append((kind, detail))

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value


def make(b0, ours_seq, loaded=False, worker_mb=0, ollama_mb=0):
    sch = S.Scheduler.__new__(S.Scheduler)
    sch.worker, sch.lease, sch.b0 = None, None, b0
    sch._idle_samples, sch._unblock_tries = [], 0
    seq = list(ours_seq)
    state = {"ours": seq[0]}

    async def unload():                  # 확인 한 번(_try_unblock)에 한 번 부르므로 여기서 다음 표본으로 넘어간다
        if len(seq) > 1:
            seq.pop(0)
        state["ours"] = seq[0]

    async def stop():
        pass

    async def is_loaded():
        return loaded

    async def survey():
        return {"used_mb": state["ours"] + 20000, "ours_mb": state["ours"], "worker_mb": worker_mb, "ollama_mb": ollama_mb,
                "foreign": [], "foreign_mb": 20000}

    sch.unload_ollama, sch.stop_worker, sch._ollama_loaded, sch._survey = unload, stop, is_loaded, survey
    return sch


async def run(sch, n):
    out = []
    for _ in range(n):
        out.append(await sch._try_unblock())
    return out


def with_fakes(fn):
    db = FakeDb()
    old = (S.db, S.gpu.available)
    S.db = db
    S.gpu.available = lambda: True
    try:
        return asyncio.run(fn(db))
    finally:
        S.db, S.gpu.available = old


def t_unblocks_by_rebaselining_when_idle_and_stable():
    """11-21 04:05: 기준 534 + 여유 256 = 790, 우리 몫 854 → 정지. 15초마다 보아도 같은 값이라 풀리지 않았다"""
    old = settings.vram_idle_margin_mb
    settings.vram_idle_margin_mb = 256                       # 예전 여유로 재현
    try:
        async def go(db):
            sch = make(534, [854] * 20)
            res = await run(sch, settings.vram_rebase_checks)                  # 풀리면 대기열 루프는 더 부르지 않는다
            assert res == [False] * (settings.vram_rebase_checks - 1) + [True], res   # 표본 8개가 모이는 8번째 확인에서 풀린다
            assert sch.b0 == 854 and "blocked" not in db.settings
            kinds = [k for k, _ in db.incidents]
            assert kinds == ["auto_rebaselined", "auto_unblocked"], kinds
            d = dict(db.incidents[0][1])
            assert d["old_baseline_mb"] == 534 and d["new_baseline_mb"] == 854 and len(d["samples"]) == 8
        with_fakes(go)
    finally:
        settings.vram_idle_margin_mb = old


def t_a_wider_margin_unblocks_at_the_first_check():
    async def go(db):
        sch = make(534, [854] * 5)
        assert settings.vram_idle_margin_mb == 512 and await sch._try_unblock() is True       # 534 + 512 = 1046 >= 854
        assert [k for k, _ in db.incidents] == ["auto_unblocked"] and sch.b0 == 534
    with_fakes(go)


def t_does_not_rebaseline_a_value_far_above_the_baseline_or_while_a_model_is_loaded():
    async def go(db):
        sch = make(534, [3000] * 20)                          # 기준보다 2.4GB 높게 안정: 누수일 수 있다 → 푸는 대신 남는다
        assert not any(await run(sch, 12)) and sch.b0 == 534 and "blocked" in db.settings
        assert not any(k == "auto_rebaselined" for k, _ in db.incidents)
        last = db.settings["blocked"]["last_check"]                    # 5분마다 이유를 정지 기록에 남긴다
        assert last["why"] == "ours_over_baseline" and last["ours_mb"] == 3000 and last["baseline_mb"] == 534
        sch2 = make(534, [900] * 20, loaded=True)             # Ollama 에 모델이 올라가 있으면 표본을 모으지 않는다
        assert not any(await run(sch2, 12)) and sch2._idle_samples == [] and sch2.b0 == 534
        sch3 = make(534, [900] * 20, worker_mb=300)           # 실행 공간이 메모리를 쓰고 있어도
        assert not any(await run(sch3, 12)) and sch3._idle_samples == []
    with_fakes(go)


def t_unsteady_values_never_form_a_baseline():
    async def go(db):
        sch = make(534, [1400, 900, 1300, 950, 1350, 920, 1280, 960, 1300, 940, 1290, 955])
        assert not any(await run(sch, 12)) and sch.b0 == 534
    old = settings.vram_idle_margin_mb
    settings.vram_idle_margin_mb = 256
    try:
        with_fakes(go)
    finally:
        settings.vram_idle_margin_mb = old


async def _why(sch, **kw):
    return await sch._why_not_clean()


def t_why_not_clean_names_the_reason():
    def mk(**kw):
        return make(534, [854] * 3, **kw)

    async def go(db):
        assert await mk(loaded=True)._why_not_clean() == "ollama_loaded"
        assert await mk(ollama_mb=500)._why_not_clean() == "ollama_mem"
        assert await mk(worker_mb=500)._why_not_clean() == "worker_mem"
        old = settings.vram_idle_margin_mb
        settings.vram_idle_margin_mb = 256
        try:
            assert await mk()._why_not_clean() == "ours_over_baseline"
        finally:
            settings.vram_idle_margin_mb = old
        assert await mk()._why_not_clean() == "ok"
    with_fakes(go)


def t_incident_kinds_used_in_code_are_allowed_by_the_schema():
    """가짜 DB 는 kind 를 검사하지 않아 auto_rebaselined 가 실제 CHECK 에 막히는 것을 놓쳤다(11-24).
    코드가 db.incident·proc_incident 에 넘기는 kind 글자가 모두 schema.sql 의 incidents_kind_check 허용값에 있어야 한다"""
    import glob
    import re
    app = os.path.join(os.path.dirname(__file__), "..", "app")
    schema = open(os.path.join(app, "schema.sql"), encoding="utf-8").read()
    m = re.search(r"ADD CONSTRAINT incidents_kind_check\s+CHECK \(kind IN \(([^)]*)\)\)", schema)
    assert m, "schema.sql 에서 incidents_kind_check 를 찾지 못함"
    allowed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    used = set()
    for path in glob.glob(os.path.join(app, "**", "*.py"), recursive=True):
        src = open(path, encoding="utf-8").read()
        used |= set(re.findall(r"db\.(?:proc_)?incident\(\s*\"([a-z_]+)\"", src))
        used |= set(re.findall(r"self\._unblocked\(\s*\"([a-z_]+)\"", src))   # _unblocked(kind) 가 그대로 incident 로 넘긴다
    assert "auto_rebaselined" in used and "foreign_gpu_process" in used, used   # 찾는 식이 낡지 않았는지
    assert used <= allowed, f"schema 허용값에 없는 kind: {sorted(used - allowed)}"


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("t_"):
            v()
            print("ok", k)
    print("ok scheduler_block")
