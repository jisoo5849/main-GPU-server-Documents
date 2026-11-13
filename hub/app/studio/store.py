"""채팅 DB 접근 (docs/chat-studio-design.md 9절). SQL 만 둔다. db 모듈의 fetch/fetchrow/fetchval/execute 를 쓴다.

상태 바꾸기는 모두 조건부다: UPDATE … WHERE id=$1 AND state=$2 (취소·복구·두 번 누른 버튼이 먼저 바꿨으면 0줄).
"""
import uuid

from .. import db

OPEN_STATES = ("pending", "active", "awaiting_approval")
FINAL_STATES = ("done", "failed", "cancelled", "interrupted")
JSON_COLS = ("work", "result", "error", "decision", "progress", "plan", "request")


def _j(v):
    """jsonb 칸에 넣을 값. db.py 의 연결이 jsonb 인코더(json.dumps)를 달고 있으므로 파이썬 값을 그대로 준다
    (미리 문자열로 만들면 이중으로 인코딩된다)"""
    return v


def _row(r):
    return None if r is None else dict(r)


async def studio_env(slug: str):
    """채팅 작업공간. 없으면 None"""
    r = await db.fetchrow(
        "SELECT e.*, u.username, u.linux_uid FROM envs e JOIN users u ON u.id = e.owner_id WHERE e.slug=$1", slug)
    return _row(r)


# ── 대화 ──
async def create_thread(user_id: int) -> dict:
    tid = uuid.uuid4()
    r = await db.fetchrow("INSERT INTO chat_threads(id, user_id) VALUES ($1,$2) RETURNING *", tid, user_id)
    return dict(r)


async def get_thread(thread_id, user_id: int):
    try:
        tid = uuid.UUID(str(thread_id))
    except ValueError:
        return None
    return _row(await db.fetchrow(
        "SELECT * FROM chat_threads WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL", tid, user_id))


async def list_threads(user_id: int) -> list[dict]:
    rows = await db.fetch(
        "SELECT id, title, updated_at, settings FROM chat_threads WHERE user_id=$1 AND deleted_at IS NULL "
        "ORDER BY updated_at DESC LIMIT 100", user_id)
    return [dict(r) for r in rows]


async def thread_settings(thread_id) -> dict:
    v = await db.fetchval("SELECT settings FROM chat_threads WHERE id=$1", thread_id)
    return v if isinstance(v, dict) else {}


async def set_thread_settings(thread_id, patch: dict):
    await db.execute("UPDATE chat_threads SET settings = COALESCE(settings,'{}'::jsonb) || $2::jsonb WHERE id=$1",
                     thread_id, _j(patch))


async def set_message_data(message_id, patch: dict):
    """메시지의 data 에 값을 합친다 (계획 카드의 승인·취소 상태 등)"""
    await db.execute("UPDATE chat_messages SET data = COALESCE(data,'{}'::jsonb) || $2::jsonb WHERE id=$1",
                     message_id, _j(patch))


async def awaiting_turn(thread_id):
    """이 대화에서 승인을 기다리는 요청 (한 대화에 하나뿐이다)"""
    return _row(await db.fetchrow("SELECT * FROM chat_turns WHERE thread_id=$1 AND state='awaiting_approval' "
                                  "ORDER BY created_at DESC LIMIT 1", thread_id))


async def expired_plans(hours: int) -> list[dict]:
    rows = await db.fetch("SELECT * FROM chat_turns WHERE state='awaiting_approval' "
                          "AND updated_at < now() - make_interval(hours => $1)", hours)
    return [dict(r) for r in rows]


async def recent_medians() -> dict:
    """최근 끝난 GPU 작업 50건의 걸린 시간 중앙값(초). 종류별 {comfy, llm}. 없으면 None (8.5)"""
    out = {}
    for kind, expr in (("comfy", "COALESCE(cold_start_ms,0) + COALESCE(run_ms,0) + COALESCE(cleanup_ms,0)"),
                       ("llm", "COALESCE(run_ms,0) + COALESCE(cleanup_ms,0)")):
        v = await db.fetchval(
            f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t) FROM (SELECT {expr} AS t FROM jobs "
            "WHERE kind=$1 AND state='finished' AND outcome='success' ORDER BY seq DESC LIMIT 50) x", kind)
        out[kind] = None if v is None else float(v) / 1000
    return out


async def touch_thread(thread_id, title: str | None = None):
    if title:
        await db.execute("UPDATE chat_threads SET updated_at=now(), title=CASE WHEN title='' THEN $2 ELSE title END "
                         "WHERE id=$1", thread_id, title[:60])
    else:
        await db.execute("UPDATE chat_threads SET updated_at=now() WHERE id=$1", thread_id)


