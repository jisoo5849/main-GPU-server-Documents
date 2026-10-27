"""LLM·임베딩 중계 (포트 11434, Ollama·OpenAI 형식 그대로).

- 모델 목록 조회 등 GPU 를 쓰지 않는 요청은 바로 Ollama 로 넘긴다.
- 생성·채팅·임베딩 요청은 ComfyUI 작업과 같은 DB 대기열에 넣고, 차례가 오면 Ollama 로 넘긴다.
- 지금 GPU 를 쓰고 있는 실행 공간(워크플로우 안의 Ollama 노드)에서 온 호출은 대기열에 다시 넣지 않고
  바로 처리한다. 자기 차례를 자기가 기다리는 교착을 막기 위해서다.
"""
import asyncio
import contextlib
import ipaddress
import json
import logging
import time
import uuid

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import db, jobs, security
from .config import settings
from .scheduler import LlmWaiter, scheduler

log = logging.getLogger("mgs.llm")

QUEUED = {
    "/api/generate": "llm", "/api/chat": "llm", "/v1/chat/completions": "llm", "/v1/completions": "llm",
    "/v1/responses": "llm", "/v1/messages": "llm",
    "/api/embed": "embed", "/api/embeddings": "embed", "/v1/embeddings": "embed",
}
DIRECT = {"/api/tags", "/api/version", "/api/show", "/api/ps", "/v1/models"}
ADMIN_ONLY = {"/api/pull", "/api/push", "/api/create", "/api/delete", "/api/copy"}
HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "authorization", "cookie",
       "accept-encoding"}

_client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))
_bg: set = set()


def _keep(task):
    """끝날 때까지 참조를 잡아 두어 가비지 컬렉션으로 사라지지 않게 한다."""
    _bg.add(task)
    task.add_done_callback(_bg.discard)


def _in(subnet: str, ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(subnet)
    except ValueError:
        return False


async def _caller(request: Request):
    """요청한 사용자: API 키 → 로그인 세션 → 편집·실행 공간 컨테이너 주소 순으로 확인."""
    user = await security.api_key_user(security.bearer(request.headers))
    if user:
        return user
    user = await security.session_user(request.cookies.get(security.SESSION_COOKIE))
    if user:
        return user
    ip = request.client.host if request.client else ""
    if _in(settings.editor_subnet, ip) or _in(settings.worker_subnet, ip):
        from . import dockerctl
        for c in await dockerctl.list_by_role("editor") + await dockerctl.list_by_role("worker"):
            nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
            if any((n or {}).get("IPAddress") == ip for n in nets.values()):
                slug = c.labels.get("mgs.env")
                row = await db.fetchrow("SELECT u.* FROM envs e JOIN users u ON u.id=e.owner_id WHERE e.slug=$1",
                                        slug)
                if row:
                    return row
    return None


def _headers(request: Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in HOP}


def _force_unload(path: str, body: bytes) -> bytes:
    """Ollama 고유 요청은 keep_alive 0 으로 고정 (응답 직후 VRAM 반환)."""
    if not path.startswith("/api/") or not body:
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if isinstance(data, dict):
        data["keep_alive"] = 0
        return json.dumps(data).encode()
    return body


def _model_of(body: bytes) -> str | None:
    try:
        data = json.loads(body)
        return data.get("model") if isinstance(data, dict) else None
    except ValueError:
        return None


def _usage(tail: bytes) -> tuple[int | None, int | None]:
    for line in reversed(tail.decode(errors="replace").strip().splitlines()):
        line = line.strip().removeprefix("data:").strip()
        if not line or line == "[DONE]":
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if "usage" in d and isinstance(d["usage"], dict):
            return d["usage"].get("prompt_tokens"), d["usage"].get("completion_tokens")
        if "prompt_eval_count" in d or "eval_count" in d:
            return d.get("prompt_eval_count"), d.get("eval_count")
        return None, None
    return None, None


async def _forward(request: Request, path: str, body: bytes, on_done):
    """Ollama 로 넘기고 응답을 그대로 흘려보낸다. 끝나면 on_done(status, tail, ms) 호출."""
    t0 = time.monotonic()
    upstream = _client.build_request(request.method, f"{settings.ollama_upstream}{path}",
                                     params=request.query_params, headers=_headers(request), content=body)
    try:
        resp = await _client.send(upstream, stream=True)
    except httpx.HTTPError as e:
        await on_done(502, b"", int((time.monotonic() - t0) * 1000))
        return JSONResponse({"error": f"Ollama 연결 실패: {e}"}, status_code=502)

    async def finish(tail):
        try:
            await resp.aclose()
        finally:
            await on_done(resp.status_code, tail, int((time.monotonic() - t0) * 1000))

    async def stream():
        tail = b""
        try:
            async for chunk in resp.aiter_raw():
                tail = (tail + chunk)[-8192:]
                yield chunk
        finally:
            # 요청한 쪽이 중간에 끊으면 이 생성기는 취소된다. 마무리(기록, 대기열 해제)는 취소되지 않는 별도 작업으로 돌린다.
            _keep(asyncio.get_running_loop().create_task(finish(tail)))

    headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP | {"content-encoding"}}
    return StreamingResponse(stream(), status_code=resp.status_code, headers=headers)


