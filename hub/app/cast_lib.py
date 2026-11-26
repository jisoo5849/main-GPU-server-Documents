"""인물 라이브러리 (11-19): 이야기에 이미 정해진 인물의 외형·목소리·기준 그림·그림체.

11-19 메인 첫 시험에서 스튜디오가 만든 대사 영상은 실사였고 인물도 손으로 만든 니키·푸토르와 달랐다. 요청 글에 외형을 길게 써도
인물 그림은 글에서 새로 그려져 같은 얼굴이 되지 않는다. 그래서 정해진 인물은 여기에 두고:
- 요청 글에 이름이 나오면 구성안을 쓰는 모델에게 그 인물을 알려 주고(외형·그림체),
- 구성안이 나온 뒤 설정집의 외형·목소리를 이 값으로 덮어쓰고(모델이 다르게 바꿔 쓰지 못하게),
- 기준 그림(ref)이 있으면 말하는 얼굴·듣는 얼굴은 그 그림을 편집해 만든다 (감독 `_dlg_casts`).

순수 함수만 둔다(파일 읽기 외에 DB·네트워크 없음). 라이브러리 파일은 `cast_library.json` (STUDIO_CAST_FILE 로 바꿀 수 있다).
"""
import difflib
import json
import logging
import os
import re

from .config import settings

log = logging.getLogger("mgs.cast")

LIB_FILE = os.path.join(os.path.dirname(__file__), "cast_library.json")
REQUIRED = ("id", "names", "look")
# 화풍 열쇠 (11-20 장면 잇기 설계 4절, 11-24 2차): 한 영화의 인물은 모두 한 화풍이어야 해서, 화풍 글을 열쇠 하나로 줄여 인물마다 그 화풍의
# 기준 그림을 고르고, 정지 그림의 화풍 문장·검사 문장·기준 그림 변환 지시문을 열쇠별로 고른다. STYLE_KEYS 는 라이브러리 refs 칸 이름이다.
# custom(그 밖에 분명히 말한 화풍: 수채·클레이·픽셀 …)은 화풍 글마다 달라 라이브러리에 저장하지 않는다 (화풍 원문을 그대로 쓴다)
STYLE_KEYS = ("photo", "semi", "anime", "toon3d")
CUSTOM_KEY = "custom"
# 화풍 말 (11-24 화풍 일반화). 한글이 바로 붙는 '2D로'·'2.5D 느낌' 도 잡게 낱말 경계(\b) 대신 앞뒤 글자를 본다
_SEMI_RE = re.compile(r"semi\s*-?\s*(?:real|photo)|반\s*실사|(?<![0-9.])2\.5\s*-?\s*d(?![a-z])", re.I)
_2D = r"(?<![0-9a-z.])2\s*-?\s*d(?![a-z])"
_3D = r"(?<![0-9a-z.])3\s*-?\s*d(?![a-z])"
_2D_RE, _3D_RE = re.compile(_2D, re.I), re.compile(_3D, re.I)
# 애니 말: 이 말만으로 애니 열쇠 (anime·cel·2D·만화·웹툰·지브리). 영어 manga·manhwa·webtoon 도 (11-24 3차 검토: 구성안은 화풍을 영어로 써서
# 'Korean webtoon style digital illustration' 이 custom 으로 읽혀 애니 문장·애니 정밀 판 부정 글·애니 검사 줄이 모두 빠졌다)
_ANIME_RE = re.compile(r"anime|manga|manhwa|webtoon|\bcel(?:\b|[\s-]*shad)|" + _2D + r"|만화|웹툰|망가|지브리|ghibli", re.I)
# 2D 말고 애니를 직접 말한 말 (5차 검토: '2D pixel art animation'·'soft watercolor-style 2D animation' 은 2D 하나로 애니가 되어 cel 셰이딩 문장이
# 원문의 픽셀·수채와 부딪혔다 → 2D 하나뿐이고 재료 말이 함께면 그 재료 화풍(custom))
_ANIME_WORD_RE = re.compile(r"anime|manga|manhwa|webtoon|\bcel(?:\b|[\s-]*shad)|만화|웹툰|망가|지브리|ghibli", re.I)
# '움직이는 그림' 말 (animation·animated·cartoon·카툰·애니·애니메이션): 홀로 오면 애니(1차와 같다), 다른 화풍 말이 붙으면 그 화풍이다
# ('stop-motion animation'·'클레이 애니' 를 애니로 읽으면 2D 애니 문장이 점토 원문과 부딪힌다, 11-24 2차). 한국어 '애니'는 애니메이션의 준말로도 쓴다
_MOVING_RE = re.compile(r"anim(?:ation|ated)|cartoon|카툰|애니", re.I)
# 3D 애니 말 (toon3d): 3D animated·Pixar-like·Disney 3D(디즈니 3D)·CG animation·3D cartoon·stylized 3D·3D 애니·픽사.
# 4차 검토: 한국어 'CG 애니메이션'·'CGI 애니'와 3D 스튜디오 드림웍스를 2D 애니로 읽어, 구성안이 맞게 쓴 3D 화풍을 2D 애니 글로 덮었다
_TOON3D_RE = re.compile(r"pixar|픽사|드림웍스|dreamworks|(?:disney|디즈니)[\s-]*3\s*-?\s*d|(?<![a-z])cgi?[\s-]*(?:animat|애니)|" + _3D
                        + r"[\s-]*(?:animat|cartoon|애니)|styli[sz]ed[\s-]*3\s*-?\s*d", re.I)
# 셀 셰이딩 만화책 화풍 (11-24 2차 검토): 'cel' 이 애니 말이지만 'cel-shaded comic' 은 코믹 화풍(custom)이다 — 애니 판정에서 뺀다
_CEL_COMIC = r"cel[\s-]*shad(?:ed|ing)?[\s-]+comics?"
_CEL_COMIC_RE = re.compile(_CEL_COMIC, re.I)
_PHOTO_RE = re.compile(r"(?<!반)(?<!반 )실사|포토\s*리얼|photo\s*-?\s*real|live[\s-]*action|photograph|realistic\s+photo|사진\s*(?:처럼|같)", re.I)
# 사실적인 영화 말 (11-24 3차 검토): 실사 요청을 따른 구성안 화풍 'hyper-realistic cinematic look'·'cinematic realism'·'lifelike cinematic
# style'·'naturalistic film look' 이 custom 으로 읽혀 실사 영화에 '사진 아님'·'그린 그림' 문장이 붙고 실사 변환·실사 검사가 빠졌다.
# 다른 화풍 말과 함께면 그 화풍의 꾸밈말이다 ('hyper-realistic oil painting' 은 유화, 'realistic lighting' 은 화풍 말이 아니다).
# 요청 글(_REQ_KEY)과 화풍 글(_REAL_RE)이 이 한 목록을 쓴다 (4차 검토: 목록이 둘이라 'realistic style' 이 요청에서는 실사, 화풍 글에서는 custom
# 이었고, '사실적인 느낌으로'·'리얼하게'·'실제 사람처럼'·'A realistic video of …'·'real actors' 를 몰라 실사 요청이 반실사가 됐다)
_REALISM = (r"hyper[\s-]*real\w*|ultra[\s-]*real\w*|(?:cinematic|photographic|film|documentary|gritty|naturalistic)\s+realism"
            r"|realistic[\s,]+(?:cinemat\w*|film|movie|footage|look|live|drama|documentary|style|video|scene|clip|short)|cinematic\s+realistic"
            r"|life-?like|true[\s-]*to[\s-]*life|naturalistic|real-life|real\s+life\s+(?:look|style|version|film|movie|video)|real\s+actors?"
            r"|played\s+by\s+(?:real\s+)?actors|사실적(?:인|으로)?|리얼(?:한|하게|하고|리즘|리티|\s*(?:영상|영화|느낌|스타일|버전))"
            # 5차 검토: '현실적인 느낌으로'·'실제 사람이 나오는'·'real people'·'like a real movie' 를 몰라 실사 요청이 라이브러리 반실사가 됐다
            r"|현실적(?:인|으로)?|현실감(?:\s*있(?:게|는))?|real\s+people|like\s+a\s+real\s+(?:movie|film)"
            r"|(?:실제|진짜)\s*(?:사람|배우|인물)(?:들)?(?:이|가)?\s*(?:연기|출연|등장|나오는|나온|나와|나오)|실제\s*(?:사람|배우|인물)|진짜\s*(?:사람|배우)")
_REAL_RE = re.compile(_REALISM, re.I)
# 사진 말 없이 쓴 실사 영화 화풍 글 (4차 검토: 'cinematic film look with dramatic lighting'·'35mm film look'·'documentary style handheld look'·
# 'realistic, cinematic film look' 이 custom 으로 읽혀 라이브러리 화풍을 잃고 '사진 아님' 문장과 부딪혔다). 그림 말(그림·삽화·애니메이션 …)이
# 함께 없을 때만 실사다 ('fantasy concept art with a cinematic look' 은 그림이다)
_FILM_LOOK_RE = re.compile(r"\bcinematic\s+(?:\w+\s+){0,2}?look\b|\b(?:film|movie)\s+look\b|\b35\s*mm\b|\bdocumentary[\s-]+(?:style|look|footage)\b"
                           r"|\brealistic\s*,", re.I)
_ART_FORM_RE = re.compile(r"\b(?:concept\s+art|illustrat\w*|drawings?|drawn|paint\w*|sketch\w*|art\s*style|artwork|render\w*|anim\w*|cartoon\w*"
                          r"|comic\w*|manga|anime|storybook|picture\s+book)\b", re.I)
# 반실사 쪽 그림 말 (painterly·digital painting·painted). 수채·유화는 2차부터 custom 말이다
_PAINT_RE = re.compile(r"painterly|digital\s+painting|\bpainted\b", re.I)
# custom 말 (11-24 2차): 세 열쇠(반실사·애니·실사)와 3D 애니 밖의 분명한 화풍. (정규식, 영어 화풍 이름). 화풍 글(구성안 bible.style)을 읽는
# style_key 가 쓴다: 색 이름 '파스텔 톤'·'스테인드글라스 창문' 같은 말은 화풍이 아니라 거른다. 요청 글은 화풍으로 쓴 말만 센다 (_REQ_CUSTOM).
# 3차 검토: 목탄·펜화·아크릴·모자이크·종이접기·실루엣·흑백 무성영화, 목판화·동판화·전래동화(앞 글자가 붙은 판화·동화)도
_KO_LOOK = r"\s*(?:화|그림|풍|체|느낌|스타일|일러스트|드로잉|스케치|선화|터치|질감|아트|(?:로|으로)\s*(?:그린|그려|칠한|칠해))"
_CUSTOM_WORDS = (
    (r"watercolou?r|(?<![가-힣])수채", "watercolour painting"),
    (r"gouache|과슈", "gouache painting"),
    (r"oil[\s-]*paint(?:ing|ed)|(?<![가-힣])유화", "oil painting"),
    (r"acrylic|아크릴", "acrylic painting"),
    (r"pastel(?![\s-]*(?:tones?|toned|colou?rs?|colou?red|shades?|palette|hues?))|파스텔" + _KO_LOOK, "soft pastel drawing"),
    (r"crayon|(?:크레용|크레파스)" + _KO_LOOK, "crayon drawing"),
    (r"colou?red[\s-]*pencils?|색연필" + _KO_LOOK, "coloured pencil drawing"),
    (r"pencil[\s-]*(?:sketch|drawing)|(?<!색)연필" + _KO_LOOK, "pencil sketch"),
    (r"charcoal|목탄", "charcoal drawing"),
    (r"pen[\s-]*and[\s-]*ink|ink[\s-]*drawing|펜화|펜\s*드로잉", "pen and ink drawing"),
    (r"ink[\s-]*wash|sumi[\s-]*e(?![a-z])|(?<![가-힣])수묵|담채", "ink wash painting"),
    (r"woodblock|wood[\s-]*cut|lino[\s-]*cut|etching|ukiyo[\s-]*e(?![a-z])|(?<![가-힣])(?:목|동|석|고무)?판화|우키요에", "woodblock print"),
    (r"story[\s-]*book|picture[\s-]*book|fairy[\s-]*tale|(?<![가-힣])(?:전래\s*|창작\s*)?동화|그림\s*책|삽화", "storybook illustration"),
    (r"claymation|(?<![a-z])clay(?![a-z])|클레이|점토", "claymation"),
    (r"stop[\s-]*motion|스톱\s*모션", "stop-motion"),
    (r"paper[\s-]*cut|cut[\s-]*out\s+paper|종이\s*(?:공예|오리기|컷|인형|아트|질감|풍|느낌|스타일)", "paper cut-out"),
    (r"origami|종이\s*접기", "origami paper craft"),
    (r"(?<![a-z])felt(?:ed)?[\s-]*(?:puppets?|craft|texture|animation|style|look|dolls?|figures?)|needle[\s-]*felt|펠트", "felt craft"),
    (r"pixel[\s-]*art|픽셀|(?<![0-9])(?:8|16)[\s-]*(?:bit|비트)|도트\s*(?:그래픽|그림|아트|풍|느낌|스타일)", "pixel art"),
    (r"mosaic|모자이크", "mosaic"),
    (r"silhouette|실루엣", "silhouette"),
    (r"(?<![a-z])lego(?![a-z])|레고|toy[\s-]*bricks?|brick[\s-]*(?:built|figures?|toys?)", "toy brick figures"),
    (_CEL_COMIC + r"|comic[\s-]*book|comic[\s-]*(?:style|art|strip)|코믹\s*(?:스|북|그림|화풍|풍|체|느낌|스타일|아트)", "comic book art"),
    (r"graphic[\s-]*novel|그래픽\s*노블", "graphic novel art"),
    (r"(?<![a-z])noir(?![a-z])|(?<!르)누아르", "film noir"),      # 르누아르(Renoir)는 화가 이름이다 (fixA)
    (r"black[\s-]*and[\s-]*white|monochrome|흑백|silent\s+(?:film|movie)|무성\s*영화", "black-and-white film"),
    (r"stained[\s-]*glass(?![\s-]*(?:windows?|panes?|doors?))|스테인드\s*글라스(?!\s*(?:창|문|유리))", "stained glass"),
    (r"low[\s-]*poly|로우\s*폴리", "low poly"),
)
_CUSTOM_RE = re.compile("|".join(f"(?:{p})" for p, _n in _CUSTOM_WORDS), re.I)
_CUSTOM_BY_NAME = {n: re.compile(p, re.I) for p, n in _CUSTOM_WORDS}
# 재료·기법 custom 말 (4차 검토): '3D 클레이 애니메이션'·'로우폴리 3D 애니메이션'·'3D felt puppet animation' 을 3D 애니(toon3d)로 읽어 구성안이 맞게
# 쓴 점토·로우폴리 글을 3D 애니 기본 글('smooth soft shading …')로 덮었다. 이 말이 함께 있으면 3D 가 있어도 custom(원문)이다 (동화·누아르처럼
# 분위기만 꾸미는 말은 뺀다: 'stylized 3D animated film with a storybook feel' 은 3D 애니)
_MATERIAL_RE = re.compile("|".join(f"(?:{p})" for p, n in _CUSTOM_WORDS
                                   if n not in ("storybook illustration", "comic book art", "graphic novel art", "film noir",
                                                "black-and-white film", "silhouette")), re.I)
