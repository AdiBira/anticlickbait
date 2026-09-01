// Pure scoring logic - no runtime access, unit-testable from node.

import { CORROBORATION_THRESHOLD, GATES, RETITLE } from "./config.ts";

export type Segment = { t: number; text: string };

// words / (duration in minutes)
export function computeWpm(segments: Segment[], durationS: number): number {
  if (durationS <= 0) return 0;
  const words = segments.reduce(
    (n, s) => n + s.text.trim().split(/\s+/).filter(Boolean).length,
    0,
  );
  return words / (durationS / 60);
}

// caption time span / duration, clamped to 1
export function computeCoverage(segments: Segment[], durationS: number): number {
  if (durationS <= 0 || segments.length === 0) return 0;
  const times = segments.map((s) => s.t);
  const span = Math.max(...times) - Math.min(...times);
  return Math.min(1, span / durationS);
}

export type Meta = { category?: string; is_live?: boolean };
export type GateResult = { ok: true } | { ok: false; reason: string };

// Gate order is fixed: music -> live -> too_long -> no_captions -> low_density.
export function evaluateGates(
  meta: Meta,
  durationS: number,
  segments: Segment[],
  wpm: number,
  coverage: number,
): GateResult {
  if (meta.category === GATES.MUSIC_CATEGORY) return { ok: false, reason: "music" };
  if (meta.is_live) return { ok: false, reason: "live" };
  if (durationS > GATES.MAX_DURATION_S) return { ok: false, reason: "too_long" };
  if (segments.length === 0) return { ok: false, reason: "no_captions" };
  if (wpm < GATES.MIN_WPM || coverage < GATES.MIN_COVERAGE) {
    return { ok: false, reason: "low_density" };
  }
  return { ok: true };
}

export function retitleTriggered(deception: number, tcs: number): boolean {
  return deception >= RETITLE.MIN_DECEPTION || tcs <= RETITLE.MAX_TCS;
}

function clamp10(n: number): number {
  return Math.max(0, Math.min(10, n));
}

// v1.1 composite, unchanged.
export function computeComposite(
  tcs: number,
  focus: number,
  ttmc: number,
  deception: number,
  sponsor: number,
): number {
  tcs = clamp10(tcs);
  focus = clamp10(focus);
  ttmc = clamp10(ttmc);
  deception = clamp10(deception);
  sponsor = clamp10(sponsor);

  const base = (0.5 * tcs + 0.3 * focus + 0.2 * ttmc) * 10;
  const score = base - (deception / 10) * 20 - (sponsor / 10) * 10;
  return Math.round(Math.max(0, Math.min(100, score)) * 100) / 100;
}

// ============================================================
// Crowd-corroboration: a score is served to OTHER users only after two DISTINCT
// users independently submit transcripts that AGREE. Agreement is per-outcome,
// keyed by outcome_hash (below). See decideCorroboration for the state machine.
// ============================================================

// The two possible outcomes of a submission. Scored needs an LLM run; unscoreable
// is decided by the cheap gates alone.
export type Outcome =
  | { status: "scored" }
  | { status: "unscoreable"; reason: string };

export function outcomeFromGate(gate: GateResult): Outcome {
  return gate.ok ? { status: "scored" } : { status: "unscoreable", reason: gate.reason };
}

// Concatenate segment texts only (ignore timestamps), lowercase, strip everything
// except [a-z0-9 ], collapse whitespace, trim. Two honest fetches of the same
// caption track normalize to the same string despite minor formatting differences.
export function normalizeTranscript(segments: Segment[]): string {
  return segments
    .map((s) => s.text)
    .join(" ")
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

// "S:" + sha256(normalized transcript) for scored outcomes; "U:" + reason for
// unscoreable ones (so corroboration is per-outcome). sha256 is injected to keep
// this module free of any runtime crypto (node tests pass their own).
export async function outcomeHash(
  outcome: Outcome,
  segments: Segment[],
  sha256: (s: string) => Promise<string>,
): Promise<string> {
  if (outcome.status === "unscoreable") return `U:${outcome.reason}`;
  return `S:${await sha256(normalizeTranscript(segments))}`;
}

// State fed to the corroboration decision. `existingHash` is the outcome_hash of
// the single video_scores row for this video (PK video_id), or null if none.
// `agreeCount` is the number of DISTINCT users who have submitted `submissionHash`
// (this submitter already counted).
export type Corroboration = {
  existingVerified: boolean;
  existingHash: string | null;
  submissionHash: string;
  agreeCount: number;
};

// What score-video should do with this submission. Every branch except cache_hit
// (handled upstream) and promote_flip runs an LLM only when the outcome is scored.
export type CorroborationAction =
  // A verified row exists with a DIFFERENT hash: serve this submitter their own
  // freshly-computed result, persist nothing, leave the shared row untouched.
  | { action: "ephemeral" }
  // n>=2 and the existing provisional row already holds THIS hash: flip it to
  // verified and reuse its stored fields (no LLM).
  | { action: "promote_flip" }
  // n>=2 but no matching provisional row: compute this hash's result and upsert it
  // as verified (overwrites any lone provisional of a different hash).
  | { action: "promote_compute" }
  // n<2 and no row exists yet: compute and store as a provisional (verified=false).
  | { action: "store_provisional" }
  // n<2 and a provisional row of a DIFFERENT hash exists: serve own result,
  // persist nothing (don't overwrite the other provisional).
  | { action: "ephemeral_conflict" };

export function decideCorroboration(s: Corroboration): CorroborationAction {
  // The agreeing verified case is served as a cache hit upstream (no submission,
  // no LLM), so a verified row reaching here always has a different hash.
  if (s.existingVerified) return { action: "ephemeral" };

  const rowPresent = s.existingHash !== null;
  const sameHash = s.existingHash === s.submissionHash;

  if (s.agreeCount >= CORROBORATION_THRESHOLD) {
    if (rowPresent && sameHash) return { action: "promote_flip" };
    return { action: "promote_compute" };
  }

  if (!rowPresent || sameHash) return { action: "store_provisional" };
  return { action: "ephemeral_conflict" };
}
