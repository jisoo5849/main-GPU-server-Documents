"""운영자 기록 화면 시험 (12절): 화면용 값 만들기와 템플릿 렌더링(자동 이스케이프). DB 없이 돈다.
템플릿 렌더링은 jinja2 가 있을 때만 한다 (허브 이미지에는 있다. 개발 PC 에 없으면 건너뛴다).

  cd hub && python tests/test_studio_opview.py
"""
import datetime
import os
import sys
import types
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402,F401

_db = types.ModuleType("app.db")
_db.ip = lambda v: v
sys.modules["app.db"] = _db
app.db = _db

from app.studio import opview, recipes  # noqa: E402

NOW = datetime.datetime(2026, 11, 13, 10, 0, 0, tzinfo=datetime.timezone.utc)
TID = uuid.uuid4()
AID = uuid.uuid4()
JID = uuid.uuid4()
XSS = "<script>alert(1)</script>"


def turn(**kw):
    t = {"id": TID, "thread_id": uuid.uuid4(), "user_id": 7, "client_ip": "10.0.0.5", "state": "done", "stage": "collect",
         "action": "i2v", "recipe_id": "wan22_i2v_14b_fast", "recipe_version": 1, "retry_of": None,
         "request": {"text": XSS + " 천천히", "attachment_ids": [str(AID)], "spec": {"seconds": None}, "mode": "new"},
         "decision": {"action": "i2v", "by": "rule", "rule": "R3"},
         "work": {"prompt": "A calm scene. " + XSS, "profile": "wan22_i2v_long", "frame": {"subject": XSS},
                  "length_choice": {"tier": "long", "reason": "자세해서", "by": "rule"}, "ko": {"subject": "조종석"}},
         "result": {"attachment_ids": []}, "error": None, "created_at": NOW, "finished_at": NOW + datetime.timedelta(seconds=95),
         "username": "bob", "title": XSS}
    t.update(kw)
    return t


# ── summarize_turn ──
s = opview.summarize_turn(turn(), [{"wait_ms": 1500, "run_ms": 90000}, {"wait_ms": 500, "run_ms": None}], {"notice"})
assert s["seconds"] == 95 and s["jobs"] == 2 and s["wait_ms"] == 2000 and s["run_ms"] == 90000 and s["notice"] and not s["attention"]
assert s["id"] == str(TID) and s["recipe"] == "wan22_i2v_14b_fast v1" and len(s["text"]) <= 61
a = opview.summarize_turn(turn(state="failed", error={"code": "submit"}))
assert a["attention"] and a["error_code"] == "submit"
assert not opview.summarize_turn(turn(state="failed", error={"code": "run_error"}))["attention"]
assert opview.summarize_turn(turn(finished_at=None))["seconds"] is None
assert opview.summarize_turn(turn(retry_of=uuid.uuid4()))["retry"]
assert opview.short("가" * 100, 10) == "가" * 10 + "…" and opview.short(None) == ""

# ── repro_prompt: 레시피와 접수 때 넣은 값으로 prompt 를 다시 만든다 ──
att = {str(AID): {"id": AID, "media": "image", "input_rel": "studio/bob/%s.png" % AID, "width": 1920, "height": 1080,
                  "source": "upload"}}
ev = [{"kind": "comfy_submitted", "ts": NOW, "detail": {"recipe": "wan22_i2v_14b_fast", "version": 1, "seed": 42,
                                                         "size": [848, 480], "frames": 81}}]
p = opview.repro_prompt(turn(), ev, att)
assert p is not None
# 소리 포함(LTX) 요청도 다시 만들 수 있다: frames 칸이 없고 seconds 칸이 있다
LTX_V = recipes.load_all()["ltx25_i2v_sound"]["version"]
ev_ltx = [{"kind": "comfy_submitted", "ts": NOW, "detail": {"recipe": "ltx25_i2v_sound", "version": LTX_V, "seed": 7,
                                                             "size": [1280, 704], "frames": 241}}]
