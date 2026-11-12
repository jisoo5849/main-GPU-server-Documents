// 메인 GPU 패널: ComfyUI 화면 안에서 모델 고르기, 시작 워크플로우 열기, 메인 전체 GPU 대기열 보기.
// 허브의 /mgs/api/* (같은 주소, 같은 로그인 쿠키)를 부른다. ComfyUI 프런트엔드 1.57.1 번들에서 확인한 API 만 쓴다.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAB_ID = "mgs-main-gpu";
const KINDS = [
  ["image", "이미지"],
  ["video", "영상"],
  ["other", "기타"],
  ["llm", "LLM"],
  ["embed", "임베딩"],
];
const KIND_LABEL = { comfy: "ComfyUI", llm: "LLM", embed: "임베딩" };
const INPUT_LOADERS = { LoadImage: "image", LoadImageMask: "image", LoadVideo: "file", LoadAudio: "audio" };

const state = {
  me: null,
  catalog: null,
  queue: null,
  kind: "image",
  filter: "",
  busy: false,
  issuing: false,
  timer: null,
  root: null,
  alts: {},
  newKey: null,
  error: null,
  loginUrl: null,
  // 프롬프트 도우미 (메타 규칙 확장)
  helper: { text: "", style: "", notes: "", busy: false, result: null, open: false, crew: true },
  // 영상 렌더러: 요청(도우미 칸) + 첫 화면 → 영상 모델용 움직임 프롬프트
  video: { open: false, profile: "", seconds: 5, readFrame: true, busy: false, result: null, profiles: null, loading: false, loadError: null },
  // 결과 자동 검사 (VLM)
  check: { on: false, expect: "", results: [], seen: new Set() },
};

// 사용자가 적어 둔 그림체·필수 특징·자동 검사 설정은 브라우저에 기억한다 (작업공간 포트마다 따로)
const SAVED_KEYS = {
  style: "mgs.helper.style", expect: "mgs.check.expect", on: "mgs.check.on", crew: "mgs.helper.crew",
  videoProfile: "mgs.video.profile", videoRead: "mgs.video.read",
};
try {
  state.helper.style = localStorage.getItem(SAVED_KEYS.style) || "";
  state.helper.crew = localStorage.getItem(SAVED_KEYS.crew) !== "0";
  state.video.profile = localStorage.getItem(SAVED_KEYS.videoProfile) || "";
  state.video.readFrame = localStorage.getItem(SAVED_KEYS.videoRead) !== "0";
  state.check.expect = localStorage.getItem(SAVED_KEYS.expect) || "";
  state.check.on = localStorage.getItem(SAVED_KEYS.on) === "1";
} catch { /* 저장소를 못 쓰는 브라우저 */ }
const save = (k, v) => { try { localStorage.setItem(SAVED_KEYS[k], v); } catch { /* 무시 */ } };

class LoginError extends Error {}

const fileURL = (p) => (api && typeof api.fileURL === "function" ? api.fileURL(p) : p);

async function getJSON(path, opts = {}) {
  const r = await fetch(fileURL(path), { credentials: "same-origin", cache: "no-store", ...opts });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) {
    if (data.login) state.loginUrl = data.login;
    throw new LoginError("로그인이 필요합니다");
  }
  if (!r.ok) {
    const e = new Error(data.error || `HTTP ${r.status}`);
    e.status = r.status;
    e.data = data;
    throw e;
  }
  return data;
}

