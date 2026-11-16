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


def _thread_settings(t: dict) -> dict:
    s = t.get("settings")
    return {"ask": s.get("ask") if isinstance(s, dict) and s.get("ask") in ("auto", "ask_long") else "auto"}


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
                         "features": {a: bool(d and d.recipe_for(a)) for a in ("image", "i2v", "t2v", "edit")},
                         "quality_high": {a: bool(d and d.recipe_for(a, "high")) for a in ("i2v", "t2v", "edit")}})


async def threads(request: Request):
    user, e = await gate(request)
    if e:
        return e
    if request.method == "POST":
        t = await store.create_thread(user["id"])
        return JSONResponse({"thread": {"id": str(t["id"]), "title": t["title"], "updated_at": _iso(t["updated_at"]),
                                        "settings": _thread_settings(t)}}, status_code=201)
    rows = await store.list_threads(user["id"])
    return JSONResponse({"threads": [{"id": str(r["id"]), "title": r["title"], "updated_at": _iso(r["updated_at"]),
                                      "settings": _thread_settings(r)} for r in rows]})


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
    if request.method == "PATCH":      # 대화 설정: 긴 영상은 먼저 계획 확인 (5.7)
        data = await _json(request)
        ask = ((data or {}).get("settings") or {}).get("ask") if isinstance((data or {}).get("settings"), dict) else None
        if data is None or ask not in ("auto", "ask_long"):
            return err(400, "bad_request", "요청 형식이 올바르지 않습니다.")
        await store.set_thread_settings(t["id"], {"ask": ask})
        t = await store.get_thread(t["id"], user["id"])
    return JSONResponse({"thread": {"id": str(t["id"]), "title": t["title"], "settings": _thread_settings(t)}})


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
        options = intent.clean_options(data.get("options"))
    except intent.InputError as ex:
        return err(400, ex.code, ex.message)
    d = state.director
    waiting = await store.awaiting_turn(thread["id"]) if d else None
    if waiting is None and await store.count_open(user["id"]) >= settings.studio_max_open_per_user:
        # 승인 대기 계획을 고치는 메시지는 열린 요청을 늘리지 않으니 한도에서 뺀다
        return err(429, "too_many_open", "진행 중인 요청이 많습니다. 끝난 뒤 다시 보내 주세요.")
    if not _rate_ok(user["id"]):
        return err(429, "too_many_messages", "메시지를 너무 빨리 보내고 있습니다. 잠시 뒤 다시 보내 주세요.")
    shown = "" if used_default else text
    last = await _last_info(thread)
    if waiting is not None and waiting.get("action") in ("i2v", "t2v") and not att_ids:
        if intent.APPROVE_RE.match(text):
            # "응", "네 진행해줘" 는 계획 수정이 아니라 승인이다
            res = await d.approve(waiting)
            m = await store.add_message(thread["id"], "user", "text", shown, None, None, cmid)
            a = await store.add_message(thread["id"], "assistant", "text",
                                        "승인했습니다. 만들기를 시작합니다." if res == "ok" else "이미 처리된 계획입니다.", None, None, None)
            await hub.send(user["id"], {"type": "message.new", "thread_id": str(thread["id"]), "message": _msg(a)})
            return JSONResponse({"message": _msg(m), "turn": _turn(await store.get_turn(waiting["id"], user["id"])),
                                 "reply": _msg(a)}, status_code=202)
        base = intent.decide(text, 0, None, used_default, _video_hint)
        if base["action"] == "answer":
            waiting, dec = None, base                  # "고마워" 같은 말은 계획을 접지 않고 답만 한다
        else:
            # 승인 대기 중에 온 새 메시지는 계획을 고쳐 달라는 말이다: 계획을 접고 원래 요청 + 이번 말로 다시 계획한다 (5.7)
            last = {"turn": waiting, "att": None, "short": {"action": waiting["action"], "media": "video"}}
            dec = {"rule": "plan_revise", "action": waiting["action"], "source": "none", "carry": "revise",
                   "attachment_role": "none", "reply": None, "notes": [], "later": False}
    else:
        waiting = None       # 새 사진이 붙은 메시지 등은 새 요청이다. 계획은 그대로 두고 승인·취소·만료를 기다린다
        dec = intent.decide(text, len(att_ids), last["short"] if last else None, used_default, _video_hint)
    # 화질: 칸에서 높음을 골랐거나, 이어 말하기(고치기)가 높음 요청을 이어 갈 때. 이미지는 가속 없는 레시피가 없어 빠름뿐이다
    want_high = options.get("quality") == "high" or (dec["carry"] == "revise" and last and d
                                                     and d.quality_of_turn(last["turn"]) == "high")
    high_ok = want_high and dec["action"] in ("i2v", "t2v", "edit")
    recipe = d.recipe_for(dec["action"], "high" if high_ok else "fast") if d and dec["action"] != "answer" else None
    if dec["action"] == "answer" or recipe is None:
        # 만들 일이 없거나 아직 없는 기능: 정해 둔 답만 한다 (GPU 안 씀, 5.3·13.1)
        reply = dec["reply"] if dec["action"] == "answer" else (
            "화질 높음은 지금 준비 중입니다. 화질을 빠름으로 바꿔 보내 주세요." if high_ok else "이 기능은 지금 준비 중입니다.")
        return await _reply_only(user, thread, cmid, shown, att_ids, reply)
    if want_high and not high_ok and dec["action"] == "image":
        dec = {**dec, "notes": list(dec["notes"]) + ["이미지는 화질 높음이 아직 없어 빠름으로 만들었습니다"]}
    env = await store.studio_env(settings.studio_env_slug)
    if env is None:
        return err(503, "studio_not_ready", state.reason)
    if waiting is not None and not await d.supersede(waiting):
        # 그 사이 승인돼 돌고 있다: 새 요청을 만들어 두 번 접수하지 않는다
        return err(409, "state_changed", "그 계획은 이미 처리됐습니다. 진행 상황을 보고 다시 보내 주세요.")
    turn_atts = list(att_ids) if dec["action"] in ("i2v", "edit") and dec["source"] == "upload" else []
    if dec["source"] == "last_result":           # R5·R7: 직전 결과 이미지를 입력으로 복사해 첫 화면·편집 대상으로 쓴다 (7.3)
        try:
            turn_atts = [await _copy_result_input(env, user, thread, last["att"])]
        except files.UploadError as ex:
            return await _reply_only(user, thread, cmid, shown, att_ids, ex.message)
    if dec["carry"] == "repeat":                 # R6b: 저장된 프롬프트·설정으로 시드만 새로 (다시 만들기와 같다)
        return await _spawn_retry(request, user, thread, last["turn"], "new_seed", shown or text, cmid,
                                  {"action": "repeat"}, rule="R6b",
                                  spec_over=intent.apply_options(intent.extract_spec(text), options))
    m = await store.add_message(thread["id"], "user", "text", shown,
                                {"attachment_ids": att_ids} if att_ids and dec["source"] != "last_result" else None, None, cmid)
    if att_ids and dec["source"] != "last_result":
        got = await store.attach_to_message(att_ids, m["id"], thread["id"], user["id"])
        if len(got) != len(att_ids):
            return err(400, "bad_request", "올린 사진을 찾을 수 없습니다. 다시 올려 주세요.")
    if turn_atts and dec["source"] == "last_result":
        await store.attach_to_message(turn_atts, m["id"], thread["id"], user["id"])
    req_text = text
    reuse = {}
    if dec["carry"] == "revise" and last and dec["action"] in ("image", "i2v", "t2v"):
        # R6a·R6·R7b: 원래 요청 + 이번 말로 다시 쓴다 (5.8). 영상은 같은 첫 화면과 읽어 둔 결과를 다시 쓴다
        lreq = last["turn"].get("request") or {}
        prev = str(lreq.get("text") or "").strip()
        req_text = (prev + "\n" + text).strip()[-intent.MAX_TEXT:] if prev else text
        if dec["action"] == "i2v":
            turn_atts = list(lreq.get("attachment_ids") or [])
            reuse = {"reuse_frame_of": str(last["turn"]["id"])}
    spec = intent.apply_options(intent.extract_spec(req_text if not used_default else ""), options)
    req = {"text": req_text, "used_default": used_default, "attachment_ids": turn_atts, "spec": spec, "mode": "new",
           "notes": dec["notes"], "rule": dec["rule"], "carry": dec["carry"], **reuse}
    turn = await store.create_turn(thread["id"], user["id"], _client_ip(request), req, m["id"], recipe["id"],
                                   recipe["version"], dec["action"])
    await store.set_decision(turn["id"], {"action": dec["action"], "source": dec["source"], "carry": dec["carry"],
                                          "by": "rule", "rule": dec["rule"]})
    await store.touch_thread(thread["id"], shown[:30] if shown else None)
    asyncio.ensure_future(d.pump())
    return JSONResponse({"message": _msg(m), "turn": _turn(turn)}, status_code=202)


