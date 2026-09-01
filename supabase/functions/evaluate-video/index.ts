// Supabase Edge Function: evaluate a YouTube video for clickbait
// Input: { video_id }
// - Reads transcript + title + duration from eval_cache (stored by Vercel coordinator)
// - Runs 2 parallel LLM calls (OpenRouter gpt-4.1-nano)
// - Computes v1.1 score
// - Writes result to eval_cache
// - Deducts 1 credit on success

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const OPENROUTER_API_KEY = Deno.env.get("OPENROUTER_API_KEY")!;
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const MODEL = "openai/gpt-4.1-nano";
const MAX_RETRIES = 2;

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// ─── LLM prompts (same as src/text_evaluator.py) ────────────────

const TITLE_ANALYSIS_PROMPT = `You are part of a system that holds clickbait creators accountable by scoring how honestly a video's title represents its content.

We use the video transcript as a proxy for video content. The transcript cannot capture visuals, reactions, or body language. Score based on whether the underlying topic and substance are covered - not whether specific title words appear verbatim. Judge thematic relevance: content that discusses, responds to, or is driven by the titled subject counts as on-topic.

TITLE: {title}
TRANSCRIPT LANGUAGE: {language}

TRANSCRIPT:
{transcript}

First, read the title as a typical viewer would and state what it promises in plain language. Use the most natural, common-sense reading - do not invent unlikely interpretations. If the title contains multiple topics or claims, list each one.

CONTENT_SUMMARY - one factual sentence describing what the video actually covers.
  Not what the title claims. What a viewer would walk away having learned or watched.
  Be specific: name the tools, topics, people, or events discussed.
  Keep it under 30 words.

Then score each metric below. For each metric, first write your reasoning, then give a score (integer 0-10).

Return JSON only:
{"title_promises":"plain language summary of what the title promises","content_summary":"...","title_content_similarity_reasoning":"...","title_content_similarity_score":0-10,"deception_score_reasoning":"...","deception_score":0-10}

If the title contains a quote (text in "" or ''), treat it as a highlighted excerpt from the video. A quoted title is slightly more acceptable than an unquoted claim - it signals "someone said this" rather than "this is the whole video." However, if the quoted text is barely relevant to the actual content or is taken out of context to mislead, penalize normally.

If the title names specific people (hosts, guests, creators), those people ARE the speakers in the transcript. Their names will rarely appear verbatim because people don't say their own names in conversation. Judge whether the speakers discuss the topics the title promises, not whether names are spoken.

TITLE_CONTENT_SIMILARITY - does the video deliver what the title promises?
  0-1: Complete bait-and-switch. Title topic is never addressed.
  2-3: Title topic barely mentioned, video is mostly about something else.
  4-5: Title topic covered but superficially, or only ~half of what's promised.
  6-7: Title topic covered substantially but with noticeable exaggeration or missing key parts.
  8-9: Title accurately reflects content with minor embellishment.
  10: Title is a precise, literal description of the video content.

DECEPTION_SCORE - does the title make factual claims the video contradicts or never addresses?
  0: No false claims. Title is honest.
  1-3: Minor misleading framing - technically true but implies more than delivered.
  4-6: Title makes a specific claim that is only partially true or significantly stretched.
  7-9: Title makes concrete claims that the video mostly contradicts.
  10: Title states something the video explicitly disproves or never addresses at all.`;

