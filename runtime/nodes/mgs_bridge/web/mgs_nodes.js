// 메인 GPU 패널의 워크플로우 그래프 도우미 (화면 없이 그래프 객체만 다룬다. mgs_panel.js 가 불러 쓰고 node 로 시험한다).
// - 프롬프트 글 노드를 찾고 긍정·부정 역할을 정한다 (서브그래프 안까지)
// - 긍정 프롬프트가 부정 노드에 들어갔는지 감지한다
// - 지금 열린 워크플로우가 이미지→영상인지, 글→영상인지, 참조 영상이 필요한 Fun Control 인지 알아본다

export const TEXT_WIDGETS = ["text", "prompt", "positive", "text_g"];
const MAX_DEPTH = 8;

export function getLink(graph, id) {
  const links = graph?.links;
  if (!links) return null;
  return (typeof links.get === "function" ? links.get(id) : links[id]) || null;
}

export function rootOf(graph) {
  return graph?.rootGraph || graph || null;
}

export function nodesOf(graph) {
  return graph?._nodes || graph?.nodes || [];
}

/** 최상위 그래프와 서브그래프 정의 전부 */
export function graphsOf(root) {
  const r = rootOf(root);
  if (!r) return [];
  const subs = r.subgraphs ? (typeof r.subgraphs.values === "function" ? [...r.subgraphs.values()] : Object.values(r.subgraphs)) : [];
  return [r, ...subs];
}

function defOf(root, node) {
  if (node?.subgraph) return node.subgraph;
  const subs = rootOf(root)?.subgraphs;
  if (!subs || !node?.type) return null;
  return (typeof subs.get === "function" ? subs.get(node.type) : subs[node.type]) || null;
}

// links 가 배열이 아니라 Int32Array 같은 배열 비슷한 값일 때도 읽는다 (실제 서브그래프 노드의 출력이 그렇다)
const listOf = (x) => (!x ? [] : Array.isArray(x) ? x : typeof x[Symbol.iterator] === "function" ? [...x] : Object.values(x));

const roleByName = (name) => {
  const n = String(name || "").toLowerCase();
  if (/^positive/.test(n)) return "positive";
  if (/^negative/.test(n)) return "negative";
  return null;
};

/** 글 노드(CLIPTextEncode 등)의 출력을 따라가 샘플러의 positive·negative 입력에 닿는지 본다 */
function roleDownstream(root, graph, node, depth = 0, seen = new Set()) {
  const roles = new Set();
  if (!node || depth > MAX_DEPTH) return roles;
  const mark = `${graph?.id}:${node.id}`;
  if (seen.has(mark)) return roles;
  seen.add(mark);
  for (const out of node.outputs || []) {
    for (const lid of listOf(out.links)) {
      const link = getLink(graph, lid);
      const target = link && graph.getNodeById?.(link.target_id);
      if (!target) continue;
      const direct = roleByName(target.inputs?.[link.target_slot]?.name);
      if (direct) { roles.add(direct); continue; }
      const def = defOf(root, target);
      if (def) {   // 서브그래프 입력으로 들어가면 그 안에서 이어서 따라간다
        for (const id of listOf(def.inputs?.[link.target_slot]?.linkIds)) {
          const inner = getLink(def, id);
          const innerNode = inner && def.getNodeById?.(inner.target_id);
          if (!innerNode) continue;
          const r = roleByName(innerNode.inputs?.[inner.target_slot]?.name);
          if (r) roles.add(r);
          else for (const x of roleDownstream(root, def, innerNode, depth + 1, seen)) roles.add(x);
        }
        continue;
      }
      for (const x of roleDownstream(root, graph, target, depth + 1, seen)) roles.add(x);
    }
  }
  return roles;
}

const textWidgetOf = (node, name) =>
  node?.widgets?.find((w) => (name ? w.name === name : TEXT_WIDGETS.includes(w.name)) && typeof w.value === "string");

function roleFromTitle(node) {
  const t = `${node?.title || ""}`;
  if (/negative|neg\b|부정/i.test(t)) return "negative";
  if (/positive|긍정/i.test(t)) return "positive";
  return null;
}

function settle(set, node, text) {
  if (set.size === 1) return [...set][0];
  if (set.size > 1) return "mixed";
  return roleFromTitle(node) || (looksNegative(text) ? "negative" : "unknown");
}

/**
 * 글 입력칸이 있는 노드를 모두 모아 역할을 붙인다.
 * 서브그래프 노드는 입력으로 열어 둔 글 칸(예: 공식 Wan 2.2 템플릿의 text)을 안쪽 노드의 역할로 판단한다.
 * 돌려주는 항목: { node, widget, role: positive|negative|unknown|mixed, graph, key, label, inner }
 */
