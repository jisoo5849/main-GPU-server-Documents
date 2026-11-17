# loc_nature 보고서 (location_set 자연·이세계 묶음)

## 수치
- 검수: 689항목 (rural, coast_sea, mountain, water_body, forest, desert, jungle, cave, sky, underwater, waterfront, ruins, otherworld_scifi, otherworld_fantasy)
- 고침 146, 지움 3, 추가 368. 결과 묶음 합계 약 1,056항목 (16개 묶음).
- check: 오류 0, 경고 93 (대부분 기존 긴 id, 새 묶음 알림)

## 문제 유형과 대표 예 (전 → 후)
1. 사람 낱말 때문에 사람 없는 요청에서 메뉴에서 빠짐 (약 30건): `cliff faces`, `its face`, `passenger`, `wizard`, `crowd`, `workers`, `hiker`, `riders` 등. 예) 출렁다리 "between two cliff faces" → "between two sheer cliffs"; 말 탄 사람 해변 → "새벽 해변을 달리는 말들".
2. 글자·간판 유발 (sign, signs, banners, 시계 문자판, 이정표, 광고): 사막 모텔 "neon sign" → "pink neon tubes"; 이정표 갈림길 → 벤치; 시계탑 → 거대 모사시계; 깃발 → 창·방패.
3. 상표·고유 이름: "jeep" → "off-road truck"; "ring world" → "ring-shaped world"; 태그의 실제 지명(지중해·북해·알프스·영국·유럽·동해 등)을 일반 이름으로.
4. 칸이 틀린 항목을 묶음 이동(update로 group 변경): 사바나·초원·스텝 → grassland, 툰드라·극지 기지 → polar, 안 맞는 갯벌/눈들판/외딴 나무/서리 들판 → 알맞은 묶음, 남극·북극 얼음 → coast_sea.
5. 중복·거의 중복 삭제 3: 잔교 일출(sunrise_pier…), 사이프러스 언덕길, 수족관 터널(실내 묶음과 중복). 비슷한 쌍은 구분되게 고침(튤립 이랑, 빨간 등대, 염전, 해저 대리석 도시, 유리 숲 낮 장면).
6. 지질·생태 정확성: 억새≠팜파스그라스, 칠면초·함초, 잔교≠방파제, 빙어 낚시는 저수지, 마른 삼림 등.
7. terms 가 흔한 말뿐인 10건(giant, valley, sea 등)을 고유 구절로. fits 에 landscape 누락 약 30건 보강.
8. 어두운 항목 표시(dark): 종말 폐허 3, 디스토피아, 다크 판타지 3, 카타콤, 유령선, 으스스한 숲.

## 새 묶음
- `grassland` (14): 사바나·스텝·팜파스·대평원. 농촌(rural)과 섞이면 '시골' 요청에 기린·사자가 나옴.
- `polar` (15): 빙원·툰드라·극야·백야. 사막·산 묶음에 있던 극지 항목을 정리.

## 추가 영역
한국 시골·산·바다·계곡·숲(정자나무, 장승, 원두막, 황태 덕장, 간이역, 오름, 상고대, 산성, 곶자왈, 편백 숲, 저수지 좌대, 고인돌, 절터), 화산·빙하 지형, 카르스트, 습지·지열, 하늘 현상·구름 종류, 수중 생물·심해, 동굴 지형, 우주 공간·외계 지형·미래 지구, 한국·동양 설화와 서양 동화·초현실 판타지.

## 정하지 못한 것 (조정자에게)
- `crew_knowledge.json` 의 landscape 단계 prefer 에 `grassland`, `polar` 추가 필요 (otherworld_ 는 이미 접두사로 포함).
- 제품 단계 prefer 에도 넣을지: sky·coast_sea 처럼 배경으로 쓸 만한 `grassland`.
- `place_hints` 후보: 초원·사바나·평원 → grassland, 극지·남극·북극·툰드라 → polar, 계곡·폭포·저수지 → water_body, 동굴 → cave.
- `crew_intent.json` 후보: 상고대, 안개/해무, 노을(낙조), 오로라, 은하수, 장마.
- 한국 지명 없는 일반 이름만 썼음. 기존 `fisherman_stilt_poles_dawn_mist_lake` 는 낚시꾼이 나와 fits 에 character 유지(사람 요청에서만 사용).
