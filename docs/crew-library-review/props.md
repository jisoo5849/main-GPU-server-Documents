# props 칸 검수 보고 (소품 담당)

## 수치
- 검수: 1,077항목 전부 (seed 4개 제외). 고침 312, 지움 36, 추가 327 (순증 291 → 약 1,372항목).
- check: 오류 0. 경고 154 = 새 묶음 알림 151 + 흔한 말 태그 `light` 1(스테인드글라스, 의도) + `cover`·`map` 각 1(저널, 여행 지도: 표지·지도가 소품 자체).

## 문제 유형별 (전 → 후)
1. 글자·라벨을 부르는 말 (약 25건): `a plain neon arrow sign` → `a glowing pink neon arrow of bent glass tubing`; `unlabeled body` → `smooth plain body`; `letters` → `sealed envelopes`; `writing desk` → `wooden desk`; 번호판·도로 표지판 삭제.
2. 소품이 아닌 항목 (36건 삭제): 장소(조종석·격납고·클린룸·계단식 논·차밭·포도밭·터널·승강장 등, location_set 에 같은 것이 있음), 조명·대기 효과(창 그림자·잎 그림자·먼지 빛줄기·물결 빛·공중부양·불꽃놀이 등, lighting/atmosphere/genre 에 있음).
3. 대상 이름 제한으로 공간 요청에서 빠지던 항목 (약 90건): ko·태그에 병·책·쿠션·화분·와인·테이블·카드·키보드 같은 제품명이 있으면 crew_lib 가 그 말이 없는 공간 요청에서 항목을 뺀다. 예: `면봉 병` → `면봉 유리병`, `커피 테이블 책 더미` → `커피테이블 북 더미`. 사무실·주방·카페처럼 공간 요청에도 나와야 할 것은 태그에 그 공간어를 넣고 제한을 일부러 남김.
4. 사람 거름 오탐 (약 25건): `heads`·`face`·`hair`·`model`·`child`·`baby`·`couple`·`chef`·`dancer` 가 들어가 사람 없는 요청에서 빠짐. 예: `hydrangea heads` → `hydrangea blooms`, `baby shoes` → `newborn booties`, `chef knife` → `wide bladed kitchen knife`.
5. 문화·사실 오류: 칼라(카라)는 백합이 아님, 홍등·붉은 봉투(중화권)와 한국 세뱃돈·복주머니 구분, 복주머니 모양 정정, 방패연 방구멍, 가야금은 눕혀 둠, 송편은 한과가 아니라 떡, 돌솥과 뚝배기 구분, 청자 상감 묘사, 갓 태그 보강, 공작 부채 ko·en 불일치, 깃펜 ko·en 불일치, 수련·연꽃·무궁화 태그 정리.
6. 상표·태그: 레고·포스트잇·폴라로이드 태그 제거. 엉뚱한 요청에 걸리는 태그(바람·시간·모래·왕관·수정(고치다)) 정리.
7. 확인어가 흔하거나 틀림 (약 80건): `table`, `blue`, `wooden`, `light` 등 → 그 선택만의 말.
8. 같은 한국어 이름: `유리 구슬` 2건 → 구분. fits 보강(식음·카페 소품에 space 추가 등).
9. 어두운 표시: 해골 촛대, 거미줄 장식에 `dark: true`.

## 새 묶음 (7)
- `kitchen_props` 52, `bathroom_props` 31, `korean_everyday` 68 (한국 생활·옛 가정·분식·명절 소품), 그리고 기존 묶음에 추가: home_decor 66, kids_toys_baby 41, cafe_bar_props 40, library_office_props 29.
- 신규 묶음은 kitchen_props, bathroom_props, korean_everyday 3개. 이유: 지침이 꼽은 공간별 소품(주방·욕실)과 한국 생활 소품이 기존 묶음에 없고, 섞으면 prefer 로 따로 지정할 수 없다.

## 정하지 못한 것
- `crew_knowledge.json` 공간 분야 props 단계 prefer 에 `kitchen_props`, `bathroom_props`, `korean_everyday` 추가 권장. 지금은 prefer 밖이라 "다양성 칸"(약 25%)으로만 나온다.
- `place_hints` 에 `욕실`→bathroom_props 는 location_set 용이라 해당 없음. props 에는 장소 고정이 없어 주방·욕실 요청의 props 단계는 태그 점수로만 맞는다.
- 추가분 목표 중 아래는 이번에 얕음: 계절·행사 소품(gift_festive 기존 44), 반려동물(14), 여행(14), 웨딩(11), 업종별 촬영 소품(뷰티·푸드·패션·테크). 다음 차례 후보.
- 장소처럼 보여 지운 것 중 타 칸에 같은 항목이 없던 것은 없음(모두 location_set·lighting·atmosphere 에 대응 항목 확인).
- `crew_intent.json` 후보: 없음(소품은 사용자 의도 사전에 넣지 않는다).
