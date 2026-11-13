"""진행률 시험 (8절): 문서 8.3 의 예시 표대로 %가 움직이고, 줄지 않으며, 다른 작업의 이벤트는 무시한다.

  cd hub && python tests/test_studio_progress.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.studio import progress as P  # noqa: E402
from app.studio import recipes as R  # noqa: E402

rec = R.load_all()["wan22_i2v_14b_fast"]
bands = rec["progress"]["bands"]
PID = "job-1"
t = P.Tracker(bands, PID)


def ev(typ, **d):
    d.setdefault("prompt_id", PID)
    t.feed(typ, d)
    return t.pct


assert t.pct == 0
assert ev("execution_start") == 0 and t.started
prep = bands[0]["nodes"]
seq = []
for n in prep:
    ev("executing", node=n)
    seq.append(t.pct)
assert seq == sorted(seq) and 0 < seq[-1] <= 10, seq            # 1 → 10
assert ev("executing", node="129:86") == 10                     # 생성 1/2 시작
assert t.band_label() == "생성 1/2"
assert ev("progress", node="129:86", value=1, max=2) == 30
assert ev("progress", node="129:86", value=2, max=2) == 50
assert ev("executing", node="129:85") == 50
assert t.band_label() == "생성 2/2"
assert ev("progress", node="129:85", value=1, max=2) == 70
assert ev("progress", node="129:85", value=2, max=2) == 90
last = 90
for n in ("129:87", "129:94", "108", "900", "901"):
    ev("executing", node=n)
    assert last <= t.pct <= 99, (n, t.pct)
    last = t.pct
assert 92 <= last <= 98 and t.band_label() == "영상 만들기", (last, t.band_label())
assert ev("execution_success") == 100

# 줄지 않는다: 오래된 진행 메시지가 늦게 와도
t2 = P.Tracker(bands, PID)
t2.feed("executing", {"node": "129:85", "prompt_id": PID})
t2.feed("progress", {"node": "129:85", "value": 2, "max": 2, "prompt_id": PID})
hi = t2.pct
t2.feed("progress", {"node": "129:86", "value": 1, "max": 2, "prompt_id": PID})
assert t2.pct >= hi

# 다른 작업의 이벤트는 무시한다
t3 = P.Tracker(bands, PID)
assert t3.feed("progress", {"node": "129:86", "value": 2, "max": 2, "prompt_id": "other"}) is False and t3.pct == 0
assert t3.feed("execution_success", {"prompt_id": "other"}) is False and t3.pct == 0

# 샘플러 띠가 아닌 노드의 progress(타일 디코드)는 무시한다
t4 = P.Tracker(bands, PID)
t4.feed("executing", {"node": "129:87", "prompt_id": PID})
before = t4.pct
t4.feed("progress", {"node": "129:87", "value": 1, "max": 9, "prompt_id": PID})
assert t4.pct == before

# 뒤 띠가 시작되면 앞 띠는 끝난 것으로 본다 (준비 노드가 일부 실행되지 않아도 막대가 멈추지 않는다)
t5 = P.Tracker(bands, PID)
t5.feed("executing", {"node": "129:86", "prompt_id": PID})
assert t5.pct == 10

# 캐시된 노드는 끝난 것으로 센다
t6 = P.Tracker(bands, PID)
t6.feed("execution_cached", {"nodes": prep, "prompt_id": PID})
assert t6.pct == 10, t6.pct

# progress_state 스냅샷으로 다시 계산한다 (이벤트를 놓쳐도)
t7 = P.Tracker(bands, PID)
t7.feed("progress_state", {"prompt_id": PID, "nodes": {**{n: {"state": "finished"} for n in prep},
                                                       "129:86": {"state": "finished"},
                                                       "129:85": {"state": "running", "value": 1, "max": 2}}})
assert t7.pct == 70, t7.pct

# executing 의 node: null 도 끝 신호다
t8 = P.Tracker(bands, PID)
t8.feed("executing", {"node": None, "prompt_id": PID})
assert t8.pct == 100

# 대기 순번과 문구
q = [{"id": "a", "state": "running", "seq": 1}, {"id": "b", "state": "queued", "seq": 2}, {"id": "c", "state": "queued", "seq": 3}]
assert P.queue_position(q, "c") == 2 and P.queue_position(q, "b") == 1 and P.queue_position(q, "a") is None
assert P.wait_text(2, True) == "대기 2번째 (생성 차례), 앞에서 1건 실행 중"
assert P.PURPOSE_STAGE["video_expand"] == "prompt" and P.STAGE_TEXT[("fix", "running")] == "프롬프트 다듬는 중"

# 칩: 다듬기는 고치기 차례를 열 때만
assert [c["key"] for c in P.chips("i2v", False, "frame")] == ["frame", "prompt", "generate", "collect"]
c = P.chips("i2v", True, "generate")
assert [x["state"] for x in c] == ["done", "done", "done", "now", "todo"], c
print("ok studio_progress")
