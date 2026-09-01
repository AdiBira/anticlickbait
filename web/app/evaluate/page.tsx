import { Suspense } from "react";
import { createClient } from "@supabase/supabase-js";
import { EVAL_CACHE_FIELDS_FULL } from "../lib/api";
import CompletedChannelResults from "./CompletedChannelResults";
import ClientEvaluatePage from "./ClientEvaluate";

function parseChannelId(url: string): { type: "id" | "handle"; value: string } | null {
  const m1 = url.match(/\/@([A-Za-z0-9_.-]+)/);
  if (m1) return { type: "handle", value: `@${m1[1]}` };
  const m2 = url.match(/\/channel\/(UC[A-Za-z0-9_-]+)/);
  if (m2) return { type: "id", value: m2[1] };
  return null;
}

export default async function EvaluatePage({
  searchParams,
}: {
  searchParams: Promise<{ v?: string; channel?: string }>;
}) {
  const params = await searchParams;

  // Server-side: if channel is already fully evaluated, render results directly (no loading flash)
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;

  if (params.channel && serviceKey && supabaseUrl) {
    const parsed = parseChannelId(params.channel);
    if (parsed) {
      try {
        const sb = createClient(supabaseUrl, serviceKey);

        let query = sb
          .from("channels")
          .select("channel_id,title,thumbnail_url,subscriber_count,channel_score,channel_summary,videos_evaluated");
        if (parsed.type === "id") query = query.eq("channel_id", parsed.value);
        else query = query.eq("handle", parsed.value);

        const { data: channels } = await query.limit(1);
        const channel = channels?.[0];

        if (channel?.channel_summary && channel?.channel_score != null) {
          const { data: videos } = await sb
            .from("eval_cache")
            .select(EVAL_CACHE_FIELDS_FULL)
            .eq("channel_id", channel.channel_id)
            .eq("status", "complete");

          if (videos && videos.length > 0) {
            return <CompletedChannelResults channel={channel} videos={videos} />;
          }
        }
      } catch {
        // Server check failed, fall through to client component
      }
    }
  }

  // Client component handles: new evals, in-progress evals, video evals, auth, history
  return (
    <Suspense>
      <ClientEvaluatePage videoId={params.v || ""} channelUrl={params.channel || ""} />
    </Suspense>
  );
}
