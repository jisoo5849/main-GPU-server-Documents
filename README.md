# 메인 GPU 서버 (main GPU server)

사무실 작업자가 브라우저로 ComfyUI 를 자유롭게 쓰고, **모든 연산은 메인(Ubuntu) GPU 한 장에서 접수 순서대로 한 건씩** 처리하는 서버입니다.

- 설계서: [docs/main-gpu-server-design.html](docs/main-gpu-server-design.html) (브라우저로 열기)
- 사용 방법(사내·외부): [docs/usage-guide.md](docs/usage-guide.md)
- 설치 안내: [docs/install-guide.md](docs/install-guide.md)
- 캐릭터 LoRA 학습 계획서 (이 서버에 학습 작업을 더하는 안): [LoRA-FINE-TUNING-Image-models](https://github.com/jisoo5849/LoRA-FINE-TUNING-Image-models)

## 사용 방법

자세한 내용: [docs/usage-guide.md](docs/usage-guide.md)

| 위치 | 접속 주소 | 준비 |
|---|---|---|
| 사내 | `http://192.168.10.29/` | 관리자가 만든 ComfyUI 계정 |
| 외부 (VPN) | `http://100.112.253.4/` | Tailscale 설치·초대 로그인(`Connected`) + ComfyUI 계정 |

- 사내와 외부 모두 같은 GPU 대기열을 쓰고, 한 번에 한 작업씩 순서대로 처리됩니다.
- 외부에서는 결과 폴더(`\\...\results`)와 채팅 화면(8090)이 막혀 있습니다. 결과는 ComfyUI 화면에서 저장합니다.
- 메인 서버가 꺼져 있거나 재부팅 중이면 사용할 수 없습니다.
- 계정은 관리자(지수)에게 요청합니다. 새 계정 만들기, 외부 접속 절차는 [docs/usage-guide.md](docs/usage-guide.md)에 있습니다.

## 핵심 동작

```
A · B · C 브라우저 ──▶ 허브 (로그인 · 화면 중계 · 실행 접수 · 스케줄러)
                        ├─ 편집 공간 (사용자·프로젝트별 Docker, GPU 없음)
                        ├─ PostgreSQL (접수 순서 대기열 · 계정 · 기록)
                        ├─ GPU 실행 공간 (항상 최대 1개, 접수 때 고정한 환경 버전으로 실행)
                        └─ Ollama (LLM · 임베딩, 같은 대기열)
공용 모델 /srv/models (읽기 전용)   결과 /srv/results (\\main\results, 누구나 읽기·쓰기)
```

1. 작업자는 `http://main/` 에 로그인하면 바로 자기 ComfyUI(`http://main:<작업공간 포트>/`)로 들어가 편집하고 노드를 설치합니다 (편집 공간에는 GPU 가 없음).
   ComfyUI 상단 **메인 GPU** 버튼으로 모델 선택·시작 워크플로우·GPU 대기열 패널을 엽니다.
2. 실행 버튼을 누르면 허브가 편집 공간에서 검증하고, **환경을 스냅샷으로 고정**하고, 접수 순번을 붙여 DB 에 넣습니다.
3. 스케줄러가 맨 앞 작업의 사용자·환경 버전·모델을 확인하고, 그 버전으로 GPU 실행 공간을 띄워 실행합니다.
4. 진행률·미리보기·결과는 작업자의 ComfyUI 화면에 그대로 나옵니다.
5. 결과·기록을 저장하고, GPU 메모리가 기준치로 돌아왔는지 메인에서 직접 측정한 뒤 다음 작업으로 넘어갑니다.

## 저장소 구성

| 경로 | 내용 |
|---|---|
| `compose.yaml` | 상시 서비스: PostgreSQL, Ollama, 허브, (선택) Open WebUI |
| `.env.example` | 설정값. `.env` 로 복사해서 사용 |
| `install/` | 메인 설치 스크립트 (00 확인 → 01 패키지 → 02 저장소 → 03 Samba → 04 방화벽 → 05 ComfyUI 이미지·템플릿), 사용자 추가 |
| `images/comfy-runtime/` | 편집·실행 공간 공용 이미지 (Ubuntu 24.04 + Python 3.12) |
| `runtime/` | 모든 환경에 읽기 전용으로 연결되는 파일: 공용 모델 경로(`models.yaml`), 허브 연동 노드(`mgs_bridge`) |
| `hub/` | 허브 (Python, Starlette): 포털, 화면 중계, 접수, 스케줄러, LLM 중계, 관리 명령 |
| `docs/` | 설계서, 설치 안내 |

## 빠른 설치 순서 (메인에서)

```bash
git clone https://github.com/jisoo5849/main-GPU-server-Documents.git /opt/mgs-src
cd /opt/mgs-src
cp .env.example .env        # 값 수정 (비밀번호, 사무실 네트워크 대역 등)
bash install/00-check.sh
sudo bash install/01-host-packages.sh
sudo bash install/02-storage.sh --format /dev/<빈 파티션>   # 이미 btrfs 면 --format 생략
sudo bash install/03-samba.sh
sudo bash install/04-firewall.sh
sudo bash install/05-build-runtime.sh
sudo bash install/06-vpn.sh          # 선택: 사무실 밖 접속(Tailscale). 이후 sudo tailscale up 은 직접
sudo docker compose up -d --build
sudo bash install/add-user.sh admin "관리자" --admin
sudo bash install/add-user.sh alice "앨리스"
```

업데이트: `cd /opt/mgs-src && sudo bash install/update.sh`

사무실 밖 접속은 Tailscale VPN 으로만 허용하고 인터넷 쪽 포트는 열지 않습니다: [docs/vpn-tailscale.md](docs/vpn-tailscale.md)

자세한 설명과 확인 방법은 [docs/install-guide.md](docs/install-guide.md) 에 있습니다.

## 현재 상태

- 버전 0.1: 설계서의 1~5단계 기능을 처음 구현한 상태입니다. **메인 실기기에서 아직 실행해 보지 않았습니다.**
  첫 설치 때 설치 안내의 "검수" 항목으로 확인하고, 문제가 나오면 이 저장소에서 고칩니다.
- 개발 PC 에서 확인한 것: 파이썬 문법 검사, `hub/tests` 단위 테스트(작업 내용 다루기).
- 아직 없는 것: 작업 이력의 "같은 환경 버전으로 다시 실행" 버튼, 실행 공간의 외부 접속 허용 목록(현재는 인터넷 접속 허용),
  프로젝트 작업공간 공유(여러 사용자가 같은 작업공간 사용), Grafana 대시보드.