export function promptCandidates(graphLike) {
  const root = rootOf(graphLike);
  const out = [];
  for (const graph of graphsOf(root)) {
    const isRoot = graph === root;
    for (const node of nodesOf(graph)) {
      if (node.mode === 2 || node.mode === 4) continue;   // 꺼 둔·건너뛰는 노드
      const def = isRoot ? defOf(root, node) : null;
      if (def) {
        (node.inputs || []).forEach((inp, i) => {
          const wname = inp.widget?.name || inp.name;
          const widget = TEXT_WIDGETS.includes(wname) ? textWidgetOf(node, wname) : null;
          if (!widget || inp.link != null) return;
          const set = new Set();
          for (const id of listOf(def.inputs?.[i]?.linkIds)) {
            const link = getLink(def, id);
            const innerNode = link && def.getNodeById?.(link.target_id);
            if (!innerNode) continue;
            const direct = roleByName(innerNode.inputs?.[link.target_slot]?.name);
            if (direct) set.add(direct);
            else for (const x of roleDownstream(root, def, innerNode)) set.add(x);
          }
          out.push({ node, widget, role: settle(set, node, widget.value), graph, key: `${graph.id}:${node.id}:${widget.name}`,
                     label: node.title || node.type, inner: false });
        });
        continue;
      }
      const widget = textWidgetOf(node);
      if (!widget || !(node.outputs || []).length) continue;   // 출력이 없는 메모 노드(MarkdownNote 등)는 제외
      if (/Note|Markdown/i.test(node.type || "")) continue;
      const set = roleDownstream(root, graph, node);
      out.push({ node, widget, role: settle(set, node, widget.value), graph, key: `${graph.id}:${node.id}:${widget.name}`,
                label: node.title || node.type, inner: !isRoot });
    }
  }
  return out;
}

/** 긍정 프롬프트를 넣을 곳. 선택한 노드가 긍정이면 그것을, 아니면 그래프에서 찾은 긍정 노드를 고른다 */
export function pickPositive(cands, selected = []) {
  const ids = new Set(selected.map((n) => n.id));
  const selPos = cands.find((c) => ids.has(c.node.id) && c.role === "positive");
  if (selPos) return selPos;
  const outer = (c) => !c.inner;   // 서브그래프 안쪽 노드보다 화면에 열려 있는 글 칸을 먼저 쓴다
  const pos = cands.filter((c) => c.role === "positive");
  if (pos.length) return pos.find(outer) || pos[0];
  // 역할을 못 찾았는데 부정 노드가 따로 없고 글 칸이 하나뿐이면 그것이 긍정이다
  const unknown = cands.filter((c) => c.role === "unknown");
  if (unknown.length === 1 && !cands.some((c) => c.role === "negative")) return unknown[0];
  return null;
}

export function setWidgetText(cand, text, canvas) {
  cand.widget.value = text;
  try { cand.widget.callback?.(text, canvas, cand.node); } catch { /* 값은 이미 넣었다 */ }
  try { cand.node.graph?.setDirtyCanvas?.(true, true); } catch { /* 화면 갱신은 선택 */ }
}

// ── 글 내용으로 부정·긍정 가리기 ────────────────────────────

const NEG_TERMS = new RegExp(
  "\\b(?:low quality|worst quality|bad anatomy|bad quality|extra (?:fingers|digits|limbs)|missing (?:fingers|limbs)|blurry|"
  + "blurred|watermark|deformed|disfigured|ugly|jpeg artifacts|signature|nsfw|overexposed|static|lowres|cropped|mutated|"
  + "out of frame|poorly drawn)\\b|色调艳丽|过曝|静态|最差质量|低质量|丑陋|畸形|模糊",
  "gi",
);
const cjkRatio = (t) => (t.match(/[㐀-鿿]/g) || []).length / Math.max(1, t.length);
const wordCount = (t) => String(t || "").trim().split(/\s+/).filter(Boolean).length;
const norm = (t) => String(t || "").toLowerCase().replace(/\s+/g, " ").trim();

/** 부정 프롬프트처럼 보이는지: 나쁜 품질을 나열한 말이 둘 이상 (한자 부정 목록은 하나만 있어도) */
export function looksNegative(text) {
  const t = String(text || "");
  const hits = (t.match(NEG_TERMS) || []).length;
  return hits >= 2 || (hits >= 1 && cjkRatio(t) > 0.3);
}

/** 긍정 프롬프트처럼 보이는지: 문장으로 쓴 25낱말 이상의 영문이고 부정 나열이 아님 */
export function looksPositive(text) {
  const t = String(text || "").trim();
  if (wordCount(t) < 25 || looksNegative(t) || cjkRatio(t) > 0.2) return false;
  return (t.match(/[.!?](?:\s|$)/g) || []).length >= 2;
}

