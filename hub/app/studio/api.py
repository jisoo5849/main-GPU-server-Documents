"""채팅 REST·WebSocket (docs/chat-studio-design.md 10절). 포털 앱(포트 80)에 더한다.

- 인증은 로그인 쿠키(mgs_sess). 시범 사용자(STUDIO_ALLOWED_USERS)가 아니면 403.
- GET 이 아닌 요청은 Origin 이 같은 호스트일 때만 받는다(14.3). 업로드만 multipart, 나머지는 JSON.
- 사용자에게 보이는 값만 돌려준다: 프롬프트 전문·위반 목록·job id 는 운영자 기록에만 있다(G3).
"""
import asyncio
import collections
import html as html_lib
import json
import logging
import os
import shutil
import time
import urllib.parse
import uuid

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import security
from ..config import settings
from . import files, intent, store
from .ws import hub

log = logging.getLogger("mgs.studio.api")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STATIC_OK = {"studio.js": "text/javascript; charset=utf-8", "studio.css": "text/css; charset=utf-8"}


class State:
    """시작 작업이 채우는 값. ready 가 아니면 /studio 는 503 안내다 (5.10)"""
    director = None
    ready = False
    reason = "스튜디오를 준비 중입니다."


state = State()
_recent: dict[int, collections.deque] = {}


def err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def public_user(u: dict) -> dict:
    return {"username": u["username"], "display": u.get("display_name") or u["username"]}


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True            # 주소창·curl 같은 직접 호출. 쿠키가 있어야 하므로 위험하지 않다 (SameSite=Lax)
    host = request.headers.get("host", "")
    try:
        return urllib.parse.urlsplit(origin).netloc == host
    except ValueError:
        return False


async def gate(request: Request, need_ready: bool = True):
    """(user, error 응답). 로그인·시범 사용자·Origin·준비 상태를 한 번에 본다"""
    user = await security.session_user(request.cookies.get(security.SESSION_COOKIE))
    if user is None:
        return None, err(401, "login", "로그인이 필요합니다.")
    request.state.user = user
    if not settings.studio_enabled:
        return None, err(503, "studio_off", "스튜디오는 아직 열리지 않았습니다.")
    if settings.studio_allowed_users and user["username"] not in settings.studio_allowed_users \
            and user["role"] != "admin":
        return None, err(403, "not_allowed", "스튜디오를 쓸 수 있는 사용자가 아닙니다.")
    if request.method not in ("GET", "HEAD") and not _origin_ok(request):
        return None, err(403, "origin", "요청을 처리할 수 없습니다. 새로 고침해 주세요.")
    if need_ready and not state.ready:
        return None, err(503, "studio_not_ready", state.reason)
    return user, None


def _client_ip(request: Request):
    return request.client.host if request.client else None


async def _json(request: Request):
    if "application/json" not in request.headers.get("content-type", ""):
        return None
    try:
        data = await request.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _msg(m: dict) -> dict:
    return {"id": m["id"], "role": m["role"], "kind": m["kind"], "body": m["body"], "data": m.get("data"),
            "turn_id": str(m["turn_id"]) if m.get("turn_id") else None, "created_at": _iso(m["created_at"])
            if m.get("created_at") else None}


def _turn(t: dict | None) -> dict | None:
    if t is None:
        return None
    err_ = t.get("error") or {}
    return {"id": str(t["id"]), "thread_id": str(t["thread_id"]), "state": t["state"], "stage": t.get("stage"),
            "action": t.get("action"), "progress": t.get("progress"), "result": t.get("result"),
            "error": {"code": err_.get("code")} if err_ else None}


# ── 화면 ──

