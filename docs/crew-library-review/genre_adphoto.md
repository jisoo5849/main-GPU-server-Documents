# genre_adphoto 검수 보고 (genre_style 중 ad_·photo_ 묶음)

## 수치
- 검수: 1,013항목 (ad_ 658, photo_ 355)
- 고침(update): 560, 지움(delete): 48, 추가(add): 814
- `check --facet genre_style` 오류 0 (경고 37: 영문 낀 ko, 'billboard'·'neon tube'·'cover' 같은 글자 유발 우려 말, 새 id 와 지운 옛 id 의 비슷함)
- 패치 파일: patches/genre_adphoto.jsonl (생성 스크립트·결정 파일은 work/genre_adphoto/)

## 문제 유형과 대표 예 (전 → 후)
1. **fits 때문에 메뉴에 한 번도 안 나오는 항목 (가장 큰 문제, 약 330건)**: 광고 요청은 제품 분야로 가는데 ad_ 항목 339개가 fits 에 product·any 가 없었다(character·food·vehicle 등만). 또 photo_ 일부는 분야 값(landscape·space·product)이 하나도 없었다.
   - 예: `fashion_accessories…` 류 character 전용 → fits 에 product 추가. 새 항목은 처음부터 `product` 를 넣었다.
2. **사람 낱말이 없어 사람 없는 요청에 모델이 끼는 항목 / 반대로 사람 낱말 때문에 빠지는 항목 (약 150건)**: crew_lib 은 en 의 사람 낱말(model·hair·family·kids·head 등)로만 거른다.
   - 예: `hair_dryer…` "shiny hair" → "blow dryer … warm airflow" (hair 가 걸려 제품 요청에서 늘 빠짐), `beer_golden_pour_foam` "foam head"(head 가 걸림) → "foam collar", `fashion_beach_resort_wear` 사람 낱말 없음 → "a model on a white sand beach".
3. **글자·간판·숫자·가격표를 부르는 말 (약 60건)**: signs, typography space, headline space, price tag, lettering, numerals, monogram, captions, callout, 번호·날짜 스탬프·타임스탬프.
   - 예: `cocktail_neon_bar_glow` "neon signs reflecting" → "neon tube reflections", `billboard_outdoor_giant_minimal` "huge headline space" → "a simple graphic that reads from a distance", `promo_anniversary_gold_numerals_sparkle` "giant gold numerals" → "giant gold foil balloons".
4. **상표·실제 지명 이름이 id·en·태그에 든 항목 (7건)**: polaroid, instagram, tiktok, airbnb, jeep, xerox, times square. id 는 바꿀 수 없어 옛 항목을 지우고 일반 이름으로 다시 넣거나(instagram→`social_feed_grid_muted_aesthetic`, tiktok→`social_short_form_split_screen`, airbnb→`travel_vacation_rental_cozy_loft`, times square→`billboard_city_square_screens_night`, xerox→`film_photocopy_toner_texture`), 같은 항목이 이미 있어 지웠다(polaroid → `instant_instant_frame` 이 이미 있음). jeep 은 "open safari vehicle" 로 고침.
5. **어색하거나 틀린 영문**: "dark dark outlines", "dark-outlined crowd arms", "friends dark outlines" 같은 silhouette 대체 오타 약 60건(이 프로젝트가 silhouette 을 모델이 잘못 읽어 쓰지 않는 규칙을 쓰다가 생긴 부작용), "bright bright", 관사 빠짐, "an artisan hands".
   - 예: `street_silhouette_backlit` "dark dark outlines of pedestrians" → "pedestrians outlined against a glowing low sun".
6. **칸이 틀린 항목 (지움 33건)**: photo_ 안의 카메라 앵글·구도·조명 패턴은 camera·composition·lighting 칸과 겹침 (버드아이, 더치 틸트, 로우앵글, 오버 더 숄더, 삼분할, 리딩 라인, 프레임 인 프레임, 웜 아이 뷰, 돌리 줌, 텔레포토 압축, 림라이트, 쇼트·버터플라이·클램쉘·루프·스플릿 조명, 촛불·헤드라이트·갓레이·창가광 등). 지우고 이유에 대응 id 를 적었다.
7. **중복·거의 중복 (지움 15건, 구분 수정 8건)**: product_clean_white_catalog·product_floating_levitating·product_ghost_mannequin_apparel 등은 ad_ 쪽과 같은 뜻이라 지움. 비슷한 쌍은 서로 다르게 고침(`hero_product_scatter_ingredients_motion` 방사 폭발, `retro_super8_home_movie_warm` 둥근 게이트 모서리, `product_car_studio_light_sweep` 흰 사이클로라마 등). 같은 한국어 이름 `아르데코 여행 포스터`, `일회용 카메라`, `대칭 파스텔 구도` 정리.
8. **사실 오류·존중**: `promo_lunar_new_year_red_lanterns` 는 홍등·매듭이 중국 춘절 관습이라 "춘절"로 바로잡고, 한국 설날·정월대보름·동지·김장은 새 항목으로 추가. `perfume_desert_dunes_golden`·`cosmetic_hanbang_oriental_luxury` 의 'oriental/exotic/오리엔탈' 은 타자화하는 말이라 앰버·한국 한방으로 고침. `tenebrism` ko 오자(템브리즘→테네브리즘).
9. **terms 가 너무 흔함 (약 60건)**: dust, rim light, steam, floating, balanced 등 → 그 항목만의 구절로 교체.

