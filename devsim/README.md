# devsim — 노트북용 시험 허브

메인의 허브와 같은 흐름(로그인 → 실행 접수 → 순번 대기열 → 한 번에 한 건 GPU 실행 → 진행 중계 → 메모리 해제 확인 → 기록)을
Docker·btrfs·PostgreSQL 없이 Windows 노트북에서 돌려 본다. 이 PC 의 ComfyUI Desktop 과 Ollama 를 그대로 쓴다.

## 실행

1. ComfyUI Desktop 을 켠다 (기본 주소 `http://127.0.0.1:8000`). Ollama 도 켜져 있어야 LLM 시험이 된다.
2. `devsim\run.cmd` 를 더블클릭 (처음 한 번은 가상환경을 만드느라 1~2분 걸린다).
3. 브라우저에서 `http://127.0.0.1:8080/` → `alice` / `test1234` 로 로그인.
4. "내 ComfyUI" 버튼 → `http://127.0.0.1:8101/` 에 ComfyUI 화면이 열린다. 워크플로우를 만들고 **Run**.
5. 다른 브라우저(시크릿 창)에서 `bob` 으로 로그인해 `:8102` 에서 동시에 Run 을 눌러 본다.

## 확인할 것

| 확인 | 어디서 |
|---|---|
| 두 사용자가 동시에 Run 해도 한 번에 하나만 실행 | 포털 대기열, `nvidia-smi` |
| 진행률·미리보기·결과가 **실행을 누른 탭에만** 표시 | alice 탭과 bob 탭 비교 |
| 작업 사이에 VRAM 이 기준으로 돌아옴 | 작업 이력의 VRAM 열 (전 → 최대 → 후) |
| LLM 요청이 같은 줄에 섬 | ComfyUI 실행 중에 아래 curl 실행 → 끝난 뒤 응답 |
| 접속·실행·LLM 호출 기록 | 포털 "접속 기록", "작업 이력" |

```bat
curl http://127.0.0.1:11436/api/generate -H "Authorization: Bearer alice" -d "{\"model\":\"llama3.2\",\"prompt\":\"안녕\",\"stream\":false}"
```

## 메인과 다른 점

- 작업공간 2개가 ComfyUI 하나를 함께 쓴다. 메인에서는 사용자마다 별도 컨테이너(편집 공간)이고 GPU 는 실행 공간에만 붙는다.
- 환경 고정은 `custom_nodes` 폴더 목록의 해시로 기록만 한다. 메인에서는 btrfs 스냅샷으로 실제 고정.
- 메모리 해제는 `/free` 두 단계까지만. 메인에서는 실행 공간 종료·Ollama 재시작·대기열 정지까지 간다.
- 결과 파일은 ComfyUI Desktop 의 output 폴더(`<사용자>/<날짜>/...`)에 남는다. Samba 공유 없음.
- 환경 변수로 바꿀 수 있는 값: `COMFY_URL`, `OLLAMA_URL`, `CUSTOM_NODES_DIR`, `PORTAL_PORT`(8080), `LLM_PORT`(11436), `VRAM_MARGIN_MB`(1500).
