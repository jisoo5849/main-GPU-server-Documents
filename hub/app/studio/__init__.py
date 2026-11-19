"""채팅 스튜디오 (docs/chat-studio-design.md). 허브 안의 감독이 기존 GPU 대기열로 ComfyUI 와 AI 부품을 돌린다.

- prepare(): 허브 시작 때 스케줄러보다 먼저. 감독을 만든다 (실패해도 허브 시작을 막지 않는다)
- start(): 스케줄러를 시작한 뒤. 레시피·모델·작업공간을 검사하고 돌던 요청을 복구한다. 못 열면 /studio 는 503 안내이고,
  허브를 다시 시작하지 않아도 30초~5분 간격으로 다시 검사해서 준비되는 대로 연다 (11-18·19 메인: 시작 때 모델 저장소를
  못 읽어 레시피가 전부 꺼진 채 허브를 손으로 다시 시작할 때까지 503 이었다)
- routes(): 포털 앱에 더할 경로
"""
import asyncio
import hashlib
import logging
import os

from ..config import settings

log = logging.getLogger("mgs.studio")

RETRY_FIRST, RETRY_MAX = 30, 300       # 못 열었을 때 다시 검사하는 간격(초): 30 → 60 → … → 300 에서 멈춘다
_last_ok: dict[str, bool] = {}          # 레시피별 지난 검사 결과 (같으면 DB·로그에 다시 남기지 않는다)
_cleanup_started = False


def wants_studio_home(user) -> bool:
    """로그인 첫 화면을 채팅으로 보낼 사용자인가: users.home 이 'studio' 이고 스튜디오가 켜져 있고 쓸 수 있는 사용자 (D1b)"""
    if not user or user.get("home") != "studio" or not settings.studio_enabled:
        return False
    return not settings.studio_allowed_users or user["username"] in settings.studio_allowed_users or user.get("role") == "admin"


