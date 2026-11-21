# 레퍼런스 체계 설계: 화풍·인물·구도 (2026-11-21)

> 상태: **구현함(12절), 메인 실기기 확인 전.** 11-21 메인 실험(9절 R1~R6) 결과는 11절. 두 단계 방식은 사람 수·배치·장소를 지켰고,
> ②에 배경 문장이 꼭 필요하며, 화풍 보드는 효과가 작아 기본 흐름에서 뺀다.
> 앞선 설계: `docs/scene-chaining-design.md` (장면 잇기. 13절이 지금 구현된 '기준 그림으로 새로 그리기', PR #41).

## 0. 요약

사용자 요구(11-21): **화풍은 고정하고, 장면의 구성 요소(인물 수·자리·카메라·행동)는 자유롭게 바꾼다.**
레퍼런스를 세 가지로 나누고, 그림 세 장을 그냥 넘기는 것이 아니라 **각 그림의 역할을 모델에 구분해서 전달**한다.
마지막 프레임은 이어지는 장면에서만 시작 화면으로 쓴다.

| 레퍼런스 | 담는 것 | 지금 |
|---|---|---|
| A 공통 화풍 기준 (화풍 보드) | 화풍·질감·빛·색. 특정 인물이나 구도의 영향이 없는 자료 | 없음 (화풍 문장뿐) |
| B 인물 기준 | 인물마다 얼굴·체형·머리·옷 | 있음 (`hub/app/cast_library.json`) |
| C 구도 그림 | 다음 장면의 인물 수·자리·방향·자세·카메라. 이전 장면을 따라가지 않음 | 없음 (글로만) |

제안:

1. 키프레임을 **두 단계**로 그린다.
   ① 구도 그림: 화풍 보드 + 장소 그림 + 구도 글 → 우리 화풍의 장소에 얼굴 없는 인물 형상이 정확한 수와 자리에 있는 그림.
   ② 키프레임: ①의 그림을 바탕(Picture 1)으로, 인물 기준 그림(Picture 2·3)에서 얼굴·머리·옷만 입힌다.
2. 그림의 역할은 세 가지로 구분해 전달한다: 그림 번호표(Picture N)에 묶은 역할 문장, 바탕 그림(Picture 1)이 지켜지는 성질을
   구도에 쓰기, 단계마다 그림 3장 이내.
3. 화풍 고정은 세 겹: 화풍 LoRA(나중) + 같은 화풍으로 만든 화풍 보드·인물 그림 + 화풍 문장과 검사.
4. 마지막 프레임: 이어지는 컷의 시작 화면으로만 쓴다. 그 밖의 컷에는 빛·색·시간대를 글로 넘기고 색을 맞춘다.

## 1. 왜 (경과)

- 11-20 PR #40: 모든 컷을 앞 장면 끝 프레임을 고쳐 이었다. 첫 영상(요청 9089e3c7, 니키·베스·제임스 아침 식탁)에서
  앞 프레임의 사람·배치가 남았다 (베스 클로즈업에 니키·제임스가 남고, 부모 장면에서 니키가 베스 닮은 사람이 됨).
- 11-20 메인 시험 N1~N3 → 11-21 PR #41: 이어지지 않는 컷은 인물 기준 그림(+장소 그림)으로 새로 그린다.
  사용자 승인: "화풍이 다 동일해 이렇게만 영상이 나오면 돼".
- PR #41 로도 남는 것:
  - **사람 수:** 구도를 글로만 정해 씨앗에 따라 틀린다 (N1 씨앗 2 는 베스가 둘, N3 씨앗 2 는 니키 모자·옷이 빠짐).
    지금은 검사하고 다섯 번까지 다시 그려 막는다.
  - **화풍 자료:** 사람 없는 화풍 그림이 없다. 화풍은 화풍 문장과 인물 그림에 기댄다.
  - **모델 섞임:** 장소 그림과 사람 없는 장면은 글→그림 모델(Qwen-Image 2512), 인물 장면은 편집 모델(Qwen-Image-Edit 2511)이다.
  - **그림 3장 한도:** 인물이 셋이면 장소 그림을 넣을 자리가 없다.

**정정 (scene-chaining-design 13절):** "빈 잠재로 바꿔 고쳐졌다"는 설명은 맞지 않다. 샘플러가 디노이즈 1 로 시작하면 흐름(flow)
모델은 첫 단계가 잡음뿐이라 시작 잠재의 내용이 남지 않는다 (ComfyUI `noise_scaling`: σ·잡음 + (1−σ)·잠재, 첫 σ = 1)
[코드 기준 추정]. 고쳐진 까닭은 **앞 프레임을 그림 목록에서 뺀 것**이고, 빈 잠재는 출력 크기를 영상 비율로 정하는 역할이다.
그래서 구도 그림 C 는 잠재가 아니라 그림(Picture 1)으로 넣어야 효과가 있다.

## 2. 레퍼런스 세 가지와 장소 그림

### A 공통 화풍 기준 — 화풍 보드

- 담는 것: 붓 터치·질감·음영 방식·색 팔레트·빛, 재질 조각(옷감·머리카락 결·소품).
- 담지 않는 것: 사람 얼굴, 전신, 알아볼 만한 장면 배치 (구도가 따라오지 않게).
- 만드는 때: 화풍마다 한 번 (반실사·애니·실사). **우리 확정 컨셉아트에서만** 자른다 (다른 작품 그림은 저작권 문제).
- 형태: 조각 여러 개를 모은 한 장짜리 보드 [TBD: 한 장 vs 여러 장 중 고르기, 9절 R1].
- 저장: 라이브러리에 화풍 열쇠별로 (예: `cast_library.json` 의 `styles.semi.board`) [TBD].
- 후보 (11-20 화풍 세트 초안, 사람이 없는지 확인 필요): 서버 입력 `styleset/texture_niki_cape.png`(망토 질감),
  `styleset/light_beth_greenhouse.png`(빛, 베스가 보이면 제외), 결과 `admin/main/2026-11-20/styleset/` 의
  `bg_forest_00001_.png`·`bg_greenhouse_00001_.png`·`bg_kitchen2_00001_.png`.

### B 인물 기준 — 있음

- 인물마다 화풍별 기준 그림(`refs.semi`)과 알아볼 소품(`signature`): 니키·푸토르·베스·제임스·제임스(안경).
  다른 화풍 영화는 반실사 그림을 그 화풍으로 바꿔 만든다 (`director._style_convert`).
- **화풍 보드와 같은 화풍이어야 한다.** 얼굴·머리·옷을 그리는 방식은 B 가 실어 나르므로(5.2절), 둘이 다르면 ②에서 두 화풍이 섞인다.

### C 구도 그림 — 컷마다 새로

- 담는 것: 사람 수, 각자의 자리(왼쪽·가운데·오른쪽, 앞·뒤), 방향, 자세, 카메라(샷 크기·높이), 장소 안의 배치.
- 담지 않는 것: 얼굴·정체성 (얼굴 없는 형상). 이전 장면을 따라가지 않는다.
- 만드는 법: 4절 ①.

### 장소 그림 (보조)

- 장소마다 사람 없는 그림 한 장 (PR #41 의 `director._place_card`).
- **화풍 보드에서 같은 편집 모델로 그리게 바꾼다.** 지금은 Qwen-Image 2512 글→그림이라 붓질·색이 다를 수 있다.

## 3. 역할을 모델에 구분해 전달하는 방법

### 3.1 그림 번호표 + 역할 문장

편집 모델의 글 인코더(Qwen2.5-VL)는 그림마다 "Picture 1:", "Picture 2:" 꼬리표가 붙은 채로 그림을 본다 (공식 파이프라인
`QwenImageEditPlusPipeline` 과 ComfyUI `TextEncodeQwenImageEditPlus` 의 형식 [서버 ComfyUI 버전 코드에서 확인]).
지시문은 같은 말로 그림마다 역할 하나를 적고, 가져올 것과 가져오지 말 것을 함께 쓴다. 지금 코드는 "image N" 이라고 쓰므로
"Picture N" 으로 맞춘다 (9절 R5 로 차이 확인).

역할 문장 예 (실제 글은 영어로 코드가 만든다):

| 역할 | 문장 |
|---|---|
| 화풍 보드 | Picture 1 is a style board. Use it only for brushwork, texture, shading, colour palette and light. Do not copy any object, person or layout from Picture 1. |
| 장소 | Picture 2 shows the place. Keep its architecture, furniture and props. |
| 구도 (바탕) | Picture 1 is the layout. Keep exactly its camera, the number of figures, their positions and poses, the place, the light and the painting style. |
| 인물 | The faceless figure on the left becomes the woman in Picture 2: take only her face, hair, body and clothes, including the (signature). Nothing else from Picture 2. |

### 3.2 바탕 그림이 지켜지는 성질을 구도에 쓰기

편집 모델은 Picture 1 의 내용을 지키려 한다. 11-20 에 앞 프레임을 Picture 1 로 넣었을 때 그 사람·배치가 남은 것이 이 성질이다
(공식 시스템 지시문도 "원본과의 일관성 유지"를 요구한다 [확인 필요]). 오류의 원인이었던 이 성질을 거꾸로 쓴다:
**정확한 수의 형상이 정확한 자리에 있는 C 를 Picture 1 로 두면, 사람 수와 배치를 붙잡는 쪽으로 작동**할 것으로 본다 [추정, R3].
(11-21 실험: 사람 수와 자리는 지켜졌지만, 인물이 한 명이면 기준 그림의 단색 배경이 바탕을 이겼다. ②에 배경 문장을 넣어 고쳤다 — 11.1절)

### 3.3 단계 나누기

편집 노드 한 번에 그림은 3장까지다. 화풍 보드(1) + 인물(1~3) + 장소(1) + 구도(1)를 한 번에 넣을 수 없으므로
단계마다 3장 이내로 나눈다 (4.3절).

## 4. 키프레임 흐름 (장면 전환 관리)

```
준비 (화풍마다 한 번)   화풍 보드 A · 인물 기준 그림 B (같은 화풍) · (나중) 화풍 LoRA
                        장소 그림: 장소마다 한 장 (A 에서 편집 모델로)
컷마다
 1 이전 장면 분석       이어지는 샷(continue)이면 마지막 프레임 = 시작 화면 (여기서 끝)
                        아니면 마지막 프레임의 빛·색·시간대를 글로 (이어짐 메모, 6절)
 2 다음 장면 요구       구성안의 인물·샷·장소·화풍 + LLM 구도 명세 (누가 어디에, 방향, 자세)
 3 레퍼런스 고르기      규칙: A = 영화 화풍, B = 장면 인물 (말하는 사람 먼저), 장소 그림 = 장면 장소
 4 ① 구도 그림         Picture 1 = A, Picture 2 = 장소 그림 → C (얼굴 없는 형상, 정확한 수)    검사: 사람 수·화풍
 5 ② 키프레임          Picture 1 = C, Picture 2·3 = 인물 → 키프레임                           검사: 사람 수·인물 소품·화풍
 6 ③ (세 사람)         Picture 1 = ② 결과, Picture 2 = 세 번째 인물
 7 색 맞추기            같은 장소·시간이면 앞 장면 마지막 프레임에 색·밝기를 맞춤 (CPU)
 8 끝 키프레임          지금처럼 (키프레임을 같은 샷의 나중 순간으로 편집)
 9 영상                 LTX(지금) 또는 Wan: 첫 화면 + 끝 키프레임
```

### 4.1 레퍼런스 고르기 (Reference Selector)

고르는 일은 규칙으로 한다. LLM 이 필요한 것은 2의 구도 명세뿐이다.

| 레퍼런스 | 이어지는 샷 | 새 샷·장소 바뀜 |
|---|---|---|
| 화풍 보드 A | 안 씀 | ①에서 씀 |
| 인물 기준 B | 안 씀 (끝 키프레임에서만) | ②③에서 씀 |
| 장소 그림 | 안 씀 | ①에서 씀 |
| 이전 마지막 프레임 | **시작 화면** | 그림으로 안 씀 (이어짐 메모와 색 맞추기만) |
| 구도 그림 C | 안 만듦 | ①에서 만듦 |

### 4.2 구도 명세 (LLM)

장면표의 한 장면 → JSON. `camera`(샷 크기·높이), `figures`(장면 인물마다 `id`, `side` left/center/right, `depth` front/back,
`facing`, `pose`, `action`). 사람 수와 id 목록은 코드가 장면 인물에서 정하고, 명세가 다르면 고쳐 쓰게 한다 (수를 LLM 에 맡기지 않는다).
형상의 차례(왼쪽→오른쪽)가 ②의 인물 대응이 된다.

### 4.3 그림 자리 (3장 한도)

| 장면 인물 | ① 구도 그림 | ② 키프레임 | ③ |
|---|---|---|---|
| 0명 | A, 장소 그림 (형상 없음) → 그대로 첫 화면 | — | — |
| 1명 | A, 장소 그림 | C, 인물 1 (한 자리 남음: A 를 더 넣을지 R6) | — |
| 2명 | A, 장소 그림 | C, 인물 1, 인물 2 | — |
| 3명 | A, 장소 그림 | C, 인물 1, 인물 2 | ② 결과, 인물 3 |
| 4명 이상 | 같음 | 같음 | 한 명씩 반복하거나 엑스트라는 글로만 [TBD] |

### 4.4 레시피

새 레시피는 필요 없다. ①②③ 모두 PR #41 의 `qwen_image_compose_ref` (Picture 1~3, 출력 크기 = 영상 크기)를 쓴다.
디노이즈 1 이라 시작 잠재는 크기만 정하므로 (1절 정정), Picture 1 이 바탕 그림(C)이어도 같은 그래프로 된다.
바뀌는 것은 그림 순서와 지시문이다.

### 4.5 검사

- ① 뒤: 사람 수 **정확히** (형상 수), 몸의 이상, 화풍. 인물이 없는 단계라 다시 그리기가 싸다. 사람 수는 여기서 확정한다.
- ②③ 뒤: 사람 수가 ①과 같은지, 인물마다 알아볼 소품 (`checks.cast_problems`, PR #41), 화풍.
- 다시 그리기 횟수: ①은 [TBD, 예: 5], ②는 PR #41 과 같은 다섯 번.

### 4.6 시간 [추정]

컷마다 편집 한 번과 검사 한 번이 늘어 약 35초 (편집 약 17초 + 검사 약 18초). 세 사람 장면은 ③으로 35초 더. 다시 그리기는 따로.

## 5. 화풍 고정

### 5.1 화풍이 정해지는 곳과 이어지는 곳

| 화풍 부분 | 정해지는 곳 | ① 구도 그림 | ② 키프레임 |
|---|---|---|---|
| 배경·빛·색·질감 | 화풍 보드 A | 입힘 | C 에서 이어받음 |
| 얼굴·머리카락·옷 표현 | 인물 기준 그림 B | 없음 (얼굴 없는 형상) | B 에서 들어옴 |

- 화풍이 실제로 정해지는 곳은 **준비 단계** (A·B 를 같은 컨셉아트로 만들 때)이고, ①②는 그 화풍을 적용한다.
- "이어받는다"는 복사가 아니다. ②는 C 를 보며 그림 전체를 다시 그리므로 조금 흔들릴 수 있어, 화풍 문장과 검사를 둔다.
- 컷마다 같은 A·B 에서 다시 시작하므로, 마지막 프레임을 이어 쓸 때처럼 흔들림이 컷을 거듭하며 쌓이지 않는다.
- 약한 점: 화풍을 직접 넣는 곳이 ① 하나라서, ①이 어긋나면 ②가 물려받는다 → ① 검사, 나중에 LoRA.

### 5.2 배경만으로는 고정되지 않는다

| 화풍 요소 | 배경 조각 (A) | 인물 기준 그림 (B) |
|---|---|---|
| 붓 터치·질감 | 전함 | 전함 |
| 색·빛 | 전함 | 일부 |
| 얼굴 그리는 방식 (눈·코·입, 피부) | 못 전함 | 전함 |
| 머리카락·옷 주름 | 재질 조각을 넣으면 일부 | 전함 |
| 몸 비율 | 못 전함 | 전함 |

화풍이 바뀌었다고 느끼는 것은 대부분 인물 쪽이다 (11-20 지적 "클로즈업은 3D, 넓은 장면은 2D"도 인물의 윤기·입체감 차이였다).
그래서 화풍 보드에는 재질 조각을 함께 담고, 인물 기준 그림은 화풍 보드와 같은 화풍으로 맞춘다.

### 5.3 세 겹

| 겹 | 어디에 | 성격 | 상태 |
|---|---|---|---|
| 화풍 LoRA | 모든 그림 단계 (①②③, 끝 키프레임, 장소 그림, 화풍 변환) | 모델에 새김 | 나중 (7절) |
| 같은 화풍의 A·B | ① (A), ②③ (B) | 보여 주기 | 이 설계 |
| 화풍 문장·검사 | 모든 단계 | 크게 어긋난 것 다시 그리기 | 있음 (`video.STYLE_KEEP`, `checks.style_negative`) |

- 모델을 섞지 않는다: 장소 그림과 사람 없는 장면도 같은 편집 모델 + 화풍 보드로 그린다.
- 영상 모델(LTX) 안에서 생기는 변화는 첫 화면과 끝 키프레임으로만 붙잡는다 (Qwen 용 LoRA 는 LTX 에 걸리지 않는다).

## 6. 마지막 프레임의 쓰임

- 이어지는 샷 (continue, 앞 끝 프레임이 검사 통과): 시작 화면 그대로 (지금과 같음).
- 그 밖의 컷: 그림으로 넣지 않는다 (사람·배치가 따라온다, 1절). 대신
  - **이어짐 메모:** 시각 모델이 마지막 프레임의 빛 방향·색 온도·시간대·날씨를 짧은 영어 글로 읽어 ①의 지시문에 넣는다.
    장소·시간이 바뀌는 컷(jump)에서는 쓰지 않는다.
  - **색 맞추기:** 같은 장소·시간의 컷이면 새 키프레임의 색·밝기 분포를 앞 장면 마지막 프레임에 맞춘다 (평균·분산 맞추기, CPU).
    scene-chaining 설계에 '색 평균 맞추기'로 남아 있던 것이다.
- 끝 프레임 검사(`end_fail`)와 끝 키프레임 규칙은 그대로 둔다.

## 7. 화풍 LoRA (나중 단계, 선택)

- **무엇:** 우리가 직접 학습하는 화풍 LoRA. 확정 반실사 화풍을 편집 모델(Qwen-Image-Edit 2511)에 새긴다.
  - 지금 레시피에 있는 Lightning 4스텝 LoRA(속도용)와 다르다.
  - 인물 LoRA 는 하지 않는다 (인물 기준 그림이 맡는다).
  - 받아 쓰는 화풍 LoRA 는 우리 화풍과 맞지 않고, 다른 작가 그림으로 학습된 것은 저작권 위험이 있어 쓰지 않는다.
- **데이터:** 확정 반실사 컨셉아트 (반실사 48컷 등). 화풍마다 하나.
  - 캡션은 내용(누가·어디서·무엇)만 적고, 화풍에 대한 말은 빼거나 고유 단어 하나로 묶는다.
  - 니키 그림에 치우치면 니키의 얼굴·모자까지 배워 다른 인물이 니키를 닮아 간다 → 배경·소품·여러 인물을 섞고, 캡션에 인물 외형을 적는다.
  - 러다이트 결과물(약관상 학습 금지)과 다른 작품의 그림은 넣지 않는다.
- **넣는 곳:** 편집 레시피의 Lightning LoRA 노드(`170:153`, LoraLoaderModelOnly) 뒤에 LoRA 노드 하나. 세기 [TBD].
- **정할 것:**
  - 학습 도구 (예: AI-Toolkit. 2511 지원 여부 확인).
  - 메인 GPU 몇 시간 [추정]. 모든 GPU 사용은 대기열(DB)을 거쳐야 하는데 허브는 ComfyUI 작업만 돌린다 → 학습 작업을 대기열에 넣는 방법.
  - 결과 파일은 `model-inbox` → 포털 "모델 추가"로 등록.
  - Lightning LoRA 와 함께 걸었을 때의 화풍과 세기 시험.

## 8. 바뀔 코드 (실험 뒤, 구현 순서)

> 11-21 구현은 12절. 설정 이름은 `STUDIO_REF_LAYOUT`(기본 켜짐)이고, 화풍 보드(1번)는 실험 결과(11.1)에 따라 뺐으며,
> 구도 명세(3번)는 LLM 대신 규칙으로, 이어짐 메모·색 맞추기(6·7번)는 아직이다.

PR #41 의 새로 그리기 레시피, 인물 소품 검사, 장소 그림, 이어지는 샷 규칙은 그대로 쓰고, 그 위에 ① 단계와 화풍 보드를 더한다.

1. 화풍 보드 등록: 라이브러리 `styles.<열쇠>.board`, `cast_lib.style_board(key)`, `studio-check` 가 파일을 확인.
2. 장소 그림을 화풍 보드에서 편집 모델로: `_place_card` → `qwen_image_compose_ref` (Picture 1 = A), `video.place_card_prompt` 에 역할 문장.
3. 구도 명세: `video.layout_spec` (LLM, JSON) + 검사 (사람 수·id 가 장면 인물과 같은지).
4. ① 구도 그림: `video.layout_prompt`, 감독 `_layout_image`, 검사 `checks.layout_problems`.
5. ②③ 키프레임: `video.identity_prompt` (형상 ↔ 인물 대응), 감독 `_compose_first_frame` 을 두 단계로.
6. 이어짐 메모: 마지막 프레임을 시각 모델로 한 번 읽기.
7. 색 맞추기: `assemble.match_colour`.
8. 모든 편집 지시문의 그림 번호 표현을 "Picture N" 으로.
9. 설정 `STUDIO_REF_SYSTEM` (기본 꺼짐 → 메인 확인 뒤 켬), 예상 시간, 운영자 기록 화면, 시험, 문서.

## 9. 먼저 할 메인 실험 (ComfyUI 화면에서, 약 30장)

| 번호 | 무엇 | 비교 | 볼 것 |
|---|---|---|---|
| R1 | 화풍 보드 누수 | 보드 2종 (배경만 / 배경 + 재질 조각) × 씨앗 2 | 보드의 물건이 옮겨 그려지는지, 화풍이 따라오는지 |
| R2 | 형상에 인물 입히기 | C (형상 2개) + 베스·제임스 × 씨앗 3 | 얼굴·옷이 맞는지, 왼쪽·오른쪽 대응 |
| R3 | 사람 수 붙잡기 | 같은 장면: PR #41 방식 vs 두 단계 × 씨앗 4 | 사람 수와 인물이 맞는 비율 |
| R4 | 세 사람 | ②+③ vs PR #41 방식 × 씨앗 2 | 셋 다 맞는지, 걸린 시간 |
| R5 | 번호 표현 | "Picture N" vs "image N", 같은 씨앗 | 역할 혼동 |
| R6 | ②에 화풍 보드 추가 (1명 장면) | 있음 vs 없음 | 화풍 차이 |

## 10. 정해야 할 것 [TBD]

- 화풍 보드 구성 (배경만 / 재질 조각 포함)과 장수, 저장 위치.
- 구도 형상 모양 (얼굴 없는 마네킹 / 실루엣 / 회색 인형).
- 네 명 이상 장면.
- 사람 없는 장면: 장소 그림 그대로 쓸지, ①로 새 샷을 그릴지.
- 화풍 LoRA 를 할지, 언제 할지, 학습 작업을 대기열에 어떻게 넣을지.

## 11. 메인 실험 결과 (11-21)

관리자 ComfyUI 화면에서 실행했다. 결과는 `admin/main/2026-11-21/refexp/`, 입력은 `refexp/`
(화풍 보드 `board_A1`(숲·온실과 그 확대)·`board_A2`(A1 에서 숲 확대 대신 망토 질감), 구도 그림 사본 `C1_a`·`C2_a`·`C3_a`).
그래프는 PR #41 의 `qwen_image_compose_ref` 그대로 (Lightning 4스텝, 1280×704), 장소 그림은 11-20 부엌 그림(`styleset/bg_kitchen.png`).
한 장 약 12~15초.

| 번호 | 무엇 | 결과 |
|---|---|---|
| R1 | 화풍 보드 누수 (보드 2종 × 씨앗 2, 보드 없이 장소 그림만 1) | 5/5 형상 2개·부엌 유지. 보드의 숲·온실·망토가 옮겨 그려지지 않았다. 그러나 보드 없는 것과 화풍 차이가 거의 없다 → ①의 화풍은 사실상 장소 그림이 실어 나른다 |
| R1b | 화풍 보드로 장소 그림 그리기 (씨앗 2) | 사람 없음·누수 없음. 그러나 붓 터치가 옮겨지지 않고 평평한 일러스트가 됐다 → 배경 보드만으로는 화풍이 고정되지 않는다 (5.2절 확인) |
| R2 | 형상 2개에 베스·제임스 입히기 (씨앗 3) | 3/3 얼굴·옷·좌우 대응이 맞음. 구도는 2/3 유지 (씨앗 b 는 카메라가 다가가 부엌이 바뀜) |
| R2t | R2 씨앗 b + 배경 문장 | 구도가 유지됐다 |
| R5 | "Picture N" vs "image N" (같은 씨앗) | 차이 없음 (씨앗 하나) |
| R3 지금 방식 | 혼자 대사 컷: 베스 기준 그림 + 장소 그림, 장면 글에 니키 언급 (씨앗 3) | **3/3 베스가 둘** (말 상대를 복제). 11-21 스튜디오 시험의 2번째 시도와 같은 오류 |
| R3 두 단계 | ① 형상 1개 구도 그림 → ② 베스 (씨앗 3) | 3/3 베스 한 명·인물 맞음. 그러나 **부엌이 사라지고 기준 그림의 단색 배경**이 됐다 (기준 그림이 바탕 그림을 이김) |
| R3b | 같은 씨앗으로 고치는 법 | 디노이즈 0.75(구도 그림을 잠재로): 구도는 유지, 인물이 흐려짐(머리 모양·옷). 0.85·0.95: 단색 배경. 순서 바꿈(인물 1, 구도 2): 단색 배경. **배경 문장 추가: 성공** |
| R3c | 배경 문장 + 씨앗 3개 더 | 3/3 성공 (씨앗 a 포함 4/4): 베스 한 명, 부엌·자리 유지, 화면 가운데 쪽 |
| R6 | ②에 화풍 보드를 Picture 3 으로 (1명) | 더 나빠짐: 기준 그림 초상화와 거의 같은 그림 |
| R4 | 세 사람: 지금 방식 vs ①→②→③ (씨앗 2) | 둘 다 2/2 인물이 맞음. 지금 방식은 부엌이 다른 장면과 다르고, 두 단계는 같은 부엌. 두 단계는 넓은 구도라 얼굴이 작고, 작은 형상 크기를 따라 니키가 인형처럼 보임 |

### 11.1 설계에 반영할 것

1. **②에 배경 문장이 꼭 필요하다:** "The background stays the same <장소> as in Picture 1, with <눈에 띄는 것>; it is not a plain studio background."
   이 문장이 없으면 기준 그림(단색 배경의 초상화)이 바탕 그림을 이긴다. 3.2절의 "Picture 1 의 내용이 지켜진다"는 가정은 늘 맞지 않는다 (인물 한 명일 때 특히).
   잠재를 구도 그림에서 시작하는 방법(디노이즈 < 1)은 인물을 흐리게 해 쓰지 않는다.
2. **화풍 보드는 기본 흐름에서 뺀다:** 누수는 없지만 효과가 작고(R1·R1b), ②에 넣으면 해롭다(R6).
   화풍은 같은 화풍으로 만든 장소 그림과 인물 기준 그림이 실어 나른다. 그래서 장소 그림을 화풍에 맞게 만드는 방법
   (확정 컨셉아트 계열 그림을 편집해 만들기 — 11-20 부엌 그림처럼)과 화풍 LoRA(7절)가 더 중요해졌다.
3. **지금 방식(PR #41)의 대사 컷은 위험하다:** 장면 글에 다른 인물이 나오면 3/3 복제가 났다. 두 단계로 바꾸기 전에도 바로 고칠 것:
   새로 그린 첫 화면 검사의 사람 수가 장면 글의 mother·woman 같은 말(`checks.EXTRAS_RE`) 때문에 more_ok 가 되는 문제,
   대사 컷 글에서 화면 밖 인물의 이름을 빼기.
4. **①의 구도 글에 샷 크기를 더 분명히:** 혼자 대사 컷은 "medium close-up, head and shoulders large" 로도 미디엄 샷이 나왔다.
   세 사람 넓은 장면은 얼굴이 작다. 샷 크기·인물 크기 지시를 강화하고, 아이 형상은 아이 키 정도로 적는다.
5. **시간:** 컷마다 편집 한 번(약 13초)과 검사가 늘고, 세 사람이면 두 번 더. 같은 컷을 다섯 번 다시 그리는 것(11-21 스튜디오 베스 컷)보다 싸다.

### 11.2 실험에 쓴 지시문 (구현의 출발점)

① 화풍 보드 문장은 11.1-2 에 따라 뺀다. 장소 문장과 구도 문장은 그대로 쓴다.

- ① 장소: `Picture 1 shows the place, a sunny family kitchen. Keep its architecture, window, cabinets, furniture and props, seen from a slightly different camera angle.`
- ① 두 사람: `A new picture of this kitchen: a medium two-shot at eye level. Two faceless mannequin figures sit side by side at the wooden breakfast table: the figure on the left is slim and turned toward the right, the figure on the right is taller and turned toward the left. Each figure has a smooth blank head with no face and no hair, and plain matte light grey clothes, like an artist's mannequin. Both figures' heads and upper bodies are fully in frame, in the middle part of the picture. Warm amber morning sunlight from the window.` + 화풍 문장 + `Exactly 2 figures, no other figure or person.`
- ① 한 사람: `… a medium close-up at eye level of one faceless mannequin figure sitting at the breakfast table, turned slightly toward the left, its head and shoulders large in the centre of the frame, the whole head inside the frame with some space above it. … The kitchen behind is softly out of focus.`
- ① 세 사람: `… a medium-wide shot at eye level of a family breakfast. Three faceless mannequin figures sit around the wooden table: on the left a small child-sized figure turned toward the right, in the middle a slim adult figure facing the camera, on the right a taller adult figure turned toward the left. … All three heads and upper bodies are fully in frame and apart from each other.`
- ② 두 사람: `Picture 1 is the layout. Keep exactly its camera angle, the kitchen, the table and props, the light, the painting style, and the two figures' positions, sizes and poses. Replace the faceless figure on the left with the woman in Picture 2: take only her face, hair, body and clothes, including <소품>. Replace the faceless figure on the right with the man in Picture 3: take only his face, hair and clothes, including <소품>. Take nothing else from Picture 2 or Picture 3.` + 배경 문장 + 표정·손 + 화풍 문장 + `Exactly 2 people, no other person, no reflection, no copy of anyone.`
- ② 한 사람: 같은 짜임에 `She looks toward the left edge of the frame with a gentle, encouraging smile, lips slightly parted as if speaking, face in three-quarter view. The hands are not visible.` + 한 사람 문장(`video.ONE_PERSON`).
- ③ 세 번째 인물: `Picture 1 is the picture to finish. Keep everything in Picture 1 exactly: the camera, the kitchen, the light, the painting style, the woman and the man. Replace only the small faceless figure on the left with the girl in Picture 2: take only her face, hair, body and clothes, including <소품>. Take nothing else from Picture 2.` + 배경 문장 + 화풍 문장 + `Exactly 3 people, …`
- 화풍 문장: `A soft painted semi-realistic fantasy illustration with visible painterly brush texture, matte soft shading and natural proportions. Not a glossy 3D render, not a cartoon, not cel-shaded.`

## 12. 구현 상태 (11-21)

사용자: "구현 후 다시 수정 가능해?" → 설정으로 끄고 켤 수 있게 구현, 실험 결과(11.1)를 반영했다. **로컬, 커밋 전. 시험은 가짜 부품으로만, 메인 실기기 확인 전.**

**구현함** (`STUDIO_REF_LAYOUT`, 기본 켜짐, `STUDIO_CHAIN` 이 켜져 있고 `qwen_image_compose_ref` 가 켜져 있을 때):
- 감독 `director._layout_first_frame`: 사람이 나오는 새 컷(같은 샷이 이어지는 컷 말고)을 세 단계로 그린다.
  - ① 구도 그림 (작업 표시 `L{i}c`): Picture 1 = 장소 그림, `video.layout_prompt` — 얼굴 없는 형상의 수·자리·키·방향·자세와 샷 크기만.
    장면 글(동작)은 넣지 않는다(화면 밖 인물 이름 → 복제). 샷 글의 '뒤에서(from behind)' 같은 말은 빼고 얼굴 방향(정면·3/4)을 정한다. 검사 `checks.layout_problems`(형상 수를 특징 줄 `checks.layout_trait` 로,
    얼굴 없음은 문제로 보지 않음), 세 번까지(`LAYOUT_TRIES`).
  - ② 인물 입히기 (`L{i}d` — 기준 그림으로 바로 그리는 길의 `L{i}e` 와 섞이지 않게): Picture 1 = 구도 그림, Picture 2·3 = 형상 차례(왼쪽→오른쪽)의 앞 두 사람 기준 그림, `video.identity_prompt`
    — 배경 문장(`video.background_sentence`) 필수, 대사 컷은 말하는 방향·감정만. 다섯 번까지.
  - ③ 세 번째 사람부터 한 명씩 (`L{i}f{k}`): Picture 1 = 지금까지 그린 그림, Picture 2 = 그 사람 기준 그림, `video.finish_prompt`. 세 번까지.
  - 사람 수는 마지막 단계만 정확히 센다(`checks.compose_rule`). 중간 그림은 인물 모습만 보고(남긴 형상의 얼굴 없음은 문제로 보지 않음,
    `checks.LAYOUT_SKIP`) 결과 알림에 남기지 않는다.
  - 끝난 단계는 고른 그림을 `work["L{i}c_done"]`·`L{i}d_done`·`L{i}f{k}_done` 에 남긴다: 허브가 다시 시작해도 그 단계를 다시 검사·다시
    그리지 않고 고른 그림으로 잇는다. 첫 화면 설명(`scene_prompt`, 영상 글 쓰는 LLM 이 읽음)은 `video.layout_description`(그림 번호·형상 말 없음).
  - 형상 차례: 한 사람은 가운데, 두 사람은 오른쪽을 보는 화자(`video.speaker_side`)를 왼쪽에 두어 마주 보게, 셋 이상은 장면에 나오는 차례.
    키는 외형의 머리 구절로(`video.figure_size`: 열세 살 이하·아이 낱말이면 아이, dwarf·short 등이면 작은 사람 — 'short hair'·'tiny glasses' 는
    보지 않음), 자세는 장면 글에서 가장 먼저 나오는 자세 동사, 없으면 같은 장소가 이어지는 앞 장면 가운데 이 사람이 나온 장면의 멈춘 자세
    (`video.scene_pose`, 걷기·달리기는 물려받지 않음). 같은 인물이 두 번 적혀도 형상은 하나(`video.scene_people`).
  - 장소 그림이 없거나(사람이 남아 버림 등) 기준 그림이 없는 사람이 있으면 기준 그림으로 바로 새로 그린다(13절의 PR #41 방식). 끄면 늘 그 방식.
  - 장면 기록: `first_from = "layout"`, `layout_rel`(구도 그림), 사건 `layout_done`. 운영자 기록 화면에 "구도 그림에 인물을 입힘".
  - 다시 만들기(`_long_reset`)는 새 단계의 접수 정보(`L{k}c|d|f{n}_pending`)와 고른 그림(`_done`), 끝 키프레임(`L{k}k_pending`, 예전부터
    비우지 않아 옛 끝 그림을 이어받던 문제)도 비운다. 예상 시간에 구도 그림·③ 을 더했다.
  - 11-21 여러 검토자 검토(세 관점 + 반박 확인)에서 확인된 지적 18건(겹친 것을 합쳐 10가지)을 위처럼 고쳤다.
- 지금 방식(PR #41)도 함께 고침: 새로 그린 첫 화면의 사람 수를 `checks.compose_rule`(무리 말 `checks.CROWD_RE` 만 봐줌 — 복수 낱말
  people·children·crowded 등은 군중으로 봄, mother·woman·girl 같은 한 사람 낱말은 보지 않음)로,
  대사 컷 지시문에서 장면 글을 빼고 감정만.
- 끝 프레임 검사 문장(`checks.cast_trait`): "몸 전체가 화면 안" → "얼굴 전체가 화면 안" (대사 클로즈업을 실패로 보던 문제).

**아직 안 한 것**: 화풍 보드(11.1-2 에 따라 뺌), 장소 그림을 화풍에 맞게 만드는 방법(지금은 Qwen-Image 2512 글→그림),
LLM 구도 명세(지금은 규칙), 이어짐 메모·색 맞추기, 화풍 LoRA.

**메인에서 확인할 것**:
- 11-21 영상(요청 e33b7def, 니키·베스·제임스 아침 식탁 30초)의 '다시 만들기'로 같은 구성안을 새 방식으로 만들어 컷마다 비교.
- 검사 모델이 형상 수 특징 줄에 맞게 답하는지(형상을 사람으로 세는지), 대사 컷의 얼굴 크기, 세 사람 장면의 얼굴 크기, 걸린 시간.
