"""작업공간(환경) 관리: 생성, 편집 공간 시작, 버전 고정(스냅샷), 되돌리기, 오래된 버전 정리."""
import asyncio
import logging
import os
import re
import shutil
import time

from . import db, dockerctl, hostops
from .config import settings

log = logging.getLogger("mgs.envs")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MANAGER_CONFIG = """[default]
security_level = normal
network_mode = public
allow_git_url_install = true
allow_pip_install = true
"""

_env_locks: dict[int, asyncio.Lock] = {}
_create_lock = asyncio.Lock()   # 두 요청이 같은 포트를 고르지 않게
_bg: set = set()
_port_cache: dict[int, dict] = {}
_port_cache_at = 0.0


def env_lock(env_id: int) -> asyncio.Lock:
    return _env_locks.setdefault(env_id, asyncio.Lock())


async def env_by_port(port: int):
    """환경 포트 → 환경 정보 (10초 캐시)."""
    global _port_cache, _port_cache_at
    if port not in _port_cache or time.monotonic() - _port_cache_at > 10:
        rows = await db.fetch(
            "SELECT e.*, u.username, u.linux_uid FROM envs e JOIN users u ON u.id = e.owner_id"
        )
        _port_cache = {r["port"]: dict(r) for r in rows}
        _port_cache_at = time.monotonic()
    return _port_cache.get(port)


def invalidate_cache():
    global _port_cache_at
    _port_cache_at = 0.0


async def get_env(env_id: int):
    row = await db.fetchrow(
        "SELECT e.*, u.username, u.linux_uid FROM envs e JOIN users u ON u.id = e.owner_id WHERE e.id=$1", env_id
    )
    return dict(row) if row else None


async def _free_port() -> int:
    used = {r["port"] for r in await db.fetch("SELECT port FROM envs")}
    for p in range(settings.env_port_start, settings.env_port_end + 1):
        if p not in used:
            return p
    raise RuntimeError("사용할 수 있는 환경 포트가 없습니다. .env 의 HUB_ENV_PORT_END 를 늘리세요.")


def _prepare_state(slug: str, uid: int):
    gid = settings.group_gid
    base = settings.state_path(slug)
    for sub in ("", "home", "cache", "input", "user", "editor", "worker", "user/__manager"):
        hostops.ensure_dir(os.path.join(base, sub), uid, gid, 0o2770 if sub else 0o2770)
    cfg = os.path.join(base, "user", "__manager", "config.ini")
    if not os.path.exists(cfg):
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(MANAGER_CONFIG)
        os.chown(cfg, uid, gid)


async def create_env(owner_id: int, name: str, source_version_id: int | None = None) -> dict:
    """새 작업공간을 만든다. 기본은 템플릿에서, source_version_id 가 있으면 그 버전을 복제해서."""
    if not NAME_RE.match(name):
        raise ValueError("환경 이름은 영문 소문자·숫자·- 로 32자 이내여야 합니다.")
    user = await db.fetchrow("SELECT * FROM users WHERE id=$1", owner_id)
    if user is None:
        raise ValueError("사용자가 없습니다.")
    slug = f"{user['username']}-{name}"
    if await db.fetchval("SELECT 1 FROM envs WHERE slug=$1", slug):
        raise ValueError(f"이미 있는 환경입니다: {name}")

    live = settings.live_path(slug)
    if source_version_id:
        src = await db.fetchval("SELECT snapshot_path FROM env_versions WHERE id=$1 AND deleted_at IS NULL",
                                source_version_id)
        if not src:
            raise ValueError("복제할 버전이 없습니다.")
    else:
        src = settings.template_path()
        if not os.path.isdir(src):
            raise ValueError(f"템플릿이 없습니다: {src} (install/05-build-runtime.sh 실행 필요)")
    async with _create_lock:
        try:
            await hostops.snapshot(src, live)
            await hostops.chown_tree(live, user["linux_uid"], settings.group_gid)
            await asyncio.to_thread(_prepare_state, slug, user["linux_uid"])
            hostops.ensure_dir(os.path.join(settings.results_dir, user["username"]), user["linux_uid"],
                               settings.group_gid)
            hostops.ensure_dir(os.path.join(settings.results_dir, user["username"], name), user["linux_uid"],
                               settings.group_gid)
            port = await _free_port()
            row = await db.fetchrow(
                "INSERT INTO envs(owner_id, name, slug, port, template) VALUES ($1, $2, $3, $4, $5) RETURNING *",
                owner_id, name, slug, port, settings.template,
            )
        except Exception:
            # 반쯤 만들어진 작업공간을 치워서 같은 이름으로 다시 만들 수 있게 한다
            await hostops.delete_subvolume(live)
            await asyncio.to_thread(shutil.rmtree, settings.state_path(slug), True)
            raise
    invalidate_cache()
    await dockerctl.start_editor(slug, user["username"], name, user["linux_uid"])
    return dict(row)