def routes() -> list:
    from starlette.routing import Route, WebSocketRoute

    from . import api, opview
    return opview.routes() + [
        Route("/studio", api.page),
        Route("/studio/static/{name}", api.static),
        Route("/studio/logout", api.logout, methods=["POST"]),
        Route("/studio/api/me", api.me),
        Route("/studio/api/threads", api.threads, methods=["GET", "POST"]),
        Route("/studio/api/threads/{id}", api.thread_item, methods=["GET", "PATCH", "DELETE"]),
        Route("/studio/api/threads/{id}/messages", api.messages, methods=["GET", "POST"]),
        Route("/studio/api/uploads", api.uploads, methods=["POST"]),
        Route("/studio/api/files/{id}", api.file_get),
        Route("/studio/api/turns/{id}", api.turn_get),
        Route("/studio/api/turns/{id}/cancel", api.turn_cancel, methods=["POST"]),
        Route("/studio/api/turns/{id}/retry", api.turn_retry, methods=["POST"]),
        Route("/studio/api/turns/{id}/to_video", api.turn_to_video, methods=["POST"]),
        Route("/studio/api/turns/{id}/approve", api.turn_approve, methods=["POST"]),
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
    """V1~V10·V13 (파일만 보고 하는 검사). 통과하지 못한 레시피는 끈다. 결과는 DB 에 남긴다 (6.4).
    다시 검사할 때는 결과가 지난번과 달라진 레시피만 DB·로그에 남긴다 (몇 분마다 같은 줄이 쌓이지 않게)"""
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
        changed = _last_ok.get(rid) != ok
        _last_ok[rid] = ok
        if changed:
            try:
                await store.save_recipe_check(r, hashlib.sha256(recipes.raw_bytes(r)).hexdigest(), ok, report)
            except Exception:  # noqa: BLE001
                log.exception("레시피 검증 기록 실패")
        if not ok:
            d.disabled.add(rid)
            if changed:
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
    """스케줄러 시작 뒤. 실패해도 허브는 계속 돌고, 못 열었으면 다시 검사하는 일을 뒤에서 돌린다"""
    from . import api
    d = api.state.director
    if not settings.studio_enabled or d is None:
        return
    if not await try_open(d, 0):
        asyncio.ensure_future(_retry_open(d))


async def _retry_open(d):
    from . import api
    wait, n = RETRY_FIRST, 0
    while not api.state.ready:
        await asyncio.sleep(wait)
        n += 1
        if await try_open(d, n):
            return
        wait = min(RETRY_MAX, wait * 2)


def _not_ready(reason: str, attempt: int, msg: str, *args):
    """준비 안 됨: 화면 안내를 바꾸고, 처음과 이후 10번마다 한 번만 경고를 남긴다"""
    from . import api
    api.state.ready = False
    api.state.reason = reason
    if attempt % 10 == 0:
        log.warning(msg + (" (%d번째 검사, 자동으로 다시 검사합니다)" % (attempt + 1)), *args)


def _store_state() -> tuple[bool, int]:
    """모델 저장소가 보이는지, 모델 파일이 몇 개인지 (시작 때 비어 보였던 원인을 로그로 남기려고)"""
    from .. import models_index
    return os.path.isdir(settings.models_dir), sum(len(v) for v in models_index.index().values())


async def try_open(d, attempt: int = 0) -> bool:
    """레시피·모델·작업공간을 검사하고 되면 연다. 열었으면 True. 다시 부를 수 있다 (꺼 둔 레시피도 다시 검사한다)"""
    global _cleanup_started
    from .. import models_index
    from . import api, store
    try:
        env = await store.studio_env(settings.studio_env_slug)
        if env is None:
            _not_ready("스튜디오를 준비 중입니다. (채팅 작업공간이 아직 없습니다)", attempt,
                       "채팅 작업공간 %s 이 없어 스튜디오를 열지 않습니다 (docs/chat-studio-design.md 7.2)", settings.studio_env_slug)
            return False
        if attempt:
            models_index.index(max_age=0)       # 저장소를 새로 읽는다: 처음 검사 때 비어 보였을 수 있다
            d.disabled.clear()                  # 열기 전에는 요청이 돌지 않으므로 꺼 둔 레시피를 모두 다시 본다
        await _check_recipes(d)
        models = await _ollama_models()
        if models is not None:
            need = {settings.expand_model, settings.video_frame_model}
            lack = sorted(m for m in need if m not in models)
            if lack:
                _not_ready("스튜디오를 준비 중입니다. (AI 모델이 아직 없습니다)", attempt,
                           "Ollama 에 모델이 없어 스튜디오를 열지 않습니다: %s", ", ".join(lack))
                return False
        if d.recipe_for("i2v") is None and d.recipe_for("i2v", sound=True) is None:
            seen, files = _store_state()
            if not files:
                _not_ready("스튜디오를 준비 중입니다. (모델 저장소가 비어 보입니다. 자동으로 다시 확인합니다)", attempt,
                           "모델 저장소 %s 가 %s: 레시피를 쓸 수 없어 스튜디오를 열지 않습니다. 계속되면 허브를 다시 시작해 주세요",
                           settings.models_dir, "비어 있습니다(모델 파일 0개)" if seen else "보이지 않습니다")
            else:
                _not_ready("스튜디오를 준비 중입니다. (사용할 수 있는 레시피가 없습니다. 자동으로 다시 확인합니다)", attempt,
                           "쓸 수 있는 영상 레시피가 없어 스튜디오를 열지 않습니다 (모델 저장소 %s, 모델 파일 %d개)",
                           settings.models_dir, files)
            return False
        api.state.ready = True
        await d.recover()
        if not _cleanup_started:
            _cleanup_started = True
            asyncio.ensure_future(_cleanup_loop())
        log.info("스튜디오가 열렸습니다 (레시피 %s)%s", ", ".join(sorted(set(d.recipe_map) - d.disabled)),
                 f" — {attempt + 1}번째 검사에서" if attempt else "")
        return True
    except Exception:  # noqa: BLE001
        log.exception("스튜디오 시작 작업 실패")
        api.state.ready = False
        api.state.reason = "스튜디오를 준비 중입니다."
        return False


async def _cleanup_loop():
    """보내지 않은 업로드를 지운다 (9.4). 1시간마다"""
    from . import api, files, store
    while True:
        try:
            env = await store.studio_env(settings.studio_env_slug)
            if api.state.director is not None:
                await api.state.director.expire_plans()
            for a in await store.unsent_uploads(settings.studio_unsent_hours):
                if env and a.get("input_rel"):
                    p = files.input_path(env, a["input_rel"])
                    if p:
                        os.remove(p)
                await store.mark_attachment_deleted(a["id"])
        except Exception:  # noqa: BLE001
            log.exception("스튜디오 정리 작업 실패")
        await asyncio.sleep(3600)