# ── 요청 글이 말한 화풍 (11-24 2차·3차 검토) ──
# 요청 글의 화풍 말을 찾아 그 자리의 꼴로 '화풍으로 쓴 말'인지 가린다 (_req_scan):
# - 물건·활동·관용 표현이면 화풍이 아니다: '만화를 그리는'·'애니메이션을 보는'·'픽사 영화를 보는'·'애니메이션 감독'·'만화 같은 하루'·'고양이 애니와
#   노는'·'펠트 인형을 만드는'·'클레이로 인형을 만드는', 'watches cartoons'·'an animated conversation'·'takes a photograph of'·'drawing anime
#   characters'·'a clay pot' (3차 검토: 열쇠 말을 글 전체에서 찾아 이런 요청이 애니·실사·3D 요청이 되어 라이브러리 화풍을 잃었다)
# - 열쇠 말(반실사·애니·3D 애니·실사, _REQ_KEY)은 그 밖에는 화풍이다 ('니키와 푸토르가 대화하는 애니메이션', '실사 영화처럼 대화')
# - custom 말(수채·클레이 …)과 '사진'·'손그림'은 화풍 꼴일 때만: 뒤에 표지(풍·화풍·느낌·스타일·그림체·기법·애니메이션·영상 …, style·look·art·
#   animation·film …)가 오거나, 'X로 그린'·'X로 …'(재료로 무엇을 만드는 말이 아니면)·'X처럼 …'·'X 같은 영상' 으로 영화 전체를 꾸미거나, 앞에
#   'in·as·like·in the style of·painted in' 이 오거나, 'a watercolor painting of …' (3차 검토: 표지를 말 바로 뒤에서만 찾아 '수채화로 니키와
#   푸토르가 숲을 걷는 영상'·'수채화처럼 …'·'in the style of a watercolor painting'·'as an oil painting' 을 놓쳤다)
# 아는 말이 없는데 화풍 표지가 붙은 말('반 고흐 스타일로'·'사이버펑크 느낌으로'·'아르데코 풍으로'·'Van Gogh style'·'디즈니처럼'·'흑백 무성영화처럼'·
# '따뜻한 화풍으로')은 화풍일 수도 있다(maybe): 구성안 모델에게 판단을 맡기고, 모델이 쓴 화풍(실사 말고)을 덮지 않는다 (said_level)
# (말, 영어 화풍 이름 — 이름은 _CUSTOM_WORDS 와 같다)
_REQ_CUSTOM = (
    (r"watercolou?r|(?<![가-힣])수채", "watercolour painting"),
    (r"gouache|과슈", "gouache painting"),
    (r"oil[\s-]*paint(?:ing|ed)?|(?<![가-힣])유화", "oil painting"),
    (r"acrylic|아크릴", "acrylic painting"),
    (r"pastel|파스텔", "soft pastel drawing"),
    (r"crayons?|크레용|크레파스", "crayon drawing"),
    (r"colou?red[\s-]*pencils?|색연필", "coloured pencil drawing"),
    (r"pencil|(?<!색)연필", "pencil sketch"),
    (r"charcoal|목탄", "charcoal drawing"),
    (r"pen[\s-]*and[\s-]*ink|ink[\s-]*drawing|펜화|펜\s*드로잉", "pen and ink drawing"),
    (r"ink[\s-]*wash|sumi[\s-]*e(?![a-z])|(?<![가-힣])수묵|담채", "ink wash painting"),
    (r"woodblock|wood[\s-]*cut|lino[\s-]*cut|etching|ukiyo[\s-]*e(?![a-z])|(?<![가-힣])(?:목|동|석|고무)?판화|우키요에", "woodblock print"),
    (r"story[\s-]*book|picture[\s-]*book|fairy[\s-]*tale|(?<![가-힣])(?:전래\s*|창작\s*)?동화|그림\s*책|삽화", "storybook illustration"),
    (r"claymation|(?<![a-z])clay(?![a-z])|클레이|점토", "claymation"),
    (r"stop[\s-]*motion|스톱\s*모션", "stop-motion"),
    (r"paper[\s-]*cut(?:[\s-]*outs?)?|cut[\s-]*out\s+paper|종이(?:\s*오리기|\s*공예)?", "paper cut-out"),
    (r"origami|종이\s*접기", "origami paper craft"),
    (r"(?<![a-z])felt(?:ed)?(?![a-z])|needle[\s-]*felt|펠트", "felt craft"),
    (r"pixel|픽셀|(?<![0-9])(?:8|16)[\s-]*(?:bit|비트)|도트", "pixel art"),
    (r"mosaic|모자이크", "mosaic"),
    (r"silhouette|실루엣", "silhouette"),
    (r"(?<![a-z])lego(?![a-z])|레고|toy[\s-]*bricks?|brick[\s-]*(?:built|figures?|toys?)", "toy brick figures"),
    (_CEL_COMIC + r"|comics?(?:[\s-]*(?:book|strip))?|코믹스(?!토리)|코믹(?:\s*북)?", "comic book art"),
    (r"graphic[\s-]*novel|그래픽\s*노블", "graphic novel art"),
    (r"(?<![a-z])noir(?![a-z])|(?<!르)누아르", "film noir"),      # 르누아르(Renoir)는 화가 이름이다 (fixA)
    (r"black[\s-]*and[\s-]*white|monochrome|흑백|silent\s+(?:film|movie)|무성\s*영화", "black-and-white film"),
    (r"stained[\s-]*glass|스테인드\s*글라스", "stained glass"),
    (r"low[\s-]*poly|로우\s*폴리", "low poly"),
)
_REQ_CUSTOM_RE = re.compile("|".join(f"(?:{p})" for p, _n in _REQ_CUSTOM), re.I)
# 열쇠 말 (요청 글에서는 물건·활동·관용 표현만 빼고 화풍이다)
_REQ_KEY = re.compile(
    _SEMI_RE.pattern
    + r"|anime|manga|manhwa|webtoon|애니(?:메이션)?|만화|웹툰|망가|지브리|ghibli|cartoons?|카툰|anim(?:ation|ated)|cel[\s-]*shad\w*|" + _2D
    + r"|pixar|픽사|드림웍스|dreamworks|(?:disney|디즈니)[\s-]*3\s*-?\s*d|(?<![a-z])cgi?(?![a-z])|styli[sz]ed[\s-]*3\s*-?\s*d|" + _3D
    + r"|(?<!반)(?<!반 )실사|포토\s*리얼|photo[\s-]*real\w*|live[\s-]*action|photographic(?!\s+memory)|" + _REALISM, re.I)
# 2D 인지 3D 인지 모호한 스튜디오 + '움직이는 그림' 말 ('디즈니 애니메이션처럼'): 화풍일 수도 있는 말(maybe)로 두고 2D·3D 는 구성안 모델이 정한다
# (4차 검토: 2D 애니로 고정되어 구성안의 3D 화풍을 덮었다)
_AMBIG_BRAND_BEFORE = re.compile(r"(?:디즈니|disney)(?:['’]s)?[\s-]*$", re.I)
_MOVING_WORD = re.compile(r"애니(?:메이션)?|anim(?:ation|ated)|cartoons?|카툰", re.I)
# 화풍 꼴일 때만 화풍인 열쇠 쪽 말 ('사진을 찍는'·'사진 같은 풍경'·'a photograph of'·'a hand-drawn map' 은 화풍이 아니다)
_REQ_MARKED = re.compile(r"사진|photographs?|손\s*그림|hand[\s-]*drawn", re.I)
# 그 자체로 화풍인 말 (표지가 없어도 화풍 요청)
_REQ_SURE = re.compile(r"claymation|stop[\s-]*motion|스톱\s*모션|pixel[\s-]*art|픽셀\s*아트|ukiyo[\s-]*e(?![a-z])|우키요에|low[\s-]*poly"
                       r"|로우\s*폴리|sumi[\s-]*e(?![a-z])|" + _CEL_COMIC, re.I)
# 받침 ㄴ 으로 끝나는 꾸밈말 ('예쁜'·'고운'·'멋진'·'따뜻한'·'몽환적인') — '수채화처럼 예쁜 영상' 의 '예쁜'
_ADJ_N = "[" + "".join(chr(0xAC00 + k) for k in range(11172) if k % 28 == 4) + "]"
_KO_MEDIUM = r"(?:담채화|담채|그래픽|화|그림|물감|스케치|드로잉|북|책|인형(?!극)|게임|종이)"
_KO_PURE = (r"(?:풍(?!경|선|차|부|성|족|요|습|속|파|랑|채|미|토|년|월|기|향|수|금|악|자|물|문|력|화)|화풍|그림체|그림풍|작화|느낌|스타일|질감|"
            r"터치|색감|기법|인형극|분위기)")
_KO_WORK = r"(?:애니메이션|애니|영상|영화|작품|화면|버전|아트|일러스트|삽화|월드)"
_KO_TARGET = r"(?:영상|화면|그림체|그림풍|그림|애니메이션|애니|영화|작품|장면|분위기|느낌|스타일|화풍|질감|색감|컷|일러스트|삽화)"
_KO_IMPER = r"(?:[가-힣]*\s*(?:줘|주세요|주십시오|달라|봐))"
_KO_OBJ_VERB = (r"(?:보|봐|본|볼|봤|봄|읽|그리|그려|그린|그릴|그렸|감상|만들|만드|만든|만듭|좋아|틀|튼|찍|켜|켠|배우|배운|모으|모은|모아|사|산|샀|"
                r"선물|쓰|쓴|쓸|썼|들|든|쥐|쥔|펼|넘기|넘긴|빚|접|하|한|할|했|해|듣|즐기|즐긴|구경|소개|설명)")
_KO_OBJ_NOUN = (r"(?:방|가게|카페|감독|작가|채널|동아리|수업|학원|회사|캐릭터|피규어|장난감|굿즈|포스터|잡지|프린터|안경|카메라|펜|도구|키트|대회|"
                r"전시|축제|레이터|숙제|공부|덕후|팬|주제가|노래|OST|ost|음악|사운드트랙|촬영장|오디션|모델링|퍼즐|주인공|이야기|얘기)")
_KO_IDIOM_NOUN = (r"(?:하루|일상|모험|이야기|인생|순간|사건|상황|꿈|풍경|세상|기분|사랑|우정|시간|날|여행|광경|일|얘기|목소리|표정|웃음|미소|눈빛|"
                  r"추억|기억)")
_KO_NOUNS = r"(?:\s*(?:" + _KO_MEDIUM + "|" + _KO_WORK + "|" + _KO_OBJ_NOUN + r")){0,3}"
# 물건·활동: 'X를 보는'·'X 영화를 보는'(명령 '만들어 줘' 는 아니다)·'X가 '·'X에서'·'X 감독'
_KO_OBJ_AT = re.compile(_KO_NOUNS + r"\s*(?:(?:을|를)\s*(?:[가-힣]+\s+)?" + _KO_OBJ_VERB + r"(?!" + _KO_IMPER + r")"
                        r"|(?:이|가|은|는|도|만|에|에서|에게|한테)(?:\s+(?!좋|괜찮|어울|나을|낫|멋질)|$))")
# '고양이 애니와 노는' (이름·함께하는 것). 뒤가 화풍 말이면('수채화와 동화풍') 이어진 화풍이라 따로 본다 (_req_object)
_KO_WITH_AT = re.compile(_KO_NOUNS + r"\s*(?:와|과|랑|이랑|하고)\s+")
_KO_OBJ_NOUN_AT = re.compile(r"(?:\s*(?:" + _KO_MEDIUM + "|" + _KO_WORK + r")){0,2}\s*" + _KO_OBJ_NOUN
                             + r"(?![가-힣]*\s*(?:(?:같은|같이|처럼)\s*)?(?:" + _KO_PURE + "|" + _KO_TARGET + "))")
_KO_IDIOM_AT = re.compile(r"(?:\s*" + _KO_MEDIUM + r")?\s*(?:같은|같이|처럼)\s*(?:[가-힣]*" + _ADJ_N + r"\s+)?" + _KO_IDIOM_NOUN
                          + r"(?=\s|$|[,.!?]|(?:이|가|을|를|은|는|의|로|으로|에|도|만)(?![가-힣]))(?![가-힣]*\s*" + _KO_TARGET + ")")
# 화풍 꼴 (말 + 재료 말 + 표지)
_KO_STYLE_AT = re.compile(
    r"(?:\s*" + _KO_MEDIUM + r"){0,2}\s*(?:" + _KO_PURE
    + r"|" + _KO_WORK + r"(?=\s*(?:으로|로|처럼|같이|같은|풍|느낌|스타일|$|[,.!?]|(?:을|를)\s*(?:만들어|그려|보여)" + _KO_IMPER + r"))"
    + r"|(?:으로|로)\s*(?:그린|그려|칠한|칠해|표현|만든|만들어|바꿔|꾸며|꾸민|찍은|찍어)"
    + r"|(?:으로|로)(?=\s*(?:$|[,.!?]))"
    + r"|(?:처럼|같이)\s*(?:그린|그려|찍|만든|만들|보이|표현|꾸민|꾸며)"
    + r"|(?:처럼|같이)\s*(?:생생|선명|실감\s*나|사실적|리얼)(?:하게|하고|히|게)(?=\s)"
    + r"|(?:처럼|같이)(?=\s*(?:$|[,.!?]))"
    + r"|(?:처럼|같이|같은)\s*(?:[가-힣]*" + _ADJ_N + r"\s+)?" + _KO_TARGET + r")", re.I)
# 'X로 …' 로 영화 전체를 꾸밈 (뒤가 'Y를 만드는·쓰는·그린다' 처럼 재료로 무엇을 만드는 말이면 아니다: '클레이로 인형을 만드는'·'연필로 편지를 쓰는')
_KO_RO_AT = re.compile(r"(?:\s*" + _KO_MEDIUM + r"){0,2}\s*(?:으로|로)\s+(?P<rest>[^,.!?]*)")
_KO_MATERIAL = re.compile(r"(?:을|를)\s*(?:만들|만드|빚|접|쓰|쓴|그리|그린다|그렸|칠하|색칠|조각|깎|꾸미)(?!" + _KO_IMPER + ")")
# 'X처럼 니키와 …' 처럼 영화 전체를 꾸밈 ('사진처럼 선명한 꿈' 의 꾸밈말은 아니다)
_KO_LIKE_AT = re.compile(r"(?:\s*" + _KO_MEDIUM + r"){0,2}\s*(?:처럼|같이)\s+[가-힣]+(?:와|과|가|이|를|을|의|는|은)(?:\s|$)")
# 'X로' 가 인물이 쓰는 도구인 활동 (4차 검토: '푸토르가 연필로 약초를 스케치하며'·'니키가 크레파스로 낙서하며'·'니키가 레고로 성을 쌓으며'·
# '니키가 클레이로 놀면서'·'크레용으로 그림 그리며' 를 영화 화풍 요청으로 읽어 승인된 라이브러리 화풍을 잃었다). 영화 전체를 꾸미는 꼴은 화풍이다:
# 'X로 만들어 줘'·'X로 된 영상'·문장 끝 'X로'·'X로 니키와 푸토르가 …'(뒤 글이 주어로 시작한다)·'X로 그린 영상'. 뒤 글의 첫 동작이 도구를 쓰는
# 말(스케치·낙서·그리·쌓·놀·만들 …)이면 활동이다 (_ko_instrument). 5차 검토: 'X로' 앞에 주어만 있으면 활동으로 보아 '니키와 푸토르가 수채화로
# 대화하는 영상'·'베스와 제임스가 실사로 나오는 영상' 같은 가장 흔한 화풍 요청이 통째로 사라졌다 → 주어가 앞에 있어도 첫 동작을 본다
_KO_RO_ANY = re.compile(r"(?:\s*" + _KO_MEDIUM + r"){0,2}\s*(?:으로|로)(?![가-힣])\s*(?P<rest>[^,.!?]*)")
_KO_STYLE_IMPER = re.compile(r"(?:[가-힣]+게\s+)?(?:만들어|그려|바꿔|표현해|꾸며|보여|담아|제작해|그려내)\s*" + _KO_IMPER)
# 도구를 쓰는 동작 ('스케치하며'·'낙서하며'·'그림 그리며'·'그려 주는'·'쌓으며'·'놀면서'·'만드는'·'쓰는') — 그 말로 무엇을 하는 활동이다
_KO_ACT_VERB = re.compile(r"스케치|낙서|색칠|조각|그리(?:며|고|는|면서|다가)|그림\s*그리|그려\s*(?:주|서|가며)|칠하(?:며|고|는|면서)|쌓"
                          r"|(?<!뛰)놀(?:면서|며|고|아|이)|(?<!뛰)노는|(?<!뛰)논다|가지고\s*놀|갖고\s*놀|만들(?:며|고|면서|기|다가)|만드는"
                          r"|빚(?:으며|고|는)|접(?:으며|고|는)|깎|꾸미(?:며|고|는)|오리(?:며|고|는)|붙이|찍(?:으며|고|는)|쓰(?:는|며|고|면서)")
# 'X로 그린·만든 …' (그 말로 그린 것): 뒤에 물건 말이 목적어로 이어질 때만 물건이다 ('클레이로 만든 인형을 들고'). 끝나거나('과슈로 그린')·
# '듯'·'느낌'·영상 말·'…처럼' 이 오면 화풍 꼴이다 ('크레파스로 그린 듯'·'수채화로 그린 영상'·'클레이로 만든 인형처럼')
_KO_MADE_VERB = re.compile(r"그린|칠한|만든|빚은|접은|꾸민|찍은|그려진|만들어진|표현한|오린|쓴")
# 영상 말 뒤가 '을/를 + 동작'이면 그린 것을 보여 주는 물건이다 ('수채화로 그린 그림을 보여주는', 5차 검토) — '…영상을 만들어 줘' 는 화풍
_KO_MADE_STYLE = re.compile(r"\s*(?:$|듯|것\s*(?:처럼|같)|느낌|풍|스타일|처럼|같이|같은|" + _KO_TARGET
                            + r"(?!\s*(?:을|를)\s*(?!(?:만들어|그려|보여|바꿔|표현해|꾸며)\s*" + _KO_IMPER + r")[가-힣])|[가-힣]+?(?:처럼|같이|같은))")
# 'X로' 바로 뒤(목적어 하나 건너)의 첫 동작이 도구를 쓰는 말 ('약초를 스케치하며'·'낙서하며'·'엄마 얼굴을 그려 주는'·'만든 인형을'):
# 그 말로 무엇을 하는 활동이다. 다른 동작이 먼저면('대화하는'·'나오는'·'숲을 걷는') 영화 전체를 꾸미는 화풍이다
_KO_TOOL_HEAD = re.compile(r"(?:(?:[가-힣0-9]+\s+){0,2}[가-힣0-9]+(?:을|를)\s*)?(?P<verb>" + _KO_ACT_VERB.pattern + "|" + _KO_MADE_VERB.pattern + ")")
# 한국어 주어 자리: 아무 낱말 + 가·께서, 사람 말 + 이·은·는·도 ('니키가'·'푸토르가'·'선생님이'·'엄마는'). 맨 '은·는·이' 는 꾸밈말·부사('걷는'·'작은'·
# '같이')일 수 있어 사람 말 뒤에서만 본다
_KO_PERSON_WORD = (r"(?:엄마|아빠|어머니|아버지|할머니|할아버지|아이|아이들|소녀|소년|친구|친구들|선생님|사람|사람들|가족|우리|나|내|제|저|그|그녀|부부"
                   r"|형|누나|언니|오빠|동생|[가-힣]{1,4}님)")
_KO_SUBJECT_RE = re.compile(r"(?:(?<![가-힣])" + _KO_PERSON_WORD + r"(?:이|은|는|도|께서)|[가-힣]{1,8}(?:가|께서))(?=\s)")