async def soft_delete_thread(thread_id):
    await db.execute("UPDATE chat_threads SET deleted_at=now() WHERE id=$1", thread_id)


# ── 메시지 ──
async def add_message(thread_id, role: str, kind: str = "text", body: str = "", data=None, turn_id=None,
                      client_msg_id=None) -> dict:
    r = await db.fetchrow(
        "INSERT INTO chat_messages(thread_id, turn_id, role, kind, body, data, client_msg_id) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *", thread_id, turn_id, role, kind, body, _j(data), client_msg_id)
    return dict(r)


async def find_message_by_client_id(thread_id, client_msg_id):
    return _row(await db.fetchrow("SELECT * FROM chat_messages WHERE thread_id=$1 AND client_msg_id=$2",
                                  thread_id, client_msg_id))


async def messages(thread_id, after: int = 0, limit: int = 200) -> list[dict]:
    rows = await db.fetch("SELECT * FROM chat_messages WHERE thread_id=$1 AND id>$2 ORDER BY id LIMIT $3",
                          thread_id, after, limit)
    return [dict(r) for r in rows]


# ── 요청(turn) ──
async def create_turn(thread_id, user_id: int, ip, request: dict, message_id: int | None, recipe_id: str,
                      recipe_version: int, action: str) -> dict:
    tid = uuid.uuid4()
    dep = await db.fetchval(
        "SELECT id FROM chat_turns WHERE thread_id=$1 AND state = ANY($2::text[]) ORDER BY created_at DESC LIMIT 1",
        thread_id, list(OPEN_STATES))
    r = await db.fetchrow(
        "INSERT INTO chat_turns(id, thread_id, user_id, client_ip, request, message_id, recipe_id, recipe_version, "
        "action, depends_on) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *",
        tid, thread_id, user_id, db.ip(ip), _j(request), message_id, recipe_id, recipe_version, action, dep)
    return dict(r)


async def get_turn(turn_id, user_id: int | None = None):
    try:
        tid = uuid.UUID(str(turn_id))
    except ValueError:
        return None
    if user_id is None:
        return _row(await db.fetchrow("SELECT * FROM chat_turns WHERE id=$1", tid))
    return _row(await db.fetchrow("SELECT * FROM chat_turns WHERE id=$1 AND user_id=$2", tid, user_id))


async def thread_turns(thread_id) -> list[dict]:
    rows = await db.fetch("SELECT * FROM chat_turns WHERE thread_id=$1 ORDER BY created_at", thread_id)
    return [dict(r) for r in rows]


async def open_turns() -> list[dict]:
    rows = await db.fetch("SELECT * FROM chat_turns WHERE state = ANY($1::text[]) ORDER BY created_at",
                          list(OPEN_STATES))
    return [dict(r) for r in rows]


async def count_open(user_id: int) -> int:
    return await db.fetchval("SELECT count(*) FROM chat_turns WHERE user_id=$1 AND state = ANY($2::text[])",
                             user_id, list(OPEN_STATES))


async def count_active(user_id: int) -> int:
    return await db.fetchval("SELECT count(*) FROM chat_turns WHERE user_id=$1 AND state='active'", user_id)


async def move(turn_id, from_state, to_state: str, stage: str | None = None, **cols) -> bool:
    """조건부 상태 전이. 0줄이면 False (다른 길이 먼저 바꿈)"""
    froms = [from_state] if isinstance(from_state, str) else list(from_state)
    sets = ["state=$2", "updated_at=now()"]
    args = [turn_id, to_state]
    if stage is not None:
        args.append(stage)
        sets.append(f"stage=${len(args)}")
    if to_state == "active":
        sets.append("started_at=COALESCE(started_at, now())")
    if to_state in FINAL_STATES:
        sets.append("finished_at=now()")
    for k, v in cols.items():
        args.append(_j(v) if k in JSON_COLS else v)
        sets.append(f"{k}=${len(args)}")
    args.append(froms)
    r = await db.fetchval(f"UPDATE chat_turns SET {', '.join(sets)} WHERE id=$1 AND state = ANY(${len(args)}::text[]) "
                          "RETURNING 1", *args)
    return r == 1


async def set_stage(turn_id, stage: str):
    await db.execute("UPDATE chat_turns SET stage=$2, updated_at=now() WHERE id=$1 AND state='active'", turn_id, stage)


async def merge_work(turn_id, patch: dict):
    await db.execute("UPDATE chat_turns SET work = COALESCE(work, '{}'::jsonb) || $2::jsonb, updated_at=now() "
                     "WHERE id=$1", turn_id, _j(patch))


