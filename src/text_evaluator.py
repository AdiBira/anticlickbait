"""
Text evaluator for YouTube video clickbait analysis (v1.1).
Uses 2 parallel LLM calls for better accuracy and lower hallucination.

Call 1 (Title Analysis): title_content_similarity, deception_score
Call 2 (Content Analysis): focus_ratio, time_to_main_content, sponsor_interruption_score

All sub-metrics are 0-10. Chain-of-thought reasoning per metric.

Scoring v1.1 formula:
  base = (0.50 * title_content_similarity + 0.30 * focus_ratio + 0.20 * time_to_main_content) * 10
  deception_penalty = -(deception_score / 10) * 20
  sponsor_penalty = -(sponsor_interruption_score / 10) * 10
  final = clamp(base + deception_penalty + sponsor_penalty, 0, 100)
"""

import json
import os
import time
import concurrent.futures
import re
from openai import OpenAI, APIError, RateLimitError, APIConnectionError

from config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    OPENAI_MAX_RETRIES,
    OPENAI_RETRY_DELAY_SECONDS,
)


TITLE_ANALYSIS_PROMPT = """You are part of a system that holds clickbait creators accountable by scoring \
how honestly a video's title represents its content.

We use the video transcript as a proxy for video content. The transcript \
cannot capture visuals, reactions, or body language. Score based on whether \
the underlying topic and substance are covered — not whether specific title \
words appear verbatim. Judge thematic relevance: content that discusses, \
responds to, or is driven by the titled subject counts as on-topic.

TITLE: {title}

TRANSCRIPT:
{transcript}

First, read the title as a typical viewer would and state what it promises \
in plain language. Use the most natural, common-sense reading — do not invent \
unlikely interpretations. If the title contains multiple topics or claims, \
list each one. Then score each metric below.

For each metric, first write your reasoning, then give a score (integer 0-10).

Return JSON only:
{{"title_promises":"plain language summary of what the title promises","title_content_similarity_reasoning":"...","title_content_similarity_score":0-10,"deception_score_reasoning":"...","deception_score":0-10}}

If the title contains a quote (text in "" or ''), treat it as a highlighted \
excerpt from the video. A quoted title is slightly more acceptable than an \
unquoted claim — it signals "someone said this" rather than "this is the \
whole video." However, if the quoted text is barely relevant to the actual \
content or is taken out of context to mislead, penalize normally.

If the title names specific people (hosts, guests, creators), those people \
ARE the speakers in the transcript. Their names will rarely appear verbatim \
because people don't say their own names in conversation. Judge whether the \
speakers discuss the topics the title promises, not whether names are spoken.

TITLE_CONTENT_SIMILARITY — does the video deliver what the title promises?
  0-1: Complete bait-and-switch. Title topic is never addressed.
  2-3: Title topic barely mentioned, video is mostly about something else.
  4-5: Title topic covered but superficially, or only ~half of what's promised.
  6-7: Title topic covered substantially but with noticeable exaggeration or missing key parts.
  8-9: Title accurately reflects content with minor embellishment.
  10: Title is a precise, literal description of the video content.

DECEPTION_SCORE — does the title make factual claims the video contradicts or never addresses?
  0: No false claims. Title is honest.
  1-3: Minor misleading framing — technically true but implies more than delivered.
  4-6: Title makes a specific claim that is only partially true or significantly stretched.
  7-9: Title makes concrete claims that the video mostly contradicts.
  10: Title states something the video explicitly disproves or never addresses at all."""


