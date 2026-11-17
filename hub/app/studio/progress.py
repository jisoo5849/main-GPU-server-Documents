"""진행률: ComfyUI 이벤트 → 생성 % 와 띠 이름 (docs/chat-studio-design.md 8절). 순수 함수·클래스만 둔다.

- 샘플러 띠는 마지막 progress 의 value/max, 그 밖의 띠는 끝난 노드 수/띠의 노드 수.
- 띠 k 의 노드가 시작되면 앞 띠는 모두 끝난 것으로 본다(막대가 멈추지 않게).
- %는 줄지 않고, execution_success 에서 100 이다. 띠에 없는 노드는 세지 않는다.
"""

# purpose(llm_turn) → 감독 단계 (8.4)
PURPOSE_STAGE = {"video_frame": "frame", "video_expand": "prompt", "video_fix": "fix", "image_inspect": "inspect",
                 "prompt_expand": "prompt", "edit_prompt": "prompt", "video_plan": "prompt", "video_story": "story"}
# 글→영상을 장면 이미지 → 영상 두 단계로 만들 때의 단계 이름. 사용자에게는 이미지라는 말을 쓰지 않는다
PIPELINE_PURPOSE_STAGE = {"prompt_expand": "prompt", "video_frame": "motion", "video_expand": "motion", "video_fix": "motion",
                          "image_inspect": "inspect", "video_plan": "prompt", "video_story": "story"}

STAGE_TEXT = {
    ("frame", "queued"): "첫 화면 읽기 차례를 기다리는 중",
    ("frame", "running"): "첫 화면 읽는 중",
    ("prompt", "queued"): "프롬프트 작성 차례를 기다리는 중",
    ("prompt", "running"): "프롬프트 작성 중",
    ("fix", "queued"): "다듬기 차례를 기다리는 중",
    ("fix", "running"): "프롬프트 다듬는 중",
    ("motion", "queued"): "움직임 설계 차례를 기다리는 중",
    ("motion", "running"): "움직임 설계 중",
    ("story", "queued"): "구성안 차례를 기다리는 중",
    ("story", "running"): "구성안 쓰는 중",
}

# 종류마다 진행 카드의 제목과 단계 칩 (8.4). P1 은 i2v 만 쓰지만 표는 문서대로 둔다
CARD = {
    "i2v": ("영상 만드는 중", ["frame", "prompt", "fix", "generate", "collect"]),
    "t2v": ("영상 만드는 중", ["prompt", "generate", "collect"]),
    "image": ("이미지 만드는 중", ["prompt", "generate", "collect"]),
    "edit": ("이미지 편집하는 중", ["prompt", "generate", "collect"]),
    "pipeline": ("영상 만드는 중", ["prompt", "scene", "motion", "generate", "collect"]),
    "long": ("긴 영상 만드는 중", ["story", "scenes", "music", "assemble"]),      # 여러 장면을 이어 붙이는 긴 영상
}
CHIP_LABEL = {"frame": "첫 화면 읽기", "prompt": "프롬프트 작성", "fix": "다듬기", "generate": "생성", "collect": "마무리",
              "scene": "장면 준비", "motion": "움직임 설계", "story": "구성안", "scenes": "장면", "music": "음악",
              "assemble": "합치기"}


def long_pct(done: int, n: int, phase: str, frac: float = 0.0) -> int | None:
    """긴 영상 전체 %: (끝난 장면 + 지금 장면의 진행) ÷ (장면 수 + 1). 남은 1 은 음악·합치기 몫이다. 구성안 단계는 % 없음"""
    n = max(1, int(n or 1))
    if phase == "scenes":
        return min(99, int(100 * (max(0, int(done or 0)) + max(0.0, min(1.0, frac))) / (n + 1)))
    if phase == "music":
        return int(100 * (n + 0.3) / (n + 1))
    if phase == "assemble":
        return min(99, int(100 * (n + 0.7) / (n + 1)))
    return None


def wait_text(position: int | None, running_ahead: bool = False, kind: str = "생성") -> str:
    """'대기 n번째 (생성 차례)'. 내 줄보다 앞선 queued 줄 수 + 1 이 순번이다 (8.4)"""
    base = f"대기 {position}번째 ({kind} 차례)" if position else f"{kind} 차례를 기다리는 중"
    return base + (", 앞에서 1건 실행 중" if running_ahead else "")


def queue_position(queue: list[dict], job_id: str) -> int | None:
    """jobs.global_queue() 모양의 목록에서 내 줄의 순번. queued 가 아니면 None"""
    mine = next((j for j in queue if str(j.get("id")) == str(job_id)), None)
    if not mine or mine.get("state") != "queued":
        return None
    return 1 + sum(1 for j in queue if j.get("state") == "queued" and j.get("seq", 0) < mine.get("seq", 0))


