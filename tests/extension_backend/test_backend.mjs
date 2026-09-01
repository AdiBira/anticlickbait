// Full backend integration test — runs against a LOCAL supabase stack, never prod.
// Skipped unless RUN_INTEGRATION=1 (Docker was unavailable in the build env, so the
// pure-logic gate is tests/extension_backend/test_units.mjs; this file is the stack test).
//
// How to run (all local):
//   1. supabase start
//   2. supabase db reset            # applies extension_pool + crowd_corroboration migrations
//   3. In another shell, serve the functions with the mock + test overrides:
//        MOCK_PORT=8899
//        supabase functions serve --no-verify-jwt --env-file tests/extension_backend/serve.env
//      where serve.env sets:
//        DEEPSEEK_API_KEY=mock
//        DEEPSEEK_API_BASE=http://host.docker.internal:8899
//        OEMBED_API_BASE=http://host.docker.internal:8899
//        TEST_QUOTA=1
//   4. Export the local stack values printed by `supabase start`:
//        SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, FUNCTIONS_URL
//   5. RUN_INTEGRATION=1 node --test tests/extension_backend/test_backend.mjs
//
// Corroboration note: a scored/unscoreable row from ONE user is provisional and is
// NOT returned by the anon bulk read. Two DISTINCT users submitting the SAME
// transcript (same outcome_hash) verify it; only then is it publicly readable.
// The service-role (crawler) seeds verified rows directly.

import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

const RUN = process.env.RUN_INTEGRATION === "1";
const skip = !RUN;

const MOCK_PORT = Number(process.env.MOCK_PORT ?? 8899);
const SUPABASE_URL = process.env.SUPABASE_URL ?? "http://127.0.0.1:54321";
const FUNCTIONS_URL = process.env.FUNCTIONS_URL ?? `${SUPABASE_URL}/functions/v1`;
const ANON = process.env.SUPABASE_ANON_KEY ?? "";
const SERVICE = process.env.SUPABASE_SERVICE_ROLE_KEY ?? "";

// Deterministic DeepSeek + oEmbed mock. Transcript containing "bait" -> dishonest
// (deception high -> honest_title set); otherwise honest (honest_title null).
let mockServer;
before(() => {
  if (skip) return;
  mockServer = http.createServer((req, res) => {
    let bodyChunks = "";
    req.on("data", (c) => (bodyChunks += c));
    req.on("end", () => {
      res.setHeader("Content-Type", "application/json");
      if (req.url.startsWith("/oembed")) {
        res.end(JSON.stringify({ title: "Original clickbait title" }));
        return;
      }
      // /chat/completions
      const baity = bodyChunks.includes("bait");
      const payload = baity
        ? { title_content_similarity_score: 2, deception_score: 8, focus_ratio_score: 3,
            time_to_main_content_score: 4, sponsor_interruption_score: 0, main_content_start_s: 120,
            language: "en", honest_title: "A calmer factual title", verdict_line: "Big gap between title and content." }
        : { title_content_similarity_score: 9, deception_score: 0, focus_ratio_score: 8,
            time_to_main_content_score: 9, sponsor_interruption_score: 0, main_content_start_s: 5,
            language: "en", honest_title: "Should be null", verdict_line: "Title matches the content." };
      res.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(payload) } }] }));
    });
  });
  mockServer.listen(MOCK_PORT);
});
after(() => mockServer?.close());

// --- helpers ---
const vid = (s) => s.padEnd(11, "x").slice(0, 11);
const denseSegments = () => {
  const segs = [];
  for (let i = 0; i < 20; i++) segs.push({ t: i * 30, text: "one two three four five six seven" });
  return segs; // ~140 wpm over 600s, full coverage
};
// Same words but re-timed / re-cased: normalizes to the SAME transcript -> SAME hash.
const denseSegmentsVariant = () => {
  const segs = [];
  for (let i = 0; i < 20; i++) segs.push({ t: i * 31 + 0.5, text: "One, Two, THREE four five six  seven!" });
  return segs;
};
async function score(token, videoId, segments, durationS, meta) {
  const resp = await fetch(`${FUNCTIONS_URL}/score-video`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId, segments, duration_s: durationS, meta, source: "extension" }),
  });
  return { status: resp.status, body: await resp.json() };
}
async function bulkRead(ids) {
  const inList = ids.map((i) => `"${i}"`).join(",");
  const resp = await fetch(
    `${SUPABASE_URL}/rest/v1/video_scores?video_id=in.(${inList})&select=video_id,status,video_score,flags_count`,
    { headers: { apikey: ANON, Authorization: `Bearer ${ANON}` } },
  );
  return resp.json();
}
async function flag(token, videoId) {
  const r = await fetch(`${FUNCTIONS_URL}/flag-video`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId, reason: "wrong_score" }),
  });
  return r.status;
}

