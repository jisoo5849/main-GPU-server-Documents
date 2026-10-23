"""허브 연동 노드 (관리자 제공, 읽기 전용으로 모든 환경에 연결된다).

편집 공간(MGS_ROLE=editor)
  - POST /mgs/validate : 작업을 대기열에 넣지 않고 검증만 한다. 허브가 실행 접수 때 호출한다.
  - 편집 공간의 자체 대기열은 막는다. 편집 공간은 어떤 작업도 직접 실행하지 않는다.
실행 공간(MGS_ROLE=worker)
  - 아무것도 바꾸지 않는다.
"""
import logging
import os

import execution
import server
from aiohttp import web

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

ROLE = os.environ.get("MGS_ROLE", "")
log = logging.getLogger("mgs_bridge")


def _jsonable(value):
    if isinstance(value, (set, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


if ROLE == "editor":
    prompt_server = server.PromptServer.instance

    @prompt_server.routes.post("/mgs/validate")
    async def mgs_validate(request):
        data = await request.json()
        prompt = data.get("prompt") or {}
        prompt_id = data.get("prompt_id") or "00000000-0000-0000-0000-000000000000"
        partial = data.get("partial_execution_targets")
        replacer = getattr(prompt_server, "node_replace_manager", None)
        if replacer is not None:
            replacer.apply_replacements(prompt)
        valid = await execution.validate_prompt(prompt_id, prompt, partial)
        return web.json_response({
            "valid": bool(valid[0]),
            "error": _jsonable(valid[1]),
            "outputs": _jsonable(valid[2]),
            "node_errors": _jsonable(valid[3]),
            "prompt": prompt,
        })

    _original_put = prompt_server.prompt_queue.put

    def _blocked_put(item):
        log.warning("mgs: editor does not execute prompts; dropped %s", item[1] if len(item) > 1 else item)

    prompt_server.prompt_queue.put = _blocked_put
    log.info("mgs_bridge: editor mode (validation only, local queue disabled)")
