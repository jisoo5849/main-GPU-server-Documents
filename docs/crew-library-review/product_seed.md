# product_seed 보고서 (product_presentation 전부 + seed.jsonl 136항목)

## 숫자
- 검수: product_presentation 510 + seed 136 (이 중 시드 제품 연출 8개는 양쪽에 걸침) = 약 638
- 고침(update) 440 / 지움(delete) 88 / 추가(add) 141. check 오류 0, 경고 0
- product_presentation: 510 → 563 (지움 88, 추가 141). 보호 항목(front_single 등)은 id 유지, 내용만 손질
- seed 는 지우지 않고 116개만 고침(나머지 20개는 문제 없음)

## 문제 유형
1. 같은 틀에 제품 이름만 바꾼 반복: 18종 x 18틀 = 319항목. 235개를 제품의 실제 촬영 관습으로 다시 씀(향수 분사구·시트러스 노트, 다이버 워치, 케이스 접사 등), 84개는 지움
   - 지운 이유: 가죽 가방·지갑·종이 노트·과자 봉지를 물 위나 이끼 바위에 두는 조합, 시드(podium·hero_45·levitating)·일반 항목과 같은 뜻, 일반 항목과 같은 뜻인 diorama/giant/paper 반복
   - 예: `a leather wallet resting on a rippling water surface ...` → 삭제(소재 손상 조합) / `a polished steel wristwatch resting on a stepped round podium ...` → `a watch wrapped around a cylindrical stone pedestal under a soft spotlight`
2. 사람 거름에 걸려 메뉴에 한 번도 안 나오던 항목: diorama 18종 ("miniature figures"), toy_parade, miniature_world, product_as_architecture, kids_room → 사람 말 제거 또는 삭제
3. 대상 이름 거름 문제(crew_lib._entry_subjects): ko/태그의 '테이블'·'공간'·'방'·'향수(추억)'가 다른 제품 요청에서 항목을 빼던 것(natural_table, wide_room, nostalgic, futuristic_lab, vanity_table 등)과, 제품 전용인데 대상 이름이 없어 모든 요청에 끼던 것('패션 소품'=지갑, '웨어러블'=스마트워치, 영양제, 베이커리) → 태그를 맞게 고침
4. fits 가 vehicle 뿐이라 제품 메뉴(cat=product)에 안 나오던 자동차 항목 → fits 에 product 추가
5. 글자·모양을 부르는 말: graffiti wall, 지도(map), 동전, 자, 여권, 손글씨 카드, poster, `neon-lit city` 간판, `silhouette`(promptx 규칙 6) → 풀어 씀
6. 약한 확인어와 잘린 태그: seed 의 `alone`·`simple`·`dark`·`candinavian`·`apanese`(잘림), 40자로 잘린 tags_en, 조각말 태그('함께','있게','오는') → 교체. `minimal`/`art-deco` 처럼 문장과 낱말이 안 맞아 확인 검사가 늘 실패하던 것 수정
7. 추상 문장: seed 분위기 10+8+6개가 두 낱말뿐이라 보이는 요소(색·빛·질감) 보충
8. 칸이 틀린 것: orbit_around, slow_zoom_close(영상 카메라 움직임) 삭제, gradient_sweep·overhead_top_down 은 시드와 중복이라 삭제

## 추가한 141개 (새 묶음 없음, 기존 present_* 묶음)
- 일반 기법 86: 투명 큐브·벽감·원형 프레임·물막·서리, 비 오는 창가·호텔·이끼 바닥·계곡·소금 사막·밀밭·벽난로, 쌓기·나선·타공판·빨랫줄·저울·서랍, 파도·컨페티·줌 버스트·김, 스노우볼·떠 있는 섬·얼음 동굴·네온 그리드 등. 대상 이름 없음(모든 제품에 나옴)
- 업종별 55: 향수·화장품·음료·주류·시계·신발·가방·주얼리·전자기기·가구·완구·반려동물·식물·식품·생활용품. ko/tags_ko 에 대상 이름이 있어 그 제품 요청에서만 나옴

## 정하지 못한 것 / 제안
- crew_knowledge.json `subject_nouns` 에 추가하면 좋은 말: 영양제, 비타민, 선글라스(이미 있음), 케이스, 베이커리, 디저트, 지갑(있음). 영양제·디저트·스킨케어는 없어서 제품 전용 항목이 일반 항목처럼 나옴
- `prefer` 의 `present_` 접두어는 그대로 유효. 시드 묶음 `seed_product_presentation`(8개)은 유지
- 다른 칸에서 같은 문제: lighting.product_light·camera.product_camera 에 `label readable angle`(camera.product_label_readable_angle) 같은 라벨 요구 항목이 있음 (담당 칸에서 처리 필요)
- crew_intent.json 에 넣을 만한 말: 누끼(흰 배경), 플랫레이, 팩샷, 호캉스
- seed 의 `genre_style.none`(보호)은 손대지 않음. seed 의 `mood_emotion.futuristic`/`luxurious` 는 내용만 손질
- `crew.TEXT_NOTE_RE` 에 `map`·`poster` 는 없으나 이미지 모델이 글자를 그리기 쉬워 항목에서 피함
