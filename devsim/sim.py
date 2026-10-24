"""노트북용 시험 허브 (devsim).

메인의 허브와 같은 흐름을 Docker·btrfs·PostgreSQL 없이 이 PC에서 돌려 본다.
  - 작업공간 2개(alice: 8101, bob: 8102)가 이 PC의 ComfyUI 하나를 함께 쓴다 (편집 공간 2개를 흉내)
  - 실행 버튼 → SQLite 대기열(접수 순번) → 스케줄러가 한 번에 한 건씩 ComfyUI 에 넘김
  - 진행률·미리보기·결과는 실행을 누른 브라우저 탭으로만 전달
  - 작업이 끝나면 /free 로 모델을 내리고 nvidia-smi 로 VRAM 이 기준으로 돌아왔는지 확인
  - LLM 중계(11436): 생성·채팅·임베딩은 같은 대기열, 목록 조회는 바로 응답
  - 포털(8080): 로그인, 전체 대기열, 작업 이력, 접속 기록

메인과 다른 점 (시험용 단순화)
  - 환경 고정은 custom_nodes 폴더 목록의 해시로만 기록한다 (스냅샷 없음)
  - 결과 파일은 ComfyUI 의 output 폴더에 그대로 남고, /view 는 ComfyUI 로 넘긴다
  - 워크플로우 안의 Ollama 호출 구분: ComfyUI 작업 실행 중 들어온 LLM 요청은 내부 호출로 본다

실행:  devsim\run.cmd   (또는 python sim.py)   → http://127.0.0.1:8080/
"""
import asyncio
import datetime
import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hub"))
from app import promptops  # noqa: E402  (허브와 같은 순수 함수 재사용)

log = logging.getLogger("devsim")

# ── 설정 ──────────────────────────────────────────────────
COMFY_URL = os.environ.get("COMFY_URL", "")            # 비우면 8000(Desktop) → 8188 순서로 찾는다
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
CUSTOM_NODES_DIR = os.environ.get("CUSTOM_NODES_DIR", "")
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", 8080))
LLM_PORT = int(os.environ.get("LLM_PORT", 11436))
VRAM_MARGIN_MB = int(os.environ.get("VRAM_MARGIN_MB", 1500))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim.sqlite")
USERS = {"alice": {"password": "test1234", "env_id": 1, "port": 8101, "display": "앨리스"},
         "bob": {"password": "test1234", "env_id": 2, "port": 8102, "display": "밥"}}
PORT_ENV = {u["port"]: (name, u["env_id"]) for name, u in USERS.items()}
COOKIE = "sim_sess"
ACTIVE = ("queued", "running", "cleaning")

# ── DB (SQLite) ────────────────────────────────────────────
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_dblock = threading.Lock()
_db.executescript("""
CREATE TABLE IF NOT EXISTS access_log(id INTEGER PRIMARY KEY, ts TEXT, user TEXT, ip TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, seq INTEGER UNIQUE, kind TEXT, user TEXT, env_id INTEGER,
  client_id TEXT, model TEXT, env_hash TEXT, state TEXT, outcome TEXT, prompt TEXT, submitted_at REAL,
  started_at REAL, ended_at REAL, finished_at REAL, vram_before INTEGER, vram_peak INTEGER, vram_after INTEGER,
  cleanup_ms INTEGER, cleanup_method TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS job_models(job_id TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS llm_calls(id INTEGER PRIMARY KEY, ts TEXT, job_id TEXT, user TEXT, endpoint TEXT,
  model TEXT, nested INTEGER, status INTEGER, duration_ms INTEGER);
""")


def q(sql, *args):
    with _dblock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur


def rows(sql, *args):
    with _dblock:
        return [dict(r) for r in _db.execute(sql, args).fetchall()]


def row(sql, *args):
    r = rows(sql, *args)
    return r[0] if r else None


