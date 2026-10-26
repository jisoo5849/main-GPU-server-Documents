# 메인 GPU 서버 (main GPU server)

이 저장소는 사무실 작업자가 브라우저로 ComfyUI 를 쓰고, 모든 연산은 메인(Ubuntu, GPU 1장 72GB)에서
접수 순서대로 한 건씩 처리하는 서버의 코드와 문서다. 설계는 끝났고, 메인 실기기 설치·검증이 다음 단계다.

## 먼저 읽을 것

1. `docs/handover-2026-10-23.md` — 지금까지의 결정, 현재 상태, 다음 할 일 (대화 기록 요약)
2. `README.md` — 구조와 설치 순서
3. `docs/install-guide.md` — 메인 설치 절차와 검수표
4. `docs/main-gpu-server-design.html` — 설계서 v1.1 (브라우저로 열기)

## 바뀌지 않는 설계 원칙

- **메인 GPU 는 한 번에 한 작업만.** DB 순번(seq)이 유일한 실행 순서. 편집 공간에는 GPU 장치를 절대 연결하지 않는다.
  GPU 는 `hub/app/dockerctl.py` 의 `run_worker()` 에서만 요청한다.
- **환경 고정.** 실행 접수 때 btrfs 읽기 전용 스냅샷을 찍고, 대기 중인 작업은 그 버전으로 실행한다.
- **모든 GPU 사용은 DB 를 거친다.** LLM·임베딩도 같은 `jobs` 테이블에 들어간다. 실행 중 워크플로우 안의 Ollama 호출만
  대기열을 거치지 않는다 (교착 방지, `llm_calls.nested=true` 로 기록).
- **메모리 해제 확인은 메인에서 NVML 로 직접 잰다.** 기준에 못 미치면 단계적으로 정리하고, 끝내 안 되면 대기열을 멈춘다(BLOCKED).
- **결과 폴더(`\\main\results`)는 누구나 읽기·쓰기.** 삭제·덮어쓰기는 `.recycle/<사용자>/` 에 30일 보관.
  공용 모델 저장소(`/srv/models`)는 읽기 전용이고 `model-inbox` → 포털 "모델 추가" 로만 넣는다.
- Redis·Celery 는 쓰지 않는다 (GPU 1장, 순서 보장은 PostgreSQL 잠금과 LISTEN/NOTIFY 로 충분).

## 작업 규칙

- `sudo` 가 필요한 명령은 Claude 가 직접 실행하지 않고 사용자에게 명령을 안내한다.
- 메인에서 실행할 파일은 LF 줄바꿈 (`.gitattributes` 로 고정). Windows 에서 편집해도 유지된다.
- 커밋 메시지와 문서는 한국어. 코드 주석은 "왜" 를 짧게.
- 버전 기준 (2026-10-23 확인): Ubuntu 24.04, NVIDIA 드라이버 R580+ (open), Docker 29.x,
  NVIDIA Container Toolkit 1.20.x (CDI, runc 기본 유지), CUDA 13 / torch cu130, ComfyUI v0.42.5, PostgreSQL 18.
- 설정값은 전부 `.env` (`.env.example` 참고). 코드에 경로를 박지 않는다.

## 코드 지도

| 파일 | 역할 |
|---|---|
| `hub/app/main.py` | 포털(80), LLM 중계(11434), 환경 포트(8101~)를 한 프로세스로 듣고 포트별로 나눔 |
| `hub/app/portal.py` + `templates/` | 로그인(`/` → 내 ComfyUI 로 이동), `/portal` 모델 선택, 내 작업공간·버전, 공유 작업 이력, 모델 추가, 관리자 |
| `hub/app/panel.py` | ComfyUI 안 '메인 GPU' 패널용 API (`/mgs/api/me·catalog·queue·select·starter·keys`, 환경 포트) |
| `hub/app/templates_index.py` | 공식 워크플로우 템플릿에서 모델 파일 → 시작 워크플로우 찾기 (정확 일치 → 같은 모델 다른 양자화, 파일 이름 바꿔 넣기) |
| `runtime/nodes/mgs_bridge/web/mgs_panel.js` | ComfyUI 프런트엔드 확장: 상단 '메인 GPU' 버튼 + 왼쪽 패널 (1.57.1 번들로 API 확인) |
| `install/update.sh` | 메인 업데이트: git → 런타임 복사 → compose → 편집 공간 재생성 (`.applied-commit` 로 반영 여부 기억) |
| `hub/app/proxy.py` | 환경 포트의 ComfyUI 화면 중계. `/prompt`, `/queue`, `/history`, `/api/jobs`, `/view`, `/ws` 는 허브가 처리 |
| `hub/app/jobs.py` | 실행 접수 (편집 공간 검증 → 스냅샷 고정 → 입력 고정 → 저장 위치 → DB 순번), 대기열·이력 조회 |
| `hub/app/scheduler.py` | GPU 한 건 루프, 실행 공간 생명주기, 진행 중계, 결과 수집, 메모리 해제 확인, 감시, 복구 |
| `hub/app/llm.py` | Ollama·OpenAI 형식 중계, 내부 호출 직통, API 키 |
| `hub/app/envs.py` | 작업공간 생성·복제·되돌리기, 스냅샷 버전, Manager 설치 중 감지, 오래된 버전 정리 |
| `hub/app/dockerctl.py` | 편집 공간(GPU 없음)·실행 공간(GPU, 최대 1개) 컨테이너, 네트워크(icc=false) |
| `hub/app/gpu.py` | NVML 측정, PID → 컨테이너 매핑 |
| `hub/app/promptops.py` | 워크플로우 JSON 다루기 (순수 함수, `hub/tests` 로 검증) |
| `hub/app/schema.sql` | DB. `jobs_one_gpu_holder` 고유 인덱스가 "실행 중 1건" 을 DB 수준에서 보장 |
| `runtime/nodes/mgs_bridge` | 편집 공간용 `/mgs/validate`, 편집 공간 자체 큐 차단 |
| `images/comfy-runtime/entrypoint.sh` | build / editor / worker 세 모드 |
| `install/` | 메인 설치 스크립트 (순서대로 00~05, add-user) |

## 현재 상태 (2026-10-23)

- v0.1 코드 완성. 개발 PC 에서 문법 검사, 단위 테스트 8개, 화면 렌더링까지 확인.
- **메인 실기기에서는 아직 실행하지 않았다.** 첫 설치에서 나오는 문제는 이 저장소에서 고친다.
- 다음 단계는 `docs/handover-2026-10-23.md` 의 "다음 할 일" 참고.

## 테스트

```bash
cd hub && python -I tests/test_promptops.py && python tests/test_templates_index.py
```