async def save_progress(turn_id, progress: dict):
    await db.execute("UPDATE chat_turns SET progress=$2, updated_at=now() WHERE id=$1", turn_id, _j(progress))


async def request_cancel(turn_id) -> bool:
    return await db.fetchval("UPDATE chat_turns SET cancel_requested=true, updated_at=now() WHERE id=$1 "
                             "AND state = ANY($2::text[]) RETURNING 1", turn_id, list(OPEN_STATES)) == 1


async def cancel_requested(turn_id) -> bool:
    return bool(await db.fetchval("SELECT cancel_requested FROM chat_turns WHERE id=$1", turn_id))


# ── 작업 연결·기록 ──
async def link_job(turn_id, job_id, step: str, purpose: str | None = None, attempt: int = 1):
    await db.execute("INSERT INTO chat_turn_jobs(turn_id, job_id, step, purpose, attempt) VALUES ($1,$2,$3,$4,$5) "
                     "ON CONFLICT (turn_id, job_id) DO NOTHING", turn_id, uuid.UUID(str(job_id)), step, purpose, attempt)


async def event(turn_id, kind: str, detail=None):
    await db.execute("INSERT INTO chat_events(turn_id, kind, detail) VALUES ($1,$2,$3)", turn_id, kind, _j(detail))


async def events(turn_id) -> list[dict]:
    rows = await db.fetch("SELECT id, ts, kind, detail FROM chat_events WHERE turn_id=$1 ORDER BY id", turn_id)
    return [dict(r) for r in rows]


# ── 작업(jobs) 읽기 ──
async def job_state(job_id):
    return _row(await db.fetchrow(
        "SELECT id, seq, state, outcome, error, started_at, exec_started_at, finished_at FROM jobs WHERE id=$1",
        uuid.UUID(str(job_id))))


async def job_by_client(env_id: int, client_id: str):
    """접수 예외 뒤에 줄이 커밋돼 있는지 (6.6 find_submitted)"""
    r = await db.fetchrow("SELECT id, seq FROM jobs WHERE env_id=$1 AND client_id=$2 ORDER BY seq DESC LIMIT 1",
                          env_id, client_id)
    return None if r is None else {"prompt_id": str(r["id"]), "number": r["seq"], "node_errors": {}}


async def job_outputs(job_id) -> list[dict]:
    rows = await db.fetch("SELECT node_id, rel_path, media FROM job_outputs WHERE job_id=$1 ORDER BY id",
                          uuid.UUID(str(job_id)))
    return [dict(r) for r in rows]


# ── 첨부 ──
async def add_attachment(att: dict) -> dict:
    r = await db.fetchrow(
        "INSERT INTO chat_attachments(id, thread_id, user_id, message_id, source, media, mime, width, height, frames, "
        "bytes, sha256, input_rel, result_rel, job_id, meta) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,"
        "$15,$16) RETURNING *",
        att["id"], att["thread_id"], att["user_id"], att.get("message_id"), att["source"], att["media"], att["mime"],
        att.get("width"), att.get("height"), att.get("frames"), att.get("bytes"), att.get("sha256"),
        att.get("input_rel"), att.get("result_rel"), att.get("job_id"), _j(att.get("meta")))
    return dict(r)


async def get_attachment(att_id, user_id: int):
    try:
        aid = uuid.UUID(str(att_id))
    except ValueError:
        return None
    return _row(await db.fetchrow(
        "SELECT * FROM chat_attachments WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL", aid, user_id))


async def attach_to_message(att_ids: list, message_id: int, thread_id, user_id: int) -> list[dict]:
    """보내지 않은 업로드를 메시지에 붙인다. 이 사용자·이 대화의 것만"""
    ids = [uuid.UUID(str(a)) for a in att_ids]
    rows = await db.fetch(
        "UPDATE chat_attachments SET message_id=$2 WHERE id = ANY($1::uuid[]) AND user_id=$3 AND thread_id=$4 "
        "AND source IN ('upload','copy') AND message_id IS NULL AND deleted_at IS NULL RETURNING *", ids, message_id, user_id,
        thread_id)
    return [dict(r) for r in rows]


async def attachments_of(ids: list) -> list[dict]:
    if not ids:
        return []
    rows = await db.fetch("SELECT * FROM chat_attachments WHERE id = ANY($1::uuid[])", [uuid.UUID(str(i)) for i in ids])
    return [dict(r) for r in rows]


async def upload_bytes_used(user_id: int) -> int:
    return int(await db.fetchval("SELECT COALESCE(sum(bytes),0) FROM chat_attachments WHERE user_id=$1 "
                                 "AND deleted_at IS NULL", user_id) or 0)