async def page(request: Request):
    user = await security.session_user(request.cookies.get(security.SESSION_COOKIE))
    if user is None:
        return Response(status_code=302, headers={"location": "/login?next=/studio"})
    notice = None
    if not settings.studio_enabled:
        notice = "스튜디오는 아직 열리지 않았습니다."
    elif settings.studio_allowed_users and user["username"] not in settings.studio_allowed_users \
            and user["role"] != "admin":
        notice = "스튜디오는 시범 사용자에게만 열려 있습니다."
    elif not state.ready:
        notice = state.reason
    with open(os.path.join(STATIC_DIR, "studio.html"), encoding="utf-8") as f:
        html = f.read()
    code = 200 if notice is None else (403 if "시범" in notice else 503)
    html = html.replace("{{NOTICE}}", html_lib.escape(json.dumps(notice, ensure_ascii=False), quote=True))
    return Response(html, status_code=code, media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                                                        "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'",
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


async def static(request: Request):
    name = request.path_params["name"]
    if name not in STATIC_OK:
        return Response("없음", status_code=404, media_type="text/plain; charset=utf-8")
    return FileResponse(os.path.join(STATIC_DIR, name), media_type=STATIC_OK[name].split(";")[0],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


async def logout(request: Request):
    if not _origin_ok(request):
        return err(403, "origin", "요청을 처리할 수 없습니다. 새로 고침해 주세요.")
    sid = request.cookies.get(security.SESSION_COOKIE)
    if sid:
        await security.end_session(sid, "logout")
    resp = Response(status_code=204)
    resp.delete_cookie(security.SESSION_COOKIE)
    return resp


# ── API ──

async def me(request: Request):
    user, e = await gate(request, need_ready=False)
    if e:
        return e
    d = state.director
    return JSONResponse({"user": public_user(user), "ready": state.ready,
                         "limits": {"upload_max_mb": settings.studio_upload_max_mb,
                                    "max_active": settings.studio_max_active_per_user,
                                    "max_open": settings.studio_max_open_per_user},
                         "features": {a: bool(d and d.recipe_for(a)) for a in ("image", "i2v", "t2v", "edit")}})


async def threads(request: Request):
    user, e = await gate(request)
    if e:
        return e
    if request.method == "POST":
        t = await store.create_thread(user["id"])
        return JSONResponse({"thread": {"id": str(t["id"]), "title": t["title"], "updated_at": _iso(t["updated_at"])}},
                            status_code=201)
    rows = await store.list_threads(user["id"])
    return JSONResponse({"threads": [{"id": str(r["id"]), "title": r["title"], "updated_at": _iso(r["updated_at"])}
                                     for r in rows]})


async def thread_item(request: Request):
    user, e = await gate(request)
    if e:
        return e
    t = await store.get_thread(request.path_params["id"], user["id"])
    if t is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    if request.method == "DELETE":
        for turn in await store.thread_turns(t["id"]):
            if turn["state"] in store.OPEN_STATES:
                await state.director.cancel(turn)
        await store.soft_delete_thread(t["id"])
        return Response(status_code=204)
    return JSONResponse({"thread": {"id": str(t["id"]), "title": t["title"]}})


async def messages(request: Request):
    user, e = await gate(request)
    if e:
        return e
    t = await store.get_thread(request.path_params["id"], user["id"])
    if t is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    if request.method == "POST":
        return await _post_message(request, user, t)
    try:
        after = int(request.query_params.get("after", 0))
    except ValueError:
        after = 0
    msgs = await store.messages(t["id"], after)
    open_turns = [x for x in await store.thread_turns(t["id"]) if x["state"] in store.OPEN_STATES]
    return JSONResponse({"messages": [_msg(m) for m in msgs], "turns": [_turn(x) for x in open_turns]})


def _rate_ok(user_id: int) -> bool:
    now = time.monotonic()
    q = _recent.setdefault(user_id, collections.deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= settings.studio_max_messages_per_min:
        return False
    q.append(now)
    return True


async def _post_message(request: Request, user: dict, thread: dict):
    data = await _json(request)
    if data is None:
        return err(400, "bad_request", "요청 형식이 올바르지 않습니다.")
    att_ids = data.get("attachment_ids") or []
    if not isinstance(att_ids, list) or any(not isinstance(a, str) for a in att_ids):
        return err(400, "bad_request", "요청 형식이 올바르지 않습니다.")
    if len(att_ids) > 1:
        return err(400, "too_many_attachments", "사진은 한 장만 보낼 수 있습니다.")
    for a in att_ids:      # 메시지를 만들기 전에 형식과 주인을 확인한다
        try:
            uuid.UUID(a)
        except ValueError:
            return err(400, "bad_request", "요청 형식이 올바르지 않습니다.")
        got_att = await store.get_attachment(a, user["id"])
        if got_att is None or got_att.get("message_id") is not None or got_att["thread_id"] != thread["id"]:
            return err(400, "bad_request", "올린 사진을 찾을 수 없습니다. 다시 올려 주세요.")
    cmid = None
    if data.get("client_msg_id"):
        try:
            cmid = uuid.UUID(str(data["client_msg_id"]))
        except ValueError:
            return err(400, "bad_request", "요청 형식이 올바르지 않습니다.")
        dup = await store.find_message_by_client_id(thread["id"], cmid)
        if dup:     # 같은 메시지를 두 번 보내도 처음 만든 것을 돌려준다
            return JSONResponse({"message": _msg(dup), "turn": _turn(await store.turn_of_message(dup["id"]))},
                                status_code=202)
    try:
        text, used_default = intent.resolve_text(data.get("text"), len(att_ids))
    except intent.InputError as ex:
        return err(400, ex.code, ex.message)
    if await store.count_open(user["id"]) >= settings.studio_max_open_per_user:
        return err(429, "too_many_open", "진행 중인 요청이 많습니다. 끝난 뒤 다시 보내 주세요.")
    if not _rate_ok(user["id"]):
        return err(429, "too_many_messages", "메시지를 너무 빨리 보내고 있습니다. 잠시 뒤 다시 보내 주세요.")
    shown = "" if used_default else text
    d = state.director
    # R1 (P1): 첨부가 있으면 i2v. 없으면 정해 둔 안내 (GPU 안 씀)
    if not att_ids:
        m = await store.add_message(thread["id"], "user", "text", shown, None, None, cmid)
        a = await store.add_message(thread["id"], "assistant", "text", "사진을 함께 올려 주세요.", None, None, None)
        await store.touch_thread(thread["id"], shown[:30] if shown else None)
        await hub.send(user["id"], {"type": "message.new", "thread_id": str(thread["id"]), "message": _msg(a)})
        return JSONResponse({"message": _msg(m), "turn": None, "reply": _msg(a)}, status_code=202)
    recipe = d.recipe_for("i2v") if d else None
    if recipe is None:
        return err(503, "studio_not_ready", "이 기능은 지금 준비 중입니다.")
    m = await store.add_message(thread["id"], "user", "text", shown, {"attachment_ids": att_ids}, None, cmid)
    got = await store.attach_to_message(att_ids, m["id"], thread["id"], user["id"])
    if len(got) != len(att_ids):
        return err(400, "bad_request", "올린 사진을 찾을 수 없습니다. 다시 올려 주세요.")
    spec = intent.extract_spec(text if not used_default else "")
    req = {"text": text, "used_default": used_default, "attachment_ids": att_ids, "spec": spec, "mode": "new"}
    turn = await store.create_turn(thread["id"], user["id"], _client_ip(request), req, m["id"], recipe["id"],
                                   recipe["version"], "i2v")
    await store.touch_thread(thread["id"], shown[:30] if shown else None)
    asyncio.ensure_future(d.pump())
    return JSONResponse({"message": _msg(m), "turn": _turn(turn)}, status_code=202)


async def uploads(request: Request):
    user, e = await gate(request)
    if e:
        return e
    cl = request.headers.get("content-length")
    if not cl or not cl.isdigit():
        return err(411, "length_required", "업로드 크기를 알 수 없습니다.")
    if int(cl) > (settings.studio_upload_max_mb + 1) * 1024 * 1024:
        return err(413, "too_large", f"사진은 {settings.studio_upload_max_mb}MB 까지 올릴 수 있습니다.")
    env = await store.studio_env(settings.studio_env_slug)
    if env is None:
        return err(503, "studio_not_ready", state.reason)
    free = shutil.disk_usage(settings.envs_dir).free if os.path.isdir(settings.envs_dir) else None
    if free is not None and free < settings.studio_min_free_gb * 1024 ** 3:
        return err(503, "disk_low", "저장 공간이 부족해 지금은 사진을 올릴 수 없습니다.")
    if await store.upload_bytes_used(user["id"]) + int(cl) > settings.studio_upload_quota_mb * 1024 * 1024:
        return err(429, "quota", "올린 사진의 용량 한도를 넘었습니다.")
    form = await request.form()
    thread = await store.get_thread(str(form.get("thread_id", "")), user["id"])
    up = form.get("file")
    if thread is None or up is None or not hasattr(up, "read"):
        return err(404 if thread is None else 400, "not_found" if thread is None else "bad_request",
                   "찾을 수 없습니다." if thread is None else "사진 파일이 없습니다.")
    raw = await up.read()
    try:
        png, w, h = await asyncio.to_thread(files.process_image, raw)
    except files.UploadError as ex:
        return err(ex.status, {"bad_format": "bad_type", "too_big_pixels": "too_large"}.get(ex.code, ex.code), ex.message)
    att_id, rel, sha = await asyncio.to_thread(files.write_upload, env, user["username"], user["id"], png)
    try:
        await store.add_attachment({"id": uuid.UUID(att_id), "thread_id": thread["id"], "user_id": user["id"],
                                    "source": "upload", "media": "image", "mime": "image/png", "width": w, "height": h,
                                    "bytes": len(png), "sha256": sha, "input_rel": rel})
    except Exception:
        p = files.input_path(env, rel)      # 기록이 실패하면 파일이 고아로 남지 않게 지운다
        if p:
            os.remove(p)
        raise
    return JSONResponse({"attachment": {"id": att_id, "media": "image", "width": w, "height": h, "bytes": len(png),
                                        "url": f"/studio/api/files/{att_id}"}}, status_code=201)


async def file_get(request: Request):
    user, e = await gate(request)
    if e:
        return e
    a = await store.get_attachment(request.path_params["id"], user["id"])
    if a is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    if a.get("result_rel"):
        path = files.result_path(a["result_rel"])
        mime = a["mime"]
    else:
        env = await store.studio_env(settings.studio_env_slug)
        path = files.input_path(env, a["input_rel"]) if env else None
        mime = "image/png"
    if path is None:
        return err(404, "not_found", "파일을 찾을 수 없습니다 (지워졌거나 옮겨졌습니다).")
    return FileResponse(path, media_type=mime, headers={"X-Content-Type-Options": "nosniff",
                                                        "Cache-Control": "private, max-age=3600"})


async def turn_get(request: Request):
    user, e = await gate(request)
    if e:
        return e
    t = await store.get_turn(request.path_params["id"], user["id"])
    if t is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    return JSONResponse({"turn": _turn(t)})


async def turn_cancel(request: Request):
    user, e = await gate(request)
    if e:
        return e
    t = await store.get_turn(request.path_params["id"], user["id"])
    if t is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    if t["state"] not in store.OPEN_STATES:
        return err(409, "state_changed", "이미 끝난 요청입니다.")
    await state.director.cancel(t)
    return JSONResponse({"turn": _turn(await store.get_turn(t["id"], user["id"]))})


async def turn_retry(request: Request):
    user, e = await gate(request)
    if e:
        return e
    src = await store.get_turn(request.path_params["id"], user["id"])
    if src is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    data = await _json(request) or {}
    mode = data.get("mode")
    if mode not in ("new_seed", "rewrite"):
        return err(400, "bad_request", "요청 형식이 올바르지 않습니다.")
    if src["state"] in store.OPEN_STATES:
        return err(409, "state_changed", "아직 진행 중인 요청입니다.")
    if mode == "new_seed" and not (src.get("work") or {}).get("prompt"):
        mode = "rewrite"      # 저장된 프롬프트가 없으면 처음부터 (13.2)
    if not _rate_ok(user["id"]):
        return err(429, "too_many_messages", "잠시 뒤 다시 눌러 주세요.")
    if await store.count_open(user["id"]) >= settings.studio_max_open_per_user:
        return err(429, "too_many_open", "진행 중인 요청이 많습니다. 끝난 뒤 다시 보내 주세요.")
    d = state.director
    recipe = d.recipe_for(src.get("action") or "i2v")
    if recipe is None:
        return err(503, "studio_not_ready", "이 기능은 지금 준비 중입니다.")
    thread = await store.get_thread(src["thread_id"], user["id"])
    if thread is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    cmid = None
    try:
        cmid = uuid.UUID(str(data["client_msg_id"])) if data.get("client_msg_id") else None
    except ValueError:
        cmid = None
    if cmid:
        dup = await store.find_message_by_client_id(thread["id"], cmid)
        if dup:
            return JSONResponse({"message": _msg(dup), "turn": _turn(await store.turn_of_message(dup["id"]))},
                                status_code=202)
    label = "다시 만들기" if mode == "new_seed" else "다시 시도"
    m = await store.add_message(thread["id"], "user", "text", label, {"action": mode, "source_turn": str(src["id"])},
                                None, cmid)
    sreq = dict(src.get("request") or {})
    req = {**sreq, "mode": mode, "source_turn": str(src["id"])}
    turn = await store.create_turn(thread["id"], user["id"], _client_ip(request), req, m["id"], recipe["id"],
                                   recipe["version"], src.get("action") or "i2v")
    await store.set_retry_of(turn["id"], src["id"])
    asyncio.ensure_future(d.pump())
    return JSONResponse({"message": _msg(m), "turn": _turn(turn)}, status_code=202)


async def queue_counts(request: Request):
    user, e = await gate(request)
    if e:
        return e
    rows = await state.director.deps.queue()
    return JSONResponse({"waiting": sum(1 for r in rows if r.get("state") == "queued"),
                         "running": sum(1 for r in rows if r.get("state") != "queued"),
                         "paused": bool(await state.director.deps.setting("paused")),
                         "blocked": bool(await state.director.deps.setting("blocked"))})


# ── WebSocket ──

async def ws_endpoint(websocket: WebSocket):
    """Origin 을 accept 전에 확인한다(다른 사이트 페이지가 쿠키로 붙지 못하게). 로그인 실패는 accept 뒤 4401 로 닫는다 (14.3)"""
    origin = websocket.headers.get("origin")
    if origin and urllib.parse.urlsplit(origin).netloc != websocket.headers.get("host", ""):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    user = await security.session_user(websocket.cookies.get(security.SESSION_COOKIE))
    if user is None:
        await websocket.close(code=4401)
        return
    if not settings.studio_enabled or (settings.studio_allowed_users and user["username"] not in
                                       settings.studio_allowed_users and user["role"] != "admin"):
        await websocket.close(code=4403)
        return
    hub.add(user["id"], websocket)
    try:
        await websocket.send_text(json.dumps({"type": "hello", "ready": state.ready}))
        while True:
            ev = await websocket.receive()            # 화면이 보내는 것은 살아 있음 확인(ping)뿐이다
            if ev.get("type") == "websocket.disconnect":
                break
            if ev.get("text") == "ping":
                await websocket.send_text('{"type":"pong"}')
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.remove(user["id"], websocket)
