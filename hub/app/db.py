"""PostgreSQL 연결 풀."""
import ipaddress
import json
import logging
from pathlib import Path

import asyncpg

from .config import settings

log = logging.getLogger("mgs.db")

pool: asyncpg.Pool | None = None

# schema.sql 이 붙이는 제약. 기존 데이터가 어긋나면 허브는 시작하되 제약이 빠지거나 '검사 전' 으로 남는다
_GUARDS = ("job_models_pkey", "env_versions_env_id_id_key", "jobs_env_version_fk", "jobs_env_version_needs_env",
           "jobs_state_check", "jobs_outcome_check", "incidents_kind_check")


async def _init_conn(conn):
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def init() -> asyncpg.Pool:
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=12, init=_init_conn)
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            # 여러 프로세스(허브, CLI)가 동시에 스키마를 적용하지 않게 잠근다
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(7200)")
                await conn.execute(schema)
            rows = await conn.fetch("SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY($1::text[])",
                                    list(_GUARDS))
            have = {r["conname"]: r["convalidated"] for r in rows}
            missing = [c for c in _GUARDS if c not in have]
            unchecked = [c for c, ok in have.items() if not ok]
            if missing or unchecked:
                log.warning("DB 제약 미적용 missing=%s unvalidated=%s: 기존 행에 위반이 있다. "
                            "install/db/precheck-2026-11.sql 로 위반 행을 찾는다", missing, unchecked)
    return pool


async def connect_raw() -> asyncpg.Connection:
    conn = await asyncpg.connect(settings.database_url)
    await _init_conn(conn)
    return conn


async def fetch(sql, *args):
    async with pool.acquire() as c:
        return await c.fetch(sql, *args)


async def fetchrow(sql, *args):
    async with pool.acquire() as c:
        return await c.fetchrow(sql, *args)


async def fetchval(sql, *args):
    async with pool.acquire() as c:
        return await c.fetchval(sql, *args)


async def execute(sql, *args):
    async with pool.acquire() as c:
        return await c.execute(sql, *args)


async def get_setting(key, default=None):
    v = await fetchval("SELECT value FROM settings WHERE key=$1", key)
    return default if v is None else v


async def set_setting(key, value):
    await execute(
        "INSERT INTO settings(key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key, value,
    )


def ip(value):
    """inet 컬럼에 넣을 값. 잘못된 값이면 None."""
    if not value:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


async def log_access(user_id, client_ip, event, detail=None):
    await execute(
        "INSERT INTO access_log(user_id, ip, event, detail) VALUES ($1, $2, $3, $4)",
        user_id, ip(client_ip), event, detail,
    )


async def incident(kind, detail):
    await execute("INSERT INTO incidents(kind, detail) VALUES ($1, $2)", kind, detail)


async def proc_incident(kind, proc_key, detail):
    """같은 프로세스의 반복 감지는 새 줄 대신 기존 줄의 마지막 시각·횟수·최근 내용을 고친다.
    최근 사용량(mem_mb)은 매번 바뀌므로 지금까지의 최대값은 max_mem_mb 로 따로 지킨다."""
    await execute(
        "INSERT INTO incidents(kind, detail, proc_key, last_seen) VALUES ($1, $2, $3, now()) "
        "ON CONFLICT (kind, proc_key) WHERE proc_key IS NOT NULL DO UPDATE "
        "SET last_seen = now(), seen_count = incidents.seen_count + 1, "
        "detail = EXCLUDED.detail || jsonb_build_object('max_mem_mb', GREATEST("
        "(incidents.detail->>'max_mem_mb')::int, (EXCLUDED.detail->>'max_mem_mb')::int))",
        kind, detail, proc_key,
    )