const postJSON = (path, body) =>
  getJSON(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

function toast(severity, summary, detail, life = 4000) {
  try {
    app.extensionManager.toast.add({ severity, summary, detail, life });
  } catch {
    console.log(`[메인 GPU] ${summary}: ${detail || ""}`);
  }
}

async function copyText(text, what = "복사했습니다") {
  // 사무실 HTTP 주소에서는 navigator.clipboard 가 막혀 있어 예전 방식으로도 시도한다
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      toast("success", what);
      return;
    }
  } catch { /* 아래 방식으로 */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  if (ok) toast("success", what);
  else toast("warn", "자동 복사가 안 됩니다. 아래 내용을 직접 복사하세요", text, 15000);
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function injectStyle() {
  if (document.getElementById("mgs-panel-style")) return;
  const css = `
.mgs-panel{display:flex;flex-direction:column;gap:10px;padding:10px 12px 16px;color:var(--fg-color);font-size:13px;height:100%;overflow-y:auto;box-sizing:border-box}
.mgs-panel *{box-sizing:border-box}
.mgs-head{display:flex;flex-direction:column;gap:6px}
.mgs-title{font-size:15px;font-weight:600;display:flex;align-items:center;gap:6px}
.mgs-sub{color:var(--descrip-text);font-size:12px}
.mgs-card{background:var(--comfy-input-bg);border:1px solid var(--border-color);border-radius:6px;padding:8px 10px;display:flex;flex-direction:column;gap:4px}
.mgs-warn{color:var(--error-text);font-weight:600}
.mgs-ok{color:#4caf7a}
.mgs-tabs{display:flex;flex-wrap:wrap;gap:4px}
.mgs-tab{border:1px solid var(--border-color);background:transparent;color:var(--fg-color);border-radius:999px;padding:3px 10px;cursor:pointer;font-size:12px}
.mgs-tab.on{background:var(--comfy-menu-secondary-bg,var(--comfy-menu-bg));border-color:var(--fg-color)}
.mgs-input,.mgs-select{width:100%;background:var(--comfy-input-bg);color:var(--input-text);border:1px solid var(--border-color);border-radius:6px;padding:5px 8px;font:inherit}
.mgs-list{display:flex;flex-direction:column;gap:6px}
.mgs-item{background:var(--comfy-input-bg);border:1px solid var(--border-color);border-radius:6px;padding:7px 9px;display:flex;flex-direction:column;gap:5px}
.mgs-name{word-break:break-all;line-height:1.35}
.mgs-row{display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.mgs-btn{border:1px solid var(--border-color);background:var(--comfy-menu-secondary-bg,var(--comfy-menu-bg));color:var(--fg-color);border-radius:5px;padding:3px 9px;cursor:pointer;font-size:12px}
.mgs-btn:hover{background:var(--content-hover-bg);color:var(--content-hover-fg)}
.mgs-btn:disabled{opacity:.5;cursor:wait}
.mgs-btn.primary{border-color:var(--fg-color)}
.mgs-link{color:var(--fg-color);text-decoration:underline;cursor:pointer;font-size:12px;background:none;border:0;padding:0;font-family:inherit}
.mgs-foot{display:flex;flex-wrap:wrap;gap:8px 12px;padding-top:6px;border-top:1px solid var(--border-color)}
.mgs-code{font-family:ui-monospace,monospace;font-size:11px;word-break:break-all;white-space:pre-wrap;background:var(--comfy-menu-bg);padding:4px 6px;border-radius:4px}
.mgs-empty{color:var(--descrip-text);padding:6px 2px}
`;
  document.head.append(h("style", { id: "mgs-panel-style" }, css));
}

function fmtElapsed(s) {
  if (s < 60) return `${s}초`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}분 ${s % 60}초`;
  return `${Math.floor(m / 60)}시간 ${m % 60}분`;
}

// ── 화면 ──────────────────────────────────────────────

function render(container) {
  // 사이드바를 열 때마다 새 div 로 다시 불린다. 이전 화면과 타이머는 버린다.
  stopTimer();
  injectStyle();
  const root = h("div", { class: "mgs-panel" });
  container.replaceChildren(root);
  state.root = root;
  draw();
  loadAll();
  state.timer = setInterval(() => {
    const active = app.extensionManager?.sidebarTab?.activeSidebarTabId;
    if (!state.root || !state.root.isConnected || (active !== undefined && active !== TAB_ID)) {
      // 다른 탭으로 바뀌었거나 패널이 닫혔다 (이 경우 destroy 가 불리지 않을 수 있다)
      return stopTimer();
    }
    if (!document.hidden) refreshQueue();
  }, 3000);
}

function stopTimer() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

async function loadAll() {
  try {
    const [me, catalog, queue] = await Promise.all([
      getJSON("/mgs/api/me"),
      getJSON("/mgs/api/catalog"),
      getJSON("/mgs/api/queue"),
    ]);
    Object.assign(state, { me, catalog, queue, error: null });
  } catch (e) {
    state.error = e instanceof LoginError ? "login" : e.message;
  }
  draw();
}

async function refreshQueue() {
  try {
    const q = await getJSON("/mgs/api/queue");
    if (state.error === "login") {
      // 다른 탭에서 다시 로그인했다
      state.error = null;
      return loadAll();
    }
    state.queue = q;
    const box = state.root?.querySelector(".mgs-queue");
    if (box) box.replaceWith(queueCard());
  } catch (e) {
    if (e instanceof LoginError && state.error !== "login") {
      state.error = "login";
      draw();
    }
  }
}

function titleRow() {
  return h("div", { class: "mgs-title" }, h("i", { class: "pi pi-server" }), "메인 GPU");
}

function draw() {
  const root = state.root;
  if (!root) return;   // 열리는 도중에는 아직 문서에 붙지 않았을 수 있다. 그래도 그려 둔다.
  if (state.error === "login") {
    const base = state.loginUrl || state.me?.links?.login || `http://${location.hostname}/login`;
    const loginUrl = `${base}?next=${encodeURIComponent(location.href)}`;
    root.replaceChildren(titleRow(),
      h("div", { class: "mgs-card" },
        h("div", { class: "mgs-warn", text: "로그인이 만료되었습니다." }),
        h("a", { class: "mgs-link", href: loginUrl, text: "다시 로그인" })));
    return;
  }
  if (state.error) {
    root.replaceChildren(titleRow(),
      h("div", { class: "mgs-card" }, h("div", { class: "mgs-warn", text: "불러오지 못했습니다" }),
        h("div", { class: "mgs-sub", text: state.error }),
        h("button", { class: "mgs-btn", onclick: () => { state.error = null; draw(); loadAll(); } }, "다시 시도")));
    return;
  }
  if (!state.me) {
    root.replaceChildren(titleRow(), h("div", { class: "mgs-empty", text: "불러오는 중…" }));
    return;
  }
  root.replaceChildren(...[header(), queueCard(), helperCard(), videoCard(), checkCard(), tabs(), search(), list(), keyBox(),
    footer()].filter(Boolean));
}

function header() {
  const { me } = state;
  const options = [];
  if (!me.envs.some((e) => e.current)) {
    // 관리자가 다른 사람의 작업공간을 연 경우
    options.push(h("option", { value: "", selected: true }, `작업공간: ${me.env.owner} / ${me.env.name}`));
  }
  for (const e of me.envs) {
    options.push(h("option", {
      value: e.current ? "" : e.url, selected: e.current, disabled: !e.running && !e.current,
    }, `작업공간: ${e.name}${e.running ? "" : " (꺼짐)"}`));
  }
  const sel = h("select", {
    class: "mgs-select", title: "작업공간 바꾸기",
    onchange: (ev) => { const u = ev.target.value; if (u) location.href = u; },
  }, options);
  return h("div", { class: "mgs-head" },
    titleRow(),
    h("div", { class: "mgs-sub", text: `${me.user.display} · 모든 생성은 메인 GPU에서 순서대로 한 건씩 실행됩니다` }),
    options.length > 1 ? sel : null,
    me.env.mine ? null : h("div", { class: "mgs-warn", text: `${me.env.owner} 님의 작업공간을 보고 있습니다` }));
}

function queueCard() {
  const q = state.queue;
  const card = h("div", { class: "mgs-card mgs-queue" });
  if (!q) return card;
  if (q.blocked) card.append(h("div", { class: "mgs-warn", text: "GPU 메모리 확인 문제로 대기열이 멈췄습니다. 관리자에게 알려 주세요. (접수는 계속됩니다)" }));
  if (q.paused) card.append(h("div", { class: "mgs-warn", text: "관리자가 대기열을 일시 정지했습니다. (접수는 계속됩니다)" }));
  const r = q.running;
  card.append(h("div", {},
    r ? h("span", {}, "GPU 사용 중: ", h("b", { text: r.user }), ` · ${KIND_LABEL[r.kind] || r.kind}${r.env ? ` · ${r.env}` : ""} · ${fmtElapsed(r.elapsed_s)}`)
      : h("span", { class: "mgs-ok", text: "GPU 비어 있음" })));
  const waiting = q.queue.filter((x) => !x.running).length;
  card.append(h("div", { class: "mgs-sub", text: `대기 ${waiting}건` }));
  for (const m of q.mine) {
    card.append(h("div", {
      text: m.running ? `내 작업 #${m.seq}: 실행 중 (${fmtElapsed(m.elapsed_s)})` : `내 작업 #${m.seq}: 대기 ${m.position}번째`,
    }));
  }
  return card;
}