async def ensure_editors():
    """허브 시작 때: 실행 중이어야 할 편집 공간을 모두 켠다."""
    for e in await db.fetch(
        "SELECT e.*, u.username, u.linux_uid FROM envs e JOIN users u ON u.id = e.owner_id WHERE e.state='running'"
    ):
        try:
            await dockerctl.start_editor(e["slug"], e["username"], e["name"], e["linux_uid"])
        except Exception as ex:  # noqa: BLE001
            log.error("editor %s start failed: %s", e["slug"], ex)


async def restart_editor(env: dict):
    await dockerctl.remove(dockerctl.editor_name(env["slug"]))
    await dockerctl.start_editor(env["slug"], env["username"], env["name"], env["linux_uid"])


async def manager_busy(env: dict) -> bool:
    """편집 공간에서 노드 설치(pip, uv, git)가 진행 중인지."""
    for cmd in await dockerctl.process_cmdlines(dockerctl.editor_name(env["slug"])):
        c = f" {cmd} "
        if (" pip install" in c or " -m pip " in c or "/uv " in c or " uv pip" in c
                or re.search(r"\bgit (clone|pull|fetch|checkout|submodule)\b", c)):
            return True
    return False


async def pin_version(env: dict) -> int:
    """실행 접수 때 호출. 마지막 버전 이후 환경이 바뀌었으면 새 읽기 전용 스냅샷을 찍는다. 버전 id 반환."""
    slug = env["slug"]
    live = settings.live_path(slug)
    async with env_lock(env["id"]):
        last = await db.fetchrow(
            "SELECT * FROM env_versions WHERE env_id=$1 AND deleted_at IS NULL ORDER BY vno DESC LIMIT 1", env["id"]
        )
        gen = await hostops.generation(live)
        # 세대 번호 비교는 파일 삭제·이름 변경까지 잡는다
        if last is not None and last["btrfs_gen"] == gen and os.path.isdir(last["snapshot_path"]):
            return last["id"]
        vno = (last["vno"] + 1) if last else 1
        snap = os.path.join(settings.snap_dir(slug), f"v{vno:04d}")
        await hostops.snapshot(live, snap, readonly=True)
        gen_after = await hostops.generation(live)
        vid = await db.fetchval(
            "INSERT INTO env_versions(env_id, vno, snapshot_path, btrfs_gen) VALUES ($1, $2, $3, $4) RETURNING id",
            env["id"], vno, snap, gen_after,
        )
    t = asyncio.create_task(_record_version_details(vid, env, snap))
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return vid


async def _record_version_details(vid: int, env: dict, snap: str):
    try:
        commit = await hostops.git_head(os.path.join(snap, "ComfyUI"))
        nodes = await hostops.list_nodes(os.path.join(snap, "ComfyUI", "custom_nodes"))
        freeze = await dockerctl.exec_text(dockerctl.editor_name(env["slug"]),
                                           ["/env/venv/bin/python", "-m", "pip", "freeze"])
        await db.execute(
            "UPDATE env_versions SET comfy_commit=$2, nodes=$3, pip_freeze=$4 WHERE id=$1",
            vid, commit, nodes, freeze,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("version details for %s failed: %s", vid, e)


async def rollback(env: dict, version_id: int):
    """작업공간을 지정한 버전으로 되돌린다. 현재 상태는 먼저 새 버전으로 남겨 둔다."""
    ver = await db.fetchrow("SELECT * FROM env_versions WHERE id=$1 AND env_id=$2 AND deleted_at IS NULL",
                            version_id, env["id"])
    if ver is None:
        raise ValueError("버전이 없습니다.")
    await pin_version(env)
    async with env_lock(env["id"]):
        await dockerctl.remove(dockerctl.editor_name(env["slug"]))
        live = settings.live_path(env["slug"])
        await hostops.delete_subvolume(live)
        await hostops.snapshot(ver["snapshot_path"], live)
    await dockerctl.start_editor(env["slug"], env["username"], env["name"], env["linux_uid"])


async def gc_versions(protected_version_ids: set[int]):
    """오래된 버전 스냅샷 정리: 대기·실행 중 작업이 쓰지 않고, 보관 표시가 없고, 환경의 최신 버전이 아닌 것."""
    rows = await db.fetch(
        """
        SELECT v.* FROM env_versions v
        WHERE v.deleted_at IS NULL AND NOT v.keep
          AND v.created_at < now() - make_interval(days => $1)
          AND v.vno < (SELECT max(vno) FROM env_versions w WHERE w.env_id = v.env_id AND w.deleted_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.env_version_id = v.id AND j.state <> 'finished')
        """,
        settings.version_keep_days,
    )
    for v in rows:
        if v["id"] in protected_version_ids:
            continue
        try:
            await hostops.delete_subvolume(v["snapshot_path"])
            await db.execute("UPDATE env_versions SET deleted_at=now() WHERE id=$1", v["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("gc %s failed: %s", v["snapshot_path"], e)
