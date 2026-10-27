# 메인 GPU 서버 검수 (2026-10-27)

설계가 실기기에서 처음 도는 것이라, 아래 순서로 확인합니다. **A는 sudo 한 줄**, **B는 다른 PC 브라우저 + 이 PC 터미널**을 번갈아 씁니다.
각 단계의 `기대 결과`와 다르면 그 단계 번호와 화면을 알려 주세요.

## A. 모델 폴더 4종 설정 적용 [sudo, 1분]

허브 설정 `/opt/mgs/models.yaml` 에 `detection`, `frame_interpolation`, `geometry_estimation`, `background_removal` 폴더가 없어
얼굴 검출·프레임 보간·깊이·배경 제거 모델 5개가 화면에 안 보입니다. 고친 파일은 `runtime/models.yaml` 입니다(원본에 5줄 추가).

```bash
sudo install -m 644 -o root -g root runtime/models.yaml /opt/mgs/models.yaml
sudo install -m 644 -o root -g root runtime/models.yaml /opt/mgs-src/runtime/models.yaml
```
- 첫 줄: 컨테이너가 읽는 설정. **이미 떠 있는 편집 공간**은 다시 시작해야 반영됩니다 → 포털 "내 작업공간" → "편집 공간 다시 시작".
  새로 만드는 작업공간은 바로 반영됩니다.
- 둘째 줄: 저장소 원본도 같이 고쳐 둡니다(나중에 커밋). 
- 기대 결과: 편집 공간을 다시 시작한 뒤 ComfyUI 화면에서 `R` → 노드 검색 `Load Upscale` 또는 `RIFE` 계열 노드의 모델 칸에 `rife_v4.26.safetensors` 가 보임.

## B. 검수표 (install-guide.md 12장) — 이 PC에 맞춘 순서

### B-0. 준비 (다른 PC)
1. `http://192.168.10.29/` → `admin` 로그인 → 모델 선택 → 작업공간 `main` 의 ComfyUI 열기.
2. 템플릿 하나를 엽니다. 처음은 가벼운 `Z-Image-Turbo: Text to Image` 를 권합니다(약 19GB, 1분 안쪽).

### B-1. 편집 공간에 GPU 없음 [이 PC, sudo]
B-0 뒤에 편집 공간 컨테이너(`mgs-ed-admin-main`)가 떠 있습니다.
```bash
sudo docker ps --format '{{.Names}}\t{{.Status}}'
sudo docker exec mgs-ed-admin-main ls /dev/nvidia0
```
- 기대 결과: 둘째 줄이 `No such file or directory`. (GPU 장치가 편집 공간에 없음 = 설계의 핵심 보장)

### B-2. 실행 접수·순서 (A 혼자서도 가능)
다른 PC에서 **실행(Run) 버튼을 3번 연속** 누릅니다(seed 를 randomize 로 두면 3건 모두 새 작업).
```bash
# 이 PC, 작업이 도는 동안 몇 번 실행
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
sudo docker ps --format '{{.Names}}'
```
- 기대 결과: 포털 "대기열"에 순번 1·2·3 으로 보이고 차례로 실행. `nvidia-smi` 에 **ComfyUI(python) 프로세스는 항상 최대 1개**,
  `docker ps` 에 `mgs-worker` 는 1개만. (gnome-shell·Xwayland 는 화면 프로그램이라 무시)

### B-3. 진행 화면
- 기대 결과: 실행 중 ComfyUI 화면에 노드 강조·진행률·미리보기, 끝나면 결과 이미지가 보임. 포털 "작업 이력"에 기록됨.

### B-4. 환경 고정
1. 실행을 1건 접수한 **직후**(대기 중), Manager 로 작은 커스텀 노드 하나를 설치합니다(예: `ComfyUI-Custom-Scripts`).
2. 포털 "내 작업공간 → 버전" 을 봅니다.
- 기대 결과: 대기 작업은 **설치 전 버전(vNNNN)** 으로 실행되고, 버전 목록에 새 스냅샷이 생김.

### B-5. 메모리 해제 [이 PC]
작업이 끝난 뒤 15초쯤 지나서:
```bash
nvidia-smi --query-gpu=memory.used --format=csv
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```
- 기대 결과: ComfyUI 프로세스가 사라지거나(실행 공간 종료) 실행 공간 기본 점유만 남음(약 0.5~1.5GB). 화면 프로그램 약 300MB 는 별도.
  관리자 화면(`/admin`)에 빨간 "대기열 정지" 배너가 **없어야** 함.

### B-6. LLM 대기열
영상 작업(Wan2.2, 5분 내외)을 실행해 두고, 이 PC에서:
```bash
curl -s http://127.0.0.1:11434/api/generate -H "Authorization: Bearer <admin API 키>" \
  -d '{"model":"qwen3.5:9b","prompt":"안녕","stream":false}' | head -c 300
```
- API 키는 포털 관리자 화면에서 발급합니다. 기대 결과: **영상이 끝난 뒤** 응답이 오고, 작업 이력에 llm 작업 1건이 생김.

### B-7. 워크플로 안 Ollama 노드
`comfyui-ollama` 노드(Ollama Generate, url `http://127.0.0.1:11434`)가 든 워크플로를 실행합니다.
- 기대 결과: 멈추지 않고 완료. 이력에 내부 호출(`nested`) 기록.
- 이 환경에는 comfyui-ollama 가 아직 없으므로 Manager 로 먼저 설치(B-4 와 겸할 수 있음).

### B-8. 결과 공유 (다른 PC)
```bat
net use R: \\192.168.10.29\results /user:admin /persistent:yes
```
- 기대 결과: `R:\admin\main\<날짜>\` 에 결과 파일. 파일 하나를 지우면 `R:\.recycle\admin\` 에 남음.

### B-9. 기록
포털 `/admin` 에 로그인·작업공간 열기·모델 선택 기록이 보이면 통과.

## 통과 뒤 할 일
- `.env` 의 `VRAM_*_MARGIN_MB`, `JOB_MAX_SECONDS`, `WORKER_START_TIMEOUT` 를 실측값으로 조정 (B-5 수치 기준).
- 작업자 계정 추가: `sudo bash /opt/mgs-src/install/add-user.sh <아이디> "<이름>"`.
- 야간 DB 백업(pg_dump) 추가 — 별도 작업으로 준비.
