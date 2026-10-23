"""로그인, 세션, API 키."""
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

from . import db
from .config import settings

SESSION_COOKIE = "mgs_sess"
_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, VerificationError):
        return False


async def authenticate(username: str, password: str):
    user = await db.fetchrow("SELECT * FROM users WHERE username=$1 AND active", username)
    if user and verify_password(user["pw_hash"], password):
        return user
    return None


async def create_session(user_id: int, client_ip, user_agent: str) -> str:
    sid = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO sessions(id, user_id, ip, user_agent) VALUES ($1, $2, $3, $4)",
        sid, user_id, db.ip(client_ip), (user_agent or "")[:300],
    )
    return sid


async def session_user(sid: str | None):
    """유효한 세션이면 사용자 행을 돌려준다. 오래 쓰지 않은 세션은 만료 처리한다."""
    if not sid:
        return None
    row = await db.fetchrow(
        "SELECT s.id AS sid, s.last_seen, s.ended_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.id=$1",
        sid,
    )
    if not row or row["ended_at"] is not None or not row["active"]:
        return None
    expired = await db.fetchval(
        "SELECT now() - $1::timestamptz > make_interval(hours => $2)", row["last_seen"], settings.session_hours
    )
    if expired:
        await end_session(sid, "expired")
        await db.log_access(row["id"], None, "session_expired")
        return None
    await db.execute("UPDATE sessions SET last_seen=now() WHERE id=$1 AND last_seen < now() - interval '1 minute'", sid)
    return row


async def end_session(sid: str, reason: str):
    await db.execute(
        "UPDATE sessions SET ended_at=now(), end_reason=$2 WHERE id=$1 AND ended_at IS NULL", sid, reason
    )


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_api_key(user_id: int, label: str) -> str:
    key = "mgs_" + secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO api_keys(user_id, prefix, key_hash, label) VALUES ($1, $2, $3, $4)",
        user_id, key[:10], _key_hash(key), label,
    )
    return key


async def api_key_user(key: str | None):
    if not key:
        return None
    row = await db.fetchrow(
        "SELECT k.id AS key_id, u.* FROM api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE k.key_hash=$1 AND k.revoked_at IS NULL AND u.active",
        _key_hash(key),
    )
    if row:
        await db.execute("UPDATE api_keys SET last_used_at=now() WHERE id=$1", row["key_id"])
    return row


def bearer(headers) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
