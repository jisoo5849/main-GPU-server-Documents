#!/usr/bin/env bash
# 2026-11 DB 보완을 메인에 한 번에 반영한다: 업데이트 → 사전 점검 → 기존 반복 기록 합치기 → (확인 후) 백업 삭제
#
#   cd /opt/mgs-src && sudo bash install/db/apply-2026-11.sh
#
# 작업자 PC 에서는 Downloads 의 mgs-db-fix-2026-11.cmd 가 SSH 로 이 스크립트를 실행한다.
source "$(dirname "$0")/../lib.sh"
need_root "$@"
cd "$REPO_DIR"
psql_run() { docker compose exec -T postgres psql -U mgs -d mgs -P pager=off "$@"; }

say "1/4 업데이트 (install/update.sh)"
bash install/update.sh || die "업데이트가 끝나지 않아 여기서 멈춥니다. 위 메시지를 확인하세요."

say "2/4 사전 점검 (모든 결과가 0행이어야 제약이 전부 걸림)"
psql_run < install/db/precheck-2026-11.sql
if docker compose logs --since 10m hub 2>&1 | grep -q "DB 제약 미적용"; then
  warn "허브 로그에 'DB 제약 미적용' 경고가 있습니다. 허브는 정상 동작하지만 일부 제약이 빠졌습니다. 위 점검 결과를 개발자에게 보내 주세요."
else
  echo "  제약 경고 없음"
fi

say "3/4 기존 반복 감시 기록 합치기 (실행 전 전체를 incidents_backup_20261105 에 백업)"
psql_run -v ON_ERROR_STOP=1 < install/db/incidents-merge-2026-11.sql || die "합치기 SQL 이 실패했습니다. 변경은 모두 취소되었습니다."

say "4/4 백업 테이블 삭제"
echo "  위 결과(after_rows, 프로세스별 감지 횟수)가 맞으면 백업을 지웁니다. 지우면 되돌릴 수 없습니다."
read -r -p "  백업 테이블 incidents_backup_20261105 를 지울까요? (y/N) " ans < /dev/tty || ans=""
if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
  psql_run -c "DROP TABLE IF EXISTS incidents_backup_20261105;"
  echo "  백업을 지웠습니다."
else
  echo "  백업을 남겨 둡니다. 나중에 지우려면:"
  echo "    sudo docker compose exec -T postgres psql -U mgs mgs -c \"DROP TABLE incidents_backup_20261105;\""
fi
say "완료"
