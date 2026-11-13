// 패널의 그래프 도우미(mgs_nodes.js) 시험: 가짜 그래프로 노드 역할·부정 노드 감시·워크플로우 종류를 확인한다.
// 실행: node hub/tests/panel_nodes.test.mjs   (브라우저·ComfyUI 없이)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = readFileSync(path.join(here, "..", "..", "runtime", "nodes", "mgs_bridge", "web", "mgs_nodes.js"), "utf8");
const M = await import("data:text/javascript;base64," + Buffer.from(src, "utf8").toString("base64"));

// ── 가짜 그래프 ───────────────────────────────────────────
function makeGraph(id, nodes, links, extra = {}) {
  const g = { id, _nodes: nodes, links: new Map(links.map((l) => [l.id, l])), ...extra };
  g.getNodeById = (nid) => nodes.find((n) => n.id === nid);
  nodes.forEach((n) => { n.graph = g; });
  return g;
}
const textNode = (id, type, title, text, outLinks, extra = {}) => ({
  id, type, title, mode: 0, widgets: [{ name: "text", value: text }], inputs: [{ name: "clip" }, { name: "text", widget: { name: "text" } }],
  outputs: [{ name: "CONDITIONING", links: outLinks }], ...extra,
});
const sampler = (id, type = "KSampler") => ({
  id, type, title: type, mode: 0, widgets: [],
  inputs: [{ name: "model" }, { name: "positive" }, { name: "negative" }], outputs: [],
});

const POS = "A first-person view from the pilot's seat, looking forward through the large windshield. The control yoke stays centred in the lower foreground. Soft morning light falls across the dashboard and the clouds drift slowly past the glass.";
const NEG_CN = "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的";
const NEG_EN = "low quality, bad anatomy, extra fingers, blurry, watermark";

// 1) 평범한 SD 그래프: 연결로 역할을 안다 (제목이 헷갈려도)
{
  const pos = textNode(1, "CLIPTextEncode", "CLIP Text Encode (Prompt)", "a cat", [10]);
  const neg = textNode(2, "CLIPTextEncode", "CLIP Text Encode (Prompt)", NEG_EN, [11]);
  const ks = sampler(3);
  const g = makeGraph("root", [pos, neg, ks], [
    { id: 10, origin_id: 1, origin_slot: 0, target_id: 3, target_slot: 1 },
    { id: 11, origin_id: 2, origin_slot: 0, target_id: 3, target_slot: 2 },
  ]);
  g.rootGraph = g;
  const c = M.promptCandidates(g);
  assert.equal(c.find((x) => x.node.id === 1).role, "positive");
  assert.equal(c.find((x) => x.node.id === 2).role, "negative");
  assert.equal(M.pickPositive(c, []).node.id, 1);
  assert.equal(M.pickPositive(c, [neg]).node.id, 1, "부정 노드를 골라도 긍정으로 간다");
  assert.equal(M.pickPositive(c, [pos]).node.id, 1);
  // 컨디셔닝을 한 번 거쳐도 (ConditioningSetTimestepRange 같은 중간 노드) 따라간다
  const mid = { id: 5, type: "ConditioningSetTimestepRange", title: "mid", mode: 0, widgets: [], inputs: [{ name: "conditioning" }], outputs: [{ links: [12] }] };
  const pos2 = textNode(4, "CLIPTextEncode", "x", "a dog", [13]);
  const g2 = makeGraph("root", [pos2, mid, ks], [
    { id: 13, origin_id: 4, origin_slot: 0, target_id: 5, target_slot: 0 },
    { id: 12, origin_id: 5, origin_slot: 0, target_id: 3, target_slot: 1 },
  ]);
  g2.rootGraph = g2;
  assert.equal(M.promptCandidates(g2).find((x) => x.node.id === 4).role, "positive");
}

