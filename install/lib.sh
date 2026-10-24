# 설치 스크립트 공용 함수. 각 스크립트가 source 한다.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$REPO_DIR/.env" ]]; then
  echo "[!] $REPO_DIR/.env 가 없습니다. 먼저 실행하세요:  cp .env.example .env  (값 수정 후 다시 실행)" >&2
  exit 1
fi
# .env 는 docker compose 도 읽으므로 쉘로 해석하지 않고 KEY=값 만 읽는다 (특수문자 안전)
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%$'\r'}"
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" == *=* ]] || continue
  key="${line%%=*}"; val="${line#*=}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  export "$key=$val"
done < "$REPO_DIR/.env"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

need_root() {
  [[ $EUID -eq 0 ]] || die "관리자 권한이 필요합니다:  sudo $0 $*"
}
