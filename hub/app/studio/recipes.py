"""레시피: 요청마다 값을 넣는 API 형식 워크플로우 (docs/chat-studio-design.md 6절).

화면 없는 서버는 UI 형식 워크플로우를 API 형식으로 바꿀 수 없으므로, 값을 넣을 자리(inject)를 미리 적은
API 형식 JSON 을 저장소에 둔다. 이 모듈은 순수 함수만 둔다(DB·네트워크 없음):
- 읽기와 검증 V1·V2·V4·V5·V6·V7·V8·V13 (파일만 보고 하는 것, 6.4). V3·V9·V10 은 `check_files` 가 가능한 만큼 본다.
- 값 넣기 `build` (6.6)와 크기 규칙 `size_for`.
"""
import copy
import json
import math
import os
import re

RECIPE_DIR = os.path.join(os.path.dirname(__file__), "recipes")
ACTIONS = {"image", "i2v", "t2v", "edit", "music",       # music: 긴 영상의 배경음악 (ACE-Step, docs/long-video-design.md 7절)
           "voice", "voice_clone", "talk",               # 대사 영상(11-19): 목소리 설계·목소리 복제(Qwen3-TTS), 대사 소리로 입 맞춤(LTX-2.3 IA2V)
           "scene_image",                                # 대사 영상의 장면 그림 (Qwen-Image 2512, 인물 라이브러리의 반실사 회화체와 같은 계열)
           "edit_ref", "i2v_end",                        # 장면 잇기(11-20, docs/scene-chaining-design.md): 그림 여러 장 편집, 끝 키프레임 가이드가 붙은 긴 영상 클립
           "compose_ref",                                # 기준 그림으로 새로 그리기(11-20): 같은 편집 모델, 출력 크기는 빈 잠재의 width·height (앞 프레임은 그림에 넣지 않는다)
           "talk_duo",                                   # 두세 사람 대사 컷(11-24): InfiniteTalk 두 화자 모드. audio = 말하는 사람 대사(마스크 1 = 그 사람 칸), audio2 = 무음(마스크 2 = 나머지)
           "edit_photo"}                                 # 기준 그림 실사 변환 전용(11-24 메인 P1, Qwen-Image 2.1 편집). edit 와 따로 둔다: 같은 edit 면 id 정렬상
                                                         # qwen_image21… 이 qwen_image_edit 보다 앞에 와서 모든 편집을 가로챈다
# 그림 여러 장을 글 노드 image1~3 에 넣는 레시피. edit_ref 는 그림 1을 고치고, compose_ref 는 그림 1~3을 기준으로 새로 그린다
REF_ACTIONS = {"edit_ref", "compose_ref"}
# 대사 영상의 부품: 엔진을 따지지 않고 action 마다 하나를 고른다 (음악과 같다). 화질은 같은 화질 판이 있으면 그것 (pick)
PART_ACTIONS = {"music", "voice", "voice_clone", "talk", "scene_image", "talk_duo", "edit_photo"}
# 글이 영상·이미지 모델의 positive 로 가지 않는 레시피(목소리)는 부정 자리도 없다: negative_source "none"
NO_NEGATIVE_ACTIONS = {"voice", "voice_clone"}
SLOT_TYPES = {"string", "int", "float", "bool", "input_image", "input_audio", "prefix"}
SLOT_NAMES = {"prompt", "first_frame", "edit_image", "seed", "frames", "seconds", "width", "height", "check_index", "save_prefix",
              "duration", "bpm", "keyscale", "audio", "voice_instruct", "max_tokens", "ref_text", "x_vector",
              "ref_image", "ref_image2", "end_frame", "last_index", "audio2", "speaker_x", "speaker_w",
              "negative"}       # 부정 글 (11-24 2차 C5b): fixed.negative 와 같은 자리. 장면 그림 정밀 판(cfg 4)에 화풍별 부정 글을 넣는다
# 실사 변환 레시피(edit_photo)의 글 노드: 한 노드가 긍정 조건(출력 0)·부정 조건(출력 1)·입력 그림 크기의 잠재(출력 2)를 함께 낸다
PHOTO_ENCODER = "TextEncodeQwenImage21"
PHOTO_IMAGE_INPUT = "images.image_1"
AUDIO_SLOT_NAMES = ("audio", "audio2")
# 두세 사람 대사 레시피(talk_duo)의 영상 노드: 소리 자리마다 닿아야 하는 입력 (말하는 사람의 대사 = audio → 1, 무음 = audio2 → 2).
# 노드는 두 소리를 '자리 1 먼저, 자리 2 다음'으로 이어 붙여 대사가 늘 0초에서 시작한다 (11-24 메인 X9~X13). 바뀌어 이어지면 엉뚱한 사람의
# 입이 움직인다. 화면의 모든 칸이 화자 1·2 로 나뉘므로(model_multitalk 의 argmax) 마스크 2 는 마스크 1(말하는 사람 칸)의 반전이어야 한다
DUO_NODE = "WanInfiniteTalkToVideo"
DUO_AUDIO_INPUTS = {"audio": "audio_encoder_output_1", "audio2": "mode.audio_encoder_output_2"}
DUO_SPAN_SLOTS = ("speaker_x", "speaker_w")      # 말하는 사람 칸 (마스크 1 의 MaskComposite.x · 조각 SolidMask.width): talk_duo 만 쓴다
# 값이 없으면 그래프에서 빼도 되는(optional) 이미지 자리는 참고 그림뿐이다. 첫 화면·편집 대상·끝 프레임이 빠지면 그래프가 깨진다
OPTIONAL_OK_SLOTS = {"ref_image", "ref_image2"}
IMAGE_SLOT_NAMES = ("first_frame", "edit_image", "ref_image", "ref_image2", "end_frame")
ZERO_OUT = {"ConditioningZeroOut"}
SAVE_NODES = {"SaveVideo", "SaveImage", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG",
              "SaveAudio", "SaveAudioMP3", "SaveAudioOpus", "SaveAudioAdvanced"}
BAD_NODE_RE = re.compile(r"ollama|LoadVideo|LLM|OpenAI|Gemini|Anthropic", re.I)    # 4절: 허브가 GPU 차례를 따로 관리하는 일이다
USER_DIR_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")


class RecipeError(ValueError):
    """레시피 형식이 틀렸거나 넣을 값이 맞지 않음"""


# ── 읽기 ────────────────────────────────────────────────

def load_recipe_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 화질 '높음': 4스텝 가속(LoRA)을 끄면 템플릿의 기본 방식(영상 20스텝·CFG 3.5, 편집 40스텝·CFG 4)으로 돌아가고, CFG 가 1 보다
# 커서 부정 프롬프트가 실제로 적용된다. 시간이 크게 늘어(영상 4~6분 [추정]) 계획 카드를 꼭 거친다.
# Wan 은 중국어 부정 프롬프트를 쓰므로 같은 말로 "엉뚱한 물체·사람·중복 피사체" 를 더한다.
QUALITY_NEGATIVE_EXTRA = "，多余的物体，无关的人物，重复的主体，凭空出现的陌生物体"


def quality_of(recipe: dict | None) -> str:
    return "high" if recipe and recipe.get("quality") == "high" else "fast"


def quality_variant(r: dict) -> dict | None:
    """가속 스위치(fixed.turbo)가 있는 빠른 레시피에서 높음 변형을 만든다. 없으면 None"""
    turbo = (r.get("fixed") or {}).get("turbo")
    if not turbo or r.get("quality") == "high":
        return None
    v = copy.deepcopy(r)
    v["prompt"][str(turbo["node"])]["inputs"][turbo["input"]] = False
    v["fixed"]["turbo"]["value"] = False
    v["id"] = r["id"] + "_quality"
    v["quality"] = "high"
    v["quality_label"] = "높음"
    v["title"] = r["title"].split("·")[0].strip() + " · 높음(가속 끔)"
    v["derived_from"] = r["id"]
    neg = (v.get("fixed") or {}).get("negative")
    if neg:
        n = v["prompt"][str(neg["node"])]["inputs"]
        if str(n.get(neg["input"]) or "").strip():
            n[neg["input"]] = str(n[neg["input"]]).rstrip() + QUALITY_NEGATIVE_EXTRA
    d = v.get("defaults") or {}
    if r.get("action") in ("i2v", "t2v") and d.get("width"):
        # 480p 에서 1024×576 으로 (면적 약 1.45배). 720p 는 모델이 지원하지만 메인에서 다른 프로그램이 GPU 14GB 를 쓰는 때가 있어
        # 메모리 부족을 피하려고 한 단계 낮춘다 [추정]
        d.update(width=1024, height=576, max_side=1024)
        v["defaults"] = d
    gpu = int((r.get("estimate") or {}).get("gpu_seconds") or 60)
    scale = 6 if r.get("action") in ("i2v", "t2v") else 5       # 해상도까지 올리면 더 걸린다
    v["estimate"] = {"gpu_seconds": gpu * scale, "basis": "가속을 끄면 스텝이 5배, CFG>1 이라 스텝마다 계산이 2번(실행 약 10배), "
                                                     "콜드 스타트·정리는 그대로 [추정]"}
    v["limits"] = {"max_run_seconds": max(3600, int(r["limits"]["max_run_seconds"]) * 4)}
    return v


