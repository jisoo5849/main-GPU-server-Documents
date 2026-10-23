#!/usr/bin/env bash
# 실행 방식
#   build   템플릿 만들기: /env 에 ComfyUI + 가상환경 설치 (install/05-build-runtime.sh)
#   editor  편집 공간: GPU 없이(--cpu) 화면·노드 설치·검증만 한다
#   worker  GPU 실행 공간: 허브가 보낸 작업 하나를 실행한다
# 컨테이너 안 경로
#   /env         환경(ComfyUI, venv)         /state   환경별 상태(설정, 입력, 캐시, 임시)
#   /srv/models  공용 모델(읽기 전용)        /srv/results  결과 저장소
#   /opt/mgs     공용 런타임 파일(읽기 전용)
set -euo pipefail
umask 0002
MODE="${1:-editor}"
PY=/env/venv/bin/python

if [[ "$MODE" == "build" ]]; then
  : "${COMFY_TAG:?}" "${TORCH_INDEX_URL:?}"
  cd /env
  git clone --depth 1 --branch "$COMFY_TAG" https://github.com/comfyanonymous/ComfyUI ComfyUI
  python3.12 -m venv /env/venv
  "$PY" -m pip install -U pip wheel setuptools
  "$PY" -m pip install torch torchvision torchaudio --index-url "$TORCH_INDEX_URL"
  "$PY" -m pip install -r ComfyUI/requirements.txt
  [[ -f ComfyUI/manager_requirements.txt ]] && "$PY" -m pip install -r ComfyUI/manager_requirements.txt
  # 노드 설치가 torch·CUDA 패키지를 다른 버전으로 바꾸지 못하게 고정 목록을 남긴다
  "$PY" -m pip freeze | grep -iE '^(torch|torchvision|torchaudio|nvidia-[a-z0-9-]+|triton)==' > /env/constraints.txt || true
  rm -rf /root/.cache
  echo "build ok: $(git -C ComfyUI describe --tags) / $("$PY" -c 'import torch;print(torch.__version__)')"
  exit 0
fi

: "${MGS_USER:?}" "${MGS_ENV:?}"
export HOME=/state/home
export XDG_CACHE_HOME=/state/cache
export PIP_CACHE_DIR=/state/cache/pip
export UV_CACHE_DIR=/state/cache/uv
export HF_HOME=/state/cache/huggingface
export TORCH_HOME=/state/cache/torch
# 바이트코드 캐시를 환경 밖에 두어, 실행만 해도 환경이 '바뀐' 것으로 감지되지 않게 한다
export PYTHONPYCACHEPREFIX=/state/cache/pycache
export PIP_CONSTRAINT=/env/constraints.txt
export UV_CONSTRAINT=/env/constraints.txt
export MGS_ROLE="$MODE"
mkdir -p /state/home /state/cache /state/input /state/user "/srv/results/$MGS_USER/$MGS_ENV"

COMMON=(
  --listen 127.0.0.1 --port 8188
  --disable-auto-launch
  --extra-model-paths-config /opt/mgs/models.yaml
  --input-directory /state/input
  --output-directory "/srv/results/$MGS_USER"
)

# ComfyUI는 127.0.0.1 에서만 듣는다(ComfyUI-Manager가 '로컬'로 인식해 노드 설치를 허용하는 조건).
# 허브는 8189 포트로 들어와 socat 을 거쳐 연결된다.
socat TCP-LISTEN:8189,fork,reuseaddr TCP:127.0.0.1:8188 &

case "$MODE" in
  editor)
    exec "$PY" /env/ComfyUI/main.py "${COMMON[@]}" \
      --cpu --enable-manager ${MGS_EDITOR_EXTRA_ARGS:-} \
      --user-directory /state/user \
      --temp-directory /state/editor
    ;;
  worker)
    # 워크플로우 안의 Ollama 노드가 기본 주소(127.0.0.1:11434)로 호출해도 허브 LLM 중계로 가게 한다
    socat TCP-LISTEN:11434,bind=127.0.0.1,fork,reuseaddr TCP:host.docker.internal:11434 &
    mkdir -p /tmp/wuser
    exec "$PY" /env/ComfyUI/main.py "${COMMON[@]}" \
      --user-directory /tmp/wuser \
      --temp-directory /state/worker
    ;;
  *)
    echo "unknown mode: $MODE" >&2; exit 2 ;;
esac