// ── 프롬프트 도우미 ──────────────────────────────────────

function textArea(value, placeholder, rows, oninput) {
  const ta = h("textarea", { class: "mgs-input", rows, placeholder, oninput: (ev) => oninput(ev.target.value) });
  ta.value = value;
  return ta;
}

function helperCard() {
  const hp = state.helper;
  const card = h("div", { class: "mgs-card mgs-helper" });
  card.append(h("div", { class: "mgs-row" },
    h("b", { text: "프롬프트 도우미" }),
    h("button", { class: "mgs-link", onclick: () => { hp.open = !hp.open; redrawHelper(); } }, hp.open ? "접기" : "펼치기")));
  if (!hp.open) {
    card.append(h("div", { class: "mgs-sub", text: "짧게 적으면 조명·카메라·배경·질감을 채운 생성용 영문 프롬프트로 늘려 줍니다." }));
    return card;
  }
  card.append(
    textArea(hp.text, "예: 책상 위 고양이 / 비 오는 날 우산을 씌워 주는 청년", 3, (v) => { hp.text = v; }),
    textArea(hp.notes, "꼭 지킬 특징 (선택) 예: 손은 항상 긴 소매 속에 숨김", 2, (v) => { hp.notes = v; }),
    textArea(hp.style, "그림체 고정 문장 (선택, 영문). 적으면 끝에 그대로 붙이고 다른 그림체 말은 막습니다.", 2,
      (v) => { hp.style = v; save("style", v); }),
    h("label", { class: "mgs-row" },
      h("input", { type: "checkbox", checked: hp.crew, onchange: (ev) => { hp.crew = ev.target.checked; save("crew", hp.crew ? "1" : "0"); } }),
      h("span", { class: "mgs-sub", text: "제작진 모드: 제품·공간·풍경 요청이면 전문가 지식으로 기획·연출·촬영·디테일을 먼저 정합니다 (캐릭터 요청은 건너뜀)" })),
    h("div", { class: "mgs-row" },
      h("button", { class: "mgs-btn primary", disabled: hp.busy, onclick: runExpand }, hp.busy ? "메인 GPU에서 작성 중…" : "프롬프트 확장"),
      h("span", { class: "mgs-sub", text: "GPU 대기열을 거칩니다" })));
  const r = hp.result;
  if (r) {
    if (r.crew?.used) card.append(crewSheet(r.crew));
    card.append(h("div", { class: "mgs-code", text: r.prompt }));
    card.append(h("div", { class: "mgs-sub", text: `규칙 검사 ${r.rounds}회 · ${r.model}` }));
    if (r.violations?.length) {
      card.append(h("div", { class: "mgs-warn", text: "아직 규칙에 어긋나는 점이 있습니다 (그대로 써도 되지만 확인하세요):" }));
      for (const v of r.violations) card.append(h("div", { class: "mgs-sub", text: `· ${v}` }));
    } else {
      card.append(h("div", { class: "mgs-ok", text: "규칙 검사 통과: 주제 유지 · 조명·카메라·배경·질감 포함 · 부정어 없음" }));
    }
    for (const a of r.advice || []) card.append(h("div", { class: "mgs-sub", text: `참고: ${a}` }));
    card.append(h("div", { class: "mgs-row" },
      h("button", { class: "mgs-btn", onclick: () => copyText(r.prompt, "프롬프트를 복사했습니다") }, "복사"),
      h("button", { class: "mgs-btn", onclick: () => putIntoSelectedNode(r.prompt) }, "선택한 노드에 넣기")));
  }
  return card;
}

// 제작진 모드가 쓰였을 때 보여 주는 한국어 지시서: 고른 항목과 [기획][연출][촬영·조명][디테일]
function crewSheet(c) {
  const box = h("div", { class: "mgs-item" });
  box.append(h("div", { class: "mgs-name", text: `제작진 지시서 · ${c.title}` }));
  box.append(h("div", { class: "mgs-sub", text: c.picked.map((p) => `${p.step}: ${p.ko}`).join(" / ") }));
  const parts = [["기획", c.ko.plan], ["연출", c.ko.direction], ["촬영·조명", c.ko.camera_lighting], ["디테일", c.ko.details]];
  for (const [name, text] of parts) if (text) box.append(h("div", { class: "mgs-sub", text: `[${name}] ${text}` }));
  return box;
}

function redrawHelper() {
  const old = state.root?.querySelector(".mgs-helper");
  if (old) old.replaceWith(helperCard());
}

async function runExpand() {
  const hp = state.helper;
  if (hp.busy) return;
  if (!hp.text.trim()) { toast("info", "요청을 먼저 적어 주세요"); return; }
  hp.busy = true;
  redrawHelper();
  try {
    const key = helperKey();
    hp.result = await postJSON("/mgs/api/expand", { text: hp.text, notes: hp.notes, style: hp.style, crew: hp.crew });
    hp.resultFor = key;
  } catch (e) {
    if (e instanceof LoginError) { state.error = "login"; draw(); return; }
    toast("error", "프롬프트를 확장하지 못했습니다", e.message, 8000);
  } finally {
    hp.busy = false;
    redrawHelper();
  }
}

function putIntoSelectedNode(text) {
  // 선택한 노드의 글 입력칸(CLIPTextEncode 의 text, Qwen 편집 노드의 prompt 등)에 넣는다
  const sel = Object.values(app.canvas?.selected_nodes || {});
  for (const node of sel) {
    const w = node.widgets?.find((x) => ["text", "prompt", "positive", "text_g"].includes(x.name) && typeof x.value === "string");
    if (!w) continue;
    w.value = text;
    w.callback?.(w.value, app.canvas, node);
    app.canvas?.setDirty?.(true, true);
    markChanged();
    toast("success", "노드에 넣었습니다", node.title || node.type);
    return;
  }
  toast("info", "글 입력칸이 있는 노드를 먼저 선택하세요", "예: CLIP Text Encode (Prompt)", 6000);
}

// ── 영상 프롬프트 (렌더러) ─────────────────────────────────
// 요청·특징·그림체는 프롬프트 도우미 칸의 글을 그대로 쓴다. 첫 화면은 화면의 LoadImage 노드에서 찾는다.

const helperKey = () => JSON.stringify([state.helper.text, state.helper.notes, state.helper.style]);
// 지금 도우미 칸의 요청으로 만든 확장 결과만 장면으로 쓴다
const freshScene = () => (state.helper.result?.body && state.helper.resultFor === helperKey() ? state.helper.result.body : "");