// 2) 공식 Wan 2.2 템플릿 구조: 서브그래프 노드의 text 입력이 안쪽 긍정 CLIPTextEncode 로 이어진다
function wanTemplate(negText = NEG_CN) {
  const inner93 = textNode(93, "CLIPTextEncode", "CLIP Text Encode (Positive Prompt)", "", [2]);
  const inner89 = textNode(89, "CLIPTextEncode", "CLIP Text Encode (Negative Prompt)", negText, [3]);
  const wan = { id: 98, type: "WanImageToVideo", title: "WAN", mode: 0, widgets: [], inputs: [{ name: "positive" }, { name: "negative" }, { name: "vae" }], outputs: [] };
  const def = makeGraph("sub-uuid", [inner93, inner89, wan], [
    { id: 1, origin_id: -10, origin_slot: 0, target_id: 93, target_slot: 1 },
    { id: 2, origin_id: 93, origin_slot: 0, target_id: 98, target_slot: 0 },
    { id: 3, origin_id: 89, origin_slot: 0, target_id: 98, target_slot: 1 },
  ]);
  def.inputs = [{ name: "text", linkIds: [1] }];
  const inst = { id: 129, type: "sub-uuid", title: "Image to Video (Wan2.2)", mode: 0, widgets: [{ name: "text", value: "" }, { name: "width", value: 640 }],
                 inputs: [{ name: "text", widget: { name: "text" }, link: null }], outputs: [] };
  const load = { id: 97, type: "LoadImage", title: "이미지 로드", mode: 0, widgets: [{ name: "image", value: "a.png" }], inputs: [], outputs: [{ links: [] }] };
  const note = { id: 66, type: "MarkdownNote", title: "Model Links", mode: 0, widgets: [{ name: "text", value: "## links" }], inputs: [], outputs: [] };
  const root = makeGraph("root", [inst, load, note], [], { subgraphs: new Map([["sub-uuid", def]]) });
  root.rootGraph = root;
  def.rootGraph = root;
  inst.subgraph = def;
  return { root, def, inst, inner93, inner89 };
}
{
  const { root, inst, inner89 } = wanTemplate();
  const c = M.promptCandidates(root);
  const top = c.find((x) => x.node.id === 129);
  assert.ok(top && top.role === "positive" && !top.inner, "서브그래프의 text 입력은 긍정이다");
  assert.equal(c.find((x) => x.node.id === 89).role, "negative");
  assert.equal(c.find((x) => x.node.id === 93).role, "positive");
  assert.ok(!c.some((x) => x.node.id === 66), "메모 노드는 후보가 아니다");
  assert.equal(M.pickPositive(c, []).node.id, 129, "열려 있는 칸(서브그래프 노드)을 먼저 쓴다");
  assert.equal(M.pickPositive(c, [inner89]).node.id, 129);
  assert.equal(M.workflowKind(root).kind, "i2v");
  assert.equal(M.workflowKind(root).hasLoadImage, true);
  // 실제 서브그래프 안 노드의 출력 links 는 Int32Array 처럼 배열이 아닌 값이다
  const typed = wanTemplate();
  typed.inner93.outputs[0].links = Int32Array.from([2]);
  typed.def.inputs[0].linkIds = Int32Array.from([1]);
  const tc = M.promptCandidates(typed.root);
  assert.equal(tc.find((x) => x.node.id === 129).role, "positive");
  assert.equal(tc.find((x) => x.node.id === 93).role, "positive");
  M.setWidgetText(top, "hello", null);
  assert.equal(inst.widgets[0].value, "hello");
}

