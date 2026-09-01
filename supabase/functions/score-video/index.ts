// Edge Fn: score-video — the extension's fresh-eval endpoint. See the API contract.
//
// Crowd-corroboration: a score reaches OTHER users (the public bulk read) only
// after 2 DISTINCT users independently submit transcripts that AGREE. A submitter
// always gets their own freshly-computed result in the POST response; unverified
// rows are invisible to the anon/authenticated read (RLS USING verified=true).
//
// Order: OPTIONS/auth -> parse -> charset-validate (M2) -> outcome+hash ->
// verified-cache fast path -> record submission -> decide -> (atomic quota reserve
// BEFORE any LLM, refund on failure) -> persist per the decision -> return.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { authenticate, CORS, json, sha256 } from "../_shared/http.ts";
import { MODEL, QUOTA_LIMIT, SCORER_VERSION, MAX_HONEST_TITLE, MAX_VERDICT_LINE } from "../_shared/config.ts";
import {
  computeComposite,
  computeCoverage,
  computeWpm,
  decideCorroboration,
  evaluateGates,
  outcomeFromGate,
  outcomeHash,
  retitleTriggered,
  type Outcome,
  type Segment,
} from "../_shared/logic.ts";
import { sanitizeText } from "../_shared/sanitize.ts";
import { buildPrompt, SCORER_V2_PROMPT } from "../_shared/prompts.ts";
import { callDeepSeek } from "../_shared/deepseek.ts";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const LIMIT = Number(Deno.env.get("TEST_QUOTA") ?? QUOTA_LIMIT);
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

const SELECT =
  "video_id,status,unscoreable_reason,scorer_version,model,language,title,honest_title,verdict_line,video_score,tcs,deception,focus,ttmc,sponsor,reasonings,main_content_start_s,duration_s,words_per_min,caption_coverage,flags_count,created_at,detail_at";
const RESPONSE_KEYS = SELECT.split(",");

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

async function freshUsed(userId: string): Promise<number> {
  const { data } = await supabase
    .from("ext_usage")
    .select("fresh_evals")
    .eq("user_id", userId)
    .eq("day", today())
    .limit(1);
  return data && data.length > 0 ? data[0].fresh_evals : 0;
}

async function fetchTitle(videoId: string): Promise<string | null> {
  const base = Deno.env.get("OEMBED_API_BASE") ?? "https://www.youtube.com";
  const url = `${base}/oembed?url=https://www.youtube.com/watch?v=${videoId}&format=json`;
  const resp = await fetch(url);
  if (!resp.ok) return null;
  const data = await resp.json();
  return typeof data?.title === "string" ? data.title : null;
}

function transcriptText(segments: Segment[]): string {
  return segments.map((s) => `[${s.t.toFixed(1)}s] ${s.text}`).join("\n");
}

function pick(row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of RESPONSE_KEYS) out[k] = row[k] ?? null;
  return out;
}

type RowFields = Record<string, unknown>;

// Build the persisted/returned fields for an unscoreable outcome (no LLM).
function unscoreableFields(
  videoId: string,
  reason: string,
  durationS: number,
  wpm: number,
  coverage: number,
  transcriptHash: string | null,
): RowFields {
  return {
    video_id: videoId,
    status: "unscoreable",
    unscoreable_reason: reason,
    scorer_version: SCORER_VERSION,
    duration_s: durationS,
    transcript_hash: transcriptHash,
    words_per_min: wpm,
    caption_coverage: coverage,
  };
}

