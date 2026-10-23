"""작업자 브라우저의 WebSocket 목록. GPU 실행 공간의 진행 상황을 해당 브라우저 탭으로 전달한다."""
import json
import logging

log = logging.getLogger("mgs.relay")


class BrowserSockets:
    def __init__(self):
        # (env_id, client_id) → set of starlette WebSocket
        self._socks: dict[tuple[int, str], set] = {}
        self._flags: dict[tuple[int, str], dict] = {}

    def register(self, env_id: int, client_id: str, ws):
        self._socks.setdefault((env_id, client_id), set()).add(ws)

    def unregister(self, env_id: int, client_id: str, ws):
        s = self._socks.get((env_id, client_id))
        if s:
            s.discard(ws)
            if not s:
                self._socks.pop((env_id, client_id), None)

    def set_flags(self, env_id: int, client_id: str, flags: dict):
        self._flags[(env_id, client_id)] = flags

    def flags(self, env_id: int, client_id: str) -> dict | None:
        return self._flags.get((env_id, client_id))

    async def _send(self, socks, text=None, data=None):
        dead = []
        for ws in list(socks):
            try:
                if text is not None:
                    await ws.send_text(text)
                else:
                    await ws.send_bytes(data)
            except Exception:  # noqa: BLE001 - 끊긴 탭
                dead.append(ws)
        for ws in dead:
            socks.discard(ws)

    async def send_text(self, env_id: int, client_id: str | None, text: str):
        if client_id and (env_id, client_id) in self._socks:
            await self._send(self._socks[(env_id, client_id)], text=text)

    async def send_bytes(self, env_id: int, client_id: str | None, data: bytes):
        if client_id and (env_id, client_id) in self._socks:
            await self._send(self._socks[(env_id, client_id)], data=data)

    async def send_json(self, env_id: int, client_id: str | None, typ: str, data: dict):
        await self.send_text(env_id, client_id, json.dumps({"type": typ, "data": data}))

    async def broadcast_status(self, remaining: int):
        text = json.dumps({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": remaining}}}})
        for socks in list(self._socks.values()):
            await self._send(socks, text=text)


sockets = BrowserSockets()
