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

  // 크게 보기: 브라우저의 기본 전체 화면 단추가 안 눌리는 환경(앱 안 브라우저 창 등)에서도 되도록 화면 가득 덮는 보기를 직접 만든다.
  // 가능하면 진짜 전체 화면도 함께 켠다 (안 되면 덮개만 보인다). Esc·닫기·바깥을 누르면 닫힌다
  function bigView(src) {
    const t0 = src.currentTime, wasPlaying = !src.paused;
    src.pause();
    const over = el("div", "vbig");
    const v = document.createElement("video");
    v.src = src.src; v.controls = true; v.loop = true; v.autoplay = true; v.playsInline = true;
    v.addEventListener("loadedmetadata", () => { try { v.currentTime = t0; } catch (e) { /* 처음부터 */ } }, { once: true });
    const close = el("button", "vclose", "닫기 ✕"); close.type = "button";
    let closed = false;
    const done = () => {
      if (closed) return; closed = true;
      document.removeEventListener("keydown", onKey, true); document.removeEventListener("fullscreenchange", onFs);
      try { src.currentTime = v.currentTime; } catch (e) { /* 그대로 */ }
      v.pause(); over.remove();
      if (document.fullscreenElement) { const p = document.exitFullscreen(); if (p && p.catch) p.catch(() => {}); }
      if (wasPlaying) { const p = src.play(); if (p && p.catch) p.catch(() => {}); }
    };
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); done(); } };
    const onFs = () => { if (!document.fullscreenElement) done(); };
    close.addEventListener("click", done);
    over.addEventListener("click", (e) => { if (e.target === over) done(); });
    document.addEventListener("keydown", onKey, true);
    over.append(v, close); document.body.append(over);
    try {
      const p = over.requestFullscreen ? over.requestFullscreen() : null;
      if (p && p.then) p.then(() => document.addEventListener("fullscreenchange", onFs)).catch(() => {});
    } catch (e) { /* 덮개만 보인다 */ }
    const pl = v.play(); if (pl && pl.catch) pl.catch(() => {});
  }

  function attachmentNode(id, media) {
    const url = "/studio/api/files/" + id;
    if (media === "video") {
      const wrap = el("div", "vwrap");
      const v = document.createElement("video"); v.src = url; v.controls = true; v.loop = true; v.playsInline = true; v.preload = "metadata";
      const big = el("button", "vfs", "⛶ 크게 보기"); big.type = "button"; big.title = "화면 가득 크게 봅니다 (Esc 로 닫기)";
      big.addEventListener("click", () => bigView(v));
      wrap.append(v, big);
      return wrap;
    }
    const i = document.createElement("img"); i.src = url; i.className = "thumb"; i.alt = ""; return i;
  }

  const PLAN_STATUS = { approved: "승인함", cancelled: "취소됨", expired: "시간이 지나 취소됨", replanned: "바꿔서 다시 계획함" };

  function setPlanStatus(card, status) {
    const acts = card.querySelector(".acts"); if (acts) acts.remove();
    let s = card.querySelector(".status");
    if (!s) { s = el("div", "status"); card.append(s); }
    s.textContent = PLAN_STATUS[status] || "";
  }

  function longPlanBody(card, p) {
    // 긴 영상(여러 장면을 이어 붙임): 장면표·배경음악·총 길이·예상 시간
    card.append(el("h4", null, "긴 영상 만들기 계획 (승인하면 시작합니다)"));
    const dl = el("dl");
    const row = (k, v) => { if (v) { dl.append(el("dt", null, k), el("dd", null, v)); } };
    row("제목", p.title);
    row("줄거리", p.logline);
    row("길이", (p.label || (p.seconds ? p.seconds + "초" : "")) + (p.scenes ? " · 장면 " + p.scenes.length + "개" : ""));
    row("크기", p.size && p.size.length ? p.size[0] + "×" + p.size[1] : "");
    row("품질", p.quality);
    row("배경음악", p.music);
    row("완료까지", p.estimate_s ? "약 " + Math.max(1, Math.round(p.estimate_s / 60)) + "분 (예상)" : "");
    card.append(dl);
    const ol = el("ol", "scenes");
    for (const s of (p.scenes || [])) {
      ol.append(el("li", null, "(" + s.seconds + "초) " + s.ko + (s.continues ? " · 앞 장면에서 이어짐" : "")));
    }
    card.append(ol);
  }

  function planCard(m, data) {
    const p = data.plan || {};
    const card = el("div", "plan");
    if (p.action === "long") {
      longPlanBody(card, p);
      return planActions(card, m, data);
    }
    card.append(el("h4", null, (p.action === "edit" ? "이미지 편집 계획" : "영상 만들기 계획") + " (승인하면 시작합니다)"));
    if (p.attachment_ids && p.attachment_ids[0]) card.append(attachmentNode(p.attachment_ids[0], "image"));
    const dl = el("dl");
    const row = (k, v) => { if (v) { dl.append(el("dt", null, k), el("dd", null, v)); } };
    row("종류", p.action === "t2v" ? "글→영상" : (p.action === "edit" ? "이미지 편집" : "이미지→영상"));
    row("길이", p.seconds ? p.seconds + "초 (" + p.frames + "프레임, " + p.fps + "fps)" : (p.frames ? p.frames + "프레임" : ""));
    row("크기", p.size && p.size.length ? p.size[0] + "×" + p.size[1] : "사진 크기를 따릅니다");
    row("품질", p.quality);
    row("프롬프트", (p.words ? p.words + "낱말" : "") + (p.length_reason ? " · " + p.length_reason : ""));
    row("완료까지", p.estimate_s ? "약 " + Math.max(1, Math.round(p.estimate_s / 60)) + "분 (예상)" : "");
    card.append(dl);
    const ko = Object.values(p.ko || {}).filter(Boolean).join("\n");
    if (ko) card.append(el("div", "ko", ko));
    return planActions(card, m, data);
  }

  function planActions(card, m, data) {
    if (data.status && data.status !== "pending") { setPlanStatus(card, data.status); return card; }
    const acts = el("div", "acts");
    const ok = el("button", "primary", "승인"); ok.type = "button";
    const no = el("button", null, "취소"); no.type = "button";
    const lock = (v) => { ok.disabled = v; no.disabled = v; };
    ok.addEventListener("click", async () => {
      lock(true); showError("");
      try {
        const r = await api("/studio/api/turns/" + m.turn_id + "/approve", { method: "POST", json: {} });
        setPlanStatus(card, r.replanned ? "replanned" : "approved");
        if (r.turn && !r.replanned) cardFor(r.turn.id);
      } catch (ex) { showError(ex.message); lock(false); }
    });
    no.addEventListener("click", async () => {
      lock(true); showError("");
      try { await api("/studio/api/turns/" + m.turn_id + "/cancel", { method: "POST", json: {} }); setPlanStatus(card, "cancelled"); }
      catch (ex) { showError(ex.message); lock(false); }
    });
    acts.append(ok, no); card.append(acts);
    return card;
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

  // 말풍선 시간: 오늘이면 '오후 3:11', 다른 날이면 '11월 17일 오후 3:11' (이 브라우저의 시간대)
  function msgTime(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) return null;
    const hm = d.toLocaleTimeString("ko-KR", { hour: "numeric", minute: "2-digit", hour12: true });
    const n = new Date();
    const same = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
    return { text: same ? hm : (d.getMonth() + 1) + "월 " + d.getDate() + "일 " + hm, full: d.toLocaleString("ko-KR") };
  }

  function renderMessage(m) {
    if (state.shown.has(m.id)) return;
    state.shown.add(m.id); state.lastId = Math.max(state.lastId, m.id);
    clearEmpty();
    const wrap = el("div", "msg " + m.role);
    wrap.dataset.mid = m.id;
    const data = m.data || {};
    if (m.kind === "plan") {
      wrap.append(planCard(m, data));
    } else if (m.kind === "notice") {
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
    const when = msgTime(m.created_at);
    if (when) { const t = el("div", "mtime", when.text); t.title = when.full; wrap.append(t); }
    feed.append(wrap);
    if (m.kind === "result" || m.kind === "notice" || m.kind === "plan") dropCard(m.turn_id);
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

  function syncAsk() {
    const t = state.threads.find((x) => x.id === state.thread);
    $("ask-long").checked = !!t && t.settings && t.settings.ask === "ask_long";
  }

  async function openThread(id) {
    state.thread = id; state.unread.delete(id); state.shown = new Set(); state.lastId = 0;
    for (const c of state.cards.values()) c.box.remove();
    state.cards.clear(); feed.replaceChildren();
    store.set("studio.thread", id);
    document.body.classList.remove("menu");
    await loadMessages();
    await loadThreads();
    syncAsk();
    emptyState();
  }

  async function loadMessages() {
    const r = await api("/studio/api/threads/" + state.thread + "/messages?after=" + state.lastId);
    for (const m of r.messages) renderMessage(m);
    for (const t of r.turns) {
      if (t.state === "awaiting_approval") continue;          // 계획 카드가 대신한다
      if (t.progress) updateProgress(t.id, t.progress); else cardFor(t.id);
    }
    // 이미 끝난 요청의 카드는 지운다 (놓친 이벤트 보정)
    const open = new Set(r.turns.filter((t) => t.state !== "awaiting_approval").map((t) => t.id));
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
      if ($("opt-quality").value) options.quality = $("opt-quality").value;
      if ($("opt-sound").value) options.sound = $("opt-sound").value;
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
      } else if (m.type === "message.update") {
        const w = feed.querySelector('[data-mid="' + String(m.message_id).replace(/[^0-9]/g, "") + '"] .plan');
        if (w && m.data && m.data.status) setPlanStatus(w, m.data.status);
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
    $("ask-long").addEventListener("change", async (e) => {
      const box = e.target; showError("");
      const want = box.checked;           // 새 대화를 만들면 스위치가 새 대화의 값으로 돌아가므로 먼저 읽어 둔다
      try {
        if (!state.thread) await newThread();
        box.checked = want;
        const r = await api("/studio/api/threads/" + state.thread, { method: "PATCH", json: { settings: { ask: want ? "ask_long" : "auto" } } });
        const t = state.threads.find((x) => x.id === state.thread); if (t) t.settings = r.thread.settings;
      } catch (ex) { box.checked = !box.checked; showError(ex.message); }
    });
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
