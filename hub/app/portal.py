"""포털(포트 80): 로그인, 모델 선택, 내 작업공간, 전체 대기열, 공유 작업 이력, 모델 추가, 관리자."""
import datetime
import os
import shutil
import urllib.parse

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import db, envs, jobs, models_index, security
from .config import settings
from .scheduler import scheduler

_tpl = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
                   autoescape=select_autoescape(["html"]))
INBOX_FOLDERS = ("checkpoints", "diffusion_models", "loras", "vae", "text_encoders", "clip_vision", "controlnet",
                 "upscale_models", "embeddings", "audio_encoders", "model_patches")


def _ip(request: Request):
    return request.client.host if request.client else None


def _host(request: Request) -> str:
    return (request.headers.get("host") or settings.public_host).split(":")[0]


def env_url(request: Request, port: int) -> str:
    return f"http://{_host(request)}:{port}/"


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["host"] = _host(request)
    ctx["flash"] = request.query_params.get("msg")
    return HTMLResponse(_tpl.get_template(name).render(request=request, **ctx))


async def current_user(request: Request):
    user = await security.session_user(request.cookies.get(security.SESSION_COOKIE))
    request.state.user = user
    return user


def login_required(handler):
    async def wrapper(request: Request):
        if await current_user(request) is None:
            nxt = urllib.parse.quote(str(request.url.path))
            return RedirectResponse(f"/login?next={nxt}", 302)
        return await handler(request)
    return wrapper


def admin_required(handler):
    async def wrapper(request: Request):
        user = await current_user(request)
        if user is None:
            return RedirectResponse("/login", 302)
        if user["role"] != "admin":
            return Response("관리자만 볼 수 있습니다.", status_code=403, media_type="text/plain; charset=utf-8")
        return await handler(request)
    return wrapper


