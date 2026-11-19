FROM python:3.12-slim

# ffmpeg: 채팅 스튜디오의 긴 영상 합치기 (hub/app/studio/assemble.py, GPU 를 쓰지 않는다)
# fonts-nanum: 대사 영상의 한글 자막 그림 (assemble.render_subtitle, 11-19)
RUN apt-get update && apt-get install -y --no-install-recommends btrfs-progs git procps ffmpeg fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hub
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