CONTENT_ANALYSIS_PROMPT = """You are part of a system that holds clickbait creators accountable by scoring \
how honestly a video's title represents its content.

We use the video transcript as a proxy for video content. The transcript \
cannot capture visuals, reactions, or body language. Score based on whether \
the underlying topic and substance are covered — not whether specific title \
words appear verbatim. Judge thematic relevance: content that discusses, \
responds to, or is driven by the titled subject counts as on-topic.

The goal of this analysis: determine whether the titled topic is truly the \
main intent of the entire video, not just a segment mentioned in passing. \
A video that spends 3 minutes on the titled topic and 12 minutes on something \
else is not delivering on its title, even if it technically "covers" it.

TITLE: {title}
DURATION: {duration_seconds}s

TRANSCRIPT (with timestamps):
{transcript}

For each metric, first write your reasoning (use timestamps from the \
transcript to support your analysis), then give a score (integer 0-10).

Return JSON only:
{{"focus_ratio_reasoning":"...","focus_ratio_score":0-10,"time_to_main_content_reasoning":"...","time_to_main_content_score":0-10,"sponsor_interruption_reasoning":"...","sponsor_interruption_score":0-10}}

If the title contains a quote (text in "" or ''), the entire conversation or
interview context surrounding that quote counts as on-topic — not just the
exact moment the quote was said. However, if the quoted text has no real
connection to the video's actual subject matter, treat it as off-topic normally.

If the title names specific people (hosts, guests, creators), those people
ARE the speakers in the transcript. Their names will rarely appear verbatim
because people don't say their own names in conversation. Content where those
speakers discuss the titled topic counts as on-topic.

FOCUS_RATIO — what fraction of the video's runtime is spent on the titled topic?
  Match content against the IDEAS and themes the title promises, not the \
  specific words. Content is on-topic if it addresses the subject matter the \
  title refers to — even if the exact title words are never spoken. Causes, \
  context, implications, analysis, and discussion of a titled subject all \
  count as on-topic.
  When the title contains multiple topics (separated by colons, commas, or \
  conjunctions), content relevant to ANY part of the title is on-topic.
  Be stringent about literal coverage only when the title asks a specific \
  narrow question or makes a concrete factual claim — the video should \
  actually address that question or substantiate that claim.
  To measure: identify segments that are CLEARLY UNRELATED to any aspect \
  of the title. Use timestamps to estimate total off-topic seconds. Everything \
  else counts as on-topic.
  Be strict about what counts as off-topic: examples, supporting arguments, \
  related analysis, Q&A, context-setting, and implications ALL count as \
  on-topic. A segment is off-topic ONLY if it discusses a completely \
  different subject with no relevance to any part of the title.
  Convert on-topic percentage to a score:
  0-1: <10% of runtime on topic.
  2-3: 10-30% of runtime on topic.
  4-5: 30-55% of runtime on topic.
  6-7: 55-75% of runtime on topic.
  8-9: 75-95% of runtime on topic.
  10: 95-100% of runtime on topic.

TIME_TO_MAIN_CONTENT — how far into the video before the titled topic begins?
  Use the [X.Xs] timestamps. Score based on when sustained discussion of the
  titled topic starts (not a brief mention or tease).
  10: Starts immediately or within first 2% of video duration.
  8-9: Starts within first 5-10% (brief intro/hook).
  6-7: Starts within first 10-20% (extended intro, context setting).
  4-5: Starts within first 20-35% (long preamble).
  2-3: Starts after 35-50% of video.
  0-1: Main topic starts after 50%+ or never truly begins.

SPONSOR_INTERRUPTION_SCORE — how much sponsor/ad content interrupts the video?
  THIS IS A PENALTY METRIC: 0 = best (no sponsors), 10 = worst (heavy sponsors).
  Unlike the other metrics above, a LOWER score is better here.
  Use timestamps to estimate total seconds of sponsor reads, ad segments, paid
  promotions, and "this video is brought to you by" segments. Then calculate
  as a percentage of total video duration ({duration_seconds}s).
  0: No sponsor segments at all. (BEST — no penalty)
  1-2: Sponsor only at the very start or end, under 15 seconds total.
  3-4: Sponsor content is 1-5% of total video duration.
  5-6: Sponsor content is 5-10% of total video duration.
  7-8: Sponsor content is 10-20% of total video duration.
  9-10: Sponsor content exceeds 20% of total video duration. (WORST — max penalty)"""


LONG_TRANSCRIPT_ROUTE_TOKENS_EST = 110_000
CHARS_PER_TOKEN_EST = 4
CHUNK_TARGET_PROMPT_TOKENS_EST = 45_000
TOKEN_ESTIMATE_SAFETY_MULTIPLIER = 1.35


CHUNK_CONTENT_ANALYSIS_PROMPT = """Analyze this transcript CHUNK from a YouTube video.

TITLE: {title}
DURATION: {duration_seconds}s
CHUNK RANGE: {chunk_start_seconds}s to {chunk_end_seconds}s

TRANSCRIPT CHUNK (with timestamps):
{transcript}

Return JSON only:
{{"focus_ratio_pct":0-100,"main_topic_starts_in_chunk":boolean,"main_topic_start_seconds":number|null,"midroll_sponsor_seconds":number,"midroll_sponsor_interruptions":integer,"summary":"1-2 sentences"}}

- focus_ratio_pct: % of this chunk spent on the titled topic (including necessary context). Exclude filler/off-topic tangents.
- main_topic_starts_in_chunk: true only if sustained discussion of the titled topic begins inside this chunk.
- main_topic_start_seconds: absolute timestamp in seconds if main_topic_starts_in_chunk is true, else null.
- midroll_sponsor_seconds: estimated sponsor/ad content duration inside this chunk that interrupts the main content (exclude obvious intro/outro promos).
- midroll_sponsor_interruptions: count of mid-content sponsor interruptions in this chunk.
- summary: factual chunk summary for later title-vs-content aggregation. Preserve key claims/events, be concise."""


