"""환경 포트(8101~)의 ComfyUI 화면 중계.

작업자는 http://main:<환경 포트>/ 로 자기 편집 공간의 ComfyUI 를 그대로 쓴다. 대부분의 요청은 편집 공간으로
그대로 넘기고, 아래 요청만 허브가 직접 처리한다.
  POST /prompt                 실행 접수 (DB 대기열)
  GET/POST /queue, /interrupt  대기열 조회·취소·중단 (DB 와 GPU 실행 공간 기준)
  /history, /api/jobs          이력 (DB 기준)
  GET /view                    결과·입력·미리보기 파일
  POST /free                   무시 (GPU 메모리는 스케줄러가 관리)
  /ws                          WebSocket: 편집 공간 연결 + GPU 실행 공간 진행 상황 전달
"""
import asyncio
import json
import logging
import mimetypes
import os
import urllib.parse

import httpx
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import db, dockerctl, envs, jobs, security
from .config import settings
from .relay import sockets
from .scheduler import scheduler

log = logging.getLogger("mgs.proxy")
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "host", "content-length"}
_client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))


def _port(scope) -> int | None:
    server = scope.get("server")
    return server[1] if server else None


def _portal_url(request_host: str, next_url: str = "") -> str:
    host = (request_host or settings.public_host).split(":")[0]
    port = "" if settings.portal_port == 80 else f":{settings.portal_port}"
    q = f"?next={urllib.parse.quote(next_url)}" if next_url else ""
    return f"http://{host}{port}/login{q}"


async def _authorize(conn):
    """세션 사용자와 이 포트의 환경. 주인(또는 관리자)만 열 수 있다."""
    env = await envs.env_by_port(_port(conn.scope))
    if env is None:
        return None, None, "env"
    user = await security.session_user(conn.cookies.get(security.SESSION_COOKIE))
    if user is None:
        return env, None, "login"
    if user["id"] != env["owner_id"] and user["role"] != "admin":
        return env, user, "forbidden"
    return env, user, None


def _route(raw_path: str) -> str:
    path = urllib.parse.unquote(raw_path)
    return path[4:] if path.startswith("/api/") else path


async def handle_http(request: Request):
    env, user, problem = await _authorize(request)
    if problem == "env":
        return Response("이 포트에 연결된 작업공간이 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
    if problem == "login":
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(_portal_url(request.headers.get("host", ""), str(request.url)), 302)
        return JSONResponse({"error": "로그인이 필요합니다."}, status_code=401)
    if problem == "forbidden":
        return Response("다른 사용자의 작업공간입니다.", status_code=403, media_type="text/plain; charset=utf-8")

    raw = request.scope.get("raw_path", request.url.path.encode()).decode("latin-1")
    path = _route(raw)
    method = request.method
    ip = request.client.host if request.client else None

    if method == "POST" and path == "/prompt":
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(jobs.comfy_error("invalid_json", "잘못된 요청입니다."), status_code=400)
        try:
            return JSONResponse(await jobs.submit_comfy(env, user, body, ip))
        except jobs.SubmitError as e:
            return JSONResponse(e.body, status_code=e.status)
    if path == "/queue":
        if method == "GET":
            return JSONResponse(await jobs.queue_view(env["id"]))
        data = await _json(request)
        if data.get("clear"):
            await jobs.cancel_queued(env["id"], None)
        if data.get("delete"):
            await jobs.cancel_queued(env["id"], [str(x) for x in data["delete"]])
        return Response(status_code=200)
    if method == "POST" and path == "/interrupt":
        data = await _json(request)
        await scheduler.interrupt(env["id"], data.get("prompt_id"))
        return Response(status_code=200)
    if method == "POST" and path == "/free":
        return Response(status_code=200)
    if path == "/jobs" and method == "GET":
        return JSONResponse(await jobs.jobs_list(env["id"], request.query_params))
    if path.startswith("/jobs/"):
        parts = path.split("/")
        if method == "POST" and path == "/jobs/cancel":
            data = await _json(request)
            ids = [str(x) for x in data.get("job_ids", [])]
            n = await jobs.cancel_queued(env["id"], ids)
            for jid in ids:
                await scheduler.interrupt(env["id"], jid)
            return JSONResponse({"cancelled": n > 0})
        if method == "POST" and len(parts) == 4 and parts[3] == "cancel":
            n = await jobs.cancel_queued(env["id"], [parts[2]])
            await scheduler.interrupt(env["id"], parts[2])
            return JSONResponse({"cancelled": n > 0})
        if method == "GET" and len(parts) == 3:
            d = await jobs.job_detail(env["id"], parts[2])
            return JSONResponse(d) if d else JSONResponse({"error": "Job not found"}, status_code=404)
    if path == "/history" or path.startswith("/history/"):
        if method == "GET":
            pid = path.split("/", 2)[2] if path.startswith("/history/") else None
            mi = request.query_params.get("max_items")
            return JSONResponse(await jobs.history_view(env["id"], pid, int(mi) if mi else None))
        return Response(status_code=200)
    if method == "GET" and path == "/view":
        return await _view(request, env)
    if path.startswith("/mgs/"):
        return Response(status_code=404)
    if method == "GET" and path == "/":
        await db.log_access(user["id"], ip, "env_open", {"env": env["slug"]})

    return await _forward(request, env, raw)


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


async def _forward(request: Request, env: dict, raw_path: str):
    ip = await dockerctl.editor_ip(env["slug"])
    if not ip:
        return Response("편집 공간이 실행 중이 아닙니다. 포털의 '내 작업공간'에서 다시 시작하세요.",
                        status_code=503, media_type="text/plain; charset=utf-8")
    qs = request.scope.get("query_string", b"").decode("latin-1")
    url = f"http://{ip}:8189{raw_path}" + (f"?{qs}" if qs else "")
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "cookie"]
    req = _client.build_request(request.method, url, headers=headers, content=request.stream())
    try:
        resp = await _client.send(req, stream=True)
    except httpx.HTTPError as e:
        return Response(f"편집 공간 연결 실패: {e}", status_code=502, media_type="text/plain; charset=utf-8")
    out_headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in HOP]

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    r = StreamingResponse(body(), status_code=resp.status_code)
    r.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out_headers]
    return r


