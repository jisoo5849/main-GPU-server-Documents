#!/usr/bin/env bash
# ComfyUI 공용 이미지와 기본 환경 템플릿을 만든다.
# 템플릿 = ComfyUI(COMFY_TAG) + Python 가상환경(torch, 요구 패키지, ComfyUI-Manager). 새 환경은 이 템플릿의 스냅샷으로 시작한다.
#   sudo bash install/05-build-runtime.sh
source "$(dirname "$0")/lib.sh"
need_root

say "이미지 빌드: ${COMFY_RUNTIME_IMAGE}"
docker build -t "$COMFY_RUNTIME_IMAGE" "$REPO_DIR/images/comfy-runtime"

TPL="$MGS_ENVS/templates/$COMFY_TEMPLATE"
if [[ -e "$TPL" ]]; then
  warn "템플릿 $TPL 이 이미 있습니다. 다시 만들려면 먼저 지우세요: sudo btrfs subvolume delete $TPL"
  exit 0
fi

say "템플릿 생성: $TPL"
btrfs subvolume create "$TPL"
# 빌드는 GPU 없이 진행한다(torch 휠은 CUDA 런타임을 포함하므로 GPU가 없어도 설치된다)
docker run --rm \
  -e COMFY_TAG="$COMFY_TAG" \
  -e TORCH_INDEX_URL="$TORCH_INDEX_URL" \
  -v "$TPL:/env" \
  "$COMFY_RUNTIME_IMAGE" build

say "템플릿 확인 (GPU 연결해서 torch가 GPU를 보는지)"
docker run --rm --gpus "device=${GPU_INDEX:-0}" -v "$TPL:/env:ro" --entrypoint /env/venv/bin/python "$COMFY_RUNTIME_IMAGE" \
  -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'gpu', torch.cuda.get_device_name(0))"

# 템플릿은 읽기 전용으로 잠근다. 새 환경은 이것의 쓰기 가능한 스냅샷으로 만들어진다.
btrfs property set -ts "$TPL" ro true
say "완료. 다음: docker compose up -d --build"
