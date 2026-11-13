"""채팅 화면으로 보내는 실시간 이벤트 (docs/chat-studio-design.md 10.2).

사용자마다 열린 WebSocket 을 모아 두고, 감독이 만든 이벤트를 그 사용자의 화면에만 보낸다.
이벤트를 놓쳐도 되도록 화면은 새로 고침할 때 REST 로 상태를 다시 읽는다 (진행 상태는 chat_turns.progress 에 저장).
"""
import asyncio
import json
import logging

log = logging.getLogger("mgs.studio.ws")


class Hub:
    def __init__(self):
        self._conns: dict[int, set] = {}

    def add(self, user_id: int, ws):
        self._conns.setdefault(user_id, set()).add(ws)

    def remove(self, user_id: int, ws):
        s = self._conns.get(user_id)
        if s:
            s.discard(ws)
            if not s:
                self._conns.pop(user_id, None)

    def count(self, user_id: int) -> int:
        return len(self._conns.get(user_id, ()))

    async def send(self, user_id: int, event: dict):
        text = json.dumps(event, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self._conns.get(user_id, ())):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=3)    # 멈춘 탭이 감독을 붙잡지 않게
            except Exception:  # noqa: BLE001 - 끊긴 탭
                dead.append(ws)
        for ws in dead:
            self.remove(user_id, ws)


hub = Hub()