CHUNKED_TITLE_AGGREGATION_PROMPT = """Analyze the YouTube TITLE for clickbait using FULL-VIDEO coverage represented as chunk summaries.

TITLE: {title}
DURATION: {duration_seconds}s

CHUNK SUMMARIES (cover the full transcript in order):
{chunk_summaries}

Return JSON only:
{{"title_content_similarity":0-10,"title_sensationalism":0-10,"sensationalism_mismatch":boolean,"deception_flag":boolean,"reasoning":"1-2 sentences"}}

- title_content_similarity: Based on all chunk summaries together, does the video deliver what the title promises? 0-3=bait-switch, 4-7=partial, 8-10=honest.
- title_sensationalism: Judge the title text alone (0=completely factual, 10=extreme hype).
- sensationalism_mismatch: true if title tone is much more dramatic than what chunk summaries describe.
- deception_flag: true ONLY if the title makes a concrete claim contradicted by the chunk summaries.
Be conservative. If uncertain, avoid setting deception_flag true."""


def _format_transcript(segments: list[dict], use_integer_timestamps: bool = False) -> str:
    """Format transcript with timestamps."""
    lines = []
    for seg in segments:
        start = seg.get("start", 0)
        text = seg.get("text", "").strip()
        if text:
            ts = int(round(start)) if use_integer_timestamps else float(start)
            ts_text = f"{ts}s" if use_integer_timestamps else f"{ts:.1f}s"
            lines.append(f"[{ts_text}] {text}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())


TITLE_ANALYSIS_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "title_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title_promises": {"type": "string"},
                "title_content_similarity_reasoning": {"type": "string"},
                "title_content_similarity_score": {"type": "integer"},
                "deception_score_reasoning": {"type": "string"},
                "deception_score": {"type": "integer"},
            },
            "required": ["title_promises", "title_content_similarity_reasoning", "title_content_similarity_score", "deception_score_reasoning", "deception_score"],
            "additionalProperties": False,
        },
    },
}

CONTENT_ANALYSIS_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "content_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "focus_ratio_reasoning": {"type": "string"},
                "focus_ratio_score": {"type": "integer"},
                "time_to_main_content_reasoning": {"type": "string"},
                "time_to_main_content_score": {"type": "integer"},
                "sponsor_interruption_reasoning": {"type": "string"},
                "sponsor_interruption_score": {"type": "integer"},
            },
            "required": ["focus_ratio_reasoning", "focus_ratio_score", "time_to_main_content_reasoning", "time_to_main_content_score", "sponsor_interruption_reasoning", "sponsor_interruption_score"],
            "additionalProperties": False,
        },
    },
}


def _call_llm(prompt: str, max_retries: int, retry_delay: float, response_format: dict = None) -> dict:
    """Make LLM call with retries."""
    client_kwargs = {"api_key": OPENAI_API_KEY}
    if os.environ.get("OPENAI_API_BASE"):
        client_kwargs["base_url"] = os.environ["OPENAI_API_BASE"]
    client = OpenAI(**client_kwargs)
    last_error = None

    kwargs = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    }
    if response_format:
        kwargs["response_format"] = response_format

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(**kwargs)
            return _parse_json(response.choices[0].message.content)

        except (RateLimitError, APIConnectionError, APIError) as e:
            last_error = str(e)
            wait = retry_delay * (2 ** attempt)
            time.sleep(wait)

        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {e}"
            wait = retry_delay * (2 ** attempt)
            time.sleep(wait)

        except Exception as e:
            last_error = str(e)
            break

    raise Exception(f"LLM call failed after {max_retries} retries: {last_error}")


def _analyze_title(title: str, transcript: str, max_retries: int, retry_delay: float) -> dict:
    """Call 1: Title analysis."""
    prompt = TITLE_ANALYSIS_PROMPT.format(title=title, transcript=transcript)
    return _call_llm(prompt, max_retries, retry_delay, response_format=TITLE_ANALYSIS_SCHEMA)


