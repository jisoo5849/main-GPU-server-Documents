# genre_film 검수 보고 (genre_style: film_*, tv_format, music_video, documentary, fantasy_world, scifi_world)

## 숫자
- 검수 781항목 → 고침 399, 지움 24, 추가 504 (적용 뒤 1,261항목). `check` 오류 0, 경고 78 (아래 참고).
- 패치: `patches/genre_film.jsonl`. 만든 스크립트: `work/genre_film/` (`build.py` 로 처음부터 다시 만들 수 있음).

## 문제 유형별
| 유형 | 수 | 대표 예 (전 → 후) |
|---|---:|---|
| 글자·간판·숫자를 부르는 말 | 약 45 | `neon vacancy sign` → `neon tubes`, `bold captions` → `quick zoom punches`, `title cards` → `iris-out transitions`, `chalkboard equations` → `chalk-dusted blackboard` |
| 'dark outline(s)' 치환 오류 (silhouette 를 기계적으로 바꾼 흔적) | 약 35 | `bat dark outlines` → `fluttering bats`, `dinosaur dark outlines` → `towering dinosaurs half hidden in mist` |
| 사람 거름이 못 잡는 사람 말 (사람 없는 요청 메뉴에 사람이 끼거나, 반대로 사람 장면에 character 없음) | 약 90 | `lone swordsman` → `lone sword-carrying figure`, 사람 없는 장면은 fits 에 landscape·space 보강 |
| fits 가 제작진 분야(제품·공간·풍경)와 맞지 않아 메뉴에 못 나옴 | 약 50 | 고가 철도·지하 도시 등 architecture·vehicle 만 있던 항목에 landscape 추가 |
| dark 오표시 | 7 | 버디 캅·케이퍼 4종·타임루프는 밝은 톤이라 dark 해제 (케이퍼·버디캅 요청에서 안 나왔음). 재난 가족·공성전·밀리터리 SF·핵겨울·함대 전투·외계 침공은 dark 추가 |
| 같은 내용·같은 이름 중복 (다른 묶음과) | 24 | 솔라펑크·달 기지·스팀펑크·메카 격납고·사이버펑크 비 도시·초원 유목 서사 등 삭제, 나머지는 서로 다르게 고침 (엘프 도시·해저 왕국·궁궐 등) |
| 상표·작품 이름 | 8 | `Candy Land`(보드게임) → 사탕 왕국, `waterworld`·`spice`·스타게이트·크툴루·로드 워리어 등 제목성 말 제거 |
| ko 문제 | 약 15 | `사이드 코미디`(없는 말) → 크린지 코미디, `힙 코미디` → 슬래커 코미디, MV 28개는 'MV' → '뮤직비디오' |
| 확인어(terms) 너무 흔함/부정확 | 약 10 | `ruined`, `surface`, `joints`, `practical` 등 교체 |
| 태그 | 약 20 | 다크 아카데미아 정확 태그가 호러를 열던 것 분리, 호러 대표 항목 12개에 `호러` 태그 |

## 추가 504항목 (적용 뒤 묶음 구성)
- 기존 묶음 보강: 액션 26, 모험 28, 코미디 27, 범죄 29(밝은 케이퍼 제외 모두 dark), 재난 22(전부 dark), 드라마 34, 판타지 35, 호러 34(전부 dark, id 에 horror), 뮤지컬 28, 미스터리 29, SF 30, 서바이벌 8, 로맨스 14, 느와르 10, 스릴러 10, 전쟁 10, 서부극 10.
- TV 포맷 21, 뮤직비디오 13, 다큐 16, 판타지 세계 16, SF 세계 10.
- 새 묶음 `film_era` 30, `film_world` 38.

## 새 묶음과 이유
- `film_era`: 시대별 영화 양식 (무성·3색 컬러·50년대 와이드스크린·70mm·80~2010년대·한국 영화 시대). 극 중 시대는 film_drama, 생활 디자인의 시대 감성은 era_modern_decades 와 겹치지 않게 '그 시대에 찍힌 화면'(필름·조명·카메라 관습)만 담았다.
- `film_world`: 국가·지역 영화 양식 (대만·이란·중국 세대·홍콩·일본·한국 독립·인도·남미·북유럽·동유럽·중동·아프리카 …). 감독 이름 없이 화면 관습으로 썼고, 희화화 없이 장소·조명·카메라로만 묘사.

## 내가 정하지 못한 것
- `crew_knowledge.json` 의 genre `prefer`: 풍경 분야에 `film_`, `fantasy_world`, `scifi_world` 가 이미 있어 새 `film_era`·`film_world` 는 `film_` 접두로 자동 포함된다. 제품 분야에는 `film_` 이 없다(`tv_`·`music_video`도 없음) — 광고 요청에서 영화·뮤비·TV 양식이 필요하면 `film_era`, `music_video`, `tv_format` 추가를 검토.
- 의도 사전 `crew_intent.json` 에 넣을 만한 말: 한국 사용자가 쓰는 `뮤직비디오`는 이미 있으나 `영화처럼`(cinematic)의 하위로 `무성영화`, `서부극`, `재난 영화`, `좀비`, `느와르`(있음), `다큐`(있음), `시트콤`, `예능`, `드라마`, `1900~2000년대 영화`. 특히 `좀비`(dark_words 에는 있음)와 `재난`·`서바이벌`은 의도 사전에 없다.
- 경고 78개 중 다수는 새 묶음 안내(50여 건)이고, 나머지는 banners·map·cover·manuscript(꼭 필요해서 둠)와 ko 의 영문 약어(MV·EDM·K팝·Y2K·ASMR).
- 다른 칸으로 옮길 것: `film_drama.eighties_neon_drama`, `slice_of_life_anime_school`은 era/animation 쪽 성격이 강하나 내용상 둠. 다른 패치(genre_art 등)와 ko·id·en 충돌은 `xcheck.py` 로 확인해 0.
- `genre_style.none`, `genre_style.sci_fi` 는 건드리지 않았다.