def back(path: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(path + (f"?msg={urllib.parse.quote(msg)}" if msg else ""), 303)


# ── 로그인 ─────────────────────────────────────────────────

async def login(request: Request):
    if request.method == "GET":
        return render(request, "login.html", next=request.query_params.get("next", "/"), user=None)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    nxt = str(form.get("next") or "/")
    user = await security.authenticate(username, str(form.get("password", "")))
    if user is None:
        await db.log_access(None, _ip(request), "login_fail", {"username": username})
        return render(request, "login.html", next=nxt, error="아이디 또는 비밀번호가 맞지 않습니다.", user=None)
    sid = await security.create_session(user["id"], _ip(request), request.headers.get("user-agent", ""))
    await db.log_access(user["id"], _ip(request), "login_ok")
    # 같은 호스트의 다른 포트(작업공간)에서도 쓰는 쿠키. 포트는 쿠키 범위를 나누지 않는다.
    target = nxt if nxt.startswith(("/", f"http://{_host(request)}:")) else "/"
    resp = RedirectResponse(target, 303)
    resp.set_cookie(security.SESSION_COOKIE, sid, httponly=True, samesite="lax",
                    max_age=settings.session_hours * 3600)
    return resp


async def logout(request: Request):
    sid = request.cookies.get(security.SESSION_COOKIE)
    user = await security.session_user(sid)
    if sid:
        await security.end_session(sid, "logout")
    if user:
        await db.log_access(user["id"], _ip(request), "logout")
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(security.SESSION_COOKIE)
    return resp


# ── 홈: 모델 선택 ──────────────────────────────────────────

async def _ollama_models() -> tuple[list, list]:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            data = (await c.get(f"{settings.ollama_upstream}/api/tags")).json()
    except (httpx.HTTPError, ValueError):
        return [], []
    llm, embed = [], []
    for m in data.get("models", []):
        fam = " ".join((m.get("details") or {}).get("families") or [])
        item = {"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
                "params": (m.get("details") or {}).get("parameter_size", "")}
        (embed if ("embed" in item["name"] or "bert" in fam) else llm).append(item)
    return llm, embed


@login_required
async def home(request: Request):
    user = request.state.user
    my_envs = await db.fetch("SELECT * FROM envs WHERE owner_id=$1 ORDER BY name", user["id"])
    cat = models_index.catalog()
    llm, embed = await _ollama_models()
    return render(request, "home.html", envs=my_envs, catalog=cat, llm=llm, embed=embed,
                  queue=await jobs.global_queue(), lease=scheduler.lease,
                  llm_url=f"http://{_host(request)}:{settings.llm_port}",
                  webui_url=f"http://{_host(request)}:8090/",
                  paused=await db.get_setting("paused", False), blocked=await db.get_setting("blocked"),
                  new_key=request.query_params.get("key"))


@login_required
async def select(request: Request):
    user = request.state.user
    form = await request.form()
    kind, model = str(form.get("kind", "")), str(form.get("model", ""))
    env_id = form.get("env_id")
    await db.log_access(user["id"], _ip(request), "portal_select", {"kind": kind, "model": model, "env_id": env_id})
    if kind in ("image", "video"):
        env = await db.fetchrow("SELECT * FROM envs WHERE id=$1 AND owner_id=$2", int(env_id or 0), user["id"])
        if env is None:
            return back("/", "작업공간을 먼저 선택하세요.")
        await db.log_access(user["id"], _ip(request), "env_open", {"env": env["slug"]})
        return RedirectResponse(env_url(request, env["port"]), 303)
    if kind == "llm":
        return RedirectResponse(f"http://{_host(request)}:8090/", 303)
    return back("/", f"임베딩 API: {settings.llm_port} 포트의 /api/embed 를 API 키와 함께 호출하세요. 모델: {model}")


@login_required
async def create_key(request: Request):
    user = request.state.user
    form = await request.form()
    key = await security.create_api_key(user["id"], str(form.get("label", ""))[:60])
    return RedirectResponse(f"/?key={urllib.parse.quote(key)}", 303)


async def status_json(request: Request):
    if await current_user(request) is None:
        return JSONResponse({"error": "login"}, status_code=401)
    q = await jobs.global_queue()
    for r in q:
        r["id"] = str(r["id"])
        for k in ("submitted_at", "started_at"):
            r[k] = r[k].isoformat() if r[k] else None
    return JSONResponse({"queue": q, "paused": await db.get_setting("paused", False),
                         "blocked": bool(await db.get_setting("blocked"))})


# ── 내 작업공간 ────────────────────────────────────────────

@login_required
async def env_create(request: Request):
    user = request.state.user
    form = await request.form()
    name = str(form.get("name", "")).strip().lower()
    src = form.get("source_version_id")
    try:
        env = await envs.create_env(user["id"], name, int(src) if src else None)
    except (ValueError, RuntimeError) as e:
        return back("/", str(e))
    await db.log_access(user["id"], _ip(request), "env_create", {"env": env["slug"]})
    return back("/", f"작업공간 '{name}' 을 만들었습니다 (포트 {env['port']}).")


async def _my_env(request: Request):
    env = await envs.get_env(int(request.path_params["env_id"]))
    user = request.state.user
    if env is None or (env["owner_id"] != user["id"] and user["role"] != "admin"):
        return None
    return env


@login_required
async def env_restart(request: Request):
    env = await _my_env(request)
    if env is None:
        return back("/", "작업공간이 없습니다.")
    await envs.restart_editor(env)
    return back("/", f"'{env['name']}' 편집 공간을 다시 시작했습니다.")


@login_required
async def env_versions(request: Request):
    env = await _my_env(request)
    if env is None:
        return back("/", "작업공간이 없습니다.")
    versions = await db.fetch("SELECT * FROM env_versions WHERE env_id=$1 ORDER BY vno DESC", env["id"])
    return render(request, "versions.html", env=env, versions=versions)


@login_required
async def env_rollback(request: Request):
    env = await _my_env(request)
    if env is None:
        return back("/", "작업공간이 없습니다.")
    form = await request.form()
    try:
        await envs.rollback(env, int(form.get("version_id")))
    except ValueError as e:
        return back(f"/envs/{env['id']}/versions", str(e))
    await db.log_access(request.state.user["id"], _ip(request), "env_rollback",
                        {"env": env["slug"], "version_id": int(form.get("version_id"))})
    return back(f"/envs/{env['id']}/versions", "선택한 버전으로 되돌렸습니다.")


# ── 공유 작업 이력 ─────────────────────────────────────────

@login_required
async def history(request: Request):
    q = request.query_params
    where, args = ["j.state='finished'"], []
    for key, col in (("user", "u.username"), ("env", "e.name"), ("kind", "j.kind"), ("outcome", "j.outcome")):
        if q.get(key):
            args.append(q[key])
            where.append(f"{col} = ${len(args)}")
    if q.get("model"):
        args.append(f"%{q['model']}%")
        where.append(f"(j.model ILIKE ${len(args)} OR EXISTS (SELECT 1 FROM job_models m "
                     f"WHERE m.job_id=j.id AND m.name ILIKE ${len(args)}))")
    page = max(int(q.get("page", 1) or 1), 1)
    args += [60, (page - 1) * 60]
    rows = await db.fetch(
        f"""SELECT j.*, u.username, e.name AS env_name, v.vno,
                   (SELECT array_agg(name) FROM job_models m WHERE m.job_id=j.id) AS models,
                   (SELECT json_agg(json_build_object('rel', o.rel_path, 'media', o.media)) FROM job_outputs o
                     WHERE o.job_id=j.id) AS outputs
            FROM jobs j JOIN users u ON u.id=j.user_id LEFT JOIN envs e ON e.id=j.env_id
            LEFT JOIN env_versions v ON v.id=j.env_version_id
            WHERE {' AND '.join(where)} ORDER BY j.seq DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
        *args)
    items = []
    for r in rows:
        outs = []
        for o in r["outputs"] or []:
            path = os.path.join(settings.results_dir, o["rel"])
            outs.append({**o, "exists": os.path.isfile(path), "ext": os.path.splitext(o["rel"])[1].lower()})
        items.append({**dict(r), "outs": outs})
    users = await db.fetch("SELECT username FROM users ORDER BY username")
    return render(request, "history.html", items=items, users=users, q=q, page=page)


@login_required
async def files(request: Request):
    rel = request.path_params["path"]
    base = os.path.realpath(settings.results_dir)
    path = os.path.realpath(os.path.join(base, rel))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return Response("파일 없음 (지워졌거나 옮겨졌습니다)", status_code=404, media_type="text/plain; charset=utf-8")
    return FileResponse(path)


# ── 모델 추가 (\\main\model-inbox) ─────────────────────────

@login_required
async def inbox(request: Request):
    base = os.path.join(settings.models_dir, "inbox")
    if request.method == "POST":
        form = await request.form()
        name, folder = str(form.get("name", "")), str(form.get("folder", ""))
        if folder not in INBOX_FOLDERS:
            return back("/models", "폴더를 선택하세요.")
        src = os.path.realpath(os.path.join(base, name))
        if not src.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(src):
            return back("/models", "파일이 없습니다.")
        if not name.lower().endswith(models_index.MODEL_EXTS):
            return back("/models", "모델 파일 형식이 아닙니다.")
        dst = os.path.join(settings.models_dir, folder, os.path.basename(name))
        if os.path.exists(dst):
            trash = os.path.join(settings.models_dir, ".trash", datetime.date.today().isoformat())
            os.makedirs(trash, exist_ok=True)
            shutil.move(dst, os.path.join(trash, os.path.basename(dst)))
        shutil.move(src, dst)
        os.chown(dst, 0, settings.group_gid)
        os.chmod(dst, 0o644)
        await db.log_access(request.state.user["id"], _ip(request), "model_intake",
                            {"file": os.path.basename(name), "folder": folder})
        return back("/models", f"{os.path.basename(name)} → {folder} 로 옮겼습니다.")
    pending = []
    for root, _d, fs in os.walk(base):
        for fn in fs:
            full = os.path.join(root, fn)
            pending.append({"name": os.path.relpath(full, base), "size_gb": round(os.path.getsize(full) / 1e9, 2)})
    return render(request, "models.html", pending=pending, folders=INBOX_FOLDERS, catalog=models_index.catalog())


# ── 관리자 ─────────────────────────────────────────────────

@admin_required
async def admin(request: Request):
    return render(
        request, "admin.html",
        queue=await jobs.global_queue(), lease=scheduler.lease, worker=scheduler.worker, b0=scheduler.b0,
        paused=await db.get_setting("paused", False), blocked=await db.get_setting("blocked"),
        incidents=await db.fetch("SELECT * FROM incidents ORDER BY id DESC LIMIT 30"),
        users=await db.fetch("SELECT * FROM users ORDER BY username"),
        envs=await db.fetch("SELECT e.*, u.username FROM envs e JOIN users u ON u.id=e.owner_id ORDER BY port"),
        access=await db.fetch("SELECT a.*, u.username FROM access_log a LEFT JOIN users u ON u.id=a.user_id "
                              "ORDER BY a.id DESC LIMIT 50"),
    )


@admin_required
async def admin_action(request: Request):
    action = request.path_params["action"]
    form = await request.form()
    user = request.state.user
    if action == "pause":
        await db.set_setting("paused", True)
    elif action == "resume":
        await db.set_setting("paused", False)
    elif action == "unblock":
        await db.execute("DELETE FROM settings WHERE key='blocked'")
        scheduler.wake.set()
    elif action == "cancel":
        await jobs.cancel_queued(None, [str(form.get("job_id"))])
    await db.log_access(user["id"], _ip(request), "admin_action", {"action": action})
    return back("/admin", "처리했습니다.")


def build_app() -> Starlette:
    return Starlette(routes=[
        Route("/", home),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/select", select, methods=["POST"]),
        Route("/keys", create_key, methods=["POST"]),
        Route("/api/status", status_json),
        Route("/envs", env_create, methods=["POST"]),
        Route("/envs/{env_id:int}/restart", env_restart, methods=["POST"]),
        Route("/envs/{env_id:int}/versions", env_versions),
        Route("/envs/{env_id:int}/rollback", env_rollback, methods=["POST"]),
        Route("/history", history),
        Route("/files/{path:path}", files),
        Route("/models", inbox, methods=["GET", "POST"]),
        Route("/admin", admin),
        Route("/admin/{action}", admin_action, methods=["POST"]),
    ])