function findFirstFrame() {
  const isLoad = (n) => (n.comfyClass || n.type) === "LoadImage";
  const live = (n) => n.mode !== 2 && n.mode !== 4;   // 꺼 둔(mute)·건너뛰는(bypass) 노드는 첫 화면이 아니다
  const sel = Object.values(app.canvas?.selected_nodes || {});
  const graph = currentGraph();
  const nodes = (graph?._nodes || graph?.nodes || []).filter((n) => isLoad(n) && live(n));
  // 선택한 노드 → 영상 노드의 start_image 에 연결된 노드 → 첫 LoadImage 순으로 고른다
  const wired = nodes.find((n) => (n.outputs?.[0]?.links || []).some((id) => {
    const link = graph?.links?.[id] ?? graph?.links?.get?.(id);
    const target = link && graph.getNodeById?.(link.target_id);
    return target?.inputs?.[link.target_slot]?.name === "start_image";
  }));
  const node = sel.find(isLoad) || wired || nodes[0];
  const w = node?.widgets?.find((x) => x.name === "image");
  let v = String(w?.value || "").trim();
  if (!v) return null;
  let type = "input";
  const m = v.match(/^(.*) \[(input|output|temp)\]$/);
  if (m) { v = m[1]; type = m[2]; }
  // 서버가 읽을 수 있는 것만 보낸다 (입력·결과 폴더의 png/jpg/webp). 그 밖은 글만 보고 쓴다
  if (type === "temp" || !/\.(png|jpe?g|webp)$/i.test(v)) return null;
  const i = v.lastIndexOf("/");
  return { filename: i >= 0 ? v.slice(i + 1) : v, subfolder: i >= 0 ? v.slice(0, i) : "", type, label: v };
}

function videoCard() {
  const vd = state.video;
  const card = h("div", { class: "mgs-card mgs-video" });
  card.append(h("div", { class: "mgs-row" },
    h("b", { text: "영상 프롬프트" }),
    h("button", { class: "mgs-link", onclick: () => { vd.open = !vd.open; if (vd.open) loadVideoProfiles(); redrawVideo(); } },
      vd.open ? "접기" : "펼치기")));
  if (!vd.open) {
    card.append(h("div", { class: "mgs-sub", text: "한 줄 요청과 첫 화면을 보고 영상 모델용 프롬프트를 씁니다. 장문 프로필은 러다이트처럼 구도 · 카메라와 빛 · 시간 순서 · 마무리 네 단락의 긴 영어 프롬프트를 씁니다." }));
    return card;
  }
  const profs = vd.profiles || [];
  const cur = profs.find((p) => p.id === vd.profile) || profs[0];
  card.append(h("div", { class: "mgs-sub", text: "요청·꼭 지킬 특징·그림체는 위 프롬프트 도우미 칸의 글을 씁니다. 제작진 모드 체크도 그대로 따릅니다." }));
  if (!profs.length) {
    if (vd.loadError) {
      card.append(h("div", { class: "mgs-warn", text: `영상 모델 목록을 불러오지 못했습니다: ${vd.loadError}` }),
        h("button", { class: "mgs-btn", onclick: () => { vd.loadError = null; loadVideoProfiles(); } }, "다시 시도"));
    } else {
      card.append(h("div", { class: "mgs-sub", text: "영상 모델 목록을 불러오는 중…" }));
      if (!vd.loading) loadVideoProfiles();   // 패널을 다시 그렸을 때도 목록을 채운다
    }
    return card;
  }
  card.append(h("select", {
    class: "mgs-select",
    onchange: (ev) => { vd.profile = ev.target.value; save("videoProfile", vd.profile); redrawVideo(); },
  }, profs.map((p) => h("option", { value: p.id, selected: p.id === cur.id, text: p.title }))));
  card.append(h("label", { class: "mgs-row" },
    h("span", { class: "mgs-sub", text: `길이(초, 최대 ${cur.max_seconds})` }),
    h("input", {
      class: "mgs-input", type: "number", min: "1", max: String(cur.max_seconds), step: "0.5", value: String(vd.seconds),
      style: "width:90px",
      oninput: (ev) => { vd.seconds = Math.min(cur.max_seconds, Math.max(1, Number(ev.target.value) || cur.default_seconds)); },
    })));
  if (cur.mode === "i2v") {
    const ff = findFirstFrame();
    card.append(h("div", { class: "mgs-sub", text: ff ? `첫 화면: ${ff.label}` : "첫 화면: LoadImage 노드를 찾지 못했습니다 (요청 글만 보고 씁니다)" }));
    card.append(h("label", { class: "mgs-row" },
      h("input", { type: "checkbox", checked: vd.readFrame, onchange: (ev) => { vd.readFrame = ev.target.checked; save("videoRead", vd.readFrame ? "1" : "0"); } }),
      h("span", { class: "mgs-sub", text: "첫 화면을 시각 모델로 읽어 실제 그림에 있는 것만 움직이게 (GPU 차례 한 번 더)" })));
  } else if (cur.long) {
    card.append(h("div", { class: "mgs-sub", text: freshScene()
      ? "장면: 위 도우미의 확장 결과를 바탕으로 네 단락의 장문 프롬프트를 씁니다"
      : "장면: 도우미 확장 결과가 없어 같은 GPU 차례 안에서 장면부터 쓰고 장문으로 풀어 씁니다" }));
  } else {
    card.append(h("div", { class: "mgs-sub", text: freshScene()
      ? "장면: 위 도우미의 확장 결과를 쓰고 그 뒤에 움직임을 붙입니다"
      : "장면: 도우미 확장 결과가 없어 같은 GPU 차례 안에서 장면부터 씁니다" }));
  }
  card.append(h("div", { class: "mgs-row" },
    h("button", { class: "mgs-btn primary", disabled: vd.busy, onclick: runVideo }, vd.busy ? "메인 GPU에서 작성 중…" : "영상 프롬프트 만들기"),
    h("span", { class: "mgs-sub", text: "GPU 대기열을 거칩니다" })));
  const r = vd.result;
  if (r) {
    if (r.crew?.used) {
      const box = h("div", { class: "mgs-item" });
      box.append(h("div", { class: "mgs-name", text: `영상 제작진 · ${r.crew.title}` }));
      box.append(h("div", { class: "mgs-sub", text: r.crew.picked.map((p) => `${p.step}: ${p.ko}`).join(" / ") }));
      card.append(box);
    }
    const plan = [["카메라", r.ko?.camera], ["움직임", r.ko?.motion], ["시간", r.ko?.timing]];
    for (const [name, text] of plan) if (text) card.append(h("div", { class: "mgs-sub", text: `[${name}] ${text}` }));
    if (r.frame) {
      const f = r.frame;
      card.append(h("div", { class: "mgs-sub", text: `첫 화면 읽기: ${f.subject}${f.setting ? ` · ${f.setting}` : ""}${f.movable?.length ? ` · 움직일 수 있는 것: ${f.movable.join(", ")}` : ""}` }));
    }
    card.append(h("div", { class: "mgs-code", text: r.prompt }));
    const s = r.settings || {};
    card.append(h("div", { class: "mgs-sub", text: `권장 설정: ${s.frames}프레임 · ${s.fps}fps (약 ${s.seconds}초) · ${r.profile_title} · 규칙 검사 ${r.rounds}회` }));
    if (r.violations?.length) {
      card.append(h("div", { class: "mgs-warn", text: "아직 규칙에 어긋나는 점이 있습니다 (그대로 써도 되지만 확인하세요):" }));
      for (const v of r.violations) card.append(h("div", { class: "mgs-sub", text: `· ${v}` }));
    } else {
      card.append(h("div", { class: "mgs-ok", text: r.long
        ? "규칙 검사 통과: 사용자 말 · 한 컷 · 카메라 이동 하나 · 네 단락 · 부정어 없음 · 품질 유행어 없음"
        : "규칙 검사 통과: 사용자 말 · 한 컷 · 카메라 이동 하나 · 외형 반복 없음 · 부정어 없음" }));
    }
    if (r.scene_violations?.length) {
      card.append(h("div", { class: "mgs-warn", text: "장면(도우미 확장)에 남은 점:" }));
      for (const v of r.scene_violations) card.append(h("div", { class: "mgs-sub", text: `· ${v}` }));
    }
    for (const a of r.advice || []) card.append(h("div", { class: "mgs-sub", text: `참고: ${a}` }));
    if (r.prompt) {   // 빈 결과를 노드에 넣어 사용자의 글을 지우지 않게
      card.append(h("div", { class: "mgs-row" },
        h("button", { class: "mgs-btn", onclick: () => copyText(r.prompt, "영상 프롬프트를 복사했습니다") }, "복사"),
        h("button", { class: "mgs-btn", onclick: () => putIntoSelectedNode(r.prompt) }, "선택한 노드에 넣기")));
    }
  }
  return card;
}

