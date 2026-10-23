# 메인 GPU 서버 설치 안내

메인(Ubuntu 24.04, GPU 1장)에 처음 설치하는 순서입니다. `sudo` 가 붙은 명령은 메인 관리자가 직접 실행합니다.

## 0. 준비물

- **NVIDIA 드라이버**: Blackwell 카드는 580 이상(open 커널 모듈). `nvidia-smi` 가 동작해야 합니다.
- **btrfs 로 쓸 빈 파티션 또는 디스크**: 작업공간과 환경 버전(스냅샷)을 저장합니다. 200GB 이상 권장.
- **NVMe**: 공용 모델 저장소(`/srv/models`). 작업마다 모델을 다시 읽기 때문에 SATA SSD 면 느려집니다.
- 메인 주소: 작업자 PC 에서 `main` 이라는 이름으로 접속하려면 공유기 DNS 또는 각 PC 의 `hosts` 파일에 등록합니다.
  (`.env` 의 `HUB_PUBLIC_HOST` 를 IP 로 써도 됩니다.)

## 1. 받기와 설정

```bash
sudo git clone https://github.com/jisoo5849/main-GPU-server-Documents.git /opt/mgs-src
cd /opt/mgs-src
sudo cp .env.example .env
sudo nano .env
```

반드시 바꿀 값: `POSTGRES_PASSWORD`, `HUB_SECRET`(`openssl rand -hex 32`), `LAN_SUBNET`(사무실 대역), `HUB_PUBLIC_HOST`.

## 2. 상태 확인

```bash
bash install/00-check.sh
```

RAM, CPU, 디스크(NVMe 여부), GPU, Docker 상태를 보여 줍니다. 이 결과로 btrfs 파티션과 모델 저장소 위치를 정합니다.

## 3. 패키지 (Docker, NVIDIA Container Toolkit, btrfs, Samba, ufw)

```bash
sudo bash install/01-host-packages.sh
```

끝에서 두 가지를 자동 확인합니다.
- GPU 를 요청하지 않은 컨테이너에서는 GPU 가 **보이지 않아야** 합니다 (편집 공간이 GPU 를 못 쓰는 근거).
- GPU 를 요청한 컨테이너에서는 GPU 가 보여야 합니다.

## 4. 저장소

```bash
sudo bash install/02-storage.sh --format /dev/nvme1n1p1   # 예시. 파티션 내용이 지워집니다
# 이미 /srv/envs 가 btrfs 로 마운트되어 있으면:
sudo bash install/02-storage.sh
```

만들어지는 것
- `/srv/envs` (btrfs): `templates/` 템플릿, `live/` 작업공간, `snap/` 환경 버전, `run/` 실행 공간 사본, `state/` 설정·입력·캐시
- `/srv/models`: 공용 모델 (`checkpoints`, `diffusion_models`, `loras`, `vae`, `text_encoders` …, `ollama`, `inbox`)
- `/srv/results`: 결과 저장소. 그룹 `comfyusers` 읽기·쓰기, 새 파일도 같은 권한을 물려받음
- `/opt/mgs`: 모든 환경에 읽기 전용으로 연결되는 파일

## 5. Samba

```bash
sudo bash install/03-samba.sh
```