async def _log_call(job_id, parent_id, user_id, path, model, nested, status, tail, ms):
    pt, ct = _usage(tail)
    await db.execute(
        "INSERT INTO llm_calls(job_id, parent_job_id, user_id, endpoint, model, nested, prompt_tokens, "
        "completion_tokens, duration_ms, status) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
        job_id, parent_id, user_id, path, model, nested, pt, ct, ms, status,
    )


async def handle(request: Request):
    path = "/" + request.path_params["path"]
    ip = request.client.host if request.client else ""
    body = await request.body()

    if path in DIRECT:
        return await _forward(request, path, body, _noop)

    # 실행 중인 워크플로우 안에서 나온 호출: 대기열을 거치지 않고 바로 처리
    w, lease = scheduler.worker, scheduler.lease
    if w and lease and lease["kind"] == "comfy" and ip == w.get("ip"):
        if not scheduler.lease_open:
            return JSONResponse({"error": "작업이 끝나는 중이라 LLM 호출을 받지 않습니다."}, status_code=409)
        scheduler.nested_inflight += 1
        model = _model_of(body)
        parent = lease["id"]
        once = []

        async def nested_done(status, tail, ms):
            if once:
                return
            once.append(True)
            scheduler.nested_inflight -= 1
            await _log_call(None, parent, lease["user_id"], path, model, True, status, tail, ms)

        return await _forward(request, path, _force_unload(path, body), nested_done)

    user = await _caller(request)
    if user is None:
        return JSONResponse({"error": "인증이 필요합니다. Authorization: Bearer <API 키> 를 보내세요."},
                            status_code=401)
    if path in ADMIN_ONLY:
        if user["role"] != "admin":
            return JSONResponse({"error": "관리자만 사용할 수 있습니다."}, status_code=403)
        await db.log_access(user["id"], ip, "admin_action", {"llm": path, "model": _model_of(body)})
        return await _forward(request, path, body, _noop)
    if path not in QUEUED:
        return JSONResponse({"error": f"지원하지 않는 경로입니다: {path}"}, status_code=404)

    await db.log_access(user["id"], ip, "api_key_use", {"endpoint": path})
    job_id = str(uuid.uuid4())
    model = _model_of(body)
    waiter = LlmWaiter()
    scheduler.llm_waiters[job_id] = waiter
    try:
        req_meta = json.loads(body) if body else {}
        if isinstance(req_meta, dict):
            req_meta = {k: v for k, v in req_meta.items() if k not in ("images",)}
    except ValueError:
        req_meta = {"raw_len": len(body)}
    async with db.pool.acquire() as conn:
        await jobs.insert_job(conn, job_id=job_id, kind=QUEUED[path], user_id=user["id"], client_ip=ip,
                              api_endpoint=path, model=model, request=req_meta)
    scheduler.wake.set()
    await jobs.notify_queue_changed()

    # 차례가 올 때까지 기다린다. 그 사이 요청한 쪽이 끊으면 취소한다.
    while not waiter.go.is_set():
        try:
            await asyncio.wait_for(waiter.go.wait(), timeout=1)
        except asyncio.TimeoutError:
            if await request.is_disconnected():
                waiter.cancelled = True
                await jobs.cancel_queued(None, [job_id])
                scheduler.llm_waiters.pop(job_id, None)
                return Response(status_code=499)
    # 차례가 오기 전에 관리자가 취소했거나 사전 확인에서 거부된 경우
    if waiter.cancelled:
        scheduler.llm_waiters.pop(job_id, None)
        return JSONResponse({"error": waiter.result.get("message", "요청이 취소되었습니다.")}, status_code=409)

    async def queued_done(status, tail, ms):
        if waiter.done.is_set():
            return
        waiter.result = {"status": status}
        await _log_call(uuid.UUID(job_id), None, user["id"], path, model, False, status, tail, ms)
        waiter.done.set()

    return await _forward(request, path, _force_unload(path, body), queued_done)


