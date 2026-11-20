"""감독: 채팅 요청 하나를 끝까지 진행한다 (docs/chat-studio-design.md 5절). P1 은 사진 → 이미지→영상 한 줄기다.

흐름: 요청 → (첫 화면 읽기 → 프롬프트 작성 → 필요하면 다듬기: 기존 video.render·video.fix 를 그대로 부름)
      → 레시피에 값 넣기 → 기존 접수 함수로 ComfyUI 작업 접수 → 진행 감시 → 결과 전달 → (결과 검사)

바깥 일(영상 렌더러·접수·스케줄러·DB 읽기)은 Deps 로 모아 시험에서 가짜로 바꾼다.
GPU 는 직접 쓰지 않는다. 모든 GPU 사용은 기존 대기열(llm_turn·submit_comfy)을 거친다.
"""
import asyncio
import contextlib
import logging
import math
import os
import random
import re
import time

from ..config import settings
from . import assemble, checks, files, intent, progress, recipes, store

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
    "assemble": ("장면을 이어 붙이지 못했습니다. 다시 시도해 볼까요? (만든 장면은 그대로 씁니다)", RETRY_FULL),
}
# 긴 영상: 장면 작업이 이렇게 실패하면 그 장면만 시드를 바꿔 한 번 더 한다 (docs/long-video-design.md 7.2)
LONG_RETRY_CODES = {"run_error", "run_oom", "lost", "no_output", "run_timeout", "gpu_prepare"}
# 긴 영상의 실패 안내: 버튼은 늘 '다시 시도'(같은 구성안, 끝난 장면은 그대로, 남은 장면부터). '다시 만들기'(새 시드)는 장면을 모두 버린다
LONG_KEEP = " (만든 장면은 그대로 씁니다)"
LONG_FAIL_TEXT = {
    "gone": ("장면 파일이 없어져 이어 붙이지 못했습니다. 다시 시도하면 그 장면부터 다시 만듭니다.", RETRY_FULL),
    "changed": ("장면 파일이 바뀌어 이어 붙이지 못했습니다. 다시 시도하면 그 장면부터 다시 만듭니다.", RETRY_FULL),
    "no_image": ("장면의 첫 화면을 찾지 못했습니다. 다시 시도해 볼까요?" + LONG_KEEP, RETRY_FULL),
}
LONG_SCENE_OVERHEAD = 135      # 장면마다 GPU 실행 말고 더 걸리는 시간(초) [추정]: 장면 이미지 약 25 + LLM 약 90 + 콜드 스타트 약 20
LONG_TAIL = 70                 # 음악 약 40 + 합치기 약 30 [추정]
LONG_RESUME_MAX = 5            # 허브가 다시 시작될 때 긴 영상을 자동으로 이어 가는 횟수 (재시작이 되풀이될 때 끝없이 돌지 않게)
DISSOLVE_SECONDS = 0.6     # 이어 만든 장면 사이의 디졸브 길이(초) [추정 값, 메인 시험 전]
LONG_SCENE_KEYS = ("plan", "scene_prompt", "image_rel", "first_from", "prompt", "ko", "profile", "settings", "frame_known",
                   "frame_people", "no_chain", "clip_rel", "clip_sha", "last_rel", "check_rel", "seed", "seconds", "violations",
                   "edit_rel", "first_people")
# 대사 영상 (11-19, docs/short-film-dialogue-design.md 12절)
TALK_TAIL = 0.9            # 말하는 장면은 대사 끝 + 이만큼에서 자른다 (11-19 숲길: 대사가 끝난 뒤 IA2V 화면이 어두워짐)
TALK_MIN_SHOT = 3.0
TALK_PAD = 1.0             # 말하는 장면 길이 = 대사 길이 + 이만큼 (올림, 4~20초)
DIALOGUE_PART_OVERHEAD = 10   # 녹음·기준 그림·편집마다 실행 말고 더 걸리는 시간(초). 11-19 메인 시험: 편집 20초·기준 그림 7~14초·녹음 50~71초
DIALOGUE_CAST_SECONDS = 15    # 인물 기준 그림 한 장(Z-Image 8스텝) 실행 시간(초). 11-19 메인 시험 7~14초
# 그림·클립 자동 검사 (11-20, studio/checks.py, STUDIO_PICTURE_CHECK): 사람 수·몸의 이상·화풍이 틀리면 씨앗을 바꿔 다시 만든다
STILL_TRIES = 3            # 장면 그림·편집 그림·인물 기준 그림: 처음 + 다시 두 번 (한 장 6~20초라 싸다)
CLIP_TRIES = 2             # 장면 클립·입 맞춤 클립: 처음 + 다시 한 번 (한 번에 1~2분이라 적게)
PICTURE_CHECK_SECONDS = 18    # 검사 한 장(시각 모델 qwen3-vl 8B) 시간(초). 11-20 메인 실측 13~19초
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
        self.long: dict | None = None    # 긴 영상의 진행(단계·지금 장면·끝난 장면 수). 장면마다의 작업 ctx 가 같은 dict 를 나눠 쓴다
        self.client_tag: str | None = None   # ComfyUI client_id 끝 표시 (긴 영상은 장면·종류마다 다르게)
        self.sub = False              # 긴 영상의 장면·음악 작업: 결과를 채팅에 내지 않고 돌려준다
        self.outs: list | None = None # sub 작업의 모든 결과 (마지막 프레임 등)


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

    async def plan(self, user, ip, env, **kw):
        """연출 계획표 (장면 이미지를 그리기 전에): 장소·빛·공기·디테일·구간별 연기"""
        from .. import video
        return await video.plan(user, ip, env["username"], env["slug"], kw.pop("text"), **kw)

    async def storyboard(self, user, ip, env, **kw):
        """긴 영상 구성안: 설정집·배경음악·장면표 (docs/long-video-design.md 7.2)"""
        from .. import video
        return await video.storyboard(user, ip, env["username"], env["slug"], kw.pop("text"), **kw)

    def assemble(self, clips, music, out, sound=True, transitions=None):
        """장면 클립 + 음악 합치기 (ffmpeg, 스레드에서 부른다)"""
        return assemble.run(clips, music, out, sound, transitions)

    def trim_voice(self, src, out, fx=None, level=False, max_seconds=None):
        """녹음한 대사의 앞뒤 무음을 잘라 입력 폴더에 WAV 로 (ffmpeg, 스레드에서). fx 는 목소리 후처리 이름, level 이면 소리 크기를
        목표(assemble.VOICE_LUFS)로 맞추고, max_seconds 는 앞 몇 초만 남긴다(기준 목소리). 길이(초)"""
        return assemble.trim_voice(src, out, fx, assemble.VOICE_LUFS if level else None, max_seconds)

    def render_subtitle(self, text, size, out):
        """투명 PNG 자막 (Pillow, 스레드에서). 글꼴이 없으면 None"""
        return assemble.render_subtitle(text, (int(size[0]), int(size[1])), out)

    def assemble_dialogue(self, clips, lines, music, out, trims=None):
        """대사 영상 합치기: 디졸브 + 대사 소리 + 자막 + 음악 (ffmpeg, 스레드에서)"""
        return assemble.run_dialogue(clips, lines, music, out, trims=trims)

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


def _video():
    from .. import video
    return video


def video_pick_staging(text: str) -> str | None:
    """방향을 정하지 않은 보행 요청의 연출(옆 추적 좌·우, 멀어짐, 다가옴)을 무작위로 고른다 (video.pick_staging)"""
    from .. import video
    return video.pick_staging(text)


def video_staging(sid) -> dict | None:
    from .. import video
    return video.staging_of(sid)


def video_plan_scene_text(plan) -> str:
    from .. import video
    return video.plan_scene_text(plan)


def video_scene_notes(story: dict, i: int) -> str:
    from .. import video
    return video.scene_notes(story, i)


def video_scene_direction(story: dict, i: int, prev_plan) -> str:
    """연출 계획표를 짜는 모델에게 줄 앞뒤 장면·이음새 정보 (video.scene_direction)"""
    from .. import video
    return video.scene_direction(story, i, prev_plan if isinstance(prev_plan, dict) else None)


def video_scene_image_text(story: dict, i: int, plan) -> str:
    from .. import video
    return video.scene_image_text(story, i, plan)