const CONTENT_ANALYSIS_PROMPT = `You are part of a system that holds clickbait creators accountable by scoring how honestly a video's title represents its content.

We use the video transcript as a proxy for video content. The transcript cannot capture visuals, reactions, or body language. Score based on whether the underlying topic and substance are covered - not whether specific title words appear verbatim. Judge thematic relevance: content that discusses, responds to, or is driven by the titled subject counts as on-topic.

The goal of this analysis: determine whether the titled topic is truly the main intent of the entire video, not just a segment mentioned in passing. A video that spends 3 minutes on the titled topic and 12 minutes on something else is not delivering on its title, even if it technically "covers" it.

TITLE: {title}
DURATION: {duration_seconds}s
TRANSCRIPT LANGUAGE: {language}

TRANSCRIPT (with timestamps):
{transcript}

For each metric, first write your reasoning (use timestamps from the transcript to support your analysis), then give a score (integer 0-10).

Return JSON only:
{"focus_ratio_reasoning":"...","focus_ratio_score":0-10,"time_to_main_content_reasoning":"...","time_to_main_content_score":0-10,"sponsor_interruption_reasoning":"...","sponsor_interruption_score":0-10}

If the title contains a quote (text in "" or ''), the entire conversation or interview context surrounding that quote counts as on-topic - not just the exact moment the quote was said. However, if the quoted text has no real connection to the video's actual subject matter, treat it as off-topic normally.

If the title names specific people (hosts, guests, creators), those people ARE the speakers in the transcript. Their names will rarely appear verbatim because people don't say their own names in conversation. Content where those speakers discuss the titled topic counts as on-topic.

FOCUS_RATIO - what fraction of the video's runtime is spent on the titled topic?
  Match content against the IDEAS and themes the title promises, not the specific words. Content is on-topic if it addresses the subject matter the title refers to - even if the exact title words are never spoken. Causes, context, implications, analysis, and discussion of a titled subject all count as on-topic.
  When the title contains multiple topics (separated by colons, commas, or conjunctions), content relevant to ANY part of the title is on-topic.
  Be stringent about literal coverage only when the title asks a specific narrow question or makes a concrete factual claim - the video should actually address that question or substantiate that claim.
  To measure: identify segments that are CLEARLY UNRELATED to any aspect of the title. Use timestamps to estimate total off-topic seconds. Everything else counts as on-topic.
  Be strict about what counts as off-topic: examples, supporting arguments, related analysis, Q&A, context-setting, and implications ALL count as on-topic. A segment is off-topic ONLY if it discusses a completely different subject with no relevance to any part of the title.
  Convert on-topic percentage to a score:
  0-1: <10% of runtime on topic.
  2-3: 10-30% of runtime on topic.
  4-5: 30-55% of runtime on topic.
  6-7: 55-75% of runtime on topic.
  8-9: 75-95% of runtime on topic.
  10: 95-100% of runtime on topic.

TIME_TO_MAIN_CONTENT - how far into the video before the titled topic begins?
  Use the [X.Xs] timestamps. Score based on when sustained discussion of the titled topic starts (not a brief mention or tease).
  10: Starts immediately or within first 2% of video duration.
  8-9: Starts within first 5-10% (brief intro/hook).
  6-7: Starts within first 10-20% (extended intro, context setting).
  4-5: Starts within first 20-35% (long preamble).
  2-3: Starts after 35-50% of video.
  0-1: Main topic starts after 50%+ or never truly begins.

SPONSOR_INTERRUPTION_SCORE - what percentage of total video duration is sponsor/ad content?
  IMPORTANT: This metric uses an INVERTED scale. 0 = NO sponsors (best). 10 = heavy sponsors (worst). If you find NO sponsor segments, the score MUST be 0.
  Use timestamps to estimate total seconds of sponsor reads, ad segments, paid promotions, and "this video is brought to you by" segments. Then calculate as a percentage of total video duration ({duration_seconds}s).
  0: No sponsor segments at all.
  1-2: Sponsor only at the very start or end, under 15 seconds total.
  3-4: Sponsor content is 1-5% of total video duration.
  5-6: Sponsor content is 5-10% of total video duration.
  7-8: Sponsor content is 10-20% of total video duration.
  9-10: Sponsor content exceeds 20% of total video duration.`;

const VIDEO_VERDICT_PROMPT = `You are writing an editorial verdict for a YouTube video's clickbait evaluation.

You have the video title, a factual summary of what the video covers, its final score, and 5 metric scores with the LLM reasoning behind each. Your job is to synthesize all of this into a specific, opinionated assessment of this video's title honesty.

TITLE: {title}
CONTENT: {content_summary}
FINAL SCORE: {final_score}/100
DURATION: {duration_seconds}s

METRICS AND REASONING:

Title-Content Similarity: {tcs_score}/10
{tcs_reasoning}

Deception: {deception_score}/10
{deception_reasoning}

Focus Ratio: {focus_ratio_score}/10
{focus_reasoning}

Time to Main Content: {ttmc_score}/10
{time_reasoning}

Sponsor Interruption: {sponsor_score}/10
{sponsor_reasoning}

Write a 3-4 sentence verdict as a single paragraph. Structure:

- Lead with the clickbait assessment: the biggest gap between what the title promises and what the viewer gets. If there is no gap, say what makes the title honest and effective.
- Then describe what the video actually covers, so the reader knows what they'd get if they clicked.

Rules:

- Be specific to THIS video. Reference the actual title, topic, or content. Never write something generic that could apply to any video.
- Write in third person ("the title...", "the video actually..."), as an objective editorial assessment.
- Do NOT restate scores or metric names. The reader already sees the numbers.
- Tone: direct, informal, slightly opinionated. Like a review card, not a report.

Return JSON only:
{"verdict": "..."}`;

// ─── Helpers ─────────────────────────────────────────────────────

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

