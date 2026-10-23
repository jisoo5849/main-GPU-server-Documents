FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends btrfs-progs git procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hub
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
