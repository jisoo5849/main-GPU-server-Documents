"""공식 ComfyUI 워크플로우 템플릿에서 '이 모델로 시작하는 워크플로우' 찾기.

템플릿은 작업공간의 Python 환경 안 comfyui_workflow_templates_json 패키지에 있다 (ComfyUI 가 /templates/ 로 제공하는 것과 같은 파일).
템플릿 JSON 의 로더 노드에는 모델 파일 이름이 두 군데 들어 있다.
  - node.properties.models = [{name, url, directory}]  (다운로드 정보)
  - node.widgets_values[...]                          (실제로 불러오는 값, 위치 기준)
widgets_values_named 는 오래된 값이 남아 있을 수 있어 쓰지 않는다. 메모 노드 글이나 단순 문자열 검색도 쓰지 않는다.

순수 함수(build_index, family_key, rank, substitute)는 파일 시스템 없이 테스트할 수 있다 (hub/tests).
"""
import copy
import glob
import json
import logging
import os
import re

from . import models_index
from .config import settings

log = logging.getLogger("mgs.templates")

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft")
MAIN_LOADERS = {"UNETLoader", "CheckpointLoaderSimple", "ImageOnlyCheckpointLoader", "UnetLoaderGGUF"}
MAIN_DIRS = {"diffusion_models", "checkpoints", "unet"}
_QUANT_TOKEN = re.compile(r"^(fp|bf|f|int|uint|nf|mxfp|nvfp)\d+\w*$")
_DROP_TOKENS = {"e4m3fn", "e4m3fnuz", "e5m2", "scaled", "convrot", "mixed", "fp8mixed", "gptq", "awq", "kj",
                "comfy", "comfyui", "pruned"}
_GGUF_QUANT = re.compile(r"[-_.](q\d+_[a-z0-9_]+|q\d+|iq\d+_[a-z]+)$")

_cache: dict[str, tuple[float, dict]] = {}


def _basename(v: str) -> str:
    return v.replace("\\", "/").rsplit("/", 1)[-1]


def family_key(filename: str) -> str:
    """같은 모델의 다른 양자화 버전을 묶는 키. 예) z_image_turbo_bf16 ~ z_image_turbo_int8_convrot."""
    name = _basename(filename).lower()
    for ext in MODEL_EXTS:
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    name = _GGUF_QUANT.sub("", name)
    tokens = [t for t in re.split(r"[_\-\s]+", name) if t]
    kept = [t for t in tokens if not _QUANT_TOKEN.match(t) and t not in _DROP_TOKENS
            and not re.match(r"^w\d+a\d+$", t)]
    return "_".join(kept)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _walk_nodes(j: dict):
    for n in j.get("nodes") or []:
        yield n, False
    for sg in ((j.get("definitions") or {}).get("subgraphs") or []):
        for n in sg.get("nodes") or []:
            yield n, True


def build_index(templates: dict[str, dict], meta: dict[str, dict]) -> dict:
    """templates: {이름: 템플릿 JSON}, meta: {이름: index.json 항목}."""
    exact: dict[str, dict[str, set]] = {}
    required: dict[str, dict[str, str]] = {}
    for tname, j in templates.items():
        req = required.setdefault(tname, {})
        for node, _in_sg in _walk_nodes(j):
            ntype = str(node.get("type", ""))
            for m in ((node.get("properties") or {}).get("models") or []):
                if isinstance(m, dict) and m.get("name"):
                    f = _basename(m["name"])
                    req[f] = m.get("directory") or ""
                    exact.setdefault(f, {}).setdefault(tname, set()).add((ntype, m.get("directory") or ""))
            wv = node.get("widgets_values")
            if isinstance(wv, list):
                for v in wv:
                    if isinstance(v, str) and v.lower().endswith(MODEL_EXTS):
                        f = _basename(v)
                        exact.setdefault(f, {}).setdefault(tname, set()).add((ntype, ""))
    family: dict[str, set] = {}
    for f in exact:
        family.setdefault(family_key(f), set()).add(f)
    return {"exact": exact, "required": required, "family": family, "meta": meta}