/** 패널이 만든 프롬프트(영상·도우미 결과)와 같은 글인지 (앞 80자 비교) */
export function matchesGenerated(text, generated = []) {
  const t = norm(text);
  if (t.length < 60) return false;
  return generated.some((g) => {
    const x = norm(g);
    return x.length >= 60 && (t.startsWith(x.slice(0, 80)) || x.startsWith(t.slice(0, 80)));
  });
}

/**
 * 부정 프롬프트 노드에 긍정 프롬프트가 들어갔는지 지켜본다 (tick 을 주기적으로 부른다).
 * 글이 두 번 연속(약 2초) 같은 값으로 머물 때만 움직여서, 사용자가 타이핑하는 중에는 건드리지 않는다.
 * 감지하면 부정 노드를 이전 글로 되돌리고 긍정 노드에 넣은 뒤 onMoved 로 알린다.
 */
export class NegativeGuard {
  constructor({ getGraph, getGenerated, onMoved, onStuck, canvas }) {
    Object.assign(this, { getGraph, getGenerated, onMoved, onStuck, canvas });
    this.snaps = new Map();
  }

  tick() {
    const cands = promptCandidates(this.getGraph());
    const generated = this.getGenerated?.() || [];
    for (const c of cands) {
      if (c.role !== "negative") continue;
      const cur = String(c.widget.value ?? "");
      const snap = this.snaps.get(c.key);
      if (!snap) { this.snaps.set(c.key, { text: cur, pend: null }); continue; }
      if (cur === snap.text) { snap.pend = null; continue; }
      if (!(looksPositive(cur) || matchesGenerated(cur, generated))) { snap.text = cur; snap.pend = null; continue; }
      if (!snap.pend || snap.pend.text !== cur) { snap.pend = { text: cur, n: 1 }; continue; }
      if (++snap.pend.n < 2) continue;
      const to = pickPositive(cands, []);
      if (!to || to.key === c.key) { snap.text = cur; snap.pend = null; this.onStuck?.(c, cur); continue; }
      setWidgetText(to, cur, this.canvas?.());
      setWidgetText(c, snap.text, this.canvas?.());
      const after = this.snaps.get(to.key);
      if (after) after.text = cur;
      snap.pend = null;
      this.onMoved?.({ from: c, to, text: cur, restored: snap.text });
    }
  }
}

// ── 지금 열린 워크플로우 종류 ──────────────────────────────

const I2V_NODES = /^(?:WanImageToVideo|Wan22ImageToVideoLatent|WanFirstLastFrameToVideo|WanSoundImageToVideo|WanAnimateToVideo|LTXVImgToVideo|HunyuanImageToVideo|CosmosImageToVideoLatent|WanPhantomSubjectToVideo)$/;
const FUN_NODES = /^(?:WanFunControlToVideo|Wan22FunControlToVideo|WanFunInpaintToVideo|WanCameraImageToVideo|WanVaceToVideo)$/;
const T2V_NODES = /^(?:EmptyHunyuanLatentVideo|EmptyLTXVLatentVideo|EmptyMochiLatentVideo|EmptyCosmosLatentVideo|EmptyHunyuanVideo15Latent)$/;

/**
 * { kind: i2v|t2v|control|unknown, label, hasLoadImage, hasLoadVideo }
 * control = 참조 영상(포즈·윤곽선)이 있어야 만들어지는 Fun Control·VACE 류.
 */
export function workflowKind(graphLike) {
  const types = new Set();
  let hasLoadImage = false;
  let hasLoadVideo = false;
  for (const graph of graphsOf(graphLike)) {
    for (const n of nodesOf(graph)) {
      if (n.mode === 2 || n.mode === 4) continue;
      types.add(n.type);
      if (n.type === "LoadImage") hasLoadImage = true;
      if (n.type === "LoadVideo") hasLoadVideo = true;
    }
  }
  const has = (rx) => [...types].some((t) => rx.test(String(t)));
  const base = { hasLoadImage, hasLoadVideo };
  if (has(FUN_NODES) && hasLoadVideo) {
    return { ...base, kind: "control", label: "참조 영상이 필요한 워크플로우(Fun Control·VACE)" };
  }
  if (has(I2V_NODES)) return { ...base, kind: "i2v", label: "이미지 → 영상 워크플로우" };
  if (has(T2V_NODES)) return { ...base, kind: "t2v", label: "글 → 영상 워크플로우" };
  return { ...base, kind: "unknown", label: "영상 워크플로우를 알아보지 못했습니다" };
}