def _ko_subject_at(text: str) -> int | None:
    """한국어 글에서 처음 나오는 주어 자리 (_KO_SUBJECT_RE, 라이브러리 인물 이름 + 은·는·도도). 없으면 None"""
    hits = [m.start() for m in _KO_SUBJECT_RE.finditer(text)]
    names = [n for e in library() for n in e["names"] if re.search(r"[가-힣]", n)]
    if names:
        rx = re.compile("(?:" + "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True)) + r")(?:이|은|는|도)(?=\s)")
        hits += [m.start() for m in rx.finditer(text)]
    return min(hits) if hits else None


def _ko_instrument(t: str, s: int, e: int) -> bool | None:
    """요청 글 t[s:e] 뒤가 'X로' 꼴이면 그 말을 도구로 쓴 활동인가: True = 물건·활동, False = 영화 전체를 꾸미는 화풍 꼴, None = 'X로' 꼴이 아님"""
    m = _KO_RO_ANY.match(t, e)
    if not m:
        return None
    rest = m.group("rest").strip()
    if not rest or _KO_STYLE_IMPER.match(rest):
        return False                     # '… 수채화로'·'수채화로 만들어 줘'
    done = re.match(r"된\s+([가-힣]+)", rest)
    if done:
        return not re.match(_KO_TARGET, done.group(1))      # '픽셀로 된 영상' 은 화풍, '픽셀로 된 게임' 은 물건
    head = None if re.match(r"(?:나와|나오|나온|등장|나타나)", rest) else _KO_TOOL_HEAD.match(rest)
    if not head:
        return False                     # '수채화로 숲을 걷는 니키와 푸토르'·'니키와 푸토르가 수채화로 대화하는'·'크레파스로 니키가 뛰노는'
    if _KO_MADE_VERB.fullmatch(head.group("verb")) and _KO_MADE_STYLE.match(rest, head.end()):
        return False                     # '과슈로 그린'·'수채화로 그린 영상'·'니키가 만화로 그려진 영상'
    return True                          # '푸토르가 연필로 약초를 스케치하며'·'니키가 클레이로 만든 인형을'·'니키가 수채화로 그린 그림을 보여주는'


# 'like' 는 앞에 주어·조동사가 올 때만 '좋아하다' 다 ('I like anime'·'would like a clay pot'). 'make it look like a watercolor painting'·
# ', like a claymation film' 은 비유라 화풍 꼴이다 (4차 검토: 비유를 동사로 읽어 화풍 요청이 통째로 사라졌다)
_EN_OBJ_VERB = (r"(?:watch(?:es|ed|ing)?|see(?:s|ing)?|saw|read(?:s|ing)?|draw(?:s|ing)?|drew|paint(?:s|ing|ed)?|mak(?:e|es|ing)|made"
                r"|lov(?:e|es|ed|ing)|lik(?:es|ed|ing)|(?:i|you|we|they|kids|children|people|both|would|'d|do|does|did|don't|doesn't|didn't"
                r"|really|also|to)\s+like|enjoy(?:s|ed|ing)?|show(?:s|ed|ing)?|tak(?:e|es|ing)|took|buy(?:s|ing)?|bought"
                r"|collect(?:s|ed|ing)?|play(?:s|ed|ing)?\s+with|play(?:s|ed|ing)?|hold(?:s|ing)?|held|look(?:s|ed|ing)?\s+at|admir(?:e|es|ed|ing)|hang(?:s|ing)?"
                r"|hung|stud(?:y|ies|ied|ying)|learn(?:s|ed|ing)?|wear(?:s|ing)?|wore|open(?:s|ed|ing)?|carr(?:y|ies|ied|ying)"
                r"|shap(?:e|es|ed|ing)|mou?ld(?:s|ed|ing)?|sculpt(?:s|ed|ing)?|fold(?:s|ed|ing)?|cut(?:s|ting)?|us(?:e|es|ed|ing)"
                r"|giv(?:e|es|ing)|gave|find(?:s|ing)?|found|get(?:s|ting)?|got)")
_EN_DET = (r"(?:(?:a|an|the|some|her|his|their|its|my|your|old|new|favou?rite|little|small|big|tiny|huge|colou?rful|bright|lump\s+of"
           r"|piece\s+of|box\s+of|set\s+of|pair\s+of)\s+){0,3}")
_EN_OBJ_BEFORE = re.compile(r"\b(?P<v>" + _EN_OBJ_VERB + r")\s+" + _EN_DET + r"$", re.I)
# 시스템에 내리는 요청의 앞머리 (5차 검토: 'Can you make an anime video of …'·'Could you make a watercolor video …'·'I want to make …'·
# 'Can you draw Niki and Putor in watercolor?' 를 물건·활동 꼴로 읽어 화풍 요청이 사라졌다)
_EN_LEAD = (r"\W*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?"
            r"|(?:i|we)(?:\s+(?:want|wanna|need|would\s+like)|\s*['’]d\s+like)\s+(?:you\s+)?to\s+|let['’]?s\s+|let\s+us\s+)?")
_EN_IMPER = re.compile(r"^" + _EN_LEAD + r"(?:make|draw|paint|sketch|show|render|create|turn|give)\b", re.I)
_EN_LEAD_RE = re.compile(_EN_LEAD, re.I)
# 'I'd like a …'·"We'd love a …" + 화풍 말 + 영상 말: 좋아하는 물건이 아니라 만들어 달라는 영상이다
_EN_WISH_VERB = re.compile(r"\b(?:i|we)\s*(?:['’]d|\s+would)\s+(?:really\s+|so\s+)?(?:like|love)$|\b(?:i|we)\s+(?:want|need)$", re.I)
_EN_FILM_AFTER = re.compile(r"(?:[\s-]+(?:style|styled|art|inspired))?[\s-]+(?:videos?|films?|movies?|shorts?|clips?|animations?|versions?|cartoons?"
                            r"|stor(?:y|ies)|scenes?|series|episodes?)\b", re.I)
# 'about her favourite Pixar movie' (이야기 소재, 5차 검토)
_EN_ABOUT_BEFORE = re.compile(r"\babout\s+(?:(?:a|an|the|her|his|their|my|your|our|some|this|that|favou?rite|old|new|famous|classic)\s+){0,3}$", re.I)
# '지브리 영화 속 장면'·'웹툰 속 주인공': 그 작품 안의 것 (이야기 소재, 5차 검토)
_KO_INSIDE_AT = re.compile(r"(?:\s*(?:" + _KO_MEDIUM + "|" + _KO_WORK + r")){0,2}\s*속(?![가-힣])")
# '캐릭터로 만들어·바꿔·나오는'·'캐릭터처럼 니키와 …': 물건(캐릭터 상품)이 아니라 인물을 그 화풍으로 그리라는 말 (5차 검토: '캐릭터' 를 늘 물건으로 보아
# '애니메이션 캐릭터로 만들어 줘'·'픽사 캐릭터처럼 …' 이 화풍 요청이 아니게 됐다)
_KO_CHAR_STYLE = re.compile(r"\s*캐릭터\s*(?:(?:으로|로)\s*(?:만들|바꿔|바꾸|그려|그린|그려진|나오|나와|나온|등장|표현|변신)"
                            r"|(?:처럼|같이)(?:\s+[가-힣]+(?:와|과|가|이|를|을|의|는|은)(?:\s|$)|\s*(?:$|[,.!?])|\s*(?:그린|그려|만든|만들|보이|표현|꾸민|꾸며)))")
_EN_MEDIUM = r"(?:[\s-]+(?:paintings?|prints?|drawings?|sketch(?:es)?|paper|puppets?|crafts?|dolls?|figures?|cut[\s-]*outs?|games?|pixel))"
_EN_OBJ_AFTER = re.compile(r"(?:(?:" + _EN_MEDIUM + r")?(?:[\s-]+art)?[\s-]+(?:characters?|figurines?|toys?|club|class(?:es)?|channel|shop|store|museum"
                           r"|theme\s+songs?|songs?|soundtracks?|music|modell?ing|puzzles?|hero(?:es)?|heroines?"
                           r"|gallery|exhibitions?|contest|workshop|lessons?|course|kit|printer|glasses|goggles|pots?|bowls?|cups?|teacher|artist"
                           r"|director|studio|festival|posters?|magazines?|series|shows?|marathon|cinema|theat(?:er|re)|night|fans?|maker|map"
                           r"|cards?|letters?|album|camera|memory|games?|apps?|sprites?|consoles?|gamers?)\b"
                           r"|[\s-]+art[\s-]+(?:drawings?|pictures?)\b(?!\s+of\b))(?![\s-]*(?:style|look|like|feel|aesthetic|vibe|inspired))", re.I)
# 활동 동사 + 목적어 + 'in X' ('Putor sketches the herbs in pencil'·'Niki draws Putor in crayon'·'Beth paints the garden in watercolor'):
# 'in X' 는 활동의 도구다 (4차 검토). 'painted in watercolor'·'a drawing of Niki in pencil' 같은 화풍 꼴은 아니다
_EN_ACT_BEFORE = re.compile(r"(?<!\ba )(?<!\ban )(?<!\bthe )\b(?:sketch(?:es|ing)?|draw(?:s|ing)?|paint(?:s|ing)?|doodl(?:e|es|ing)"
                            r"|scribbl(?:e|es|ing)|colou?r(?:s|ing)?|shad(?:e|es|ing)|writ(?:e|es|ing)|trac(?:e|es|ing))\s+(?![Oo]f\b)"
                            r"(?:[\w'’-]+\s+){1,5}in\s+$", re.I)
_EN_IDIOM_AFTER = re.compile(r"[\s-]+(?:conversation|discussion|debate|voice|tone|gestures?|faces?|expressions?|chat|talk|argument|mood|way"
                             r"|exchange|smile|eyes|movements?)\b", re.I)
_EN_MARK_AFTER = re.compile(r"(?:" + _EN_MEDIUM + r"){0,2}(?:[\s-]+(?:styles?|styled|look|aesthetics?|art(?:work)?|illustrations?|illustrated"
                            r"|animations?|animated|rendered|render(?:ing)?|graphics|footage|films?|short\s+films?|movies?|videos?|shorts?"
                            r"|features?|versions?|cartoons?|world|universe)\b|" + _EN_MEDIUM + r"\s+of\b)", re.I)
_EN_MARK_BEFORE = re.compile(r"\b(?:in|as|like|into)\s+(?:the\s+(?:style|manner)\s+of\s+(?:(?:an?|the)\s+)?|an?\s+)?(?:(?!the\b|this\b|that\b|her\b|his\b|their\b)[\w'’-]+\s+)?$", re.I)
# 이어진 화풍 말 사이 ('수채화 동화풍' 의 '화 ', 'watercolor and storybook style' 의 ' and ', '8비트 픽셀 게임 느낌' 의 ' ')
_REQ_JOIN = re.compile(r"(?:[\s-]*(?:화|그림|painting|drawing))?\s*(?:,|와|과|하고|and|&|/)?\s*", re.I)
# 아는 말이 없는 화풍 요청(maybe)의 표지. 꾸밈말·계절·분위기 말('따뜻한 느낌'·'봄 느낌')과 배경('유럽풍 마을')은 아니다
_MOOD_KO = re.compile(r"^(?:따뜻|따스|몽환|귀엽|귀여|잔잔|밝|어두|슬프|슬픈|행복|신비|감성|아련|포근|화사|차분|평화|우울|쓸쓸|외로|설레|기쁜|무서|"
                      r"공포|웃긴|재미|코믹|진지|웅장|장엄|고요|조용|시원|청량|봄|여름|가을|겨울|아침|저녁|밤|새벽|낮|크리스마스|명절|축제|파티|"
                      r"패션|헤어|옷|머리|라이프|생활|말투|연기|색|컬러|톤|분위기|감정|자연|일상|현실|평소|원래|기존|같은|그|이|저|"
                      r"영화|시네마|광고|뮤비|뮤직|다큐|화보|드라마|예능)")      # '영화처럼 만들어 줘'·'영화 느낌으로' 는 완성도 말 (4차 검토)
_GENERIC_KO = re.compile(
    r"(?P<w>[가-힣A-Za-z0-9.'’-]+)\s*(?:풍(?=\s*(?:으로|로|의|이|$|[,.!?])|\s*(?:영상|그림|애니|화면|영화))|화풍|그림체|그림풍|작화"
    r"|스타일(?=\s*(?:로|으로|의|이|$|[,.!?]))|느낌(?=\s*(?:으로|로|의|이|$|[,.!?]))|처럼\s*(?:그린|그려|만든|만들|표현|보이게|꾸민|꾸며|찍))")
# '영화처럼'·'영화 같은 분위기로'·'광고처럼'·'뮤비처럼' 은 분위기·완성도 말이지 화풍이 아니다 (4차 검토: 화풍일 수도 있는 말로 읽어 모델이 쓴
# 'cinematic film look' 이 남아 승인된 라이브러리 화풍을 잃었다). '애니메이션처럼'·'게임처럼' 은 화풍일 수도 있다
_GENERIC_WORK_KO = re.compile(r"(?:애니메이션|애니|만화|게임)(?:처럼|같이|\s*같은\s*(?:영상|화면|그림|분위기|느낌))")
_BRAND = (r"(?:디즈니|disney|드림웍스|dreamworks|마블|marvel|고흐|gogh|모네|monet|피카소|picasso|클림트|klimt|뭉크|munch|르누아르|renoir"
          r"|웨스\s*앤더슨|wes\s+anderson|팀\s*버튼|tim\s+burton|일러스트(?!레이터)|illustrat(?:ed|ion))")
_BRAND_RE = re.compile(_BRAND + r"(?:['’]s)?(?:[\s-]*(?:애니메이션|애니|영화|그림|작품|animation|animated|cartoons?|movies?|films?|paintings?|art(?:work)?))?"
                       r"[\s-]*(?:처럼|같이|같은|풍|느낌|스타일|style|look|inspired|like|-like)", re.I)
_GENERIC_EN = re.compile(r"\b(?:in\s+the\s+(?:style|manner)\s+of|inspired\s+by)\s+\w|\b(?:art|visual|drawing|animation|painting)\s+style\b"
                         r"|\b(?P<w>[\w’'-]+)[\s-](?:style|aesthetic|inspired)\b", re.I)
_MOOD_EN = re.compile(r"^(?:fashion|hair|life|play|fighting|speaking|writing|cooking|singing|dancing|free|living|her|his|their|my|your|its|this"
                      r"|that|same|own|new|different|similar|unique|personal|signature|warm|soft|dreamy|calm|cozy|cosy|bright|dark|happy|sad"
                      r"|gentle|cute|sweet|funny|scary|moody|quiet|peaceful|cheerful|serious|nice|beautiful|pretty|any|some|every|one|a|the"
                      r"|cinematic|filmic|epic|dramatic|movie|film|commercial|blockbuster|hollywood|[\w’'-]+['’]s)$", re.I)
# 화풍일 수도 있는 말만 있는 요청(maybe)에서 구성안의 custom 화풍 글을 둘 만한가: 그림·재료·화가·화풍 갈래 말이 들어 있다 (4차 검토: 모델이 쓴
# 'cinematic fantasy look' 처럼 화풍 말이 없는 글이 custom 으로 남아 라이브러리 인물의 승인된 기준 그림을 바꿨다)
_ART_WORD_RE = re.compile(r"\b(?:illustrat\w*|paint\w*|drawings?|drawn|sketch\w*|prints?|posters?|art|artwork|engrav\w*|lithograph\w*|mural"
                          r"|collage|graphics?|impressionis\w*|expressionis\w*|cubis\w*|surrealis\w*|baroque|renaissance|rococo|cyberpunk"
                          r"|steampunk|vaporwave|synthwave|anim\w*|cartoon\w*|comic\w*)\b|" + _BRAND, re.I)
# 요청이 화풍을 말했는데 구성안 화풍이 비었거나 어긋날 때 채우는 그 열쇠의 기본 화풍 글 (라이브러리 화풍은 반실사라 애니·실사·3D 요청에
# 맞지 않는다). custom 은 요청의 화풍 말을 영어 이름으로 옮긴다 (custom_style_text)
SAID_STYLE_TEXT = {"anime": "2D anime feature film with clean cel-shaded line art and flat colours with soft shading",
                   "photo": "photorealistic cinematic live-action film",
                   "toon3d": "stylized 3D animated feature film with smooth soft shading, rounded stylized proportions and clean materials"}
# 요청의 화풍 말만으로는 열쇠가 custom 인데 애니·3D 애니를 뜻하는 말 (said_key: '손그림 느낌'·'카툰 스타일'·'3D로'·'CG 느낌')
_ANIME_HINT = re.compile(r"손\s*그림|hand[\s-]*drawn|카툰|cartoon|지브리|ghibli", re.I)
_TOON_HINT = re.compile(_3D + r"|(?<![a-z])cgi?(?![a-z])|픽사|pixar", re.I)
_STYLE_WORD_RE = re.compile(r"그림체|화풍|그림풍|작화")
# 정해진 인물의 원래 모습을 그대로 쓰라는 말은 새 화풍을 말한 것이 아니다 ("원래 그림체 그대로")
_KEEP_LOOK_RE = re.compile(r"(?:원래|기존|평소|정해진|같은)\s*(?:의\s*)?(?:그림체|화풍|그림풍|작화)"
                           r"|(?:그림체|화풍|그림풍|작화)\s*(?:을|를|은|는|이|가|도)?\s*(?:그대로|유지)")
