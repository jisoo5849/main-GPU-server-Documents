"""허브 실행: 포털(80), LLM 중계(11434), 환경 포트(8101~)를 한 프로세스에서 듣고, 들어온 포트로 나눈다."""
import asyncio
import logging
import socket

import uvicorn

from . import db, dockerctl, envs, gpu, llm, portal, proxy
from .config import settings
from .scheduler import scheduler

log = logging.getLogger("mgs")

portal_app = portal.build_app()
llm_app = llm.build_app()
env_app = proxy.build_app()


async def startup():
    await db.init()
    if not gpu.init():
        log.warning("NVML 을 쓸 수 없습니다. GPU 메모리 확인 없이 동작합니다(개발 환경에서만 허용).")
    await dockerctl.ensure_networks()
    await envs.ensure_editors()
    await scheduler.start()
    log.info("hub ready: portal :%s, llm :%s, envs :%s-%s", settings.portal_port, settings.llm_port,
             settings.env_port_start, settings.env_port_end)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await startup()
                except Exception as e:  # noqa: BLE001
                    log.exception("startup failed")
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    port = (scope.get("server") or (None, None))[1]
    if port == settings.portal_port:
        await portal_app(scope, receive, send)
    elif port == settings.llm_port:
        await llm_app(scope, receive, send)
    else:
        await env_app(scope, receive, send)


def _bind(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(256)
    s.set_inheritable(True)
    return s


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ports = [settings.portal_port, settings.llm_port,
             *range(settings.env_port_start, settings.env_port_end + 1)]
    sockets = [_bind(p) for p in ports]
    config = uvicorn.Config(app, lifespan="on", ws="websockets", proxy_headers=False, log_level="info",
                            timeout_keep_alive=30, ws_max_size=256 * 1024 * 1024)
    server = uvicorn.Server(config)
    asyncio.run(server.serve(sockets=sockets))


if __name__ == "__main__":
    main()
