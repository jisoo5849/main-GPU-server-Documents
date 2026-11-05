"""관리 명령. 메인에서 허브 컨테이너 안으로 실행한다.

  docker compose exec hub python -m app.cli create-user alice --display "앨리스" --uid 1001 --password-stdin
  docker compose exec hub python -m app.cli create-env alice main
  docker compose exec hub python -m app.cli set-password alice --password-stdin
  docker compose exec hub python -m app.cli api-key alice "영상 파이프라인"
  docker compose exec hub python -m app.cli deactivate alice
  docker compose exec hub python -m app.cli status            # 실행 중·대기 중 작업 수
  docker compose exec hub python -m app.cli restart-editors   # 모든 편집 공간을 새 런타임으로 다시 만들기
  docker compose exec hub python -m app.cli --by 홍길동 set-password alice --password-stdin   # 실행한 사람 기록
"""
import argparse
import asyncio
import getpass
import sys

from . import db, dockerctl, envs, security


def _password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    pw = getpass.getpass("비밀번호: ")
    if pw != getpass.getpass("비밀번호 확인: "):
        sys.exit("비밀번호가 다릅니다.")
    return pw


async def _log(args, target, **extra):
    """터미널 관리 명령에는 로그인 사용자가 없으므로 무엇을·누구에게 했는지 접속 기록에 남긴다. 비밀번호·키 원문은 넣지 않는다."""
    await db.log_access(None, None, "admin_cli", {"cmd": args.cmd, "target": target, "by": args.by or None, **extra})


async def run(args):
    await db.init()
    if args.cmd == "create-user":
        pw = _password(args)
        if len(pw) < 8:
            sys.exit("비밀번호는 8자 이상이어야 합니다.")
        # 이미 있는 계정이면 비밀번호·권한을 덮어쓰고 다시 켠다. 기록에서 새 계정과 구분한다
        before = await db.fetchrow("SELECT role, active FROM users WHERE username=$1", args.username)
        await db.execute(
            "INSERT INTO users(username, display_name, pw_hash, role, linux_uid) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (username) DO UPDATE SET display_name=EXCLUDED.display_name, pw_hash=EXCLUDED.pw_hash, "
            "role=EXCLUDED.role, linux_uid=EXCLUDED.linux_uid, active=true",
            args.username, args.display or args.username, security.hash_password(pw),
            "admin" if args.admin else "user", args.uid)
        extra = {"existed": before is not None}
        if before is not None:
            extra.update(prev_role=before["role"], prev_active=before["active"])
        await _log(args, args.username, admin=args.admin, uid=args.uid, **extra)
        print(f"사용자 {args.username} 저장 완료")
    elif args.cmd == "create-env":
        user = await db.fetchrow("SELECT * FROM users WHERE username=$1", args.username)
        if user is None:
            sys.exit("사용자가 없습니다.")
        if await db.fetchval("SELECT 1 FROM envs WHERE owner_id=$1 AND name=$2", user["id"], args.name):
            print("이미 있는 작업공간입니다.")
            return
        await dockerctl.ensure_networks()
        env = await envs.create_env(user["id"], args.name)
        await _log(args, args.username, env=env["slug"])
        print(f"작업공간 {env['slug']} 생성: 포트 {env['port']}")
    elif args.cmd == "set-password":
        pw = _password(args)
        res = await db.execute("UPDATE users SET pw_hash=$2 WHERE username=$1", args.username, security.hash_password(pw))
        if res == "UPDATE 0":
            sys.exit("사용자가 없습니다.")
        await _log(args, args.username)
        print("비밀번호를 바꿨습니다. Samba 비밀번호는 메인에서 sudo smbpasswd 로 따로 바꾸세요.")
    elif args.cmd == "api-key":
        user = await db.fetchrow("SELECT * FROM users WHERE username=$1", args.username)
        if user is None:
            sys.exit("사용자가 없습니다.")
        key = await security.create_api_key(user["id"], args.label)
        await _log(args, args.username, label=args.label)   # 키 원문은 남기지 않는다
        print(key)
    elif args.cmd == "status":
        rows = await db.fetch("SELECT kind, state, count(*) AS n FROM jobs "
                              "WHERE state IN ('queued','starting','running','cleaning') GROUP BY kind, state")
        active = sum(r["n"] for r in rows if r["state"] != "queued")
        queued = sum(r["n"] for r in rows if r["state"] == "queued")
        print(f"active={active} queued={queued} paused={bool(await db.get_setting('paused', False))} "
              f"blocked={bool(await db.get_setting('blocked'))}")
    elif args.cmd == "restart-editors":
        # 새 런타임 파일(허브 연동 노드, ComfyUI 패널)과 새 이미지를 편집 공간에 반영한다
        await dockerctl.ensure_networks()
        rows = await db.fetch("SELECT id FROM envs WHERE state='running' ORDER BY id")
        failed, skipped = 0, 0
        for r in rows:
            env = await envs.get_env(r["id"])
            # 노드 설치(pip·git) 중인 편집 공간을 끄면 설치가 반쯤 끝난 채 남는다
            if await envs.manager_busy(env):
                print(f"건너뜀 (노드 설치 중): {env['slug']}")
                skipped += 1
                continue
            try:
                await envs.restart_editor(env)
                print(f"다시 만듦: {env['slug']} (포트 {env['port']})")
            except Exception as e:  # noqa: BLE001
                print(f"실패: {env['slug']}: {e}")
                failed += 1
        await _log(args, None, total=len(rows), failed=failed, skipped=skipped)
        if failed or skipped:
            print(f"실패 {failed}개, 건너뜀 {skipped}개. 나중에 다시: docker compose exec hub python -m app.cli restart-editors")
            sys.exit(1)
    elif args.cmd == "deactivate":
        res = await db.execute("UPDATE users SET active=false WHERE username=$1", args.username)
        if res == "UPDATE 0":
            sys.exit("사용자가 없습니다.")
        await db.execute("UPDATE sessions SET ended_at=now(), end_reason='admin' WHERE ended_at IS NULL AND "
                         "user_id=(SELECT id FROM users WHERE username=$1)", args.username)
        await _log(args, args.username)
        print("사용 중지했습니다.")


def main():
    p = argparse.ArgumentParser(prog="app.cli")
    # 메인 리눅스 계정은 공용이라 실행한 사람을 알 수 없다. 기록용으로 이름을 적는다 (하위 명령 앞에 쓴다)
    p.add_argument("--by", default="", help="실행한 사람 이름 (access_log 기록용)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--display")
    c.add_argument("--uid", type=int, required=True)
    c.add_argument("--admin", action="store_true")
    c.add_argument("--password-stdin", action="store_true")
    c = sub.add_parser("create-env")
    c.add_argument("username")
    c.add_argument("name")
    c = sub.add_parser("set-password")
    c.add_argument("username")
    c.add_argument("--password-stdin", action="store_true")
    c = sub.add_parser("api-key")
    c.add_argument("username")
    c.add_argument("label", nargs="?", default="")
    c = sub.add_parser("deactivate")
    c.add_argument("username")
    sub.add_parser("status")
    sub.add_parser("restart-editors")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
