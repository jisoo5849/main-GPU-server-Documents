"""ComfyUI 화면 안 '메인 GPU' 패널이 부르는 API (환경 포트의 /mgs/api/*).

ComfyUI 페이지와 같은 주소(origin)에서 불리므로 로그인 쿠키가 그대로 쓰인다. 작업공간 주인(또는 관리자)만
이 포트를 열 수 있다는 확인은 proxy.handle_http 가 먼저 한다.
  GET  /mgs/api/me        사용자·작업공간·바로가기 주소
  GET  /mgs/api/catalog   모델 목록 (이미지·영상·기타 ComfyUI 모델, LLM, 임베딩) + 로더 노드 정보
  GET  /mgs/api/queue     메인 전체 GPU 대기열과 내 순서
  POST /mgs/api/select    모델 선택 기록 (접속 기록에 portal_select)
  GET  /mgs/api/starter   모델에 맞는 시작 워크플로우 (공식 템플릿)
  POST /mgs/api/keys      API 키 발급 (작업공간 주인만)
  POST /mgs/api/expand    짧은 요청 → 메타 규칙으로 검사한 생성용 프롬프트 (promptx.expand, GPU 대기열 경유)
  POST /mgs/api/inspect   결과 이미지의 해부학 오류·필수 특징 자동 검사 (promptx.inspect, GPU 대기열 경유)
"""
import asyncio
import datetime
import os

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import db, jobs, models_index, promptx, security, templates_index
from .config import settings
from .scheduler import scheduler

# 폴더 → (로더 노드, 모델을 넣는 위젯 이름)
LOADERS = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "unet": ("UNETLoader", "unet_name"),
    "loras": ("LoraLoaderModelOnly", "lora_name"),
    "vae": ("VAELoader", "vae_name"),
    "text_encoders": ("CLIPLoader", "clip_name"),
    "clip": ("CLIPLoader", "clip_name"),
    "clip_vision": ("CLIPVisionLoader", "clip_name"),
    "controlnet": ("ControlNetLoader", "control_net_name"),
    "upscale_models": ("UpscaleModelLoader", "model_name"),
}
# GGUF 파일은 기본 로더 목록에 나오지 않는다 (ComfyUI-GGUF 노드가 설치된 작업공간에서만 쓸 수 있음)
GGUF_LOADERS = {
    "diffusion_models": ("UnetLoaderGGUF", "unet_name"),
    "unet": ("UnetLoaderGGUF", "unet_name"),
    "text_encoders": ("CLIPLoaderGGUF", "clip_name"),
    "clip": ("CLIPLoaderGGUF", "clip_name"),
}
OTHER_FOLDERS = ("loras", "vae", "text_encoders", "clip_vision", "controlnet", "upscale_models")


def _host(request: Request) -> str:
    return (request.headers.get("host") or settings.public_host).split(":")[0]


def portal_url(request: Request, path: str = "/") -> str:
    port = "" if settings.portal_port == 80 else f":{settings.portal_port}"
    return f"http://{_host(request)}{port}{path}"


