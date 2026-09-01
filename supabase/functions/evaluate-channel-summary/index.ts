// Supabase Edge Function: generate channel summary after all videos scored
// Input: { channel_id, channel_name, thumbnail_url, subscriber_count,
//          category_name, country, video_results }
// - Makes 1 LLM call with channel summary prompt
// - Computes channel_score (avg of video scores)
// - Upserts into channels table

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const OPENROUTER_API_KEY = Deno.env.get("OPENROUTER_API_KEY")!;
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const MODEL = "openai/gpt-4.1-nano";

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

const CHANNEL_SUMMARY_PROMPT = `You are writing an editorial summary of a YouTube channel's clickbait patterns based on its evaluated videos.

You have the channel name, overall channel score, and each video's title, score, duration, content summary, and verdict.

CHANNEL: {channel_name}
CHANNEL SCORE: {channel_score}/100
VIDEOS EVALUATED: {video_count}

VIDEO RESULTS (sorted by score, highest first):
{video_results}

Each video entry is formatted as:
- "{title}" - {score}/100 ({duration}s)
  Content: {content_summary}
  Verdict: {verdict}

Write a 3-5 sentence channel summary as a single paragraph. Rules:

- Identify the channel's clickbait PATTERN, not just an average. Does it clickbait some types of videos and not others? Is there a consistent trick (exaggerated claims, bait-and-switch topics, sensational framing)?
- If there's a clear split (e.g., short videos honest, long videos clickbaity), call it out specifically with examples.
- Name specific video titles when illustrating a pattern. Don't just say "some videos" - say which ones.
- Use content summaries to identify topic-level patterns (e.g., "clickbait clusters around X topic").
- Do NOT restate the channel score or list individual video scores. The reader already sees them.
- Do NOT give advice to the creator. This is for the viewer, not the channel.
- Tone: direct, objective, editorial. Like a critic's capsule review of the channel.

Return JSON only:
{"channel_summary": "..."}`;

interface VideoResult {
  video_id: string;
  title: string;
  video_score: number;
  duration_seconds: number;
  content_summary: string;
  verdict: string;
}

interface ChannelInput {
  channel_id: string;
  channel_name: string;
  thumbnail_url?: string;
  subscriber_count?: number;
  category_name?: string;
  country?: string;
  video_results: VideoResult[];
}

function buildPrompt(
  template: string,
  vars: Record<string, string | number>
): string {
  let result = template;
  for (const [key, value] of Object.entries(vars)) {
    result = result.replaceAll(`{${key}}`, String(value));
  }
  return result;
}

function parseJsonResponse(text: string): Record<string, unknown> {
  let cleaned = text.trim();
  if (cleaned.startsWith("```json")) cleaned = cleaned.slice(7);
  else if (cleaned.startsWith("```")) cleaned = cleaned.slice(3);
  if (cleaned.endsWith("```")) cleaned = cleaned.slice(0, -3);
  return JSON.parse(cleaned.trim());
}

async function callLLM(prompt: string): Promise<Record<string, unknown>> {
  const resp = await fetch(
    "https://openrouter.ai/api/v1/chat/completions",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${OPENROUTER_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: MODEL,
        messages: [{ role: "user", content: prompt }],
        temperature: 0.0,
        max_tokens: 500,
      }),
    }
  );

  if (!resp.ok) {
    throw new Error(`LLM HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
  }

  const data = await resp.json();
  const content = data?.choices?.[0]?.message?.content;
  if (!content) throw new Error("Empty LLM response");
  return parseJsonResponse(content);
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "POST only" }), {
      status: 405,
      headers: { "Content-Type": "application/json" },
    });
  }

  let input: ChannelInput;
  try {
    input = await req.json();
    if (!input.channel_id || !input.channel_name || !input.video_results?.length) {
      throw new Error("missing required fields");
    }
  } catch {
    return new Response(
      JSON.stringify({ error: "Invalid body" }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    );
  }

  try {
    const videos = input.video_results
      .filter((v) => v.video_score != null)
      .sort((a, b) => b.video_score - a.video_score);

    if (videos.length === 0) {
      throw new Error("No scored videos");
    }

    const channelScore = Math.round(
      videos.reduce((sum, v) => sum + v.video_score, 0) / videos.length * 100
    ) / 100;

    // Format video results for prompt
    const videoResultsText = videos
      .map((v) =>
        `- "${v.title}" - ${Math.round(v.video_score)}/100 (${v.duration_seconds}s)\n  Content: ${v.content_summary || "N/A"}\n  Verdict: ${v.verdict || "N/A"}`
      )
      .join("\n\n");

    const prompt = buildPrompt(CHANNEL_SUMMARY_PROMPT, {
      channel_name: input.channel_name,
      channel_score: Math.round(channelScore),
      video_count: videos.length,
      video_results: videoResultsText,
    });

    const result = await callLLM(prompt);
    const channelSummary = String(result.channel_summary || "");

    // Upsert into channels table
    const { error: upsertError } = await supabase
      .from("channels")
      .upsert(
        {
          channel_id: input.channel_id,
          title: input.channel_name,
          thumbnail_url: input.thumbnail_url || null,
          subscriber_count: input.subscriber_count || null,
          country: input.country || null,
          channel_score: channelScore,
          videos_evaluated: videos.length,
          channel_summary: channelSummary,
          last_evaluated_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
        { onConflict: "channel_id" }
      );

    if (upsertError) {
      throw new Error(`DB upsert failed: ${upsertError.message}`);
    }

    return new Response(
      JSON.stringify({
        status: "complete",
        channel_id: input.channel_id,
        channel_score: channelScore,
        channel_summary: channelSummary,
        videos_evaluated: videos.length,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (e) {
    return new Response(
      JSON.stringify({ status: "error", error: String(e).slice(0, 500) }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }
});