def chips(action: str, has_fix: bool, current: str | None) -> list[dict]:
    """단계 칩. 다듬기 칩은 고치기 차례를 열 때만 생긴다. 현재 단계 앞은 done, 뒤는 todo"""
    order = [s for s in CARD.get(action, CARD["i2v"])[1] if s != "fix" or has_fix]
    out, seen = [], False
    for s in order:
        state = "now" if s == current else ("todo" if seen else "done")
        if s == current:
            seen = True
        out.append({"key": s, "label": CHIP_LABEL[s], "state": state})
    if current not in order:        # 칩에 없는 단계(plan·inspect 등)는 마지막으로 끝난 칩까지 done
        pass
    return out


class Tracker:
    """한 ComfyUI 작업의 진행. feed(type, data) 로 이벤트를 넣고 snapshot() 으로 읽는다."""

    def __init__(self, bands: list[dict], prompt_id: str | None = None):
        self.bands = bands
        self.prompt_id = str(prompt_id) if prompt_id else None
        self.node_band = {}
        for i, b in enumerate(bands):
            for n in b.get("nodes") or []:
                self.node_band.setdefault(str(n), i)
        self.total = sum(float(b["weight"]) for b in bands) or 1.0
        self.done_nodes: set = set()
        self.cur_node: str | None = None
        self.cur_band = 0
        self.sampler_frac: dict[str, float] = {}
        self.sampler_n: dict[str, tuple[int, int]] = {}   # 노드 → (value, max) (8.3 의 n 저장)
        self.pct = 0
        self.started = False
        self.success = False
        self.max_band = 0

    # 다른 작업의 이벤트는 무시 (prompt_id 가 없으면 지금 노드 보정에만)
    def _mine(self, data: dict) -> bool:
        pid = data.get("prompt_id")
        return pid is None or self.prompt_id is None or str(pid) == self.prompt_id

    def _touch(self, nid: str | None):
        if nid is None:
            return
        nid = str(nid)
        self.cur_node = nid
        i = self.node_band.get(nid)
        if i is not None:
            self.max_band = max(self.max_band, i)

    def feed(self, typ: str, data: dict | None) -> bool:
        """이벤트를 넣는다. %가 바뀌었으면 True"""
        data = data or {}
        if typ == "execution_start":
            if not self._mine(data):
                return False
            self.started = True
        elif typ == "execution_cached":
            if not self._mine(data):
                return False
            self.done_nodes |= {str(n) for n in data.get("nodes") or []}
        elif typ == "executing":
            if not self._mine(data):
                return False
            nid = data.get("node")
            if nid is None:
                if data.get("prompt_id") is not None:
                    self.success = True       # 끝날 때 {node: null}
            else:
                if self.cur_node is not None and self.cur_node != str(nid):
                    self.done_nodes.add(self.cur_node)
                self._touch(nid)
        elif typ == "progress":
            if not self._mine(data):
                return False
            nid = str(data.get("node")) if data.get("node") is not None else self.cur_node
            i = self.node_band.get(nid)
            if i is None or not self.bands[i].get("sampler"):
                return False                  # 샘플러 띠가 아닌 노드의 progress 는 무시 (타일 디코드 등)
            mx = max(int(data.get("max") or 0), 1)
            val = min(int(data.get("value") or 0), mx)
            self.sampler_frac[nid] = val / mx
            self.sampler_n[nid] = (val, mx)
            self._touch(nid)
        elif typ == "progress_state":
            if not self._mine(data):
                return False
            for nid, st in (data.get("nodes") or {}).items():
                nid = str(nid)
                if st.get("state") == "finished":
                    self.done_nodes.add(nid)
                elif st.get("state") == "running":
                    self._touch(nid)
                    i = self.node_band.get(nid)
                    mx = int(st.get("max") or 0)
                    if i is not None and self.bands[i].get("sampler") and mx > 0:
                        self.sampler_frac[nid] = min(int(st.get("value") or 0), mx) / mx
                        self.sampler_n[nid] = (min(int(st.get("value") or 0), mx), mx)
        elif typ == "execution_success":
            if not self._mine(data):
                return False
            self.success = True
        else:
            return False
        return self._recompute()

    def _band_fraction(self, i: int) -> float:
        b = self.bands[i]
        nodes = [str(n) for n in b.get("nodes") or []]
        if i < self.max_band:
            return 1.0                        # 뒤 띠가 시작됐으면 앞 띠는 끝난 것으로 본다
        if b.get("sampler"):
            if any(n in self.done_nodes for n in nodes):
                return 1.0
            return max([self.sampler_frac.get(n, 0.0) for n in nodes] or [0.0])
        if not nodes:
            return 0.0
        return sum(1 for n in nodes if n in self.done_nodes) / len(nodes)

    def _recompute(self) -> bool:
        if self.success:
            new = 100
        else:
            s = sum(float(b["weight"]) * self._band_fraction(i) for i, b in enumerate(self.bands))
            new = min(99, int(100 * s / self.total))     # 100 은 성공 신호에서만
        new = max(new, self.pct)                          # 줄지 않게
        changed = new != self.pct
        self.pct = new
        return changed

    def band_label(self) -> str:
        i = self.node_band.get(self.cur_node or "")
        i = max(self.max_band if i is None else i, 0)
        return self.bands[min(i, len(self.bands) - 1)]["label"]

    def snapshot(self) -> dict:
        return {"pct": self.pct, "band": self.band_label(), "started": self.started, "success": self.success}