## 추가 (814)
- 광고 업종별 소묵음의 빈 곳 채움: 애니메이션 16, 가전 19, 자동차·이동 22, 음료 20(막걸리·식혜·유자차·달고나·바나나우유 등 한국 음료), 뷰티 30(토너패드·선스틱·슬리핑마스크·시카·LED 마스크 등), 친환경 9, 에디토리얼 12, 교육 14, 이벤트 12, 패션 잡화 20, 브랜드 필름 13, 금융 11, 음식 31(한식·분식·간식·세계 음식), 향 14, 가구 14, 주얼리 11, 키비주얼 11, 럭셔리 11, 미니멀 10, 공통 화풍 15, 옥외 형식 12, 반려동물 15, 의약·건강 12, 제품 히어로 20, 공익 12, 부동산 13, 외식 공간 16, 유통·매장 18(편의점·전통시장·드럭스토어 등 상표 없이), 시대 광고 16(80·90년대 한국 CF, 다방, 오락실, 자개장), 시즌·명절 20(설날·정월대보름·동지·김장·수능·블랙데이·라마단·디왈리), SNS 숏폼 15, 스포츠 18, 테크 15, 장난감 13, 여행 21(제주·부산·경주·한옥마을·세계 여행지), TV·홈쇼핑 11, 시계 9, 웨딩 12(폐백·전통 혼례·함·프리웨딩).
- 사진 장르·기법: 항공 12, 필름·현상·인화 양식 15(풀·푸시, 리스 프린트, 솔라리제이션, 포토그램, 앰브로타입, 스테레오 카드 등), 건축 14, 천체 12(월식·야광운·혜성·황도광 등), 영화 장면 장르 12, 다큐 12(해녀·시장·광부 등), 행사 사진 11(돌잔치·환갑·졸업·운동회), 파인아트 10, 음식 13, 조명 상황 14, 자연 14, 인물 12, 제품 12, 스포츠 10, 거리 11, 기법 15, 수중 10(+11번 보충 배치 30).
- 같은 틀에 낱말만 바꾼 대량 생성은 하지 않았다(각 항목이 서로 다른 장면·소재). 상표 없이 일반 명칭만 썼고 글자·간판·가격·숫자를 요구하는 말은 쓰지 않았다.

## 새 묶음
- 없음 (기존 ad_·photo_ 묶음만 사용).

## 정하지 못한 것 / 다음 사람에게
1. **crew_lib 사람 거름의 한계**: PEOPLE_EN_RE 가 'model', 'hair', 'head', 'family', 'kids', 'figure', 'player' 등 흔한 낱말을 사람으로 보는데, 제품 설명에도 자주 쓰여 항목이 빠지거나(hair dryer, foam head, action figure, model house, media player) 반대로 사람 낱말이 없는 인물 연출이 끼었다. 정규식에 'hair dryer', 'foam head', 'action figure', 'model house' 같은 예외를 두거나 항목에 `people: true` 같은 명시 칸을 두는 편이 낫다.
2. **crew_knowledge.json prefer**: 제품 장르의 prefer 에 `photo_product`, `ad_` 가 있어 ad_ 묶음이 메뉴를 거의 차지한다. 사진 쪽 `photo_food`, `photo_lighting`, `photo_technique` 는 제품 prefer 에 없고, 풍경 prefer 에 `photo_documentary`(한국 일상 기록)·`photo_underwater` 가 없다. 공간 prefer 에는 `ad_realestate`, `ad_restaurant`, `ad_retail`, `ad_furniture`, `ad_wedding` 같은 공간형 광고 묶음을 넣을 만하다.
3. **crew_intent.json 에 넣을 만한 말**: 장르 의도로 '광고 룩', '필름 사진', '다큐 사진', '증명사진', '스냅', '화보' / 한국 맥락 '설날', '추석', '김장', '장마', '벚꽃' 등이 있다. 대상 이름(제품)은 넣지 않는다는 규칙에 맞춰 장르·양식 말만.
4. **다른 칸으로 옮길 것**: 조명(lighting) 칸에 'photo_lit_*' 로 추가한 상황별 조명(냉장고 불빛, 손전등 아래 조명 등)은 lighting 칸에도 어울린다. 사진 기법의 카메라 효과(렌즈 웨킹, 스플릿 디옵터, 브레니저)는 camera 칸 후보.
5. **어두운 항목(dark)**: `adv_kv_thriller_rain_alley`, `pho_cin_heist_blueprint_table`, `pho_cin_folk_horror_field_dusk`, `pho_lit_underlit_flashlight_chin`, `pho_doc_refugee_camp_tents_dust` 에 `dark: true` 를 붙였다. 기존 `cinematic_horror_dim`·`cinematic_postapoc_dust`·`doc_war_aftermath_quiet` 는 그대로 둠.
6. 이름만 남은 위험: 글자가 생기기 쉬운 말('billboard' 2건, 'neon tube' 3건, 'cover' 1건, 'menu' 1건)은 시각 요소가 핵심이라 남겼다. 모델이 글자를 그리면 이 항목들을 우선 점검.
