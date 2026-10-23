# 편집 공간과 GPU 실행 공간이 함께 쓰는 공용 이미지.
# ComfyUI·커스텀 노드·Python 패키지는 이미지에 넣지 않는다. 각 환경 폴더(/env)에 있고 환경마다 다르다.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.12 python3.12-venv python3.12-dev python3-pip \
      git git-lfs socat ffmpeg tini procps curl ca-certificates \
      build-essential cmake pkg-config \
      libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/mgs-entrypoint
RUN chmod 755 /usr/local/bin/mgs-entrypoint

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/mgs-entrypoint"]
CMD ["editor"]
