// Service worker: cache, bulk PostgREST reads, serial fresh-eval queue, auth,
// quota, kill switch. Content scripts and the popup talk to it via messages.

import * as C from "./config.js";

// In demo mode, stale rows from earlier mock formats must not survive a
// reload - flush the score cache so every demo session starts clean.
if (C.MOCK) {
  chrome.storage.local.get(null, (all) => {
    const stale = Object.keys(all).filter((k) => k.startsWith(C.CACHE_PREFIX));
    if (stale.length) chrome.storage.local.remove(stale);
  });
}

// ---------- storage helpers ----------

async function get(key, fallback) {
  const o = await chrome.storage.local.get(key);
  return key in o ? o[key] : fallback;
}
async function set(obj) {
  await chrome.storage.local.set(obj);
}

async function getSettings() {
  return { ...C.DEFAULT_SETTINGS, ...(await get(C.K_SETTINGS, {})) };
}

// ---------- cache ----------

function cacheKey(id) {
  return C.CACHE_PREFIX + id;
}
async function cacheGet(id) {
  const v = await get(cacheKey(id), null);
  if (!v) return null;
  if (v.expiresAt < Date.now()) {
    await chrome.storage.local.remove(cacheKey(id));
    return null;
  }
  return v; // { row, state, expiresAt }
}
async function cachePut(id, state, row) {
  const ttl = state === "scored" ? C.TTL_SCORED : C.TTL_UNSCOREABLE;
  const entry = { row, state, expiresAt: Date.now() + ttl };
  try {
    await set({ [cacheKey(id)]: entry });
  } catch {
    await evictOldest();
    try {
      await set({ [cacheKey(id)]: entry });
    } catch {
      /* give up silently; cache is best-effort */
    }
  }
}
async function evictOldest() {
  const all = await chrome.storage.local.get(null);
  const rows = Object.keys(all)
    .filter((k) => k.startsWith(C.CACHE_PREFIX))
    .map((k) => [k, all[k].expiresAt || 0])
    .sort((a, b) => a[1] - b[1]);
  const drop = rows.slice(0, Math.max(1, Math.floor(rows.length * 0.2))).map((r) => r[0]);
  if (drop.length) await chrome.storage.local.remove(drop);
}

// ---------- auth ----------

async function getAuth() {
  return await get(C.K_AUTH, null);
}

async function signIn() {
  const redirect = chrome.identity.getRedirectURL();
  const url = `${C.AUTH_AUTHORIZE}?provider=google&redirect_to=${encodeURIComponent(redirect)}`;
  const redirectUrl = await chrome.identity.launchWebAuthFlow({ url, interactive: true });
  const frag = redirectUrl.split("#")[1] || redirectUrl.split("?")[1] || "";
  const p = new URLSearchParams(frag);
  const access_token = p.get("access_token");
  const refresh_token = p.get("refresh_token");
  if (!access_token) throw new Error("no_access_token");
  const expires_at = Date.now() + (parseInt(p.get("expires_in") || "3600", 10) - 60) * 1000;
  const email = await fetchEmail(access_token);
  const auth = { access_token, refresh_token, expires_at, email };
  await set({ [C.K_AUTH]: auth });
  return auth;
}

async function fetchEmail(token) {
  try {
    const r = await fetch(`${C.SUPABASE_URL}/auth/v1/user`, {
      headers: { apikey: C.SUPABASE_ANON_KEY, Authorization: `Bearer ${token}` },
    });
    const j = await r.json();
    return j.email || null;
  } catch {
    return null;
  }
}

async function signOut() {
  await chrome.storage.local.remove(C.K_AUTH);
}

// Returns a valid access token or null (never throws).
async function validToken() {
  const auth = await getAuth();
  if (!auth) return null;
  if (auth.expires_at > Date.now()) return auth.access_token;
  if (!auth.refresh_token) return null;
  try {
    const r = await fetch(`${C.AUTH_TOKEN}?grant_type=refresh_token`, {
      method: "POST",
      headers: { apikey: C.SUPABASE_ANON_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: auth.refresh_token }),
    });
    if (!r.ok) {
      await signOut();
      return null;
    }
    const j = await r.json();
    const next = {
      access_token: j.access_token,
      refresh_token: j.refresh_token,
      expires_at: Date.now() + (j.expires_in - 60) * 1000,
      email: auth.email,
    };
    await set({ [C.K_AUTH]: next });
    return next.access_token;
  } catch {
    return null;
  }
}

