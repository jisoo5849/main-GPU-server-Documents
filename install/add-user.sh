#!/usr/bin/env bash
# 작업자 계정을 추가한다: 리눅스 계정(파일 소유자) + Samba 비밀번호 + 허브 로그인 계정 + 기본 작업공간.
#   sudo bash install/add-user.sh alice "앨리스"
#   sudo bash install/add-user.sh admin "관리자" --admin
source "$(dirname "$0")/lib.sh"
need_root "$@"

NAME="${1:?사용자 이름(영문 소문자)을 입력하세요}"
DISPLAY_NAME="${2:-$NAME}"
ROLE_FLAG="${3:-}"
[[ "$NAME" =~ ^[a-z][a-z0-9_-]{1,30}$ ]] || die "사용자 이름은 영문 소문자로 시작하고 영문 소문자·숫자·-·_ 만 쓸 수 있습니다."

say "리눅스 계정 $NAME (로그인 쉘 없음)"
if ! id "$NAME" >/dev/null 2>&1; then
  useradd --no-create-home --shell /usr/sbin/nologin --gid "$MGS_GROUP" --groups "$MGS_GROUP" "$NAME"
fi
uid=$(id -u "$NAME")

say "비밀번호 설정 (Samba와 허브 로그인에 같은 비밀번호를 씁니다)"
read -r -s -p "비밀번호: " PW1; echo
read -r -s -p "비밀번호 확인: " PW2; echo
[[ "$PW1" == "$PW2" && ${#PW1} -ge 8 ]] || die "비밀번호가 다르거나 8자 미만입니다."
printf '%s\n%s\n' "$PW1" "$PW1" | smbpasswd -s -a "$NAME"
smbpasswd -e "$NAME" >/dev/null

say "허브 계정과 기본 작업공간"
cd "$REPO_DIR"
printf '%s\n' "$PW1" | docker compose exec -T hub python -m app.cli create-user "$NAME" \
  --display "$DISPLAY_NAME" --uid "$uid" --password-stdin $ROLE_FLAG
docker compose exec -T hub python -m app.cli create-env "$NAME" main

say "완료: $NAME (uid $uid)"
echo "Windows 연결:  net use R: \\\\${HUB_PUBLIC_HOST}\\results /user:${NAME}"