pl = opview.repro_prompt(turn(recipe_id="ltx25_i2v_sound"), ev_ltx, att)
assert pl is not None and pl["398:362"]["inputs"]["value"] == 10 and pl["398:339"]["inputs"]["noise_seed"] == 7
assert pl["900"]["inputs"]["batch_index"] == 120 and pl["398:372"]["inputs"]["value"] == 1280
assert pl["902"]["inputs"]["batch_index"] == -1 and pl["903"]["inputs"]["filename_prefix"].endswith("/last_frame")   # 마지막 프레임 저장
old_ev = [{"kind": "comfy_submitted", "ts": NOW, "detail": dict(ev_ltx[0]["detail"], version=LTX_V - 1)}]
assert opview.repro_prompt(turn(recipe_id="ltx25_i2v_sound"), old_ev, att) is None    # 레시피 판이 바뀐 접수 기록은 다시 만들지 않는다
assert p["129:93"]["inputs"]["text"].startswith("A calm scene") and p["129:86"]["inputs"]["noise_seed"] == 42
assert (p["129:98"]["inputs"]["width"], p["129:98"]["inputs"]["height"], p["129:98"]["inputs"]["length"]) == (848, 480, 81)
assert p["97"]["inputs"]["image"].startswith("studio/bob/") and p["108"]["inputs"]["filename_prefix"].startswith("repro/")
assert opview.repro_prompt(turn(), [], att) is None                                   # 접수 기록 없음
assert opview.repro_prompt(turn(work={}), ev, att) is None                            # 프롬프트 없음
bad_ev = [{"kind": "comfy_submitted", "ts": NOW, "detail": {"recipe": "gone", "seed": 1, "size": [1, 1]}}]
assert opview.repro_prompt(turn(), bad_ev, att) is None                               # 레시피가 사라짐
# 이미지 레시피(첨부 없음, 크기 있음)와 편집 레시피(크기 없음, 편집 대상 첨부)도 다시 만든다
ev_img = [{"kind": "comfy_submitted", "ts": NOW, "detail": {"recipe": "zimage_turbo_t2i", "seed": 5, "size": [1024, 1024]}}]
pi = opview.repro_prompt(turn(action="image", request={"text": "x", "attachment_ids": []}), ev_img, {})
assert pi and pi["57:3"]["inputs"]["seed"] == 5 and pi["9"]["inputs"]["filename_prefix"].startswith("repro/")
ev_ed = [{"kind": "comfy_submitted", "ts": NOW, "detail": {"recipe": "qwen_image_edit", "seed": 9, "size": []}}]
pe = opview.repro_prompt(turn(action="edit"), ev_ed, att)
assert pe and pe["41"]["inputs"]["image"].startswith("studio/bob/") and pe["170:169"]["inputs"]["seed"] == 9

ev_old = [{"kind": "comfy_submitted", "ts": NOW, "detail": {"recipe": "wan22_i2v_14b_fast", "version": 99, "seed": 1,
                                                             "size": [848, 480], "frames": 81}}]
assert opview.repro_prompt(turn(), ev_old, att) is None                               # 레시피 판이 달라짐

# ── 날짜 거르기·응답 헤더 ──
captured = {}


class FakeDB:
    async def fetch(self, sql, *args):
        captured["sql"], captured["args"] = sql, args
        return []


_db.fetch = FakeDB().fetch
import asyncio  # noqa: E402
asyncio.run(opview.list_turns("bob", "done", "2026-11-13", 50))
assert captured["args"][2] == datetime.date(2026, 11, 13) and captured["args"][-2:] == (opview.PAGE, 50), captured["args"]
assert "$3" in captured["sql"] and "LIMIT $4 OFFSET $5" in captured["sql"], captured["sql"]
asyncio.run(opview.list_turns("", "", "not-a-date", 0))                               # 형식이 틀리면 무시
assert captured["args"] == (opview.PAGE, 0) and "created_at::date" not in captured["sql"]
asyncio.run(opview.list_turns("", "weird", "", 10 ** 30))                             # 모르는 상태·큰 offset
assert captured["args"] == (opview.PAGE, 10 ** 9)
assert "default-src 'none'" in opview.ADMIN_HEADERS["Content-Security-Policy"] and opview.ADMIN_HEADERS["X-Content-Type-Options"] == "nosniff"

# ── build_detail ──
events = ev + [
    {"kind": "llm_job", "ts": NOW, "detail": {"job_id": str(JID), "purpose": "video_expand", "end": "done"}},
    {"kind": "render", "ts": NOW, "detail": {"crew": {"category": "공간"}, "violations": []}},
    {"kind": "notice", "ts": NOW, "detail": {"code": "inspect", "text": "검사"}},
    {"kind": "node_timing", "ts": NOW, "detail": {"node": "129:86", "ms": 100}},
]
jobs = [{"id": JID, "seq": 11, "kind": "llm", "step": "prompt", "purpose": "video_expand", "attempt": 1, "state": "finished",
         "outcome": "success", "model": "qwen", "wait_ms": 100, "run_ms": 2000}]
calls = [{"job_id": JID, "endpoint": "helper:video_expand", "model": "qwen", "prompt_tokens": 10, "completion_tokens": 20,
          "duration_ms": 1900, "status": 200}]
d = opview.build_detail(turn(), events, jobs, calls, [{"id": 1, "role": "user", "kind": "text", "body": XSS}],
                        [att[str(AID)]], None)
