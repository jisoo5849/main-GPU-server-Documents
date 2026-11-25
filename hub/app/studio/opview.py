"""운영자 기록 화면 /admin/studio (docs/chat-studio-design.md 12절). 관리자만 본다.

- 데이터를 모으는 일(DB 조회)과 화면용 모양으로 바꾸는 일(순수 함수)을 나눈다. 뒤쪽은 DB 없이 시험한다.
- 사용자 글·LLM 답·첫 화면 읽기 결과는 모두 믿지 않는 데이터다. 템플릿의 자동 이스케이프만 쓰고 |safe 를 쓰지 않는다 (14.3).
- 화면 코드는 portal 을 늦게 불러온다(portal 이 studio 를 불러오므로 순환을 피한다).
"""
import datetime
import json
import logging

from . import recipes

log = logging.getLogger("mgs.studio.opview")

PAGE = 50
STATES = ("pending", "active", "awaiting_approval", "done", "failed", "cancelled", "interrupted")
# 관리자 확인이 필요한 실패 (13절: 접수 검증 실패·모델 파일 바뀜·계정·환경 문제)
ATTENTION_CODES = ("submit", "rejected", "prepare_down")

LENGTH_BY_KO = {"rule": "규칙", "llm": "LLM"}


def short(v, n: int = 80) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "…"


def duration_s(t: dict) -> int | None:
    a, b = t.get("created_at"), t.get("finished_at")
    if a is None or b is None:
        return None
    return int((b - a).total_seconds())


def summarize_turn(t: dict, jobs: list[dict] | None = None, notice_kinds: set | None = None) -> dict:
    """목록 한 줄 (12절 목록 표). jobs 는 이 요청의 GPU 줄, notice_kinds 는 이 요청의 chat_events kind 집합"""
    jobs = jobs or []
    kinds = notice_kinds or set()
    req = t.get("request") or {}
    err = t.get("error") or {}
    return {
        "id": str(t["id"]), "created_at": t.get("created_at"), "username": t.get("username"),
        "title": t.get("title") or "", "action": t.get("action") or "-", "state": t["state"], "stage": t.get("stage") or "",
        "seconds": duration_s(t), "text": short(req.get("text"), 60), "recipe": f"{t.get('recipe_id') or '-'} v{t.get('recipe_version') or '-'}",
        "jobs": len(jobs), "wait_ms": sum(int(j.get("wait_ms") or 0) for j in jobs),
        "run_ms": sum(int(j.get("run_ms") or 0) for j in jobs),
        "notice": "notice" in kinds, "retry": bool(t.get("retry_of")) or "retry" in kinds,
        "error_code": err.get("code") if isinstance(err, dict) else None,
        "attention": isinstance(err, dict) and err.get("code") in ATTENTION_CODES,
    }


