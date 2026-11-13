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
ACTIONS = {"image", "i2v", "t2v", "edit"}
SLOT_TYPES = {"string", "int", "float", "bool", "input_image", "prefix"}
SLOT_NAMES = {"prompt", "first_frame", "edit_image", "seed", "frames", "width", "height", "check_index", "save_prefix"}
ZERO_OUT = {"ConditioningZeroOut"}
SAVE_NODES = {"SaveVideo", "SaveImage", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG"}
BAD_NODE_RE = re.compile(r"ollama|LoadVideo|LLM|OpenAI|Gemini|Anthropic", re.I)    # 4절: 허브가 GPU 차례를 따로 관리하는 일이다
USER_DIR_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")


class RecipeError(ValueError):
    """레시피 형식이 틀렸거나 넣을 값이 맞지 않음"""


# ── 읽기 ────────────────────────────────────────────────

def load_recipe_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_all(directory: str = RECIPE_DIR) -> dict[str, dict]:
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
    if recipe.get("negative_source") not in ("node", "zero_out"):
        e.append("negative_source 는 node 또는 zero_out 이어야 합니다")
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

    # V4 긍정 자리
    e = []
    ps = slots_of(recipe, "prompt")
    if len(ps) != 1:
        e.append("inject.prompt 는 자리 하나여야 합니다")
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
    for name, cls_in in (("first_frame", [("", "start_image")]), ("edit_image", [("TextEncodeQwenImageEdit", "image1"), ("VAEEncode", "pixels")])):
        for s in slots_of(recipe, name):
            n = prompt.get(str(s.get("node")), {})
            if n.get("class_type") != "LoadImage":
                e.append(f"inject.{name} 노드는 LoadImage 여야 합니다")
                continue
            for cls, inp in cls_in:
                if not (reaches(prompt, str(s["node"]), cls, inp)):
                    e.append(f"inject.{name} 의 이미지가 {cls or '노드'} 의 {inp} 입력에 닿지 않습니다")
    if recipe.get("action") == "i2v" and not slots_of(recipe, "first_frame"):
        e.append("i2v 레시피는 inject.first_frame 이 필요합니다")
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
    loads = {str(s["node"]) for name in ("first_frame", "edit_image") for s in slots_of(recipe, name)}
    for nid, n in prompt.items():
        ct = str(n.get("class_type", ""))
        if BAD_NODE_RE.search(ct):
            e.append(f"노드 {nid}({ct}) 는 채팅 레시피에 쓸 수 없습니다")
        if ct == "LoadImage" and nid not in loads:
            e.append(f"노드 {nid}(LoadImage) 가 inject 자리가 아닙니다 (예시 파일이 남아 실행이 실패합니다)")
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
    if t == "input_image":
        if not isinstance(value, str) or not value or value.startswith("/") or ".." in value.replace("\\", "/").split("/"):
            raise RecipeError("입력 이미지 경로가 올바르지 않습니다")
        if input_dir is not None and not os.path.isfile(os.path.join(input_dir, value)):
            raise RecipeError("입력 이미지 파일이 없습니다")
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