def _video_hint(text: str) -> bool:
    """카메라·속도·루프 말(crew_lib)과 1인칭 말을 영상 말로 본다 (5.3). 실패해도 규칙은 계속 돈다"""
    try:
        from .. import crew_lib, video
        return bool(crew_lib.query(text).video_demands) or bool(video.pov_wanted(text))
    except Exception:  # noqa: BLE001
        return False


async def _last_info(thread: dict):
    """직전 결과: 결과 첨부가 있는 마지막 done 요청 (5.8)"""
    t = await store.last_result_turn(thread["id"])
    a = await store.result_attachment(t) if t else None
    if not t or not a:
        return None
    return {"turn": t, "att": a, "short": {"action": t.get("action"), "media": a["media"]}}


async def _reply_only(user: dict, thread: dict, cmid, shown: str, att_ids: list, reply: str):
    """GPU 를 쓰지 않는 답. 사용자 말풍선과 정해 둔 답을 남긴다"""
    m = await store.add_message(thread["id"], "user", "text", shown, {"attachment_ids": att_ids} if att_ids else None,
                                None, cmid)
    if att_ids:
        await store.attach_to_message(att_ids, m["id"], thread["id"], user["id"])
    a = await store.add_message(thread["id"], "assistant", "text", reply, None, None, None)
    await store.touch_thread(thread["id"], shown[:30] if shown else None)
    await hub.send(user["id"], {"type": "message.new", "thread_id": str(thread["id"]), "message": _msg(a)})
    return JSONResponse({"message": _msg(m), "turn": None, "reply": _msg(a)}, status_code=202)