def raw_bytes(r: dict) -> bytes:
    """레시피 검증 기록의 지문용 바이트 (파생 레시피는 파일이 없으므로 내용으로 만든다)"""
    return json.dumps(r, sort_keys=True, ensure_ascii=False).encode("utf-8")


def engine_of(recipe: dict | None) -> str:
    """영상 엔진. 레시피에 engine 이 없으면 wan (소리 없는 Wan 2.2), LTX-2.5 레시피는 ltx (소리 포함)"""
    return str((recipe or {}).get("engine") or "wan")


def pick(recipe_map: dict, disabled, action: str, quality: str = "fast", engine: str = "wan") -> dict | None:
    """action·화질·엔진에 맞는 켜진 레시피 하나 (id 순서). 엔진을 말하지 않으면 wan.
    대사 영상의 부품(PART_ACTIONS: 음악·목소리·입 맞춤·장면 그림 …)은 엔진을 보지 않고 같은 화질의 판을 먼저 고른다: 높음을 찾는데 없으면
    빠른 판(예전처럼 화질을 따지지 않는다), 빠름을 찾을 때는 높음 판을 고르지 않는다 (장면 그림 정밀 판 qwen_image_2512_scene_quality 는
    감독이 반실사가 아닌 영화의 장소 그림에만 쓴다, 11-24 2차 C5b. 빠른 판이 꺼졌다고 모든 장면 그림이 40스텝이 되지 않게)"""
    fallback = None
    for rid in sorted(recipe_map):
        r = recipe_map[rid]
        if rid in disabled or r["action"] != action:
            continue
        if action in PART_ACTIONS:
            if quality_of(r) == quality:
                return r
            if quality == "high" and fallback is None:
                fallback = r
        elif quality_of(r) == quality and engine_of(r) == engine:
            return r
    return fallback


def load_all(directory: str = RECIPE_DIR) -> dict[str, dict]:
    out = _load_files(directory)
    for r in list(out.values()):
        v = quality_variant(r)
        if v is not None and v["id"] not in out:
            out[v["id"]] = v
    return out


def _load_files(directory: str) -> dict[str, dict]:
    out = {}
    if not os.path.isdir(directory):
        return out
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            r = load_recipe_file(os.path.join(directory, name))
            out[str(r.get("id") or name[:-5])] = r
    return out


def slots_of(recipe: dict, name: str) -> list[dict]:
    v = (recipe.get("inject") or {}).get(name)
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def is_link(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[1], int)


# ── 그래프 추적 (패널의 mgs_nodes.js roleDownstream 을 API 형식으로 옮긴 것) ──

def roles_of(prompt: dict, start: str, depth: int = 0, seen: frozenset = frozenset(), zero: bool = False, out: int | None = None) -> set:
    """start 노드의 출력이 닿는 입력 이름으로 positive/negative 를 모은다. 0 조건 노드를 거쳐 닿은 것은
    positive_zero/negative_zero 로 따로 센다 (Z-Image 처럼 글 노드 하나로 쓰는 구성).
    out 이 있으면 start 의 그 출력 번호에서 나간 링크만 본다: 긍정·부정 조건을 한 노드가 함께 내는 글 노드(실사 변환의
    TextEncodeQwenImage21: 출력 0 = 긍정, 1 = 부정)는 출력마다 따로 봐야 한다 (자리의 'output' 칸, 11-24 2차)"""
    roles = set()
    for nid, node in prompt.items():
        for name, val in (node.get("inputs") or {}).items():
            if is_link(val) and str(val[0]) == start and (out is None or val[1] == out):
                if name.startswith("positive"):
                    roles.add("positive_zero" if zero else "positive")
                elif name.startswith("negative"):
                    roles.add("negative_zero" if zero else "negative")
                elif depth < 8 and nid not in seen:
                    roles |= roles_of(prompt, nid, depth + 1, seen | {nid}, zero or node.get("class_type") in ZERO_OUT)
    return roles


def reaches(prompt: dict, start: str, node_class_prefix: str, input_name: str, depth: int = 0,
            seen: frozenset = frozenset()) -> bool:
    """start 의 출력이 (깊이 8 안에서) class_type 이 접두어로 시작하는 노드의 input_name 입력에 닿는가"""
    for nid, node in prompt.items():
        for name, val in (node.get("inputs") or {}).items():
            if is_link(val) and str(val[0]) == start:
                if name == input_name and str(node.get("class_type", "")).startswith(node_class_prefix):
                    return True
                if depth < 8 and nid not in seen and reaches(prompt, nid, node_class_prefix, input_name, depth + 1,
                                                             seen | {nid}):
                    return True
    return False


def reaches_input(prompt: dict, start: str, input_name: str, depth: int = 0, seen: frozenset = frozenset()) -> bool:
    for nid, node in prompt.items():
        for name, val in (node.get("inputs") or {}).items():
            if is_link(val) and str(val[0]) == start:
                if name == input_name:
                    return True
                if depth < 8 and nid not in seen and reaches_input(prompt, nid, input_name, depth + 1, seen | {nid}):
                    return True
    return False


def _reaches_at(prompt: dict, start: str, target: str, input_name: str, depth: int = 0, seen: frozenset = frozenset()) -> bool:
    """start 의 출력이 (깊이 8 안에서) 노드 target 의 input_name 입력에 닿는가. reaches 는 같은 종류의 노드 아무데나 닿으면
    참이라, 글 노드가 둘(긍정·부정)인 편집 레시피에서 한쪽만 이어져도 통과해 버린다"""
    for nid, node in prompt.items():
        for name, val in (node.get("inputs") or {}).items():
            if is_link(val) and str(val[0]) == start:
                if name == input_name and nid == target:
                    return True
                if depth < 8 and nid not in seen and _reaches_at(prompt, nid, target, input_name, depth + 1, seen | {nid}):
                    return True
    return False


def _encode_nodes(prompt: dict) -> list[str]:
    return [nid for nid, n in prompt.items() if str(n.get("class_type", "")).startswith("TextEncodeQwenImageEdit")]


# ── 검증 (파일만 보고 하는 것, 6.4) ──────────────────────

def _slot_ok(prompt: dict, slot, where: str, errs: list):
    if not isinstance(slot, dict) or not {"node", "input", "type"} <= set(slot):
        errs.append(f"{where}: node·input·type 이 필요합니다")
        return
    if slot["type"] not in SLOT_TYPES:
        errs.append(f"{where}: 알 수 없는 type {slot['type']!r}")
    node = prompt.get(str(slot["node"]))
    if node is None:
        errs.append(f"{where}: prompt 에 노드 {slot['node']} 이 없습니다")
        return
    if slot["input"] not in (node.get("inputs") or {}):
        errs.append(f"{where}: 노드 {slot['node']} 에 입력 {slot['input']} 이 없습니다")
        return
    if is_link(node["inputs"][slot["input"]]):
        errs.append(f"{where}: 노드 {slot['node']}.{slot['input']} 은 링크라 값을 넣을 수 없습니다 (리터럴이어야 합니다)")
    if slot.get("optional") and slot["type"] != "input_image":
        errs.append(f"{where}: optional 은 input_image 자리에만 쓸 수 있습니다")