def _analyze_content(title: str, duration: int, transcript: str, max_retries: int, retry_delay: float) -> dict:
    """Call 2: Content analysis."""
    prompt = CONTENT_ANALYSIS_PROMPT.format(title=title, duration_seconds=duration, transcript=transcript)
    return _call_llm(prompt, max_retries, retry_delay, response_format=CONTENT_ANALYSIS_SCHEMA)


def _estimate_tokens_from_chars(text: str) -> int:
    return max(1, int((len(text) / CHARS_PER_TOKEN_EST) * TOKEN_ESTIMATE_SAFETY_MULTIPLIER))


def _is_context_length_error(error_text: str | None) -> bool:
    if not error_text:
        return False
    t = str(error_text).lower()
    return "maximum context length" in t or "context_length_exceeded" in t


def _chunk_transcript_segments(segments: list[dict], max_chars: int) -> list[list[dict]]:
    """
    Split transcript segments into ordered chunks by formatted-char budget.

    Invariants:
    - preserves order
    - preserves every non-empty segment exactly once
    - does not alter transcript text/punctuation
    - uses integer timestamps only when formatting chunk prompts (not mutating segments)
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue

        start = int(round(seg.get("start", 0)))
        line = f"[{start}s] {text}"
        line_chars = len(line)
        if current:
            line_chars += 1  # newline join overhead

        # If adding this line exceeds the budget, flush current chunk first.
        if current and current_chars + line_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
            # recompute without leading newline for new chunk
            line_chars = len(f"[{start}s] {text}")

        current.append(seg)
        current_chars += line_chars

    if current:
        chunks.append(current)

    return chunks


def _analyze_content_chunk(
    title: str,
    duration_seconds: int,
    chunk_segments: list[dict],
    max_retries: int,
    retry_delay: float,
) -> dict:
    chunk_start = int(round(chunk_segments[0].get("start", 0))) if chunk_segments else 0
    chunk_end = int(round(chunk_segments[-1].get("start", 0))) if chunk_segments else chunk_start
    chunk_transcript = _format_transcript(chunk_segments, use_integer_timestamps=True)
    prompt = CHUNK_CONTENT_ANALYSIS_PROMPT.format(
        title=title,
        duration_seconds=duration_seconds,
        chunk_start_seconds=chunk_start,
        chunk_end_seconds=chunk_end,
        transcript=chunk_transcript,
    )
    result = _call_llm(prompt, max_retries, retry_delay)
    result["_chunk_start_seconds"] = chunk_start
    result["_chunk_end_seconds"] = chunk_end
    result["_segment_count"] = len(chunk_segments)
    return result


def _aggregate_chunk_content_metrics(
    chunk_results: list[dict],
    duration_seconds: int,
) -> tuple[dict, str]:
    """
    Deterministically aggregate chunk-level content metrics across the full transcript.
    """
    if not chunk_results:
        raise ValueError("No chunk results to aggregate")

    weighted_focus_sum = 0.0
    weighted_duration_sum = 0.0
    main_topic_candidates: list[float] = []
    sponsor_seconds_total = 0.0
    sponsor_interruptions_total = 0
    chunk_summaries: list[str] = []

    for c in chunk_results:
        start = float(c.get("_chunk_start_seconds") or 0)
        end = float(c.get("_chunk_end_seconds") or start)
        span = max(1.0, end - start)

        focus = c.get("focus_ratio_pct")
        if isinstance(focus, (int, float)):
            weighted_focus_sum += float(focus) * span
            weighted_duration_sum += span

        if c.get("main_topic_starts_in_chunk") and c.get("main_topic_start_seconds") is not None:
            try:
                main_topic_candidates.append(float(c.get("main_topic_start_seconds")))
            except (TypeError, ValueError):
                pass

        try:
            sponsor_seconds_total += max(0.0, float(c.get("midroll_sponsor_seconds") or 0))
        except (TypeError, ValueError):
            pass
        try:
            sponsor_interruptions_total += max(0, int(c.get("midroll_sponsor_interruptions") or 0))
        except (TypeError, ValueError):
            pass

        summary = (c.get("summary") or "").strip()
        if summary:
            chunk_summaries.append(summary)

    focus_ratio_pct = (weighted_focus_sum / weighted_duration_sum) if weighted_duration_sum > 0 else -1
    time_to_main_content_seconds = min(main_topic_candidates) if main_topic_candidates else -1

    if sponsor_seconds_total > 60 or sponsor_interruptions_total >= 2:
        sponsor_interruption = "excessive"
    elif sponsor_seconds_total >= 15 or sponsor_interruptions_total == 1:
        sponsor_interruption = "minor"
    else:
        sponsor_interruption = "none"

    content_result = {
        "focus_ratio_pct": focus_ratio_pct,
        "time_to_main_content_seconds": time_to_main_content_seconds,
        "sponsor_interruption": sponsor_interruption,
    }

    content_reasoning = (
        f"Chunked full-transcript analysis across {len(chunk_results)} chunks. "
        f"Weighted focus={focus_ratio_pct:.1f}%. "
        f"Earliest sustained main-topic start={time_to_main_content_seconds:.1f}s. "
        f"Estimated mid-roll sponsor interruptions={sponsor_interruptions_total}, "
        f"mid-roll sponsor seconds≈{sponsor_seconds_total:.1f}s ({sponsor_interruption})."
    )
    if chunk_summaries:
        content_reasoning += " " + " ".join(chunk_summaries[:3])

    return content_result, content_reasoning


def _analyze_title_from_chunk_summaries(
    title: str,
    duration_seconds: int,
    chunk_results: list[dict],
    max_retries: int,
    retry_delay: float,
) -> dict:
    lines = []
    for idx, c in enumerate(chunk_results, start=1):
        start = int(round(c.get("_chunk_start_seconds", 0)))
        end = int(round(c.get("_chunk_end_seconds", start)))
        focus = c.get("focus_ratio_pct")
        sponsor_s = c.get("midroll_sponsor_seconds")
        sponsor_n = c.get("midroll_sponsor_interruptions")
        summary = (c.get("summary") or "").strip()
        lines.append(
            f"Chunk {idx} [{start}s-{end}s] "
            f"(focus={focus}, sponsor_seconds={sponsor_s}, sponsor_interruptions={sponsor_n}): {summary}"
        )
    prompt = CHUNKED_TITLE_AGGREGATION_PROMPT.format(
        title=title,
        duration_seconds=duration_seconds,
        chunk_summaries="\n".join(lines),
    )
    return _call_llm(prompt, max_retries, retry_delay)


def _evaluate_video_chunked(
    video_id: str,
    channel_id: str,
    title: str,
    duration_seconds: int,
    transcript_segments: list[dict],
    max_retries: int,
    retry_delay: float,
) -> dict:
    """
    Full-transcript chunked path for long transcripts.

    Strategy:
    1) Split transcript into bounded chunks (preserving all segments verbatim).
    2) LLM analyzes content structure per chunk.
    3) Deterministically aggregate content metrics across all chunks.
    4) LLM computes title-vs-content metrics from chunk summaries covering the full video.
    """
    max_chunk_chars = CHUNK_TARGET_PROMPT_TOKENS_EST * CHARS_PER_TOKEN_EST
    chunks = _chunk_transcript_segments(transcript_segments, max_chars=max_chunk_chars)
    if not chunks:
        return _create_fallback(video_id, channel_id, title, duration_seconds, "Transcript empty after chunking")

    chunk_results = []
    for chunk in chunks:
        try:
            chunk_results.append(_analyze_content_chunk(title, duration_seconds, chunk, max_retries, retry_delay))
        except Exception as e:
            return _create_fallback(video_id, channel_id, title, duration_seconds, f"Chunk content analysis failed: {e}")

    try:
        content_result, content_reasoning = _aggregate_chunk_content_metrics(chunk_results, duration_seconds)
    except Exception as e:
        return _create_fallback(video_id, channel_id, title, duration_seconds, f"Chunk aggregation failed: {e}")

    try:
        title_result = _analyze_title_from_chunk_summaries(title, duration_seconds, chunk_results, max_retries, retry_delay)
    except Exception as e:
        return _create_fallback(video_id, channel_id, title, duration_seconds, f"Chunked title aggregation failed: {e}")

    title_content_similarity = title_result.get("title_content_similarity", -1)
    title_sensationalism = title_result.get("title_sensationalism", -1)
    sensationalism_mismatch = title_result.get("sensationalism_mismatch", False)
    deception_flag = title_result.get("deception_flag", False)
    title_reasoning = title_result.get("reasoning", "")

    focus_ratio = content_result.get("focus_ratio_pct", -1)
    time_to_main = content_result.get("time_to_main_content_seconds", -1)
    sponsor_interruption = content_result.get("sponsor_interruption", "none")
    time_to_main_fraction = time_to_main / duration_seconds if time_to_main >= 0 and duration_seconds > 0 else -1
    combined_reasoning = f"{title_reasoning} {content_reasoning}".strip()

    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": title,
        "duration_seconds": duration_seconds,
        "title_content_similarity_score": title_content_similarity,
        "title_sensationalism_score": title_sensationalism,
        "sensationalism_mismatch": sensationalism_mismatch,
        "deception_flag": deception_flag,
        "focus_ratio_pct": focus_ratio,
        "time_to_main_content_seconds": time_to_main,
        "time_to_main_content_fraction": time_to_main_fraction,
        "sponsor_interruption": sponsor_interruption,
        "title_analysis_reasoning": title_reasoning,
        "content_analysis_reasoning": content_reasoning,
        "llm_explanation": combined_reasoning,
        "evaluation_success": True,
        "error": None,
        "evaluation_route": "chunked_full_transcript",
        "chunk_count": len(chunks),
    }


def evaluate_video(
    video_id: str,
    channel_id: str,
    title: str,
    duration_seconds: int,
    transcript_segments: list[dict],
    max_retries: int = None,
    retry_delay: float = None,
) -> dict:
    """
    Evaluate video for clickbait using 2 parallel LLM calls.

    Returns dict with all metrics and combined reasoning.
    """
    if max_retries is None:
        max_retries = OPENAI_MAX_RETRIES
    if retry_delay is None:
        retry_delay = OPENAI_RETRY_DELAY_SECONDS

    if not OPENAI_API_KEY:
        return _create_fallback(video_id, channel_id, title, duration_seconds, "OPENAI_API_KEY not set")

    transcript = _format_transcript(transcript_segments)
    estimated_tokens = _estimate_tokens_from_chars(transcript)

    # Full-transcript chunked path for long videos that are likely to exceed model context.
    if estimated_tokens >= LONG_TRANSCRIPT_ROUTE_TOKENS_EST:
        return _evaluate_video_chunked(
            video_id=video_id,
            channel_id=channel_id,
            title=title,
            duration_seconds=duration_seconds,
            transcript_segments=transcript_segments,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )

    # Run both calls in parallel
    title_result = None
    content_result = None
    title_error = None
    content_error = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        title_future = executor.submit(_analyze_title, title, transcript, max_retries, retry_delay)
        content_future = executor.submit(_analyze_content, title, duration_seconds, transcript, max_retries, retry_delay)

        try:
            title_result = title_future.result()
        except Exception as e:
            title_error = str(e)

        try:
            content_result = content_future.result()
        except Exception as e:
            content_error = str(e)

    # Handle failures
    if title_error and content_error:
        if _is_context_length_error(title_error) or _is_context_length_error(content_error):
            return _evaluate_video_chunked(
                video_id=video_id,
                channel_id=channel_id,
                title=title,
                duration_seconds=duration_seconds,
                transcript_segments=transcript_segments,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
        return _create_fallback(video_id, channel_id, title, duration_seconds, f"Both calls failed: {title_error}")

    # Extract metrics with defaults (v1.1: all 0-10, no booleans)
    title_content_similarity = title_result.get("title_content_similarity_score", -1) if title_result else -1
    deception_score = title_result.get("deception_score", -1) if title_result else -1
    title_reasoning = title_result.get("title_content_similarity_reasoning", "") if title_result else f"Title analysis failed: {title_error}"
    deception_reasoning = title_result.get("deception_score_reasoning", "") if title_result else ""

    focus_ratio = content_result.get("focus_ratio_score", -1) if content_result else -1
    time_to_main_content = content_result.get("time_to_main_content_score", -1) if content_result else -1
    sponsor_interruption_score = content_result.get("sponsor_interruption_score", -1) if content_result else -1
    focus_reasoning = content_result.get("focus_ratio_reasoning", "") if content_result else f"Content analysis failed: {content_error}"
    time_reasoning = content_result.get("time_to_main_content_reasoning", "") if content_result else ""
    sponsor_reasoning = content_result.get("sponsor_interruption_reasoning", "") if content_result else ""

    # Combine reasoning
    combined_reasoning = f"{title_reasoning} {deception_reasoning} {focus_reasoning} {time_reasoning} {sponsor_reasoning}".strip()

    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": title,
        "duration_seconds": duration_seconds,
        "title_content_similarity_score": title_content_similarity,
        "deception_score": deception_score,
        "focus_ratio": focus_ratio,
        "time_to_main_content": time_to_main_content,
        "sponsor_interruption_score": sponsor_interruption_score,
        "title_analysis_reasoning": title_reasoning,
        "deception_reasoning": deception_reasoning,
        "focus_reasoning": focus_reasoning,
        "time_reasoning": time_reasoning,
        "sponsor_reasoning": sponsor_reasoning,
        "llm_explanation": combined_reasoning,
        "evaluation_success": title_result is not None or content_result is not None,
        "error": None,
        "evaluation_route": "two_call_parallel",
    }


def _create_fallback(video_id: str, channel_id: str, title: str, duration: int, error: str) -> dict:
    """Create fallback response on failure."""
    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": title,
        "duration_seconds": duration,
        "title_content_similarity_score": -1,
        "deception_score": -1,
        "focus_ratio": -1,
        "time_to_main_content": -1,
        "sponsor_interruption_score": -1,
        "title_analysis_reasoning": "",
        "deception_reasoning": "",
        "focus_reasoning": "",
        "time_reasoning": "",
        "sponsor_reasoning": "",
        "llm_explanation": f"Evaluation failed: {error}",
        "evaluation_success": False,
        "error": error,
    }


def compute_video_score(result: dict) -> float | None:
    """
    Compute final score from metrics (v1.1).

    All sub-metrics are 0-10.

    Formula:
        base = (0.50 * title_content_similarity + 0.30 * focus_ratio + 0.20 * time_to_main_content) * 10
        deception_penalty = -(deception_score / 10) * 20    (max -20)
        sponsor_penalty = -(sponsor_interruption_score / 10) * 10  (max -10)
        final = clamp(base + deception_penalty + sponsor_penalty, 0, 100)
    """
    if not result.get("evaluation_success"):
        return None

    tcs = result.get("title_content_similarity_score", -1)
    fr = result.get("focus_ratio", -1)
    ttmc = result.get("time_to_main_content", -1)
    deception = result.get("deception_score", 0)
    sponsor = result.get("sponsor_interruption_score", 0)

    # Skip if any base metric failed
    if tcs < 0 or fr < 0 or ttmc < 0:
        return None

    # Clamp sub-metrics to valid range
    tcs = max(0, min(10, tcs))
    fr = max(0, min(10, fr))
    ttmc = max(0, min(10, ttmc))
    deception = max(0, min(10, deception))
    sponsor = max(0, min(10, sponsor))

    base = (0.50 * tcs + 0.30 * fr + 0.20 * ttmc) * 10
    deception_penalty = -(deception / 10) * 20
    sponsor_penalty = -(sponsor / 10) * 10

    return max(0, min(100, base + deception_penalty + sponsor_penalty))


def compute_channel_score(results: list[dict]) -> float | None:
    """Compute average channel score from video results."""
    scores = [compute_video_score(r) for r in results]
    valid = [s for s in scores if s is not None]
    return sum(valid) / len(valid) if valid else None


SCORING_VERSION_V1 = "v1"


def _as_float_or_none(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value, digits: int = 2):
    return None if value is None else round(value, digits)


def _build_unevaluated_response_v1(
    video_id: str,
    channel_id: str | None,
    error_code: str,
    error_message: str,
    retryable: bool = True,
    transcript_result: dict | None = None,
) -> dict:
    transcript_meta = {
        "language_code": None,
        "is_generated": None,
        "segment_count": 0,
        "transcript_required": True,
    }

    if transcript_result:
        transcript_meta["language_code"] = transcript_result.get("language_code")
        transcript_meta["is_generated"] = transcript_result.get("is_generated")
        transcript_meta["segment_count"] = len(transcript_result.get("segments") or [])

    return {
        "status": "unevaluated",
        "scoring_version": SCORING_VERSION_V1,
        "video_id": video_id,
        "channel_id": channel_id,
        "score": None,
        "metrics": None,
        "penalties": None,
        "components": None,
        "rationale": None,
        "transcript_meta": transcript_meta,
        "errors": [
            {
                "code": error_code,
                "message": error_message,
                "retryable": retryable,
            }
        ],
    }


def to_analyze_video_response_v1(
    evaluation_result: dict,
    transcript_result: dict | None = None,
    combined_score: float | None = None,
) -> dict:
    """Map legacy text_evaluator output to the shared analyzeVideo v1 response shape."""
    video_id = evaluation_result.get("video_id")
    channel_id = evaluation_result.get("channel_id")

    if not evaluation_result.get("evaluation_success"):
        return _build_unevaluated_response_v1(
            video_id=video_id,
            channel_id=channel_id,
            error_code="EVALUATION_FAILED",
            error_message=evaluation_result.get("error") or "Evaluation failed",
            retryable=True,
            transcript_result=transcript_result,
        )

    text_score = compute_video_score(evaluation_result)
    if text_score is None:
        return _build_unevaluated_response_v1(
            video_id=video_id,
            channel_id=channel_id,
            error_code="METRIC_INCOMPLETE",
            error_message="Required scoring metrics missing or invalid",
            retryable=False,
            transcript_result=transcript_result,
        )

    tcs = _as_float_or_none(evaluation_result.get("title_content_similarity_score"))
    fr = _as_float_or_none(evaluation_result.get("focus_ratio"))
    ttmc = _as_float_or_none(evaluation_result.get("time_to_main_content"))
    deception = _as_float_or_none(evaluation_result.get("deception_score")) or 0
    sponsor = _as_float_or_none(evaluation_result.get("sponsor_interruption_score")) or 0

    if tcs is None or fr is None or ttmc is None or tcs < 0 or fr < 0 or ttmc < 0:
        return _build_unevaluated_response_v1(
            video_id=video_id,
            channel_id=channel_id,
            error_code="METRIC_INCOMPLETE",
            error_message="Required scoring metrics missing or invalid",
            retryable=False,
            transcript_result=transcript_result,
        )

    # Clamp to valid range
    tcs = max(0, min(10, tcs))
    fr = max(0, min(10, fr))
    ttmc = max(0, min(10, ttmc))
    deception = max(0, min(10, deception))
    sponsor = max(0, min(10, sponsor))

    base_score = (0.50 * tcs + 0.30 * fr + 0.20 * ttmc) * 10
    deception_penalty = -(deception / 10) * 20
    sponsor_penalty = -(sponsor / 10) * 10

    transcript_meta = {
        "language_code": transcript_result.get("language_code") if transcript_result else None,
        "is_generated": transcript_result.get("is_generated") if transcript_result else None,
        "segment_count": len((transcript_result or {}).get("segments") or []),
        "transcript_required": True,
    }

    return {
        "status": "evaluated",
        "scoring_version": SCORING_VERSION_V1,
        "video_id": video_id,
        "channel_id": channel_id,
        "score": {
            "text_score": _round_or_none(text_score),
            "combined_score": _round_or_none(combined_score),
            "score_type": "combined" if combined_score is not None else "text",
        },
        "metrics": {
            "title_content_similarity_score": _round_or_none(tcs, 1),
            "deception_score": _round_or_none(deception, 1),
            "focus_ratio": _round_or_none(fr, 1),
            "time_to_main_content": _round_or_none(ttmc, 1),
            "sponsor_interruption_score": _round_or_none(sponsor, 1),
        },
        "penalties": {
            "deception_penalty": _round_or_none(deception_penalty),
            "sponsor_penalty": _round_or_none(sponsor_penalty),
        },
        "components": {
            "base_score": _round_or_none(base_score),
        },
        "rationale": {
            "title_analysis_reasoning": evaluation_result.get("title_analysis_reasoning") or "",
            "deception_reasoning": evaluation_result.get("deception_reasoning") or "",
            "focus_reasoning": evaluation_result.get("focus_reasoning") or "",
            "time_reasoning": evaluation_result.get("time_reasoning") or "",
            "sponsor_reasoning": evaluation_result.get("sponsor_reasoning") or "",
            "summary": evaluation_result.get("llm_explanation") or "",
        },
        "transcript_meta": transcript_meta,
        "errors": [],
    }


def analyze_video_v1(
    video_id: str,
    channel_id: str | None,
    title: str,
    duration_seconds: int,
    transcript_result: dict | None,
) -> dict:
    """Transcript-required analyzeVideo wrapper returning the shared v1 schema."""
    if not transcript_result or not transcript_result.get("success"):
        error_message = "Transcript retrieval failed"
        if transcript_result and transcript_result.get("error"):
            error_message = transcript_result.get("error")
        return _build_unevaluated_response_v1(
            video_id=video_id,
            channel_id=channel_id,
            error_code="TRANSCRIPT_UNAVAILABLE",
            error_message=error_message,
            retryable=True,
            transcript_result=transcript_result,
        )

    segments = transcript_result.get("segments") or []
    if not segments:
        return _build_unevaluated_response_v1(
            video_id=video_id,
            channel_id=channel_id,
            error_code="TRANSCRIPT_EMPTY",
            error_message="Transcript missing segments",
            retryable=True,
            transcript_result=transcript_result,
        )

    evaluation_result = evaluate_video(
        video_id=video_id,
        channel_id=channel_id,
        title=title,
        duration_seconds=duration_seconds,
        transcript_segments=segments,
    )
    return to_analyze_video_response_v1(evaluation_result, transcript_result=transcript_result)
