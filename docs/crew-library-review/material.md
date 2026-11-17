# material_texture 검수 보고 (material)

## 수치
- 검수: 865항목 전부 (seed_product_focus 8개 제외)
- 고침(update) 327, 지움(delete) 8, 추가(add) 509 → 검수 뒤 항목 수 1,366
- `check` 오류 0, 경고 44 (모두 새 묶음 `weathering` 안내)
- 패치: `patches/material.jsonl` (생성 스크립트는 `work/material/`)

## 문제 유형별 (대표 예)
1. 칸·묶음이 틀림 (약 35): `floor_wall` 에 차체·전자·잡화·밧줄이 섞여 있었다. 재료 묶음으로 옮김 (예: 보트 선체 → plastic, 키캡 → plastic, 앞유리 → glass, 오크통 → wood, 녹슨 차체 → weathering). 조개껍데기는 stone → organic.
2. 글자·상표를 부름 (약 14): "embossed lettering shapes" → "embossed raised pattern", "grey text columns" → "halftone dot patterns", "neon tube" → "bent glass tubing filled with glowing neon gas", letterpress → 잉크 없는 블라인드 압인, 키캡 → blank keycaps, lurex → 라메(상표), 포스트잇·styrofoam·뷰티 블렌더 정리.
3. 사람 거름 오탐 (약 20): en 의 face·head·hair·figure 때문에 사람 없는 요청의 메뉴에서 빠짐 (예: "smooth pale cut face" → "cut surface", "rivet heads" → "domed rivets").
4. 재질 설명이 부정확 (약 40): 구리 팬 안팎이 뒤바뀜(바깥 구리색·안쪽 주석 은색), 분청 귀얄·킨츠기·델프트(주석유)·히노키(편백)·상고대(rime)·삼베(대마)와 황마 구분, 대모갑(바다거북 아님), 아노다이징 비드 블라스트, 캔디 도장(메탈릭 바탕 위 투명 착색).
5. fits 오류 (약 90): 탈것·제품이 아닌데 vehicle, 보석에 건축, 음식 아닌 액체에 food 등 정리.
6. 확인어(terms)가 너무 흔함 (약 70): "glossy", "thin", "pale" 만 있던 것을 그 선택만의 말로.
7. 중복·거의 중복 삭제 (8): glazed_donut_ceramic_blue, terrazzo_tile_speckled, obsidian_glass_black, melted_cheese_stretch, chocolate_sauce_flow, honey_drip_golden, ice_crystals_feathery, tree_sap_glossy_bead.
8. ko·태그: 한 글자·뜻 헷갈림 정리 (김→구운 김, 떡→쫀득한 찹쌀떡, 짚→볏짚, 뿔→사슴뿔, 랩→비닐 랩, 수정→투명 수정 결정, 재유→재유약 흘림), '녹은 왕초' 오타(→ 녹은 양초 웅덩이), 제품 요청에만 걸려야 할 태그(차·크림·샴푸·텀블러) 정리.

## 추가 (509)
금속 마감(PVD·비드블라스트·청화·열변색 등) 44, 도자 29(상감청자·청화백자·철화·기와·전돌·뚝배기), 유리 18, 화장품 제형·피부 마감 25, 가죽 15, 직물 48(삼베·광목·쪽빛·감물·누비·색동·금박 등 한국 전통 직물 포함), 바닥·벽 마감재 39(장판지·우물마루·화문석·꽃담·징크·브리즈 블록), 도장·미술 재료 28(옻칠·나전칠기·단청), 음식 62(한식 중심), 액체 21, 종이 18, 자연 표면 36, 플라스틱·복합재 20, 목재 35(수종·가공), 석재·원석 31, 노화·손상 40.

## 새 묶음
- `weathering` (40개 + 옮긴 4개): 녹물·백화·이끼·누수·그을음·햇볕 바램 등 재료를 가리지 않는 노화·손상 표현. 기존에 노화 표현이 칸마다 흩어져 있어 한 곳에 모았다. 어두운 분위기 3개(거미줄 구석·침수 자국·재 덮인 표면)는 `dark: true`.

## 정하지 못한 것
- material_texture 는 아직 단계가 없다 (`crew_knowledge.json` 에 재질 단계 추가 시 prefer 후보: metal, fabric, wood, stone, ceramic, glass, leather, paint_finish, weathering).
- 한글 태그 일부가 `subject_nouns`(크림·빵·병·음료·케이스 등)와 겹쳐 그 제품 요청에서만 나온다 (의도된 동작이나 18개 정도 '크림' 포함). 남은 것은 필요하면 subject 거름 기준을 다시 볼 것.
- 미수정으로 남긴 항목은 터치하지 않은 약 480개 (큰 문제 없다고 판단한 것).
- 사용자 의도 사전(`crew_intent.json`) 후보: 무광·유광·브러시드·옻칠·한지·자개·녹슨·낡은·빈티지 같은 마감 말.