async def _copy_result_input(env: dict, user: dict, thread: dict, src_att: dict) -> str:
    """결과 이미지를 입력 폴더로 복사한 새 첨부 id (7.3). 파일이 바뀌었거나 없으면 UploadError"""
    if not src_att.get("result_rel"):
        raise files.UploadError("gone", "결과 파일을 찾을 수 없어 이어서 만들 수 없습니다.", 404)
    att_id, rel, sha, w, h, n = await asyncio.to_thread(files.copy_result_as_input, env, user["username"], user["id"],
                                                        src_att["result_rel"], src_att.get("sha256"))
    try:
        await store.add_attachment({"id": uuid.UUID(att_id), "thread_id": thread["id"], "user_id": user["id"],
                                    "source": "copy", "media": "image", "mime": "image/png", "width": w, "height": h,
                                    "bytes": n, "sha256": sha, "input_rel": rel})
    except Exception:
        p = files.input_path(env, rel)
        if p:
            os.remove(p)
        raise
    return att_id


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
    thread = await store.get_thread(src["thread_id"], user["id"])
    if thread is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    cmid = None
    try:
        cmid = uuid.UUID(str(data["client_msg_id"])) if data.get("client_msg_id") else None
    except ValueError:
        cmid = None
    label = "다시 만들기" if mode == "new_seed" else "다시 시도"
    return await _spawn_retry(request, user, thread, src, mode, label, cmid, {"action": mode})


async def _spawn_retry(request: Request, user: dict, thread: dict, src: dict, mode: str, label: str, cmid,
                       msg_data: dict, rule: str | None = None, spec_over: dict | None = None):
    """src 요청을 같은 설정으로 다시 한다 (13.2). mode=new_seed 는 저장된 프롬프트로 시드만 새로, rewrite 는 처음부터"""
    d = state.director
    # 다시 만들기·다시 시도는 원래 요청과 같은 레시피(같은 화질)로 한다. 그 레시피가 꺼졌으면 같은 화질의 다른 레시피로
    recipe = d.recipe_map.get(src.get("recipe_id") or "")
    if recipe is None or recipe["id"] in d.disabled:
        recipe = d.recipe_for(src.get("action") or "i2v", d.quality_of_turn(src))
    if recipe is None:
        return err(503, "studio_not_ready", "이 기능은 지금 준비 중입니다.")
    if cmid:
        dup = await store.find_message_by_client_id(thread["id"], cmid)
        if dup:
            return JSONResponse({"message": _msg(dup), "turn": _turn(await store.turn_of_message(dup["id"]))},
                                status_code=202)
    m = await store.add_message(thread["id"], "user", "text", label, {**msg_data, "source_turn": str(src["id"])},
                                None, cmid)
    sreq = dict(src.get("request") or {})
    req = {**sreq, "mode": mode, "source_turn": str(src["id"])}
    if spec_over:      # 이번 글에 비율·길이 지정이 있으면 그것을 따른다
        req["spec"] = {**(sreq.get("spec") or {}), **{k: v for k, v in spec_over.items() if v and k in ("seconds", "aspect")}}
    turn = await store.create_turn(thread["id"], user["id"], _client_ip(request), req, m["id"], recipe["id"],
                                   recipe["version"], src.get("action") or "i2v")
    await store.set_retry_of(turn["id"], src["id"])
    if rule:
        await store.set_decision(turn["id"], {"action": src.get("action"), "source": "none", "carry": "repeat",
                                              "by": "rule", "rule": rule})
    asyncio.ensure_future(d.pump())
    return JSONResponse({"message": _msg(m), "turn": _turn(turn)}, status_code=202)