def log_access(user, ip, event, detail=None):
    q("INSERT INTO access_log(ts,user,ip,event,detail) VALUES (?,?,?,?,?)",
      datetime.datetime.now().isoformat(timespec="seconds"), user, ip, event, json.dumps(detail, ensure_ascii=False) if detail else None)


# ── 공용 상태 ──────────────────────────────────────────────
sessions: dict[str, str] = {}                               # sid → user
browser_socks: dict[tuple[int, str], set] = {}              # (env_id, client_id) → WebSocket 들
lease: dict | None = None                                    # 지금 GPU 를 쓰는 작업
lease_open = False
llm_waiters: dict[str, dict] = {}
wake = asyncio.Event()
vram_baseline: int | None = None
http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5))


def vram_used_mb() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return int(out[0]) if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def env_hash() -> str:
    """환경 고정 흉내: custom_nodes 폴더 이름 목록의 해시."""
    if not CUSTOM_NODES_DIR or not os.path.isdir(CUSTOM_NODES_DIR):
        return "-"
    names = sorted(n for n in os.listdir(CUSTOM_NODES_DIR) if not n.startswith((".", "__")))
    return hashlib.sha1("\n".join(names).encode()).hexdigest()[:10]


async def detect_comfy() -> str:
    global COMFY_URL
    cands = [COMFY_URL] if COMFY_URL else ["http://127.0.0.1:8000", "http://127.0.0.1:8188"]
    for c in cands:
        try:
            r = await http.get(f"{c}/system_stats", timeout=3)
            if r.status_code == 200:
                COMFY_URL = c
                return c
        except httpx.HTTPError:
            pass
    return ""


def ws_url(path: str) -> str:
    return COMFY_URL.replace("http://", "ws://").replace("https://", "wss://") + path


async def send_to_tab(env_id, client_id, text=None, data=None):
    for ws in list(browser_socks.get((env_id, client_id), ())):
        try:
            if text is not None:
                await ws.send_text(text)
            else:
                await ws.send_bytes(data)
        except Exception:  # noqa: BLE001
            browser_socks[(env_id, client_id)].discard(ws)


