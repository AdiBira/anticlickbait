// Unit tests for the pure backend logic (gates, wpm/coverage, composite, retitle).
// Run: node --test tests/extension_backend/test_units.mjs
// (Node imports the .ts modules directly via type stripping.)

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  computeComposite,
  computeCoverage,
  computeWpm,
  decideCorroboration,
  evaluateGates,
  normalizeTranscript,
  outcomeFromGate,
  outcomeHash,
  retitleTriggered,
} from "../../supabase/functions/_shared/logic.ts";
import { sanitizeText } from "../../supabase/functions/_shared/sanitize.ts";

const sha256 = (s) => Promise.resolve(createHash("sha256").update(s).digest("hex"));

const seg = (t, text) => ({ t, text });
const dense = [seg(0, "one two three four five"), seg(30, "six seven eight nine ten")];

test("computeWpm: words over duration in minutes", () => {
  // 10 words over 1 minute = 10 wpm
  assert.equal(computeWpm([seg(0, "a b c d e f g h i j")], 60), 10);
  assert.equal(computeWpm([], 60), 0);
  assert.equal(computeWpm(dense, 0), 0);
});

test("computeCoverage: caption span over duration, clamped", () => {
  assert.equal(computeCoverage([seg(0, "x"), seg(30, "y")], 60), 0.5);
  assert.equal(computeCoverage([seg(0, "x"), seg(120, "y")], 60), 1); // clamped
  assert.equal(computeCoverage([], 60), 0);
});

test("evaluateGates: fixed order music>live>too_long>no_captions>low_density", () => {
  assert.deepEqual(evaluateGates({ category: "Music" }, 600, dense, 100, 1), { ok: false, reason: "music" });
  assert.deepEqual(evaluateGates({ is_live: true }, 600, dense, 100, 1), { ok: false, reason: "live" });
  assert.deepEqual(evaluateGates({}, 5401, dense, 100, 1), { ok: false, reason: "too_long" });
  assert.deepEqual(evaluateGates({}, 600, [], 100, 1), { ok: false, reason: "no_captions" });
  assert.deepEqual(evaluateGates({}, 600, dense, 39, 1), { ok: false, reason: "low_density" });
  assert.deepEqual(evaluateGates({}, 600, dense, 100, 0.59), { ok: false, reason: "low_density" });
});

test("evaluateGates: music wins over live and too_long", () => {
  assert.deepEqual(
    evaluateGates({ category: "Music", is_live: true }, 9999, [], 0, 0),
    { ok: false, reason: "music" },
  );
});

test("evaluateGates: passes when dense and within limits", () => {
  assert.deepEqual(evaluateGates({ category: "Gaming" }, 600, dense, 120, 0.9), { ok: true });
});

test("computeComposite: v1.1 formula, clamped 0-100", () => {
  // perfect honest: (0.5*10+0.3*10+0.2*10)*10 = 100
  assert.equal(computeComposite(10, 10, 10, 0, 0), 100);
  // deception penalty: base 100 - (10/10)*20 = 80
  assert.equal(computeComposite(10, 10, 10, 10, 0), 80);
  // sponsor penalty: base 100 - (10/10)*10 = 90
  assert.equal(computeComposite(10, 10, 10, 0, 10), 90);
  // clamps to 0
  assert.equal(computeComposite(0, 0, 0, 10, 10), 0);
  // out-of-range inputs clamp to 0-10 first
  assert.equal(computeComposite(99, 10, 10, -5, 0), 100);
});

test("retitleTriggered: deception>=5 OR tcs<=4", () => {
  assert.equal(retitleTriggered(5, 10), true); // deception hits
  assert.equal(retitleTriggered(0, 4), true); // tcs hits
  assert.equal(retitleTriggered(4, 5), false); // neither
  assert.equal(retitleTriggered(0, 10), false); // honest video -> no retitle
});

// ---- outcome_hash normalization ----

test("normalizeTranscript: formatting variants of the same text agree", () => {
  const a = [seg(0, "Hello, WORLD!"), seg(12.5, "It's a  test.")];
  const b = [seg(3.2, "hello world"), seg(99, "its a test")];
  assert.equal(normalizeTranscript(a), normalizeTranscript(b));
});

test("normalizeTranscript: different text disagrees", () => {
  assert.notEqual(
    normalizeTranscript([seg(0, "the cat sat")]),
    normalizeTranscript([seg(0, "the dog ran")]),
  );
});

test("outcomeHash: scored is S:<sha> and agrees across formatting; unscoreable is U:reason", async () => {
  const scored = outcomeFromGate({ ok: true });
  const a = await outcomeHash(scored, [seg(0, "Hello, WORLD!")], sha256);
  const b = await outcomeHash(scored, [seg(9, "hello world")], sha256);
  assert.equal(a, b); // same normalized text -> same hash
  assert.ok(a.startsWith("S:"));

  const unscoreable = outcomeFromGate({ ok: false, reason: "music" });
  const u = await outcomeHash(unscoreable, [], sha256);
  assert.equal(u, "U:music"); // per-outcome, segments ignored

  const diff = await outcomeHash(scored, [seg(0, "totally different words")], sha256);
  assert.notEqual(a, diff);
});