// Create a fresh confirmed user and return a user access_token.
async function makeUser() {
  const email = `t${Date.now()}${Math.random().toString(36).slice(2)}@example.com`;
  const password = "password123!";
  await fetch(`${SUPABASE_URL}/auth/v1/admin/users`, {
    method: "POST",
    headers: { apikey: SERVICE, Authorization: `Bearer ${SERVICE}`, "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, email_confirm: true }),
  });
  const resp = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { apikey: ANON, "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const data = await resp.json();
  return data.access_token;
}

test("submitter gets own scored result (provisional, honest -> honest_title null)", { skip }, async () => {
  const token = await makeUser();
  const { status, body } = await score(token, vid("solo1"), denseSegments(), 600, { category: "Gaming" });
  assert.equal(status, 200);
  assert.equal(body.cached, false);
  assert.equal(body.row.status, "scored");
  assert.equal(body.row.honest_title, null);
  assert.equal(body.row.model, "deepseek-chat");
  // provisional: not yet in the public pool
  const rows = await bulkRead([vid("solo1")]);
  assert.equal(rows.length, 0);
});

test("corroboration: 2 distinct users, same transcript -> verified + publicly readable", { skip }, async () => {
  const t1 = await makeUser();
  const t2 = await makeUser();
  const id = vid("corrob1");
  const r1 = await score(t1, id, denseSegments(), 600, {});
  assert.equal(r1.body.cached, false);
  assert.equal((await bulkRead([id])).length, 0); // still provisional
  const r2 = await score(t2, id, denseSegmentsVariant(), 600, {}); // formatting variant -> same hash
  assert.equal(r2.body.cached, false);
  const rows = await bulkRead([id]);
  assert.equal(rows.length, 1); // now verified
  assert.equal(rows[0].status, "scored");
});

test("corroboration: 3rd user who agrees with a verified row gets cached", { skip }, async () => {
  const t1 = await makeUser(), t2 = await makeUser(), t3 = await makeUser();
  const id = vid("corrob2");
  await score(t1, id, denseSegments(), 600, {});
  await score(t2, id, denseSegments(), 600, {}); // verified
  const r3 = await score(t3, id, denseSegments(), 600, {});
  assert.equal(r3.body.cached, true);
});

test("corroboration: different-hash submitter vs a verified row is ephemeral, pool untouched", { skip }, async () => {
  const t1 = await makeUser(), t2 = await makeUser(), t3 = await makeUser();
  const id = vid("corrob3");
  await score(t1, id, denseSegments(), 600, {});
  await score(t2, id, denseSegments(), 600, {}); // verified (honest)
  const baitSegs = denseSegments().map((s, i) => (i === 0 ? { ...s, text: "this is bait bait bait" } : s));
  const r3 = await score(t3, id, baitSegs, 600, {}); // different transcript -> different hash
  assert.equal(r3.body.cached, false);
  assert.equal(r3.body.row.status, "scored"); // submitter still gets their own result
  assert.equal((await bulkRead([id])).length, 1); // shared verified row unchanged
});

test("service role (crawler) seeds a verified row directly", { skip }, async () => {
  const id = vid("crawler1");
  const r = await score(SERVICE, id, denseSegments(), 600, {});
  assert.equal(r.body.cached, false);
  assert.equal((await bulkRead([id])).length, 1); // verified immediately, no corroboration needed
  const again = await score(SERVICE, id, denseSegments(), 600, {});
  assert.equal(again.body.cached, true); // idempotent
});

test("scored dishonest video -> honest_title set", { skip }, async () => {
  const token = await makeUser();
  const segs = denseSegments().map((s, i) => (i === 0 ? { ...s, text: "this is bait bait bait" } : s));
  const { status, body } = await score(token, vid("baitvid"), segs, 600, {});
  assert.equal(status, 200);
  assert.equal(body.row.status, "scored");
  assert.ok(body.row.honest_title && body.row.honest_title.length > 0);
});

test("gate: music (provisional unscoreable, no quota)", { skip }, async () => {
  const token = await makeUser();
  const { body } = await score(token, vid("music1"), denseSegments(), 600, { category: "Music" });
  assert.equal(body.row.status, "unscoreable");
  assert.equal(body.row.unscoreable_reason, "music");
  assert.equal(body.quota.used, 0); // no quota burned (no LLM)
});

test("gate: live", { skip }, async () => {
  const token = await makeUser();
  const { body } = await score(token, vid("live1"), denseSegments(), 600, { is_live: true });
  assert.equal(body.row.unscoreable_reason, "live");
});

test("gate: too_long", { skip }, async () => {
  const token = await makeUser();
  const { body } = await score(token, vid("long1"), denseSegments(), 6000, {});
  assert.equal(body.row.unscoreable_reason, "too_long");
});

test("gate: no_captions (empty segments)", { skip }, async () => {
  const token = await makeUser();
  const { body } = await score(token, vid("nocap1"), [], 600, {});
  assert.equal(body.row.unscoreable_reason, "no_captions");
});

test("gate: low_density", { skip }, async () => {
  const token = await makeUser();
  const { body } = await score(token, vid("lowd1"), [{ t: 0, text: "hi" }, { t: 5, text: "bye" }], 600, {});
  assert.equal(body.row.unscoreable_reason, "low_density");
});

test("charset: non-matching video_id -> 400 (M2)", { skip }, async () => {
  const token = await makeUser();
  const { status, body } = await score(token, "abc!@#12345", denseSegments(), 600, {});
  assert.equal(status, 400);
  assert.equal(body.error, "bad_request");
});

test("atomic quota exhaustion -> 403 after TEST_QUOTA=1", { skip }, async () => {
  const token = await makeUser();
  await score(token, vid("q1"), denseSegments(), 600, {}); // reserves the 1 allowed
  const { status, body } = await score(token, vid("q2"), denseSegments(), 600, {});
  assert.equal(status, 403);
  assert.equal(body.error, "quota_exhausted");
});

test("401 unauth (no token)", { skip }, async () => {
  const resp = await fetch(`${FUNCTIONS_URL}/score-video`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: vid("una"), segments: [], duration_s: 1 }),
  });
  assert.equal(resp.status, 401);
});

