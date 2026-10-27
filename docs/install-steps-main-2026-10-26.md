# 메인 GPU 서버 설치 순서 (이 PC 맞춤, 2026-10-26)

원본 안내(`/opt/mgs-src/docs/install-guide.md`)와 달라진 점만 이 문서에 반영했습니다.
`sudo` 명령은 `CLAUDE.md` 규칙대로 직접 실행해 주세요. 각 단계 끝의 **확인**이 맞아야 다음으로 갑니다.

## 이 PC에 맞춘 결정 (사용자 확인함)

| 항목 | 설계 기본값 | 이 PC |
|---|---|---|
| 기존 서비스 | - | **새 시스템으로 교체**: 영상 서버(443), 브라우저 ComfyUI(8443), 호스트 Ollama(11434)는 중지 |
| btrfs 저장소 | 빈 파티션을 포맷 | 빈 파티션이 없어서 **`/home` 안의 300GB 이미지 파일**(`/home/mgs/envs.img`)을 btrfs로 만들어 `/srv/envs`에 마운트 |
| 모델·결과·DB 위치 | `/srv/...` (루트 파티션, 여유 157GB) | **`/home/mgs/{models,results,pg}`** (모델이 360GB라 루트에 안 들어감) |
| 주소 | `192.168.0.x`, `main` | `192.168.10.29`, 대역 `192.168.10.0/24` (이름 대신 IP로 접속) |

`.env` 초안은 `.env (.env.example 을 복사해 작성)`(권한 600, 비밀번호는 무작위 생성)입니다. 위 값이 이미 들어 있습니다.

## 0. 작업이 없는지 확인하고 기존 서비스 중지 [sudo]

```bash
# [일반] 영상 서버에 실행 중인 작업이 없어야 합니다 (running 0)
/home/elixir/comfy-video-server/.venv/bin/cvsctl status
```
```bash
# [sudo] 기존 서비스 중지 (파일과 설정은 그대로 둡니다. 되돌리기는 맨 아래)
sudo systemctl disable --now comfy-video-worker comfy-video-api comfy-video-backup.timer
sudo docker compose -f /home/elixir/comfyui-interactive/compose.yaml stop
sudo docker compose -f /home/elixir/comfyui-docker/compose.yaml stop
sudo systemctl disable --now nginx
```
**확인:** `ss -ltn | grep -E ':(443|8443|8188|8189)\b'` 에 아무것도 안 나와야 합니다.

## 1. 폴더와 .env [sudo]

```bash
sudo mkdir -p /home/mgs/models /home/mgs/results /home/mgs/pg
sudo install -m 600 -o root -g root .env (.env.example 을 복사해 작성) /opt/mgs-src/.env
sudo grep -cE '^[A-Z_]+=' /opt/mgs-src/.env      # 값이 있는 줄 수 (30개 안팎)
```

## 2. 패키지 [sudo]

```bash
cd /opt/mgs-src && sudo bash install/01-host-packages.sh
```
btrfs-progs, samba, acl, ufw, jq를 설치하고 docker를 재시작합니다(이 PC에는 Docker 29.9, NVIDIA 툴킷 1.20.1이 이미 있어서 건너뜁니다).
**확인:** 끝의 두 가지 — GPU 요청 없는 컨테이너는 `/dev/nvidia0`이 안 보이고(`OK`), GPU를 요청한 컨테이너는 `nvidia-smi -L`에 카드가 보여야 합니다.

## 3. btrfs 이미지와 저장소 [sudo]

```bash
sudo mkdir -p /srv/envs
sudo truncate -s 300G /home/mgs/envs.img          # 빈 파일(실제로는 쓴 만큼만 차지)
sudo mkfs.btrfs -f -L mgs-envs /home/mgs/envs.img
echo '/home/mgs/envs.img /srv/envs btrfs loop,noatime,compress=zstd:1,x-systemd.requires-mounts-for=/home 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount /srv/envs
findmnt -no FSTYPE /srv/envs                       # btrfs 라고 나와야 함
```
```bash
cd /opt/mgs-src && sudo bash install/02-storage.sh   # --format 없이 실행
```
**확인:** `findmnt -no FSTYPE,SOURCE /srv/envs` → `btrfs /dev/loopN`, `ls /home/mgs/models` 에 `checkpoints loras vae …`가 생겨 있어야 합니다.

## 4. 모델 옮기기 [sudo]