// ---- sanitizeText ----

test("sanitizeText: strips URLs, kills control chars, caps length", () => {
  assert.equal(sanitizeText("see https://evil.com/x now", 200), "see now");
  assert.equal(sanitizeText("visit www.evil.com today", 200), "visit today");
  assert.equal(sanitizeText("go to evil.com please", 200), "go to please");
  const raw = "a" + String.fromCharCode(0) + "b" + String.fromCharCode(9) + "c";
  assert.equal(sanitizeText(raw, 200), "a b c"); // control chars -> spaces, collapsed
  assert.equal(sanitizeText("x".repeat(300), 100).length, 100); // hard cap
});

// ---- corroboration state machine (pure decision, fake in-memory store) ----

function makeSim(flagThreshold = 3) {
  let row = null; // { hash, verified, agree_count }
  const flags = new Set();
  const submissions = new Map(); // userId -> hash

  function submit(userId, hash) {
    // verified + agree -> served as a cache hit upstream (no submission recorded).
    if (row && row.verified && row.hash === hash) return { action: "cache_hit" };
    submissions.set(userId, hash);
    const n = [...submissions.values()].filter((h) => h === hash).length;
    const decision = decideCorroboration({
      existingVerified: !!(row && row.verified),
      existingHash: row ? row.hash : null,
      submissionHash: hash,
      agreeCount: n,
    });
    if (decision.action === "promote_flip") { row.verified = true; row.agree_count = n; }
    else if (decision.action === "promote_compute") row = { hash, verified: true, agree_count: n };
    else if (decision.action === "store_provisional") row = { hash, verified: false, agree_count: n };
    // ephemeral / ephemeral_conflict: persist nothing.
    return { action: decision.action };
  }

  function flag(userId) {
    if (!row) return;
    flags.add(userId);
    if (flags.size >= flagThreshold) { row.verified = false; row.agree_count = 1; submissions.clear(); }
  }

  return { submit, flag, get row() { return row; } };
}

test("corroboration: a lone attacker never yields a verified row", () => {
  const sim = makeSim();
  sim.submit("attacker", "S:fake");
  sim.submit("attacker", "S:fake"); // resubmit: still one distinct user
  assert.equal(sim.row.verified, false);
});

test("corroboration: 2 distinct users, same hash -> verified", () => {
  const sim = makeSim();
  assert.equal(sim.submit("u1", "S:x").action, "store_provisional");
  assert.equal(sim.submit("u2", "S:x").action, "promote_flip");
  assert.equal(sim.row.verified, true);
  assert.equal(sim.row.agree_count, 2);
});

test("corroboration: 2 users with different hashes -> neither verified", () => {
  const sim = makeSim();
  assert.equal(sim.submit("u1", "S:a").action, "store_provisional");
  assert.equal(sim.submit("u2", "S:b").action, "ephemeral_conflict");
  assert.equal(sim.row.verified, false);
});

test("corroboration: different-hash submitter vs a verified row is ephemeral, no flip", () => {
  const sim = makeSim();
  sim.submit("u1", "S:a");
  sim.submit("u2", "S:a"); // verified S:a
  const r = sim.submit("u3", "S:b");
  assert.equal(r.action, "ephemeral");
  assert.equal(sim.row.verified, true); // untouched
  assert.equal(sim.row.hash, "S:a");
});

test("corroboration: flag threshold un-verifies the row", () => {
  const sim = makeSim(3);
  sim.submit("u1", "S:a");
  sim.submit("u2", "S:a"); // verified
  assert.equal(sim.row.verified, true);
  sim.flag("f1");
  sim.flag("f2");
  assert.equal(sim.row.verified, true); // below threshold
  sim.flag("f3");
  assert.equal(sim.row.verified, false); // suppressed, must re-corroborate
});

// ---- atomic quota reserve (models the ext_reserve_fresh RPC contract) ----

function makeQuota(limit) {
  let n = 0;
  return {
    reserve: () => (n >= limit ? -1 : ++n), // atomic: never increments past limit
    refund: () => (n = Math.max(0, n - 1)),
    get n() { return n; },
  };
}

test("atomic quota: N reserves never exceed the limit; refund restores one", () => {
  const q = makeQuota(150);
  let granted = 0;
  for (let i = 0; i < 200; i++) if (q.reserve() !== -1) granted++;
  assert.equal(granted, 150); // exactly the cap was handed out
  assert.equal(q.n, 150); // never past the limit
  assert.equal(q.reserve(), -1); // still blocked
  q.refund();
  assert.equal(q.n, 149); // refund frees one
  assert.notEqual(q.reserve(), -1); // now grantable again
});
