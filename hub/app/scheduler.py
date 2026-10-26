"""스케줄러: 메인 GPU에서 한 번에 한 작업만, 접수 순번대로 실행한다.

  1. DB 대기열 맨 앞(가장 작은 seq) 작업을 꺼낸다
  2. 사용자·환경 버전·모델을 확인한다
  3. 그 버전의 GPU 실행 공간에서 실행한다 (LLM·임베딩은 Ollama)
  4. 결과와 기록을 저장한다
  5. GPU 메모리가 기준치로 돌아왔는지 직접 측정한다 (안 되면 단계적으로 정리, 끝내 안 되면 대기열 정지)
  6. 다음 작업
"""
import asyncio
import datetime
import json
import logging
import os
import time
import uuid

import httpx
import websockets

from . import db, dockerctl, envs, gpu, hostops, jobs, promptops
from .config import settings
from .relay import sockets

log = logging.getLogger("mgs.scheduler")

SCHEDULER_LOCK = 4242


class WorkerStartError(Exception):
    pass


class LlmWaiter:
    def __init__(self):
        self.go = asyncio.Event()
        self.done = asyncio.Event()
        self.cancelled = False
        self.result: dict = {}


class Scheduler:
    def __init__(self):
        self.worker: dict | None = None      # 켜져 있는 GPU 실행 공간
        self.lease: dict | None = None       # 지금 GPU를 쓰는 작업
        self.lease_open = False              # 실행 중 워크플로우의 내부 LLM 호출 허용 여부
        self.nested_inflight = 0
        self.llm_waiters: dict[str, LlmWaiter] = {}
        self.wake = asyncio.Event()
        self.b0: int | None = None           # GPU를 아무도 쓰지 않을 때의 사용량(MB)
        self.peak = 0
        self._lock_conn = None
        self._incident_pids: dict[int, float] = {}
        self._bg: set = set()   # 끝날 때까지 참조를 잡아 두는 보조 작업들
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))

    # ── 시작·복구 ─────────────────────────────────────────────

    async def start(self):
        self._lock_conn = await db.connect_raw()
        while not await self._lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", SCHEDULER_LOCK):
            log.warning("다른 스케줄러가 실행 중입니다. 10초 뒤 다시 시도합니다.")
            await asyncio.sleep(10)
        await self._lock_conn.add_listener("mgs_jobs", lambda *a: self.wake.set())
        await self.recover()
        await self._measure_b0()
        self.spawn(self._loop(), name="scheduler")
        self.spawn(self._watchdog(), name="gpu-watchdog")
        self.spawn(self._housekeeping(), name="housekeeping")

    def spawn(self, coro, name=None):
        t = asyncio.create_task(coro, name=name)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def release_waiter(self, job_id: str, message: str):
        """차례가 오기 전에 끝난 LLM 작업: 기다리는 요청을 깨워서 409 로 돌려보낸다."""
        w = self.llm_waiters.pop(str(job_id), None)
        if w is not None:
            w.cancelled = True
            w.result = {"status": 409, "message": message}
            w.go.set()

    async def _survey(self) -> dict:
        """지금 GPU 를 쓰는 프로세스를 우리 것(실행 공간, Ollama)과 그 밖의 것으로 나눈다.

        그 밖의 것(메인의 화면 프로그램 Xorg·gnome-shell, 관리자가 직접 돌린 스크립트 등)은 우리가 끌 수 없으므로
        메모리 확인에서 그 사용량을 빼고 본다. 연산(compute) 프로세스는 감시 기록에 남긴다.
        """
        ollama_id = await self._ollama_container_id()
        worker_id = self.worker["id"] if self.worker else None
        s = {"worker_mb": 0, "ollama_mb": 0, "foreign_mb": 0, "foreign": [], "other_container": []}
        for pid, mem, kind in gpu.processes():
            cid = gpu.pid_container(pid)
            if worker_id and cid == worker_id:
                s["worker_mb"] += mem
            elif cid and cid == ollama_id:
                s["ollama_mb"] += mem
            else:
                s["foreign_mb"] += mem
                s["foreign"].append({"pid": pid, "mem_mb": mem, "kind": kind, "name": gpu.process_name(pid),
                                     "container": cid})
                if cid:
                    s["other_container"].append(pid)
        s["used_mb"] = gpu.used_mb()
        # 우리 몫: 전체 사용량에서 남의 프로세스 몫을 뺀 값 (드라이버 기본 점유 포함)
        s["ours_mb"] = max(0, s["used_mb"] - s["foreign_mb"])
        return s

    async def _measure_b0(self):
        """유휴 기준값: 실행 공간이 없고 Ollama 에 올라간 모델도 없을 때 '우리 몫' 의 GPU 사용량 (최솟값 유지)."""
        if not gpu.available() or self.worker or self.lease:
            return
        if await self._ollama_loaded():
            return
        s = await self._survey()
        if s["worker_mb"] or s["ollama_mb"]:
            return
        self.b0 = s["ours_mb"] if self.b0 is None else min(self.b0, s["ours_mb"])

    async def recover(self):
        """허브가 다시 시작됐을 때: 실행 중이던 작업을 이어받거나 정리한다."""
        rows = await db.fetch("SELECT * FROM jobs WHERE state IN ('starting','running','cleaning') ORDER BY seq")
        for job in rows:
            job = dict(job)
            reattached = False
            if job["kind"] == "comfy" and job["state"] == "running":
                w = await dockerctl.get(settings.worker_name)
                if w is not None and w.status == "running" and \
                        w.labels.get("mgs.version") == str(job["env_version_id"]):
                    await asyncio.to_thread(w.reload)
                    nets = w.attrs["NetworkSettings"]["Networks"]
                    self.worker = {
                        "name": settings.worker_name, "id": w.id, "pid": w.attrs["State"]["Pid"],
                        "ip": (nets.get(settings.worker_network) or {}).get("IPAddress"),
                        "version_id": job["env_version_id"], "env_id": job["env_id"], "ctx_mb": 1536,
                    }
                    log.info("작업 %s 을 이어받습니다", job["id"])
                    self.spawn(self._resume(job))
                    reattached = True
            if not reattached:
                await db.execute("UPDATE jobs SET state='finished', outcome='lost', finished_at=now(), "
                                 "error=$2 WHERE id=$1", job["id"],
                                 {"message": "허브가 다시 시작되어 작업을 이어받지 못했습니다."})
        if self.worker is None:
            await self.stop_worker()

    async def _resume(self, job):
        await self._process(job, resume=True)

    # ── 메인 루프 ─────────────────────────────────────────────

    async def _loop(self):
        while True:
            try:
                if self.lease is not None:
                    await asyncio.sleep(1)
                    continue
                if await db.get_setting("paused", False):
                    await self._wait(3)
                    continue
                if await db.get_setting("blocked"):
                    # 메모리가 늦게라도 돌아오면 관리자 없이 스스로 다시 시작한다
                    if await self._try_unblock():
                        continue
                    await self._wait(15)
                    continue
                row = await db.fetchrow("SELECT * FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1")
                if row is None:
                    await self._measure_b0()
                    await self._wait(3)
                    continue
                await self._process(dict(row))
            except Exception:  # noqa: BLE001
                log.exception("scheduler loop error")
                await asyncio.sleep(3)

    async def _try_unblock(self) -> bool:
        if self.worker:
            await self.stop_worker()
        await self.unload_ollama()
        if not await self._check(False):
            return False
        await db.execute("DELETE FROM settings WHERE key='blocked'")
        s = await self._survey()
        await db.incident("auto_unblocked", {"used_mb": s["used_mb"], "ours_mb": s["ours_mb"], "baseline_mb": self.b0})
        log.info("GPU 메모리가 기준으로 돌아와 대기열을 다시 시작합니다")
        return True

    async def _wait(self, seconds: float):
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _process(self, job: dict, resume: bool = False):
        job_id = str(job["id"])
        if not resume:
            ok, reason = await self._precheck(job)
            if not ok:
                await self._finish(job, "rejected", error={"message": reason})
                await self._tell_browser_error(job, reason)
                self.release_waiter(job_id, reason)
                return
            await db.execute(
                "UPDATE jobs SET state='starting', started_at=now(), "
                "wait_ms = (extract(epoch from now() - submitted_at) * 1000)::int WHERE id=$1", job["id"])
        job["state"] = "starting"
        self.lease = job
        self.lease_open = job["kind"] == "comfy"
        self.peak = gpu.used_mb()
        await jobs.notify_queue_changed()
        outcome, error = "error", None
        try:
            if job["kind"] == "comfy":
                outcome, error = await self._run_comfy(job, resume=resume)
            else:
                outcome, error = await self._run_llm(job)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            outcome, error = "error", {"message": str(e)}
        finally:
            await db.execute("UPDATE jobs SET state='cleaning', exec_ended_at=coalesce(exec_ended_at, now()) "
                             "WHERE id=$1", job["id"])
            clean_ok, method, cleanup_ms = await self._cleanup(job)
            await db.execute(
                "UPDATE jobs SET cleanup_ms=$2, cleanup_method=$3, vram_peak_mb=$4, vram_after_mb=$5 WHERE id=$1",
                job["id"], cleanup_ms, method, self.peak, gpu.used_mb())
            await self._finish(job, outcome, error=error)
            jobs.SECRETS.pop(job_id, None)
            self.lease = None
            if not clean_ok:
                s = await self._survey()
                detail = {"since": datetime.datetime.now().isoformat(), "job": job_id, "used_mb": s["used_mb"],
                          "ours_mb": s["ours_mb"], "baseline_mb": self.b0, "foreign": s["foreign"]}
                await db.set_setting("blocked", detail)
                await db.incident("vram_not_released", detail)
                log.error("GPU 메모리가 해제되지 않아 대기열을 멈췄습니다. 15초마다 다시 확인합니다 (ours=%sMB, 기준=%sMB)",
                          s["ours_mb"], self.b0)
            self._cleanup_pinned_inputs(job)
            await jobs.notify_queue_changed()

    async def _finish(self, job, outcome, error=None):
        await db.execute(
            "UPDATE jobs SET state='finished', outcome=$2, error=coalesce($3, error), finished_at=now(), "
            "run_ms = CASE WHEN exec_started_at IS NOT NULL "
            "THEN (extract(epoch from coalesce(exec_ended_at, now()) - exec_started_at) * 1000)::int END "
            "WHERE id=$1", job["id"], outcome, error)

    async def _precheck(self, job) -> tuple[bool, str]:
        user = await db.fetchrow("SELECT active FROM users WHERE id=$1", job["user_id"])
        if not user or not user["active"]:
            return False, "비활성 사용자입니다."
        if job["kind"] != "comfy":
            if str(job["id"]) not in self.llm_waiters:
                return False, "요청한 프로그램과의 연결이 끊겼습니다."
            return True, ""
        ver = await db.fetchrow("SELECT * FROM env_versions WHERE id=$1", job["env_version_id"])
        if ver is None or ver["deleted_at"] is not None or not os.path.isdir(ver["snapshot_path"]):
            return False, "접수 때 고정한 환경 버전이 없습니다."
        for m in await db.fetch("SELECT * FROM job_models WHERE job_id=$1 AND path IS NOT NULL", job["id"]):
            try:
                st = os.stat(m["path"])
            except OSError:
                return False, f"모델 파일이 접수 뒤 삭제되었습니다: {m['name']}"
            if st.st_size != m["size"] or abs(st.st_mtime - (m["mtime"] or 0)) > 1:
                return False, f"모델 파일이 접수 뒤 바뀌었습니다: {m['name']}"
        return True, ""

    # ── ComfyUI 작업 ──────────────────────────────────────────

    async def _ensure_worker(self, job, env) -> int:
        """작업의 환경 버전으로 GPU 실행 공간을 준비한다. 같은 버전이 켜져 있으면 그대로 쓴다."""
        vid = job["env_version_id"]
        if self.worker and self.worker["version_id"] == vid and await dockerctl.is_running(settings.worker_name):
            return 0
        t0 = time.monotonic()
        await self.stop_worker()
        ver = await db.fetchrow("SELECT * FROM env_versions WHERE id=$1", vid)
        run = settings.run_path()
        await hostops.delete_subvolume(run)
        # 고정된 버전(읽기 전용)의 쓰기 가능한 사본. 작업 중 노드가 자기 폴더에 써도 버전은 바뀌지 않는다.
        await hostops.snapshot(ver["snapshot_path"], run)
        info = await dockerctl.run_worker(run, settings.state_path(env["slug"]), env["username"], env["name"],
                                          env["linux_uid"], {"mgs.version": str(vid), "mgs.env": env["slug"]})
        self.worker = {"name": settings.worker_name, **info, "version_id": vid, "env_id": env["id"], "ctx_mb": 0}
        deadline = time.monotonic() + settings.worker_start_timeout
        while True:
            if not await dockerctl.is_running(settings.worker_name):
                tail = await dockerctl.logs_tail(settings.worker_name)
                await self.stop_worker()
                raise WorkerStartError(f"실행 공간이 시작 중 종료되었습니다.\n{tail}")
            try:
                r = await self.http.get(f"http://{info['ip']}:8189/system_stats", timeout=3)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                tail = await dockerctl.logs_tail(settings.worker_name)
                await self.stop_worker()
                raise WorkerStartError(f"실행 공간이 {settings.worker_start_timeout}초 안에 준비되지 않았습니다.\n{tail}")
            await asyncio.sleep(1)
        self.worker["ctx_mb"] = self._container_mem(info["id"])
        return int((time.monotonic() - t0) * 1000)

    async def _run_comfy(self, job, resume=False):
        job_id = str(job["id"])
        env = await envs.get_env(job["env_id"])
        client_id = job["client_id"]
        if not resume:
            await sockets.send_json(env["id"], client_id, "progress_text",
                                    {"nodeId": "", "prompt_id": job_id, "text": "GPU 실행 공간 준비 중"})
            try:
                cold_ms = await self._ensure_worker(job, env)
            except WorkerStartError as e:
                await self._tell_browser_error(job, str(e))
                return "error", {"message": str(e)}
            await db.execute("UPDATE jobs SET cold_start_ms=$2 WHERE id=$1", job["id"], cold_ms)
            if job.get("cancel_requested"):
                return "cancelled", None
        w = self.worker
        base = f"http://{w['ip']}:8189"

        ws = await websockets.connect(f"ws://{w['ip']}:8189/ws?clientId=gw-{job_id}", max_size=None,
                                      ping_interval=20, ping_timeout=60)
        try:
            flags = sockets.flags(env["id"], client_id)
            if flags:
                await ws.send(json.dumps({"type": "feature_flags", "data": flags}))
            if not resume:
                extra = dict(job["extra_data"] or {})
                extra.update(jobs.SECRETS.get(job_id, {}))
                body = {"prompt": job["prompt"], "prompt_id": job_id, "client_id": f"gw-{job_id}",
                        "extra_data": extra}
                if job["partial_targets"]:
                    body["partial_execution_targets"] = job["partial_targets"]
                r = await self.http.post(f"{base}/prompt", json=body)
                if r.status_code != 200:
                    err = _safe_json(r)
                    msg = (err.get("error") or {}).get("message") if isinstance(err.get("error"), dict) else None
                    await self._tell_browser_error(job, msg or r.text[:300], node_errors=err.get("node_errors"))
                    return "error", {"message": msg or r.text[:300], "node_errors": err.get("node_errors")}
                await db.execute("UPDATE jobs SET state='running', exec_started_at=now() WHERE id=$1", job["id"])
                job["state"] = "running"
            outcome, error = await self._relay_until_done(ws, job, env, base)
        finally:
            await ws.close()

        await self._collect_results(job, env, base)
        return outcome, error

    async def _relay_until_done(self, ws, job, env, base):
        """실행 공간의 진행 메시지를 작업자 브라우저 탭으로 그대로 전달하고, 끝나면 결과를 돌려준다."""
        reopened = []
        try:
            return await self._relay_steps(ws, job, env, base, reopened)
        finally:
            for extra in reopened:
                await extra.close()

    async def _relay_steps(self, ws, job, env, base, reopened):
        job_id = str(job["id"])
        client_id = job["client_id"]
        deadline = time.monotonic() + settings.job_max_seconds
        outcome, error = None, None
        while True:
            if time.monotonic() > deadline:
                await self.http.post(f"{base}/interrupt", json={"prompt_id": job_id})
                return "timeout", {"message": f"최대 실행 시간 {settings.job_max_seconds}초를 넘었습니다."}
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                state = await self._poll_job(base, job_id)
                if state == "gone":
                    return "lost", {"message": "실행 공간이 작업 도중 종료되었습니다."}
                if state in ("completed", "failed", "cancelled"):
                    return {"completed": "success", "failed": "error", "cancelled": "cancelled"}[state], error
                continue
            except websockets.ConnectionClosed:
                if not await dockerctl.is_running(settings.worker_name):
                    return "lost", {"message": "실행 공간이 작업 도중 종료되었습니다."}
                ws = await websockets.connect(f"ws://{self.worker['ip']}:8189/ws?clientId=gw-{job_id}",
                                              max_size=None)
                reopened.append(ws)
                continue
            if isinstance(msg, bytes):
                await sockets.send_bytes(env["id"], client_id, msg)
                continue
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            typ = data.get("type")
            d = data.get("data") or {}
            if typ in ("status", "feature_flags"):
                continue
            await sockets.send_text(env["id"], client_id, msg)
            if d.get("prompt_id") != job_id:
                continue
            if typ == "execution_start":
                await db.execute("UPDATE jobs SET exec_started_at=now() WHERE id=$1", job["id"])
            elif typ == "execution_error":
                outcome, error = "error", d
                break
            elif typ == "execution_interrupted":
                outcome = "cancelled"
                break
            elif typ == "execution_success":
                outcome = outcome or "success"
                break
            elif typ == "executing" and d.get("node") is None:
                outcome = outcome or "success"
                break
        await db.execute("UPDATE jobs SET exec_ended_at=now() WHERE id=$1", job["id"])
        return outcome, error

    async def _poll_job(self, base, job_id) -> str:
        if not await dockerctl.is_running(settings.worker_name):
            return "gone"
        try:
            r = await self.http.get(f"{base}/api/jobs/{job_id}", timeout=5)
            if r.status_code == 200:
                return r.json().get("status", "")
        except httpx.HTTPError:
            pass
        return ""

    async def _collect_results(self, job, env, base):
        """실행 공간의 기록(history, job)을 DB 에 옮기고 결과 파일 목록과 설명 파일을 남긴다."""
        job_id = str(job["id"])
        hist, detail = None, None
        for _ in range(10):
            try:
                r = await self.http.get(f"{base}/history/{job_id}", timeout=5)
                hist = (_safe_json(r) or {}).get(job_id)
                if hist:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        try:
            r = await self.http.get(f"{base}/api/jobs/{job_id}", timeout=5)
            detail = _safe_json(r) if r.status_code == 200 else None
        except httpx.HTTPError:
            pass
        await db.execute("UPDATE jobs SET comfy_history=$2, comfy_job=$3 WHERE id=$1", job["id"], hist, detail)
        if not hist:
            return
        files = promptops.history_outputs(hist)
        rows, first_dir = [], None
        for f in files:
            if f["type"] != "output":
                continue
            rel = "/".join(p for p in (env["username"], f["subfolder"], f["filename"]) if p)
            rows.append((job["id"], f["node_id"], rel, f["media"]))
            first_dir = first_dir or os.path.dirname(os.path.join(settings.results_dir, rel))
        if rows:
            async with db.pool.acquire() as c:
                await c.executemany(
                    "INSERT INTO job_outputs(job_id, node_id, rel_path, media) VALUES ($1,$2,$3,$4)", rows)
        if first_dir and os.path.isdir(first_dir):
            await asyncio.to_thread(self._write_sidecar, job, env, first_dir, [r[2] for r in rows])

    def _write_sidecar(self, job, env, folder, outputs):
        path = os.path.join(folder, f"{job['id']}.json")
        info = {
            "job_id": str(job["id"]), "user": env["username"], "env": env["name"],
            "env_version_id": job["env_version_id"], "submitted_at": str(job["submitted_at"]),
            "outputs": outputs, "workflow": job["workflow"], "prompt": job["prompt"],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        os.chown(path, env["linux_uid"], settings.group_gid)
        os.chmod(path, 0o664)

    def _cleanup_pinned_inputs(self, job):
        if job["kind"] != "comfy" or not job.get("env_id"):
            return
        self.spawn(self._rm_pinned(job))

    async def _rm_pinned(self, job):
        env = await envs.get_env(job["env_id"])
        if env:
            path = os.path.join(settings.state_path(env["slug"]), "input", promptops.PINNED_DIR, str(job["id"]))
            await asyncio.to_thread(_rmtree, path)

    async def stop_worker(self):
        await dockerctl.remove(settings.worker_name, timeout=30)
        await hostops.delete_subvolume(settings.run_path())
        self.worker = None

    async def interrupt(self, env_id: int, prompt_id: str | None):
        lease = self.lease
        if not lease or lease["kind"] != "comfy" or lease["env_id"] != env_id:
            return
        if prompt_id and prompt_id != str(lease["id"]):
            return
        if lease["state"] == "starting" or not self.worker:
            # 실행 공간을 띄우는 중: 실행 전에 취소 표시를 남겨 두면 _run_comfy 가 시작하지 않는다
            lease["cancel_requested"] = True
            return
        await self._post_quiet(f"http://{self.worker['ip']}:8189/interrupt", {"prompt_id": str(lease["id"])})

    # ── LLM·임베딩 작업 ───────────────────────────────────────

    async def _run_llm(self, job):
        job_id = str(job["id"])
        # ComfyUI 실행 공간과 Ollama 가 GPU 를 함께 쓰지 않게, 켜 둔 실행 공간을 먼저 끈다
        await self.stop_worker()
        waiter = self.llm_waiters.get(job_id)
        if waiter is None or waiter.cancelled:
            return "cancelled", {"message": "요청한 프로그램과의 연결이 끊겼습니다."}
        await db.execute("UPDATE jobs SET state='running', exec_started_at=now() WHERE id=$1", job["id"])
        job["state"] = "running"
        waiter.go.set()
        try:
            await asyncio.wait_for(waiter.done.wait(), timeout=settings.job_max_seconds)
        except asyncio.TimeoutError:
            return "timeout", {"message": "LLM 요청이 최대 실행 시간을 넘었습니다."}
        finally:
            self.llm_waiters.pop(job_id, None)
        res = waiter.result
        return ("success" if res.get("status", 500) < 400 else "error"), (res if res.get("status", 500) >= 400
                                                                         else None)

    # ── 메모리 해제 확인 ──────────────────────────────────────

    def _container_mem(self, container_id: str) -> int:
        return sum(mem for pid, mem, _kind in gpu.processes() if gpu.pid_container(pid) == container_id)

    async def _ollama_loaded(self) -> list[str]:
        try:
            r = await self.http.get(f"{settings.ollama_upstream}/api/ps", timeout=5)
            return [m.get("name") for m in (r.json().get("models") or [])]
        except (httpx.HTTPError, ValueError):
            return []

    async def unload_ollama(self):
        for name in await self._ollama_loaded():
            try:
                await self.http.post(f"{settings.ollama_upstream}/api/generate",
                                     json={"model": name, "keep_alive": 0}, timeout=30)
            except httpx.HTTPError:
                pass

    async def _ollama_container_id(self):
        return await dockerctl.container_id(settings.ollama_container)

    async def _check(self, warm: bool) -> bool:
        """warm: 실행 공간을 켜 둔 상태 기준 / 아니면 GPU 를 아무도 쓰지 않는 상태 기준."""
        if not gpu.available():
            return True
        if await self._ollama_loaded():
            return False
        s = await self._survey()
        if s["ollama_mb"]:
            return False
        if not warm and s["worker_mb"]:
            return False
        if self.b0 is None:
            # 기준값을 아직 못 잡았으면 지금 값을 기준으로 삼는다 (더 낮은 값이 보이면 그때 내려간다)
            self.b0 = s["ours_mb"] - s["worker_mb"]
        ours = s["ours_mb"]
        if warm and self.worker:
            ctx = self.worker["ctx_mb"]
            return (s["worker_mb"] <= ctx + settings.vram_worker_margin_mb
                    and ours <= self.b0 + ctx + settings.vram_total_margin_mb)
        return ours <= self.b0 + settings.vram_idle_margin_mb

    async def _poll_clean(self, warm: bool, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await self._check(warm):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _cleanup(self, job) -> tuple[bool, str, int]:
        t0 = time.monotonic()
        # 실행 중 워크플로우의 내부 LLM 호출을 먼저 막고, 진행 중인 호출이 끝나길 기다린다
        self.lease_open = False
        for _ in range(120):
            if self.nested_inflight == 0:
                break
            await asyncio.sleep(0.5)
        await self.unload_ollama()
        ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731
        worker_alive = self.worker is not None and await dockerctl.is_running(settings.worker_name)

        if job["kind"] == "comfy" and worker_alive:
            base = f"http://{self.worker['ip']}:8189"
            await self._post_quiet(f"{base}/free", {"unload_models": True})
            if await self._poll_clean(True, 30):
                return True, "free", ms()
            await self._post_quiet(f"{base}/free", {"unload_models": True, "free_memory": True})
            if await self._poll_clean(True, 30):
                return True, "free_memory", ms()
        await self.stop_worker()
        if await self._poll_clean(False, 60):
            return True, "stop", ms()
        if job["kind"] != "comfy":
            await dockerctl.restart(settings.ollama_container)
            if await self._poll_clean(False, 60):
                return True, "restart_ollama", ms()
        return False, "blocked", ms()

    async def _post_quiet(self, url, body):
        try:
            await self.http.post(url, json=body, timeout=10)
        except httpx.HTTPError:
            pass

    # ── 감시·정리 ─────────────────────────────────────────────

    async def _watchdog(self):
        """허용되지 않은 프로세스가 GPU 를 쓰는지 2초마다 확인하고, 실행 중 최대 사용량을 잰다."""
        while True:
            try:
                if gpu.available():
                    self.peak = max(self.peak, gpu.used_mb())
                    allowed = set()
                    if self.worker:
                        allowed.add(self.worker["id"])
                    oid = await self._ollama_container_id()
                    if oid:
                        allowed.add(oid)
                    now = time.monotonic()
                    for pid, mem, kind in gpu.processes():
                        cid = gpu.pid_container(pid)
                        if cid in allowed:
                            continue
                        if kind == "graphics" and cid is None:
                            continue   # 메인의 화면 프로그램(Xorg, gnome-shell 등)은 정상
                        if now - self._incident_pids.get(pid, 0) > 600:
                            self._incident_pids[pid] = now
                            name = gpu.process_name(pid)
                            await db.incident("foreign_gpu_process",
                                              {"pid": pid, "name": name, "mem_mb": mem, "kind": kind, "container": cid})
                            log.error("허용되지 않은 GPU 연산 프로세스: pid=%s %s mem=%sMB container=%s", pid, name, mem, cid)
            except Exception:  # noqa: BLE001
                log.exception("watchdog error")
            await asyncio.sleep(2)

    async def _housekeeping(self):
        while True:
            await asyncio.sleep(3600)
            try:
                protected = {self.worker["version_id"]} if self.worker else set()
                await envs.gc_versions(protected)
            except Exception:  # noqa: BLE001
                log.exception("housekeeping error")

    async def _tell_browser_error(self, job, message, node_errors=None):
        if job["kind"] != "comfy":
            return
        if node_errors:
            # 실행 공간의 검증 오류를 노드별로 요약해 메시지에 덧붙인다
            parts = []
            for nid, ne in node_errors.items():
                errs = "; ".join(e.get("message", "") for e in ne.get("errors", []))
                parts.append(f"[{ne.get('class_type', nid)}] {errs}")
            message = f"{message}\n" + "\n".join(parts)
        await sockets.send_json(job["env_id"], job["client_id"], "execution_error", {
            "prompt_id": str(job["id"]), "node_id": "", "node_type": "", "executed": [],
            "exception_message": message, "exception_type": "mgs", "traceback": [],
            "current_inputs": {}, "current_outputs": {}, "timestamp": int(time.time() * 1000),
        })


def _safe_json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


scheduler = Scheduler()


def new_job_id() -> str:
    return str(uuid.uuid4())