# ── 레시피 검증 기록 ──
async def save_recipe_check(recipe: dict, sha: str, ok: bool, report: list):
    await db.execute("INSERT INTO studio_recipe_checks(recipe_id, recipe_version, recipe_sha256, ok, report) "
                     "VALUES ($1,$2,$3,$4,$5)", recipe["id"], recipe["version"], sha, ok, _j(report))


async def latest_recipe_check(recipe_id: str):
    return _row(await db.fetchrow("SELECT * FROM studio_recipe_checks WHERE recipe_id=$1 "
                                  "ORDER BY checked_at DESC, id DESC LIMIT 1", recipe_id))


# ── 감독이 쓰는 조회 ──
async def get_user(user_id: int):
    return _row(await db.fetchrow("SELECT * FROM users WHERE id=$1", user_id))


async def pending_turns() -> list[dict]:
    rows = await db.fetch("SELECT * FROM chat_turns WHERE state='pending' ORDER BY created_at")
    return [dict(r) for r in rows]


async def active_turns() -> list[dict]:
    rows = await db.fetch("SELECT * FROM chat_turns WHERE state='active' ORDER BY created_at")
    return [dict(r) for r in rows]


async def turn_job(turn_id, step: str):
    r = await db.fetchrow("SELECT job_id FROM chat_turn_jobs WHERE turn_id=$1 AND step=$2 "
                          "ORDER BY created_at DESC LIMIT 1", turn_id, step)
    return None if r is None else str(r["job_id"])


async def last_result_turn(thread_id):
    """결과 첨부가 있는 마지막 done 요청 (5.8 의 '직전 결과')"""
    return _row(await db.fetchrow(
        "SELECT * FROM chat_turns WHERE thread_id=$1 AND state='done' AND result IS NOT NULL "
        "AND jsonb_array_length(COALESCE(result->'attachment_ids','[]'::jsonb)) > 0 "
        "ORDER BY created_at DESC LIMIT 1", thread_id))


async def turn_of_message(message_id: int):
    return _row(await db.fetchrow("SELECT * FROM chat_turns WHERE message_id=$1 ORDER BY created_at DESC LIMIT 1",
                                  message_id))


async def approve_turn(turn_id, stage: str = "queue") -> bool:
    """승인: awaiting_approval 인 요청만 pending 으로 되돌리고 같은 UPDATE 에서 work.approved 를 남긴다.
    그러면 승인과 approved 기록 사이에 허브가 죽어도 다시 계획하지 않고, 시작은 pump 가 한도를 지켜 한다"""
    r = await db.fetchval(
        "UPDATE chat_turns SET state='pending', stage=$2, updated_at=now(), "
        "work = COALESCE(work,'{}'::jsonb) || '{\"approved\": true}'::jsonb "
        "WHERE id=$1 AND state='awaiting_approval' RETURNING 1", turn_id, stage)
    return r == 1


async def replan_turn(turn_id, stage: str, recipe_id: str, version: int) -> bool:
    """레시피 판이 바뀐 뒤의 승인: 새 판으로 처음부터 다시 계획한다 (work 와 plan 을 비운다)"""
    r = await db.fetchval(
        "UPDATE chat_turns SET state='pending', stage=$2, work='{}'::jsonb, plan=NULL, recipe_id=$3, recipe_version=$4, "
        "updated_at=now() WHERE id=$1 AND state='awaiting_approval' RETURNING 1", turn_id, stage, recipe_id, version)
    return r == 1


async def set_decision(turn_id, decision: dict):
    await db.execute("UPDATE chat_turns SET decision=$2 WHERE id=$1", turn_id, _j(decision))


async def result_attachment(turn: dict):
    """요청의 결과 첨부 하나 (없으면 None)"""
    ids = ((turn or {}).get("result") or {}).get("attachment_ids") or []
    rows = await attachments_of(ids[:1])
    return rows[0] if rows else None


async def set_retry_of(turn_id, source_id):
    await db.execute("UPDATE chat_turns SET retry_of=$2 WHERE id=$1", turn_id, source_id)


async def unsent_uploads(hours: int) -> list[dict]:
    """보내지 않고 오래된 업로드와 복사본 (9.4). 복사본은 메시지를 만들다 실패해 고아가 된 경우를 거둔다. 결과 행은 대상이 아니다"""
    rows = await db.fetch(
        "SELECT * FROM chat_attachments WHERE source IN ('upload','copy') AND message_id IS NULL AND deleted_at IS NULL "
        "AND created_at < now() - make_interval(hours => $1)", hours)
    return [dict(r) for r in rows]


async def mark_attachment_deleted(att_id):
    await db.execute("UPDATE chat_attachments SET deleted_at=now() WHERE id=$1", att_id)
