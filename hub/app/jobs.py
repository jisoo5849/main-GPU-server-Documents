"""작업 접수와 대기열 조회.

실행 버튼(POST /prompt) → 편집 공간에서 검증 → 환경 버전 고정 → 입력 파일 고정 → 저장 위치 지정
→ 접수 순번(seq)을 붙여 DB 에 저장. GPU 실행은 scheduler 가 순번대로 한다.
"""
import asyncio
import datetime
import logging
import os
import time
import uuid

import httpx

from . import db, envs, hostops, models_index, promptops
from .config import settings
from .relay import sockets

log = logging.getLogger("mgs.jobs")

# Comfy 계정 키는 DB 에 남기지 않고 실행 때까지만 메모리에 둔다
SECRETS: dict[str, dict] = {}
ACTIVE_STATES = ("queued", "starting", "running", "cleaning")


class SubmitError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body)
        self.status = status
        self.body = body


def comfy_error(etype: str, message: str, details: str = "") -> dict:
    return {"error": {"type": etype, "message": message, "details": details or message, "extra_info": {}},
            "node_errors": {}}


async def notify_queue_changed():
    remaining = await db.fetchval("SELECT count(*) FROM jobs WHERE state = ANY($1::text[])", list(ACTIVE_STATES))
    await sockets.broadcast_status(int(remaining))


async def insert_job(conn, *, job_id: str, kind: str, user_id: int, env_id=None, env_version_id=None,
                     client_ip=None, client_id=None, api_endpoint=None, model=None, prompt=None, workflow=None,
                     extra_data=None, partial_targets=None, request=None) -> int:
    """접수 순번은 잠금 안에서 매겨서, 순번 순서와 저장(커밋) 순서가 항상 같게 한다."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7202)")
        seq = await conn.fetchval("SELECT nextval('jobs_seq')")
        await conn.execute(
            """INSERT INTO jobs(id, seq, kind, user_id, env_id, env_version_id, client_ip, client_id, api_endpoint,
                                model, prompt, workflow, extra_data, partial_targets, request)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
            uuid.UUID(job_id), seq, kind, user_id, env_id, env_version_id, db.ip(client_ip), client_id,
            api_endpoint, model, prompt, workflow, extra_data, partial_targets, request,
        )
        await conn.execute("NOTIFY mgs_jobs")
    return seq


async def _validate_in_editor(env: dict, prompt: dict, prompt_id: str, partial) -> tuple[bool, dict, dict]:
    ip = await _editor_ip(env)
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"http://{ip}:8189/mgs/validate",
                         json={"prompt": prompt, "prompt_id": prompt_id, "partial_execution_targets": partial})
    if r.status_code != 200:
        raise SubmitError(503, comfy_error("mgs_editor_unavailable",
                                           "편집 공간 검증에 실패했습니다. 잠시 뒤 다시 실행하세요.", r.text[:500]))
    res = r.json()
    if res.get("prompt"):
        prompt.clear()
        prompt.update(res["prompt"])
    if res.get("valid"):
        return True, {}, {}
    return False, res.get("error") or {}, res.get("node_errors") or {}


async def _editor_ip(env: dict) -> str:
    from . import dockerctl
    ip = await dockerctl.editor_ip(env["slug"])
    if not ip:
        raise SubmitError(503, comfy_error("mgs_editor_unavailable", "편집 공간이 실행 중이 아닙니다."))
    return ip


async def _wait_manager_idle(env: dict, timeout: float = 120.0):
    deadline = time.monotonic() + timeout
    while await envs.manager_busy(env):
        if time.monotonic() > deadline:
            raise SubmitError(400, comfy_error(
                "mgs_env_busy", "노드 설치가 진행 중입니다. 설치가 끝난 뒤 다시 실행하세요."))
        await asyncio.sleep(2)


def _model_records(prompt: dict) -> list[tuple]:
    rows = []
    for m in promptops.find_models(prompt):
        hit = models_index.resolve(m["name"])
        if hit:
            folder, path = hit
            st = os.stat(path)
            rows.append((folder, m["name"], path, st.st_size, st.st_mtime))
        else:
            rows.append((None, m["name"], None, None, None))
    return rows


