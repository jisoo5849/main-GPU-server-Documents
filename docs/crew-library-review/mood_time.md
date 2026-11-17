# mood_time 보고서 (mood_emotion · time_weather)

패치: patches/mood_time.jsonl (check 오류 0, 경고 23 = 새 묶음 mood_sensory 알림뿐)
작업 도구: work/mood_time/ (b10_mood.py, b20_mood_new.py, b30_tw_fix.py, b40_tw_new.py, build.py, qa.py, sandbox.py)
확인: 패치를 적용한 복사본에서 test_crew(36), test_video(73), test_promptx(15) 모두 통과. 저장소는 건드리지 않음.

## 수
| | mood_emotion | time_weather | 합계 |
|---|---:|---:|---:|
| 검수(씨드 제외) | 516 | 375 | 891 |
| 고침 | 256 | 328 | 584 |
| 지움 | 260 | 3 | 263 |
| 추가 | 88 | 51 | 139 |
| 적용 후 (씨드 제외) | 344 | 423 | |
| 적용 후 (씨드 포함) | 368 | 433 | |

목표 범위: mood 300~700, time_weather 300~600 안.

## 문제 유형과 대표 예
1. 분야 6개로 기계적 복제 (mood 516 = 기본 86 x 6). 문장 꼬리말만 다름("on a building exterior…", "on a portrait of a person…").
   - 건축·음식·인물 260개는 지움. fits 가 건축·음식·인물이라 지금 단계(제품·공간·풍경)에는 한 번도 나오지 않았고, 인물은 사람 단어라 사람 없는 요청에서도 빠짐.
   - 제품·실내·풍경 256개는 분야마다 다른 화면 단서로 다시 씀.
   - 예: cheerful_interior 전 "…bouncy open composition in a lived-in room with matching furniture…" → 후 "sunlight pouring through wide windows onto white walls and bright primary colored accents".
   - 단서가 억지인 것(adrenaline 실내, minimal_luxury 풍경)은 만들지 않고 지움.
2. 추상어. "echo the feeling", "matching props", "a quiet focal point" 같은 말 → 빛·색·질감·구도로.
3. 태그 문제. 모든 실내 분위기에 '실내 공간' 태그가 있었는데 '공간'이 대상 이름(subject)이라 공간 요청에서 86개가 전부 메뉴에서 빠졌음 → 제거. time_weather 도 ko·태그에 우산·카페·차·방·베란다·커피·음료·가게·자전거가 든 항목은 해당 대상 요청에서 빠지므로 말을 바꿈(예: "카페 차양 아래 비" → "차양 아래 빗줄기").
4. 의도 사전(crew_intent.json)과 태그가 안 맞음. '비 오는'·'밤'·'노을'·'새벽' 등과 맞는 항목이 의도마다 1~4개뿐이라 알맞은 장면을 골라도 검사에 걸렸음. 항목에 영문 확인어 태그(rain, night, sunset …)를 달아 의도별 쓸 수 있는 항목이 비 오는 3→51, 밤 1→39, 노을 4→22, 겨울 2→74 등으로 늘었음.
5. 사람·소리·냄새·간판. time_weather 24개가 사람 단어(군중·얼굴·행인·농부·children, 'grain heads' 의 heads)를 담아 사람 없는 요청에서 빠졌음 → 사람을 빼고 고침. 소리·냄새(foghorn, rumble, hiss, smell of dust, singing birds)는 보이는 모습으로. neon signs → neon tubes/lights.
6. fits. time_weather 는 풍경 단계에서만 쓰이는데 바깥 도시 장면 81개가 건축·탈것만 표시돼 풍경 요청에서 빠졌음 → landscape 추가.
7. 정확성.
   - 부무지개는 색 순서가 반대이고 두 무지개 사이가 어둡다.
   - 22° 햇무리 안쪽 테는 붉다.
   - 붉은 스프라이트는 해파리 모양.
   - 개기일식은 지평선 전체가 노을처럼 빛남.
   - 초승달 어두운 부분은 지구조(earthshine).
   - 한국 위도에서는 정오에도 해가 머리 바로 위가 아님.
   - '층운'→층적운, '무리해'→해무리, '매실 장마'→장마·장맛비, 금환식은 개기일식과 다른 현상이라 태그에서 뺌.
   - 황사(봄 누런 먼지)와 모래폭풍 벽(하붑) 태그 분리.
   - 강에 띄우는 등은 유등(연등은 연꽃 등).
   - '벚꽃 엔딩'(노래 제목) 태그 제거.
   - 싸락눈(graupel)은 가루눈과 다른 현상이라 새 항목으로 분리.