// ---------- kill switch ----------

async function checkKillSwitch() {
  if (C.MOCK) {
    await set({ [C.K_KILLED]: false });
    return;
  }
  try {
    const r = await fetch(
      `${C.REST_URL}?video_id=eq.__kill__&select=status,unscoreable_reason`,
      { headers: { apikey: C.SUPABASE_ANON_KEY, Authorization: `Bearer ${C.SUPABASE_ANON_KEY}` } }
    );
    const rows = await r.json();
    const killed =
      Array.isArray(rows) &&
      rows[0] &&
      rows[0].status === "unscoreable" &&
      rows[0].unscoreable_reason === "kill";
    await set({ [C.K_KILLED]: !!killed });
  } catch {
    await set({ [C.K_KILLED]: false });
  }
}

// ---------- bulk read + fresh-eval queue ----------

const pending = new Map(); // videoId -> tabId  (awaiting a bulk lookup)
let bulkTimer = null;
const evalQueue = []; // { videoId, tabId }
let evalRunning = false;
let quotaBlocked = false; // 403 this session -> stop fresh evals

function scheduleBulk() {
  if (bulkTimer) return;
  bulkTimer = setTimeout(flushBulk, C.BULK_DEBOUNCE_MS);
}

async function flushBulk() {
  bulkTimer = null;
  if (pending.size === 0) return;
  const batch = [...pending.entries()].slice(0, C.BULK_MAX_IDS);
  for (const [id] of batch) pending.delete(id);
  const ids = batch.map(([id]) => id);
  const tabOf = new Map(batch);

  const found = C.MOCK ? mockBulk(ids) : await bulkRead(ids);

  const missing = [];
  for (const id of ids) {
    const row = found.get(id);
    if (row) {
      const state = row.status === "scored" ? "scored" : "unscoreable";
      await cachePut(id, state, row);
      send(tabOf.get(id), { type: "score:result", videoId: id, state, row });
    } else {
      missing.push(id);
    }
  }
  for (const id of missing) enqueueEval(id, tabOf.get(id));
  if (pending.size) scheduleBulk();
}

async function bulkRead(ids) {
  const map = new Map();
  const list = ids.map((id) => `"${id}"`).join(",");
  try {
    const r = await fetch(`${C.REST_URL}?video_id=in.(${list})&select=${C.BULK_SELECT}`, {
      headers: { apikey: C.SUPABASE_ANON_KEY, Authorization: `Bearer ${C.SUPABASE_ANON_KEY}` },
    });
    const rows = await r.json();
    if (Array.isArray(rows)) for (const row of rows) map.set(row.video_id, row);
  } catch {
    /* offline -> treat all as missing */
  }
  return map;
}

async function enqueueEval(videoId, tabId) {
  if (C.MOCK) {
    const m = mockFresh(videoId);
    if (m.state === "evaluating") {
      // Demonstrates the live flow: dot first, score when the "eval" lands.
      send(tabId, { type: "score:result", videoId, state: "evaluating" });
      const row = mockRowFor(videoId, m.score ?? 62, null);
      setTimeout(async () => {
        await cachePut(videoId, "scored", row);
        send(tabId, { type: "score:result", videoId, state: "scored", row });
      }, m.resolveMs ?? 8000);
      return;
    }
    await cachePut(videoId, m.state, m.row);
    send(tabId, { type: "score:result", videoId, state: m.state, row: m.row });
    return;
  }

  const settings = await getSettings();
  if (!settings.enabled) return;
  const token = await validToken();
  if (!token) {
    send(tabId, { type: "score:result", videoId, state: "signin" });
    return;
  }
  if (quotaBlocked) return;
  send(tabId, { type: "score:result", videoId, state: "evaluating" });
  evalQueue.push({ videoId, tabId });
  runQueue();
}

async function runQueue() {
  if (evalRunning) return;
  evalRunning = true;
  while (evalQueue.length) {
    const { videoId, tabId } = evalQueue.shift();
    if (quotaBlocked) break;
    await freshEval(videoId, tabId);
  }
  evalRunning = false;
}

