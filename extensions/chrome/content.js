// Isolated-world content script. Discovers feed thumbnails, dwell-gates them,
// asks the service worker for scores, and renders badges / retitles / dim /
// hover card in Shadow DOM. Talks to the MAIN-world injected.js (transcript
// fetch) over window.postMessage.
//
// Loaded as a classic content script; pulls config + selectors via dynamic
// import of web-accessible module URLs.

(async () => {
  const C = await import(chrome.runtime.getURL("config.js"));
  const S = await import(chrome.runtime.getURL("selectors.js"));

  const MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace";
  const SANS = `"Roboto","Segoe UI",system-ui,-apple-system,sans-serif`;

  // Badge: adopts YouTube's own duration-badge grammar (20px, 4px radius, dark
  // blur scrim) so it reads as native chrome; only the number carries color.
  const BADGE_CSS = `<style>
    :host{position:absolute;left:8px;bottom:8px;pointer-events:auto;z-index:5;}
    .wrap{animation:in .14s cubic-bezier(.2,0,0,1);}
    .pill,.state{display:flex;align-items:center;justify-content:center;
      height:20px;min-width:26px;padding:0 6px;border-radius:4px;box-sizing:border-box;
      background:rgba(0,0,0,.72);
      -webkit-backdrop-filter:blur(8px) saturate(140%);backdrop-filter:blur(8px) saturate(140%);
      box-shadow:inset 0 0 0 1px rgba(255,255,255,.12),0 1px 3px rgba(0,0,0,.35);}
    .pill{font-family:${MONO};font-size:11.5px;font-weight:700;line-height:1;
      font-variant-numeric:tabular-nums;letter-spacing:.01em;cursor:default;
      transition:transform .12s ease;}
    .pill:hover{transform:scale(1.05);}
    .dot{width:7px;height:7px;border-radius:50%;}
    .dot.eval{background:rgba(255,255,255,.92);animation:p 1.1s ease-in-out infinite;}
    .dot.faint{background:rgba(255,255,255,.5);}
    @keyframes p{0%,100%{opacity:.25;transform:scale(.85)}50%{opacity:1;transform:scale(1)}}
    @keyframes in{from{opacity:0;transform:translateY(2px)}to{opacity:1;transform:none}}
    @media (prefers-reduced-motion:reduce){.wrap{animation:none}.dot.eval{animation-duration:2.2s}}
  </style>`;

  // Card: theme-aware (follows YouTube light/dark), native 12px radius,
  // #212121 like YouTube's own menus in dark mode, white in light mode.
  const CARD_CSS = `<style>
    .card{--surface:#212121;--ink:#f1f1f1;--ink2:#aaaaaa;--ink3:#757575;
      --line:rgba(255,255,255,.10);--soft:rgba(255,255,255,.06);
      box-shadow:0 12px 32px rgba(0,0,0,.4),0 2px 8px rgba(0,0,0,.3);}
    .card[data-theme="light"]{--surface:#ffffff;--ink:#0f0f0f;--ink2:#5f6368;--ink3:#8a8f94;
      --line:rgba(0,0,0,.09);--soft:rgba(0,0,0,.045);
      box-shadow:0 12px 32px rgba(0,0,0,.14),0 2px 8px rgba(0,0,0,.08);}
    .card{position:fixed;width:320px;box-sizing:border-box;
      max-height:calc(100vh - 16px);overflow-y:auto;overscroll-behavior:contain;
      background:var(--surface);color:var(--ink);border:1px solid var(--line);
      border-radius:12px;padding:14px 16px 10px;
      font-family:${SANS};font-size:12px;line-height:1.45;
      animation:cin .16s cubic-bezier(.2,0,0,1);}
    @keyframes cin{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
    @media (prefers-reduced-motion:reduce){.card{animation:none}}
    .head{display:flex;align-items:center;gap:12px;}
    .score{font-family:${MONO};font-size:30px;font-weight:700;line-height:1;
      font-variant-numeric:tabular-nums;letter-spacing:-.02em;}
    .headtxt{flex:1;min-width:0;}
    .klabel{font-size:9px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
      color:var(--ink3);margin-bottom:3px;}
    .verdict{color:var(--ink);font-size:12px;line-height:1.45;}
    .divider{height:1px;background:var(--line);margin:12px -16px;}
    .origtitle{font-size:12.5px;font-weight:500;line-height:1.4;color:var(--ink);}
    .metrics{display:flex;flex-direction:column;gap:8px;}
    .mrow{display:flex;align-items:center;gap:10px;}
    .mlab{width:96px;flex:none;color:var(--ink2);font-size:11px;}
    .mbar{flex:1;height:4px;background:var(--soft);border-radius:99px;overflow:hidden;}
    .mbar i{display:block;height:100%;border-radius:99px;}
    .mval{width:16px;flex:none;text-align:right;color:var(--ink2);
      font-family:${MONO};font-size:10.5px;font-variant-numeric:tabular-nums;}
    a.mainc{display:block;color:var(--ink2);font-size:11.5px;margin-top:10px;
      text-decoration:none;cursor:pointer;}
    a.mainc b{font-family:${MONO};font-weight:600;color:var(--ink);}
    a.mainc:hover b{text-decoration:underline;}
    .why{display:flex;flex-direction:column;gap:7px;}
    .why:not(:empty){margin-top:12px;}
    .rline{color:var(--ink2);font-size:11.5px;line-height:1.5;}
    .rline b{color:var(--ink);font-weight:600;}
    .actions{display:flex;justify-content:flex-end;gap:2px;margin-top:8px;}
    .whybtn,.flagbtn{appearance:none;background:transparent;border:0;color:var(--ink2);
      font:500 11px ${SANS};padding:6px 10px;border-radius:6px;cursor:pointer;
      transition:background .12s ease,color .12s ease;}
    .whybtn:hover,.flagbtn:hover{background:var(--soft);color:var(--ink);}
    .whybtn:disabled,.flagbtn:disabled{opacity:.55;cursor:default;background:transparent;}
    a.brand{display:block;margin-top:6px;text-align:right;font-size:8.5px;font-weight:600;
      letter-spacing:.14em;color:var(--ink3);opacity:.6;text-decoration:none;
      transition:opacity .12s ease;}
    a.brand:hover{opacity:1;}
  </style>`;

  let settings = { ...C.DEFAULT_SETTINGS };
  let killed = false;

  const results = new Map(); // videoId -> { state, row }
  const recs = new Map(); // renderer element -> { videoId, timer, requested }
  const showOriginal = new Set(); // videoIds toggled back to original title

  // ---- initial state from SW ----
  const st = await sw({ type: "state:get" });
  if (st) {
    settings = st.settings;
    killed = st.killed;
  }
  if (killed) return; // kill switch: no DOM modification at all

  // Page-level style for the "fade clickbait" effect (the badge/card live in
  // shadow DOM, but acb-dim is applied to YouTube's own tile elements).
  const pageStyle = document.createElement("style");
  pageStyle.textContent =
    ".acb-dim{opacity:.5;filter:grayscale(.7);transition:opacity .2s ease,filter .2s ease;}" +
    ".acb-dim:hover{opacity:1;filter:none;}";
  document.documentElement.appendChild(pageStyle);

  let signedIn = !!st?.auth?.email;
  if (!signedIn) maybeShowSigninChip();

  chrome.storage.onChanged.addListener((changes) => {
    if (changes[C.K_SETTINGS]) {
      settings = { ...C.DEFAULT_SETTINGS, ...changes[C.K_SETTINGS].newValue };
      rescan();
    }
    if (changes[C.K_AUTH]) {
      signedIn = !!changes[C.K_AUTH].newValue?.email;
      if (signedIn) removeSigninChip();
    }
  });

  // A subtle, dismissible prompt so a signed-out user isn't met with a dead
  // extension. One per session (respects a dismissed flag).
  function maybeShowSigninChip() {
    if (sessionStorage.getItem("acb_chip_dismissed")) return;
    if (document.getElementById("acb-signin-chip")) return;
    const host = document.createElement("div");
    host.id = "acb-signin-chip";
    host.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:2147483646;";
    const sr = host.attachShadow({ mode: "open" });
    sr.innerHTML = `<style>
      .chip{display:flex;align-items:center;gap:10px;background:#fff;color:#0f0f0f;
        border:1px solid rgba(0,0,0,.1);border-radius:12px;padding:10px 12px;
        box-shadow:0 8px 28px rgba(0,0,0,.18);font-family:"Roboto","Segoe UI",system-ui,sans-serif;
        font-size:12.5px;animation:s .18s cubic-bezier(.2,0,0,1);}
      @keyframes s{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
      b{font-weight:700;}
      button{font:600 12px inherit;border-radius:8px;cursor:pointer;padding:6px 10px;border:0;}
      .in{background:#0f0f0f;color:#fff;}
      .x{background:transparent;color:#8a8f94;padding:6px;}
      .x:hover{color:#0f0f0f;}
    </style>
    <div class="chip"><span><b>AntiClickbait</b> - sign in to score your feed</span>
      <button class="in" id="in">Sign in</button><button class="x" id="x">✕</button></div>`;
    document.body.appendChild(host);
    sr.getElementById("x").addEventListener("click", () => { sessionStorage.setItem("acb_chip_dismissed", "1"); removeSigninChip(); });
    sr.getElementById("in").addEventListener("click", async () => {
      sr.getElementById("in").textContent = "Opening...";
      const r = await sw({ type: "auth:signin" });
      if (r?.auth?.email) { signedIn = true; removeSigninChip(); rescan(); }
      else sr.getElementById("in").textContent = "Sign in";
    });
  }
  function removeSigninChip() {
    document.getElementById("acb-signin-chip")?.remove();
  }

  // ---- transcript bridge (SW -> content -> MAIN world -> back) ----
  const pendingT = new Map();
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || e.data.type !== C.MSG_RESULT) return;
    const r = pendingT.get(e.data.reqId);
    if (r) {
      pendingT.delete(e.data.reqId);
      r(e.data);
    }
  });
  function fetchTranscript(videoId) {
    return new Promise((resolve) => {
      const reqId = Math.random().toString(36).slice(2);
      pendingT.set(reqId, resolve);
      window.postMessage({ type: C.MSG_FETCH, videoId, reqId }, "*");
      setTimeout(() => {
        if (pendingT.has(reqId)) {
          pendingT.delete(reqId);
          resolve({ error: "timeout" });
        }
      }, 8000);
    });
  }

  // ---- SW messages ----
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "transcript:fetch") {
      fetchTranscript(msg.videoId).then(sendResponse);
      return true;
    }
    if (msg.type === "score:result") {
      results.set(msg.videoId, { state: msg.state, row: msg.row || null });
      manageEvalTimeout(msg.videoId, msg.state);
      for (const el of document.querySelectorAll(`[data-acb-vid="${msg.videoId}"]`)) renderState(el, msg.videoId);
    }
  });

  // A stuck "evaluating" dot must not live forever: after 90s with no result,
  // fall back to absence (the video can be retried on a later page load).
  const evalTimers = new Map();
  const EVAL_TIMEOUT_MS = 90 * 1000;
  function manageEvalTimeout(videoId, state) {
    clearTimeout(evalTimers.get(videoId));
    evalTimers.delete(videoId);
    if (state !== "evaluating") return;
    evalTimers.set(
      videoId,
      setTimeout(() => {
        if (results.get(videoId)?.state !== "evaluating") return;
        results.set(videoId, { state: "timeout", row: null });
        for (const el of document.querySelectorAll(`[data-acb-vid="${videoId}"]`)) renderState(el, videoId);
      }, EVAL_TIMEOUT_MS)
    );
  }

  function sw(msg) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(msg, (r) => {
        void chrome.runtime.lastError;
        resolve(r);
      });
    });
  }

  // ---- dwell ----
  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        const rec = recs.get(e.target);
        if (!rec) continue;
        if (e.isIntersecting) {
          if (!rec.requested && !rec.timer) {
            rec.timer = setTimeout(() => {
              rec.timer = null;
              if (rec.requested) return;
              rec.requested = rec.videoId;
              sw({ type: "score:request", videoId: rec.videoId });
            }, C.DWELL_MS);
          }
        } else if (rec.timer) {
          clearTimeout(rec.timer);
          rec.timer = null;
        }
      }
    },
    { threshold: 0.5 }
  );

  // ---- scan / setup ----
  function scan() {
    if (!settings.enabled) return;
    for (const el of document.querySelectorAll(S.RENDERER_SELECTOR)) {
      const videoId = S.getVideoId(el);
      if (!videoId) {
        cleanup(el);
        continue;
      }
      const rec = recs.get(el);
      if (rec && rec.videoId === videoId) {
        renderState(el, videoId);
        continue;
      }
      if (rec) cleanup(el); // recycled element -> reset
      el.dataset.acbVid = videoId;
      recs.set(el, { videoId, timer: null, requested: null });
      io.observe(el);
      if (results.has(videoId)) renderState(el, videoId);
    }
  }

  function cleanup(el) {
    const rec = recs.get(el);
    if (rec?.timer) clearTimeout(rec.timer);
    io.unobserve(el);
    recs.delete(el);
    delete el.dataset.acbVid;
    const anchor = S.getThumbAnchor(el);
    anchor?.querySelector(":scope > .acb-host")?.remove();
    el.querySelector(".acb-marker")?.remove();
    restoreTitle(el);
    el.classList.remove("acb-dim");
  }

  function rescan() {
    if (!settings.enabled) {
      for (const el of [...recs.keys()]) cleanup(el);
      return;
    }
    for (const [el, rec] of recs) if (document.body.contains(el)) renderState(el, rec.videoId);
    scan();
  }

  // ---- rendering ----
  function ensureHost(el) {
    const anchor = S.getThumbAnchor(el);
    if (!anchor) return null;
    let host = anchor.querySelector(":scope > .acb-host");
    if (host) return host;
    if (getComputedStyle(anchor).position === "static") anchor.style.position = "relative";
    host = document.createElement("div");
    host.className = "acb-host";
    const sr = host.attachShadow({ mode: "open" });
    sr.innerHTML = BADGE_CSS + '<div class="wrap"></div>';
    anchor.appendChild(host);
    host.addEventListener("mouseenter", () => {
      const r = results.get(el.dataset.acbVid);
      if (r?.state === "scored") openCard(el.dataset.acbVid, host.getBoundingClientRect());
    });
    host.addEventListener("mouseleave", () => scheduleClose(200));
    return host;
  }

  function clampScore(v) {
    return Math.max(0, Math.min(100, Math.round(Number(v) || 0)));
  }

  function pageTheme() {
    return document.documentElement.hasAttribute("dark") ? "dark" : "light";
  }

  // The true original title: prefer what we captured from the DOM before
  // rewriting; fall back to the row's server-fetched title.
  function getOriginalTitle(videoId, row) {
    for (const el of document.querySelectorAll(`[data-acb-vid="${videoId}"]`)) {
      const t = S.getTitleEl(el);
      if (t?.dataset.acbOrig) return t.dataset.acbOrig;
    }
    return row.title || "";
  }

  function renderState(el, videoId) {
    const r = results.get(videoId);
    if (!r) return;
    const host = ensureHost(el);
    if (!host) return;
    const wrap = host.shadowRoot.querySelector(".wrap");

    if (r.state === "scored") {
      const score = clampScore(r.row.video_score);
      const c = C.scoreColor(score);
      wrap.innerHTML = `<div class="pill" style="color:${c}">${score}</div>`;
      host.style.display = "block";
      applyRetitle(el, r.row);
      applyDim(el, r.row);
    } else if (r.state === "evaluating") {
      wrap.innerHTML = `<div class="state"><div class="dot eval"></div></div>`;
      host.style.display = "block";
    } else if (r.state === "unscoreable") {
      restoreTitle(el);
      el.classList.remove("acb-dim");
      if (settings.showDots) {
        wrap.innerHTML = `<div class="state"><div class="dot faint" title="${C.REASON_LABEL[r.row?.unscoreable_reason] || "Not scoreable"}"></div></div>`;
        host.style.display = "block";
      } else {
        host.style.display = "none";
        setReasonTooltip(el, C.REASON_LABEL[r.row?.unscoreable_reason] || "Not scoreable");
      }
    } else {
      // signin / none / error -> absence
      host.style.display = "none";
    }
  }

  function setReasonTooltip(el, text) {
    const anchor = S.getThumbAnchor(el);
    if (anchor && !anchor.title) anchor.title = text;
  }

  // ---- retitle ----
  function applyRetitle(el, row) {
    const titleEl = S.getTitleEl(el);
    if (!titleEl) return;
    const wantOriginal = showOriginal.has(row.video_id);
    const shouldRetitle = row.honest_title && settings.retitleMode !== "off";
    if (!shouldRetitle) {
      restoreTitle(el);
      return;
    }
    if (!titleEl.dataset.acbOrig) titleEl.dataset.acbOrig = titleText(titleEl);
    const desired = wantOriginal ? titleEl.dataset.acbOrig : row.honest_title;
    if (titleText(titleEl) !== desired) setTitleText(titleEl, desired);
    ensureMarker(el, row.video_id, titleEl);
  }

  function ensureMarker(el, videoId, titleEl) {
    if (el.querySelector(".acb-marker")) return;
    const m = document.createElement("span");
    m.className = "acb-marker";
    m.textContent = "✎"; // pencil: "this title was rewritten"
    m.title = "Title rewritten by AntiClickbait - click to see the original";
    m.style.cssText =
      "cursor:pointer;display:inline-block;margin-left:6px;font-size:13px;line-height:1;opacity:.9;transition:opacity .12s ease,transform .12s ease;";
    m.addEventListener("mouseenter", () => { m.style.opacity = "1"; m.style.transform = "scale(1.15)"; });
    m.addEventListener("mouseleave", () => { m.style.opacity = ".9"; m.style.transform = "none"; });
    m.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (showOriginal.has(videoId)) showOriginal.delete(videoId);
      else showOriginal.add(videoId);
      for (const t of document.querySelectorAll(`[data-acb-vid="${videoId}"]`)) {
        const r = results.get(videoId);
        if (r?.row) applyRetitle(t, r.row);
      }
    });
    titleEl.insertAdjacentElement("afterend", m);
  }

  function restoreTitle(el) {
    const titleEl = S.getTitleEl(el);
    if (titleEl?.dataset.acbOrig && titleText(titleEl) !== titleEl.dataset.acbOrig) {
      setTitleText(titleEl, titleEl.dataset.acbOrig);
    }
    el.querySelector(".acb-marker")?.remove();
  }

  function titleText(titleEl) {
    return (titleEl.getAttribute("title") || titleEl.textContent || "").trim();
  }
  function setTitleText(titleEl, text) {
    // YouTube titles use both textContent and a title attr / inner span.
    const span = titleEl.querySelector("span") || titleEl;
    span.textContent = text;
    if (titleEl.hasAttribute("title")) titleEl.setAttribute("title", text);
    if (titleEl.getAttribute("aria-label")) titleEl.setAttribute("aria-label", text);
  }

  // ---- dim ----
  function applyDim(el, row) {
    if (settings.dim && row.video_score < C.DIM_THRESHOLD) el.classList.add("acb-dim");
    else el.classList.remove("acb-dim");
  }

  // ---- hover card ----
  // Open on badge hover; close when the pointer settles outside badge+card,
  // on any scroll (a fixed-position card must never ride over other videos),
  // or on SPA navigation.
  let cardHost = null;
  let cardRoot = null;
  let cardVideoId = null;
  let closeTimer = null;
  let pointerInCard = false;
  let cardAnchorRect = null;

  function ensureCard() {
    if (cardHost) return;
    cardHost = document.createElement("div");
    cardHost.style.cssText = "position:fixed;z-index:2147483647;top:0;left:0;";
    cardRoot = cardHost.attachShadow({ mode: "open" });
    document.body.appendChild(cardHost);
    cardHost.addEventListener("mouseenter", () => { pointerInCard = true; cancelClose(); });
    cardHost.addEventListener("mouseleave", () => { pointerInCard = false; scheduleClose(150); });
    // Close on page scroll ONLY when the pointer isn't inside the card - so a user
    // reading (or scrolling within) the card doesn't have it vanish under them.
    window.addEventListener("scroll", () => { if (!pointerInCard) closeCard(); }, { passive: true, capture: true });
    window.addEventListener("yt-navigate-start", closeCard);
  }

  function cancelClose() {
    clearTimeout(closeTimer);
    closeTimer = null;
  }
  function scheduleClose(ms) {
    cancelClose();
    closeTimer = setTimeout(closeCard, ms);
  }
  function closeCard() {
    cancelClose();
    if (cardHost) cardHost.style.display = "none";
    cardVideoId = null;
    pointerInCard = false;
  }

  // Place the card against its anchor badge, clamped to the viewport. Called on
  // open and again after the card grows (e.g. "Explain score" expands it).
  function positionCard() {
    const cardEl = cardRoot.querySelector(".card");
    const rect = cardAnchorRect;
    if (!cardEl || !rect) return;
    const w = 320;
    let left = rect.left;
    if (left + w > innerWidth - 8) left = innerWidth - w - 8;
    if (left < 8) left = 8;
    cardEl.style.left = left + "px";
    let top = rect.bottom + 6;
    if (top + cardEl.offsetHeight > innerHeight - 8) top = rect.top - cardEl.offsetHeight - 6;
    cardEl.style.top = Math.max(8, top) + "px";
  }

  function openCard(videoId, rect) {
    const r = results.get(videoId);
    if (!r || r.state !== "scored") return;
    ensureCard();
    cancelClose();
    if (cardVideoId === videoId && cardHost.style.display !== "none") return; // already open
    cardVideoId = videoId;
    cardAnchorRect = rect;
    cardRoot.innerHTML = CARD_CSS + cardHtml(r.row, videoId);
    cardHost.style.display = "block";
    positionCard();
    wireCard(videoId);
  }

  function metricBar(label, val) {
    const v = Math.max(0, Math.min(10, Math.round(Number(val) || 0)));
    const col = C.scoreColor(v * 10);
    return `<div class="mrow"><span class="mlab">${label}</span><span class="mbar"><i style="width:${v * 10}%;background:${col}"></i></span><span class="mval">${v}</span></div>`;
  }

  function fmtTime(s) {
    const m = Math.floor(s / 60);
    const ss = String(Math.floor(s % 60)).padStart(2, "0");
    return `${m}:${ss}`;
  }

  function cardHtml(row, videoId) {
    const score = clampScore(row.video_score);
    const c = C.scoreColor(score);
    const start =
      row.main_content_start_s > 0
        ? `<a class="mainc" href="/watch?v=${videoId}&t=${Math.floor(row.main_content_start_s)}s">Main content starts at <b>${fmtTime(row.main_content_start_s)}</b></a>`
        : "";
    // The tile already shows the rewritten title; the card's job is to reveal
    // the ORIGINAL the creator wrote.
    const origTitle = row.honest_title ? getOriginalTitle(videoId, row) : "";
    const titles = origTitle
      ? `<div class="divider"></div>
         <div class="titles">
           <div class="klabel">Original title</div>
           <div class="origtitle">${esc(origTitle)}</div>
         </div>`
      : "";
    return `<div class="card" data-theme="${pageTheme()}">
      <div class="head">
        <span class="score" style="color:${c}">${score}</span>
        <div class="headtxt">
          <div class="klabel">Title honesty</div>
          <div class="verdict">${esc(row.verdict_line || "")}</div>
        </div>
      </div>
      ${titles}
      <div class="divider"></div>
      <div class="metrics">
        ${metricBar("Similarity", row.tcs)}
        ${metricBar("Focus", row.focus)}
        ${metricBar("Time to content", row.ttmc)}
        ${metricBar("Honesty", 10 - (row.deception ?? 0))}
        ${metricBar("No sponsors", 10 - (row.sponsor ?? 0))}
      </div>
      ${start}
      <div class="why"></div>
      <div class="actions">
        <button class="whybtn">Explain score</button>
        <button class="flagbtn">Wrong score?</button>
      </div>
      <a class="brand" href="https://anticlickbait.vercel.app" target="_blank" rel="noopener">ANTICLICKBAIT</a>
    </div>`;
  }

  // Per-video card state so flag/reasonings survive the card being rebuilt
  // on every hover (the card is transient; this map is not).
  const cardState = new Map(); // videoId -> { flagged, reasonings }
  function ui(videoId) {
    if (!cardState.has(videoId)) cardState.set(videoId, { flagged: false, reasonings: null });
    return cardState.get(videoId);
  }

  // Same labels and order as the metric bars, so "Why?" reads as an
  // explanation of the bars above it.
  const RLABELS = [
    ["tcs", "Similarity"],
    ["focus", "Focus"],
    ["ttmc", "Time to content"],
    ["deception", "Honesty"],
    ["sponsor", "Sponsors"],
  ];
  function reasoningsHtml(R) {
    return RLABELS.map(([k, label]) => `<div class="rline"><b>${label}</b> ${esc(R[k] || "")}</div>`).join("");
  }

  function wireCard(videoId) {
    const q = (s) => cardRoot.querySelector(s);
    const st = ui(videoId);
    const whyBtn = q(".whybtn");
    const flagBtn = q(".flagbtn");
    const why = q(".why");

    // Restore state from previous opens of this video's card.
    if (st.reasonings) {
      why.innerHTML = reasoningsHtml(st.reasonings);
      whyBtn.style.display = "none";
    }
    if (st.flagged) {
      flagBtn.textContent = "Flagged - thanks";
      flagBtn.disabled = true;
    }

    flagBtn.addEventListener("click", async () => {
      flagBtn.disabled = true;
      flagBtn.textContent = "Flagging...";
      const resp = await sw({ type: "flag", videoId, reason: "wrong_score" });
      if (resp?.ok) {
        st.flagged = true;
        flagBtn.textContent = "Flagged - thanks";
      } else if (resp?.error === "signin") {
        flagBtn.textContent = "Sign in to flag";
        flagBtn.disabled = false;
      } else {
        flagBtn.textContent = "Failed - tap to retry";
        flagBtn.disabled = false;
      }
    });

    whyBtn.addEventListener("click", async () => {
      whyBtn.disabled = true;
      whyBtn.textContent = "Analyzing...";
      let resp = await sw({ type: "expand:request", videoId });
      if (resp?.error === "segments_required") {
        const t = await fetchTranscript(videoId);
        resp = t?.segments ? await sw({ type: "expand:request", videoId, segments: t.segments }) : resp;
      }
      if (resp?.reasonings) {
        st.reasonings = resp.reasonings;
        why.innerHTML = reasoningsHtml(resp.reasonings);
        whyBtn.style.display = "none";
        positionCard(); // card grew - re-clamp so it never spills off-screen
      } else {
        whyBtn.textContent = resp?.error === "signin" ? "Sign in to explain" : "Unavailable - tap to retry";
        whyBtn.disabled = false;
      }
    });
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---- observers ----
  const mo = new MutationObserver(() => {
    clearTimeout(mo._t);
    mo._t = setTimeout(scan, 250);
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("yt-navigate-finish", () => setTimeout(scan, 300));
  scan();
})();
