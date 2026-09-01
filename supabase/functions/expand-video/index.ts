// Edge Fn: expand-video — the "Why?" detail pass. See the API contract.
// Only a VERIFIED row can be expanded (404 if missing, 409 if unverified) so it
// can't be used to burn budget on unverified/junk rows. reasonings already present
// -> return cached (no quota). Else atomic-reserve one expand (H2), run the detail
// pass, sanitize + store the reasonings. Needs the transcript again -> segments
// required (422), since transcripts are never stored.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { authenticate, CORS, json, sha256 } from "../_shared/http.ts";
import { EXPAND_LIMIT, MAX_REASONING } from "../_shared/config.ts";
import { normalizeTranscript, type Segment } from "../_shared/logic.ts";
import { sanitizeText } from "../_shared/sanitize.ts";
import { buildPrompt, DETAIL_PASS_PROMPT } from "../_shared/prompts.ts";
import { callDeepSeek } from "../_shared/deepseek.ts";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const LIMIT = Number(Deno.env.get("TEST_EXPAND_QUOTA") ?? EXPAND_LIMIT);
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

function transcriptText(segments: Segment[]): string {
  return segments.map((s) => `[${s.t.toFixed(1)}s] ${s.text}`).join("\n");
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "bad_request", detail: "POST only" }, 400);

  const auth = await authenticate(req);
  if (auth.kind === "invalid") return json({ error: "auth_required" }, 401);
  const userId = auth.kind === "user" ? auth.userId : null;

  let body: { video_id?: string; segments?: Segment[] };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request", detail: "invalid JSON" }, 400);
  }

  const videoId = body.video_id ?? "";
  if (!VIDEO_ID_RE.test(videoId)) {
    return json({ error: "bad_request", detail: "video_id must match [A-Za-z0-9_-]{11}" }, 400);
  }

  const { data: rows } = await supabase
    .from("video_scores")
    .select("video_id,status,verified,outcome_hash,title,duration_s,tcs,deception,focus,ttmc,sponsor,reasonings")
    .eq("video_id", videoId)
    .limit(1);
  const row = rows && rows.length > 0 ? rows[0] : null;
  if (!row) return json({ error: "not_found" }, 404);
  // Only scored videos have metrics to explain.
  if (row.status !== "scored") return json({ error: "not_scoreable" }, 409);

  if (row.reasonings) return json({ reasonings: row.reasonings });

  const segments = body.segments ?? [];
  if (segments.length === 0) return json({ error: "segments_required" }, 422);

  // The submitted transcript must match the one that produced this score, so the
  // explanation is always derived from the corroborated transcript and can't be
  // poisoned. This lets a submitter explain their own provisional row (they hold
  // the matching transcript) without opening a defamation vector via reasonings.
  const submittedHash = `S:${await sha256(normalizeTranscript(segments))}`;
  if (submittedHash !== row.outcome_hash) return json({ error: "transcript_mismatch" }, 409);

  // Atomic reserve against the expand cap (service role skips). Refund on failure.
  if (userId) {
    const { data: reserved } = await supabase.rpc("ext_reserve_expand", { p_user_id: userId, p_limit: LIMIT });
    if (typeof reserved !== "number" || reserved < 0) {
      return json({ error: "quota_exhausted", quota: { used: LIMIT, limit: LIMIT } }, 403);
    }
  }

  let llm: Record<string, unknown>;
  try {
    llm = await callDeepSeek(
      buildPrompt(DETAIL_PASS_PROMPT, {
        title: row.title ?? "",
        duration_seconds: row.duration_s ?? 0,
        transcript: transcriptText(segments),
        tcs: row.tcs,
        deception: row.deception,
        focus: row.focus,
        ttmc: row.ttmc,
        sponsor: row.sponsor,
      }),
    );
  } catch {
    if (userId) await supabase.rpc("ext_refund_expand", { p_user_id: userId });
    return json({ error: "llm_failed" }, 502);
  }

  const reasonings = {
    tcs: sanitizeText(String(llm.tcs ?? ""), MAX_REASONING),
    deception: sanitizeText(String(llm.deception ?? ""), MAX_REASONING),
    focus: sanitizeText(String(llm.focus ?? ""), MAX_REASONING),
    ttmc: sanitizeText(String(llm.ttmc ?? ""), MAX_REASONING),
    sponsor: sanitizeText(String(llm.sponsor ?? ""), MAX_REASONING),
  };

  await supabase
    .from("video_scores")
    .update({ reasonings, detail_at: new Date().toISOString() })
    .eq("video_id", videoId);

  return json({ reasonings });
});