FOLDER_ALIASES = {"unet": "diffusion_models", "clip": "text_encoders"}
LOADERS_FOR = {
    "checkpoints": {"CheckpointLoaderSimple", "ImageOnlyCheckpointLoader", "CheckpointLoader"},
    "diffusion_models": {"UNETLoader", "UnetLoaderGGUF"},
}


def canon_folder(folder: str | None) -> str:
    return FOLDER_ALIASES.get(folder or "", folder or "")


def _compatible(roles, folder: str | None) -> bool:
    """템플릿이 이 파일을 서버의 폴더와 같은 종류의 로더로 불러오는가."""
    if not folder:
        return True
    want = canon_folder(folder)
    for ntype, directory in roles:
        if directory and canon_folder(directory) == want:
            return True
        if ntype in LOADERS_FOR.get(want, ()):
            return True
    return False


def _same_kind(a: str, b: str) -> bool:
    """GGUF 와 safetensors 는 서로 다른 로더를 쓰므로 같은 모델로 보지 않는다."""
    return a.lower().endswith(".gguf") == b.lower().endswith(".gguf")


def _available(file: str, directory: str, server_files: set[str], by_dir: dict | None) -> bool:
    fam = family_key(file)
    if by_dir is not None and directory:
        pool = by_dir.get(canon_folder(directory), set())
    else:
        pool = server_files
    return file in pool or any(family_key(s) == fam and _same_kind(s, file) for s in pool)


def _date_num(value) -> int:
    digits = re.sub(r"\D", "", str(value or ""))[:8]
    return int(digits) if digits else 0


def lookup(idx: dict, filename: str, server_files: set[str], folder: str | None = None,
           by_dir: dict | None = None) -> list[dict]:
    """모델 파일에 맞는 템플릿 목록 (좋은 순서).

    server_files: 서버에 있는 모델 파일 basename 집합
    folder:       서버에서 이 파일이 있는 폴더. 템플릿이 같은 종류의 로더로 이 파일을 불러올 때만 고른다
                  (체크포인트 파일을 UNETLoader 템플릿에 넣으면 검증에서 실패하므로)
    by_dir:       {폴더: 그 폴더의 basename 집합}. 있으면 빠진 모델을 폴더 기준으로 판단한다
    """
    f = _basename(filename)
    hits = []
    for tname, roles in idx["exact"].get(f, {}).items():
        if _compatible(roles, folder):
            hits.append({"name": tname, "match": "exact", "source_file": f, "roles": roles})
    if not hits:
        for g in sorted(idx["family"].get(family_key(f), set()) - {f}):
            if not _same_kind(f, g):
                continue
            for tname, roles in idx["exact"].get(g, {}).items():
                if _compatible(roles, folder):
                    hits.append({"name": tname, "match": "family", "source_file": g, "roles": roles})
    nf = _norm(f)
    out = []
    for h in hits:
        meta = idx["meta"].get(h["name"], {})
        if not meta:
            continue   # index.json 에 없는 파일은 제공하지 않는다
        req = idx["required"].get(h["name"], {})
        # 서버에 없는 모델 (같은 모델의 다른 양자화 파일이 있으면 그것으로 바꿔 쓰므로 빠진 것으로 보지 않는다)
        missing = [{"file": x, "directory": d} for x, d in sorted(req.items())
                   if x != h["source_file"] and not _available(x, d, server_files, by_dir)]
        main_signal = 0 if any(_norm(m) and _norm(m) in nf for m in meta.get("models") or []) else 1
        role = 0 if any(t in MAIN_LOADERS or d in MAIN_DIRS for t, d in h["roles"]) else 1
        open_source = meta.get("openSource", True) is not False
        h.update({
            "title": meta.get("title") or h["name"],
            "category": meta.get("category") or "",
            "description": meta.get("description") or "",
            "thumbnail": f"/templates/{h['name']}-1.{meta.get('mediaSubtype') or 'webp'}",
            "missing": missing,
            "openSource": open_source,
            "isApp": bool(meta.get("isApp")),
            # 유료 API 노드 템플릿은 뒤로. 빠진 모델은 '있다/없다' 만 보고, 그다음은 많이 쓰이는 순·최신 순
            "_key": (0 if h["match"] == "exact" else 1, main_signal, role, 0 if open_source else 1,
                     1 if missing else 0, 1 if meta.get("isApp") else 0,
                     0 if (meta.get("username") or "ComfyUI") == "ComfyUI" else 1,
                     -(meta.get("usage") or 0), -_date_num(meta.get("date"))),
        })
        del h["roles"]
        out.append(h)
    out.sort(key=lambda h: h["_key"])
    for h in out:
        del h["_key"]
    return out


