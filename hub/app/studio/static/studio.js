// 스튜디오 채팅 화면. 글은 모두 textContent 로만 넣는다(HTML 로 해석하지 않는다, 14.3).
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const feed = $("feed"), threadsEl = $("threads"), textEl = $("text"), sendEl = $("send"), fileEl = $("file");
  const attachEl = $("attach"), errorEl = $("error"), bannerEl = $("banner"), fullEl = $("full-notice");
  const state = { thread: null, threads: [], attachment: null, uploading: false, sending: false, shown: new Set(),
                  cards: new Map(), unread: new Set(), ws: null, retry: 0, lastId: 0 };

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => { const r = Math.random() * 16 | 0; return (c === "x" ? r : (r & 3 | 8)).toString(16); }));
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* 저장 못 해도 동작한다 */ } } };

  function showError(msg) { errorEl.textContent = msg || ""; errorEl.hidden = !msg; }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
    if (opts.json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
    if (opts.form) init.body = opts.form;
    let r;
    try { r = await fetch(path, init); } catch (e) { throw { code: "network", message: "연결이 끊겼습니다. 잠시 뒤 다시 시도해 주세요." }; }
    if (r.status === 401) { location.href = "/login?next=/studio"; throw { code: "login", message: "로그인이 필요합니다." }; }
    if (r.status === 204) return null;
    let body = null;
    try { body = await r.json(); } catch (e) { /* 본문 없음 */ }
    if (!r.ok) throw (body && body.error) || { code: "internal", message: "잠시 뒤 다시 보내 주세요." };
    return body;
  }

  // ── 처음 한 번 알림 (5초 뒤 사라진다, D3) ──
  function oneTimeNotice() {
    if (store.get("studio.sharedNotice")) return;
    store.set("studio.sharedNotice", "1");
    bannerEl.textContent = "결과(이미지·영상)는 공유 폴더에도 저장됩니다";
    bannerEl.hidden = false;
    setTimeout(() => { bannerEl.hidden = true; }, 5000);
  }

  // ── 그리기 ──
  function emptyState() {
    if (feed.children.length) return;
    feed.append(el("div", "empty", "사진을 올리면 영상으로, 글만 적으면 이미지로 만들어 드립니다. 사진만 보내도 영상이 됩니다."));
  }
  function clearEmpty() { const e = feed.querySelector(".empty"); if (e) e.remove(); }
  function scrollDown() { feed.scrollTop = feed.scrollHeight; }

  function attachmentNode(id, media) {
    const url = "/studio/api/files/" + id;
    if (media === "video") { const v = document.createElement("video"); v.src = url; v.controls = true; v.loop = true; v.playsInline = true; v.preload = "metadata"; return v; }
    const i = document.createElement("img"); i.src = url; i.className = "thumb"; i.alt = ""; return i;
  }

  function actionButtons(turnId, actions) {
    const acts = el("div", "acts");
    for (const a of actions) {
      const b = el("button", null, a.label); b.type = "button";
      b.addEventListener("click", async () => {
        b.disabled = true;
        const ok = a.id === "to_video" ? await toVideo(turnId) : await retry(turnId, a.id === "retry_seed" ? "new_seed" : "rewrite");
        if (!ok) b.disabled = false;       // 실패하면 다시 누를 수 있게 (429·409 등)
      });
      acts.append(b);
    }
    return acts;
  }

  function renderMessage(m) {
    if (state.shown.has(m.id)) return;
    state.shown.add(m.id); state.lastId = Math.max(state.lastId, m.id);
    clearEmpty();
    const wrap = el("div", "msg " + m.role);
    wrap.dataset.mid = m.id;
    const data = m.data || {};
    if (m.kind === "notice") {
      const n = el("div", "notice" + ((data.level === "error") ? " error" : ""), m.body);
      if (Array.isArray(data.actions) && data.actions.length) n.append(actionButtons(m.turn_id, data.actions));
      wrap.append(n);
    } else {
      const b = el("div", "bubble");
      if (m.body) b.append(document.createTextNode(m.body));
      for (const id of (data.attachment_ids || [])) b.append(attachmentNode(id, m.kind === "result" ? (data.media || "video") : "image"));
      if (m.kind === "result" && (data.notes || []).length) b.append(el("div", "note", data.notes.join(" · ")));
      if (m.kind === "result" && Array.isArray(data.actions) && data.actions.length) b.append(actionButtons(m.turn_id, data.actions));
      if (!m.body && !(data.attachment_ids || []).length) b.textContent = "…";
      wrap.append(b);
    }
    feed.append(wrap);
    if (m.kind === "result" || m.kind === "notice") dropCard(m.turn_id);
    scrollDown();
  }

  function cardFor(turnId) {
    let c = state.cards.get(turnId);
    if (c) return c;
    clearEmpty();
    const box = el("div", "msg assistant");
    const card = el("div", "progress");
    const h = el("h4", null, "준비 중");
    const chips = el("div", "chips");
    const bar = el("div", "bar indet"); bar.append(document.createElement("i"));
    const ptext = el("div", "ptext"); const label = el("span", null, "");
    const cancel = el("button", null, "취소"); cancel.type = "button";
    cancel.addEventListener("click", () => { cancel.disabled = true; api("/studio/api/turns/" + turnId + "/cancel", { method: "POST", json: {} }).catch((e) => showError(e.message)); });
    ptext.append(label, cancel);
    card.append(h, chips, bar, ptext); box.append(card);
    feed.append(box);
    c = { box, h, chips, bar, label };
    state.cards.set(turnId, c);
    scrollDown();
    return c;
  }
  function dropCard(turnId) { const c = state.cards.get(turnId); if (c) { c.box.remove(); state.cards.delete(turnId); } }

  function updateProgress(turnId, p) {
    if (!p) return;
    const c = cardFor(turnId);
    c.h.textContent = p.title || "준비 중";
    c.chips.replaceChildren(...(p.chips || []).map((x) => el("span", "chip " + x.state, x.label)));
    const pct = typeof p.pct === "number" ? p.pct : null;
    c.bar.classList.toggle("indet", pct === null);
    c.bar.firstChild.style.width = pct === null ? "" : pct + "%";
    c.label.textContent = (pct !== null ? (p.band ? p.band + " · " : "") + pct + "%" : (p.text || ""));
  }

  // ── 대화 ──
  async function loadThreads() {
    const r = await api("/studio/api/threads");
    state.threads = r.threads;
    threadsEl.replaceChildren(...r.threads.map((t) => {
      const li = el("li", (t.id === state.thread ? "on " : "") + (state.unread.has(t.id) ? "dot" : ""), t.title || "새 대화");
      li.addEventListener("click", () => openThread(t.id));
      return li;
    }));
  }

  async function openThread(id) {
    state.thread = id; state.unread.delete(id); state.shown = new Set(); state.lastId = 0;
    for (const c of state.cards.values()) c.box.remove();
    state.cards.clear(); feed.replaceChildren();
    store.set("studio.thread", id);
    document.body.classList.remove("menu");
    await loadMessages();
    await loadThreads();
    emptyState();
  }

  async function loadMessages() {
    const r = await api("/studio/api/threads/" + state.thread + "/messages?after=" + state.lastId);
    for (const m of r.messages) renderMessage(m);
    for (const t of r.turns) if (t.progress) updateProgress(t.id, t.progress); else cardFor(t.id);
    // 이미 끝난 요청의 카드는 지운다 (놓친 이벤트 보정)
    const open = new Set(r.turns.map((t) => t.id));
    for (const id of [...state.cards.keys()]) if (!open.has(id)) dropCard(id);
  }

  async function newThread() {
    const r = await api("/studio/api/threads", { method: "POST", json: {} });
    await openThread(r.thread.id);
    return r.thread.id;
  }

  // ── 보내기 ──
  function refreshSend() { sendEl.disabled = state.sending || state.uploading || !(textEl.value.trim() || state.attachment); }

  function renderAttach() {
    attachEl.replaceChildren();
    attachEl.hidden = !state.attachment && !state.uploading;
    if (state.uploading) { attachEl.append(el("span", null, "사진 올리는 중…")); return; }
    if (!state.attachment) return;
    const img = document.createElement("img"); img.src = "/studio/api/files/" + state.attachment.id; img.alt = "";
    const rm = el("button", null, "지우기"); rm.type = "button";
    rm.addEventListener("click", () => { state.attachment = null; renderAttach(); refreshSend(); });
    attachEl.append(img, rm);
  }

  async function onFile() {
    const f = fileEl.files[0]; fileEl.value = "";
    if (!f) return;
    showError("");
    state.uploading = true; renderAttach(); refreshSend();
    try {
      if (!state.thread) await newThread();
      const form = new FormData(); form.append("thread_id", state.thread); form.append("file", f);
      const r = await api("/studio/api/uploads", { method: "POST", form });
      state.attachment = r.attachment;
    } catch (e) { showError(e.message); }
    state.uploading = false; renderAttach(); refreshSend();
  }

  async function send(e) {
    e.preventDefault();
    if (sendEl.disabled) return;
    showError("");
    state.sending = true; refreshSend();
    try {
      if (!state.thread) await newThread();
      const body = { text: textEl.value, attachment_ids: state.attachment ? [state.attachment.id] : [], client_msg_id: uuid() };
      const options = {};
      if ($("opt-seconds").value) options.seconds = Number($("opt-seconds").value);
      if ($("opt-aspect").value) options.aspect = $("opt-aspect").value;
      if (Object.keys(options).length) body.options = options;
      const r = await api("/studio/api/threads/" + state.thread + "/messages", { method: "POST", json: body });
      textEl.value = ""; textEl.style.height = ""; state.attachment = null; renderAttach();
      renderMessage(r.message);
      if (r.reply) renderMessage(r.reply);
      if (r.turn && ["pending", "active"].includes(r.turn.state)) cardFor(r.turn.id);
      oneTimeNotice();
      loadThreads();
    } catch (ex) { showError(ex.message); }
    state.sending = false; refreshSend();
  }

  async function toVideo(turnId) {
    showError("");
    try {
      const r = await api("/studio/api/turns/" + turnId + "/to_video", { method: "POST", json: { client_msg_id: uuid() } });
      if (r.message) renderMessage(r.message);
      if (r.turn) cardFor(r.turn.id);
      return true;
    } catch (ex) { showError(ex.message); return false; }
  }

  async function retry(turnId, mode) {
    showError("");
    try {
      const r = await api("/studio/api/turns/" + turnId + "/retry", { method: "POST", json: { mode, client_msg_id: uuid() } });
      renderMessage(r.message);
      if (r.turn) cardFor(r.turn.id);
      return true;
    } catch (ex) { showError(ex.message); return false; }
  }

  // ── 실시간 ──
  function connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/studio/ws");
    state.ws = ws;
    ws.onopen = () => { state.retry = 0; if (state.thread) loadMessages().catch(() => {}); };
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === "turn.progress") { if (m.thread_id === state.thread) updateProgress(m.turn_id, m.progress); }
      else if (m.type === "message.new") {
        if (m.thread_id === state.thread) renderMessage(m.message);
        else { state.unread.add(m.thread_id); loadThreads().catch(() => {}); }
      } else if (m.type === "turn.state") {
        if (["failed", "cancelled", "interrupted"].includes(m.state)) dropCard(m.turn_id);
      }
    };
    ws.onclose = (ev) => {
      if (ev.code === 4401) { location.href = "/login?next=/studio"; return; }
      if (ev.code === 4403 || ev.code === 1008) return;
      setTimeout(connect, Math.min(15000, 1000 * 2 ** state.retry++));
    };
    const timer = setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 30000);
    ws.addEventListener("close", () => clearInterval(timer));
  }

  // ── 시작 ──
  async function init() {
    const notice = JSON.parse(document.body.dataset.notice || "null");
    $("logout").addEventListener("click", async () => { try { await api("/studio/logout", { method: "POST", json: {} }); } catch (e) { /* 이미 로그아웃 */ } location.href = "/login?next=/studio"; });
    if (notice) {
      fullEl.textContent = notice; fullEl.hidden = false; feed.hidden = true; $("composer").hidden = true;
      return;
    }
    $("new-thread").addEventListener("click", () => { newThread().catch((e) => showError(e.message)); });
    $("composer").addEventListener("submit", send);
    textEl.addEventListener("input", () => { textEl.style.height = "auto"; textEl.style.height = Math.min(160, textEl.scrollHeight) + "px"; refreshSend(); });
    textEl.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); } });
    fileEl.addEventListener("change", onFile);
    textEl.addEventListener("paste", (e) => {
      const f = [...(e.clipboardData ? e.clipboardData.files : [])].find((x) => x.type.startsWith("image/"));
      if (f) { e.preventDefault(); const dt = new DataTransfer(); dt.items.add(f); fileEl.files = dt.files; onFile(); }
    });
    await loadThreads();
    const saved = store.get("studio.thread");
    const start = state.threads.find((t) => t.id === saved) || state.threads[0];
    if (start) await openThread(start.id); else emptyState();
    connect();
    refreshSend();
  }
  init().catch((e) => showError(e.message || "화면을 불러오지 못했습니다."));
})();
