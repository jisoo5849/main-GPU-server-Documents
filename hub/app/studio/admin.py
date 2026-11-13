"""관리 명령 studio-check·studio-import (docs/chat-studio-design.md 6.4·6.7). 메인에서 허브 컨테이너 안으로 실행한다.

  docker compose exec -T hub python -m app.cli studio-check              # 모든 레시피 V1~V13
  docker compose exec -T hub python -m app.cli studio-check wan22_i2v_14b_fast --no-editor   # 편집 공간 검사(V11·V12)는 건너뜀
  docker compose exec -T hub python -m app.cli studio-import 내보낸파일.json --id my_recipe --action i2v

studio-check 는 파일만 보는 검사(V1~V10·V13)와, 채팅 작업공간의 편집 공간이 떠 있으면 V11(노드·입력 이름)·V12(ComfyUI 검증)를
돌려 결과를 studio_recipe_checks 에 남긴다. GPU 는 쓰지 않는다 (편집 공간은 CPU 로 검증만 한다).
studio-import 는 Export (API) 파일에서 레시피 뼈대를 만든다. DB 가 필요 없다.
"""
import hashlib
import json
import os
import re
import sys
import uuid

from ..config import settings
from . import recipes

LEVEL_MARK = {"ok": "통과", "warn": "경고", "error": "실패"}


def print_report(rid: str, report: list[dict]):
    print(f"== {rid}")
    for x in report:
        print(f"  {x['id']:<4} {LEVEL_MARK.get(x['level'], x['level'])}" + (f"  {x['msg']}" if x["msg"] else ""))


async def file_report(r: dict) -> list[dict]:
    """파일만 보는 검사 V1~V10·V13. 허브 시작 검사와 같다"""
    from .. import models_index, promptops, video
    profiles = {p["id"] for p in video.profile_list()}
    report = recipes.check_recipe(r, profiles)
    missing = [m["name"] for m in promptops.find_models(r["prompt"]) if not models_index.resolve(m["name"])]
    report.append({"id": "V10", "ok": not missing, "level": "error" if missing else "ok",
                   "msg": ("공용 저장소에 없는 모델: " + ", ".join(missing)) if missing else ""})
    return report


async def editor_report(env: dict, r: dict) -> list[dict]:
    """V11·V12. 채팅 작업공간의 편집 공간이 떠 있어야 한다"""
    import httpx

    from .. import jobs, promptops
    from . import files
    out = []
    try:
        ip = await jobs._editor_ip(env)
        async with httpx.AsyncClient(timeout=120) as c:
            oi = (await c.get(f"http://{ip}:8189/object_info")).json()
    except Exception as e:  # noqa: BLE001 - 편집 공간이 꺼져 있거나 응답이 없음
        msg = f"편집 공간에 연결하지 못했습니다: {str(e)[:200]}"
        return [{"id": "V11", "ok": False, "level": "error", "msg": msg},
                {"id": "V12", "ok": False, "level": "error", "msg": "V11 을 하지 못해 건너뜀"}]
    errs, warns = recipes.check_object_info(r["prompt"], oi, settings.accept_missing_nodes)
    out.append({"id": "V11", "ok": not errs, "level": "error" if errs else ("warn" if warns else "ok"),
                "msg": "; ".join(errs + warns)})
    try:
        files.write_sample_png(env)
        prompt = recipes.build(r, recipes.sample_values(r))
        ok, error, node_errors = await jobs._validate_in_editor(env, prompt, str(uuid.uuid4()), None)
        if not ok:
            ok, node_errors = promptops.filter_node_errors(error, node_errors, settings.accept_missing_nodes)
            msg = json.dumps(node_errors, ensure_ascii=False)[:600] if not ok else "누락 노드만 있어 통과 (실행 공간에서 확인됩니다)"
        else:
            msg = ""
        out.append({"id": "V12", "ok": bool(ok), "level": "ok" if ok and not msg else ("warn" if ok else "error"), "msg": msg})
    except Exception as e:  # noqa: BLE001
        out.append({"id": "V12", "ok": False, "level": "error", "msg": f"검증 호출 실패: {str(e)[:300]}"})
    return out


async def studio_check(args):
    from .. import db
    from . import store
    all_r = recipes.load_all()
    ids = args.recipes or sorted(all_r)
    unknown = [i for i in ids if i not in all_r]
    if unknown:
        sys.exit("모르는 레시피: " + ", ".join(unknown))
    env = await store.studio_env(settings.studio_env_slug)
    if not args.no_editor and env is None:
        print(f"채팅 작업공간 {settings.studio_env_slug} 이 없어 V11·V12 는 건너뜁니다 (설계서 7.2).")
    failed = 0
    for rid in ids:
        r = all_r[rid]
        report = await file_report(r)
        if not args.no_editor and env is not None:
            report += await editor_report(env, r)
        print_report(rid, report)
        ok = recipes.recipe_ok(report)
        failed += 0 if ok else 1
        raw = open(os.path.join(recipes.RECIPE_DIR, rid + ".json"), "rb").read()
        await store.save_recipe_check(r, hashlib.sha256(raw).hexdigest(), ok, report)
    await db.log_access(None, None, "admin_cli", {"cmd": "studio-check", "target": ",".join(ids), "by": args.by or None,
                                                  "failed": failed})
    if failed:
        print(f"실패한 레시피 {failed}개. 허브를 다시 시작하면 시작 검사 결과로 켜지거나 꺼집니다.")
        sys.exit(1)
    print("모든 레시피가 통과했습니다. 꺼져 있던 레시피는 허브를 다시 시작하면 다시 켜집니다.")


def studio_import(args):
    if not re.fullmatch(r"[a-z0-9_]{3,48}", args.id or ""):
        sys.exit("레시피 id 는 영문 소문자·숫자·밑줄 3~48자여야 합니다.")
    with open(args.file, encoding="utf-8") as f:
        api = json.load(f)
    if not isinstance(api, dict) or "nodes" in api or not all(isinstance(v, dict) and "class_type" in v for v in api.values()):
        sys.exit("API 형식 파일이 아닙니다. ComfyUI 메뉴의 'Export (API)' 로 내보낸 파일이어야 합니다.")
    r, todo = recipes.skeleton_from_api(api, args.id, args.action, args.title)
    out = args.out or os.path.join(recipes.RECIPE_DIR, args.id + ".json")
    if os.path.exists(out) and not args.force:
        sys.exit(f"{out} 이 이미 있습니다. 덮어쓰려면 --force")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"레시피 뼈대를 만들었습니다: {out}")
    print_report(args.id, recipes.check_recipe(r, None))
    print("사람이 확인할 것:")
    for t in todo:
        print("  - " + t)