async def ollama_models() -> tuple[list, list]:
    """Ollama 모델을 LLM 과 임베딩으로 나눈다."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            data = (await c.get(f"{settings.ollama_upstream}/api/tags")).json()
    except (httpx.HTTPError, ValueError):
        return [], []
    llm, embed = [], []
    for m in data.get("models", []):
        details = m.get("details") or {}
        fam = " ".join(details.get("families") or [])
        caps = m.get("capabilities") or []
        item = {"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
                "params": details.get("parameter_size", ""), "quant": details.get("quantization_level", "")}
        is_embed = "embedding" in caps or "embed" in (item["name"] or "") or "bert" in fam
        (embed if is_embed else llm).append(item)
    return llm, embed


def _other_models() -> list[dict]:
    out = []
    for rel, hits in sorted(models_index.index().items()):
        for folder, full in hits:
            if folder not in OTHER_FOLDERS:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            out.append({"name": rel, "folder": folder, "size_gb": round(size / 1e9, 2)})
            break
    return out


def _with_loader(items: list[dict]) -> list[dict]:
    for it in items:
        table = GGUF_LOADERS if it["name"].lower().endswith(".gguf") else LOADERS
        node, widget = table.get(it["folder"], (None, None))
        it["loader"] = {"node": node, "widget": widget} if node else None
    return items


async def me(request: Request, env: dict, user) -> dict:
    rows = await db.fetch("SELECT id, name, port, state FROM envs WHERE owner_id=$1 ORDER BY name", user["id"])
    host = _host(request)
    envs_out = [{"id": e["id"], "name": e["name"], "url": f"http://{host}:{e['port']}/",
                 "running": e["state"] == "running", "current": e["id"] == env["id"]} for e in rows]
    return {
        "user": {"username": user["username"], "display": user["display_name"] or user["username"],
                 "role": user["role"]},
        "env": {"id": env["id"], "name": env["name"], "port": env["port"], "owner": env["username"],
                "mine": env["owner_id"] == user["id"]},
        "envs": envs_out,
        "links": {
            "portal": portal_url(request, "/portal"),
            "login": portal_url(request, "/login"),
            "history": portal_url(request, "/history"),
            "versions": portal_url(request, f"/envs/{env['id']}/versions"),
            "models": portal_url(request, "/models"),
            "admin": portal_url(request, "/admin") if user["role"] == "admin" else None,
            "logout": portal_url(request, "/logout"),
            "webui": f"http://{host}:8090/",
        },
        "llm_url": f"http://{host}:{settings.llm_port}",
        "results_share": f"\\\\{host}\\results",
    }


def _catalog_sync() -> tuple[dict, list]:
    return models_index.catalog(), _other_models()


async def catalog(request: Request, env: dict) -> dict:
    # 모델 폴더를 훑는 작업은 이벤트 루프를 막지 않게 스레드에서
    (cat, other), (llm, embed), starters = await asyncio.gather(
        asyncio.to_thread(_catalog_sync), ollama_models(), templates_index.models_with_templates(env))
    image = _with_loader(cat["image"])
    video = _with_loader(cat["video"])
    for it in image + video:
        key = (templates_index.canon_folder(it["folder"]), os.path.basename(it["name"]))
        # 공식 템플릿이 있으면 그것으로, 없으면 체크포인트만 기본 txt2img 그래프를 화면에서 만든다
        it["has_starter"] = key in starters
        it["basic_graph"] = (not it["has_starter"]) and it["folder"] == "checkpoints" \
            and not it["name"].lower().endswith(".gguf")
    return {"image": image, "video": video, "other": _with_loader(other), "llm": llm, "embed": embed}


def _iso(ts):
    return ts.isoformat() if ts else None


async def queue(user) -> dict:
    rows = await jobs.global_queue()
    now = datetime.datetime.now(datetime.timezone.utc)
    items, mine, waiting_pos = [], [], 0
    for r in rows:
        running = r["state"] != "queued"
        if not running:
            waiting_pos += 1
        item = {
            "seq": r["seq"], "user": r["username"], "kind": r["kind"], "env": r["env_name"], "model": r["model"],
            "running": running,
            # 대기 중인 작업들 사이의 순서 (실행 중인 작업은 세지 않는다)
            "position": None if running else waiting_pos,
            "submitted_at": _iso(r["submitted_at"]),
            "elapsed_s": int((now - (r["started_at"] or r["submitted_at"])).total_seconds()),
        }
        items.append(item)
        if r["username"] == user["username"]:
            mine.append(item)
    lease = scheduler.lease
    return {
        "queue": items,
        "mine": mine,
        "running": next((it for it in items if it["running"]), None),
        "lease_kind": lease["kind"] if lease else None,
        "paused": bool(await db.get_setting("paused", False)),
        "blocked": bool(await db.get_setting("blocked")),
    }


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def handle(request: Request, env: dict, user, path: str):
    ip = request.client.host if request.client else None
    method = request.method
    if path == "/mgs/api/me" and method == "GET":
        return JSONResponse(await me(request, env, user))
    if path == "/mgs/api/catalog" and method == "GET":
        return JSONResponse(await catalog(request, env))
    if path == "/mgs/api/queue" and method == "GET":
        return JSONResponse(await queue(user))
    if path == "/mgs/api/select" and method == "POST":
        data = await _body(request)
        await db.log_access(user["id"], ip, "portal_select",
                            {"kind": data.get("kind"), "model": data.get("model"), "action": data.get("action"),
                             "env": env["slug"], "via": "comfyui_panel"})
        return JSONResponse({"ok": True})
    if path == "/mgs/api/starter" and method == "GET":
        q = request.query_params
        result = await templates_index.starter_for(env, q.get("model", ""), q.get("folder", ""), q.get("template"))
        if result is None:
            return JSONResponse({"error": "이 모델에 맞는 공식 템플릿이 없습니다. '로더 추가'로 노드만 넣어 쓰세요."},
                                status_code=404)
        return JSONResponse(result)
    if path == "/mgs/api/keys" and method == "POST":
        # 이 화면에는 사용자가 설치한 노드의 스크립트도 함께 돈다. 관리자가 남의 작업공간을 열었을 때
        # 그 스크립트가 관리자 키를 받아 가지 못하게, 키는 작업공간 주인에게만 발급한다.
        if env["owner_id"] != user["id"]:
            return JSONResponse({"error": "API 키는 자기 작업공간에서만 발급할 수 있습니다."}, status_code=403)
        data = await _body(request)
        key = await security.create_api_key(user["id"], str(data.get("label", "ComfyUI 패널"))[:60])
        await db.log_access(user["id"], ip, "api_key_create", {"via": "comfyui_panel"})
        return JSONResponse({"key": key})
    if path == "/mgs/api/expand" and method == "POST":
        data = await _body(request)
        try:
            crew = data.get("crew")   # 제작진 모드: true/false 면 이 요청만 그 값, 없으면 설정(CREW_MODE)
            result = await promptx.expand(user, ip, data.get("text"), data.get("style", ""), data.get("notes", ""),
                                          crew if isinstance(crew, bool) else None)
        except promptx.PromptxError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        await db.log_access(user["id"], ip, "prompt_expand",
                            {"env": env["slug"], "rounds": result["rounds"], "left": len(result["violations"]),
                             "crew": (result.get("crew") or {}).get("category"),
                             "crew_used": bool((result.get("crew") or {}).get("used"))})
        return JSONResponse(result)
    if path == "/mgs/api/inspect" and method == "POST":
        data = await _body(request)
        if str(data.get("type") or "output") != "output":
            return JSONResponse({"error": "결과 폴더의 이미지만 검사합니다."}, status_code=400)
        try:
            # 결과 폴더는 작업공간 주인 기준 (proxy._view 와 같은 위치)
            result = await promptx.inspect(user, ip, env["username"], str(data.get("filename") or ""),
                                           str(data.get("subfolder") or ""), data.get("expect"))
        except promptx.PromptxError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        await db.log_access(user["id"], ip, "image_inspect",
                            {"env": env["slug"], "file": result["file"], "ok": result["ok"]})
        return JSONResponse(result)
    return JSONResponse({"error": "not found"}, status_code=404)