function redrawVideo() {
  const old = state.root?.querySelector(".mgs-video");
  if (old) old.replaceWith(videoCard());
}

async function loadVideoProfiles() {
  const vd = state.video;
  if (vd.profiles?.length || vd.loading) return;
  vd.loading = true;
  try {
    vd.profiles = (await getJSON("/mgs/api/video_profiles")).profiles || [];
    const cur = vd.profiles.find((p) => p.id === vd.profile) || vd.profiles[0];
    if (cur) { vd.profile = cur.id; vd.seconds = Math.min(vd.seconds, cur.max_seconds) || cur.default_seconds; }
    else vd.loadError = "프로필이 없습니다";
  } catch (e) {
    if (e instanceof LoginError) { vd.loading = false; state.error = "login"; draw(); return; }
    vd.loadError = e.message;   // 다시 그릴 때마다 다시 부르지 않게 오류를 남기고 '다시 시도' 를 보여 준다
  } finally {
    vd.loading = false;
  }
  redrawVideo();
}

async function runVideo() {
  const vd = state.video;
  const hp = state.helper;
  if (vd.busy) return;
  if (!hp.text.trim()) { toast("info", "프롬프트 도우미 칸에 요청을 먼저 적어 주세요"); hp.open = true; redrawHelper(); return; }
  const cur = (vd.profiles || []).find((p) => p.id === vd.profile);
  const ff = cur?.mode === "i2v" ? findFirstFrame() : null;
  vd.busy = true;
  redrawVideo();
  try {
    vd.result = await postJSON("/mgs/api/expand_video", {
      text: hp.text, notes: hp.notes, style: hp.style, crew: hp.crew, profile: vd.profile, seconds: vd.seconds,
      image: ff ? { filename: ff.filename, subfolder: ff.subfolder, type: ff.type } : null,
      image_prompt: freshScene(), read_frame: vd.readFrame,
    });
  } catch (e) {
    if (e instanceof LoginError) { state.error = "login"; draw(); return; }
    toast("error", "영상 프롬프트를 만들지 못했습니다", e.message, 8000);
  } finally {
    vd.busy = false;
    redrawVideo();
  }
}

// ── 결과 자동 검사 ──────────────────────────────────────

function checkCard() {
  const ck = state.check;
  const card = h("div", { class: "mgs-card mgs-check" });
  const box = h("input", {
    type: "checkbox", checked: ck.on,
    onchange: (ev) => { ck.on = ev.target.checked; save("on", ck.on ? "1" : "0"); redrawCheck(); },
  });
  card.append(h("label", { class: "mgs-row" }, box, h("b", { text: "결과 자동 검사" })));
  card.append(h("div", { class: "mgs-sub", text: "이 화면에서 실행한 결과 이미지를 시각 모델이 보고 팔·다리·머리 오류와 아래 특징을 확인합니다. 한 장마다 GPU 대기열을 한 번 더 씁니다." }));
  if (ck.on) {
    card.append(textArea(ck.expect, "꼭 보여야 할 특징, 한 줄에 하나 (선택) 예: hands hidden inside long sleeves", 2,
      (v) => { ck.expect = v; save("expect", v); }));
  }
  for (const r of ck.results.slice(0, 6)) card.append(checkResult(r));
  return card;
}