8. dark. 재난·공포 항목에 dark 추가: 해일·홍수·산불 연기·화산재·눈사태·화염 회오리·급류·묘지 안개, 분위기 eerie·haunting·noir·ominous.
9. 거의 중복. time_weather 3개 삭제: season_spring_dusk_blossom_lamp(밤 벚꽃 야경과 같음), snow_christmas_street_decor(크리스마스 이브 눈과 같음), snow_village_fireplace_smoke(눈 덮인 겨울 마을과 같음). summer_heat_haze_road 는 도시·사막 열기와 겹쳐 논길로, tropical_wet_season_steam 은 정글 수증기와 겹쳐 적란운으로 바꿈.

## 새 묶음
- mood_sensory (분위기, 23개): 한국 광고·SNS 가 쓰는 감각 말(촉촉·보송·투명·달콤·차가운·따끈·탄산·크리미·매끈·청정). 기존 8개 묶음에 맞는 자리가 없고 화면 단서(물방울·김·기포·광택)로 쓸 수 있어서. crew_knowledge.json 의 분위기 단계는 prefer 가 없어 새 묶음도 그대로 메뉴에 나옴.
- 나머지 새 분위기는 기존 묶음에 넣음(설렘·통쾌·아기자기 → bright, 고즈넉·단아·정갈·은은 → calm, 그리움·아련·감성 → nostalgic, 황량 → dark, 경외·아찔·야생·모험·힙 → energetic, 퇴폐·중후 → luxury, 키치 → surreal, 휴양지 → seasonal).

## 새로 넣은 것
- 분위기 88개: 감정 폭(설렘·그리움·아련함·경외·통쾌·황량·아찔·모험·고독한 새벽 감성), 한국어 정서 말(고즈넉·단아·정갈·은은·아기자기·감성·생활감·빗소리 힐링), 감각 말.
- 날씨·시간 51개
  - 시간대 빛: 아침 골든아워, 블루아워, 알펜글로우, 비너스의 띠, 월출, 달빛 바닷길, 은하수, 도시 불빛에 물든 밤구름, 시골 자정, 열대야.
  - 하늘 현상: 잔광, 아침놀, 안개 무지개, 달무지개, 채운, 파도 구름, 비행운, 쾌청, 달무리, 김안개, 미류운, 유방운, 개기월식.
  - 한국 계절: 진달래, 개나리, 이팝나무, 청보리, 메밀꽃, 감나무, 산수유, 봄 아지랑이, 겨울 바다, 배롱나무, 철새, 여름 계곡, 장마 끝, 첫서리, 눈 덮인 논, 빙폭, 겨울비.
  - 명절·행사: 새해 첫 일출, 정월대보름, 연등, 설날 아침 한옥 마을, 빛 축제.

## 정하지 못한 것 (제안)
- 보호 항목 time_weather.rainy / snowy, mood futuristic / luxurious, 씨드 묶음(seed_*)은 손대지 않음. 씨드 time_weather 의 ko_tags 에 '오는', '시간' 같은 흔한 말이 있고 en_tags 가 잘려 있음(씨드 담당 확인).
- crew_intent.json 에 넣을 말
  - time_weather: 블루아워, 매직아워, 골든아워, 은하수, 달빛, 첫눈, 장마, 황사, 열대야, 안개비, 우박, 서리, 진달래·개나리·유채 같은 계절 꽃.
  - mood_emotion: 설렘, 그리움, 아련한, 고즈넉한, 정갈한, 은은한, 촉촉한, 키치한, 경외감, 통쾌한, 황량한.
  - '여름'(현재 intent 로는 time_weather 항목 29개가 잡히지만 '여름밤'·'열대야'는 아직 없음).
- 사람이 나오는 분위기(인물 컷)는 제작진 모드가 건너뛰므로 만들지 않고 지움. 인물 분야를 연다면 별도 묶음으로 새로 써야 함.
- time_weather 풍경 단계는 prefer 가 없어 묶음 77~78개짜리 season 이 메뉴를 많이 차지함. 필요하면 time_of_day·clear_sky 에 prefer 를 줄 것.
- 영상 쪽 video.py 는 time_weather 의도를 장면에 쓰므로(날씨는 장면 글에 있어도 통과), 바뀐 확인어(rain, night, sunset 태그)가 영상 시험에서 어떻게 나오는지 메인 시험 때 확인 필요.