class HelperBusy(Exception):
    """허브 내부 LLM 호출이 차례를 받지 못했다 (대기열이 길거나 관리자가 취소)."""


@contextlib.asynccontextmanager
async def llm_turn(user, ip: str, model: str, purpose: str, wait_seconds: int | None = None):
    """허브 안에서 쓰는 LLM 호출(프롬프트 확장·결과 검사)도 사용자 요청과 같은 GPU 대기열을 거친다.

    차례가 오면 call(payload) 로 Ollama 를 여러 번 부를 수 있다 (고쳐 쓰기 반복 동안 모델을 다시 올리지 않게).
    끝나면 모델을 내리고 llm_calls 에 한 번 기록한 뒤 대기열을 놓아 준다.
    """
    job_id = str(uuid.uuid4())
    waiter = LlmWaiter()
    scheduler.llm_waiters[job_id] = waiter
    async with db.pool.acquire() as conn:
        await jobs.insert_job(conn, job_id=job_id, kind="llm", user_id=user["id"], client_ip=ip,
                              api_endpoint="/api/chat", model=model, request={"purpose": purpose})
    scheduler.wake.set()
    await jobs.notify_queue_changed()
    try:
        await asyncio.wait_for(waiter.go.wait(), timeout=wait_seconds or settings.helper_wait_seconds)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        waiter.cancelled = True
        scheduler.llm_waiters.pop(job_id, None)
        await asyncio.shield(jobs.cancel_queued(None, [job_id]))
        if isinstance(e, asyncio.CancelledError):
            raise
        raise HelperBusy("GPU 대기열이 길어 차례를 받지 못했습니다. 잠시 뒤 다시 시도하세요.") from None
    if waiter.cancelled:
        scheduler.llm_waiters.pop(job_id, None)
        raise HelperBusy(waiter.result.get("message", "요청이 취소되었습니다."))

    t0 = time.monotonic()
    counts = {"prompt_eval_count": 0, "eval_count": 0}
    status = 200

    async def call(payload: dict, endpoint: str = "/api/chat") -> dict:
        nonlocal status
        body = {**payload, "model": model, "stream": False, "keep_alive": "2m"}
        try:
            r = await _client.post(f"{settings.ollama_upstream}{endpoint}", json=body,
                                   timeout=httpx.Timeout(600, connect=10))
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            status = 502
            raise RuntimeError(f"Ollama 호출 실패: {e}") from None
        if r.status_code >= 400:
            status = r.status_code
            raise RuntimeError(f"Ollama 오류 ({r.status_code}): {data.get('error', '')}")
        for k in counts:
            counts[k] += int(data.get(k) or 0)
        return data

    try:
        yield call
    except BaseException:
        status = status if status >= 400 else 500
        raise
    finally:
        # 끝나면 바로 모델을 내린다 (scheduler 의 메모리 해제 확인이 기다리지 않게). 실패해도 대기열은 놓아 준다.
        try:
            await asyncio.shield(_client.post(f"{settings.ollama_upstream}/api/generate",
                                              json={"model": model, "keep_alive": 0}, timeout=30))
        except Exception:  # noqa: BLE001 - 정리 단계
            pass
        try:
            tail = json.dumps(counts).encode()
            await asyncio.shield(_log_call(uuid.UUID(job_id), None, user["id"], f"helper:{purpose}", model, False,
                                           status, tail, int((time.monotonic() - t0) * 1000)))
        except Exception:  # noqa: BLE001
            log.exception("llm_turn 기록 실패")
        waiter.result = {"status": status}
        waiter.done.set()


async def _noop(*_a):
    return None


async def root(_request):
    return Response("Ollama is running (mgs hub)", media_type="text/plain")


def build_app() -> Starlette:
    return Starlette(routes=[
        Route("/", root, methods=["GET", "HEAD"]),
        Route("/{path:path}", handle, methods=["GET", "POST", "DELETE", "HEAD"]),
    ])
