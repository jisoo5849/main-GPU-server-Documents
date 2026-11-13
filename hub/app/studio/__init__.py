"""채팅 스튜디오 (docs/chat-studio-design.md). 허브 안의 감독이 기존 GPU 대기열로 ComfyUI 와 AI 부품을 돌린다.

- prepare(): 허브 시작 때 스케줄러보다 먼저. 감독을 만든다 (실패해도 허브 시작을 막지 않는다)
- start(): 스케줄러를 시작한 뒤. 레시피·모델·작업공간을 검사하고 돌던 요청을 복구한다. 실패하면 /studio 는 503 안내다 (5.10)
- routes(): 포털 앱에 더할 경로
"""
import asyncio
import hashlib
import logging
import os

from ..config import settings

log = logging.getLogger("mgs.studio")


def routes() -> list:
    from starlette.routing import Route, WebSocketRoute

    from . import api
    return [
        Route("/studio", api.page),
        Route("/studio/static/{name}", api.static),
        Route("/studio/logout", api.logout, methods=["POST"]),
        Route("/studio/api/me", api.me),
        Route("/studio/api/threads", api.threads, methods=["GET", "POST"]),
        Route("/studio/api/threads/{id}", api.thread_item, methods=["GET", "DELETE"]),
        Route("/studio/api/threads/{id}/messages", api.messages, methods=["GET", "POST"]),
        Route("/studio/api/uploads", api.uploads, methods=["POST"]),
        Route("/studio/api/files/{id}", api.file_get),
        Route("/studio/api/turns/{id}", api.turn_get),
        Route("/studio/api/turns/{id}/cancel", api.turn_cancel, methods=["POST"]),
        Route("/studio/api/turns/{id}/retry", api.turn_retry, methods=["POST"]),
        Route("/studio/api/turns/{id}/to_video", api.turn_to_video, methods=["POST"]),
        Route("/studio/api/queue", api.queue_counts),
        WebSocketRoute("/studio/ws", api.ws_endpoint),
    ]


async def prepare():
    """감독을 만든다. 꺼져 있으면 아무것도 하지 않는다"""
    from . import api, director
    if not settings.studio_enabled:
        api.state.reason = "스튜디오는 아직 열리지 않았습니다."
        return
    try:
        api.state.director = director.Director()
    except Exception:  # noqa: BLE001
        log.exception("스튜디오 감독을 만들지 못했습니다")
        api.state.reason = "스튜디오를 준비 중입니다."


async def _check_recipes(d) -> list[str]:
    """V1~V10·V13 (파일만 보고 하는 검사). 통과하지 못한 레시피는 끈다. 결과는 DB 에 남긴다 (6.4)"""
    from .. import models_index, promptops, video
    from . import recipes, store
    problems = []
    profiles = {p["id"] for p in video.profile_list()}
    for rid, r in sorted(d.recipe_map.items()):
        report = recipes.check_recipe(r, profiles)
        missing = [m["name"] for m in promptops.find_models(r["prompt"]) if not models_index.resolve(m["name"])]
        report.append({"id": "V10", "ok": not missing, "level": "error" if missing else "ok",
                       "msg": ("공용 저장소에 없는 모델: " + ", ".join(missing)) if missing else ""})
        ok = recipes.recipe_ok(report)
        raw = open(os.path.join(recipes.RECIPE_DIR, rid + ".json"), "rb").read() \
            if os.path.exists(os.path.join(recipes.RECIPE_DIR, rid + ".json")) else b""
        try:
            await store.save_recipe_check(r, hashlib.sha256(raw).hexdigest(), ok, report)
        except Exception:  # noqa: BLE001
            log.exception("레시피 검증 기록 실패")
        if not ok:
            d.disabled.add(rid)
            bad = [f"{x['id']}: {x['msg']}" for x in report if not x["ok"]]
            log.warning("레시피 %s 를 껐습니다: %s", rid, "; ".join(bad))
            problems.append(rid)
    return problems


async def _ollama_models() -> set[str] | None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.ollama_upstream}/api/tags")
            return {m.get("name") for m in r.json().get("models", [])}
    except Exception:  # noqa: BLE001
        return None


async def start():
    """스케줄러 시작 뒤. 실패해도 허브는 계속 돈다"""
    from . import api, store
    d = api.state.director
    if not settings.studio_enabled or d is None:
        return
    try:
        env = await store.studio_env(settings.studio_env_slug)
        if env is None:
            api.state.reason = "스튜디오를 준비 중입니다. (채팅 작업공간이 아직 없습니다)"
            log.warning("채팅 작업공간 %s 이 없어 스튜디오를 열지 않습니다 (docs/chat-studio-design.md 7.2)", settings.studio_env_slug)
            return
        await _check_recipes(d)
        models = await _ollama_models()
        if models is not None:
            need = {settings.expand_model, settings.video_frame_model}
            lack = sorted(m for m in need if m not in models)
            if lack:
                api.state.reason = "스튜디오를 준비 중입니다. (AI 모델이 아직 없습니다)"
                log.warning("Ollama 에 모델이 없어 스튜디오를 열지 않습니다: %s", ", ".join(lack))
                return
        if d.recipe_for("i2v") is None:
            api.state.reason = "스튜디오를 준비 중입니다. (사용할 수 있는 레시피가 없습니다)"
            return
        api.state.ready = True
        await d.recover()
        asyncio.ensure_future(_cleanup_loop())
        log.info("스튜디오가 열렸습니다 (레시피 %s)", ", ".join(sorted(set(d.recipe_map) - d.disabled)))
    except Exception:  # noqa: BLE001
        log.exception("스튜디오 시작 작업 실패")
        api.state.ready = False
        api.state.reason = "스튜디오를 준비 중입니다."


async def _cleanup_loop():
    """보내지 않은 업로드를 지운다 (9.4). 1시간마다"""
    from . import files, store
    while True:
        try:
            env = await store.studio_env(settings.studio_env_slug)
            for a in await store.unsent_uploads(settings.studio_unsent_hours):
                if env and a.get("input_rel"):
                    p = files.input_path(env, a["input_rel"])
                    if p:
                        os.remove(p)
                await store.mark_attachment_deleted(a["id"])
        except Exception:  # noqa: BLE001
            log.exception("스튜디오 정리 작업 실패")
        await asyncio.sleep(3600)