- `\\main\results`: 계정이 있는 사용자 누구나 읽기·쓰기. 지운 파일은 `.recycle\<사용자>\` 에 30일 보관.
- `\\main\model-inbox`: 모델 업로드. 포털의 "모델 추가"에서 종류를 골라 공용 저장소로 옮깁니다.

## 6. 방화벽

```bash
sudo bash install/04-firewall.sh
```

사무실 대역에서만 포털(80), 작업공간 포트(8101~8130), LLM(11434), 채팅(8090), Samba 를 허용합니다.
컨테이너는 메인의 LLM 중계 포트(11434)만 쓸 수 있습니다.

## 7. ComfyUI 이미지와 템플릿

```bash
sudo bash install/05-build-runtime.sh
```

`COMFY_TAG`(기본 v0.42.5) ComfyUI 와 torch(cu130)를 설치한 템플릿을 만들고, GPU 에서 torch 가 카드를 인식하는지 확인합니다.
새 작업공간은 이 템플릿의 스냅샷으로 시작하므로 거의 즉시 만들어집니다.

## 8. 서비스 시작

```bash
sudo docker compose up -d --build
sudo docker compose logs -f hub        # "hub ready" 가 나오면 정상
# 선택: LLM 채팅 화면
sudo docker compose --profile webui up -d
```

## 9. 사용자 추가

```bash
sudo bash install/add-user.sh admin "관리자" --admin
sudo bash install/add-user.sh alice "앨리스"
```

리눅스 계정(파일 소유자, 로그인 불가), Samba 비밀번호, 허브 로그인, 기본 작업공간(`main`)을 한 번에 만듭니다.

## 10. 작업자 PC (Windows)

- 브라우저: `http://main/` 로그인 → 모델 선택 → 작업공간의 ComfyUI 가 열림
- 결과 폴더 연결 (명령 프롬프트):

```bat
net use R: \\main\results /user:alice /persistent:yes
```

## 11. 모델 넣기

- ComfyUI 모델: Samba `\\main\model-inbox` 에 넣고 포털 "모델 추가"에서 옮기기. 또는 메인에서 `/srv/models/<종류>/` 에 직접 복사.
- Ollama 모델: 관리자 API 키로 `curl http://main:11434/api/pull -H "Authorization: Bearer <키>" -d '{"model":"qwen3:8b"}'`

## 12. 검수 (처음 설치 후 반드시)

| 확인 | 방법 | 기대 결과 |
|---|---|---|
| 편집 공간에 GPU 없음 | `sudo docker exec mgs-ed-alice-main ls /dev/nvidia0` | 파일 없음 |
| 실행 접수·순서 | A·B 가 동시에 실행 버튼 여러 번 | 포털 대기열에 순번대로, `nvidia-smi` 에 GPU 프로세스 최대 1개 |
| 진행 화면 | 실행 중 ComfyUI 화면 | 노드 강조, 진행률, 미리보기, 결과 이미지 표시 |
| 환경 고정 | 실행 접수 후 대기 중에 노드 하나 설치 | 대기 작업은 설치 전 환경으로 실행 (포털 > 버전에서 확인) |
| 메모리 해제 | 작업 사이 `nvidia-smi` | 실행 공간 기본 점유(약 0.5~1.5GB)만 남음 |
| LLM 대기열 | 영상 작업 중 LLM API 호출 | 영상 작업이 끝난 뒤 처리됨 |
| 워크플로우 안 Ollama 노드 | Ollama 노드가 든 워크플로우 실행 | 멈춤 없이 완료, 이력에 내부 호출 기록 |
| 결과 공유 | 다른 사용자 계정으로 `\\main\results` 에서 파일 수정·삭제 | 수정 가능, 삭제한 파일은 `.recycle` 에 있음 |
| 기록 | 관리자 화면 | 로그인, 작업공간 열기, 모델 선택 기록 |

## 문제 해결

- **대기열이 멈춤 (관리자 화면 빨간 배너)**: GPU 메모리가 기준치로 돌아오지 않은 경우입니다. `nvidia-smi` 로 남은 프로세스를
  확인하고 정리한 뒤 관리자 화면에서 "GPU 확인 완료, 대기열 재개"를 누릅니다.
- **편집 화면이 안 열림**: 포털 "내 작업공간"에서 "편집 공간 다시 시작". 로그: `sudo docker logs mgs-ed-<사용자>-<작업공간>`
- **실행 공간이 시작 중 종료**: 작업 이력의 오류 내용에 실행 공간 로그 끝부분이 남습니다. 해당 버전으로 되돌리거나 노드를 정리합니다.
- **허브 로그**: `sudo docker compose logs -f hub`