def _int_value(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def slot_out(slot) -> int | None:
    """자리의 'output' 칸: 그 글 노드의 몇 번째 출력이 이 조건인가. 없거나 정수가 아니면 None (모든 출력을 본다)"""
    return _int_value(slot.get("output")) if isinstance(slot, dict) else None


def split_outputs(a, b) -> bool:
    """두 자리(긍정 글·부정 글)가 같은 글 노드의 서로 다른 입력·출력인가 (실사 변환의 TextEncodeQwenImage21: prompt → 출력 0,
    negative_prompt → 출력 1)"""
    oa, ob = slot_out(a), slot_out(b)
    return oa is not None and ob is not None and oa != ob and a.get("input") != b.get("input")


def default_negative(recipe: dict) -> str:
    """레시피 그래프에 적힌 부정 글 (inject.negative 자리, 없으면 fixed.negative 자리의 처음 값). 화풍별 부정 글이 없을 때(custom·빠른 판)
    감독이 이 글을 그대로 넣는다. 높음 판이면 빠른 판에서 만들 때 더한 말(QUALITY_NEGATIVE_EXTRA)도 들어 있다"""
    sl = slots_of(recipe, "negative")
    s0 = sl[0] if sl and isinstance(sl[0], dict) else ((recipe.get("fixed") or {}).get("negative") or {})
    node = (recipe.get("prompt") or {}).get(str(s0.get("node")), {})
    return str((node.get("inputs") or {}).get(s0.get("input")) or "")


def _photo_problems(recipe: dict, prompt: dict) -> list[str]:
    """V6 의 실사 변환 레시피(edit_photo) 검사 (11-24 메인 P1 그래프): 글 노드(TextEncodeQwenImage21) 하나에 inject.prompt 가 있고,
    샘플러 하나의 잠재가 그 노드의 셋째 출력(입력 그림 크기)이어야 한다. 크기 자리가 없어 잠재를 다른 곳에서 만들면 결과 크기가
    입력 그림과 어긋난다. 편집 대상이 그 노드의 images.image_1 에 닿는지는 V6 의 편집 대상 검사가 본다"""
    encs = [nid for nid, n in prompt.items() if n.get("class_type") == PHOTO_ENCODER]
    if len(encs) != 1:
        return [f"edit_photo 레시피는 {PHOTO_ENCODER} 노드가 하나여야 합니다 (지금 {len(encs)}개)"]
    enc, e = encs[0], []
    ps = slots_of(recipe, "prompt")
    if not (len(ps) == 1 and isinstance(ps[0], dict) and str(ps[0].get("node")) == enc and ps[0].get("input") == "prompt"):
        e.append(f"edit_photo 레시피의 inject.prompt 는 {PHOTO_ENCODER} {enc} 의 prompt 여야 합니다")
    sams = [nid for nid, n in prompt.items() if n.get("class_type") == "KSampler"]
    if len(sams) != 1:
        e.append(f"edit_photo 레시피는 KSampler 가 하나여야 합니다 (지금 {len(sams)}개)")
    for sid in sams:
        lat = (prompt[sid].get("inputs") or {}).get("latent_image")
        if not (is_link(lat) and str(lat[0]) == enc and lat[1] == 2):
            e.append(f"edit_photo 레시피의 KSampler {sid} 잠재는 {PHOTO_ENCODER} {enc} 의 셋째 출력(입력 그림 크기)이어야 합니다")
    return e


def _duo_mask_problems(recipe: dict, prompt: dict, nid: str, ins: dict) -> list[str]:
    """talk_duo 의 마스크 검사 (11-24 X11~X13 방식). 마스크 1 = 말하는 사람 칸: MaskComposite(add, y 0) 의 바탕(destination)이 생성 크기의
    빈 SolidMask(0), 조각(source)이 생성 높이의 SolidMask(1) 이고, inject.speaker_x 가 그 MaskComposite 의 x, inject.speaker_w 가 조각의 width.
    마스크 2 = InvertMask(마스크 1): 말하는 사람을 뺀 나머지 전체 (화면의 모든 칸이 화자 1·2 로 나뉘어, 칸 밖의 세 번째 사람이
    대사에 묶이지 않게). 감독은 fixed.gen_width 로 칸을 계산하므로 그 자리가 이 노드의 width 여야 한다"""
    e = []
    width, height = _int_value(ins.get("width")), _int_value(ins.get("height"))
    if not width or not height or width <= 0 or height <= 0:
        return [f"talk_duo 레시피의 {DUO_NODE} {nid} 의 width·height 는 숫자(생성 크기)여야 합니다"]
    gw = (recipe.get("fixed") or {}).get("gen_width")
    if not (isinstance(gw, dict) and str(gw.get("node")) == nid and gw.get("input") == "width"):
        e.append(f"talk_duo 레시피는 fixed.gen_width 가 {DUO_NODE} {nid} 의 width 여야 합니다 (감독이 말하는 사람 칸을 이 폭으로 계산합니다)")
    m1 = ins.get("mode.mask_1")
    comp_id = str(m1[0]) if is_link(m1) else ""
    comp = prompt.get(comp_id, {})
    cin = comp.get("inputs") or {}
    if comp.get("class_type") != "MaskComposite" or cin.get("operation") != "add" or cin.get("y") != 0:
        e.append(f"{DUO_NODE} {nid} 의 mode.mask_1 은 말하는 사람 칸을 만드는 MaskComposite(operation add, y 0)여야 합니다")
    else:
        dest = prompt.get(str(cin["destination"][0]), {}) if is_link(cin.get("destination")) else {}
        din = dest.get("inputs") or {}
        if dest.get("class_type") != "SolidMask" or din.get("value") != 0 or din.get("width") != width or din.get("height") != height:
            e.append(f"mode.mask_1 의 바탕(MaskComposite {comp_id}.destination)은 생성 크기({width}×{height})의 빈 SolidMask(value 0)여야 합니다")
        piece_id = str(cin["source"][0]) if is_link(cin.get("source")) else ""
        piece = prompt.get(piece_id, {})
        pin = piece.get("inputs") or {}
        if piece.get("class_type") != "SolidMask" or pin.get("value") != 1 or pin.get("height") != height:
            e.append(f"mode.mask_1 의 조각(MaskComposite {comp_id}.source)은 생성 높이({height})의 SolidMask(value 1)여야 합니다")
        for name, node, inp, lo, hi in (("speaker_x", comp_id, "x", 0, width - 1), ("speaker_w", piece_id, "width", 1, width)):
            sl = slots_of(recipe, name)
            s0 = sl[0] if len(sl) == 1 and isinstance(sl[0], dict) else {}
            if not (str(s0.get("node")) == node and s0.get("input") == inp and s0.get("type") == "int"):
                e.append(f"talk_duo 레시피의 inject.{name} 은 마스크 1 의 {node}.{inp} 에 넣는 int 자리여야 합니다")
            elif not (_int_value(s0.get("min")) is not None and _int_value(s0.get("max")) is not None
                      and lo <= s0["min"] <= s0["max"] <= hi):
                e.append(f"talk_duo 레시피의 inject.{name} 범위(min·max)는 {lo}~{hi} 안이어야 합니다")
    m2 = ins.get("mode.mask_2")
    inv = prompt.get(str(m2[0]), {}) if is_link(m2) else {}
    if not (inv.get("class_type") == "InvertMask" and comp_id and is_link((inv.get("inputs") or {}).get("mask"))
            and str(inv["inputs"]["mask"][0]) == comp_id):
        e.append(f"{DUO_NODE} {nid} 의 mode.mask_2 는 마스크 1({comp_id or '?'})의 InvertMask 여야 합니다 (말하는 사람을 뺀 나머지 전체)")
    return e


def _duo_problems(recipe: dict, prompt: dict) -> list[str]:
    """V6 의 두세 사람 대사 레시피(talk_duo) 검사: 영상 노드 하나(두 화자 모드), 첫 화면이 그 노드의 start_image 에,
    소리 자리 둘이 정해진 화자 입력에 (대사 audio → audio_encoder_output_1, 무음 audio2 → mode.audio_encoder_output_2, 바뀌지 않게).
    감독은 '말하는 사람 = audio = 마스크 1(speaker_x·speaker_w 칸)', '무음 = audio2 = 마스크 2(마스크 1 의 반전)' 와
    'defaults.fps 로 계산한 frames·last_index' 를 전제로 하므로 마스크 짜임(_duo_mask_problems), frames 자리가 그 노드의 length(4n+1)인지,
    CreateVideo fps 가 defaults.fps 인지도 본다"""
    e = []
    nodes = [nid for nid, n in prompt.items() if n.get("class_type") == DUO_NODE]
    if len(nodes) != 1:
        return [f"talk_duo 레시피는 {DUO_NODE} 노드가 하나여야 합니다 (지금 {len(nodes)}개)"]
    nid = nodes[0]
    ins = prompt[nid].get("inputs") or {}
    if ins.get("mode") != "two_speakers":
        e.append(f"talk_duo 레시피의 {DUO_NODE} 는 mode 가 two_speakers 여야 합니다")
    e += _duo_mask_problems(recipe, prompt, nid, ins)
    # 프레임 수: frames 자리 = 이 노드의 length, 4n+1 (감독이 recipes.frames_for 로 올림한다)
    fr = slots_of(recipe, "frames")
    f0 = fr[0] if len(fr) == 1 and isinstance(fr[0], dict) else {}
    if not (str(f0.get("node")) == nid and f0.get("input") == "length" and f0.get("step") == 4 and f0.get("offset") == 1):
        e.append(f"talk_duo 레시피의 inject.frames 는 {DUO_NODE} {nid} 의 length 에 step 4·offset 1 (4n+1) 이어야 합니다")
    # fps: 감독은 defaults.fps 로 프레임 수·잇는 프레임 번호를 계산한다. 영상 노드의 fps 가 다르면 길이·자르는 지점이 어긋난다
    fps = (recipe.get("defaults") or {}).get("fps")
    for cid, n in prompt.items():
        if n.get("class_type") == "CreateVideo" and (n.get("inputs") or {}).get("fps") != fps:
            e.append(f"talk_duo 레시피의 CreateVideo {cid} fps({(n.get('inputs') or {}).get('fps')})가 defaults.fps({fps})와 다릅니다")
    for s in slots_of(recipe, "first_frame"):
        if prompt.get(str(s.get("node")), {}).get("class_type") == "LoadImage" and not _reaches_at(prompt, str(s["node"]), nid, "start_image"):
            e.append(f"inject.first_frame 의 이미지가 {DUO_NODE} {nid} 의 start_image 에 닿지 않습니다")
    for name, inp in DUO_AUDIO_INPUTS.items():
        sl = slots_of(recipe, name)
        if not sl:
            e.append(f"talk_duo 레시피는 inject.{name} 이 필요합니다")
        other = [x for k, x in DUO_AUDIO_INPUTS.items() if k != name][0]
        for s in sl:
            node = str(s.get("node"))
            if not _reaches_at(prompt, node, nid, inp):
                e.append(f"inject.{name} 의 소리가 {DUO_NODE} {nid} 의 {inp} 에 닿지 않습니다")
            if _reaches_at(prompt, node, nid, other):
                e.append(f"inject.{name} 의 소리가 다른 화자 입력 {other} 에도 닿습니다 (왼쪽·오른쪽이 바뀝니다)")
    return e


def check_recipe(recipe: dict, profile_ids=None) -> list[dict]:
    """V1·V2·V4·V5·V6·V7·V8·V13. [{"id": "V1", "ok": bool, "level": "error"|"warn", "msg": str}]"""
    res = []

    def add(vid, errs, warns=()):
        res.append({"id": vid, "ok": not errs, "level": "error" if errs else ("warn" if warns else "ok"),
                    "msg": "; ".join(list(errs) + list(warns))})

    # V1 형식
    e = []
    for k in ("schema", "id", "version", "title", "quality_label", "action", "negative_source", "prompt", "inject",
              "outputs", "defaults", "progress", "limits"):
        if k not in recipe:
            e.append(f"필수 칸 {k} 이 없습니다")
    if recipe.get("schema") != "studio-recipe-1":
        e.append("schema 는 studio-recipe-1 이어야 합니다")
    if not re.fullmatch(r"[a-z0-9_]{3,48}", str(recipe.get("id", ""))):
        e.append("id 형식(영문 소문자·숫자·밑줄 3~48자)이 틀렸습니다")
    if not isinstance(recipe.get("version"), int) or recipe.get("version", 0) < 1:
        e.append("version 은 1 이상의 정수여야 합니다")
    if recipe.get("action") not in ACTIONS:
        e.append(f"action 은 {sorted(ACTIONS)} 중 하나여야 합니다")
    no_neg = recipe.get("action") in NO_NEGATIVE_ACTIONS
    if recipe.get("negative_source") not in (("none",) if no_neg else ("node", "zero_out")):
        e.append("negative_source 는 " + ("목소리 레시피면 none" if no_neg else "node 또는 zero_out") + " 이어야 합니다")
    mx = (recipe.get("limits") or {}).get("max_run_seconds")
    if not isinstance(mx, int) or mx <= 0:
        e.append("limits.max_run_seconds 는 0 보다 큰 정수여야 합니다")
    if recipe.get("action") in ("i2v", "i2v_end", "t2v"):
        vp = recipe.get("video_profile")
        if not vp:
            e.append("영상 레시피는 video_profile 이 필요합니다")
        elif profile_ids is not None and vp not in profile_ids:
            e.append(f"video_profile {vp!r} 을 영상 렌더러가 모릅니다")
    for name in (recipe.get("inject") or {}):
        if name not in SLOT_NAMES:
            e.append(f"inject 의 자리 이름 {name!r} 은 감독 코드와 약속한 이름이 아닙니다")
    add("V1", e)
    if e and not isinstance(recipe.get("prompt"), dict):
        return res
    prompt = recipe.get("prompt") or {}

    # V2 자리가 있음
    e = []
    for name, slots in (recipe.get("inject") or {}).items():
        for i, s in enumerate(slots if isinstance(slots, list) else [slots]):
            _slot_ok(prompt, s, f"inject.{name}[{i}]", e)
    for name, s in (recipe.get("fixed") or {}).items():
        _slot_ok(prompt, s, f"fixed.{name}", e)
        if isinstance(s, dict) and "value" in s and s.get("node") in prompt and s.get("input") in prompt[s["node"]]["inputs"]:
            if prompt[s["node"]]["inputs"][s["input"]] != s["value"]:
                e.append(f"fixed.{name}: prompt 의 값이 기대값 {s['value']!r} 과 다릅니다")
    for name, o in (recipe.get("outputs") or {}).items():
        if not isinstance(o, dict) or str(o.get("node")) not in prompt:
            e.append(f"outputs.{name}: prompt 에 노드가 없습니다")
    add("V2", e)

    # V4 긍정 자리 (목소리 레시피의 글은 말할 대사라 positive 에 닿지 않는다)
    e = []
    ps = slots_of(recipe, "prompt")
    if len(ps) != 1:
        e.append("inject.prompt 는 자리 하나여야 합니다")
    elif recipe.get("negative_source") == "none":
        pass
    else:
        roles = roles_of(prompt, str(ps[0]["node"]), out=slot_out(ps[0]))
        zero_ok = recipe.get("negative_source") == "zero_out"
        if "positive" not in roles:
            e.append(f"inject.prompt 노드 {ps[0]['node']} 가 positive 입력에 닿지 않습니다 (닿는 곳: {sorted(roles) or '없음'})")
        if "negative" in roles:
            e.append("inject.prompt 노드가 negative 입력에도 닿습니다")
        if "positive_zero" in roles:
            e.append("inject.prompt 노드가 0 조건 노드를 거쳐 positive 에 닿습니다")
        if "negative_zero" in roles and not zero_ok:
            e.append("0 조건 노드를 거쳐 negative 에 닿으려면 negative_source 가 zero_out 이어야 합니다")
    add("V4", e)

    # V5 부정 자리
    e = []
    neg = (recipe.get("fixed") or {}).get("negative")
    if recipe.get("negative_source") == "node":
        if not neg:
            e.append("negative_source=node 이면 fixed.negative 가 필요합니다")
        else:
            nr = roles_of(prompt, str(neg.get("node")), out=slot_out(neg))
            if "negative" not in nr or nr - {"negative"}:
                e.append(f"fixed.negative 노드가 negative 에만 닿아야 합니다 (닿는 곳: {sorted(nr)})")
            # 같은 노드라도 긍정·부정을 다른 입력·출력으로 내는 글 노드면 된다 (실사 변환 TextEncodeQwenImage21, 자리의 output 칸)
            if ps and str(neg.get("node")) == str(ps[0].get("node")) and not split_outputs(ps[0], neg):
                e.append("fixed.negative 와 inject.prompt 가 같은 노드입니다")
    elif recipe.get("negative_source") == "none":
        if neg:
            e.append("negative_source=none 이면 fixed.negative 가 없어야 합니다")
    elif recipe.get("negative_source") == "zero_out":
        if neg:
            e.append("negative_source=zero_out 이면 fixed.negative 가 없어야 합니다")
        elif ps:
            pn = str(ps[0]["node"])
            zeros = [nid for nid, n in prompt.items() if n.get("class_type") in ZERO_OUT
                     and any(is_link(v) and str(v[0]) == pn for v in (n.get("inputs") or {}).values())]
            if not zeros:
                e.append("프롬프트 자리 노드의 출력을 받는 0 조건 노드가 없습니다")
    # 부정 글 자리(inject.negative, 11-24 2차 C5b): 감독이 화풍별 부정 글을 넣는 자리는 fixed.negative 와 같은 노드·입력이어야
    # 위의 부정 자리 검사와 접수 직전 확인(negative_problem)이 그대로 지킨다. fixed.negative 에 value 를 두면 값 넣기가 바꾼 글을 거부한다
    negs = slots_of(recipe, "negative")
    if negs:
        n0 = negs[0] if len(negs) == 1 and isinstance(negs[0], dict) else {}
        if recipe.get("negative_source") != "node" or not neg:
            e.append("inject.negative 는 negative_source=node 이고 fixed.negative 가 있는 레시피에서만 쓸 수 있습니다")
        elif not (str(n0.get("node")) == str(neg.get("node")) and n0.get("input") == neg.get("input") and n0.get("type") == "string"):
            e.append("inject.negative 는 fixed.negative 와 같은 노드·입력의 string 자리 하나여야 합니다")
        elif "value" in neg:
            e.append("fixed.negative 에 value 가 있으면 inject.negative 로 부정 글을 바꿀 수 없습니다")
        elif not default_negative(recipe).strip():
            e.append("inject.negative 의 기본 부정 글(그래프의 글)이 비어 있습니다 (화풍별 부정 글이 없을 때 감독이 이 글을 넣습니다)")
    add("V5", e)

    # V6 첫 화면·편집 대상·참고 그림·끝 프레임 자리
    e = []
    action = recipe.get("action")
    # 새로 그리기(compose_ref)는 출력 크기를 빈 잠재의 width·height(영상 크기)로 정한다. 그림 1이 VAEEncode 로 잠재가 되면 크기가 그림 1을 따른다.
    # 디노이즈 1 이라 잠재의 내용은 남지 않는다: 그림의 사람·배치가 남는 것은 글 노드에 넣은 그림 때문이다 (11-21 정정).
    # 실사 변환(edit_photo)은 그림이 글 노드 TextEncodeQwenImage21 의 images.image_1 로 들어가고 잠재도 그 노드가 만든다 (11-24 P1)
    if action == "edit_photo":
        edit_in = [(PHOTO_ENCODER, PHOTO_IMAGE_INPUT)]
    else:
        edit_in = [("TextEncodeQwenImageEdit", "image1")] + ([] if action == "compose_ref" else [("VAEEncode", "pixels")])
    for name, cls_in in (("first_frame", [("", "start_image"), ("LTXVImgToVideoInplace", "image")]),
                         ("edit_image", edit_in)):
        for s in slots_of(recipe, name):
            n = prompt.get(str(s.get("node")), {})
            if n.get("class_type") != "LoadImage":
                e.append(f"inject.{name} 노드는 LoadImage 여야 합니다")
                continue
            # 첫 화면은 엔진마다 닿는 곳이 다르다: 하나라도 닿으면 된다. 편집 대상은 모두 닿아야 한다
            hits = [(cls, inp) for cls, inp in cls_in if reaches(prompt, str(s["node"]), cls, inp)]
            if name == "first_frame":
                if not hits:
                    e.append("inject.first_frame 의 이미지가 영상 노드(start_image 또는 LTXVImgToVideoInplace.image)에 닿지 않습니다")
            else:
                for cls, inp in cls_in:
                    if (cls, inp) not in hits:
                        e.append(f"inject.{name} 의 이미지가 {cls or '노드'} 의 {inp} 입력에 닿지 않습니다")
                if action in REF_ACTIONS:     # 그림 여러 장 편집은 긍정·부정 글 노드 모두에 닿아야 한다 (한쪽만 이어지면 두 조건이 어긋난다)
                    for tid in _encode_nodes(prompt):
                        if not _reaches_at(prompt, str(s["node"]), tid, "image1"):
                            e.append(f"inject.edit_image 의 이미지가 글 노드 {tid} 의 image1 입력에 닿지 않습니다")
                if action == "compose_ref" and reaches(prompt, str(s["node"]), "VAEEncode", "pixels"):
                    e.append("compose_ref 레시피의 그림 1이 VAEEncode 로 잠재에 들어갑니다 (출력 크기를 width·height 자리로 정해야 합니다)")
    if action == "compose_ref":       # 샘플러의 잠재는 빈 잠재여야 하고, 그 크기가 width·height 자리여야 한다 (영상 비율로 그린다)
        sams = [nid for nid, n in prompt.items() if n.get("class_type") == "KSampler"]
        if len(sams) != 1:
            e.append(f"compose_ref 레시피는 KSampler 가 하나여야 합니다 (지금 {len(sams)}개)")
        for sid in sams:
            lat = (prompt[sid].get("inputs") or {}).get("latent_image")
            lnode = prompt.get(str(lat[0]), {}) if is_link(lat) else {}
            if lnode.get("class_type") not in EMPTY_LATENTS:
                e.append(f"compose_ref 레시피의 KSampler {sid} 의 latent_image 가 빈 잠재 노드가 아닙니다 ({lnode.get('class_type')})")
            else:
                for dim in ("width", "height"):
                    if not any(str(x.get("node")) == str(lat[0]) and x.get("input") == dim for x in slots_of(recipe, dim)):
                        e.append(f"compose_ref 레시피는 빈 잠재 {lat[0]} 의 {dim} 이 inject.{dim} 이어야 합니다")
    if action in ("i2v", "i2v_end", "talk", "talk_duo") and not slots_of(recipe, "first_frame"):
        e.append(f"{action} 레시피는 inject.first_frame 이 필요합니다")
    if action == "talk_duo":
        e += _duo_problems(recipe, prompt)
    else:
        for name in ("audio2",) + DUO_SPAN_SLOTS:
            if slots_of(recipe, name):
                e.append(f"inject.{name} 는 talk_duo 레시피에서만 쓸 수 있습니다")
    if action == "edit_photo":
        e += _photo_problems(recipe, prompt)
    # 참고 그림(edit_ref·compose_ref): ref_image → image2, ref_image2 → image3, 글 노드 모두에
    for name, inp in (("ref_image", "image2"), ("ref_image2", "image3")):
        if slots_of(recipe, name) and action not in REF_ACTIONS:
            e.append(f"inject.{name} 은 edit_ref·compose_ref 레시피에서만 쓸 수 있습니다")
        if action in REF_ACTIONS and not slots_of(recipe, name):
            e.append(f"{action} 레시피는 inject.{name} 이 필요합니다")
        for s in slots_of(recipe, name):
            if prompt.get(str(s.get("node")), {}).get("class_type") != "LoadImage":
                e.append(f"inject.{name} 노드는 LoadImage 여야 합니다")
            elif action in REF_ACTIONS:
                encs = _encode_nodes(prompt)
                if not encs:
                    e.append(f"{action} 레시피에 TextEncodeQwenImageEdit 계열 글 노드가 없습니다")
                for tid in encs:
                    if not _reaches_at(prompt, str(s["node"]), tid, inp):
                        e.append(f"inject.{name} 의 이미지가 글 노드 {tid} 의 {inp} 입력에 닿지 않습니다")
    # 끝 프레임(i2v_end): 모든 LTXVAddGuide(1단계·2단계)의 image 가 end_frame 에서 LTXVPreprocess 를 거쳐 와야 한다
    ends = slots_of(recipe, "end_frame")
    if ends and action != "i2v_end":
        e.append("inject.end_frame 은 i2v_end 레시피에서만 쓸 수 있습니다")
    if action == "i2v_end" and not ends:
        e.append("i2v_end 레시피는 inject.end_frame 이 필요합니다")
    for s in ends:
        if prompt.get(str(s.get("node")), {}).get("class_type") != "LoadImage":
            e.append("inject.end_frame 노드는 LoadImage 여야 합니다")
        elif action == "i2v_end":
            guides = [nid for nid, n in prompt.items() if n.get("class_type") == "LTXVAddGuide"]
            if len(guides) < 2:
                e.append(f"i2v_end 레시피는 LTXVAddGuide 가 1단계·2단계 둘 필요합니다 (지금 {len(guides)}개)")
            for gid in guides:
                src = (prompt[gid].get("inputs") or {}).get("image")
                if not (is_link(src) and prompt.get(str(src[0]), {}).get("class_type") == "LTXVPreprocess"
                        and _reaches_at(prompt, str(s["node"]), str(src[0]), "image")):
                    e.append(f"inject.end_frame 의 이미지가 LTXVAddGuide {gid} 의 LTXVPreprocess.image 로 이어지지 않습니다")
    for name in IMAGE_SLOT_NAMES:
        for s in slots_of(recipe, name):
            if isinstance(s, dict) and s.get("optional") and name not in OPTIONAL_OK_SLOTS:
                e.append(f"inject.{name} 은 optional 일 수 없습니다 (참고 그림만 비울 수 있습니다)")
    for name in AUDIO_SLOT_NAMES:
        for s in slots_of(recipe, name):
            if prompt.get(str(s.get("node")), {}).get("class_type") != "LoadAudio":
                e.append(f"inject.{name} 노드는 LoadAudio 여야 합니다")
    if action in ("talk", "voice_clone") and not slots_of(recipe, "audio"):
        e.append(f"{action} 레시피는 inject.audio 가 필요합니다")
    if action in ("edit", "edit_ref", "compose_ref", "edit_photo") and not slots_of(recipe, "edit_image"):
        e.append(f"{action} 레시피는 inject.edit_image 가 필요합니다")
    add("V6", e)

    # V7 저장 노드
    e = []
    main = (recipe.get("outputs") or {}).get("main") or {}
    mn = prompt.get(str(main.get("node")), {})
    if mn.get("class_type") not in SAVE_NODES:
        e.append(f"outputs.main 노드가 저장 노드가 아닙니다 ({mn.get('class_type')})")
    prefixes = slots_of(recipe, "save_prefix")
    pnodes = {str(s["node"]) for s in prefixes}
    saves = {nid for nid, n in prompt.items() if n.get("class_type") in SAVE_NODES}
    if saves - pnodes:
        e.append(f"저장 노드 {sorted(saves - pnodes)} 의 filename_prefix 가 inject.save_prefix 에 없습니다")
    for s in prefixes:
        if s.get("type") != "prefix" or "/" in str(s.get("suffix", "")) or not s.get("suffix"):
            e.append("save_prefix 자리는 type=prefix 이고 suffix 에 / 가 없어야 합니다")
    add("V7", e)

    # V8 시드
    e, w = [], []
    seeds = {(str(s["node"]), s["input"]) for s in slots_of(recipe, "seed")}
    for nid, n in prompt.items():
        ins = n.get("inputs") or {}
        for k in ("noise_seed", "seed"):
            if k in ins and not is_link(ins[k]) and str(ins.get("add_noise", "enable")) != "disable" and (nid, k) not in seeds:
                w.append(f"노이즈를 넣는 노드 {nid}.{k} 가 inject.seed 에 없습니다")
    add("V8", e, w)

    # V9 금지 노드 (파일만 보고)
    e = []
    loads = {str(s["node"]) for name in ("first_frame", "edit_image", "audio", "audio2", "ref_image", "ref_image2", "end_frame")
             for s in slots_of(recipe, name)}
    for nid, n in prompt.items():
        ct = str(n.get("class_type", ""))
        if BAD_NODE_RE.search(ct):
            e.append(f"노드 {nid}({ct}) 는 채팅 레시피에 쓸 수 없습니다")
        if ct in ("LoadImage", "LoadAudio") and nid not in loads:
            e.append(f"노드 {nid}({ct}) 가 inject 자리가 아닙니다 (예시 파일이 남아 실행이 실패합니다)")
    add("V9", e)

    # V13 진행 띠
    e = []
    bands = (recipe.get("progress") or {}).get("bands") or []
    if not bands:
        e.append("progress.bands 가 없습니다")
    seen_nodes = set()
    for b in bands:
        if not (b.get("key") and b.get("label") and isinstance(b.get("weight"), (int, float)) and b["weight"] > 0):
            e.append(f"띠 {b.get('key')!r}: key·label·weight(>0) 가 필요합니다")
        for nid in b.get("nodes") or []:
            if str(nid) not in prompt:
                e.append(f"띠 {b.get('key')}: 노드 {nid} 가 prompt 에 없습니다")
            seen_nodes.add(str(nid))
    samplers = [nid for nid, n in prompt.items() if "Sampler" in str(n.get("class_type", "")) and "Select" not in str(n.get("class_type"))]
    sband = {str(nid) for b in bands if b.get("sampler") for nid in b.get("nodes") or []}
    for nid in samplers:
        if nid not in sband:
            e.append(f"샘플러 노드 {nid} 가 sampler 띠에 없습니다")
    add("V13", e)
    return res


def recipe_ok(report: list[dict]) -> bool:
    return all(r["ok"] for r in report)


# ── 값 넣기 (6.6) ──────────────────────────────────────

def user_folder(username: str, user_id: int) -> str:
    return username if USER_DIR_RE.match(str(username or "")) else f"u{user_id}"


def prefix_value(slot: dict, username: str, user_id: int, turn_id: str) -> str:
    return f"{user_folder(username, user_id)}/{str(turn_id)[:8]}/{slot['suffix']}"


def check_value(slot: dict, value, input_dir: str | None = None):
    t = slot["type"]
    if t == "string":
        if not isinstance(value, str) or not value.strip():
            raise RecipeError("글이 비어 있습니다")
        if len(value) > int(slot.get("max_len") or 8000):
            raise RecipeError("글이 너무 깁니다")
        return value
    if t in ("int", "float"):
        ok = isinstance(value, int) and not isinstance(value, bool) if t == "int" else isinstance(value, (int, float)) and not isinstance(value, bool)
        if not ok:
            raise RecipeError(f"{slot['input']} 은 {'정수' if t == 'int' else '숫자'}여야 합니다")
        if "min" in slot and value < slot["min"] or "max" in slot and value > slot["max"]:
            raise RecipeError(f"{slot['input']} 의 범위({slot.get('min')}~{slot.get('max')})를 벗어났습니다")
        if t == "int" and slot.get("step") and (value - int(slot.get("offset") or 0)) % int(slot["step"]):
            raise RecipeError(f"{slot['input']} 은 {slot['step']} 단위(오프셋 {slot.get('offset', 0)})여야 합니다")
        return value
    if t == "bool":
        if not isinstance(value, bool):
            raise RecipeError(f"{slot['input']} 은 참/거짓이어야 합니다")
        return value
    if t in ("input_image", "input_audio"):
        what = "이미지" if t == "input_image" else "소리"
        if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.replace("\\", "/").split("/"):
            raise RecipeError(f"입력 {what} 경로가 올바르지 않습니다")
        if input_dir is not None and not os.path.isfile(os.path.join(input_dir, value)):
            raise RecipeError(f"입력 {what} 파일이 없습니다")
        return value
    if t == "prefix":
        if not isinstance(value, str) or ".." in value or value.startswith("/") or "\\" in value:
            raise RecipeError("저장 이름이 올바르지 않습니다")
        return value
    raise RecipeError(f"알 수 없는 type {t}")


def prune_load_image(prompt: dict, node_id: str):
    """값이 없는 optional 이미지 자리: LoadImage 노드와 그 노드로 가는 링크 입력을 모두 지운다.
    지워지는 입력은 참고 그림처럼 '없어도 되는' 입력이어야 한다 (image2·image3 이 비면 편집 노드는 그림 한 장으로 돈다)"""
    nid = str(node_id)
    prompt.pop(nid, None)
    for node in prompt.values():
        ins = node.get("inputs") or {}
        for k in [k for k, v in ins.items() if is_link(v) and str(v[0]) == nid]:
            del ins[k]


def build(recipe: dict, values: dict, input_dir: str | None = None) -> dict:
    """레시피의 prompt 를 복사해 값을 넣는다. submit_comfy 가 dict 를 그 자리에서 바꾸므로 복사본을 준다.
    optional 자리(참고 그림)는 값 키가 없거나 값이 None 이면 비운 것으로 보고 그래프에서 뺀다. 필수 자리가 비면 RecipeError."""
    prompt = copy.deepcopy(recipe["prompt"])
    for name, slots in recipe["inject"].items():
        sl = slots if isinstance(slots, list) else [slots]
        if name not in values:
            if not all(s.get("optional") for s in sl):
                raise RecipeError(f"값 {name} 이 없습니다")
            vals = [None] * len(sl) if isinstance(slots, list) else None
        else:
            vals = values[name]
        if isinstance(slots, list) and not isinstance(vals, list):
            raise RecipeError(f"{name} 은 자리마다 값 목록이어야 합니다")
        for i, slot in enumerate(sl):
            v = vals[i] if isinstance(slots, list) else vals
            if v is None and slot.get("optional"):
                prune_load_image(prompt, slot["node"])
                continue
            prompt[str(slot["node"])]["inputs"][slot["input"]] = check_value(slot, v, input_dir)
    for key, slot in (recipe.get("fixed") or {}).items():
        if "value" in slot and prompt[str(slot["node"])]["inputs"][slot["input"]] != slot["value"]:
            raise RecipeError(f"fixed.{key} 값이 레시피와 다릅니다")
    return prompt


def frames_for(recipe: dict, seconds: float) -> int:
    """seconds 초를 담는 프레임 수 (defaults.fps). frames 자리에 단위(step·offset, Wan 은 4n+1)가 있으면 그 단위로 올림한다.
    fps 16·24 는 초 × fps + 1 이 늘 4n+1 이지만 25fps(InfiniteTalk, 11-24)는 아닐 때가 있어 그대로 넣으면 build 가 거부한다"""
    fps = int((recipe.get("defaults") or {}).get("fps") or 24)
    n = int(math.ceil(float(seconds) * fps - 1e-6)) + 1
    slot = (recipe.get("inject") or {}).get("frames")
    if isinstance(slot, dict) and slot.get("step"):
        step, off = int(slot["step"]), int(slot.get("offset") or 0)
        n += (-(n - off)) % step
    return n


def size_for(w0: int, h0: int, area: int = 640 * 640, step: int = 16, max_side: int = 1024) -> tuple[int, int]:
    """사진 비율을 지키며 면적이 area 에 가깝고 step 의 배수인 (너비, 높이)"""
    ar = min(max(w0 / h0, 0.25), 4.0)
    w, h = math.sqrt(area * ar), math.sqrt(area / ar)
    if max(w, h) > max_side:
        k = max_side / max(w, h)
        w, h = w * k, h * k
    return max(step, round(w / step) * step), max(step, round(h / step) * step)


ASPECTS = {"portrait": (9, 16), "landscape": (16, 9), "square": (1, 1)}


def default_size(recipe: dict, photo: tuple[int, int] | None = None, aspect: str | None = None) -> tuple[int, int]:
    d = recipe["defaults"]
    if d.get("size_rule") == "fixed":
        return int(d["width"]), int(d["height"])
    area = int(d["width"]) * int(d["height"])
    if photo:
        return size_for(photo[0], photo[1], area, int(d.get("size_step", 16)), int(d.get("max_side", 1024)))
    if aspect in ASPECTS:
        a, b = ASPECTS[aspect]
        return size_for(a, b, area, int(d.get("size_step", 16)), int(d.get("max_side", 1024)))
    return int(d["width"]), int(d["height"])


# ── 편집 공간이 필요한 검사 V11 (6.4)와 표본 값 V12 ──

SAMPLE_IMAGE = "studio/_check/sample.png"
SAMPLE_AUDIO = "studio/_check/sample.wav"
EMPTY_LATENTS = {"EmptyLatentImage", "EmptySD3LatentImage", "EmptyHunyuanLatentVideo", "EmptyFlux2LatentImage"}


def sample_values(recipe: dict, frames: int | None = None) -> dict:
    """V12 에 쓰는 표본 값. 검증만 하고 실행하지 않으므로 저장 파일은 생기지 않는다 (6.4)"""
    d = recipe.get("defaults") or {}
    f = frames or int(d.get("frames") or 0) or 1
    inj = recipe.get("inject") or {}
    gen_w = ((recipe.get("fixed") or {}).get("gen_width") or {}).get("value")
    gen_w = gen_w if isinstance(gen_w, int) and not isinstance(gen_w, bool) and gen_w > 1 else 848
    vals = {"prompt": "a calm test scene", "seed": 0, "width": int(d.get("width") or 640),
            "height": int(d.get("height") or 640), "frames": f, "check_index": max(0, min(f // 2, 80)),
            "first_frame": SAMPLE_IMAGE, "edit_image": SAMPLE_IMAGE, "ref_image": SAMPLE_IMAGE, "ref_image2": SAMPLE_IMAGE,
            "end_frame": SAMPLE_IMAGE, "last_index": max(0, min(f * 3 // 4, 480)),
            "seconds": max(2, min(int(round(max(f - 1, 1) / int(d.get("fps") or 24))), 20)),
            "bpm": 90, "keyscale": "C major", "audio": SAMPLE_AUDIO, "audio2": SAMPLE_AUDIO, "voice_instruct": "A calm adult voice.",
            "max_tokens": 512, "ref_text": "안녕하세요.", "x_vector": False,
            "speaker_x": 0, "speaker_w": gen_w // 2,           # 두세 사람 대사 컷: 두 사람의 왼쪽 사람이 말하는 칸
            "negative": default_negative(recipe) or "low quality, blurry"}    # 부정 글 자리: 레시피에 적힌 기본 부정 글
    vals["save_prefix"] = [f"_check/00000000/{s['suffix']}" for s in slots_of(recipe, "save_prefix")]
    if "duration" in inj:      # 음악 길이는 자리가 둘(빈 잠재·글 인코더)이라 자리마다 같은 값
        vals["duration"] = [float(d.get("duration") or 30)] * len(slots_of(recipe, "duration"))
    return {k: v for k, v in vals.items() if k in inj}


# Export (API) 파일에 들어 있지만 ComfyUI 화면에서만 쓰고 백엔드는 모르는 입력 (SaveVideo 의 video-preview 등).
# 메인에서 내보낸 Wan 레시피가 이 입력을 가진 채로 실제 영상을 만들었다
FRONTEND_ONLY_INPUTS = {"video-preview", "audioUI", "upload", "videoUI"}


def check_object_info(prompt: dict, object_info: dict, accept_missing: bool = True) -> tuple[list, list]:
    """V11: 모든 class_type 이 /object_info 에 있고 입력 이름이 있는가. (오류, 경고).
    편집 공간은 CPU 로 떠서 CUDA 가 필요한 노드가 '없는 노드'로 나온다 → accept_missing 이면 경고로 남기고 입력 검사는 건너뛴다.
    점 이름(format.codec)은 첫 마디가 입력에 있는지만 본다(동적 콤보 안쪽 검사는 하지 않는다 [추정]). API 노드는 거부한다."""
    errs, warns = [], []
    for nid, node in prompt.items():
        ct = node.get("class_type")
        info = object_info.get(ct)
        if info is None:
            (warns if accept_missing else errs).append(
                f"노드 {nid}: 편집 공간에 {ct} 가 없습니다" + (" (실행 공간에서 확인됩니다)" if accept_missing else ""))
            continue
        if info.get("api_node"):
            errs.append(f"노드 {nid}({ct}) 는 API 노드라 채팅 레시피에 쓸 수 없습니다")
            continue
        inputs = info.get("input") or {}
        known = set((inputs.get("required") or {})) | set((inputs.get("optional") or {}))
        for name in (node.get("inputs") or {}):
            if name in FRONTEND_ONLY_INPUTS:
                continue
            if name.split(".")[0] not in known:
                errs.append(f"노드 {nid}({ct}): 입력 {name} 을 모릅니다")
    return errs, warns


# ── Export (API) 파일에서 레시피 뼈대 만들기 (CLI studio-import, 6.7) ──

def _src_of(prompt: dict, nid: str) -> list[tuple[str, str]]:
    """nid 노드가 받는 링크 [(입력 이름, 원본 노드 id)]"""
    return [(k, str(v[0])) for k, v in (prompt[nid].get("inputs") or {}).items() if is_link(v)]


def _downstream(prompt: dict, starts: set[str]) -> set[str]:
    seen, stack = set(), list(starts)
    while stack:
        cur = stack.pop()
        for nid in prompt:
            if nid not in seen and any(src == cur for _k, src in _src_of(prompt, nid)):
                seen.add(nid)
                stack.append(nid)
    return seen


def skeleton_from_api(api: dict, recipe_id: str, action: str, title: str | None = None) -> tuple[dict, list[str]]:
    """Export (API) JSON 에서 값을 넣을 자리를 찾아 레시피 뼈대를 만든다. (레시피, 사람이 확인할 목록).
    자동으로 못 찾는 것은 확인 목록에 남긴다. 만든 뒤 check_recipe 로 검사하고 사람이 눈으로 확인해야 한다."""
    prompt = copy.deepcopy(api)
    todo: list[str] = []
    inject: dict = {}
    fixed: dict = {}
    texts = [nid for nid, n in prompt.items() if n.get("class_type") in (
        "CLIPTextEncode", "TextEncodeQwenImageEditPlus", "TextEncodeQwenImageEdit")]
    pos = [nid for nid in texts if "positive" in roles_of(prompt, nid)]
    neg = [nid for nid in texts if "negative" in roles_of(prompt, nid) and "positive" not in roles_of(prompt, nid)]
    zero_out = bool(pos) and "negative_zero" in roles_of(prompt, pos[0])
    key = "text" if all("text" in prompt[n]["inputs"] for n in pos) else "prompt"
    if len(pos) == 1:
        inject["prompt"] = {"node": pos[0], "input": key, "type": "string", "role": "positive", "max_len": 8000}
    else:
        todo.append(f"긍정 글 노드를 하나로 정하지 못했습니다 (후보 {pos})")
    if zero_out:
        negative_source = "zero_out"
    else:
        negative_source = "node"
        if len(neg) == 1:
            fixed["negative"] = {"node": neg[0], "input": key, "type": "string", "role": "negative"}
        else:
            todo.append(f"부정 글 노드를 하나로 정하지 못했습니다 (후보 {neg})")
    seeds = [(nid, k) for nid, n in prompt.items() for k in ("noise_seed", "seed")
             if k in (n.get("inputs") or {}) and not is_link(n["inputs"][k])
             and str(n["inputs"].get("add_noise", "enable")) != "disable" and "Sampler" in str(n.get("class_type"))]
    if len(seeds) == 1:
        inject["seed"] = {"node": seeds[0][0], "input": seeds[0][1], "type": "int", "min": 0, "max": 281474976710655}
    else:
        todo.append(f"시드 자리를 하나로 정하지 못했습니다 (후보 {seeds})")
    sizes = [nid for nid, n in prompt.items() if n.get("class_type") in EMPTY_LATENTS | {"WanImageToVideo"}
             and all(k in n["inputs"] and not is_link(n["inputs"][k]) for k in ("width", "height"))]
    defaults = {"size_rule": "keep_aspect_area", "size_step": 16, "max_side": 1024, "fps": 16}
    if len(sizes) == 1:
        n = prompt[sizes[0]]["inputs"]
        inject["width"] = {"node": sizes[0], "input": "width", "type": "int", "min": 256, "max": 1536, "step": 16}
        inject["height"] = {"node": sizes[0], "input": "height", "type": "int", "min": 256, "max": 1536, "step": 16}
        defaults.update(width=int(n["width"]), height=int(n["height"]))
        if action in ("i2v", "t2v"):
            if "length" in n and not is_link(n["length"]):
                inject["frames"] = {"node": sizes[0], "input": "length", "type": "int", "min": 5, "max": 81, "step": 4,
                                    "offset": 1}
                defaults["frames"] = int(n["length"])
            else:
                todo.append("프레임 수(length)가 링크라 자리를 못 만들었습니다. 수식 노드를 지우고 숫자로 바꾸세요")
    else:
        todo.append(f"크기 노드를 하나로 정하지 못했습니다 (후보 {sizes})")
    loads = [nid for nid, n in prompt.items() if n.get("class_type") == "LoadImage"]
    if action in ("i2v", "edit"):
        slot = "first_frame" if action == "i2v" else "edit_image"
        if len(loads) == 1:
            inject[slot] = {"node": loads[0], "input": "image", "type": "input_image"}
            prompt[loads[0]]["inputs"]["image"] = SAMPLE_IMAGE
        else:
            todo.append(f"LoadImage 를 하나로 정하지 못했습니다 (후보 {loads}). 편집 대상 한 장만 남기세요")
    saves = [nid for nid, n in prompt.items() if n.get("class_type") in SAVE_NODES]
    videos = [n for n in saves if prompt[n]["class_type"] == "SaveVideo"]
    outputs: dict = {}
    main = videos[0] if len(videos) == 1 else (saves[0] if len(saves) == 1 else None)
    if main is not None:
        kind = "video" if prompt[main]["class_type"] == "SaveVideo" else "image"
        slots = [{"node": main, "input": "filename_prefix", "type": "prefix", "suffix": "video" if kind == "video" else "image"}]
        outputs = {"main": {"node": main, "kind": kind, "ext": [".mp4", ".webm"] if kind == "video" else [".png"]}}
        extra = [n for n in saves if n != main]
        for n in extra:       # 영상 레시피의 검사 프레임 저장 노드 (이미지 저장 노드가 하나일 때만 outputs.check 로 잡는다)
            slots.append({"node": n, "input": "filename_prefix", "type": "prefix", "suffix": "check_frame"})
        if len(extra) == 1 and prompt[extra[0]]["class_type"] == "SaveImage":
            outputs["check"] = {"node": extra[0], "kind": "image", "ext": [".png"]}
        elif extra:
            todo.append(f"저장 노드가 더 있습니다 ({extra}). 검사 프레임으로 쓸 하나를 outputs.check 에 정하세요")
        inject["save_prefix"] = slots
    else:
        todo.append(f"저장 노드를 하나로 정하지 못했습니다 (후보 {saves}). main 저장 노드를 정하세요")
    samplers = [nid for nid, n in prompt.items()
                if "Sampler" in str(n.get("class_type")) and "Select" not in str(n.get("class_type"))]
    after = _downstream(prompt, set(samplers)) - set(samplers)
    bands = [{"key": "load", "label": "준비", "weight": 10, "nodes": [n for n in prompt if n not in set(samplers) | after]}]
    each = 80 // max(1, len(samplers))
    for i, nid in enumerate(samplers):
        bands.append({"key": f"gen{i + 1}", "label": f"생성 {i + 1}/{len(samplers)}" if len(samplers) > 1 else "생성",
                      "weight": each, "nodes": [nid], "sampler": True})
    bands.append({"key": "finish", "label": "마무리", "weight": 10, "nodes": sorted(after)})
    todo += ["limits.max_run_seconds 와 estimate 는 [추정] 값입니다. 메인에서 재서 고치세요",
             "4스텝 LoRA 같은 빠른 설정은 fixed 에 넣고 prompt 값을 맞추세요 (예: Wan 은 fixed.turbo)"]
    if action in ("i2v", "t2v"):
        todo.append("video_profile 을 정하세요 (wan22_i2v_long 등)")
    recipe = {"schema": "studio-recipe-1", "id": recipe_id, "version": 1, "title": title or recipe_id,
              "quality_label": "빠름", "action": action, "negative_source": negative_source,
              "source": {"imported": True, "exported": "studio-import 가 Export (API) 파일에서 만든 뼈대"},
              "prompt": prompt, "inject": inject, "fixed": fixed, "outputs": outputs, "defaults": defaults,
              "progress": {"bands": bands}, "inspect": {"output": "main", "when": "after_delivery"},
              "estimate": {"gpu_seconds": 60, "basis": "[추정]"}, "limits": {"max_run_seconds": 600}}
    if action in ("i2v", "t2v"):
        recipe["video_profile"] = "wan22_i2v_long" if action == "i2v" else "wan22_t2v_long"
    return recipe, todo