def video_scene_has_person(story: dict, i: int) -> bool:
    from .. import video
    return video.scene_has_person(story, i)


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

    def dialogue_ready(self) -> bool:
        """대사 영상에 필요한 레시피(목소리 설계·복제, 입 맞춤, 장면 이미지)가 모두 켜져 있는가 (합치기 도구는 긴 영상 조건이 본다)"""
        return all(self.recipe_for(a) is not None for a in ("voice", "voice_clone", "talk", "image"))

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
        if ctx.long is not None:
            prog = self._long_progress(ctx)
            stage = prog["stage"]
        else:
            card = "pipeline" if ctx.via else ctx.action
            title, _ = progress.CARD.get(card, progress.CARD["i2v"])
            has_fix = ctx.has_fix or any(progress.PURPOSE_STAGE.get(e["purpose"]) == "fix" for e in ctx.bag)
            shown = shown_stage(ctx)
            pct_ok = ctx.stage in ("generate", "collect") and not ctx.scene_phase     # 장면 이미지 단계는 % 를 보이지 않는다
            prog = {"title": title, "stage": shown, "text": ctx.text,
                    "pct": ctx.tracker.pct if pct_ok else None,
                    "band": ctx.tracker.band_label() if pct_ok and ctx.stage == "generate" else None,
                    "chips": progress.chips(card, has_fix, CHIP_OF_STAGE.get(shown, shown))}
            stage = ctx.stage
        await store.save_progress(ctx.tid, prog)
        if stage != ctx.saved_stage:       # 운영자 화면이 보는 chat_turns.stage 도 단계가 바뀔 때 갱신한다
            ctx.saved_stage = stage
            await store.set_stage(ctx.tid, stage)
        await self.push(ctx.user["id"], {"type": "turn.progress", "thread_id": str(ctx.turn["thread_id"]),
                                         "turn_id": str(ctx.tid), "progress": prog})

    @staticmethod
    def _long_progress(ctx: Ctx) -> dict:
        """긴 영상의 진행 카드: 칩은 구성안 → 장면 → 음악 → 합치기, 글은 '장면 3/18 · 생성 45%'"""
        L = ctx.long
        phase = L.get("phase") or "story"
        n = int(L.get("n") or 0)
        text = ctx.text or ""
        frac = 0.0
        if phase == "scenes":
            if ctx.sub and not ctx.scene_phase and ctx.stage in ("generate", "collect"):
                frac = ctx.tracker.pct / 100.0
            head = f"장면 {int(L.get('i') or 0) + 1}/{max(1, n)}"
            text = f"{head} · {text}" if text else head
        elif phase == "voices":          # 대사 영상의 녹음: '녹음 3/9 · 대사 녹음 40%'
            vn, vd = max(1, int(L.get("vn") or 1)), int(L.get("vdone") or 0)
            cur = ctx.tracker.pct / 100.0 if ctx.sub and ctx.stage in ("generate", "collect") else 0.0
            frac = (vd + cur) / vn
            head = f"녹음 {min(vd + 1, vn)}/{vn}"
            text = f"{head} · {text}" if text else head
        pct = progress.long_pct(int(L.get("done") or 0), n, phase, frac)
        if pct is not None:
            pct = max(pct, int(L.get("pct") or 0))     # 줄지 않게
            L["pct"] = pct
        # 채팅 화면은 % 가 있으면 '띠 · %' 만 보인다: 띠 자리에 '장면 1/3 · 생성' 을 넣는다 (지금 단계의 % 는 빼고 전체 % 만, 11-17 메인)
        band = re.sub(r"\s*\d+%$", "", text).strip() if pct is not None else None
        card = "dialogue" if L.get("dialogue") else "long"
        return {"title": progress.CARD[card][0], "stage": phase, "text": text, "pct": pct, "band": band or None,
                "chips": progress.chips(card, False, phase)}

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
        if (turn.get("request") or {}).get("long"):
            text, actions = LONG_FAIL_TEXT.get(code, (text, actions))
            if actions != RETRY_FULL and actions:
                text, actions = text.replace("다시 만들어 볼까요?", "다시 시도해 볼까요?") + LONG_KEEP, RETRY_FULL
            elif actions == RETRY_FULL and LONG_KEEP not in text and "그 장면부터" not in text:
                text += LONG_KEEP
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
        if req.get("long"):          # 여러 장면을 이어 붙이는 긴 영상 (docs/long-video-design.md 7절)
            await self._run_long(ctx, work)
            return
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
    @classmethod
    def scene_hint(cls, story: dict) -> str:
        """장면 그림 글 뒤에 붙이는 힌트. 화풍이 사실이 아니면(애니메이션 등) '사진' 말고 그 화풍의 한 컷으로 그리게 한다"""
        from .. import video
        style = video.story_style(story)
        if not style:
            return cls.SCENE_HINT
        return (f"(Make this the first instant of the scene as one still frame drawn in this style: {style} Not a photograph, not a sequence. "
                "If someone or something moves, show it already in motion, slightly off-center with open space ahead of its direction.)")

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
                    staging = work.get("staging") or video_pick_staging(ctx.req_text)
                    st = video_staging(staging)
                    plan = work.get("plan")
                    if plan is None and not ctx.first_person:
                        # 연출 계획표: 장소·빛은 장면 이미지에 그리고, 연기·공기·디테일은 영상 글에 쓴다. 못 짜도 요청은 계속한다
                        try:
                            plan = await self._guard_llm(ctx, self.deps.plan(
                                ctx.user, turn.get("client_ip") and str(turn["client_ip"]) or "", ctx.env, text=ctx.req_text,
                                profile_id=ctx.recipe["video_profile"], seconds=(ctx.spec or {}).get("seconds"),
                                staging=staging))
                        except StepError as e:
                            if e.code == "queue_wait":
                                raise
                            await store.event(turn["id"], "plan_failed", {"code": e.code, "detail": e.detail})
                            plan = None
                        for e in bag:
                            await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
                        bag.clear()
                        await store.event(turn["id"], "plan", {"plan": plan})
                    hint = (self.SCENE_HINT + (self.SCENE_HINT_POV if ctx.first_person else "")
                            + (" " + st["scene"] if st else "")
                            + (" " + video_plan_scene_text(plan) if plan else ""))
                    if len(ctx.req_text) + len(hint) > 1900:     # 이미지 확장 입력은 2000자까지: 긴 요청이면 계획표 문장은 덜어 낸다
                        hint = self.SCENE_HINT + (self.SCENE_HINT_POV if ctx.first_person else "") + (" " + st["scene"] if st else "")
                    r = await self._guard_llm(ctx, self.deps.expand_image(
                        ctx.user, turn.get("client_ip") and str(turn["client_ip"]) or "", ctx.req_text + "\n" + hint))
                    for e in bag:
                        await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
                await store.event(turn["id"], "expand", {"crew": r.get("crew"), "violations": r.get("violations"),
                                                          "rounds": r.get("rounds"), "advice": r.get("advice"),
                                                          "people": r.get("people"), "scene": True})
                if not (r.get("prompt") or "").strip():
                    raise StepError("llm_error", "장면 프롬프트를 만들지 못했습니다")
                work = {**work, "scene_prompt": r["prompt"], "crew": (r.get("crew") or {}).get("category"),
                        "staging": staging if st else None, "plan": plan}
                await store.merge_work(turn["id"], {"scene_prompt": r["prompt"], "crew": work.get("crew"),
                                                    "staging": work["staging"], "plan": plan})
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

    # ── 긴 영상: 구성안 → 계획 카드 → 장면마다(계획표 → 첫 화면 → 영상 글 → LTX) → 배경음악 → 합치기 (docs/long-video-design.md 7절) ──
    async def _run_long(self, ctx: Ctx, work: dict):
        turn = ctx.turn
        dialogue = bool((turn.get("request") or {}).get("dialogue"))
        ctx.long = {"phase": "story", "i": 0, "n": 0, "done": 0, "pct": 0, "dialogue": dialogue}
        ctx.stage = "prompt"
        if recipes.engine_of(ctx.recipe) != "ltx":
            # 장면표(10초 안팎, 효과음)는 소리 포함 LTX 레시피 기준이다. 소리 없는 Wan(5초)으로 조용히 바꾸지 않는다
            raise StepError("unavailable", "긴 영상은 소리 포함(LTX) 레시피로만 만듭니다")
        work = await self._long_inherit(ctx, work)
        if not work.get("story"):
            work = await self._write_story(ctx, work)
        story = work["story"]
        n = len(story["scenes"])
        ctx.long["n"] = n
        if not work.get("long_size"):
            work["long_size"] = list(recipes.default_size(ctx.recipe, aspect=(ctx.spec or {}).get("aspect")))
            await store.merge_work(turn["id"], {"long_size": work["long_size"]})
        if not work.get("approved"):
            await self._park_long(ctx, work)
            return
        scenes = [dict(s or {}) for s in (work.get("scenes") or [])][:n]
        scenes += [{} for _ in range(n - len(scenes))]
        if dialogue and story.get("dialogue"):
            # 목소리를 먼저: 인물마다 목소리 하나 → 그 목소리로 모든 대사 → 장면 길이를 녹음에 맞춤 → 인물 기준 그림
            if not await self._dlg_voices(ctx, work) or not await self._dlg_casts(ctx, work):
                return           # 취소됐다
            story = work["story"]
        ctx.long.update(phase="scenes", done=sum(1 for s in scenes if s.get("clip_rel")))
        for i in range(n):
            s = scenes[i]
            if s.get("clip_rel"):
                ok = (await self._long_file_ok(s["clip_rel"], s["clip_sha"]) if s.get("clip_sha")
                      else bool(files.result_path(s["clip_rel"])))
                if ok:
                    continue
                await self._long_reset(ctx, work, scenes, i)
            if dialogue and story.get("dialogue") and _video().cast_scene(story, i):
                st = await self._dlg_scene(ctx, work, scenes, i)
            else:
                st = await self._long_scene(ctx, work, scenes, i)
            if st is None:
                return           # 취소됐다 (_collect 가 상태를 끝냈다)
            scenes[i] = st
            ctx.long["done"] = sum(1 for s in scenes if s.get("clip_rel"))
        work["scenes"] = scenes
        music = await self._long_music(ctx, work)
        if music is False:
            return               # 취소됐다
        if dialogue and story.get("dialogue"):
            await self._dlg_finish(ctx, work, scenes, music)
        else:
            await self._long_finish(ctx, work, scenes, music)

    async def _long_reset(self, ctx: Ctx, work: dict, scenes: list, i: int):
        """끝난 장면 i 의 클립이 없어졌거나 바뀌었다: 그 장면과, 그 장면에서 이어지는 뒤 장면들(첫 화면이 i 의 마지막 프레임)을
        계획표·그림 글만 남기고 비운다. 끝난 옛 작업을 이어받지 않게 접수 정보도 비운다(시도 번호는 이어서 늘린다).
        재시작해도 옛 클립이 되살아나지 않게 바로 저장한다"""
        story = work["story"]
        idx = [i]
        while idx[-1] + 1 < len(scenes) and story["scenes"][idx[-1] + 1].get("continues"):
            idx.append(idx[-1] + 1)
        patch = {}
        left = dict(work.get("check_left") or {})
        for k in idx:
            if scenes[k].get("clip_rel") or scenes[k].get("first_att"):
                await store.event(ctx.turn["id"], "long_scene_redo", {"index": k, "chained": k != i})
            scenes[k] = {key: v for key, v in scenes[k].items() if key in ("plan", "scene_prompt")}
            for key in (f"L{k}i_pending", f"L{k}v_pending"):
                if work.get(key):
                    work[key] = patch[key] = {"client_id": None, "attempt": int((work[key] or {}).get("attempt") or 0)}
            for key in (f"{k}:image", f"{k}:clip"):          # 다시 만드는 장면의 남은 검사 문제는 이제 그 영상에 없다
                left.pop(key, None)
        if left != (work.get("check_left") or {}):
            work["check_left"] = patch["check_left"] = left
        work["scenes"] = scenes
        patch["scenes"] = [{k: s[k] for k in LONG_SCENE_KEYS + ("first_att",) if k in s} for s in scenes]
        await store.merge_work(ctx.turn["id"], patch)
        ctx.long["done"] = sum(1 for s in scenes if s.get("clip_rel"))

    async def _long_inherit(self, ctx: Ctx, work: dict) -> dict:
        """다시 만들기(새 시드)는 원래 구성안을 그대로 쓰고 계획 카드를 다시 보인다. 실패한 요청의 다시 시도는 구성안·승인·끝난 장면을
        이어받아 남은 장면부터 만든다 (지문이 같은 클립만, 처음 빠진 장면부터는 다시: 이어지는 장면의 첫 화면이 앞 장면에 기대므로)"""
        req = ctx.turn.get("request") or {}
        if work.get("story") or not req.get("source_turn"):
            return work
        src = await store.get_turn(req["source_turn"], ctx.turn["user_id"])
        sw = (src or {}).get("work") or {}
        if not sw.get("story"):
            return work
        patch = {"story": sw["story"]}
        if sw.get("long_size"):
            patch["long_size"] = sw["long_size"]
        kept = 0
        if (src or {}).get("state") != "done":
            # 실패·중단·취소된 요청을 다시 하면(버튼은 '다시 시도', 말로 한 '다시 만들기'도) 끝난 장면부터 잇는다
            scenes = []
            for s in sw.get("scenes") or []:
                if not (s.get("clip_rel") and await self._long_file_ok(s["clip_rel"], s.get("clip_sha"))):
                    break
                if s.get("last_rel") and not files.result_path(s["last_rel"]):
                    s = {k: v for k, v in s.items() if k != "last_rel"}
                scenes.append({k: s[k] for k in LONG_SCENE_KEYS if k in s})
            kept = len(scenes)
            patch["scenes"] = scenes
            if sw.get("music_rel") and files.result_path(sw["music_rel"]):
                patch["music_rel"] = sw["music_rel"]
            for k in ("voices", "lines", "casts"):        # 대사 영상: 녹음한 목소리·대사와 인물 기준 그림 (파일은 쓸 때 다시 확인한다)
                if sw.get(k):
                    patch[k] = sw[k]
            # 이어받은 장면·인물 기준 그림에 남아 있던 검사 문제 (결과 안내가 다시 시도에서 사라지지 않게, 11-20 검토)
            left = {key: why for key, why in (sw.get("check_left") or {}).items()
                    if (str(key).split(":")[0].isdigit() and int(str(key).split(":")[0]) < kept)
                    or (str(key).startswith("c") and sw.get("casts"))}
            if left:
                patch["check_left"] = left
            if sw.get("approved"):
                patch["approved"] = True        # 승인한 적 없는 계획은 카드를 다시 보인다 (승인 없이 GPU 를 쓰지 않게)
        await store.merge_work(ctx.turn["id"], patch)
        await store.event(ctx.turn["id"], "long_inherited", {"source": str(req["source_turn"]), "mode": req.get("mode"),
                                                             "scenes_kept": kept})
        return {**work, **patch}

    @staticmethod
    async def _long_file_ok(rel: str, sha: str | None) -> bool:
        """결과 폴더는 누구나 고칠 수 있다: 남겨 둔 지문과 같을 때만 그 장면 클립을 다시 쓴다"""
        if not files.result_path(rel):
            return False
        if not sha:
            return False
        try:
            _n, now = await asyncio.to_thread(files.fingerprint, rel)
        except OSError:
            return False
        return now == sha

    async def _write_story(self, ctx: Ctx, work: dict) -> dict:
        turn = ctx.turn
        ip = turn.get("client_ip") and str(turn["client_ip"]) or ""
        with self.deps.llm_context() as bag:
            ctx.bag = bag
            ctx.stage, ctx.text = "story", "구성안 쓰는 중"
            await self.emit(ctx, True)
            extra = {"dialogue": True} if (turn.get("request") or {}).get("dialogue") else {}
            r = await self._guard_llm(ctx, self.deps.storyboard(ctx.user, ip, ctx.env, text=ctx.req_text,
                                                                seconds=(ctx.spec or {}).get("seconds"), **extra))
            for e in bag:
                await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
            bag.clear()
        story = r.get("story") if isinstance(r, dict) else None
        if not story or not story.get("scenes"):
            raise StepError("llm_error", "구성안을 만들지 못했습니다")
        await store.event(turn["id"], "story", {"scenes": len(story["scenes"]), "seconds": story.get("seconds"),
                                                "violations": r.get("violations"), "rounds": r.get("rounds"),
                                                "lines": sum(1 for sc in story["scenes"] if sc.get("line_ko")) if story.get("dialogue") else None})
        work = {**work, "story": story}
        await store.merge_work(turn["id"], {"story": story})
        return work

    async def estimate_long(self, ctx: Ctx, story: dict) -> int:
        """긴 영상 완료까지 예상 시간(초) [추정]: 장면마다 LTX 실행(레시피 추정식) + 장면 이미지·LLM, 음악·합치기, 앞 줄"""
        est = ctx.recipe.get("estimate") or {}
        own = float(est.get("gpu_seconds") or 60)
        ref = float(est.get("ref_frames") or 121)
        exp = float(est.get("exponent") or 1.0)
        fps = int(ctx.recipe["defaults"].get("fps") or 24)
        total = LONG_TAIL
        dlg = bool(story.get("dialogue"))
        talk = self.recipe_for("talk") if dlg else None
        test = (talk or {}).get("estimate") or {}
        for sc in story["scenes"]:
            frames = int(sc["seconds"]) * fps + 1
            if talk is not None and sc.get("kind") == "talk":      # 말하는 장면: 편집 한 번 + 입 맞춤, LLM 없음
                tref, texp = float(test.get("ref_frames") or 145), float(test.get("exponent") or 1.0)
                total += float(test.get("gpu_seconds") or 60) * max(0.4, min(8.0, (frames / tref) ** texp)) + 2 * DIALOGUE_PART_OVERHEAD
            else:
                total += own * max(0.4, min(8.0, (frames / ref) ** exp)) + LONG_SCENE_OVERHEAD
        if dlg:          # 녹음(CPU, 인물마다 목소리 한 번 + 대사마다 한 번)과 인물 기준 그림
            v = _video()
            tts = float(((self.recipe_for("voice_clone") or {}).get("estimate") or {}).get("gpu_seconds") or 120)
            parts = len(v.speakers_of(story)) + sum(1 for sc in story["scenes"] if sc.get("line_ko"))
            total += parts * (tts + DIALOGUE_PART_OVERHEAD) + len(v.cast_ids(story)) * (DIALOGUE_CAST_SECONDS + DIALOGUE_PART_OVERHEAD)
        if settings.studio_picture_check:    # 장면마다 그림 한 장 + 클립 검사 프레임 한 장 (다시 만들면 더 걸린다)
            total += 2 * len(story["scenes"]) * PICTURE_CHECK_SECONDS
        try:
            med = await self.deps.medians() or {}
            q = await self.deps.queue()
            total += sum(1 for j in q if j.get("kind") == "comfy") * float(med.get("comfy") or own)
            total += sum(1 for j in q if j.get("kind") == "llm") * float(med.get("llm") or 30)
        except Exception:  # noqa: BLE001 - 앞 줄을 못 세도 계획은 보인다
            log.exception("긴 영상 앞 줄 세기 실패")
        return int(total)

    async def _park_long(self, ctx: Ctx, work: dict):
        """긴 영상 계획 카드: 장면표·배경음악·총 길이·예상 시간. 긴 영상은 대화 설정과 상관없이 늘 승인을 받는다 (GPU 를 오래 쓴다)"""
        turn, recipe, story = ctx.turn, ctx.recipe, work["story"]
        plan = {"action": "long", "title": story.get("title_ko") or "", "logline": story.get("logline_ko") or "",
                "seconds": story.get("seconds"), "label": intent.long_label(story.get("seconds")),
                "scenes": [{"seconds": s["seconds"], "ko": s["ko"], "continues": bool(s.get("continues"))} for s in story["scenes"]],
                "music": (story.get("music") or {}).get("ko") or "", "size": work.get("long_size"),
                "fps": recipe["defaults"].get("fps"), "quality": recipe.get("quality_label"),
                "estimate_s": await self.estimate_long(ctx, story), "recipe_version": recipe["version"],
                "ttl_hours": settings.studio_plan_ttl_hours, "attachment_ids": []}
        if story.get("dialogue"):        # 대사 영상: 목소리와 장면마다 대사를 카드에 보인다 (녹음 전에 대본을 확인하게)
            chars = {c["id"]: c for c in story["bible"]["characters"]}
            plan["dialogue"] = True
            plan["voices"] = [{"name": chars[c].get("name_ko") or c, "ko": chars[c].get("voice_ko") or ""}
                              for c in _video().speakers_of(story) if c in chars]
            plan["library"] = [c.get("name_ko") or c["id"] for c in story["bible"]["characters"] if c.get("library")]
            plan["style"] = _video().story_style(story)
            for ps, sc in zip(plan["scenes"], story["scenes"]):
                if sc.get("line_ko"):
                    ps["line"] = f"{chars.get(sc['speaker'], {}).get('name_ko') or sc['speaker']}: {sc['line_ko']}"
                    ps["talk"] = sc.get("kind") == "talk"
        old_mid = (turn.get("work") or {}).get("plan_message_id")
        if old_mid:
            await store.set_message_data(old_mid, {"status": "replanned", "actions": []})
        m = await self.message(turn, "assistant", "plan", "", {"plan": plan, "status": "pending", "actions": PLAN_ACTIONS})
        plan["message_id"] = m["id"]
        await store.merge_work(turn["id"], {"plan_message_id": m["id"]})
        ctx.stage = "plan"
        await store.event(turn["id"], "plan", {"seconds": plan["seconds"], "scenes": len(plan["scenes"]), "size": plan["size"],
                                               "estimate_s": plan["estimate_s"], "long": True, "dialogue": bool(plan.get("dialogue"))})
        if not await self.finish_state(turn, "awaiting_approval", stage="plan", plan=plan):
            await store.set_message_data(m["id"], {"status": "cancelled"})

    def _sub_ctx(self, ctx: Ctx, recipe: dict, tag: str, step: str, pend: str, label: str,
                 scene_phase: bool = False) -> Ctx:
        """긴 영상의 장면 이미지·장면 영상·음악 작업 하나의 ctx (진행 표시는 부모의 long 을 나눠 쓴다)"""
        sub = Ctx(ctx.turn, ctx.user, ctx.env, recipe)
        sub.via, sub.sub, sub.scene_phase = True, True, scene_phase
        sub.job_step, sub.pend_key, sub.client_tag, sub.label = step, pend, tag, label
        sub.spec, sub.notes, sub.first_person, sub.req_text = ctx.spec, ctx.notes, ctx.first_person, ctx.req_text
        sub.stage, sub.text = "queue", "대기 중"
        sub.long = ctx.long
        return sub

    async def _run_sub(self, ctx: Ctx, sub: Ctx, swork: dict, fresh: bool = False):
        """작업 하나를 접수(또는 재시작 전에 접수한 작업을 이어서)하고 결과를 기다린다. 취소면 None.
        취소·예기치 못한 오류로 빠져나가면 ctxs 를 sub 로 남긴다: _on_cancel·_run_guarded 가 지금 작업을 멈추게 (_pipeline_front 와 같다).
        StepError 는 작업이 이미 끝났거나(실패) 접수되지 않은 것이라 부모로 되돌린다 (긴 영상은 그 장면을 다시 할 수 있다)"""
        turn = ctx.turn
        self.ctxs[turn["id"]] = sub            # 취소는 지금 돌고 있는 작업을 멈춘다
        try:
            found = None
            prev = (swork.get(sub.pend_key) or {}).get("client_id")
            if prev and not fresh:
                found = await store.job_by_client(ctx.env["id"], prev)
            if found:
                sub.job_id = found["prompt_id"]
                await store.event(turn["id"], "long_resumed", {"job_id": str(sub.job_id), "key": sub.pend_key})
            else:
                sub.job_id = await self._submit(sub, swork)
            main = await self._watch_job(sub, swork)
            if main is not None:
                sub.close_nodes()
                if sub.node_log:
                    await store.event(turn["id"], "node_timing", {"key": sub.pend_key, "nodes": sub.node_log[:200]})
        except StepError:
            self.ctxs[turn["id"]] = ctx
            raise
        finally:
            self._release(sub)
        self.ctxs[turn["id"]] = ctx
        return main

    async def _save_scene(self, ctx: Ctx, work: dict, scenes: list, i: int, st: dict):
        scenes[i] = st
        work["scenes"] = scenes
        await store.merge_work(ctx.turn["id"], {"scenes": [{k: s[k] for k in LONG_SCENE_KEYS + ("first_att",) if k in s}
                                                           for s in scenes]})

    async def _long_scene(self, ctx: Ctx, work: dict, scenes: list, i: int) -> dict | None:
        turn, story = ctx.turn, work["story"]
        sc = story["scenes"][i]
        st = dict(scenes[i] or {})
        ip = turn.get("client_ip") and str(turn["client_ip"]) or ""
        ctx.long.update(phase="scenes", i=i)
        ctx.motion, ctx.has_fix = False, False
        notes = video_scene_notes(story, i)
        prev = scenes[i - 1] if i > 0 else {}
        chain = prev.get("last_rel") if sc.get("continues") and prev.get("last_rel") and not st.get("no_chain") else None
        if chain and not files.result_path(chain):
            chain = None                      # 앞 장면의 마지막 프레임이 없어졌다: 새 장면 이미지로 시작한다
        await store.event(turn["id"], "long_scene_start", {"index": i, "seconds": sc["seconds"], "chain": bool(chain)})
        direction = video_scene_direction(story, i, prev.get("plan"))
        # 1) 연출 계획표와 (새 장면이면) 장면 이미지 글.
        #    이어지는 장면의 계획표는 여기서 쓰지 않는다: 이어 받은 첫 화면을 읽은 뒤 영상 글을 쓰는 같은 차례 안에서 쓴다
        #    (11-18 3분 실측: 첫 화면을 모른 채 쓴 계획표가 클로즈업 뒤에 미디엄 와이드를 계획해 큰 점프가 났다)
        if (st.get("plan") is None and not chain) or (not chain and not st.get("scene_prompt")):
            with self.deps.llm_context() as bag:
                ctx.bag = bag
                ctx.stage, ctx.text = "prompt", "연출 계획 중"
                await self.emit(ctx, True)
                if st.get("plan") is None and not chain:
                    try:
                        st["plan"] = await self._guard_llm(ctx, self.deps.plan(
                            ctx.user, ip, ctx.env, text=sc["ko"], notes=notes, profile_id=ctx.recipe["video_profile"],
                            seconds=sc["seconds"], staging=None, direction=direction, safe=True))
                    except StepError as e:
                        if e.code == "queue_wait":
                            raise
                        await store.event(turn["id"], "plan_failed", {"index": i, "code": e.code, "detail": e.detail})
                        st["plan"] = None
                if not chain and not st.get("scene_prompt"):
                    ctx.text = "장면 그림 글 쓰는 중"
                    r = await self._guard_llm(ctx, self.deps.expand_image(
                        ctx.user, ip, video_scene_image_text(story, i, st.get("plan")) + "\n" + self.scene_hint(story)))
                    if not (r.get("prompt") or "").strip():
                        raise StepError("llm_error", f"장면 {i + 1} 의 그림 글을 만들지 못했습니다")
                    st["scene_prompt"] = r["prompt"]
                for e in bag:
                    await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
                bag.clear()
            await self._save_scene(ctx, work, scenes, i, st)
        # 2) 첫 화면: 이어지는 장면은 앞 장면 마지막 프레임, 아니면 장면 이미지(Z-Image)
        if not st.get("first_att"):
            if chain:
                att = await self.deps.stage_image(ctx.env, ctx.user, turn, chain)
                st["first_from"] = "last"
            else:
                att = await self._long_image(ctx, work, i, st)
                if att is None:
                    return None
                st["first_from"] = "image"
            st["first_att"] = att["id"]
            await self._save_scene(ctx, work, scenes, i, st)
        # 3) 영상 글 (첫 화면 읽기 → 계획표·설정집을 지킨 네 단락 + 음악 없는 소리 단락)
        if not st.get("prompt"):
            st.update(await self._long_prompt(ctx, work, i, st, notes, direction))
            if st.get("first_from") == "last" and st.get("frame_people") == 0 and video_scene_has_person(story, i):
                # 앞 장면이 사람이 화면 밖으로 나간 채 끝나 이어 받은 첫 화면이 비었다 (11-17 메인 첫 긴 영상). 빈 화면에서 영상 모델이
                # 사람을 새로 지어내게 두지 않고 새 장면 이미지로 다시 시작한다. 빈 첫 화면을 보고 쓴 계획표는 버리고 새로 쓴다
                await store.event(turn["id"], "long_chain_empty", {"index": i})
                st = {"no_chain": True}
                await self._save_scene(ctx, work, scenes, i, st)
                return await self._long_scene(ctx, work, scenes, i)
            await self._save_scene(ctx, work, scenes, i, st)
        # 4) 장면 영상 (실패하면 시드를 바꿔 한 번 더)
        got = await self._long_video(ctx, work, i, st)
        if got is None:
            return None
        st.update(got)
        await self._save_scene(ctx, work, scenes, i, st)
        await store.event(turn["id"], "long_scene_done", {"index": i, "clip": st["clip_rel"], "first_from": st.get("first_from")})
        return st

    def _scene_image_recipe(self, story: dict) -> dict | None:
        """장면 그림 레시피. 인물 라이브러리의 인물이 나오는 영상은 대사가 있든 없든 Qwen-Image 2512 로 그린다: 라이브러리의 그림체(반실사 회화)와
        기준 그림이 그 모델 계열이다. 11-19 메인 시험에서 Z-Image 는 같은 그림체 글에도 광택 있는 디즈니·픽사풍 3D 로 나와, 말하는 장면(기준
        그림 편집)과 한 영상 안에서 화풍이 바뀌었다. 11-20 사용자: 화풍은 대사가 나올 때나 안 나올 때나 항상 같아야 한다 → 대사 없는 긴 영상도
        같은 레시피. 레시피가 꺼져 있으면 지금까지처럼 Z-Image"""
        if any(c.get("library") for c in story["bible"]["characters"]):
            r = self.recipe_for("scene_image")
            if r is not None:
                return r
        return self.recipe_for("image")

    @staticmethod
    def _scene_image_prompt(recipe: dict, story: dict, prompt: str) -> str:
        """Qwen 장면 그림 글 맨 앞에 이야기의 그림체를 붙인다 (손으로 만들 때 쓴 \"Semi-realistic digital painting, 2.5D concept art…\" 앞머리와
        같다). Qwen 은 앞쪽의 그림체 표현에 크게 좌우되므로 글 끝에 같은 문장이 이미 있어도 붙인다. 글이 이미 그 문장으로 시작하면 그대로.
        Z-Image 글은 그대로"""
        style = _video().story_style(story)
        if recipe.get("action") != "scene_image" or not style:
            return prompt
        head = f"{style[:1].upper()}{style[1:].rstrip('.')}. "
        return prompt if prompt.lower().startswith(head.lower()[:40]) else head + prompt

    # ── 그림·클립 자동 검사 (11-20): 사람 수·몸의 이상·화풍이 틀리면 씨앗을 바꿔 다시 만든다 (studio/checks.py) ──
    async def _picture_check(self, ctx: Ctx, rel: str | None, rule, style: str, what: str, index: int,
                             attempt: int) -> tuple[list[str], int | None]:
        """그림 한 장(장면 그림·편집 그림·인물 기준 그림·클립의 검사 프레임)을 시각 모델로 검사해 (다시 만들 까닭, 센 사람 수)를 돌려준다.
        rule 은 checks.rule() 기준(또는 정확히 맞아야 하는 사람 수, None 이면 세지 않음).
        11-20 메인 재시험: 기준 그림을 편집한 첫 화면에 푸토르가 둘로 나왔고, 시각 모델은 그 그림을 사람 2명으로 셌다.
        검사를 껐거나·검사할 그림이 없거나·검사가 실패하면 빈 까닭(통과)이다: 검사가 결과를 막으면 안 된다"""
        if not settings.studio_picture_check or not rel:
            return [], None
        owner, sub, name = files.split_rel(rel)
        trait = checks.style_trait(style)
        ip = str(ctx.turn.get("client_ip") or "")
        ctx.text = "그림 검사 중"
        await self.emit(ctx, True)
        try:
            with self.deps.llm_context() as bag:
                ctx.bag = bag
                insp = await self._guard_llm(ctx, self.deps.inspect(ctx.user, ip, owner, name, sub, [trait] if trait else None))
                for e in bag:
                    await store.link_job(ctx.tid, e["job_id"], "inspect", e["purpose"])
        except StepError as e:
            await store.event(ctx.turn["id"], "picture_check", {"what": what, "index": index, "attempt": attempt,
                                                                "skipped": e.code, "detail": str(e.detail or "")[:200]})
            return [], None
        r = rule if isinstance(rule, dict) else checks.rule(rule)
        why = checks.problems(insp, r, style)
        seen = (insp or {}).get("people")
        seen = seen if isinstance(seen, int) and not isinstance(seen, bool) else None
        await store.event(ctx.turn["id"], "picture_check", {
            "what": what, "index": index, "attempt": attempt, "file": name, "people": seen, "want": r["want"],
            "more_ok": r["more_ok"], "fewer_ok": r["fewer_ok"], "why": why,
            "issues": [str(x.get("type")) for x in (insp or {}).get("issues") or [] if isinstance(x, dict)][:8]})
        return why, seen

    @staticmethod
    def _scene_cast(story: dict, i: int) -> tuple[int, int]:
        """장면 i 에 나오는 설정집 인물 수 (사람, 동물). 같은 id 가 두 번 적혀도 한 번 센다 (그림 검사의 사람 수 기준)"""
        v = _video()
        looks = {c["id"]: c.get("look") or "" for c in story["bible"]["characters"]}
        ids = list(dict.fromkeys(story["scenes"][i].get("characters") or []))
        animals = sum(1 for c in ids if v.is_animal_look(looks.get(c, "")))
        return len(ids) - animals, animals

    @staticmethod
    def _scene_extras(story: dict, i: int) -> bool:
        """장면 글(동작·샷·장소)에 설정집 인물 말고 다른 사람(행인·상인·군중) 말이 있는가"""
        sc = story["scenes"][i]
        place = next((p.get("look") or "" for p in story["bible"]["places"] if p["id"] == sc.get("place")), "")
        return bool(checks.EXTRAS_RE.search(" ".join([sc.get("action") or "", sc.get("shot") or "", place, sc.get("scene_prompt") or ""])))

    @staticmethod
    def _is_animal(story: dict, cid: str | None) -> bool:
        look = next((c.get("look") or "" for c in story["bible"]["characters"] if c["id"] == cid), "")
        return _video().is_animal_look(look)

    async def _check_left(self, ctx: Ctx, work: dict, key: str, why: list[str]):
        """다시 만들어도 남은 검사 문제를 기억한다 (결과에 한 줄로 알린다). 통과하면 지운다. key 는 '장면 번호:무엇'"""
        left = dict(work.get("check_left") or {})
        if why:
            left[key] = why
        elif key in left:
            left.pop(key)
        else:
            return
        work["check_left"] = left
        await store.merge_work(ctx.turn["id"], {"check_left": left})

    async def _long_image_once(self, ctx: Ctx, img_recipe: dict, i: int, swork: dict, pend: str, fresh: bool = False):
        """장면 이미지 작업 한 번 (실패하면 시드를 바꿔 한 번 더: 한 번의 메모리 부족으로 요청 전체를 잃지 않게). 취소면 None"""
        for attempt in (1, 2):
            sub = self._sub_ctx(ctx, img_recipe, f"L{i}i", "scene", pend, "장면 준비", scene_phase=True)
            try:
                return await self._run_sub(ctx, sub, swork, fresh=fresh or attempt > 1)
            except StepError as e:
                if e.code not in LONG_RETRY_CODES or attempt == 2:
                    raise
                await store.event(ctx.turn["id"], "long_image_retry", {"index": i, "code": e.code, "detail": e.detail})
        return None

    async def _long_image(self, ctx: Ctx, work: dict, i: int, st: dict) -> dict | None:
        story = work["story"]
        img_recipe = self._scene_image_recipe(story)
        if img_recipe is None:
            raise StepError("prepare_down")
        vsz = work["long_size"]
        ssz = list(recipes.default_size(img_recipe, photo=(vsz[0], vsz[1])))
        pend = f"L{i}i_pending"
        swork = {"prompt": self._scene_image_prompt(img_recipe, story, st["scene_prompt"]), "size": ssz, pend: work.get(pend)}
        # 사람 수 기준은 장면 크기에 따라 (checks.scene_rule): 설정집 인물만 알아서 행인·상인을 셀 수 없다 (11-20 검토)
        humans, animals = self._scene_cast(story, i)
        v = _video()
        rule = checks.scene_rule(humans, animals, v.shot_rank(story["scenes"][i].get("shot") or ""), self._scene_extras(story, i))
        style = v.story_style(story)
        best, best_why, best_seen = None, None, None
        for check in range(1, STILL_TRIES + 1):
            ctx.text = "장면 준비 중"
            try:
                main = await self._long_image_once(ctx, img_recipe, i, swork, pend, fresh=check > 1)
            except StepError as e:
                if best is None:
                    raise
                # 검사 때문에 다시 만든 작업이 실패했다: 앞서 만든 그림으로 잇는다 (다시 만들기가 영상을 잃게 하면 안 된다)
                await store.event(ctx.turn["id"], "picture_retry_failed", {"what": "scene_image", "index": i, "attempt": check,
                                                                           "code": e.code})
                break
            if main is None:
                return None
            work[pend] = swork.get(pend)
            why, seen = await self._picture_check(ctx, main["rel_path"], rule, style, "scene_image", i, check)
            if checks.better(best_why, why):
                best, best_why, best_seen = main, why, seen
            if not why:
                break
            if check < STILL_TRIES:
                await store.event(ctx.turn["id"], "picture_retry", {"what": "scene_image", "index": i, "attempt": check, "why": why})
        await self._check_left(ctx, work, f"{i}:image", best_why)
        st["image_rel"] = best["rel_path"]
        # 이 그림에서 센 사람 수: 클립 검사의 상한. 그림을 정확히 센 장면만 쓴다 (넓은 장면·행인 장면은 수를 믿을 수 없다)
        st["first_people"] = best_seen if rule.get("want") is not None and not rule.get("more_ok") else None
        return await self.deps.stage_image(ctx.env, ctx.user, ctx.turn, best["rel_path"])

    async def _long_prompt(self, ctx: Ctx, work: dict, i: int, st: dict, notes: str, direction: str = "") -> dict:
        turn, story = ctx.turn, work["story"]
        sc = story["scenes"][i]
        ip = turn.get("client_ip") and str(turn["client_ip"]) or ""
        atts = await store.attachments_of([st["first_att"]])
        img = next((a for a in atts if a["media"] == "image" and a.get("input_rel")), None)
        if img is None:
            raise StepError("no_image")
        image = {"filename": img["input_rel"].split("/")[-1], "subfolder": "/".join(img["input_rel"].split("/")[:-1]),
                 "type": "input"}
        with self.deps.llm_context() as bag:
            ctx.bag = bag
            ctx.motion = True
            ctx.stage, ctx.text = "frame", "움직임 설계 중"
            await self.emit(ctx, True)
            for attempt in (1, 2):
                r = await self._guard_llm(ctx, self.deps.render(
                    ctx.user, ip, ctx.env, text=sc["ko"], notes=notes, style="", profile_id=ctx.recipe["video_profile"],
                    seconds=sc["seconds"], image=image, image_prompt=st.get("scene_prompt") or "", staging=None,
                    plan=st.get("plan"), crew=None, read_frame_on=True, auto_length=True, frame=None, music=False,
                    direction=direction, safe=True))
                f = None
                if needs_fix(r.get("violations")):
                    ctx.has_fix = True
                    ctx.stage = "fix"
                    await self.emit(ctx, True)
                    f = await self._guard_llm(ctx, self.deps.fix(
                        ctx.user, ip, ctx.env, text=sc["ko"], notes=notes, style="", profile_id=r["profile"],
                        seconds=r["settings"]["seconds"], current={"ko": r["ko"], "parts": r["parts"], "motion": r["motion"]},
                        frame=r["frame"], scene=r.get("scene") or "", staging=None, plan=r.get("plan") or st.get("plan")))
                left = (f if f is not None else r).get("violations") or []
                blocking = [v for v in left if BLOCKING_RE.search(v)]
                if not blocking:
                    break
                await store.event(turn["id"], "prompt_rejected", {"index": i, "attempt": attempt, "violations": blocking})
            else:
                for e in bag:
                    await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
                raise StepError("bad_prompt", f"장면 {i + 1}: " + "; ".join(blocking)[:280])
            for e in bag:
                await store.event(turn["id"], "llm_job", {k: e.get(k) for k in LLM_JOB_KEYS})
            bag.clear()
        final = f["prompt"] if f and f.get("changed") else r["prompt"]
        if r.get("sound") and r["sound"] not in final:
            final = f"{final}\n\n{r['sound']}"
        lock = _video().style_lock(story)         # 말하는 장면의 영상 글과 같은 화풍 고정 문장 (11-20)
        if lock and lock not in final:
            final = f"{final}\n\n{lock}"
        await store.event(turn["id"], "render", {"index": i, "profile": r.get("profile"), "violations": r.get("violations"),
                                                 "rounds": r.get("rounds"), "fixed": bool(f), "left": left,
                                                 "frame_read": self.deps.frame_known(r.get("frame"))})
        people = (r.get("frame") or {}).get("people") if isinstance(r.get("frame"), dict) else None
        return {"prompt": final, "plan": r.get("plan") or st.get("plan"),
                "ko": {**(r.get("ko") or {}), **((f or {}).get("ko") or {})}, "profile": r.get("profile"),
                "settings": r.get("settings"), "frame_known": self.deps.frame_known(r.get("frame")), "violations": left,
                "frame_people": people if isinstance(people, int) and not isinstance(people, bool) else None}

    async def _long_video_once(self, ctx: Ctx, recipe: dict, i: int, swork: dict, pend: str, fresh: bool = False) -> dict | None:
        """장면 영상 작업 한 번 (실패하면 시드를 바꿔 한 번 더). 클립·마지막 프레임·검사 프레임. 취소면 None"""
        main, sub = None, None
        for attempt in (1, 2):
            sub = self._sub_ctx(ctx, recipe, f"L{i}v", "generate", pend, "생성")
            try:
                main = await self._run_sub(ctx, sub, swork, fresh=fresh or attempt > 1)
                break
            except StepError as e:
                if e.code not in LONG_RETRY_CODES or attempt == 2:
                    raise
                await store.event(ctx.turn["id"], "long_scene_retry", {"index": i, "code": e.code, "detail": e.detail})
        if main is None:
            return None
        outs = sub.outs or []
        try:
            _n, sha = await asyncio.to_thread(files.fingerprint, main["rel_path"])
        except OSError:
            sha = None
        return {"clip_rel": main["rel_path"], "clip_sha": sha, "last_rel": self._output_rel(recipe, outs, "last"),
                "check_rel": self._output_rel(recipe, outs, "check"), "seed": swork.get("seed")}

    @staticmethod
    def _output_rel(recipe: dict, outs: list, key: str) -> str | None:
        """레시피 outputs 의 key(검사 프레임 check·마지막 프레임 last) 결과 파일 (없으면 None)"""
        o = (recipe.get("outputs") or {}).get(key)
        hit = files.pick_output(outs or [], o["node"], o["kind"], o.get("ext")) if o else None
        return (hit or {}).get("rel_path")

    async def _long_video(self, ctx: Ctx, work: dict, i: int, st: dict) -> dict | None:
        recipe = ctx.recipe
        story = work["story"]
        sc = story["scenes"][i]
        fps = int(recipe["defaults"].get("fps") or 24)
        frames = int((st.get("settings") or {}).get("frames") or (int(sc["seconds"]) * fps + 1))
        pend = f"L{i}v_pending"
        swork = {"prompt": st["prompt"], "scene_att": st["first_att"], "settings": {"frames": frames},
                 "size": list(work["long_size"]), pend: work.get(pend)}
        # 클립 가운데 프레임(검사 프레임)을 검사한다: 영상 모델이 사람을 지어내거나 몸이 겹치거나 화풍이 바뀌면 시드를 바꿔 한 번 더.
        # 사람 수는 첫 화면에서 센 수(장면 그림 검사, 없으면 영상 글을 쓸 때 읽은 첫 화면)보다 많아졌을 때만 본다: 새 시드로는
        # 첫 화면에 있는 사람 수를 바꿀 수 없다 (11-20 검토)
        _humans, animals = self._scene_cast(story, i)
        base = st.get("first_people") if isinstance(st.get("first_people"), int) else None
        rule = checks.clip_rule(base, animals)
        style = _video().story_style(story)
        best, best_why = None, None
        for check in range(1, CLIP_TRIES + 1):
            ctx.text = "생성 대기"
            try:
                got = await self._long_video_once(ctx, recipe, i, swork, pend, fresh=check > 1)
            except StepError as e:
                if best is None:
                    raise
                await store.event(ctx.turn["id"], "picture_retry_failed", {"what": "clip", "index": i, "attempt": check, "code": e.code})
                break
            if got is None:
                return None
            work[pend] = swork.get(pend)
            why, _seen = await self._picture_check(ctx, got.get("check_rel"), rule, style, "clip", i, check)
            if checks.better(best_why, why):
                best, best_why = got, why
            if not why:
                break
            if check < CLIP_TRIES:
                await store.event(ctx.turn["id"], "picture_retry", {"what": "clip", "index": i, "attempt": check, "why": why})
        await self._check_left(ctx, work, f"{i}:clip", best_why)
        return {**best, "seconds": sc["seconds"]}

    async def _long_music(self, ctx: Ctx, work: dict):
        """배경음악 한 곡 (rel). 레시피가 없거나 실패하면 None (음악 없이 합친다), 취소면 False"""
        turn, story = ctx.turn, work["story"]
        if work.get("music_rel") and files.result_path(work["music_rel"]):
            return work["music_rel"]
        music = story.get("music") or {}
        mrec = self.recipe_for("music")
        if mrec is None or not music.get("tags"):
            await store.event(turn["id"], "long_music", {"skipped": "음악 레시피가 없음" if mrec is None else "구성안에 음악 없음"})
            return None
        ctx.long["phase"] = "music"
        ctx.text = "배경음악 만드는 중"
        await self.emit(ctx, True)
        sub = self._sub_ctx(ctx, mrec, "M", "music", "music_pending", "배경음악")
        total = float(sum(int(s["seconds"]) for s in story["scenes"]))
        swork = {"prompt": music["tags"], "duration": min(400.0, total + 2.0), "bpm": music.get("bpm"), "keyscale": music.get("key"),
                 "music_pending": work.get("music_pending")}
        try:
            main = await self._run_sub(ctx, sub, swork)
        except StepError as e:
            if e.code in ("queue_wait",):
                raise
            await store.event(turn["id"], "long_music", {"failed": e.code, "detail": e.detail})
            return None
        if main is None:
            return False
        work["music_rel"] = main["rel_path"]
        await store.merge_work(turn["id"], {"music_rel": main["rel_path"]})
        await store.event(turn["id"], "long_music", {"rel": main["rel_path"]})
        return main["rel_path"]

    async def _long_finish(self, ctx: Ctx, work: dict, scenes: list, music_rel):
        turn, story = ctx.turn, work["story"]
        ctx.long["phase"] = "assemble"
        ctx.stage, ctx.text = "collect", "영상 합치는 중"
        await self.emit(ctx, True)
        if await store.cancel_requested(turn["id"]):
            await self.finish_state(turn, "cancelled")
            return
        clips = await self._long_clips(scenes)
        music_abs = files.result_path(music_rel) if music_rel else None
        out_rel = f"{os.path.dirname(scenes[0]['clip_rel'])}/long_{str(turn['id'])[:8]}.mp4"
        out_abs = os.path.join(os.path.realpath(settings.results_dir), out_rel)
        try:
            # 앞 클립의 마지막 프레임에서 이어 만든 장면과의 이음새는 짧은 디졸브로 움직임 끊김을 가리고,
            # 장면이 바뀌는 이음새는 일부러 컷으로 보이게 둔다 (11-18 3분 실측: 10초마다 끊김)
            seams = [DISSOLVE_SECONDS if (scenes[i + 1].get("first_from") == "last") else 0.0 for i in range(len(scenes) - 1)]
            info = await asyncio.to_thread(self.deps.assemble, clips, music_abs, out_abs, True, seams)
        except assemble.AssembleError as e:
            await store.event(turn["id"], "error", {"where": "assemble", "detail": str(e)[:500]})
            raise StepError("assemble", str(e)[:300]) from None
        await store.event(turn["id"], "long_assembled", info)
        await self._long_deliver(ctx, work, scenes, out_rel, music_rel, [])

    async def _long_clips(self, scenes: list) -> list[str]:
        """합칠 장면 클립의 절대 경로. 없거나 지문이 바뀐 클립이 있으면 StepError (다시 시도하면 그 장면부터)"""
        clips = []
        for i, s in enumerate(scenes):
            p = files.result_path(s.get("clip_rel") or "")
            if p is None:
                raise StepError("gone", f"장면 {i + 1} 클립이 없습니다")
            if s.get("clip_sha") and not await self._long_file_ok(s["clip_rel"], s["clip_sha"]):
                raise StepError("changed", f"장면 {i + 1} 클립이 바뀌었습니다")
            clips.append(p)
        return clips

    async def _long_deliver(self, ctx: Ctx, work: dict, scenes: list, out_rel: str, music_rel, extra_notes: list):
        """합친 영상을 결과 첨부로 남기고 채팅에 낸다"""
        turn, story = ctx.turn, work["story"]
        await store.merge_work(turn["id"], {"final_rel": out_rel})
        try:
            nbytes, sha = await asyncio.to_thread(files.fingerprint, out_rel)
        except OSError:
            nbytes, sha = None, None
        import uuid
        size = work.get("long_size") or []
        att = {"id": uuid.uuid4(), "thread_id": turn["thread_id"], "user_id": turn["user_id"], "source": "result",
               "media": "video", "mime": files.mime_of(out_rel), "width": size[0] if size else None,
               "height": size[1] if len(size) > 1 else None, "frames": None, "bytes": nbytes, "sha256": sha,
               "result_rel": out_rel, "job_id": None,
               "meta": {"long": True, "scenes": len(scenes), **({"dialogue": True} if story.get("dialogue") else {})}}
        await store.add_attachment(att)
        notes = list(ctx.notes) + list((ctx.spec or {}).get("notes") or []) + list(extra_notes or [])
        # 다시 만들어도 자동 검사를 통과하지 못한 장면 (11-20): 가장 나은 결과로 이었다고 알린다
        left = {}
        for key, why in (work.get("check_left") or {}).items():
            idx = str(key).split(":")[0]
            if idx.isdigit() and checks.describe(why):
                left.setdefault(int(idx), []).append(checks.describe(why))
        for idx in sorted(left):
            notes.append(f"장면 {idx + 1}: 다시 만들어도 자동 검사에서 {'·'.join(dict.fromkeys(left[idx]))} 문제가 남아 가장 나은 결과로 이었습니다")
        portrait = [checks.describe(why) for key, why in (work.get("check_left") or {}).items()
                    if str(key).startswith("c") and checks.describe(why)]
        if portrait:
            notes.append(f"인물 기준 그림: 다시 그려도 자동 검사에서 {'·'.join(dict.fromkeys(portrait))} 문제가 남아 가장 나은 그림을 썼습니다")
        if not music_rel and (story.get("music") or {}).get("tags"):
            notes.append("배경음악을 만들지 못해 음악 없이 이어 붙였습니다")
        ctx.text = "마무리"
        await self.emit(ctx, True)
        msg = await self.message(ctx, "assistant", "result", "", {"attachment_ids": [str(att["id"])], "notes": notes,
                                                                  "media": "video", "actions": RETRY})
        await self.finish_state(turn, "done", result={"attachment_ids": [str(att["id"])], "message_id": msg["id"],
                                                      "notices": notes, "media": "video"})

    # ── 대사 영상: 목소리 → 대사 녹음 → 인물 기준 그림 → 장면(말하는 장면은 입 맞춤) → 음악 → 합치기(대사·자막) (11-19) ──
    async def _dlg_job(self, ctx: Ctx, recipe: dict, tag: str, pend: str, label: str, swork: dict, step: str,
                       scene_phase: bool = False, fresh: bool = False):
        """대사 영상의 작업 하나 (실패하면 시드를 바꿔 한 번 더). 취소면 None. 결과에 그 작업의 모든 출력(outs)을 붙인다
        (입 맞춤 클립의 검사 프레임을 고르려고)"""
        for attempt in (1, 2):
            sub = self._sub_ctx(ctx, recipe, tag, step, pend, label, scene_phase=scene_phase)
            try:
                main = await self._run_sub(ctx, sub, swork, fresh=fresh or attempt > 1)
                return None if main is None else {**main, "outs": list(sub.outs or [])}
            except StepError as e:
                if e.code not in LONG_RETRY_CODES or attempt == 2:
                    raise
                await store.event(ctx.turn["id"], "dialogue_retry", {"tag": tag, "code": e.code, "detail": e.detail})
        return None

    def _dlg_has(self, ctx: Ctx, rec) -> bool:
        return bool(rec and rec.get("in_rel") and files.input_path(ctx.env, rec["in_rel"]))

    async def _dlg_stage_audio(self, ctx: Ctx, result_rel: str, name: str, fx: str | None = None,
                               level: bool = False, max_seconds: float | None = None) -> tuple[str, float]:
        """녹음 결과(결과 폴더 mp3)의 앞뒤 무음을 잘라 입력 폴더에 WAV 로 둔다 (입 맞춤의 LoadAudio·목소리 복제의 기준·합치기가 쓴다).
        fx 는 인물 라이브러리의 목소리 후처리(저음 보강 등), level 은 소리 크기 맞추기: 둘 다 대사 줄에만 쓰고 복제의 기준 목소리에는
        쓰지 않는다 (기준 목소리가 이미 처리돼 있으면 줄마다 두 번 걸린다)"""
        src = files.result_path(result_rel)
        if src is None:
            raise StepError("gone", f"녹음 파일이 없습니다: {result_rel}")
        in_rel = files.turn_input_rel(ctx.user["username"], ctx.user["id"], ctx.turn["id"], name)
        dst = files.input_write_path(ctx.env, in_rel)
        try:
            extra = ({"fx": fx} if fx else {}) | ({"level": True} if level else {}) | ({"max_seconds": max_seconds} if max_seconds else {})
            sec = await asyncio.to_thread(lambda: self.deps.trim_voice(src, dst, **extra))
        except assemble.AssembleError as e:
            raise StepError("run_error", f"녹음 다듬기 실패: {e}") from None
        files.own_input(ctx.env, dst)
        return in_rel, float(sec)

    async def _dlg_voices(self, ctx: Ctx, work: dict) -> bool:
        """인물마다 기준 목소리 하나를 정하고, 대사는 그 소리의 나이·말투·감정을 따라(맥락 복제) 한 줄씩 녹음한다. 녹음 길이에 맞춰 장면 길이를 고친다.
        11-19 숲길 단편: 줄마다 목소리를 설계하면 컷마다 목소리가 바뀌었다 → 기준 목소리 하나를 복제해 고정. 여러 줄을 한 작업에
        넣으면 한 줄이 늘어져 전체가 멈췄다 → 한 줄에 한 작업, 토큰 상한, 너무 길면 시드를 바꿔 한 번 더.
        11-19 메인 시험 2: 인사말로 새로 설계한 기준 소리의 '목소리 특징만' 복제했더니 80세 푸토르가 젊은 남자 목소리로, 감정 없이
        책 읽는 투로 나왔다 → ① 인물 라이브러리에 승인된 기준 소리(voice_ref)가 있으면 영상마다 설계하지 않고 그 소리를 쓰고,
        ② 없으면 그 인물이 할 법한 말(sample_ko)로 설계하고, ③ 복제는 기준 소리와 그 글을 함께 주는 맥락 복제로 한다. 취소면 False"""
        turn, story = ctx.turn, work["story"]
        v = _video()
        vrec, crec = self.recipe_for("voice"), self.recipe_for("voice_clone")
        if crec is None:
            raise StepError("unavailable", "대사 녹음 레시피가 꺼져 있습니다")
        speakers = v.speakers_of(story)
        chars = {c["id"]: c for c in story["bible"]["characters"]}
        fx_of = {cid: c.get("voice_fx") for cid, c in chars.items()}
        idx = [i for i, sc in enumerate(story["scenes"]) if sc.get("line_ko")]
        voices, lines = dict(work.get("voices") or {}), dict(work.get("lines") or {})
        L = ctx.long
        L.update(phase="voices", vn=len(speakers) + len(idx))

        def done() -> int:
            return (sum(1 for c in speakers if self._dlg_has(ctx, voices.get(c)))
                    + sum(1 for i in idx if self._dlg_has(ctx, lines.get(str(i)))))
        for k, cid in enumerate(speakers):
            if self._dlg_has(ctx, voices.get(cid)):
                continue
            L["vdone"] = done()
            c = chars.get(cid) or {}
            ref_rel, ref_text = c.get("voice_ref"), str(c.get("voice_ref_text") or "").strip()
            if ref_rel and ref_text and files.result_path(ref_rel):
                # 인물 라이브러리의 승인된 기준 소리: 영상마다 설계하지 않고 그대로 쓴다 (같은 목소리가 영상마다 같다)
                in_rel, sec = await self._dlg_stage_audio(ctx, ref_rel, f"voice_{k}.wav")
                voices[cid] = {"rel": ref_rel, "in_rel": in_rel, "seconds": sec, "text": ref_text, "library": True}
                work["voices"] = voices
                await store.merge_work(turn["id"], {"voices": voices})
                await store.event(turn["id"], "dialogue_voice", {"speaker": cid, "seconds": sec, "library": True})
                continue
            if ref_rel:
                await store.event(turn["id"], "dialogue_voice_ref_missing", {"speaker": cid, "ref": ref_rel})
            if vrec is None:
                raise StepError("unavailable", "대사 녹음 레시피가 꺼져 있습니다")
            ctx.text = "목소리 만드는 중"
            await self.emit(ctx, True)
            pend = f"V{k}_pending"
            sample = v.voice_sample(story, cid)
            for attempt in (1, 2):
                swork = {"prompt": sample, "instruct": v.voice_instruct(story, cid), "max_tokens": v.VOICE_REF_TOKENS,
                         pend: work.get(pend)}
                main = await self._dlg_job(ctx, vrec, f"V{k}", pend, "목소리", swork, "voice", fresh=attempt > 1)
                if main is None:
                    return False
                work[pend] = swork.get(pend)
                in_rel, sec = await self._dlg_stage_audio(ctx, main["rel_path"], f"voice_{k}.wav")
                # 맥락 복제는 기준 소리와 글이 맞아야 해서 소리를 잘라 쓰지 못한다: 너무 늘어졌으면 시드를 바꿔 한 번 더 설계한다
                if sec <= v.voice_ref_max_seconds(sample) or attempt == 2:
                    break
                await store.event(turn["id"], "dialogue_voice_retry", {"speaker": cid, "seconds": sec, "reason": "too_long"})
            voices[cid] = {"rel": main["rel_path"], "in_rel": in_rel, "seconds": sec, "text": sample}
            work["voices"] = voices
            await store.merge_work(turn["id"], {"voices": voices})
            await store.event(turn["id"], "dialogue_voice", {"speaker": cid, "seconds": sec})
        changed = False
        for i in idx:
            sc = story["scenes"][i]
            if not self._dlg_has(ctx, lines.get(str(i))):
                L["vdone"] = done()
                ctx.text = "대사 녹음 중"
                await self.emit(ctx, True)
                pend = f"T{i}_pending"
                got = None
                ref = voices[sc["speaker"]]
                for attempt in (1, 2):
                    swork = {"prompt": sc["line_ko"], "audio_in": ref["in_rel"], "ref_text": ref.get("text") or v.VOICE_REF_TEXT,
                             "x_vector": settings.studio_voice_clone == "xvector",
                             "max_tokens": v.line_tokens(sc["line_ko"]), pend: work.get(pend)}
                    main = await self._dlg_job(ctx, crec, f"T{i}", pend, "대사 녹음", swork, "voice", fresh=attempt > 1)
                    if main is None:
                        return False
                    work[pend] = swork.get(pend)
                    try:
                        in_rel, sec = await self._dlg_stage_audio(ctx, main["rel_path"], f"line_{i}.wav",
                                                                  fx_of.get(sc["speaker"]), level=True)
                    except StepError as e:
                        if attempt == 2:
                            raise
                        await store.event(turn["id"], "dialogue_line_retry", {"index": i, "code": e.code, "detail": e.detail})
                        continue
                    got = {"rel": main["rel_path"], "in_rel": in_rel, "seconds": sec}
                    if sec <= v.line_max_seconds(sc["line_ko"]) or attempt == 2:
                        break
                    # 대사보다 훨씬 길다: 말이 끝나지 않고 늘어진 녹음이다 (11-19 숲길 L3: 10초 대사가 32초)
                    await store.event(turn["id"], "dialogue_line_retry", {"index": i, "seconds": sec, "reason": "too_long"})
                lines[str(i)] = got
                work["lines"] = lines
                await store.merge_work(turn["id"], {"lines": lines})
                await store.event(turn["id"], "dialogue_line", {"index": i, "seconds": got["seconds"]})
            sec = float(lines[str(i)]["seconds"])
            want = math.ceil(sec + TALK_PAD)
            want = min(20, max(4, want) if sc.get("kind") == "talk" else max(int(sc["seconds"]), want))
            if want != sc["seconds"]:
                sc["seconds"] = want
                changed = True
        before = [s["seconds"] for s in story["scenes"]]
        if v.stretch_scenes(story, float((ctx.spec or {}).get("seconds") or 0)):         # 대사가 짧아 요청 길이보다 짧아졌다
            changed = True
            await store.event(turn["id"], "dialogue_stretch", {"before": before, "after": [s["seconds"] for s in story["scenes"]],
                                                              "target": (ctx.spec or {}).get("seconds")})
        if changed:
            story["seconds"] = sum(int(s["seconds"]) for s in story["scenes"])
            work["story"] = story
            await store.merge_work(turn["id"], {"story": story})
            await store.event(turn["id"], "dialogue_timing", {"seconds": [s["seconds"] for s in story["scenes"]]})
        L["vdone"] = len(speakers) + len(idx)
        return True

    async def _dlg_still(self, ctx: Ctx, work: dict, recipe: dict, tag: str, pend: str, label: str, swork: dict, what: str,
                         index: int, key: str, cid: str | None = None) -> dict | None:
        """대사 영상의 그림 한 장(인물 기준 그림·기준 그림을 고친 첫 화면)을 검사하며 만든다. 둘 다 한 사람만 나와야 한다
        (인물이 동물이면 사람 수와 팔다리는 보지 않는다). 틀리면 씨앗을 바꿔 다시 만들고(STILL_TRIES), 다 틀리면 가장 나은 그림을 쓰고
        남은 문제를 기억한다. 결과에 센 사람 수(seen)를 붙인다. 취소면 None"""
        story = work["story"]
        style = _video().story_style(story)
        rule = checks.rule(None, skip=checks.ANIMAL_SKIP) if self._is_animal(story, cid) else checks.ONE
        best, best_why, best_seen = None, None, None
        for check in range(1, STILL_TRIES + 1):
            ctx.text = label + " 중"
            try:
                main = await self._dlg_job(ctx, recipe, tag, pend, label, swork, "scene", scene_phase=True, fresh=check > 1)
            except StepError as e:
                if best is None:
                    raise
                await store.event(ctx.turn["id"], "picture_retry_failed", {"what": what, "index": index, "attempt": check, "code": e.code})
                break
            if main is None:
                return None
            work[pend] = swork.get(pend)
            why, seen = await self._picture_check(ctx, main["rel_path"], rule, style, what, index, check)
            if checks.better(best_why, why):
                best, best_why, best_seen = main, why, seen
            if not why:
                break
            if check < STILL_TRIES:
                await store.event(ctx.turn["id"], "picture_retry", {"what": what, "index": index, "attempt": check, "why": why})
        await self._check_left(ctx, work, key, best_why)
        return {**best, "seen": best_seen}

    async def _dlg_casts(self, ctx: Ctx, work: dict) -> bool:
        """혼자 가까이 나오는 인물(말하는 사람·듣는 얼굴)의 기준 그림을 한 장씩 만든다. 그 장면들의 첫 화면은 이 그림을 고쳐 만들어
        얼굴을 맞춘다 (11-19 시범: 같은 반신 사진에서 편집한 첫 화면 6장이 같은 얼굴). 취소면 False"""
        story = work["story"]
        v = _video()
        need = v.cast_ids(story)
        if not need:
            return True
        # 기준 그림을 새로 그릴 때(라이브러리의 기준 그림 파일이 없을 때)도 장면 그림과 같은 모델 계열로 그려 화풍이 갈리지 않게 한다
        img = self._scene_image_recipe(story)
        if img is None:
            raise StepError("prepare_down")
        casts = dict(work.get("casts") or {})
        vsz = work["long_size"]
        ssz = list(recipes.default_size(img, photo=(vsz[0], vsz[1])))
        refs = {c["id"]: c.get("ref") for c in story["bible"]["characters"]}
        ctx.long.update(phase="scenes", i=0)
        for k, cid in enumerate(need):
            c = dict(casts.get(cid) or {})
            if c.get("att") and await store.attachments_of([c["att"]]):
                continue
            if not (c.get("rel") and files.result_path(c["rel"])) and refs.get(cid) and files.result_path(refs[cid]):
                # 인물 라이브러리의 기준 그림이 있으면 새로 그리지 않고 그 그림을 쓴다 (손으로 만든 니키·푸토르와 같은 얼굴)
                c = {"rel": refs[cid], "library": True}
            if not (c.get("rel") and files.result_path(c["rel"])):
                if refs.get(cid):
                    await store.event(ctx.turn["id"], "dialogue_cast_ref_missing", {"character": cid, "ref": refs[cid]})
                ctx.text = "인물 준비 중"
                await self.emit(ctx, True)
                pend = f"C{k}_pending"
                swork = {"prompt": self._scene_image_prompt(img, story, v.cast_prompt(story, cid)), "size": ssz, pend: work.get(pend)}
                main = await self._dlg_still(ctx, work, img, f"C{k}", pend, "인물 준비", swork, "portrait", k, f"c{k}:portrait", cid)
                if main is None:
                    return False
                c = {"rel": main["rel_path"]}
            att = await self.deps.stage_image(ctx.env, ctx.user, ctx.turn, c["rel"])
            c["att"] = att["id"]
            casts[cid] = c
            work["casts"] = casts
            await store.merge_work(ctx.turn["id"], {"casts": casts})
            await store.event(ctx.turn["id"], "dialogue_cast", {"character": cid, "rel": c["rel"], "library": bool(c.get("library"))})
        return True

    async def _dlg_scene(self, ctx: Ctx, work: dict, scenes: list, i: int) -> dict | None:
        """인물 한 명이 가까이 나오는 장면. 첫 화면은 기준 그림을 이 장면으로 고친 그림.
        말하는 장면은 그 대사 소리로 입을 맞추고(IA2V), 듣는 얼굴 같은 장면은 긴 영상과 같은 길(계획표 → 영상 글 → LTX)로 만든다"""
        turn, story = ctx.turn, work["story"]
        v = _video()
        sc = story["scenes"][i]
        st = dict(scenes[i] or {})
        cid = v.cast_scene(story, i)
        ctx.long.update(phase="scenes", i=i)
        ctx.motion, ctx.has_fix = False, False
        if not st.get("first_att"):
            await store.event(turn["id"], "long_scene_start", {"index": i, "seconds": sc["seconds"], "chain": False, "cast": cid,
                                                               "talk": sc.get("kind") == "talk"})
            cast = (work.get("casts") or {}).get(cid) or {}
            if not cast.get("att"):
                raise StepError("no_image", f"장면 {i + 1}: 인물 기준 그림이 없습니다")
            erec = self.recipe_for("edit")
            if erec is None:         # 편집 레시피가 꺼졌다: 기준 그림을 그대로 첫 화면으로 (배경은 다르지만 얼굴은 같다)
                st.update(first_att=cast["att"], first_from="cast")
            else:
                pend = f"L{i}e_pending"
                swork = {"prompt": v.cast_edit_prompt(story, i), "scene_att": cast["att"], pend: work.get(pend)}
                main = await self._dlg_still(ctx, work, erec, f"L{i}e", pend, "장면 준비", swork, "edit", i, f"{i}:image", cid)
                if main is None:
                    return None
                att = await self.deps.stage_image(ctx.env, ctx.user, turn, main["rel_path"])
                st.update(first_att=att["id"], first_from="edit", edit_rel=main["rel_path"], scene_prompt=swork["prompt"],
                          first_people=main.get("seen"))
            await self._save_scene(ctx, work, scenes, i, st)
        if sc.get("kind") != "talk":
            return await self._long_scene(ctx, work, scenes, i)
        trec = self.recipe_for("talk")
        if trec is None:
            raise StepError("unavailable", "입 맞춤 레시피가 꺼져 있습니다")
        line = (work.get("lines") or {}).get(str(i)) or {}
        if not self._dlg_has(ctx, line):
            raise StepError("gone", f"장면 {i + 1}의 대사 소리가 없습니다")
        st["prompt"] = st.get("prompt") or v.talk_prompt(story, i)
        fps = int(trec["defaults"].get("fps") or 24)
        pend = f"L{i}v_pending"
        swork = {"prompt": st["prompt"], "scene_att": st["first_att"], "audio_in": line["in_rel"],
                 "settings": {"frames": int(sc["seconds"]) * fps + 1}, "size": list(work["long_size"]), pend: work.get(pend)}
        style = v.story_style(story)
        rule = checks.clip_rule(st.get("first_people"), 1 if self._is_animal(story, cid) else 0, talk=True)
        best, best_why = None, None
        for check in range(1, CLIP_TRIES + 1):
            ctx.text = "생성 대기"
            try:
                main = await self._dlg_job(ctx, trec, f"L{i}v", pend, "입 맞춤", swork, "generate", fresh=check > 1)
            except StepError as e:
                if best is None:
                    raise
                await store.event(turn["id"], "picture_retry_failed", {"what": "talk_clip", "index": i, "attempt": check, "code": e.code})
                break
            if main is None:
                return None
            work[pend] = swork.get(pend)
            main = {**main, "seed": swork.get("seed")}
            why, _seen = await self._picture_check(ctx, self._output_rel(trec, main.get("outs"), "check"), rule, style, "talk_clip", i, check)
            if checks.better(best_why, why):
                best, best_why = main, why
            if not why:
                break
            if check < CLIP_TRIES:
                await store.event(turn["id"], "picture_retry", {"what": "talk_clip", "index": i, "attempt": check, "why": why})
        await self._check_left(ctx, work, f"{i}:clip", best_why)
        main = best
        try:
            _n, sha = await asyncio.to_thread(files.fingerprint, main["rel_path"])
        except OSError:
            sha = None
        st.update(clip_rel=main["rel_path"], clip_sha=sha, seed=main.get("seed"), seconds=sc["seconds"])
        await self._save_scene(ctx, work, scenes, i, st)
        await store.event(turn["id"], "long_scene_done", {"index": i, "clip": st["clip_rel"], "first_from": st.get("first_from"),
                                                          "talk": True})
        return st

    async def _dlg_finish(self, ctx: Ctx, work: dict, scenes: list, music_rel):
        """대사 영상 합치기: 샷 사이 디졸브, 대사 소리를 샷 시작 + 0.2초에, 자막(투명 PNG), 배경음악. 말하는 샷은 대사 끝에서 자른다.
        장면 클립 안의 소리는 쓰지 않는다 (11-19 사용자 확인 설정, assemble.run_dialogue)"""
        turn, story = ctx.turn, work["story"]
        ctx.long["phase"] = "assemble"
        ctx.stage, ctx.text = "collect", "영상 합치는 중"
        await self.emit(ctx, True)
        if await store.cancel_requested(turn["id"]):
            await self.finish_state(turn, "cancelled")
            return
        clips = await self._long_clips(scenes)
        music_abs = files.result_path(music_rel) if music_rel else None
        out_rel = f"{os.path.dirname(scenes[0]['clip_rel'])}/dialogue_{str(turn['id'])[:8]}.mp4"
        out_abs = os.path.join(os.path.realpath(settings.results_dir), out_rel)
        size = work.get("long_size") or [1280, 704]
        lines_map = work.get("lines") or {}
        items, trims, notes, subs_ok = [], [], [], True
        for i, sc in enumerate(story["scenes"]):
            ln = lines_map.get(str(i))
            trims.append(max(TALK_MIN_SHOT, float(ln["seconds"]) + TALK_TAIL) if ln and sc.get("kind") == "talk" else None)
            if not ln:
                continue
            path = files.input_path(ctx.env, ln.get("in_rel") or "")
            if path is None:
                raise StepError("gone", f"장면 {i + 1}의 대사 소리가 없습니다")
            item = {"file": path, "shot": i}
            if subs_ok:
                sub_out = os.path.join(os.path.dirname(out_abs), f"sub_{str(turn['id'])[:8]}_{i}.png")
                try:
                    sub = await asyncio.to_thread(self.deps.render_subtitle, sc["line_ko"], size, sub_out)
                except Exception as e:  # noqa: BLE001 - 자막을 못 그려도 영상은 낸다
                    await store.event(turn["id"], "error", {"where": "subtitle", "detail": str(e)[:300]})
                    sub = None
                if sub:
                    item["sub"] = sub
                else:
                    subs_ok = False
            items.append(item)
        if not subs_ok:
            items = [{k: x for k, x in it.items() if k != "sub"} for it in items]
            notes.append("자막을 그리지 못해 자막 없이 이어 붙였습니다")
        try:
            info = await asyncio.to_thread(self.deps.assemble_dialogue, clips, items, music_abs, out_abs, trims)
        except assemble.AssembleError as e:
            await store.event(turn["id"], "error", {"where": "assemble", "detail": str(e)[:500]})
            raise StepError("assemble", str(e)[:300]) from None
        await store.event(turn["id"], "long_assembled", {**(info or {}), "dialogue": True})
        await self._long_deliver(ctx, work, scenes, out_rel, music_rel, notes)

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
                                           "frame_known", "crew", "scene_prompt", "staging", "plan") if k in sw}
                if ctx.via:
                    # 장면 이미지는 새로 만드니, 영상 프롬프트도 그 이미지를 보고 새로 쓴다 (옛 이미지의 세부가 남지 않게)
                    work = {k: v for k, v in work.items() if k in ("scene_prompt", "crew", "staging", "plan")}
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
            if (turn.get("request") or {}).get("long"):
                recipe = self.recipe_for("i2v", self.quality_of_turn(turn), True)     # 긴 영상은 LTX 만 (높음이 꺼지면 빠른 LTX)
            else:
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
            old = turn.get("work") or {}
            keep = ({"story": old["story"]} if (turn.get("request") or {}).get("long") and old.get("story") else None)
            if not await store.replan_turn(turn["id"], first_stage(turn.get("action")), recipe["id"], recipe["version"], keep):
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
                    image_prompt=work.get("scene_prompt") or "", staging=work.get("staging"), plan=work.get("plan"),
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
                        scene=r.get("scene") or "", staging=work.get("staging"),
                        plan=r.get("plan") or work.get("plan")))
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
                "frame": r.get("frame"), "plan": r.get("plan") or work.get("plan")}
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
        # 장면 이미지(s)와 영상(g)은 이름이 겹치지 않는다. 긴 영상은 장면·종류마다 표시가 다르다 (L3i·L3v·M)
        tag = ctx.client_tag or ("s" if ctx.scene_phase else "g")
        client_id = f"studio-{turn['id']}-{tag}{attempt}"
        prefix_slots = recipes.slots_of(recipe, "save_prefix")
        values = {"prompt": work["prompt"], "seed": seed,
                  "save_prefix": [recipes.prefix_value(s, env_user(ctx), ctx.user["id"], str(turn["id"])) for s in prefix_slots]}
        if "duration" in inject:        # 음악: 길이 자리가 둘(빈 잠재·글 인코더)이라 같은 값을 둘에
            values["duration"] = [float(work["duration"])] * len(recipes.slots_of(recipe, "duration"))
        if "bpm" in inject:
            values["bpm"] = int(work.get("bpm") or 90)
        if "keyscale" in inject:
            values["keyscale"] = str(work.get("keyscale") or "C major")
        if "audio" in inject:          # 목소리 복제의 기준 목소리, 입 맞춤의 대사 소리 (입력 폴더 상대 경로)
            if not work.get("audio_in"):
                raise StepError("gone", "소리 파일이 없습니다")
            values["audio"] = work["audio_in"]
        if "voice_instruct" in inject:
            values["voice_instruct"] = str(work.get("instruct") or "A natural, clear Korean speaking voice.")
        if "max_tokens" in inject:
            values["max_tokens"] = int(work.get("max_tokens") or 512)
        if "ref_text" in inject:       # 맥락 복제: 기준 소리가 말하는 글 (소리와 다르면 어긋나므로 반드시 있어야 한다)
            if not str(work.get("ref_text") or "").strip():
                raise StepError("gone", "기준 소리의 글이 없습니다")
            values["ref_text"] = str(work["ref_text"])
        if "x_vector" in inject:       # True 면 기준 소리의 목소리 특징만 복제하고(글 불필요), False 면 기준 소리의 말투까지 따르는 맥락 복제
            values["x_vector"] = bool(work.get("x_vector"))
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
        # 긴 영상의 장면 작업은 접수 정보만 남긴다 (요청 전체의 size·seed 를 장면 값으로 덮지 않게)
        await store.merge_work(turn["id"], {ctx.pend_key: pend} if ctx.sub else
                               {"size": list(size), "seed": seed, ctx.pend_key: pend})
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
                    ctx.text = "장면 준비 중" if ctx.scene_phase else f"{ctx.label} {ctx.tracker.pct}%"
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
        if ctx.scene_phase or ctx.sub:       # 장면 이미지·긴 영상의 장면: 채팅에는 내지 않고 다음 단계가 쓴다
            ctx.outs = outs
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
            if (t.get("request") or {}).get("long"):
                # 긴 영상은 장면마다 기록이 남아 끝난 장면을 건너뛰고 접수한 작업을 이어서 본다. 재시작이 되풀이되면 멈춘다
                n = int((t.get("work") or {}).get("long_resumes") or 0)
                if n < LONG_RESUME_MAX:
                    await store.merge_work(t["id"], {"long_resumes": n + 1})
                    await store.event(t["id"], "recovered", {"result": "resumed", "long": True, "count": n + 1})
                    self.spawn(t, resume=True)
                    continue
                await store.event(t["id"], "recovered", {"result": "interrupted", "long": True})
                if await self.finish_state(t, "interrupted"):
                    await self.message(t, "assistant", "notice", FAIL_TEXT["interrupted"][0],
                                       {"level": "warn", "actions": RETRY_FULL})
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
    if recipe.get("negative_source") == "none":       # 목소리 레시피: 글은 말할 대사다 (positive·negative 가 없다)
        return None
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
