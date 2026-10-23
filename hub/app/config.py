"""허브 설정. 값은 모두 .env 에서 온다 (compose.yaml 의 env_file)."""
import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    database_url = _env("DATABASE_URL", "postgresql://mgs:mgs@127.0.0.1:5432/mgs")

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

    ollama_upstream = _env("OLLAMA_UPSTREAM", "http://127.0.0.1:11435")
    ollama_container = _env("OLLAMA_CONTAINER", "mgs-ollama-1")

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