// 3) 부정 노드 감시: 긍정 프롬프트가 부정 노드에 들어가면 되돌리고 긍정 노드로 옮긴다
{
  const pos = textNode(1, "CLIPTextEncode", "a", "", [10]);
  const neg = textNode(2, "CLIPTextEncode", "b", NEG_CN, [11]);
  const ks = sampler(3);
  const g = makeGraph("root", [pos, neg, ks], [
    { id: 10, origin_id: 1, origin_slot: 0, target_id: 3, target_slot: 1 },
    { id: 11, origin_id: 2, origin_slot: 0, target_id: 3, target_slot: 2 },
  ]);
  g.rootGraph = g;
  const moved = [];
  const stuck = [];
  const guard = new M.NegativeGuard({ getGraph: () => g, getGenerated: () => [], onMoved: (m) => moved.push(m), onStuck: (c) => stuck.push(c), canvas: () => null });
  guard.tick();                                   // 처음 본 값은 기억만 한다
  neg.widgets[0].value = POS;                     // 사용자가 긍정 프롬프트를 부정 노드에 붙여 넣음
  guard.tick();                                   // 첫 감지: 아직 기다린다 (타이핑 중일 수 있음)
  assert.equal(neg.widgets[0].value, POS);
  guard.tick();                                   // 같은 값으로 머묾 → 옮긴다
  assert.equal(moved.length, 1);
  assert.equal(neg.widgets[0].value, NEG_CN, "부정 노드는 이전 글로 돌아간다");
  assert.equal(pos.widgets[0].value, POS, "긍정 노드에 들어간다");
  assert.equal(moved[0].to.node.id, 1);
  guard.tick(); guard.tick();
  assert.equal(moved.length, 1, "한 번만 옮긴다");
  // 부정 노드를 평범하게 고쳐 쓰면 건드리지 않는다
  neg.widgets[0].value = NEG_EN + ", text";
  guard.tick(); guard.tick(); guard.tick();
  assert.equal(neg.widgets[0].value, NEG_EN + ", text");
  assert.equal(moved.length, 1);
  // 타이핑 도중에 바뀌는 값은 기다린다
  neg.widgets[0].value = POS.slice(0, 120);
  guard.tick();
  neg.widgets[0].value = POS;
  guard.tick();
  assert.equal(moved.length, 1, "값이 계속 바뀌는 동안은 건드리지 않는다");
  // 패널이 만든 글을 붙이면(짧아서 휴리스틱에 안 걸려도) 앞 80자 비교로 잡는다
  const gen = "First-person POV from the pilot's seat inside a commercial airplane cockpit, looking forward through the large curved windshield.";
  const guard2 = new M.NegativeGuard({ getGraph: () => g, getGenerated: () => [gen], onMoved: (m) => moved.push(m), canvas: () => null });
  neg.widgets[0].value = NEG_CN;
  guard2.tick();
  neg.widgets[0].value = gen;
  guard2.tick(); guard2.tick();
  assert.equal(moved.length, 2);
  assert.equal(neg.widgets[0].value, NEG_CN);
  // 서브그래프 안의 부정 노드도 지킨다
  const t = wanTemplate();
  const moved2 = [];
  const guard3 = new M.NegativeGuard({ getGraph: () => t.root, getGenerated: () => [], onMoved: (m) => moved2.push(m), canvas: () => null });
  guard3.tick();
  t.inner89.widgets[0].value = POS;
  guard3.tick(); guard3.tick();
  assert.equal(moved2.length, 1);
  assert.equal(t.inner89.widgets[0].value, NEG_CN);
  assert.equal(t.inst.widgets[0].value, POS, "서브그래프 노드의 text 칸(열려 있는 긍정 칸)으로 간다");
}

// 4) 글 내용 판별
{
  assert.ok(M.looksNegative(NEG_CN) && M.looksNegative(NEG_EN));
  assert.ok(!M.looksNegative(POS) && M.looksPositive(POS));
  assert.ok(!M.looksPositive(NEG_EN) && !M.looksPositive("a cat") && !M.looksPositive(NEG_CN));
  assert.ok(!M.looksNegative("The scene is static for a moment, then the camera glides forward."));   // 'static' 하나만으로는 부정이 아니다
  assert.ok(M.matchesGenerated(POS, [POS]) && !M.matchesGenerated("a cat on a mat, soft light and a warm glow all around", [POS]));
}

// 5) 워크플로우 종류
{
  const mk = (types) => makeGraph("root", types.map((t, i) => ({ id: i + 1, type: t, mode: 0, widgets: [], inputs: [], outputs: [] })), []);
  const k = (types) => { const g = mk(types); g.rootGraph = g; return M.workflowKind(g); };
  assert.equal(k(["LoadImage", "WanImageToVideo"]).kind, "i2v");
  assert.equal(k(["EmptyHunyuanLatentVideo", "KSampler"]).kind, "t2v");
  const ctrl = k(["LoadImage", "LoadVideo", "Wan22FunControlToVideo"]);
  assert.equal(ctrl.kind, "control");
  assert.match(ctrl.label, /참조 영상/);
  assert.equal(k(["KSampler"]).kind, "unknown");
  const muted = mk(["LoadImage", "WanImageToVideo"]);
  muted._nodes[1].mode = 4;
  muted.rootGraph = muted;
  assert.equal(M.workflowKind(muted).kind, "unknown", "건너뛰는 노드는 세지 않는다");
}

console.log("ok panel_nodes (5 groups)");
