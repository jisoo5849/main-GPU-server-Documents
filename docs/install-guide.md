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
VPN 인터페이스(`VPN_IFACE`, 기본 `tailscale0`)에도 포털·작업공간·LLM 포트를 엽니다 (14단계, VPN 을 안 쓰면 아무 영향 없음).

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

- 브라우저: `http://main/` 로그인 → **바로 내 ComfyUI 가 열림** (마지막으로 쓴 작업공간, 없으면 `main`)
- ComfyUI 상단의 **메인 GPU** 버튼(또는 `Alt+Shift+G`)을 누르면 왼쪽에 패널이 열린다.
  - 이미지 · 영상 · 기타 모델: **시작 워크플로우**(그 모델을 쓰는 공식 템플릿을 새 탭으로) / **로더 추가**(지금 캔버스에 로더 노드)
  - LLM · 임베딩: 채팅 열기, API 예시 복사
  - 메인 전체 GPU 대기열과 내 순서, 작업 이력·버전·모델 추가·API 키 바로가기
- 예전 모델 선택 화면은 `http://main/portal` 에 그대로 있다.
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

## 13. 업데이트 (GitHub 의 새 코드 반영)

```bash
cd /opt/mgs-src && sudo bash install/update.sh
```

하는 일 (순서대로):
1. 새 커밋 받기. 업데이트 스크립트 자체가 바뀌었으면 새 스크립트로 다시 실행
2. `.env` 에 없는 새 설정 항목, 직접 다시 실행해야 할 설치 스크립트(Samba·방화벽 등)가 바뀌었으면 알림
3. 허브·ComfyUI 이미지를 미리 빌드 (이 동안 서비스는 그대로 동작)
4. **GPU 대기열을 잠시 멈추고 지금 실행 중인 GPU 작업이 끝나길 기다림** (접수는 계속 받음, 기본 최대 120분)
5. `/opt/mgs` 런타임 파일 교체 (메인에서 직접 고친 `models.yaml` 은 백업)
6. 허브·DB·Ollama 다시 시작 (Open WebUI 를 쓰고 있으면 함께)
7. 런타임이 바뀐 경우에만 편집 공간 다시 만들기 → 대기열을 원래 상태로 돌림

- 처음 한 번은 업데이트 스크립트가 메인에 없으므로 먼저 `sudo git pull` 을 한 뒤 위 명령을 실행한다.
- 편집 공간을 다시 만들면 작업자마다 ComfyUI 화면이 수십 초 끊긴다. 저장하지 않은 워크플로우는 브라우저에 남아 있다.
  허브를 다시 시작할 때도 모든 화면이 잠깐 끊긴다.
- 옵션: `--wait-minutes 30` (기다리는 시간), `--no-editors` (편집 공간은 다음 업데이트 때), `--reapply` (새 커밋이 없어도 다시 반영)
- `--force` 는 GPU 작업을 기다리지 않는다. 이때 실행 중(running)인 ComfyUI 작업만 이어받고, 준비 중·정리 중 작업은
  '끊김(lost)' 으로 기록되며, 기다리던 LLM 요청은 모두 끊긴다 (결과 파일은 남는다).
- 허브가 응답하지 않으면 스크립트가 되돌리는 명령(`git reset --hard <이전 커밋>`)을 알려 준다.

## 14. 사무실 밖 접속 (VPN, 선택)

자세한 절차와 작업자 노트북 안내는 [vpn-tailscale.md](vpn-tailscale.md), 보안 판단은 [handover-2026-10-30-vpn.md](handover-2026-10-30-vpn.md)에 있습니다.

- 사무실 밖에서는 Tailscale VPN 에 들어온 기기만 메인에 접속합니다.
  - 예: 공유기 포트 포워딩, 공개 터널(Cloudflare Tunnel, ngrok, `tailscale funnel`)은 쓰지 않습니다.
- 설치 전에 회사 네트워크·IT 담당의 승인을 받습니다.
  - 예: Tailscale 은 외부 회사의 조정 서버를 씁니다. 금지되면 자체 WireGuard 로 바꿉니다.
- 방화벽을 다시 적용하고 Tailscale 을 설치합니다.

```bash
cd /opt/mgs-src && sudo bash install/04-firewall.sh
sudo bash install/06-vpn.sh
```

- 메인 로그인은 관리자가 직접 합니다.
  - 예: 나오는 주소를 브라우저로 열어 회사 계정으로 승인합니다.

```bash
sudo tailscale up --hostname=main-gpu --accept-dns=false
tailscale ip -4
```

- 관리 콘솔에서 메인의 키 만료를 끄고, 태그와 접근 정책을 넣습니다.
  - 예: 정책 예시는 [vpn-tailscale.md](vpn-tailscale.md) 3절.
- VPN 으로 열리는 것은 포털(80), 작업공간(8101~8130), LLM(11434)입니다.
  - 예: SSH(22)와 결과 폴더(Samba)는 기본으로 막히고, `.env` 의 `VPN_ALLOW_SSH`, `VPN_ALLOW_SAMBA` 를 `true` 로 바꾼 뒤 `04-firewall.sh` 를 다시 실행하면 열립니다.
  - 예: 채팅(Open WebUI, 8090)은 사무실 전용입니다.
- 작업자는 노트북에 Tailscale 을 설치하고 `http://main-gpu/`(또는 `http://100.x.y.z/`)를 엽니다.

## 문제 해결

- **대기열이 멈춤 (관리자 화면 빨간 배너)**: GPU 메모리가 기준치로 돌아오지 않은 경우입니다. `nvidia-smi` 로 남은 프로세스를
  확인하고 정리한 뒤 관리자 화면에서 "GPU 확인 완료, 대기열 재개"를 누릅니다.
  - 멈춘 채로 있어도 허브가 15초마다 스스로 풀 수 있는지 봅니다. 11-24 부터는 GPU 를 아무도 안 쓰는데(실행 공간·Ollama 모델 없음)
    '우리 몫'이 기준보다 조금(`VRAM_REBASE_MAX_MB`, 기본 1024MB 이하) 높은 채 거의 같은 값(오차 `VRAM_REBASE_TOL_MB`, 기본 64MB)으로
    `VRAM_REBASE_CHECKS`번(기본 8번, 약 2분) 이어지면 그 값을 새 기준으로 삼고 스스로 풉니다(기록 `auto_rebaselined`).
    11-21 사무실 기록(기준 534MB, 우리 몫 843~854MB, 두 번 정지·몇 시간 안 풀림)이 이 경우입니다. 유휴 판정 여유 `VRAM_IDLE_MARGIN_MB` 기본값도
    256 → 512 로 올렸습니다.
  - 안 풀릴 때는 5분마다 이유가 `docker compose logs hub` 와 관리자 화면의 정지 기록(`last_check.why`)에 남습니다:
    `ollama_loaded`(Ollama 에 모델이 올라가 있음 — 다른 작업자가 직접 호출로 계속 올리면 풀리지 않습니다), `ollama_mem`, `worker_mem`
    (실행 공간이 메모리를 씀), `ours_over_baseline`(기준보다 너무 높게 남음 — 누수나 남의 점유일 수 있어 스스로 기준을 올리지 않습니다).
- **편집 화면이 안 열림**: 포털 "내 작업공간"에서 "편집 공간 다시 시작". 로그: `sudo docker logs mgs-ed-<사용자>-<작업공간>`
- **실행 공간이 시작 중 종료**: 작업 이력의 오류 내용에 실행 공간 로그 끝부분이 남습니다. 해당 버전으로 되돌리거나 노드를 정리합니다.
- **허브 로그**: `sudo docker compose logs -f hub`