async function freshEval(videoId, tabId) {
  const t = await requestTranscript(tabId, videoId);
  if (!t || t.error) {
    // No captions / unplayable -> nothing renders. Cache short negative locally.
    send(tabId, { type: "score:result", videoId, state: "none" });
    return;
  }
  const token = await validToken();
  if (!token) {
    send(tabId, { type: "score:result", videoId, state: "signin" });
    return;
  }
  try {
    const r = await fetch(C.FN_SCORE, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        video_id: videoId,
        segments: t.segments,
        duration_s: t.duration_s,
        meta: t.meta,
        source: "extension",
      }),
    });
    if (r.status === 401) {
      await signOut();
      send(tabId, { type: "score:result", videoId, state: "signin" });
      return;
    }
    if (r.status === 403) {
      const j = await r.json().catch(() => ({}));
      quotaBlocked = true;
      if (j.quota) await set({ [C.K_QUOTA]: j.quota });
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setBadgeBackgroundColor({ color: "#f85149" });
      send(tabId, { type: "score:result", videoId, state: "none" });
      return;
    }
    if (r.status === 502 || !r.ok) {
      send(tabId, { type: "score:result", videoId, state: "none" });
      return;
    }
    const j = await r.json();
    const row = j.row;
    if (j.quota) await set({ [C.K_QUOTA]: j.quota });
    const state = row.status === "scored" ? "scored" : "unscoreable";
    await cachePut(videoId, state, row);
    send(tabId, { type: "score:result", videoId, state, row });
  } catch {
    send(tabId, { type: "score:result", videoId, state: "none" });
  }
}

// Ask a tab's content script (which relays to the MAIN-world script) for the
// transcript. Returns { segments, duration_s, meta } or { error }.
function requestTranscript(tabId, videoId) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, { type: "transcript:fetch", videoId }, (resp) => {
      if (chrome.runtime.lastError) return resolve({ error: "no_tab" });
      resolve(resp || { error: "no_resp" });
    });
  });
}

// ---------- expand / flag ----------

async function expand(videoId, segments) {
  if (C.MOCK) {
    await new Promise((r) => setTimeout(r, 600));
    return { reasonings: mockReasonings() };
  }
  const token = await validToken();
  if (!token) return { error: "signin" };
  const body = segments ? { video_id: videoId, segments } : { video_id: videoId };
  const r = await fetch(C.FN_EXPAND, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.status === 422) return { error: "segments_required" };
  if (!r.ok) return { error: "failed" };
  return await r.json();
}

async function flag(videoId, reason) {
  if (C.MOCK) return { ok: true };
  const token = await validToken();
  if (!token) return { error: "signin" };
  const r = await fetch(C.FN_FLAG, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId, reason }),
  });
  return r.ok ? { ok: true } : { error: "failed" };
}

// ---------- messaging ----------

function send(tabId, msg) {
  if (tabId == null) return;
  chrome.tabs.sendMessage(tabId, msg, () => void chrome.runtime.lastError);
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab?.id;

  if (msg.type === "score:request") {
    handleScoreRequest(msg.videoId, tabId);
    return; // no response
  }
  if (msg.type === "state:get") {
    (async () => {
      sendResponse({
        settings: await getSettings(),
        auth: await getAuth(),
        quota: await get(C.K_QUOTA, null),
        killed: await get(C.K_KILLED, false),
      });
    })();
    return true;
  }
  if (msg.type === "settings:set") {
    (async () => {
      const next = { ...(await getSettings()), ...msg.patch };
      await set({ [C.K_SETTINGS]: next });
      sendResponse(next);
    })();
    return true;
  }
  if (msg.type === "auth:signin") {
    signIn().then((a) => sendResponse({ auth: a })).catch((e) => sendResponse({ error: String(e) }));
    return true;
  }
  if (msg.type === "auth:signout") {
    signOut().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "expand:request") {
    expand(msg.videoId, msg.segments).then(sendResponse);
    return true;
  }
  if (msg.type === "flag") {
    flag(msg.videoId, msg.reason).then(sendResponse);
    return true;
  }
});

async function handleScoreRequest(videoId, tabId) {
  if (await get(C.K_KILLED, false)) return;
  const cached = await cacheGet(videoId);
  if (cached) {
    send(tabId, { type: "score:result", videoId, state: cached.state, row: cached.row });
    return;
  }
  pending.set(videoId, tabId);
  scheduleBulk();
}

// ---------- lifecycle ----------

chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("onboarding.html") });
  }
});
chrome.runtime.onStartup.addListener(checkKillSwitch);
checkKillSwitch();

