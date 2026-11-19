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
           "scene_image"}                                # 대사 영상의 장면 그림 (Qwen-Image 2512, 인물 라이브러리의 반실사 회화체와 같은 계열)
# 대사 영상의 부품: 화질·엔진을 따지지 않고 action 마다 하나를 고른다 (음악과 같다)
PART_ACTIONS = {"music", "voice", "voice_clone", "talk", "scene_image"}
# 글이 영상·이미지 모델의 positive 로 가지 않는 레시피(목소리)는 부정 자리도 없다: negative_source "none"
NO_NEGATIVE_ACTIONS = {"voice", "voice_clone"}
SLOT_TYPES = {"string", "int", "float", "bool", "input_image", "input_audio", "prefix"}
SLOT_NAMES = {"prompt", "first_frame", "edit_image", "seed", "frames", "seconds", "width", "height", "check_index", "save_prefix",
              "duration", "bpm", "keyscale", "audio", "voice_instruct", "max_tokens", "ref_text", "x_vector"}
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
    """action·화질·엔진에 맞는 켜진 레시피 하나 (id 순서). 엔진을 말하지 않으면 wan. 음악은 엔진·화질을 보지 않는다"""
    for rid in sorted(recipe_map):
        r = recipe_map[rid]
        if rid in disabled or r["action"] != action:
            continue
        if action in PART_ACTIONS or (quality_of(r) == quality and engine_of(r) == engine):
            return r
    return None


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

def roles_of(prompt: dict, start: str, depth: int = 0, seen: frozenset = frozenset(), zero: bool = False) -> set:
    """start 노드의 출력이 닿는 입력 이름으로 positive/negative 를 모은다. 0 조건 노드를 거쳐 닿은 것은
    positive_zero/negative_zero 로 따로 센다 (Z-Image 처럼 글 노드 하나로 쓰는 구성)."""
    roles = set()
    for nid, node in prompt.items():
        for name, val in (node.get("inputs") or {}).items():
            if is_link(val) and str(val[0]) == start:
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
    if recipe.get("action") in ("i2v", "t2v"):
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
        roles = roles_of(prompt, str(ps[0]["node"]))
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
            nr = roles_of(prompt, str(neg.get("node")))
            if "negative" not in nr or nr - {"negative"}:
                e.append(f"fixed.negative 노드가 negative 에만 닿아야 합니다 (닿는 곳: {sorted(nr)})")
            if ps and str(neg.get("node")) == str(ps[0].get("node")):
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
    add("V5", e)

    # V6 첫 화면·편집 대상 자리
    e = []
    for name, cls_in in (("first_frame", [("", "start_image"), ("LTXVImgToVideoInplace", "image")]),
                         ("edit_image", [("TextEncodeQwenImageEdit", "image1"), ("VAEEncode", "pixels")])):
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
    if recipe.get("action") in ("i2v", "talk") and not slots_of(recipe, "first_frame"):
        e.append(f"{recipe.get('action')} 레시피는 inject.first_frame 이 필요합니다")
    for s in slots_of(recipe, "audio"):
        if prompt.get(str(s.get("node")), {}).get("class_type") != "LoadAudio":
            e.append("inject.audio 노드는 LoadAudio 여야 합니다")
    if recipe.get("action") in ("talk", "voice_clone") and not slots_of(recipe, "audio"):
        e.append(f"{recipe.get('action')} 레시피는 inject.audio 가 필요합니다")
    if recipe.get("action") == "edit" and not slots_of(recipe, "edit_image"):
        e.append("edit 레시피는 inject.edit_image 가 필요합니다")
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
    loads = {str(s["node"]) for name in ("first_frame", "edit_image", "audio") for s in slots_of(recipe, name)}
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


def build(recipe: dict, values: dict, input_dir: str | None = None) -> dict:
    """레시피의 prompt 를 복사해 값을 넣는다. submit_comfy 가 dict 를 그 자리에서 바꾸므로 복사본을 준다."""
    prompt = copy.deepcopy(recipe["prompt"])
    for name, slots in recipe["inject"].items():
        if name not in values:
            raise RecipeError(f"값 {name} 이 없습니다")
        vals = values[name]
        sl = slots if isinstance(slots, list) else [slots]
        if isinstance(slots, list) and not isinstance(vals, list):
            raise RecipeError(f"{name} 은 자리마다 값 목록이어야 합니다")
        for i, slot in enumerate(sl):
            v = vals[i] if isinstance(slots, list) else vals
            prompt[str(slot["node"])]["inputs"][slot["input"]] = check_value(slot, v, input_dir)
    for key, slot in (recipe.get("fixed") or {}).items():
        if "value" in slot and prompt[str(slot["node"])]["inputs"][slot["input"]] != slot["value"]:
            raise RecipeError(f"fixed.{key} 값이 레시피와 다릅니다")
    return prompt


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
    vals = {"prompt": "a calm test scene", "seed": 0, "width": int(d.get("width") or 640),
            "height": int(d.get("height") or 640), "frames": f, "check_index": max(0, min(f // 2, 80)),
            "first_frame": SAMPLE_IMAGE, "edit_image": SAMPLE_IMAGE,
            "seconds": max(2, min(int(round(max(f - 1, 1) / int(d.get("fps") or 24))), 20)),
            "bpm": 90, "keyscale": "C major", "audio": SAMPLE_AUDIO, "voice_instruct": "A calm adult voice.",
            "max_tokens": 512, "ref_text": "안녕하세요.", "x_vector": False}
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