async def submit_comfy(env: dict, user: dict, body: dict, client_ip: str) -> dict:
    prompt = body.get("prompt")
    if not isinstance(prompt, dict) or not prompt:
        raise SubmitError(400, comfy_error("no_prompt", "No prompt provided"))
    job_id = str(uuid.uuid4())
    partial = body.get("partial_execution_targets")
    client_id = body.get("client_id")
    extra, secrets = promptops.split_sensitive(body.get("extra_data") or {})

    ok, error, node_errors = await _validate_in_editor(env, prompt, job_id, partial)
    if not ok:
        accepted, remaining = promptops.filter_node_errors(error, node_errors, settings.accept_missing_nodes)
        if not accepted:
            raise SubmitError(400, {"error": error or {"type": "prompt_outputs_failed_validation",
                                                       "message": "Prompt outputs failed validation",
                                                       "details": "", "extra_info": {}},
                                    "node_errors": remaining})

    await _wait_manager_idle(env)
    version_id = await envs.pin_version(env)

    input_dir = os.path.join(settings.state_path(env["slug"]), "input")
    await asyncio.to_thread(promptops.pin_inputs, prompt, input_dir, job_id, hostops.copy_reflink)
    promptops.rewrite_filename_prefix(prompt, env["name"], datetime.date.today().isoformat())
    model_rows = await asyncio.to_thread(_model_records, prompt)
    workflow = (extra.get("extra_pnginfo") or {}).get("workflow")
    extra["create_time"] = int(time.time() * 1000)
    if client_id:
        extra["client_id"] = client_id

    async with db.pool.acquire() as conn:
        seq = await insert_job(
            conn, job_id=job_id, kind="comfy", user_id=user["id"], env_id=env["id"], env_version_id=version_id,
            client_ip=client_ip, client_id=client_id, prompt=prompt, workflow=workflow, extra_data=extra,
            partial_targets=partial,
        )
        if model_rows:
            await conn.executemany(
                "INSERT INTO job_models(job_id, kind, name, path, size, mtime) VALUES ($1,$2,$3,$4,$5,$6)",
                [(uuid.UUID(job_id), *r) for r in model_rows],
            )
    if secrets:
        SECRETS[job_id] = secrets
    await notify_queue_changed()
    return {"prompt_id": job_id, "number": seq, "node_errors": {}}


# ── 작업자 화면(ComfyUI 대기열·이력 패널)에 보여 줄 값 ──

def _ms(ts):
    return int(ts.timestamp() * 1000) if ts else None


def _queue_tuple(row) -> list:
    extra = dict(row["extra_data"] or {})
    extra.setdefault("create_time", _ms(row["submitted_at"]))
    return [row["seq"], str(row["id"]), {}, extra, []]


async def queue_view(env_id: int) -> dict:
    rows = await db.fetch(
        "SELECT id, seq, state, extra_data, submitted_at FROM jobs WHERE env_id=$1 AND state = ANY($2::text[]) "
        "ORDER BY seq", env_id, list(ACTIVE_STATES),
    )
    return {
        "queue_running": [_queue_tuple(r) for r in rows if r["state"] != "queued"],
        "queue_pending": [_queue_tuple(r) for r in rows if r["state"] == "queued"],
    }


_OUTCOME_STATUS = {"success": "completed", "cancelled": "cancelled"}


def _job_summary(row) -> dict:
    if row["comfy_job"]:
        j = dict(row["comfy_job"])
        j.pop("outputs", None)
        j.pop("workflow", None)
        return j
    state = row["state"]
    if state == "queued":
        status = "pending"
    elif state != "finished":
        status = "in_progress"
    else:
        status = _OUTCOME_STATUS.get(row["outcome"], "failed")
    out = {
        "id": str(row["id"]),
        "status": status,
        "priority": row["seq"],
        "create_time": _ms(row["submitted_at"]),
        "outputs_count": 0,
        "previewable_outputs_count": 0,
    }
    if row["exec_started_at"]:
        out["execution_start_time"] = _ms(row["exec_started_at"])
    if row["exec_ended_at"]:
        out["execution_end_time"] = _ms(row["exec_ended_at"])
    if row["error"] and status == "failed":
        out["execution_error"] = row["error"]
    return out