# '아닌 것'을 말한 구절 (구성안 화풍 글의 "not a cartoon"·"non-photorealistic"): 그 화풍으로 읽지 않게 지우고 본다.
# 'non-' 은 바로 붙은 낱말 하나만 부정한다 ('non-realistic 2D animation' 의 'animation' 까지 지우면 애니가 반실사로 읽혔다, 11-24 검토).
# not·never·no·without 은 쉼표까지 ("not a cel-shaded cartoon" 은 구절 전체가 아닌 것)
_NOT_RE = re.compile(r"\bnon[\s-]*[a-z0-9]+(?:-[a-z0-9]+)*|\b(?:not|never|no|without|rather than|instead of)\b[^,.;:!?]*", re.I)
_cache: dict = {}


def style_key(style: str) -> str:
    """화풍 글 → 열쇠 'semi'(반실사)·'anime'(애니)·'toon3d'(3D 애니메이션)·'photo'(실사)·'custom'(그 밖에 분명히 말한 화풍).
    '아닌 것' 구절은 지우고, 위에서부터 먼저 맞는 것 (11-24 2차, 수채·3D·클레이 요청이 세 열쇠 중 하나로 묶여 'not a cartoon'·
    'not a 3D render' 같은 부딪히는 문장이 들어가던 것을 고침):
    1) 분명한 반실사 말(semi-real·semi photo·반실사·2.5D) → semi (라이브러리 화풍 글 'semi-realistic'이 실사 낱말에 걸리지 않게 먼저)
    2) 애니 말(anime·manga·manhwa·webtoon·cel·2D·만화·웹툰·지브리, '움직이는 그림' 말 animation·animated·cartoon·카툰·애니) → anime: 그림 말보다
       먼저 본다 (11-18 애니 시험 문장 '2D … cel-shaded …, soft painted backgrounds' 가 'painted' 때문에 반실사로 읽혔다). 단
       - 3D 나 3D 애니 말이 함께 있으면 toon3d. 2D 까지 함께 말했으면(2D 선 + 3D 인물 같은 섞인 화풍) custom: 애니 문장의
         'not a 3D render' 와 3D 애니 문장의 'not a flat 2D drawing' 이 둘 다 원문과 부딪힌다. 재료·기법 말(클레이·로우폴리·종이·펠트·픽셀 …,
         _MATERIAL_RE)이 함께 있어도 custom ('3D 클레이 애니메이션', 4차 검토: 3D 애니 기본 글이 점토 원문을 덮었다)
       - '움직이는 그림' 말만 있고 custom 말이 붙었으면('stop-motion animation'·'클레이 애니') 그 화풍(5)
       - 애니 말이 '2D' 하나뿐이고 재료 말이 함께면('2D pixel art animation'·'watercolor-style 2D animation') custom (5차 검토)
    3) 3D 애니 말(3D animated·Pixar-like·Disney 3D·CG animation·CG 애니·3D cartoon·stylized 3D·3D 애니·픽사·드림웍스) → toon3d (재료 말이
       함께 있으면 custom)
    4) 실사 말(실사·photoreal·photo-real·live action·photograph·realistic photo·사진처럼·사진 같은) → photo. 사실적인 영화 말(hyper-realistic·
       cinematic realism·realistic film·realistic style·lifelike·naturalistic·사실적·리얼한·실제 사람, 3차·4차)은 custom 말이 함께 없을 때만,
       사진 말 없는 실사 영화 글(cinematic film look·35mm·documentary style, 4차)은 custom 말·그림 말이 함께 없을 때만 photo
    5) custom 말(수채·과슈·유화·아크릴·파스텔·크레용·색연필·연필 스케치·목탄·펜화·수묵·판화·우키요에·동화·그림책·클레이·스톱모션·종이 오리기·
       종이접기·펠트·픽셀·모자이크·실루엣·코믹·그래픽 노블·누아르·흑백 무성영화·스테인드글라스·로우 폴리) → custom
    6) 그림 말(painterly·digital painting·painted) → semi
    7) 빈 글 → semi, 비어 있지 않은데 아무 말에도 안 맞으면 custom (화풍 원문을 그대로 쓴다)"""
    raw = str(style or "")
    if not raw.strip():
        return "semi"
    t = _NOT_RE.sub(" ", raw)
    if _SEMI_RE.search(t):
        return "semi"
    anime, moving = _ANIME_RE.search(_CEL_COMIC_RE.sub(" ", t)), _MOVING_RE.search(t)     # 'cel-shaded comic' 은 코믹(5)
    if anime or moving:
        if _3D_RE.search(t) or _TOON3D_RE.search(t):
            return CUSTOM_KEY if _2D_RE.search(t) or _MATERIAL_RE.search(t) else "toon3d"
        if _ANIME_WORD_RE.search(_CEL_COMIC_RE.sub(" ", t)):
            return "anime"
        if anime:                           # 애니 말이 2D 하나뿐: 재료 말이 함께면 그 재료 화풍 (5차 검토)
            return CUSTOM_KEY if _MATERIAL_RE.search(t) else "anime"
        if not _CUSTOM_RE.search(t):
            return "anime"
    if _TOON3D_RE.search(t):
        return CUSTOM_KEY if _MATERIAL_RE.search(t) else "toon3d"
    if _PHOTO_RE.search(t) or (_REAL_RE.search(t) and not _CUSTOM_RE.search(t)):
        return "photo"
    if _FILM_LOOK_RE.search(t) and not _CUSTOM_RE.search(t) and not _ART_FORM_RE.search(t):
        return "photo"
    if _CUSTOM_RE.search(t):
        return CUSTOM_KEY
    if _PAINT_RE.search(t):
        return "semi"
    return CUSTOM_KEY


def is_photo_style(style: str) -> bool:
    """실사 화풍인가. video.story_style(사실적이지 않은 화풍만 돌려준다)과 화풍 열쇠가 같은 판정을 쓰게 한 곳에 둔다
    (예전에는 정규식이 둘이라 'semi-photorealistic digital painting' 이 열쇠로는 반실사, story_style 로는 실사였다)"""
    return style_key(style) == "photo"


def _req_object(t: str, s: int, e: int, key: bool) -> bool:
    """요청 글 t[s:e] 의 화풍 말이 물건·활동·관용 표현으로 쓰였는가 (_req_scan)"""
    if _KO_CHAR_STYLE.match(t, e):
        return False                     # '애니메이션 캐릭터로 만들어 줘'·'픽사 캐릭터처럼 니키와 …'
    if _KO_OBJ_AT.match(t, e) or _KO_OBJ_NOUN_AT.match(t, e) or _KO_IDIOM_AT.match(t, e) or _KO_INSIDE_AT.match(t, e):
        return True
    m = _KO_WITH_AT.match(t, e)
    if m and not (_REQ_KEY.match(t, m.end()) or _REQ_MARKED.match(t, m.end()) or _REQ_CUSTOM_RE.match(t, m.end())):
        return True
    tool = _ko_instrument(t, s, e)
    if tool is not None:
        return tool
    m = _EN_OBJ_BEFORE.search(t, 0, s)
    if m and not _EN_IMPER.match(t[:m.end("v")]) and not (_EN_WISH_VERB.search(t[:m.end("v")]) and _EN_FILM_AFTER.match(t, e)):
        return True
    m = _EN_ACT_BEFORE.search(t, 0, s)
    if m and not _EN_LEAD_RE.fullmatch(t[:m.start()]):
        return True                      # 'Niki draws Putor in crayon' — 'Draw Niki and Putor talking in watercolor' 는 요청 자체의 동사
    m = _EN_OBJ_AFTER.match(t, e)
    if m and not (re.match(r"[\s-]+characters?\b", t[e:], re.I) and (re.search(r"\b(?:as|into|like)\s+(?:[\w'’-]+\s+)?$", t[:s], re.I)
                                                                      or _EN_IMPER.match(t))):
        return True                      # 'drawing anime characters' 는 물건, 'as Pixar characters'·'Make Niki and Putor anime characters' 는 화풍
    if _EN_ABOUT_BEFORE.search(t, 0, s):
        return True                      # 'tells Putor about her favourite Pixar movie'
    return key and t[s:e].lower().startswith("anim") and bool(_EN_IDIOM_AFTER.match(t, e))


def _req_marked(t: str, s: int, e: int) -> int:
    """요청 글 t[s:e] 의 말 뒤(앞)에 화풍 꼴이 붙었으면 그 꼴이 끝나는 자리, 아니면 0"""
    m = _KO_STYLE_AT.match(t, e) or _EN_MARK_AFTER.match(t, e)
    if m:
        return m.end()
    if _EN_MARK_BEFORE.search(t, 0, s):
        return e
    m = _KO_RO_AT.match(t, e)
    if m and not _KO_MATERIAL.search(m.group("rest")):
        return m.start("rest")
    m = _KO_LIKE_AT.match(t, e)
    return m.end() if m else 0


# 요청 글의 사실·리얼·현실적·real-life·real people 은 화풍 꼴일 때만 실사 요청이다 (5차 검토: '사실적인 대화'·'리얼한 표정'·'리얼하게 다투는'·
# 'a real-life story' 같은 연기·대화를 꾸미는 말을 실사 요청으로 읽어 라이브러리 인물이 승인된 반실사를 잃었다). 낱말 목록(_REALISM)은 화풍 글과 같다
_REQ_REAL_FORM = re.compile(r"사실적|리얼|현실적|현실감|실제|진짜|real[\s-]+life|real\s+people", re.I)
_KO_REAL_TARGET = re.compile(r"\s*(?:느낌|스타일|화풍|그림체|영상|동영상|화면|버전|화질|영화|작품|분위기|톤|질감|색감|연출|그래픽|비주얼|룩|장면)")
_EN_REAL_TARGET = re.compile(r"[\s-]+(?:look|style|version|footage|film|movie|video|feel|aesthetic)s?\b", re.I)
# 끝 꼬리표로 붙인 실사 말 ('…, realistic, not anime'·'…, real people'): 열쇠 말이라 꼬리표 자리면 화풍이다
_REAL_TAIL_RE = re.compile(r"(?:^|[,.;(])\s*(?P<w>(?:very\s+)?realistic|real\s+people|real\s+actors?)\s*(?=$|[,.;)!?]|\b(?:and|but|not)\b)", re.I)
# 부정한 화풍 말 (5차 검토: '실사 말고 애니메이션으로'·'실사 느낌은 빼고'·'realistic, not anime' 의 부정한 말까지 세어 고정 우선순위로 골랐다)
_KO_NEG_AFTER = re.compile(r"(?:\s*(?:느낌|스타일|풍|화풍|그림체|분위기|톤|색감|질감|영상|버전))?\s*(?:은|는|이|가|을|를|도)?\s*"
                           r"(?:말고|빼고|빼\s*(?:줘|주세요|달라)|대신(?:에)?|아니라|아닌|아니고|하지\s*말고|없이)")
_EN_NEG_BEFORE = re.compile(r"\b(?:not|no|never|without|instead\s+of|rather\s+than)\s+(?:(?:a|an|the|in|as|like|any|too)\s+)?$|\bnon[\s-]*$", re.I)


def _real_form_ok(t: str, s: int, e: int) -> bool:
    """요청 글 t[s:e] 의 사실·리얼·현실적·real-life·real people 이 화풍 꼴인가: 뒤에 화풍 말이 온다('사실적인 느낌으로'·'리얼한 영화처럼'·
    'real-life look'), 부사 꼴로 영화 전체를 꾸민다('리얼하게 베스와 제임스가 …'·'현실적으로 …'·'사실적으로 만들어 줘'), 꼬리표·'with real people'.
    연기·대화를 꾸미면('사실적인 대화'·'리얼한 표정'·'리얼하게 다투는'·'real-life story') 아니다"""
    w = t[s:e]
    if re.match(r"실제|진짜", w):
        # '실제 사람이 나오는'·'진짜 사람이 연기하는' 은 실사, 맨 '실제 인물' 은 '…처럼 니키와 베스가 …'·'…처럼 보이게' 일 때만 ('실제 인물을 바탕으로'·
        # '실제 인물처럼 생생하게 대화하는' 은 이야기·연기 말이다)
        if re.search(r"(?:연기|출연|등장|나오는|나온|나와|나오)$", w):
            return True
        m = re.match(r"(?:들)?\s*(?:처럼|같이|같은)\s*", t[e:])
        if not m:
            return bool(_KO_REAL_TARGET.match(t, e))
        rest = t[e + m.end():]
        if not rest.strip() or _KO_REAL_TARGET.match(rest) or re.match(r"(?:보이|표현|찍|만들|그려)", rest):
            return True
        sub = _ko_subject_at(rest + " ")
        return sub is not None and bool(re.fullmatch(r"\s*(?:[가-힣]+(?:와|과|랑|이랑|하고)\s+)*", rest[:sub]))
    if re.match(r"real\s+people", w, re.I):
        return bool(re.search(r"(?:^|[,.;(]|\b(?:with|by|starring|featuring|using|as)\b)\s*$", t[:s], re.I))
    if re.match(r"real[\s-]+life", w, re.I):
        return bool(re.search(r"(?:look|style|version|film|movie|video)$", w, re.I) or _EN_REAL_TARGET.match(t, e))
    if _KO_REAL_TARGET.match(t, e) or re.search(r"(?:영상|영화|느낌|스타일|버전|리즘|리티)$", w):
        return True
    if re.search(r"(?:으로|하게|게|히|있게)$", w):
        rest = t[e:]
        if not rest.strip() or re.match(r"\s*[,.!?]", rest) or _KO_STYLE_IMPER.match(rest.strip()):
            return True
        sub = _ko_subject_at(rest + " ")
        return sub is not None and bool(re.fullmatch(r"\s*(?:[가-힣]+(?:와|과|랑|이랑|하고)\s+)*", rest[:sub]))
    return False


def _negated(t: str, s: int, e: int) -> bool:
    """요청 글 t[s:e] 의 화풍 조각을 부정했는가 ('실사 말고'·'실사 느낌은 빼고'·'애니메이션이 아닌'·'not anime'·'instead of 3D')"""
    return bool(_KO_NEG_AFTER.match(t, e) or _EN_NEG_BEFORE.search(t, 0, s))


def _word_hits(t: str) -> list[tuple[int, int, str, str]]:
    """글 한 줄의 화풍 말 [(시작, 끝, 'key'·'mark', custom 영어 이름 또는 "")]: 열쇠 말·끝 꼬리표 실사 말·화풍 꼴일 때만 화풍인 말·custom 말.
    겹치면 긴 말 하나 ('colored pencil' 의 'pencil'). 화풍으로 쓰였는지는 보지 않는다 (요청 글은 _req_hits 가, 구성안의 화풍 구절은 _phrase_words 가 쓴다)"""
    hits = [(m.start(), m.end(), "key", "") for m in _REQ_KEY.finditer(t)] \
        + [(m.start("w"), m.end("w"), "key", "") for m in _REAL_TAIL_RE.finditer(t)] \
        + [(m.start(), m.end(), "mark", "") for m in _REQ_MARKED.finditer(t)] \
        + [(m.start(), m.end(), "mark", name) for pat, name in _REQ_CUSTOM for m in re.finditer(pat, t, re.I)]
    hits.sort(key=lambda h: (h[0], h[0] - h[1]))
    words: list[tuple[int, int, str, str]] = []
    for h in hits:
        if not words or h[0] >= words[-1][1]:
            words.append(h)
    return words


def _req_hits(t: str) -> list[tuple[int, int, str, str, bool]]:
    """요청 글 한 줄에서 화풍으로 쓴 말 [(시작, 끝, 조각, custom 영어 이름 또는 "", 부정했는가)] (_req_scan)"""
    t = str(t or "")
    words = _word_hits(t)
    used: list[tuple[int, int, str, str]] = []
    chained = False
    for k in range(len(words) - 1, -1, -1):          # 뒤에서부터: 이어진 말은 뒤 말이 화풍이면 함께 화풍이다
        s, e, cls, name = words[k]
        end = 0
        if cls == "key" and _MOVING_WORD.fullmatch(t[s:e]) and _AMBIG_BRAND_BEFORE.search(t, 0, s):
            chained = False
            continue                                  # '디즈니 애니메이션처럼': 2D·3D 는 구성안 모델이 정한다 (said_level maybe)
        if not _req_object(t, s, e, cls == "key") and not (cls == "key" and _REQ_REAL_FORM.match(t[s:e]) and not _real_form_ok(t, s, e)):
            end = e if cls == "key" or _REQ_SURE.match(t, s) else _req_marked(t, s, e)
            if not end and chained and k + 1 < len(words) and _REQ_JOIN.fullmatch(t, e, words[k + 1][0]):
                end = e
        chained = bool(end)
        if end:
            stop = max(end, e)
            used.append((s, e, t[s:stop].strip(), name, _negated(t, s, stop)))
    return used[::-1]


def _style_part(text: str) -> str:
    """요청 글에서 화풍을 볼 줄 (5차 검토): 결과 뒤 고치기는 '앞 요청 + 줄바꿈 + 이번 말' 이라('실사로 …' 다음 '애니메이션으로 바꿔 줘')
    화풍 말(부정한 말도)이 있는 가장 나중 줄이 지금 바라는 화풍이다. 그런 줄이 없으면 글 전체"""
    text = str(text or "")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) > 1:
        for ln in reversed(lines):
            if _req_hits(ln) or _maybe_style(ln):
                return ln
    return text