async def broadcast_status():
    n = row("SELECT count(*) AS n FROM jobs WHERE state IN ('queued','running','cleaning')")["n"]
    text = json.dumps({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": n}}}})
    for socks in list(browser_socks.values()):
        for ws in list(socks):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                socks.discard(ws)


# ── 접수 ──────────────────────────────────────────────────
def insert_job(job_id, kind, user, env_id=None, client_id=None, model=None, prompt=None, env_h=None) -> int:
    with _dblock:
        seq = (_db.execute("SELECT coalesce(max(seq),0)+1 AS s FROM jobs").fetchone()["s"])
        _db.execute("INSERT INTO jobs(id,seq,kind,user,env_id,client_id,model,env_hash,state,prompt,submitted_at) "
                    "VALUES (?,?,?,?,?,?,?,?,'queued',?,?)",
                    (job_id, seq, kind, user, env_id, client_id, model, env_h, json.dumps(prompt) if prompt else None,
                     time.time()))
        _db.commit()
    wake.set()
    return seq


async def submit_comfy(user, env_id, body, ip):
    prompt = body.get("prompt")
    if not isinstance(prompt, dict) or not prompt:
        return JSONResponse({"error": {"type": "no_prompt", "message": "No prompt provided", "details": "",
                                       "extra_info": {}}, "node_errors": {}}, status_code=400)
    job_id = str(uuid.uuid4())
    # 저장 위치: <사용자>/<날짜>/...  (메인에서는 /srv/results/<사용자>/<환경>/<날짜>)
    promptops.rewrite_filename_prefix(prompt, user, datetime.date.today().isoformat())
    extra, _secrets = promptops.split_sensitive(body.get("extra_data") or {})
    seq = insert_job(job_id, "comfy", user, env_id, body.get("client_id"), None,
                     {"prompt": prompt, "extra_data": extra, "partial": body.get("partial_execution_targets")},
                     env_hash())
    for m in promptops.find_models(prompt):
        q("INSERT INTO job_models(job_id,name) VALUES (?,?)", job_id, m["name"])
    log_access(user, ip, "submit", {"job": job_id, "seq": seq})
    await broadcast_status()
    return JSONResponse({"prompt_id": job_id, "number": seq, "node_errors": {}})


# ── 스케줄러 ──────────────────────────────────────────────
async def scheduler():
    global lease, lease_open, vram_baseline
    vram_baseline = vram_used_mb()
    log.info("VRAM 유휴 기준: %s MB", vram_baseline)
    while True:
        try:
            job = row("SELECT * FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1")
            if job is None:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                continue
            lease, lease_open = job, job["kind"] == "comfy"
            q("UPDATE jobs SET state='running', started_at=?, vram_before=? WHERE id=?", time.time(), vram_used_mb(), job["id"])
            await broadcast_status()
            peak = [vram_used_mb() or 0]
            try:
                if job["kind"] == "comfy":
                    outcome, error = await run_comfy(job, peak)
                else:
                    outcome, error = await run_llm(job, peak)
            except Exception as e:  # noqa: BLE001
                log.exception("job failed")
                outcome, error = "error", str(e)
            q("UPDATE jobs SET state='cleaning', ended_at=? WHERE id=?", time.time(), job["id"])
            lease_open = False
            method, ms = await cleanup(job)
            q("UPDATE jobs SET state='finished', outcome=?, error=?, finished_at=?, vram_peak=?, vram_after=?, "
              "cleanup_ms=?, cleanup_method=? WHERE id=?",
              outcome, error, time.time(), peak[0], vram_used_mb(), ms, method, job["id"])
            lease = None
            await broadcast_status()
        except Exception:  # noqa: BLE001
            log.exception("scheduler error")
            await asyncio.sleep(3)


async def run_comfy(job, peak):
    data = json.loads(job["prompt"])
    env_id, client_id, job_id = job["env_id"], job["client_id"], job["id"]
    ws = await websockets.connect(ws_url(f"/ws?clientId=gw-{job_id}"), max_size=None)
    try:
        body = {"prompt": data["prompt"], "prompt_id": job_id, "client_id": f"gw-{job_id}",
                "extra_data": data["extra_data"]}
        if data.get("partial"):
            body["partial_execution_targets"] = data["partial"]
        r = await http.post(f"{COMFY_URL}/prompt", json=body)
        if r.status_code != 200:
            err = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text}
            await send_to_tab(env_id, client_id, text=json.dumps({"type": "execution_error", "data": {
                "prompt_id": job_id, "node_id": "", "node_type": "", "executed": [],
                "exception_message": json.dumps(err.get("error"), ensure_ascii=False)[:500], "exception_type": "validation",
                "traceback": [], "current_inputs": {}, "current_outputs": {}, "timestamp": int(time.time() * 1000)}}))
            return "error", json.dumps(err, ensure_ascii=False)[:1000]
        outcome, error = None, None
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                peak[0] = max(peak[0], vram_used_mb() or 0)
                r = await http.get(f"{COMFY_URL}/api/jobs/{job_id}", timeout=5)
                st = r.json().get("status") if r.status_code == 200 else ""
                if st in ("completed", "failed", "cancelled"):
                    return {"completed": "success", "failed": "error", "cancelled": "cancelled"}[st], error
                continue
            if isinstance(msg, bytes):
                await send_to_tab(env_id, client_id, data=msg)
                continue
            d = json.loads(msg)
            typ, dd = d.get("type"), d.get("data") or {}
            if typ in ("status", "feature_flags"):
                continue
            await send_to_tab(env_id, client_id, text=msg)
            if dd.get("prompt_id") != job_id:
                continue
            if typ == "progress":
                peak[0] = max(peak[0], vram_used_mb() or 0)
            if typ == "execution_error":
                return "error", dd.get("exception_message")
            if typ == "execution_interrupted":
                return "cancelled", None
            if typ == "execution_success" or (typ == "executing" and dd.get("node") is None):
                return "success", None
    finally:
        await ws.close()


async def run_llm(job, peak):
    w = llm_waiters.get(job["id"])
    if w is None:
        return "cancelled", "요청한 쪽이 연결을 끊었습니다."
    w["go"].set()
    deadline = time.monotonic() + 3600
    try:
        while not w["done"].is_set():
            if time.monotonic() > deadline:
                return "timeout", None
            try:
                await asyncio.wait_for(w["done"].wait(), timeout=0.5)
            except asyncio.TimeoutError:
                peak[0] = max(peak[0], vram_used_mb() or 0)
    finally:
        llm_waiters.pop(job["id"], None)
    return ("success" if w.get("status", 500) < 400 else "error"), None


async def unload_ollama():
    try:
        ps = (await http.get(f"{OLLAMA_URL}/api/ps", timeout=5)).json().get("models") or []
        for m in ps:
            await http.post(f"{OLLAMA_URL}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=30)
    except (httpx.HTTPError, ValueError):
        pass


async def cleanup(job):
    """메모리 해제: /free → 기준치 확인. 메인에서는 실행 공간 종료까지 가지만 여기서는 /free 두 단계만."""
    t0 = time.monotonic()
    await unload_ollama()
    base = (vram_baseline or 0) + VRAM_MARGIN_MB
    for method, body in (("free", {"unload_models": True}), ("free_memory", {"unload_models": True, "free_memory": True})):
        if job["kind"] == "comfy":
            try:
                await http.post(f"{COMFY_URL}/free", json=body, timeout=10)
            except httpx.HTTPError:
                pass
        for _ in range(40):
            used = vram_used_mb()
            if used is None or used <= base:
                return method, int((time.monotonic() - t0) * 1000)
            await asyncio.sleep(0.5)
    log.warning("VRAM 이 기준(%s MB)으로 돌아오지 않음: %s MB", base, vram_used_mb())
    return "not_released", int((time.monotonic() - t0) * 1000)


# ── 공통: 로그인 ───────────────────────────────────────────
def current_user(conn) -> str | None:
    return sessions.get(conn.cookies.get(COOKIE, ""))


def client_ip(req) -> str:
    return req.client.host if req.client else ""


# ── 포털 (8080) ───────────────────────────────────────────
STYLE = """<style>body{font:14px/1.6 'Malgun Gothic',system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1b2230}
main{max-width:1100px;margin:0 auto;padding:20px 16px}table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border-bottom:1px solid #dde1e8;padding:6px 10px;text-align:left;vertical-align:top}th{color:#5a6475;background:#eef0f4}
.card{background:#fff;border:1px solid #dde1e8;border-radius:8px;padding:12px 16px;margin-bottom:14px}
a.btn,button{background:#1f5fae;color:#fff;border:0;border-radius:6px;padding:6px 12px;text-decoration:none;cursor:pointer;font:inherit}
.ok{color:#1e7a4c}.bad{color:#b23a3a}.muted{color:#5a6475}code{background:#e6eef9;padding:1px 5px;border-radius:4px}
nav a{margin-right:14px}</style>"""


def page(title, body, user=None):
    nav = (f"<nav><b>main GPU (시험)</b> &nbsp; <a href='/'>대기열</a><a href='/history'>작업 이력</a>"
           f"<a href='/access'>접속 기록</a><span style='float:right'>{user} <a href='/logout'>로그아웃</a></span></nav><hr>"
           if user else "")
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{title}</title>{STYLE}<main>{nav}{body}</main>")


async def login(request: Request):
    if request.method == "GET":
        return page("로그인", """<div class=card style='max-width:360px;margin:60px auto'><h2>시험 허브 로그인</h2>
<form method=post><p>아이디 <input name=username></p><p>비밀번호 <input name=password type=password></p><button>로그인</button></form>
<p class=muted>alice / bob, 비밀번호 test1234</p></div>""")
    form = await request.form()
    u, p = str(form.get("username", "")), str(form.get("password", ""))
    if u not in USERS or USERS[u]["password"] != p:
        log_access(u, client_ip(request), "login_fail")
        return page("로그인", "<p class=bad>아이디 또는 비밀번호가 맞지 않습니다.</p><a href='/login'>다시</a>")
    sid = secrets.token_urlsafe(24)
    sessions[sid] = u
    log_access(u, client_ip(request), "login_ok")
    resp = RedirectResponse("/", 303)
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax")
    return resp


async def logout(request: Request):
    u = current_user(request)
    sessions.pop(request.cookies.get(COOKIE, ""), None)
    if u:
        log_access(u, client_ip(request), "logout")
    r = RedirectResponse("/login", 303)
    r.delete_cookie(COOKIE)
    return r


def fmt(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"


async def home(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", 302)
    me = USERS[u]
    host = request.headers.get("host", "127.0.0.1").split(":")[0]
    qrows = rows("SELECT * FROM jobs WHERE state IN ('queued','running','cleaning') ORDER BY seq")
    trs = "".join(f"<tr><td>{j['seq']}</td><td>{j['user']}</td><td>{j['kind']} {j['model'] or ''}</td>"
                  f"<td>{'<b class=ok>GPU 실행 중</b>' if j['state']!='queued' else '대기'}</td><td>{fmt(j['submitted_at'])}</td></tr>"
                  for j in qrows) or "<tr><td colspan=5 class=muted>비어 있음</td></tr>"
    body = f"""<div class=card><b>내 ComfyUI:</b> <a class=btn href='http://{host}:{me['port']}/' target=_blank>http://{host}:{me['port']}/</a>
&nbsp; <span class=muted>ComfyUI: {COMFY_URL or '연결 안 됨'} · Ollama: {OLLAMA_URL} · VRAM 유휴 기준 {vram_baseline} MB · 지금 {vram_used_mb()} MB · 환경 해시 {env_hash()}</span></div>
<div class=card><b>LLM API (대기열 적용):</b> <code>http://{host}:{LLM_PORT}/v1/chat/completions</code> &nbsp;
예: <code>curl http://{host}:{LLM_PORT}/api/generate -d "{{\\"model\\":\\"llama3.2\\",\\"prompt\\":\\"hi\\"}}"</code> (로그인 쿠키 또는 <code>Authorization: Bearer alice</code>)</div>
<h3>GPU 대기열 (전체)</h3><table><tr><th>순번</th><th>사용자</th><th>종류</th><th>상태</th><th>접수</th></tr>{trs}</table>
<p class=muted>이 페이지는 5초마다 새로고침됩니다.</p><script>setTimeout(()=>location.reload(),5000)</script>"""
    return page("대기열", body, u)


async def history(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", 302)
    js = rows("SELECT * FROM jobs WHERE state='finished' ORDER BY seq DESC LIMIT 100")
    trs = ""
    for j in js:
        models = ", ".join(m["name"] for m in rows("SELECT name FROM job_models WHERE job_id=?", j["id"]))
        wait = (j["started_at"] or 0) - (j["submitted_at"] or 0)
        run = (j["ended_at"] or 0) - (j["started_at"] or 0)
        cls = "ok" if j["outcome"] == "success" else "bad"
        trs += (f"<tr><td>{j['seq']}</td><td>{j['user']}</td><td>{j['kind']} {j['model'] or ''}</td><td class={cls}>{j['outcome']}</td>"
                f"<td>{models}</td><td>{j['env_hash']}</td><td>{wait:.1f}s / {run:.1f}s / {(j['cleanup_ms'] or 0)/1000:.1f}s</td>"
                f"<td>{j['vram_before']} → {j['vram_peak']} → {j['vram_after']} ({j['cleanup_method']})</td>"
                f"<td class=muted>{(j['error'] or '')[:120]}</td></tr>")
    body = f"<h3>작업 이력 (모든 사용자)</h3><table><tr><th>순번</th><th>사용자</th><th>종류</th><th>결과</th><th>모델</th><th>환경</th><th>대기/실행/정리</th><th>VRAM MB</th><th>오류</th></tr>{trs}</table>"
    return page("작업 이력", body, u)


async def access(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", 302)
    trs = "".join(f"<tr><td>{a['ts']}</td><td>{a['user'] or ''}</td><td>{a['ip'] or ''}</td><td>{a['event']}</td><td class=muted>{a['detail'] or ''}</td></tr>"
                  for a in rows("SELECT * FROM access_log ORDER BY id DESC LIMIT 100"))
    return page("접속 기록", f"<h3>접속 기록</h3><table><tr><th>시각</th><th>사용자</th><th>IP</th><th>내용</th><th>상세</th></tr>{trs}</table>", u)


portal_app = Starlette(routes=[Route("/", home), Route("/login", login, methods=["GET", "POST"]),
                               Route("/logout", logout), Route("/history", history), Route("/access", access)])


# ── 작업공간 포트 (8101, 8102): ComfyUI 화면 중계 ───────────
HOP = {"connection", "keep-alive", "transfer-encoding", "host", "content-length", "cookie"}


async def env_http(request: Request):
    port = request.scope["server"][1]
    owner, env_id = PORT_ENV[port]
    u = current_user(request)
    if not u:
        if "text/html" in request.headers.get("accept", ""):
            host = request.headers.get("host", "127.0.0.1").split(":")[0]
            return RedirectResponse(f"http://{host}:{PORTAL_PORT}/login", 302)
        return JSONResponse({"error": "login"}, status_code=401)
    if u != owner:
        return Response("다른 사용자의 작업공간입니다.", status_code=403, media_type="text/plain; charset=utf-8")
    if not COMFY_URL:
        return Response("ComfyUI 가 실행 중이 아닙니다. ComfyUI Desktop 을 켜고 새로고침하세요.", status_code=503,
                        media_type="text/plain; charset=utf-8")
    raw = request.scope.get("raw_path", b"").decode("latin-1") or request.url.path
    path = urllib.parse.unquote(raw)
    path = path[4:] if path.startswith("/api/") else path
    m = request.method
    ip = client_ip(request)

    if m == "POST" and path == "/prompt":
        return await submit_comfy(u, env_id, await request.json(), ip)
    if path == "/queue":
        if m == "GET":
            js = rows("SELECT * FROM jobs WHERE env_id=? AND state IN ('queued','running','cleaning') ORDER BY seq", env_id)
            tup = lambda j: [j["seq"], j["id"], {}, {"create_time": int(j["submitted_at"] * 1000)}, []]  # noqa: E731
            return JSONResponse({"queue_running": [tup(j) for j in js if j["state"] != "queued"],
                                 "queue_pending": [tup(j) for j in js if j["state"] == "queued"]})
        data = await request.json()
        ids = [str(x) for x in data.get("delete", [])]
        if data.get("clear"):
            q("UPDATE jobs SET state='finished', outcome='cancelled', finished_at=? WHERE env_id=? AND state='queued'", time.time(), env_id)
        for jid in ids:
            q("UPDATE jobs SET state='finished', outcome='cancelled', finished_at=? WHERE id=? AND env_id=? AND state='queued'", time.time(), jid, env_id)
        await broadcast_status()
        return Response(status_code=200)
    if m == "POST" and path == "/interrupt":
        data = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        if lease and lease["kind"] == "comfy" and lease["env_id"] == env_id:
            await http.post(f"{COMFY_URL}/interrupt", json={"prompt_id": lease["id"]})
        return Response(status_code=200)
    if m == "POST" and path == "/free":
        return Response(status_code=200)
    if m == "POST" and path.startswith("/jobs/") and path.endswith("/cancel"):
        parts = path.split("/")
        ids = [parts[2]] if len(parts) == 4 else [str(x) for x in (await request.json()).get("job_ids", [])]
        n = 0
        for jid in ids:
            n += q("UPDATE jobs SET state='finished', outcome='cancelled', finished_at=? WHERE id=? AND env_id=? AND state='queued'",
                   time.time(), jid, env_id).rowcount
            if lease and lease["id"] == jid and lease["env_id"] == env_id:
                await http.post(f"{COMFY_URL}/interrupt", json={"prompt_id": jid})
                n += 1
        await broadcast_status()
        return JSONResponse({"cancelled": n > 0})
    if m == "GET" and path == "/jobs":
        # ComfyUI 의 기록 + 아직 ComfyUI 에 넘기지 않은 대기 작업
        r = await http.get(f"{COMFY_URL}/api/jobs", params=request.query_params)
        data = r.json() if r.status_code == 200 else {"jobs": [], "pagination": {}}
        pending = [{"id": j["id"], "status": "pending", "priority": j["seq"], "create_time": int(j["submitted_at"] * 1000),
                    "outputs_count": 0, "previewable_outputs_count": 0}
                   for j in rows("SELECT * FROM jobs WHERE env_id=? AND state='queued' ORDER BY seq DESC", env_id)]
        data["jobs"] = pending + [j for j in data["jobs"] if j.get("status") != "pending"]
        return JSONResponse(data)

    # 나머지(화면, 노드 정보, 모델 목록, /view, /history, 저장 등)는 ComfyUI 로 그대로
    qs = request.scope.get("query_string", b"").decode("latin-1")
    url = f"{COMFY_URL}{raw}" + (f"?{qs}" if qs else "")
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP]
    req = http.build_request(m, url, headers=headers, content=await request.body())
    try:
        resp = await http.send(req, stream=True)
    except httpx.HTTPError as e:
        return Response(f"ComfyUI 연결 실패: {e}", status_code=502, media_type="text/plain; charset=utf-8")

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
    out = StreamingResponse(body(), status_code=resp.status_code)
    out.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in resp.headers.multi_items()
                       if k.lower() not in HOP | {"content-encoding"}]
    return out


async def env_ws(websocket: WebSocket):
    port = websocket.scope["server"][1]
    owner, env_id = PORT_ENV[port]
    if current_user(websocket) != owner or not COMFY_URL:
        await websocket.close(code=4401)
        return
    client_id = websocket.query_params.get("clientId") or uuid.uuid4().hex
    upstream = await websockets.connect(ws_url(f"/ws?clientId={client_id}"), max_size=None)
    await websocket.accept()
    browser_socks.setdefault((env_id, client_id), set()).add(websocket)

    async def to_comfy():
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                await upstream.send(msg["text"])
            elif msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])

    async def to_browser():
        async for msg in upstream:
            if isinstance(msg, str):
                d = json.loads(msg)
                if d.get("type") == "status":
                    n = row("SELECT count(*) AS n FROM jobs WHERE state IN ('queued','running','cleaning')")["n"]
                    d["data"].setdefault("status", {}).setdefault("exec_info", {})["queue_remaining"] = n
                    msg = json.dumps(d)
                # 실행 중인 다른 사용자의 진행 메시지는 ComfyUI 가 gw-<job> 에게만 보내므로 여기로 오지 않는다
                await websocket.send_text(msg)
            else:
                await websocket.send_bytes(msg)

    tasks = [asyncio.create_task(to_comfy()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        browser_socks.get((env_id, client_id), set()).discard(websocket)
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass


env_app = Starlette(routes=[WebSocketRoute("/ws", env_ws), WebSocketRoute("/api/ws", env_ws),
                            Route("/{path:path}", env_http, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])])


# ── LLM 중계 (11436 → Ollama) ─────────────────────────────
QUEUED = {"/api/generate", "/api/chat", "/api/embed", "/api/embeddings", "/v1/chat/completions", "/v1/completions",
          "/v1/embeddings"}


async def llm_handle(request: Request):
    global lease_open
    path = "/" + request.path_params["path"]
    body = await request.body()
    try:
        model = json.loads(body).get("model") if body else None
    except ValueError:
        model = None
    user = current_user(request)
    auth = request.headers.get("authorization", "")
    if not user and auth.lower().startswith("bearer "):
        user = auth[7:].strip() if auth[7:].strip() in USERS else None

    async def forward(on_done):
        t0 = time.monotonic()
        up = http.build_request(request.method, f"{OLLAMA_URL}{path}", params=request.query_params, content=body,
                                headers={"content-type": request.headers.get("content-type", "application/json")})
        resp = await http.send(up, stream=True)

        async def stream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await on_done(resp.status_code, int((time.monotonic() - t0) * 1000))
        return StreamingResponse(stream(), status_code=resp.status_code,
                                 headers={k: v for k, v in resp.headers.items() if k.lower() not in HOP | {"content-encoding"}})

    def record(job_id, nested):
        async def done(status, ms):
            q("INSERT INTO llm_calls(ts,job_id,user,endpoint,model,nested,status,duration_ms) VALUES (?,?,?,?,?,?,?,?)",
              datetime.datetime.now().isoformat(timespec="seconds"), job_id, user, path, model, int(nested), status, ms)
        return done

    if path not in QUEUED:
        return await forward(record(None, False))
    # 워크플로우 안의 Ollama 노드: ComfyUI 작업이 GPU 를 쓰는 동안 들어온 호출은 내부 호출로 보고 바로 처리
    if lease and lease["kind"] == "comfy" and lease_open:
        return await forward(record(lease["id"], True))
    if not user:
        return JSONResponse({"error": "로그인 쿠키 또는 Authorization: Bearer <사용자> 가 필요합니다."}, status_code=401)
    job_id = str(uuid.uuid4())
    w = {"go": asyncio.Event(), "done": asyncio.Event(), "status": 500}
    llm_waiters[job_id] = w
    insert_job(job_id, "embed" if "embed" in path else "llm", user, model=model)
    log_access(user, client_ip(request), "llm_request", {"endpoint": path, "model": model, "job": job_id})
    await broadcast_status()
    while not w["go"].is_set():
        try:
            await asyncio.wait_for(w["go"].wait(), timeout=1)
        except asyncio.TimeoutError:
            if await request.is_disconnected():
                llm_waiters.pop(job_id, None)
                q("UPDATE jobs SET state='finished', outcome='cancelled', finished_at=? WHERE id=? AND state='queued'", time.time(), job_id)
                return Response(status_code=499)

    async def done(status, ms):
        w["status"] = status
        await record(job_id, False)(status, ms)
        w["done"].set()
    return await forward(done)


llm_app = Starlette(routes=[Route("/{path:path}", llm_handle, methods=["GET", "POST", "DELETE"])])


# ── 실행 ──────────────────────────────────────────────────
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                found = await detect_comfy()
                log.info("ComfyUI: %s", found or "없음 (ComfyUI Desktop 을 켜면 자동으로 연결됩니다)")
                asyncio.create_task(scheduler())
                asyncio.create_task(_redetect())
                await send({"type": "lifespan.startup.complete"})
            elif m["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    port = scope["server"][1]
    if port == PORTAL_PORT:
        await portal_app(scope, receive, send)
    elif port == LLM_PORT:
        await llm_app(scope, receive, send)
    else:
        await env_app(scope, receive, send)


async def _redetect():
    while True:
        await asyncio.sleep(10)
        if not COMFY_URL:
            await detect_comfy()


def bind(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(64)
    s.set_inheritable(True)
    return s


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ports = [PORTAL_PORT, LLM_PORT] + [u["port"] for u in USERS.values()]
    print(f"\n  포털       http://127.0.0.1:{PORTAL_PORT}/   (alice / bob, 비밀번호 test1234)")
    print(f"  작업공간   alice → :8101   bob → :8102")
    print(f"  LLM API    http://127.0.0.1:{LLM_PORT}/v1   (Ollama {OLLAMA_URL} 로 중계)\n")
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", ws="websockets", log_level="warning",
                                           ws_max_size=256 * 1024 * 1024))
    asyncio.run(server.serve(sockets=[bind(p) for p in ports]))
