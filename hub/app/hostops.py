"""메인에서 직접 실행하는 작업: btrfs 스냅샷, 파일 소유권, git 정보."""
import asyncio
import os
import shutil


class HostOpError(RuntimeError):
    pass


async def run(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise HostOpError(f"{' '.join(args)} → {proc.returncode}: {err.decode(errors='replace').strip()}")
    return out.decode(errors="replace")


async def snapshot(src: str, dst: str, readonly: bool = False):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    args = ["btrfs", "subvolume", "snapshot"]
    if readonly:
        args.append("-r")
    await run(*args, src, dst)


async def delete_subvolume(path: str):
    if os.path.exists(path):
        await run("btrfs", "subvolume", "delete", path)


async def generation(path: str) -> int:
    """subvolume 의 세대 번호. 파일이 추가·수정·삭제되면 커진다."""
    out = await run("btrfs", "subvolume", "show", path)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Generation:"):
            return int(line.split(":", 1)[1].strip())
    raise HostOpError(f"generation not found for {path}")


async def chown_tree(path: str, uid: int, gid: int):
    await run("chown", "-R", f"{uid}:{gid}", path)


def ensure_dir(path: str, uid: int, gid: int, mode: int = 0o2775):
    os.makedirs(path, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


async def git_head(repo: str) -> str | None:
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None
    out = await run("git", "-c", "safe.directory=*", "-C", repo, "rev-parse", "HEAD", check=False)
    return out.strip() or None


async def list_nodes(custom_nodes_dir: str) -> list[dict]:
    nodes = []
    if not os.path.isdir(custom_nodes_dir):
        return nodes
    for name in sorted(os.listdir(custom_nodes_dir)):
        path = os.path.join(custom_nodes_dir, name)
        if not os.path.isdir(path) or name.startswith((".", "__")):
            continue
        nodes.append({
            "name": name,
            "enabled": not name.endswith(".disabled"),
            "commit": await git_head(path),
        })
    return nodes


def copy_reflink(src: str, dst: str):
    """같은 btrfs 안이면 공간을 거의 쓰지 않는 복사(reflink), 아니면 일반 복사."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        import fcntl
        FICLONE = 0x40049409
        with open(src, "rb") as s, open(dst, "wb") as d:
            fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
    except (OSError, ImportError):
        shutil.copy2(src, dst)