def _req_scan(text: str) -> list[tuple[int, int, str, str]]:
    """요청 글에서 화풍으로 쓴 말 [(시작, 끝, 조각, custom 영어 이름 또는 "")]. 조각은 말과 그 표지 ('수채화 느낌'·'애니메이션으로'·'watercolor style').
    겹치면 긴 말 하나 ('colored pencil' 의 'pencil'). 이어진 custom 말은 뒤 말이 화풍이면 함께 화풍이다 ('수채화 동화풍').
    여러 줄이면 화풍을 말한 가장 나중 줄만 보고(_style_part), 부정한 말('실사 말고'·'not anime')은 뺀다 (5차 검토)"""
    return [h[:4] for h in _req_hits(_style_part(text)) if not h[4]]


def said_level(text: str) -> str | None:
    """요청 글이 화풍을 말했는가 (11-24 3차 검토): 'sure' = 아는 화풍 말을 화풍으로 썼다(_req_scan), 'maybe' = 아는 말은 없지만 화풍 표지가
    붙은 말('반 고흐 스타일로'·'사이버펑크 느낌으로'·'아르데코 풍으로'·'Van Gogh style'·'디즈니처럼'·'흑백 무성영화처럼'·'따뜻한 화풍으로'),
    None = 말하지 않았다 ('원래 그림체 그대로' 처럼 정해진 모습을 쓰라는 말과 물건·활동·관용 표현은 화풍이 아니다)"""
    if _req_scan(text):
        return "sure"
    return "maybe" if _maybe_style(_style_part(text)) else None


def _maybe_style(text: str) -> bool:
    """아는 화풍 말 없이 화풍 표지만 붙은 말이 있는가 (said_level 의 maybe)"""
    t = _KEEP_LOOK_RE.sub(" ", str(text or ""))       # '원래 그림체 그대로'·'같은 그림체로' 는 화풍 표지가 아니다 ('그림책 같은 그림체' 는 위에서 화풍)
    for m in _GENERIC_KO.finditer(t):
        w = m.group("w")
        if not (_MOOD_KO.match(w) or re.search(_ADJ_N + r"$|(?:의|가|이|을|를|은|는|와|과|도|만|에|게|히|던)$", w)):
            return True
    for m in _GENERIC_EN.finditer(t):
        if not (m.group("w") and _MOOD_EN.match(m.group("w"))):
            return True
    return bool(_GENERIC_WORK_KO.search(t) or _BRAND_RE.search(t) or _STYLE_WORD_RE.search(t))


def said_style(text: str) -> bool:
    """요청 글이 화풍을 말했는가 (said_level 이 sure·maybe). 말했으면 구성안에 라이브러리 화풍을 그대로 베끼라고 하지 않고, 구성안이 따른 화풍을
    덮어쓰지 않는다 (니키가 나오는 애니 요청이 반실사가 되던 문제)"""
    return said_level(text) is not None


def _said_snips(text: str) -> list[str]:
    return [snip for _s, _e, snip, _n in _req_scan(text)]


_REAL_TAIL_WORD = re.compile(r"(?:very\s+)?realistic|real\s+(?:people|actors?)", re.I)


def _photo_snip(snip: str) -> bool:
    """화풍 조각이 실사인가 (꼬리표로 붙인 'realistic'·'real people' 도, 5차 검토)"""
    return style_key(snip) == "photo" or bool(_REAL_TAIL_WORD.fullmatch(snip))


def said_photo(text: str) -> bool:
    """요청 글이 실사를 화풍으로 말했는가 ('반실사'·'semi-photorealistic' 의 '실사·photo' 와 '사진을 찍는'·'a photograph of' 는 아니다)"""
    return any(_photo_snip(snip) for snip in _said_snips(text))


def said_key(text: str) -> str | None:
    """요청 글이 말한 화풍의 열쇠 (semi·anime·toon3d·photo·custom). 화풍을 말하지 않았으면 None, 아는 말 없이 화풍 표지만 있으면 custom.
    화풍으로 쓴 말(_req_scan)만 본다: '점토 인형을 만드는 애니메이션' 은 애니, 'painted in watercolor' 는 수채(custom, 3차 검토: 'painted' 때문에
    반실사로 읽혔다). 실사 말이 있으면 photo. 말만으로 custom 인데 애니·3D 애니를 뜻하면(손그림·카툰·지브리, 맨 3D·CG) 그 열쇠"""
    snips = _said_snips(text)
    if not snips:
        return CUSTOM_KEY if said_level(text) else None
    if any(_photo_snip(snip) for snip in snips):
        return "photo"
    joined = " ".join(snips)
    key = style_key(joined)
    if key == CUSTOM_KEY and not _CUSTOM_RE.search(joined):
        if _TOON_HINT.search(joined):
            return "toon3d"
        if _ANIME_HINT.search(joined):
            return "anime"
    return key