def _safe_join(base: str, *parts: str) -> str | None:
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, *[p for p in parts if p]))
    if target == base_real or target.startswith(base_real + os.sep):
        return target
    return None


async def _view(request: Request, env: dict):
    q = request.query_params
    filename = q.get("filename", "")
    ftype = q.get("type", "output")
    for tag in ("output", "input", "temp"):
        if filename.endswith(f" [{tag}]"):
            filename, ftype = filename[: -len(tag) - 3], tag
    if not filename or filename.startswith("/") or ".." in filename:
        return Response(status_code=400)
    sub = q.get("subfolder", "")
    state = settings.state_path(env["slug"])
    if ftype == "output":
        bases = [os.path.join(settings.results_dir, env["username"])]
    elif ftype == "input":
        bases = [os.path.join(state, "input")]
    else:
        bases = [os.path.join(state, "worker", "temp"), os.path.join(state, "editor", "temp")]
    for base in bases:
        path = _safe_join(base, sub, filename)
        if path and os.path.isfile(path):
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            return FileResponse(path, media_type=ctype, headers={"Cache-Control": "no-cache"})
    return Response(status_code=404)


# ── WebSocket ─────────────────────────────────────────────

async def handle_ws(websocket: WebSocket):
    env, _user, problem = await _authorize(websocket)
    if problem:
        await websocket.close(code=4401)
        return
    ip = await dockerctl.editor_ip(env["slug"])
    if not ip:
        await websocket.close(code=1013)
        return
    client_id = websocket.query_params.get("clientId") or os.urandom(16).hex()
    try:
        upstream = await websockets.connect(f"ws://{ip}:8189/ws?clientId={urllib.parse.quote(client_id)}",
                                            max_size=None, ping_interval=20, ping_timeout=60)
    except (OSError, websockets.WebSocketException):
        await websocket.close(code=1011)
        return
    await websocket.accept()
    sockets.register(env["id"], client_id, websocket)
    remaining = await db.fetchval("SELECT count(*) FROM jobs WHERE state = ANY($1::text[])",
                                  list(jobs.ACTIVE_STATES))

    async def browser_to_editor():
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                text = msg["text"]
                try:
                    data = json.loads(text)
                    if data.get("type") == "feature_flags":
                        sockets.set_flags(env["id"], client_id, data.get("data") or {})
                except ValueError:
                    pass
                await upstream.send(text)
            elif msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])

    async def editor_to_browser():
        async for msg in upstream:
            if isinstance(msg, bytes):
                await websocket.send_bytes(msg)
                continue
            try:
                data = json.loads(msg)
            except ValueError:
                await websocket.send_text(msg)
                continue
            if data.get("type") == "status":
                # 대기열 숫자는 메인 전체 기준으로 바꿔서 보여 준다 (sid 는 그대로 둔다)
                st = (data.get("data") or {}).setdefault("status", {})
                st.setdefault("exec_info", {})["queue_remaining"] = int(remaining_now[0])
                msg = json.dumps(data)
            await websocket.send_text(msg)

    remaining_now = [remaining]

    async def refresh_remaining():
        while True:
            await asyncio.sleep(5)
            remaining_now[0] = await db.fetchval("SELECT count(*) FROM jobs WHERE state = ANY($1::text[])",
                                                 list(jobs.ACTIVE_STATES))

    tasks = [asyncio.create_task(browser_to_editor()), asyncio.create_task(editor_to_browser()),
             asyncio.create_task(refresh_remaining())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        sockets.unregister(env["id"], client_id, websocket)
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass


def build_app() -> Starlette:
    return Starlette(routes=[
        WebSocketRoute("/ws", handle_ws),
        WebSocketRoute("/api/ws", handle_ws),
        Route("/{path:path}", handle_http, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]),
    ])
