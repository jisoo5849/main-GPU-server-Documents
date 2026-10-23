"""PostgreSQL 연결 풀."""
import ipaddress
import json
from pathlib import Path

import asyncpg

from .config import settings

pool: asyncpg.Pool | None = None


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