async def jobs_list(env_id: int, query) -> dict:
    rows = await db.fetch(
        "SELECT id, seq, state, outcome, submitted_at, exec_started_at, exec_ended_at, error, comfy_job "
        "FROM jobs WHERE env_id=$1 AND kind='comfy' ORDER BY seq DESC LIMIT 500", env_id,
    )
    jobs = [_job_summary(r) for r in rows]
    status = query.get("status")
    if status:
        wanted = {s.strip().lower() for s in status.split(",") if s.strip()}
        jobs = [j for j in jobs if j.get("status") in wanted]
    if query.get("sort_order", "desc").lower() == "asc":
        jobs.reverse()
    offset = max(int(query.get("offset", 0) or 0), 0)
    limit = query.get("limit")
    total = len(jobs)
    page = jobs[offset: offset + int(limit)] if limit else jobs[offset:]
    return {"jobs": page, "pagination": {"offset": offset, "limit": int(limit) if limit else None,
                                         "total": total, "has_more": offset + len(page) < total}}


async def job_detail(env_id: int, job_id: str) -> dict | None:
    row = await db.fetchrow(
        "SELECT id, seq, state, outcome, submitted_at, exec_started_at, exec_ended_at, error, comfy_job, workflow "
        "FROM jobs WHERE env_id=$1 AND id=$2", env_id, _uuid(job_id),
    )
    if row is None:
        return None
    if row["comfy_job"]:
        return row["comfy_job"]
    d = _job_summary(row)
    if row["workflow"]:
        d["workflow"] = {"extra_data": {"extra_pnginfo": {"workflow": row["workflow"]}}}
    return d


async def history_view(env_id: int, prompt_id: str | None = None, max_items: int | None = None) -> dict:
    if prompt_id:
        rows = await db.fetch("SELECT id, comfy_history FROM jobs WHERE env_id=$1 AND id=$2 "
                              "AND comfy_history IS NOT NULL", env_id, _uuid(prompt_id))
    else:
        rows = await db.fetch("SELECT id, comfy_history FROM jobs WHERE env_id=$1 AND comfy_history IS NOT NULL "
                              "ORDER BY seq DESC LIMIT $2", env_id, max_items or 200)
        rows = list(reversed(rows))
    return {str(r["id"]): r["comfy_history"] for r in rows}


def _uuid(value: str):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return uuid.UUID(int=0)


async def cancel_queued(env_id: int | None, job_ids: list[str] | None, user_id: int | None = None) -> int:
    """대기 중인 작업 취소. job_ids 가 None 이면 그 환경의 대기 작업 전부."""
    if job_ids is None:
        res = await db.execute(
            "UPDATE jobs SET state='finished', outcome='cancelled', finished_at=now() "
            "WHERE env_id=$1 AND state='queued'", env_id)
    else:
        ids = [_uuid(j) for j in job_ids]
        if env_id is not None:
            res = await db.execute(
                "UPDATE jobs SET state='finished', outcome='cancelled', finished_at=now() "
                "WHERE env_id=$1 AND id = ANY($2::uuid[]) AND state='queued'", env_id, ids)
        else:
            res = await db.execute(
                "UPDATE jobs SET state='finished', outcome='cancelled', finished_at=now() "
                "WHERE id = ANY($1::uuid[]) AND state='queued'", ids)
    n = int(res.split()[-1])
    if n:
        await notify_queue_changed()
    return n


async def global_queue() -> list[dict]:
    """포털 화면용 전체 대기열 (누가 무엇을 기다리는지)."""
    rows = await db.fetch(
        """SELECT j.id, j.seq, j.kind, j.state, j.model, j.submitted_at, j.started_at, u.username,
                  e.name AS env_name
           FROM jobs j JOIN users u ON u.id = j.user_id LEFT JOIN envs e ON e.id = j.env_id
           WHERE j.state = ANY($1::text[]) ORDER BY j.seq""", list(ACTIVE_STATES))
    return [dict(r) for r in rows]
