#!/usr/bin/env bash
# Docker Engine, NVIDIA Container Toolkit, btrfs, Samba, 방화벽을 설치한다.
# NVIDIA 드라이버는 이 스크립트가 설치하지 않는다(재부팅이 필요하므로 아래 안내대로 먼저 설치).
#   sudo bash install/01-host-packages.sh
source "$(dirname "$0")/lib.sh"
need_root

if ! command -v nvidia-smi >/dev/null; then
  warn "NVIDIA 드라이버가 없습니다. 먼저 설치하고 재부팅하세요."
  cat <<'EOF'
  ubuntu-drivers list --gpgpu          # 설치 가능한 드라이버 목록
  sudo ubuntu-drivers install --gpgpu  # 권장 드라이버 설치 (Blackwell 카드는 580 이상, open 커널 모듈)
  sudo reboot
EOF
  exit 1
fi

say "기본 패키지"
apt-get update
apt-get install -y ca-certificates curl gnupg btrfs-progs samba acl ufw jq

say "Docker Engine (docker.com 저장소)"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

say "NVIDIA Container Toolkit"
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt-get install -y nvidia-container-toolkit
fi
# 기본 런타임은 runc 그대로 둔다(--set-as-default 금지). GPU는 허브가 명시적으로 요청한 컨테이너에만 연결된다.
nvidia-ctk runtime configure --runtime=docker
systemctl enable --now nvidia-cdi-refresh.service 2>/dev/null || true
# CDI 명세가 없으면 직접 만든다 (툴킷 버전에 따라 자동 생성 서비스가 없을 수 있음)
[[ -s /var/run/cdi/nvidia.yaml || -s /etc/cdi/nvidia.yaml ]] || nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml || true
systemctl restart docker

say "GPU 지속 모드 (메모리 측정 안정화)"
nvidia-smi -pm 1 || true

say "확인: GPU가 없는 컨테이너에서는 GPU가 보이지 않아야 합니다"
if docker run --rm ubuntu:24.04 ls /dev/nvidia0 >/dev/null 2>&1; then
  die "GPU 요청 없이도 /dev/nvidia0 이 보입니다. Docker 기본 런타임 설정을 확인하세요."
fi
echo "OK"

say "확인: GPU를 요청한 컨테이너에서는 GPU가 보여야 합니다"
docker run --rm --gpus "device=${GPU_INDEX:-0}" ubuntu:24.04 nvidia-smi -L

say "완료. 다음: sudo bash install/02-storage.sh"