def substitute(workflow: dict, mapping: dict[str, str]) -> dict:
    """템플릿 안의 모델 파일 이름을 서버에 있는 실제 이름으로 바꾼다 (정확히 같은 값만).

    바뀐 모델의 properties.models 항목은 지운다 (다른 양자화 파일의 다운로드 주소가 남지 않게).
    """
    wf = copy.deepcopy(workflow)
    if not mapping:
        return wf
    for node, _ in _walk_nodes(wf):
        wv = node.get("widgets_values")
        if isinstance(wv, list):
            node["widgets_values"] = [mapping.get(v, mapping.get(_basename(v), v)) if isinstance(v, str)
                                      and v.lower().endswith(MODEL_EXTS) else v for v in wv]
        named = node.get("widgets_values_named")
        if isinstance(named, dict):
            for k, v in list(named.items()):
                if isinstance(v, str) and (v in mapping or _basename(v) in mapping):
                    named[k] = mapping.get(v, mapping.get(_basename(v)))
        props = node.get("properties")
        if isinstance(props, dict) and isinstance(props.get("models"), list):
            props["models"] = [m for m in props["models"]
                               if not (isinstance(m, dict) and _basename(m.get("name", "")) in mapping
                                       and mapping[_basename(m["name"])] != m.get("name"))]
    return wf


# ── 파일 시스템 ───────────────────────────────────────────

def templates_dir(env: dict) -> str | None:
    pattern = os.path.join(settings.live_path(env["slug"]), "venv", "lib", "python3*", "site-packages",
                           "comfyui_workflow_templates_json", "templates")
    found = sorted(glob.glob(pattern))
    return found[-1] if found else None


def _load_index_file(tdir: str, fname: str) -> dict:
    path = os.path.join(tdir, fname)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        cats = json.load(f)
    out = {}
    for cat in cats if isinstance(cats, list) else []:
        for t in cat.get("templates") or []:
            if t.get("name"):
                out[t["name"]] = {**t, "category": cat.get("title") or cat.get("category") or ""}
    return out


def _build_for_dir(tdir: str) -> tuple[dict, int]:
    """(색인, 읽지 못한 템플릿 수)."""
    meta = _load_index_file(tdir, "index.json")
    ko = _load_index_file(tdir, "index.ko.json")
    for name, m in ko.items():
        if name in meta:
            meta[name]["title"] = m.get("title") or meta[name].get("title")
            meta[name]["description"] = m.get("description") or meta[name].get("description")
            meta[name]["category"] = m.get("category") or meta[name].get("category")
    templates, failures = {}, 0
    for name in meta:
        path = os.path.join(tdir, f"{name}.json")
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            failures += 1
            continue
        if isinstance(j, dict) and "nodes" in j:
            templates[name] = j
    log.info("템플릿 색인: %s (%d개, 실패 %d)", tdir, len(templates), failures)
    return build_index(templates, meta), failures


def index_for(env: dict) -> tuple[dict | None, str | None]:
    tdir = templates_dir(env)
    if not tdir:
        return None, None
    real = os.path.realpath(tdir)
    try:
        stamp = (os.path.getmtime(os.path.join(tdir, "index.json")), os.stat(tdir).st_mtime_ns)
    except OSError:
        return None, None
    hit = _cache.get(real)
    if hit is None or hit[0] != stamp:
        idx, failures = _build_for_dir(tdir)
        if failures:
            # 노드 설치 등으로 패키지가 바뀌는 중일 수 있다. 불완전한 색인은 저장하지 않는다.
            return idx, tdir
        hit = (stamp, idx)
        _cache[real] = hit
        for key in [k for k in _cache if not os.path.isdir(k)]:
            _cache.pop(key, None)   # 지워진 작업공간의 색인
    return hit[1], tdir


