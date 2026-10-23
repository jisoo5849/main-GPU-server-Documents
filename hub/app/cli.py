"""관리 명령. 메인에서 허브 컨테이너 안으로 실행한다.

  docker compose exec hub python -m app.cli create-user alice --display "앨리스" --uid 1001 --password-stdin
  docker compose exec hub python -m app.cli create-env alice main
  docker compose exec hub python -m app.cli set-password alice --password-stdin
  docker compose exec hub python -m app.cli api-key alice "영상 파이프라인"
  docker compose exec hub python -m app.cli deactivate alice
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


async def run(args):
    await db.init()
    if args.cmd == "create-user":
        pw = _password(args)
        if len(pw) < 8:
            sys.exit("비밀번호는 8자 이상이어야 합니다.")
        await db.execute(
            "INSERT INTO users(username, display_name, pw_hash, role, linux_uid) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (username) DO UPDATE SET display_name=EXCLUDED.display_name, pw_hash=EXCLUDED.pw_hash, "
            "role=EXCLUDED.role, linux_uid=EXCLUDED.linux_uid, active=true",
            args.username, args.display or args.username, security.hash_password(pw),
            "admin" if args.admin else "user", args.uid)
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
        print(f"작업공간 {env['slug']} 생성: 포트 {env['port']}")
    elif args.cmd == "set-password":
        pw = _password(args)
        await db.execute("UPDATE users SET pw_hash=$2 WHERE username=$1", args.username, security.hash_password(pw))
        print("비밀번호를 바꿨습니다. Samba 비밀번호는 메인에서 sudo smbpasswd 로 따로 바꾸세요.")
    elif args.cmd == "api-key":
        user = await db.fetchrow("SELECT * FROM users WHERE username=$1", args.username)
        if user is None:
            sys.exit("사용자가 없습니다.")
        print(await security.create_api_key(user["id"], args.label))
    elif args.cmd == "deactivate":
        await db.execute("UPDATE users SET active=false WHERE username=$1", args.username)
        await db.execute("UPDATE sessions SET ended_at=now(), end_reason='admin' WHERE ended_at IS NULL AND "
                         "user_id=(SELECT id FROM users WHERE username=$1)", args.username)
        print("사용 중지했습니다.")


def main():
    p = argparse.ArgumentParser(prog="app.cli")
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
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
