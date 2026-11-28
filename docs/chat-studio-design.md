# 채팅 스튜디오 설계 (러다이트식 채팅 화면 + 백그라운드 자동화)

- 날짜: 2026-11-12
- 상태: **P1~P4 구현, 메인 확인 완료(2026-11-13).** 0~20절은 설계 시점(2026-11-12) 기준이고, 구현에서 달라진 점과 메인에서 잰 값은 **21절**에 있다. 쓰고 운영하는 방법은 `docs/chat-studio-guide.md`.
- 기준 코드: 브랜치 `crew-b2`, 커밋 `e2ceb95`. 문서의 `파일:줄` 은 모두 이 커밋 기준이다(구현 뒤에는 줄 번호가 달라졌다).
- 개정: 같은 날 검토(정확성·완전성·실현성) 지적을 반영했고, 그 반영을 다시 본 검증 지적도 반영했다.
- 결정(2026-11-12, 사용자): D3·D17·D18 은 18.3 의 '결정' 줄대로 정했고, 나머지 D 는 18.3 의 '권장' 칸대로 진행한다. 아래 본문에서 D3·D17·D18 을 따로 적은 곳은 이 결정으로 고쳤다.
- 표기: [추정] 은 재지 않았거나 근거가 약한 값, [TBD] 는 아직 모르거나 정해야 하는 것이다. [CUI]·[FE]·[TPL]·DFC 같은 줄임말은 20절에 있다.

## 0. 한눈에 보기

| 항목 | 내용 |
|---|---|
| 목표 | 사용자는 채팅 화면만 본다. 허브가 뒤에서 ComfyUI 와 지금까지 만든 AI 기능을 자동으로 돌린다. 채팅에는 완성 전 진행(단계와 %)과 결과(이미지·영상)만 보인다. |
| 권장안 | 허브가 새 채팅 화면을 직접 제공한다(포털 포트 80 의 `/studio`). 허브 안의 '감독'이 요청을 받아 기존 GPU 대기열로 실행한다. 15절의 대안 3이다. |
| 그대로 쓰는 것 | GPU 대기열(접수 함수 `jobs.submit_comfy` 와 스케줄러), 진행 중계(`relay.sockets`), 제작진 모드와 프롬프트 도우미(`promptx`), 결과 폴더 규칙, 로그인 세션. 영상 렌더러·자동 고치기(`video.render`·`video.fix`)와 결과 검사(`promptx.inspect`)도 그대로 부르지만, P3 의 한 차례 묶기와 선택 수정이 있다(D22) |
| 앞선 제안(A1·A2)·렌더러 v1 과 달라진 것 | ① 결과는 각자 폴더가 아니라 시스템 계정 `studio` 폴더 아래 사용자별 하위 폴더에 쌓인다(A2, 7.4, D4). ② 채팅은 프레임·크기·시드를 레시피에 자동으로 넣는다. 영상 렌더러 v1 은 '제안만'이다(1.3, D17). ③ 결과 검사는 결과를 채팅에 보낸 뒤에 한다(A1, 3.1, D22). ④ A2 에서 그대로 쓴다고 한 허브 안 AI 모델 차례 함수(`llm.llm_turn`)를 고친다(9.2, D22). A1·A2 의 뜻은 20.1 |
| 새로 만드는 것 | 채팅 화면과 WebSocket, 감독(상태 기계·의도 판단), 레시피(API 형식 워크플로우 + 값 넣을 자리), 진행률 변환, 채팅 DB 표(`chat_*`)와 레시피 검증 기록(`studio_recipe_checks`), 업로드, 편집 지시문(P3), 레시피 CLI `studio-check`·`studio-import`(P2), 운영자 기록 화면 |
| 조금 고치는 기존 코드 | 허브 안 AI 모델 차례 함수(`llm.py`: 컨텍스트 변수, 차례를 놓는 길 보강, 9.2), 포털 경로와 허브 시작 작업(`portal.py`·`main.py`), 메뉴 링크(`base.html`), DB 표(`schema.sql`), 설정(`config.py`·`.env.example`), 코드 지도(`CLAUDE.md`). P2 에 `cli.py`, P3 에 `video.py`(한 차례 묶기), P4 에 `db.py`, 선택으로 `promptx.py`. 스케줄러·접수·중계·패널은 바꾸지 않는다(19.2) |
| ComfyUI 화면 | 그대로 둔다. 고급 사용자와 관리자가 계속 쓴다. 채팅은 입구를 하나 더하는 것이다. |
| 단계 | P1 이미지 올리기 → 이미지→영상 → 진행률 → 결과 (레시피 1개) / P2 이미지 생성 + 제작진 / P3 글→영상·이미지 편집·이어 말하기 / P4 감독 판단·Ask 계획 카드·운영자 기록 화면 (16절) |
| 사용자가 정할 것 | 18.3 의 D1a~D22 (D3·D17·D18 은 정해졌고 나머지는 권장안으로 확정). P1 전에: D3·D4·D6·D10·D11·D12·D14~D17·D20·D22 (권장대로면 한 번에 승인할 수 있다). P2 전: D19. P2 뒤: D13. P3 뒤: D7. P4 전: D1a·D1b·D2·D8. P1 측정 뒤: D21. 언제든(기본값으로 시작): D5·D9·D18 |

설계가 정한 것 여섯 가지 (개발자용. 사용자가 정할 것은 18.3):

1. 채팅 작업은 시스템이 관리하는 채팅 전용 작업공간(`studio-chat`)에서 실행한다. 사용자 작업공간은 쓰지 않는다(7절).
2. 허브는 화면 없이 ComfyUI 화면용 워크플로우를 실행용 형식(API 형식)으로 바꿀 수 없다. 그래서 실행용 형식의 레시피에 값을 넣을 자리를 미리 적어 둔다(6절).
3. GPU 를 쓰는 일은 기존 대기열을 거치는 두 함수로만 한다: AI 모델 차례(`llm.llm_turn`)와 ComfyUI 접수(`jobs.submit_comfy`). GPU 를 쓰지 않는 확인(Ollama 모델 목록 `/api/tags`, 편집 공간 검증 V11·V12)만 감독 쪽이 직접 부른다(4절, 6.4).
4. 진행률은 ComfyUI 의 노드별 단계 진행을 레시피에 적은 가중치로 합친다. %는 '생성' 단계에서 보이고, 그 전에는 대기 순번과 단계 이름이 보인다(8절).
5. 작업이 끝났는지는 DB 의 작업 상태(`jobs.state`·`outcome`)로 정한다. 진행 메시지는 화면을 빨리 바꾸는 데만 쓴다. 스케줄러가 메시지 없이 작업을 끝내는 길이 있기 때문이다(5.13).
6. 채팅 기록은 새 DB 표(`chat_*`)에 둔다. 기존 작업 표(`jobs`)는 바꾸지 않는다(9절).

## 1. 목표와 범위

### 1.1 목표

| # | 사용자 요구 | 이 설계에서 |
|---|---|---|
| G1 | 러다이트처럼 채팅 화면만 보인다. ComfyUI 는 보이지 않는다 | `/studio` 한 화면만 쓴다. ComfyUI 는 숨은 일꾼(실행 공간)으로만 돈다. P1~P3 은 `/studio` 주소(북마크·포털 링크)로 들어오면 로그인 뒤에도, 채팅 화면에서 로그아웃한 뒤에도 채팅 화면으로 돌아온다. 로그인 기본 착지(`/`)까지 채팅으로 바꾸는 것은 P4 다(11.1, 14.1, D1b) |
| G2 | 뒤에서 ComfyUI 와 지금까지 만든 기능이 모두 자동으로 돈다 | 아래 표의 기능을 5·6절에서 하나씩 연결한다 |
| G3 | 채팅에는 결과(이미지·영상)와 완성 전 %·현재 단계만 보인다 | 진행 카드와 결과 카드만 둔다. 실패와 경고는 한 줄 안내로 낸다(13절) |
| G4 | 모든 것이 자동이다 | 사용자는 모델·노드·길이·해상도를 고르지 않는다. 프레임·크기·시드는 레시피가 자동으로 넣는다(1.3, D17). Ask 계획 카드는 사용자가 켤 때만 나온다(5.7) |

G2 의 기능과 채팅에서 쓰는 곳:

| 기능 | 채팅에서 쓰는 곳 | 기존 코드 |
|---|---|---|
| 장문 프롬프트(네 단락) | I2V·T2V 프롬프트 작성 | `video.render` 장문 프로필 (`hub/app/video.py:829-914`, `hub/app/video_knowledge.json:93-107`) |
| 제작진 모드 | 제품·공간·풍경 요청이면 자동 (사람·손 숨김·캐릭터 정본 요청은 건너뜀) | `promptx.crew_route` (`hub/app/promptx.py:430-441`), 영상 쪽은 `video.render` 안 (`hub/app/video.py:857-859`, `886-895`) |
| 프롬프트 도우미 | 이미지 생성, T2V 장면 | `promptx.expand_in_turn` (`hub/app/promptx.py:444-484`) |
| LLM | 의도 판단, 프롬프트, 검사 | `llm.llm_turn` (`hub/app/llm.py:245-321`) |
| 첫 화면 읽기(VLM) | I2V | `video.read_frame` (`hub/app/video.py:464-466`) |
| 길이 자동 선택 | I2V·T2V | `video.decide_length` (`hub/app/video.py:196-224`), `auto_length` (`hub/app/video.py:877-880`) |
| 규칙 위반 자동 고치기 | 낱말 수가 아닌 위반이 남으면 (5.12) | `video.fix` (`hub/app/video.py:917-967`) |
| 결과 자동 검사(VLM) | 결과를 보낸 뒤 (기본은 대기열에 다른 ComfyUI 줄이 없을 때만, D15) | `promptx.inspect` (`hub/app/promptx.py:517-550`) |
| 긍정·부정 프롬프트 노드 판단 | 레시피 등록 때 검증, 접수 직전 확인 (5.5) | `promptCandidates`·`pickPositive`·`NegativeGuard` (`runtime/nodes/mgs_bridge/web/mgs_nodes.js:101-151`, `200-228`) → 파이썬으로 옮기고, 부정 글 노드가 없는 구성(0 조건 노드)까지 다룬다(6.4) |
| 워크플로우 종류 판별 | 레시피 등록 때 검증 | `workflowKind` (`runtime/nodes/mgs_bridge/web/mgs_nodes.js:240-260`) → 파이썬으로 옮기고, 이미지·편집 판별을 더한다(6.4) |

### 1.2 범위 (P1~P4)

- 만드는 것: 이미지 생성, 이미지→영상(I2V), 글→영상(T2V), 이미지 편집.
- 이어 말하기: 직전 결과를 바탕으로 고친다.
  - 예: "더 천천히", "배경만 바꿔줘", "이걸로 영상 만들어줘".
- 진행 표시, 실패 안내, 다시 만들기·다시 시도, 취소, Ask 계획 카드(선택), 운영자 기록 화면.

### 1.3 비목표

- ComfyUI 화면과 '메인 GPU' 패널을 없애지 않는다. 작업자가 ComfyUI 를 자유롭게 쓰는 것은 기존 요구다(`docs/handover-2026-10-23.md:12`).
- 컷 구성(클립 잇기), 한 번에 5초를 넘는 영상, 사운드, LTX 는 다루지 않는다. 영상 렌더러 v1 의 제외 범위와 같다(`docs/video-renderer.md:16-22`, `157-161`). 요청 글에 이런 지정이 있으면 말없이 무시하지 않고 한 줄로 알린다(5.3, 13.1).
- 렌더러 v1 의 다른 결정과는 다르게 간다. v1 은 "프레임 수·fps 는 제안만 표시(노드에 자동으로 넣지 않음)"로 정했다(`docs/video-renderer.md:22`). 이것은 패널 규칙으로 그대로 둔다. 패널은 권장 설정을 보여 주기만 한다(`runtime/nodes/mgs_bridge/web/mgs_panel.js:602`). 채팅은 G4(모두 자동) 때문에 레시피가 프레임·크기·시드를 자동으로 넣는다. 사용자 확인 항목 D17 이다.
- 공개 게시, 외부 공유 링크, SNS 영상 리믹스는 하지 않는다. 인터넷 쪽 포트를 열지 않는다(`CLAUDE.md:26`).
- 러다이트를 호출하거나 자동화하지 않는다. 러다이트의 공식 MCP 도 연결하지 않는다(이유는 2.4).
- 대기열 우선 처리('더 빨리!')는 없다. 순서는 전원 공평하다(`docs/main-gpu-server-design.html:424`).
- 채팅 답을 글자 단위로 흘려 보내지 않는다. 허브 안 LLM 호출이 `stream: False` 다(`hub/app/llm.py:279`).
- 일반 대화용 챗봇이 아니다. 그 역할은 Open WebUI(8090)다(`compose.yaml:77-93`). 채팅 스튜디오는 만들기 전용이다.
- 여러 장 레퍼런스, 한 요청에 이미지와 영상을 함께 만드는 묶음 요청은 P4 이후 후보다 [TBD].

## 2. 사용자 경험

### 2.1 화면

```
┌──────────────┬──────────────────────────────────────────────────────┐
│ [+ 새 대화]   │  파일럿 1인칭 비행                                      │
│              │                                                      │
│ 오늘          │                          [cockpit.png 썸네일]          │
│  파일럿 1인칭 │                나: 이 조종석 사진으로 파일럿의 1인칭      │
│  비 오는 골목 │                    비행 영상을 만들어줘                  │
│ 어제          │  ┌ 진행 ───────────────────────────────────────────┐  │
│  향수 광고    │  │ ✓ 첫 화면 읽기  ✓ 프롬프트 작성  ● 생성 62%  ○ 마무리 │  │
│              │  │ ████████████████░░░░░░░░░  62%   경과 2분 13초     │  │
│ ──────────── │  │                                          [취소]  │  │
│ 설정          │  └────────────────────────────────────────────────┘  │
│ 긴 영상은     │                                                      │
│ 먼저 확인 [끔] │ ┌──────────────────────────────────────────────────┐ │
│              │ │ [첨부] 만들고 싶은 장면을 적어 주세요          [보내기] │ │
└──────────────┴─┴──────────────────────────────────────────────────┴─┘
```

- 왼쪽에는 대화 목록과 설정 하나("긴 영상은 먼저 계획 확인")만 있다.
- 가운데에는 말풍선마다 진행 카드가 붙고, 끝나면 결과 카드로 바뀐다. 러다이트도 말풍선마다 결과 카드를 붙인다(DFC/01:66).
- 입력창은 글과 이미지 첨부(버튼·끌어 놓기·붙여 넣기)만 받는다. 사진만 보내도 된다(글이 비거나 공백뿐이면 기본 요청문, 5.3 R0).
- 입력창 안내 글은 P1 동안 "사진을 올리고 원하는 움직임을 적어 주세요. 사진만 보내도 됩니다."이고, 이미지 생성이 생기는 P2 부터 그림의 "만들고 싶은 장면을 적어 주세요"다(11.4).
- 모델, 노드, 길이, 해상도, 품질을 고르는 칸은 없다(G4).
- 화면 구성과 상태별 모습은 11절에 있다.

### 2.2 대화 예시

**① 이미지 → 영상 (P1)**

```
나        [cockpit.png] 이 조종석 사진으로, 비행기를 운항하는 파일럿의 1인칭 시점 영상 만들어줘
스튜디오   (진행 카드) 대기 1번째 → 첫 화면 읽는 중 → 대기 1번째 → 프롬프트 작성 중 → 대기 1번째 → GPU 준비 중
                     → 생성 25% … 100% → 마무리
스튜디오   (결과 카드) [영상 5초 · 848×480] [내려받기] [다시 만들기]
```

- 사진만 보내도 된다. 글이 비면(공백뿐이어도) "이 사진 속 장면이 부드럽게 움직이는 짧은 영상"을 요청 글로 쓴다(5.3 R0).

뒤에서 일어나는 일:

| 순서 | 하는 일 | 기존 코드 |
|---|---|---|
| 1 | 올린 이미지를 채팅 작업공간 입력 폴더에 저장 | 새 코드 (7.3) |
| 2 | 첫 화면 읽기 (8B 시각 모델, GPU 차례 1) | `video.render` 안 `llm_turn(..., "video_frame")` (`hub/app/video.py:865-868`) |
| 3 | 1인칭 감지, 길이 자동 선택, 장문 네 단락 작성 (27B, GPU 차례 2) | `video.render` (`hub/app/video.py:869-899`), `pov_wanted` (`hub/app/video.py:286-288`) |
| 4 | 낱말 수가 아닌 규칙 위반이 남으면 자동 고치기 (27B, GPU 차례 3) | `video.fix` (`hub/app/video.py:917-967`) |
| 5 | Wan 2.2 I2V 레시피에 값을 넣고 접수 (GPU 차례 4) | `jobs.submit_comfy` (`hub/app/jobs.py:108-152`) |
| 6 | 진행 메시지를 받아 %로 바꾸고, 끝났는지는 DB 로 확인 | `relay.sockets.register` (`hub/app/relay.py:14-15`) + 새 변환기 (8절), `jobs.state`·`outcome` (5.13) |
| 7 | 결과 파일을 찾아 채팅에 보냄 | `job_outputs` (`hub/app/scheduler.py:452-463`) |
| 8 | 가운데 프레임을 검사 (8B, GPU 차례 5, 결과를 보낸 뒤. 기본값은 대기열에 다른 ComfyUI 줄이 없을 때만, D15) | `promptx.inspect` (`hub/app/promptx.py:517-550`) |

**② 이미지 생성 (P2)**

```
나        비 오는 밤 골목의 작은 카페, 창문에서 새어 나오는 따뜻한 빛
스튜디오   (진행 카드) 대기 1번째 → 프롬프트 작성 중 → GPU 준비 중 → 생성 40% → 마무리
스튜디오   (결과 카드) [이미지] [내려받기] [다시 만들기] [이걸로 영상 만들기]
```

- 첨부도 영상 말도 없으므로 이미지 생성이다(규칙 판단, 5.3).
- 공간 요청이라 제작진 모드가 자동으로 켜진다(`promptx.crew_route`, `hub/app/promptx.py:430-441`). 사람이 나오는 요청, 손 숨김 요청, 캐릭터 정본 이름이 있는 요청은 조용히 건너뛴다(`hub/app/crew.py:67-76`).

**③ 이어 말하기 (P3)**

```
나        더 천천히
스튜디오   (진행 → 결과) 같은 첫 화면, 같은 장면에서 카메라만 더 느리게
나        배경만 노을로 바꿔줘        ← 직전 결과가 이미지일 때
스튜디오   (진행 → 결과) 직전 이미지를 편집한 결과
```

- "더 천천히"는 직전 영상 요청을 고치는 것이다. 첫 화면 읽기 결과를 다시 쓰고(8B 차례를 아낀다), 직전 네 단락을 고쳐 쓰기 시작점으로 준다(5.8).
- "배경만 바꿔줘"는 직전 이미지 결과를 편집 대상으로 하는 편집 요청이다.
- '직전 결과'는 이 대화에서 결과(이미지·영상)를 낸 마지막 성공 요청의 결과다. 답만 한 요청(정해 둔 답 한 줄 등)은 건너뛴다. 앞 요청이 아직 돌고 있으면 새 메시지는 그것이 끝난 뒤에 판단한다(5.8, 5.11).

**④ 실패와 경고**

```
스튜디오   (결과 카드) [영상]
스튜디오   검사에서 얼굴이 뭉개져 보입니다. 다시 만들어 볼까요?                    [다시 만들기]

스튜디오   글자·간판 표현을 자동으로 빼지 못한 채 만들었습니다. 프롬프트를 다시 써 볼까요?  [다시 시도]

스튜디오   오래 기다려도 차례가 오지 않아 멈췄습니다. 다시 시도할까요?             [다시 시도]
```

- 안내는 한 줄이고 버튼 하나를 붙인다. 원인과 기록은 운영자 화면에서 본다(12·13절).
- [다시 만들기]는 같은 프롬프트로 시드만 바꿔 생성 단계부터 한다. [다시 시도]는 같은 요청 글로 처음부터 한다(13.2).

**⑤ Ask 계획 카드 (P4, 사용자가 켰을 때만)**

```
스튜디오   (계획 카드)
           이미지 → 영상 · 5초(81프레임, 16fps) · 848×480 · 빠름
           글 길이: 장문 — 요청에 사람과 1인칭 시점이 있어 자세히 쓰기로 했습니다.
           카메라: 조종석 시점에서 천천히 앞으로 나아갑니다.
           움직임: 창밖 구름이 흐르고 손이 조종간을 살짝 당깁니다.
           시간: 5초 동안 한 번의 부드러운 움직임 뒤 차분히 멈춥니다.
           완료까지 약 4분 [추정]
                                                       [취소] [승인]
```

- 세 줄 계획(카메라·움직임·시간)은 영상 렌더러가 이미 돌려주는 `ko` 세 칸이다(`hub/app/video.py:910`).
- 길이 이유는 `length_choice.reason` 이다(`hub/app/video.py:913`, `221`). 첫 화면을 못 읽어 움직임만 프로필로 바뀌면 길이 판단을 건너뛰어 `length_choice` 가 없다(`hub/app/video.py:872-880`). 그때는 "글 길이: 짧게 — 사진을 읽지 못해 움직임만 썼습니다"를 보인다.

### 2.3 진행 표시 (사용자가 보는 순서)

```
대기 2번째 (첫 화면 읽기 차례) → 첫 화면 읽는 중 → 대기 1번째 (프롬프트 작성 차례) → 프롬프트 작성 중
→ (대기 1번째 (다듬기 차례) → 프롬프트 다듬는 중) → 대기 1번째 (생성 차례) → GPU 준비 중
→ 생성 12% … 62% … 100% → 마무리 → (결과 카드)
```

- GPU 가 한 장이라 단계마다 대기열에 다시 줄을 선다. 그래서 '대기'가 여러 번 나올 수 있다(5.6).
- %는 '생성' 단계에서만 숫자로 보인다. 다른 단계는 단계 이름과 경과 시간을 보인다(8.6에 이유).
- 종류(action)마다 진행 카드의 제목·단계 칩·단계 글은 8.4 표를 따른다.

### 2.4 러다이트에서 가져온 것과 가져오지 않은 것

| 가져온 것 (흐름만 참고해 새로 구현) | 러다이트 관찰 | 우리 쪽 |
|---|---|---|
| 채팅 화면, 말풍선마다 결과 카드 | DFC/01:19, 60, 66 | 2.1, 11절 |
| 감독(에이전트)이 설정을 대신 정함 | DFC/01:76, DFC/02:108 | 감독 + 레시피가 정한다 (5·6절) |
| Ask/Auto 진행 방식 | DFC/01:80 | 기본은 자동, Ask 는 사용자가 켤 때만 (5.7) |
| 계획 카드와 승인·취소 | DFC/01:77-79, DFC/02:102-106 | 계획 카드 (2.2 ⑤, 11절). 러다이트도 질문 없이 카드 한 장으로 승인만 받았다(DFC/02:106) |
| 대기열 공개, 예상 시간, 단계 글 | DFC/01:62, 65, DFC/02:131 | 대기 순번·단계·경과 시간·남은 시간 [추정] (8절). %는 러다이트 기록에 없어 우리가 더한다 |
| 한 줄 → 한국어 계획 → 영어 상세의 3층 | DFC/03:85-88 | 사용자 말 → `ko` 세 칸 → 영문 네 단락 (`docs/video-renderer.md:163-192`) |
| 작업 이름 자동 변경 | DFC/02:131 | 대화 제목을 첫 요청 글 앞 30자로 자동 설정(10.1), P4 부터 LLM 판단의 `title_ko`(5.3) |

| 가져오지 않은 것 | 이유 |
|---|---|
| '더 빨리!' 우선 처리 (DFC/02:132) | 전원 공평한 순서 (`docs/main-gpu-server-design.html:424`) |
| 공개 게시, 탐색, URL 리믹스 | 인터넷 쪽 포트 금지 (`CLAUDE.md:26`) |
| 요금·크레딧 | 사내 도구 |
| 사운드 칸 | 렌더러 v1 이 사운드를 다루지 않고(`docs/video-renderer.md:21`), Wan 레시피는 소리 없는 영상을 낸다(5.3). v2 후보 (`docs/video-renderer.md:160`) |
| 기획을 외부 API 로 하는 구조 | 모든 GPU 사용은 메인 대기열을 거친다 (`CLAUDE.md:20-21`) |
| 완료·실패 때 아무 말 없음 (DFC/02:131) | 실패는 한 줄로 알린다 (13절) |
| 러다이트 공식 Remote MCP 연결 (DFC/01:46, 100-104) | 약관 12조(승인되지 않은 자동화 금지)에 걸릴 수 있어 연결하지 않는다. 러다이트에 서면 질의는 보내지 않는다(사용자 결정). 이 설계는 우리 메인 안에서만 도는 자체 시스템이다 |

러다이트에 관한 제약 (이 설계가 지키는 것):

- 러다이트를 스크립트나 브라우저로 자동화하지 않는다(약관 12조, DFC/02:9, DFC/03:106).
- 러다이트의 공식 Remote MCP 도 연결하지 않는다(위 표).
- 러다이트 결과물로 다른 모델을 학습·미세조정하지 않는다(DFC/02:11).
- 니키 등 자체 IP 원본을 러다이트에 올리지 않는다(DFC/02:10).
- UI·결과물 자산을 복제하지 않는다. 흐름만 참고해 우리 쪽에서 새로 만든다(DFC/03:107).
- 이 설계는 러다이트를 부르는 코드를 하나도 갖지 않는다. 우리 메인 서버 안에서만 돈다.

## 3. 전체 구조

### 3.1 구조도

```
 브라우저: 채팅 화면  http://<메인>/studio
   │  REST  /studio/api/*  (대화·메시지·업로드·승인·취소·다시 만들기)
   │  WS    /studio/ws     (진행·결과·안내 이벤트)
   ▼
 허브 (uvicorn 한 프로세스, 포털 포트 80 앱에 경로 추가)
   ├─ studio.api / studio.ws ── 로그인 세션 재사용 (security.session_user)
   │        │
   │        ▼
   │   감독 studio.director  (요청 하나 = asyncio 작업 하나, 상태는 chat_turns 에 저장)
   │     (1) 의도: image / i2v / t2v / edit / answer, 이어 말하기(revise·repeat), 첨부 이미지의 역할
   │     (2) 프롬프트: 첫 화면 읽기 · 제작진 · 도우미 · 장문 · 길이 자동 · 자동 고치기
   │     (3) 레시피 고르기 → 값 넣기 (프롬프트·첫 화면·시드·프레임·크기·저장 이름)
   │     (4) jobs.submit_comfy → DB 대기열(seq) → 스케줄러 → 실행 공간
   │           └ relay.sockets 에 수신기를 등록해 진행 메시지를 받는다 → 진행률 변환
   │     (5) 끝 확인(jobs.state·outcome) → 결과 찾기(job_outputs) → 채팅에 전달 → 결과 검사(VLM)
   │
   ├─ llm.llm_turn ─────── jobs(kind=llm) ── 스케줄러 ── Ollama (27B 확장, 8B 시각)
   └─ jobs.submit_comfy ── 편집 공간 검증 → 스냅샷 → 입력 고정 → jobs(kind=comfy)
                              └ 스케줄러 ── 실행 공간 mgs-worker (ComfyUI, 사용자에게 안 보임)

 PostgreSQL: jobs · job_outputs · llm_calls (기존, 그대로) + chat_* · studio_recipe_checks (새로)
```

- A1 제안의 (5)는 '결과 검사 → 채팅에 전달' 순서였다. 검사는 8B 차례 하나라 앞에 다른 작업이 있으면 몇 분이 늘 수 있다. 사용자가 검사를 기다리지 않게 '전달 → 검사'로 바꿨다(5.6 규칙 4). A1 을 바꾸는 것이라 사용자 결정 D22 에 넣었다(A1 은 20.1).
- 작업이 끝났는지는 진행 메시지가 아니라 DB 로 확인한다(5.13).

### 3.2 구성 요소

| 구성 요소 | 파일 (새로) | 하는 일 | 단계 |
|---|---|---|---|
| 채팅 화면 | `hub/app/templates/studio.html`, `hub/app/static/studio.js`, `studio.css` | 대화 목록, 말풍선, 진행·결과·계획 카드, 입력창 | P1 |
| REST API | `hub/app/studio/api.py` | 대화·메시지·업로드·파일·승인·취소 (10.1) | P1 |
| WebSocket | `hub/app/studio/ws.py` | 사용자별 이벤트 전달 (10.2) | P1 |
| 감독 | `hub/app/studio/director.py` | 상태 기계, 단계 실행, 재시도, 복구 (5절) | P1 (I2V 만) → P4 |
| 의도 판단 | `hub/app/studio/intent.py` | 글의 길이·비율·소리 지정 뽑기, 규칙 → LLM (5.3) | P1 지정 뽑기, P2 규칙, P4 LLM |
| 레시피 | `hub/app/studio/recipes.py`, `hub/app/studio/recipes/*.json` | 읽기·검증·값 넣기 (6절) | P1 |
| 진행률 변환 | `hub/app/studio/progress.py` | 수신기, % 계산, 단계 이름 (8절) | P1 |
| 저장 | `hub/app/studio/store.py` | `chat_*` 표 읽기·쓰기 (9절) | P1 |
| 파일 | `hub/app/studio/files.py` | 업로드 검사·저장, 결과 파일 내보내기 (7.3, 14절) | P1 |
| 운영자 화면 | `hub/app/studio/admin.py`, `templates/admin_studio*.html` | 기록 보기 (12절) | P4 (기록 쌓기는 P1 부터) |

### 3.3 그대로 쓰는 것과 새로 만드는 것

| 구분 | 무엇 | 위치 | 채팅에서 |
|---|---|---|---|
| 그대로 | ComfyUI 작업 접수 | `jobs.submit_comfy` (`hub/app/jobs.py:108-152`) | 레시피로 만든 API 형식 prompt 를 넣는다. 편집 공간 검증·스냅샷·입력 고정·저장 위치·순번을 그대로 거친다 |
| 그대로 | GPU 한 건 루프, 진행 중계, 결과 수집 | `hub/app/scheduler.py:154-177`, `364-418`, `431-465` | 손대지 않는다 |
| 그대로 | 진행 메시지 받기 | `relay.sockets.register(env_id, client_id, obj)` (`hub/app/relay.py:14-15`) | `send_text`·`send_bytes` 를 가진 수신기 객체를 등록한다. 진행 메시지는 그 키로만 전달된다(`hub/app/relay.py:44-50`) |
| 그대로 | 도우미·제작진 | `promptx.expand`·`expand_in_turn`·`crew_route` (`hub/app/promptx.py:430-503`) | 이미지 생성, T2V 장면 |
| 그대로 (P1·P2) | 영상 렌더러·자동 고치기 | `video.render`·`video.fix` (`hub/app/video.py:829-967`) | P1·P2 는 함수를 그대로 부른다. P3 에서 같은 차례로 묶는 함수를 꺼내 쓴다(5.6) |
| 그대로 | 결과 검사 | `promptx.inspect` (`hub/app/promptx.py:517-550`) | 이미지 결과, 영상의 가운데 프레임. 파일 읽기를 스레드로 옮기기와 (P3 선택) 같은 차례의 첫 화면 읽기는 선택 수정이다(19.2, D22) |
| 그대로 | 대기 순번 계산 | `jobs.global_queue` (`hub/app/jobs.py:338-345`), `panel.queue` 의 순번 규칙 (`hub/app/panel.py:152-179`) | 대기 n번째 |
| 그대로 | 결과 폴더 규칙 | `promptops.rewrite_filename_prefix` (`hub/app/promptops.py:24-33`), 사이드카 (`hub/app/scheduler.py:467-477`) | 저장 이름 앞에 사용자·요청을 넣는다(7.4). 위치는 A2 제안과 다르다: 시스템 계정 `studio` 폴더 아래 사용자 하위 폴더 (D4) |
| 그대로 | 로그인 | `security.session_user` (`hub/app/security.py:42-61`), `portal.login_required` (`hub/app/portal.py:48-54`) | 같은 쿠키 `mgs_sess` |
| 조금 고침 | 허브 안 LLM 차례 | `llm.llm_turn` (`hub/app/llm.py:245-321`) | 의도·프롬프트·검사 모두 이것으로 부른다. 컨텍스트 변수 셋(job id·끝난 이유, 대기 시간, 중첩 막기)과 차례를 놓는 모든 길에서 `waiter.done` 세우기(9.2). 패널이 부를 때는 동작이 같다. A2 를 바꾸는 것이라 D22 |
| 조금 고침 | 포털 경로, 시작 작업 | `portal.build_app` (`hub/app/portal.py:380-398`), `main.startup` (`hub/app/main.py:19-27`) | `/studio` 경로 추가, 스케줄러 시작 전 `studio.prepare()`·뒤 `studio.start()` 호출 (5.10) |
| 조금 고침 (P3) | 한 차례로 묶는 렌더러 함수 | `video.render`·`video.fix` 본문 (`hub/app/video.py:869-899`, `951-953`) | `render_in_turn`·`fix_in_turn` 으로 꺼내고, 기존 함수는 감싸기만 한다(패널 동작 그대로, D22) |
| 새로 | 채팅 화면·API·WS, 감독, 레시피, 진행률, 채팅 표, 업로드, 운영자 화면 | 3.2 표 | |

### 3.4 기존 ComfyUI 화면·패널과의 관계

- ComfyUI 화면과 '메인 GPU' 패널은 그대로 둔다. 노드를 직접 다루고 싶은 사용자와 관리자가 계속 쓴다.
- 둘은 같은 허브 함수를 쓴다(`promptx`, `video`, `submit_comfy`). P3 의 렌더러 꺼내기도 `render`·`fix` 의 인자와 결과를 바꾸지 않으므로 패널 API(`hub/app/panel.py:221-279`)와 패널 시험이 그대로다.
- 상태는 나누어 둔다. 패널 설정은 브라우저 localStorage 에 있고(`runtime/nodes/mgs_bridge/web/mgs_panel.js:42-55`), 채팅 상태는 DB 의 `chat_*` 에 있다. 서로 읽지 않는다.
- 같은 대기열을 쓰므로 서로 보인다.
  - ComfyUI 화면의 대기 숫자는 메인 전체 활성 작업 수라 채팅 작업도 센다(`hub/app/proxy.py:277-278`, `307-320`).
  - 패널의 대기열 목록과 공유 작업 이력에 채팅 작업이 "<사용자> · chat" 으로 보인다(`hub/app/panel.py:152-179`, `hub/app/portal.py:265-296`).
- 관리자는 채팅 작업공간의 ComfyUI 를 열어 노드를 설치한다. 레시피 내보내기는 관리용 작업공간에서 한다(6.7). studio-chat 의 ComfyUI 화면에는 모든 채팅 사용자의 대기 작업이 보이고, '대기열 비우기'와 id 없는 '중단'이 채팅 작업을 멈추기 때문이다(`hub/app/proxy.py:99-111`). 재현용 API JSON 은 레시피와 넣은 값으로 다시 만든 것을 내려받아 ComfyUI 에서 다시 실행해 볼 수 있다(10.1, 12절).
- 채팅 결과를 사용자의 ComfyUI 로 넘기는 버튼("내 ComfyUI 에서 열기")은 P4 이후 후보다 [TBD].

## 4. 바뀌지 않는 설계 원칙과의 관계

`CLAUDE.md:15-35` 의 원칙과 작업 규칙을 채팅이 어떻게 지키는지 적는다.

| 원칙 | 근거 | 채팅 설계에서 |
|---|---|---|
| 메인 GPU 는 한 번에 한 작업만 | `CLAUDE.md:17`, `hub/app/schema.sql:120-122` | 감독은 GPU 를 직접 쓰지 않는다. 모든 단계가 `jobs` 한 줄이 되고 스케줄러가 한 건씩 실행한다(`hub/app/scheduler.py:169-174`). `jobs_one_gpu_holder` 고유 색인도 그대로다 |
| DB 순번(seq)이 유일한 실행 순서 | `CLAUDE.md:17`, `hub/app/jobs.py:43-58` | 채팅 단계마다 `insert_job` 이 순번을 새로 받는다. 차례 예약·묶기·우선순위는 만들지 않는다. 그래서 단계 사이에 다른 사람 작업이 끼어들 수 있고, 화면에 그대로 보인다(2.3). 채팅의 `pending` 대기(같은 대화의 앞 요청, 사용자 한도, 5.11)는 `jobs` 에 줄을 넣기 전의 문지기다. 넣는 시점만 늦출 뿐 넣은 뒤의 실행 순서는 바꾸지 않는다 |
| GPU 는 `run_worker()` 에서만 요청 | `CLAUDE.md:17-18`, `hub/app/dockerctl.py:143-165` | 채팅 코드는 Docker 를 다루지 않는다. 실행 공간은 스케줄러가 띄운다 |
| 편집 공간에는 GPU 장치를 붙이지 않는다 | `CLAUDE.md:17` | 채팅은 편집 공간을 CPU 검증에만 쓴다: 접수 검증(`/mgs/validate`, `hub/app/jobs.py:61-75`)과 레시피 검사 V11·V12(6.4). 채팅 코드는 컨테이너를 만들거나 장치를 붙이지 않는다 |
| 모든 GPU 사용은 DB 를 거친다 (LLM 포함) | `CLAUDE.md:20-21` | 의도 판단, 첫 화면 읽기, 프롬프트 작성, 결과 검사가 모두 `llm.llm_turn` 이다. `llm_turn` 은 `kind='llm'` 줄을 넣고 차례를 기다린다(`hub/app/llm.py:252-261`). 차례 안에서 다른 차례를 열거나 생성 결과를 기다리지 않는다(교착, 5.6 규칙 5) |
| 내부 호출(nested) 예외 | `CLAUDE.md:20-21`, `hub/app/llm.py:168-185` | 채팅은 이 예외를 쓰지 않는다. LLM 단계를 ComfyUI 작업 안에 넣어 차례를 줄이는 방법도 쓰지 않는다. 그 예외는 교착 방지용이지 최적화용이 아니다. 레시피 등록 때 Ollama 를 부르는 노드가 있으면 거부한다(6.4) |
| 메모리 해제 확인(NVML), BLOCKED | `CLAUDE.md:22`, `hub/app/scheduler.py:581-608`, `234-241` | 그대로다. 채팅은 DB `settings` 표의 `paused`·`blocked` 를 `db.get_setting` 으로 읽어 점검·정지 안내(8.4 의 화면 글)를 보여 주기만 한다(`hub/app/panel.py:177-178` 과 같은 값). 정지·점검 동안의 대기는 채팅의 대기 기한에 세지 않는다(5.6) |
| 환경 고정(스냅샷) | `CLAUDE.md:19`, `hub/app/envs.py:148-171` | 채팅 작업도 `submit_comfy` 를 거치므로 접수 때 `pin_version` 으로 고정된다(`hub/app/jobs.py:127`). 첨부 이미지도 `pin_inputs` 로 작업 전용 사본이 된다(`hub/app/jobs.py:129-130`) |
| 결과 폴더는 누구나 읽기·쓰기 | `CLAUDE.md:23` | 채팅 결과도 결과 폴더에 그대로 쌓이고 모두가 볼 수 있다. 결과 파일 메타데이터와 설명 파일에 든 최종 영문 프롬프트도 함께 공유된다(14.2). 채팅은 결과 파일을 지우지 않는다(9.4). 누구나 덮어쓸 수 있으므로 결과를 보이거나 입력으로 복사하기 전에 바뀌었는지 본다(7.3). 한국어 대화 글은 본인과 관리자만 본다(14.2) |
| 공용 모델 저장소는 읽기 전용 | `CLAUDE.md:24`, `hub/app/dockerctl.py:51` | 레시피는 저장소에 있는 모델만 쓴다. 등록 검증에서 `models_index.resolve` 로 파일이 있는지 본다(6.5). 채팅이 모델을 받지 않는다 |
| Redis·Celery 를 쓰지 않는다 | `CLAUDE.md:25` | 감독은 허브 프로세스 안의 asyncio 작업이다. 상태는 PostgreSQL(`chat_turns`)에 두고, 사용자 화면으로의 전달은 프로세스 안 이벤트 목록으로 한다. 허브가 한 프로세스라 가능하다(`hub/app/main.py:63-71`). 별도 프로세스인 CLI(`studio-check`)는 결과를 DB 에 쓰고 허브가 요청 때 읽는다(6.4) |
| 인터넷 쪽 포트를 열지 않는다, 밖은 VPN 으로만 | `CLAUDE.md:26`, `install/04-firewall.sh:14`, `38` | 새 포트를 만들지 않는다. 이미 LAN 과 VPN 에 열린 포털 포트 80 에 경로만 더한다. HTTP 만 쓴다(`docs/handover-2026-10-30-vpn.md:82-91`) |
| 설정은 전부 `.env` | `CLAUDE.md:35`, `hub/app/config.py:1` | 새 값은 `STUDIO_*` 로 `config.py` 와 `.env.example` 에 함께 넣는다(19.3). 값에 공백·따옴표·#·$ 를 쓰지 않는다(`.env.example:3-4`). 기록 폴더 같은 경로도 `.env` 값(`STUDIO_RECORD_DIR`)으로 둔다 |
| sudo 는 사용자에게 안내 | `CLAUDE.md:30` | 시스템 계정 만들기 같은 sudo 명령은 문서에 안내만 한다(7.2) |
| 새 state·kind 값은 같은 커밋에서 CHECK 도 | `hub/app/schema.sql:219-221` | `jobs.kind`·`state`·`outcome` 에 새 값을 더하지 않는다. `jobs.kind` 의 CHECK 는 CREATE TABLE 안에 있어 `schema.sql` 을 고쳐도 기존 DB 에는 반영되지 않는다(`hub/app/schema.sql:86`). 바꾸려면 자동으로 붙은 제약 이름을 찾아 따로 지우고 다시 붙여야 한다. 새 표의 CHECK 는 이름을 붙여 나중에 바꿀 수 있게 한다(9.1) |
| LF 줄바꿈, 한국어 문서·커밋 | `CLAUDE.md:31-32` | 새 파일도 같다 |
| 작업자 PC 는 브라우저만 | `docs/handover-2026-10-23.md:11` | 채팅도 브라우저만 쓴다 |

## 5. 감독 (조율 서버)

### 5.1 위치와 실행 방식

- 파일은 `hub/app/studio/director.py` 다. 허브 프로세스 안에서 돈다.
  - 별도 서비스로 빼면 API 키로 접수할 길이 없고(`hub/app/proxy.py:51-61` 는 쿠키만 본다), 진행 메시지 목록(`hub/app/relay.py:61`)과 LLM 대기자(`hub/app/scheduler.py:48`)가 프로세스 메모리에 있어 쓸 수 없다. 그래서 허브 안에 둔다.
- 요청(턴) 하나마다 asyncio 작업 하나를 띄운다. 참조는 집합에 잡아 둔다(선례: `Scheduler.spawn`, `hub/app/scheduler.py:71-75`).
- 단계가 끝날 때마다 결과를 `chat_turns` 에 저장한다. 허브가 다시 시작해도 마지막 단계부터 이어 간다(5.10).
- 이미지 열기·메뉴 뽑기 같은 CPU 일은 `asyncio.to_thread` 로 뺀다. 이벤트 루프 하나가 모든 ComfyUI 화면 중계도 맡기 때문이다(선례: `hub/app/video.py:852`, `hub/app/promptx.py:469`). 다만 그대로 쓰는 `promptx.inspect` 는 결과 파일 읽기·base64·Pillow 자르기를 루프에서 한다(`hub/app/promptx.py:522-524`, `537-539`). 안에 `llm_turn` 이 있어 감독이 스레드로 감쌀 수 없다(20.4).
- 감독은 GPU 를 직접 쓰지 않는다. 기다리는 동안 하는 일은 DB 의 `jobs` 줄과 진행 메시지를 지켜보는 것뿐이다.
- 감독 작업을 취소하는 것은 요청마다 한 번이다(5.9). 상태 바꾸기는 모두 조건부다(5.2).

### 5.2 상태 기계

`state` 는 DB 가 CHECK 로 막는 큰 상태이고, `stage` 는 코드에서만 정하는 세부 단계다. 세부 단계가 늘어도 CHECK 를 바꾸지 않게 둘을 나눈다(`hub/app/schema.sql:219-221` 의 교훈).

| state | 뜻 |
|---|---|
| `pending` | 받았지만 아직 시작 전. 같은 대화의 앞 요청이 끝나기를, 또는 사용자 활성 한도를 기다린다(5.11) |
| `active` | 감독이 단계를 진행 중 (`stage` 로 세부 단계를 안다) |
| `awaiting_approval` | Ask 계획 카드의 승인을 기다림. GPU 차례를 갖지 않으므로 활성 한도에 세지 않는다 |
| `done` | 결과를 채팅에 보냄 (결과 검사는 이 상태에서 이어서 돈다) |
| `failed` | 끝내 실패, 안내를 보냄 |
| `cancelled` | 사용자 취소, 대화 삭제, 계획 카드 만료·고쳐 쓰기, 관리자 취소 |
| `interrupted` | 허브 재시작으로 멈췄고 자동으로 이어 가지 않는 요청. 끝난 상태다. [다시 시도]·[다시 만들기]는 새 요청을 만든다 |

| stage | 화면 글 | 이 단계의 GPU 작업 |
|---|---|---|
| `understand` | 요청 이해 중 (P4. 칩은 '프롬프트 작성'에 묶인다, 8.4) | (P4) 27B 차례의 첫 호출. P1~P3 은 규칙이라 GPU 없이 바로 끝나 화면에 보이지 않는다 |
| `frame` | 첫 화면 읽는 중 | 8B `llm_turn` |
| `prompt` | 프롬프트 작성 중 | 27B `llm_turn` |
| `fix` | 프롬프트 다듬는 중 | 27B `llm_turn` (P1) 또는 같은 차례 안 (P3) |
| `plan` | 계획 확인 대기 | 없음 |
| `queue` | 대기 n번째 | `jobs` 의 `queued` |
| `prepare` | GPU 준비 중 | 스케줄러 `starting` (실행 공간 준비) |
| `generate` | 생성 n% | ComfyUI 실행 (`running`) |
| `collect` | 마무리 | 결과 확정 (`jobs` 가 `cleaning`·`finished`, 5.13) |
| `inspect` | (사용자에게 안 보임) | 8B `llm_turn`, 결과를 보낸 뒤 |

```
 메시지 ─▶ pending ──(같은 대화의 앞 요청이 끝남, 활성 한도 안)──▶ active
                                               understand → frame → prompt → fix
                                               → [plan ⇄ awaiting_approval] → queue → prepare → generate → collect
                                                                                                 │
          cancelled ◀── 취소 (어느 단계든) ───────┤                                               ▼
          failed    ◀── 복구 못 하는 오류·기한 ───┤                                      done ─▶ (inspect, 같은 done)
          interrupted ◀── 허브 재시작 뒤 이어 갈 수 없음 (5.10) ┘
```

| 지금 | 사건 | 다음 | 저장 (`chat_turns`) |
|---|---|---|---|
| (없음) | 사용자 메시지 | `pending` | `request`(글(앞뒤 공백을 지운 것, 10.1), 첨부 id, 글에서 뽑은 지정 5.3), `depends_on`(같은 대화의 마지막 열린 요청) |
| (없음) | [다시 만들기]·[다시 시도]·[이걸로 영상 만들기] (13.2) | `pending` | `retry_of` 또는 `request.source_turn`, `request.mode` |
| `pending` | 같은 대화의 앞 요청이 끝난 상태(`done`·`failed`·`cancelled`·`interrupted`)이고 `active` 요청 < `STUDIO_MAX_ACTIVE_PER_USER` | `active`/`understand` (다시 만들기면 `active`/`queue`, 원래 요청의 `work` 를 복사) | `started_at` |
| `understand` | 의도 확정. 이때 '직전 결과'를 정한다(5.8) | `frame` (첨부 + i2v), `prompt`. `carry=repeat`(R6b)면 `queue`(직전 요청의 `work.prompt`·`settings`·`size` 를 복사하고 시드만 새로, 5.8). `answer` 면 바로 `done` | `decision` |
| `understand` (P4) | 규칙으로 정하지 못했고 첨부가 있음 (5.3) | `frame` (의도를 정하기 전) | |
| `frame` (P4, 의도를 정하기 전) | 첫 화면 읽기 끝 | `understand` (이어지는 27B 차례의 첫 호출, 5.3) | `work.frame` |
| `understand` (P4, 27B 차례 안) | 의도 확정 | `prompt` (같은 차례). `answer` 면 `done` | `decision` |
| `frame` | 첫 화면 읽기 끝 | `prompt` | `work.frame` |
| `prompt` | 프롬프트 완성 | 고칠 위반이 남으면 `fix`, 아니면 `plan`·`queue` | `work.prompt`·`parts`·`ko`·`length_choice`·`profile`·`settings`·`crew`·`violations` |
| `fix` | 고치기 끝 | `plan` 또는 `queue` | `work.fix` (전·후 위반, 낱말 수) |
| `plan` | Ask 조건이 맞음 (5.7) | `awaiting_approval` | `plan` |
| `awaiting_approval` | 승인 | `active`/`queue` | |
| `awaiting_approval` | 같은 대화에 새 메시지 (5.7) | `cancelled` (새 요청이 계획을 다시 만든다) | |
| `awaiting_approval` | 취소, 또는 `STUDIO_PLAN_TTL_HOURS` 지남 | `cancelled` | |
| `queue` | 접수 직전 | `queue` | `work.pending` = {client_id, attempt}, `work.size`·`work.seed`. 재현 값의 정본은 `work` 이고 `chat_events.comfy_submitted` 는 사본이다 (5.10, 6.6) |
| `queue` | `submit_comfy` 성공 | `queue` (순번 표시) | `chat_turn_jobs(step='generate')` |
| `queue` | DB `starting` (또는 수신기의 `progress_text`) | `prepare` | `progress` |
| `prepare` | DB `running` (또는 `execution_start`) | `generate` | `progress` |
| `queue`·`prepare`·`generate` | DB `state` 가 `cleaning` (5.13) | `collect` (화면은 '마무리') | `progress` (성공 신호를 받았으면 100%) |
| `queue`·`prepare`·`generate` | DB `finished` (`cleaning` 을 폴링이 놓쳤거나, 사전 확인 거부·대기 중 취소처럼 `cleaning` 을 거치지 않음, `hub/app/scheduler.py:203`, `hub/app/jobs.py:311-326`) | `collect` 로 건너뛰고 바로 아래 `collect` 행을 따른다 | |
| `collect` | 결과를 고를 때가 됨: 이 작업의(prompt_id 가 같은) 성공 신호(`execution_success`, 또는 `executing` 의 `node: null`)를 받았고 DB 가 `cleaning`·`finished`, 또는 DB 가 `finished`+`success` (5.13) | `job_outputs` 에 레시피 main 출력이 있으면 `done` (결과 메시지), 없으면 `failed` | `result` 또는 `error` |
| `collect` | 성공 신호 없이 DB `finished`, outcome 이 `success` 가 아님 | `lost` 는 5.10 표(출력이 있으면 결과로 쓴다). `cancelled` 는 감독이 취소를 요청했으면 사용자 취소, 아니면 관리자 취소 안내(13.1) 뒤 `cancelled`. 그 밖(`error`·`timeout`·`rejected`)은 `failed` (안내는 13.1 의 outcome 가르기). main 출력이 있어도 결과를 내지 않는다 | `error` |
| `done` | 결과를 이미 보냈는데 나중 outcome 이 `success` 가 아님 | `done` 그대로, 기록만 (5.13 의 8) | `chat_events` |
| `done` | 결과 검사 끝 | `done` (문제가 있으면 안내 메시지) | `result.inspection` |
| 활성 상태 아무 데나 | 사용자 취소, 대화 삭제 | `cancelled` | 5.9 |
| 활성 상태 아무 데나 | 단계 기한 넘김 (5.12. 결과 확정 기한은 실패로 끝내지 않고 진행 카드 글만 바꾼다) | `failed` | `error` |
| 활성 상태 아무 데나 | 복구 못 하는 오류 | `failed` | `error` |

- 상태 바꾸기는 모두 조건부다: `UPDATE chat_turns SET state=$3, … WHERE id=$1 AND state=$2 RETURNING id`. 0줄이면 다른 길(취소·복구·두 번 누른 버튼)이 먼저 바꾼 것이므로 아무것도 하지 않는다.
- 규칙으로 정하면 `understand` 는 GPU 없이 바로 끝난다(P1~P3).
- P4 에서 첨부가 있고 규칙으로 못 정하면 `frame`(8B)을 먼저 하고(의도를 정하기 전), `understand` 는 이어지는 27B 차례의 첫 호출로 한다(5.3, 위 표의 P4 행). 화면 칩은 '첫 화면 읽기 → 프롬프트 작성'이고, 프롬프트 작성 칩 안에서 글이 '요청 이해 중' → '프롬프트 작성 중'으로 바뀐다(8.4).
- DB 상태가 단계보다 앞서 있으면(예: `queue` 인데 DB 가 `running`·`cleaning`·`finished`) 바로 그 단계로 건너뛴다. 따뜻한 실행 공간이면 `starting` 이 폴링 간격보다 짧을 수 있다(`hub/app/scheduler.py:277-278`).
- 스케줄러는 실행을 시작한 작업이면 결과와 상관없이 `cleaning` 을 먼저 쓰고, outcome 은 정리가 끝난 뒤 `finished` 와 함께 쓴다(`hub/app/scheduler.py:224-231`, `245-250`). 그래서 `cleaning` 만으로는 성공인지 모른다. 성공 신호가 없으면 화면은 '마무리'로 두고 outcome 을 기다린다(결과 확정 기한은 5.12).
- outcome 이 `error`·`timeout`·`cancelled` 이면 main 출력이 남아 있어도 결과를 내지 않는다. 생성이 끝까지 돌지 않았을 수 있다. 남은 출력을 결과로 쓰는 것은 `lost` 의 복구(5.10)뿐이다.

### 5.3 의도 판단

결과는 세 값이다.

- `action`: `image` | `i2v` | `t2v` | `edit` | `answer`
- `source`: `upload` (이번에 올린 이미지) | `last_result` (직전 결과) | `none`
- `carry`: `new` | `revise` (직전 요청을 고침) | `repeat` (같은 프롬프트로 다시 만들기)

판단 순서는 **규칙 → 애매할 때만 LLM → 실패하면 규칙 기본값**이다. `decide_length` 와 같은 구조다(`hub/app/video.py:196-224`). 사용자에게 되묻지 않는다. 러다이트도 질문 없이 진행했다(DFC/02:106). 판단은 같은 대화의 앞 요청이 끝난 뒤에 한다(5.11). 그래서 '직전 결과'(5.8)가 늘 정해져 있다.

| 순서 | 조건 | 판단 | 단계 |
|---|---|---|---|
| R0 | 글이 비고(공백·줄바꿈만 있어도 빈 글이다: `text.strip() == ""`) 첨부만 있음 | 글을 기본 요청문 "이 사진 속 장면이 부드럽게 움직이는 짧은 영상"(코드 상수 `DEFAULT_I2V_TEXT`)으로 바꾸고 아래 규칙을 이어 본다 → R1·R3 으로 `i2v`. 이 문장은 제작진 분야 키워드에 걸리지 않게 골랐다(`promptx.crew_route` 가 None. '자연스럽게'는 풍경 키워드 '자연'에 걸린다, `hub/app/crew_knowledge.json:267`, `hub/app/crew_lib.py:31`). 기본 요청문을 쓴 요청은 렌더러에 `crew=False` 를 넘긴다(5.5). 말풍선에는 사진만 보인다. 글도 첨부도 없으면 API 가 400 `empty_message` 다(10.1) | P1 |
| R1 | P1 동안 | 첨부가 있으면 `i2v`·`upload`. 없으면 "사진을 함께 올려 주세요" 안내(LLM 안 씀) | P1 |
| R2 | 만들 뜻이 없는 말: 인사·감사·도움말 낱말("안녕", "고마워", "감사", "도움말", "뭐 할 수 있어"), 또는 질문·보기 요청("?", "왜", "어떻게", "뭐야", "보여줘", "알려줘")인데 영상 말·편집 말·만들기 말("만들어", "그려", "바꿔")이 없음 | `answer`, 정해 둔 짧은 답 (GPU 안 씀, 13.1) | P2 |
| R2b | 첨부 + 참고 말("그림체", "스타일", "참고", "이런 느낌", "처럼") + 영상 말·편집 말 없음 | `image`·`none`. 글만으로 그리고 "참고 이미지는 아직 쓰지 못해 글만 보고 그렸습니다" 한 줄 (5.4) | P2 |
| R3 | 첨부 이미지 + 영상 말 | `i2v`·`upload` | P2 |
| R4 | 첨부 이미지 + 편집 말 + 영상 말 없음 | `edit`·`upload` | P3 |
| R5 | 첨부 없음 + 영상 말 + 직전 결과가 이미지 + 가리키는 말("이걸로", "이거", "방금", "그걸로") | `i2v`·`last_result` ([이걸로 영상 만들기]와 같은 흐름) | P2 |
| R6b | 첨부 없음 + 직전 결과 있음 + 바꿀 점 없는 반복 말("하나 더", "한 번 더", "다시 만들어") | 직전과 같은 action, `repeat` (다시 만들기와 같다, LLM 안 씀) | P2 |
| R6 | 첨부 없음 + 직전 결과가 영상 + 고치는 말("더", "덜", "좀 더", "다시", "~하게 바꿔") | 직전과 같은 `i2v`/`t2v`, `revise` | P3 |
| R6a | 첨부 없음 + 직전 결과가 이미지 + 고치는 말 + 편집 말 없음 | `image`, `revise` (원래 요청 + 이번 말로 다시 확장, 새 시드, 5.8) | P2 |
| R7 | 첨부 없음 + 직전 결과가 이미지 + 편집 말 | `edit`·`last_result` | P3 |
| R7b | 첨부 없음 + 직전 결과가 영상 + 편집 말 | 직전이 `t2v` 면 `t2v`·`revise`(바꿀 점을 장면에 반영, 5.8). 직전이 `i2v` 면 `answer` 한 줄 "영상의 배경과 모습은 첫 화면 사진에서 정해집니다. 사진을 먼저 편집한 뒤 영상으로 만들어 주세요."(LLM 안 씀). 첫 화면을 편집한 뒤 다시 만드는 흐름(edit → i2v)은 P4 이후 [TBD] | P3 |
| R8 | 첨부 없음 + 영상 말 | `t2v` | P3 |
| R9 | 첨부 없음 + 영상 말·편집 말 없음 | `image` | P2 |
| R10 | 어느 규칙에도 확실히 맞지 않음 (예: 첨부와 모호한 말) | P4: LLM. P2·P3: 첨부가 있으면 `i2v`, 없으면 `image` (이 기본값은 D19 에서 확인) | P2 (기본값), P4 (LLM) |

- 위에서부터 처음 맞는 규칙을 쓴다. R6b 는 "하나 더"의 "더"가 R6 에 걸리지 않게 R6 앞에 둔다. R2b 는 편집 말이 있으면 맞지 않으므로, 사진과 "이 사진을 수채화 스타일로 바꿔줘"는 R4(편집)로 간다.
- 단계 칸이 지금보다 나중인 규칙에 맞으면 action 을 정하고 "이 기능은 지금 준비 중입니다"(13.1)로 끝낸다(GPU 안 씀). 건너뛰고 아래 규칙으로 가지 않는다. 예: P2 에서 이미지 결과 뒤 "배경만 노을로 바꿔줘"(R7), 첨부 없는 "바다 위를 나는 드론 영상"(R8). R1 만은 P1 에서만 쓰는 규칙이라 P2 부터 건너뛴다. 정한 action 의 레시피가 꺼져 있을 때도 같은 안내다.
- 영상 말은 다음을 합친다.
  - 낱말: 영상, 동영상, 움직이게, 움직이는, 애니메이션, 클립 [추정: 첫 시험 뒤 보탠다].
  - 카메라·속도·루프 의도 15개: `crew_lib.query(text).video_demands` (`hub/app/crew_lib.py:331-333`, `docs/video-renderer.md:67-74`).
  - 1인칭: `video.pov_wanted` (`hub/app/video.py:286-288`).
- 편집 말: 바꿔, 바꾸, 지워, 없애, 빼, 추가해, 넣어, 고쳐, 편집, 배경만, 색만 [추정: 첫 시험 뒤 고친다].
- 진행 카드의 제목("영상 만드는 중", "이미지 만드는 중")이 판단 결과를 알려 준다. 판단을 따로 말하지 않는다(G3). 제목 목록은 8.4 다.

글에 든 길이·비율·소리 지정 (규칙, LLM 안 씀. 뽑는 코드는 P1 부터 `intent.py` 에 두고, 지정마다 쓰는 단계는 표의 칸이다). 말없이 무시하지 않고 반영하거나 한 줄로 알린다(13.1):

| 지정 | 뽑는 법 | 처리 | 단계 |
|---|---|---|---|
| 길이 | `(\d+(?:\.\d+)?)\s*초` | 0.5~5초면 렌더러에 `seconds` 로 넘긴다(프레임은 4n+1, `hub/app/video.py:249-261`). 5초를 넘으면 5초로 만들고 결과 뒤에 "한 번에 5초까지라 5초로 만들었습니다". 지정이 없으면 프로필 기본 5초다(`hub/app/video.py:255`) | P1 (`t2v` 는 P3) |
| 비율 | "세로", "가로", "정사각", "9:16", "16:9", "1:1" 등 | `image`·`t2v` 는 그 비율로 크기를 정한다(6.6). `i2v`·`edit` 는 사진 비율을 따르고, 지정과 다르면 결과 뒤에 "영상 비율은 사진 비율을 따릅니다" | `i2v` 안내 P1, `image` 크기 P2, `t2v` 크기·`edit` 안내 P3 |
| 소리 | "소리", "음악", "효과음", "사운드", "BGM" | 소리 없이 만들고 결과 뒤에 "소리는 아직 넣지 못합니다". 렌더러 v1 은 사운드를 다루지 않고(`docs/video-renderer.md:21`), Wan 레시피의 `CreateVideo` 는 audio 입력이 비어 있어 소리 없는 영상을 낸다([TPL] `templates/video_wan2_2_14B_i2v.json:704-707`) | P1 |

- 렌더러의 길이 상한 안내(`capped`)는 "클립을 나눠 이어 붙여야 한다"는 패널 문구라 쓰지 않는다(`hub/app/video.py:846-848`). 위 표의 문구로 바꾼다.

LLM 판단 (P4):

- 27B 프롬프트 작성 차례의 **첫 호출**로 한다. 같은 모델이라 GPU 차례가 늘지 않는다(`hub/app/llm.py:249-250`).
- 첨부가 있고 판단이 애매하면 8B 첫 화면 읽기를 먼저 하고, 그 요약(대상·배경)을 판단 입력에 넣는다.
- 첨부가 없는데 판단이 `i2v`·`last_result` 이고 그 결과의 `meta.frame` 이 없으면, 27B 차례를 닫고 8B 첫 화면 차례를 연 뒤 27B 차례를 다시 연다. 차례 안에서 다른 차례를 열 수 없다(5.6 규칙 5).
- 설정은 `format`=아래 스키마, `think: False`, `temperature` 0.2 다. `decide_length` 와 같다(`hub/app/video.py:214-216`).
- 입력: 이번 말, 첨부 요약(장수·크기·첫 화면 요약), 대화 맥락(5.8), 규칙.
- 답은 믿지 않는 데이터로 다룬다(14.3). 스키마와 어긋나면(예: 첨부가 없는데 `source=upload`) 규칙으로 고친다. 직전 결과가 영상인데 `edit`·`last_result` 이면 R7b 로 고친다(편집 레시피는 이미지만 받는다). JSON 이 깨지면 R10 기본값을 쓴다. 글 칸은 서버에서 길이를 다시 자른다.

```json
{
  "type": "object",
  "required": ["action", "source", "carry", "attachment_role", "request_ko", "change_ko", "title_ko"],
  "additionalProperties": false,
  "properties": {
    "action":          {"type": "string", "enum": ["image", "i2v", "t2v", "edit", "answer"]},
    "source":          {"type": "string", "enum": ["upload", "last_result", "none"]},
    "carry":           {"type": "string", "enum": ["new", "revise", "repeat"]},
    "attachment_role": {"type": "string", "enum": ["first_frame", "edit_target", "reference", "none"]},
    "seconds":         {"type": ["number", "null"], "description": "글에 적힌 길이(초), 없으면 null"},
    "aspect":          {"type": "string", "enum": ["auto", "portrait", "landscape", "square"]},
    "request_ko":      {"type": "string", "description": "이번에 만들 것을 맥락까지 합쳐 한국어 한 문장으로"},
    "change_ko":       {"type": "string", "description": "revise 일 때 직전 결과에서 바꿀 점, 아니면 빈 글"},
    "title_ko":        {"type": "string", "description": "대화 제목 후보, 15자 이내"},
    "reply_ko":        {"type": "string", "description": "action 이 answer 일 때만 보일 한두 문장"}
  }
}
```

### 5.4 첨부 이미지 판단

| 역할 | 뜻 | 정하는 법 | 레시피 |
|---|---|---|---|
| `first_frame` | I2V 의 시작 화면 | R1·R3·R5 (R0 는 글만 채우고 R1·R3 으로 이어진다), 또는 LLM `attachment_role=first_frame` | 첫 화면 자리(`inject.first_frame`) |
| `edit_target` | 편집 대상 | R4·R7, 또는 LLM `attachment_role=edit_target` | 편집 레시피의 입력 이미지 자리(`inject.edit_image`, P3) |
| `reference` | 화풍·구도 참고 | R2b, 또는 LLM `attachment_role=reference` | 레퍼런스 레시피가 없어 P4 이후 [TBD]. 지금은 글만으로 이미지를 만들고 "참고 이미지는 아직 쓰지 못해 글만 보고 그렸습니다" 한 줄 |

- 화면은 메시지당 이미지 한 장만 받는다. API 는 두 장 이상이면 400 `too_many_attachments` 다(10.1). 여러 장 레시피가 없다.
- 첫 화면 비율은 출력 크기를 정할 때 쓴다(6.6). Wan 은 첫 화면을 출력 크기에 맞춰 가운데를 자르기 때문이다([CUI] `comfy_extras/nodes_wan.py:47`).

### 5.5 프롬프트 만들기 (기존 함수 연결)

| action | 단계 | 부르는 함수 | 레시피로 가는 값 |
|---|---|---|---|
| `i2v` (P1) | 첫 화면 + 프롬프트 + 고치기 | `video.render(..., auto_length=True)` → 낱말 수가 아닌 위반이 남으면 `video.fix(...)` (5.12) | `prompt` = 고쳤으면 `fix` 결과의 `prompt`, 아니면 `render` 의 `prompt`. `frames` = `settings.frames` |
| `i2v` (P3) | 같음, 한 차례로 | `video.render_in_turn(call, ...)` + `video.fix_in_turn(call, ...)` (5.6 의 꺼내기) | 같음 |
| `image` (P2) | 의도 + 제작진 + 확장 | 감독이 연 `llm_turn` 안에서 `promptx.expand_in_turn(call, text, "", "", None)` (그림체 칸은 비운다, 아래). 부르기 전에 감독이 `promptx.clean_input`(빈 글·2000자)을 한다. `promptx.expand` 가 하던 일인데(`hub/app/promptx.py:487-494`) `expand_in_turn` 에는 없다(`hub/app/promptx.py:444-484`) | `prompt` = 결과 `prompt` (`hub/app/promptx.py:224-226`) |
| `image` revise (P2) | 같음 | 글 = 원래 요청 + "\n" + 이번 말 (5.8) | 같음, 새 시드 |
| `t2v` (P3) | 의도 + 장면 + 길이 + 초안 + 고치기 | `video.render_in_turn(call, ...)` T2V 경로 (안에서 `expand_in_turn` 으로 장면, `hub/app/video.py:881-885`) | `prompt` = `compose` 결과 (`hub/app/video.py:775-787`) |
| `edit` (P3) | 의도 + 편집 지시문 | 새 함수 `studio.edit.write_edit(call, text, frame)` (아래). `frame` 은 편집 대상 첨부의 `meta.frame`(5.6 의 선택 항목으로 이미지 검사 때 읽어 둔 것)이 있으면 쓰고 없으면 `None` 이다. 편집에는 8B 차례를 따로 열지 않는다 | `prompt` = 영문 편집 지시문 |
| `answer` | 없음 | 정해 둔 글 (P4 는 LLM 판단의 `reply_ko`) | 레시피 안 씀 |

P1 의 실제 호출 (패널과 같은 인자 구성, `hub/app/panel.py:255-259`, `runtime/nodes/mgs_bridge/web/mgs_panel.js:665-670`):

```python
env = await store.studio_env()        # envs.get_env(<STUDIO_ENV_SLUG 의 id>), hub/app/envs.py:50-54. 없으면 채팅이 꺼져 여기 오지 않는다 (5.10)
image = {"filename": "<첨부 uuid>.png", "subfolder": "studio/<사용자 폴더>", "type": "input"}
used_default = not (request_text or "").strip()                 # 공백·줄바꿈만 있어도 빈 글 (5.3 R0)
text = DEFAULT_I2V_TEXT if used_default else request_text.strip()
bag: list = []
t_jobs, t_wait = llm.STUDIO_JOBS.set(bag), llm.STUDIO_WAIT.set(STUDIO_LLM_HARD_WAIT)   # 9.2, 5.6
try:
    r = await video.render(user, ip, env["username"], env["slug"], text,
                           notes="", style="", profile_id=recipe["video_profile"],            # wan22_i2v_long
                           seconds=spec_seconds, image=image, image_prompt="",                # 글의 길이 지정, 없으면 None
                           crew=False if used_default else None,                              # 기본 요청문이면 제작진 생략
                           read_frame_on=True, auto_length=True)
    f = None
    if needs_fix(r["violations"]):          # 낱말 수 위반만 남았으면 False (5.12)
        f = await video.fix(user, ip, env["username"], env["slug"], text, notes="", style="",
                            profile_id=r["profile"], seconds=r["settings"]["seconds"],
                            current={"ko": r["ko"], "parts": r["parts"], "motion": r["motion"]},
                            frame=r["frame"], scene="")
finally:
    llm.STUDIO_WAIT.reset(t_wait)
    llm.STUDIO_JOBS.reset(t_jobs)
final_prompt = f["prompt"] if f and f["changed"] else r["prompt"]
```

- `user` 는 채팅 사용자다. GPU 차례와 `llm_calls` 기록이 그 사용자 이름으로 남는다.
- `style` 은 늘 빈 글이다. 대화별 그림체 설정은 두지 않는다(2.1 의 설정은 하나뿐이다, G4). 사용자가 글에 쓴 화풍 말은 요청 글 그대로 모델에 들어간다(`hub/app/video.py:507`).
- `env["username"]`·`env["slug"]` 는 채팅 작업공간의 주인(`studio`)과 slug(`studio-chat`)다. 첫 화면을 그 입력 폴더에서 찾는다(`hub/app/video.py:389-404`). 패널도 파일 위치는 작업공간 주인 기준, 차례는 로그인 사용자 기준으로 나눈다(`hub/app/panel.py:254-256`).
- `<사용자 폴더>` 는 사용자 이름이 `^[a-z][a-z0-9_-]{1,30}$` 에 맞으면 그 이름, 아니면 `u<사용자 id>` 다(6.6).
- `STUDIO_LLM_HARD_WAIT` 는 `llm_turn` 이 스스로 끊지 않게 주는 아주 긴 값(코드 상수 86400초)이다. 언제 끊을지는 감독의 대기 기한이 정한다(5.6, 5.12).
- 기본 요청문을 쓴 요청은 `crew=False` 로 제작진을 쓰지 않고, 기록(`chat_events.render`)에 '기본 요청문이라 제작진 생략'을 남긴다. 기본 요청문은 사진 내용을 모르는 문장이라 분야를 정할 수 없다(제품·인물 사진도 한 분야 메뉴로 쓰게 된다). 그 밖에는 `crew=None` 이다. `crew=None` 이면 `CREW_MODE` 를 따른다. 제품·공간·풍경이면 영상 제작진 메뉴를 쓴다. 캐릭터 정본 이름, 손 숨김 요청, 사람이 나오는 요청이면 조용히 건너뛰고 이유를 `advice` 에 남긴다(`hub/app/crew.py:67-76`, `85-91`, `hub/app/promptx.py:423-441`, `hub/app/config.py:61-65`).
- `auto_length=True` 이면 장문·보통을 허브가 정한다(`hub/app/video.py:877-880`). 이유는 `length_choice` 에 남는다(`hub/app/video.py:913`).
- `profile_id` 를 비우면 첫 프로필이 쓰이지만(`hub/app/video.py:236`) 레시피가 명시한다.
- 첫 화면을 못 읽으면 렌더러는 글만 보고 쓰고, 장문이면 움직임만 프로필로 바꾼다(`hub/app/video.py:851-856`, `872-876`). 감독은 이것을 `advice` 문장으로 판단하지 않는다. `advice` 는 코드 없는 한국어 문장 목록이라 문자열 비교에 기대게 되기 때문이다. 대신 첨부가 있었는데 `not video.frame_known(r["frame"])` 이면 '사진을 읽지 못함'으로 본다(`hub/app/video.py:227-229`). 시각 모델이 JSON 은 냈지만 대상·배경이 빈 경우도 여기에 든다. 렌더러가 움직임만 프로필로 바꾸는 조건(`hub/app/video.py:872`)과 같다. 결과 모습이 달라질 수 있는 정보라 사용자에게 한 줄로 보인다(13.1). 나머지 `advice` 는 운영자 기록에만 남긴다(5.12).

긍정·부정 프롬프트:

- 프롬프트는 레시피의 `inject.prompt` 자리에만 넣는다. 그 자리가 샘플러의 positive 로 이어지는지는 등록 때 검증한다(6.4 V4).
- 부정 쪽은 레시피 구성(`negative_source`, 6.2)에 따라 다르다. 채팅이 부정 프롬프트를 쓰는 일은 없다.
  - `node`: 부정 글 노드가 따로 있다(Wan, Qwen 편집). 레시피에 적힌 값(`fixed.negative`) 그대로 둔다.
  - `zero_out`: 부정 글 노드가 없다. Z-Image-Turbo 는 글 노드 하나의 출력을 positive 로 보내고, 같은 출력을 0 조건 노드 `ConditioningZeroOut` 으로 지워 negative 로 보낸다([TPL] `templates/image_z_image_turbo.json:436`, `594`, `764`, 링크 `898`·`938`·`946`).
  - 4스텝 LoRA 구성과 Z-Image-Turbo 는 cfg 1 이라 ComfyUI 가 부정 쪽 계산을 건너뛴다([CUI] `comfy/samplers.py:610-611`). 이 구성에서 부정 프롬프트를 고쳐도 결과가 바뀌지 않는다.
- 접수 직전에 확인한다. 패널의 부정 노드 감시(`runtime/nodes/mgs_bridge/web/mgs_nodes.js:200-228`)가 하던 일을 화면 없이 하는 것이다. 맞지 않으면 접수하지 않는다.
  - `node`: 만든 prompt 에서 부정 자리의 글이 레시피 값과 같고, 긍정 자리 글과 다르다.
  - `zero_out`: 0 조건 노드가 그대로 있고 그 입력이 긍정 자리 노드다.

편집 지시문 (P3, 새 함수 `hub/app/studio/edit.py`):

- 출력: 영문 지시문 한두 문장 (예: "Replace the background with a warm sunset sky and keep the person, pose and lighting on the person unchanged").
- 검사(기존 정규식을 그대로 쓴다): 영문만(`video.HANGUL_RE`, `hub/app/video.py:38`), 부정어 없음(`promptx.NEGATION_RE`, `hub/app/promptx.py:42`), 글자·로고 말 없음(`crew.TEXT_NOTE_RE`, `hub/app/crew.py:47`), 8~60낱말 [추정].
- 걸리면 한 번 고쳐 쓴다. 편집용 규칙의 품질은 [TBD]다(`hub/app/promptx.py:506-514` 의 안내 문구만 있고 편집 규칙은 없다).

### 5.6 LLM 차례 배치 (GPU 한 장에서 LLM 과 ComfyUI 번갈아 쓰기)

알고 있는 사실:

- LLM 차례가 시작될 때 켜져 있던 실행 공간을 끈다(`hub/app/scheduler.py:511-512`). 그래서 그다음 ComfyUI 작업은 실행 공간을 새로 띄운다(`hub/app/scheduler.py:274-307`, 걸린 시간은 `cold_start_ms`).
- LLM 차례가 끝나면 모델을 내린다(`hub/app/llm.py:308-311`). 차례마다 모델을 다시 올린다.
- 한 차례 안의 여러 호출은 모델을 다시 올리지 않는다(`hub/app/llm.py:249-250`, `279`). 한 차례에는 모델 하나만 올린다(`compose.yaml:32`, `hub/app/video.py:865-871`).
- ComfyUI 작업이 끝나면 `/free` 로 모델을 내린다(`hub/app/scheduler.py:595`). 같은 환경 버전이면 실행 공간은 다시 쓴다(`hub/app/scheduler.py:277-278`).
- 단계마다 순번을 새로 받으므로 단계 사이에 다른 작업이 끼어든다(`hub/app/jobs.py:46-58`, `hub/app/scheduler.py:169`).

배치 규칙:

1. 같은 모델의 LLM 단계는 한 차례로 묶는다. 의도 판단, 길이 판단, 초안, 고치기가 모두 27B 이므로 한 차례다(P3 부터).
2. 모델이 다르면 차례를 나눈다. 8B 시각(첫 화면 읽기·검사)과 27B 확장은 다른 차례다.
3. LLM 단계는 ComfyUI 앞에 모은다. 생성 뒤의 LLM 차례는 결과 검사 하나뿐이다.
4. 결과 검사는 결과를 채팅에 보낸 **뒤에** 한다. 사용자는 검사를 기다리지 않는다.
5. 차례 안에서는 다른 `llm_turn` 을 열지 않고, `submit_comfy` 결과나 다른 GPU 줄을 기다리지 않는다. 열린 차례는 GPU 를 쥐고 있고 스케줄러는 차례가 닫히기를 기다리므로(`hub/app/llm.py:302-321`, `hub/app/scheduler.py:157-159`, `519-520`) 자기 차례를 자기가 기다리는 교착이 된다. 내부 호출 예외가 막으려는 교착과 같은 것이다(`CLAUDE.md:20-21`). 코드로 막는다: `llm_turn` 이 컨텍스트 변수 `IN_TURN` 을 세우고, `llm_turn` 과 감독의 접수 함수는 그것이 켜져 있으면 전용 예외 `NestedTurnError` 를 낸다(9.2).

흐름별 대기열 줄 (한 칸 = `jobs` 한 줄 = GPU 차례 하나):

| 흐름 | 순서 | 차례 수 | 실행 공간 |
|---|---|---|---|
| I2V, P1 (기존 함수 그대로) | ① 8B `video_frame` → ② 27B `video_expand` → ③ 낱말 수가 아닌 위반이 남으면 27B `video_fix` → ④ ComfyUI Wan I2V → ⑤ 8B `image_inspect` (전달 뒤, D15) | 3~5 | ①에서 꺼짐 → ④ 새로 띄움 → ⑤에서 다시 꺼짐 |
| I2V, P3 (묶음) | ① 8B 첫 화면 읽기 → ② 27B 길이 + 초안 + 고치기 → ③ ComfyUI → ④ 8B 검사 | 3~4 | 같음 |
| 이미지 생성, P2 | ① 27B (P4 부터 의도 +) 제작진 + 확장 → ② ComfyUI Z-Image → ③ 8B 검사 | 2~3 | ①에서 꺼짐 → ② 새로 띄움 → ③ 꺼짐 |
| 이미지 고치기 (revise), P2 | ① 27B 다시 확장 → ② ComfyUI → ③ 8B 검사 | 2~3 | 같음 |
| T2V, P3 | ① 27B 장면 + 길이 + 초안 + 고치기 → ② ComfyUI Wan T2V → ③ 8B 검사 | 2~3 | 같음 |
| 편집, P3 | ① 27B 편집 지시문 → ② ComfyUI 편집 → ③ 8B 검사 | 2~3 | 같음 |
| 영상 고치기 (revise), P3 | ① 27B 고쳐 쓰기 (첫 화면 읽기 다시 씀) → ② ComfyUI → ③ 8B 검사 | 2~3 | 같음 |
| 다시 만들기 (시드만 바꿈) | ① ComfyUI → ② 8B 검사 | 1~2 | 앞 요청의 검사 차례가 이미 실행 공간을 껐으므로 보통 새로 띄운다. 검사가 꺼져 있고 그 사이 LLM 작업이 없을 때만 다시 쓴다(`hub/app/scheduler.py:277-278`, `511-512`) |
| P4 의 '27B 판단 → 8B 필요' | ① 27B 판단 (닫음) → ② 8B 첫 화면 → ③ 27B 프롬프트 → ④ ComfyUI → ⑤ 8B 검사 | 하나 늘어남 | 같음 |

- P3 의 한 차례 묶기는 `video.render` 본문 중 차례 안 부분(`hub/app/video.py:869-899`)을 `render_in_turn(call, ...)` 으로, `video.fix` 의 차례 안 부분(`hub/app/video.py:951-953`)을 `fix_in_turn(call, ...)` 으로 꺼내 만든다. `render`·`fix` 는 이 함수들을 감싸기만 하므로 패널 API(`hub/app/panel.py:249-279`)는 그대로다.
- LLM 차례의 대기 한도 (P1 부터):
  - 지금 진입 함수들은 `llm_turn` 의 `wait_seconds` 를 넘기지 않아 900초가 한도다(`hub/app/video.py:867`, `869`, `951`, `hub/app/llm.py:261`, `hub/app/config.py:57`). 900초가 지나면 대기 줄을 취소해 순번을 잃는다(`hub/app/llm.py:262-268`).
  - `update.sh` 는 대기열을 최대 120분 멈춘다(`install/update.sh:16`, `109`, `118-127`). 앞에 15분 넘는 작업이 있거나 대기열이 정지(BLOCKED)돼도 900초를 넘는다. 그대로면 13절의 '점검 중에는 실패가 아니라 기다림'을 지킬 수 없다. P1·P2 의 I2V 는 `video.render` 를 그대로 부르므로(P3 의 `render_in_turn` 전까지) 감독이 차례를 직접 여는 것만으로는 풀리지 않는다.
  - `update.sh` 는 정지한 채로 새로 빌드한 허브 이미지로 컨테이너를 다시 만들고(`install/update.sh:95`, `157`, `hub/Dockerfile:9`), 스크립트가 끝날 때 정지를 되돌린다(`install/update.sh:110-114`). 그래서 업데이트 끝에는 허브 재시작(5.10)이 따른다. P1 에서는 이때 LLM 단계에 있던 요청이 `interrupted` 가 된다. 아래의 기다림으로 풀리는 것은 정지 시간 동안뿐이다.
  - 그래서 `llm.py` 에 컨텍스트 변수 `STUDIO_WAIT` 를 더한다. `llm_turn` 은 `wait_seconds or STUDIO_WAIT.get() or settings.helper_wait_seconds` 를 쓴다. 패널은 값이 없어 동작이 같다(9.2).
  - 감독은 아주 긴 값(`STUDIO_LLM_HARD_WAIT`)을 주고 언제 끊을지는 스스로 정한다. 대기 시간을 세되 DB `settings` 표의 `paused`·`blocked`(`db.get_setting("paused")`·`db.get_setting("blocked")`, `hub/app/db.py:72-74`)가 켜진 동안은 세지 않는다. 스케줄러도 같은 값으로 멈춘다(`hub/app/scheduler.py:160-167`). 센 시간이 `STUDIO_LLM_WAIT_SECONDS`(기본 3600 [추정])를 넘거나 사용자가 [취소]하면 감독 작업을 한 번 취소하고 그 이유(`deadline`·`user`)를 들고 있는다(5.9, 5.12, 9.2).
  - 시간 초과 뒤 자동으로 다시 줄 서지 않는다. 다시 서면 맨 뒤로 가서, 대기열이 계속 길면 끝내 통과하지 못한다. 이 대기 정책은 D14 다.
- (P3 선택) 이미지 결과의 검사 차례(8B)에서 `video.read_frame` 도 함께 부른다. 그 결과를 첨부의 `meta.frame` 에 저장해 두면 "이걸로 영상 만들어줘"에서 8B 차례를 아낄 수 있다. 8B 호출이 한 번 늘어난다. `promptx.inspect` 는 차례를 안에서 열므로(`hub/app/promptx.py:534`) 차례 안 부분을 꺼내는 `promptx.py` 수정이 필요하다(19.2, D22). 검사 모델과 첫 화면 읽기 모델이 같을 때만 한 차례에 묶인다(기본은 같다, `hub/app/config.py:56`, `69`).
- 쓰지 않는 방법: LLM 을 ComfyUI 작업 안(내부 호출)에 넣어 차례를 줄이는 것. 4절의 이유로 쓰지 않는다.

시간 [추정, 대기열이 비어 있을 때]:

| 단계 | 시간 | 근거 |
|---|---|---|
| 27B 짧은 이미지 프롬프트 차례 | 11.3~23.0초 | 메인 실측 6건 중 값이 적힌 5건, 11-11 개편 전 (`docs/crew-mode.md:167`) |
| 27B 장문 영상 프롬프트 차례 | [TBD] (위보다 길다고 본다 [추정]) | 출력이 약 450낱말 ≈ 600토큰 (`docs/video-renderer.md:239`). 메인 실측 없음 |
| 27B 자동 고치기 차례 | [TBD] | 최대 3회 호출 (`hub/app/video.py:953`) |
| 8B 첫 화면 읽기, 8B 검사 | [TBD] | 실측 없음 |
| 실행 공간 새로 띄우기 | 10~40초 [추정] | `docs/main-gpu-server-design.html:422` |
| 14B 영상 모델 다시 읽기 | 10~25초 [추정] | `docs/main-gpu-server-design.html:423` |
| Wan 2.2 I2V 14B, 640×640, 81프레임, 4스텝 LoRA | 약 1분 반 | 지난 영상 시험 (`docs/video-renderer.md:116`) |
| Z-Image-Turbo 한 장 | 1분 안쪽 [추정] | 검수 안내 문구 (`docs/checklist-2026-10-27.md:24`) |
| Qwen-Image-Edit 4스텝 한 장 | [TBD] | 실측 없음 |

흐름별 결과까지 시간과 GPU 점유 [모두 추정]. 위 표의 [TBD] 칸은 이렇게 가정했다: 8B 차례 10~40초, 27B 장문 영상 차례 30~90초(P3 묶음은 고치기 포함 30~150초), 27B 고치기 차례 20~90초, 편집 지시문 차례 10~30초, 편집 생성 30~90초, 차례마다 정리(모델 내리기·메모리 확인) 5~20초. 1분 반 생성 시간에 모델 적재가 들어 있는지는 모른다 [TBD]. P1 측정(17.3) 뒤 실측으로 바꾼다.

| 흐름 | 사용자가 기다리는 시간 (대기열 빔) | GPU 를 쥐는 시간 (정리·검사 포함) | 콜드 스타트 | 채팅만 있을 때 시간당 처리 |
|---|---|---|---|---|
| I2V, P1 | 약 2분 30초~7분 | 약 3~8분 | 결과 전 1번 (+ 검사 뒤 다음 ComfyUI 작업 1번) | 약 7~20건 |
| I2V, P3 묶음 | 약 2분 30초~6분 | 약 3~7분 | 같음 | 약 8~20건 |
| 이미지 생성, P2 | 약 45초~2분 30초 | 약 1~4분 | 같음 | 약 15~50건 |
| T2V, P3 | 약 2분 30초~5분 | 약 3~6분 30초 | 같음 | 약 9~20건 |
| 편집, P3 | 약 1~3분 | 약 1분 30초~4분 30초 | 같음 | 약 13~40건 |
| 다시 만들기 (영상) | 약 1분 40초~2분 10초 | 약 2~3분 30초 | 같음 | 약 17~30건 |

- 계산: 사용자가 기다리는 시간 = 결과 전 차례 시간과 정리의 합 + 콜드 스타트(10~40초) + 생성. GPU 를 쥐는 시간 = 여기에 생성 뒤 정리와 결과 검사 차례를 더한 것. 시간당 처리 = 3600초 ÷ GPU 를 쥐는 시간.
- 여러 사람이 동시에 보내면 뒤 사람은 앞 요청들의 GPU 시간 합만큼 더 기다린다. 예: I2V 요청 3건이 앞에 있으면 약 9~24분 [추정]. 대기 순번을 단계마다 보이는 이유다(8.4).
- 실측 방법: `llm_calls.duration_ms`(엔드포인트 `helper:<purpose>`, `hub/app/llm.py:316-317`)와 `jobs` 의 `wait_ms`·`cold_start_ms`·`run_ms`·`cleanup_ms`(`hub/app/schema.sql:106-110`)를 `chat_turn_jobs` 로 묶어 읽는다(17.3).
- 비용: 결과 검사 차례는 실행 공간을 끄므로, 그 뒤의 ComfyUI 작업(다른 사람 것일 수도 있다)이 콜드 스타트를 한 번 더 한다. 패널의 자동 검사도 같다. 그래서 기본값은 대기열에 다른 ComfyUI 줄이 없을 때만 검사한다(D15). P1 시험에서 `cold_start_ms` 로 영향을 잰다.

### 5.7 Ask 계획 카드 (선택)

- 대화 설정 `chat_threads.settings.ask` 가 `auto`(기본) 또는 `ask_long` 이다. 화면 왼쪽의 "긴 영상은 먼저 계획 확인" 켜고 끄기와 같다.
- 조건: `ask_long` 이고, `action` 이 `i2v`·`t2v` 이고, 완료까지 예상 시간이 `STUDIO_ASK_LONG_SECONDS` 이상이다.
  - 예상 시간 = 내 앞 대기열의 예상 + 레시피 예상(8.5). 기본값 300초 [추정]. 0 이면 모든 영상에 카드를 띄운다.
- 시점: 프롬프트를 쓴 뒤, `submit_comfy` 전이다. 카드에 렌더러의 `ko` 세 칸과 길이 이유를 보여 주기 위해서다. LLM 차례는 이미 썼다. 러다이트도 계획을 쓴 뒤 승인을 받았다(DFC/02:104).
- 카드 내용: 종류, 첫 화면 썸네일, 길이(초·프레임·fps), 크기, 품질 단계(레시피의 `quality_label`, 예: "빠름". 모델 이름·LoRA·스텝 수는 보이지 않는다, 6.2), 글 길이와 이유, `ko` 세 칸, 완료까지 예상 시간. 길이 이유가 없으면(첫 화면을 못 읽어 움직임만 썼을 때) 그 사실을 쓴다(2.2 ⑤).
- 승인하면 바로 접수한다. 승인은 조건부 상태 전이라 두 번 눌러도 한 번만 접수한다(5.2). 취소하면 ComfyUI 작업 없이 끝나고 카드가 "취소됨"으로 바뀐다(DFC/01:79 와 같은 흐름).
- `STUDIO_PLAN_TTL_HOURS`(기본 24) 안에 답이 없으면 자동 취소하고, 카드는 "시간이 지나 취소됨"으로 접힌다.
- 승인을 기다리는 동안 같은 대화에 새 메시지가 오면 그 계획을 고쳐 달라는 말로 본다. 카드는 "바꿔서 다시 계획함"으로 접히고(`cancelled`), 새 요청이 `revise`(원래 요청 + 이번 말)로 계획을 다시 만든다. LLM 차례를 다시 쓴다.
- 승인할 때 레시피 판(`recipe_version`)이 계획 때와 다르면(그 사이 배포) 접수하지 않고 계획을 다시 만든다(6.5).
- `awaiting_approval` 은 GPU 차례를 갖지 않으므로 활성 한도에 세지 않는다. 열린 요청 한도에는 센다(5.11).
- 칩(길이·크기)을 눌러 바꾸는 기능은 넣지 않는다. 러다이트에서도 그 동작을 보지 못했다(DFC/02:110) [TBD].

### 5.8 이어 말하기 맥락

요청마다 저장하는 것 (`chat_turns`):

| 칸 | 내용 | 나중에 쓰는 곳 |
|---|---|---|
| `request` | 원래 글, 첨부 id, 이어 말하기면 바뀐 점 | 다음 요청의 맥락 |
| `decision` | action·source·carry, 판단 방법(규칙·LLM)과 이유 | 운영자 기록 |
| `work.frame` | 첫 화면 읽기 결과 JSON (`hub/app/video.py:432-446`) | revise 때 8B 차례 생략 |
| `work.prompt`, `work.parts`, `work.ko` | 최종 프롬프트, 네 단락, 한국어 세 칸 | revise 의 고쳐 쓰기 시작점, 계획 카드 |
| `work.length_choice`, `work.profile` | 길이 판단과 실제 프로필 | revise 때 같은 프로필 유지 |
| `work.settings`, `work.size`, `work.seed` | 프레임·fps·초, 출력 크기, 시드 | 다시 만들기 |
| `result` | 결과 첨부 id, 검사 결과 | "이걸로 영상", 편집 대상 |

- '직전 결과'는 결과 첨부(`result.attachment_ids`)가 있는 마지막 `done` 요청의 결과다. `answer` 요청(R2·R7b 의 정해 둔 답, P4 의 `answer`)과 결과 없이 `done` 이 된 요청은 실패·취소·중단된 요청처럼 건너뛰고 그 앞의 결과를 본다. 그런 결과가 하나도 없으면 R5~R7b 는 맞지 않고 새 요청이 된다.
- 결과를 골라 이어 가는 법: 결과 카드의 버튼([다시 만들기], [이걸로 영상 만들기])은 그 카드의 요청을 가리킨다(13.2). 글로 "첫 번째 걸로"처럼 앞 결과를 고르는 것은 P3 까지 하지 않는다. P4 에서 LLM 판단에 최근 결과 셋 중 하나를 고르는 칸을 둘지 정한다 [TBD].
- LLM 에 주는 맥락은 직전 세 요청의 요약만이다(action, `request_ko`, 결과 종류, 프롬프트 앞 300자). 대화 전체를 넣지 않는다. 허브 안 LLM 호출은 `num_ctx` 12288 로 고정이고(`hub/app/llm.py:280-281`, `hub/app/config.py:74`) T2V 장문 지시문만 약 2,200토큰이라서다(`docs/video-renderer.md:233`).
- 영상 고치기(`revise`):
  - 글은 `원래 요청 + "\n" + 이번 말` 이다. 사용자 말(예: '천천히')이 검사 대상 요구가 되게 하려는 것이다(`hub/app/video.py:797`, `620`).
  - 2000자를 넘으면 앞의 바뀐 점부터 뺀다(`promptx.clean_input` 한도, `hub/app/promptx.py:68-74`).
  - 첫 화면은 이전 첨부를 그대로 쓰고, 읽기 결과도 `work.frame` 을 다시 쓴다.
  - 초안은 새로 쓰지 않고 이전 네 단락에서 시작한다. `video.draft` 의 `start` 구조를 쓴다(`hub/app/video.py:790-824`, `802-803`). 지금 `start` 는 고칠 위반 목록을 전제로 해서(`hub/app/promptx.py:219-221`), P3 에서 `revise` 인자(바꿀 점 글)를 더해 첫 메시지를 "이 변경을 네 단락에 적용하고 나머지는 지켜라"로 바꾼다.
- 이미지 고치기(`revise`, R6a, P2): 글은 `원래 요청 + "\n" + 이번 말` 이고 새로 확장한다. 시드도 새로 뽑는다. 원래 그림을 살리면서 일부만 바꾸려면 편집 말을 쓰면 되고, 그러면 편집(R7, P3)이 된다.
- 이미지 편집("배경만 바꿔줘")은 `edit`·`last_result` 다. 직전 결과 파일을 입력 폴더로 복사해 새 첨부(`source='copy'`)로 만들고 편집 레시피에 넣는다(7.3).
- 반복("하나 더", R6b)은 다시 만들기와 같다. 저장된 프롬프트·설정·크기(`work.prompt`·`settings`·`size`)로 시드만 바꾼다(5.2 의 `understand` → `queue`).
- 한 대화의 요청은 한 번에 하나씩 처리한다(5.11). 앞 요청이 아직 돌면 새 요청은 `pending` 으로 기다리고 진행 카드에 "앞 작업이 끝나면 시작합니다"가 보인다. 앞 요청이 끝나면 그때 의도를 판단한다. 앞 요청이 실패·취소되면 위 정의대로 그 앞의 성공 결과를 직전 결과로 본다.

### 5.9 취소

| 취소한 때 | 하는 일 | 근거 |
|---|---|---|
| `pending`, `plan`, `awaiting_approval` | 상태만 `cancelled`. GPU 작업이 없다 | |
| LLM 차례를 기다리는 중 | 감독 작업을 취소한다. `llm_turn` 이 대기 줄을 취소한다 | `hub/app/llm.py:262-268` |
| LLM 차례 실행 중 | 감독 작업을 취소한다. `llm_turn` 의 정리 단계가 모델을 내리고 차례를 놓는다. Ollama 쪽 생성이 연결 끊김에 바로 멈추는지는 [추정] | `hub/app/llm.py:302-321` |
| ComfyUI 작업을 접수하는 중 | 접수는 끝까지 하게 둔다. 중간에 끊으면 접수된 작업이 남아 돌거나, 입력 고정 사본 폴더(`_mgs_pinned/<job_id>/`)가 남는다. 그 폴더를 지우는 곳은 작업 끝 정리뿐이다. `asyncio.shield` 로 감싸기만 하면 모자라다: 감독 작업이 취소되면 shield 를 기다리던 자리에서 바로 `CancelledError` 가 나고 안쪽 접수만 계속돼, 감독은 접수된 작업의 id 를 받지 못한다. 그래서 접수 작업을 따로 쥐고(`asyncio.ensure_future`) 취소가 오면 그 작업이 끝나기를 다시 기다려 id 를 받는다. 그다음 `chat_turn_jobs` 를 쓰고 아래 줄을 한 뒤 취소를 다시 올린다(6.6). 접수는 편집 공간 검증과 노드 설치 대기로 각각 최대 120초 걸릴 수 있어 이 틈은 짧지 않다 | `hub/app/jobs.py:63`, `86-92`, `129-130`, `hub/app/promptops.py:76-79`, `hub/app/scheduler.py:479-488` |
| ComfyUI 작업이 대기·준비·실행 중 | 늘 `jobs.cancel_queued(env_id, [job_id])` 를 먼저 부르고, 0건이면 `scheduler.interrupt(env_id, job_id)` 를 부른다. 기존 ComfyUI 화면도 둘을 함께 부른다. interrupt 는 꼭 job id 를 준다. id 없이 부르면 같은 작업공간의 다른 사람 작업까지 멈춘다 | `hub/app/jobs.py:311-335`, `hub/app/proxy.py:121-131`, `hub/app/scheduler.py:495-505` |
| 결과 검사 중 | 검사만 멈춘다. 결과는 이미 전달했다 | |
| 대화 삭제 | 그 대화의 열린 요청을 위 규칙으로 모두 취소한 뒤 대화를 숨긴다 | 10.1, 9.4 |

감독 작업을 취소하는 규칙 (LLM 차례가 대기열 전체를 멈추지 않게):

- 요청 하나에 `task.cancel()` 은 한 번만 부른다. 취소 버튼 두 번, 대기 기한, 대화 삭제처럼 여러 길이 같은 작업을 취소하려 해도 플래그로 한 번만 보낸다. 플래그에는 이유(`user`·`thread_deleted`·`deadline`·`run_deadline`)를 함께 둔다. 13.1 의 안내를 고르고 `chat_events.llm_job` 에 남기는 데 쓴다(9.2).
- `llm_turn` 의 정리(finally) 중에 취소가 오면(첫 번째든 두 번째든. 예: 본문이 정상으로 끝나 정리하는 동안 기한이나 사용자 취소가 걸림) 첫 `await asyncio.shield(...)` 에서 `CancelledError` 가 나고, `except Exception` 이 그것을 잡지 못해 마지막 줄 `waiter.done.set()` 까지 가지 않는다(`hub/app/llm.py:307-321`. 허브의 python 3.12 에서 `CancelledError` 는 `BaseException` 이다, `hub/Dockerfile:1`). 그러면 스케줄러가 `waiter.done` 을 `JOB_MAX_SECONDS`(기본 14400초) 동안 기다리고, 그동안 대기열 전체가 멈춘다(`hub/app/scheduler.py:518-522`, `hub/app/config.py:42`). '한 번만 취소' 규칙으로는 이 경우를 막지 못하고, 아래의 `llm.py` 고침이 막는다.
- 한 번만 취소해도 생기는 구멍이 둘 있다.
  - 스케줄러가 대기자를 확인한 뒤(`hub/app/scheduler.py:513-514`) DB 를 갱신하고(`516`) `go` 를 세우는(`518`) 틈에 취소가 오면, `llm_turn` 은 대기 줄 취소만 하고 `waiter.done` 은 세우지 않는다(`hub/app/llm.py:260-268`). 대기 줄 취소는 `state='queued'` 조건이라 아무 일도 하지 않는다(`hub/app/jobs.py:323-326`). 관리자 취소(`release_waiter`)가 같은 틈에 와도 같다(`hub/app/llm.py:269-271`).
  - `llm_turn` 은 대기자를 등록한 뒤(`hub/app/llm.py:254`) insert 커밋(`255-257`), `scheduler.wake`(`258`), `notify_queue_changed`(`259`, DB 조회와 방송을 await, `hub/app/jobs.py:38-40`)를 try(`260`) 밖에서 한다. 이 사이에 취소가 오거나 DB 조회가 예외를 내면 대기자는 `cancelled=False` 로 남고 jobs 줄은 `queued` 로 남는다. 스케줄러는 나중에 그 줄을 꺼내 사전 확인을 통과시키고(`hub/app/scheduler.py:256-259`) `done` 을 `JOB_MAX_SECONDS` 동안 기다린다(`513-522`).
  - 어느 쪽이든 스케줄러는 차례를 쥔 동안 다음 작업을 꺼내지 않는다(`hub/app/scheduler.py:157-159`).
- 그래서 P1 에서 `llm.py` 를 고친다(9.2): 대기자 등록부터 차례 기다리기까지(`hub/app/llm.py:254-271`)를 한 try 로 묶는다. 빠져나가는 모든 길(시간 초과·취소·예외·`waiter.cancelled`)에서 첫 await 전에 `waiter.result = {"status": 499}` 와 `waiter.done.set()` 을 한다. finally 의 정리는 `try … finally: waiter.done.set()` 으로 감싸 `CancelledError` 에도 마지막 줄이 돌게 한다. 그러면 스케줄러가 바로 `error`(또는 `cancelled`)로 끝내고 정리 단계로 간다. 패널에도 이로운 고침이다.

알려진 구멍 (어느 경우든 감독은 취소한 요청의 결과를 채팅에 내지 않는다. 결과 파일은 결과 폴더에 남는다):

- 실행 공간을 띄운 직후부터 `running` 으로 바뀌기 전까지 온 중단은 표시만 남고 실행된다(`hub/app/scheduler.py:322-347`, `501-504`).
- DB 가 `running` 으로 바뀐 직후, 실행 공간이 프롬프트를 아직 꺼내 가지 않았을 때 온 interrupt 는 ComfyUI 가 '실행 중 아님'으로 건너뛴다([CUI] `server.py:1263-1280`, `hub/app/scheduler.py:340-347`).
- 스케줄러는 `queued` 줄을 읽은 뒤(`hub/app/scheduler.py:169`) 상태 조건 없이 `starting` 으로 바꾼다(`207-209`). 그 사이 대기 중 취소가 들어오면 `cancelled` 로 바뀐 줄을 덮어써 실행한다. 스케줄러 UPDATE 에 `AND state='queued'` 와 행 수 확인을 더하는 것은 별도 작업으로 제안한다(19.2).
- 허브 재시작으로 이어받은 작업은 중단 요청이 실행 공간으로 가지 않는다(`hub/app/scheduler.py:210`, `313`, `333-347`).

### 5.10 허브 재시작과 복구

- `main.startup` 에서 스케줄러 시작 **전에** `studio.prepare()` 를, 뒤에 `studio.start()` 를 부른다(`hub/app/main.py:19-27`). 포털 앱의 시작 훅은 돌지 않으므로 여기에 넣는다(`hub/app/main.py:31-44`).
  - `prepare()`: DB 에서 열린 요청을 읽고, 연결된 ComfyUI 작업의 수신기를 같은 `client_id` 로 다시 등록한다. 스케줄러 시작 안의 복구가 이어받은 작업의 중계를 바로 시작하고(`hub/app/main.py:25`, `hub/app/scheduler.py:65`, `140`), 등록되지 않은 키의 메시지는 버려지기 때문이다(`hub/app/relay.py:44-46`). 끝 판정은 DB 로 하므로(5.13) 메시지를 놓쳐도 %만 늦게 보인다.
  - `start()`: 아래 표대로 끝나지 않은 요청을 처리한다.
  - 둘 다 예외를 잡아 허브 로그와 기록에 남기고 채팅만 끈다(`/studio` 는 503 `studio_not_ready` "스튜디오를 준비 중입니다", 10.1, 11.4). 허브 시작은 막지 않는다. `startup` 에서 예외가 나면 허브 전체(포털 80·LLM 11434·작업공간 포트)가 뜨지 않기 때문이다(`hub/app/main.py:34-40`).
  - 느린 일(Ollama `/api/tags` 확인)은 백그라운드 작업으로 돌린다. 파일만 보는 레시피 검사(V1~V10·V13)는 바로 한다(6.4).
  - 채팅 작업공간이 없거나 꺼져 있으면(`store.studio_env()` 가 None, `hub/app/envs.py:50-54`) 채팅을 끄고 같은 503 안내를 보인다. 7.2 를 하기 전에 P1 코드가 배포돼도 허브와 ComfyUI 화면은 정상이다.

| 남은 상태 | P1 | P2 부터 |
|---|---|---|
| `pending` | 감독 작업을 다시 만든다 | 같음 |
| `awaiting_approval` | 그대로 둔다. 만료 타이머만 다시 건다 | 같음 |
| `active`, LLM 단계(`understand`·`frame`·`prompt`·`fix`) | `interrupted`, "서버가 다시 시작되어 멈췄습니다. 다시 시도할까요?" [다시 시도](처음부터. 프롬프트가 아직 끝나지 않았다, 13.1). LLM 대기자는 메모리에 있어 사라진다(`hub/app/scheduler.py:48`, `256-258`) | 저장된 마지막 단계부터 한 번 자동으로 다시 한다. 또 실패하면 `interrupted` |
| `active`/`plan` | 저장된 `work` 로 계획 카드를 다시 띄우고 `awaiting_approval` 로 둔다(GPU 없음) | 같음 |
| `active`/`queue`, `work.pending` 없음 | 아직 접수 전이므로 접수부터 다시 한다(승인 직후 등) | 같음 |
| `active`/`queue`, `work.pending` 은 있고 `chat_turn_jobs` 는 없음 | `SELECT id FROM jobs WHERE env_id=$1 AND client_id=$2` 로 찾는다(6.6). 있으면 연결하고 이어서 지켜본다. 없으면 접수 전에 멈춘 것이라 다시 접수한다 | 같음 |
| ComfyUI 작업 `queued` | 이어서 지켜본다. 작업은 DB 에 있어 스케줄러가 순서대로 실행한다 | 같음 |
| ComfyUI 작업 `running`, 스케줄러가 이어받음 | 이어서 지켜본다(`hub/app/scheduler.py:128-141`) | 같음 |
| ComfyUI 작업이 재시작 전에 이미 `finished` (단계는 `queue`·`prepare`·`generate`) | DB outcome 으로 5.2 의 `collect` 행을 따른다(성공이면 결과를 고른다) | 같음 |
| ComfyUI 작업 `lost` | 먼저 `job_outputs` 에서 레시피 main 출력과 실제 파일을 찾는다. 있으면 `collect` 로 간다. 정리(cleaning) 중에 재시작돼도 `lost` 가 되는데(`hub/app/scheduler.py:124`, `142-145`), 그때는 결과 수집이 이미 끝나 있다(`352` → `225-226`). 출력이 없으면 `failed` + [다시 만들기] | 출력이 없을 때만 같은 값으로 한 번 자동 재접수 |
| `active`/`collect` | DB 에서 결과를 다시 고른다 | 같음 |
| `done`, 검사 중 | 검사를 건너뛰고 기록만 남긴다 | 같음 |

- `update.sh` 는 GPU 를 쥔 작업만 기다린다(`install/update.sh:101-129`). 그동안 대기열이 `paused` 라 화면은 "점검 중"을 보이고, 감독은 점검 시간을 대기 기한에 세지 않는다(5.6). 그 뒤 허브 컨테이너를 다시 만드므로(`install/update.sh:95`, `157`) 위 표를 따른다. P1 에서는 LLM 단계 요청이 이때 `interrupted` 가 된다. 정지만 하고 허브를 다시 만들지 않는 것은 관리자 화면의 일시 정지다(`hub/app/portal.py:364-367`).
- LLM 단계 자동 재개를 P2 로 미루는 이유: P1 은 기존 부품이 이어지는지 확인하는 가장 좁은 한 줄이다(16절). 재개는 단계마다 저장된 값으로 다시 시작해야 해서 시험할 것이 많다.

### 5.11 동시성

- 한 대화 안: 요청은 한 번에 하나씩 처리한다. 새 메시지는 `pending` 으로 받고 `depends_on` 을 같은 대화의 마지막 열린 요청으로 채운다. 그 요청이 끝난 상태(`done`·`failed`·`cancelled`·`interrupted`)가 되면 시작하고, 그때 의도를 판단한다(5.3). 검사 중인 `done` 은 끝난 것으로 본다. 승인 대기 중인 요청은 새 메시지가 오면 계획을 고쳐 달라는 말로 보고 끝낸다(5.7).
- 한 사용자: 동시에 `active` 인 요청은 `STUDIO_MAX_ACTIVE_PER_USER`(기본 2)까지다. 넘는 요청은 `pending` 으로 기다리다 자리가 나면 보낸 순서대로 시작한다. `awaiting_approval` 은 GPU 차례를 갖지 않으므로 세지 않는다.
- 열린 요청(`pending`·`active`·`awaiting_approval`)은 사용자당 `STUDIO_MAX_OPEN_PER_USER`(기본 5 [추정])까지다. 넘으면 메시지 보내기가 429 `too_many_open` 이고 입력창 아래에 "진행 중인 작업이 많습니다. 하나가 끝난 뒤 보내 주세요"가 보인다. 분당 10건 한도만으로는 10분에 100건이 쌓이고, 각각 GPU 차례 3~5개를 쓸 수 있어서다.
- 이 대기들은 `jobs` 에 줄을 넣기 **전**의 문지기다. 줄을 넣는 시점만 늦출 뿐, 넣은 뒤의 실행 순서는 seq 그대로다(4절).
- 여러 사용자: 각 단계가 `jobs` 한 줄이라 메인 전체 순번(seq)대로 돈다. 채팅에 따로 대기열을 두지 않는다.
- 공평함: 채팅 요청 하나는 대기열 줄 3~5개를 쓰고, 단계마다 맨 뒤에 다시 선다. 그래서 붐빌 때 ComfyUI 화면에서 바로 실행하는 것보다 오래 걸린다. 순번 규칙을 바꾸지 않는 대신 화면에 대기 순번을 단계마다 보인다.
- 한 사용자의 여러 탭: WebSocket 은 사용자 단위로 모두에게 같은 이벤트를 보낸다(10.2).
- 요청 한 번의 DB 연결: `llm_turn` 은 줄을 넣을 때만 연결을 쓰고 기다리는 동안은 쓰지 않는다(`hub/app/llm.py:255-261`). 연결 풀은 12개다(`hub/app/db.py:28`).

### 5.12 자동 고치기·결과 검사·재시도 정책

| 상황 | 자동으로 하는 일 | 채팅에 보이는 것 |
|---|---|---|
| 영상 프롬프트에 위반이 남음 | 낱말 수 위반만 남았으면 고치지 않는다. 메인 시험에서 27B 가 낱말 수를 자주 넘겼고, 고쳐 쓰기와 자동 고치기로도 줄지 않았으며, 해롭지 않았다(`docs/video-renderer.md:248-249`, `270`). 그 밖의 위반이 남으면 P1: `video.fix` 차례 한 번, P3: 같은 차례 안에서 고치기 (최대 3회 호출, `hub/app/video.py:953`) | 없음 (아래 '심각한 위반'만 안내) |
| 고친 뒤에도 위반이 남음 | 생성은 그대로 한다. `fix` 는 나아지지 않으면 원래 글을 지킨다(`hub/app/video.py:958-959`) | 심각한 위반이면 결과 뒤에 한 줄 + [다시 시도] (프롬프트를 다시 쓴다) |
| 결과 검사에서 문제 | 자동 재생성은 기본으로 하지 않는다(`STUDIO_AUTO_REROLL=0`). 패널에도 완전 자동 재실행은 없다(`docs/prompt-helper.md:112`) | 결과 뒤에 한 줄 + [다시 만들기] |
| LLM 차례가 끝나지 못함 | 다시 줄 서지 않는다. 원인은 둘로 가른다. 감독이 스스로 취소했으면 감독의 취소 이유(`deadline`·`run_deadline`·`user`·`thread_deleted`, 5.9)로 정한다. 이 길의 `end` 는 `caller_cancelled` 다. 그 밖에는 `STUDIO_JOBS` 항목의 `end` 로 정한다(9.2): `cancelled`(관리자 취소), `rejected`(비활성 사용자 등 사전 확인 거부), `timeout`(24시간 `STUDIO_LLM_HARD_WAIT` 를 넘김. 대기 기한과 같게 안내한다). P1 에서는 `video.render` 가 `HelperBusy` 를 `PromptxError(503)` 로 바꿔 원인이 사라지므로(`hub/app/video.py:900-901`) 문자열 대신 이 값을 쓴다. 관리자가 포털에서 채팅의 LLM 줄을 취소했는데(`hub/app/portal.py:372-375` → `hub/app/jobs.py:328-332` → `hub/app/scheduler.py:77-83` → `hub/app/llm.py:269-271`) 감독이 다시 줄을 서면 그 취소를 되돌리게 된다 | 13.1 의 해당 줄 |
| LLM 호출 오류 (502) | 없음 | 한 줄 + [다시 시도] |
| 편집 공간 꺼짐·준비 중 | 60초 뒤 다시 접수, 최대 5회. `SubmitError`(503)뿐 아니라 `httpx.HTTPError`(연결 거부·시간 초과)도 '편집 공간 준비 중'으로 본다. 편집 공간 컨테이너는 떠 있고 ComfyUI 가 아직 듣지 않는 동안에는 이 예외가 `SubmitError` 로 바뀌지 않고 그대로 올라온다(`hub/app/jobs.py:61-68`, `78-83`). 기존 화면은 이 경우 500 을 낸다(`hub/app/proxy.py:91-98`) | "준비 중" 단계 글. 끝내 실패하면 한 줄 + [다시 시도] |
| 노드 설치 중 (`mgs_env_busy`, 400) | 같음 (`hub/app/jobs.py:86-92`) | 같음 |
| 접수 검증 실패 (400 + `node_errors`, 실행 공간의 접수 거부 포함) | 기록하고 관리자 확인 표시(13.1). 같은 레시피로 `STUDIO_RECIPE_FAIL_LIMIT`(기본 3)번 잇달아 나면 그 레시피를 끄고 `studio_recipe_checks` 에 끔 행을 남긴다. 다시 켜는 길은 6.4 | 한 줄 (버튼 없음) |
| 그 밖의 접수 예외 | 먼저 `client_id` 로 jobs 줄을 찾는다(6.6 `find_submitted`). 줄이 있으면 커밋 뒤의 예외이므로 접수된 것으로 보고 이어 간다. 없으면 기록(`chat_events.error`) 뒤 실패 | 일반 실패 한 줄 (13.1) |
| ComfyUI 실행 오류 (`error`) | 다시 하지 않는다 | 한 줄 + [다시 만들기] |
| 실행 공간이 사라짐 (`lost`) | 5.10: 결과가 있으면 그대로 쓴다. 없으면 P1 은 실패, P2 부터 한 번 자동 재접수 | 실패하면 한 줄 + [다시 만들기] |
| 단계 기한 넘김 | 아래 표 | 한 줄 + 버튼 (13.1). 결과 확정 기한은 진행 카드 글만 바꾼다 |

단계 기한 (감독의 감시 작업이 지킨다. 이벤트 없이 멈춘 요청이 '생성 n%'·'GPU 준비 중'에 영원히 남지 않게 한다):

| 단계 | 기한 | 넘으면 |
|---|---|---|
| LLM 차례 대기 | `STUDIO_LLM_WAIT_SECONDS`(3600 [추정]). `db.get_setting("paused")`·`db.get_setting("blocked")` 가 켜진 동안은 세지 않는다 | 감독 작업을 한 번 취소(이유 `deadline`) → `failed` |
| LLM 차례 실행 | `STUDIO_LLM_TURN_SECONDS`(900 [추정]). Ollama 호출 하나의 한도는 600초다(`hub/app/llm.py:284`) | 감독 작업을 한 번 취소(이유 `run_deadline`) → `failed` |
| ComfyUI 대기(`queued`) | 없음. 순서대로 돈다. 사용자가 취소할 수 있다 | |
| ComfyUI 실행(`starting`~`running`) | 레시피 `limits.max_run_seconds` (필수 칸, V1 이 0 보다 큰지 본다. 예시 I2V 900초 [추정]). DB 에서 `now() - jobs.started_at` 으로 잰다. `started_at` 은 `starting` 때 써지므로(`hub/app/scheduler.py:207-209`) 허브가 다시 시작해 이어받은 작업도 기한이 처음부터 다시 시작하지 않는다. 스케줄러의 한도 `JOB_MAX_SECONDS`(기본 4시간, `hub/app/config.py:42`)는 채팅에는 너무 길다 | `cancel_queued` → `scheduler.interrupt(env_id, job_id)`, `failed` |
| 결과 확정(`collect`) | 성공 신호를 받았으면 `cleaning` 에서, 아니면 `finished` 에서 읽는다(5.2, 5.13). `collect` 에 들어간 지 600초 [추정]를 넘으면 늦은 것으로 본다. 정리(`_cleanup`)가 최악 약 4~5분 걸릴 수 있어 그보다 길게 잡았다(`hub/app/scheduler.py:581-608`) | 실패로 끝내지 않는다. 진행 카드에 "마무리가 늦어지고 있습니다"를 보이고 `deadline` 을 기록한다. outcome 이 오면 5.2 대로 끝낸다 |

- 심각한 위반: 결과에 직접 보이는 것만 안내한다. 낱말 수 위반은 기록에만 남긴다.

| 심각한 위반 (영문 검사 문구) | 이유 |
|---|---|
| 글자·로고·간판 (`Do not mention text, letters…`, `hub/app/video.py:678-679`) | 영상에 가짜 글자가 그려질 수 있다 |
| 한글 섞임 (`must be English only`, `hub/app/video.py:650-654`) | 영상 모델이 읽지 못한다 |
| 컷 전환 (`Keep one continuous shot`, `hub/app/video.py:687-688`) | 한 컷 클립이 깨진다 |
| 1인칭인데 자기 몸이 보임 (`hub/app/video.py:694-700`) | 러다이트 파일럿 시험의 어깨너머 실패와 같은 종류 (`docs/video-renderer.md:205`) |
| 사람 없는 요청에 사람·손 (`hub/app/video.py:680-681`) | 결과에 사람이 생긴다 |
| 요청한 말이 글에 없음 (`The user wrote '…'`, `hub/app/video.py:668-672`) | 요청한 움직임(예: '천천히')이 결과에 없을 수 있다. 이어 말하기 "더 천천히"가 반영되지 않아도 사용자가 모른다 |
| 손 숨김 위반 (`The hands must stay hidden`, `hub/app/video.py:682-686`) | 숨기라고 한 손이 보일 수 있다 |
| 카메라 이동 둘 이상 (`Use one main camera move`, `hub/app/video.py:689-693`) | 한 컷 안에서 카메라가 이리저리 움직인다 |

- 안내 글은 위반마다 `video.explain_ko` 의 풀이(`hub/app/video.py:715-749`)를 바탕으로 만든다. 예: "요청하신 '천천히'를 글에 넣지 못한 채 만들었습니다. 프롬프트를 다시 써 볼까요?" [다시 시도]. 검사 문구에 사용자가 쓴 낱말이 들어 있다(`The user wrote '천천히'`).

흐름별 남은 위반과 안내(`advice`) 처리:

| 흐름 | 고쳐 쓰기 뒤 남은 위반 | 렌더러·도우미의 안내(`advice`) |
|---|---|---|
| `i2v`·`t2v` | 위 '심각한 위반'만 결과 뒤 한 줄, 나머지는 기록 | 사용자에게 보이는 것은 '사진을 읽지 못함'(5.5)과 길이·비율·소리 지정 안내(5.3)뿐이다. 그 밖의 안내는 패널을 가리키는 문구라 운영자 기록에만 남긴다: 길이 상한(`hub/app/video.py:846-848`), "LoadImage 노드가 있으면…"(`856`), 제작진 건너뜀(`hub/app/promptx.py:423-427`), 영상 제작진 초안 실패(`hub/app/video.py:893`) |
| `t2v` 장면 확장 | `scene_violations` 는 기록만 한다(`hub/app/video.py:912`). 최종 판단은 영상 위반 기준이다 | `expand_in_turn` 의 안내가 합쳐져 온다(`hub/app/video.py:885`). 모두 기록만 |
| `image` | `expand_in_turn` 이 고쳐 쓰기 뒤 남은 `violations` 를 돌려준다(`hub/app/promptx.py:471-484`). 생성은 그대로 하고 기록만 한다. 이미지 위반 가운데 결과에 바로 보이는 것을 고르는 기준은 P2 시험 뒤 정한다 [TBD] | 손 숨김·두 사람 안내(`hub/app/promptx.py:506-514`)는 "Qwen-Image-Edit 로 … 계획하세요", "결과 자동 검사의 인물 수를 확인하세요"처럼 패널 작업을 가리키므로 보이지 않는다. 두 사람 요청이면 감독이 검사의 인물 수를 직접 보고, 1명이면 "두 사람이 한 몸처럼 붙어 보입니다. 다시 만들어 볼까요?" 한 줄 |
| `edit` | 편집 지시문 검사(5.5)에 걸리면 한 번 고쳐 쓰고, 그래도 걸리면 생성은 그대로 하고 기록 | 없음 |

결과 검사의 안내 기준 (P1, 감독 쪽 거르기, `promptx` 는 바꾸지 않는다). 검사 종류는 열한 개다(`hub/app/promptx.py:236`). 안내 글은 "검사에서 <한국어> 보입니다. 다시 만들어 볼까요?" [다시 만들기] 이다. 안내 여부는 P1 측정 뒤 오탐률을 보고 고친다 [추정]:

| 검사 종류 | 채팅 안내 | 한국어 |
|---|---|---|
| `extra_head` | 함 | 머리가 하나 더 있는 것처럼 |
| `detached_head` | 함 | 목이 끊긴 것처럼 |
| `merged_bodies` | 함 | 두 사람이 붙은 것처럼 |
| `face` | 함 | 얼굴이 뭉개져 |
| `text_artifact` | 함 (글자 위반을 심각하게 보는 이유와 같다) | 알아볼 수 없는 글자가 생긴 것처럼 |
| `missing_head` | 1인칭이 아니면 함 | 머리가 없는 것처럼 |
| `extra_limb` | 1인칭이 아니면 함 | 팔다리가 하나 더 있는 것처럼 |
| `floating_limb` | 1인칭이 아니면 함 | 팔다리가 몸에서 떨어져 떠 있는 것처럼 |
| `missing_limb` | 안 함 (기록만). 가려진 팔다리를 없는 것으로 보기 쉽다 [추정] | |
| `extra_fingers` | 안 함 (기록만). 영상 프레임은 작고 흐려 손가락 수를 믿기 어렵다 [추정] | |
| `other` | 안 함 (기록만) | |
| 기대 특징이 안 보임 (`traits`) | 함 | 보는 사람이 화면에 보이는 것처럼 (1인칭 특징) |

- 1인칭 요청이면 `missing_head`·`extra_limb`·`floating_limb` 를 안내하지 않는다. 화면 가장자리의 자기 손·팔은 몸에 붙어 보이지 않아 오탐이 되기 쉽다(`hub/app/promptx.py:281-289`) [추정].
- 기대 특징(`expect`): 1인칭 요청이면 "The view is first-person: no head, back or shoulders of the viewer are visible" 을 넣는다. 특징이 안 보이면 안내한다.
- 전체 검사 결과는 운영자 기록에 남긴다.

### 5.13 결과 찾기와 전달

1. 끝 판정은 DB 로 한다. 감독의 감시 작업이 지금 기다리는 `jobs` 줄을 1~2초마다, 그리고 `status` 방송(8.4)이 올 때 `SELECT state, outcome, error FROM jobs WHERE id = $1` 로 읽는다. 수신기의 진행 메시지(`execution_success` 등)는 화면을 빨리 바꾸는 데만 쓴다.
   - 이유: 스케줄러는 수신기에 아무 메시지도 보내지 않고 작업을 끝낼 수 있다. 최대 실행 시간 초과(`hub/app/scheduler.py:369-372`), 실행 공간 사라짐(`377-378`, `383-384`), 5초 무응답 뒤 `/api/jobs` 폴링으로 끝을 안 경우(`374-381`), WebSocket 재연결 사이(`382-388`), 시작 전 취소(`322-323`), 처리 중 예외(`221-223`)가 그렇다. 중계가 끝나면 WebSocket 을 닫으므로 뒤따르는 `execution_interrupted` 도 오지 않는다(`349-350`). 허브 형식 `execution_error` 를 보내는 것은 사전 확인 거부, 실행 공간 시작 실패, 실행 공간의 접수 거부뿐이다(`204`, `319`, `344`).
2. `state` 가 `cleaning` 이나 `finished` 가 되면 `job_outputs` 는 이미 다 들어가 있다. 결과 수집이 정리보다 먼저 끝나기 때문이다(`hub/app/scheduler.py:352` → `225-226`). 그때 `SELECT node_id, rel_path, media FROM job_outputs WHERE job_id = $1` 을 한 번 읽는다.
   - 시간 창을 두고 기다리지 않는 이유: 스케줄러의 결과 수집은 history 를 최대 10번(요청마다 5초 한도 + 0.5초 쉼) 읽고, `/api/jobs` 를 5초 한도로 한 번 더 읽은 뒤에야 이 표를 채운다(`hub/app/scheduler.py:435-463`). 최악이면 약 1분이다.
   - outcome 은 정리가 끝난 뒤 `finished` 와 함께 써지고(`hub/app/scheduler.py:231`), 정리는 몇 분 걸릴 수 있다(`581-608`). 그래서 수신기가 이 작업의 `execution_success`(또는 `executing` 의 `node: null`)를 받았으면 `cleaning` 에서 바로 결과를 고른다. 받지 못했으면 `finished` 를 기다려 outcome 이 `success` 인지 본다. `success` 가 아니면 결과를 고르지 않고 5.2 의 `collect` 실패 행을 따른다(`lost`·`cancelled`·그 밖).
3. 레시피의 `outputs.main.node` 와 같은 `node_id` 이고 확장자가 `outputs.main.ext` 에 있는 줄을 결과로 고른다. `media` 칸으로 고르지 않는다(6.3 끝).
4. `chat_attachments` 에 결과 행을 넣는다: `source='result'`, `media`=`outputs.main.kind`, `result_rel`=`rel_path`, `job_id`, 크기·프레임은 넣은 값으로. `bytes`·`sha256`·`meta.mtime` 은 파일에서 잰다. 결과 폴더는 누구나 덮어쓸 수 있어(`install/smb.conf:15-24`) 나중에 바뀌었는지 비교하려는 것이다(7.3, 10.1).
5. `chat_messages`(kind `result`)를 넣고, 같은 트랜잭션에서 결과 행의 `message_id` 를 이 메시지 id 로 채운다. `turn.result`·`message.new` 를 보낸다. `chat_turns.state` 를 `done` 으로 바꾼다(조건부, 5.2).
6. 검사(`STUDIO_INSPECT` 가 `on` 이거나, `idle` 이고 대기열에 다른 ComfyUI 줄이 없을 때, 그리고 레시피에 `inspect.output` 이 있고 `inspect.when` 이 `off` 가 아니면): 검사할 줄(`outputs[inspect.output].node`. 예시 I2V 는 `check` 의 `901`, 이미지·편집은 `main`)의 `rel_path` 에서 앞의 작업공간 주인 이름을 떼어 `subfolder`·`filename` 으로 나누고 `promptx.inspect(user, ip, env["username"], filename, subfolder, expect)` 를 부른다(`hub/app/promptx.py:517-520`, 결과 폴더 기준 경로 `hub/app/promptx.py:416-418`).
   - 예: `studio/chat/2026-11-12/alice/1a2b3c4d/check_frame_00001_.png` → `subfolder="chat/2026-11-12/alice/1a2b3c4d"`, `filename="check_frame_00001_.png"`.
   - 이미지 결과(P2)는 결과 파일 자체를 검사한다.
   - 검사가 실패하거나 LLM 차례를 받지 못하면 조용히 건너뛰고 기록만 남긴다. 결과는 이미 전달했다.
7. 성공인데 결과 파일을 못 찾으면 `failed` 와 안내(13절).
8. 결과는 정리가 끝나기 전에 보낼 수 있다(2의 경우). 나중에 outcome 이 `success` 가 아니면(결과를 낸 뒤 오류) 이미 보낸 결과는 그대로 두고 기록에만 남긴다.

## 6. 레시피

레시피는 "작업 하나 = API 형식 워크플로우 하나 + 값을 넣을 자리 표"다. 이미지 생성, 이미지→영상, 글→영상, 이미지 편집마다 하나씩 둔다.

### 6.1 왜 값 넣을 자리를 미리 정해 두는가 (화면 없는 서버)

- **허브는 UI 워크플로우를 실행할 수 없다.** 실행 접수는 API 형식만 받는다(`hub/app/promptops.py:1-3`, `18-21`, [CUI] `server.py:1177-1201`). UI JSON → API 형식 변환은 브라우저의 ComfyUI 프런트엔드만 한다([FE] `src/utils/executionUtil.ts:26`). 패널도 실행은 프런트엔드의 `app.queuePrompt` 로 한다(`runtime/nodes/mgs_bridge/web/mgs_panel.js:809`).
- **서브그래프를 펼치면 노드 id 가 바뀐다.** 공식 Wan 2.2 템플릿의 긍정 프롬프트 노드는 화면에서는 서브그래프 안의 93번이지만 API 형식에서는 `"129:93"` 이다(6.7). 화면 기준 탐색 결과를 그대로 쓸 수 없다.
- **패널의 노드 찾기는 짐작이고, 사람이 확인한다.** `promptCandidates`·`pickPositive` 는 샘플러 입력 이름 → 노드 제목 → 글 내용 순으로 역할을 짐작한다(`runtime/nodes/mgs_bridge/web/mgs_nodes.js:83-94`, `101-151`). 못 찾으면 사용자에게 노드를 골라 달라고 한다(`runtime/nodes/mgs_bridge/web/mgs_panel.js:400-412`). 채팅 사용자는 그래프를 보지 않으므로 잘못 넣어도 아무도 모른다. 예: 긍정 글이 부정 노드에 들어가면 결과만 이상해진다.
- **재현과 시험.** 자리를 고정하면 같은 입력에 같은 prompt 가 나온다. 단위 시험으로 값 넣기를 검사할 수 있고, 모델·노드가 있는지 미리 확인할 수 있다.
- 그래서 짐작(그래프 추적)은 **레시피를 등록할 때 한 번** 하고, 관리자가 결과를 확인한 뒤 자리를 고정한다(6.4).

### 6.2 형식

- 레시피는 저장소의 JSON 파일이다: `hub/app/studio/recipes/<id>.json`. 허브 이미지에 함께 들어간다(`hub/Dockerfile:9`). 바꾸면 커밋하고, 관리자가 메인에서 `sudo bash install/update.sh` 로 반영한다(root 가 필요하다, `install/update.sh:4`, `13`).
  - DB 에 두고 화면에서 올리는 방식은 기각했다. 레시피는 코드처럼 검토·버전 관리·단위 시험을 해야 해서다. 검증 결과만 DB 에 남긴다(`studio_recipe_checks`, 9.1).
- 아래 JSON 스키마는 문서용이다. 코드는 같은 규칙을 직접 검사한다. `jsonschema` 같은 새 의존성은 넣지 않는다(`hub/requirements.txt:1-11`).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "studio-recipe-1",
  "type": "object",
  "required": ["schema", "id", "version", "title", "quality_label", "action", "negative_source", "prompt", "inject", "outputs", "defaults", "progress", "limits"],
  "additionalProperties": false,
  "properties": {
    "schema":        {"const": "studio-recipe-1"},
    "id":            {"type": "string", "pattern": "^[a-z0-9_]{3,48}$"},
    "version":       {"type": "integer", "minimum": 1},
    "title":         {"type": "string", "description": "운영자 화면·검증 기록용 이름 (모델 이름을 써도 된다. 사용자에게 보이지 않는다)"},
    "quality_label": {"type": "string", "description": "계획 카드에 보일 품질 단계 이름 (예: 빠름). 모델·LoRA·스텝 수를 쓰지 않는다"},
    "action":        {"enum": ["image", "i2v", "t2v", "edit"]},
    "video_profile": {"type": "string", "description": "video_knowledge.json 의 프로필 id (영상 레시피만)"},
    "negative_source": {"enum": ["node", "zero_out"], "description": "부정 조건을 만드는 방식 (5.5, 6.4 V4·V5)"},
    "source":        {"type": "object", "description": "템플릿 이름·템플릿 패키지 판·프런트엔드 판·내보낸 날짜·지운 노드"},
    "prompt":        {"type": "object", "description": "API 형식 {노드 id: {class_type, inputs}}",
                      "additionalProperties": {"type": "object", "required": ["class_type", "inputs"]}},
    "inject":        {"type": "object", "description": "요청마다 바꾸는 자리. 이름 → 자리 하나 또는 여러 개",
                      "additionalProperties": {"oneOf": [{"$ref": "#/$defs/slot"},
                                                         {"type": "array", "items": {"$ref": "#/$defs/slot"}}]}},
    "fixed":         {"type": "object", "description": "바꾸지 않지만 검증하는 자리 (부정 프롬프트, 4스텝 켬 등)",
                      "additionalProperties": {"$ref": "#/$defs/slot"}},
    "outputs":       {"type": "object", "required": ["main"],
                      "properties": {"main": {"$ref": "#/$defs/output"}, "check": {"$ref": "#/$defs/output"}}},
    "defaults":      {"type": "object", "properties": {
                        "width": {"type": "integer"}, "height": {"type": "integer"}, "fps": {"type": "integer"},
                        "frames": {"type": "integer"}, "size_rule": {"enum": ["fixed", "keep_aspect_area"]},
                        "size_step": {"type": "integer"}, "max_side": {"type": "integer"}}},
    "progress":      {"type": "object", "required": ["bands"],
                      "properties": {"bands": {"type": "array", "items": {"$ref": "#/$defs/band"}}}},
    "inspect":       {"type": "object", "properties": {"output": {"type": "string"}, "when": {"enum": ["after_delivery", "off"]}}},
    "estimate":      {"type": "object", "properties": {"gpu_seconds": {"type": "number"}, "basis": {"type": "string"}}},
    "limits":        {"type": "object", "required": ["max_run_seconds"],
                      "properties": {"max_run_seconds": {"type": "integer", "minimum": 1, "description": "실행 기한 (5.12)"}}}
  },
  "$defs": {
    "slot": {"type": "object", "required": ["node", "input", "type"], "additionalProperties": false, "properties": {
      "node":   {"type": "string"},
      "input":  {"type": "string"},
      "type":   {"enum": ["string", "int", "float", "bool", "input_image", "prefix"]},
      "role":   {"enum": ["positive", "negative"]},
      "min":    {"type": "number"}, "max": {"type": "number"},
      "step":   {"type": "integer"}, "offset": {"type": "integer"},
      "max_len":{"type": "integer"},
      "suffix": {"type": "string", "description": "prefix 형식: <사용자 폴더>/<요청 앞 8자>/<suffix>"},
      "value":  {"description": "fixed 자리의 기대값"}}},
    "output": {"type": "object", "required": ["node", "kind"], "properties": {
      "node": {"type": "string"}, "kind": {"enum": ["image", "video"]},
      "ext":  {"type": "array", "items": {"type": "string"}}}},
    "band": {"type": "object", "required": ["key", "label", "weight", "nodes"], "properties": {
      "key": {"type": "string"}, "label": {"type": "string"}, "weight": {"type": "number", "exclusiveMinimum": 0},
      "nodes": {"type": "array", "items": {"type": "string"}}, "sampler": {"type": "boolean"}}}
  }
}
```

- 자리 이름은 감독 코드와 약속한 것만 쓴다: `prompt`, `first_frame`, `edit_image`, `seed`, `frames`, `width`, `height`, `check_index`, `save_prefix`.
- `title` 은 운영자 화면과 검증 기록에만 보인다. 계획 카드와 `turn.plan` 은 `quality_label` 만 쓴다(5.7, 10.2). 모델 이름은 사용자에게 보이지 않는다(11.2).
- `action` 마다 검증을 통과한 레시피 하나를 쓴다. 같은 `action` 의 레시피가 둘 이상 켜져 있으면 허브 시작 때 경고하고 `id` 순으로 첫 것을 쓴다. 품질 단계를 여러 개 둘지는 사용자 결정 D7 이다.
- 자리 값은 반드시 리터럴이어야 한다. 링크(`["노드", 슬롯]`)인 입력에는 넣지 않는다. 링크를 리터럴로 바꿔야 하면 등록 도구가 바꾸고, 쓰이지 않게 된 노드를 지운 뒤 `source.removed_nodes` 에 적는다.
- `negative_source` 는 둘 가운데 하나다. `node`: 부정 글 노드가 따로 있다(`fixed.negative` 필수). `zero_out`: 글 노드 하나의 출력을 0 조건 노드로 지워 negative 로 쓴다(`fixed.negative` 없음, Z-Image). negative 입력이 없는 구조(예: `BasicGuider` 는 입력이 `model`·`conditioning` 뿐이다, [CUI] `comfy_extras/nodes_custom_sampler.py:808-811`)는 계획한 레시피에 없어 다루지 않는다. 필요해지면 V4·V5·접수 직전 확인 규칙과 함께 더한다.
- 저장 노드가 여럿이면 모두 같은 폴더에 저장한다. `save_prefix` 의 suffix 에 `/` 를 쓰지 않는다. 설명 파일이 늘 요청 폴더에 생기게 하려는 것이다(6.3).
- 같은 레시피를 요청에 저장된 판과 다른 판으로 다시 쓸 때의 규칙은 6.5 에 있다.

### 6.3 Wan 2.2 14B 이미지→영상 전체 예시

출처와 한계:

- 개발 PC 의 공식 템플릿 `video_wan2_2_14B_i2v.json`([TPL])을 읽어 프런트엔드의 펼치기 규칙(6.7)대로 손으로 옮긴 것이다. **메인에서 내보낸 파일이 아니다.** P1 에서는 관리용 작업공간(6.7)에서 내보낸 "Export (API)" 결과를 레시피로 손으로 옮긴 것으로 바꾼다 [TBD].
- 템플릿 구조 (최상위 노드 3개 + 서브그래프 1개):
  - `97` LoadImage → `129` 서브그래프 "Image to Video (Wan2.2)" → `108` SaveVideo(`video/Wan2.2_i2v`).
  - 서브그래프가 열어 둔 입력: `start_image`, `text`(prompt), `width`, `height`, `value_1`(duration, 초), `noise_seed`, 모델 넷(고·저노이즈 모델과 LoRA), `clip_name`, `vae_name`, `value`(enable_turbo_mode = 4스텝 LoRA 켜기).
  - 개발 PC 판은 `length` 대신 `duration` 을 열어 두고 안에서 `floor(duration × fps + 1)` 로 프레임을 계산한다(노드 161·162·163). 메인 판이 `length` 를 직접 열었는지는 [TBD]다. 어느 쪽이든 레시피는 펼친 뒤의 `WanImageToVideo.length` 에 프레임 수를 넣는다.
  - 4스텝 LoRA 켜기는 `PrimitiveBoolean`(131) 하나가 스위치 다섯 개(모델 둘, 단계 수, cfg, 나누는 단계)를 바꾸는 구조다. 켜면 단계 4, cfg 1, 나누는 단계 2 다. 지난 시험 구성(`docs/video-renderer.md:116`)과 같게 켠 상태로 저장한다.
- 이 예시는 프레임 수를 직접 넣으므로 `129:161`(Duration)과 `129:163`(수식)을 지웠다. `129:162`(FPS 16)는 `CreateVideo` 가 쓰므로 남긴다.
- 검사 프레임용으로 `900`(ImageFromBatch)과 `901`(SaveImage)을 더했다. 둘 다 기본 노드다(ImageFromBatch [CUI] `comfy_extras/nodes_images.py:286-310`, SaveImage [CUI] `nodes.py:1669-1728`). 검사 프레임은 결과 영상과 같은 폴더에 `check_frame_00001_.png` 로 저장된다(파일 이름 규칙 [CUI] `nodes.py:1719`).
- `SaveVideo` 의 `format` 은 동적 콤보(DynamicCombo)이고, 고른 값의 하위 입력 `codec` 은 API 형식에서 `format.codec` 이라는 점 이름으로 들어간다. 백엔드가 이름을 점으로 이어 만들고([CUI] `comfy_api/latest/_io.py:1085-1091`, `2050-2062`) `execute` 는 `format` dict 의 codec 을 먼저 쓰고, 없을 때만 최상위 `codec` 을 쓴다([CUI] `comfy_extras/nodes_video.py:148-158`, `166-174`). 프런트엔드도 같은 규칙이다([FE] `src/core/graph/widgets/dynamicWidgets.ts:122`). `/object_info` 에서 `format.codec` 은 `format` 옵션의 `inputs` 안에만 있다(`_io.py:1234-1258`, `2084-2089`). 최상위에도 숨은 선택 입력 `codec` 이 따로 있지만(`nodes_video.py:158`) 점 이름 `format.codec` 과는 다른 입력이다. 그래서 V11 은 점 이름을 따로 다룬다(6.4). 개발 PC 0.42.3 기준이고, 메인 v0.42.5 는 [추정]이다.

```json
{
  "schema": "studio-recipe-1",
  "id": "wan22_i2v_14b_fast",
  "version": 1,
  "title": "Wan 2.2 14B 이미지→영상 · 빠름(4스텝 LoRA)",
  "quality_label": "빠름",
  "action": "i2v",
  "video_profile": "wan22_i2v_long",
  "negative_source": "node",
  "source": {
    "template": "video_wan2_2_14B_i2v",
    "templates_json_version": "TBD (메인 값)",
    "frontend_version": "TBD (메인 값)",
    "exported": "ComfyUI 메뉴 Export (API), enable_turbo_mode 켬",
    "removed_nodes": ["129:161", "129:163"]
  },
  "prompt": {
    "97":      {"class_type": "LoadImage", "inputs": {"image": "studio/_check/sample.png"}},
    "129:84":  {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
    "129:90":  {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
    "129:95":  {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "129:96":  {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors", "weight_dtype": "default"}},
    "129:101": {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors", "strength_model": 1.0, "model": ["129:95", 0]}},
    "129:102": {"class_type": "LoraLoaderModelOnly", "inputs": {"lora_name": "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors", "strength_model": 1.0, "model": ["129:96", 0]}},
    "129:131": {"class_type": "PrimitiveBoolean", "inputs": {"value": true}},
    "129:116": {"class_type": "ComfySwitchNode", "inputs": {"on_false": ["129:95", 0], "on_true": ["129:101", 0], "switch": ["129:131", 0]}},
    "129:117": {"class_type": "ComfySwitchNode", "inputs": {"on_false": ["129:96", 0], "on_true": ["129:102", 0], "switch": ["129:131", 0]}},
    "129:104": {"class_type": "ModelSamplingSD3", "inputs": {"shift": 5.0, "model": ["129:116", 0]}},
    "129:103": {"class_type": "ModelSamplingSD3", "inputs": {"shift": 5.0, "model": ["129:117", 0]}},
    "129:128": {"class_type": "PrimitiveInt", "inputs": {"value": 20}},
    "129:118": {"class_type": "PrimitiveInt", "inputs": {"value": 4}},
    "129:127": {"class_type": "PrimitiveInt", "inputs": {"value": 10}},
    "129:124": {"class_type": "PrimitiveInt", "inputs": {"value": 2}},
    "129:126": {"class_type": "PrimitiveFloat", "inputs": {"value": 3.5}},
    "129:122": {"class_type": "PrimitiveFloat", "inputs": {"value": 1.0}},
    "129:119": {"class_type": "ComfySwitchNode", "inputs": {"on_false": ["129:128", 0], "on_true": ["129:118", 0], "switch": ["129:131", 0]}},
    "129:120": {"class_type": "ComfySwitchNode", "inputs": {"on_false": ["129:126", 0], "on_true": ["129:122", 0], "switch": ["129:131", 0]}},
    "129:125": {"class_type": "ComfySwitchNode", "inputs": {"on_false": ["129:127", 0], "on_true": ["129:124", 0], "switch": ["129:131", 0]}},
    "129:93":  {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["129:84", 0]}},
    "129:89":  {"class_type": "CLIPTextEncode", "inputs": {"text": "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走", "clip": ["129:84", 0]}},
    "129:98":  {"class_type": "WanImageToVideo", "inputs": {"width": 640, "height": 640, "length": 81, "batch_size": 1,
                "positive": ["129:93", 0], "negative": ["129:89", 0], "vae": ["129:90", 0], "start_image": ["97", 0]}},
    "129:86":  {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "enable", "noise_seed": 0, "sampler_name": "euler", "scheduler": "simple",
                "start_at_step": 0, "return_with_leftover_noise": "enable", "model": ["129:104", 0], "positive": ["129:98", 0],
                "negative": ["129:98", 1], "latent_image": ["129:98", 2], "steps": ["129:119", 0], "cfg": ["129:120", 0],
                "end_at_step": ["129:125", 0]}},
    "129:85":  {"class_type": "KSamplerAdvanced", "inputs": {"add_noise": "disable", "noise_seed": 0, "sampler_name": "euler", "scheduler": "simple",
                "return_with_leftover_noise": "disable", "model": ["129:103", 0], "positive": ["129:98", 0], "negative": ["129:98", 1],
                "latent_image": ["129:86", 0], "steps": ["129:119", 0], "cfg": ["129:120", 0], "start_at_step": ["129:125", 0],
                "end_at_step": ["129:119", 0]}},
    "129:87":  {"class_type": "VAEDecode", "inputs": {"samples": ["129:85", 0], "vae": ["129:90", 0]}},
    "129:162": {"class_type": "PrimitiveFloat", "inputs": {"value": 16.0}},
    "129:94":  {"class_type": "CreateVideo", "inputs": {"images": ["129:87", 0], "fps": ["129:162", 0]}},
    "108":     {"class_type": "SaveVideo", "inputs": {"filename_prefix": "video/Wan2.2_i2v", "format": "auto", "format.codec": "auto", "video": ["129:94", 0]}},
    "900":     {"class_type": "ImageFromBatch", "inputs": {"image": ["129:87", 0], "batch_index": 40, "length": 1}},
    "901":     {"class_type": "SaveImage", "inputs": {"filename_prefix": "check_frame", "images": ["900", 0]}}
  },
  "inject": {
    "prompt":      {"node": "129:93", "input": "text", "type": "string", "role": "positive", "max_len": 8000},
    "first_frame": {"node": "97", "input": "image", "type": "input_image"},
    "seed":        {"node": "129:86", "input": "noise_seed", "type": "int", "min": 0, "max": 281474976710655},
    "frames":      {"node": "129:98", "input": "length", "type": "int", "min": 5, "max": 81, "step": 4, "offset": 1},
    "width":       {"node": "129:98", "input": "width", "type": "int", "min": 256, "max": 1024, "step": 16},
    "height":      {"node": "129:98", "input": "height", "type": "int", "min": 256, "max": 1024, "step": 16},
    "check_index": {"node": "900", "input": "batch_index", "type": "int", "min": 0, "max": 80},
    "save_prefix": [{"node": "108", "input": "filename_prefix", "type": "prefix", "suffix": "video"},
                    {"node": "901", "input": "filename_prefix", "type": "prefix", "suffix": "check_frame"}]
  },
  "fixed": {
    "negative": {"node": "129:89", "input": "text", "type": "string", "role": "negative"},
    "turbo":    {"node": "129:131", "input": "value", "type": "bool", "value": true}
  },
  "outputs": {
    "main":  {"node": "108", "kind": "video", "ext": [".mp4", ".webm"]},
    "check": {"node": "901", "kind": "image", "ext": [".png"]}
  },
  "defaults": {"width": 640, "height": 640, "fps": 16, "frames": 81, "size_rule": "keep_aspect_area", "size_step": 16, "max_side": 1024},
  "progress": {"bands": [
    {"key": "load",   "label": "준비",       "weight": 10, "nodes": ["97", "129:84", "129:90", "129:95", "129:96", "129:101", "129:102", "129:93", "129:89", "129:98"]},
    {"key": "high",   "label": "생성 1/2",   "weight": 40, "nodes": ["129:86"], "sampler": true},
    {"key": "low",    "label": "생성 2/2",   "weight": 40, "nodes": ["129:85"], "sampler": true},
    {"key": "finish", "label": "영상 만들기", "weight": 10, "nodes": ["129:87", "129:94", "108", "900", "901"]}
  ]},
  "inspect": {"output": "check", "when": "after_delivery"},
  "estimate": {"gpu_seconds": 90, "basis": "docs/video-renderer.md:116 — 640×640, 81프레임, 4스텝 LoRA 에서 약 1분 반"},
  "limits": {"max_run_seconds": 900}
}
```

자리별 설명:

| 자리 | 넣는 값 | 만드는 곳 |
|---|---|---|
| `prompt` | 최종 영문 프롬프트 (장문이면 네 단락) | 5.5 의 `final_prompt` |
| `first_frame` | `studio/<사용자 폴더>/<첨부 uuid>.png` (입력 폴더 기준) | 7.3 |
| `seed` | `secrets.randbits(48)`. 요청에 저장해 '다시 만들기'·재현에 쓴다. 패널도 48비트 난수다(`runtime/nodes/mgs_bridge/web/mgs_panel.js:800`) | 감독 |
| `frames` | `settings.frames` (4n+1, 최대 81) | `video.suggest_settings` (`hub/app/video.py:249-261`) |
| `width`, `height` | 첫 화면 비율을 따르고 면적은 640×640 과 비슷하게, 16의 배수 (6.6) | 감독 |
| `check_index` | `frames // 2` (가운데 프레임) | 감독 |
| `save_prefix` | `<사용자 폴더>/<요청 id 앞 8자>/video`, `…/check_frame` | 감독 → 허브가 앞에 `<작업공간>/<날짜>/` 를 붙인다(7.4) |

- 샘플러 두 개(`129:86` 고노이즈, `129:85` 저노이즈)가 각각 2단계씩 돈다(단계 4, 나누는 단계 2). 진행 가중치를 40·40 으로 같게 둔 이유다(8.3).
- 결과 노드 `108` 의 기록은 `images` 칸에 들어간다. `SaveVideo` 가 화면 출력을 `{"images": …, "animated": [true]}` 로 내기 때문이다([CUI] `comfy_api/latest/_ui.py:426-431`). 그래서 `job_outputs.media` 는 영상도 `images` 다(`hub/app/promptops.py:121-131`). 감독은 `media` 가 아니라 레시피의 노드 id 와 확장자로 결과를 고른다.
- 검사 프레임을 하위 폴더(`check/frame`)가 아니라 같은 폴더(`check_frame`)에 두는 이유: 스케줄러는 history 출력 가운데 **첫** 파일의 폴더에 설명 파일을 쓴다(`hub/app/scheduler.py:453-465`, `hub/app/promptops.py:118`). 출력 순서는 실행 순서인데, ComfyUI 는 출력 노드를 집합으로 모아 `108` 과 `901` 중 어느 쪽이 먼저일지 정해지지 않는다([CUI] `execution.py:1205`, `1232`, `1301`). 두 출력이 한 폴더에 있으면 설명 파일이 늘 요청 폴더에 생긴다.

### 6.4 등록과 검증 (패널의 그래프 추적을 옮긴 곳)

| # | 검사 | 방법 | 옮겨 온 패널 로직 |
|---|---|---|---|
| V1 | 형식 | 필수 칸, 타입, 자리 이름. `limits.max_run_seconds` 가 0 보다 크다. 영상 레시피는 `video_profile` 이 `video.profile_of` 로 열린다(모르는 프로필이면 요청마다 `PromptxError` 400 이 난다, `hub/app/video.py:234-239`) | |
| V2 | 자리가 있음 | `inject`·`fixed`·`outputs` 의 노드와 입력이 prompt 에 있고, 값이 리터럴이다 | |
| V3 | 워크플로우 종류 | `i2v`·`t2v` 레시피: `workflowKind` 와 같은 정규식으로 본 영상 종류가 `action` 과 같아야 한다. 참조 영상이 필요한 종류(control)는 거부한다. `image`·`edit` 레시피: 이 정규식은 i2v·t2v·control·unknown 만 내고 이미지·편집은 모두 unknown 이다. 그래서 영상 종류가 아니어야 한다는 것만 보고, 종류는 클래스 집합으로 따로 본다. `image` = `EmptyLatentImage`·`EmptySD3LatentImage` 계열이 있고 영상 노드가 없음. `edit` = `TextEncodeQwenImageEdit*` 계열과 `inject.edit_image` 의 LoadImage 가 있음 | `workflowKind` (`runtime/nodes/mgs_bridge/web/mgs_nodes.js:232-260`) |
| V4 | 긍정 자리 | `inject.prompt` 노드에서 링크를 따라 내려가(깊이 8) 이름이 `positive` 로 시작하는 입력에 닿아야 하고, `negative` 입력에는 닿지 않아야 한다. 예외: `negative_source` 가 `zero_out` 이면 0 조건 노드(`ConditioningZeroOut`)를 거쳐 negative 에 닿는 것은 허용한다(Z-Image). 패널 규칙은 이런 노드를 'mixed' 로 보고 고르지 않는다(`mgs_nodes.js:90-94`, `140-151`) | `roleDownstream`·`promptCandidates` (`runtime/nodes/mgs_bridge/web/mgs_nodes.js:41-78`, `101-137`) |
| V5 | 부정 자리 | `node`: `fixed.negative` 노드는 `negative` 에만 닿아야 하고 프롬프트 자리와 다른 노드여야 한다. `zero_out`: `fixed.negative` 가 없어야 하고, negative 로 가는 0 조건 노드의 입력이 프롬프트 자리 노드여야 한다 | `NegativeGuard` 의 목적 (`runtime/nodes/mgs_bridge/web/mgs_nodes.js:200-228`) |
| V6 | 첫 화면·편집 대상 자리 | `inject.first_frame` 은 LoadImage 이고 출력이 이름 `start_image` 인 입력에 닿아야 한다. `inject.edit_image` 는 LoadImage 이고 출력이 편집 글 노드(`TextEncodeQwenImageEdit*`)의 `image1` 과 `VAEEncode` 의 `pixels` 에 닿아야 한다(사이의 크기 맞춤 노드는 지나간다) | `findFirstFrame` 의 연결 규칙 (`runtime/nodes/mgs_bridge/web/mgs_panel.js:461-466`) |
| V7 | 저장 노드 | `outputs.main` 이 저장 노드이고 그 `filename_prefix` 가 `inject.save_prefix` 에 있다. 저장 노드가 여럿이면 모두 `save_prefix` 에 있고 같은 폴더에 저장한다(6.2) | |
| V8 | 시드 | 노이즈를 넣는 샘플러(`add_noise=enable`)의 `noise_seed`·`seed` 가 `inject.seed` 에 있다. 없으면 경고 | `rerollAndQueue` 가 `seed`·`noise_seed` 를 바꾸던 것 (`runtime/nodes/mgs_bridge/web/mgs_panel.js:796-805`) |
| V9 | 금지 노드 (파일만 보고) | Ollama 등 LLM 을 부르는 노드(이유는 4절. 거부할 class_type 이름 목록은 레시피를 넣을 때 그 작업공간의 노드로 정한다 [TBD]), LoadVideo 처럼 채울 수 없는 입력 노드, `inject` 자리가 아닌 LoadImage(템플릿의 예시 파일이 남아 V12 에서 떨어진다) | |
| V10 | 모델 파일 | `promptops.find_models` → `models_index.resolve` 로 공용 저장소에 있는지 (`hub/app/jobs.py:95-105` 와 같은 방법) | |
| V11 | 노드·입력 이름 (편집 공간 필요) | 채팅 작업공간 편집 공간의 `/object_info` 에 모든 class_type 이 있고 입력 이름이 있다. 편집 공간은 `--cpu` 로 뜨므로(`images/comfy-runtime/entrypoint.sh:68`) CUDA 가 있어야 불러와지는 노드는 '없는 노드'로 나온다(`hub/app/promptops.py:95-96`). 그래서 V12 와 같은 정책을 쓴다: `/object_info` 에 없는 class_type 은 `ACCEPT_MISSING_NODES` 가 true(기본, `hub/app/config.py:38`)면 경고로 `studio_recipe_checks` 에 남기고 그 노드의 입력 이름 검사는 건너뛴다(실행 공간에서 확인된다). false 면 실패다. 점 이름(`format.codec`)은 선택값을 지금까지의 점 이름 전체(`format`, `format.codec` …)로 prompt 입력에서 찾고, 고른 옵션의 `inputs.required`·`optional` 에서 다음 마디를 찾는다(백엔드도 이렇게 찾는다, [CUI] `comfy_api/latest/_io.py:1266-1281`, `2073-2089`). 지원하는 동적 형식은 `COMFY_DYNAMICCOMBO_V3` 하나다. 다른 동적 형식(Autogrow·DynamicSlot 등)의 점 이름은 V11 실패로 하고 레시피에서 그 노드를 지운다. API 노드(`api_node: true`, [CUI] `server.py:842-843`, `comfy_api/latest/_io.py:2014`. 옛 형식 노드는 `server.py:879-880`. 외부 서비스·Comfy 계정 키가 필요하고 키는 메모리에만 있다 `hub/app/jobs.py:21-22`)는 여기서 거부한다 | |
| V12 | ComfyUI 검증 | 표본 값을 넣은 prompt 를 편집 공간 `/mgs/validate` 로 보낸다 (`jobs._validate_in_editor`, `hub/app/jobs.py:61-75`; `runtime/nodes/mgs_bridge/__init__.py:39-57`). `missing_node_type` 만 있으면 통과 (`promptops.filter_node_errors`, `hub/app/promptops.py:92-112`) | |
| V13 | 진행 띠 | `progress.bands` 의 노드가 prompt 에 있고, 샘플러 노드가 `sampler` 띠에 있다 | |

API 형식에서 V4·V5 를 하는 법 (링크가 `[노드 id, 슬롯]` 이라 서브그래프 처리가 필요 없다):

```python
ZERO_OUT = {"ConditioningZeroOut"}

def roles_of(prompt: dict, start: str, depth: int = 0, seen: frozenset = frozenset(), zero: bool = False) -> set[str]:
    """start 노드의 출력이 닿는 입력 이름으로 positive/negative 를 모은다 (mgs_nodes.js roleDownstream 과 같은 규칙).
    0 조건 노드를 거쳐 닿은 것은 positive_zero/negative_zero 로 따로 센다 (Z-Image 처럼 글 노드 하나로 쓰는 구성)."""
    roles = set()
    for nid, node in prompt.items():
        for name, val in node["inputs"].items():
            if isinstance(val, list) and len(val) == 2 and str(val[0]) == start:
                if name.startswith("positive"):
                    roles.add("positive_zero" if zero else "positive")
                elif name.startswith("negative"):
                    roles.add("negative_zero" if zero else "negative")
                elif depth < 8 and nid not in seen:
                    roles |= roles_of(prompt, nid, depth + 1, seen | {nid}, zero or node["class_type"] in ZERO_OUT)
    return roles
```

- V4 통과 조건: `positive` 가 있고 `negative`·`positive_zero` 가 없다. `negative_zero` 는 `zero_out` 레시피에서만 허용한다.
- 예시 레시피에서 `129:93` 은 `129:98.positive` 에 닿아 `{"positive"}`, `129:89` 는 `{"negative"}` 다.
- Z-Image(6.8)의 `57:27` 은 `57:3.positive` 와, `57:33`(ConditioningZeroOut)을 거쳐 `57:3.negative` 에 닿아 `{"positive", "negative_zero"}` 다. 0 조건 노드를 구분하지 않으면 `{"positive", "negative"}` 가 되어 거부된다. 부정 글 노드가 없으므로 V5 와 접수 직전 부정 확인도 `zero_out` 규칙을 쓴다(5.5).
- Qwen 편집(6.8)의 `170:151` 은 `FluxKontextMultiReferenceLatentMethod` 를 거쳐 `170:169.positive` 에 닿아 `{"positive"}`, `170:149` 는 같은 길로 `{"negative"}` 다.

언제 검사하나:

| 때 | 검사 | 실패하면 |
|---|---|---|
| 허브 시작 (`studio.start()`) | V1~V10, V13 (파일만 보면 되는 것). 결과는 `studio_recipe_checks` 에 | 그 레시피를 끈다. 그 종류 요청에는 "이 기능은 지금 준비 중입니다" |
| 허브 시작 뒤 백그라운드, CLI `studio-check` (P2) | V11·V12 (편집 공간 필요) | 같음. 결과는 `studio_recipe_checks` 에 |
| 접수마다 | V2 + 부정 자리 확인(5.5). V12 에 해당하는 검증은 `submit_comfy` 가 어차피 한다(`hub/app/jobs.py:117-124`) | 접수하지 않음, 안내 |
| 같은 레시피로 접수 검증(400, 실행 공간의 접수 거부 포함)이 잇달아 실패 | `STUDIO_RECIPE_FAIL_LIMIT`(기본 3)번 | 레시피를 끄고 `studio_recipe_checks` 에 끔 행(ok=false, 사유 '잇단 접수 실패')을 남기고 허브 로그에 경고한다. 다시 켜는 길: 허브를 다시 시작해 시작 검사를 통과하면(통과 행이 끔 행보다 새로우면) 켜진다(P1 부터). P2 부터는 `studio-check` 통과로도 켜진다 |

- '채팅 작업공간 버전이 마지막 검증과 다를 때 접수 전에 V11' 은 하지 않는다. 버전이 바뀌었는지는 접수 전에도 `pin_version` 과 같은 방법(작업공간의 btrfs 세대 번호와 마지막 버전의 `btrfs_gen` 비교, `hub/app/envs.py:153-159`, `hub/app/hostops.py:34-41`)으로 알 수 있다. 그래도 접수마다 btrfs 조회와 `/object_info` 확인을 더하지 않는다. 노드가 바뀌어 레시피가 깨지면 `submit_comfy` 의 접수 검증이 어차피 잡고(`hub/app/jobs.py:117-124`), 위의 '잇달아 실패' 규칙으로 레시피를 끈다.
- 레시피 켬·끔은 허브 메모리에만 두지 않는다. 요청 때 시작 검사 결과와 `studio_recipe_checks` 의 최신 행으로 정한다. CLI 는 별도 프로세스라(`hub/app/cli.py:34-35`) 허브 메모리를 바꿀 수 없기 때문이다. 잇단 실패의 끔 행도 이 표에 남기므로, 그보다 새 통과 행(허브 재시작의 시작 검사, P2 부터 `studio-check`)이 있어야 다시 켜진다.
- '관리자 확인 표시'는 P1~P3 에서 허브 로그 경고와 `studio_recipe_checks` 의 끔 행, 그 요청의 `chat_events`(`error`·`notice`)다. 메일·메신저 알림은 없다. P4 부터 운영자 화면 목록 위에 모아 보인다(12절).
- 시작 검사와 1시간 정리 때 `/api/tags`(GPU 를 쓰지 않는 직통 경로, `hub/app/llm.py:33`, 선례 `hub/app/panel.py:61-77`)로 `PROMPT_EXPAND_MODEL`·`VIDEO_FRAME_MODEL`·`IMAGE_INSPECT_MODEL` 이 Ollama 에 있는지 본다. 없으면 그 모델을 쓰는 기능을 '준비 중'으로 끈다. 그러지 않으면 요청마다 대기열을 기다리고 GPU 차례(실행 공간 끄기 포함)를 쓴 뒤에야 502 로 실패한다(`hub/app/llm.py:295-297`, `hub/app/video.py:902-903`).
- CLI (P2): 관리자가 메인에서 `cd /opt/mgs-src && sudo docker compose exec -T hub python -m app.cli studio-check [레시피 id]` 로 돌린다(새 하위 명령, `hub/app/cli.py:117-141` 에 더한다. 기존 CLI 안내도 `sudo docker compose exec` 다, `docs/vpn-tailscale.md:118`). 보고를 출력하고 DB 에 저장한다.
  - V12 의 표본 값: 프롬프트 "a calm test scene", 시드 0, 기본 크기·프레임, `check_index` = 프레임 수 // 2, `save_prefix` = `_check/00000000/<suffix>`, 첫 화면·편집 대상은 `studio-check` 가 입력 폴더에 만드는 64×64 회색 PNG(`studio/_check/sample.png`)다. 예시 레시피의 `97.image` 값이 이 파일이다. V12 는 검증만 하고 실행하지 않으므로(`runtime/nodes/mgs_bridge/__init__.py:39-57`) 저장 파일은 생기지 않는다. 사용자 폴더 규칙(6.6)은 이 값에만 예외다.
- CLI (P2): `cd /opt/mgs-src && sudo docker compose exec -T hub python -m app.cli studio-import --id <id> --action i2v < export.json > recipe.json`. 허브 컨테이너에는 메인의 임의 경로가 연결돼 있지 않으므로(`compose.yaml:62-68`) 내보낸 API JSON 을 표준 입력으로 받고 레시피 뼈대를 표준 출력으로 낸다. V4·V6·V8 규칙으로 자리 후보를 제안한다. 관리자가 `recipe.json` 을 확인해 개발 PC 저장소의 `hub/app/studio/recipes/` 로 옮기고 커밋한다.

### 6.5 버전·모델 확인

- 레시피는 내보낸 그대로 얼린 prompt 다. 메인의 ComfyUI 나 템플릿 패키지가 바뀌어도 레시피는 저절로 바뀌지 않는다. 새 템플릿을 쓰고 싶을 때만 다시 내보낸다.
- 검증 기록(`studio_recipe_checks`)에 레시피 해시, 채팅 작업공간 버전 id 를 남긴다. 버전의 ComfyUI 커밋은 `env_versions.comfy_commit` 에 있다(`hub/app/schema.sql:69`, `hub/app/envs.py:174-185`).
- 모델: V10 은 시작 때 본다. 실행 직전에는 스케줄러가 접수 뒤 모델 파일이 바뀌었는지 다시 본다(`hub/app/scheduler.py:263-269`). 바뀌었으면 `rejected` 로 끝나고 감독이 안내한다(13절).
- 레시피 `version` 은 prompt 나 자리가 바뀔 때 올린다. 요청에는 쓴 `recipe_id`·`recipe_version` 을 저장한다(9.1).
- 다시 만들기나 승인 때 레시피 판이 요청에 저장된 판과 다르면(그 사이 배포): 다시 만들기는 새 판으로 새로 만들고(시드는 유지하지 않는다), 승인은 계획을 다시 만든다(5.7).

### 6.6 값 넣기

```python
def build(recipe: dict, values: dict) -> dict:
    prompt = copy.deepcopy(recipe["prompt"])        # submit_comfy 가 dict 를 그 자리에서 바꾼다 (jobs.py:70-72, 129-131)
    for name, slots in recipe["inject"].items():
        for slot in (slots if isinstance(slots, list) else [slots]):
            prompt[slot["node"]]["inputs"][slot["input"]] = check_value(slot, values[name])  # 타입·범위·step 검사
    for key, slot in recipe.get("fixed", {}).items():
        if "value" in slot and prompt[slot["node"]]["inputs"][slot["input"]] != slot["value"]:
            raise RecipeError(f"fixed.{key} 값이 레시피와 다릅니다")   # assert 는 python -O 에서 사라진다
    return prompt
```

| 형식 | 검사 |
|---|---|
| `string` | 문자열, `max_len` 이하, 비어 있지 않음 |
| `int` | 정수, `min`~`max`, `step` 이 있으면 `(값 - offset) % step == 0` (예: 프레임 4n+1) |
| `input_image` | 채팅 작업공간 입력 폴더 안의 실제 파일, 폴더 밖 경로 거부 |
| `prefix` | `<사용자 폴더>/<요청 앞 8자>/<suffix>`. 사용자 폴더는 사용자 이름이 `^[a-z][a-z0-9_-]{1,30}$`(`install/add-user.sh:11` 과 같은 규칙)에 맞으면 그 이름, 아니면 `u<사용자 id>` 다. CLI `create-user` 는 이름 형식을 검사하지 않는다(`hub/app/cli.py:36-47`) |

크기 규칙 (`keep_aspect_area`):

```python
def size_for(w0: int, h0: int, area: int = 640 * 640, step: int = 16, max_side: int = 1024) -> tuple[int, int]:
    ar = min(max(w0 / h0, 0.25), 4.0)            # 4:1 보다 긴 그림은 4:1 로 두고 나머지는 Wan 이 가운데를 잘라 맞춘다
    w, h = math.sqrt(area * ar), math.sqrt(area / ar)
    if max(w, h) > max_side:                     # 아주 긴 그림은 긴 변을 줄인다
        k = max_side / max(w, h)
        w, h = w * k, h * k
    return max(step, round(w / step) * step), max(step, round(h / step) * step)
```

- 예: 1:1 → 640×640, 16:9 → 848×480, 9:16 → 480×848, 4:3 → 736×560.
- Wan 의 폭·높이는 16 단위, 길이는 4 단위다([CUI] `comfy_extras/nodes_wan.py:28-30`). 첫 화면은 출력 크기에 맞춰 가운데를 자른다(`nodes_wan.py:47`). 비율을 맞추면 잘리는 부분이 거의 없다.
- 면적은 지난 시험 구성(640×640, `docs/video-renderer.md:116`)과 같게 둔다. 크기를 키우면 시간이 늘어난다 [추정]. 품질 단계는 사용자 결정이다(D7).
- `image`·`t2v` 는 글의 비율 지정(5.3)이 있으면 그 비율(세로 9:16, 가로 16:9, 정사각 1:1)로 `size_for` 를 부른다. 없으면 레시피 기본 크기다. `i2v`·`edit` 는 사진 비율을 따른다.

접수:

```python
client_id = f"studio-{turn_id}-g{attempt}"
await store.save_pending(turn_id, client_id, attempt, size, seed)   # 접수 전에 남긴다: 재시작·재시도 때 jobs 에서 찾는다 (5.10)
body = {"prompt": build(recipe, values), "client_id": client_id,
        "extra_data": {"studio": {"turn": str(turn_id), "recipe": recipe["id"], "version": recipe["version"]}}}
sockets.register(env["id"], client_id, receiver)          # 접수 전에 등록 (8.1)
sub = asyncio.ensure_future(jobs.submit_comfy(env, user, body, turn_ip))   # 참조를 쥔다: 취소돼도 접수는 끝까지 (5.9)
try:
    res = await asyncio.shield(sub)
except asyncio.CancelledError:
    try:
        res = await sub                                   # 접수가 끝나기를 기다려 id 를 받는다
    except Exception:
        res = await find_submitted(env["id"], client_id)  # 줄을 커밋한 뒤의 예외면 작업이 남아 있다 (아래)
        if res is None:
            raise asyncio.CancelledError from None        # 줄이 없으면 남은 작업이 없다
    await store.link_job(turn_id, res["prompt_id"], step="generate")
    if await jobs.cancel_queued(env["id"], [res["prompt_id"]]) == 0:
        await scheduler.interrupt(env["id"], res["prompt_id"])
    raise
except Exception:
    res = await find_submitted(env["id"], client_id)      # 줄을 커밋한 뒤의 예외면 접수된 것으로 보고 이어 간다
    if res is None:
        raise                                             # 접수 안 됨: 5.12 의 접수 오류 처리
await store.link_job(turn_id, res["prompt_id"], step="generate")   # chat_turn_jobs
# res = {"prompt_id", "number", "node_errors"}. find_submitted 도 같은 모양을 돌려준다 (아래)
```

- `extra_data` 는 `jobs.extra_data` 에 저장되고 실행 공간에도 넘어간다(`hub/app/jobs.py:133-141`, `hub/app/scheduler.py:334-337`). ComfyUI 는 `extra_data` 에서 정해 둔 키(`client_id`, `preview_method` 등)만 읽고 나머지는 저장만 한다([CUI] `server.py:1202-1227`, `execution.py:733-742`). 메인 판(v0.42.5)과의 차이만 [추정]이다.
- `client_id` 는 아무 문자열이나 된다. 진행 메시지는 `(env_id, client_id)` 로 등록된 곳으로만 간다(`hub/app/relay.py:44-50`, `hub/app/scheduler.py:312`, `400`).
- 이미 접수된 작업을 찾는 법(재시작·재시도 때): `SELECT id FROM jobs WHERE env_id = $1 AND client_id = $2`. 기존 색인 `jobs_env(env_id, seq DESC)`(`hub/app/schema.sql:118`)로 채팅 작업공간 줄만 훑으므로 `jobs` 에 색인을 더하지 않는다. `insert_job` 은 자체 트랜잭션으로 먼저 커밋하므로(`hub/app/jobs.py:47-57`), 접수가 끝난 뒤 `chat_turn_jobs` 를 쓰기 전에 허브가 멈춰도 이렇게 찾을 수 있다. 같은 이유로 `submit_comfy` 는 줄을 커밋한 뒤에도 예외를 낼 수 있다(`job_models` 기록이나 `notify_queue_changed` 실패, `hub/app/jobs.py:144-151`). 그래서 위 조각은 접수 예외가 나면 먼저 `find_submitted` 로 줄을 찾는다. `find_submitted` 는 위 조회에 `seq` 를 더해 `{"prompt_id": str(id), "number": seq, "node_errors": {}}` 를 만들고, 줄이 없으면 `None` 이다. 찾지 않으면 감독이 모르는 작업이 끝까지 돌고, [다시 시도] 때 같은 생성이 두 번 돈다.

### 6.7 API 형식을 얻는 법과 서브그래프 펼치기

얻는 법:

- **채택: 메인에서 "Export (API)" 한 번.** 관리자가 관리용 작업공간(관리자 자기 작업공간, 또는 studio-chat 의 버전을 복제한 작업공간, `envs.create_env` 의 `source_version_id`, `hub/app/envs.py:77-119`)의 ComfyUI 를 연다. 공식 템플릿을 연다(패널의 '시작 워크플로우', `runtime/nodes/mgs_bridge/web/mgs_panel.js:1033-1067`, 또는 ComfyUI 템플릿 메뉴). 4스텝 LoRA 를 켜고 메뉴의 Export (API) 로 저장한다. 그 파일을 `studio-import` 로 레시피 뼈대로 만들고 사람이 확인한 뒤, `studio-check` 로 studio-chat 의 편집 공간에서 V11·V12 를 돌린다(노드 구성이 다르면 여기서 잡힌다). 두 CLI 는 P2 부터다. P1 의 레시피 하나는 손으로 옮기고, V11·V12 는 메인 시험의 첫 접수 검증(`hub/app/jobs.py:117-124`)으로 대신한다.
  - studio-chat 의 ComfyUI 를 직접 여는 것은 노드를 설치할 때뿐이다. 관리자는 남의 작업공간을 열 수 있지만(`hub/app/proxy.py:59`), 그 화면의 대기열·이력에는 모든 채팅 사용자의 작업이 보이고(`hub/app/jobs.py:167-175`, `293-301`), '대기열 비우기'는 채팅 사용자 전원의 대기 작업을 취소하며(`hub/app/proxy.py:99-107`, `hub/app/jobs.py:313-316`), prompt_id 없는 '중단'은 실행 중인 채팅 작업을 멈춘다(`hub/app/proxy.py:108-111`, `hub/app/scheduler.py:495-505`). 관리자의 시험 실행 결과도 채팅 결과 폴더에 섞인다.
- **기각: 허브에서 UI JSON 을 API 형식으로 바꾸기.** 허브에 그런 코드가 없다. `promptops` 는 API 형식만 다루고(`hub/app/promptops.py:1-3`), `templates_index` 는 UI JSON 을 그대로 돌려준다(`hub/app/templates_index.py:358-364`). ComfyUI 백엔드도 API 형식만 검증한다([CUI] `server.py:1177-1201`). 위젯 순서, 서브그래프, Primitive, 음소거·건너뛰기, `control_after_generate` 같은 프런트엔드 규칙을 다시 만들어야 하고, 프런트엔드 판이 바뀌면 깨진다 [추정].
- **기각: 헤드리스 브라우저로 프런트엔드를 띄워 바꾸기.** 무겁고 프런트엔드에 묶인다.

서브그래프를 펼칠 때 일어나는 일 (개발 PC 프런트엔드 1.49.6 소스로 확인, 메인 판은 [추정]):

- 서브그래프 노드를 안쪽 노드들로 펼친다. 안쪽 노드 id 는 `서브그래프 노드 id 경로 + ":" + 안쪽 id` 다([FE] `src/lib/litegraph/src/subgraph/ExecutableNodeDTO.ts:113`). 겹친 서브그래프는 `1:2:3` 처럼 길어진다(같은 파일 60-67줄 설명).
- 서브그래프 노드 자체는 가상 노드라 API 형식에 나오지 않는다([FE] `src/lib/litegraph/src/subgraph/SubgraphNode.ts:63`, `src/utils/executionUtil.ts:86`).
- 서브그래프 바깥에 열어 둔 값(prompt, width, height…)은 안쪽 노드의 입력 값으로 들어간다([FE] `src/utils/executionUtil.ts:125-126`).
- `serialize` 가 꺼진 위젯(시드의 `control_after_generate` 같은 것)은 빠진다([FE] `src/utils/executionUtil.ts:101`). 화면이 해 주던 시드 바꾸기를 허브가 직접 해야 하는 이유다(`inject.seed`).

이 때문에:

| 영향 | 대응 |
|---|---|
| 값 넣을 자리는 펼친 id(`"129:93"`)로 적어야 한다 | 레시피는 펼친 뒤 기준으로만 적는다 |
| 진행 메시지의 노드 id 도 펼친 id 다(`progress.node` = `"129:86"`) | 진행 띠도 펼친 id 로 적는다(6.3) |
| 템플릿 판이 바뀌어 안쪽 id 가 바뀌면 자리가 틀린다 | 다시 내보내고 `version` 을 올린다. V2·V12 가 틀린 자리를 잡는다 |
| 결과 기록은 최상위 저장 노드 id(`"108"`)로 온다 | `outputs.main.node` 로 고른다 |

### 6.8 다른 레시피 (P2·P3)

| id (안) | action | 공식 템플릿 ([TPL] 판의 이름) | 넣는 자리 (펼친 id, 안) | 단계 |
|---|---|---|---|---|
| `zimage_turbo_t2i` | `image` | `image_z_image_turbo` | prompt `57:27.text`, seed `57:3.seed`, width·height `57:13`, save_prefix `9` | P2 |
| `wan22_t2v_14b_fast` | `t2v` | `video_wan2_2_14B_t2v` | prompt `128:89.text`, seed `128:81.noise_seed`, frames·width·height `128:74`, check_index `900`, save_prefix `80`·`901` | P3 |
| `qwen_image_edit` | `edit` | `image_qwen_image_edit_2511` | prompt `170:151.prompt`, edit_image `41.image`, seed `170:169.seed`, save_prefix `9` | P3 |

- 펼친 id 는 개발 PC 템플릿의 서브그래프 노드 id(57·128·170)와 안쪽 id 로 6.7 의 규칙대로 만든 안이다. 메인에서 내보낸 파일로 바꾼다 [TBD].
- 세 레시피 모두 메인에서 내보낸 뒤 V1~V13 을 통과해야 그 단계(P2·P3)가 끝난다(16절). 아래 표에서 채우지 못한 칸은 그 단계의 완료 기준과 함께 정한다.

`zimage_turbo_t2i` (P2):

| 항목 | 값 |
|---|---|
| 구조 | 최상위 `57`(서브그래프 "Text to Image (Z-Image-Turbo)") → `9` SaveImage. 안쪽: `27` CLIPTextEncode, `33` ConditioningZeroOut, `13` EmptySD3LatentImage(1024×1024), `3` KSampler(8스텝, cfg 1), `8` VAEDecode, 로더 `28`·`29`·`30`, `11` ModelSamplingAuraFlow ([TPL] `templates/image_z_image_turbo.json`) |
| `negative_source` | `zero_out`. 부정 글 노드가 없고 `fixed.negative` 도 없다. cfg 1 이라 부정 쪽 계산을 건너뛴다(5.5) |
| `defaults` | width 1024, height 1024, size_rule `keep_aspect_area`(면적 1024×1024, step 16, max_side 1536 [추정]) |
| `progress.bands` | 준비 15 (`57:30`, `57:29`, `57:28`, `57:27`, `57:33`, `57:13`, `57:11`), 생성 75 (`57:3`, sampler), 저장 10 (`57:8`, `9`) [추정] |
| `inspect.output` | `main` (결과 이미지 자체) |
| `estimate` / `limits` | gpu_seconds 60 [추정: '1분 안쪽', `docs/checklist-2026-10-27.md:24`] / max_run_seconds 300 [추정] |
| 지울 노드 | 없음 |

`wan22_t2v_14b_fast` (P3):

| 항목 | 값 |
|---|---|
| 구조 | 최상위 `128`(서브그래프 "Text to Video(Wan2.2)") → `80` SaveVideo. 안쪽: 긍정 `89`·부정 `72` CLIPTextEncode, `74` EmptyHunyuanLatentVideo(640×640, 81프레임), 샘플러 `81`(고노이즈, `add_noise` enable)·`78`(저노이즈), `87` VAEDecode, `88` CreateVideo, fps `125`(16), 길이 `126`(5초)·`127`(수식), 4스텝 켜기 `129` PrimitiveBoolean ([TPL] `templates/video_wan2_2_14B_t2v.json`) |
| `video_profile` | `wan22_t2v_long` (`hub/app/video_knowledge.json:108`) |
| `negative_source` | `node`. `fixed.negative` = `128:72.text`, `fixed.turbo` = `128:129.value` true |
| 지울 노드·더할 노드 | `128:126`·`128:127` 을 지운다(프레임을 직접 넣는다, 6.3 과 같다). 검사 프레임용 `900`(ImageFromBatch, `128:87` 에서)과 `901`(SaveImage, `check_frame`)을 더한다 |
| `defaults` | width 848, height 480 [추정: 글에 비율 지정이 없을 때 16:9], fps 16, frames 81, size_rule `keep_aspect_area`(면적 640×640) |
| `progress.bands` | 준비 10, 생성 1/2 40 (`128:81`), 생성 2/2 40 (`128:78`), 영상 만들기 10 (`128:87`, `128:88`, `80`, `900`, `901`) [추정] |
| `inspect.output` | `check` (가운데 프레임) |
| `estimate` / `limits` | gpu_seconds 90 [추정: I2V 와 같은 14B·4스텝] / max_run_seconds 900 [추정] |

`qwen_image_edit` (P3):

| 항목 | 값 |
|---|---|
| 구조 | 최상위 LoadImage 둘(`41` image ← 예시 `leather_sofa.png`, `83` image2 ← 예시 `texture_fur.png`, [TPL] `templates/image_qwen_image_edit_2511.json:57`, `103`) → `170` 서브그래프 "Image Edit (Qwen-Image 2511)" → `9` SaveImage. 안쪽: 긍정 `151`·부정 `149` TextEncodeQwenImageEditPlus, `160` FluxKontextImageScale(입력 크기를 맞춤), `156` VAEEncode, `169` KSampler, `158` VAEDecode, 4스텝 LoRA 켜기 `168` PrimitiveBoolean(템플릿 기본은 꺼짐: 40스텝, cfg 4) |
| 지울 노드 | `83` 과 안쪽 노드의 `image2` 입력(`170:151.image2`, `170:149.image2`). 편집 대상 한 장만 쓴다. 남겨 두면 예시 파일이 없어 V12 에서 떨어진다(V9) |
| `inject.edit_image` | `41.image`. V6 의 편집 규칙으로 검증한다 |
| `negative_source` | `node`. `fixed.negative` = `170:149.prompt` 빈 글, `fixed.turbo` = `170:168.value` true (품질 단계는 D7) |
| `defaults` | 크기 자리가 없다. 출력 크기는 입력 사진을 `FluxKontextImageScale` 이 맞춘 크기를 따른다 [추정] |
| `progress.bands` | 준비 15, 생성 75 (`170:169`, sampler), 저장 10 (`170:158`, `9`) [추정] |
| `inspect.output` | `main` |
| `estimate` / `limits` | gpu_seconds [TBD] (4스텝 기준 60 [추정]) / max_run_seconds 600 [추정] |

- 템플릿 이름과 구조는 메인 판에서 다시 확인한다 [TBD]. 4스텝·LoRA 같은 품질 단계도 레시피마다 정한다(D7).
- T2V 의 기본 크기(글에 비율 지정이 없을 때)는 [TBD]다.

## 7. 실행 환경

### 7.1 결정: 시스템이 관리하는 채팅 전용 작업공간 하나

| 안 | 좋은 점 | 나쁜 점 | 판단 |
|---|---|---|---|
| A. 사용자 자기 작업공간(`main`) | 결과가 자기 폴더에 쌓인다. 새 계정이 필요 없다 | 사용자가 설치·삭제한 노드와 Manager 변경이 레시피를 깨뜨린다. 채팅 사용자는 ComfyUI 를 보지 않으니 고칠 수 없다. 사용자마다 환경 버전이 달라 실행 공간을 매번 새로 띄운다(`hub/app/scheduler.py:277-278`). 그 사용자가 노드를 설치 중이면 접수가 최대 120초 기다리다 실패한다(`hub/app/jobs.py:86-92`). 채팅만 쓰는 사람에게도 작업공간(포트 30개 한도 `hub/app/config.py:33-34`, 리눅스·Samba 계정 `install/add-user.sh:15-31`)이 필요하다 | 기각 |
| **B. 시스템 채팅 작업공간 하나** | 관리자가 노드를 관리하므로 레시피가 재현된다. 한 번 검증하면 모두에게 맞다. 채팅 작업끼리 환경 버전이 같아 실행 공간을 다시 쓸 수 있다. 포트와 편집 공간을 하나만 더 쓴다 | 결과가 시스템 계정 폴더에 모인다(사용자별 하위 폴더로 나눈다, 7.4). A2 제안의 '사용자별 결과 폴더 그대로'와 다르다(D4). 시스템 계정이 하나 필요하다(`users.linux_uid` NOT NULL, `hub/app/schema.sql:9`) | **채택** |
| C. 사용자마다 숨은 채팅 작업공간 | 결과가 사용자 폴더에 | 포트·편집 공간이 사용자 수만큼 든다. 레시피 검증도 작업공간마다 | 기각 |
| D. 작업공간 없이 실행 | | 접수에는 편집 공간 검증과 스냅샷이 꼭 필요하다(`hub/app/jobs.py:117-131`) | 불가 |

- `jobs.user_id`(누가)와 `env_id`(어디서)는 서로 독립이다(`hub/app/jobs.py:139-141`, `hub/app/schema.sql:87-88`). 그래서 채팅 사용자를 `user_id` 로, 채팅 작업공간을 `env_id` 로 둘 수 있다. 실행 전 확인은 작업한 사용자의 `active` 만 본다(`hub/app/scheduler.py:253-255`).
- 그래서 시스템 계정 `studio` 를 사용 중지해도 채팅 작업은 돈다. 편집 공간 시작도 주인의 `active` 를 보지 않는다(`hub/app/envs.py:122-130`). 7.2 에서 사용 중지하는 이유다.

### 7.2 만드는 법 (관리자가 메인에서 직접 실행)

`install/add-user.sh` 의 단계(`install/add-user.sh:15-31`)를 따르되 Samba 계정과 `main` 작업공간은 만들지 않는다.

```bash
# 0) 저장소 폴더에서. 그룹 이름은 .env 의 MGS_GROUP (기본 comfyusers, .env.example:19). .env 는 root 만 읽는다
cd /opt/mgs-src && MGS_GROUP=$(sudo grep '^MGS_GROUP=' .env | cut -d= -f2)
# 1) 결과 파일 주인이 될 리눅스 계정 (로그인 쉘 없음)
sudo useradd --no-create-home --shell /usr/sbin/nologin --gid "$MGS_GROUP" --groups "$MGS_GROUP" studio
# 2) 허브 계정. 아무도 로그인하지 않으므로 비밀번호는 무작위로 만들고 적어 두지 않는다
openssl rand -hex 24 | sudo docker compose exec -T hub python -m app.cli --by <관리자> \
  create-user studio --display "채팅 스튜디오" --uid "$(id -u studio)" --password-stdin
# 3) 채팅 작업공간 (포트 하나, 편집 공간 하나를 쓴다)
sudo docker compose exec -T hub python -m app.cli --by <관리자> create-env studio chat
# 4) 로그인 막기. 채팅 작업은 그대로 돈다 (7.1)
sudo docker compose exec -T hub python -m app.cli --by <관리자> deactivate studio
# 5) 설정: STUDIO_ENV_SLUG=studio-chat, STUDIO_ALLOWED_USERS=<시범 사용자> 를 넣고 STUDIO_ENABLED=true 로 바꾼다 (19.3)
sudo nano .env
# 6) 허브를 다시 만들어 설정을 읽힌다 (허브는 시작할 때 한 번 읽는다).
#    먼저 관리자 화면에서 대기열을 일시 정지하고, 아래 status 의 active 가 0 이 된 뒤 다시 만들고 재개한다 (아래 설명)
sudo docker compose exec -T hub python -m app.cli status
sudo docker compose up -d --force-recreate hub
```

- 5)의 slug 는 `<사용자>-<이름>` 이다(`hub/app/envs.py:84`). `STUDIO_ENABLED` 의 기본값은 `false` 다(19.3). `update.sh` 는 .env 에 없는 새 항목을 기본값으로 두므로(`install/update.sh:77-81`) P1 코드를 먼저 배포해도 5)·6) 전에는 아무에게도 열리지 않는다.
- 6)의 허브 재생성은 실행 중인 작업을 기다리지 않는다. 준비·정리 중이던 작업과 실행 중인 LLM 작업은 `lost` 가 되고(`hub/app/scheduler.py:122-145`), 차례를 기다리던 LLM 요청(패널·API)은 끊긴다. 그래서 관리자 화면의 일시 정지(`hub/app/portal.py:364-367`)로 새 작업이 시작되지 않게 한 뒤, `app.cli status` 의 `active` 가 0 이 되면(`hub/app/cli.py:78-83`) 다시 만들고 재개한다. 기다리던 LLM 요청은 그래도 끊기므로 사용자가 적은 시간에 한다.
- 4)를 하는 이유: 누군가 `studio` 로 로그인하면 `studio-chat` 의 주인이 되어 모든 채팅 사용자의 업로드(입력 폴더)를 보고 대기열을 비울 수 있다(`hub/app/proxy.py:51-61`, `99-107`). 로그인은 active 계정만 된다(`hub/app/security.py:27`). 사용 중지는 세션도 끝낸다(`hub/app/cli.py:107-114`).
- 2)를 다시 실행하면 비밀번호를 덮어쓰고 active 를 다시 켠다(`hub/app/cli.py:40-47`). 그 뒤에는 4)를 다시 한다.
- 포털의 '내 작업공간'은 주인 기준이라 다른 사용자에게 보이지 않는다(`hub/app/portal.py:150`). 관리자 화면에는 보인다(`hub/app/portal.py:353`).
- 노드가 더 필요하면 관리자가 이 작업공간의 ComfyUI 를 열어 설치한다(6.7 의 주의를 따른다). 기본 Wan·Z-Image·Qwen 템플릿은 커스텀 노드가 필요 없다고 본다 [추정].
- 계정 이름 `studio` 와 uid 는 사용자 결정이다(D12).

### 7.3 업로드 입력 배치

| 항목 | 값 | 근거 |
|---|---|---|
| 저장 위치 | `<MGS_ENVS>/state/studio-chat/input/studio/<사용자 폴더>/<첨부 uuid>.png` | 입력 폴더 `settings.state_path(slug)/input` (`hub/app/config.py:98-99`), 편집 공간과 실행 공간이 같은 `/state/input` 을 본다 (`images/comfy-runtime/entrypoint.sh:49`) |
| LoadImage 값 | `studio/<사용자 폴더>/<첨부 uuid>.png` | 하위 폴더 값이 된다: LoadImage 는 `VALIDATE_INPUTS` 로 파일 존재만 보고 목록 검사를 건너뛴다 ([CUI] `nodes.py:1817-1821`, `execution.py:1032`, `folder_paths.py:369-379`). 기존 입력 고정도 하위 폴더 값을 쓴다 (`hub/app/promptops.py:76-80`) |
| 파일 형식 | 다시 인코딩한 PNG. 긴 변을 `STUDIO_UPLOAD_STORE_SIDE`(2048)로 줄여 저장한다(원본 보관 안 함) | 14.4 업로드 검사 |
| 주인·권한 | `studio` uid : gid 2000, 0660 | 입력 폴더는 2770 이다 (`hub/app/envs.py:65-69`). 허브는 root 라 쓰고 chown 할 수 있다 (`compose.yaml:55-57`, 선례 `hub/app/scheduler.py:476-477`) |
| 접수 때 | `pin_inputs` 가 `_mgs_pinned/<job_id>/studio/<사용자 폴더>/<uuid>.png` 로 작업 전용 사본을 만들고 끝나면 지운다 | `hub/app/promptops.py:57-82`, `hub/app/scheduler.py:479-488` |
| 첫 화면 읽기 | `video.frame_file` 이 `type: input` 으로 같은 파일을 읽는다 | `hub/app/video.py:397-403` |
| 보내지 않은 업로드 (`source='upload'`) | `STUDIO_UNSENT_HOURS`(24) 뒤 지운다 | 9.4 |

- 업로드는 작업공간·스냅샷과 같은 btrfs 볼륨(`MGS_ENVS`) 아래에 쌓인다(`hub/app/config.py:92-99`, `.env.example:7-8`). 볼륨이 차면 모든 사용자의 접수(입력 고정·스냅샷, `hub/app/jobs.py:127-130`, `hub/app/envs.py:162`)가 실패할 수 있다 [추정]. 그래서 여유 공간이 `STUDIO_MIN_FREE_GB`(기본 50 [추정]) 아래면 업로드를 거부하고(503 `disk_low`), 사용자별 업로드 용량을 `STUDIO_UPLOAD_QUOTA_MB` 로 막는다(14.4).
- 직전 결과를 입력으로 쓸 때(`source='copy'`)는 결과 파일을 입력 폴더로 복사한다(`hostops.copy_reflink`, `hub/app/hostops.py:77-86`).
  - 복사 전에 결과 파일의 sha256 을 결과를 받을 때 남긴 값(5.13)과 비교한다. 결과 폴더는 누구나 덮어쓸 수 있기 때문이다(`install/smb.conf:15-24`, `CLAUDE.md:23`). 다르면 복사하지 않고 "결과 파일이 바뀌어 이어서 만들 수 없습니다" 한 줄(13.1).
  - `[output]` 값으로 결과 폴더를 바로 가리키는 방법은 기각했다. `pin_inputs` 가 `[output]` 값을 고정하지 않고(`hub/app/promptops.py:68-72`), 결과 폴더는 누구나 지우고 고칠 수 있어(`CLAUDE.md:23`) 실행 전에 바뀔 수 있다.

### 7.4 결과 위치

- **A2 제안과 달라진 점**: 제안은 '사용자별 결과 폴더 그대로'였다. 채팅 작업은 시스템 작업공간에서 돌므로 결과가 `studio` 폴더 아래 사용자 하위 폴더에 쌓인다. 사용자는 `\\main\results\<나>` 가 아니라 `\\main\results\studio\chat\<날짜>\<나>\` 에서 찾는다. 채팅 화면의 첫 안내와 결과 카드 메뉴, 사용 안내 문서에 이 위치를 적는다(11.1, 11.2). 이 변경을 승인할지는 D4 다.
- 실행 공간의 결과 폴더는 `/srv/results/$MGS_USER` 이고 `MGS_USER` 는 작업공간 주인이다(`images/comfy-runtime/entrypoint.sh:50`, `hub/app/dockerctl.py:151`, `hub/app/scheduler.py:286-287`). 채팅 작업공간이면 `/srv/results/studio` 다.
- 레시피가 `filename_prefix = <사용자 폴더>/<요청 앞 8자>/video` 를 넣고, 허브가 앞에 `<작업공간 이름>/<날짜>/` 를 붙인다(`hub/app/promptops.py:24-33`, `hub/app/jobs.py:131`).

| 항목 | 예 (사용자 alice, 요청 id 1a2b3c4d…, 11-12 접수) |
|---|---|
| 결과 파일 | `/srv/results/studio/chat/2026-11-12/alice/1a2b3c4d/video_00001_.mp4` |
| 검사 프레임 | `/srv/results/studio/chat/2026-11-12/alice/1a2b3c4d/check_frame_00001_.png` |
| `job_outputs.rel_path` | `studio/chat/2026-11-12/alice/1a2b3c4d/video_00001_.mp4` (`hub/app/scheduler.py:457`) |
| 설명 파일(사이드카) | 같은 폴더의 `<job_id>.json` (`hub/app/scheduler.py:464-477`). 두 출력이 한 폴더라 늘 여기에 생긴다(6.3). 최종 영문 프롬프트가 들어 있다(14.2) |
| 네트워크 폴더 | `\\main\results\studio\chat\2026-11-12\alice\1a2b3c4d\` |
| 채팅 화면 | `/studio/api/files/<첨부 id>` (소유자 확인 뒤 내보냄, 10.1) |
| 공유 작업 이력 | `/history` 에 "alice · chat" 카드로 보인다 (`hub/app/portal.py:265-296`, `hub/app/templates/history.html:22`). 대화 글은 보이지 않는다 |

- 날짜는 실행일이 아니라 접수일이다(`hub/app/jobs.py:131`). 허브 이미지(`hub/Dockerfile:1-12`), compose 의 hub 서비스(`compose.yaml:47-75`), `.env.example` 어디에도 시간대 설정이 없다. 그래서 메인 `.env` 에 `TZ` 를 따로 넣지 않았다면 UTC 날짜이고, 한국 시각 00~09시 접수분이 전날 폴더로 간다. `.env` 에 `TZ=KST-9`(시간대 자료가 없어도 되는 POSIX 형식 [추정])를 넣을지는 D20 이다.
- 결과를 각자 폴더(`/srv/results/<사용자>/`)에도 복사하는 것은 D4 의 선택지다. P1~P4 의 기본은 복사하지 않는다.

### 7.5 스냅샷과 실행 공간

- 채팅 ComfyUI 작업도 접수 때 채팅 작업공간의 버전을 고정한다(`hub/app/jobs.py:127`, `hub/app/envs.py:148-171`). 작업공간이 바뀌지 않았으면 같은 버전 id 를 다시 쓴다(`hub/app/envs.py:158-159`).
- 채팅 ComfyUI 작업이 연달아 오고 그 사이에 LLM 작업이 없으면 실행 공간을 다시 쓴다(`hub/app/scheduler.py:277-278`). 실제로는 채팅 요청마다 LLM 단계가 앞에 있어 보통 새로 띄운다(5.6).
- 관리자가 노드를 설치하면 다음 채팅 작업은 새 버전이 되어 실행 공간을 한 번 새로 띄운다. 레시피 검사(V11·V12)는 관리자가 `studio-check` 로 다시 돌린다(P2). 그 전까지는 접수 검증이 매번 막고, 잇달아 실패하면 레시피를 끈다(6.4).
- 되돌리기는 포털의 버전 화면을 관리자가 쓴다(`hub/app/portal.py:238-260`).
- 접수에는 채팅 작업공간의 편집 공간이 켜져 있어야 한다(`hub/app/jobs.py:78-83`). 허브가 시작할 때 켜고(`hub/app/envs.py:122-130`), 재시작 정책이 `unless-stopped` 다(`hub/app/dockerctl.py:100`).

## 8. 진행률

### 8.1 진행 메시지를 받는 방법

- 스케줄러는 실행 공간의 메시지를 `status`·`feature_flags` 만 빼고 그대로 `relay.sockets.send_text/send_bytes(env_id, client_id, …)` 로 보낸다(`hub/app/scheduler.py:389-400`). 그 키로 등록된 곳이 없으면 버린다(`hub/app/relay.py:44-50`).
- 감독은 접수 **전에** 수신기 객체를 `sockets.register(채팅 작업공간 id, client_id, 수신기)` 로 등록한다(`hub/app/relay.py:14-15`). relay 는 `send_text`·`send_bytes` 두 메서드만 부르므로(`hub/app/relay.py:31-42`) 코드 수정 없이 받을 수 있다. 끝나면 `unregister` 한다(`hub/app/relay.py:17-23`).

```python
class TurnReceiver:
    """relay 가 부르는 가짜 소켓. 빨리 돌아오고 절대 예외를 내지 않는다."""
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue
    async def send_text(self, text: str):
        try:
            self.queue.put_nowait(text)        # 처리(계산·DB·전달)는 감독 작업이 한다
        except Exception:                      # relay 는 예외를 낸 소켓을 목록에서 뺀다 (relay.py:39-42)
            pass
    async def send_bytes(self, data: bytes):
        pass                                   # 미리보기 이미지는 P1 에서 버린다
```

- 수신기는 빨리 돌아와야 한다. relay 는 소켓마다 차례로 보내고 기다리므로(`hub/app/relay.py:33-38`) 느린 수신기는 스케줄러의 중계 루프와 완료 감지를 늦춘다.
- 대기열이 바뀔 때 허브가 모든 등록 소켓에 보내는 `status` 도 수신기에 온다(`hub/app/relay.py:55-58`). 대기 순번을 다시 셀 신호로 쓴다(8.4).
- 미리보기: 실행 공간은 `--preview-method` 없이 뜬다(`images/comfy-runtime/entrypoint.sh:45-51`). 프롬프트마다 `extra_data.preview_method` 로 정할 수 있다([CUI] `execution.py:734`). 채팅에 미리보기를 보일지는 P4 이후 [TBD]다.
- 허브가 다시 시작되면 스케줄러 시작 전에 `jobs.client_id`(`hub/app/schema.sql:91`)로 같은 키에 다시 등록한다(`studio.prepare()`, 5.10).

### 8.2 쓰는 ComfyUI 이벤트

메시지 모양은 개발 PC 의 ComfyUI 0.42.3 소스로 확인했다. 메인은 v0.42.5 다(`CLAUDE.md:34`). 두 판의 차이는 [추정]으로 둔다.

| type | data | 진행률에서 | 근거 |
|---|---|---|---|
| `progress_text` (허브) | `{"nodeId": "", "prompt_id", "text": "GPU 실행 공간 준비 중"}` | stage `prepare` | `hub/app/scheduler.py:314-315` |
| `execution_start` | `{prompt_id, timestamp}` | stage `generate`, 0% | [CUI] `execution.py:745` |
| `execution_cached` | `{nodes: [...], prompt_id}` | 그 노드들을 끝난 것으로 | [CUI] `execution.py:773` |
| `executing` | `{node, display_node, prompt_id}`. 끝날 때 `{node: null}`. 실행 공간 WebSocket 을 다시 붙이면 `{node}` 만 온다([CUI] `server.py:352-354`). 허브는 prompt_id 를 확인하기 전에 중계하므로 이 모양도 수신기에 온다(`hub/app/scheduler.py:385-388`, `398-402`) | 지금 노드. 앞 노드는 끝난 것으로. prompt_id 가 있으면 작업 id 와 비교하고, 없는 것은 지금 노드 보정에만 쓴다 | [CUI] `execution.py:497`, `main.py:392` |
| `progress` | `{value, max, prompt_id, node}` | 샘플러 띠 안의 비율 `value/max` | [CUI] `main.py:461-464` |
| `progress_state` | `{prompt_id, nodes: {id: {value, max, state, …}}}` | %를 다시 계산하는 기준 스냅샷 (8.3) | [CUI] `comfy_execution/progress.py:161-186` |
| `executed` | `{node, display_node, output, prompt_id}` | 결과 노드가 끝남 (파일은 DB 에서 다시 읽는다) | [CUI] `execution.py:580` |
| `execution_success` | `{prompt_id}` | 100%. 결과를 곧 고를 수 있다는 신호 (확정은 DB, 5.13) | [CUI] `execution.py:827` |
| `execution_error` | 실행 공간 형식 또는 허브 형식(`exception_type: "mgs"`) | 화면 신호만. 실패 확정은 DB outcome (5.2, 5.13) | [CUI] `execution.py:704-715`, `hub/app/scheduler.py:665-679` |
| `execution_interrupted` | `{prompt_id, node_id, …}` | 화면 신호만. 취소 확정은 DB outcome (5.2, 5.13) | [CUI] `execution.py:696-702` |

- 샘플러의 `max` 는 그 샘플러가 실제로 도는 단계 수다. `KSamplerAdvanced` 가 시작·끝 단계로 시그마를 잘라 쓰고(`comfy/samplers.py:1447-1454`), 단계 수를 자른 길이로 센 뒤(`comfy/samplers.py:995`) 진행을 `step+1 / total_steps` 로 보낸다(`latent_preview.py:139`, 모두 [CUI]). 그래서 4스텝 구성의 두 샘플러는 각각 `1/2 → 2/2` 를 보낸다. `4/4` 가 아니다.
- 진행 메시지는 0.1초 이상, 0.5% 이상 바뀔 때만 보내고 처음·끝·미리보기는 바로 보낸다([CUI] `comfy/utils.py:1303-1345`). 샘플러당 단계가 2개라 %가 큰 걸음으로 움직인다.
- 모델 적재, 텍스트 인코딩, VAE 디코드, 영상 저장은 진행 메시지를 보내지 않는다. 그래서 노드 단위로만 센다.
- 완료는 DB 로 확정한다(5.13). `execution_success`(또는 `executing` 의 `node: null`)는 '곧 끝남' 신호다. 받으면 `cleaning` 에서 바로 결과를 고르고, 받지 못하면 `finished` 와 outcome 을 기다린다. outcome 은 메모리 정리 뒤에 써진다(`hub/app/scheduler.py:225-231`).

### 8.3 % 계산식과 가중치

```
띠 b 의 가중치 W_b (레시피 progress.bands), 합 W = Σ W_b

띠 안의 진행 f_b
  - sampler 띠: 그 노드가 끝났으면 1, 실행 중이면 마지막 progress 의 value / max, 아니면 0
  - 그 밖의 띠: 끝난 노드 수 / 띠의 노드 수        (진행 메시지가 없는 노드들)

생성 % = floor(100 × Σ_b (W_b × f_b) / W)
  - 앞선 값보다 작아지지 않게 한다
  - execution_success 에서 100
  - 띠에 없는 노드(스위치·Primitive 등)는 세지 않는다
  - execution_cached 로 온 노드는 끝난 것으로 센다
```

- 두 Wan 샘플러에 가중치를 나누는 이유: 고노이즈·저노이즈 두 샘플러는 같은 크기(14B)의 모델로 같은 단계 수(4스텝 구성에서 2·2)를 돈다. 단계당 시간이 비슷하다고 보고 [추정] 같은 가중치를 준다. 단계 수가 다르면 단계 수에 비례하게 레시피에 적는다.
- 처음 가중치(준비 10, 생성 1/2 40, 생성 2/2 40, 영상 만들기 10)는 [추정]이다. P1 부터 노드별 시작·끝 시각을 `chat_events`(kind `node_timing`)에 남기고, P4 에서 레시피 판마다 실제 시간 비율의 중앙값으로 바꾼다.
- 첫 샘플러 단계에는 모델을 GPU 에 올리는 시간이 섞인다고 본다 [추정]. 그동안 %가 10에 머물 수 있어, 카드에 띠 이름("생성 1/2")과 경과 시간을 함께 보인다. 두 번째 샘플러 앞에서 저노이즈 14B 모델을 올리는 동안에도 50% 근처에서 멈출 수 있다 [추정].
- 증분 이벤트에만 기대지 않는다(재시작·놓친 이벤트에 약하다).
  - %는 `progress_state` 를 받을 때마다 그 스냅샷(대기 중이 아닌 모든 노드의 상태, [CUI] `comfy_execution/progress.py:161-186`)으로 다시 계산한다. `executing`·`progress` 는 그 사이를 채운다.
  - 띠 k 의 노드가 시작되면 앞 띠는 모두 끝난 것으로 본다. 띠에 넣은 노드가 띠 순서와 다르게 실행되거나 끝내 실행되지 않아도 막대가 멈추지 않게 한다.
  - 샘플러 띠가 아닌 노드의 `progress` 는 무시한다. 예: `VAEDecode` 가 메모리 부족으로 타일 디코드로 바뀌면 progress 를 보낸다([CUI] `comfy/sd.py:1308-1329`, `1178-1188`).

예시 (6.3 레시피, 4스텝):

| 이벤트 | 띠 상태 | 생성 % |
|---|---|---|
| `execution_start` | 모두 0 | 0 |
| 준비 노드들이 차례로 끝남 (`97` … `129:98`) | 준비 10개 중 n개 | 1 → 10 |
| `executing 129:86` | 생성 1/2 시작 | 10 |
| `progress 129:86 1/2` | 생성 1/2 = 0.5 | 30 |
| `progress 129:86 2/2` | 생성 1/2 = 1 | 50 |
| `executing 129:85`, `progress 1/2` | 생성 2/2 = 0.5 | 70 |
| `progress 129:85 2/2` | 생성 2/2 = 1 | 90 |
| `129:87` → `129:94` → `108` → `900` → `901` 이 차례로 끝남 | 영상 만들기 5개 중 n개 | 92 → 98 |
| `execution_success` | | 100 |

### 8.4 단계 이름과 대기 순번

종류(action)마다 진행 카드의 제목과 단계 칩:

| action | 진행 카드 제목 | 단계 칩 (순서) |
|---|---|---|
| (의도를 정하기 전) | 준비 중 | |
| `i2v` (P1) | 영상 만드는 중 | 첫 화면 읽기 · 프롬프트 작성 · (다듬기) · 생성 · 마무리 |
| `i2v` (P3 묶음) | 영상 만드는 중 | 첫 화면 읽기 · 프롬프트 작성 · 생성 · 마무리 |
| `i2v`·`t2v` revise | 영상 고치는 중 | 프롬프트 작성 · 생성 · 마무리 (첫 화면 읽기를 다시 쓰므로 칩이 없다) |
| `t2v` | 영상 만드는 중 | 프롬프트 작성 · 생성 · 마무리 |
| `image` | 이미지 만드는 중 | 프롬프트 작성 · 생성 · 마무리 |
| `image` revise | 이미지 고치는 중 | 프롬프트 작성 · 생성 · 마무리 |
| `edit` | 이미지 편집하는 중 | 편집 지시문 작성 · 생성 · 마무리 |
| 다시 만들기 (`repeat`) | 다시 만드는 중 | 생성 · 마무리 |
| `answer` | (카드 없음, 답 말풍선) | |

- (다듬기) 칩은 고치기 차례를 열 때만 생긴다.

단계마다 화면 글:

| 감독 stage | 이 단계 작업의 상태 | 화면 글 |
|---|---|---|
| (`pending`) | 같은 대화의 앞 요청이 도는 중, 또는 활성 한도 | 앞 작업이 끝나면 시작합니다 |
| `understand` (P4) | LLM 줄 `queued` | 대기 n번째 (프롬프트 작성 차례) |
| `understand` (P4) | `running` | 요청 이해 중 |
| `frame` | LLM 줄 `queued` | 대기 n번째 (첫 화면 읽기 차례) |
| `frame` | `running` | 첫 화면 읽는 중 |
| `prompt` | `queued` | 대기 n번째 (프롬프트 작성 차례) |
| `prompt` | `running` | 프롬프트 작성 중 (편집이면 "편집 지시문 작성 중") |
| `fix` | `queued` | 대기 n번째 (다듬기 차례) |
| `fix` | `running` | 프롬프트 다듬는 중 |
| `plan` | | 계획 확인을 기다리는 중 |
| `queue` | ComfyUI 줄 `queued` | 대기 n번째 (생성 차례) |
| `prepare` | `starting` | GPU 준비 중 |
| `generate` | `running` | 생성 n% (띠 이름: 준비 / 생성 1/2 / 생성 2/2 / 영상 만들기) |
| `collect` | `cleaning`·`finished` | 마무리 |
| `collect` | 600초 [추정]를 넘김 (5.12) | 마무리가 늦어지고 있습니다 |
| 아무 단계 | settings 표의 `paused` | 점검 중이라 새 작업이 잠시 멈췄습니다 |
| 아무 단계 | settings 표의 `blocked` | GPU 대기열이 멈췄습니다. 관리자가 확인 중입니다 |

- 이 표가 상태별 화면 글의 정본이다. 4절·11.4·13절은 이 표의 글을 쓴다. 화면 이름(D6)이 정해지기 전에는 화면 글에 '채팅 스튜디오' 같은 이름을 넣지 않고 "스튜디오"만 쓴다.

- 대기 순번 = 1 + (내 줄보다 순번이 작은 `queued` 줄 수)다. 종류와 사용자를 가리지 않는다. `panel.queue` 와 같은 셈이다(`hub/app/panel.py:155-164`). 목록은 `jobs.global_queue()` 로 읽는다(`hub/app/jobs.py:338-345`).
- 실행 중인 작업이 있으면 "앞에서 1건 실행 중"을 함께 보인다.
- 감독이 지금 어느 `jobs` 줄을 기다리는지는 이렇게 안다.
  - LLM 단계: `llm_turn` 이 줄을 넣자마자 컨텍스트 변수의 목록에 job id 를 넣는다(9.2). 감독의 진행 감시 작업이 같은 목록을 보고 그 줄의 상태를 읽는다.
  - P1 은 `video.render` 안에서 열린 차례를 목록의 `purpose` 로 단계에 맞춘다: `video_frame` → `frame`, `video_expand` → `prompt`, `video_fix` → `fix`, `image_inspect` → `inspect`. 줄의 상태(`queued`·`running`)로 위 표의 글을 고른다.
  - ComfyUI 단계: `submit_comfy` 가 돌려준 `prompt_id` 다(`hub/app/jobs.py:152`).
- 다시 셀 때: 허브는 접수·시작·끝·취소 때마다 `status` 를 방송한다(`hub/app/jobs.py:38-40`, `151`, `333-334`, `hub/app/scheduler.py:214`, `243`). 채팅은 `studio.start()` 에서 대기열 감시용 수신기 하나를 늘 등록해 두고(키는 `(0, "studio-queue")`, 방송은 키를 가리지 않는다 `hub/app/relay.py:57`), 방송이 오면 진행 중인 요청의 순번을 다시 세고 `jobs` 상태를 다시 읽는다. 방송을 놓쳐도 되게 대기 순번은 5초마다 한 번 더 센다(ComfyUI 화면 중계도 5초마다 센다, `hub/app/proxy.py:316-320`). `jobs` 상태는 이와 따로 5.13 대로 1~2초마다 읽는다.
- `paused`·`blocked` 는 같은 때 `db.get_setting` 으로 읽는다(`hub/app/db.py:72-74`).
- 다른 대화에서 요청이 끝나면 왼쪽 대화 목록의 그 대화에 점을 붙인다(11.1).

### 8.5 남은 시간 [추정]

| 구간 | 계산 | 단계 |
|---|---|---|
| 생성 중 | 생성 단계 경과 × (100 − p) / p. p ≥ 20 이고 10초 이상 지났을 때만 보인다 | P1 |
| 생성 전 | 내 앞 줄마다 종류별 중앙값을 더함: ComfyUI 는 최근 50건의 `cold_start_ms + run_ms + cleanup_ms`, LLM 은 최근 50건의 `llm_calls.duration_ms`(엔드포인트별) + 내 남은 단계의 중앙값(없으면 레시피 `estimate.gpu_seconds`) | P4 |

- 표시는 "약 n분 (예상)"으로 반올림한다. 초 단위는 보이지 않는다.
  - 러다이트도 예상 9분 30초에 실제 약 4분이었다(DFC/02:129). 예상임을 밝힌다.
- 원자료는 이미 쌓인다: `jobs` 의 시간 칸(`hub/app/schema.sql:106-110`), `llm_calls.duration_ms`(`hub/app/schema.sql:156`).

### 8.6 %를 '생성' 단계에만 보이는 이유

- 요청 전체를 하나의 %로 보이는 방법은 기각했다. 대기 시간이 대부분이고 예측하기 어렵다. 앞 작업 하나가 최대 4시간까지 갈 수 있다(`hub/app/config.py:42`). 전체 %는 오래 멈춰 있거나 크게 뛴다.
- 러다이트도 %가 아니라 개수와 단계 글, 예상 시간을 보였다(DFC/01:62, DFC/02:131).
- 그래서 잴 수 있는 곳(생성)에서만 %를 보이고, 나머지는 대기 순번·단계 이름·경과 시간으로 보인다.

### 8.7 전달

- WebSocket 이벤트 `turn.progress` 로 보낸다(10.2). 같은 요청에 대해 `STUDIO_PROGRESS_MIN_INTERVAL_MS`(기본 500) 안에 한 번만 보내고, 단계가 바뀌면 바로 보낸다.
- 마지막 상태를 `chat_turns.progress` 에 1초에 한 번 이하, 그리고 단계가 바뀔 때 저장한다. 진행률은 `jobs` 표 어디에도 없기 때문이다(`hub/app/schema.sql:83-116`). 새로 고침하거나 다른 기기에서 열어도 저장된 값으로 바로 이어 보인다.
- 이벤트마다 요청별 일련번호 `n` 을 붙인다. 화면은 받은 것보다 작은 번호를 버린다. `n` 은 `chat_turns.progress.n` 에 저장해 허브가 다시 시작해도 이어 쓴다. 화면은 `hello`·`snapshot` 을 받으면 요청별 기준을 snapshot 의 `n` 으로 새로 잡는다.

## 9. 데이터

### 9.1 DDL 초안

- `hub/app/schema.sql` 끝(256줄 뒤)에 새 구획으로 더한다. 허브가 시작할 때마다 실행되므로 모두 멱등으로 쓴다(`hub/app/schema.sql:173-176`, `hub/app/db.py:25-34`).
- 새 표의 CHECK 는 이름을 붙인다. 이름 없는 CHECK 는 표가 이미 있으면 `schema.sql` 을 고쳐도 반영되지 않고, 자동으로 붙은 이름을 찾아 따로 지워야 한다(`hub/app/schema.sql:86`, `219-221`).
- 허용값을 바꿀 때는 B3·B4 방식으로 한다. 새 이름(`…_check_v2`)은 `pg_constraint` 에 없을 때만 `NOT VALID` 로 붙인다(`hub/app/schema.sql:222-235`). VALIDATE 는 같은 블록에서 하지 않고, 기존 행 검사처럼 `BEGIN … EXCEPTION WHEN check_violation THEN RAISE WARNING … END` 로 감싼 블록에서 한다(`hub/app/schema.sql:237-250`). 검증에 성공했을 때만 옛 이름을 지운다. 새 이름은 `db.py` 의 `_GUARDS` 에 더한다(`hub/app/db.py:15-17`). 감싸지 않은 VALIDATE 가 기존 행 때문에 실패하면 `schema.sql` 전체가 한 트랜잭션이라 되돌려지고(`hub/app/db.py:32-34`) 허브가 시작하지 못한다(`hub/app/main.py:35-40`). 구획 규칙도 '기존 데이터가 어긋나도 허브 시작을 막지 않게'다(`hub/app/schema.sql:173-176`).
- `DROP CONSTRAINT IF EXISTS … ; ADD CONSTRAINT …` 를 그대로 두면 허브가 시작할 때마다 실행되어(`hub/app/db.py:29-34`) 매번 표를 잠그고 모든 행을 다시 검사한다.
- 지우는 표를 가리키는 FK(CASCADE·`ON DELETE SET NULL`)에는 색인을 둔다. 대화를 실제로 지울 때 표를 훑지 않게 하려는 것이다(기존 B5 규칙, `hub/app/schema.sql:252-256`). 허브가 지우지 않는 표(`jobs`·`envs`·`env_versions`)를 가리키는 FK 는 조회에 쓰는 칸(`chat_turn_jobs.job_id`, `chat_attachments.job_id`)에만 색인을 둔다.
- 이름 규칙은 기존 표를 따른다: 복수형 snake_case, `<대상>_id`, `*_at timestamptz`, 색인 이름 `<표>_<용도>`.

```sql
-- ── 2026-11 채팅 스튜디오 (docs/chat-studio-design.md 9절). 허브가 시작할 때마다 실행되므로 모두 멱등 ──
-- jobs 표는 바꾸지 않는다. 채팅 요청과 GPU 작업은 chat_turn_jobs 로 잇는다.

-- C1 대화. 사용자 한 명의 대화 묶음
CREATE TABLE IF NOT EXISTS chat_threads (
    id          uuid PRIMARY KEY,
    user_id     int  NOT NULL REFERENCES users(id),
    title       text NOT NULL DEFAULT '',
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"ask": "auto"|"ask_long"}
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz                            -- 사용자가 지운 때. STUDIO_PURGE_DAYS 뒤 실제로 지운다
);
CREATE INDEX IF NOT EXISTS chat_threads_user ON chat_threads(user_id, updated_at DESC) WHERE deleted_at IS NULL;

-- C2 요청 하나 = 감독의 작업 하나 (상태 기계, 5.2). 단계 결과를 저장해 재시작 뒤 이어 간다
CREATE TABLE IF NOT EXISTS chat_turns (
    id               uuid PRIMARY KEY,
    thread_id        uuid NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    user_id          int  NOT NULL REFERENCES users(id),
    client_ip        inet,
    state            text NOT NULL DEFAULT 'pending',
    stage            text,                  -- understand|frame|prompt|fix|plan|queue|prepare|generate|collect|inspect (코드에서만 정한다)
    action           text,                  -- image|i2v|t2v|edit|answer
    recipe_id        text,
    recipe_version   int,
    depends_on       uuid REFERENCES chat_turns(id) ON DELETE SET NULL,   -- 기대는 앞 요청 (이어 말하기)
    retry_of         uuid REFERENCES chat_turns(id) ON DELETE SET NULL,   -- 다시 만들기의 원래 요청
    request          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {text, attachment_ids, change, mode, spec: {seconds, aspect, sound}, source_turn}
    decision         jsonb,                 -- {action, source, carry, by: rule|llm, rule, request_ko, change_ko}
    work             jsonb,                 -- {frame, prompt, parts, ko, profile, length_choice, crew, violations, fix, settings, size, seed, pending: {client_id, attempt}}
    plan             jsonb,                 -- Ask 계획 카드
    progress         jsonb,                 -- 마지막 진행 상태 (8.7)
    result           jsonb,                 -- {attachment_ids, inspection, notices}
    error            jsonb,
    cancel_requested bool NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chat_turns_state_check
      CHECK (state IN ('pending', 'active', 'awaiting_approval', 'done', 'failed', 'cancelled', 'interrupted')),
    CONSTRAINT chat_turns_action_check
      CHECK (action IS NULL OR action IN ('image', 'i2v', 't2v', 'edit', 'answer'))
);
CREATE INDEX IF NOT EXISTS chat_turns_thread ON chat_turns(thread_id, created_at);
CREATE INDEX IF NOT EXISTS chat_turns_open   ON chat_turns(user_id) WHERE state IN ('pending', 'active', 'awaiting_approval');
CREATE INDEX IF NOT EXISTS chat_turns_depends ON chat_turns(depends_on) WHERE depends_on IS NOT NULL;   -- FK 삭제 검사용 (B5 규칙)
CREATE INDEX IF NOT EXISTS chat_turns_retry   ON chat_turns(retry_of)   WHERE retry_of IS NOT NULL;

-- C3 메시지. 진행 상태는 메시지가 아니라 chat_turns.progress 에 둔다
CREATE TABLE IF NOT EXISTS chat_messages (
    id             bigserial PRIMARY KEY,
    thread_id      uuid NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    turn_id        uuid REFERENCES chat_turns(id) ON DELETE SET NULL,
    role           text NOT NULL,
    kind           text NOT NULL DEFAULT 'text',
    body           text NOT NULL DEFAULT '',
    data           jsonb,                  -- result: {attachment_ids}, notice: {level, actions}, plan: {...}, 버튼 말풍선: {action, source_turn}
    client_msg_id  uuid,                   -- 같은 메시지를 두 번 보내지 않게 (브라우저가 만든다)
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chat_messages_role_check CHECK (role IN ('user', 'assistant')),
    CONSTRAINT chat_messages_kind_check CHECK (kind IN ('text', 'result', 'plan', 'notice'))
);
CREATE INDEX IF NOT EXISTS chat_messages_thread ON chat_messages(thread_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS chat_messages_client ON chat_messages(thread_id, client_msg_id) WHERE client_msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS chat_messages_turn ON chat_messages(turn_id) WHERE turn_id IS NOT NULL;
-- 요청을 만든 사용자 메시지 (표를 만드는 순서 때문에 나중에 붙인다)
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS message_id bigint REFERENCES chat_messages(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS chat_turns_message ON chat_turns(message_id) WHERE message_id IS NOT NULL;

-- C4 첨부: 올린 이미지(upload), 결과(result), 결과를 입력으로 복사한 것(copy)
CREATE TABLE IF NOT EXISTS chat_attachments (
    id          uuid PRIMARY KEY,
    thread_id   uuid NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    user_id     int  NOT NULL REFERENCES users(id),
    message_id  bigint REFERENCES chat_messages(id) ON DELETE SET NULL,
    source      text NOT NULL,
    media       text NOT NULL,
    mime        text NOT NULL,
    width       int,
    height      int,
    frames      int,
    bytes       bigint,
    sha256      text,
    input_rel   text,            -- 채팅 작업공간 입력 폴더 기준 (LoadImage 값). upload·copy
    result_rel  text,            -- 결과 폴더 기준 (job_outputs.rel_path 와 같은 형식). result
    job_id      uuid REFERENCES jobs(id),
    meta        jsonb,           -- {frame: 첫 화면 읽기 결과, check_of: 검사 프레임이면 본 결과 id, mtime: 결과 파일 수정 시각}
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,
    CONSTRAINT chat_attachments_source_check CHECK (source IN ('upload', 'result', 'copy')),
    CONSTRAINT chat_attachments_media_check  CHECK (media IN ('image', 'video')),
    CONSTRAINT chat_attachments_path_check   CHECK (input_rel IS NOT NULL OR result_rel IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS chat_attachments_thread ON chat_attachments(thread_id, created_at);
CREATE INDEX IF NOT EXISTS chat_attachments_message ON chat_attachments(message_id);
CREATE INDEX IF NOT EXISTS chat_attachments_unsent  ON chat_attachments(created_at)
    WHERE source = 'upload' AND message_id IS NULL AND deleted_at IS NULL;                                                     -- 보내지 않은 업로드 정리 (9.4). 결과·복사 행은 대상이 아니다
CREATE INDEX IF NOT EXISTS chat_attachments_user    ON chat_attachments(user_id) WHERE deleted_at IS NULL;                         -- 사용자별 업로드 용량 (14.4)
CREATE INDEX IF NOT EXISTS chat_attachments_job     ON chat_attachments(job_id) WHERE job_id IS NOT NULL;                          -- 결과 행을 작업으로 찾기

-- C5 요청 ↔ GPU 작업 (LLM 차례와 ComfyUI 작업 모두). jobs 는 허브가 지우지 않으므로 지우기 규칙을 두지 않는다
CREATE TABLE IF NOT EXISTS chat_turn_jobs (
    turn_id    uuid NOT NULL REFERENCES chat_turns(id) ON DELETE CASCADE,
    job_id     uuid NOT NULL REFERENCES jobs(id),
    step       text NOT NULL,                 -- understand|frame|prompt|fix|generate|inspect
    purpose    text,                          -- llm_turn 의 purpose (video_frame 등) 또는 레시피 id
    attempt    int  NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (turn_id, job_id)
);
CREATE INDEX IF NOT EXISTS chat_turn_jobs_job ON chat_turn_jobs(job_id);

-- C6 운영자 기록 (사용자에게 보이지 않는다, 12절). kind 는 access_log.event 처럼 CHECK 없이 코드에서 정한다
CREATE TABLE IF NOT EXISTS chat_events (
    id       bigserial PRIMARY KEY,
    turn_id  uuid NOT NULL REFERENCES chat_turns(id) ON DELETE CASCADE,
    ts       timestamptz NOT NULL DEFAULT now(),
    kind     text NOT NULL,
    detail   jsonb
);
CREATE INDEX IF NOT EXISTS chat_events_turn ON chat_events(turn_id, id);
CREATE INDEX IF NOT EXISTS chat_events_kind_ts ON chat_events(kind, ts);   -- node_timing 보관 기간 정리 (9.4)

-- C7 레시피 검증 기록 (6.4)
CREATE TABLE IF NOT EXISTS studio_recipe_checks (
    id             bigserial PRIMARY KEY,
    recipe_id      text NOT NULL,
    recipe_version int  NOT NULL,
    recipe_sha256  text NOT NULL,
    env_id         int REFERENCES envs(id),
    env_version_id int REFERENCES env_versions(id),
    ok             bool NOT NULL,
    report         jsonb NOT NULL,          -- 검사 항목(V1~V13)별 결과
    checked_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS studio_recipe_checks_recipe ON studio_recipe_checks(recipe_id, checked_at DESC);
```

- (P4, 사용자 결정 D1b 가 '채팅 사용자는 `/` 에서도 `/studio`'로 정하면) `users.home` 칸을 더한다. 작업공간이 있는 사용자도 `home='studio'` 를 고를 수 있다. 작업공간 없는 사용자를 받을지(D1a)와는 따로다.

```sql
-- C8 로그인 뒤 첫 화면: comfy(내 ComfyUI) | studio(채팅 스튜디오)
ALTER TABLE users ADD COLUMN IF NOT EXISTS home text NOT NULL DEFAULT 'comfy';
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_home_check') THEN
    ALTER TABLE users ADD CONSTRAINT users_home_check CHECK (home IN ('comfy', 'studio')) NOT VALID;
    ALTER TABLE users VALIDATE CONSTRAINT users_home_check;   -- 새 칸이 기본값 'comfy' 로 채워져 실패하지 않는다
  END IF;
END $$;
```

- `users_home_check` 는 붙인 바로 뒤에 VALIDATE 한다. `NOT VALID` 로만 두면 `convalidated=false` 로 남아 허브가 시작할 때마다 '검사 전' 경고가 난다(`hub/app/db.py:35-42`). 기존 행 검사 목록은 정해진 다섯 이름만 VALIDATE 하므로(`hub/app/schema.sql:237-250`) 그 목록에도 `users_home_check` 를 더한다. `db.py` 의 `_GUARDS` 에도 더해 빠지면 경고가 나게 한다(`hub/app/db.py:15-17`).

`chat_events.kind` 목록 (코드 상수):

| kind | detail |
|---|---|
| `turn_created` | 글 길이, 첨부 수 |
| `spec` | 글에서 뽑은 길이·비율·소리 지정 (5.3) |
| `decision` | action·source·carry, 규칙 번호 또는 LLM 답 |
| `step_started` / `step_done` | stage, 걸린 ms |
| `llm_job` | job_id, purpose, model, 끝난 이유 `end`(9.2 의 표), 감독이 취소했으면 `cancel_reason` |
| `render` | `video.render` 결과에서 profile·length_choice·crew·violations·violations_ko·rounds·advice·frame (프롬프트 전문은 `chat_turns.work` 에). 기본 요청문이라 제작진을 생략했으면 그 사유 |
| `expand` | 이미지 흐름(`promptx.expand_in_turn`) 결과에서 crew(분야·사용 여부·건너뛴 이유)·violations·advice·rounds (P2) |
| `fix` | before_ko·violations·words_before·words_after·rounds·changed·fixed (`hub/app/video.py:963-967`) |
| `negative_check` | 접수 직전 부정 자리 확인(5.5) 결과: `negative_source`, 통과 여부, 걸린 내용. 걸리면 접수하지 않는다 |
| `comfy_submitted` | job_id, seq, client_id, recipe·version, 넣은 값(시드·크기·프레임·저장 이름·첫 화면 첨부 id, 프롬프트는 `work.prompt`). 재현용 prompt 를 다시 만드는 데 쓴다(10.1). 재현 값의 정본은 `chat_turns.work` 다 |
| `comfy_event` | 시작·오류·중단 같은 중요한 메시지만 |
| `node_timing` | 노드 id, 시작·끝 (8.3 가중치 보정용) |
| `output` | rel_path, media, 고른 이유(노드·확장자) |
| `inspect` | 검사 결과 전체 (`hub/app/promptx.py:549-550`) |
| `notice` | 사용자에게 보낸 안내와 원인 |
| `retry` / `cancel` / `recovered` / `error` | 사유 |
| `deadline` | 넘긴 단계 기한과 단계 (5.12) |

### 9.2 jobs 와의 연결

- `jobs` 에 칸을 더하지 않고 연결 표 `chat_turn_jobs` 를 둔다.
  - 이유: `jobs` 는 대기열의 중심이다. 스케줄러 동작에 영향을 줄 수 있는 변경을 피한다. 허브 코드는 `jobs` 줄을 지우지 않으므로 연결 표만으로 충분하다.
- ComfyUI 작업 id 는 `submit_comfy` 가 돌려주는 `prompt_id` 다(`hub/app/jobs.py:152`). 그 전에 허브가 멈추면 `work.pending` 의 `client_id` 로 찾는다(6.6).
- LLM 작업 id 는 `llm_turn` 이 안에서 만들고 밖으로 알려 주지 않는다(`hub/app/llm.py:252-257`). `video.render` 처럼 `llm_turn` 을 안에서 여는 함수도 있다. 함수 인자를 모두 바꾸지 않으려고 **컨텍스트 변수**를 쓴다. asyncio 는 await 로 부른 함수에 컨텍스트를 그대로 넘긴다.

```python
# hub/app/llm.py 에 더하는 것 (P1). 패널이 부를 때는 컨텍스트 변수가 비어 있어 동작이 같다
import contextvars
STUDIO_JOBS: contextvars.ContextVar[list | None] = contextvars.ContextVar("studio_jobs", default=None)
STUDIO_WAIT: contextvars.ContextVar[int | None] = contextvars.ContextVar("studio_wait", default=None)
IN_TURN: contextvars.ContextVar[bool] = contextvars.ContextVar("in_turn", default=False)

class NestedTurnError(Exception):
    """차례 안에서 차례를 열려고 함. RuntimeError 가 아니다: 호출부가 RuntimeError 를 Ollama 실패(502)로 바꾼다"""

# llm_turn 맨 앞: 차례 안에서 차례를 열면 자기 차례를 자기가 기다린다 (5.6 규칙 5)
    if IN_TURN.get():
        raise NestedTurnError("llm_turn 안에서 llm_turn 을 열 수 없습니다")

# 대기자 등록부터 차례 기다리기까지를 한 try 로 묶는다 (llm.py:254-271 을 바꿈)
    entry = {"job_id": job_id, "purpose": purpose, "model": model, "end": None}
    scheduler.llm_waiters[job_id] = waiter                                 # 기존 254
    try:
        async with db.pool.acquire() as conn:
            await jobs.insert_job(conn, ...)                                   # 기존 255-257 그대로
        bag = STUDIO_JOBS.get()
        if bag is not None:   # 채팅 감독이 부른 차례면 job id 와 끝난 이유를 알려 준다 (진행 표시·운영자 기록·재시도 판단)
            bag.append(entry)
        scheduler.wake.set()
        await jobs.notify_queue_changed()                                    # 기존 258-259, 이제 try 안
        await asyncio.wait_for(waiter.go.wait(),
                               timeout=wait_seconds or STUDIO_WAIT.get() or settings.helper_wait_seconds)
    except BaseException as e:
        # 첫 await 전에 스케줄러를 풀어 준다. 이미 차례를 넘겼어도 done 을 보고 바로 정리로 간다 (5.9)
        waiter.cancelled = True
        scheduler.llm_waiters.pop(job_id, None)
        waiter.result = {"status": 499}
        waiter.done.set()
        entry["end"] = ("timeout" if isinstance(e, asyncio.TimeoutError)
                        else "caller_cancelled" if isinstance(e, asyncio.CancelledError) else "error")
        await asyncio.shield(jobs.cancel_queued(None, [job_id]))
        if isinstance(e, asyncio.TimeoutError):
            raise HelperBusy("GPU 대기열이 길어 차례를 받지 못했습니다. 잠시 뒤 다시 시도하세요.") from None
        raise
    if waiter.cancelled:      # release_waiter 로 깨어남: 관리자 취소 또는 사전 확인 거부 (기존 269-271)
        msg = waiter.result.get("message", "요청이 취소되었습니다.")     # 499 로 덮기 전에 꺼낸다 (패널 안내 그대로)
        scheduler.llm_waiters.pop(job_id, None)
        waiter.result = {"status": 499}
        waiter.done.set()
        entry["end"] = "cancelled"
        if await db.fetchval("SELECT outcome FROM jobs WHERE id=$1", uuid.UUID(job_id)) == "rejected":
            entry["end"] = "rejected"            # 그 밖(NULL·cancelled·error)은 관리자 취소로 본다 (아래 표)
        raise HelperBusy(msg)

# 차례 안 (yield 앞뒤). 기존 302-306 의 except 에 취소 기록을 더한다
    token = IN_TURN.set(True)
    try:
        yield call
    except BaseException as e:
        status = status if status >= 400 else 500                        # 기존 305
        if isinstance(e, asyncio.CancelledError):
            entry["end"] = "caller_cancelled"   # 감독의 실행 기한·사용자 취소·대화 삭제 (아래 표)
        raise
    finally:
        IN_TURN.reset(token)
        # 기존 정리(llm.py:307-321)는 try/finally 로 감싸 CancelledError 가 와도 마지막 waiter.done.set() 이 돈다
        # 그때 entry["end"] 가 비어 있으면 status 로 정한다: 400 미만이면 "done", 그 밖의 예외는 "error"

# 감독 쪽
bag: list = []
t_jobs, t_wait = llm.STUDIO_JOBS.set(bag), llm.STUDIO_WAIT.set(STUDIO_LLM_HARD_WAIT)
try:
    r = await video.render(...)        # 안에서 연 llm_turn 들이 bag 에 job id 를 넣는다
finally:
    llm.STUDIO_WAIT.reset(t_wait)
    llm.STUDIO_JOBS.reset(t_jobs)
```

끝난 이유(`end`)는 메시지 글이 아니라 빠져나간 길로 정한다:

| `end` | 길 | 감독이 보이는 안내 (13.1) |
|---|---|---|
| `caller_cancelled` | 부른 쪽이 작업을 취소함(`CancelledError`). 감독의 대기 기한·실행 기한·사용자 취소·대화 삭제가 모두 이 길이다(`hub/app/llm.py:262-267` 은 시간 초과와 취소를 한 except 로 받는다) | 감독이 들고 있는 취소 이유(5.9)로 고른다 |
| `timeout` | `llm_turn` 자신의 시간 초과. 채팅은 `STUDIO_LLM_HARD_WAIT`(24시간)라 거의 없다 | 대기 기한 넘김과 같다 |
| `cancelled` | `release_waiter` 로 깨어났고 그 jobs 줄의 outcome 이 `rejected` 가 아님. 대기 중 관리자 취소는 outcome 이 `cancelled` 다(`hub/app/jobs.py:314-326`). 관리자 취소가 차례 넘김과 겹치면 `cancel_queued` 가 0줄을 바꾸고도 `release_waiter` 를 불러(`hub/app/jobs.py:318-332`) outcome 이 아직 NULL 이고, 나중에 `error`(499)로 써진다 | 관리자 취소 |
| `rejected` | `release_waiter` 로 깨어났고 outcome 이 `rejected`. 사전 확인 거부는 outcome 을 먼저 쓰고 깨운다(`hub/app/scheduler.py:201-205`) | 사전 확인 거부 |
| `error` | 차례를 기다리다, 또는 차례 안에서 그 밖의 예외가 남(예: DB 조회 실패, Ollama 오류) | 예상하지 못한 오류. Ollama 오류면 LLM 호출 오류 |
| `done` | 차례를 받은 뒤 정상으로 끝남 | 없음 |

- 감독은 자기가 취소한 이유(`deadline`·`run_deadline`·`user`·`thread_deleted`)를 취소 플래그와 함께 들고 있다가 `chat_events.llm_job` 에 `cancel_reason` 으로 덧붙인다. 스케줄러는 바꾸지 않는다.
- 패널에서 부를 때는 컨텍스트 변수가 비어 있어(`None`, `False`) 동작이 바뀌지 않는다. 바뀌는 것은 빠져나가는 길에서 `waiter.done` 을 세우는 것뿐이다. 이는 스케줄러가 최대 4시간 기다리는 일을 막아 패널에도 이롭다. `waiter.cancelled` 길의 안내 글(`HelperBusy` 의 메시지)도 그대로다.
- 감독의 접수 함수도 `IN_TURN` 이 켜져 있으면 `NestedTurnError` 를 낸다(5.6 규칙 5). 감독은 이 예외를 '예상하지 못한 오류'(13.1)로 처리한다. `RuntimeError` 로 내면 호출부가 Ollama 실패로 보고 `PromptxError(502)` 로 바꿔(`hub/app/video.py:902-903`, `hub/app/promptx.py:501-502`, `547-548`) 원인이 가려지고, 결과 검사에서는 조용히 건너뛰어진다(5.13).

### 9.3 무엇이 언제 쌓이나

| 때 | 표 |
|---|---|
| 업로드 | `chat_attachments`(source `upload`, `message_id` 는 보낼 때 채움) |
| 메시지 보냄 | `chat_messages`(role `user`), `chat_turns`(`pending`), `chat_events`(`turn_created`) |
| 단계 진행 | `chat_turns.stage`·`work`·`progress`, `chat_turn_jobs`, `chat_events` |
| 접수 직전 | `chat_turns.work.pending` (client_id, attempt, 5.10) |
| 결과 | `chat_attachments`(source `result`, `job_id`), `chat_messages`(role `assistant`, kind `result`) |
| 안내 | `chat_messages`(kind `notice`), `chat_events`(`notice`) |
| 계획 카드 | `chat_turns.plan`, `chat_messages`(kind `plan`) |

### 9.4 보관과 삭제

| 대상 | 규칙 |
|---|---|
| 대화 | 사용자가 지울 때까지 둔다. 지우면 열린 요청을 먼저 취소하고(5.9) 바로 숨긴다(`deleted_at`). `STUDIO_PURGE_DAYS`(기본 30) 뒤 실제로 지운다 (CASCADE 로 요청·메시지·첨부 행·기록이 함께 지워진다) |
| 오래된 대화 자동 만료 | 두지 않는다. 기간을 정할지는 사용자 결정 (D5) |
| 올린 이미지 파일(입력 폴더) | 대화를 실제로 지울 때 함께 지운다. 대기 중인 작업은 접수 때 만든 사본을 쓰므로 영향이 없다(`hub/app/promptops.py:57-82`) |
| 결과를 입력으로 복사한 파일(`source='copy'`) | 올린 이미지와 같다 |
| 올리고 보내지 않은 업로드(`source='upload'`, `message_id` 없음) | `STUDIO_UNSENT_HOURS`(기본 24) 뒤 입력 폴더의 파일과 행을 지운다. 결과(`result`)·복사(`copy`) 행은 대상이 아니고, 결과 폴더의 파일(`result_rel`)은 어느 정리도 지우지 않는다 |
| 결과 파일(결과 폴더) | 채팅이 지우지 않는다. 결과 폴더는 공유 공간이고 삭제·덮어쓰기는 Samba 휴지통 규칙을 따른다(`CLAUDE.md:23`). 휴지통은 Samba 를 거친 삭제에만 쓰이므로(`install/smb.conf:26-32`) 허브가 지우면 남지 않는다 |
| 결과 파일 메타데이터·설명 파일 | 결과 파일과 같다. 대화를 지워도 남고, 최종 영문 프롬프트가 들어 있다(14.2) |
| `jobs`·`job_outputs`·`llm_calls` | 기존 규칙 그대로 (지우지 않는다). `jobs.prompt` 에도 최종 프롬프트가 남는다 |
| `chat_events` 의 `node_timing` | 요청마다 노드 수만큼(예시 레시피 약 30줄) 쌓인다. `STUDIO_EVENTS_KEEP_DAYS`(기본 90 [추정]) 뒤 `node_timing` 만 지운다. 나머지 기록은 대화와 함께 지운다 |
| `studio_recipe_checks` | 레시피마다 최근 50건만 남긴다 |
| 진행 메시지 원문 기록 (`STUDIO_RECORD_EVENTS`) | `STUDIO_RECORD_DIR`(비우면 `<MGS_ENVS>/state/<STUDIO_ENV_SLUG>/records`)에 쓰고 7일 뒤 지운다. 시험 때만 켠다 |
| 실제 삭제 작업 | 채팅 모듈이 1시간마다 돈다 (스케줄러의 정리 주기와 같다, `hub/app/scheduler.py:656-663`) |
| 백업 | 야간 `pg_dump` 가 아직 없다(`docs/checklist-2026-10-27.md:86`). 채팅 기록이 쌓이기 전에 먼저 넣기를 권한다 (D10) |

## 10. API

### 10.1 REST

- 모두 포털 포트 80 의 포털 앱에 더한다(`hub/app/portal.py:380-398`). 포트로 앱을 나누는 구조라 WebSocket 도 같은 앱으로 온다(`hub/app/main.py:45-47`).
- 경로를 `/chat` 이 아니라 `/studio` 로 둔다. 문서에서 "채팅 화면"은 이미 Open WebUI(8090)를 뜻하고(`README.md:19`, `docs/usage-guide.md:50`), `/api/chat` 은 LLM 포트의 Ollama 경로다(`hub/app/llm.py:29`).
- 인증은 로그인 쿠키 `mgs_sess` 다(`hub/app/security.py:11`, `42-61`). 로그인 전이면 API 는 401, 화면은 `/login?next=/studio` 로 보낸다(`hub/app/portal.py:48-54` 와 같은 방식). `STUDIO_ALLOWED_USERS` 에 없는 사용자는 403 이다(D16).
- GET 이 아닌 요청은 Origin 확인과 `Content-Type: application/json` 을 요구한다(14.3). 업로드만 `multipart/form-data` 다.
- 시각은 `ts.isoformat()` 으로 보낸다(선례 `hub/app/panel.py:148-149`). asyncpg 는 timestamptz 를 UTC 로 돌려주므로 `+00:00` 이 붙는다 [추정: asyncpg 동작, 개발 PC 에 설치돼 있지 않아 확인 못 함]. 화면이 브라우저 시간대로 바꿔 보인다.

| 메서드 | 경로 | 받는 값 | 돌려주는 값 | 단계 |
|---|---|---|---|---|
| GET | `/studio` | | 채팅 화면 HTML (`STUDIO_ENABLED=false` 거나 준비가 안 됐으면 503 안내 화면, 11.4) | P1 |
| POST | `/studio/logout` | | 204. 세션을 끝내고 쿠키를 지운다. 화면은 `/login?next=/studio` 로 옮긴다(11.1) | P1 |
| GET | `/studio/api/me` | | `{user: {username, display}, limits: {upload_max_mb, max_active, max_open}, features: {image, i2v, t2v, edit}}` (켜진 레시피) | P1 |
| GET | `/studio/api/threads` | `?before=<updated_at>&limit=30` | `{threads: [{id, title, updated_at, open_turns}]}` | P1 |
| POST | `/studio/api/threads` | `{title?}` | `{thread}` | P1 |
| PATCH | `/studio/api/threads/{id}` | `{title?, settings?: {ask}}` | `{thread}` | P1 (제목), P4 (ask) |
| DELETE | `/studio/api/threads/{id}` | | 204. 열린 요청을 취소한 뒤 숨긴다(5.9, 9.4) | P1 |
| GET | `/studio/api/threads/{id}/messages` | `?after=<메시지 id>&limit=100` | `{messages: [...], turns: [끝나지 않은 요청의 state·stage·progress·plan]}` | P1 |
| POST | `/studio/api/threads/{id}/messages` | `{text, attachment_ids: [], client_msg_id}`. 서버는 `text` 의 앞뒤 공백을 지워 저장한다. 지운 글은 비어도 되지만 그때는 첨부가 있어야 한다(5.3 R0). 지운 글이 2000자를 넘으면 400 `too_long`. 첨부는 0~1개. 제목이 빈 대화의 첫 메시지면 제목을 글 앞 30자로 정한다(글이 비면 제목을 비워 두고 화면은 "새 대화"로 보인다. P4 부터 `title_ko`, 5.3) | 202 `{message, turn}` | P1 |
| POST | `/studio/api/uploads` | multipart `thread_id`, `file`. `Content-Length` 필수 | 201 `{attachment: {id, media, width, height, bytes, url}}` | P1 |
| GET | `/studio/api/files/{attachment_id}` | | 파일 (주인 또는 관리자만). 결과 파일의 크기·수정 시각이 결과를 받을 때(`bytes`, `meta.mtime`)와 다르면 409 `file_changed` | P1 |
| GET | `/studio/api/turns/{id}` | | `{turn: {state, stage, progress, result, error}}` | P1 |
| POST | `/studio/api/turns/{id}/cancel` | | `{turn}` | P1 |
| POST | `/studio/api/turns/{id}/retry` | `{mode: "new_seed" \| "rewrite", client_msg_id}` | 202 `{message, turn}` (새 요청, `retry_of`, 13.2) | P1 |
| POST | `/studio/api/turns/{id}/to_video` | `{text?, client_msg_id}`. `text` 는 메시지와 같이 앞뒤 공백을 지운다. 비면 `DEFAULT_I2V_TEXT`(5.3 R0, 제작진 생략), 2000자를 넘으면 400 `too_long`. 원래 요청 글(`source_turn` 의 `request.text`)을 쓰는 안은 [TBD] | 202 `{message, turn}` ("이걸로 영상 만들기") | P2 |
| POST | `/studio/api/turns/{id}/approve` | | `{turn}`. 이미 처리된 요청이면 409 `state_changed` | P4 |
| GET | `/studio/api/queue` | | `{waiting, running, paused, blocked}` (개수만, 이름 없음) | P1 |
| GET | `/admin/studio` | `?user=&state=&from=&page=` | 운영자 목록 HTML (관리자) | P4 |
| GET | `/admin/studio/turns/{id}` | | 운영자 상세 HTML (관리자) | P4 |
| GET | `/admin/studio/turns/{id}/prompt.json` | | 재현용 API 형식 prompt (아래) | P4 |

- 재현용 prompt 는 레시피와 넣은 값(`chat_events.comfy_submitted`)으로 `build()` 를 다시 한 것이다. 입력 이미지 자리는 `chat_attachments.input_rel`, 저장 이름은 레시피 값이다. 실제로 접수한 `jobs.prompt` 는 그대로 다시 실행할 수 없다. 접수 때 LoadImage 값이 작업 전용 사본 경로 `_mgs_pinned/<job_id>/…` 로 바뀌는데 그 사본은 작업이 끝나면 지워지고(`hub/app/jobs.py:129-131`, `139-142`, `hub/app/scheduler.py:479-488`), 입력 고정은 `_mgs_pinned` 값을 다시 고정하지 않으며(`hub/app/promptops.py:68`), `filename_prefix` 에는 이미 `<작업공간>/<날짜>/` 가 붙어 있어 다시 접수하면 한 번 더 붙는다(`hub/app/promptops.py:24-33`).

오류 응답:

- 모양은 모두 `{"error": {"code": "…", "message": "한국어 한 줄"}}` 이다. 화면은 `message` 를 그대로 보인다.

| 상태 | code 예 | 언제 | 화면에 보이는 곳 |
|---|---|---|---|
| 400 | `empty_message`, `too_long`, `too_many_attachments`, `bad_request` | 글(앞뒤 공백을 지운 것)·첨부가 둘 다 없음, 글이 2000자를 넘음, 첨부 2장 이상, 잘못된 값 | 입력창 아래 |
| 401 | `login` | 세션 없음·만료 | 로그인 화면으로 보낸다(`/login?next=/studio`) |
| 403 | `origin`, `not_allowed`, `admin_only` | Origin 다름, 시범 사용자 아님, 관리자 아님 | 위쪽 띠 "요청을 처리할 수 없습니다. 새로 고침해 주세요". 시범 사용자 아님은 화면 전체 안내(11.4) |
| 404 | `not_found` | 남의 것이거나 없음 | 그 카드에 "찾을 수 없습니다" |
| 409 | `state_changed`, `file_changed` | 이미 처리된 요청에 승인·취소, 결과 파일이 바뀜 | 카드를 새 상태로 다시 그림 / 카드에 "결과 파일이 바뀌었습니다" |
| 411 | `length_required` | 업로드에 `Content-Length` 없음 | 입력창 아래 |
| 413 | `too_large` | 업로드가 한도를 넘음 | 입력창 아래 |
| 415 | `bad_type` | PNG·JPEG·WebP 가 아님 | 입력창 아래 |
| 429 | `too_many_messages`, `too_many_open`, `quota` | 분당 메시지, 열린 요청, 업로드 용량 한도 | 입력창 아래, [보내기]를 잠시 잠근다 |
| 503 | `studio_off`, `studio_not_ready`, `disk_low` | `STUDIO_ENABLED=false`, 채팅 작업공간이 없거나 시작 작업이 실패함(5.10), 업로드 볼륨 여유 공간 부족 | 화면 전체 안내 / 화면 전체 안내 / 입력창 아래 |
| 500 | `internal` | 예상하지 못한 예외 (원문은 허브 로그와 `chat_events`) | 입력창 아래 "잠시 뒤 다시 보내 주세요" |

메시지 보내기 예:

```http
POST /studio/api/threads/6f1c0c1e-…/messages
Content-Type: application/json
Origin: http://main

{"text": "이 조종석 사진으로 파일럿의 1인칭 비행 영상을 만들어줘",
 "attachment_ids": ["0b7d9a52-…"], "client_msg_id": "c2f4e1aa-…"}
```

```json
{"message": {"id": 812, "role": "user", "kind": "text", "body": "이 조종석 사진으로 파일럿의 1인칭 비행 영상을 만들어줘",
             "data": {"attachment_ids": ["0b7d9a52-…"]}, "created_at": "2026-11-12T05:03:11+00:00"},
 "turn":    {"id": "1a2b3c4d-…", "state": "pending", "stage": null}}
```

- 같은 `client_msg_id` 로 다시 보내면 처음 만든 메시지와 요청을 그대로 돌려준다(유일 색인 `chat_messages_client`). [다시 만들기]·[이걸로 영상 만들기]도 화면이 버튼마다 만든 `client_msg_id` 로 같다. [승인]·[취소]는 조건부 상태 전이(5.2)라 두 번 눌러도 한 번만 처리된다.
- 요청 상태 응답은 사용자에게 보일 값만 담는다. 프롬프트 전문, 위반 목록, job id 는 운영자 화면에서만 본다(G3, 12절). 다만 최종 영문 프롬프트는 결과 파일 메타데이터와 설명 파일로 결과 폴더에 공유된다(14.2).
- 사용자 행에는 `pw_hash` 가 들어 있다(`hub/app/security.py:47`). 사용자 정보는 칸을 골라 담는다(선례 `hub/app/panel.py:108-110`).

### 10.2 WebSocket `/studio/ws`

- 주소는 `ws://${location.host}/studio/ws` 다(Host 기준 링크 원칙, `docs/handover-2026-10-30-vpn.md:72-74`).
- 연결할 때 Origin 을 먼저 본다. 다르면 accept 하지 않고 거절한다(다른 출처에는 소켓을 열지 않는다). 그다음 쿠키로 사용자를 확인하고, 실패하면 **accept 한 뒤** 4401 로 닫는다.
  - accept 전에 close 하면 uvicorn 은 HTTP 403 으로 핸드셰이크를 거절하고, 닫힘 코드는 브라우저에 가지 않는다. 브라우저에는 보통 1006 으로 보인다(개발 PC uvicorn 0.40.0 `protocols/websockets/websockets_impl.py:286-295`, 허브의 0.32.1 도 같다고 본다 [추정]). 기존 ComfyUI 화면 WS 는 accept 전에 4401 로 닫으므로(`hub/app/proxy.py:259-263`) 브라우저에는 403/1006 으로 보인다.
  - 화면은 연결 실패가 이어지면 `GET /studio/api/me` 를 불러 401 이면 로그인 화면으로 보낸다. 닫힘 코드에만 기대지 않는다.
- 60초마다 세션을 다시 확인하고, 끝났으면 4401 로 닫는다. 관리자의 사용 중지는 DB 세션만 끝내므로(`hub/app/cli.py:107-114`) 열린 연결을 따로 닫아야 한다.
- 허브는 `사용자 id → 연결 목록` 을 프로세스 메모리에 둔다(허브는 한 프로세스, `hub/app/main.py:63-71`). 사용자의 모든 대화 이벤트를 그 사용자의 모든 탭에 보내고, 화면이 `thread_id` 로 거른다.
- 연결하면 서버가 `hello` 와 `snapshot`(끝나지 않은 요청 전부)을 보낸다. 끊겼다 다시 붙어도 이것으로 이어 보인다. 브라우저는 3초, 6초, 12초… 최대 30초 간격으로 다시 붙는다.
- 브라우저 → 서버 메시지는 없다. 행동은 모두 REST 로 한다.

| type | 예 |
|---|---|
| `hello` | `{"type": "hello", "user": "alice", "server_time": "2026-11-12T05:03:00+00:00"}` |
| `snapshot` | `{"type": "snapshot", "turns": [{"turn_id": "…", "thread_id": "…", "state": "active", "stage": "generate", "progress": {…}, "n": 41}]}` |
| `message.new` | `{"type": "message.new", "thread_id": "…", "message": {"id": 813, "role": "assistant", "kind": "result", "body": "", "data": {"attachment_ids": ["…"]}}}` |
| `turn.state` | `{"type": "turn.state", "turn_id": "…", "thread_id": "…", "state": "active", "stage": "prompt", "title": "영상 만드는 중"}` |
| `turn.progress` | 아래 |
| `turn.plan` | `{"type": "turn.plan", "turn_id": "…", "message_id": 814, "plan": {"kind": "i2v", "seconds": 5.0, "frames": 81, "fps": 16, "size": [848, 480], "quality": "빠름", "length": {"tier": "long", "reason": "…"}, "ko": {"camera": "…", "motion": "…", "timing": "…"}, "eta_s": 240}}` |
| `turn.result` | `{"type": "turn.result", "turn_id": "…", "message_id": 815, "items": [{"attachment_id": "…", "media": "video", "url": "/studio/api/files/…", "width": 848, "height": 480, "seconds": 5.0}]}` |
| `turn.notice` | `{"type": "turn.notice", "turn_id": "…", "message_id": 816, "level": "warn", "text": "검사에서 얼굴이 뭉개져 보입니다. 다시 만들어 볼까요?", "actions": [{"id": "retry_seed", "label": "다시 만들기"}]}` |
| `queue.state` | `{"type": "queue.state", "paused": false, "blocked": false}` |

- `actions` 의 id 는 `retry_seed`([다시 만들기], `mode=new_seed`)와 `retry_rewrite`([다시 시도], `mode=rewrite`) 둘이다(13.2).
- [다시 만들기]·[다시 시도]·[이걸로 영상 만들기]로 생긴 요청은 서버가 사용자 쪽 작은 말풍선(`data.action`)을 만들어 `message.new` 로 보낸다. 새 진행 카드는 그 말풍선에 붙는다(11.2).

```json
{"type": "turn.progress", "turn_id": "1a2b3c4d-…", "thread_id": "6f1c0c1e-…", "n": 41,
 "title": "영상 만드는 중", "stage": "generate", "label": "생성 1/2", "percent": 30,
 "queue": {"position": null, "running_ahead": 0},
 "elapsed_s": 143, "eta_s": 60,
 "steps": [{"key": "frame",    "label": "첫 화면 읽기",  "state": "done"},
           {"key": "prompt",   "label": "프롬프트 작성", "state": "done"},
           {"key": "generate", "label": "생성",          "state": "active", "percent": 30},
           {"key": "collect",  "label": "마무리",        "state": "todo"}]}
```

- `steps` 는 action 마다 8.4 표의 칩이다. 위 예는 P1 `i2v` 이고 고치기 차례를 열지 않은 경우다.

## 11. 채팅 화면

### 11.1 구성

- 한 장짜리 화면이다. 포털 메뉴(`hub/app/templates/base.html:51-57`)를 쓰지 않는다. 채팅만 보이게 하려는 것이다(G1).
- 색은 포털과 같은 토큰을 쓴다(`hub/app/templates/base.html:8-13`, 어두운 화면 포함).
- 왼쪽: [+ 새 대화], 대화 목록(제목·날짜, 승인 대기면 주황 점, 다른 대화에서 새 결과가 나오면 파란 점), 설정 "긴 영상은 먼저 계획 확인"(P4), 아래에 사용자 이름과 [로그아웃].
- [로그아웃]은 `POST /studio/logout` 이다(10.1). 세션을 끝내고 `/login?next=/studio` 로 보내므로 다시 로그인해도 채팅 화면으로 돌아온다. 포털의 로그아웃(`hub/app/portal.py:108-117`)은 `/login` 으로만 보내고, 그 뒤 로그인의 기본 `next` 가 `/` 라 ComfyUI 로 가기 때문이다(`hub/app/portal.py:90`, `93`, `135-142`).
- 가운데: 메시지 목록과 입력창. 폭이 좁은 화면(휴대폰)에서는 왼쪽 목록을 접는다.
- 처음 한 번 위쪽에 안내를 보인다: "결과(이미지·영상)는 공유 폴더 `\\main\results\studio\chat\<날짜>\<나>\` 에도 저장됩니다". 안내는 5초 뒤 저절로 사라지고 다시 보이지 않는다(7.4, 14.2, D3).
- 들어오는 길 (G1):
  - P1~P3: 주소 `http://<메인>/studio` 를 북마크로 나눠 준다. 로그인하지 않았으면 `/login?next=/studio` 로 가고, 로그인하면 `/studio` 로 돌아온다(`hub/app/portal.py:48-54`, `90-101`). 포털 메뉴에도 '스튜디오' 링크를 둔다(쓸 수 있는 사용자에게만, 11.5). ComfyUI 패널은 바꾸지 않는다(19.2).
  - 그냥 `http://<메인>/` 로 들어오면 P3 까지는 지금처럼 내 ComfyUI(작업공간이 없으면 `/portal`)로 간다(`hub/app/portal.py:135-142`). 이 길로 온 사용자는 ComfyUI 를 보게 된다.
  - P4: `users.home='studio'` 인 사용자는 `/` 에서도 `/studio` 로 간다(D1b). 이때 G1 이 모든 길에서 이뤄진다.

### 11.2 메시지 종류

```
[사용자 말풍선]                          [진행 카드]
┌──────────────────────────┐            ┌─────────────────────────────────────────────┐
│ [썸네일]                   │            │ 영상 만드는 중                     경과 2:13 │
│ 이 조종석 사진으로 …        │            │ ✓ 첫 화면 읽기 ✓ 프롬프트 작성 ● 생성 ○ 마무리 │
└──────────────────────────┘            │ 생성 1/2  ██████████░░░░░░░░░░  30%        │
                                        │ 남은 시간 약 1분 (예상)              [취소]  │
                                        └─────────────────────────────────────────────┘
[결과 카드]                              [계획 카드 (P4)]
┌─────────────────────────────┐         ┌─────────────────────────────────────────────┐
│ ▶ (영상 플레이어)              │         │ 영상 생성 계획                               │
│ 5초 · 848×480                 │         │ [이미지→영상] [5초] [848×480] [빠름]          │
│ [내려받기] [다시 만들기]        │         │ 글 길이: 장문 — 요청에 1인칭 시점이 있어 …    │
└─────────────────────────────┘         │ 카메라: … / 움직임: … / 시간: …               │
                                        │ 완료까지 약 4분 (예상)          [취소] [승인] │
[안내]                                   └─────────────────────────────────────────────┘
 검사에서 얼굴이 뭉개져 보입니다. 다시 만들어 볼까요?  [다시 만들기]
```

| 종류 | 보이는 것 | 보이지 않는 것 |
|---|---|---|
| 사용자 말풍선 | 글, 첨부 썸네일, 시각 | |
| 진행 카드 | 제목(무엇을 만드는지), 단계 칩, 생성 단계의 막대와 %, 대기 순번, 경과 시간, 남은 시간(예상), [취소] | job id, 모델 이름, 노드 이름 |
| 결과 카드 | 영상 또는 이미지, 길이·크기, [내려받기], [다시 만들기], 이미지면 [이걸로 영상 만들기], '…' 메뉴의 공유 폴더 위치(7.4) | 프롬프트, 위반 목록 (운영자 화면에서 본다) |
| 계획 카드 | 칩(종류·길이·크기·품질 `quality_label`), 글 길이와 이유, 한국어 계획 세 줄, 예상 시간, [취소]·[승인]. 끝나면 "승인함"·"취소됨"·"시간이 지나 취소됨"·"바꿔서 다시 계획함"으로 접힌다 | 영문 프롬프트, 모델 이름, 레시피 id, 스텝 수 |
| 안내 | 한 줄과 버튼 하나 (13절) | 오류 원문 |
| 답 | `answer` 의 짧은 글 | |
| 버튼 말풍선 | 사용자 쪽 작은 말풍선 "↻ 다시 만들기", "↻ 다시 시도", "▶ 이걸로 영상 만들기". 서버가 만들고 새 진행 카드가 붙는다(10.2, 13.2) | |

- 진행 카드는 끝나면 그 자리에서 결과 카드로 바뀐다. 실패하면 안내로 바뀐다.
- 결과 영상은 `<video controls preload="metadata">` 로 `/studio/api/files/<id>` 를 연다. 공유 이력 화면도 같은 방식이다(`hub/app/templates/history.html:29`).
- 사용자에게 프롬프트를 보이는 "자세히" 펼치기를 둘지는 사용자 결정 D9 다. 정하기 전에는 두지 않는다(G3).

### 11.3 입력

- 글: Enter 로 보내고 Shift+Enter 로 줄을 바꾼다. 앞뒤 공백을 지운 길이로 2000자까지다(넘으면 API 가 400 `too_long`, `hub/app/promptx.py:68-74` 와 같은 한도).
- 이미지: [첨부] 버튼, 끌어 놓기, 붙여 넣기. PNG·JPEG·WebP 한 장(P1~P3).
  - 붙여 넣기는 `paste` 이벤트로 받는다. 클립보드를 읽는 API 는 HTTPS 에서만 되는데 메인은 HTTP 다(`docs/handover-2026-10-30-vpn.md:90`). `paste` 이벤트는 HTTP 에서도 된다고 본다 [추정].
- 올리는 동안 썸네일에 진행 표시를 하고 [보내기]를 잠근다. 올린 이미지는 × 로 뺄 수 있다.
- [보내기]는 앞뒤 공백을 지운 글이 있거나 사진이 있을 때만 켜진다(서버와 같은 기준). 사진만 보내도 된다(5.3 R0).

### 11.4 상태별 화면

| 상태 | 화면 |
|---|---|
| 대화가 비어 있음 | 가운데 안내. P1 동안은 "사진을 올리고 원하는 움직임을 적어 주세요. 사진만 보내도 됩니다." (P1 은 글만 보내면 R1 안내가 나온다). P2 부터 "만들고 싶은 장면을 적어 주세요. 사진만 올려도 영상으로 만들어 드립니다." 입력창 안내 글도 같게 바꾼다 |
| WebSocket 끊김 | 위쪽 띠 "연결이 끊겨 다시 붙는 중입니다". 다시 붙으면 `snapshot` 으로 채운다. 실패가 이어지면 `/studio/api/me` 로 로그인 상태를 본다(10.2) |
| 세션 만료 | 로그인 화면으로 보낸다(`/login?next=/studio`). 쓰던 글은 브라우저에 임시로 남긴다 |
| 보내기 실패 (네트워크) | 그 말풍선에 "보내지 못했습니다 [다시 보내기]". 같은 `client_msg_id` 로 다시 보내므로 두 번 생기지 않는다 |
| 서버 오류 | 입력창 아래 한 줄 (10.1 오류 표) |
| 대기열 일시 정지 | 위쪽 띠 "점검 중이라 새 작업이 잠시 멈췄습니다" (settings 표의 `paused`, 글은 8.4) |
| 대기열 정지 | 위쪽 띠 "GPU 대기열이 멈췄습니다. 관리자가 확인 중입니다" (settings 표의 `blocked`, 글은 8.4) |
| 레시피가 꺼짐 | 그 종류 요청에 안내 "이 기능은 지금 준비 중입니다" (13.1) |
| 스튜디오 꺼짐 (`STUDIO_ENABLED=false`) | 화면 대신 "스튜디오가 잠시 꺼져 있습니다. 관리자에게 문의해 주세요" (503 `studio_off`) |
| 스튜디오 준비 중 (채팅 작업공간 없음, 시작 작업 실패, 5.10) | 화면 대신 "스튜디오를 준비 중입니다. 잠시 뒤 다시 열어 주세요" (503 `studio_not_ready`) |
| 시범 사용자 아님 | 화면 대신 "스튜디오는 아직 시범 중입니다" (403, D16) |
| 진행 중 요청이 있는 대화 지우기 | 확인 창 "진행 중인 작업이 있습니다. 대화를 지우면 작업도 취소됩니다." |

- 화면 글의 이름 "스튜디오"는 D6 이 정해질 때까지 잠정이다. 작업공간 없는 사용자(D1a)에게도 맞게 ComfyUI 화면을 안내하지 않는다.

### 11.5 만드는 법

- `hub/app/templates/studio.html`(Jinja, 껍데기만)과 `hub/app/static/studio.js`·`studio.css`(빌드 도구 없는 순수 JS). 포털도 빌드 도구 없이 템플릿만 쓴다(`hub/app/portal.py:17-18`).
- 화면에 넣는 글(사용자 글, LLM 이 만든 제목·답, 안내, 파일 이름)은 모두 `textContent` 로 넣는다. `innerHTML` 을 쓰지 않는다. 템플릿에서 `|safe` 를 쓰지 않는다. 화면에 넘기는 JSON 은 `<script type="application/json" id="boot">{{ boot|tojson }}</script>` 처럼 실행되지 않는 블록이나 data 속성에 넣고 `studio.js` 가 읽는다(14.3). 인라인 `<script>` 코드는 CSP 가 막는다.
- 정적 파일은 포털 앱에 `Mount("/studio/static", StaticFiles(...))` 로 낸다. 지금 포털에는 정적 파일 경로가 없다(`hub/app/portal.py:380-398`). `hub/app` 아래에 두면 이미지에 들어간다(`hub/Dockerfile:9`).
- 포털 메뉴에 "스튜디오" 링크를 더한다(`hub/app/templates/base.html:51-57`). `STUDIO_ENABLED` 가 켜져 있고 쓸 수 있는 사용자(P1 은 `STUDIO_ALLOWED_USERS` 에 든 사용자)에게만 보인다.
- 영상 탐색(앞뒤로 넘기기)에는 HTTP Range 응답이 필요하다. 개발 PC 의 Starlette 1.3.1 `FileResponse` 는 Range·HEAD 를 처리한다(`responses.py:318`, `342`, `361-379`). 허브의 0.41.3 이 같은지는 메인에서 확인한다(17.2) [추정].

## 12. 운영자 기록 화면

- 사용자에게는 보이지 않고 관리자만 본다. `admin_required` 로 막는다(`hub/app/portal.py:57-65`). 관리자 메뉴 옆에 링크를 둔다(`hub/app/templates/base.html:56`).
- 대화마다 접속 기록(`access_log`)에 쓰지 않는다. 관리자 화면의 최근 50줄이 채팅 기록으로 덮이기 때문이다(`hub/app/portal.py:354-355`). 채팅 기록은 `chat_events` 에 둔다.

목록 `/admin/studio`:

| 칸 | 출처 |
|---|---|
| 시각, 사용자, 대화 제목 | `chat_turns`, `users`, `chat_threads` |
| 종류(action), 상태, 단계 | `chat_turns` |
| 걸린 시간 | `finished_at - created_at` |
| GPU 차례 수, 대기 합, 실행 합 | `chat_turn_jobs` ⨝ `jobs` |
| 안내 여부, 재시도 여부 | `chat_events` (`notice`, `retry`) |
| 레시피 | `recipe_id`·`recipe_version` |

- 거르기: 사용자, 상태, 날짜. 50건씩.
- 관리자 확인이 필요한 요청(접수 검증 실패, 모델 파일 바뀜, 레시피 꺼짐 — 13절)은 목록 위에 따로 모아 보인다(P4. 그 전의 '관리자 확인 표시'는 6.4). 메일·메신저 알림은 없다. 관리자 화면을 열어야 보인다.

상세 `/admin/studio/turns/{id}` (시간 순):

| 칸 | 보이는 것 | 출처 |
|---|---|---|
| 요청 | 사용자 글, 첨부 썸네일, 시각, IP | `chat_messages`, `chat_attachments`, `chat_turns.client_ip` |
| 판단 | action·source·carry, 규칙 번호 또는 LLM 답 원문 | `chat_turns.decision`, `chat_events(decision)` |
| 첫 화면 읽기 | 대상·배경·사람 수·빛·색 | `chat_turns.work.frame` |
| 프롬프트 | 최종 영문 전문, 네 단락, 한국어 세 칸, 프로필 | `chat_turns.work` |
| 장문/보통을 고른 이유 | `tier`·`reason`·`by`(rule·llm) | `work.length_choice` (`hub/app/video.py:196-224`) |
| 제작진 | 분야, 사용 여부, 고른 항목, 건너뛴 이유 | `work.crew`, `advice` |
| 걸린 검사 | 위반(영문 + 한국어 풀이), 고치기 전후, 낱말 수, 회차 | `work.violations`, `work.fix` (`hub/app/video.py:749-761`, `963-967`) |
| 레시피와 넣은 값 | id·판, 시드, 크기, 프레임, 저장 이름, 마지막 검증 결과 | `chat_events(comfy_submitted)`, `studio_recipe_checks` |
| GPU 작업 | job id(ComfyUI 작업은 `prompt_id` 와 같다, `hub/app/jobs.py:112`, `152`), 순번, 종류, purpose, 상태·결과, 접수·시작·실행 시각, `wait_ms`·`cold_start_ms`·`run_ms`·`cleanup_ms`·`cleanup_method`·`vram_peak_mb` | `chat_turn_jobs` ⨝ `jobs` (`hub/app/schema.sql:101-112`) |
| LLM 호출 | 차례마다 걸린 시간, 토큰 수 | `llm_calls` (`job_id` 로, `hub/app/schema.sql:145-158`) |
| LLM 차례 끝난 이유 | `end`(`done`·`caller_cancelled`·`timeout`·`cancelled`·`rejected`·`error`, 9.2)와 감독의 취소 이유(`cancel_reason`) | `chat_events(llm_job)` |
| 안내 원문 | 렌더러·도우미의 `advice` 원문(사용자에게 보이지 않은 것 포함) | `chat_events(render)` |
| 노드 시간 | 노드별 시작·끝 | `chat_events(node_timing)` |
| 결과 | 파일 경로, 검사 결과 전체 | `job_outputs`, `chat_turns.result` |
| 안내·재시도·취소·오류 | 내용과 원인 | `chat_events` |
| 재현 | [API JSON 내려받기] — 레시피와 넣은 값으로 다시 만든 prompt(10.1). 관리자가 ComfyUI 에서 열어 다시 실행해 볼 수 있다. 실제로 접수한 `jobs.prompt` 는 비교용으로 함께 보인다(그대로는 다시 실행되지 않는다) | `chat_events(comfy_submitted)`, 레시피, `jobs.prompt` |

- 운영자 화면의 글(사용자 글, LLM 답 원문, 첫 화면 읽기 결과)은 모두 믿지 않는 데이터로 다룬다. 템플릿 자동 이스케이프만 쓰고 `|safe` 를 쓰지 않는다(14.3).

## 13. 실패와 안내

안내 글의 규칙:

- 한 줄, 한국어, 원인 + 제안 질문 하나("다시 만들어 볼까요?"). 버튼은 하나까지다.
- 영어 오류 원문, 노드 이름, job id 는 보이지 않는다. 원문은 운영자 기록에 남긴다.
- 버튼은 원인을 실제로 고칠 수 있는 가장 싼 것을 붙인다. 프롬프트가 원인이면 [다시 시도](처음부터), 생성 단계가 원인이면 [다시 만들기](같은 프롬프트, 새 시드)다. 뜻은 13.2.

### 13.1 안내 표

| 원인 | 감지 | 자동 처리 | 채팅 한 줄 | 버튼 |
|---|---|---|---|---|
| 빈 메시지 (공백뿐인 글 포함) | API 400 `empty_message` | 없음 | (입력창 아래) 보낼 글이나 사진이 없습니다. | |
| 첨부 없음 (P1) | 규칙 R1 | 없음 | 사진을 함께 올려 주세요. 사진을 영상으로 만들어 드립니다. | |
| 만들 뜻이 없는 말 (P2~) | 규칙 R2 | 정해 둔 답 (GPU 안 씀) | (답) 만들고 싶은 장면을 적어 주세요. 결과 설명과 프롬프트 보기는 아직 하지 않습니다. | |
| 참고 이미지 요청 (P2~) | 규칙 R2b | 글만으로 이미지 생성 | (결과 뒤) 참고 이미지는 아직 쓰지 못해 글만 보고 그렸습니다. | |
| 사진으로 만든 영상 뒤 편집 말 (P3~) | 규칙 R7b (직전이 `i2v`) | 정해 둔 답 (GPU 안 씀) | (답) 영상의 배경과 모습은 첫 화면 사진에서 정해집니다. 사진을 먼저 편집한 뒤 영상으로 만들어 주세요. | |
| 업로드 거부 | API 400·411·413·415 | 없음 | (입력창 아래) PNG·JPG·WebP 이미지, 20MB 이하만 올릴 수 있습니다. | |
| 업로드 용량·저장 공간 | API 429 `quota`, 503 `disk_low` | 없음 | (입력창 아래) 올린 사진이 많아 더 올릴 수 없습니다. / 저장 공간이 부족합니다. 관리자에게 알려 주세요. | |
| 메시지·열린 요청 한도 | API 429 `too_many_messages`·`too_many_open` | 없음 | (입력창 아래) 진행 중인 작업이 많습니다. 하나가 끝난 뒤 보내 주세요. | |
| 앞 요청·활성 한도 | 5.11 | `pending` 으로 기다림 | (진행 카드) 앞 작업이 끝나면 시작합니다 | |
| 레시피 꺼짐, 나중 단계 기능 | 6.4 (시작 검사 실패, 잇단 접수 실패, 모델 없음), 5.3 (단계 칸이 나중인 규칙) | 없음, 관리자 확인 표시(6.4) | 이 기능은 지금 준비 중입니다. | |
| 길이 지정이 5초를 넘음 | 5.3 | 5초로 만듦 | (결과 뒤) 한 번에 5초까지라 5초로 만들었습니다. | |
| 비율 지정과 사진 비율이 다름 | 5.3 | 사진 비율을 따름 | (결과 뒤) 영상 비율은 사진 비율을 따릅니다. | |
| 소리 요청 | 5.3 | 소리 없이 만듦 | (결과 뒤) 소리는 아직 넣지 못합니다. | |
| 글이 너무 김 | API 400 `too_long` (앞뒤 공백을 지운 글이 2000자를 넘음. `hub/app/promptx.py:68-74` 와 같은 한도) | 없음 | (입력창 아래) 글이 너무 깁니다. 2000자 안으로 줄여 주세요. | |
| LLM 차례 대기 기한 넘김 | 감독의 취소 이유 `deadline` (5.12). 24시간 `STUDIO_LLM_HARD_WAIT` 를 넘긴 `end=timeout` 도 같게 본다 | 다시 줄 서지 않음 | 오래 기다려도 차례가 오지 않아 멈췄습니다. 다시 시도할까요? | 다시 시도 |
| 관리자가 LLM 줄을 취소 | `end=cancelled` (9.2) | 없음 | 관리자가 작업을 취소했습니다. | |
| LLM 줄 거부 | `end=rejected` | 없음 | 이 계정으로는 지금 만들 수 없습니다. 관리자에게 문의해 주세요. | |
| LLM 호출 오류 | `PromptxError` 502 (`hub/app/video.py:902-903`) | 없음 | 프롬프트를 만드는 중 문제가 생겼습니다. 다시 시도할까요? | 다시 시도 |
| LLM 차례 실행 기한 넘김 | 감독의 취소 이유 `run_deadline` (5.12) | 감독 작업을 취소 | 프롬프트를 만드는 데 너무 오래 걸려 멈췄습니다. 다시 시도할까요? | 다시 시도 |
| 첫 화면을 못 읽음 | 첨부가 있는데 `video.frame_known(r["frame"])` 이 거짓 (5.5) | 글만 보고 계속 (렌더러 동작) | (결과 뒤) 사진을 읽지 못해 글만 보고 만들었습니다. 다시 시도할까요? | 다시 시도 |
| 심각한 위반이 남음 | 5.12 | 생성은 진행 | (결과 뒤) 위반 종류에 맞춘 글(5.12). 예: 글자·간판 표현을 자동으로 빼지 못한 채 만들었습니다. 프롬프트를 다시 써 볼까요? | 다시 시도 |
| 두 사람 요청인데 한 사람으로 보임 (이미지) | 검사의 인물 수 (5.12) | 없음 | (결과 뒤) 두 사람이 한 몸처럼 붙어 보입니다. 다시 만들어 볼까요? | 다시 만들기 |
| 편집 공간 꺼짐·준비 중, 노드 설치 중 | `SubmitError` 503, `httpx.HTTPError`, 400 `mgs_env_busy` (`hub/app/jobs.py:61-92`) | 60초 뒤 다시, 최대 5회 | (진행 카드) 준비 중 → 끝내 실패하면: 생성 준비가 아직 끝나지 않았습니다. 잠시 뒤 다시 시도해 주세요. | 다시 시도 |
| 접수 검증 실패 | `SubmitError` 400 + `node_errors` (`hub/app/jobs.py:118-124`). 또는 DB outcome `error` 이고 `exec_started_at` 이 없고 `error.node_errors` 가 있음(실행 공간의 접수 거부, `hub/app/scheduler.py:340-345`) | 기록, 관리자 확인 표시(6.4). 3번 잇달아면 레시피를 끈다 | 만들기 설정에 문제가 있습니다. 관리자가 볼 수 있게 기록했습니다. | |
| 실행 공간을 띄우지 못함 | DB outcome `error` 이고 `exec_started_at` 이 없고 `node_errors` 가 없음 (`hub/app/scheduler.py:316-320`). 허브의 `execution_error` 는 화면 신호만 | 없음 | GPU 를 준비하지 못했습니다. 다시 만들어 볼까요? | 다시 만들기 |
| 모델 파일이 바뀜·없어짐 | DB outcome `rejected`, 메시지가 '모델 파일이 접수 뒤 …'(`hub/app/scheduler.py:263-269`) | 관리자 확인 표시 | 필요한 모델 파일이 바뀌어 만들지 못했습니다. 관리자가 볼 수 있게 기록했습니다. | |
| 고정한 환경 버전이 없음 | DB outcome `rejected`, 메시지가 '접수 때 고정한 환경 버전이 없습니다'(`hub/app/scheduler.py:260-262`) | 관리자 확인 표시 | 만들기 환경에 문제가 있어 만들지 못했습니다. 관리자가 볼 수 있게 기록했습니다. | |
| 계정이 사용 중지됨 | DB outcome `rejected`, 메시지가 '비활성 사용자입니다'(`hub/app/scheduler.py:253-255`) | 없음 | 이 계정으로는 지금 만들 수 없습니다. 관리자에게 문의해 주세요. | |
| 실행 오류 | DB outcome `error` 이고 `exec_started_at` 이 있음 | 없음 | 생성 중 오류가 났습니다. 다시 만들어 볼까요? (메모리 부족이면 "GPU 메모리가 모자랐습니다") | 다시 만들기 |
| 실행 기한 넘김 | 레시피 `limits` (5.12) 또는 outcome `timeout` (`hub/app/scheduler.py:367-372`) | interrupt | 시간이 너무 오래 걸려 멈췄습니다. 다시 만들어 볼까요? | 다시 만들기 |
| 실행 공간이 사라짐 | DB outcome `lost` (`hub/app/scheduler.py:377-384`) | 5.10 (결과가 있으면 그대로 씀) | GPU 작업이 중간에 끊겼습니다. 다시 만들어 볼까요? | 다시 만들기 |
| ComfyUI 줄의 관리자 취소 | DB outcome `cancelled` 인데 감독이 취소하지 않음 (포털 관리자 취소, `hub/app/portal.py:372-375` → `hub/app/jobs.py:311-335`) | 없음 | 관리자가 작업을 취소했습니다. | |
| 결과 파일 없음 | DB 는 성공인데 레시피 main 출력이 없음 (5.13) | 없음 | 결과 파일을 찾지 못했습니다. 다시 만들어 볼까요? | 다시 만들기 |
| 결과 파일이 바뀜 | 크기·시각·sha256 이 다름 (7.3, 10.1) | 보이거나 복사하지 않음 | 결과 파일이 바뀌어 이어서 만들 수 없습니다. | |
| 결과 검사에서 문제 | 5.12 의 안내 기준 | 자동 재생성 없음 (기본) | (결과 뒤) 검사에서 얼굴이 뭉개져 보입니다. 다시 만들어 볼까요? | 다시 만들기 |
| 대기열 정지·점검 | `settings` | 기다림 (실패 아님, 대기 기한에 세지 않음) | (위쪽 띠) 11.4 | |
| 허브 재시작 | 5.10 | P1 은 멈춤, P2 부터 한 번 자동 재개 | 서버가 다시 시작되어 멈췄습니다. 다시 시도할까요? | 다시 시도 (`interrupted` 는 LLM 단계에서만 생긴다. ComfyUI 단계는 이어지거나 위 `lost` 줄을 따른다) |
| 사용자 취소 | 5.9 (감독의 취소 이유 `user`) | | (카드) 취소됨 | |
| 계획 카드 만료 | 5.7 | | (카드) 시간이 지나 취소됨 | |
| 예상하지 못한 오류 | 감독 코드의 예외 (감독 안에서 난 `PromptxError` 400, `NestedTurnError` 포함) | 기록(`chat_events.error`) | 만드는 중 문제가 생겼습니다. 다시 시도할까요? | 다시 시도 |

- ComfyUI 작업의 DB outcome 은 원인이 여럿이라 이렇게 가른다. `rejected` 는 `jobs.error.message` 의 앞부분(허브가 정한 문구, `hub/app/scheduler.py:255`, `262`, `267`, `269`)으로, `error` 는 `jobs.exec_started_at` 이 있는지(없으면 실행 전 실패)와 `error.node_errors` 가 있는지로, `cancelled` 는 감독이 취소를 요청했는지로 가른다.
- 검사 문제의 안내 여부와 한국어는 5.12 의 안내 기준 표를 따른다.

### 13.2 버튼과 다시 하기

| 버튼 | API | mode | 시작 단계 | LLM 차례 | 화면 |
|---|---|---|---|---|---|
| [다시 만들기] | `POST /studio/api/turns/{id}/retry` | `new_seed` | `queue`. 저장된 `work.prompt`·`settings`·`size` 로 시드만 새로 뽑는다 | 없음 (+ 결과 검사) | 대화 끝에 사용자 쪽 작은 말풍선 "↻ 다시 만들기"와 새 진행 카드 |
| [다시 시도] | 같음 | `rewrite` | `understand`. 같은 요청 글·첨부로 처음부터 하고, 첫 화면도 다시 읽는다 | 모두 | 말풍선 "↻ 다시 시도"와 새 진행 카드 |
| [이걸로 영상 만들기] | `POST /studio/api/turns/{id}/to_video` | | `understand` → `i2v`·`last_result` (결과를 입력으로 복사, 7.3) | 8B·27B | 말풍선 "▶ 이걸로 영상 만들기"와 새 진행 카드 |
| [취소] | `POST /studio/api/turns/{id}/cancel` | | | | 카드가 "취소됨"으로 접힘 |
| [승인] | `POST /studio/api/turns/{id}/approve` | | `queue` | | 카드가 "승인함"으로 접힘 |

- 새 요청은 원래 결과 카드 아래가 아니라 대화 끝에 붙는다. 같은 대화의 요청은 한 번에 하나씩 처리하므로(5.11) 앞 요청이 돌고 있으면 "앞 작업이 끝나면 시작합니다"로 기다린다.
- 같은 버튼을 두 번 눌러도 버튼마다 화면이 만든 `client_msg_id` 와 조건부 상태 전이(5.2)로 한 번만 처리한다.
- '같은 값(같은 시드)으로 다시'는 두지 않는다. 같은 실행 공간에 같은 prompt 를 다시 보내면 ComfyUI 캐시가 새 파일 없이 옛 결과를 돌려준다(18.2).

## 14. 보안·권한·제한

### 14.1 로그인과 사용자

- 로그인 화면과 세션은 포털 것을 그대로 쓴다(`hub/app/portal.py:88-105`, `hub/app/security.py:33-61`). 쿠키는 포트를 가리지 않아 ComfyUI 화면과 같은 로그인이다(`hub/app/portal.py:100`).
- P1 시범은 `STUDIO_ALLOWED_USERS`(쉼표 목록, 비우면 모두)에 든 사용자만 쓴다(D16). 채팅은 기본으로 꺼져 있고(`STUDIO_ENABLED=false`, 19.3), 관리자가 목록을 넣은 뒤 켠다(7.2 의 5·6). P2·P3 은 허브 계정이 있는 사람 누구나 쓴다.
- 들어오는 길은 11.1 이다. P1~P3 은 `/studio` 주소로 들어온다. 지금 `/` 는 작업공간이 있으면 내 ComfyUI, 없으면 `/portal`('작업공간이 없습니다')로 보낸다(`hub/app/portal.py:135-142`).
- 채팅 화면의 로그아웃은 `/login?next=/studio` 로 돌아온다(11.1).
- 로그인 첫 화면은 사용자 결정 D1b 다. '보냄'이면 P4 에서 `users.home='studio'` 인 사용자는 로그인 뒤와 `/` 에서 `/studio` 로 보낸다. 작업공간이 있는 사용자도 고를 수 있다.
- 채팅만 쓰는 사용자(작업공간 없음)를 받을지는 사용자 결정 D1a 다. 받으면 P4 에서:
  - `users.linux_uid` 가 NOT NULL 이라(`hub/app/schema.sql:9`) 계정을 만들 때 uid 가 필요하다. 채팅 결과 파일 주인은 `studio` 라서 이 uid 는 파일에 쓰이지 않는다 [TBD: 어떤 값을 줄지].
  - 그 사용자는 `home='studio'` 로 만든다(D1b 의 칸을 쓴다).
- CLI `create-user` 는 이름 형식을 검사하지 않는다(`hub/app/cli.py:36-47`). `install/add-user.sh:11` 의 규칙에 맞지 않는 이름이면 결과 하위 폴더 이름을 `u<사용자 id>` 로 쓴다(6.6).

### 14.2 사용자 사이 격리와 공개 범위

- `/studio/api/*` 의 모든 조회는 `user_id = 세션 사용자` 로 거른다. 남의 대화·요청·첨부 id 는 404 로 돌려준다(있는지조차 알리지 않게).
- 관리자는 운영자 화면에서만 남의 대화를 본다.
- 한국어 대화 원문은 본인과 관리자만 본다. 설계서의 기본 제안과 같다(`docs/main-gpu-server-design.html:480`).
- 결과 파일은 원칙대로 모두에게 열려 있다. 네트워크 폴더, 공유 작업 이력, 포털 `/files`(로그인만 확인, `hub/app/portal.py:299-306`)로 볼 수 있다(`CLAUDE.md:23`). 채팅의 파일 경로 `/studio/api/files/<id>` 는 화면 편의를 위한 것이고 비공개 보장이 아니다. 올린 사진이 담긴 결과 영상도 마찬가지다.
- **최종 영문 프롬프트는 숨긴다(D18 결정).** 숨기지 않으면 대화 글을 번역·확장한 것이라 내용이 드러난다. 지금 코드는 이렇게 남긴다.
  - 설명 파일 `<job_id>.json` 에 prompt 전체와 workflow 가 들어가고 0664 로 써진다(`hub/app/scheduler.py:467-477`).
  - ComfyUI 저장 노드가 결과 파일 메타데이터에 prompt 를 넣는다. PNG 의 `prompt` 글 조각([CUI] `nodes.py:1710-1716`), 영상 컨테이너 메타데이터([CUI] `comfy_extras/nodes_video.py:185-193`)다. 실행 공간은 `--disable-metadata` 없이 뜬다(`images/comfy-runtime/entrypoint.sh:45-51`, `76-78`).
  - 대화를 지워도 `jobs.prompt`, 결과 파일과 메타데이터, 설명 파일은 남는다(9.4).
- 숨기는 법(D18, 채팅 작업공간 `studio-chat` 의 작업에만): 실행 공간에 `--disable-metadata` 를 주어 결과 파일 메타데이터에 prompt 를 넣지 않게 하고, 설명 파일 `<job_id>.json` 에는 prompt 와 workflow 를 쓰지 않는다. `entrypoint.sh`·`scheduler.py` 를 고치는 일이라 19.2 의 '바꾸지 않는다'에서 이 두 곳은 제외한다(P1). 실행 공간은 작업공간이 달라도 하나를 돌려 쓰므로 `--disable-metadata` 를 작업마다 켜고 끄는 방법과 다른 작업공간 결과를 바꾸지 않는 방법은 [TBD]이다. 그 전에는 이 항목을 P1 완료로 보지 않는다. 숨겨도 `jobs.prompt` 는 DB 에 남고 관리자만 본다(12). 사용자의 어떤 작업공간 결과도 숨기지 않는다.

### 14.3 출처 확인과 믿지 않는 출력

- 지금 허브에는 CSRF 토큰도 Origin 확인도 없다. 패널 API 도 쿠키만 본다(`hub/app/panel.py:1-4`, `182-187`).
- 같은 호스트의 다른 포트(작업공간 8101~)에서 도는 페이지에는 사용자가 설치한 노드의 JS 가 함께 돈다(`hub/app/panel.py:213-214` 의 설명). 그 페이지가 포트 80 으로 요청을 보내면 쿠키가 실린다. 쿠키는 포트를 가리지 않고(RFC 6265 8.5절, 포털 주석 `hub/app/portal.py:100`), SameSite 는 포트가 아니라 사이트(스킴과 호스트)로 판단하기 때문이다.
- 그래서 채팅은:
  - POST·PATCH·DELETE 와 WebSocket 연결에서 `Origin` 이 `http://<요청 Host>` 와 정확히 같아야 한다. 없거나 다르면 403 이다(WS 는 accept 하지 않고 거절, 10.2).
  - JSON 요청은 `Content-Type: application/json` 이어야 한다. 다른 출처에서는 사전 요청(preflight)이 필요해지고 허브는 그에 답하지 않는다.
  - WebSocket 은 CORS 의 보호를 받지 않으므로 Origin 확인이 없으면 다른 포트의 페이지가 채팅 이벤트를 읽을 수 있다 [추정]. 그래서 연결 때 꼭 확인한다.
- 채팅 화면을 작업공간 포트가 아니라 포트 80 에 두는 이유도 이것이다. 작업공간 포트에 두면 ComfyUI 페이지와 같은 출처가 되어 노드 JS 가 응답까지 읽을 수 있다.

LLM·VLM 출력은 믿지 않는 데이터로 다룬다 (프롬프트 인젝션):

- 사용자 글과 올린 사진 속 글자가 모델을 거쳐 저장되고 화면에 보인다. 8B 첫 화면 읽기의 `subject` 같은 칸(각 160자, `hub/app/video.py:432-446`), 27B 판단의 `title_ko`·`reply_ko`, LLM 답 원문이 그렇다. 이들은 채팅 화면과 운영자 화면에 나온다.
- 규칙:
  - 서버에서 스키마와 길이를 다시 자른다(제목 30자, 답 300자).
  - LLM 답을 권한·경로·첨부 선택에 쓰지 않는다. 첨부와 결과는 서버가 대화에서 고른다.
  - 화면(채팅·운영자)은 `textContent` 와 템플릿 자동 이스케이프(`hub/app/portal.py:17-18`)만 쓴다. `|safe`·`innerHTML` 을 쓰지 않는다. JSON 은 `<script type="application/json">` 블록이나 data 속성에 `|tojson` 으로 넣고 스크립트 파일이 읽는다. 실행되지 않는 블록이라 CSP 에 걸리지 않는다.
  - `/studio`·`/admin/studio` 응답에 `Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'`(인라인 스크립트 금지)와 `X-Content-Type-Options: nosniff` 를 붙인다. 인라인 스타일은 허용한다. 운영자 화면이 인라인 `<style>` 과 `style=` 속성을 쓰는 `base.html` 을 상속하기 때문이다(`hub/app/templates/base.html:7-26`, `hub/app/templates/admin.html:1`, `12`).
- 이유: 운영자 화면에서 스크립트가 돌면 관리자 세션으로 출처 확인이 없는 포털 POST(`/keys`, `/admin/{action}`, `hub/app/portal.py:183-188`, `359-377`)를 부를 수 있다. 그 스크립트가 관리자 API 키를 만들면 LLM 관리 경로(`/api/pull`·`/api/delete` 등)도 쓸 수 있다(`hub/app/llm.py:191-195`).

### 14.4 업로드 검사

| 단계 | 검사 |
|---|---|
| 1 | `Content-Length` 가 없으면 411, `STUDIO_UPLOAD_MAX_MB` + 1MB 를 넘으면 읽기 전에 413. 브라우저의 `fetch` + `FormData` 는 길이를 보낸다 |
| 2 | 본문을 `request.stream()` 으로 직접 읽으며 바이트 수를 센다. 한도를 넘으면 바로 413 으로 끊는다. Starlette 의 multipart 파서는 파일 부분에 크기 상한이 없고(`max_part_size` 는 파일이 아닌 칸에만 적용) 파일을 끝까지 임시 파일로 받기 때문이다(개발 PC Starlette 1.3.1 `formparsers.py:159`, `181-188`, `230`. 허브의 0.41.3 도 같은 구조로 본다 [추정]). 읽은 바이트를 내는 async 생성기로 Starlette 의 `MultiPartParser(request.headers, <생성기>, max_files=1, max_fields=4)` 를 직접 돌린다(`python-multipart` 는 이미 있다 `hub/requirements.txt:7`). `request.form()` 은 쓰지 않는다. 스트림을 다 읽은 뒤 부르면 'Stream consumed' 예외가 나기 때문이다(개발 PC Starlette 1.3.1 `requests.py:239-240`, `268-291`) |
| 3 | Pillow 로 연다: `Image.open(fp, formats=["PNG", "JPEG", "WEBP"])`(`hub/requirements.txt:11`). 헤더만 읽은 상태에서 `img.size` 로 긴 변 64~`STUDIO_UPLOAD_MAX_SIDE`(4096)와 픽셀 수(4096×4096 이하)를 직접 검사하고, 넘으면 `load()` 전에 거부한다 |
| 4 | `verify()` 뒤 다시 연다 |
| 5 | 움직이는 이미지(애니메이션 GIF·WebP·APNG)는 첫 장만 쓴다 |
| 6 | `ImageOps.exif_transpose` 로 방향을 바로잡고, RGB 로 바꾸고, 긴 변을 `STUDIO_UPLOAD_STORE_SIDE`(2048)로 줄여 PNG 로 다시 저장한다. 메타데이터가 빠진다. 2048 은 레시피의 가장 긴 변(편집 1568, [CUI] `comfy_extras/nodes_flux.py:106-124`. 그 밖 1024)보다 커서 첫 화면·편집에 충분하다. Wan 은 출력 크기에 맞춰 다시 자르고([CUI] `comfy_extras/nodes_wan.py:47`), 첫 화면 읽기는 1024 로 줄여 읽는다(`hub/app/video.py:35`, `419`) |
| 7 | 파일 이름은 서버가 만든 uuid 만 쓴다. 사용자가 보낸 이름은 경로에 쓰지 않는다 |
| 8 | sha256 과 크기를 `chat_attachments` 에 남긴다 |
| 9 | 사용자별 업로드 용량(`STUDIO_UPLOAD_QUOTA_MB`. 입력 폴더에 남는 `upload`·`copy` 첨부의 `bytes` 합이고 결과 행은 세지 않는다)과 볼륨 여유 공간(`STUDIO_MIN_FREE_GB`, 7.3)을 본다. 넘으면 429 `quota` / 503 `disk_low` |

- `Image.MAX_IMAGE_PIXELS` 는 바꾸지 않는다. 프로세스 전체 값이라 같은 허브의 첫 화면 읽기·결과 검사에도 걸린다(`hub/app/video.py:407-424`, `hub/app/promptx.py:371-391`, 허브는 한 프로세스 `hub/app/main.py:63-71`). 또 Pillow 는 한도의 2배를 넘을 때만 오류를 내고 1~2배는 경고만 한다(개발 PC Pillow 12.3 `PIL/Image.py:3564-3582`, 허브의 11.0.0 도 같은 규칙으로 본다 [추정]). 그래서 3단계에서 직접 잰다.
- 2 는 이벤트 루프에서 돌린다(요청 스트림은 루프에서만 읽는다). 3~8 은 `asyncio.to_thread` 로 돌린다.
- 허브는 root·privileged·docker.sock 권한으로 돈다(`compose.yaml:53-57`, `63`). (선택) 디코딩을 자원 한도를 건 하위 프로세스에서 한다. Pillow 판 올리기를 업데이트 점검 항목에 넣는다(19.2).

### 14.5 제한

| 항목 | 값 | 설정 |
|---|---|---|
| 글 길이 | 2000자 (앞뒤 공백을 지운 길이) | API 400 `too_long`(10.1). 감독 쪽은 `promptx.clean_input` (`hub/app/promptx.py:68-74`) |
| 메시지당 이미지 | 1장 (P1~P3) | 코드 |
| 업로드 크기 | 20MB [추정] | `STUDIO_UPLOAD_MAX_MB` |
| 업로드 긴 변 (받기 / 저장) | 4096px / 2048px | `STUDIO_UPLOAD_MAX_SIDE`, `STUDIO_UPLOAD_STORE_SIDE` |
| 사용자당 업로드 용량 | 2048MB [추정] | `STUDIO_UPLOAD_QUOTA_MB` |
| 보내지 않은 첨부 보관 | 24시간 | `STUDIO_UNSENT_HOURS` |
| 업로드 볼륨 여유 공간 | 50GB 아래면 거부 [추정] | `STUDIO_MIN_FREE_GB` |
| 사용자당 활성 요청 | 2 | `STUDIO_MAX_ACTIVE_PER_USER` |
| 사용자당 열린 요청 | 5 [추정] | `STUDIO_MAX_OPEN_PER_USER` |
| 사용자당 메시지 | 분당 10 [추정], 넘으면 429 | `STUDIO_MAX_MESSAGES_PER_MIN` |
| 사용자당 WebSocket | 5 | 코드 |

### 14.6 그 밖

- HTTP 만 쓴다. 알림·클립보드 읽기처럼 보안 컨텍스트가 필요한 브라우저 기능은 쓰지 않는다(`docs/handover-2026-10-30-vpn.md:82-91`).
- 채팅 코드는 메인 밖으로 나가는 요청을 하지 않는다. API 노드는 P2 부터 레시피 검사 V11 로 거부한다. P1 레시피(6.3)에는 API 노드가 없다.
- 콘텐츠 안전: `promptx.py`·`video.py`·`crew.py` 에 성적 표현·미성년자 같은 내용을 거르는 규칙이 없다(검색 결과 없음). 채팅은 패널보다 쓰기 쉬워서 이 공백이 더 드러날 수 있다. 넣을지와 어느 단계에 넣을지는 사용자 결정 D11 이다(권장: P1 전에 최소 낱말 목록).

## 15. 대안 비교

| 기준 | 대안 1. 오픈소스 채팅 UI 연결 (Open WebUI 등) | 대안 2. 지금 ComfyUI 패널 숨기기 | **대안 3. 새 채팅 화면 (권장)** |
|---|---|---|---|
| ComfyUI 가 안 보임 (G1) | 된다 | 겉으로만. ComfyUI 페이지를 띄워 두고 가리는 것이라 사용자 작업공간·편집 공간이 필요하다 | 된다 |
| 진행 % 와 단계 (G3) | 맞추기 어렵다. 상태 글 정도만 보낼 수 있다 [추정] | ComfyUI 진행 이벤트를 그대로 받으므로 된다 | 우리가 설계한다 (8절) |
| 영상 결과 표시 | 맞추기 어렵다. 답 글 안에 영상을 넣는 방식에 기댄다 [추정] | 된다 | 결과 카드 (11절) |
| 자동화가 도는 곳 | Open WebUI 플러그인 → 허브 HTTP. 허브는 API 키로 ComfyUI 접수를 받지 않는다(`hub/app/proxy.py:51-61`). 허브에 새 API 가 필요하다 | 브라우저 JS. 지금 패널처럼 탭이 열려 있어야 돈다(`runtime/nodes/mgs_bridge/web/mgs_panel.js:696-723`). 탭을 닫으면 멈춘다 | 허브 안 감독. 탭과 상관없이 돈다 |
| 로그인 | Open WebUI 자체 계정이라 따로 로그인 [추정] | 허브 세션 | 허브 세션 |
| 접속 범위 | 8090 은 사무실 전용, VPN 에서 안 열린다(`compose.yaml:89-93`, `install/04-firewall.sh:23-24`) | 작업공간 포트 (LAN·VPN) | 포트 80 (LAN·VPN, `install/04-firewall.sh:14`, `38`) |
| 보안 | 별도 서비스 하나 더 | 사용자 노드 JS 와 같은 출처 | 포털과 같은 출처, 노드 JS 와 분리 (14.3) |
| 바뀔 때 깨질 위험 | Open WebUI 판에 묶인다 | ComfyUI 프런트엔드 판에 묶인다(패널은 1.57.1 번들로 확인했다, `CLAUDE.md:45`) | 우리 코드만 |
| 이어 말하기·운영자 기록 | Open WebUI 대화 저장과 허브 기록이 둘로 나뉜다 | 브라우저 상태라 남지 않는다(패널 설정도 localStorage, `runtime/nodes/mgs_bridge/web/mgs_panel.js:42-55`) | `chat_*` 표 하나 |
| 일의 양 | 처음은 적다 | 중간 | 가장 많다 |
| 판단 | 기각 | 기각 (구조가 지저분하고 늘리기 어렵다) | **채택** (진행률·결과를 끝까지 맞출 수 있다) |

## 16. 단계별 진행

> 구현 상태(2026-11-13): P1~P4 구현·메인 확인 완료. 실제로 달라진 점과 확인하지 못한 완료 기준은 21절.

### P1 — 가장 좁은 한 줄: 채팅 → 이미지 올리기 → 이미지→영상 → 진행률 → 결과

- P1 을 이미지 생성(차례 하나, 1분 안쪽)이 아니라 I2V 로 하는 이유: 사용자가 정한 첫 단계이고, 기존 부품(첫 화면 읽기·장문·길이 자동·자동 고치기·결과 검사·긍정 자리)을 한 번에 가장 많이 잇는다.

만드는 것:

- `schema.sql` C1~C7 과 색인 (9.1), `config.py`·`.env.example` 의 `STUDIO_*` (19.3).
- `hub/app/studio/` 의 `api.py`·`ws.py`·`store.py`·`director.py`(I2V 만, 기존 `video.render`·`video.fix` 를 그대로 부름)·`intent.py`(글의 길이·비율·소리 지정 뽑기만, 5.3)·`recipes.py`·`progress.py`·`files.py`·`__init__.py`(`prepare()`: 수신기 다시 등록, `start()`: 레시피 시작 검사 V1~V10·V13, Ollama 모델 확인, 대기열 감시 수신기, 복구, 정리 작업. 둘 다 실패해도 허브 시작을 막지 않는다, 5.10).
- 레시피 `wan22_i2v_14b_fast.json` (메인에서 내보낸 API 형식으로).
- `templates/studio.html`, `static/studio.js`·`studio.css`.
- 기존 파일 수정: `portal.build_app`(경로·Mount), `main.startup`(`studio.prepare()`·`studio.start()`), `llm.llm_turn`(컨텍스트 변수 셋, 대기자 등록부터 차례 기다리기까지 한 try, 빠져나가는 모든 길에서 `waiter.done`, 끝난 이유, 9.2), `base.html`(쓸 수 있는 사용자에게만 보이는 메뉴 링크).
- 단위 시험 (17.1).

P2 로 미루는 것 (P1 을 좁게): 허브 재시작 때 LLM 단계 자동 재개(P1 은 `interrupted` + [다시 시도]), `lost` 자동 재접수, CLI `studio-check` 와 V11·V12. 접수 검증은 `submit_comfy` 가 매번 한다(`hub/app/jobs.py:117-124`). 잇단 접수 실패로 꺼진 레시피는 P1 에서 허브 재시작의 시작 검사로 다시 켠다(6.4).

완료 기준 (메인에서 확인):

1. 시범 사용자로 `/studio` 를 열어 PNG 한 장과 "천천히 앞으로 나아가는 영상으로 만들어줘"를 보내면, 채팅에 mp4 결과 카드가 뜨고 재생된다.
2. 사진만 보내도(글 없음, 또는 공백·줄바꿈만) 기본 요청문으로 영상 결과가 나온다. 글도 사진도 없으면 [보내기]가 잠기고, API 를 직접 부르면 400 `empty_message` 다(공백만 보내도 같다). 2000자를 넘는 글은 400 `too_long` 이다.
3. 진행 카드가 대기 → 첫 화면 읽기 → 대기 → 프롬프트 작성 → (대기 → 다듬기) → 대기 → GPU 준비 → 생성 → 마무리 순서로 바뀌고, 생성 %가 줄지 않고 100 에 닿는다.
4. `chat_turn_jobs` 에 그 요청의 `video_frame`·`video_expand`(·`video_fix`)·ComfyUI·(`image_inspect`) 줄이 있고, `jobs.seq` 가 그 순서로 커진다. 모든 줄의 `user_id` 는 채팅 사용자이고 ComfyUI 줄의 `env_id` 는 `studio-chat` 이다.
5. 부품마다 산출물이 남는다(대기열에 다른 ComfyUI 줄이 없는 상태에서 확인한다. 기본 `STUDIO_INSPECT=idle` 이라 그래야 검사가 돈다). `chat_events` 에 `render`(길이 이유 `length_choice.reason`, 또는 움직임만으로 바뀐 사실), `llm_job`(끝난 이유 포함), `negative_check`(접수 직전 부정 자리 확인), `comfy_submitted`, `node_timing`, `output`, `inspect` 가 각각 1건 이상 있다. 향수병·카페 같은 제품·공간 사진 요청에서는 `work.crew.used` 가 남고, 파일럿 요청에서는 건너뛴 이유(사람)가 남는다. 사진만 보낸 요청에서는 '기본 요청문이라 제작진 생략'이 남는다.
6. 1인칭 파일럿 요청에서 자기 손 때문에 생기는 검사 안내(`floating_limb`·`extra_limb`·`missing_head`)가 채팅에 나오지 않는다(5.12 의 안내 기준 표).
7. 결과 파일이 `/srv/results/studio/chat/<날짜>/<사용자>/<요청 앞 8자>/` 에 있고, 같은 폴더에 검사 프레임(`check_frame_00001_.png`)과 설명 파일이 있다. `job_outputs` 가 있다.
8. 생성 중 새로 고침하면 2초 안에 같은 진행 상태가 보인다. 다른 PC 에서 같은 계정으로 열어도 같다.
9. 취소: ComfyUI 줄이 `queued` 일 때 취소하면 `cancelled` 로 끝난다. `running` 일 때 취소하면 outcome 이 `cancelled` 이고 결과가 채팅에 나오지 않는다. `starting` 일 때 취소하면 결과를 채팅에 내지 않는다(알려진 구멍이라 생성은 끝까지 돌 수 있다, 5.9). LLM 차례를 기다리는 중에 취소해도 대기열이 멈추지 않는다(다음 작업이 바로 시작한다).
10. 다른 사용자가 그 대화·요청·첨부 id 로 API 를 부르면 404 다. 다른 Origin 의 POST 는 403 이고, 다른 Origin 의 WebSocket 은 열리지 않는다.
11. 깨지기 쉬운 상황:
    - 앞에 20분짜리 작업이 있어도 실패하지 않고 대기 순번이 보인다.
    - 관리자 화면에서 대기열을 일시 정지하면(`hub/app/portal.py:364-367`) "점검 중"으로 기다리고 대기 기한에 세지 않는다. 재개하면 이어서 결과가 온다. `update.sh` 는 끝에서 허브를 다시 만들므로 아래 '허브 재시작'과 같다(ComfyUI 단계는 이어지고, LLM 단계는 P1 에서 `interrupted` + [다시 시도], P2 부터 자동 재개).
    - 실행 기한을 넘기면 바로, 실행 공간을 강제로 끄면 2분 안에 [추정] 안내가 뜬다(진행 메시지가 오지 않아도). 강제 종료는 스케줄러가 결과 수집(최악 약 1분)과 정리를 마친 뒤 outcome 을 쓰기 때문이다(`hub/app/scheduler.py:348-352`, `435-448`, `225-231`).
    - 관리자가 포털에서 채팅의 LLM 줄을 취소하면 다시 줄 서지 않고 "관리자가 작업을 취소했습니다"가 뜬다.
    - 관리자가 허브를 다시 시작하면, ComfyUI 단계에 있던 요청은 이어서 결과가 오고 LLM 단계에 있던 요청은 `interrupted` + [다시 시도]가 된다.
12. `studio` 계정으로 로그인할 수 없고(사용 중지), 채팅 작업은 정상이다.
13. 기존 시험이 모두 통과한다(`CLAUDE.md:73-76`). 일반 작업공간의 패널(영상 프롬프트·자동 고치기·결과 검사)이 그대로 동작한다.
14. 허브 시작 검사가 레시피를 V1~V10·V13 통과로 `studio_recipe_checks` 에 남긴다(V11·V12 는 P2).
15. 측정: 대기열이 빈 상태에서 3번 돌려 단계별 시간(17.3)과 채팅 한 건의 GPU 점유 합(대기 제외)을 잰다. 그 사이 채팅이 아닌 ComfyUI 작업의 `cold_start_ms` 가 얼마나 늘었는지 본다. 5.6 의 [추정]을 실측으로 바꾸고 D15·D21 을 다시 판단한다.
16. 채팅 작업공간이 없거나 레시피가 깨진 상태로 허브를 시작해도 허브와 ComfyUI 화면은 정상이고, `/studio` 는 "스튜디오를 준비 중입니다"(503)다(5.10).

하지 않는 것: 의도 판단(첨부가 있으면 I2V), 이미지 생성, 이어 말하기, 계획 카드, 운영자 화면(기록은 쌓는다), 위 'P2 로 미루는 것'.

### P2 — 이미지 생성 + 제작진 모드

- 레시피 `zimage_turbo_t2i`(6.8, `negative_source: zero_out`). 감독이 직접 연 `llm_turn`(`STUDIO_WAIT`) 안에서 `promptx.expand_in_turn` 을 부른다(5.5).
- 의도 규칙 R2·R2b·R3·R5·R6a·R6b·R9 와 R10 의 기본값, 나중 단계 규칙의 '준비 중' 안내(5.3). 글의 비율 지정으로 이미지 크기 정하기(5.3, 6.6). 결과 이미지 검사(`promptx.inspect`). "이걸로 영상 만들기"(결과 → 입력 복사 → P1 흐름).
- P1 에서 미룬 것: LLM 단계 자동 재개, `lost` 자동 재접수, CLI `studio-check`(V11·V12), CLI `studio-import`.
- 완료 기준:
  1. "비 오는 밤 골목의 작은 카페"는 이미지가 되고, 운영자 기록(`chat_events.expand`)에 제작진 분야 '공간'과 사용 여부가 남는다.
  2. "향수병 광고 사진"은 제품 분야로 제작진을 쓴다. "니키가 …" 요청과 사람이 나오는 요청은 제작진을 건너뛰고 이유가 기록된다(`hub/app/config.py:64-65`, `hub/app/crew.py:67-76`).
     - 11-26 바뀜: 인물 라이브러리의 정본 인물("니키가 …")만 나오는 요청은 건너뛰지 않고 정본 장면 분야(`cast_scene`)로 빛·카메라·색·분위기만 고르며,
       expand 기록의 crew 에 `category: cast_scene` 과 `cast` 가 남는다 (`CREW_CAST_MODE`, `docs/crew-mode.md` 11-26 절). 라이브러리에 없는 정본 이름·
       손 숨김·사람이 나오는 요청은 지금처럼 건너뛰고 이유가 기록된다.
  3. 이미지 결과의 [이걸로 영상 만들기]가 I2V 결과를 만든다. 글 "이걸로 영상 만들어줘"(R5)도 같다. 입력 폴더에 `source='copy'` 첨부가 생긴다. 결과 파일을 바꿔 두면 복사하지 않고 안내한다.
  4. 이미지 결과 뒤 "더 밝게"는 이미지 고치기(R6a), "하나 더"는 다시 만들기(R6b)가 된다. "고마워", "왜 이렇게 나왔어?"는 GPU 줄 없이 정해 둔 답이 나온다. "배경만 노을로 바꿔줘"(R7)와 첨부 없는 "바다 위를 나는 드론 영상"(R8)은 GPU 줄 없이 "이 기능은 지금 준비 중입니다"가 나온다.
  5. `zimage_turbo_t2i` 를 메인에서 내보낸 파일로 바꾸고 V1~V13 을 통과한다(0 조건 노드 예외 포함). `studio-check` 가 결과를 남긴다.
  6. 허브 재시작 때 LLM 단계 요청이 한 번 자동으로 다시 시작한다.
  7. P1 기준이 그대로 통과한다.

### P3 — 글→영상, 이미지 편집, 이어 말하기

- 레시피 `wan22_t2v_14b_fast`, `qwen_image_edit`(6.8). 편집 지시문 함수(5.5).
- 의도 규칙 R4·R6·R7·R7b·R8, `revise` 흐름(5.8), `t2v` 의 비율 지정과 `edit` 의 비율 안내(5.3).
- `video.render_in_turn`·`fix_in_turn` 꺼내기와 `draft` 의 `revise` 인자(5.6, 5.8). 기존 `render`·`fix` 는 감싸기만 한다.
- (선택) 이미지 검사 차례에서 첫 화면 읽기도 함께(5.6). `promptx.py` 의 검사 차례 꺼내기가 필요하다(19.2, D22).
- 완료 기준:
  1. 영상 결과 뒤 "더 천천히"를 보내면 같은 첫 화면으로 새 영상이 나오고, 그 요청의 `chat_turn_jobs` 에 `video_frame` 줄이 없다(첫 화면 읽기를 다시 씀). 새 프롬프트에 slow 계열 확인어가 있다.
  2. 이미지 결과 뒤 "배경만 노을로 바꿔줘"가 편집 결과를 만든다. 영상 결과 뒤 같은 말은 직전이 I2V 면 정해 둔 답 한 줄(R7b)이 나오고, T2V 면 장면을 바꿔 다시 만든다.
  3. 첨부 없이 "바다 위를 나는 드론 영상"이 T2V 결과를 만든다. "세로로"를 붙이면 480×848 이다. "10초 영상"은 5초로 만들고 한 줄로 알린다.
  4. I2V 한 요청의 LLM 차례가 첫 화면 읽기 1 + 27B 1 (+ 검사 1)로 줄어든다.
  5. 두 레시피를 메인에서 내보낸 파일로 바꾸고 V1~V13 을 통과한다(편집 레시피는 두 번째 LoadImage 를 지운 뒤).
  6. 기존 `hub/tests/test_video.py` 가 그대로 통과한다(패널 API 변화 없음).

### P4 — 감독 판단, Ask 계획 카드, 운영자 기록 화면

- LLM 의도 판단(R10, 5.3), 첨부 역할 판단(5.4), 대화 제목 자동(`title_ko`).
- Ask 계획 카드(5.7), 승인 API, 생성 전 남은 시간(8.5), 진행 가중치 보정(8.3).
- 운영자 화면(12절), 재현용 API JSON 내려받기(10.1).
- 사용자 결정에 따라 `users.home`(D1b), 작업공간 없는 사용자(D1a), 자동 재생성(D8).
- 길이·크기를 사용자가 고르는 칸(D17 결정). 그 전(P1~P3)에는 모두 자동이다.
- 완료 기준:
  1. 정해 둔 애매한 문장 20개(17.1)에서 기대한 action 이 18개 이상 나온다 [추정 기준].
  2. Ask 를 켜면 예상 시간이 기준 이상인 영상 요청에 계획 카드가 뜬다. 승인하면 만들고, 취소하면 ComfyUI 줄이 생기지 않는다. 24시간 답이 없으면 자동 취소된다. 승인 대기 중에 새 메시지를 보내면 계획을 다시 만든다. [승인]을 두 번 눌러도 ComfyUI 줄은 하나다.
  3. 관리자는 아무 요청이나 상세 기록(프롬프트, 길이 이유, 걸린 검사, job id, 시간, LLM 차례 끝난 이유)을 본다. 관리자가 아니면 403 이다.
  4. `<script>` 가 든 사용자 글·제목·첫 화면 읽기 결과가 채팅·운영자 화면에서 글자로만 보인다(17.1 보안 시험).
  5. D1b 가 '보냄'이면 `users.home='studio'` 인 사용자는 `/` 로 들어와도 채팅 화면만 본다.

## 17. 시험 계획

### 17.1 단위 시험 (개발 PC, `hub/tests`, `python -I`)

| 파일 | 내용 |
|---|---|
| `test_studio_recipes.py` | 예시 레시피의 V1~V10·V13, `build()` 가 원본을 바꾸지 않음(깊은 복사), `fixed` 값이 다르면 예외(assert 가 아님), 자리 값 검사(4n+1, 16 단위, 저장 이름 글자, 사용자 폴더 `u<id>`), `roles_of` 의 긍정·부정과 0 조건 노드(Z-Image 구조, Qwen 편집 구조), V3 의 이미지·편집 판별, V6 의 편집 대상 규칙, V9 의 남은 LoadImage 거부, V11 의 점 이름(`format.codec`)을 `/object_info` 모양의 사례로, 워크플로우 종류 정규식이 `mgs_nodes.js` 와 같은 결과(`hub/tests/panel_nodes.test.mjs` 의 사례를 옮김), `size_for` 예 |
| `test_studio_progress.py` | [CUI] 모양으로 만든 이벤트 열 → % 열이 줄지 않음, `execution_cached`, 놓친 progress, `progress_state` 스냅샷으로 다시 계산, 띠 순서가 어긋난 실행, 샘플러 띠가 아닌 노드의 progress 무시, prompt_id 없는 `executing`, 중간 오류, 샘플러 단계 2·2 와 10·10 |
| `test_studio_intent.py` | 규칙 R0~R10 의 문장 표(한국어), 1인칭 감지와 '영상 내 시점 변화 없이' 같은 예외(`hub/app/video.py:90-95`), 길이·비율·소리 지정 뽑기, '직전 결과' 정의(앞 요청 실패·취소, 답만 한 요청과 결과 없는 요청 건너뛰기), 승인 대기 중 새 메시지, P4 용 애매한 문장 20개. 경계 사례: 공백만 + 사진 → R0, 공백만 → 400 `empty_message`, `promptx.crew_route(DEFAULT_I2V_TEXT)[0] is None` 과 R0 이면 `crew=False`, 사진 + "이 사진을 수채화 스타일로 바꿔줘" → R4, 영상 뒤 "배경만 바꿔줘" → R7b, 이미지 → "고마워"(R2 답) → "더 밝게" → R6a(답을 건너뛰고 앞 이미지를 고친다), I2V → "배경만 바꿔줘"(R7b 답) → "하나 더" → 직전 I2V 의 `repeat`, 지금 단계보다 나중인 규칙 → '준비 중' |
| `test_studio_director.py` | 가짜 `llm_turn`(선례 `hub/tests/test_video.py:301-309`)·가짜 `submit_comfy`·가짜 relay·가짜 DB 로 상태 기계 전체, 단계별 취소, 대화 삭제 때 열린 요청 취소, 두 번 누른 버튼(조건부 전이), DB 행으로부터 복구(5.10 표의 모든 줄). 이벤트 없이 끝난 작업(timeout·lost·폴링 판정·시작 전 취소), 재시작 틈(수신기 등록 전 메시지), `lost` 인데 `job_outputs` 있음, 접수 중 취소(shield 를 기다리던 중 취소돼도 접수된 작업까지 취소되는지), `work.pending` 으로 접수 여부 찾기, 단계 기한 넘김, `cleaning` 을 본 뒤 `lost`·`error` 로 끝남, 성공 신호 없는 `cleaning` 에서 결과를 고르지 않음. 대기 기한 시계: 가짜 `db.get_setting` 으로 `paused`·`blocked` 를 켰다 끄며 시계가 멈췄다 이어지는지, 기한을 넘기면 `task.cancel` 이 한 번이고 새 jobs 줄이 생기지 않는지. 시각 응답이 빈 값 dict 면 '사진을 읽지 못함'. `prepare()`·`start()` 가 예외를 내도 허브 시작이 계속됨 |
| `test_llm_turn.py` | 가짜 스케줄러로: `go` 를 넘기는 순간의 취소·시간 초과에도 `waiter.done` 이 세워짐, insert 커밋 뒤·`notify_queue_changed` 도중 취소, 스케줄러가 꺼내기 전 대기 중 취소, 본문 정상 종료 뒤 정리 중 첫 취소, 정리 중 두 번째 취소, `release_waiter` 와 겹침(안내 글이 그대로인지), 차례 안 예외·정상 끝에서 `done` 과 `status`, 끝난 이유(`end`) 기록(9.2 의 표), `STUDIO_WAIT` 가 `timeout` 으로 쓰임, 차례 안에서 `llm_turn` 을 열면 `NestedTurnError`, 패널 경로(컨텍스트 변수 없음)의 동작이 같음. 개발 PC 에 없는 모듈(asyncpg 등)은 가짜로 넣는다 [추정] |
| `test_studio_security.py` | Origin 확인 함수, 업로드 검사(형식 위조, 압축 폭탄, 너무 큰 파일, `Content-Length` 없음 411, 스트림 한도 413, `load()` 전 크기 거부, 애니메이션), `<script>` 가 든 글·제목·첫 화면 읽기 결과가 서버 렌더에서 이스케이프되는지, CSP 머리말 — Pillow 로 작은 이미지를 만들어 시험 |
| `studio_ui.test.mjs` | node 로 돌리는 화면 로직 시험(선례 `hub/tests/panel_nodes.test.mjs`): 일련번호 `n` 으로 오래된 이벤트 버리기, `hello`·`snapshot` 뒤 다시 붙기와 기준 다시 잡기, 진행 → 결과·안내 카드 전환, 버튼 말풍선, 글을 `textContent` 로만 넣는지 |
| 기존 시험 | `CLAUDE.md:73-76` 의 명령 전부 |

- P4 용 애매한 문장 20개는 P4 시작 때 `test_studio_intent.py` 에 표로 만든다. 규칙 경계마다 2~3개씩 고른다: 첨부 + 모호한 말, 가리키는 말 없는 "이걸로", 이미지 뒤 "더", 영상 뒤 "하나 더", 참고 말, 질문형, 편집 말과 영상 말이 함께 있는 말. 기대 action 은 사람이 적는다 [TBD: 목록].

### 17.2 개발 PC 통합 (GPU 없이)

- 메인 P1 시험 때 한 번 실행의 진행 메시지 원문을 파일로 남긴다(관리자 전용 기록 스위치 `STUDIO_RECORD_EVENTS`, 위치 `STUDIO_RECORD_DIR`, 9.4). 개발 PC 에서는 가짜 `submit_comfy` 가 이 파일을 다시 보내 화면과 진행률을 시험한다.
- 화면: 데스크톱·휴대폰 폭, 밝은·어두운 화면에서 진행·결과·계획·안내 카드.
- Range: 메인에서 `curl -I -b "mgs_sess=<세션>" -H "Range: bytes=0-99" http://<메인>/studio/api/files/<id>` 가 206 을 주는지 본다. 파일 경로는 로그인 쿠키가 있어야 한다(10.1). 안 주면 영상 파일만 Range 를 처리하는 응답을 직접 만든다.

### 17.3 메인 통합 (관리자와 함께)

1. 시스템 계정·채팅 작업공간 만들기(7.2 의 0~4), 레시피 넣기, 시범 사용자 목록을 넣고 켜기(7.2 의 5·6), 시작 검사 결과 확인.
2. P1 시나리오 3번. 단계별 시간을 아래 쿼리로 잰다.

```sql
-- 요청 하나의 GPU 작업 (순서·대기·준비·실행·정리)
SELECT t.step, j.seq, j.kind, j.request->>'purpose' AS purpose, j.state, j.outcome,
       j.wait_ms, j.cold_start_ms, j.run_ms, j.cleanup_ms, j.cleanup_method
FROM chat_turn_jobs t JOIN jobs j ON j.id = t.job_id
WHERE t.turn_id = $1 ORDER BY j.seq;

-- LLM 차례 시간 (목적별 중앙값, 최근 7일)
SELECT endpoint, count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms
FROM llm_calls WHERE endpoint LIKE 'helper:%' AND ts > now() - interval '7 days'
GROUP BY endpoint ORDER BY endpoint;
```

3. 동시성: 사용자 3명이 각자 대화 둘에서 하나씩 동시에 보내고, 그 사이 다른 사람이 ComfyUI 화면에서 실행한다. 순번대로 도는지, 채팅의 대기 순번이 패널의 대기열(`/mgs/api/queue`)과 맞는지 본다.
4. 장애 연습: 채팅 편집 공간 끄기, 실행 공간 강제 종료(lost), 실행 기한 넘김, 허브 재시작(ComfyUI 단계·LLM 단계 각각), 관리자 화면의 일시 정지, `update.sh`(정지 뒤 허브 재시작), 관리자의 LLM 줄·ComfyUI 줄 취소, 대기열 정지(BLOCKED), 채팅 작업공간이 없는 채로 허브 시작.
5. 부하: 채팅 요청을 몰아 보낼 때(예: 사용자 3명이 5건씩) 다른 PC 의 ComfyUI 화면 진행 막대가 끊기는지 보고, 허브 이벤트 루프 지연(1초 타이머가 늦는 정도)을 로그로 남긴다(18.1 의 '이벤트 루프 하나').
6. 보안: 다른 사용자 id, 위조한 Origin(curl), 큰 파일, `Content-Length` 없는 업로드.
7. 회귀: 일반 작업공간에서 패널 기능.

### 17.4 사람이 보는 품질 확인

- 지난 영상 시험의 첫 화면 5장(향수, 비 오는 날, 카페, 신발, 바다 포스터, `docs/video-renderer.md:135`)을 채팅으로 만들어 패널 결과와 비교한다. 같은 함수를 쓰므로 품질이 비슷해야 한다. LLM 답은 매번 달라 글이 똑같지는 않다.
- 1인칭 파일럿 요청으로 결과 검사의 안내가 오탐을 내지 않는지 본다(5.12).

## 18. 위험·모르는 것·사용자 결정

### 18.1 위험

| 위험 | 영향 | 대응 |
|---|---|---|
| LLM 차례마다 실행 공간을 끄고 모델을 다시 올린다(`hub/app/scheduler.py:511-512`, `hub/app/llm.py:308-311`) | 채팅 요청 하나가 콜드 스타트를 부르고, 검사 차례는 다음 ComfyUI 작업의 콜드 스타트를 부른다. 작업자가 2~3명이라(`docs/handover-2026-10-23.md:11`) 한 사람의 채팅이 모두를 늦출 수 있다 | 같은 모델 단계를 한 차례로 묶는다(P3). 낱말 수 위반만 남으면 고치기 차례를 열지 않는다(5.12). 검사는 대기열에 다른 ComfyUI 줄이 없을 때만(D15). `cold_start_ms` 로 영향을 잰다(P1 기준 15). 실행 공간을 끄지 않는 스케줄러 정책은 D21 |
| 단계 사이에 다른 작업이 끼어든다 | 붐비는 날 채팅이 느리다 | 순번 규칙은 그대로 두고 단계마다 대기 순번을 보인다(5.11) |
| 스케줄러가 진행 메시지 없이 작업을 끝내는 길이 있다(5.13) | 요청이 '생성 n%'·'GPU 준비 중'에 영원히 남는다 | 끝 판정을 DB 로 하고 단계 기한을 둔다(5.12, 5.13) |
| 허브 안 LLM 대기 900초 한도(`hub/app/config.py:57`) | 긴 작업 뒤나 점검 중에 채팅이 실패한다 | P1 부터 `STUDIO_WAIT` 로 감독이 기다리는 시간을 정한다. 점검·정지 시간은 세지 않는다(5.6, D14). 다만 `update.sh` 는 끝에서 허브를 다시 만들어, P1 은 그때 LLM 단계 요청이 `interrupted` 가 된다(5.10, P2 부터 자동 재개) |
| LLM 차례 취소가 차례 넘김과 겹치거나, 줄을 넣은 직후·정리 중에 온다 | 대기열 전체가 최대 4시간 멈춘다 | `llm.py` 의 빠져나가는 모든 길에서 `waiter.done`(P1, 9.2), 요청마다 한 번만 취소(5.9) |
| 채팅 시작 작업(`studio.prepare()`·`start()`)이 실패한다(작업공간 없음, 설정 잘못, Ollama 늦음) | 허브 전체가 뜨지 않으면 모든 ComfyUI 화면이 멈춘다(`hub/app/main.py:34-40`) | 예외를 잡아 채팅만 끄고 '준비 중'을 보인다. 느린 확인은 백그라운드(5.10). P1 기준 16 |
| 같은 생성이 두 번 돈다 | GPU 낭비, 결과 둘 | `work.pending` 과 `client_id` 로 찾기, 접수 shield, 조건부 상태 전이, `lost` 전에 `job_outputs` 확인(5.2, 5.9, 5.10, 6.6) |
| 진행 메시지 모양이 메인 판(v0.42.5)에서 다를 수 있다 | %가 틀린다 | P1 메인 시험에서 원문을 남겨 확인한다(17.2) |
| 메인의 템플릿 구조가 개발 PC 와 다를 수 있다 | 레시피의 자리가 틀린다 | 메인에서 내보낸 파일로 바꾸고 V2·V12 로 잡는다 |
| 영상 결과 검사가 가운데 프레임 하나뿐이다 | 움직임·시점 문제를 놓친다 | 첫·중간·끝 프레임 비교는 렌더러 문서의 계획과 함께 뒤에 한다(`docs/video-renderer.md:140`) |
| 그림·인물용 검사 지시문을 영상 프레임에 쓴다(`hub/app/promptx.py:231-237`) | 오탐 | 5.12 의 안내 기준으로 거른다 |
| 취소 구멍(`hub/app/scheduler.py:322-347`, `207-209`, `210`, `313`) | 취소해도 생성이 끝까지 돌 수 있다 | 결과를 내지 않는다. 스케줄러 쪽 수정은 별도 작업(19.2) |
| 이벤트 루프 하나 | 채팅 처리가 무거우면 모든 ComfyUI 화면 중계가 느려진다. 그대로 쓰는 `promptx.inspect` 도 루프에서 파일 읽기·Pillow 일을 한다(`hub/app/promptx.py:522-524`, `537-539`) | CPU 일은 스레드로, 수신기는 큐에 넣기만 (8.1). 부하 시험(17.3). inspect 는 promptx 를 고쳐야 한다(20.4) |
| 프롬프트 인젝션·화면 스크립트 | 관리자 세션으로 키 발급·대기열 조작 | 14.3 의 규칙, CSP |
| 업로드가 btrfs 볼륨을 채운다 | 모든 사용자의 접수 실패 | 사용자별 용량, 여유 공간 확인, 보내지 않은 첨부 정리(7.3, 14.4) |
| 결과 파일을 누구나 덮어쓸 수 있다 | 다른 그림이 화면에 보이거나 입력으로 복사된다 | 크기·시각·sha256 비교(5.13, 7.3, 10.1) |
| 결과 파일이 모두에게 열려 있다 (영문 프롬프트는 D18 로 숨김) | 채팅 결과를 비공개로 오해할 수 있다 | 화면에 한 번 알림(D3). 영문 프롬프트는 숨김(D18) |
| DB 백업 없음 | 채팅 기록을 잃을 수 있다 | 야간 `pg_dump` 를 먼저 (D10) |
| 날짜 폴더가 UTC 다(`TZ` 가 없을 때) | 결과가 전날 폴더에 | `.env` 에 `TZ` (D20) |

### 18.2 모르는 것 [TBD]

- 단계별 시간: 8B 첫 화면 읽기, 27B 장문 영상 차례, 자동 고치기, 8B 검사, 콜드 스타트, 14B 적재, Z-Image·T2V·편집 생성 시간 (5.6). 1분 반 생성 시간에 모델 적재가 들어 있는지.
- 메인 v0.42.5 의 진행 메시지 모양, 프런트엔드 판, 템플릿 패키지 판.
- 메인 Wan 2.2 템플릿이 `length` 를 직접 여는지 `duration` 인지 (6.3). Z-Image·Qwen 편집·T2V 템플릿의 펼친 id (6.8).
- 미리보기 방식 값(`extra_data.preview_method`)과 채팅에 보일지 (8.1).
- 허브 고정판의 동작: Starlette 0.41.3 의 Range 응답(11.5), uvicorn 0.32.1 의 accept 전 close(10.2), Pillow 11.0.0 의 크기 검사 규칙(14.4), asyncpg 0.30.0 의 시각 형식(10.1). 모두 개발 PC 의 다른 판이나 문서로 본 것이다 [추정].
- `TZ=KST-9` 가 허브 이미지에서 되는지 (7.4).
- 편집 지시문의 품질 규칙 (5.5).
- 이미지 위반 가운데 결과에 바로 보이는 것 (5.12).
- T2V 기본 크기 (6.8).
- 결과 검사 안내 기준의 오탐률 (5.12).
- `/free` 뒤 모델을 다시 올리는 시간. 출력 캐시가 남는지는 코드로 확인했다: 허브의 첫 `/free` 는 `unload_models` 만 보내므로(`hub/app/scheduler.py:595`) 캐시 비우기(`e.reset()`)는 `free_memory` 를 보낼 때만 일어난다([CUI] `main.py:405-416`, `server.py:1288-1297`). 같은 실행 공간에 같은 prompt·시드를 다시 보내면 새 파일 없이 캐시된 결과가 온다(`execution_cached`, [CUI] `execution.py:447-452`). 그래서 '같은 값으로 다시'라는 다시 하기를 두지 않는다(13.2).
- Ollama 생성이 연결 끊김에 바로 멈추는지 (5.9).
- 대화에서 앞 결과를 글로 고르는 방법 (5.8, P4).

### 18.3 사용자(제품 책임자)가 정할 것

- 표의 '필요한 때' 전에 정해야 한다. '권장대로 하면 사용자에게 보이는 것'은 권장안을 승인했을 때 화면과 결과에서 달라지는 점이다. P1 전에 정할 것은 권장대로면 한 번에 승인할 수 있다.

- **결정됨 (2026-11-12, 사용자).** D3: 처음 한 번 안내를 보이고 5초 뒤 저절로 사라지게 한다. 결과(이미지·영상)는 권장안대로 공유한다. D17: 자동으로 넣는다. 길이·크기를 고르는 칸은 P4 에서 더한다. D18: 영문 프롬프트를 다른 사람에게 보이지 않게 숨긴다. 이 셋 외의 D 는 모두 '권장' 칸의 값으로 확정한다(D1a·D1b·D2·D8 은 P4 전에, D13·D7·D21 은 표의 '필요한 때'에 다시 확인한다).

| # | 결정 | 선택지 | 권장 | 필요한 때 | 권장대로 하면 사용자에게 보이는 것 |
|---|---|---|---|---|---|
| D1a | 채팅만 쓰는 사용자(작업공간 없음)를 받나 | 받지 않음 / 받음(uid 정책 필요, 14.1) | P1~P3 은 받지 않음, P4 에서 다시 판단 | P4 전 | 채팅을 쓰려면 지금처럼 허브 계정과 작업공간이 있어야 한다 |
| D1b | 로그인 첫 화면 | `/studio` 북마크·링크로만 / 채팅 사용자는 `/` 로 들어와도 `/studio`(`users.home`, 작업공간이 있는 사용자도 고를 수 있음) | P1~P3 은 북마크·링크(11.1), P4 에서 `users.home` 을 넣음 | P4 전 | P1~P3 은 `/studio` 주소로 들어와야 채팅 화면이 보이고, 그냥 `/` 로 들어오면 ComfyUI 가 보인다 |
| D2 | Ask 계획 카드 기본값과 기준 시간 | 꺼짐 / 켜짐, 기준 0·300·600초 | 꺼짐, 300초 [추정] | P4 전 | 계획 카드 없이 바로 만든다. 켜면 5분 넘게 걸릴 영상 앞에 카드가 뜬다 |
| D3 | 채팅 결과의 공개 범위 | 원칙대로 공유 / 비공개(원칙 변경 필요) | **결정: 공유.** 화면에 처음 한 번 "결과(이미지·영상)는 공유 폴더에도 저장됩니다" 안내를 보이고 5초 뒤 저절로 사라지게 한다. 영문 프롬프트는 D18 로 숨기므로 안내에 넣지 않는다 | P1 전 | 처음 한 번 안내가 5초 보였다가 사라지고, 결과(이미지·영상)는 다른 사람도 공유 폴더에서 볼 수 있다 |
| D4 | **A2 제안 변경 승인**: 결과 위치를 '사용자별 결과 폴더'에서 'studio 폴더 아래 사용자 하위 폴더'로 바꾸나 | 승인 / 승인하고 각자 폴더(`/srv/results/<사용자>/studio/…`)에도 복사 / 승인 안 함(사용자 작업공간에서 실행, 7.1 의 안 A) | 승인, 복사는 하지 않음 | P1 전 | 결과를 `\\main\results\studio\chat\<날짜>\<나>\` 에서 찾는다 |
| D5 | 대화 글 보관 기간 | 무기한 / n일 뒤 자동 만료 | 무기한, 지운 대화는 30일 뒤 실제 삭제. 대화를 지워도 `jobs`·결과 파일·메타데이터는 남는다(9.4) | 언제든 (기본값으로 시작) | 대화는 지울 때까지 남는다 |
| D6 | 채팅의 이름과 감독의 화면 이름 | "채팅 스튜디오"·"감독" / 다른 이름 ("감독"은 러다이트의 에이전트 이름이다, DFC/01:76) | 내부 이름은 감독, 화면에는 이름 없이 "스튜디오" | P1 전 | 화면 글에 "스튜디오"만 보인다 |
| D7 | 품질 단계 | 빠름(4스텝 LoRA, 640×640 면적, 약 1분 반) / 고품질(20스텝, 더 큰 크기, 시간 [TBD]) / 둘 다 두고 감독이 고름 | P1~P3 은 빠름 하나 | P3 뒤 | 모든 영상이 빠름 단계(생성 약 1분 반)로 만들어진다 |
| D8 | 검사에서 문제가 나오면 자동으로 다시 만드나 | 안 함 / 1회 (GPU 시간이 더 든다) | 안 함 | P4 전 | 문제가 보이면 한 줄 안내와 [다시 만들기]만 나온다 |
| D9 | 사용자에게 프롬프트("자세히")를 보이나 | 안 보임 / 접힌 칸으로 보임 (러다이트는 생성 상세에서 보인다, DFC/01:86) | 안 보임 (G3). 단 결과 파일 메타데이터에는 들어 있다(14.2) | 언제든 (기본값으로 시작) | 채팅 화면에 영문 프롬프트가 보이지 않는다 |
| D10 | DB 야간 백업 | 지금 넣음 / 나중 | P1 전에 넣음 | P1 전 | 보이는 변화는 없다. 채팅 기록을 잃을 위험이 줄어든다 |
| D11 | 콘텐츠 안전 거르기 | 넣음 / 안 넣음 | 넣음, P1 전에 최소 낱말 목록 [TBD: 규칙] | P1 전 | 목록에 걸리는 요청은 만들지 않고 안내한다 [TBD: 문구] |
| D12 | 시스템 계정 이름·uid | `studio` / 다른 이름 | `studio` | P1 전 (7.2) | 공유 폴더의 채팅 결과가 `studio` 폴더 아래에 있다 |
| D13 | Open WebUI(8090)의 앞날 | 그대로 / 정리 (`docs/handover-2026-10-23.md:191`) | 채팅 스튜디오 P2 뒤에 다시 판단 | P2 뒤 | 보이는 변화는 없다 |
| D14 | 채팅의 대기 정책 | 점검·정지 중에는 실패 없이 기다리고 사용자가 취소 / 시간 한도로 실패 | 점검·정지 시간은 세지 않고, 그 밖의 대기는 3600초 [추정] 뒤 실패 + [다시 시도] | P1 전 | 점검 중에는 "점검 중"으로 기다리고, 그 밖에 1시간 넘게 차례가 오지 않으면 실패 안내가 나온다 |
| D15 | 결과 검사 기본값 | 켬(`on`) / 대기열에 다른 ComfyUI 줄이 없을 때만(`idle`) / 끔(`off`) | `idle`. P1 측정 뒤 다시 정함 | P1 전 (P1 측정 뒤 다시) | 붐비지 않을 때만 결과 뒤에 검사 안내가 붙을 수 있다 |
| D16 | P1 시범 범위 | 계정 있는 사람 모두 / `STUDIO_ALLOWED_USERS` 목록 | 1명부터 | P1 전 | 처음에는 목록에 든 사람만 채팅을 쓴다 |
| D17 | 자동 설정 적용 (렌더러 v1 결정과의 차이) | 채팅은 프레임·크기·시드를 자동으로 넣음 / 채팅도 제안만 (G4 와 부딪힘) | **결정: 자동으로 넣음.** 길이·크기를 고르는 칸은 P4 에서 더한다(P1~P3 에는 없다). 패널은 그대로 제안만 | P1 전 | P1~P3 에는 길이·크기를 고르는 칸이 없다. P4 에서 고르는 칸이 생긴다 |
| D18 | 결과 파일·설명 파일에 남는 영문 프롬프트를 숨기나 | 지금처럼 공유 / 채팅 작업만 숨김(실행 공간의 메타데이터 끄기와 설명 파일 줄이기. `entrypoint.sh`·스케줄러를 바꿔야 한다) | **결정: 채팅 작업만 숨김.** 다른 사람은 공유 폴더의 결과 파일·설명 파일에서 영문 프롬프트를 볼 수 없다. 만드는 법은 14.2 | P1 전 | 공유 폴더의 결과 파일과 설명 파일에 영문 프롬프트가 없다. 결과 파일 자체는 모두에게 열려 있다 |
| D19 | 첨부 + 모호한 말의 기본값 (P2~P3) | `i2v` / Ask 카드 / 정해 둔 되묻기 한 줄 | `i2v` ("사진을 올리면 영상"이라는 안내와 같다) | P2 전 | 사진과 애매한 말을 보내면 영상이 만들어진다 |
| D20 | 오전 9시 전에 만든 결과가 전날 날짜 폴더에 들어가도 되나 (허브 시간대) | 그대로 둔다(UTC) / 한국 날짜로(`.env` 에 `TZ`) | 한국 날짜로 (메인에서 되는지 확인 뒤) | P1 전 | 결과가 만든 날의 한국 날짜 폴더에 들어간다 |
| D21 | 채팅 요청마다 생기는 GPU 실행 공간 다시 띄우기(10~40초 [추정])를 줄이려고 스케줄러 정책을 바꾸나 | 지금 그대로 / LLM 차례에도 실행 공간을 끄지 않음 (원칙 수준 변경, 메모리 확인 규칙과 함께 검토) | 지금 그대로, P1 측정 뒤 다시 판단 | P1 측정 뒤 | 보이는 변화는 없다. 채팅 요청마다 실행 공간을 다시 띄우는 시간이 그대로 든다 |
| D22 | **A1·A2 에서 그대로 쓴다고 한 부품의 수정 승인** | 승인 / 일부만 승인. 대상: ① `llm.py` 수정(P1 필수. 차례를 놓는 버그를 고치며 패널 동작은 같다, 9.2) ② `video.py` 의 한 차례 묶기 꺼내기(P3, 5.6) ③ `promptx.inspect` 의 스레드화와 검사 차례 꺼내기(선택, 19.2) ④ 결과 검사 순서: 전달 뒤 검사 / A1 대로 검사 뒤 전달(3.1) | 모두 승인, 검사는 전달 뒤 | P1 전 (②③은 그 단계 전) | 결과가 검사를 기다리지 않고 바로 보이고, 검사 안내는 결과 뒤에 붙는다 |

## 19. 파일 배치와 새 설정값

### 19.1 새 파일

```
hub/app/studio/__init__.py        prepare(): 수신기 다시 등록. start(): 레시피 시작 검사, Ollama 모델 확인, 대기열 감시 수신기, 복구, 1시간마다 정리. 예외는 잡아 채팅만 끈다
hub/app/studio/api.py             REST (/studio, /studio/api/*, /studio/logout), 오류 응답 모양
hub/app/studio/ws.py              WebSocket (/studio/ws), 사용자별 연결 목록, 이벤트 보내기
hub/app/studio/store.py           chat_* 표 읽기·쓰기 (조건부 상태 전이), 채팅 작업공간 찾기
hub/app/studio/director.py        감독: 상태 기계, 단계 실행, 단계 기한, 취소(요청마다 한 번), 복구
hub/app/studio/intent.py          길이·비율·소리 지정 뽑기(P1), 의도 규칙(P2~P3), LLM 판단(P4)
hub/app/studio/edit.py            편집 지시문 (P3)
hub/app/studio/recipes.py         레시피 읽기·검증(V1~V13)·값 넣기·크기 규칙
hub/app/studio/recipes/wan22_i2v_14b_fast.json    (P1)
hub/app/studio/recipes/zimage_turbo_t2i.json      (P2)
hub/app/studio/recipes/wan22_t2v_14b_fast.json    (P3)
hub/app/studio/recipes/qwen_image_edit.json       (P3)
hub/app/studio/progress.py        수신기, % 계산, 단계 이름, 대기 순번, 남은 시간
hub/app/studio/files.py           업로드 검사·저장(스트림 한도), 결과 파일 내보내기(바뀜 확인)
hub/app/studio/admin.py           운영자 화면 (P4)
hub/app/templates/studio.html
hub/app/templates/admin_studio.html, admin_studio_turn.html   (P4)
hub/app/static/studio.js, studio.css
hub/tests/test_studio_recipes.py, test_studio_progress.py, test_studio_intent.py,
hub/tests/test_studio_director.py, test_studio_security.py, test_llm_turn.py
hub/tests/studio_ui.test.mjs
```

### 19.2 바꾸는 기존 파일

| 파일 | 바꾸는 것 | 단계 |
|---|---|---|
| `hub/app/schema.sql` | 끝에 C1~C7 과 색인 (C8 은 P4, D1b). C8 을 넣을 때 기존 행 검사 목록(`hub/app/schema.sql:241-243`)에 `users_home_check` 를 더한다 | P1·P4 |
| `hub/app/config.py`, `.env.example` | `STUDIO_*` | P1 |
| `hub/app/portal.py` | `build_app` 에 `/studio` 경로·WebSocket·정적 Mount, `/admin/studio` (P4), `/` 의 `users.home` 처리 (P4) | P1·P4 |
| `hub/app/main.py` | `startup` 에서 스케줄러 시작 전 `studio.prepare()`, 뒤 `studio.start()` | P1 |
| `hub/app/llm.py` | `STUDIO_JOBS`·`STUDIO_WAIT`·`IN_TURN` 컨텍스트 변수와 `NestedTurnError`, 대기자 등록부터 차례 기다리기까지 한 try(`hub/app/llm.py:254-271`), 빠져나가는 모든 길에서 `waiter.done`, 끝난 이유 (9.2, D22) | P1 |
| `hub/app/cli.py` | `studio-check`, `studio-import` | P2 |
| `hub/app/templates/base.html` | 메뉴에 "스튜디오"(쓸 수 있는 사용자에게만, 11.5), 관리자 메뉴에 "스튜디오 기록" | P1·P4 |
| `hub/app/video.py` | `render_in_turn`·`fix_in_turn` 꺼내기, `draft` 의 `revise` 인자. `render`·`fix` 의 인자와 결과는 그대로 (D22) | P3 |
| `hub/app/db.py` | `_GUARDS` 에 `users_home_check` | P4 |
| `hub/app/promptx.py` (선택) | `inspect` 의 파일 읽기·Pillow 일을 스레드로 (20.4). (P3 선택) 같은 차례에서 첫 화면 읽기를 하도록 차례 안 부분을 꺼낸다(5.6). 둘 다 D22 | P2 이후 |
| `CLAUDE.md` 코드 지도 | 새 모듈 줄 | P1 |

- 스케줄러(`scheduler.py`), 접수(`jobs.py`), 중계(`relay.py`), 패널(`panel.py`, `mgs_panel.js`, `mgs_nodes.js`), 실행 공간(`entrypoint.sh`)은 바꾸지 않는다. 단 D18 때문에 `entrypoint.sh` 와 `scheduler.py` 의 설명 파일 쓰기는 채팅 작업에 한해 P1 에서 고친다(14.2).
- 별도 작업으로 제안하는 것: 스케줄러의 `UPDATE … SET state='starting'` 에 상태 조건과 행 수 확인(5.9), LLM 차례에도 실행 공간 유지(D21).
- 업데이트 점검 항목에 `hub/requirements.txt` 의 pillow 판 올리기를 더한다. 허브가 사용자가 올린 이미지를 root 권한 프로세스에서 열기 때문이다(14.4).

### 19.3 새 설정값 (`.env`)

```bash
# ── 채팅 스튜디오 (docs/chat-studio-design.md) ──
# false 면 /studio 를 열지 않는다 (화면은 503 안내). 기본은 꺼짐: 7.2 를 마치고 시범 사용자를 넣은 뒤 켠다
STUDIO_ENABLED=false
# 채팅을 쓸 수 있는 사용자 (쉼표로 구분, 비우면 모두). P1 시범용 (D16)
STUDIO_ALLOWED_USERS=
# 채팅 작업이 실행되는 시스템 작업공간 (7.2 에서 만든다)
STUDIO_ENV_SLUG=studio-chat
# 업로드: 받는 크기(MB), 받는 긴 변, 저장할 때 줄이는 긴 변, 사용자별 용량(MB)
STUDIO_UPLOAD_MAX_MB=20
STUDIO_UPLOAD_MAX_SIDE=4096
STUDIO_UPLOAD_STORE_SIDE=2048
STUDIO_UPLOAD_QUOTA_MB=2048
# 업로드 볼륨(MGS_ENVS) 여유 공간이 이보다 작으면(GB) 업로드 거부, 보내지 않은 첨부를 지우기까지 시간
STUDIO_MIN_FREE_GB=50
STUDIO_UNSENT_HOURS=24
# 사용자당 동시에 진행하는 요청, 열린 요청(대기·진행·승인 대기), 분당 메시지
STUDIO_MAX_ACTIVE_PER_USER=2
STUDIO_MAX_OPEN_PER_USER=5
STUDIO_MAX_MESSAGES_PER_MIN=10
# 감독이 LLM 차례를 기다리는 최대 시간(초). 점검·정지 시간은 세지 않는다. 패널의 HELPER_WAIT_SECONDS 와 따로 둔다 (D14)
STUDIO_LLM_WAIT_SECONDS=3600
# LLM 차례 하나의 실행 한도(초)
STUDIO_LLM_TURN_SECONDS=900
# 같은 레시피로 접수 검증이 잇달아 실패하면 레시피를 끄는 횟수
STUDIO_RECIPE_FAIL_LIMIT=3
# 긴 영상 계획 확인(Ask) 기준(초). 0 이면 Ask 를 켠 대화의 모든 영상에 계획 카드
STUDIO_ASK_LONG_SECONDS=300
STUDIO_PLAN_TTL_HOURS=24
# 결과 검사: on / idle(대기열에 다른 ComfyUI 줄이 없을 때만) / off (D15). 검사에서 문제가 나오면 자동으로 다시 만드는 횟수
STUDIO_INSPECT=idle
STUDIO_AUTO_REROLL=0
# 진행 이벤트 최소 간격(ms)
STUDIO_PROGRESS_MIN_INTERVAL_MS=500
# 지운 대화를 실제로 지우기까지 날짜, 노드 시간 기록(node_timing) 보관 날짜
STUDIO_PURGE_DAYS=30
STUDIO_EVENTS_KEEP_DAYS=90
# 진행 메시지 원문 기록 (시험용, 관리자만). 폴더를 비우면 <MGS_ENVS>/state/<STUDIO_ENV_SLUG>/records, 7일 보관
STUDIO_RECORD_EVENTS=false
STUDIO_RECORD_DIR=
# (D20 결정 뒤) 허브 시간대. 결과 날짜 폴더에 쓰인다
# TZ=KST-9
```

- 값에는 공백·따옴표·#·$ 를 쓰지 않는다(`.env.example:3-4`). 허브는 시작할 때 한 번 읽으므로 바꾸면 허브 컨테이너를 다시 만든다(`hub/app/config.py:18-105`). 다시 만들 때는 7.2 의 6) 처럼 대기열을 멈추고 실행 중인 작업이 0 이 된 뒤에 한다.
- 숫자 가운데 [추정] 인 것: `STUDIO_UPLOAD_MAX_MB`, `STUDIO_UPLOAD_QUOTA_MB`, `STUDIO_MIN_FREE_GB`, `STUDIO_MAX_OPEN_PER_USER`, `STUDIO_MAX_MESSAGES_PER_MIN`, `STUDIO_LLM_WAIT_SECONDS`, `STUDIO_LLM_TURN_SECONDS`, `STUDIO_ASK_LONG_SECONDS`, `STUDIO_EVENTS_KEEP_DAYS`. P1 측정 뒤 다시 본다.
- 설정이 아니라 코드 상수로 두는 것: `DEFAULT_I2V_TEXT`(한국어 문장이라 `.env` 값 규칙에 맞지 않는다), `STUDIO_LLM_HARD_WAIT`(86400초), 결과 확정이 늦다고 보는 시간(600초 [추정], 5.12).

## 20. 참고

### 20.1 줄임말

| 줄임말 | 뜻 |
|---|---|
| [CUI] | 개발 PC 에 설치된 ComfyUI Desktop 0.42.3 의 백엔드 소스. 경로는 ComfyUI 폴더 기준. 메인은 v0.42.5 (`CLAUDE.md:34`) |
| [FE] | 같은 설치의 ComfyUI 프런트엔드 패키지 1.49.6 의 소스맵에 든 원본. 경로는 프런트엔드 저장소 기준. 패널은 1.57.1 번들로 확인했다(`CLAUDE.md:45`) |
| [TPL] | 같은 설치의 `comfyui_workflow_templates_json` 0.1.57 의 `templates/*.json` |
| DFC/0N:줄 | 러다이트 분석 문서 저장소 `Directing_follow_for_comfyUI` (HEAD `532b40c`) 의 `0N_*.md` |
| A1·A2 | 이 설계 전에(같은 날 대화에서) 사용자에게 낸 구조 제안의 두 항목. A1 은 구조도: 채팅 화면 ↔ WebSocket, 허브 안 감독(의도 → 제작진·도우미·장문·길이 자동·자동 고치기 → 레시피에 값 넣기 → 기존 대기열로 실행하고 진행 받기 → 결과 검사(VLM) → 채팅에 전달), ComfyUI 는 숨은 일꾼. A2 는 '그대로 쓰는 것' 목록: GPU 대기열, 진행 중계, `llm_turn`, 제작진 모드, 프롬프트 도우미, 영상 렌더러와 자동 고치기, 결과 검사, 사용자별 결과 폴더. A2 를 바꾸면 문서에 적고 사용자 결정을 받는다(D4, D22) |

### 20.2 러다이트 분석 문서

| 문서 | 이 설계에서 쓴 것 |
|---|---|
| DFC/01 화면 분석 | 채팅·결과 카드(19, 60-66), 감독과 경과 초(76), 계획 카드·승인 대기·취소(77-79), Ask/Auto(80), 대기열 공개·예상 시간(62, 65), 생성 상세의 영어 프롬프트(86), 공식 Remote MCP(46, 100-104) |
| DFC/02 프롬프트 실험 | 자동화 금지·IP 업로드 금지·학습 금지(9-11), Ask 계획 카드 실제 관찰(102-111), 예상과 실제 시간(129), 승인 뒤 흐름과 '더 빨리!'(131-132) |
| DFC/03 작업 흐름 분석 | 3층 구조(85-88), 시안은 Auto·본 작업은 Ask(39), 경계: 흐름만 참고해 새로 구현(104-107) |
| DFC/05 기술 가설 | 분석 문서는 공식 MCP 와 약관 12조의 관계·요금에 서면 확인이 필요하다고 적었다(58). 우리는 러다이트에 질의를 보내지 않는다(사용자 결정). MCP 연결은 이 설계 범위 밖이다 |

### 20.3 이 저장소의 문서

- `CLAUDE.md` — 바뀌지 않는 설계 원칙, 작업 규칙, 코드 지도.
- `docs/video-renderer.md` — 영상 렌더러, 장문 프로필, 길이 자동 선택, 자동 고치기, 메인 시험.
- `docs/prompt-helper.md` — 프롬프트 도우미와 결과 자동 검사.
- `docs/crew-mode.md` — 제작진 모드(B2)와 실측.
- `docs/main-gpu-server-design.html` — 설계서 v1.1 (공평한 순서, 공개 범위 제안, 재적재 시간 추정).
- `docs/handover-2026-10-23.md`, `docs/handover-2026-10-30-vpn.md` — 결정 기록, VPN·HTTP 판단.
- `docs/checklist-2026-10-27.md` — 검수 절차와 기대 시간.

### 20.4 이 설계를 쓰며 본 기존 코드의 문제 (참고)

- 패널의 결과 자동 검사는 `executed` 의 `images` 를 모두 검사 요청으로 보낸다(`runtime/nodes/mgs_bridge/web/mgs_panel.js:768-782`). `SaveVideo` 의 화면 출력은 `images` 칸이므로([CUI] `comfy_extras/nodes_video.py:203`, `comfy_api/latest/_ui.py:430-431`) 영상 파일까지 보내고, 허브는 이미지 확장자가 아니면 400 을 돌려준다(`hub/app/promptx.py:407-408`). 코드 경로로 확인된다(메인 화면에서는 아직 보지 않았다). 채팅 설계와는 따로 고칠 거리다.
- `llm_turn` 이 차례를 받지 못하고 빠져나가는 길(대기자 등록 뒤 insert·`notify_queue_changed` 도중의 취소·예외, 시간 초과·취소, `waiter.cancelled`)과 정리 중의 취소에서 `waiter.done` 이 세워지지 않아, 스케줄러가 최대 `JOB_MAX_SECONDS` 동안 차례를 쥘 수 있다(5.9). 채팅 P1 에서 고친다(9.2).
- 스케줄러는 `queued` 줄을 상태 조건 없이 `starting` 으로 바꿔, 그 사이 취소된 줄을 실행할 수 있다(`hub/app/scheduler.py:169`, `207-209`).
- `promptx.inspect` 는 결과 파일 읽기·base64·Pillow 자르기를 이벤트 루프에서 한다(`hub/app/promptx.py:522-524`, `537-539`). 안에 `llm_turn` 이 있어 부르는 쪽이 스레드로 감쌀 수 없으므로 promptx 를 고쳐야 한다.
- 패널의 긍정 노드 고르기는 Z-Image-Turbo 처럼 글 노드 하나가 0 조건 노드를 거쳐 negative 에도 닿는 구조를 'mixed' 로 보고 고르지 않는다(`runtime/nodes/mgs_bridge/web/mgs_nodes.js:90-94`, `140-151`). 그 템플릿에서는 '긍정 프롬프트 노드에 넣기'가 자동으로 노드를 찾지 못하고 선택한 노드에 넣거나 안내로 끝날 것이다 [추정: 메인에서 확인 안 함].
- `submit_comfy` 의 편집 공간 검증에서 연결 거부·시간 초과는 `SubmitError` 로 바뀌지 않아, 기존 ComfyUI 화면은 이때 500 을 낸다(`hub/app/jobs.py:61-68`, `hub/app/proxy.py:91-98`).


## 21. 구현 결과와 설계와의 차이 (2026-11-13 갱신)

P1~P4 를 구현해 메인(192.168.10.29)에서 시험했다. 이 문서의 0~20절은 설계 시점(2026-11-12) 기준으로 두고, 구현에서 달라진 것과 잰 값은 이 절에 모은다. 쓰고 운영하는 방법은 `docs/chat-studio-guide.md` 에 있다.

### 21.1 단계별 상태

| 단계 | 구현 | 메인 확인 (사용자 확인 또는 관리자 화면) |
|---|---|---|
| P1 채팅 → 사진 → 영상 → 진행 → 결과 | 완료 | 영상 생성·진행 카드·사진만 보내기·새로 고침·취소·대기·일시 정지·`studio` 로그인 차단·레시피 시작 검사·프롬프트 숨김(D18, 영상 파일에서 문자열이 나오지 않음) 확인. 작업 기록·이벤트·결과 파일·단계별 시간(4·5·7·15 일부)은 관리자 화면으로 확인했다(21.3.2). 사용자 간 격리(10), 1인칭 안내 거르기(6), 일반 패널 영향(13)은 정식으로 확인하지 못했다 |
| P2 이미지 생성·의도 규칙·이걸로 영상 | 완료 | 이미지 생성, [이걸로 영상 만들기], "더 밝게"·"하나 더", 영상 뒤 편집 말의 정해 둔 답, 준비 중 안내, `studio-check` 4개 레시피 V1~V13 통과 |
| P3 글→영상·이미지 편집·영상 고치기 | 완료 (한 차례 묶기 제외) | 글→영상(비율·5초 상한 안내), 이미지 편집, 영상 고치기 확인 |
| P4 길이·비율 칸·운영자 화면·Ask 계획 카드 | 완료 (LLM 의도 판단·제목 자동 제외) | 선택칸, 계획 카드(스위치→카드→승인→영상), `/admin/studio` 목록·상세 확인 |

main 에는 `chat-studio-p1`~`p4` 가 PR #1 로 합쳐졌다(커밋 19개, 제작진 모드·영상 렌더러 `crew-b2` 포함).

### 21.2 설계와 달라진 점

감독·의도·레시피
1. **승인의 상태 전이**(5.2, 5.7): 설계는 `awaiting_approval → active/queue` 로 바로 갔다. 구현은 `awaiting_approval → pending` 으로 바꾸면서 같은 UPDATE 로 `work.approved` 를 남기고, 시작은 `pump` 가 사용자 활성 한도를 지켜 한다. 승인과 `approved` 기록 사이에 허브가 죽어도 다시 계획하지 않고, 승인이 한도를 우회하지 않는다. 취소·만료·새 메시지는 `awaiting_approval` 상태에서만 계획을 접는다(승인돼 도는 요청은 건드리지 않는다). 승인 대기 요청은 같은 대화의 뒤 요청을 막지 않는다.
2. **승인 대기 중 새 메시지**(5.7): 설계는 모두 계획 수정으로 보았다. 구현은 "응"·"네 진행해줘" 같은 말은 승인으로, "고마워" 같은 답 말은 계획을 그대로 두고 답만, 새 사진이 붙은 메시지는 새 요청(계획 유지)으로 처리한다. 그 밖의 글만 계획 수정이다. 계획 수정은 열린 요청 한도에서 뺀다.
3. **의도 규칙**(5.3): `later`(나중 단계라 막기) 표시는 없앴다. 정한 action 의 레시피가 없거나 꺼졌을 때만 "준비 중"으로 답한다. 보강한 것: 카메라·속도 말("천천히")은 사진이 있거나 직전 결과가 이미지일 때만 영상 말로 본다(사진 없는 "느리게 흐르는 강"이 글→영상으로 가지 않게). 편집 말은 강("바꿔"·"지워"…)과 약("넣어"·"추가해")으로 나눠 약은 "그려줘"와 함께 오면 새 그림 요청으로 본다. 영상 뒤에 "그려"·"새로"가 있으면 고치기(R6)·편집 안내(R7b)로 가로채지 않는다. R2(인사·질문)는 40자 이하 글만, 사진이 없을 때만 적용한다. R5 는 "영상으로 다시 만들어줘"에도 걸린다. '애니메이션 스타일'·'움직이는 느낌'·'클립보드'는 영상 말이 아니다.
4. **길이·비율 칸**(D17): 2·3·4·5초와 가로·세로·정사각 중에서 고르고 칸이 글의 지정보다 우선한다. 사진으로 만드는 영상은 사진 비율을 따르고 안내가 붙는다. 길이가 바뀐 "다시 만들기"는 저장된 프롬프트를 쓰지 않고 새로 쓴다.
5. **영상 고치기**(5.8): 앞 요청이 읽어 둔 첫 화면(`work.frame`)을 `video.render` 의 새 선택 인자 `frame` 으로 넘겨 8B 차례를 아낀다. 읽기에 실패해 빈 값이 저장된 경우는 다시 읽는다. 초안은 이전 네 단락에서 시작하지 않고 원래 요청 + 이번 말로 새로 쓴다(단순화).
6. **LLM 차례 묶기 미구현**(5.6, 16절 P3): `video.render_in_turn`·`fix_in_turn` 꺼내기는 하지 않았다. 영상 요청의 LLM 차례는 8B 첫 화면 읽기 → 27B 프롬프트(→ 필요하면 27B 고치기) 그대로다. 기능에는 영향이 없다.
7. **결과 검사**: 레시피의 `inspect.output` 으로 검사할 출력을 고른다(영상은 가운데 프레임 `check`, 이미지·편집은 `main`). 두 사람 요청인데 사람이 1명으로 보이면 한 줄 안내를 한다.
8. **결과 → 입력 복사**(7.3): 결과를 받을 때 `bytes`·`sha256` 을 남기고, 복사할 때 다르거나 지문이 없으면 복사하지 않는다("결과 파일이 바뀌어 이어서 만들 수 없습니다").
9. **복구**(5.10): P2 로 미뤘던 것을 구현했다. LLM 단계에서 멈춘 요청은 1회 자동 재개(그다음은 `interrupted`), `lost` 는 결과가 없을 때 같은 값으로 1회 재접수, `recover` 는 이미 돌고 있는 요청을 다시 돌리지 않는다.
10. **계획 카드의 예상 시간**(8.5): 최근 끝난 GPU 작업 50건의 종류별(comfy·llm) 중앙값으로 내 앞 줄을 더하고, 내 작업은 레시피 `estimate.gpu_seconds`(프레임 수로 보정)로 한다. 계산에 실패하면 계획 없이 진행한다.

레시피
11. **레시피 4개 모두 메인의 Export (API) 파일 기준**이다(I2V·T2V·편집·Z-Image 이미지, Z-Image 는 2026-11-13 에 개발 PC 안을 메인 Export 로 바꿨다: 차이는 `ModelSamplingAuraFlow` 의 `sampling: flow` 입력 하나였다). 길이를 계산하는 수식 노드(`ComfyMathExpression`)는 지우고 `frames` 를 숫자로 넣는다. 4스텝 LoRA 스위치(`PrimitiveBoolean`)는 `fixed` 로 켠 채 고정한다. 편집 레시피는 두 번째 LoadImage 와 `image2` 입력을 지운다.
12. **V11 은 화면 전용 입력을 건너뛴다**: Export 파일에 들어 있지만 ComfyUI 백엔드는 모르는 입력(`video-preview` 등, `FRONTEND_ONLY_INPUTS`)은 입력 이름 검사에서 뺀다. 점 이름(`format.codec`)은 첫 마디만 본다.
13. `studio-import` 는 저장 노드가 둘이면 `SaveVideo` 를 main, 이미지 저장 노드를 검사 프레임(`check`)으로 잡는다. 길이가 수식 링크면 프레임 자리를 만들지 못하고 사람에게 알린다.

기록·화면·그 밖
14. **운영자 기록**(12절): `node_timing`(노드별 걸린 시간)은 P1 에서 빠졌다가 P4 에서 구현했다. `chat_turns.stage` 는 단계가 바뀔 때마다 갱신하고, 시작 단계는 종류에 맞게 정한다(사진 영상만 `frame`). 날짜 거르기는 문자열이 아닌 `date` 로 넘긴다. `/admin/studio*` 응답에 CSP·nosniff 헤더를 붙인다(14.3).
15. **P1 의 버그 수정**: 감독이 진행 카드에 보이는 글과 사용자의 요청 글을 같은 변수(`ctx.text`)로 써서, 자동 고치기에 진행 글이 넘어갈 수 있었다. 요청 글은 `ctx.req_text` 로 나눴다.
16. **`llm.py`**(9.2): 설계대로 `STUDIO_JOBS`·`STUDIO_WAIT`·`IN_TURN`, 대기자 등록부터 한 try, 모든 길에서 `waiter.done`, 끝난 이유 기록을 넣었다. 허브가 종료되며 취소된 감독 작업은 요청을 `cancelled` 로 바꾸지 않고 `active` 로 남긴다.

### 21.3 메인에서 잰 값 (첫 실측)

채팅 요청 한 건씩 대기열이 비어 있을 때의 관리자 화면(`/admin/studio`) 값이다. 한 건의 상세와 목록 요약이므로 표본이 적다. [추정]을 바꾸려면 17.3 방식으로 더 모아야 한다.

사진 영상 한 건(I2V, 640×640·81프레임·4스텝 LoRA, 이미지에서 이어 만듦)의 GPU 줄:

| 단계 | 모델·종류 | 대기 | 콜드 스타트 | 실행 | 정리 | VRAM 최대 | 비고 |
|---|---|---|---|---|---|---|---|
| 첫 화면 읽기 | 8B (qwen3-vl) | 2.5초 | - | 4.3초 | 1.6초 | 9.6GB | 토큰 1227/124 |
| 프롬프트 작성 | 27B (qwen3.8) | 1.6초 | - | 13.1초 | 0.6초 | 20.2GB | 토큰 4190/773, 길이 선택 보통(mid) |
| 생성 | ComfyUI Wan I2V | 0.7초 | 9.9초 | 41.9초 | 0.6초 | 40.0GB | |
| 결과 검사 | 8B | 0.01초 | - | 7.6초 | 0.3초 | 9.8GB | 결과를 보낸 뒤 |

요청 전체(대기열이 비어 있을 때, 사용자가 결과를 보기까지):

| 흐름 | 걸린 시간 | GPU 차례 | 5.6 의 [추정] |
|---|---|---|---|
| 이미지 생성 (27B 확장 + Z-Image + 검사) | 약 30~38초 | 3 | 약 45초~2분 30초 |
| 사진 영상 (8B + 27B + Wan + 검사) | 약 70~76초 | 4 | 약 2분 30초~7분 |
| 글→영상 (27B + Wan T2V + 검사) | 약 73초 | 3 | 약 2분 30초~5분 |
| 다시 만들기 (영상, Wan + 검사) | 약 52초 | 2 | 약 1분 40초~2분 10초 |

설계의 [추정]보다 전체적으로 짧게 나왔다. 대기열이 비어 있었고 4스텝 LoRA 를 쓴 구성이다. 여러 사람이 동시에 보내면 뒤 사람은 앞 요청들의 GPU 시간 합만큼 기다린다(5.6).

#### 21.3.1 표본을 늘린 값 (2026-11-16, 관리자 화면 상세 8건)

위 한 건 상세에 이미지 3건, 글→영상 2건, 사진 영상 1건, 영상 다시 만들기 1건을 더한 값이다. 모두 대기열이 비어 있을 때(대기 0~2.5초)의 채팅 요청이다. 단위는 초이다.

| 항목 | 값 (표본) | 비고 |
|---|---|---|
| ComfyUI 콜드 스타트 (실행 공간 새로 띄우기) | 9.7~10.7 (7건) | 설계의 [추정] 10~40초보다 짧고 일정하다 |
| Wan 14B 영상 생성 실행 (640×640·81프레임, 4스텝 LoRA) | 39.0~41.8 (5건) | 샘플러 1단계 약 16초, 2단계 약 16초, VAE 디코드 약 4초 (글→영상 한 건의 노드 시간) |
| Z-Image 이미지 생성 실행 | 6.3~9.8 (3건) | 1024×1024, 8스텝 |
| 정리 (모델 내리기·메모리 확인) | 0.3~1.6 | 차례마다 |
| 27B 이미지 프롬프트 (제작진 + 확장) | 11.7~17.4 (3건) | 토큰 약 2,600~4,800 / 700~1,400 |
| 27B 영상 프롬프트 | 13.1 (보통) ~ 18.3~25.5 (장문) | 장문일수록 길다. 토큰 입력 4,000~7,900 / 출력 770~1,700 |
| 8B 첫 화면 읽기 | 4.2~4.3 (2건) | 토큰 약 1,230 / 100~120 |
| 8B 결과 검사 | 4.4~7.6 (7건) | 글→영상은 사람이 없어 4.4, 사람이 있는 사진은 6~7.6 |
| 대기 (앞 줄) | 0.01~2.5 | 대기열이 비어 있었다 |

GPU 메모리 최대(VRAM): 8B 약 9.6~9.9GB, 27B 약 20.2GB, Z-Image 약 21.8GB, Wan 14B 약 40.0~40.1GB. 72GB 한 장에서 모두 여유가 있다.

요청 전체(결과를 보기까지, 승인 대기 제외): 이미지 약 30~38초, 사진 영상 약 70~76초, 글→영상 약 73초(장문 약 105초 중 계획 승인 대기 포함), 다시 만들기 약 52초. GPU 를 쥔 시간의 합은 영상이 약 70초, 이미지가 약 30초이다. 시간당 처리는 영상 약 45~50건, 이미지 약 100건으로 5.6 의 [추정]보다 크다(대기열이 비어 있고 사용자가 한 명일 때).

설계 [추정]과 달라진 점: ① 콜드 스타트가 짧고 일정하다(약 10초). ② 14B 영상 생성 실행이 약 40초로 지난 시험(약 1분 반, 모델 적재 포함 여부 불명)보다 짧다. ③ 27B 장문 영상 프롬프트는 20초대, 보통은 10초대이다. ④ 검사 차례는 5~7초다. 8B 차례도 실행 공간을 끄므로(5.6) 결과 뒤의 다음 ComfyUI 작업이 콜드 스타트를 한 번 더 한다는 비용은 여전히 맞다(약 10초). 표본이 적으니 여러 사용자가 쓰는 시간대의 값은 더 모아야 한다.

#### 21.3.2 완료 기준 확인 (P1, 16절)

관리자 화면(`/admin/studio` 상세)으로 확인한 것: 4(요청의 `chat_turn_jobs` 줄과 `jobs.seq` 순서: 첫 화면 읽기 → 프롬프트 → ComfyUI → 검사, 사용자는 채팅 사용자), 5(기록 `render`·`comfy_submitted`·`node_timing`·`output`·`inspect`·`llm_job` 끝난 이유), 7(결과 파일 경로와 검사 프레임), 15 일부(단계별 시간, 위 표). 제품·공간 요청의 제작진(`work.crew.used`)과 파일럿 요청의 건너뜀 이유는 아직 보지 못했다.

### 21.4 알려진 한계와 남은 일

- **정식으로 확인하지 못한 완료 기준**: P1 의 6·10·13(1인칭 안내 거르기, 사용자 간 격리, 일반 패널 영향)과 제작진 사용 기록(5 일부). 4·5·7·15 는 21.3 에서 확인했다. 실제 DB·웹 서버를 거치는 자동 시험은 없다. 개발 PC 시험은 가짜 DB·가짜 부품이다.
- **미구현**: LLM 의도 판단(R10)·첨부 역할 판단·대화 제목 자동(P4), `render_in_turn` 한 차례 묶기(P3), 검사 차례 꺼내기(D22 ③), 시각 라벨·시간대.
- **사용자 결정 대기**: D11 콘텐츠 안전 낱말 목록(권장안은 "넣음"인데 낱말 규칙이 비어 있다), D20 허브 시간대(`.env` 의 `TZ=KST-9` 한 줄이면 되고, 지금은 UTC 라 한국 오전 9시 전 결과가 전날 폴더에 들어간다).
- **결정 상태**: D1a 작업공간 없는 사용자는 받지 않음, D1b 로그인 첫 화면(`users.home`, CLI `set-home`) 구현, D8 자동 재생성은 하지 않음, D17 길이·비율 칸 구현, D18 프롬프트 숨김(영상 확인).
- **레시피 유지**: 노드를 설치하면 `studio-check` 를 다시 돌린다. 템플릿 판이 바뀌면 안쪽 노드 id 가 바뀔 수 있어 `studio-import` 로 다시 만든다(6.7).
