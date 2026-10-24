"""컨테이너 관리. 편집 공간(GPU 없음)과 GPU 실행 공간(최대 1개)을 만든다.

GPU는 run_worker() 에서만 연결한다. 다른 어떤 컨테이너에도 GPU를 요청하지 않는다.
"""
import asyncio
import logging

import docker
from docker.errors import NotFound
from docker.types import DeviceRequest, IPAMConfig, IPAMPool

from .config import settings

log = logging.getLogger("mgs.docker")
_client = None


def client():
    global _client
    if _client is None:
        _client = docker.from_env(timeout=120)
    return _client


def _ensure_network(name: str, subnet: str):
    c = client()
    try:
        c.networks.get(name)
        return
    except NotFound:
        pass
    c.networks.create(
        name,
        driver="bridge",
        ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=subnet)]),
        # 같은 네트워크의 컨테이너끼리도 서로 접속하지 못하게 한다
        options={"com.docker.network.bridge.enable_icc": "false"},
        labels={"mgs": "1"},
    )


async def ensure_networks():
    await asyncio.to_thread(_ensure_network, settings.editor_network, settings.editor_subnet)
    await asyncio.to_thread(_ensure_network, settings.worker_network, settings.worker_subnet)


def _common_volumes(env_path: str, state_path: str) -> dict:
    return {
        env_path: {"bind": "/env", "mode": "rw"},
        state_path: {"bind": "/state", "mode": "rw"},
        settings.models_dir: {"bind": "/srv/models", "mode": "ro"},
        settings.results_dir: {"bind": "/srv/results", "mode": "rw"},
        settings.runtime_dir: {"bind": "/opt/mgs", "mode": "ro"},
    }


def editor_name(slug: str) -> str:
    return f"mgs-ed-{slug}"


def _get(name: str):
    try:
        return client().containers.get(name)
    except NotFound:
        return None


async def get(name: str):
    return await asyncio.to_thread(_get, name)


def _ip_of(container, network: str) -> str | None:
    container.reload()
    nets = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    return (nets.get(network) or {}).get("IPAddress") or None


def _start_editor(slug: str, username: str, env_name: str, uid: int):
    c = _get(editor_name(slug))
    if c is not None:
        if c.status != "running":
            c.start()
        return _ip_of(c, settings.editor_network)
    c = client().containers.run(
        settings.runtime_image,
        command=["editor"],
        name=editor_name(slug),
        detach=True,
        user=f"{uid}:{settings.group_gid}",
        environment={
            "MGS_USER": username,
            "MGS_ENV": env_name,
            "MGS_EDITOR_EXTRA_ARGS": settings.editor_extra_args,
            # GPU 장치를 절대 넣지 않는다
            "NVIDIA_VISIBLE_DEVICES": "void",
        },
        volumes=_common_volumes(settings.live_path(slug), settings.state_path(slug)),
        network=settings.editor_network,
        extra_hosts={"host.docker.internal": "host-gateway"},
        restart_policy={"Name": "unless-stopped"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        labels={"mgs.role": "editor", "mgs.env": slug},
    )
    return _ip_of(c, settings.editor_network)


async def start_editor(slug: str, username: str, env_name: str, uid: int) -> str | None:
    return await asyncio.to_thread(_start_editor, slug, username, env_name, uid)


async def editor_ip(slug: str) -> str | None:
    c = await get(editor_name(slug))
    if c is None or c.status != "running":
        return None
    return await asyncio.to_thread(_ip_of, c, settings.editor_network)


def _remove(name: str, timeout: int = 30):
    c = _get(name)
    if c is None:
        return
    try:
        c.stop(timeout=timeout)
    except Exception:  # noqa: BLE001 - 이미 멈췄거나 응답이 없으면 아래에서 강제 삭제
        pass
    try:
        c.remove(force=True)
    except NotFound:
        pass


async def remove(name: str, timeout: int = 30):
    await asyncio.to_thread(_remove, name, timeout)


async def restart(name: str):
    c = await get(name)
    if c is not None:
        await asyncio.to_thread(c.restart, timeout=20)


def _run_worker(run_path: str, state_path: str, username: str, env_name: str, uid: int, labels: dict):
    _remove(settings.worker_name, timeout=10)
    c = client().containers.run(
        settings.runtime_image,
        command=["worker"],
        name=settings.worker_name,
        detach=True,
        user=f"{uid}:{settings.group_gid}",
        environment={"MGS_USER": username, "MGS_ENV": env_name},
        volumes=_common_volumes(run_path, state_path),
        network=settings.worker_network,
        extra_hosts={"host.docker.internal": "host-gateway"},
        device_requests=[DeviceRequest(device_ids=[str(settings.gpu_index)], capabilities=[["gpu"]])],
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        labels={"mgs.role": "worker", **labels},
    )
    c.reload()
    return {
        "id": c.id,
        "ip": _ip_of(c, settings.worker_network),
        "pid": c.attrs["State"]["Pid"],
    }


async def run_worker(run_path, state_path, username, env_name, uid, labels) -> dict:
    return await asyncio.to_thread(_run_worker, run_path, state_path, username, env_name, uid, labels)


async def is_running(name: str) -> bool:
    c = await get(name)
    return c is not None and c.status == "running"


async def logs_tail(name: str, lines: int = 40) -> str:
    c = await get(name)
    if c is None:
        return ""
    data = await asyncio.to_thread(c.logs, tail=lines)
    return data.decode(errors="replace")


async def exec_text(name: str, cmd: list[str]) -> str:
    c = await get(name)
    if c is None:
        return ""
    res = await asyncio.to_thread(c.exec_run, cmd)
    return res.output.decode(errors="replace") if res.output else ""


async def process_cmdlines(name: str) -> list[str]:
    c = await get(name)
    if c is None or c.status != "running":
        return []
    # Docker 의 top 은 PID 열이 있어야 한다. 마지막 열(args)에 나머지 필드가 합쳐져 온다.
    top = await asyncio.to_thread(c.top, ps_args="-o pid,args")
    return [p[-1] for p in top.get("Processes", []) if p]


async def container_id(name: str) -> str | None:
    c = await get(name)
    return c.id if c is not None else None


async def list_by_role(role: str):
    return await asyncio.to_thread(client().containers.list, all=True, filters={"label": f"mgs.role={role}"})
