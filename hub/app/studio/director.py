"""감독: 채팅 요청 하나를 끝까지 진행한다 (docs/chat-studio-design.md 5절). P1 은 사진 → 이미지→영상 한 줄기다.

흐름: 요청 → (첫 화면 읽기 → 프롬프트 작성 → 필요하면 다듬기: 기존 video.render·video.fix 를 그대로 부름)
      → 레시피에 값 넣기 → 기존 접수 함수로 ComfyUI 작업 접수 → 진행 감시 → 결과 전달 → (결과 검사)

바깥 일(영상 렌더러·접수·스케줄러·DB 읽기)은 Deps 로 모아 시험에서 가짜로 바꾼다.
GPU 는 직접 쓰지 않는다. 모든 GPU 사용은 기존 대기열(llm_turn·submit_comfy)을 거친다.
"""
import asyncio
import contextlib
import logging
import random
import re
import time

from ..config import settings
from . import files, intent, progress, recipes, store

log = logging.getLogger("mgs.studio")

POLL = 1.5          # 작업 상태를 DB 에서 다시 읽는 간격(초). 시험은 짧게 바꾼다
LLM_POLL = 1.0
OOM_RE = re.compile(r"out of memory|oom|메모리가 부족|메모리 부족")
WORDS_ONLY_RE = re.compile(r"^The prompt has \d+ words")
CRITICAL_ALWAYS = {"extra_head", "detached_head", "merged_bodies", "face", "text_artifact"}
CRITICAL_NOT_FIRST_PERSON = {"missing_head", "extra_limb", "floating_limb"}
ISSUE_KO = {"extra_head": "머리가 하나 더 있는 것처럼", "detached_head": "목이 끊긴 것처럼",
            "merged_bodies": "두 사람이 붙은 것처럼", "face": "얼굴이 뭉개져",
            "text_artifact": "알아볼 수 없는 글자가 생긴 것처럼", "missing_head": "머리가 없는 것처럼",
            "extra_limb": "팔다리가 하나 더 있는 것처럼", "floating_limb": "팔다리가 몸에서 떨어져 떠 있는 것처럼"}
FIRST_PERSON_TRAIT = "The view is first-person: no head, back or shoulders of the viewer are visible"
CHIP_OF_STAGE = {"queue": "generate", "prepare": "generate", "plan": "prompt", "inspect": "collect"}
LLM_JOB_KEYS = ("job_id", "purpose", "model", "end", "systems")      # llm_job 기록에 남기는 값 (systems: 보낸 시스템 지시문)
RETRY = [{"id": "retry_seed", "label": "다시 만들기"}]
RETRY_FULL = [{"id": "retry", "label": "다시 시도"}]

# 실패 안내 (13.1). 한 줄, 한국어, 원인 + 제안 질문 하나
FAIL_TEXT = {
    "queue_wait": ("GPU 대기열이 길어 차례를 받지 못했습니다. 다시 시도해 볼까요?", RETRY_FULL),
    "llm_error": ("프롬프트를 만드는 중 오류가 났습니다. 다시 시도해 볼까요?", RETRY_FULL),
    "rejected": ("지금은 작업을 시작할 수 없습니다. 관리자에게 문의해 주세요.", []),
    "admin_cancelled": ("관리자가 작업을 취소했습니다.", []),
    "submit": ("작업을 접수하지 못했습니다. 관리자가 확인하고 있습니다.", []),
    "prepare_down": ("스튜디오를 준비 중입니다. 잠시 뒤 다시 시도해 볼까요?", RETRY_FULL),
    "gpu_prepare": ("GPU 를 준비하지 못했습니다. 다시 만들어 볼까요?", RETRY),
    "run_error": ("생성 중 오류가 났습니다. 다시 만들어 볼까요?", RETRY),
    "run_oom": ("GPU 메모리가 모자랐습니다. 다시 만들어 볼까요?", RETRY),
    "run_timeout": ("시간이 너무 오래 걸려 멈췄습니다. 다시 만들어 볼까요?", RETRY),
    "lost": ("GPU 작업이 중간에 끊겼습니다. 다시 만들어 볼까요?", RETRY),
    "no_output": ("결과 파일을 찾지 못했습니다. 다시 만들어 볼까요?", RETRY),
    "interrupted": ("허브가 다시 시작되어 작업이 중단되었습니다. 다시 시도해 볼까요?", RETRY_FULL),
    "unknown": ("예상하지 못한 오류가 났습니다. 다시 시도해 볼까요?", RETRY_FULL),
    "no_image": ("사진을 함께 올려 주세요.", []),
    "unavailable": ("이 기능은 지금 준비 중입니다.", []),
    "unsafe": ("이 요청은 만들 수 없습니다.", []),
    "bad_prompt": ("요청과 다른 장면으로 프롬프트가 써져서 만들지 않았습니다. 다시 시도해 볼까요?", RETRY_FULL),
    "changed": ("결과 파일이 바뀌어 이어서 만들 수 없습니다.", []),
    "gone": ("결과 파일을 찾을 수 없어 이어서 만들 수 없습니다.", []),
}
# 고쳐 쓴 뒤에도 남으면 그 프롬프트로 영상을 만들지 않는 위반: 요청과 다른 장면(예시 베끼기)이다 (11-16 파일럿 요청)
BLOCKING_RE = re.compile(r"copies the EXAMPLE|describes a different scene")
PLAN_ACTIONS = [{"id": "approve", "label": "승인"}, {"id": "cancel", "label": "취소"}]
PLAN_STATUS_TEXT = {"approved": "승인함", "cancelled": "취소됨", "expired": "시간이 지나 취소됨", "replanned": "바꿔서 다시 계획함"}
TWO_RE = re.compile(r"두\s*(?:사람|명)|둘이|커플|\btwo\b", re.I)
TO_VIDEO = [{"id": "to_video", "label": "이걸로 영상 만들기"}]


def shown_stage(ctx) -> str:
    """사용자에게 보이는 단계. 글→영상을 장면 이미지 → 영상 두 단계로 만들 때 안쪽 단계 이름(이미지·첫 화면 읽기)을 숨긴다"""
    s = ctx.stage
    if not ctx.via:
        return s
    if ctx.scene_phase and s in ("queue", "prepare", "generate", "collect"):
        return "scene"
    if s in ("frame", "fix") or (s == "prompt" and ctx.motion):
        return "motion"
    return s


def first_stage(action: str | None) -> str:
    """요청이 시작할 때의 단계. 이미지·글→영상·편집에는 첫 화면 읽기가 없으니 프롬프트 작성이 첫 단계다"""
    return "frame" if action == "i2v" else "prompt"


