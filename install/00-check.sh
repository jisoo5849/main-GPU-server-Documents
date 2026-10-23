#!/usr/bin/env bash
# 메인 하드웨어와 기본 상태를 확인한다. 아무것도 바꾸지 않는다.
#   bash install/00-check.sh
set -uo pipefail

echo "== OS =="
. /etc/os-release && echo "$PRETTY_NAME  (kernel $(uname -r))"

echo; echo "== CPU / RAM =="
echo "CPU: $(nproc) cores - $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)"
free -h | sed -n '1,2p'

echo; echo "== 디스크 =="
lsblk -o NAME,SIZE,TYPE,ROTA,TRAN,FSTYPE,MOUNTPOINTS
echo "(ROTA=0 이면 SSD, TRAN=nvme 이면 NVMe)"

echo; echo "== GPU =="
if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv
else
  echo "nvidia-smi 없음 → NVIDIA 드라이버 설치 필요 (install/01-host-packages.sh 안내 참고)"
fi

echo; echo "== Docker =="
if command -v docker >/dev/null; then
  docker version --format 'Docker {{.Server.Version}}' 2>/dev/null || echo "docker 데몬에 접근 불가 (sudo 필요 또는 미실행)"
  command -v nvidia-ctk >/dev/null && nvidia-ctk --version | head -1 || echo "NVIDIA Container Toolkit 없음"
else
  echo "Docker 없음"
fi

echo; echo "== 저장소 경로 =="
for p in /srv/envs /srv/models /srv/results; do
  if [[ -d $p ]]; then
    printf '%-14s %s\n' "$p" "$(findmnt -no FSTYPE,SOURCE --target "$p")"
  else
    printf '%-14s (없음)\n' "$p"
  fi
done
