# loc_interior 보고서 (location_set: 도시·상업·실내 14개 묶음)

## 수
- 검수: 1,074항목 전부 읽음 (audit 후보 132 포함)
- 고침(update) 429, 지움(delete) 5, 추가(add) 414 → 범위 안 최종 약 1,483항목
- check 오류 0 (경고는 긴 id(기존 항목)·'map'(작전 회의실 map table, 의도적)·ko 의 영문(KTX/PC방 등)뿐)

## 문제 유형별 (전 → 후)
- 글자·간판을 부르는 말 (약 90): "neon sign" → "neon tubes/neon glow", "menu board"/"price tags"/"posters" 제거, "wanted posters" → 삭제. 예) 도넛 가게 "a neon sign" → "pink neon tubing"
- 사람 거름 문제 (약 130): workers·guests·commuters·crowd·travelers 등이 en 에 들어 있어 사람 없는 요청에서 아예 못 나오거나(걸리는 낱말), 반대로 거름에 안 걸리는 낱말(staff, travelers, guests)로 사람이 몰래 그려짐. 사람 없이 쓰거나 character 로 정리. 예) 제철소 "workers in silver suits" → "huge ladles hanging from crane hooks"
- 제품 배경 단계에서 못 나오던 묶음: home_room·hospitality·공방류에 fits=product 가 없어 제품 광고 배경에 나온 적이 없었음 → 약 130개에 product 추가
- 중복/비슷한 항목: 5개 삭제(언덕 야경 공원, 거리 테라스, 지하상가 통로, 향신료 바자르, 심야 주유소) + 같은 한국어 이름('벼룩시장','얼음 궁전','수산시장','시장') 구분
- 틀린 ko·태그·문법: "a apartment"·"a astronaut", '빌라촌'·'다방'·'짜장면집' 태그가 다른 항목 뜻, 오타 '보호 거실'
- 어두운 항목 표시(dark): 폐병원 복도, 영안실, 감시 복도, 저주받은 다락방, 지하 묘지, 강령술사 소굴, 교도소(추가) 등
- 칸이 틀린 항목: 성가대 오르간(hospitality→public_interior), 바위 마을 열기구(urban_street→rural)

## 추가 (414)
한국 공간(빌라 골목·옥상 초록 방수 바닥·재개발 골목·붕어빵 수레·노포 골목·시장 먹거리·학원가·복도식 아파트·아파트 베란다/현관/분리수거·김치냉장고 주방·옛 문방구·방앗간·한복 대여점·동네 중국집·치킨집·호프집·백반집·빙수·경양식·찜질방 외 목욕탕·태권도장 등), 업종별 상업 공간(매장 약 50종), 교통·공공·업무 공간, 미래·판타지 실내, 한국 판타지(도깨비 시장·여우 사당), 제품 촬영 세트(한지 병풍·소반·시트러스·크롬 웨이브 등), 계절 세트.

## 새 묶음
없음 (기존 묶음만 사용).

## 정하지 못한 것 / 제안
- hospitality 묶음에 스포츠·사우나·숙박이 섞여 있음 (sports_leisure / lodging 로 나누면 place_hints 에 유리)
- crew_knowledge.json place_hints 에 추가 제안: 옥상, 베란다, 원룸, 편의점, PC방, 찜질방, 포장마차(urban_street/cafe_food/retail/home_room), 스튜디오 → studio_backdrop
- crew_intent 후보: 한국 공간 말('노포','반지하','옥탑','복도식 아파트')
- 사람이 핵심인 장소(공연 객석, 패션쇼)는 사람 없이 고쳤으므로, 사람 요청용 변형은 character 쪽에서 별도 필요
- 남은 긴 id(약 40개)는 규칙상 바꿀 수 없어 그대로 둠