// Build the persisted/returned fields for a scored outcome from an LLM response.
// honest_title / verdict_line are sanitized (C2 defense-in-depth) before storage.
function scoredFields(
  videoId: string,
  title: string,
  durationS: number,
  wpm: number,
  coverage: number,
  transcriptHash: string,
  llm: Record<string, unknown>,
): RowFields {
  const tcs = Number(llm.title_content_similarity_score);
  const deception = Number(llm.deception_score);
  const focus = Number(llm.focus_ratio_score);
  const ttmc = Number(llm.time_to_main_content_score);
  const sponsor = Number(llm.sponsor_interruption_score);
  const honest = retitleTriggered(deception, tcs)
    ? sanitizeText(String(llm.honest_title ?? ""), MAX_HONEST_TITLE)
    : null;
  return {
    video_id: videoId,
    status: "scored",
    scorer_version: SCORER_VERSION,
    model: MODEL,
    language: String(llm.language ?? ""),
    title,
    honest_title: honest,
    verdict_line: sanitizeText(String(llm.verdict_line ?? ""), MAX_VERDICT_LINE),
    video_score: computeComposite(tcs, focus, ttmc, deception, sponsor),
    tcs,
    deception,
    focus,
    ttmc,
    sponsor,
    main_content_start_s: Number(llm.main_content_start_s ?? 0),
    duration_s: durationS,
    transcript_hash: transcriptHash,
    words_per_min: wpm,
    caption_coverage: coverage,
  };
}