def repro_prompt(turn: dict, events: list[dict], atts: dict[str, dict]) -> dict | None:
    """레시피와 접수 때 넣은 값(chat_events.comfy_submitted + work.prompt)으로 prompt 를 다시 만든다.
    관리자가 ComfyUI 에서 열어 다시 실행해 볼 수 있다. 접수 기록이 없거나 레시피가 바뀌어 맞지 않으면 None"""
    sub = next((e["detail"] for e in reversed(events) if e.get("kind") == "comfy_submitted" and e.get("detail")), None)
    if not sub:
        return None
    recipe = recipes.load_all().get(sub.get("recipe") or "")
    work = turn.get("work") or {}
    if recipe is None or not work.get("prompt"):
        return None
    if sub.get("version") is not None and int(sub["version"]) != int(recipe["version"]):
        return None      # 레시피 판이 바뀌었으면 접수 때의 그래프와 다르다
    try:
        vals = {"prompt": work["prompt"], "seed": int(sub["seed"])}
        size = sub.get("size") or []
        if size:
            vals["width"], vals["height"] = int(size[0]), int(size[1])
        if sub.get("frames"):
            vals["frames"] = int(sub["frames"])
            cap = int((recipe["inject"].get("check_index") or {}).get("max", 80))
            vals["check_index"] = max(0, min(int(sub["frames"]) // 2, cap))
            if "seconds" in recipe["inject"]:      # LTX: 길이를 초로 받는다 (감독의 _submit 과 같은 식)
                sl = recipe["inject"]["seconds"]
                fps = int((recipe.get("defaults") or {}).get("fps") or 24)
                vals["seconds"] = max(int(sl.get("min", 2)), min(int(sl.get("max", 20)), int(round((int(sub["frames"]) - 1) / fps))))
        req = turn.get("request") or {}
        # 글→영상을 장면 이미지로 만든 요청은 첫 화면이 그 장면 이미지(숨은 첨부)다
        for aid in ([work["scene_att"]] if work.get("scene_att") else (req.get("attachment_ids") or [])):
            a = atts.get(str(aid))
            if a and a.get("input_rel"):
                for name in ("first_frame", "edit_image"):
                    if name in recipe["inject"]:
                        vals[name] = a["input_rel"]
        vals["save_prefix"] = [f"repro/{str(turn['id'])[:8]}/{s['suffix']}" for s in recipes.slots_of(recipe, "save_prefix")]
        if "negative" in recipe["inject"]:      # 부정 글 자리(장면 그림, 11-24 2차): 접수 때 넣은 글, 없으면 레시피의 기본 부정 글
            vals["negative"] = str(sub.get("negative") or "") or recipes.default_negative(recipe)
        return recipes.build(recipe, {k: v for k, v in vals.items() if k in recipe["inject"]})
    except (recipes.RecipeError, KeyError, ValueError, TypeError):
        return None


PURPOSE_KO = {"prompt_expand": "이미지 프롬프트 확장", "video_frame": "첫 화면 읽기", "video_expand": "영상 프롬프트 작성",
              "video_fix": "영상 프롬프트 고치기", "edit_prompt": "편집 지시문", "image_inspect": "결과 검사",
              "video_plan": "연출 계획표", "video_story": "긴 영상 구성안"}
LONG_EVENT_KINDS = ("story", "long_inherited", "long_scene_start", "long_scene_retry", "long_scene_done", "long_resumed",
                    "long_music", "long_assembled", "end_check", "end_keyframe_failed", "end_keyframe_dropped", "cast_style_convert",
                    "long_chain_empty", "long_scene_redo", "place_card", "place_card_dropped", "layout_done", "duo_fallback",
                    "quality_still_failed", "cast_style_convert_fallback")     # 정밀 판·실사 변환이 실패해 빠른 길로 내려감 (11-24 2차)


def long_view(work: dict, events: list[dict]) -> dict | None:
    """긴 영상(여러 장면) 요청의 구성안·장면별 기록·음악·합친 결과. 긴 영상이 아니면 None"""
    story = work.get("story")
    if not story:
        return None
    scenes = []
    done = {i: s for i, s in enumerate(work.get("scenes") or [])}
    for i, sc in enumerate(story.get("scenes") or []):
        s = done.get(i) or {}
        scenes.append({"index": i + 1, "seconds": sc.get("seconds"), "ko": sc.get("ko"), "continues": sc.get("continues"),
                       "shot": sc.get("shot"), "action": sc.get("action"), "sound": sc.get("sound"),
                       "first_from": s.get("first_from"), "seam": s.get("seam"), "end_rel": s.get("end_rel"),
                       "chain_rel": s.get("chain_rel"), "end_fail": s.get("end_fail"), "layout_rel": s.get("layout_rel"),
                       "plan": s.get("plan"), "scene_prompt": s.get("scene_prompt"),
                       "prompt": s.get("prompt"), "violations": s.get("violations"), "clip_rel": s.get("clip_rel"),
                       "last_rel": s.get("last_rel"), "image_rel": s.get("image_rel"), "listener": sc.get("listener"),
                       # 누구에게 하는 말인지 (11-24 사용자 확정 규칙: 독백은 혼자 클로즈업, 모두에게 하는 말은 그 자리 사람 모두)
                       "monologue": sc.get("monologue"), "to_all": sc.get("to_all"), "duo": s.get("duo")})
    return {"title": story.get("title_ko"), "logline": story.get("logline_ko"), "seconds": story.get("seconds"),
            "bible": story.get("bible"), "music": story.get("music"), "scenes": scenes,
            "music_rel": work.get("music_rel"), "final_rel": work.get("final_rel"),
            "events": [{"kind": e.get("kind"), "detail": e.get("detail")} for e in events if e.get("kind") in LONG_EVENT_KINDS]}


def system_prompts(llm_jobs: list) -> list[dict]:
    """이 요청의 LLM 차례마다 실제로 보낸 시스템 지시문 [{purpose, model, texts}]. 사용자 글은 들어 있지 않다.
    같은 글이 되풀이되면 한 번만 보인다 (이 기록이 생기기 전 요청은 비어 있다)"""
    out, seen = [], set()
    for d in llm_jobs:
        texts = [t for t in (d or {}).get("systems") or [] if isinstance(t, str) and t not in seen]
        seen.update(texts)
        if texts:
            out.append({"purpose": PURPOSE_KO.get(d.get("purpose"), d.get("purpose")), "model": d.get("model"), "texts": texts})
    return out


def build_detail(turn: dict, events: list[dict], jobs: list[dict], llm_calls: list[dict], messages: list[dict],
                 atts: list[dict], recipe_check: dict | None) -> dict:
    """상세 화면(12절 상세 표)용 값. 믿지 않는 글은 문자열 그대로 두고 이스케이프는 템플릿이 한다"""
    work = turn.get("work") or {}
    by_kind: dict[str, list] = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e.get("detail"))
    atts_by_id = {str(a["id"]): a for a in atts}
    calls = {}
    for c in llm_calls:
        calls.setdefault(str(c.get("job_id")), []).append(c)
    gpu = []
    for j in jobs:
        gpu.append({**{k: j.get(k) for k in ("id", "seq", "kind", "step", "purpose", "attempt", "state", "outcome", "model",
                                              "submitted_at", "started_at", "exec_started_at", "finished_at", "wait_ms",
                                              "cold_start_ms", "run_ms", "cleanup_ms", "cleanup_method", "vram_peak_mb")},
                    "calls": calls.get(str(j.get("id")), [])})
    llm_ends = [d for d in by_kind.get("llm_job", []) if d]
    ending = {str(d.get("job_id")): d.get("end") for d in llm_ends}
    for g in gpu:
        g["end"] = ending.get(str(g["id"]))
    # 장문·보통을 고른 이유, 걸린 검사
    lc = work.get("length_choice") or {}
    return {
        "turn": turn,
        "request": {"text": (turn.get("request") or {}).get("text"), "spec": (turn.get("request") or {}).get("spec"),
                    "mode": (turn.get("request") or {}).get("mode"), "notes": (turn.get("request") or {}).get("notes"),
                    "ip": str(turn.get("client_ip") or ""),
                    "attachments": [atts_by_id[str(i)] for i in ((turn.get("request") or {}).get("attachment_ids") or [])
                                    if str(i) in atts_by_id]},
        "decision": {"column": turn.get("decision"), "events": by_kind.get("decision", [])},
        "frame": work.get("frame"),
        "systems": system_prompts(by_kind.get("llm_job", [])),
        # 글→영상을 장면 이미지 → 영상으로 만든 요청의 장면 이미지 단계 (이미지 모델에 들어간 프롬프트와 그 기록)
        "scene": ({"prompt": work.get("scene_prompt"), "size": work.get("scene_size"),
                   "events": [e.get("detail") for e in events if e.get("kind") in ("scene_start", "scene_done", "scene_resumed", "scene_node_timing")],
                   "expand": [d for d in by_kind.get("expand", []) if d and d.get("scene")]}
                  if work.get("scene_prompt") else None),
        "plan": work.get("plan"),
        "long": long_view(work, events),
        "prompt": {"text": work.get("prompt"), "parts": work.get("parts"), "ko": work.get("ko"), "profile": work.get("profile")},
        "length": {"tier": lc.get("tier"), "reason": lc.get("reason"), "by": LENGTH_BY_KO.get(lc.get("by"), lc.get("by"))},
        "crew": {"work": work.get("crew"), "events": [d.get("crew") for d in by_kind.get("render", []) + by_kind.get("expand", [])
                                                      if d and d.get("crew")]},
        "violations": {"render": [d for d in by_kind.get("render", []) if d], "fix": [d for d in by_kind.get("fix", []) if d]},
        "submitted": by_kind.get("comfy_submitted", []),
        "recipe_check": recipe_check,
        "gpu": gpu,
        "llm_ends": llm_ends,
        "node_timing": by_kind.get("node_timing", []),
        "outputs": by_kind.get("output", []),
        "inspect": by_kind.get("inspect", []),
        "notices": by_kind.get("notice", []),
        "errors": by_kind.get("error", []),
        "cancels": by_kind.get("cancel", []),
        "events": events,
        "result": turn.get("result"),
        "messages": messages,
        "repro": repro_prompt(turn, events, atts_by_id),
    }


def pretty(v) -> str:
    """상세 화면의 JSON 보기. 문자열로만 만들고 이스케이프는 템플릿이 한다"""
    try:
        return json.dumps(v, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(v)


# ── DB 조회 (허브 안에서만) ──

async def list_turns(user: str = "", state: str = "", day: str = "", offset: int = 0) -> list[dict]:
    from .. import db
    where, args = ["TRUE"], []
    if user:
        args.append(user)
        where.append(f"u.username = ${len(args)}")
    if state in STATES:
        args.append(state)
        where.append(f"t.state = ${len(args)}")
    try:      # asyncpg 는 date 칸에 문자열을 받지 않는다. 형식이 틀리면 거르기를 무시한다
        d = datetime.date.fromisoformat(day) if day else None
    except ValueError:
        d = None
    if d is not None:
        args.append(d)
        where.append(f"t.created_at::date = ${len(args)}")
    args += [PAGE, min(max(0, int(offset)), 10 ** 9)]
    rows = await db.fetch(
        "SELECT t.*, u.username, th.title FROM chat_turns t JOIN users u ON u.id = t.user_id "
        "JOIN chat_threads th ON th.id = t.thread_id WHERE " + " AND ".join(where) +
        f" ORDER BY t.created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}", *args)
    out = []
    for r in rows:
        t = dict(r)
        jobs = [dict(x) for x in await db.fetch(
            "SELECT j.wait_ms, j.run_ms FROM chat_turn_jobs c JOIN jobs j ON j.id = c.job_id WHERE c.turn_id = $1", t["id"])]
        kinds = {x["kind"] for x in await db.fetch(
            "SELECT DISTINCT kind FROM chat_events WHERE turn_id = $1 AND kind IN ('notice','retry')", t["id"])}
        out.append(summarize_turn(t, jobs, kinds))
    return out


async def attention_turns() -> list[dict]:
    """관리자 확인이 필요한 요청 (접수 검증 실패·모델 파일 바뀜 등). 목록 위에 모아 보인다"""
    from .. import db
    rows = await db.fetch(
        "SELECT t.*, u.username, th.title FROM chat_turns t JOIN users u ON u.id = t.user_id "
        "JOIN chat_threads th ON th.id = t.thread_id WHERE t.state = 'failed' AND t.error->>'code' = ANY($1::text[]) "
        "AND t.created_at > now() - interval '7 days' ORDER BY t.created_at DESC LIMIT 20", list(ATTENTION_CODES))
    return [summarize_turn(dict(r)) for r in rows]


async def get_detail(turn_id) -> dict | None:
    from .. import db
    from . import store
    t = await store.get_turn(turn_id)
    if t is None:
        return None
    u = await db.fetchrow("SELECT username FROM users WHERE id = $1", t["user_id"])
    th = await db.fetchrow("SELECT title FROM chat_threads WHERE id = $1", t["thread_id"])
    t["username"], t["title"] = (u or {}).get("username"), (th or {}).get("title")
    events = [dict(e) for e in await db.fetch("SELECT kind, ts, detail FROM chat_events WHERE turn_id = $1 ORDER BY id", t["id"])]
    jobs = [dict(j) for j in await db.fetch(
        "SELECT c.step, c.purpose, c.attempt, j.id, j.seq, j.kind, j.state, j.outcome, j.model, j.submitted_at, j.started_at, "
        "j.exec_started_at, j.finished_at, j.wait_ms, j.cold_start_ms, j.run_ms, j.cleanup_ms, j.cleanup_method, j.vram_peak_mb "
        "FROM chat_turn_jobs c JOIN jobs j ON j.id = c.job_id WHERE c.turn_id = $1 ORDER BY j.seq", t["id"])]
    ids = [j["id"] for j in jobs]
    calls = [dict(c) for c in await db.fetch(
        "SELECT job_id, endpoint, model, prompt_tokens, completion_tokens, duration_ms, status FROM llm_calls "
        "WHERE job_id = ANY($1::uuid[]) ORDER BY id", ids)] if ids else []
    msgs = [dict(m) for m in await db.fetch(
        "SELECT id, role, kind, body, created_at FROM chat_messages WHERE turn_id = $1 OR id = $2 ORDER BY id",
        t["id"], t.get("message_id") or 0)]
    aids = list((t.get("request") or {}).get("attachment_ids") or []) + list((t.get("result") or {}).get("attachment_ids") or [])
    if (t.get("work") or {}).get("scene_att"):
        aids.append((t["work"])["scene_att"])
    atts = await store.attachments_of(aids) if aids else []
    rc = await store.latest_recipe_check(t.get("recipe_id")) if t.get("recipe_id") else None
    return build_detail(t, events, jobs, calls, msgs, atts, rc)


async def job_prompt(turn_id) -> dict | None:
    """실제로 접수한 jobs.prompt (비교용)"""
    from .. import db
    from . import store
    jid = await store.turn_job(turn_id, "generate")
    if jid is None:
        return None
    row = await db.fetchrow("SELECT prompt FROM jobs WHERE id = $1", jid)
    return None if row is None else row["prompt"]


# ── 화면 ──

def routes():
    from starlette.routing import Route
    return [
        Route("/admin/studio", page_list),
        Route("/admin/studio/turns/{id}", page_detail),
        Route("/admin/studio/turns/{id}/repro.json", repro_json),
    ]


ADMIN_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}


def _guard(handler):
    async def wrapper(request):
        from .. import portal
        resp = await portal.admin_required(handler)(request)
        for k, v in ADMIN_HEADERS.items():       # 믿지 않는 글이 이스케이프를 뚫어도 스크립트가 돌지 못하게 (14.3)
            resp.headers.setdefault(k, v)
        return resp
    return wrapper


@_guard
async def page_list(request):
    from .. import portal
    q = request.query_params
    try:
        offset = int(q.get("offset", 0))
    except ValueError:
        offset = 0
    rows = await list_turns(q.get("user", "").strip(), q.get("state", ""), q.get("day", "").strip(), offset)
    attention = await attention_turns() if offset == 0 else []
    return portal.render(request, "admin_studio.html", rows=rows, attention=attention, states=STATES,
                         f_user=q.get("user", ""), f_state=q.get("state", ""), f_day=q.get("day", ""),
                         offset=offset, page=PAGE)


@_guard
async def page_detail(request):
    from starlette.responses import Response

    from .. import portal
    d = await get_detail(request.path_params["id"])
    if d is None:
        return Response("요청을 찾을 수 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
    return portal.render(request, "admin_studio_turn.html", d=d, pretty=pretty, short=short,
                         job_prompt=await job_prompt(d["turn"]["id"]))


@_guard
async def repro_json(request):
    from starlette.responses import JSONResponse, Response
    d = await get_detail(request.path_params["id"])
    if d is None or d["repro"] is None:
        return Response("다시 만들 수 있는 기록이 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
    return JSONResponse(d["repro"], headers={"Content-Disposition": f'attachment; filename="studio-{str(d["turn"]["id"])[:8]}.json"'})