# ── 구성안이 적은 사용자의 화풍 구절 (bible.style_said, 11-25 사용자 결정 "방식 바꾸기", 3차 명세 2절) ──
# 요청 글로 '화풍을 말했는가'를 짐작하던 낱말 꼴 판정(_req_scan·said_level)이 고칠 때마다 흔한 문장에서 새로 깨졌다. 이제 구성안 모델이 요청을
# 읽고 사용자가 화풍을 말한 말만 그대로 옮겨 적는다 (물건·활동 거르기와 '말고' 같은 부정은 모델이 한다). 코드는 그 구절을 지금 낱말 표로
# 열쇠 하나로만 바꾼다. 칸이 없는 구성안(예전에 저장된 것, 모델이 빠뜨린 것)은 예전 요청 글 경로(said_level)를 그대로 쓴다
def _flat(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


# 사진 말 ('사진'·'사진 느낌'·'photograph'): 화풍 구절 안에서는 표지 없이도 실사다
_PHRASE_PHOTO_RE = re.compile(r"사진|photograph", re.I)
# 구성안이 '말하지 않음' 대신 쓴 자리 채움 글 ('none'·'없음'·'N/A'·'-'): "" 와 같다 (fixA: 기본값 실사 화풍까지 2.4b 에 세 번 모두 걸렸다)
_SAID_EMPTY_RE = re.compile(r"none|n\s*/?\s*a|null|nil|nothing|no|not\s+specified|unspecified|default|없음|없다|없어요|없습니다|해당\s*없음|지정\s*없음"
                            r"|기본|기본값|-+|—|–|\.+", re.I)
# 구절 길에서만 보는 애매한 디즈니 (fixA): '디즈니' 와 '움직이는 그림' 말 사이에 꾸밈말('풍'·'스타일'·'-style'·'느낌의'·'공주')이 끼어도 2D·3D 를
# 모른다 ('디즈니풍 애니메이션'·'Disney-style animation' 이 2D 애니로 고정되어 구성안의 3D 화풍을 거절했다). 요청 글 예비 경로는 넓히지 않는다
_PHRASE_AMBIG_BEFORE = re.compile(r"(?:디즈니|disney)(?:['’]s)?(?:[\s-]*(?:풍|식|스타일|styled?|느낌의?|같은|공주|princess|classic|클래식))*[\s-]*$", re.I)
# 구절 길의 색·분위기 말 (fixA): '파스텔 톤으로'·'pastel colors' 는 색감, '코믹한 느낌으로'·'comic mood' 는 분위기라 재료 화풍(custom)이 아니다
# (화풍 글 표 _CUSTOM_WORDS 의 거르기를 이 두 말에 옮긴다. '유화'·'도트'처럼 표지 없는 재료 말은 그대로 화풍이다)
_PHRASE_TONE_AFTER = {
    "soft pastel drawing": re.compile(r"[\s-]*(?:tones?|toned|colou?rs?|colou?red|shades?|palette|hues?)\b|\s*(?:톤|색|컬러|빛|계열|팔레트)", re.I),
    "comic book art": re.compile(r"(?:한|하게|하고|스러운|스럽게|적인|적으로)|\s*(?:분위기|톤)|[\s-]*(?:mood|tone|feel(?:ing)?|vibe)\b", re.I)}
# 열쇠가 None 인 구절이 그림·화가·애니 쪽 말인가 (fixA): 그러면 실사 화풍은 그 화풍이 아니다 (실사 화풍 글을 받을지만 가른다 — fixB: 반실사
# 글은 이 목록으로 가르지 않고 칸끼리 본다, style_said_fits). 영화 종류·분위기 말('다큐멘터리처럼'·'공포 영화처럼'·'판타지 영화처럼'·'레트로 느낌')은
# 아니다 — 실사 화풍도 맞다. fixB: 회화 말(동양화·한국화·민화·벽화·명화·회화·스케치 …)을 더했다 ('영화'·'동화' 와 헷갈리지 않게 낱말로 적는다)
_PHRASE_DRAWN_RE = re.compile(r"그림|화풍|작화|일러스트|삽화|화가|만화|동화|애니|인상파|인상주의|표현주의|입체파|입체주의|초현실|팝\s*아트|아르\s*(?:데코|누보)"
                              r"|고흐|모네|피카소|클림트|뭉크|르누아르|디즈니|드림웍스"
                              r"|회화|명화|동양화|한국화|민화|산수화|풍속화|벽화|초상화|풍경화|정물화|추상화|점묘|스케치|드로잉|그래피티|프레스코"
                              r"|\b(?:gogh|monet|picasso|klimt|munch|renoir|disney|dreamworks|paint\w*|drawings?|drawn|illustrat\w*|sketch\w*"
                              r"|art(?:work)?|artist|impressionis\w*|expressionis\w*|cubis\w*|surrealis\w*|pop[\s-]+art|art[\s-]+(?:deco|nouveau)"
                              r"|anim\w*|cartoon\w*|comic\w*|graffiti|murals?|canvas|fresco\w*)\b", re.I)
# 구절 길에서만 보는 영어 거절 꼴 ('anything but semi-realistic'·'other than anime') — 요청 글 예비 길(_EN_NEG_BEFORE)은 넓히지 않는다
_PHRASE_NEG_BEFORE = re.compile(r"\b(?:(?:anything|everything)\s+but|other\s+than|except(?:\s+for)?)\s+(?:(?:a|an|the|in|as|like)\s+)?$", re.I)
# 구절이 인물의 원래 그림체나 지금 보는 화풍을 가리키는 말 (fixC, fixD) — 뒤에 '말고'·'빼고' 가 오고 그 뒤가 화풍을 말하면 그 화풍을 거절한 것이다
# (_phrase_scan·_phrase_refused). 그림 말은 그림체·화풍·그림풍·작화·스타일 (fixD: '지금 스타일 말고 다른 스타일로'·'원래 스타일 말고 …' 를 놓쳐
# 거절한 라이브러리 반실사가 소리 없이 나왔다). 지키는 말('원래 그림체 그대로')은 said_text 가 "" 로 읽는다 (_KEEP_LOOK_RE)
# - 인물의 원래 그림체(OWN_LOOK): 원래·기존·평소·정해진 + 그림 말, 라이브러리 인물 이름 + 그림 말('니키 그림체 말고', _names_look_re)
# - 지금 보는 화풍(NOW_LOOK): 지금·현재·이 + 그림 말, 앞말 없는 그림 말. 요청이 한 메시지면 인물의 그림체, 고치기 차례(앞 메시지로 만든 영상이 있음)면
#   그 영상의 화풍이다 (now_style, fixD: '애니메이션 그림체로' 로 만든 영상 뒤의 '지금 그림체 말고 다른 스타일로' 를 라이브러리 반실사 거절로 읽어
#   바르게 읽은 반실사 구성안을 사용자가 거절한 애니로 바꿨다)
# 앞말 없는 그림 말은 앞에 한글이 붙지 않아야 하고('만화풍'·'동화풍'·'동양화풍' 의 '화풍' 이 아니다, fixD), 구절 처음이나 쉼표 뒤에 와야 하며
# (앞 낱말이 꾸미는 그림 말 '레트로 스타일 말고'·'고양이 스타일 말고' 는 그 화풍이다), 뒤가 '다른' 일 때만 거절이다 ('그림체 말고 배경만 바꿔줘'
# 는 그림체를 두고 다른 것을 바꾸라는 말이다). 앞말이 있어도 '말고' 뒤가 바꿀 다른 대상('배경만 바꿔줘')이면 거절이 아니다
_LOOK_NOUN = r"(?:그림체|화풍|그림풍|작화|스타일)"
_LOOK_NOUN_RE = re.compile(_LOOK_NOUN)
_LOOK_REF_RE = re.compile(r"(?<![가-힣A-Za-z])(?:(?:(?P<own>원래|기존|평소|정해진)|(?P<now>지금|현재|이|그))\s*(?:의\s*)?)?" + _LOOK_NOUN)
OWN_LOOK, NOW_LOOK = "own", "now"
SOME_LOOK = "?"          # 거절했는데 무엇인지 모름 (지금 화풍을 모르는 고치기 차례, 라이브러리 인물이 없는 '원래 그림체 말고'): 구성안이 고른 화풍을 믿는다
# '말고' 뒤(그 마디)가 화풍을 말하는가: 다른·딴·그림 말·화풍 말, 또는 아무것도 없이 끝남('지금 그림체 말고'·'… 말고 바꿔줘')
_REST_LOOK_RE = re.compile(r"다른|딴|그림체|화풍|그림풍|작화|스타일|느낌|룩|(?<![가-힣])풍|\b(?:look|style)\b", re.I)
_REST_ASK_RE = re.compile(r"(?:[가-힣]*\s*(?:줘|주세요|주십시오|달라)|해|하자|가자)[.!~]*")
_CLAUSE_END_RE = re.compile(r"[,.!?;\n]")
# 구절 길에서만 보는 '싫다·별로' 거절 꼴 (11-25 최종 확인: '반실사는 싫어, 애니로'·'반실사 별로, 수채화로'·'반실사 제외하고 …'·'반실사 말구 …'·
# '반실사는 안 돼, …' 를 청한 말로 읽어 사용자가 거절한 반실사가 나왔다). 거절 말 뒤의 한 낱말을 끝까지 먹는다('싫어'·'별로야'·'제외하고') —
# 뒤 마디를 읽는 _refuses_look 이 '어'·'야' 를 남은 말로 보지 않게. 요청 글 예비 길(_negated)은 넓히지 않는다
_DISLIKE = r"(?:싫|별로|제외|말구|안\s*(?:돼|되))[가-힣]*"
_PHRASE_NEG_AFTER = re.compile(r"(?:\s*(?:느낌|스타일|풍|화풍|그림체|분위기|톤|색감|질감|영상|버전))?\s*(?:은|는|이|가|을|를|도)?\s*" + _DISLIKE)
# 거절 말 (바라는 화풍 말을 뒤에서 찾는다, _wanted_words·_refusal_said·_drawn)
_NEG_WORD_RE = re.compile(r"말고|빼고|대신(?:에)?|아니라|아니고|아닌|" + _DISLIKE)


def _phrase_neg(t: str, e: int):
    """화풍 구절 t 의 e 자리부터가 거절 말인가 ('말고'·'빼고'·'대신'·'아니라' + 구절 길의 '싫어'·'별로'·'제외'·'말구'·'안 돼'): 그 match 또는 None"""
    return _KO_NEG_AFTER.match(t, e) or _PHRASE_NEG_AFTER.match(t, e)
_names_cache: dict = {}
# 거절한 화풍 대신 고를 기본 화풍의 차례와 그 화풍의 이름 (고쳐 쓰기 문구)
_REFUSE_ALT = ("anime", "toon3d", "photo")
_KEY_NAME = {"anime": "2D animation", "toon3d": "3D animation", "photo": "live action", "semi": "a semi-realistic painted look"}
# 구절 열쇠와 화풍 글 열쇠가 달라도 같은 화풍인 경우 (fixA: 2.4b 와 덮어쓰기가 열쇠가 똑같아야만 맞다고 보아, 지브리 구절의 'hand-drawn 2D animation
# with soft watercolour backgrounds'·흑백 누아르 구절의 'black-and-white film noir live action' 이 거절·덮어쓰기됐다)
_HAND_RE = re.compile(r"hand[\s-]*(?:drawn|painted|animated)|손\s*그림", re.I)
_COMIC_SAID_RE = re.compile(r"만화|웹툰|망가|manga|manhwa|webtoon|comic", re.I)
_COMIC_STYLE_RE = re.compile(r"\bcomic|graphic[\s-]*novel|manga|manhwa|webtoon", re.I)
_CGI_RE = re.compile(r"(?<![a-z])cgi?(?![a-z])", re.I)
_PHOTO_SIDE = {"film noir", "black-and-white film"}                 # 실사 영화에도 쓰는 custom 말
_DECOR = {"storybook illustration", "comic book art", "graphic novel art", "film noir", "black-and-white film", "silhouette"}   # 매체가 아닌 꾸밈 말


def said_text(v) -> str:
    """구성안의 사용자 화풍 구절(bible.style_said)을 읽는 값: 띄어쓰기를 고르고, 자리 채움 글('none'·'없음'·'N/A'·'-')은 "" (말하지 않음).
    정해진 인물의 원래 그림체를 지키라는 말뿐인 구절('원래 그림체 그대로'·'기존 그림체 유지'·'니키 그림체 그대로', 예전 경로와 같은 _KEEP_LOOK_RE)도
    "" 다 — 새 화풍을 말한 것이 아니다 (fixB: 라이브러리 화풍을 맞게 베낀 구성안을 2.4b 가 거절했다). '원래 그림체 말고' 는 거절이라 아니다"""
    t = _flat(v)
    if not t or _SAID_EMPTY_RE.fullmatch(t.strip(" \"'“”‘’.()[]")):
        return ""
    m = _KEEP_LOOK_RE.search(t)
    if m and not _phrase_neg(t, m.end()) and not _phrase_scan(t[:m.start()] + " " + t[m.end():])[0]:
        return ""
    return t


def _phrase_scan(t: str) -> tuple[list[tuple[int, int, str, str]], str, dict[str, str]]:
    """화풍 구절의 아는 화풍 말 [(시작, 끝, 'key'·'mark', custom 영어 이름 또는 "")], 뺀 말을 지운 구절, 거절한 화풍 {열쇠: 그 말}.
    구절 전체가 화풍 말이라 표지·물건은 보지 않는다. 뺀 말은 구절에서도 지운다:
    - 2D 인지 3D 인지 모르는 스튜디오 + '움직이는 그림' 말('디즈니 애니메이션처럼'·'디즈니풍 애니메이션'·'Disney-style animation')은 애매한 말
      (요청 글 경로의 maybe 와 같은 뜻, _PHRASE_AMBIG_BEFORE)
    - 거절한 말('애니메이션 말고 실사로' 의 애니메이션, 'not anime'·'anything but semi-realistic' — _negated·_PHRASE_NEG_BEFORE, fixA: 구성안이
      구절을 통째로 옮기면 거절한 화풍을 읽었다). 거절한 말의 열쇠는 따로 돌려준다 (fixB: '반실사 말고 다른 스타일로' 에서 무엇을 거절했는지 잊어
      거절한 반실사로 채웠다)
    - 색·분위기로 쓴 파스텔·코믹 ('파스텔 톤으로'·'코믹한 느낌으로', _PHRASE_TONE_AFTER)"""
    keep, masked, refused = [], t, {}
    hits = _word_hits(t)
    for s, e, cls, name in hits:
        tone = _PHRASE_TONE_AFTER.get(name)
        # '수채화 말고'·'수묵화 말고': 화풍 말 뒤의 '화' 는 그 말의 것이다 (fixD, 구절 길만 — 요청 글 예비 길의 _negated 는 넓히지 않는다)
        hwa = t[e:e + 1] == "화" and bool(_phrase_neg(t, e + 1))
        neg = _negated(t, s, e) or hwa or bool(_PHRASE_NEG_BEFORE.search(t, 0, s)) or bool(_PHRASE_NEG_AFTER.match(t, e))
        if ((cls == "key" and _MOVING_WORD.fullmatch(t[s:e]) and _PHRASE_AMBIG_BEFORE.search(t, 0, s)) or neg
                or (tone is not None and tone.match(t, e))):
            masked = masked[:s] + " " * (e - s) + masked[e:]
            if neg:
                k = said_phrase_key(t[s:e])
                if k == CUSTOM_KEY and name:
                    k = f"{CUSTOM_KEY}:{name}"          # custom 거절은 재료 이름으로 (fixD: '클레이 말고 픽셀 아트로' 의 픽셀 아트까지 거절했다)
                if k:
                    refused.setdefault(k, t[s:e + 1] if hwa else t[s:e])
            continue
        keep.append((s, e, cls, name))
    # 인물의 원래 그림체·지금 보는 화풍을 거절한 말 ('원래 그림체 말고'·'지금 스타일 말고 다른 스타일로'·'니키 그림체 말고', fixC·fixD, _look_refs):
    # 화풍 말에 붙었거나 겹친 그림 말('반실사 그림체 말고'·'만화풍 말고' 의 '화풍')은 그 화풍 말의 것이다. 구절에서 지우고(_drawn 이 거절한 '그림체' 를
    # 그림 말로 읽지 않게) OWN_LOOK·NOW_LOOK 으로 돌려준다
    for s0, e0, kind, bare in _look_refs(t):
        neg = _phrase_neg(t, e0)
        if not neg or any(s < e0 and (e > s0 or not t[e:s0].strip()) for s, e, _c, _n in hits):
            continue
        if not _refuses_look(_CLAUSE_END_RE.split(t[neg.end():], 1)[0], bare):
            continue
        masked = masked[:s0] + " " * (e0 - s0) + masked[e0:]
        refused.setdefault(kind, t[s0:e0].strip())
    return keep, masked, refused


def _names_look_re():
    """라이브러리 인물 이름 + 그림 말 ('니키 그림체'·'니키랑 푸토르 그림체'·'니키의 화풍'): 인물의 원래 그림체다 (fixD). 라이브러리가 비면 None"""
    names = tuple(sorted({_flat(n) for e in library() for n in e.get("names") or [] if _flat(n)}, key=len, reverse=True))
    if not names:
        return None
    rx = _names_cache.get(names)
    if rx is None:
        alt = "|".join(re.escape(n) for n in names)
        rx = re.compile(r"(?<![가-힣A-Za-z])(?:" + alt + r")(?:\s*(?:와|과|랑|이랑|하고|,|및)\s*(?:" + alt + r"))*\s*(?:의\s*)?" + _LOOK_NOUN, re.I)
        _names_cache.clear()
        _names_cache[names] = rx
    return rx


def _look_refs(t: str) -> list[tuple[int, int, str, bool]]:
    """구절의 인물 그림체·지금 화풍을 가리키는 말 [(시작, 끝, OWN_LOOK·NOW_LOOK, 앞말 없는 그림 말인가)] (fixD)"""
    if not _LOOK_NOUN_RE.search(t):
        return []
    out: list[tuple[int, int, str, bool]] = []
    nr = _names_look_re()
    if nr is not None:
        out += [(m.start(), m.end(), OWN_LOOK, False) for m in nr.finditer(t)]
    for m in _LOOK_REF_RE.finditer(t):
        if any(s <= m.start() < e for s, e, _k, _b in out):
            continue                     # 이름 + 그림 말의 그림 말
        bare = not (m.group("own") or m.group("now"))
        if bare and re.search(r"[가-힣A-Za-z0-9]$", t[:m.start()].rstrip()):
            continue                     # 앞 낱말이 꾸미는 그림 말 ('레트로 스타일 말고'): 그 화풍이지 지금 화풍이 아니다
        out.append((m.start(), m.end(), OWN_LOOK if m.group("own") else NOW_LOOK, bare))
    return sorted(out)


def _refuses_look(rest: str, bare: bool) -> bool:
    """그림 말 + '말고' 뒤의 마디(rest)가 화풍을 거절하는 말인가 (fixD). 앞말 없는 그림 말은 '다른'·'딴' 이 이어질 때만. 앞말이 있으면 '말고' 로
    끝나거나('지금 그림체 말고 바꿔줘') 뒤가 다른 화풍·그림 말일 때 — 바꿀 다른 대상만 오면('배경만 바꿔줘'·'대사만 바꿔줘') 그림체는 두라는 말이다"""
    r = rest.strip()
    if bare:
        return bool(re.match(r"(?:다른|딴)", r))
    if not r or _REST_ASK_RE.fullmatch(r):
        return True
    return bool(_REST_LOOK_RE.search(r) or _word_hits(r))


def _phrase_words(t: str) -> tuple[list[tuple[int, int, str, str]], str]:
    """화풍 구절의 아는 화풍 말과 뺀 말을 지운 구절 (_phrase_scan)"""
    keep, masked, _refused = _phrase_scan(t)
    return keep, masked


def _look_keys(style: str) -> list[str]:
    """화풍 글을 거절 열쇠로: 열쇠 하나, custom 은 재료 이름으로('custom:watercolour painting', 이름이 없으면 'custom'). 빈 글은 []"""
    st = _flat(style)
    if not st:
        return []
    key = style_key(st)
    if key != CUSTOM_KEY:
        return [key]
    t = _NOT_RE.sub(" ", st)
    return [f"{CUSTOM_KEY}:{n}" for n, rx in _CUSTOM_BY_NAME.items() if rx.search(t)] or [CUSTOM_KEY]


def _phrase_refused(said: str, library_style: str = "", now_style: str | None = None) -> dict[str, str]:
    """화풍 구절이 거절한 화풍 {열쇠: 그 말} ('반실사 말고 다른 스타일로' → {'semi': '반실사'}, custom 은 재료 이름 열쇠 'custom:claymation').
    - 인물의 원래 그림체를 거절한 말('원래 그림체 말고'·'니키 그림체 말고')은 라이브러리 그림체(library_style)를 거절한 것이다 (fixC)
    - 지금 보는 화풍을 거절한 말('지금 그림체 말고 다른 스타일로')은 now_style 을 거절한 것이다: None 이면 요청이 한 메시지라 라이브러리 그림체,
      글이면 고치기 차례에 앞 메시지로 만든 영상의 화풍, "" 이면 그 영상의 화풍을 모른다 (fixD)
    거절한 화풍을 모르면 SOME_LOOK 을 넣는다 — 검사와 덮어쓰기가 구성안이 고른 화풍을 믿는다 (구성안 모델은 앞 메시지를 보았다)"""
    refused = dict(_phrase_scan(said_text(said))[2])
    own, now = refused.pop(OWN_LOOK, ""), refused.pop(NOW_LOOK, "")
    for word, st in ((own, library_style), (now, library_style if now_style is None else now_style)):
        if not word:
            continue
        keys = _look_keys(st)
        for k in keys:
            refused.setdefault(k, word)
        if not keys:
            refused.setdefault(SOME_LOOK, word)
    return refused


def _refused_by(style: str, refused: dict) -> str:
    """화풍 글(style)이 거절한 화풍을 그렸으면 그 거절한 말, 아니면 "". 열쇠가 같거나, custom 거절(재료 이름)이면 그 재료 말을 담은 글 (fixD:
    custom 열쇠로만 보아 '클레이 말고 픽셀 아트로' 의 픽셀 아트 글도 거절했다)"""
    st = _flat(style)
    if not st or not refused:
        return ""
    have = style_key(st)
    if have in refused:
        return refused[have]
    t = _NOT_RE.sub(" ", st)
    for k, w in refused.items():
        if k.startswith(CUSTOM_KEY + ":") and _CUSTOM_BY_NAME[k[len(CUSTOM_KEY) + 1:]].search(t):
            return w
    return ""


def _wanted_words(sd: str) -> str:
    """거절 구절에서 마지막 거절 말 뒤에 적은 바라는 화풍 말 ('반실사 말고 고흐 그림처럼' → '고흐 그림처럼'): 그림·화가·화풍 말이 있을 때만,
    '다른 …' 은 아니다 (fixD: 열쇠가 None 인 바라는 화풍을 버리고 다른 화풍 예시를 들었다)"""
    ms = list(_NEG_WORD_RE.finditer(sd))
    if not ms:
        return ""
    rest = sd[ms[-1].end():].strip(" ,.!?~")
    if not rest or re.match(r"(?:다른|딴)", rest):
        return ""
    return rest if (_PHRASE_DRAWN_RE.search(rest) or _word_hits(rest)) else ""


def _refusal_said(sd: str) -> bool:
    """구절이 무언가를 거절하고 다른 것을 청하는가 ('이런 거 말고 다른 스타일로'): 거절한 화풍을 몰라도 style_said 를 "" 로 비우게 하지 않는다 (fixD)"""
    return bool(re.search(r"(?:말고|빼고|대신(?:에)?|아니라|아니고|" + _DISLIKE + r")\s*[^,.!?;\n]*?(?:다른|딴)", sd))


def said_phrase_key(phrase: str) -> str | None:
    """사용자의 화풍 구절(bible.style_said) → 열쇠. 구절 전체를 화풍 말로 읽는다: 표지 말이 없어도 되고 물건·활동 거르기는 하지 않는다 (구성안
    모델이 이미 했다). 거절한 말·애매한 디즈니·색으로 쓴 파스텔은 뺀다 (_phrase_words). 지금 낱말 표를 쓴다: 실사 말 → photo, 애니·웹툰·2D·지브리
    → anime, 3D·픽사·디즈니 3D·CG 애니·드림웍스 → toon3d, 반실사 → semi, 그 밖의 화풍 말(수채·유화·클레이·픽셀·도트·수묵 …) → custom.
    아는 말이 하나도 없거나 애매한 말뿐이면('고흐 그림처럼'·'디즈니 애니메이션처럼'·'다큐멘터리처럼'·'cinematic film look') None: 구성안 모델이
    요청을 읽고 정한 화풍을 믿는다 (fixB: fixA 가 화풍 글 표로 실사 영화로 읽는 영어 구절만 photo 로 보아 같은 뜻의 한국어 구절과 결과가 갈리고
    'a Disney film look' 이 실사가 됐다 — 실사 화풍 글은 None 구절의 검사가 받는다).
    여러 말의 차례는 화풍 글 열쇠(style_key)가 정한다 ('3D 클레이'·'클레이 애니메이션' 은 custom, '반실사 수채화' 는 semi).
    style_key 로는 custom 인데 custom 말이 없으면 요청 글 경로(said_key)와 같이 본다: 실사 말('realistic'·'사진 느낌') → photo,
    3D·CG·픽사 → toon3d, 손그림·카툰·지브리 → anime"""
    words, masked = _phrase_words(said_text(phrase))
    if not words:
        return None
    key = style_key(masked)
    if key != CUSTOM_KEY or _CUSTOM_RE.search(masked) or any(name for _s, _e, _c, name in words):
        return key
    if any(_photo_snip(masked[s:e]) for s, e, _c, _n in words) or _PHRASE_PHOTO_RE.search(masked):
        return "photo"
    if _TOON_HINT.search(masked):
        return "toon3d"
    if _ANIME_HINT.search(masked):
        return "anime"
    return key


def custom_style_text(text: str, phrase: bool = False) -> str:
    """글(요청)의 화풍으로 쓴 custom 말(_req_scan)을 영어 화풍 글로: 나온 차례대로 세 개까지 (예: '수채화 동화풍' → 'watercolour painting
    and storybook illustration style'). 그런 말이 없으면 빈 글 ('점토 인형'·'펠트 모자' 같은 물건 말은 옮기지 않는다, 11-24 2차 검토).
    phrase 가 참이면 text 는 구성안 모델이 옮겨 적은 사용자의 화풍 구절(bible.style_said, 11-25 3차 명세 2절)이다: 구절 전체가 화풍 말이라
    표지('풍'·'느낌' …)·물건 거르기 없이 custom 말을 모두 옮긴다 ('유화' → 'oil painting style', '도트' → 'pixel art style')"""
    if phrase:
        names = list(dict.fromkeys(name for _s, _e, _cls, name in _phrase_words(said_text(text))[0] if name))
    else:
        names = list(dict.fromkeys(name for _s, _e, _snip, name in _req_scan(text) if name))
    if "black-and-white film" in names and len(names) > 1:
        # '흑백 연필화' 의 흑백은 재료의 꾸밈말이다 (5차 검토: 'black-and-white film and pencil sketch style' 이 사진 쪽 custom 으로 읽혀 연필 그림 영화를
        # 실사처럼 다뤘다 — 기준 그림을 실사 사진으로 바꾸고 '그린 그림' 문장이 빠졌다)
        names.remove("black-and-white film")
        return "black-and-white " + " and ".join(names[:3]) + " style"
    names = names[:3]
    return (" and ".join(names) + " style") if names else ""


def ref_for(entry: dict, key: str) -> str | None:
    """인물의 그 화풍 기준 그림 경로. 없으면 None (영화 시작 때 그 화풍으로 바꿔 만든다).
    key 가 'legacy' 이면 예전 기준 그림 ref (2.5D 만화풍): 다른 열쇠에는 ref 를 대신 내주지 않는다 (화풍이 섞이므로)"""
    if not isinstance(entry, dict):
        return None
    if key == "legacy":
        return entry.get("ref") or None
    refs = entry.get("refs")
    v = refs.get(key) if isinstance(refs, dict) else None
    return v if isinstance(v, str) and v.strip() else None


def _clean_refs(e: dict) -> dict:
    """refs 는 {화풍 열쇠: 결과 폴더 안 상대 경로}만 남긴다 (틀린 칸 하나로 인물을 버리지 않는다)"""
    refs = e.get("refs")
    if refs is None:
        return e
    good = {k: v for k, v in refs.items() if isinstance(refs, dict) and k in STYLE_KEYS and isinstance(v, str)
            and v.strip() and not v.startswith("/") and ".." not in v} if isinstance(refs, dict) else {}
    if good != refs:
        log.warning("인물 라이브러리 %s 의 refs 에 형식이 틀린 칸이 있어 뺍니다", e.get("id"))
    return {**e, "refs": good}


def _valid(e) -> bool:
    return (isinstance(e, dict) and all(e.get(k) for k in REQUIRED) and isinstance(e["names"], list)
            and all(isinstance(n, str) and n.strip() for n in e["names"]) and isinstance(e["look"], str)
            and re.fullmatch(r"[a-z0-9_]{2,32}", str(e["id"])) is not None)


def library() -> list[dict]:
    """라이브러리 인물 목록. 파일이 없거나 깨졌으면 빈 목록(요청은 평소처럼 만든다). 파일 수정 시각이 바뀌면 다시 읽는다"""
    path = getattr(settings, "studio_cast_file", "") or LIB_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log.warning("인물 라이브러리를 읽지 못했습니다 (%s): %s", path, e)
        return []
    out = [e for e in (data.get("characters") or []) if _valid(e)]
    if len(out) != len(data.get("characters") or []):
        log.warning("인물 라이브러리에 형식이 틀린 항목이 있어 건너뜁니다 (%s)", path)
    out = _resolve_variants([_clean_refs(e) for e in out], path)
    _cache[path] = (mtime, out)
    return out


# 같은 사람의 다른 모습 (11-20: 제임스는 무언가에 집중할 때만 안경을 쓴다). 변형 항목은 variant_of 에 기본 인물 id 를, variant_when 에
# 그 모습을 쓸 때(영어 글)를 적고, 목소리·말투·그림체는 기본 인물 것을 이어받는다
# (기준 그림 ref·refs 와 signature 는 이어받지 않는다: 안경 쓴 얼굴은 자기 그림이 따로 있다). 이름으로는 찾지 않고 기본 인물이 나오면 따라 나온다
INHERIT = ("style", "speech", "voice", "voice_ko", "voice_fx", "voice_ref", "voice_ref_text")


def _resolve_variants(entries: list[dict], path: str = "") -> list[dict]:
    by_id = {e["id"]: e for e in entries if not e.get("variant_of")}
    out = []
    for e in entries:
        base = by_id.get(e.get("variant_of")) if e.get("variant_of") else None
        if e.get("variant_of") and base is None:
            log.warning("인물 라이브러리 변형 %s 의 기본 인물 %s 가 없어 건너뜁니다 (%s)", e["id"], e.get("variant_of"), path)
            continue
        if base is not None:
            e = {**{k: base[k] for k in INHERIT if base.get(k)}, **e}
        out.append(e)
    return out


def _name_re(name: str):
    """한글 이름은 조사가 붙으므로 부분 일치, 영문 이름은 낱말 경계로 (Niki 가 Nikita 에 걸리지 않게)"""
    n = re.escape(name.strip())
    return re.compile(n if re.search(r"[가-힣]", name) else rf"(?<![A-Za-z]){n}(?![A-Za-z])", re.I)


def known_in(text: str) -> list[dict]:
    """글에 이름이 나오는 라이브러리 인물 (글에 처음 나오는 차례)"""
    hits = []
    lib = library()
    for e in lib:
        if e.get("variant_of"):
            continue
        pos = [m.start() for n in e["names"] for m in [_name_re(n).search(text or "")] if m]
        if pos:
            hits.append((min(pos), e))
    out = []
    for _, e in sorted(hits, key=lambda h: h[0]):
        out.append(e)
        out += [v for v in lib if v.get("variant_of") == e["id"]]       # 같은 사람의 다른 모습은 기본 인물을 따라 알려 준다
    return out


def _squash(text) -> str:
    return re.sub(r"[\s()（）\[\]·.,'’\-]+", "", str(text or "").lower())


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def match_character(char: dict, entries: list[dict]) -> dict | None:
    """설정집 인물 하나와 같은 라이브러리 인물: 한글 이름(name_ko)이나 id 가 라이브러리 이름과 같거나 이름을 포함한다.
    이름도 id 도 안 맞으면(대사 없는 영상의 구성안은 name_ko 칸이 없고 모델이 id 를 'A' 로 쓸 수 있다) 외형 글이 라이브러리 외형을
    그대로 옮긴 것(거의 같은 글)인지 본다"""
    name = str(char.get("name_ko") or "").strip()
    cid = str(char.get("id") or "").strip()
    for e in entries:                                   # 1) 라이브러리 id 가 그대로, 또는 이름이 (띄어쓰기·괄호 빼고) 똑같다
        if cid and cid.lower() == e["id"].lower():
            return e
    for e in entries:
        if any((name and _squash(name) == _squash(n)) or (cid and _squash(cid) == _squash(n)) for n in e["names"]):
            return e
    best, best_len = None, 0                            # 2) 이름을 포함한다: 가장 긴 이름이 맞는 쪽 (변형 이름은 기본 인물 이름을 품는다)
    for e in entries:
        for n in e["names"]:
            if ((name and _name_re(n).search(name)) or (cid and cid.lower() == n.lower())) and len(n) > best_len:
                best, best_len = e, len(n)
    if best is not None:
        return best
    look = _norm(char.get("look"))
    if look:
        for e in entries:
            if difflib.SequenceMatcher(None, look, _norm(e["look"])).ratio() >= 0.9:
                return e
    return None


def prompt_block(entries: list[dict] | None, speech: bool = True, by_id: bool = False, text: str = "") -> str:
    """구성안 지시문 끝에 붙이는 '이미 정해진 인물' 글 (영어). 목소리는 넣지 않는다: 구성안 검사가 모델이 쓴 목소리 글을 보므로
    긴 목소리 글을 베끼게 하지 않고, 목소리는 구성안이 나온 뒤 라이브러리 값으로 바꾼다.
    speech 가 거짓이면 말투를 빼고(대사 없는 영상), by_id 가 참이면 이름 칸(name_ko) 대신 라이브러리 id 를 인물 id 로 쓰게 한다
    (대사 없는 영상의 구성안에는 name_ko 칸이 없다). text(사용자 요청 글)는 예전 호출과 맞추려고 받기만 한다.
    화풍 줄은 요청 글과 상관없이 한 가지다 (11-25 3차 명세 2.2): 요청이 화풍을 말했으면 그 말을 bible.style_said 에, 그 화풍을 bible.style 에
    쓰고, 말하지 않았으면 style_said 는 "" 이고 라이브러리 화풍을 베낀다. 화풍을 말했는지는 코드가 요청 글로 짐작하지 않고 구성안 모델이
    정한다 (11-24 에는 짐작으로 줄을 골랐다: 니키가 나오는 애니 요청에 반실사를 베끼라는 줄이 같이 가 반실사가 됐다)"""
    if not entries:
        return ""
    # 말투(speech): 11-19 메인 시험에서 모델이 푸토르를 존댓말 "선생님" 말투로 써서, 손으로 만든 때의 할아버지 말투("얘야, ~란다")와 달랐다
    given = 'with the given "id" (shown first)' if by_id else 'with the given "name_ko"'
    lines = ["\nKnown characters: these people already exist and must appear exactly as described. Put each in bible.characters "
             f'{given} and copy its look word for word; never change their age, build, hair, clothing or colours. '
             "The scenes must use only what the look lists." + (" Write each one's spoken lines in the given speech manner." if speech else "")]
    for e in entries:
        tag = f'- id "{e["id"]}" ({e["names"][0]})' if by_id else f'- name_ko "{e["names"][0]}"'
        extra = ""
        if e.get("variant_of"):
            base = next((b for b in entries if b["id"] == e["variant_of"]), None)
            base_tag = "the same person" if base is None else (f'id "{base["id"]}"' if by_id else f'name_ko "{base["names"][0]}"')
            extra = (f' This is the same person as {base_tag} in a different look: use this entry only in a scene where '
                     f'{e.get("variant_when") or "the situation fits"}, never put both entries in one scene, and use the other entry in every other scene.')
        lines.append(f'{tag}: {e["look"]}' + (f' Speech: {e["speech"]}.' if speech and e.get("speech") else "") + extra)
    style = library_style(entries)
    # fixC: 예전 줄('if the request asks for a particular look … otherwise ""')은 거절만 한 요청('반실사 말고 다른 그림체로')을 '화풍을 청하지 않음'
    # 으로 읽게 해, STORY_SYSTEM 의 '거절한 말까지 옮겨라'와 부딪히고 거절한 라이브러리 반실사를 베끼게 했다
    # fixD: 예문 '지금 그림체 말고' 는 고치기 차례에는 앞 영상의 화풍을 거절한 말이라(STORY_SYSTEM) 인물의 그림체를 거절한 예문 '원래 그림체 말고' 로 바꿨다
    head = ("Visual style of the whole film: if the request says anything about the look (asks for a look, or turns a look down, for example "
            "\"반실사 말고 다른 그림체로\" or \"원래 그림체 말고 다른 스타일로\"), write the user's words into bible.style_said and write into "
            "bible.style the look asked for, or a look you choose that is not the refused one; only when the request says nothing about the "
            "look or only keeps the characters' own look (\"원래 그림체 그대로\") is bible.style_said \"\"")
    lines.append(head + (f" and you copy this into bible.style: {style}" if style else "."))
    return "\n".join(lines)


def library_style(entries: list[dict] | None) -> str:
    """요청에 이름이 나온 라이브러리 인물(entries)의 그림체 (처음 있는 것, 없으면 ""). 지시문 화풍 줄(prompt_block)과 화풍 구절 검사가 같은 값을 본다
    (fixC: '원래 그림체 말고' 가 무엇을 거절했는지)"""
    return next((e.get("style") for e in entries or [] if isinstance(e, dict) and e.get("style")), "")


def keeps_said_style(text: str, style: str) -> bool:
    """구성안 화풍이 사용자가 말한 화풍을 이미 따랐는가 (그러면 그대로 둔다). 아니면(모델이 요청 화풍을 놓쳐 다른 화풍·실사로 썼거나 비웠다)
    apply_to_story 가 요청 화풍의 기본 글로 채운다. 11-24 3차 검토: 요청 열쇠와 구성안 열쇠가 같아야 한다 — 예전에는 실사 요청이면 어떤 글이든
    두어 실사 영화에 custom 문장이 붙고, 애니 요청에 반실사 글도 그대로 남았다.
    - 실사·애니·3D 애니·반실사 요청: 구성안 화풍이 그 열쇠
    - custom 요청: 구성안 화풍이 custom 이고 요청의 화풍 말(수채·클레이 …)이 들어 있다
    - 화풍일 수도 있는 말만 있는 요청(said_level maybe): 구성안 화풍이 실사가 아니면 둔다 (모델이 요청을 읽고 정했다). custom 이면 그림·재료·화가·
      화풍 갈래 말(_ART_WORD_RE)이 든 글만 둔다 (4차 검토: 'cinematic fantasy look' 처럼 화풍 말이 없는 글이 라이브러리 화풍을 덮었다)"""
    level = said_level(text)
    st = str(style or "").strip()
    if level is None or not st:
        return False
    if level == "maybe":
        if is_photo_style(st):
            return False
        return style_key(st) != CUSTOM_KEY or bool(_CUSTOM_RE.search(_NOT_RE.sub(" ", st)) or _ART_WORD_RE.search(_NOT_RE.sub(" ", st)))
    key, have = said_key(text), style_key(st)
    if key != CUSTOM_KEY:
        return have == key
    want = {name for _s, _e, _snip, name in _req_scan(text) if name}
    return have == CUSTOM_KEY and (not want or any(_CUSTOM_BY_NAME[n].search(_NOT_RE.sub(" ", st)) for n in want))


def said_style_fill(text: str, library_style: str) -> str:
    """요청이 화풍을 말했는데 구성안 화풍이 그 말을 따르지 않았을 때 채울 화풍 글: 애니·실사·3D 애니는 그 열쇠의 기본 글(SAID_STYLE_TEXT),
    custom 은 요청의 화풍 말을 영어로 옮긴 글(custom_style_text: 수채화 요청이 반실사 라이브러리 화풍으로 덮이지 않게, 11-24 2차),
    반실사·그림 말과 옮길 말이 없는 custom, 화풍일 수도 있는 말만 있는 요청(maybe)은 라이브러리 화풍 (11-24 검토: 애니 요청에 모델이 화풍을
    비우면 라이브러리의 반실사로 덮여 애니가 반실사가 됐다)"""
    if said_level(text) != "sure":
        return library_style
    key = said_key(text) or ""
    if key == CUSTOM_KEY:
        return custom_style_text(text) or library_style
    return SAID_STYLE_TEXT.get(key, "") or library_style


def _phrase_names(said: str) -> list[str]:
    """화풍 구절의 custom 말 영어 이름 (수채·클레이 …, 나온 차례). 거절한 말·색으로 쓴 파스텔은 뺀다 (_phrase_words)"""
    return list(dict.fromkeys(name for _s, _e, _c, name in _phrase_words(said_text(said))[0] if name))


def _key_fit(key: str, sd: str, st: str) -> bool:
    """열쇠가 있는 구절(sd)을 화풍 글(st)이 그렸는가. 열쇠가 같으면 맞다 (custom 은 구절에 아는 재료 말이 있으면 그 말을 담아야 한다).
    열쇠가 달라도 같은 화풍인 경우 (fixA — 화풍 글 열쇠의 우선순위 때문에 사용자 화풍을 그대로 담은 글의 열쇠가 바뀐다):
    - 애니 구절 + custom 글이 2D·anime·cel·manga 나 hand-drawn + 움직이는 그림 말을 담음 (배경·질감 구절의 재료 말: 'hand-drawn 2D animation with
      soft watercolour backgrounds'). '만화'·'웹툰' 구절은 코믹·그래픽 노블 글도 그 말이다. 3D 말이 있으면 아니다
    - 3D 애니 구절 + custom 글이 3D·CG 말을 담음 ('3D animated feature film with soft clay-like textures')
    - 꾸밈 custom 구절(흑백·누아르·동화·코믹 …)의 말을 모두 담은 애니·3D 애니 글('storybook-like 2D animation')
    custom 구절에 실사 글(photo)은 맞지 않는다 (fixB: fixA 가 흑백·누아르 구절의 'black-and-white … live-action film look' 을 받았지만 실사 열쇠는
    뒤 단계(story_style·실사 변환)가 화풍 원문을 버려 흑백이 사라졌다 — 'live action' 없이 쓴 'black-and-white film look' 이 custom 이고, 사진 쪽
    custom 이라 실사 영화 한 컷으로 그린다. '흑백 애니메이션' 의 실사 글도 아니다)"""
    have = style_key(st)
    t = _NOT_RE.sub(" ", st)
    if key == CUSTOM_KEY:
        want = set(_phrase_names(sd))
        if have == CUSTOM_KEY:
            return not want or any(_CUSTOM_BY_NAME[n].search(t) for n in want)
        return bool(want) and all(_CUSTOM_BY_NAME[n].search(t) for n in want) and have in ("anime", "toon3d") and want <= _DECOR
    if have == key:
        return True
    if have != CUSTOM_KEY:
        return False
    c = _CEL_COMIC_RE.sub(" ", t)
    if key == "anime":
        if _3D_RE.search(c) or _TOON3D_RE.search(c):
            return False
        return bool(_ANIME_RE.search(c) or (_HAND_RE.search(c) and _MOVING_RE.search(c))
                    or (_COMIC_SAID_RE.search(sd) and _COMIC_STYLE_RE.search(t)))
    if key == "toon3d":
        return bool(_3D_RE.search(t) or _TOON3D_RE.search(t) or _CGI_RE.search(t))
    return False


def _drawn(sd: str) -> bool:
    """열쇠가 None 인 구절이 그림·화가·애니 쪽 말인가 (_PHRASE_DRAWN_RE). 거절한 말은 빼고 본다 ('애니메이션 말고 다른 느낌으로' 는 그림 말이 아니다).
    실사 화풍 글을 받을지만 가른다. 거절 구절이면 마지막 거절 말 뒤(바라는 쪽)만 본다 (fixD: 코드가 모르는 그림 말을 거절한 '동양화풍 말고 다른
    스타일로' 가 그림 말 구절로 읽혀 실사 글을 거절했다)"""
    masked = _phrase_words(said_text(sd))[1]
    ms = list(_NEG_WORD_RE.finditer(masked))
    if ms and masked[ms[-1].end():].strip(" ,.!?~"):
        masked = masked[ms[-1].end():]
    return bool(_PHRASE_DRAWN_RE.search(masked))


def _decor_anim(said: str) -> str:
    """custom 구절이 꾸밈 말(흑백·누아르·동화·코믹 …, _DECOR)뿐이고 움직이는 그림 말이 함께 있으면 그 애니 열쇠('anime'·'toon3d'), 아니면 "" """
    names = _phrase_names(said)
    masked = _phrase_words(said_text(said))[1]
    if not names or not set(names) <= _DECOR or not (_MOVING_RE.search(masked) or _ANIME_RE.search(masked)):
        return ""
    return "toon3d" if _3D_RE.search(masked) or _TOON3D_RE.search(masked) else "anime"


def style_said_fits(said: str, style: str, library_style: str = "", now_style: str | None = None) -> bool:
    """구성안 검사 (3차 명세 2.4b, 칸끼리만 본다): bible.style 이 bible.style_said 가 말한 화풍을 그렸는가. 빈 구절(자리 채움 글 'none'·'없음'·원래
    그림체를 지키라는 말뿐인 구절도)은 늘 참, 빈 화풍 글은 틀리다 (다듬을 때 기본값 실사로 채워진다).
    - 구절이 거절한 화풍('반실사 말고 …' 의 반실사)이면 틀리다
    - 구절의 열쇠(said_phrase_key)가 있으면 _key_fit
    - 열쇠가 None 인 구절(모르는 말·애매한 말·영화 종류·분위기): 구성안 모델이 요청을 읽고 정한 화풍을 믿는다. 단
      - 실사 글은 구절이 그림·화가·애니 쪽 말(_drawn)이면 틀리다 (영화 종류 '다큐멘터리처럼'·'cinematic film look' 은 맞다)
      - 반실사 글(라이브러리 화풍을 베낀 글, 'painted'·'painterly' 로 반실사로 읽히는 글)은 틀리다: 화풍 말을 짐작하지 않고 칸끼리 본다 — 구절이
        원래 그림체를 지키라는 말이나 분위기뿐이면 style_said 를 "" 로, 다른 화풍이면 그 화풍의 재료를 적게 한다 (style_said_hint). fixB: fixA 의
        그림 말 목록(_PHRASE_DRAWN_RE)으로 가르자 목록 밖 '동양화 느낌으로'·'명화처럼' + 라이브러리 반실사가 소리 없이 반실사가 되고 '원래 그림체
        그대로' + 라이브러리 화풍은 거절되어 그림 화풍으로 바꾸게 했다. 'painted' 글('soft impressionist painted look')은 반실사로 그려져 인물이 바뀌지
        않았다. 거절한 화풍이 따로 있고('실사 말고 …') 반실사가 거절한 것이 아니면 맞다
    라이브러리 덮어쓰기(keeps_style_said)와 같은 기준이다 — 다른 것은 라이브러리 인물이 있을 때만 버리는 글(None 구절의 실사·라이브러리 같은 반실사·
    재료 없는 custom)과, 세 번 안에 못 고친 'painted' 글을 두는 것뿐이다.
    library_style 은 요청에 이름이 나온 라이브러리 인물의 그림체: 구절이 인물의 원래 그림체를 거절했으면('원래 그림체 말고') 그 화풍이 거절한
    화풍이다 (fixC, _phrase_refused). now_style 은 고치기 차례의 '지금 그림체' (앞 메시지로 만든 영상의 화풍, None 이면 한 메시지 요청, fixD).
    거절했는데 무엇인지 모르면(SOME_LOOK) 반실사 글도 믿는다. custom 거절은 재료 이름으로 본다 (_refused_by)"""
    sd, st = said_text(said), _flat(style)
    if not sd:
        return True
    if not st:
        return False
    refused = _phrase_refused(sd, library_style, now_style)
    have = style_key(st)
    if _refused_by(st, refused):
        return False
    key = said_phrase_key(sd)
    if key is not None:
        return _key_fit(key, sd, st)
    if have == "photo":
        return not _drawn(sd)
    if have == "semi":
        # 무언가를 거절한 구절이면 믿는다: 거절한 대상을 코드가 모르는 구절('동양화풍 말고 다른 스타일로')도 반실사를 거절한 것이 아니다 (fixD:
        # 예전에는 '라이브러리 화풍을 그냥 베낀 글' 로 보아 다른 화풍으로 바꾸게 했다)
        return bool(refused) or _refusal_said(sd)
    return True


def keeps_style_said(said: str, style: str, library_style: str = "", now_style: str | None = None) -> bool:
    """구성안 화풍(bible.style)이 사용자의 화풍 구절(bible.style_said)을 이미 따랐는가 (그러면 그대로 둔다, 3차 명세 2.4).
    - 구절이 거절한 화풍이면 두지 않는다
    - 구절의 열쇠가 있으면 검사와 같은 _key_fit (열쇠가 같거나, 달라도 같은 화풍)
    - 열쇠가 None 이고 구절이 반실사를 거절했으면(라이브러리 화풍이 거절한 화풍이다, fixB) 구성안이 고른 다른 화풍을 둔다 (그림 말 구절의 실사 말고)
    - 그 밖에 열쇠가 None 이면 실사도 라이브러리 같은 반실사 글(_SEMI_RE)도 아니고, custom 이면 그림·재료·화가·화풍 갈래 말(_CUSTOM_RE·_ART_WORD_RE)을
      담을 때 (예전 maybe 경로의 4차 조건, fixA: 장르·분위기 구절의 재료 없는 글 'epic high-fantasy look' 이 라이브러리 인물의 승인된 그림체를 바꿨다).
      'painted'·'painterly' 로 반실사로 읽히는 글('soft impressionist painted look')은 둔다 (fixA: 모네·고흐 구절이 라이브러리 반실사로 덮였다. 검사는
      재료를 적게 고쳐 쓰게 하고, 세 번 안에 못 고쳤을 때만 남는다)
    빈 구절(화풍을 말하지 않음)·빈 화풍 글은 두지 않는다. library_style 은 라이브러리 인물의 그림체 ('원래 그림체 말고' 가 거절한 화풍, fixC),
    now_style 은 고치기 차례의 '지금 그림체' (fixD, _phrase_refused). 거절했는데 무엇인지 모르면(SOME_LOOK) 반실사를 거절했을 때처럼 구성안이 고른
    화풍을 둔다"""
    sd, st = said_text(said), _flat(style)
    if not sd or not st:
        return False
    refused = _phrase_refused(sd, library_style, now_style)
    have = style_key(st)
    if _refused_by(st, refused):
        return False
    key = said_phrase_key(sd)
    if key is not None:
        return _key_fit(key, sd, st)
    if "semi" in refused or SOME_LOOK in refused:
        return have != "photo" or not _drawn(sd)
    t = _NOT_RE.sub(" ", st)
    scan = _phrase_scan(sd)[2]
    if have == "photo" and (OWN_LOOK in scan or NOW_LOOK in scan or (not refused and _refusal_said(sd))):
        # 지금 보는 화풍을 거절한 구절(고치기 차례에 앞 영상의 애니를 거절한 '지금 그림체 말고 다른 스타일로')과 코드가 모르는 것을 거절한 구절
        # ('동양화풍 말고 다른 스타일로'): 실사도 사용자가 청한 '다른 화풍' 이다 — 검사도 받는다 (fixD: 받은 실사 글을 덮어쓰기가 라이브러리 반실사로
        # 바꿨다). 아는 화풍 말을 거절한 구절('애니메이션 말고 다른 느낌으로')은 예전대로 라이브러리 화풍 (fixA·fixB). 반실사 글은 아래처럼 라이브러리
        # 화풍 글로 채운다 (같은 열쇠의 승인된 글)
        return not _drawn(sd)
    if have == "photo" or _SEMI_RE.search(t):
        return False
    return have != CUSTOM_KEY or bool(_CUSTOM_RE.search(t) or _ART_WORD_RE.search(t))


def style_said_hint(said: str, style: str | None = None, library_style: str = "", now_style: str | None = None) -> str:
    """2.4b 고쳐 쓰기 문구의 둘째 문장: 기대한 화풍과, 구절 쪽 실수(사용자 말 밖의 글까지 옮김)를 고치는 길 (fixA: 무엇이 틀렸는지 알려 주지 않아
    모델이 같은 글을 되풀이하거나 그림 화풍으로 바꿨다). style(구성안 화풍 글)을 주면 걸린 까닭에 맞춰 쓴다 (fixB): 거절한 화풍, 흑백·누아르 구절의
    실사 글('live action' 말 때문에 컬러 사진으로 읽힌다), 열쇠가 None 인 구절의 반실사 글(원래 그림체·분위기면 "", 다른 화풍이면 그 재료).
    거절한 화풍인데 구절이 바라는 화풍도 말했으면('반실사 말고 수채화로') 다른 화풍 예시 대신 그 화풍을 적는다 (fixC: 예시대로 고친 답이 다음
    차례에 '수채화를 써라' 로 다시 걸려 한 차례가 헛돌았다). 무엇이든 거절한 구절에는 style_said 를 "" 로 비우라고 하지 않는다 (fixC: "" 는
    라이브러리 화풍, 곧 거절한 화풍이 된다). library_style 은 '원래 그림체 말고' 가 거절한 라이브러리 그림체, now_style 은 고치기 차례의 '지금 그림체'
    (_phrase_refused). 바라는 화풍의 열쇠가 None 이어도 구절에 적은 바라는 화풍 말('반실사 말고 고흐 그림처럼')은 그대로 인용한다 (fixD, _wanted_words)"""
    sd = said_text(said)
    key = said_phrase_key(sd)
    tail = "; if bible.style_said holds more than the user's words for the look, copy only those words."
    st = _flat(style) if style is not None else None
    have = style_key(st) if st else ""
    refused = _phrase_refused(sd, library_style, now_style)
    want = {"anime": "a 2D anime look (anime, 2D, cel-shaded or hand-drawn animation)", "toon3d": "a 3D animated look",
            "photo": "a photorealistic live-action look", "semi": "a semi-realistic painted look"}.get(key or "")
    names = _phrase_names(sd) if key == CUSTOM_KEY else []
    if key == CUSTOM_KEY:
        art = "an" if names and names[0][:1].lower() in "aeiou" else "a"          # 'an oil painting look' (fixD)
        want = (f"{art} " + " and ".join(names) + " look") if names else "exactly that look"
        if _decor_anim(sd):
            want += " in animation (2D or 3D), not live action"          # '흑백 애니메이션' (fixB)
    word = _refused_by(st, refused) if st else ""
    if word:
        if key is not None and key not in refused and (key != CUSTOM_KEY or names):
            return f'The user turned down the look in "{word}" and asked for {want}: write that look in bible.style' + tail
        wanted = _wanted_words(sd) if key is None else ""
        if wanted:
            return (f'The user turned down the look in "{word}" and asked for the look in "{wanted}": write that look in bible.style and name its '
                    "medium (for example oil painting, watercolour, ink, pencil, 2D animation or 3D animation)" + tail)
        alt = ", ".join(_KEY_NAME[k] for k in (*_REFUSE_ALT, "semi") if k not in refused)
        return f'The user turned down the look in "{word}": write a different look in bible.style (for example {alt})' + tail
    if key == CUSTOM_KEY:
        if not _decor_anim(sd) and have == "photo" and set(names) & _PHOTO_SIDE:
            return (f"Write {want} in bible.style without the words live action, photorealistic or photograph, which make it read as a plain "
                    "colour photograph; it is still filmed with real people" + tail)
    elif key is None:
        if have == "semi" and _refusal_said(sd):
            # 거절한 화풍을 모르는 거절 구절('이런 거 말고 다른 스타일로'): "" 는 라이브러리 화풍이라 거절을 지우는 길이다 (fixD)
            return ("bible.style reads as the default semi-realistic painted look, but bible.style_said turns a look down: write a different look "
                    "in bible.style and name its medium (for example oil painting, watercolour, ink, pencil, 2D animation or 3D animation)" + tail)
        if have == "semi":
            return ("bible.style reads as the default semi-realistic painted look: if bible.style_said only asks to keep the characters' own look "
                    'or only gives a mood or a kind of story, set bible.style_said to ""; otherwise write that look in bible.style and name its '
                    "medium (for example oil painting, watercolour, ink, pencil, 2D animation or 3D animation)" + tail)
        if have == "photo":
            if refused:
                return ("Write a drawn or painted look other than the refused one in bible.style, not live action, because those words ask for "
                        "a drawn or painted film" + tail)
            return ("Write that drawn or painted look itself in bible.style, not live action; if those words do not ask for a drawn or painted "
                    'film (an idiom such as "그림 같은" for a pretty place), set bible.style_said to ""' + tail)
        if have:
            want = "that look"
        else:
            want = ("that drawn or painted look itself, not a photograph or a semi-realistic digital painting" if _drawn(sd) and st is None
                    else "that look")
    return f"Write {want} in bible.style" + tail


def style_said_fill(said: str, style: str, library_style: str, now_style: str | None = None) -> str:
    """구성안 화풍이 사용자의 화풍 구절을 따르지 않았을 때 채울 화풍 글 (3차 명세 2.4): 애니·실사·3D 애니는 그 열쇠의 기본 글(SAID_STYLE_TEXT),
    반실사는 라이브러리 화풍, custom 은 구절의 custom 말을 영어로 옮긴 글(custom_style_text phrase) — 옮길 말이 없으면 구성안 화풍이 custom 일 때
    그것, 아니면 라이브러리 화풍. 열쇠가 None 이면 라이브러리 화풍, 단 구절이 반실사를 거절했으면 거절하지 않은 기본 화풍(애니 먼저, fixB:
    '반실사 말고 다른 스타일로' 가 거절한 반실사로 채워졌다 — '원래 그림체 말고'·'지금 그림체 말고' 도 라이브러리 반실사를 거절한 것이다, fixC).
    빈 구절(화풍을 말하지 않음)도 라이브러리 화풍"""
    key = said_phrase_key(said) if said_text(said) else None
    if key in SAID_STYLE_TEXT:
        return SAID_STYLE_TEXT[key]
    if key == CUSTOM_KEY:
        anim = _decor_anim(said)
        if anim:
            # 꾸밈 말 + 움직이는 그림 말('흑백 애니메이션'·'누아르 애니메이션'): 꾸밈 말만 옮기면('black-and-white film style') 사진 쪽 custom 이라 실사
            # 영화로 그렸다 (fixB) — 그 애니 기본 글에 꾸밈 말을 붙인다 (검사 _key_fit 이 받는 꼴)
            return f"{SAID_STYLE_TEXT[anim]}, in {' and '.join(_phrase_names(said))} style"
        st = _flat(style)
        return custom_style_text(said, phrase=True) or (st if st and style_key(st) == CUSTOM_KEY else library_style)
    refused = _phrase_refused(said, library_style, now_style) if said_text(said) else {}
    if key is None and "semi" in refused:
        return SAID_STYLE_TEXT[next(k for k in _REFUSE_ALT if k not in refused)]
    return library_style


def apply_to_story(story: dict, entries: list[dict], text: str = "", keep_style: bool = False, now_style: str | None = None) -> dict:
    """구성안의 설정집을 라이브러리로 덮어쓴다. {설정집 인물 id: 라이브러리 id} 를 돌려준다.
    - 같은 인물: 외형(look)·목소리(voice·voice_ko)·후처리(voice_fx)·기준 그림(ref·refs)·알아볼 소품(signature)·같은 사람 표시(variant_of)와
      표시 이름을 라이브러리 값으로.
    - 그림체 (11-25 사용자 결정 "방식 바꾸기", 3차 명세 2.4): 구성안에 사용자의 화풍 구절 칸(bible.style_said)이 문자열로 있으면 그 칸만 본다.
      "" 이면 사용자가 화풍을 말하지 않았다 — 구성안이 다른 화풍을 썼어도 라이브러리 화풍(승인된 인물 그림체). 비어 있지 않으면 사용자가 말한
      화풍이다: 구성안 화풍이 그 구절을 따랐으면 그대로(keeps_style_said), 아니면 그 구절의 화풍 글(style_said_fill). 요청 글은 보지 않는다.
    - 칸이 없으면(예전에 저장된 구성안·모델이 빠뜨림) 아래 예전 경로로 요청 글(text)을 읽는다:
      요청 글이 화풍을 말했으면(said_level sure) 그 화풍을 지킨다: 구성안 화풍이 그 말을 따랐으면 그대로(keeps_said_style),
      따르지 않았으면(비었거나 애니 요청에 실사) 요청 화풍의 기본 글(said_style_fill — 애니·실사·3D 애니는 그 열쇠의 글, custom 은 요청의
      화풍 말을 영어로 옮긴 글, 반실사·그림 말은 라이브러리 화풍).
      화풍일 수도 있는 말만 있으면(maybe: '반 고흐 스타일로'·'디즈니처럼') 구성안이 쓴 화풍(실사 말고)을 두고, 없거나 실사면 라이브러리 화풍
      (11-24 3차 검토: 목록 밖 화풍을 말한 요청에서 모델이 따른 화풍이 custom 으로 읽혀 라이브러리 반실사로 덮였다).
      말하지 않았으면 지금 규칙: 모델이 그림체를 정하지 않았거나 실사로 정했으면(keep_style 이 거짓) 라이브러리의 그림체로. 구성안 화풍이
      어느 열쇠 말에도 안 맞아 custom 으로 읽히는 글이어도 라이브러리 그림체로 (11-24 2차 검토: 화풍을 말하지 않은 요청에서 모델이 쓴
      'whimsical fantasy concept art' 가 custom 이 되어 승인된 반실사 기준 그림을 바꾸고 장소 그림을 정밀 판으로 그렸다. custom 은 사용자가
      화풍을 말했을 때만 쓴다)
    keep_style 은 구성안의 그림체가 이미 사실이 아닌 화풍(애니메이션 등)이라는 뜻이다. now_style 은 고치기 차례의 '지금 그림체'(앞 메시지로 만든
    영상의 화풍, None 이면 한 메시지 요청 — fixD, _phrase_refused)"""
    matched: dict = {}
    if not entries or not isinstance(story, dict):
        return matched
    bible = story.get("bible") or {}
    for c in bible.get("characters") or []:
        e = match_character(c, entries)
        if e is None:
            continue
        c["look"] = e["look"]
        c["name_ko"] = e["names"][0] if re.search(r"[가-힣]", e["names"][0]) else (c.get("name_ko") or e["names"][0])
        c["library"] = e["id"]
        # voice_ref(+voice_ref_text): 승인된 기준 목소리 소리 파일과 그 글. 있으면 영상마다 목소리를 새로 설계하지 않고 이 소리를 복제한다
        for k in ("voice", "voice_ko", "voice_fx", "ref", "voice_ref", "voice_ref_text"):
            if e.get(k):
                c[k] = e[k]
        # refs(화풍별 기준 그림)·signature(알아볼 소품): 새 감독 코드가 읽는다. ref 는 예전 코드를 위해 그대로 둔다.
        # variant_of: 같은 사람의 다른 모습 (자리 이어 받기·자리 차례가 제임스와 안경 쓴 제임스를 한 사람으로 센다, 11-24 2차 검토)
        for k in ("refs", "signature", "variant_of"):
            if e.get(k):
                c[k] = e[k]
        matched[str(c.get("id"))] = e["id"]
    if matched:
        lib_style = next((e.get("style") for e in entries if e["id"] in matched.values() and e.get("style")), "")
        said = bible.get("style_said")
        if isinstance(said, str):
            # 새 구성안: 사용자가 말한 화풍은 구성안 모델이 옮겨 적은 칸으로만 정한다 (요청 글을 낱말로 짐작하지 않는다)
            if not said_text(said):
                style = lib_style
            else:
                style = ("" if keeps_style_said(said, bible.get("style"), lib_style, now_style)
                         else style_said_fill(said, bible.get("style"), lib_style, now_style))
        else:
            level = said_level(text)
            if level == "sure":
                style = "" if keeps_said_style(text, bible.get("style")) else said_style_fill(text, lib_style)
            elif level == "maybe":
                style = "" if keeps_said_style(text, bible.get("style")) else lib_style
            else:
                style = lib_style if not keep_style or style_key(bible.get("style")) == CUSTOM_KEY else ""
        if style:
            bible["style"] = style
    return matched