def server_files() -> tuple[set[str], dict, dict]:
    """서버의 모델 파일.

    (basename 집합, {(폴더, basename): ComfyUI 목록 이름(하위 폴더 포함)}, {폴더: basename 집합})
    폴더는 unet→diffusion_models, clip→text_encoders 로 맞춘다.
    """
    names, rel_of, by_dir = set(), {}, {}
    for rel, hits in models_index.index().items():
        b = _basename(rel)
        names.add(b)
        for folder, _full in hits:
            cf = canon_folder(folder)
            rel_of.setdefault((cf, b), rel)
            by_dir.setdefault(cf, set()).add(b)
    return names, rel_of, by_dir


def _has_template(idx: dict, f: str, folder: str) -> bool:
    if any(_compatible(roles, folder) for roles in idx["exact"].get(f, {}).values()):
        return True
    for g in idx["family"].get(family_key(f), set()) - {f}:
        if _same_kind(f, g) and any(_compatible(roles, folder) for roles in idx["exact"].get(g, {}).values()):
            return True
    return False


def _models_with_templates_sync(env: dict) -> set[tuple[str, str]]:
    idx, _ = index_for(env)
    if idx is None:
        return set()
    _names, rel_of, _by_dir = server_files()
    return {(cf, b) for (cf, b) in rel_of if _has_template(idx, b, cf)}


async def models_with_templates(env: dict) -> set[tuple[str, str]]:
    """시작 워크플로우가 있는 서버 모델 {(폴더, basename)}."""
    import asyncio
    return await asyncio.to_thread(_models_with_templates_sync, env)


def _pick_same(req_file: str, pool: set[str]) -> str | None:
    fam = family_key(req_file)
    same = sorted(s for s in pool if family_key(s) == fam and _same_kind(s, req_file))
    return same[0] if same else None


def _starter_sync(env: dict, model: str, folder: str, chosen: str | None) -> dict | None:
    idx, tdir = index_for(env)
    if idx is None:
        return None
    files, rel_of, by_dir = server_files()
    hits = lookup(idx, model, files, folder, by_dir)
    if not hits:
        return None
    pick = next((h for h in hits if h["name"] == chosen), hits[0]) if chosen else hits[0]
    with open(os.path.join(tdir, f"{pick['name']}.json"), encoding="utf-8") as f:
        wf = json.load(f)
    # 템플릿 값 → 서버의 실제 파일 이름 (양자화가 다른 같은 모델, 하위 폴더 경로)
    mapping = {}
    user_value = model.replace("\\", "/")
    if pick["source_file"] != user_value:
        mapping[pick["source_file"]] = user_value
    for req_file, directory in idx["required"].get(pick["name"], {}).items():
        if req_file == pick["source_file"]:
            continue
        cf = canon_folder(directory)
        pool = by_dir.get(cf, set()) if cf else files
        if req_file in pool:
            rel = rel_of.get((cf, req_file), req_file)
            if rel != req_file:
                mapping.setdefault(req_file, rel)
            continue
        same = _pick_same(req_file, pool)
        if same:
            mapping.setdefault(req_file, rel_of.get((cf, same), same))
    return {
        "name": pick["name"], "title": pick["title"], "category": pick["category"], "match": pick["match"],
        "source_file": pick["source_file"], "missing": pick["missing"], "openSource": pick["openSource"],
        "alternatives": [{"name": h["name"], "title": h["title"], "match": h["match"], "missing": len(h["missing"])}
                         for h in hits[:8] if h["name"] != pick["name"]],
        "workflow": substitute(wf, mapping),
    }


async def starter_for(env: dict, model: str, folder: str, chosen: str | None = None) -> dict | None:
    import asyncio
    if not model or ".." in model:
        return None
    return await asyncio.to_thread(_starter_sync, env, model, folder, chosen)
