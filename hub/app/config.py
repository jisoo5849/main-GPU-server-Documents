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
    vram_idle_margin_mb = _int("VRAM_IDLE_MARGIN_MB", 256)
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

    editor_network = "mgs-editors"
    worker_network = "mgs-workers"
    editor_subnet = _env("EDITOR_SUBNET", "172.30.0.0/24")
    worker_subnet = _env("WORKER_SUBNET", "172.30.1.0/24")
    worker_name = "mgs-worker"

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
