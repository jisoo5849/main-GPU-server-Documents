"""허브 설정. 값은 모두 .env 에서 온다 (compose.yaml 의 env_file)."""
import os
import urllib.parse


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # 비밀번호에 @ / # % 같은 글자가 있어도 되도록 여기서 조립한다
    database_url = _env("DATABASE_URL") or (
        f"postgresql://mgs:{urllib.parse.quote(_env('POSTGRES_PASSWORD', 'mgs'), safe='')}@"
        f"{_env('POSTGRES_HOST', '127.0.0.1')}:5432/mgs")

    envs_dir = _env("MGS_ENVS", "/srv/envs")
    models_dir = _env("MGS_MODELS", "/srv/models")
    results_dir = _env("MGS_RESULTS", "/srv/results")
    runtime_dir = _env("MGS_RUNTIME", "/opt/mgs")
    group_gid = _int("MGS_GID", 2000)

    public_host = _env("HUB_PUBLIC_HOST", "main")
    portal_port = _int("HUB_PORTAL_PORT", 80)
    llm_port = _int("HUB_LLM_PORT", 11434)
    env_port_start = _int("HUB_ENV_PORT_START", 8101)
    env_port_end = _int("HUB_ENV_PORT_END", 8130)

    runtime_image = _env("COMFY_RUNTIME_IMAGE", "mgs/comfy-runtime:1")
    template = _env("COMFY_TEMPLATE", "comfy-v0.42.5-cu130")
    accept_missing_nodes = _bool("ACCEPT_MISSING_NODES", True)
    editor_extra_args = _env("EDITOR_EXTRA_ARGS", "")

    gpu_index = _int("GPU_INDEX", 0)
    job_max_seconds = _int("JOB_MAX_SECONDS", 14400)
    worker_start_timeout = _int("WORKER_START_TIMEOUT", 300)
    vram_worker_margin_mb = _int("VRAM_WORKER_MARGIN_MB", 512)
    vram_total_margin_mb = _int("VRAM_TOTAL_MARGIN_MB", 768)
    # 유휴 판정 여유. 11-21 사무실 기록: 어느 프로세스에도 안 잡히는 점유(드라이버·화면)가 기준보다 53~64MB 많았을 뿐인데 대기열이 정지됐다
    vram_idle_margin_mb = _int("VRAM_IDLE_MARGIN_MB", 512)
    # 정지 중 GPU 를 아무도 안 쓰는 상태에서 '우리 몫'이 이 횟수(15초마다 한 번) 동안 거의 같은 값(오차 VRAM_REBASE_TOL_MB 이내)이고 기준보다
    # VRAM_REBASE_MAX_MB 이하로만 높으면, 그 값을 새 기준으로 삼아 스스로 푼다 (기준값은 min() 으로 내려가기만 해 몇 시간씩 정지가 풀리지 않았다)
    vram_rebase_checks = _int("VRAM_REBASE_CHECKS", 8)
    vram_rebase_tol_mb = _int("VRAM_REBASE_TOL_MB", 64)
    vram_rebase_max_mb = _int("VRAM_REBASE_MAX_MB", 1024)
    # 이보다 작은 컨테이너 밖 GPU 프로세스는 감시 기록에서 뺀다 (GTK4 화면 앱이 NVML 에 compute 로 잡힌다)
    foreign_incident_min_mb = _int("FOREIGN_INCIDENT_MIN_MB", 256)

    ollama_upstream = _env("OLLAMA_UPSTREAM", "http://127.0.0.1:11435")
    ollama_container = _env("OLLAMA_CONTAINER", "mgs-ollama-1")

    # 프롬프트 확장(메타 규칙)과 결과 자동 검사에 쓰는 Ollama 모델. 둘 다 GPU 대기열을 거친다.
    expand_model = _env("PROMPT_EXPAND_MODEL", "qwen3.8:27b")
    expand_repair_rounds = _int("PROMPT_EXPAND_REPAIR_ROUNDS", 2)
    inspect_model = _env("IMAGE_INSPECT_MODEL", "qwen3-vl:8b-instruct")
    helper_wait_seconds = _int("HELPER_WAIT_SECONDS", 900)

    # 제작진 모드(B2, docs/crew-mode.md): 분야 메뉴가 있는 요청(제품·공간·풍경)에만 도우미 앞에 전문가 지식 초안 단계를 붙인다.
    # off 는 서버에서 끈다(패널 값은 무시). 그 밖의 값은 auto: 패널이 crew 로 true/false 를 보내면 그 요청만 따르고, 안 보내면 켠다.
    crew_mode = "off" if _env("CREW_MODE", "auto").strip().lower() in ("off", "0", "false", "no") else "auto"
    crew_repair_rounds = _int("CREW_REPAIR_ROUNDS", 1)
    # 캐릭터 정본 이름이 요청에 있으면 제작진 모드를 건너뛴다 (정본 특징이 메뉴 선택보다 우선). 쉼표로 구분.
    crew_guard_names = _env("CREW_GUARD_NAMES", "니키,niki,푸토르,putor,퀼린스,quillins,제임스,james,베스,beth,타오바,taoba,"
                                                "일리아,ilia,일로케,iloke,일루키,iluki")

    # 영상 렌더러(docs/video-renderer.md): 첫 화면 이미지를 읽는 시각 모델과 움직임 초안을 고쳐 쓰게 하는 최대 횟수.
    # 시각 모델이 확장 모델과 같으면 한 GPU 차례 안에서 처리하고, 다르면 차례를 한 번 더 쓴다.
    video_frame_model = _env("VIDEO_FRAME_MODEL", "") or inspect_model
    video_repair_rounds = _int("VIDEO_REPAIR_ROUNDS", 1)
    # 허브 안 LLM 호출(도우미·제작진·영상 렌더러·결과 검사)의 문맥 길이. 장문 영상 프롬프트는 지시문 + 답 + 고쳐 쓰기 1회가
    # Ollama 기본 문맥(4096)을 넘을 수 있어 앞부분(지시문)이 잘린다. 한 차례 안의 호출은 같은 값을 써야 모델을 다시 올리지 않는다.
    # 0 이면 Ollama 기본값을 쓴다.
    helper_num_ctx = _int("HELPER_NUM_CTX", 12288)

    editor_network = "mgs-editors"
    worker_network = "mgs-workers"
    editor_subnet = _env("EDITOR_SUBNET", "172.30.0.0/24")
    worker_subnet = _env("WORKER_SUBNET", "172.30.1.0/24")
    worker_name = "mgs-worker"

    # ── 채팅 스튜디오 (docs/chat-studio-design.md 19.3). 기본은 꺼짐 ──
    studio_enabled = _bool("STUDIO_ENABLED", False)
    studio_allowed_users = [u.strip() for u in _env("STUDIO_ALLOWED_USERS").split(",") if u.strip()]
    studio_env_slug = _env("STUDIO_ENV_SLUG", "studio-chat")
    studio_upload_max_mb = _int("STUDIO_UPLOAD_MAX_MB", 20)
    studio_upload_max_side = _int("STUDIO_UPLOAD_MAX_SIDE", 4096)
    studio_upload_store_side = _int("STUDIO_UPLOAD_STORE_SIDE", 2048)
    studio_upload_quota_mb = _int("STUDIO_UPLOAD_QUOTA_MB", 2048)
    studio_min_free_gb = _int("STUDIO_MIN_FREE_GB", 50)
    studio_unsent_hours = _int("STUDIO_UNSENT_HOURS", 24)
    studio_max_active_per_user = _int("STUDIO_MAX_ACTIVE_PER_USER", 2)
    studio_max_open_per_user = _int("STUDIO_MAX_OPEN_PER_USER", 5)
    studio_max_messages_per_min = _int("STUDIO_MAX_MESSAGES_PER_MIN", 10)
    studio_llm_wait_seconds = _int("STUDIO_LLM_WAIT_SECONDS", 3600)
    studio_llm_turn_seconds = _int("STUDIO_LLM_TURN_SECONDS", 900)
    studio_recipe_fail_limit = _int("STUDIO_RECIPE_FAIL_LIMIT", 3)
    studio_ask_long_seconds = _int("STUDIO_ASK_LONG_SECONDS", 300)
    studio_plan_ttl_hours = _int("STUDIO_PLAN_TTL_HOURS", 24)
    studio_sound_default = _bool("STUDIO_SOUND_DEFAULT", True)   # 영상을 소리 포함(LTX-2.5)으로 만드는 것이 기본. 화면의 소리 칸이 우선
    studio_t2v_via_image = _bool("STUDIO_T2V_VIA_IMAGE", True)   # 글→영상을 '장면 이미지 → 영상' 두 단계로 (사용자에게는 한 작업)
    # 대사 영상 (11-19): 대화가 있는 긴 영상을 '목소리 먼저 → 입 맞춤 → 편집' 으로. 채팅 작업공간에 TTS 노드를 넣은 뒤 켠다
    studio_dialogue = _bool("STUDIO_DIALOGUE", False)
    studio_cast_file = _env("STUDIO_CAST_FILE")          # 인물 라이브러리 파일 (비우면 app/cast_library.json)
    # 대사 녹음의 복제 방식: icl = 기준 소리와 그 글을 함께 줘서 말투·나이·감정까지 따른다(맥락 복제), xvector = 목소리 특징만 복제
    studio_voice_clone = (_env("STUDIO_VOICE_CLONE") or "icl").strip().lower()
    # 긴 영상·대사 영상의 그림·클립 자동 검사 (11-20): 장면 그림·편집 그림·클립의 검사 프레임을 시각 모델로 보고 사람 수·몸의 이상·화풍이
    # 틀리면 씨앗을 바꿔 다시 만든다 (studio/checks.py). 한 장 약 15초
    studio_picture_check = _bool("STUDIO_PICTURE_CHECK", True)
    # 장면 잇기 (11-20, docs/scene-chaining-design.md): 모든 장면의 첫 화면을 앞 장면 마지막 프레임을 고쳐 만든다. 편집 레시피가 꺼지면 예전 방식
    studio_chain = _bool("STUDIO_CHAIN", True)
    # 구도 그림에서 그리기 (11-21, docs/reference-system-design.md 11절): 사람이 나오는 새 컷을 ① 얼굴 없는 형상의 구도 그림 → ② 인물 입히기
    # (세 사람 이상이면 ③ 한 명씩)로 그린다. 끄면 기준 그림으로 바로 새로 그린다(PR #41 방식)
    studio_ref_layout = _bool("STUDIO_REF_LAYOUT", True)
    # 두 사람 대화 장면 (11-24 메인 실측 X1·X6): 듣는 상대가 같은 장소에 있는 대사 컷은 두 사람을 한 화면(미디엄 투샷)에 두고
    # InfiniteTalk 두 화자 레시피(talk_duo)로 말하는 사람 입만 맞춘다. 혼자 말하는 컷은 그대로 LTX. 끄면 예전처럼 말하는 사람 혼자 클로즈업
    studio_duo_talk = _bool("STUDIO_DUO_TALK", True)
    studio_inspect = _env("STUDIO_INSPECT", "idle")            # on | idle | off
    studio_auto_reroll = _int("STUDIO_AUTO_REROLL", 0)
    studio_progress_min_interval_ms = _int("STUDIO_PROGRESS_MIN_INTERVAL_MS", 500)
    studio_purge_days = _int("STUDIO_PURGE_DAYS", 30)
    studio_events_keep_days = _int("STUDIO_EVENTS_KEEP_DAYS", 90)
    studio_record_events = _bool("STUDIO_RECORD_EVENTS", False)
    studio_record_dir = _env("STUDIO_RECORD_DIR")
    studio_llm_hard_wait = 86400                                 # 감독이 차례를 기다리는 절대 상한(코드 상수, 5.6)

    session_hours = _int("SESSION_HOURS", 12)
    version_keep_days = _int("VERSION_KEEP_DAYS", 7)

    @property
    def templates_dir(self):
        return f"{self.envs_dir}/templates"

    def template_path(self):
        return f"{self.envs_dir}/templates/{self.template}"

    def live_path(self, slug):
        return f"{self.envs_dir}/live/{slug}"

    def snap_dir(self, slug):
        return f"{self.envs_dir}/snap/{slug}"

    def state_path(self, slug):
        return f"{self.envs_dir}/state/{slug}"

    def run_path(self):
        return f"{self.envs_dir}/run/worker"


settings = Settings()
