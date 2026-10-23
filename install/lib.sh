# 설치 스크립트 공용 함수. 각 스크립트가 source 한다.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$REPO_DIR/.env" ]]; then
  echo "[!] $REPO_DIR/.env 가 없습니다. 먼저 실행하세요:  cp .env.example .env  (값 수정 후 다시 실행)" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "$REPO_DIR/.env"
set +a

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

need_root() {
  [[ $EUID -eq 0 ]] || die "관리자 권한이 필요합니다:  sudo $0 $*"
}
