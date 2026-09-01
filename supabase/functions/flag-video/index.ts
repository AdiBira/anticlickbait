// Edge Fn: flag-video — "Wrong score?" flag. See the API contract.
// Idempotent per (video_id, user_id): re-flagging the same video is a no-op that
// still returns ok, and flags_count only bumps on the first flag.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { authenticate, CORS, json } from "../_shared/http.ts";
import { FLAG_SUPPRESS_THRESHOLD } from "../_shared/config.ts";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const REASONS = ["wrong_score", "wrong_title", "other"];
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
const SUPPRESS_AT = Number(Deno.env.get("TEST_FLAG_SUPPRESS") ?? FLAG_SUPPRESS_THRESHOLD);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "bad_request", detail: "POST only" }, 400);

  const auth = await authenticate(req);
  if (auth.kind !== "user") return json({ error: "auth_required" }, 401);

  let body: { video_id?: string; reason?: string };
  try {
    body = await req.json();
  } catch {
    return json({ error: "bad_request", detail: "invalid JSON" }, 400);
  }

  const videoId = body.video_id ?? "";
  const reason = body.reason ?? "";
  if (!VIDEO_ID_RE.test(videoId)) {
    return json({ error: "bad_request", detail: "video_id must match [A-Za-z0-9_-]{11}" }, 400);
  }
  if (!REASONS.includes(reason)) return json({ error: "bad_request", detail: "invalid reason" }, 400);

  // ignoreDuplicates => idempotent; returned rows non-empty only on first insert.
  const { data } = await supabase
    .from("ext_flags")
    .upsert(
      { video_id: videoId, user_id: auth.userId, reason },
      { onConflict: "video_id,user_id", ignoreDuplicates: true },
    )
    .select("video_id");

  // flags_count bumps once per distinct user (idempotent flag), so it equals the
  // distinct flagger count. Once it reaches the threshold, un-verify the row and
  // wipe its corroboration so a sockpuppet-verified fake drops out of the public
  // pool and must be re-corroborated by fresh, independent submitters.
  if (data && data.length > 0) {
    const { data: count } = await supabase.rpc("ext_bump_flag", { p_video_id: videoId });
    if (typeof count === "number" && count >= SUPPRESS_AT) {
      await supabase.rpc("ext_suppress_video", { p_video_id: videoId });
    }
  }

  return json({ ok: true });
});