async def turn_approve(request: Request):
    """[승인]: 계획 카드를 승인해 접수를 진행한다. 이미 처리된 요청이면 409 (두 번 눌러도 접수는 한 번)"""
    user, e = await gate(request)
    if e:
        return e
    t = await store.get_turn(request.path_params["id"], user["id"])
    if t is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    if t["state"] != "awaiting_approval":
        return err(409, "state_changed", "이미 처리된 요청입니다.")
    res = await state.director.approve(t)
    if res == "conflict":
        return err(409, "state_changed", "이미 처리된 요청입니다.")
    if res == "gone":
        return err(503, "studio_not_ready", "이 기능은 지금 준비 중입니다.")
    return JSONResponse({"turn": _turn(await store.get_turn(t["id"], user["id"])), "replanned": res == "replan"})


async def turn_to_video(request: Request):
    """[이걸로 영상 만들기]: 이미지 결과를 입력으로 복사해 이미지→영상을 새로 시작한다 (13.2)"""
    user, e = await gate(request)
    if e:
        return e
    src = await store.get_turn(request.path_params["id"], user["id"])
    if src is None:
        return err(404, "not_found", "찾을 수 없습니다.")
    a = await store.result_attachment(src) if src["state"] == "done" else None
    if a is None or a["media"] != "image":
        return err(409, "state_changed", "이미지 결과에서만 영상으로 이어 만들 수 있습니다.")
    data = await _json(request) or {}
    if await store.count_open(user["id"]) >= settings.studio_max_open_per_user:
        return err(429, "too_many_open", "진행 중인 요청이 많습니다. 끝난 뒤 다시 보내 주세요.")
    if not _rate_ok(user["id"]):
        return err(429, "too_many_messages", "잠시 뒤 다시 눌러 주세요.")
    d = state.director
    recipe = d.recipe_for("i2v") if d else None
    thread = await store.get_thread(src["thread_id"], user["id"])
    env = await store.studio_env(settings.studio_env_slug)
    if recipe is None or env is None:
        return err(503, "studio_not_ready", "이 기능은 지금 준비 중입니다.")
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
    for x in await store.thread_turns(thread["id"]):     # 같은 결과로 만드는 영상이 이미 도는 중이면 그것을 돌려준다
        if x["state"] in store.OPEN_STATES and (x.get("request") or {}).get("source_turn") == str(src["id"]) \
                and x.get("action") == "i2v":
            return JSONResponse({"message": None, "turn": _turn(x)}, status_code=202)
    try:
        att_id = await _copy_result_input(env, user, thread, a)
    except files.UploadError as ex:
        return err(ex.status, ex.code, ex.message)
    m = await store.add_message(thread["id"], "user", "text", "▶ 이걸로 영상 만들기",
                                {"action": "to_video", "source_turn": str(src["id"])}, None, cmid)
    await store.attach_to_message([att_id], m["id"], thread["id"], user["id"])
    req = {"text": intent.DEFAULT_I2V_TEXT, "used_default": True, "attachment_ids": [att_id], "spec": {}, "mode": "new",
           "source_turn": str(src["id"]), "rule": "button"}
    turn = await store.create_turn(thread["id"], user["id"], _client_ip(request), req, m["id"], recipe["id"],
                                   recipe["version"], "i2v")
    await store.set_decision(turn["id"], {"action": "i2v", "source": "last_result", "carry": "new", "by": "button",
                                          "rule": "to_video"})
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
