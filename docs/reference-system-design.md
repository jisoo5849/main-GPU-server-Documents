# 레퍼런스 체계 설계: 화풍·인물·구도 (2026-11-21)

> 상태: **구현함(12절), 메인 실기기 확인 전.** 11-21 메인 실험(9절 R1~R6) 결과는 11절. 두 단계 방식은 사람 수·배치·장소를 지켰고,
> ②에 배경 문장이 꼭 필요하며, 화풍 보드는 효과가 작아 기본 흐름에서 뺀다.
> 앞선 설계: `docs/scene-chaining-design.md` (장면 잇기. 13절이 지금 구현된 '기준 그림으로 새로 그리기', PR #41).
> 11-24 화풍 일반화(화풍 열쇠 다섯·장소 그림 정밀 판·실사 변환 레시피)는 14절.

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

## 13. 11-24 메인 시험 뒤 고친 것 (얼굴 잘림, 로컬 수정, 커밋 전)

메인 시험(요청 63c7bb6a, 11-21 영상의 '다시 만들기')에서 새 방식은 사람 수·인물·같은 부엌을 지켰지만, 베스 대사 컷은 7초 중 약 4초 동안
머리가 화면 밖으로 나갔다 (11-21 영상과 같은 증상). 첫 화면에서 머리가 화면 맨 위에 붙어 있었고, 입 맞춤 영상에서 카메라가 내려가면서 머리가 잘렸다.

- 구도 그림(`video._frame_hint`): 클로즈업 한 사람은 "머리 위 빈 공간(화면 높이의 약 10%), 눈은 위에서 3분의 1, 얼굴은 가운데, 가슴은 아래 가장자리에서 잘림".
- 인물 입히기(`video.identity_prompt`): 클로즈업이면 "머리를 구도 그림의 자리에 두고 맨 위로 옮기거나 키우지 않는다".
- 입 맞춤 영상 글(`video.talk_prompt`): "아주 천천히 밀고 들어간다"를 "카메라는 삼각대에 고정, 기울이기·돌리기·줌·밀기 없음, 사람은 같은 자리, 얼굴은 화면 안 같은 위치"로.
  레시피 `ltx23_ia2v_talk` 판 4: 부정 글에 camera tilting down, camera moving, zooming, face leaving the frame, head cut off by the frame edge.
- 검사 오탐: 가슴 아래가 잘린 클로즈업의 '팔다리 떠 있음'(floating_limb)은 보지 않는다 (`checks.CLOSE_SKIP`, 클로즈업 첫 화면과 입 맞춤 클립).
  베스 첫 화면을 다섯 번 다시 그리던 것(약 3분)을 없앤다. 진짜 몸 이상(`extra_head` 등)은 그대로 본다.

**메인에서 확인할 것**: 베스 컷에서 머리가 끝까지 화면 안에 있는지(카메라 고정이 LTX 입 맞춤에서 지켜지는지), 제임스 컷이 멀리 앉은 전신샷으로 나오는 것(아직 안 고침),
세 사람 컷 마지막 단계에서 자리·크기가 바뀌는 것(아직 안 고침).

## 14. 화풍 일반화 (11-24)

사용자 요청(11-24): "반실사만 잘 되는 이유를 고치고 다른 화풍(2D 애니·실사 등)도 잘 되게", 이어서 "다른 화풍 또한 어느 요청에도 잘 되게".
**상태: 1차(반실사·애니·실사)는 커밋 2da44ca(PR #45, main 692f505). 2차(3D 애니·그 밖의 화풍, 장소 그림 정밀 판, 실사 변환 레시피)·검토 고침·3차(11-25 "방식 바꾸기": 구성안이 사용자가 말한 화풍을 `bible.style_said` 에 적는다, 14.3)는 로컬, 커밋 전. 시험은 가짜 부품으로만, 메인 실기기와 실제 구성안 모델 확인 전.**
같은 날 한 두세 사람 대화 장면은 `docs/short-film-dialogue-design.md` 13절에 있다.

### 14.1 반실사만 잘 되던 까닭 (11-24 코드 점검)

- 인물 기준 그림이 반실사뿐이다 (`cast_library.json` 의 `refs.semi`).
  - 다른 화풍은 영화마다 Lightning 4스텝 편집으로 바꿨고(`director._style_convert`), 실사로 바꾼 그림에는 그림 느낌이 남았다 (11-20 S6).
  - 바꾼 그림은 알아볼 소품을 검사하지 않았다.
- 실사는 따로 다뤄 화풍을 거의 보지 않았다.
  - 실사면 `video.story_style` 이 빈 글이라 화풍 검사, 장면 글의 화풍 줄, 화풍 고정 문장(`style_lock`), 장소 그림 앞머리가 모두 빠졌다.
  - 장면 그림·바꾼 그림·클립 가운데 프레임은 실사 화풍을 검사하지 않았다.
  - 실사 판정 정규식이 두 곳(`video`·`cast_lib`)이라 서로 어긋났다. 예: 'semi-photorealistic digital painting' 이 열쇠로는 반실사, `story_style` 로는 실사였다.
- 기본값이 모두 반실사로 모였다.
  - 화풍 열쇠의 기본값(`cast_lib.style_key`)과 모르는 열쇠의 화풍 문장(`video.style_keep`)이 반실사였다.
  - 라이브러리 인물이 나오면 요청 화풍과 상관없이 "라이브러리 화풍을 bible.style 에 베껴라" 줄이 붙었다 (`cast_lib.prompt_block`).
  - 구성안 화풍이 비었거나 실사면 라이브러리 화풍으로 덮었다 (`cast_lib.apply_to_story`).
- 열쇠 판정 순서가 어긋났다.
  - 'painted' 가 애니 말보다 먼저 걸려 11-18 애니 시험 문장('2D … cel-shaded …, soft painted backgrounds')이 반실사가 됐다.
  - 'Pixar-like 3D animated' 는 2D 애니가 되어 'not a 3D render' 문장이 붙었다.
  - 수채·클레이·코믹·유화는 반실사가 됐다.
- 첫 화면 그림(구도·인물 입히기·③·새로 그리기·끝 키프레임·고쳐 잇기)에는 영화 화풍 원문 대신 정해진 세 문장(`video.STYLE_KEEP`) 가운데 하나만 들어갔다. 화풍 원문은 장소 그림(Qwen 2512) 앞머리에만 있었다.
- "painting style" 고정 문구가 지시문에 있었다 (`identity_prompt`·`finish_prompt`·`cut_edit_prompt`).
- 장면 그림 레시피는 반실사 화풍으로 고른 것이다 (Qwen-Image 2512 Lightning, 사용자 선택 `b_new_light`). Lightning 은 애니를 반실사 쪽으로 끈다 (10-27 기록).
- 영상 모델 탓은 아니었다: X5b(LTX-2.3 IA2V, 애니 첫 화면)는 6초 동안 애니 화풍을 지켰다 (`docs/short-film-dialogue-design.md` 13.2).
- 부정 글도 원인이 아니다: 영상·장면 그림 레시피가 cfg 1 이라 부정 글은 계산되지 않는다.

### 14.2 화풍 열쇠 다섯 (`cast_lib.style_key`)

| 열쇠 | 뜻 | 화풍 글 예 |
|---|---|---|
| semi | 반실사 (라이브러리 화풍) | 'semi-realistic digital painting, 2.5D concept art …', 빈 글 |
| anime | 2D 애니 | '2D anime feature film with clean cel-shaded line art', 'Korean webtoon style digital illustration' |
| toon3d | 3D 애니메이션 | 'Pixar-like 3D animated feature film', 'stylized 3D CG animation' |
| photo | 실사 | 'photorealistic cinematic live action', 'hyper-realistic cinematic look', 'cinematic film look with dramatic lighting' |
| custom | 그 밖에 분명히 말한 화풍. 원문을 그대로 쓰고 라이브러리에 저장하지 않는다 | 'watercolour storybook illustration', 'claymation stop-motion', '3D claymation animation', 'black-and-white film noir', '2D hand-drawn lines on cel-shaded 3D characters …' |

판정 순서 (위에서부터 먼저 맞는 것, "not a cartoon" 같은 '아닌 것' 구절은 지우고 본다):
1. 분명한 반실사 말(semi-real·semi photo·반실사·2.5D) → semi.
2. 애니 말(anime·manga·manhwa·webtoon·cel·2D·만화·웹툰·지브리, animation·cartoon·카툰·애니) → anime.
   - 3D 나 3D 애니 말이 함께면 toon3d.
   - 2D 와 3D 가 함께거나 재료·기법 말(클레이·로우폴리·종이·펠트·픽셀 …)이 함께면 custom. 애니 문장의 'not a 3D render' 와 3D 애니 문장의 'not a flat 2D drawing' 이 둘 다 원문과 부딪힌다.
   - 'stop-motion animation'·'클레이 애니' 처럼 '움직이는 그림' 말에 custom 말이 붙으면 custom.
3. 3D 애니 말(Pixar·픽사·Disney 3D·CG 애니메이션·3D cartoon·stylized 3D·드림웍스) → toon3d (재료 말이 함께면 custom).
4. 실사 말(실사·photoreal·live action·photograph·사진처럼) → photo.
   - 사실적인 영화 말(hyper-realistic·cinematic realism·lifelike·naturalistic·사실적·리얼한·실제 사람)은 custom 말이 함께 없을 때 photo.
   - 사진 말 없는 영화 글(cinematic film look·35mm·documentary style)은 custom 말·그림 말이 함께 없을 때 photo.
5. custom 말 → custom: 수채·과슈·유화·아크릴·파스텔·크레용·색연필·연필 스케치·목탄·펜화·수묵·담채·판화·우키요에·동화·그림책·클레이·스톱모션·종이 오리기·종이접기·펠트·픽셀·모자이크·실루엣·레고·코믹·그래픽 노블·누아르·흑백 무성영화·스테인드글라스·로우 폴리.
6. 그림 말(painterly·digital painting·painted) → semi.
7. 빈 글 → semi, 비어 있지 않은데 어느 말에도 안 맞으면 custom.

- 실사 판정은 이 함수 하나로 한다 (`cast_lib.is_photo_style`, `video.story_style` 도 쓴다).
- 색 이름은 화풍이 아니다 (예: '파스텔 톤'·'스테인드글라스 창문').
- '르누아르'(화가 이름)는 누아르로 읽지 않는다 (화풍 글 표와 요청 글 표 모두 `(?<!르)누아르`, 11-25).
- 사진 쪽 custom(누아르·흑백 무성영화)에는 '그린 그림'·'사진 아님' 문장을 붙이지 않는다 (`video.photo_like`).
- `cast_lib.STYLE_KEYS`(photo·semi·anime·toon3d)는 라이브러리 `refs` 칸 이름이다. custom 은 저장하지 않는 열쇠다.

### 14.3 사용자가 말한 화풍과 라이브러리 화풍 (`bible.style_said`, 11-25 "방식 바꾸기")

**바꾼 까닭**

- 2차에서는 코드가 요청 글(한국어·영어)을 낱말 목록과 문장 꼴로 읽어 "사용자가 화풍을 말했는가"를 짐작했다 (`cast_lib.said_level`, 아래 '칸이 없는 구성안의 예비').
- 대사 장면의 그 자리 짐작과 함께 검토·확인을 네 번 하고 네 번 고쳤지만 지적이 14 → 17 → 23 → 28건으로 줄지 않았다. 고칠 때마다 흔한 문장이 새로 깨졌다 (28건 가운데 9건쯤이 바로 앞 고침에서 생겼다).
- 사용자 결정(11-25): "방식 바꾸기". 구성안을 쓰는 언어 모델(`settings.expand_model`, 기본 qwen3.8:27b)이 요청을 읽고, 사용자가 화풍을 말한 말을 그대로 `bible.style_said` 에 옮겨 적는다.
  - 코드는 그 칸만 본다. 검사는 칸끼리(style_said ↔ style) 맞는지만 보고, 요청 글은 읽지 않는다.
  - 요청 글 짐작은 칸이 없는 구성안(예전에 저장된 것, 모델이 칸을 빠뜨린 것)에만 쓰는 예비다. 예비 쪽은 더 넓히지 않는다.
- 같은 날 대사 장면의 그 자리 사람도 같은 방식으로 바꿨다 (`docs/short-film-dialogue-design.md` 13.5).
- 구현 뒤 확인 지적과 그 고침을 다시 확인한 지적도 새 방식 안에서 고쳤다. 아래는 그 뒤의 동작이다.
  - 그 뒤 좁은 확인 두 번(지적 9건·7건, 대사 장면과 함께 센 수, fixC·fixD)에서 화풍 쪽은 세 가지를 고쳤다: 거절만 한 화풍 요청('반실사 말고 다른 그림체로'·'원래 그림체 말고 다른 스타일로'), 거절 문구에 바라는 화풍 적기, 고치기 차례의 '지금 그림체'.

**구성안 칸과 규칙 글**

- `video.STORY_SCHEMA` 의 bible 이 `style_said`(문자열)를 요구한다 (style 바로 앞). 대사 영상 칸(`DIALOGUE_SCHEMA`·`DIALOGUE_SCHEMA_SOLO`)도 이 칸을 그대로 갖는다. 화풍은 대사가 있든 없든 같은 기능이다.
- 규칙 글(`video.STORY_SYSTEM`):
  - style_said 는 영화의 모습에 대한 사용자 말을 요청에서 그대로 옮긴 것이다 (한국어·영어, 예: "애니메이션"·"수채화 느낌으로"·"3D 픽사 스타일"·"실사"·"pixel art").
  - 화풍·매체·애니메이션·실사·영화 종류나 스튜디오의 모습만 옮긴다. 이야기 속 물건·활동(인물이 만드는 펠트 인형, 그리는 연필 스케치, 읽는 만화책, 노래, 주제)은 옮기지 않는다.
  - 'X 말고 Y' 는 Y 만 옮긴다 ("반실사 말고 애니메이션으로" → "애니메이션").
  - 거절만 하면("반실사 말고 다른 그림체로"·"지금 그림체 말고 다른 스타일로"·"원래 그림체 말고 다른 그림체로") 거절한 말까지 그대로 옮기고, style 에는 모델이 거절하지 않은 다른 화풍을 고른다. 코드가 구절에서 거절한 화풍을 읽어 그 화풍을 막는다 (아래 '구절의 열쇠'). 이 문장은 다시 확인 고침에서 바꾼 것이고 3차 명세에는 없다 (처음에는 '거절한 말을 뺀 말만' 옮기게 해서 무엇을 거절했는지 잊었다).
    - "원래 그림체 말고" 는 인물의 원래 그림체를 거절한 말이다. "지금 그림체 말고" 는 사용자가 지금 보는 화풍을 거절한 말이다: 요청에 앞 메시지가 있으면(고치기 차례) 앞 메시지로 만든 영상의 화풍, 없을 때만 인물의 원래 그림체다. 둘 다 지키는 "원래 그림체 그대로" 와 반대다 (fixD: fixC 는 둘 다 인물의 그림체라고 적어, 애니로 만든 영상 뒤의 '지금 그림체 말고 다른 스타일로' 에 모델이 반실사만 피하고 거절한 애니를 고르게 했다).
    - 고치기 차례이고 앞 영상이 긴 영상이면 요청 글 끝에 그 화풍을 한 줄 붙인다 ('The film made from the earlier message has this look: …', `video.now_look_note`). 아래 '지금 화풍' 을 보라.
  - 정해진 인물의 원래 그림체를 지키라는 말("원래 그림체 그대로")과 분위기 말("따뜻한 느낌으로")은 화풍이 아니다.
  - 요청에 앞 말과 새 말이 있으면 새 말을 따른다. 새 말이 화풍을 말하지 않으면 앞 화풍 말을 지킨다.
  - 화풍을 말하지 않았으면 "".
  - style 은 style_said 가 비어 있지 않으면 바로 그 화풍을 쓰고, "" 이면 'photorealistic cinematic live action' 이다.
  - '모두 영어' 규칙에서 style_said 는 예외다 (스튜디오·화가 이름도 그대로 옮긴다).
- 라이브러리 인물이 나오면 붙는 화풍 줄(`cast_lib.prompt_block`)은 요청 글과 상관없이 한 가지다: "요청이 화풍에 대해 무엇이든 말했으면(화풍을 청했거나 거절했으면, 예: "반실사 말고 다른 그림체로"·"원래 그림체 말고 다른 스타일로") 그 말을 bible.style_said 에, 청한 화풍이나 거절하지 않은 다른 화풍을 bible.style 에 쓴다. 화풍을 말하지 않았거나 인물의 원래 그림체를 지키라는 말뿐이면("원래 그림체 그대로") bible.style_said 는 "" 이고 라이브러리 화풍을 bible.style 에 베낀다". 11-24 에는 요청 글 짐작(sure·maybe·말하지 않음)으로 줄을 골랐다.
  - fixC: 예전 줄은 "요청이 특정 화풍을 청했으면 …, 아니면 "" 와 라이브러리 화풍" 이라 거절만 한 요청을 '청하지 않음' 으로 읽게 해, STORY_SYSTEM 의 '거절한 말까지 옮겨라' 와 부딪히고 거절한 반실사를 베끼게 했다 (11-25 최종 확인).
  - fixD: 예문 '지금 그림체 말고' 를 '원래 그림체 말고' 로 바꿨다 ('지금 그림체' 는 고치기 차례에 앞 영상의 화풍이라 인물 그림체 거절의 예문이 아니다).

**다듬기와 검사**

- 다듬기(`video.clean_story`)는 style_said 가 문자열일 때만 남긴다. 칸이 없으면 넣지 않아 '칸 없음'(예비 경로)과 ""(말하지 않음)를 구별한다.
  - 읽는 값(`cast_lib.said_text`): 띄어쓰기를 고르고, 자리 채움 글('none'·'없음'·'N/A'·'-')은 "" 로 본다. 120자까지 (`video.STYLE_SAID_MAX`).
  - 원래 그림체를 지키라는 말뿐인 구절('원래 그림체 그대로'·'기존 그림체 유지'·'니키 그림체 그대로', 예비 경로와 같은 `_KEEP_LOOK_RE`)도 "" 로 읽는다. '원래 그림체 말고 애니로' 는 거절이라 아니다.
- 검사(`video.story_problems`, 대사 영상은 `dialogue_problems` 가 이것을 한 번 부른다)는 칸끼리만 본다. style_said 에는 영어 검사를 하지 않는다.
  - style_said 가 120자를 넘으면 화풍 말만 옮기라고 한다.
  - style 이 style_said 의 화풍을 그리지 않았으면(`cast_lib.style_said_fits`, 기준은 아래) "bible.style must describe the look in bible.style_said ("…")" 에 둘째 문장(`cast_lib.style_said_hint(said, style)`)을 붙인다. 둘째 문장은 걸린 까닭에 따라 다르다.
    - 거절한 화풍의 글: 'The user turned down the look in "반실사"' 와 고를 다른 화풍(2D·3D 애니메이션·실사). 구절이 바라는 화풍도 말했으면('반실사 말고 수채화로') 다른 화풍 예시 대신 그 화풍('… and asked for a watercolour painting look: write that look', fixC — 예시대로 고친 답이 다음 차례에 다시 걸려 한 차례가 헛돌았다).
      - 바라는 화풍의 열쇠가 None 이어도 마지막 거절 말 뒤에 그림·화가·화풍 말이 있으면 그 말을 그대로 인용한다: '반실사 말고 고흐 그림처럼' → '… and asked for the look in "고흐 그림처럼": write that look in bible.style and name its medium (…)' (`cast_lib._wanted_words`, fixD — 예전에는 다른 화풍 예시를 들어, 예시대로 2D 애니로 고친 답이 문구 없이 통과하고 고흐 회화가 사라졌다). '다른 …' 은 바라는 말이 아니라 예시 그대로다.
    - 흑백·누아르 구절에 실사 글: 'live action·photorealistic·photograph' 말 없이 쓰라고 한다 (그 말 때문에 컬러 사진으로 읽힌다).
    - 꾸밈 말 + 움직이는 그림 말('흑백 애니메이션'): 애니메이션(2D 또는 3D)으로, 실사가 아니게.
    - 열쇠가 None 인 구절에 반실사 글: 원래 그림체를 지키라는 말이나 분위기뿐이면 style_said 를 "" 로, 아니면 그 화풍을 쓰고 재료(유화·수채·잉크·연필·2D·3D 애니메이션)를 밝히라고 한다. 구절이 무언가를 거절하고 다른 것을 청하면('이런 거 말고 다른 스타일로', 거절 말 뒤에 '다른'·'딴', `cast_lib._refusal_said`) "" 길을 내지 않는다 (fixD). 다만 그런 구절의 반실사 글은 검사가 받으므로(아래 '열쇠가 None 인 구절의 검사') 이 문구는 거의 나가지 않는다.
    - 열쇠가 None 인 그림 말 구절에 실사 글: 그 그림 화풍을 쓰거나, 관용 표현('그림 같은')이면 style_said 를 "" 로. 구절이 무엇이든 거절했으면 "" 로 비우라고 하지 않고 거절하지 않은 그림 화풍을 쓰게 한다 ("" 는 라이브러리 화풍, 곧 거절한 화풍이 된다, fixC).
    - 그 밖에는 기대한 화풍. 끝에 늘 "style_said 에 화풍 말 밖의 글까지 옮겼으면 그 말만 옮겨라" 가 붙는다.
  - style_said 가 "" 인데 style 이 특정 화풍(애니·3D 애니·custom)이면 둘 가운데 하나를 고치게 한다: 사용자의 가장 새 화풍 말(요청에 앞 메시지가 있으면 새 메시지)이 그 화풍을 청했거나 화풍을 거절했으면(예: "반실사 말고 다른 그림체로") 그 말을 style_said 에 옮기고, 아니면 "" 일 때의 화풍을 쓴다 (fixC: 예전 문구는 거절을 지켜 다른 화풍을 고른 구성안을 라이브러리 반실사로 돌려보냈다. fixD: '가장 새 말' 로 한정했다 — 앞 메시지가 '반실사 말고 다른 그림체로' 이고 새 메시지가 '원래 그림체 그대로' 일 때 앞 메시지의 거절을 옮기라고 읽혔다).
  - 검사는 라이브러리 인물의 그림체(`cast_lib.library_style(known)`)와 고치기 차례의 지금 화풍(now_style)을 받는다 (`story_problems(…, library_style, now_style)`, `write_story`·`dialogue_problems` 가 넘긴다). 아래 '원래 그림체 말고'·'지금 그림체 말고' 를 읽는 데 쓴다.

**구절의 열쇠 (`cast_lib.said_phrase_key`)**

- 구절 전체를 화풍 말로 읽는다. 표지('풍'·'느낌')가 없어도 되고 물건·활동 거르기는 하지 않는다 (모델이 이미 했다).
- 이런 말은 빼고 읽는다 (`cast_lib._phrase_scan`·`_phrase_words`):
  - 거절한 말 ('애니메이션 말고 실사로' 의 애니메이션, 'not anime'). 구절 길에서는 'anything but'·'other than'·'except' 도 거절이다 (`_PHRASE_NEG_BEFORE`, 예비 경로는 넓히지 않았다).
  - 2D 인지 3D 인지 모르는 디즈니 + '움직이는 그림' 말 ('디즈니 애니메이션처럼'·'디즈니풍 애니메이션').
  - 색·분위기로 쓴 파스텔·코믹 ('파스텔 톤으로'·'코믹한 느낌으로').
- 거절한 말의 열쇠는 따로 기억한다 (`cast_lib._phrase_refused`: '반실사 말고 다른 그림체로' → semi). 검사·덮어쓰기·채울 글이 이것을 본다.
  - custom 거절은 재료 이름으로 기억한다 ('클레이 말고' → 'custom:claymation', fixD). 검사·덮어쓰기·문구는 구성안 화풍 글이 그 재료 말을 담았는지로 본다 (`cast_lib._refused_by`). 예전에는 custom 열쇠째 거절해 '클레이 말고 픽셀 아트로' 의 맞는 픽셀 아트 구성안도 거절하고 끝까지 수렴하지 않았다. '수채화 말고'·'수묵화 말고' 처럼 화풍 말 뒤의 '화' 도 거절 말의 것이다 (구절 길만, 예비 경로의 `_negated` 는 넓히지 않았다).
  - 인물의 원래 그림체·지금 보는 화풍을 가리키는 말 + 거절 말 (`cast_lib._look_refs`·`_refuses_look`, fixC·fixD). 그림 말은 그림체·화풍·그림풍·작화·스타일이다 (fixD: '지금 스타일 말고 다른 스타일로'·'원래 스타일 말고 …' 를 놓쳐 거절한 반실사가 소리 없이 나왔다).
    - 인물의 원래 그림체(`OWN_LOOK`): 원래·기존·평소·정해진 + 그림 말, 라이브러리 인물 이름 + 그림 말('니키 그림체 말고'). 라이브러리 그림체(지금은 모두 semi)를 거절한 것이다.
    - 지금 보는 화풍(`NOW_LOOK`): 지금·현재·이·그 + 그림 말, 구절 처음이나 쉼표 뒤의 앞말 없는 그림 말('그림체 말고 다른 스타일로'). 요청이 한 메시지면 인물의 그림체, 고치기 차례면 앞 메시지로 만든 영상의 화풍(now_style, 아래 '지금 화풍')이다.
    - 거절한 화풍을 모르면(라이브러리 인물이 없는 '원래 그림체 말고', 앞 영상의 화풍을 모르는 고치기 차례) '모름'(`SOME_LOOK`)을 넣는다. 검사와 덮어쓰기는 구성안이 고른 화풍을 믿는다 (구성안 모델은 앞 메시지를 보았다).
    - 앞말 없는 그림 말은 앞에 한글이 붙지 않아야 한다 ('만화풍'·'동화풍'·'동양화풍'·'유화풍' 의 '화풍' 이 아니다, fixD). 앞 낱말이 꾸미는 그림 말('레트로 스타일 말고'·'고양이 스타일 말고')도 그 화풍이지 지금 화풍이 아니다 — 거절한 것을 코드가 모르는 구절로 본다. 화풍 말에 붙거나 겹친 그림 말('반실사 그림체 말고'·'만화풍 말고' 의 '화풍')은 그 화풍 말의 것이다. 예전에는 '만화풍 말고 다른 걸로' 가 {anime, semi} 를 거절해 맞는 반실사 구성안에 '"화풍" 을 거절했다' 문구가 나가고 3D 로 덮였다.
    - 앞말 없는 그림 말은 뒤가 '다른'·'딴' 일 때만 거절이다. 앞말이 있어도 '말고' 뒤가 바꿀 다른 대상('그림체 말고 배경만 바꿔줘'·'지금 그림체 말고 대사만 바꿔줘')이면 그림체를 두라는 말이라 거절이 아니다. '말고' 로 끝나거나('지금 그림체 말고') 바꿔 달라는 말만 오면('원래 그림체 말고 바꿔줘') 거절이다.
    - 구절에서도 지워, 거절한 '그림체' 를 그림 말(`_drawn`)로 읽지 않는다 — '지금 그림체 말고 다른 스타일로' + 실사 글은 맞고, '원래 그림체 말고 다른 그림체로' + 실사 글은 그림 화풍을 쓰게 한다. `_drawn` 은 거절 구절이면 마지막 거절 말 뒤(바라는 쪽)만 본다 (fixD: 코드가 모르는 그림 말을 거절한 '동양화풍 말고 다른 스타일로' 가 그림 말 구절로 읽혀 실사 글을 거절했다).
- 지금 화풍 (고치기 차례, fixD): 사용자가 결과를 보고 고치는 말을 보내면 api 가 앞 요청 + 이번 말을 한 요청으로 잇는다(`studio.api` 의 carry revise — 글→영상(t2v)으로 고치는 R6·R7b 와 계획 카드를 고치는 말. 긴 영상은 글→영상으로만 만든다). 이때 앞 결과가 긴 영상이면 그 구성안의 화풍 글(`video.film_look`: bible.style, 비었으면 기본 실사 글)을 요청의 `now_style` 에 적고, 감독(`director._write_story`)이 `storyboard(now_style=…)` 로 넘긴다. 구성안 쓰기(요청 글 끝 한 줄)·검사·덮어쓰기가 모두 이 값을 '지금 그림체' 로 쓴다. 앞 결과가 짧은 영상이라 구성안이 없으면 "" (모름 → `SOME_LOOK`). 한 메시지 요청은 이 칸이 없고 '지금 그림체' 는 인물의 그림체다.
- 지금 낱말 표(14.2)를 쓰고, 여러 말의 차례는 화풍 글 열쇠(`style_key`)가 정한다.
- 아는 말이 없거나 애매한 말뿐이면 None 이다. 영어 필름 룩 구절('cinematic film look'·'35mm film look'·'documentary style')도 None 이다.
  - 한때 이것만 `style_key` 로 읽어 photo 로 보았다. 그러자 같은 뜻의 한국어 구절('다큐멘터리처럼')과 결과가 갈리고 'a Disney film look' 이 실사가 되어 되돌렸다. 실사 글은 None 구절 검사가 받는다 (`test_fb_07`).

| 구절 | 열쇠 | 라이브러리 인물 영화에서 구성안 화풍이 따르지 않았을 때 채울 글 |
|---|---|---|
| 애니메이션·애니·웹툰 스타일·지브리 느낌 | anime | 열쇠의 기본 글 (`cast_lib.SAID_STYLE_TEXT`) |
| 3D 애니·픽사 스타일·CG 애니메이션 | toon3d | 같음 |
| 실사·실제 배우·사실적으로 | photo | 같음 |
| 반실사 | semi | 라이브러리 화풍 |
| 수채화 느낌으로 / 유화 / 도트 / 3D 클레이 / 흑백 연필 스케치 | custom | 'watercolour painting style' / 'oil painting style' / 'pixel art style' / 'claymation style' / 'black-and-white pencil sketch style' (`custom_style_text(phrase=True)`) |
| 흑백 영화 / 누아르 | custom (사진 쪽) | 'black-and-white film style' / 'film noir style'. 실사 영화 한 컷으로 그린다 |
| 흑백 애니메이션 / 누아르 애니메이션 | custom | 애니 기본 글 + ', in black-and-white film style' / ', in film noir style' (`_decor_anim`) |
| 고흐 그림처럼·디즈니 애니메이션처럼·파스텔 톤으로·다큐멘터리처럼·cinematic film look | None | 라이브러리 화풍 |
| 반실사 말고 다른 그림체로 | None (반실사 거절) | 거절하지 않은 기본 화풍, 애니 먼저 (`_REFUSE_ALT`) |
| 원래 그림체 말고 / 원래 스타일 말고 다른 스타일로 / 니키 그림체 말고 다른 그림체로 | None (라이브러리 그림체 거절, fixC·fixD) | 같음 |
| 지금 그림체 말고 다른 스타일로 (한 메시지) | None (라이브러리 그림체 거절) | 같음 |
| 지금 그림체 말고 다른 스타일로 (애니 영상을 고치는 차례) | None (앞 영상의 애니 거절, fixD) | 라이브러리 화풍 (반실사는 거절하지 않았다) |
| 만화풍 말고 다른 걸로 / 동양화풍 말고 다른 스타일로 | None (애니 거절 / 거절한 것을 모름) | 라이브러리 화풍 |
| 그림체 말고 배경만 바꿔줘 | None (거절 아님 — 그림체를 둔다) | 라이브러리 화풍 |
| 클레이 말고 픽셀 아트로 | custom (클레이만 거절) | 'pixel art style' |

**구성안 화풍을 둘 때와 채울 때 (`cast_lib.apply_to_story`, 라이브러리 인물이 있을 때)**

- style_said 가 "" 이면 구성안이 다른 화풍을 썼어도 라이브러리 화풍(승인된 인물 그림체)이다.
- 비어 있지 않으면 구성안 화풍이 구절을 따랐는지 본다 (`cast_lib.keeps_style_said`). 따랐으면 그대로, 아니면 채운다 (`style_said_fill`).
  - 구성안 화풍이 구절이 거절한 화풍이면 두지 않는다.
  - 열쇠가 있으면 `_key_fit`: 열쇠가 같으면 맞다 (custom 은 구절의 재료 말을 담아야 한다).
  - 열쇠가 달라도 같은 화풍인 경우: 애니 구절 + 2D·hand-drawn 이 든 custom 글('hand-drawn 2D animation with soft watercolour backgrounds'), 3D 애니 구절 + 3D·CG 가 든 custom 글, 꾸밈 custom 구절(흑백·누아르·동화·코믹 …)의 말을 모두 담은 애니·3D 애니 글('storybook-like 2D animation').
  - custom 구절에 실사 글은 맞지 않는다. 흑백·누아르 구절의 'black-and-white film noir live action' 은 실사 열쇠라 뒤 단계(`video.story_style`·실사 변환)가 화풍 원문을 쓰지 않아 흑백이 사라진다. 'live action' 없이 쓴 'black-and-white film look' 이 사진 쪽 custom 이고 실사 영화 한 컷으로 그린다 (`test_fb_04`).
  - 열쇠가 None 이고 구절이 반실사를 거절했거나 무엇을 거절했는지 모르면(`SOME_LOOK`) 구성안이 고른 다른 화풍을 둔다 (그림 말 구절의 실사 글은 빼고, `test_fb_06`).
  - 열쇠가 None 이고 구절이 지금 보는 화풍·인물 그림체를 거절했거나 코드가 모르는 것을 거절했으면('동양화풍 말고 다른 스타일로') 실사 글도 둔다 (그림 말 구절이 아닐 때, fixD: 애니 영상을 고치는 차례의 '지금 그림체 말고' 에 고른 실사가 라이브러리 반실사로 덮였다). 아는 화풍 말을 거절한 구절('애니메이션 말고 다른 느낌으로')의 실사 글은 예전대로 라이브러리 화풍이다.
  - 그 밖에 열쇠가 None 이면 실사도 라이브러리 같은 반실사 글(`_SEMI_RE`)도 아니고, custom 이면 그림·재료·화가·화풍 갈래 말을 담을 때만 둔다. 'painted'·'painterly' 때문에 반실사로 읽히는 그림 글('soft impressionist painted look')은 둔다. 검사가 재료(유화·수채 …)를 적어 고쳐 쓰게 하므로, 세 번 안에 못 고쳤을 때만 남는다.
- 열쇠가 None 인 구절의 검사(`style_said_fits`)는 칸끼리 본다 (`test_fb_05`).
  - 실사 글은 구절이 그림·화가·애니 쪽 말(`cast_lib._drawn`, `_PHRASE_DRAWN_RE`: 그림·화풍·만화·애니·고흐·디즈니·회화·명화·동양화·한국화·민화·벽화·스케치·그래피티·mural·canvas …)이면 틀리다. 영화 종류·분위기 말('다큐멘터리처럼'·'공포 영화처럼'·'cinematic film look')은 맞다. 이 목록은 이 경우에만 쓴다.
  - 반실사 글(라이브러리 화풍을 베낀 글, 'painted'·'painterly' 로 반실사로 읽히는 글)은 낱말 목록 없이 틀린 것으로 보고 고쳐 쓰게 한다 (문구는 위 '다듬기와 검사'). 구절이 다른 화풍을 거절했으면('실사 말고 다른 그림체로') 맞다. 거절한 대상을 코드가 모르는 거절 구절('동양화풍 말고 다른 스타일로'·'이런 거 말고 다른 스타일로')도 맞다 (fixD: 예전에는 반실사를 거절하지 않았는데도 다른 화풍으로 바꾸게 했다).
  - 애니·3D 애니·custom 글은 맞다 (구성안 모델이 정한 화풍을 믿는다).
  - 까닭: 처음 확인 고침은 그림 말 목록으로 갈랐다. 그러자 목록 밖 '동양화 느낌으로'·'명화처럼' + 라이브러리 반실사가 소리 없이 반실사가 되고, '원래 그림체 그대로' + 라이브러리 화풍은 거절되어 그림 화풍으로 바꾸게 했다.
- 검사(`style_said_fits`)와 덮어쓰기는 같은 기준이다. 다른 것은 두 가지다: 라이브러리 인물이 있을 때만 버리는 글(None 구절의 실사·라이브러리 같은 반실사·재료 없는 custom), 세 번 안에 못 고친 'painted' 글을 두는 것.
- 채울 글(`style_said_fill`)은 위 표와 같다. custom 인데 옮길 말이 없으면 구성안 화풍이 custom 일 때 그것, 아니면 라이브러리 화풍이다.
- 라이브러리 인물 영화에서 필름 룩·영화 종류 구절('cinematic film look'·'다큐멘터리처럼')은 None 이라, 구성안이 실사로 써도 라이브러리 화풍이 된다 (예비 경로와 같다).
- 라이브러리 인물이 없는 영화는 지금 그대로 구성안 화풍을 쓴다.

**칸이 없는 구성안의 예비 (2차의 요청 글 짐작, 넓히지 않는다)**

style_said 칸이 없는 구성안은 2차 경로 그대로 요청 글을 읽는다. 그 결과가 바꾸기 전과 같은지는 `test_s3_17` 이 본다.

- 요청 글을 세 가지로 읽는다 (`cast_lib.said_level`).
  - sure: 아는 화풍 말을 화풍으로 썼다 (`_req_scan`).
  - maybe: 아는 말은 없지만 화풍 표지가 붙었다. 예: '반 고흐 스타일로'·'디즈니처럼'·'Van Gogh style'.
  - 말하지 않음 (None).
- 물건·활동·관용 표현은 화풍이 아니다.
  - 예: '펠트 인형을 만드는'·'만화를 그리는'·'애니메이션 감독'·'만화 같은 하루'·'동화 같은 숲속'·'watches cartoons'·'a photograph of'.
- 열쇠 말(반실사·애니·3D 애니·실사)은 그 밖에는 화풍이다 (예: '니키와 푸토르가 대화하는 애니메이션').
- 그 자체로 화풍인 말(claymation·stop-motion·pixel art·ukiyo-e·low poly·sumi-e)은 표지가 없어도 센다.
- custom 말과 '사진'·'손그림'은 화풍 꼴일 때만 센다.
  - 예: 뒤에 표지(풍·화풍·느낌·스타일·그림체·기법·애니메이션·영상, style·look·art·film), 'X로 …'(재료로 무엇을 만드는 말이 아닐 때), 'X처럼 …', 'in·as·like·in the style of·painted in X', 'a X painting of'.
- '영화처럼'·'광고처럼'·'cinematic style' 은 화풍 표지가 아니다.
- '원래 그림체 그대로' 는 새 화풍을 말한 것이 아니다.

| 요청 | 읽은 결과 |
|---|---|
| 니키와 푸토르가 대화하는 애니메이션으로 만들어 줘 | sure, anime |
| 수채화 동화풍으로 니키와 푸토르가 숲을 걷는 영상 | sure, custom ('watercolour painting and storybook illustration style') |
| 픽사 스타일로 … / CG 애니메이션으로 … | sure, toon3d |
| 3D 클레이 애니메이션으로 … | sure, custom ('claymation style') |
| 실사 영화처럼 … / 사실적인 느낌으로 … | sure, photo |
| 반 고흐 스타일로 … / 디즈니처럼 만들어 줘 | maybe |
| 니키가 펠트 인형을 만드는 영상 / 동화 같은 숲속에서 … / 영화처럼 … | 말하지 않음 |

- 구성안 지시문의 화풍 줄은 예비 경로에서도 위의 한 가지 줄이다 (모델을 부르기 전에는 칸이 빠질지 모른다). 11-24 에는 이 짐작으로 줄을 골랐다.
  - sure → "요청한 화풍을 bible.style 에 써라".
  - maybe → "특정 화풍(화가·스튜디오·영화 종류의 모습 포함)을 말했으면 그것을, 아니면 라이브러리 화풍을 써라".
  - 말하지 않음 → "라이브러리 화풍을 베껴라".
- 구성안이 나온 뒤 라이브러리 인물이 있으면 화풍을 정리한다 (`cast_lib.apply_to_story` 의 예비 경로).
  - sure: 구성안 화풍의 열쇠가 요청 열쇠와 같으면 둔다(custom 은 요청한 재료 말이 들어 있어야 한다, `keeps_said_style`). 아니면 요청 화풍의 기본 글로 채운다 (`said_style_fill`).
    - anime·photo·toon3d 는 열쇠의 기본 글(`cast_lib.SAID_STYLE_TEXT`), custom 은 요청 말을 영어 이름으로 옮긴 글(`custom_style_text`), semi 는 라이브러리 화풍.
  - maybe: 구성안 화풍이 실사가 아니면 둔다. custom 이면 그림·재료·화가·화풍 갈래 말이 있을 때만 둔다. 아니면 라이브러리 화풍.
  - 말하지 않음: 구성안 화풍이 비었거나 실사거나 custom 이면 라이브러리 화풍, 반실사·애니·3D 애니로 읽히면 그대로 (1차와 같음).
    - custom 을 덮는 까닭: 모델이 지어낸 'whimsical fantasy concept art' 가 custom 이 되어 승인된 반실사 기준 그림을 바꾸고 장소 그림을 정밀 판으로 그렸다. custom 은 사용자가 화풍을 말했을 때만 쓴다.

**두 경로에 같은 것**

- 의도 규칙 R6b(채팅 말 '다시 만들어'를 그대로 반복할지 고칠지, `studio.intent`)는 구성안 모델을 부르기 전의 갈림이라 style_said 가 없다. 지금처럼 채팅 말을 `said_level` 로 읽어, sure 면 고치고 아니면 그대로 반복한다.
- 구성안이 화풍을 비우면 다듬을 때 기본값 'photorealistic cinematic live action' 으로 채운다 (`video.DEFAULT_STYLE`). 라이브러리 인물 영화는 그 뒤 라이브러리 화풍이 된다.
- 라이브러리 인물이 없는 영화는 구성안이 쓴 화풍(없으면 이 실사 기본값)을 그대로 쓴다.

### 14.4 화풍 원문을 모든 정지 그림에

- 정지 그림 지시문(구도·인물 입히기·③·새로 그리기·끝 키프레임·고쳐 잇기)의 화풍 문장 = "The art style of the whole film: {bible.style, 200자 이내}." + 열쇠 문장 (`video.style_keep`, `video.STYLE_KEEP`).
  - toon3d 문장은 "a stylized 3D animated feature film look … It is not a photograph, not a flat 2D drawing" 이다. 애니 문장의 'not a 3D render' 를 쓰지 않는다.
  - custom 문장에는 '아닌 것'이 없다 (무엇이 아닌지 모른다). 모르는 열쇠도 custom 문장이다.
  - 지시문이 1990자를 넘으면 화풍 원문을 80자까지 먼저 줄이고, 그래도 넘치면 앞 글을 문장 경계에서 자른다 (`video._with_style`). 끝의 사람 수 문장이 잘리면 복제를 막지 못한다.
- "painting style" 고정 문구는 "art style" 로 바꿨다.
- 장소·장면 그림 앞머리 (`director._scene_image_prompt`):
  - Qwen 2512(장면 그림 레시피)는 화풍 원문이 있으면 실사여도 앞에 붙인다. 이 레시피는 반실사 회화체 성향이라 실사 영화의 장소 그림이 그림처럼 나올 수 있다.
  - Z-Image 는 사실적이지 않은 화풍일 때만 붙인다.
- 장면 그림 글 끝 힌트 (`director.scene_hint`): 사실적이지 않은 화풍은 "그 화풍으로 그린 한 장, 사진 아님", 사진 쪽 custom 은 "그 화풍의 한 장"이다.
- 영상 글의 화풍 줄(`_style_line`)과 화풍 고정 문장(`style_lock`)은 custom·toon3d 도 원문을 쓴다. 실사만 따로 쓴다("Photorealistic cinematic look").
- 기준 그림 변환 글(`video.style_convert_prompt`)은 열쇠마다 다르다.
  - anime 는 2D 애니 극장판의 인물, toon3d 는 3D 애니 극장판의 인물로 그리게 한다.
  - custom 은 "a character drawn in this art style: {원문}", 사진 쪽 custom 은 그 화풍의 실사 영화 한 컷이다.
  - photo 는 14.6.

### 14.5 반실사가 아닌 영화의 장소 그림: 정밀 판 (실측 P3a~f)

- 실측 P3a~f (같은 부엌 장소 글 + 화풍 앞머리, Qwen-Image 2512): 빠른 판(Lightning 4스텝 cfg 1)도 앞머리 화풍을 대체로 따랐지만, 정밀 판(가속 끔 40스텝 cfg 4 + 화풍별 부정 글)이 더 충실했다.
  - 애니: 선이 깔끔하고 색이 평평했다 (빠른 판은 반실사 질감이 섞였다).
  - 실사: 명암·질감이 사진에 가까웠다.
  - 수채: 종이 결·번짐이 뚜렷했다.
  - 10-27 기록("Lightning 은 애니를 반실사로 끈다, 30스텝 cfg 4 + 앞머리 + 부정 글이면 모두 애니")과 같다.
- 그래서 열쇠가 semi 가 아닌 영화의 장소 그림과 사람 없는 장면 그림은 정밀 판으로 먼저 그린다 (`director._quality_still`·`_place_card`).
- 반실사는 사용자가 고른 빠른 판 화풍(`b_new_light`) 그대로다.
- 레시피 `qwen_image_2512_scene` 판 2:
  - 가속 스위치(238:229)를 `fixed.turbo` 로 적어 높음 파생 판 `qwen_image_2512_scene_quality` 가 생긴다 (`recipes.quality_variant`).
  - 그 판의 스텝(238:224)은 50에서 40으로 줄였다 (시간 절약, P3 값).
  - 부정 글 자리(`inject.negative`)를 더했다.
- 정밀 판의 부정 글(`video.QUALITY_NEGATIVE`)은 anime·photo·toon3d 가 열쇠별 글이고(cfg 4 라 실제로 적용된다), custom 은 레시피의 기본 부정 글이다.
  - 예: anime = 'semi-realistic, photorealistic, photograph, 3D render, realistic lighting, skin pores, low quality, blurry, text, watermark'.
- 정밀 판 작업이 실패하면 빠른 판으로 내려가고(사건 `quality_still_failed`), 그 레시피는 그 요청에서 다시 쓰지 않는다 (`failed_recipes`).
- 빠른 판을 찾을 때는 높음 판을 고르지 않는다 (`recipes.pick`). 빠른 판이 꺼졌다고 모든 장면 그림이 40스텝이 되지 않게 하려는 것이다.
- 예상 시간에 장당 약 90초를 넣는다 (`PLACE_QUALITY_SECONDS`, 실측 장당 약 1~2분 [추정]).
- 라이브러리 인물이 없는 영화는 장면 그림이 Z-Image 라 정밀 판을 쓰지 않는다 (`director._scene_image_recipe`).

### 14.6 실사 기준 그림: Qwen-Image 2.1 편집 (실측 P1)

| 시험 | 결과 |
|---|---|
| P1 Qwen-Image 2.1 편집, 25스텝 cfg 1 | 18초. 진짜 사진 질감에 같은 사람으로 보이고 가르마·앞치마·호박 브로치 유지 → 채택 |
| P2 Flux-2 Klein 4B 편집 | 사진 질감은 강하나 주근깨·나이 듦으로 다른 사람처럼 보임 → 쓰지 않음 |
| 11-20 S6 Qwen-Image-Edit 2511 Lightning | 그림 느낌이 남음 (기록과 같음) |

- 새 레시피 `qwen_image21_edit_photo` (action `edit_photo`)는 P1 그래프에서 프롬프트 보강 LLM 경로를 뺀 것이다.
  - LoadImage 470 → TextEncodeQwenImage21 474 의 `images.image_1`, 잠재는 474 의 셋째 출력(입력 그림 크기) → KSampler 458 → VAEDecode 457 → SaveImage 461.
  - 모델: qwen_image_2.1_int8_convrot, qwen3vl_8b_int8_convrot, qwen_image_2.1_vae_bf16 (메인 공용 저장소에 있음 확인).
  - action 을 `edit` 와 따로 둔 까닭: 같은 `edit` 면 id 정렬에서 `qwen_image21…` 이 `qwen_image_edit` 보다 앞이라 모든 편집을 가로챈다.
  - 글 노드 하나가 긍정(출력 0)·부정(출력 1)·잠재(출력 2)를 함께 내므로, 레시피 검사는 자리의 `output` 칸으로 출력마다 본다 (`recipes._photo_problems`, `roles_of(out=)`).
- 변환 차례 (`director._convert_recipes`): 실사(와 사진 쪽 custom)는 `edit_photo` 를 먼저 쓰고, 그다음이 1차 길인 가속을 끈 높음 편집(`qwen_image_edit_quality`, 없으면 빠른 편집)이다. 그 밖의 화풍은 빠른 편집 하나다.
- 변환 글은 P1 에서 통한 문장 모양이다: "Turn this illustrated portrait into a real photograph of the same person … It is not a painting, not an illustration, not a 3D render."
- 실사 변환 작업이 실패하면 1차 길로 한 번 내려간다 (사건 `cast_style_convert_fallback`). 그 레시피는 그 요청의 다음 인물에 쓰지 않는다.
- 바꾼 그림은 그 요청에서만 쓴다. 라이브러리에 `refs.anime`·`refs.photo` 로 저장하는 일은 사용자 승인 뒤 따로 한다 [TBD].

### 14.7 열쇠별 검사

| 열쇠 | 화풍 검사 문장 (`checks.style_negative`) |
|---|---|
| semi | a soft painted semi-realistic illustration, not a glossy 3D render, not a flat cartoon, not cel-shaded |
| anime | a 2D anime-style illustration with clean cel-shaded line art, not a 3D render, not a photograph |
| photo | looks like a real photograph or live-action film still, not a painting, not a cartoon, not a 3D render |
| toon3d | a stylized 3D animated film image, not a photograph, not a flat 2D drawing |
| custom | 없음. 화풍 원문 줄(`checks.style_trait`, 200자 이내)로 원문 화풍인지만 본다 |

- 실사 영화도 화풍을 본다: 장소·장면 그림과 클립 가운데 프레임에 실사 문장을 기대 특징으로 넣는다 (`director._style_extra`).
- 새로 그린 첫 화면·바꾼 기준 그림·끝 프레임은 `director._style_check` 로 본다 (custom 은 원문 줄).
- 기준 그림 변환 검사는 사람 수·몸에 더해 인물의 알아볼 소품(`checks.sign_trait`)과 화풍을 본다. 이 그림이 영화 내내 그 인물의 얼굴이라 `STUDIO_PICTURE_CHECK` 와 상관없이 늘 돈다.
- 구도 그림(회색 마네킹)은 custom 이면 화풍을 보지 않는다 (`director._layout_check`). 마네킹이 원문 화풍 검사에 걸려 헛되이 다시 그리지 않게 하려는 것이고, 인물을 입힌 다음 단계가 본다.

### 14.8 한계

- 라이브러리 기준 그림은 아직 반실사(`refs.semi`)뿐이다. 다른 화풍은 영화마다 바꿔 만들어, 영화가 달라지면 얼굴이 조금씩 다를 수 있다.
- custom 은 '아닌 것' 문장이 없어 검사가 약하다.
- 사용자가 화풍을 말했는지는 구성안 모델이 정한다 (style_said). 모델이 이 칸을 제대로 쓰는지는 실제 구성안 모델(qwen)로 아직 보지 않았다.
  - 모델이 화풍 말을 놓쳐 "" 로 쓰고 style 도 실사로 쓰면 잡는 검사가 없다 (요청 글은 읽지 않는다). style 이 애니·3D 애니·custom 이면 검사가 고쳐 쓰게 한다.
  - 구절의 열쇠는 지금 낱말 표로 정한다. 표 밖 말은 None 이라 구성안 모델이 정한 화풍을 믿는다.
  - 열쇠가 None 인 구절에 반실사 글이 오면 늘 고쳐 쓰게 한다 (분위기·장르 구절에 라이브러리 화풍을 베낀 답도). 최종 화풍은 같지만 한 차례가 헛돌 수 있다. 문구가 "" 를 고르게 하지만 실제 qwen 이 그렇게 하는지는 보지 않았다.
  - 'painted' 글('soft impressionist painted look')은 세 번 안에 못 고치면 그대로 남아 반실사로 그려진다.
  - 원래 그림체를 지키라는 구절을 "" 로 읽는 것은 한국어(`_KEEP_LOOK_RE`)뿐이다. 영어 'keep the original art style' 은 고쳐 쓰기 문구로만 고친다.
  - '흑백 3D 애니메이션' 처럼 열쇠가 toon3d 인 구절은 채울 때 흑백이 빠진다 (전부터 있던 문제).
  - 거절 구절은 거절 말의 꼴로 읽는다: '말고'·'빼고'·'대신'·'아니라'·'아닌', 그리고 구절 길에서만 '싫어'·'별로'·'제외'·'말구'·'안 돼' (`_PHRASE_NEG_AFTER`, 11-25 최종 확인에서 더함 — 그 전에는 '반실사는 싫어, 애니로' 를 청한 말로 읽어 반실사가 나왔다, `test_fe_01`). 이 밖의 꼴로 거절하면 그 화풍을 청한 말로 읽는다. 요청 글 예비 길(`_negated`)은 넓히지 않았다.
  - 고치기 차례의 '지금 그림체' 는 앞 결과가 긴 영상일 때만 안다 (구성안의 화풍). 짧은 영상을 고치는 차례는 모름(`SOME_LOOK`)이라 구성안이 고른 화풍을 믿는다.
  - 칸이 없는 구성안의 예비(요청 글 짐작)는 목록 밖 표현을 maybe 로 둔다 (넓히지 않는다).
- R6b 는 채팅 말을 예전처럼 읽어, '반 고흐 스타일로 다시 만들어 줘'(maybe)는 그대로 반복이다.
- 정밀 판 장소 그림은 장당 약 1~2분 더 걸린다 [추정].
- 영상 모델(LTX)이 장면 안에서 질감을 조금씩 바꾸는 것은 글로 막지 못한다. 말 없는 컷 레시피의 부정 글('cartoon, childish')은 cfg 1 이라 그대로 두었다.
- 실사 변환 레시피는 이번에 처음 쓰는 모델(Qwen-Image 2.1)이다. 접수·실행이 실패하면 1차 길(높음 편집, 없으면 빠른 편집)로 내려간다.

### 14.9 메인에서 확인할 것

1. `update.sh` 뒤 `studio-check` 가 `qwen_image21_edit_photo`·`qwen_image_2512_scene`(판 2)·`qwen_image_2512_scene_quality` 를 통과하는지 본다 (명령은 `docs/short-film-dialogue-design.md` 13.12). V11 은 점이 든 입력 이름 `images.image_1` 도 본다.
2. 니키가 나오는 애니 요청: 구성안 화풍이 애니로 남는지, 장소 그림이 정밀 판인지(사건 `place_card` 의 quality), 바꾼 기준 그림이 같은 인물·소품인지, 모든 컷이 같은 화풍인지.
3. 실사 요청('실사 영화처럼'): Qwen-Image 2.1 변환이 같은 사람·소품을 지키는지(사건 `cast_style_convert` 의 recipe), 장소 그림이 그림처럼 보이지 않는지, 걸린 시간.
4. custom 요청('수채화 동화풍으로')과 toon3d 요청('픽사 스타일로'): 원문 화풍이 첫 화면부터 끝 프레임까지 이어지는지, 화풍 검사가 헛되이 다시 그리게 하지 않는지.
5. 화풍을 말하지 않은 요청: 라이브러리 반실사가 그대로인지, 장소 그림을 빠른 판으로 그리는지.
6. 2~5 의 구성안: bible.style_said 가 요청의 화풍 말을 그대로 옮겼는지, 화풍을 말하지 않은 요청에서 "" 인지 (운영자 기록 화면의 bible JSON 에 보인다).
7. 고치기 차례의 지금 화풍 (fixD): 니키가 나오는 애니 긴 영상을 만든 뒤 "지금 그림체 말고 다른 스타일로 바꿔줘" 를 보낸다. 요청(`chat_turns.request`)에 `now_style` 이 애니 글로 적히는지, 구성안 지시의 요청 글 끝에 그 줄이 붙는지, 결과가 애니가 아닌지 본다. 화풍을 말하지 않고 만든(반실사) 영상 뒤에 같은 말을 보내면 반실사가 아닌지도 본다.
8. 잘 된 변환 그림을 사용자 승인 뒤 `refs.anime`·`refs.photo` 로 라이브러리에 등록할지 정한다 [TBD].