// ---------- MOCK data ----------

function mockHash(id) {
  let h = 2166136261;
  for (const ch of id) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h;
}

const MOCK_VERDICTS = {
  high: ["Title matches the content honestly.", "Delivers exactly what it promises.", "Straightforward title, no games."],
  mid: ["Somewhat exaggerated framing.", "Title oversells parts of the video.", "Mostly accurate, with some added drama."],
  low: ["Title badly oversells the content.", "Classic bait - the content doesn't match.", "The promise in the title never lands."],
};
const MOCK_HONEST_TITLES = [
  "What the video actually shows, plainly stated",
  "A calmer, factual version of this title",
  "What you'd actually learn from this video",
];

function mockRowFor(videoId, score, honest) {
  const h = mockHash(videoId);
  const clamp10 = (n) => Math.max(0, Math.min(10, Math.round(n)));
  const jit = (base, i) => clamp10(base + (((h >> (i * 3)) % 3) - 1)); // deterministic -1..+1
  const band = score >= 80 ? "high" : score >= 40 ? "mid" : "low";
  return {
    video_id: videoId,
    status: "scored",
    unscoreable_reason: null,
    video_score: score,
    tcs: jit(score >= 80 ? 9 : score >= 40 ? 5 : 2, 0),
    deception: jit(score >= 80 ? 0 : score >= 40 ? 3 : 8, 1),
    focus: jit(score >= 80 ? 9 : score >= 40 ? 5 : 3, 2),
    ttmc: jit(score >= 80 ? 9 : score >= 40 ? 6 : 3, 3),
    sponsor: jit(score >= 80 ? 0 : score >= 40 ? 2 : 5, 4),
    honest_title: honest,
    verdict_line: MOCK_VERDICTS[band][h % 3],
    main_content_start_s: score >= 80 ? 0 : 20 + (h % 90),
    language: "en",
    scorer_version: 2,
    reasonings: null,
  };
}

const MOCK_TABLE = {
  mock0000015: { state: "scored", row: mockRowFor("mock0000015", 15, "What the video actually shows, plainly stated") },
  mock0000045: { state: "scored", row: mockRowFor("mock0000045", 45, null) },
  mock0000085: { state: "scored", row: mockRowFor("mock0000085", 85, null) },
  mockmusic01: {
    state: "unscoreable",
    row: { video_id: "mockmusic01", status: "unscoreable", unscoreable_reason: "music" },
  },
  mockeval001: { state: "evaluating", resolveMs: 8000, score: 62 },
};

function mockScenario(videoId) {
  if (MOCK_TABLE[videoId]) return MOCK_TABLE[videoId];
  // Deterministic variety for arbitrary real feed ids: full 0-100 spread,
  // ~8% unscoreable, ~8% "evaluating" that resolves after a short delay.
  const h = mockHash(videoId);
  const roll = h % 100;
  if (roll < 8) {
    return {
      state: "unscoreable",
      row: { video_id: videoId, status: "unscoreable", unscoreable_reason: h % 2 ? "music" : "no_captions" },
    };
  }
  if (roll < 16) return { state: "evaluating", resolveMs: 1500 + (h % 2500), score: (h >> 4) % 101 };
  const score = (h >> 2) % 101;
  const honest = score < 40 ? MOCK_HONEST_TITLES[h % MOCK_HONEST_TITLES.length] : null;
  return { state: "scored", row: mockRowFor(videoId, score, honest) };
}

function mockBulk(ids) {
  // Half of arbitrary ids "exist" in the pool; the rest go to fresh eval.
  const map = new Map();
  for (const id of ids) {
    const s = mockScenario(id);
    if (MOCK_TABLE[id] && s.row) map.set(id, s.row); // named fixtures always in pool
  }
  return map;
}

function mockFresh(videoId) {
  const s = mockScenario(videoId);
  if (s.state === "evaluating") return { state: "evaluating", resolveMs: s.resolveMs, score: s.score };
  return { state: s.state, row: s.row };
}

function mockReasonings() {
  return {
    tcs: "The title promises a result the video only partially delivers.",
    deception: "A specific claim in the title is never substantiated on screen.",
    focus: "Large portions drift away from the titled subject.",
    ttmc: "The main content starts well after a long intro.",
    sponsor: "One mid-roll sponsor break interrupts the flow.",
  };
}
