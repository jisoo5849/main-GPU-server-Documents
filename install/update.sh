#!/usr/bin/env bash
# 메인 업데이트: GitHub 의 최신 코드를 받아 허브·런타임·편집 공간에 반영한다.
#
#   sudo bash install/update.sh                 # 기본: 대기열을 잠시 멈추고, 실행 중인 GPU 작업이 끝나길 기다린 뒤 반영
#   sudo bash install/update.sh --reapply       # 새 커밋이 없어도 다시 반영 (편집 공간도 다시 만듦)
#   sudo bash install/update.sh --no-editors    # 편집 공간은 그대로 둠 (다음 업데이트 때 반영)
#   sudo bash install/update.sh --wait-minutes 30   # GPU 작업을 기다리는 최대 시간 (기본 120분)
#   sudo bash install/update.sh --force         # GPU 작업이 끝나길 기다리지 않음 (주의: 아래 참고)
#
# --force 로 허브를 다시 시작하면: 실행 중(running)인 ComfyUI 작업만 이어받고, 준비 중·정리 중 작업은 '끊김(lost)'
# 으로 기록되며, 기다리던 LLM 요청은 모두 끊긴다. 결과 파일은 그대로 남는다.
source "$(dirname "$0")/lib.sh"
need_root "$@"

ARGS=("$@")
FORCE=0; REAPPLY=0; EDITORS=1; WAIT_MIN=120
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    --reapply) REAPPLY=1 ;;
    --no-editors) EDITORS=0 ;;
    --wait-minutes) shift; WAIT_MIN="${1:?--wait-minutes 뒤에 분을 쓰세요}" ;;
    *) die "모르는 옵션: $1" ;;
  esac
  shift
done
cd "$REPO_DIR"

