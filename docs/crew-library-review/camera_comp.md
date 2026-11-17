# camera_comp 보고서 (camera + composition)

## 수치
- 검수: camera 557, composition 454 (seed 묶음 제외). 합 1,011항목.
- 고침(update) 396, 지움(delete) 83, 추가(add) 335 (camera 287, composition 48).
- 적용 후 예상: camera 800, composition 485. `check --facet camera,composition` 오류 0 (경고 8: ko 의 영문 약어, 'cover' 낱말).
- 보호 항목 camera.eye_level_medium 은 건드리지 않았다.

## 문제 유형과 대표 예 (전 → 후)
1. 글자·로고·라벨·제목 자리 요구 (약 35건): `brand label faces the lens` 항목 지움(정면 샷과 동일), `space for a headline at the top` → `wide empty band across the top`, `neon signs` → `neon lights`, `embossed logo` → `embossed pattern`, 메뉴판·글자 더미 포스터·로고 자리 원형 칸은 글자 없이는 성립하지 않아 삭제.
2. 영상 움직임이 정지 화면 항목으로 들어옴 (약 25건): 달리 인·줌·팬·짐벌·스테디캠·틸트 업/다운·하이퍼랩스 등은 video_motion 칸 소관이라 삭제하거나, 정지 화면에 남는 모습(휩팬 번짐, 불릿타임 정지, 낮 장노출)으로 바꿈.
3. 사실 오류: 클린 싱글(어깨 없는 샷)을 오버숄더로 설명 → 더티 싱글로 정정; 핀홀 카메라가 왜곡 있다는 설명 → 렌즈가 없어 균일하게 부드러움; 시니스터 대각선 방향 반대 → 왼쪽 위→오른쪽 아래; 파이 그리드 '대각선' → 황금 삼각형; 1점 투시가 '코너'에서 → 벽 정면; 아나모픽 보케 타원 → 세로로 긴 타원; 핀쿠션 왜곡 방향 정정; 렌즈 숨 쉬기(영상 현상) 삭제.
4. 중복·거의 중복 (약 50건): 같은 뜻 쌍 중 약한 쪽 삭제하고 태그를 합침 (예: 틸트시프트 역/항구/군중, 드론 추격 두 개, 수평선 위치 두 항목은 구도 칸에 이미 있어 삭제).
5. 사람 낱말·fits 문제 (약 60건): `face`·`head`·`model`·`crowds`·`hair` 같은 낱말이 사람 없는 요청에서 항목을 막음 → 다른 낱말로; 손·발이 나오는 항목은 사람 거름에 걸리게 `a person's` 로 명시.
6. 대상 이름 태그가 항목을 막음 (약 45건, '카메라'·'방'·'공간'·'문구' 등), 자동차 항목에 '자동차' 태그와 fits product 추가 (자동차 광고는 제품 분야로 들어옴), 음식 항목 fits 에 product 추가, 일반 광학 항목 fits 를 any 로.
7. 숫자 태그(35mm, f1.4 등)를 tags_en 으로 이동.

## 추가 (335: camera 287, composition 48)
- camera: 필터·특수 광학(편광, 하프 ND, 크로스 스타, 프리렌징, 크리스털 볼, 적외선, 슬릿 스캔 등), 노출·셔터(장노출 번개/불꽃/별, 후막 동조, 스트로보), 특수 시점(도어뷰, 도어벨캠, 웹캠, FPV, 반사경, 포장마차 안), 프레임 장치(한옥 창살, 원형 문, 홍살문), 제품 종류별(의류·신발·시계·향수·화장품·전자기기·가방·주얼리), 음식(한식 상차림 포함), 한국 공간(대청마루, 원룸, 복층, 처마·단청), 풍경·인물·차량·스포츠.
- composition: 황금 분할·동적 대칭, 삼분할 변형, 시선 흐름, 여백 구도(글자 자리는 '비워 둔 면'으로), 포맷(레터박스, 톤도, 트립틱), 층·깊이·대칭 변형.

## 새 묶음
없음 (기존 묶음만 사용).

## 정하지 못한 것
- 영상 움직임으로 보낸 카메라 무빙(달리, 줌, 짐벌 등) 중 video_motion 에 아직 없는 것: crash zoom, steadicam, gimbal, jib, hyperlapse. video_motion 담당이 확인 필요.
- crew_knowledge.json 의 camera prefer 에 `framing_device`, `viewpoint` 를 이미 쓰지 않음: 제품에는 product_camera 가 있으나 음식 광고는 `food_camera` 가 없어 메뉴에 안 나온다 → product 의 prefer 에 `food_camera` 추가 제안. 자동차 광고용으로 `vehicle_camera` 도 제안.
- 의도 사전(crew_intent.json)에 넣을 말: 틸트시프트/미니어처, 어안, 매크로·접사, 아나모픽·시네마스코프, 로우/하이 앵글, 탑뷰·플랫레이.
- 글자 자리 요구(포스터·배너·썸네일)는 항목으로는 '비워 둔 띠·면' 만 남겼다. purpose_words 쪽 문구는 그대로 둠.