test("flag idempotency on a verified row (flags_count bumps once)", { skip }, async () => {
  const t1 = await makeUser(), t2 = await makeUser();
  const id = vid("flag1");
  await score(t1, id, denseSegments(), 600, {});
  await score(t2, id, denseSegments(), 600, {}); // verified -> publicly readable
  assert.equal(await flag(t1, id), 200);
  assert.equal(await flag(t1, id), 200); // idempotent (same user)
  const rows = await bulkRead([id]);
  assert.equal(rows[0].flags_count, 1); // one distinct flagger, below suppress threshold
});

test("flag auto-suppress: 3 distinct flaggers drop a verified row from the pool", { skip }, async () => {
  const t1 = await makeUser(), t2 = await makeUser();
  const f1 = await makeUser(), f2 = await makeUser(), f3 = await makeUser();
  const id = vid("supp1");
  await score(t1, id, denseSegments(), 600, {});
  await score(t2, id, denseSegments(), 600, {}); // verified
  assert.equal((await bulkRead([id])).length, 1);
  await flag(f1, id);
  await flag(f2, id);
  assert.equal((await bulkRead([id])).length, 1); // below threshold (default 3)
  await flag(f3, id);
  assert.equal((await bulkRead([id])).length, 0); // suppressed, must re-corroborate
});

test("expand-video: 404 missing, 409 unverified, reasonings on verified", { skip }, async () => {
  const t1 = await makeUser(), t2 = await makeUser();
  const expandReq = async (token, videoId, segments) => {
    const r = await fetch(`${FUNCTIONS_URL}/expand-video`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ video_id: videoId, segments }),
    });
    return { status: r.status, body: await r.json() };
  };
  // missing row
  assert.equal((await expandReq(t1, vid("exmiss"), denseSegments())).status, 404);
  // provisional (single user) -> 409
  const id = vid("expand1");
  await score(t1, id, denseSegments(), 600, {});
  assert.equal((await expandReq(t1, id, denseSegments())).status, 409);
  // verify then expand -> reasonings
  await score(t2, id, denseSegments(), 600, {});
  const ok = await expandReq(t1, id, denseSegments());
  assert.equal(ok.status, 200);
  assert.ok(ok.body.reasonings && typeof ok.body.reasonings.tcs === "string");
});