assert d["gpu"][0]["end"] == "done" and d["gpu"][0]["calls"][0]["duration_ms"] == 1900
assert d["length"] == {"tier": "long", "reason": "자세해서", "by": "규칙"}
assert d["request"]["attachments"][0]["width"] == 1920 and d["repro"] is not None
assert d["notices"][0]["code"] == "inspect" and d["node_timing"] and d["crew"]["events"] == [{"category": "공간"}]
assert opview.pretty({"a": "한글", "t": NOW}).count("한글") == 1                        # 한글은 그대로, 날짜도 문자열로

# ── 템플릿: 믿지 않는 글은 이스케이프되고, 화면이 깨지지 않는다 ──
lib = os.environ.get("JINJA_LIB")
if lib:
    sys.path.insert(0, lib)
try:
    import jinja2
except ImportError:
    print("(jinja2 가 없어 템플릿 시험은 건너뜀)")
    jinja2 = None
if jinja2 is not None:
    tpl_dir = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(tpl_dir), autoescape=jinja2.select_autoescape(["html"]))
    ctx = {"request": None, "user": {"role": "admin", "username": "admin", "display_name": "관리자"}, "studio_open": True,
           "flash": None, "host": "x"}
    rows = [opview.summarize_turn(turn(), [{"wait_ms": 1, "run_ms": 2}], {"notice"}),
            opview.summarize_turn(turn(state="failed", error={"code": "submit"}))]
    html = env.get_template("admin_studio.html").render(rows=rows, attention=[rows[1]], states=opview.STATES, f_user="u&v",
                                                         f_state="done", f_day="2026-11-13", offset=50, page=50, **ctx)
    assert XSS not in html and "&lt;script&gt;" in html and "관리자 확인이 필요한" in html and "u%26v" in html
    html = env.get_template("admin_studio_turn.html").render(d=d, pretty=opview.pretty, short=opview.short,
                                                              job_prompt={"1": {"inputs": {"text": XSS}}}, **ctx)
    assert XSS not in html and html.count("&lt;script&gt;") >= 5, html.count("&lt;script&gt;")
    assert "repro.json" in html and "video_expand" in html and "자세해서" in html
    html = env.get_template("admin_studio_turn.html").render(
        d=opview.build_detail(turn(work={}, request={"text": "x"}), [], [], [], [], [], None), pretty=opview.pretty,
        short=opview.short, job_prompt=None, **ctx)                      # 기록이 거의 없는 요청도 그려진다
    assert "GPU 작업이 없습니다" in html and "다시 만들 접수 기록이 없습니다" in html
    # 글→영상을 장면 이미지로 만든 요청: 장면 프롬프트 칸이 보이고 글은 이스케이프된다. 일반 요청에는 칸이 없다
    w = {"prompt": "A calm scene.", "scene_prompt": "SCENE " + XSS, "scene_size": [1024, 576],
         "length_choice": {"tier": "long", "reason": "r", "by": "rule"}}
    dv = opview.build_detail(turn(work=w), [{"kind": "scene_done", "ts": NOW, "detail": {"attachment": "a"}},
                                            {"kind": "expand", "ts": NOW, "detail": {"scene": True, "rounds": 1}}], [], [], [], [], None)
    assert dv["scene"]["prompt"].startswith("SCENE") and dv["scene"]["events"] and dv["scene"]["expand"]
    html = env.get_template("admin_studio_turn.html").render(d=dv, pretty=opview.pretty, short=opview.short, job_prompt=None, **ctx)
    assert "장면 이미지 프롬프트" in html and "SCENE" in html and XSS not in html
    assert "장면 이미지 프롬프트" not in env.get_template("admin_studio_turn.html").render(
        d=opview.build_detail(turn(), [], [], [], [], [], None), pretty=opview.pretty, short=opview.short, job_prompt=None, **ctx)
    # 시스템 프롬프트 칸: 차례마다 보낸 규칙 글, 같은 글은 한 번만, 이스케이프됨. 기록이 없는 옛 요청에는 칸이 없다
    sj = [{"kind": "llm_job", "ts": NOW, "detail": {"purpose": "video_expand", "model": "m", "systems": ["RULES " + XSS]}},
          {"kind": "llm_job", "ts": NOW, "detail": {"purpose": "video_fix", "model": "m", "systems": ["RULES " + XSS, "FIX RULES"]}},
          {"kind": "llm_job", "ts": NOW, "detail": {"purpose": "image_inspect", "model": "m"}}]
    dv = opview.build_detail(turn(), sj, [], [], [], [], None)
    assert [x["purpose"] for x in dv["systems"]] == ["영상 프롬프트 작성", "영상 프롬프트 고치기"] and dv["systems"][1]["texts"] == ["FIX RULES"]
    html = env.get_template("admin_studio_turn.html").render(d=dv, pretty=opview.pretty, short=opview.short, job_prompt=None, **ctx)
    assert "시스템 프롬프트" in html and "FIX RULES" in html and XSS not in html
    assert opview.build_detail(turn(), [], [], [], [], [], None)["systems"] == []
print("ok studio_opview")