APPLIED_FILE="$REPO_DIR/.applied-commit"     # 마지막으로 서버에 반영한 커밋
PENDING_FILE="$REPO_DIR/.editors-pending"     # --no-editors 로 미뤄 둔 편집 공간 재생성
psql_q() { docker compose exec -T postgres psql -U mgs -d mgs -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

# ── 1. 코드 ───────────────────────────────────────────────
say "1/7 GitHub 에서 새 코드 확인"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  git status --short --untracked-files=no
  die "메인에서 직접 고친 파일이 있습니다. 위 파일을 백업하거나 'sudo git stash' 한 뒤 다시 실행하세요."
fi
git fetch --quiet origin || die "GitHub 에 연결하지 못했습니다. 인터넷 연결을 확인하세요."
after=$(git rev-parse '@{u}' 2>/dev/null) \
  || die "추적 브랜치가 없습니다. 'sudo git checkout main && sudo git branch -u origin/main' 후 다시 실행하세요."
head_now=$(git rev-parse HEAD)
applied=$(cat "$APPLIED_FILE" 2>/dev/null || echo none)

if [[ "$head_now" != "$after" ]]; then
  echo "받을 변경:"
  git log --format='  %h %ad %s' --date=format:'%m-%d %H:%M' "$head_now..$after"
  git merge --ff-only --quiet "$after" || die "자동 병합이 안 됩니다. 'git log --oneline -5' 와 'git status' 를 확인하세요."
  if ! git diff --quiet "$head_now" "$after" -- install/update.sh install/lib.sh; then
    say "업데이트 스크립트가 바뀌어 새 스크립트로 다시 실행합니다"
    exec bash "$REPO_DIR/install/update.sh" "${ARGS[@]}"
  fi
elif [[ "$applied" == "$after" && $REAPPLY -eq 0 && ! -f "$PENDING_FILE" ]]; then
  echo "이미 최신이고 서버에도 반영되어 있습니다 ($(git log -1 --format='%h %s'))."
  echo "다시 반영하려면 --reapply 를 붙이세요."
  exit 0
else
  echo "코드는 최신입니다. 서버에 반영합니다."
fi

# 마지막으로 반영한 커밋부터 바뀐 파일 (처음이면 전부 바뀐 것으로 본다)
if [[ "$applied" =~ ^[0-9a-f]{40}$ ]] && git cat-file -e "$applied" 2>/dev/null; then
  changed=$(git diff --name-only "$applied" HEAD || true)
  base="$applied"
else
  changed="(처음 반영)"
  base=""
fi
first=0; [[ -z "$base" ]] && first=1
runtime_changed=0
if [[ $first -eq 1 || $REAPPLY -eq 1 ]] || grep -qE '^(runtime/|images/comfy-runtime/)' <<<"$changed"; then
  runtime_changed=1
fi

# ── 2. 직접 해야 할 일 알림 ───────────────────────────────
say "2/7 설정 확인"
missing=$(comm -23 <(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' .env.example | sort -u) \
                   <(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' .env | sort -u) | tr -d '=' || true)
if [[ -n "$missing" ]]; then
  warn ".env 에 없는 새 설정 항목 (기본값으로 동작합니다. 필요하면 .env 에 추가):"
  for k in $missing; do grep -E "^$k=" .env.example | sed 's/^/    /'; done
fi
if [[ -n "$base" ]]; then
  git diff --quiet "$base" HEAD -- .env.example || \
    warn ".env.example 이 바뀌었습니다. 기존 값이 바뀐 항목은 .env 에 자동 반영되지 않습니다:  git diff $base HEAD -- .env.example"
  manual=$(grep -E '^install/(0[0-6]-|smb\.conf)' <<<"$changed" || true)
  [[ -n "$manual" ]] && warn "다음 설치 스크립트가 바뀌었습니다. 필요하면 직접 다시 실행하세요: $(echo $manual)"
  grep -q '^images/comfy-runtime/entrypoint.sh' <<<"$changed" && \
    warn "템플릿 만들기(build) 방식이 바뀌었을 수 있습니다. 기존 작업공간에는 영향이 없고, 새 템플릿은 05-build-runtime.sh 로 만듭니다."
fi
echo "  확인 끝"

# ── 3. 이미지 미리 빌드 (서비스는 아직 그대로) ─────────────
say "3/7 이미지 빌드 (서비스는 계속 동작합니다)"
docker compose build hub
if [[ $runtime_changed -eq 1 ]] && { [[ $first -eq 1 || $REAPPLY -eq 1 ]] || grep -q '^images/comfy-runtime/' <<<"$changed"; }; then
  echo "  ComfyUI 공용 이미지: ${COMFY_RUNTIME_IMAGE}"
  docker build -q -t "$COMFY_RUNTIME_IMAGE" "$REPO_DIR/images/comfy-runtime" >/dev/null
fi

# ── 4. 대기열 일시 정지, 실행 중인 GPU 작업이 끝나길 기다림 ─
say "4/7 GPU 대기열 잠시 멈춤"
prev_paused=$(psql_q "SELECT coalesce((SELECT value::text FROM settings WHERE key='paused'),'false')")
if [[ "$prev_paused" != "true" && "$prev_paused" != "false" ]]; then
  [[ $FORCE -eq 1 ]] || die "DB 에 연결하지 못해 GPU 작업 상태를 확인할 수 없습니다. 'sudo docker compose ps' 로 확인한 뒤 --force 로 실행하세요."
  warn "DB 확인 실패. --force 이므로 계속합니다."
  prev_paused=""
else
  psql_q "INSERT INTO settings(key, value) VALUES ('paused', 'true'::jsonb) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value" >/dev/null
  restore_pause() {
    psql_q "INSERT INTO settings(key, value) VALUES ('paused', '${prev_paused}'::jsonb) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value" >/dev/null \
      || warn "대기열 일시 정지를 원래대로 돌리지 못했습니다. 관리자 화면에서 '재개'를 누르세요."
  }
  trap restore_pause EXIT
  echo "  새 GPU 작업은 시작되지 않습니다 (접수는 계속 받음). 끝나면 원래 상태로 돌립니다."
fi

if [[ $FORCE -eq 0 && -n "$prev_paused" ]]; then
  deadline=$(( $(date +%s) + WAIT_MIN * 60 ))
  while :; do
    active=$(psql_q "SELECT count(*) FROM jobs WHERE state IN ('starting','running','cleaning')")
    [[ "$active" =~ ^[0-9]+$ ]] || die "GPU 작업 상태를 읽지 못했습니다."
    [[ "$active" -eq 0 ]] && break
    (( $(date +%s) > deadline )) && die "${WAIT_MIN}분 안에 GPU 작업이 끝나지 않았습니다. 나중에 다시 실행하세요 (대기열은 원래대로 돌려 둡니다)."
    echo "  실행 중인 GPU 작업 ${active}건이 끝나길 기다리는 중... ($(date +%H:%M:%S))"
    sleep 15
  done
  echo "  실행 중인 GPU 작업 없음"
fi

# ── 5. 공용 런타임 파일 ───────────────────────────────────
say "5/7 공용 런타임 파일 (${MGS_RUNTIME})"
rt=$(realpath -m "$MGS_RUNTIME")
# 잘못된 경로를 통째로 지우지 않도록 확인 (이 폴더 안의 파일을 지우고 다시 복사한다)
[[ "$rt" == /*/* && -d "$rt" && -f "$rt/models.yaml" && -d "$rt/nodes" \
   && "$rt" != "$REPO_DIR" && "$rt" != "$REPO_DIR"/* && "$REPO_DIR" != "$rt"/* ]] \
  || die "MGS_RUNTIME 값을 확인하세요: $MGS_RUNTIME (models.yaml 과 nodes/ 가 있는 런타임 폴더여야 합니다)"
if ! cmp -s "$rt/models.yaml" "$REPO_DIR/runtime/models.yaml"; then
  if [[ -n "$base" ]] && git show "$base:runtime/models.yaml" 2>/dev/null | cmp -s - "$rt/models.yaml"; then
    :   # 저장소가 바꾼 것뿐
  else
    cp -a "$rt/models.yaml" "$rt.models.yaml.bak-$(date +%Y%m%d-%H%M%S)"
    warn "메인에서 직접 고친 models.yaml 을 백업했습니다: $rt.models.yaml.bak-*  (필요한 내용을 runtime/models.yaml 에 옮기세요)"
  fi
fi
# 폴더 자체는 지우지 않는다 (편집·실행 공간이 이 폴더를 연결해 쓰고 있다)
find "$rt" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -r "$REPO_DIR/runtime/." "$rt/"
chown -R root:root "$rt"
chmod -R a+rX "$rt"
echo "  복사함: $(find "$rt" -type f | wc -l) 개 파일"

# ── 6. 허브·DB·Ollama ─────────────────────────────────────
say "6/7 허브·DB·Ollama 반영"
PROFILES=()
[[ -n "$(docker compose --profile webui ps -a -q open-webui 2>/dev/null)" ]] && PROFILES=(--profile webui)
docker compose "${PROFILES[@]}" up -d --remove-orphans
code=000
for _ in $(seq 1 60); do
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HUB_PORTAL_PORT:-80}/login" || true)
  [[ "$code" == "200" ]] && break
  sleep 2
done
if [[ "$code" != "200" ]]; then
  warn "허브가 2분 안에 응답하지 않습니다. 로그: sudo docker compose logs --tail=80 hub"
  [[ -n "$base" ]] && warn "이전 버전으로 되돌리려면:  sudo git reset --hard $base && sudo docker compose up -d --build"
  die "업데이트를 마치지 못했습니다."
fi
echo "  허브 응답 정상"

# ── 7. 편집 공간 ──────────────────────────────────────────
if [[ $runtime_changed -eq 1 || -f "$PENDING_FILE" ]]; then
  if [[ $EDITORS -eq 1 ]]; then
    say "7/7 편집 공간 다시 만들기"
    echo "  작업자마다 ComfyUI 화면이 수십 초 끊겼다가 다시 연결됩니다 (저장 안 한 워크플로우는 브라우저에 남아 있음)."
    if docker compose exec -T hub python -m app.cli restart-editors; then
      rm -f "$PENDING_FILE"
    else
      warn "일부 편집 공간을 다시 만들지 못했거나 건너뛰었습니다 (위 목록). 나중에: sudo docker compose exec hub python -m app.cli restart-editors"
      touch "$PENDING_FILE"
    fi
  else
    say "7/7 편집 공간은 그대로 둡니다 (--no-editors). 다음 업데이트 때 다시 만듭니다."
    touch "$PENDING_FILE"
  fi
else
  say "7/7 편집 공간에 반영할 변경 없음"
fi

git rev-parse HEAD > "$APPLIED_FILE"
say "완료: $(git log -1 --format='%h %s')"