function checkResult(r) {
  const row = h("div", { class: "mgs-item" });
  row.append(h("div", { class: "mgs-name", text: r.file }));
  if (r.state === "wait") { row.append(h("div", { class: "mgs-sub", text: "검사 대기 중 (GPU 대기열)…" })); return row; }
  if (r.state === "error") { row.append(h("div", { class: "mgs-warn", text: `검사 실패: ${r.error}` })); return row; }
  const d = r.data;
  row.append(h("div", { class: d.ok ? "mgs-ok" : "mgs-warn", text: d.ok ? `문제 없음 · 인물 ${d.people}` : `확인 필요 · 인물 ${d.people}` }));
  for (const i of d.issues) row.append(h("div", { class: "mgs-sub", text: `· ${i.type}${i.where ? ` (${i.where})` : ""}: ${i.detail}` }));
  for (const t of d.traits) row.append(h("div", { class: "mgs-sub", text: `${t.present ? "✓" : "✗"} ${t.trait}${t.note ? ` · ${t.note}` : ""}` }));
  if (!d.ok) {
    row.append(h("div", { class: "mgs-row" },
      h("button", { class: "mgs-btn", onclick: rerollAndQueue }, "시드 바꿔 다시 실행")));
  }
  return row;
}

function redrawCheck() {
  const old = state.root?.querySelector(".mgs-check");
  if (old) old.replaceWith(checkCard());
}

