"""GPU 메모리 측정 (NVML). ComfyUI 자체 보고 대신 메인에서 직접 잰다."""
import logging

from .config import settings

log = logging.getLogger("mgs.gpu")

try:
    import pynvml
except ImportError:  # 개발 PC 등 NVML이 없는 곳
    pynvml = None

_handle = None


def init() -> bool:
    global _handle
    if pynvml is None:
        return False
    try:
        pynvml.nvmlInit()
        _handle = pynvml.nvmlDeviceGetHandleByIndex(settings.gpu_index)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("NVML init failed: %s", e)
        _handle = None
        return False


def available() -> bool:
    return _handle is not None


def used_mb() -> int:
    if _handle is None:
        return 0
    return int(pynvml.nvmlDeviceGetMemoryInfo(_handle).used // (1024 * 1024))


def total_mb() -> int:
    if _handle is None:
        return 0
    return int(pynvml.nvmlDeviceGetMemoryInfo(_handle).total // (1024 * 1024))


def processes() -> list[tuple[int, int, str]]:
    """GPU를 쓰는 프로세스 [(메인 PID, 사용 MB, 종류)]. 허브는 pid: host 로 실행되므로 메인 PID가 나온다.

    종류: 'compute' = CUDA 연산 (ComfyUI, Ollama, 파이썬 스크립트)  /  'graphics' = 화면 표시 (Xorg, gnome-shell 등)
    """
    if _handle is None:
        return []
    procs = []
    for kind, getter in (("compute", pynvml.nvmlDeviceGetComputeRunningProcesses),
                         ("graphics", pynvml.nvmlDeviceGetGraphicsRunningProcesses)):
        try:
            for p in getter(_handle):
                mem = p.usedGpuMemory
                procs.append((p.pid, int(mem // (1024 * 1024)) if mem else 0, kind))
        except pynvml.NVMLError:
            pass
    seen, out = set(), []
    for pid, mem, kind in procs:   # compute 가 먼저 오므로 둘 다인 프로세스는 compute 로 남는다
        if pid not in seen:
            seen.add(pid)
            out.append((pid, mem, kind))
    return out


def process_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return "?"


def pid_container(pid: int) -> str | None:
    """PID가 속한 Docker 컨테이너 ID (cgroup 경로에서 읽는다). 컨테이너 밖이면 None."""
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    for line in text.splitlines():
        path = line.split(":", 2)[-1]
        for part in path.replace(".scope", "").split("/"):
            part = part.removeprefix("docker-")
            if len(part) == 64 and all(c in "0123456789abcdef" for c in part):
                return part
    return None