받아 둔 모델은 같은 파티션(`/home`)이라 **하드링크**로 연결합니다(추가 공간 0, 원본 폴더도 그대로).

```bash
sudo cp -al /home/elixir/comfyui-interactive/storage/models/. /home/mgs/models/
sudo cp -al /home/elixir/comfyui-docker/storage/models/. /home/mgs/models/
du -sh /home/mgs/models
```
Ollama 모델(56GB)은 다른 파티션(`/`)에 있어서 **복사**합니다(몇 분).
```bash
sudo systemctl disable --now ollama
sudo cp -a /usr/share/ollama/.ollama/models/. /home/mgs/models/ollama/
sudo ls /home/mgs/models/ollama                    # blobs manifests
```
**알려진 빈틈:** `runtime/models.yaml`에 `detection`, `frame_interpolation`, `geometry_estimation`, `background_removal` 폴더가 없어서
이 4종은 ComfyUI 화면에 안 보입니다. 설치가 끝나면 `models.yaml`에 줄을 추가하는 수정을 따로 합니다(작업자 화면에는 영향 없음).

## 5. Samba, 방화벽 [sudo]

```bash
cd /opt/mgs-src && sudo bash install/03-samba.sh
sudo bash install/04-firewall.sh
```
방화벽은 22·80·8101~8130·11434·Samba만 `192.168.10.0/24`에 허용합니다. 443·8443은 열지 않습니다(0단계에서 중지했으므로 의도한 것).
**확인:** `sudo ufw status verbose` 에 위 규칙이 보여야 합니다.

## 6. ComfyUI 이미지와 템플릿 [sudo]

```bash
cd /opt/mgs-src && sudo bash install/05-build-runtime.sh
```
ComfyUI v0.42.5와 torch(cu130)를 받아 템플릿을 만듭니다(10~20분, 수 GB 다운로드).
**확인:** 마지막에 `torch … cuda 13… gpu NVIDIA RTX PRO 5000 …`이 출력되어야 합니다.

## 7. 서비스 시작 [sudo]

```bash
cd /opt/mgs-src && sudo docker compose up -d --build
sudo docker compose logs -f hub        # "hub ready" 가 나오면 Ctrl+C
```

## 8. 사용자 추가 [sudo]

```bash
cd /opt/mgs-src
sudo bash install/add-user.sh admin "관리자" --admin
sudo bash install/add-user.sh 사용자이름 "표시이름"
```
비밀번호를 묻습니다(채팅에 입력하지 마세요).

## 9. 작업자 PC에서

- 브라우저: `http://192.168.10.29/` → 로그인 → 모델 선택 → ComfyUI
- 결과 폴더(명령 프롬프트): `net use R: \\192.168.10.29\results /user:사용자이름 /persistent:yes`
- 이전 주소(`https://video.lan:8443`, Remote Video 노드)는 더 이상 동작하지 않습니다.

## 10. 검수

`/opt/mgs-src/docs/install-guide.md` 12장의 9개 항목. 처음에는 **편집 공간 GPU 없음**, **동시 실행 시 GPU 프로세스 1개**부터 확인합니다.

## 되돌리기 (새 시스템을 쓰지 않기로 할 때)

```bash
cd /opt/mgs-src && sudo docker compose down
sudo systemctl enable --now ollama nginx comfy-video-worker comfy-video-api comfy-video-backup.timer
sudo docker compose -f /home/elixir/comfyui-docker/compose.yaml start
sudo docker compose -f /home/elixir/comfyui-interactive/compose.yaml start
sudo ufw disable
```
(원래 Ollama 모델과 `~/comfyui-*` 모델 파일은 지우지 않았으므로 그대로 동작합니다.)

## 첫 설치에서 나올 수 있는 문제 (인계 문서 5장)

1. 허브가 `pid: host`로 도는데 NVML PID가 컨테이너 PID로 보이면, 모든 GPU 프로세스를 "외부"로 기록합니다.
2. btrfs 이미지를 `loop`로 마운트했으므로 허브(privileged 컨테이너)에서 스냅샷이 안 되면 마운트 전파 문제입니다.
3. 실행 공간에서 메인의 11434(허브 LLM 중계)가 안 닿으면 방화벽 `WORKER_SUBNET` 규칙을 확인합니다.
4. 이 PC는 Ubuntu 26.04입니다(설계는 24.04 기준). 호스트 쪽 단계에서 패키지 이름·저장소 오류가 나면 그 메시지를 알려 주세요.