class StepError(Exception):
    def __init__(self, code: str, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


class Ctx:
    """요청 하나를 진행하는 동안의 값"""

    def __init__(self, turn: dict, user: dict, env: dict, recipe: dict):
        self.turn, self.user, self.env, self.recipe = turn, user, env, recipe
        self.tid = turn["id"]
        self.action = recipe["action"]
        self.tracker = progress.Tracker(recipe["progress"]["bands"])
        self.job_id: str | None = None
        self.bag: list = []
        self.linked: set = set()
        self.has_fix = False
        self.stage = first_stage(self.action)
        self.text = ""
        self.last_emit = 0.0
        self.success_signal = False
        self.cancel_reason: str | None = None
        self.first_person = False
        self.spec: dict = {}
        self.receiver = None
        self.client_id: str | None = None
        self.emit_task = None
        self.emit_again = False
        self.notes: list = []
        self.saved_stage = None
        self.reused = False           # 저장된 프롬프트를 다시 쓴 요청(다시 만들기)이면 계획 카드를 건너뛴다
        self.node_log: list = []      # 노드별 걸린 시간 [{"node", "ms"}] (운영자 기록 node_timing, 12절)
        self._node_cur: tuple | None = None
        self.via = False              # 글→영상을 장면 이미지 → 영상 두 단계로 만드는 요청 (사용자에게는 한 작업)
        self.scene_phase = False      # 지금 장면 이미지 단계의 ctx
        self.motion = False           # 장면 이미지가 끝나 영상 쪽 단계로 넘어갔다
        self.label = "생성"           # 대기 글에 쓰는 이름
        self.job_step = "generate"    # chat_turn_jobs 에 남기는 단계 이름 (장면 이미지는 scene)
        self.pend_key = "pending"     # work 에 접수 정보를 남기는 키 (장면 이미지는 scene_pending)
        self.req_text = ""          # 사용자가 쓴 요청 글. (text 는 진행 카드에 보이는 글이라 따로 둔다)


def _note_node(self, node: str):
    """executing 메시지가 올 때마다 앞 노드의 걸린 시간을 닫고 새 노드를 연다"""
    now = time.monotonic()
    if self._node_cur is not None:
        prev, t0 = self._node_cur
        if prev != node:
            self.node_log.append({"node": prev, "ms": int((now - t0) * 1000)})
        else:
            return
    self._node_cur = (node, now)


def _close_nodes(self):
    if self._node_cur is not None:
        prev, t0 = self._node_cur
        self.node_log.append({"node": prev, "ms": int((time.monotonic() - t0) * 1000)})
        self._node_cur = None


Ctx.note_node = _note_node
Ctx.close_nodes = _close_nodes


class Receiver:
    """ComfyUI 진행 메시지를 받는 자리. relay.sockets 에 (작업공간, client_id) 로 등록한다 (8.1)"""

    def __init__(self, director, ctx: Ctx):
        self.d, self.ctx = director, ctx

    async def send_text(self, text: str):
        import json
        try:
            m = json.loads(text)
        except ValueError:
            return
        # 이 함수는 스케줄러의 진행 중계 루프 안에서 불린다. 느리거나 예외를 내면 GPU 대기열이 막히거나 수신기가 빠지므로
        # 값만 갱신하고(동기), 화면·DB 갱신은 따로 돌린다. 예외는 모두 삼킨다
        try:
            typ, data = m.get("type"), m.get("data") or {}
            if typ in ("execution_success",) or (typ == "executing" and data.get("node") is None and data.get("prompt_id")):
                self.ctx.success_signal = True
            if typ == "progress_text" and self.ctx.stage in ("queue", "prepare"):
                self.ctx.stage = "prepare"
            if typ == "executing" and data.get("node") and data.get("prompt_id") in (None, self.ctx.job_id):
                self.ctx.note_node(str(data["node"]))
            if typ == "execution_start":
                self.ctx.stage = "generate"
            if self.ctx.tracker.feed(typ, data) or typ in ("execution_start", "progress_text"):
                self.d.schedule_emit(self.ctx)
        except Exception:  # noqa: BLE001
            log.exception("진행 메시지 처리 실패")

    async def send_bytes(self, data: bytes):   # 미리보기 이미지는 P1 에서 쓰지 않는다
        return None


class RealDeps:
    """기존 부품을 부르는 얇은 층. 시험은 이 클래스를 바꿔 쓴다."""

    def __init__(self):
        from .ws import hub
        self.hub = hub

    @contextlib.contextmanager
    def llm_context(self):
        from .. import llm
        bag: list = []
        t1, t2 = llm.STUDIO_JOBS.set(bag), llm.STUDIO_WAIT.set(settings.studio_llm_hard_wait)
        try:
            yield bag
        finally:
            llm.STUDIO_WAIT.reset(t2)
            llm.STUDIO_JOBS.reset(t1)

    async def render(self, user, ip, env, **kw):
        from .. import video
        return await video.render(user, ip, env["username"], env["slug"], kw.pop("text"), **kw)

    async def write_edit(self, user, ip, text, frame=None):
        """편집 지시문: 감독이 연 LLM 차례 안에서 영문 한두 문장 (5.5)"""
        from .. import llm, promptx
        from . import edit
        text = promptx.clean_input(text)
        try:
            async with llm.llm_turn(user, ip, settings.expand_model, "edit_prompt") as call:
                return await edit.write_edit_in_turn(call, text, frame)
        except llm.HelperBusy as e:
            raise promptx.PromptxError(str(e), 503) from None

    async def expand_image(self, user, ip, text, crew=None):
        """이미지 프롬프트: 감독이 연 LLM 차례 안에서 제작진 초안 + 확장 (5.5). 차례를 못 얻으면 PromptxError(503)"""
        from .. import llm, promptx
        text = promptx.clean_input(text)
        try:
            async with llm.llm_turn(user, ip, settings.expand_model, "prompt_expand") as call:
                return await promptx.expand_in_turn(call, text, "", "", crew)
        except llm.HelperBusy as e:
            raise promptx.PromptxError(str(e), 503) from None

    async def stage_image(self, env, user, turn, rel):
        """장면 이미지(결과 폴더)를 입력 폴더로 복사한 숨은 첨부를 만든다. 채팅 메시지에는 붙지 않는다"""
        import asyncio
        import uuid
        nbytes, sha = await asyncio.to_thread(files.fingerprint, rel)
        att_id, in_rel, sha2, w, h, n = await asyncio.to_thread(
            files.copy_result_as_input, env, user["username"], user["id"], rel, sha)
        try:
            await store.add_attachment({"id": uuid.UUID(att_id), "thread_id": turn["thread_id"], "user_id": user["id"],
                                        "source": "copy", "media": "image", "mime": "image/png", "width": w, "height": h,
                                        "bytes": n, "sha256": sha2, "input_rel": in_rel,
                                        "meta": {"scene": True}})      # 계획 승인을 기다리는 동안 정리 대상이 아니다
        except Exception:
            p = files.input_path(env, in_rel)
            if p:
                import os
                os.remove(p)
            raise
        return {"id": att_id, "width": w, "height": h}

    async def fix(self, user, ip, env, **kw):
        from .. import video
        return await video.fix(user, ip, env["username"], env["slug"], kw.pop("text"), **kw)

    async def inspect(self, user, ip, username, filename, subfolder, expect):
        from .. import promptx
        return await promptx.inspect(user, ip, username, filename, subfolder, expect)

    def frame_known(self, frame):
        from .. import video
        return video.frame_known(frame)

    def pov(self, text):
        from .. import video
        return bool(video.POV_RE.search(text))

    async def submit(self, env, user, body, ip):
        from .. import jobs
        return await jobs.submit_comfy(env, user, body, ip)

    async def cancel_queued(self, env_id, ids):
        from .. import jobs
        return await jobs.cancel_queued(env_id, ids)

    async def interrupt(self, env_id, job_id):
        from ..scheduler import scheduler
        await scheduler.interrupt(env_id, job_id)

    async def queue(self):
        from .. import jobs
        return await jobs.global_queue()

    async def medians(self):
        return await store.recent_medians()

    async def setting(self, key):
        from .. import db
        return await db.get_setting(key, None)

    def register(self, env_id, client_id, receiver):
        from ..relay import sockets
        sockets.register(env_id, client_id, receiver)

    def unregister(self, env_id, client_id, receiver):
        from ..relay import sockets
        sockets.unregister(env_id, client_id, receiver)

    async def publish(self, user_id, event):
        await self.hub.send(user_id, event)


def needs_fix(violations: list[str]) -> bool:
    """낱말 수 위반만 남았으면 고치지 않는다 (5.12). 모델은 낱말 수를 잘 줄이지 못하고 해롭지 않다"""
    return any(not WORDS_ONLY_RE.match(v) for v in violations or [])


def notice_for_inspection(insp: dict, first_person: bool) -> str | None:
    """검사 결과에서 채팅에 안내할 한 줄 (5.12 의 안내 기준). 없으면 None"""
    kinds = {str(i.get("type")) for i in insp.get("issues") or []}
    show = [k for k in ISSUE_KO if k in CRITICAL_ALWAYS or (k in CRITICAL_NOT_FIRST_PERSON and not first_person)]
    hit = [k for k in show if k in kinds]
    if hit:
        return f"검사에서 {ISSUE_KO[hit[0]]} 보입니다. 다시 만들어 볼까요?"
    missing = [t for t in insp.get("traits") or [] if not t.get("present")]
    if missing and first_person:
        return "검사에서 보는 사람이 화면에 보이는 것처럼 보입니다. 다시 만들어 볼까요?"
    return None


class Director:
    def __init__(self, deps=None, recipe_map: dict | None = None):
        self.deps = deps or RealDeps()
        self.recipe_map = recipe_map if recipe_map is not None else recipes.load_all()
        self.tasks: dict = {}
        self.ctxs: dict = {}
        self.disabled: set = set()          # 잇단 접수 실패로 끈 레시피
        self.fail_streak: dict = {}
        self._pump_lock = asyncio.Lock()
        self._llm_state: dict = {}
        self.min_interval = settings.studio_progress_min_interval_ms / 1000

    # ── 레시피 ──
    def recipe_for(self, action: str, quality: str = "fast", sound: bool = False) -> dict | None:
        """sound=True 면 소리 포함 영상(LTX, 이미지→영상만): 높음은 증류 없는 LTX-2.3 dev, 없으면(꺼졌으면) 빠른 LTX-2.5 로 내려간다.
        아니면 소리 없는 레시피(Wan 등)"""
        if sound:
            r = recipes.pick(self.recipe_map, self.disabled, action, quality, "ltx") if quality == "high" else None
            return r or recipes.pick(self.recipe_map, self.disabled, action, "fast", "ltx")
        return recipes.pick(self.recipe_map, self.disabled, action, quality)

    def engine_of_turn(self, turn: dict | None) -> str:
        """이 요청이 쓴 영상 엔진 (이어 말하기·다시 만들기가 같은 엔진을 잇게)"""
        return recipes.engine_of(self.recipe_map.get((turn or {}).get("recipe_id") or ""))

    def quality_of_turn(self, turn: dict | None) -> str:
        """이 요청이 쓴 레시피의 화질 (이어 말하기·다시 만들기가 같은 화질을 이어 가게)"""
        return recipes.quality_of(self.recipe_map.get((turn or {}).get("recipe_id") or ""))

    # ── 화면으로 보내기 ──
    async def push(self, user_id: int, event: dict):
        try:
            await self.deps.publish(user_id, event)
        except Exception:  # noqa: BLE001 - 화면이 없어도 작업은 계속한다
            log.exception("화면 이벤트 전송 실패")

    async def emit(self, ctx: Ctx, force: bool = False):
        now = time.monotonic()
        if not force and now - ctx.last_emit < self.min_interval:
            return
        ctx.last_emit = now
        card = "pipeline" if ctx.via else ctx.action
        title, _ = progress.CARD.get(card, progress.CARD["i2v"])
        has_fix = ctx.has_fix or any(progress.PURPOSE_STAGE.get(e["purpose"]) == "fix" for e in ctx.bag)
        shown = shown_stage(ctx)
        pct_ok = ctx.stage in ("generate", "collect") and not ctx.scene_phase     # 장면 이미지 단계는 % 를 보이지 않는다
        prog = {"title": title, "stage": shown, "text": ctx.text,
                "pct": ctx.tracker.pct if pct_ok else None,
                "band": ctx.tracker.band_label() if pct_ok and ctx.stage == "generate" else None,
                "chips": progress.chips(card, has_fix, CHIP_OF_STAGE.get(shown, shown))}
        await store.save_progress(ctx.tid, prog)
        if ctx.stage != ctx.saved_stage:       # 운영자 화면이 보는 chat_turns.stage 도 단계가 바뀔 때 갱신한다
            ctx.saved_stage = ctx.stage
            await store.set_stage(ctx.tid, ctx.stage)
        await self.push(ctx.user["id"], {"type": "turn.progress", "thread_id": str(ctx.turn["thread_id"]),
                                         "turn_id": str(ctx.tid), "progress": prog})

    def schedule_emit(self, ctx: Ctx):
        """화면 갱신을 따로 돌린다. 이미 돌고 있으면 한 번만 더 돌게 표시한다"""
        t = ctx.emit_task
        if t is not None and not t.done():
            ctx.emit_again = True
            return

        async def run():
            while True:
                ctx.emit_again = False
                try:
                    await self.emit(ctx)
                except Exception:  # noqa: BLE001
                    log.exception("진행 표시 갱신 실패")
                if not ctx.emit_again:
                    return
        ctx.emit_task = asyncio.ensure_future(run())

    async def message(self, ctx_or_turn, role: str, kind: str, body: str = "", data=None):
        turn = ctx_or_turn.turn if isinstance(ctx_or_turn, Ctx) else ctx_or_turn
        m = await store.add_message(turn["thread_id"], role, kind, body, data, turn["id"])
        await store.touch_thread(turn["thread_id"])
        await self.push(turn["user_id"], {"type": "message.new", "thread_id": str(turn["thread_id"]), "message": m})
        return m

    async def finish_state(self, turn: dict, state: str, **cols):
        ok = await store.move(turn["id"], ("pending", "active", "awaiting_approval"), state, **cols)
        if ok:
            await self.push(turn["user_id"], {"type": "turn.state", "thread_id": str(turn["thread_id"]),
                                              "turn_id": str(turn["id"]), "state": state})
        return ok

    async def fail(self, turn: dict, code: str, detail=None):
        text, actions = FAIL_TEXT.get(code, FAIL_TEXT["unknown"])
        if await self.finish_state(turn, "failed", error={"code": code, "detail": detail}):
            await store.event(turn["id"], "notice", {"code": code, "text": text})
            await self.message(turn, "assistant", "notice", text, {"level": "error", "actions": actions})

    # ── 대기열 돌리기 (같은 대화는 한 번에 하나, 사용자당 동시 한도) ──
    async def pump(self):
        async with self._pump_lock:
            for t in await store.pending_turns():
                if t["id"] in self.tasks:
                    continue
                dep = t.get("depends_on")
                if dep:
                    d = await store.get_turn(dep)
                    if d and d["state"] in store.OPEN_STATES and d["state"] != "awaiting_approval":
                        continue       # 승인 대기 요청은 GPU 를 쓰지 않으니 뒤 요청을 막지 않는다
                if await store.count_active(t["user_id"]) >= settings.studio_max_active_per_user:
                    continue
                if await store.move(t["id"], "pending", "active", stage=first_stage(t.get("action"))):
                    self.spawn(t, resume=bool((t.get("work") or {}).get("approved") and (t.get("work") or {}).get("prompt")))

    def spawn(self, turn: dict, resume: bool = False):
        task = asyncio.ensure_future(self._run_guarded(turn, resume))
        self.tasks[turn["id"]] = task
        task.add_done_callback(lambda _t, tid=turn["id"]: self._done(tid))

    def _done(self, tid):
        self.tasks.pop(tid, None)
        self.ctxs.pop(tid, None)
        asyncio.ensure_future(self.pump())

    async def cancel(self, turn: dict) -> bool:
        """사용자 취소. 아직 시작 전이면 바로 끝내고, 돌고 있으면 작업을 취소해 정리하게 한다"""
        if turn.get("state") == "awaiting_approval":
            if await self._cancel_awaiting(turn):
                await self._plan_status(turn, "cancelled")
                return True
            turn = await store.get_turn(turn["id"]) or turn      # 그 사이 승인됐다: 아래 일반 취소로 처리한다
        await store.request_cancel(turn["id"])
        task = self.tasks.get(turn["id"])
        ctx = self.ctxs.get(turn["id"])
        if task is None:
            return await self.finish_state(turn, "cancelled")
        if ctx is not None:
            ctx.cancel_reason = ctx.cancel_reason or "user"
        task.cancel()
        return True

    # ── 한 요청 ──
    async def _run_guarded(self, turn: dict, resume: bool):
        try:
            await self._run(turn, resume)
        except asyncio.CancelledError:
            ctx = self.ctxs.get(turn["id"])
            await asyncio.shield(self._on_cancel(turn, ctx))
        except StepError as e:
            await asyncio.shield(self._on_step_error(turn, e))
        except Exception as e:  # noqa: BLE001
            log.exception("채팅 요청 %s 실패", turn["id"])
            ctx = self.ctxs.get(turn["id"])
            if ctx and ctx.job_id:     # 접수한 GPU 작업을 고아로 두지 않는다
                await asyncio.shield(self._stop_job(ctx))
            if ctx:
                self._release(ctx)
            await asyncio.shield(self.fail(turn, "unknown", str(e)[:300]))

    async def _on_step_error(self, turn, e: StepError):
        ctx = self.ctxs.get(turn["id"])
        if ctx:
            self._release(ctx)
        if ctx and ctx.job_id and e.code not in ("run_error", "run_oom", "gpu_prepare", "lost", "no_output", "admin_cancelled"):
            await self._stop_job(ctx)
        await self.fail(turn, e.code, e.detail)

    def _release(self, ctx: Ctx):
        """진행 수신기를 푼다 (끝난 요청의 늦은 메시지가 진행 상태를 덮어쓰지 않게)"""
        if ctx.client_id and ctx.receiver is not None:
            try:
                self.deps.unregister(ctx.env["id"], ctx.client_id, ctx.receiver)
            except Exception:  # noqa: BLE001
                log.exception("수신기 해제 실패")
            ctx.receiver = None

    async def _stop_job(self, ctx: Ctx):
        """접수한 작업을 멈춘다: 대기 중이면 취소, 아니면 중단 (5.9)"""
        try:
            if await self.deps.cancel_queued(ctx.env["id"], [ctx.job_id]) == 0:
                await self.deps.interrupt(ctx.env["id"], ctx.job_id)
        except Exception:  # noqa: BLE001
            log.exception("작업 중단 실패")

    async def _on_cancel(self, turn, ctx):
        reason = ctx.cancel_reason if ctx else None
        if reason is None and not await store.cancel_requested(turn["id"]):
            return       # 허브가 종료되며 태스크가 취소된 것이다. 요청은 active 로 남겨 두어 다음 시작 때 이어 간다 (5.10)
        reason = reason or "user"
        if ctx:
            self._release(ctx)
            if ctx.job_id:
                await self._stop_job(ctx)
        if reason == "deadline":
            await self.fail(turn, "queue_wait")
            return
        await store.event(turn["id"], "cancel", {"reason": reason})
        await self.finish_state(turn, "cancelled")

    async def _run(self, turn: dict, resume: bool):
        user = await store.get_user(turn["user_id"])
        env = await store.studio_env(settings.studio_env_slug)
        recipe = self.recipe_map.get(turn.get("recipe_id") or "")
        if not user or not env or not recipe or recipe["id"] in self.disabled:
            raise StepError("prepare_down")
        ctx = Ctx(turn, user, env, recipe)
        self.ctxs[turn["id"]] = ctx
        req = turn.get("request") or {}
        ctx.req_text = req.get("text") or intent.DEFAULT_I2V_TEXT
        ctx.spec = req.get("spec") or {}
        ctx.notes = list(req.get("notes") or [])
        ctx.first_person = self.deps.pov(ctx.req_text)
        ctx.via = bool(req.get("via_image")) and ctx.action == "i2v"
        if ctx.via:
            ctx.stage = "prompt"
        ctx.tracker = progress.Tracker(recipe["progress"]["bands"])
        await store.event(turn["id"], "turn_created", {"chars": len(ctx.req_text), "attachments": len(req.get("attachment_ids") or []),
                                                       "default_text": bool(req.get("used_default"))})
        await store.event(turn["id"], "spec", ctx.spec)
        work = dict(turn.get("work") or {})
        ctx.motion = bool(work.get("scene_att"))
        if resume and work.get("pending") is not None and not ctx.job_id:
            found = await store.job_by_client(env["id"], work["pending"]["client_id"])
            if found:
                ctx.job_id = found["prompt_id"]
        if ctx.job_id is None and resume:
            ctx.job_id = await store.turn_job(turn["id"], "generate")
        if ctx.job_id is None:
            if ctx.via:       # 장면 이미지를 먼저 만든다 (계획 카드는 그 앞에서 한 번만)
                work = await self._pipeline_front(ctx, work)
                if work is None:
                    return          # 계획 카드를 띄웠거나 취소됐다
            if not (resume and work.get("prompt")):      # 허브 재시작 뒤 자동 재개: 프롬프트가 이미 끝났으면 접수부터 (5.10)
                if ctx.action == "image":
                    work = await self._write_image_prompt(ctx, work)
                elif ctx.action == "edit":
                    work = await self._write_edit_prompt(ctx, work)
                else:
                    work = {**work, **await self._write_prompt(ctx, work)}
            if not work.get("approved") and await self._needs_plan(ctx, work):
                await self._park_for_approval(ctx, work)
                return
            ctx.job_id = await self._submit(ctx, work)
        try:
            await self._watch_job(ctx, work)
        except StepError as e:
            # 실행 공간이 사라져 결과도 없으면 같은 값으로 한 번만 다시 접수한다 (5.10, P2)
            if e.code != "lost" or work.get("lost_retried") or not work.get("prompt"):
                raise
            work["lost_retried"] = True
            await store.merge_work(turn["id"], {"lost_retried": True})
            await store.event(turn["id"], "auto_resubmit", {"reason": "lost", "old_job": str(ctx.job_id)})
            self._release(ctx)
            ctx.job_id = None
            ctx.success_signal = False
            ctx.tracker = progress.Tracker(recipe["progress"]["bands"])
            ctx.job_id = await self._submit(ctx, work)
            await self._watch_job(ctx, work)

    # ── 글→영상: 장면 이미지를 먼저 만들고 그 이미지로 영상을 만든다 (사용자에게는 한 작업) ──
    SCENE_HINT = ("(Make this the first instant of the scene as one still photograph, not a sequence. If someone or something moves, "
                  "show it already in motion, slightly off-center with open space ahead of its direction.)")
    SCENE_HINT_POV = (" The view is first-person: no head or shoulders of the viewer; if the viewer is operating something, "
                      "only a small pair of hands at the bottom edge, and the whole interior and the view ahead in clear detail.")

    def _frame_ids(self, ctx: Ctx, work: dict) -> list:
        """첫 화면이 될 사진 첨부. 글→영상은 먼저 만든 장면 이미지(사용자에게 보이지 않는 첨부)다"""
        if work.get("scene_att"):
            return [work["scene_att"]]
        return list((ctx.turn.get("request") or {}).get("attachment_ids") or [])

    async def _pipeline_front(self, ctx: Ctx, work: dict) -> dict | None:
        """장면 이미지 단계. 취소됐으면 None. 끝나면 work 에 scene_att 가 있다. 계획 카드는 영상 프롬프트까지 쓴 뒤에 뜬다
        (예전 글→영상과 같은 카드: 요약·프롬프트·길이·예상 시간). 장면 이미지는 빠름 8스텝이라 승인 전에 만들어 둔다"""
        turn = ctx.turn
        req = turn.get("request") or {}
        img_recipe = self.recipe_for("image")
        if img_recipe is None:
            raise StepError("prepare_down")
        if work.get("scene_att"):
            work.pop("size", None)       # 다시 이어받은 요청: 영상 크기는 장면 이미지에서 정한다
            return work
        if not work.get("scene_prompt"):
            reused = await self._reuse_prompt(ctx)      # 다시 만들기: 저장된 장면 프롬프트로 시드만 새로
            if reused and reused.get("scene_prompt"):
                work = {**work, **reused}
            else:
                with self.deps.llm_context() as bag:
                    ctx.bag = bag
                    ctx.stage = "prompt"
                    await self.emit(ctx, True)
                    hint = self.SCENE_HINT + (self.SCENE_HINT_POV if ctx.first_person else "")
                    r = await self._guard_llm(ctx, self.deps.expand_image(
                        ctx.user, turn.get("client_ip") and str(turn["client_ip"]) or "", ctx.req_text + "\n" + hint))
                    for e in bag:
                        await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
                await store.event(turn["id"], "expand", {"crew": r.get("crew"), "violations": r.get("violations"),
                                                          "rounds": r.get("rounds"), "advice": r.get("advice"),
                                                          "people": r.get("people"), "scene": True})
                if not (r.get("prompt") or "").strip():
                    raise StepError("llm_error", "장면 프롬프트를 만들지 못했습니다")
                work = {**work, "scene_prompt": r["prompt"], "crew": (r.get("crew") or {}).get("category")}
                await store.merge_work(turn["id"], {"scene_prompt": r["prompt"], "crew": work.get("crew")})
        # 장면 이미지 크기: 영상이 같은 비율이 되도록 영상 크기의 비율로, 이미지 레시피의 면적으로
        vsz = recipes.default_size(ctx.recipe, aspect=(ctx.spec or {}).get("aspect"))
        ssz = list(recipes.default_size(img_recipe, photo=(vsz[0], vsz[1])))
        sctx = Ctx(turn, ctx.user, ctx.env, img_recipe)
        sctx.via, sctx.scene_phase, sctx.job_step, sctx.pend_key = True, True, "scene", "scene_pending"
        sctx.spec, sctx.notes, sctx.first_person, sctx.req_text = ctx.spec, ctx.notes, ctx.first_person, ctx.req_text
        sctx.stage, sctx.text = "queue", "장면 준비 중"
        sctx.label = "장면 준비"
        swork = {"prompt": work["scene_prompt"], "size": ssz, "scene_pending": work.get("scene_pending")}
        self.ctxs[turn["id"]] = sctx           # 취소는 지금 돌고 있는 작업을 멈춘다
        try:
            await store.event(turn["id"], "scene_start", {"size": ssz, "recipe": img_recipe["id"]})
            found = None       # 재시작 전에 접수한 장면 작업이 있으면 다시 접수하지 않고 이어서 본다
            prev = (work.get("scene_pending") or {}).get("client_id")
            if prev:
                found = await store.job_by_client(ctx.env["id"], prev)
            if found:
                sctx.job_id = found["prompt_id"]
                await store.event(turn["id"], "scene_resumed", {"job_id": str(sctx.job_id)})
            else:
                sctx.job_id = await self._submit(sctx, swork)
            main = await self._watch_job(sctx, swork)
            if main is None:
                return None                    # 취소됐다 (_collect 가 상태를 끝냈다)
            sctx.close_nodes()
            if sctx.node_log:
                await store.event(turn["id"], "scene_node_timing", {"nodes": sctx.node_log[:200]})
            att = await self.deps.stage_image(ctx.env, ctx.user, turn, main["rel_path"])
        finally:
            self._release(sctx)      # 실패·취소면 ctxs 가 sctx 로 남아 정리가 장면 작업을 멈춘다
        self.ctxs[turn["id"]] = ctx
        work["scene_att"] = att["id"]
        work.pop("size", None)                 # 영상 크기는 장면 이미지 크기에서 새로 정한다
        await store.merge_work(turn["id"], {"scene_att": att["id"], "scene_size": ssz})
        await store.event(turn["id"], "scene_done", {"attachment": att["id"], "width": att.get("width"), "height": att.get("height")})
        ctx.motion = True
        ctx.stage = "frame"
        return work

    # ── 프롬프트 만들기 (LLM 차례) ──
    async def _reuse_prompt(self, ctx: Ctx) -> dict | None:
        """다시 만들기: 저장된 프롬프트·설정으로 시드만 새로 뽑는다 (LLM 차례 없음)"""
        turn = ctx.turn
        req = turn.get("request") or {}
        if req.get("mode") == "new_seed" and req.get("source_turn"):
            src = await store.get_turn(req["source_turn"], turn["user_id"])
            sw = (src or {}).get("work") or {}
            new_sec = (req.get("spec") or {}).get("seconds")
            old_sec = ((src or {}).get("request") or {}).get("spec", {}).get("seconds") if src else None
            if sw.get("prompt") and new_sec and new_sec != old_sec and ctx.action in ("i2v", "t2v"):
                await store.event(turn["id"], "reuse_skipped", {"reason": "길이가 바뀌어 프롬프트를 새로 씁니다",
                                                                 "old": old_sec, "new": new_sec})
                return None
            if sw.get("prompt"):
                work = {k: sw[k] for k in ("prompt", "parts", "ko", "profile", "length_choice", "settings", "size",
                                           "frame_known", "crew", "scene_prompt") if k in sw}
                if ctx.via:
                    # 장면 이미지는 새로 만드니, 영상 프롬프트도 그 이미지를 보고 새로 쓴다 (옛 이미지의 세부가 남지 않게)
                    work = {k: v for k, v in work.items() if k in ("scene_prompt", "crew")}
                new_aspect = (req.get("spec") or {}).get("aspect")
                if new_aspect and new_aspect != ((src.get("request") or {}).get("spec") or {}).get("aspect"):
                    work.pop("size", None)      # 다른 비율을 말했으면 크기를 새로 정한다
                ctx.reused = True
                await store.merge_work(turn["id"], work)
                await store.event(turn["id"], "step_done", {"stage": "prompt", "reused_from": str(req["source_turn"])})
                return work
        return None

    # ── Ask 계획 카드 (5.7) ──
    async def estimate_seconds(self, ctx: Ctx) -> int:
        """완료까지 예상 시간(초) [추정]: 내 앞 줄마다 종류별 중앙값 + 내 ComfyUI 작업 (8.5)"""
        try:
            med = await self.deps.medians() or {}
        except Exception:  # noqa: BLE001
            med = {}
        est = ctx.recipe.get("estimate") or {}
        own = float(est.get("gpu_seconds") or med.get("comfy") or 60)
        frames = ((ctx.turn.get("work") or {}).get("settings") or {}).get("frames")
        if frames:
            # 짧은 영상은 덜 걸린다 [추정]. Wan 은 81프레임이 기준이라 더 늘지 않는다. LTX 는 기준 프레임과 지수가 레시피에 있다(실측)
            ref = float(est.get("ref_frames") or 81)
            exp = float(est.get("exponent") or 1.0)
            own *= max(0.4, min(8.0 if est.get("ref_frames") else 1.0, (float(frames) / ref) ** exp))
        comfy = med.get("comfy") or own
        llm = med.get("llm") or 30
        q = await self.deps.queue()
        ahead_c = sum(1 for j in q if j.get("kind") == "comfy")
        ahead_l = sum(1 for j in q if j.get("kind") == "llm")
        return int(ahead_c * comfy + ahead_l * llm + own)

    async def _needs_plan(self, ctx: Ctx, work: dict) -> bool:
        """긴 영상은 먼저 계획을 보여 준다: 대화 설정이 ask_long 이고 영상이며 예상 시간이 기준 이상 (다시 만들기는 건너뜀)"""
        high = recipes.quality_of(ctx.recipe) == "high"
        if ctx.reused or not (ctx.action in ("i2v", "t2v") or (high and ctx.action == "edit")):
            return False
        if high:
            return True      # 화질 높음은 시간이 길어 대화 설정과 상관없이 늘 예상 시간을 보이고 승인을 받는다
        try:       # 예상 시간을 못 구해도 요청이 죽지 않게 한다: 계획 없이 진행한다
            if (await store.thread_settings(ctx.turn["thread_id"])).get("ask") != "ask_long":
                return False
            return await self.estimate_seconds(ctx) >= settings.studio_ask_long_seconds
        except Exception:  # noqa: BLE001
            log.exception("계획 카드 판단 실패 (계획 없이 진행)")
            return False

    async def _park_for_approval(self, ctx: Ctx, work: dict):
        """계획 카드를 보내고 승인을 기다리는 상태로 둔다. 감독 작업은 여기서 끝난다 (GPU 차례를 갖지 않는다)"""
        turn, recipe = ctx.turn, ctx.recipe
        req = turn.get("request") or {}
        atts = await store.attachments_of(req.get("attachment_ids") or [])
        photo = next(((a.get("width"), a.get("height")) for a in atts if a.get("width") and a.get("height")), None)
        size = work.get("size")
        if "width" in recipe["inject"] and not size:
            size = list(recipes.default_size(recipe, photo=photo, aspect=(ctx.spec or {}).get("aspect")))
            work["size"] = size
            await store.merge_work(turn["id"], {"size": size})
        st = work.get("settings") or {}
        lc = work.get("length_choice") or {}
        plan = {"action": "t2v" if ctx.via else ctx.action, "attachment_ids": list(req.get("attachment_ids") or [])[:1],
                "seconds": st.get("seconds"), "frames": st.get("frames") or recipe["defaults"].get("frames"),
                "fps": recipe["defaults"].get("fps"), "size": size, "quality": recipe.get("quality_label"),
                "words": len(str(work.get("prompt") or "").split()),
                "length_reason": lc.get("reason") or ("첫 화면을 읽지 못해 움직임만 썼습니다" if work.get("frame_known") is False else ""),
                "ko": work.get("ko") or {}, "estimate_s": await self.estimate_seconds(ctx),
                "recipe_version": recipe["version"], "ttl_hours": settings.studio_plan_ttl_hours}
        old_mid = (turn.get("work") or {}).get("plan_message_id")
        if old_mid:       # 카드를 만든 뒤 상태를 바꾸기 전에 멈췄던 요청이다: 낡은 카드는 접는다
            await store.set_message_data(old_mid, {"status": "replanned", "actions": []})
        m = await self.message(turn, "assistant", "plan", "", {"plan": plan, "status": "pending", "actions": PLAN_ACTIONS})
        plan["message_id"] = m["id"]
        await store.merge_work(turn["id"], {"plan_message_id": m["id"]})
        ctx.stage = "plan"
        await store.event(turn["id"], "plan", {k: plan[k] for k in ("seconds", "frames", "size", "estimate_s")})
        if not await self.finish_state(turn, "awaiting_approval", stage="plan", plan=plan):
            await store.set_message_data(m["id"], {"status": "cancelled"})   # 그 사이 취소됐다
            return

    async def _plan_status(self, turn: dict, status: str):
        """계획 카드의 상태(승인함·취소됨·만료·다시 계획)를 메시지에 남기고 화면에 알린다"""
        mid = (turn.get("plan") or {}).get("message_id")
        if not mid:
            return
        try:
            await store.set_message_data(mid, {"status": status, "actions": []})
            await self.push(turn["user_id"], {"type": "message.update", "thread_id": str(turn["thread_id"]),
                                              "message_id": mid, "data": {"status": status, "actions": []}})
        except Exception:  # noqa: BLE001
            log.exception("계획 카드 상태 갱신 실패")

    async def _cancel_awaiting(self, turn: dict) -> bool:
        """승인 대기 상태에서만 취소로 바꾼다. 그 사이 승인돼 돌고 있는 요청은 건드리지 않는다"""
        if not await store.move(turn["id"], "awaiting_approval", "cancelled"):
            return False
        await self.push(turn["user_id"], {"type": "turn.state", "thread_id": str(turn["thread_id"]),
                                          "turn_id": str(turn["id"]), "state": "cancelled"})
        return True

    async def approve(self, turn: dict) -> str:
        """[승인]. 'ok' 접수 진행 / 'replan' 레시피 판이 바뀌어 계획을 다시 만듦 / 'conflict' 이미 처리됨 / 'gone' 레시피 없음.
        상태 바꾸기가 조건부(awaiting_approval → pending, work.approved 를 같은 UPDATE 로)라 두 번 눌러도 한 번만 진행하고,
        시작은 pump 가 사용자 활성 한도를 지켜 한다 (5.2)"""
        turn = dict(turn)           # 상태를 바꾸기 전의 값(plan.message_id)으로 카드 상태를 갱신한다
        # 계획한 레시피(화질 포함)로 판단한다. 꺼졌으면 같은 화질의 다른 레시피를 찾는다
        recipe = self.recipe_map.get(turn.get("recipe_id") or "")
        if recipe is None or recipe["id"] in self.disabled:
            via = bool((turn.get("request") or {}).get("via_image"))
            act = "i2v" if via else (turn.get("action") or "i2v")
            recipe = (self.recipe_for(act, self.quality_of_turn(turn), self.engine_of_turn(turn) == "ltx")
                      or self.recipe_for(act, self.quality_of_turn(turn)))        # 소리 포함 레시피가 꺼졌으면 소리 없는 레시피로 잇는다
        if recipe is None:
            if await self._cancel_awaiting(turn):
                await self._plan_status(turn, "cancelled")
                await store.event(turn["id"], "plan_dropped", {"reason": "레시피 없음"})
                return "gone"
            return "conflict"
        stale = recipe["version"] != turn.get("recipe_version") or recipe["id"] != turn.get("recipe_id")
        if stale:
            if not await store.replan_turn(turn["id"], first_stage(turn.get("action")), recipe["id"], recipe["version"]):
                return "conflict"
            await self._plan_status(turn, "replanned")
            if recipes.quality_of(recipe) != self.quality_of_turn(turn):      # 계획한 높음 레시피가 꺼져 낮은 화질로 잇는다: 말없이 바꾸지 않는다
                try:
                    await self.message(turn, "assistant", "notice", "높음 레시피를 지금 쓸 수 없어 기본 화질로 만듭니다.", {"level": "warn"})
                except Exception:  # noqa: BLE001
                    log.exception("화질 변경 안내 실패")
            asyncio.ensure_future(self.pump())
            return "replan"
        if not await store.approve_turn(turn["id"], "queue"):
            return "conflict"
        await self._plan_status(turn, "approved")
        asyncio.ensure_future(self.pump())
        return "ok"

    async def supersede(self, turn: dict) -> bool:
        """승인 대기 중 같은 대화에 새 메시지가 오면 그 계획은 접고 새 요청이 다시 계획한다 (5.7). 이미 승인돼 돌면 False"""
        if not await self._cancel_awaiting(turn):
            return False
        await self._plan_status(turn, "replanned")
        return True

    async def expire_plans(self) -> int:
        """승인 없이 STUDIO_PLAN_TTL_HOURS 가 지난 계획을 취소한다 (승인돼 돌고 있는 요청은 건드리지 않는다)"""
        n = 0
        for t in await store.expired_plans(settings.studio_plan_ttl_hours):
            if await self._cancel_awaiting(t):
                await self._plan_status(t, "expired")
                n += 1
        return n

    async def _write_image_prompt(self, ctx: Ctx, work: dict) -> dict:
        """이미지: 제작진 초안 + 확장을 LLM 차례 한 번에 (5.5). 27B 한 번이라 첫 화면 읽기는 없다"""
        turn = ctx.turn
        reused = await self._reuse_prompt(ctx)
        if reused:
            return reused
        with self.deps.llm_context() as bag:
            ctx.bag = bag
            ctx.stage = "prompt"
            await self.emit(ctx, True)
            r = await self._guard_llm(ctx, self.deps.expand_image(
                ctx.user, turn.get("client_ip") and str(turn["client_ip"]) or "", ctx.req_text))
            for e in bag:
                await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
        await store.event(turn["id"], "expand", {"crew": r.get("crew"), "violations": r.get("violations"),
                                                  "rounds": r.get("rounds"), "advice": r.get("advice"),
                                                  "people": r.get("people")})
        work = {"prompt": r["prompt"], "crew": (r.get("crew") or {}).get("category")}
        await store.merge_work(turn["id"], work)
        return work

    async def _write_edit_prompt(self, ctx: Ctx, work: dict) -> dict:
        """편집: 영문 편집 지시문을 LLM 차례 한 번에 쓴다 (5.5)"""
        turn = ctx.turn
        reused = await self._reuse_prompt(ctx)
        if reused:
            return reused
        with self.deps.llm_context() as bag:
            ctx.bag = bag
            ctx.stage = "prompt"
            await self.emit(ctx, True)
            r = await self._guard_llm(ctx, self.deps.write_edit(
                ctx.user, turn.get("client_ip") and str(turn["client_ip"]) or "", ctx.req_text))
            for e in bag:
                await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
        await store.event(turn["id"], "edit_prompt", {"violations": r.get("violations"), "rounds": r.get("rounds")})
        if not (r.get("prompt") or "").strip():
            raise StepError("llm_error", "편집 지시문을 만들지 못했습니다")
        work = {"prompt": r["prompt"]}
        await store.merge_work(turn["id"], work)
        return work

    async def _write_prompt(self, ctx: Ctx, work: dict) -> dict:
        turn = ctx.turn
        req = turn.get("request") or {}
        reused = None if ctx.via else await self._reuse_prompt(ctx)      # 장면 이미지 경유는 새 이미지를 보고 새로 쓴다
        if reused:
            return reused
        image = None
        if ctx.action == "i2v":
            atts = await store.attachments_of(self._frame_ids(ctx, work))
            img = next((a for a in atts if a["media"] == "image" and a.get("input_rel")), None)
            if img is None:
                raise StepError("no_image")
            image = {"filename": img["input_rel"].split("/")[-1],
                     "subfolder": "/".join(img["input_rel"].split("/")[:-1]), "type": "input"}
        reuse_frame = None
        if req.get("reuse_frame_of"):       # 영상 고치기: 앞 요청이 읽어 둔 첫 화면을 다시 쓴다 (8B 차례를 아낀다)
            src = await store.get_turn(req["reuse_frame_of"], turn["user_id"])
            fr = ((src or {}).get("work") or {}).get("frame")
            reuse_frame = fr if fr and self.deps.frame_known(fr) else None     # 읽기에 실패한 빈 결과는 다시 읽는다
        seconds = (ctx.spec or {}).get("seconds")
        used_default = bool(req.get("used_default"))
        recipe = ctx.recipe
        with self.deps.llm_context() as bag:
            ctx.bag = bag
            ctx.stage = "frame" if image is not None and reuse_frame is None else "prompt"
            await self.emit(ctx, True)
            for attempt in (1, 2):
                r = await self._guard_llm(ctx, self.deps.render(
                    ctx.user, turn.get("client_ip") and str(turn["client_ip"]) or "", ctx.env, text=ctx.req_text, notes="",
                    style="", profile_id=recipe["video_profile"], seconds=seconds, image=image,
                    image_prompt=work.get("scene_prompt") or "",
                    crew=False if used_default else None, read_frame_on=True, auto_length=True,
                    frame=reuse_frame if attempt == 1 else (reuse_frame or r.get("frame"))))
                f = None
                if needs_fix(r.get("violations")):
                    ctx.has_fix = True
                    ctx.stage = "fix"
                    await self.emit(ctx, True)
                    f = await self._guard_llm(ctx, self.deps.fix(
                        ctx.user, turn.get("client_ip") and str(turn["client_ip"]) or "", ctx.env, text=ctx.req_text,
                        notes="", style="", profile_id=r["profile"], seconds=r["settings"]["seconds"],
                        current={"ko": r["ko"], "parts": r["parts"], "motion": r["motion"]}, frame=r["frame"],
                        scene=r.get("scene") or ""))
                left = (f if f is not None else r).get("violations") or []
                blocking = [v for v in left if BLOCKING_RE.search(v)]
                if not blocking:
                    break
                # 요청과 다른 장면의 프롬프트로 GPU 를 쓰지 않는다: 한 번 더 새로 쓰고, 그래도 같으면 실패로 끝낸다
                await store.event(turn["id"], "prompt_rejected", {"attempt": attempt, "violations": blocking})
                ctx.stage = "prompt"
                await self.emit(ctx, True)
            else:
                for e in bag:
                    await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
                raise StepError("bad_prompt", "; ".join(blocking)[:300])
            for e in bag:
                await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
        final = f["prompt"] if f and f.get("changed") else r["prompt"]
        if r.get("sound") and r["sound"] not in final:
            final = f"{final}\n\n{r['sound']}"
        frame_ok = self.deps.frame_known(r.get("frame"))
        await store.event(turn["id"], "render", {
            "profile": r.get("profile"), "length_choice": r.get("length_choice"), "crew": r.get("crew"),
            "violations": r.get("violations"), "rounds": r.get("rounds"), "frame_read": frame_ok,
            "crew_skipped": "기본 요청문이라 제작진 생략" if used_default else None})
        if f:
            await store.event(turn["id"], "fix", {k: f.get(k) for k in ("before_ko", "violations", "words_before",
                                                                        "words_after", "rounds", "changed", "fixed")})
        work = {"prompt": final, "parts": (f or r).get("parts"), "ko": {**(r.get("ko") or {}), **((f or {}).get("ko") or {})}, "profile": r.get("profile"),
                "length_choice": r.get("length_choice"), "settings": r.get("settings"), "frame_known": frame_ok,
                "frame": r.get("frame")}
        await store.merge_work(turn["id"], work)
        return work

    async def _guard_llm(self, ctx: Ctx, coro):
        """LLM 차례 안의 진행 표시와 기한. 감독이 취소하면 한 번만 취소한다 (이유를 남긴다)"""
        task = asyncio.ensure_future(coro)
        waited = 0.0
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=LLM_POLL)
                if task.done():
                    break
                await self._tick_llm(ctx)
                paused = await self.deps.setting("paused") or await self.deps.setting("blocked")
                running = any(self._llm_state.get(e["job_id"]) == "running" for e in ctx.bag)
                if not paused:
                    waited += LLM_POLL
                if not running and waited > settings.studio_llm_wait_seconds:
                    ctx.cancel_reason = "deadline"
                    task.cancel()
                    break
            return await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            code = {"PromptxError": "llm_error"}.get(name, "unknown")
            status = getattr(e, "status", None)
            if name == "PromptxError" and status == 503:
                code = "queue_wait"
            raise StepError(code, str(e)[:300]) from None

    async def _tick_llm(self, ctx: Ctx):
        """감독이 기다리는 LLM 줄의 상태로 단계 글을 정한다 (8.4)"""
        if not ctx.bag:
            return
        queue = await self.deps.queue()
        for e in ctx.bag:
            jid = e["job_id"]
            stage = progress.PURPOSE_STAGE.get(e["purpose"])
            if jid not in ctx.linked and stage:
                await store.link_job(ctx.tid, jid, stage, e["purpose"])
                ctx.linked.add(jid)
        cur = ctx.bag[-1]
        stage = progress.PURPOSE_STAGE.get(cur["purpose"], ctx.stage)
        st = await store.job_state(cur["job_id"])
        state = (st or {}).get("state")
        self._llm_state[cur["job_id"]] = state
        ctx.stage = stage
        shown = shown_stage(ctx)
        if state == "queued":
            pos = progress.queue_position(queue, cur["job_id"])
            ahead = any(j.get("state") not in ("queued",) for j in queue if str(j.get("id")) != str(cur["job_id"]))
            ctx.text = progress.wait_text(pos, ahead, progress.CHIP_LABEL.get(shown, "프롬프트 작성"))
        else:
            ctx.text = progress.STAGE_TEXT.get((shown, "running"), "작업 중")
        if await self.deps.setting("paused"):
            ctx.text = "점검 중이라 새 작업이 잠시 멈췄습니다"
        elif await self.deps.setting("blocked"):
            ctx.text = "GPU 대기열이 멈췄습니다. 관리자가 확인 중입니다"
        await self.emit(ctx, True)

    # ── 접수 ──
    async def _submit(self, ctx: Ctx, work: dict) -> str:
        turn, recipe, env = ctx.turn, ctx.recipe, ctx.env
        req = turn.get("request") or {}
        inject = recipe["inject"]
        img = None
        img_slot = "first_frame" if "first_frame" in inject else ("edit_image" if "edit_image" in inject else None)
        if img_slot:
            atts = await store.attachments_of(self._frame_ids(ctx, work))
            img = next((a for a in atts if a["media"] == "image" and a.get("input_rel")), None)
            if img is None:
                raise StepError("no_image")
        settings_ = work.get("settings") or {}
        d = recipe["defaults"]
        frames = int(settings_.get("frames") or d.get("frames") or 0) or None
        photo = (img.get("width"), img.get("height")) if img and img.get("width") and img.get("height") else None
        size = work.get("size")
        if "width" not in inject:
            size = []        # 편집은 입력 사진 크기를 따른다 (크기 자리가 없다)
        elif not size:
            size = list(recipes.default_size(recipe, photo=photo, aspect=(ctx.spec or {}).get("aspect")))
        seed = random.randint(0, inject["seed"].get("max", 2 ** 32))
        attempt = int((work.get(ctx.pend_key) or {}).get("attempt", 0)) + 1
        client_id = f"studio-{turn['id']}-{'s' if ctx.scene_phase else 'g'}{attempt}"     # 장면 이미지(s)와 영상(g)은 이름이 겹치지 않는다
        prefix_slots = recipes.slots_of(recipe, "save_prefix")
        values = {"prompt": work["prompt"], "seed": seed,
                  "save_prefix": [recipes.prefix_value(s, env_user(ctx), ctx.user["id"], str(turn["id"])) for s in prefix_slots]}
        if size:
            values["width"], values["height"] = size[0], size[1]
        if img is not None:
            values[img_slot] = img["input_rel"]
        if "frames" in inject:
            values["frames"] = frames
        if "seconds" in inject:      # LTX: 길이를 초로 받는다 (프레임은 초 × fps + 1)
            sl = inject["seconds"]
            values["seconds"] = max(int(sl.get("min", 2)), min(int(sl.get("max", 20)),
                                                               int(round(((frames or 121) - 1) / int(d.get("fps") or 24)))))
        if "check_index" in inject:
            values["check_index"] = max(0, min((frames or 0) // 2, int(inject["check_index"].get("max", 80))))
        try:
            prompt = recipes.build(recipe, values)
        except recipes.RecipeError as e:
            raise StepError("submit", f"레시피 값 오류: {e}") from None
        problem = negative_problem(recipe, prompt)
        await store.event(turn["id"], "negative_check", {"source": recipe["negative_source"], "ok": problem is None,
                                                         "problem": problem})
        if problem:
            self._note_fail(recipe)
            raise StepError("submit", problem)
        work["size"], work["seed"] = list(size), seed
        pend = {"client_id": client_id, "attempt": attempt}
        work[ctx.pend_key] = pend
        await store.merge_work(turn["id"], {"size": list(size), "seed": seed, ctx.pend_key: pend})
        ctx.stage = "queue"
        ctx.text = "대기 중"
        body = {"prompt": prompt, "client_id": client_id,
                "extra_data": {"studio": {"turn": str(turn["id"]), "recipe": recipe["id"], "version": recipe["version"]}}}
        receiver = Receiver(self, ctx)
        ctx.receiver = receiver
        self.deps.register(env["id"], client_id, receiver)
        ctx.client_id = client_id
        ip = str(turn.get("client_ip") or "")
        sub = asyncio.ensure_future(self.deps.submit(env, ctx.user, body, ip))
        try:
            res = await asyncio.shield(sub)
        except asyncio.CancelledError:
            try:
                res = await sub
            except Exception:  # noqa: BLE001
                res = await store.job_by_client(env["id"], client_id)
                if res is None:
                    self.deps.unregister(env["id"], client_id, receiver)
                    raise asyncio.CancelledError from None
            ctx.job_id = res["prompt_id"]
            await store.link_job(turn["id"], res["prompt_id"], ctx.job_step, recipe["id"], attempt)
            raise
        except Exception as e:  # noqa: BLE001
            res = await store.job_by_client(env["id"], client_id)
            if res is None:
                self.deps.unregister(env["id"], client_id, receiver)
                code = "prepare_down" if type(e).__name__ in ("SubmitError", "HTTPError", "ConnectError", "ReadTimeout") \
                    and getattr(e, "status", 503) == 503 else "submit"
                if code == "submit":
                    self._note_fail(recipe)
                await store.event(turn["id"], "error", {"where": "submit", "type": type(e).__name__, "detail": str(e)[:500]})
                raise StepError(code, str(e)[:300]) from None
        self.fail_streak[recipe["id"]] = 0
        ctx.job_id = res["prompt_id"]       # 기록이 실패해도 이 작업을 멈출 수 있게 먼저 쥔다
        try:
            await store.link_job(turn["id"], res["prompt_id"], ctx.job_step, recipe["id"], attempt)
            await store.event(turn["id"], "comfy_submitted", {"job_id": res["prompt_id"], "seq": res.get("number"),
                                                              "client_id": client_id, "recipe": recipe["id"],
                                                              "version": recipe["version"], "seed": seed,
                                                              "size": list(size), "frames": frames,
                                                              "first_frame": str(img["id"]) if img else None})
        except Exception:  # noqa: BLE001
            log.exception("접수 기록 실패 (작업은 접수됨)")
        tracker = ctx.tracker
        tracker.prompt_id = res["prompt_id"]
        return res["prompt_id"]

    def _note_fail(self, recipe: dict):
        n = self.fail_streak.get(recipe["id"], 0) + 1
        self.fail_streak[recipe["id"]] = n
        if n >= settings.studio_recipe_fail_limit:
            self.disabled.add(recipe["id"])
            log.warning("레시피 %s 를 잇단 접수 실패로 껐습니다 (허브를 다시 시작하면 시작 검사 뒤 켜집니다)", recipe["id"])

    # ── 작업 감시와 결과 ──
    async def _watch_job(self, ctx: Ctx, work: dict):
        turn, recipe, env = ctx.turn, ctx.recipe, ctx.env
        if ctx.job_id and ctx.receiver is None:       # 허브 재시작 뒤 이어받음
            ctx.client_id = (work.get(ctx.pend_key) or {}).get("client_id")
            if ctx.client_id:
                ctx.receiver = Receiver(self, ctx)
                self.deps.register(env["id"], ctx.client_id, ctx.receiver)
            ctx.tracker.prompt_id = ctx.job_id
        max_run = int(recipe["limits"]["max_run_seconds"])
        collect_since = None
        try:
            while True:
                st = await self._job_state(ctx.job_id)
                if st is None:
                    raise StepError("lost")
                state = st["state"]
                if await store.cancel_requested(turn["id"]) and state in ("queued", "starting", "running") \
                        and ctx.cancel_reason is None:
                    ctx.cancel_reason = "user"
                    raise asyncio.CancelledError
                if state == "queued":
                    ctx.stage = "queue"
                    queue = await self.deps.queue()
                    pos = progress.queue_position(queue, ctx.job_id)
                    ahead = any(j.get("state") != "queued" for j in queue if str(j.get("id")) != str(ctx.job_id))
                    ctx.text = progress.wait_text(pos, ahead, ctx.label)
                elif state == "starting":
                    ctx.stage, ctx.text = "prepare", "GPU 준비 중"
                elif state == "running":
                    if ctx.stage != "generate":
                        ctx.stage = "generate"
                    ctx.text = "장면 준비 중" if ctx.scene_phase else f"생성 {ctx.tracker.pct}%"
                    started = st.get("started_at")
                    if started is not None and time.time() - started.timestamp() > max_run:
                        await self._stop_job(ctx)
                        raise StepError("run_timeout")
                else:   # cleaning, finished
                    ctx.stage, ctx.text = "collect", "마무리"
                    collect_since = collect_since or time.monotonic()
                    if state == "finished" or ctx.success_signal:
                        return await self._collect(ctx, st)
                    if time.monotonic() - collect_since > 600:
                        ctx.text = "마무리가 늦어지고 있습니다"
                if await self.deps.setting("paused"):
                    ctx.text = "점검 중이라 새 작업이 잠시 멈췄습니다"
                elif await self.deps.setting("blocked"):
                    ctx.text = "GPU 대기열이 멈췄습니다. 관리자가 확인 중입니다"
                await self.emit(ctx, True)
                await asyncio.sleep(POLL)
        finally:
            if ctx.client_id and ctx.receiver is not None:
                self.deps.unregister(env["id"], ctx.client_id, ctx.receiver)

    async def _job_state(self, job_id, tries: int = 3):
        for i in range(tries):
            try:
                return await store.job_state(job_id)
            except Exception:  # noqa: BLE001 - DB 일시 오류
                if i == tries - 1:
                    raise
                await asyncio.sleep(POLL)

    async def _collect(self, ctx: Ctx, st: dict):
        """결과를 고른다. 끝 판정은 DB 가 한다 (5.13). 성공 신호를 받았거나 finished+success 일 때 결과를 쓴다"""
        turn, recipe = ctx.turn, ctx.recipe
        job_id = ctx.job_id
        for _ in range(60):
            st = await store.job_state(job_id)
            if st["state"] == "finished":
                break
            if ctx.success_signal:
                break
            await asyncio.sleep(POLL)
        outcome = st.get("outcome")
        outs = await store.job_outputs(job_id)
        main_o = recipe["outputs"]["main"]
        main = files.pick_output(outs, main_o["node"], main_o["kind"], main_o.get("ext"))
        if st["state"] == "finished" and outcome not in (None, "success"):
            if outcome == "lost" and main:
                pass       # 출력이 있으면 결과로 쓴다 (5.10)
            elif outcome == "cancelled":
                if ctx.cancel_reason or await store.cancel_requested(turn["id"]):
                    await self.finish_state(turn, "cancelled")
                    return
                raise StepError("admin_cancelled")
            elif outcome == "timeout":
                raise StepError("run_timeout")
            elif outcome == "rejected":
                raise StepError("rejected")
            elif outcome == "lost":
                raise StepError("lost")
            else:
                err = st.get("error") or {}
                text = str(err.get("message") if isinstance(err, dict) else err).lower()
                raise StepError("run_oom" if OOM_RE.search(text) else "run_error", str(err)[:300])
        if not main:
            raise StepError("no_output")
        for o in outs:
            await store.event(turn["id"], "output", {"rel_path": o["rel_path"], "media": o.get("media"), "node": o.get("node_id")})
        if await store.cancel_requested(turn["id"]):     # 취소 요청 뒤에 끝난 결과는 채팅에 내지 않는다 (5.9)
            await self.finish_state(turn, "cancelled")
            return
        if ctx.scene_phase:       # 장면 이미지: 채팅에는 내지 않고 다음 단계가 첫 화면으로 쓴다
            return main
        ctx.close_nodes()
        if ctx.node_log:
            await store.event(turn["id"], "node_timing", {"nodes": ctx.node_log[:200]})
        att_id, media = await self._add_result(ctx, main)
        notes = list(ctx.notes)
        if ctx.action == "i2v":
            notes = intent.i2v_notes(ctx.spec, aspect_note=not ctx.via) + notes if ctx.spec else notes
        elif ctx.action == "image" and ctx.spec.get("aspect") is None:
            notes = [n for n in notes if n != "영상 비율은 사진 비율을 따릅니다"]
        ctx.stage, ctx.text = "collect", "마무리"
        ctx.tracker.feed("execution_success", {"prompt_id": job_id})
        await self.emit(ctx, True)
        actions = (TO_VIDEO if media == "image" else []) + RETRY
        msg = await self.message(ctx, "assistant", "result", "", {"attachment_ids": [str(att_id)], "notes": notes,
                                                                  "media": media, "actions": actions})
        await self.finish_state(turn, "done", result={"attachment_ids": [str(att_id)], "message_id": msg["id"],
                                                      "notices": notes, "media": media})
        await self._inspect_after(ctx, outs)

    async def _add_result(self, ctx: Ctx, main: dict):
        import uuid
        turn = ctx.turn
        rel = main["rel_path"]
        size = (turn.get("work") or {}).get("size") or []
        frames = ((turn.get("work") or {}).get("settings") or {}).get("frames")
        media = "video" if main.get("media") in ("video", "videos") or rel.endswith((".mp4", ".webm")) else "image"
        try:       # 결과 폴더는 누구나 고칠 수 있어, 나중에 바뀌었는지 비교할 지문을 남긴다 (5.13)
            nbytes, sha = await asyncio.to_thread(files.fingerprint, rel)
        except OSError:
            nbytes, sha = None, None
        att = {"id": uuid.uuid4(), "thread_id": turn["thread_id"], "user_id": turn["user_id"], "source": "result",
               "media": media, "mime": files.mime_of(rel), "width": size[0] if size else None,
               "height": size[1] if len(size) > 1 else None, "frames": frames, "bytes": nbytes, "sha256": sha,
               "result_rel": rel, "job_id": uuid.UUID(str(ctx.job_id)), "meta": None}
        await store.add_attachment(att)
        return att["id"], media

    # ── 결과 검사 (결과를 보낸 뒤, 사용자에게 보이지 않는다) ──
    async def _inspect_after(self, ctx: Ctx, outs: list[dict]):
        mode = settings.studio_inspect
        recipe = ctx.recipe
        chk = (recipe.get("outputs") or {}).get((recipe.get("inspect") or {}).get("output") or "check")
        if mode == "off" or not chk or (recipe.get("inspect") or {}).get("when") == "off":
            return
        try:
            if mode == "idle":
                q = await self.deps.queue()
                if any(j.get("kind") == "comfy" and str(j.get("id")) != str(ctx.job_id) for j in q):
                    await store.event(ctx.tid, "inspect", {"skipped": "대기열에 다른 ComfyUI 작업이 있어 검사를 건너뜀"})
                    return
            o = files.pick_output(outs, chk["node"], chk["kind"], chk.get("ext"))
            if not o:
                await store.event(ctx.tid, "inspect", {"skipped": "검사 프레임이 없음"})
                return
            owner, sub, name = files.split_rel(o["rel_path"])
            expect = [FIRST_PERSON_TRAIT] if ctx.first_person else None
            with self.deps.llm_context() as bag:
                insp = await self.deps.inspect(ctx.user, str(ctx.turn.get("client_ip") or ""), owner, name, sub, expect)
                for e in bag:
                    await store.link_job(ctx.tid, e["job_id"], "inspect", e["purpose"])
            await store.event(ctx.tid, "inspect", insp)
            text = notice_for_inspection(insp, ctx.first_person)
            if text is None and ctx.action == "image" and TWO_RE.search(ctx.req_text) and int(insp.get("people") or 0) < 2:
                text = "두 사람이 한 몸처럼 붙어 보입니다. 다시 만들어 볼까요?"
            if text:
                await store.event(ctx.tid, "notice", {"code": "inspect", "text": text})
                await self.message(ctx.turn, "assistant", "notice", text, {"level": "warn", "actions": RETRY})
        except Exception as e:  # noqa: BLE001 - 검사 실패가 결과를 가리면 안 된다
            await store.event(ctx.tid, "inspect", {"error": str(e)[:300]})

    # ── 허브 시작 뒤 복구 (P1) ──
    async def recover(self):
        """돌던 요청: ComfyUI 단계에 있었으면 이어서 감시하고, LLM 단계였으면 interrupted. 대기 중 요청은 다시 돌린다 (5.10)"""
        env = await store.studio_env(settings.studio_env_slug)
        for t in await store.active_turns():
            if t["id"] in self.tasks:      # 허브가 막 열린 사이 pump 가 이미 돌린 요청이다
                continue
            jid = await store.turn_job(t["id"], "generate")
            if jid is None and env is not None:
                pend = ((t.get("work") or {}).get("pending") or {}).get("client_id")
                found = await store.job_by_client(env["id"], pend) if pend else None
                if found:
                    jid = found["prompt_id"]
                    await store.link_job(t["id"], jid, "generate", t.get("recipe_id"))
            if jid is not None:
                self.spawn(t, resume=True)
                continue
            if not (t.get("work") or {}).get("resumed"):     # P2: 처음 겪는 재시작이면 한 번 자동으로 다시 한다 (5.10)
                await store.merge_work(t["id"], {"resumed": True})
                await store.event(t["id"], "recovered", {"result": "resumed"})
                self.spawn(t, resume=True)
                continue
            await store.event(t["id"], "recovered", {"result": "interrupted"})
            if await self.finish_state(t, "interrupted"):
                await self.message(t, "assistant", "notice", FAIL_TEXT["interrupted"][0],
                                   {"level": "warn", "actions": RETRY_FULL})
        await self.pump()


def env_user(ctx: Ctx) -> str:
    return ctx.user["username"]


def negative_problem(recipe: dict, prompt: dict) -> str | None:
    """접수 직전 부정 자리 확인 (5.5). 긍정 글이 부정 노드에 들어가거나 0 조건 구성이 깨졌으면 설명을 돌려준다"""
    ps = recipes.slots_of(recipe, "prompt")
    if not ps:
        return "프롬프트 자리가 없습니다"
    pn = str(ps[0]["node"])
    text = str(prompt[pn]["inputs"][ps[0]["input"]])
    if recipe["negative_source"] == "node":
        neg = (recipe.get("fixed") or {}).get("negative")
        if not neg:
            return "부정 자리가 레시피에 없습니다"
        nn = prompt.get(str(neg["node"]), {}).get("inputs", {}).get(neg["input"])
        if str(neg["node"]) == pn:
            return "프롬프트 자리와 부정 자리가 같은 노드입니다"
        if isinstance(nn, str) and nn.strip() and nn.strip() == text.strip():
            return "긍정 프롬프트가 부정 자리에 들어갔습니다"
        roles = recipes.roles_of(prompt, pn)
        if "positive" not in roles or "negative" in roles:
            return f"프롬프트 노드가 닿는 곳이 이상합니다 ({sorted(roles)})"
    else:
        roles = recipes.roles_of(prompt, pn)
        if "positive" not in roles or "negative_zero" not in roles:
            return f"0 조건 구성이 깨졌습니다 ({sorted(roles)})"
    return None