async function onExecuted(ev) {
  const ck = state.check;
  if (!ck.on) return;
  const images = ev?.detail?.output?.images || [];
  for (const im of images) {
    if ((im.type || "output") !== "output") continue;
    const key = `${im.subfolder || ""}/${im.filename}`;
    if (ck.seen.has(key)) continue;
    ck.seen.add(key);
    const r = { file: key.replace(/^\//, ""), state: "wait" };
    ck.results.unshift(r);
    ck.results.length = Math.min(ck.results.length, 20);
    redrawCheck();
    try {
      r.data = await postJSON("/mgs/api/inspect", {
        filename: im.filename, subfolder: im.subfolder || "", type: "output",
        expect: ck.expect.split("\n").map((s) => s.trim()).filter(Boolean),
      });
      r.state = "done";
      if (!r.data.ok) toast("warn", "결과 자동 검사: 확인이 필요합니다", r.file, 8000);
    } catch (e) {
      r.state = "error";
      r.error = e.message;
    }
    redrawCheck();
  }
}

async function rerollAndQueue() {
  // 지금 화면의 워크플로우에서 시드 값만 새로 뽑아 다시 실행한다 (프롬프트와 설정은 그대로)
  const nodes = currentGraph()?._nodes || currentGraph()?.nodes || [];
  let n = 0;
  for (const node of nodes) {
    for (const w of node.widgets || []) {
      if (w.name === "seed" || w.name === "noise_seed") {
        w.value = Math.floor(Math.random() * 2 ** 48);
        w.callback?.(w.value, app.canvas, node);
        n++;
      }
    }
  }
  if (!n) { toast("info", "시드 입력칸이 있는 노드가 없습니다"); return; }
  markChanged();
  try {
    await app.queuePrompt(0, 1);
    toast("success", "시드를 바꿔 다시 실행했습니다", `시드 ${n}곳`);
  } catch (e) {
    toast("error", "다시 실행하지 못했습니다", e.message);
  }
}

function tabs() {
  const c = state.catalog || {};
  return h("div", { class: "mgs-tabs" }, KINDS.map(([k, label]) =>
    h("button", {
      class: `mgs-tab${state.kind === k ? " on" : ""}`,
      onclick: () => { state.kind = k; draw(); },
    }, `${label} ${(c[k] || []).length}`)));
}

function search() {
  return h("input", {
    class: "mgs-input", placeholder: "모델 이름 검색", value: state.filter,
    oninput: (ev) => {
      state.filter = ev.target.value;
      const old = state.root?.querySelector(".mgs-list");
      if (old) old.replaceWith(list());
    },
  });
}

function list() {
  const items = ((state.catalog || {})[state.kind] || [])
    .filter((it) => !state.filter || it.name.toLowerCase().includes(state.filter.toLowerCase()));
  const box = h("div", { class: "mgs-list" });
  if (!items.length) {
    box.append(h("div", { class: "mgs-empty", text: state.filter ? "검색 결과가 없습니다." : "이 분류의 모델이 메인에 없습니다." }));
    return box;
  }
  for (const it of items) box.append(state.kind === "llm" || state.kind === "embed" ? llmItem(it) : modelItem(it));
  return box;
}

function modelItem(it) {
  const row = h("div", { class: "mgs-row" });
  if (it.has_starter || it.basic_graph) {
    row.append(h("button", {
      class: "mgs-btn primary", disabled: state.busy,
      title: it.has_starter ? "이 모델을 쓰는 공식 워크플로우를 새 탭으로 엽니다" : "이 체크포인트로 기본 이미지 생성 그래프를 새 탭에 만듭니다",
      onclick: () => openStarter(it),
    }, "시작 워크플로우"));
  }
  if (it.loader) {
    row.append(h("button", {
      class: "mgs-btn", disabled: state.busy, title: `지금 캔버스에 ${it.loader.node} 노드를 넣습니다`,
      onclick: () => insertLoader(it),
    }, "로더 추가"));
  }
  const alts = state.alts[it.name];
  return h("div", { class: "mgs-item" },
    h("div", { class: "mgs-name", text: it.name }),
    h("div", { class: "mgs-sub", text: `${it.folder} · ${it.size_gb}GB` }),
    row,
    alts && alts.length ? h("div", { class: "mgs-row" }, h("span", { class: "mgs-sub", text: "다른 템플릿:" }),
      alts.map((a) => h("button", {
        class: "mgs-btn", disabled: state.busy, title: a.missing ? `메인에 없는 모델 ${a.missing}개` : "",
        onclick: () => openStarter(it, a.name),
      }, a.title + (a.missing ? " ⚠" : "")))) : null);
}

function llmItem(it) {
  const me = state.me;
  const isEmbed = state.kind === "embed";
  // Windows 명령 프롬프트·PowerShell 모두에서 되도록 curl.exe 와 영문 예시 문장을 쓴다
  const example = isEmbed
    ? `curl.exe ${me.llm_url}/api/embed -H "Authorization: Bearer <API key>" -d "{\\"model\\":\\"${it.name}\\",\\"input\\":\\"sample sentence\\"}"`
    : `curl.exe ${me.llm_url}/v1/chat/completions -H "Authorization: Bearer <API key>" -H "Content-Type: application/json" -d "{\\"model\\":\\"${it.name}\\",\\"messages\\":[{\\"role\\":\\"user\\",\\"content\\":\\"hello\\"}]}"`;
  return h("div", { class: "mgs-item" },
    h("div", { class: "mgs-name", text: it.name }),
    h("div", { class: "mgs-sub", text: [it.params, it.quant, it.size_gb ? `${it.size_gb}GB` : ""].filter(Boolean).join(" · ") }),
    h("div", { class: "mgs-row" },
      isEmbed ? null : h("button", {
        class: "mgs-btn primary",
        onclick: () => { logSelect("llm", it.name, "chat"); window.open(me.links.webui, "_blank", "noopener"); },
      }, "채팅 열기"),
      h("button", { class: "mgs-btn", onclick: () => { logSelect(state.kind, it.name, "api"); copyText(example, "API 예시를 복사했습니다 (<API key> 를 바꿔 쓰세요)"); } }, "API 예시 복사"),
      h("button", { class: "mgs-btn", onclick: () => copyText(it.name, "모델 이름을 복사했습니다") }, "이름 복사")));
}

function keyBox() {
  if (!state.newKey) return null;
  return h("div", { class: "mgs-card" },
    h("div", { text: "새 API 키 (지금만 보입니다)" }),
    h("div", { class: "mgs-code", text: state.newKey }),
    h("div", { class: "mgs-row" },
      h("button", { class: "mgs-btn", onclick: () => copyText(state.newKey, "API 키를 복사했습니다") }, "복사"),
      h("button", { class: "mgs-btn", onclick: () => { state.newKey = null; draw(); } }, "닫기")));
}

async function issueKey() {
  if (state.issuing) return;
  state.issuing = true;
  try {
    state.newKey = (await postJSON("/mgs/api/keys", { label: "ComfyUI 패널" })).key;
    draw();
  } catch (e) {
    if (e instanceof LoginError) { state.error = "login"; draw(); }
    else toast("error", "API 키를 발급하지 못했습니다", e.message);
  } finally {
    state.issuing = false;
  }
}

function logout() {
  // 로그아웃은 POST 로만 받는다 (다른 화면이 몰래 로그아웃시키지 못하게)
  const form = h("form", { method: "post", action: state.me.links.logout, style: "display:none" });
  document.body.append(form);
  form.submit();
}

function footer() {
  const { me } = state;
  const L = me.links;
  const link = (text, href) => h("a", { class: "mgs-link", href, target: "_blank", rel: "noopener", text });
  return h("div", { class: "mgs-foot" },
    link("작업 이력", L.history),
    link("작업공간 버전", L.versions),
    link("모델 추가", L.models),
    link("포털", L.portal),
    L.admin ? link("관리자", L.admin) : null,
    h("button", { class: "mgs-link", onclick: () => copyText(me.results_share, "결과 폴더 주소를 복사했습니다") }, "결과 폴더 주소"),
    me.env.mine ? h("button", { class: "mgs-link", onclick: issueKey }, "API 키 발급") : null,
    h("button", { class: "mgs-link", onclick: logout }, "로그아웃"));
}

// ── 동작 ──────────────────────────────────────────────

function logSelect(kind, model, action) {
  postJSON("/mgs/api/select", { kind, model, action }).catch(() => {});
}

function setBusy(v) {
  state.busy = v;
  state.root?.querySelectorAll(".mgs-list .mgs-btn").forEach((b) => { b.disabled = v; });
}

function currentGraph() {
  return app.canvas?.graph ?? app.graph;
}

function markChanged() {
  try { app.extensionManager.workflow?.activeWorkflow?.changeTracker?.captureCanvasState?.(); } catch { /* 선택 기능 */ }
}

async function setCombo(node, widgetName, value) {
  let w = node.widgets?.find((x) => x.name === widgetName);
  if (!w) return false;
  const has = () => !Array.isArray(w.options?.values) || w.options.values.includes(value);
  if (!has() && typeof app.refreshComboInNodes === "function") {
    await app.refreshComboInNodes();
    w = node.widgets?.find((x) => x.name === widgetName) || w;
  }
  w.value = value;
  w.callback?.(w.value, app.canvas, node);
  return has();
}

function addNode(type, pos) {
  const LG = window.LiteGraph;
  const node = LG?.createNode(type);
  if (!node) throw new Error(`${type} 노드가 이 작업공간에 없습니다 (필요한 커스텀 노드를 설치하세요)`);
  node.pos = pos;
  currentGraph().add(node);
  return node;
}

function canvasCenter(size = [300, 100]) {
  const area = app.canvas?.ds?.visible_area;
  if (!area) return [100, 100];
  const [x, y, w, hh] = area;
  return [x + w / 2 - size[0] / 2, y + hh / 2 - size[1] / 2];
}

async function insertLoader(it) {
  if (!it.loader || state.busy) return;
  setBusy(true);
  try {
    const node = addNode(it.loader.node, canvasCenter());
    node.pos = canvasCenter(node.size || [300, 100]);
    const listed = await setCombo(node, it.loader.widget, it.name);
    app.canvas?.selectNode?.(node);
    currentGraph().setDirtyCanvas?.(true, true);
    app.canvas?.setDirty?.(true, true);
    markChanged();
    logSelect(state.kind, it.name, "loader");
    if (listed) toast("success", "로더 노드를 넣었습니다", `${it.loader.node} · ${it.name}`);
    else toast("warn", "이 작업공간의 모델 목록에 아직 없습니다", "R 키로 새로고침한 뒤 노드에서 다시 고르세요.", 8000);
  } catch (e) {
    toast("error", "로더 노드를 넣지 못했습니다", e.message);
  } finally {
    setBusy(false);
  }
}

function tabName(title) {
  // 같은 이름의 저장된 워크플로우나 방금 연 탭이 대신 열리지 않게 초까지 붙이고, 경로 문자는 바꾼다
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const safe = String(title).replace(/[\\/:*?"<>|]/g, "_");
  return `${safe} ${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}`;
}

function warnMissingInputs() {
  // 공식 템플릿 브라우저와 달리 예시 입력 파일은 올리지 않는다. 비어 있는 입력 노드를 알려 준다.
  const nodes = currentGraph()?._nodes || currentGraph()?.nodes || [];
  const empty = [];
  for (const n of nodes) {
    const wname = INPUT_LOADERS[n.type];
    if (!wname) continue;
    const w = n.widgets?.find((x) => x.name === wname) || n.widgets?.[0];
    const values = w?.options?.values;
    if (w && Array.isArray(values) && !values.includes(w.value)) empty.push(n.title || n.type);
  }
  if (empty.length) {
    toast("warn", "입력 파일을 직접 올려 주세요", `${empty.join(", ")} 노드에 이 작업공간에 없는 예시 파일이 들어 있습니다.`, 12000);
  }
}

async function openStarter(it, templateName = null) {
  if (state.busy) return;
  setBusy(true);
  try {
    if (it.basic_graph && !it.has_starter) {
      await buildBasicCheckpointGraph(it);
      return;
    }
    const q = new URLSearchParams({ model: it.name, folder: it.folder });
    if (templateName) q.set("template", templateName);
    const data = await getJSON(`/mgs/api/starter?${q}`);
    const ok = await app.loadGraphData(data.workflow, true, true, tabName(data.title), { openSource: "template" });
    if (ok === false) throw new Error("워크플로우를 불러오지 못했습니다");
    if (ok === undefined) throw new Error("다른 워크플로우를 여는 중이라 취소되었습니다. 다시 눌러 주세요.");
    state.alts[it.name] = data.alternatives || [];
    logSelect(state.kind, it.name, `starter:${data.name}`);
    toast("success", "시작 워크플로우를 열었습니다", data.title);
    if (data.match === "family") {
      toast("info", "같은 모델의 다른 버전 템플릿입니다", `${data.source_file} 대신 메인의 ${it.name} 으로 바꿔 열었습니다.`, 8000);
    }
    if (data.missing && data.missing.length) {
      toast("warn", `메인에 없는 모델 ${data.missing.length}개`,
        `${data.missing.map((m) => `${m.directory}/${m.file}`).join(", ")} — '모델 추가'로 넣거나 관리자에게 요청하세요.`, 15000);
    }
    warnMissingInputs();
    const old = state.root?.querySelector(".mgs-list");
    if (old) old.replaceWith(list());
  } catch (e) {
    if (e instanceof LoginError) { state.error = "login"; draw(); return; }
    if (e.status === 404) toast("info", "공식 템플릿이 없습니다", "'로더 추가'로 노드를 넣어 쓰세요.", 6000);
    else toast("error", "시작 워크플로우를 열지 못했습니다", e.message);
  } finally {
    setBusy(false);
  }
}

async function buildBasicCheckpointGraph(it) {
  // 체크포인트(모델+CLIP+VAE 한 파일)용 기본 텍스트→이미지 그래프를 새 탭에 만든다
  const before = app.extensionManager.workflow?.activeWorkflow;
  await app.extensionManager.command.execute("Comfy.NewBlankWorkflow");
  const g = currentGraph();
  const count = (g?._nodes || g?.nodes || []).length;
  if (app.extensionManager.workflow?.activeWorkflow === before || count > 0) {
    // 새 탭이 열리지 않았다. 지금 작업 중인 워크플로우에 노드를 쏟아 넣지 않는다.
    throw new Error("새 탭을 만들지 못했습니다. '로더 추가'를 쓰세요.");
  }
  const ck = addNode("CheckpointLoaderSimple", [0, 180]);
  await setCombo(ck, "ckpt_name", it.name);
  const pos = addNode("CLIPTextEncode", [420, 60]);
  const neg = addNode("CLIPTextEncode", [420, 330]);
  const lat = addNode("EmptyLatentImage", [420, 600]);
  const ks = addNode("KSampler", [880, 160]);
  const dec = addNode("VAEDecode", [1260, 160]);
  const save = addNode("SaveImage", [1480, 160]);
  const setText = (n, t) => { const w = n.widgets?.find((x) => x.name === "text"); if (w) w.value = t; };
  setText(pos, "a beautiful photo");
  setText(neg, "low quality, blurry");
  const link = (a, out, b, inp) => {
    const o = a.findOutputSlot(out);
    const i = b.findInputSlot(inp);
    if (o < 0 || i < 0 || !a.connect(o, b, i)) throw new Error(`연결 실패: ${out} → ${inp}`);
  };
  link(ck, "MODEL", ks, "model");
  link(ck, "CLIP", pos, "clip");
  link(ck, "CLIP", neg, "clip");
  link(pos, "CONDITIONING", ks, "positive");
  link(neg, "CONDITIONING", ks, "negative");
  link(lat, "LATENT", ks, "latent_image");
  link(ks, "LATENT", dec, "samples");
  link(ck, "VAE", dec, "vae");
  link(dec, "IMAGE", save, "images");
  currentGraph().setDirtyCanvas?.(true, true);
  markChanged();
  logSelect(state.kind, it.name, "starter:basic_checkpoint");
  toast("success", "기본 이미지 생성 그래프를 만들었습니다", it.name);
}

function openPanel() {
  const st = app.extensionManager?.sidebarTab;
  if (st && typeof st.toggleSidebarTab === "function") {
    if (st.activeSidebarTabId !== TAB_ID) st.toggleSidebarTab(TAB_ID);
  } else {
    app.extensionManager?.command?.execute?.(`Workspace.ToggleSidebarTab.${TAB_ID}`);
  }
}

app.registerExtension({
  name: "mgs.MainGpuPanel",
  commands: [{ id: "mgs.MainGpu.Open", label: "메인 GPU 패널 열기", icon: "pi pi-server", function: openPanel }],
  keybindings: [
    { commandId: "mgs.MainGpu.Open", combo: { key: "g", alt: true, shift: true } },
    { commandId: "mgs.MainGpu.Open", combo: { key: "˝", alt: true, shift: true } },   // macOS Option+Shift+G
  ],
  actionBarButtons: [{ icon: "pi pi-server", label: "메인 GPU", tooltip: "모델 선택 · GPU 대기열 (Alt+Shift+G)", onClick: openPanel }],
  async setup() {
    app.extensionManager.registerSidebarTab({
      id: TAB_ID,
      title: "메인 GPU",
      label: "메인 GPU",
      tooltip: "모델 선택 · 시작 워크플로우 · GPU 대기열",
      icon: "pi pi-server",
      type: "custom",
      render: (container) => render(container),
      destroy: () => stopTimer(),
    });
    // 결과 자동 검사: 이 화면에서 실행한 작업이 끝날 때마다 (패널이 닫혀 있어도) 결과 이미지를 검사한다
    api.addEventListener("executed", (ev) => { onExecuted(ev); });
    // 처음 접속한 브라우저에서는 패널을 한 번 열어 위치를 알려 준다
    try {
      if (!localStorage.getItem("mgs.panel.intro")) {
        localStorage.setItem("mgs.panel.intro", "1");
        setTimeout(openPanel, 1500);
      }
    } catch { /* 저장소를 못 쓰는 브라우저 */ }
  },
});