Deno.serve(async (req): Promise<Response> => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "bad_request", detail: "POST only" }, 400);

  const auth = await authenticate(req);
  if (auth.kind === "invalid") return json({ error: "auth_required" }, 401);
  const isService = auth.kind === "service";
  const userId = auth.kind === "user" ? auth.userId : null;

  let body: {
    video_id?: string;
    segments?: Segment[];
    duration_s?: number;
    meta?: { category?: string; is_live?: boolean; channel_id?: string };
  };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request", detail: "invalid JSON" }, 400);
  }

  const videoId = body.video_id ?? "";
  if (!VIDEO_ID_RE.test(videoId)) {
    return json({ error: "bad_request", detail: "video_id must match [A-Za-z0-9_-]{11}" }, 400);
  }

  const segments = body.segments ?? [];
  const durationS = body.duration_s ?? 0;
  const meta = body.meta ?? {};

  const quotaNow = { used: userId ? await freshUsed(userId) : 0, limit: LIMIT };

  // Outcome (scored vs a gate reason) + its corroboration key. No LLM here.
  const wpm = computeWpm(segments, durationS);
  const coverage = computeCoverage(segments, durationS);
  const outcome: Outcome = outcomeFromGate(evaluateGates(meta, durationS, segments, wpm, coverage));
  const transcriptHash = segments.length > 0 ? await sha256(transcriptText(segments)) : null;
  const hash = await outcomeHash(outcome, segments, sha256);

  // Load the single row for this video (PK video_id), with its internal state.
  const existingQ = await supabase
    .from("video_scores")
    .select(SELECT + ",verified,outcome_hash,agree_count")
    .eq("video_id", videoId)
    .limit(1);
  const existing = (existingQ.data?.[0] ?? null) as Record<string, unknown> | null;

  // Runs the LLM for a scored outcome (title fetched server-side). Returns null on
  // a failure the caller should surface as 502 (title/LLM), never a partial row.
  async function computeScored(): Promise<{ ok: true; fields: RowFields } | { ok: false; resp: Response }> {
    const title = await fetchTitle(videoId);
    if (title === null) return { ok: false, resp: json({ error: "title_fetch_failed" }, 502) };
    let llm: Record<string, unknown>;
    try {
      llm = await callDeepSeek(
        buildPrompt(SCORER_V2_PROMPT, {
          title,
          duration_seconds: durationS,
          transcript: transcriptText(segments),
        }),
      );
    } catch {
      return { ok: false, resp: json({ error: "llm_failed" }, 502) };
    }
    return { ok: true, fields: scoredFields(videoId, title, durationS, wpm, coverage, transcriptHash!, llm) };
  }

  // Trusted seed: the crawler (service role) has no user to corroborate with, so it
  // writes verified rows directly and skips quota. An existing verified row is served.
  if (isService) {
    if (existing?.verified) return json({ row: pick(existing), cached: true, quota: quotaNow });
    let fields: RowFields;
    if (outcome.status === "scored") {
      const r = await computeScored();
      if (!r.ok) return r.resp;
      fields = r.fields;
    } else {
      fields = unscoreableFields(videoId, outcome.reason, durationS, wpm, coverage, transcriptHash);
    }
    const up = await supabase
      .from("video_scores")
      .upsert({ ...fields, verified: true, outcome_hash: hash, agree_count: 1 }, { onConflict: "video_id" })
      .select(SELECT);
    return json({ row: up.data![0], cached: false, quota: quotaNow });
  }

  // From here the caller is an authenticated user (userId is set).
  // Fast path: an already-verified row this user AGREES with -> serve it, no
  // submission, no LLM. (Disagreement falls through to an ephemeral own-result.)
  if (existing?.verified && existing.outcome_hash === hash) {
    return json({ row: pick(existing), cached: true, quota: quotaNow });
  }

  if (userId === null) return json({ error: "auth_required" }, 401);

  // Record this user's submission (idempotent per user+video) and count distinct
  // corroborators of THIS outcome. PK (video_id,user_id) means count == distinct.
  await supabase
    .from("video_submissions")
    .upsert({ video_id: videoId, user_id: userId, outcome_hash: hash }, { onConflict: "video_id,user_id" });
  const countQ = await supabase
    .from("video_submissions")
    .select("user_id", { count: "exact", head: true })
    .eq("video_id", videoId)
    .eq("outcome_hash", hash);
  const n = countQ.count ?? 1;

  const decision = decideCorroboration({
    existingVerified: !!existing?.verified,
    existingHash: existing ? (existing.outcome_hash as string | null) : null,
    submissionHash: hash,
    agreeCount: n,
  });

  const needsLlm = outcome.status === "scored" && decision.action !== "promote_flip";

  // Atomic quota reserve BEFORE any LLM (H2). Unscoreable outcomes and promote_flip
  // run no LLM and cost nothing. Refund on LLM/title failure.
  let quotaUsed = quotaNow.used;
  let computed: RowFields | null = null;
  if (needsLlm) {
    const { data: reserved } = await supabase.rpc("ext_reserve_fresh", { p_user_id: userId, p_limit: LIMIT });
    if (typeof reserved !== "number" || reserved < 0) {
      return json({ error: "quota_exhausted", quota: { used: LIMIT, limit: LIMIT } }, 403);
    }
    quotaUsed = reserved;
    const r = await computeScored();
    if (!r.ok) {
      await supabase.rpc("ext_refund_fresh", { p_user_id: userId });
      return r.resp;
    }
    computed = r.fields;
  } else if (outcome.status === "unscoreable") {
    computed = unscoreableFields(videoId, outcome.reason, durationS, wpm, coverage, transcriptHash);
  }

  const quota = { used: quotaUsed, limit: LIMIT };

  switch (decision.action) {
    // Verified row with a different hash, or a lone provisional of a different hash:
    // serve this submitter their own result, persist nothing, leave the pool untouched.
    case "ephemeral":
    case "ephemeral_conflict": {
      const row = pick({ ...computed!, verified: false, agree_count: 1, flags_count: 0, created_at: new Date().toISOString(), detail_at: null, reasonings: null });
      return json({ row, cached: false, quota });
    }
    // n>=2 and the provisional already holds this hash: flip to verified, reuse fields.
    case "promote_flip": {
      const up = await supabase
        .from("video_scores")
        .update({ verified: true, agree_count: n })
        .eq("video_id", videoId)
        .select(SELECT);
      return json({ row: up.data![0], cached: false, quota });
    }
    // n>=2 without a matching provisional: store this hash's result as verified
    // (overwrites any lone provisional of a different hash).
    case "promote_compute": {
      const up = await supabase
        .from("video_scores")
        .upsert({ ...computed!, verified: true, outcome_hash: hash, agree_count: n, requested_by: userId }, { onConflict: "video_id" })
        .select(SELECT);
      return json({ row: up.data![0], cached: false, quota });
    }
    // n<2 and no row yet: store as provisional (invisible to the public read).
    case "store_provisional": {
      const up = await supabase
        .from("video_scores")
        .upsert({ ...computed!, verified: false, outcome_hash: hash, agree_count: n, requested_by: userId }, { onConflict: "video_id" })
        .select(SELECT);
      return json({ row: up.data![0], cached: false, quota });
    }
  }

  // Unreachable: decision.action is exhaustive above.
  return json({ error: "bad_request", detail: "unreachable" }, 400);
});