async function callLLM(
  prompt: string,
  retries = MAX_RETRIES
): Promise<Record<string, unknown>> {
  let lastError = "";

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
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
            max_tokens: 2000,
          }),
        }
      );

      if (!resp.ok) {
        const errText = await resp.text();
        lastError = `HTTP ${resp.status}: ${errText.slice(0, 200)}`;
        if (resp.status === 429 || resp.status >= 500) {
          await sleep(2000 * Math.pow(2, attempt));
          continue;
        }
        throw new Error(lastError);
      }

      const data = await resp.json();
      const content = data?.choices?.[0]?.message?.content;
      if (!content) throw new Error("Empty LLM response");
      return parseJsonResponse(content);
    } catch (e) {
      lastError = String(e).slice(0, 200);
      if (attempt < retries) {
        await sleep(2000 * Math.pow(2, attempt));
        continue;
      }
    }
  }

  throw new Error(
    `LLM call failed after ${retries + 1} attempts: ${lastError}`
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function computeScore(
  tcs: number,
  fr: number,
  ttmc: number,
  deception: number,
  sponsor: number
): number {
  tcs = Math.max(0, Math.min(10, tcs));
  fr = Math.max(0, Math.min(10, fr));
  ttmc = Math.max(0, Math.min(10, ttmc));
  deception = Math.max(0, Math.min(10, deception));
  sponsor = Math.max(0, Math.min(10, sponsor));

  const base = (0.5 * tcs + 0.3 * fr + 0.2 * ttmc) * 10;
  const deceptionPenalty = -(deception / 10) * 20;
  const sponsorPenalty = -(sponsor / 10) * 10;

  return Math.max(0, Math.min(100, base + deceptionPenalty + sponsorPenalty));
}

// ─── Main handler ────────────────────────────────────────────────

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "POST only" }), {
      status: 405,
      headers: { "Content-Type": "application/json" },
    });
  }

  let videoId = "";

  try {
    const body = await req.json();
    videoId = body.video_id || "";
    if (!videoId) {
      return new Response(
        JSON.stringify({ error: "missing video_id" }),
        { status: 400, headers: { "Content-Type": "application/json" } }
      );
    }
  } catch {
    return new Response(
      JSON.stringify({ error: "Invalid body. Need { video_id }" }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    );
  }

  try {
    // Read transcript + metadata from eval_cache
    const { data: rows, error: readError } = await supabase
      .from("eval_cache")
      .select("video_id, title, duration_seconds, transcript_text, requested_by")
      .eq("video_id", videoId)
      .limit(1);

    if (readError) {
      throw new Error(`DB read failed: ${readError.message}`);
    }

    const row = rows && rows.length > 0 ? rows[0] : null;

    if (!row) {
      throw new Error(`eval_cache row not found for ${videoId}`);
    }

    if (!row.transcript_text || row.transcript_text.trim().length === 0) {
      throw new Error("No transcript in eval_cache");
    }

    const title = row.title || videoId;
    const durationSeconds = row.duration_seconds || 0;
    let transcript = row.transcript_text;
    const requestedBy = row.requested_by;

    // Extract language marker from first line (set by evaluate.py)
    let detectedLanguage = "en";
    const langMatch = transcript.match(/^_language:(\S+)\n/);
    if (langMatch) {
      detectedLanguage = langMatch[1];
      transcript = transcript.replace(/^_language:\S+\n/, "");
    }

    // Run both LLM calls in parallel
    const titlePrompt = buildPrompt(TITLE_ANALYSIS_PROMPT, {
      title,
      language: detectedLanguage,
      transcript,
    });

    const contentPrompt = buildPrompt(CONTENT_ANALYSIS_PROMPT, {
      title,
      duration_seconds: durationSeconds,
      language: detectedLanguage,
      transcript,
    });

    let titleResult = await callLLM(titlePrompt);
    let contentResult = await callLLM(contentPrompt);

    // Extract metrics, retry specific call if key metrics missing
    let tcs = Number(titleResult.title_content_similarity_score ?? -1);
    let deception = Number(titleResult.deception_score ?? -1);
    let fr = Number(contentResult.focus_ratio_score ?? -1);
    let ttmc = Number(contentResult.time_to_main_content_score ?? -1);
    let sponsor = Number(contentResult.sponsor_interruption_score ?? -1);

    // Retry title call if tcs missing
    if (tcs < 0) {
      await sleep(2000);
      titleResult = await callLLM(titlePrompt);
      tcs = Number(titleResult.title_content_similarity_score ?? -1);
      deception = Number(titleResult.deception_score ?? -1);
    }

    // Retry content call if fr or ttmc missing
    if (fr < 0 || ttmc < 0) {
      await sleep(2000);
      contentResult = await callLLM(contentPrompt);
      fr = Number(contentResult.focus_ratio_score ?? -1);
      ttmc = Number(contentResult.time_to_main_content_score ?? -1);
      sponsor = Number(contentResult.sponsor_interruption_score ?? -1);
    }

    if (tcs < 0 || fr < 0 || ttmc < 0) {
      throw new Error(
        `Missing base metrics after retry: tcs=${tcs} fr=${fr} ttmc=${ttmc}`
      );
    }

    // Post-scoring validation: sponsor score sanity check
    if (sponsor > 2) {
      const sponsorReasoning = String(contentResult.sponsor_interruption_reasoning || "").toLowerCase();
      const noSponsorPatterns = ["no sponsor", "no ad ", "no ads", "no paid promotion", "no evidence of sponsor", "no promotional", "does not contain any sponsor", "no sponsored", "no brand", "zero sponsor", "0 seconds of sponsor"];
      const reasoningSaysNoSponsor = noSponsorPatterns.some(p => sponsorReasoning.includes(p));
      if (reasoningSaysNoSponsor) {
        console.log(`[${videoId}] Sponsor score correction: ${sponsor} -> 0 (reasoning: "${sponsorReasoning.slice(0, 100)}")`);
        sponsor = 0;
      }
    }

    const videoScore = computeScore(
      tcs,
      fr,
      ttmc,
      deception < 0 ? 0 : deception,
      sponsor < 0 ? 0 : sponsor
    );

    // Extract content_summary from Call 1
    const contentSummary = String(titleResult.content_summary || "");

    // Verdict call (new - post-scoring)
    let verdict = "";
    try {
      const verdictPrompt = buildPrompt(VIDEO_VERDICT_PROMPT, {
        title,
        content_summary: contentSummary,
        final_score: Math.round(videoScore),
        duration_seconds: durationSeconds,
        tcs_score: tcs,
        tcs_reasoning: String(titleResult.title_content_similarity_reasoning || ""),
        deception_score: deception < 0 ? 0 : deception,
        deception_reasoning: String(titleResult.deception_score_reasoning || ""),
        focus_ratio_score: fr,
        focus_reasoning: String(contentResult.focus_ratio_reasoning || ""),
        ttmc_score: ttmc,
        time_reasoning: String(contentResult.time_to_main_content_reasoning || ""),
        sponsor_score: sponsor < 0 ? 0 : sponsor,
        sponsor_reasoning: String(contentResult.sponsor_interruption_reasoning || ""),
      });
      const verdictResult = await callLLM(verdictPrompt);
      verdict = String(verdictResult.verdict || "");
    } catch (verdictErr) {
      console.error(`[${videoId}] Verdict LLM failed after retries: ${String(verdictErr).slice(0, 200)}`);
    }

    // Write result to eval_cache
    const { error: updateError } = await supabase
      .from("eval_cache")
      .update({
        status: "complete",
        video_score: Math.round(videoScore * 100) / 100,
        title_content_similarity_score: tcs,
        deception_score: deception < 0 ? 0 : deception,
        focus_ratio: fr,
        time_to_main_content: ttmc,
        sponsor_interruption_score: sponsor < 0 ? 0 : sponsor,
        title_analysis_reasoning: String(
          titleResult.title_content_similarity_reasoning || ""
        ),
        deception_reasoning: String(
          titleResult.deception_score_reasoning || ""
        ),
        focus_reasoning: String(contentResult.focus_ratio_reasoning || ""),
        time_reasoning: String(
          contentResult.time_to_main_content_reasoning || ""
        ),
        sponsor_reasoning: String(
          contentResult.sponsor_interruption_reasoning || ""
        ),
        content_summary: contentSummary,
        verdict,
        completed_at: new Date().toISOString(),
      })
      .eq("video_id", videoId);

    if (updateError) {
      throw new Error(`DB update failed: ${updateError.message}`);
    }

    // Deduct credit
    if (requestedBy) {
      await supabase.rpc("use_credits", {
        p_user_id: requestedBy,
        p_count: 1,
      });
    }

    return new Response(
      JSON.stringify({
        status: "complete",
        video_id: videoId,
        video_score: Math.round(videoScore * 100) / 100,
        title_content_similarity_score: tcs,
        deception_score: deception < 0 ? 0 : deception,
        focus_ratio: fr,
        time_to_main_content: ttmc,
        sponsor_interruption_score: sponsor < 0 ? 0 : sponsor,
        title_analysis_reasoning: String(titleResult.title_content_similarity_reasoning || ""),
        deception_reasoning: String(titleResult.deception_score_reasoning || ""),
        focus_reasoning: String(contentResult.focus_ratio_reasoning || ""),
        time_reasoning: String(contentResult.time_to_main_content_reasoning || ""),
        sponsor_reasoning: String(contentResult.sponsor_interruption_reasoning || ""),
        content_summary: contentSummary,
        verdict,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  } catch (e) {
    const errorMsg = String(e).slice(0, 500);

    try {
      await supabase
        .from("eval_cache")
        .update({
          status: "error",
          evaluation_error: errorMsg,
        })
        .eq("video_id", videoId);
    } catch {
      // Best effort - ignore DB write failure in error handler
    }

    return new Response(
      JSON.stringify({ status: "error", error: errorMsg }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }
});
