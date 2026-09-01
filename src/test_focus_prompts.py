#!/usr/bin/env python3
"""
Test 5 different Call 2 prompt approaches for focus_ratio on multi-topic titles.
Runs all 5 on the same video and compares results.
"""

import json
import sys
from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_MODEL
from transcript_fetcher import get_transcript
import yt_dlp

META = """You are part of a system that holds clickbait creators accountable by scoring \
how honestly a video's title represents its content.

We use the video transcript as a proxy for video content. The transcript \
cannot capture visuals, reactions, or body language. Score based on whether \
the underlying topic and substance are covered — not whether specific title \
words appear verbatim. Judge thematic relevance: content that discusses, \
responds to, or is driven by the titled subject counts as on-topic."""

SCORING_GUIDE = """
FOCUS_RATIO — what fraction of the video's runtime is spent on the titled topic?
  Use timestamps to estimate how many seconds/minutes are on-topic vs off-topic.
  Include directly relevant context and setup as on-topic. Exclude filler,
  unrelated tangents, self-promotion, and segments about other topics.
  Then convert your time estimate to a percentage of total duration and score:
  0-1: <10% of runtime on topic.
  2-3: 10-30% of runtime on topic.
  4-5: 30-55% of runtime on topic.
  6-7: 55-75% of runtime on topic.
  8-9: 75-95% of runtime on topic.
  10: 95-100% of runtime on topic.

TIME_TO_MAIN_CONTENT — how far into the video before the titled topic begins?
  10: Starts immediately or within first 2% of video duration.
  8-9: Starts within first 5-10%.
  6-7: Starts within first 10-20%.
  4-5: Starts within first 20-35%.
  2-3: Starts after 35-50%.
  0-1: Main topic starts after 50%+ or never truly begins.

SPONSOR_INTERRUPTION_SCORE — what percentage of total video duration is sponsor/ad content?
  0: No sponsor segments at all.
  1-2: Under 15 seconds, at start/end only.
  3-4: 1-5% of total video duration.
  5-6: 5-10% of total video duration.
  7-8: 10-20% of total video duration.
  9-10: Exceeds 20% of total video duration."""


# ── Approach 1: Per-topic focus scoring ──
# Force LLM to score each title topic separately, then take the average
PROMPT_1 = META + """

The goal: determine whether the titled topic is truly the main intent of the video.

TITLE: {title}
DURATION: {duration_seconds}s

TRANSCRIPT (with timestamps):
{transcript}

Step 1: Break the title into ALL distinct topics it promises (separated by colons, commas, ampersands, or implied).
Step 2: For EACH topic, estimate what percentage of the video covers it (use timestamps).
Step 3: The overall focus_ratio should reflect the UNION of all on-topic time — if a segment is relevant to ANY title topic, it is on-topic.

Return JSON only:
{{"title_topics":["topic1","topic2",...],"per_topic_coverage":[{{"topic":"...","seconds_on_topic":number,"pct":number}}],"total_on_topic_pct":number,"focus_ratio_reasoning":"...","focus_ratio":0-10,"time_to_main_content_reasoning":"...","time_to_main_content":0-10,"sponsor_interruption_reasoning":"...","sponsor_interruption_score":0-10}}
""" + SCORING_GUIDE


# ── Approach 2: Inverse framing — identify OFF-topic segments ──
# Instead of "what's on topic?", ask "what's clearly unrelated?"
PROMPT_2 = META + """

The goal: determine whether the titled topic is truly the main intent of the video.

TITLE: {title}
DURATION: {duration_seconds}s

TRANSCRIPT (with timestamps):
{transcript}

Instead of identifying on-topic content, identify segments that are CLEARLY \
UNRELATED to any aspect of the title. Use timestamps to estimate total \
off-topic seconds. Everything else counts as on-topic.

A segment is off-topic ONLY if it has no connection to any part of the title — \
not even as context, background, implications, or analysis related to the titled subjects.

Return JSON only:
{{"off_topic_segments":[{{"start_s":number,"end_s":number,"description":"..."}}],"total_off_topic_seconds":number,"total_off_topic_pct":number,"focus_ratio_reasoning":"...","focus_ratio":0-10,"time_to_main_content_reasoning":"...","time_to_main_content":0-10,"sponsor_interruption_reasoning":"...","sponsor_interruption_score":0-10}}
""" + SCORING_GUIDE


# ── Approach 3: Segment classification ──
# Ask LLM to classify major content blocks before scoring
PROMPT_3 = META + """

The goal: determine whether the titled topic is truly the main intent of the video.

TITLE: {title}
DURATION: {duration_seconds}s

TRANSCRIPT (with timestamps):
{transcript}

Step 1: Divide the video into major content segments based on topic shifts \
(use timestamps). For each segment, note:
- Time range (start-end seconds)
- Brief description of what's discussed
- Whether it relates to ANY part of the title (yes/no)

Step 2: Sum up the "yes" segments to calculate focus percentage.

Return JSON only:
{{"segments":[{{"start_s":number,"end_s":number,"description":"...","on_topic":true/false}}],"total_on_topic_seconds":number,"total_duration":number,"focus_ratio_reasoning":"...","focus_ratio":0-10,"time_to_main_content_reasoning":"...","time_to_main_content":0-10,"sponsor_interruption_reasoning":"...","sponsor_interruption_score":0-10}}
""" + SCORING_GUIDE


# ── Approach 4: Title decomposition + explicit matching ──
# Two-phase: decompose title, then for each topic find transcript evidence
PROMPT_4 = META + """

The goal: determine whether the titled topic is truly the main intent of the video.

TITLE: {title}
DURATION: {duration_seconds}s

TRANSCRIPT (with timestamps):
{transcript}

Analysis steps:
1. List every distinct topic or claim the title makes.
2. For each topic, find the timestamp ranges in the transcript where it is \
discussed (directly or thematically — implications, analysis, and context count).
3. Merge all on-topic ranges (avoiding double-counting overlaps).
4. Calculate merged on-topic time as a percentage of total duration.

Return JSON only:
{{"title_topics":[{{"topic":"...","timestamp_ranges":["Xs-Ys",...],"seconds":number}}],"merged_on_topic_seconds":number,"merged_on_topic_pct":number,"focus_ratio_reasoning":"...","focus_ratio":0-10,"time_to_main_content_reasoning":"...","time_to_main_content":0-10,"sponsor_interruption_reasoning":"...","sponsor_interruption_score":0-10}}
""" + SCORING_GUIDE


# ── Approach 5: Content summary then score ──
# First summarize what the video covers, then score against title
PROMPT_5 = META + """

The goal: determine whether the titled topic is truly the main intent of the video.

TITLE: {title}
DURATION: {duration_seconds}s

TRANSCRIPT (with timestamps):
{transcript}

Step 1: Write a 3-4 sentence summary of what the video actually covers, \
noting the major topics and approximate time spent on each.
Step 2: Compare this summary against the title. What percentage of the video's \
content is relevant to what the title promises?

Return JSON only:
{{"video_summary":"...","focus_ratio_reasoning":"...","focus_ratio":0-10,"time_to_main_content_reasoning":"...","time_to_main_content":0-10,"sponsor_interruption_reasoning":"...","sponsor_interruption_score":0-10}}
""" + SCORING_GUIDE


def format_transcript(segments):
    lines = []
    for seg in segments:
        start = seg.get("start", 0)
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"[{float(start):.1f}s] {text}")
    return "\n".join(lines)


def call_llm(prompt):
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=3000,
    )
    text = response.choices[0].message.content.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())


def get_video_metadata(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True, "ignoreconfig": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {"title": info.get("title", ""), "duration": int(info.get("duration", 0))}


def main():
    video_id = sys.argv[1] if len(sys.argv) > 1 else "zT2scW8v6sg"

    print(f"Fetching metadata for {video_id}...")
    meta = get_video_metadata(video_id)
    print(f"Title: {meta['title']}")
    print(f"Duration: {meta['duration']}s")

    print("Fetching transcript...")
    t = get_transcript(video_id)
    if not t.get("success"):
        print(f"Transcript failed: {t.get('error')}")
        return
    transcript = format_transcript(t["segments"])
    print(f"Segments: {len(t['segments'])}")

    prompts = {
        "1_per_topic": PROMPT_1,
        "2_inverse": PROMPT_2,
        "3_segments": PROMPT_3,
        "4_decompose": PROMPT_4,
        "5_summary": PROMPT_5,
    }

    results = {}
    for name, prompt_template in prompts.items():
        prompt = prompt_template.format(
            title=meta["title"],
            duration_seconds=meta["duration"],
            transcript=transcript,
        )
        print(f"\n{'='*70}")
        print(f"APPROACH: {name}")
        print(f"{'='*70}")
        try:
            result = call_llm(prompt)
            results[name] = result
            fr = result.get("focus_ratio", "?")
            ttmc = result.get("time_to_main_content", "?")
            spo = result.get("sponsor_interruption_score", "?")
            print(f"  focus_ratio={fr}  time_to_main={ttmc}  sponsor={spo}")
            print(f"  Reasoning: {result.get('focus_ratio_reasoning', '')[:300]}")

            # Print approach-specific fields
            if "per_topic_coverage" in result:
                for tc in result["per_topic_coverage"]:
                    print(f"    Topic: {tc.get('topic','')} — {tc.get('seconds_on_topic',0)}s ({tc.get('pct',0)}%)")
                print(f"    Total on-topic: {result.get('total_on_topic_pct',0)}%")
            if "off_topic_segments" in result:
                for ot in result["off_topic_segments"]:
                    print(f"    Off-topic: {ot.get('start_s',0)}s-{ot.get('end_s',0)}s — {ot.get('description','')}")
                print(f"    Total off-topic: {result.get('total_off_topic_pct',0)}%")
            if "segments" in result:
                for seg in result["segments"]:
                    marker = "ON " if seg.get("on_topic") else "OFF"
                    print(f"    [{marker}] {seg.get('start_s',0)}s-{seg.get('end_s',0)}s — {seg.get('description','')[:80]}")
            if "title_topics" in result and isinstance(result["title_topics"], list) and result["title_topics"] and isinstance(result["title_topics"][0], dict):
                for tt in result["title_topics"]:
                    print(f"    Topic: {tt.get('topic','')} — {tt.get('seconds',0)}s — ranges: {tt.get('timestamp_ranges','')}")
                print(f"    Merged on-topic: {result.get('merged_on_topic_pct',0)}%")
            if "video_summary" in result:
                print(f"    Summary: {result['video_summary'][:300]}")
        except Exception as e:
            print(f"  FAILED: {e}")
            results[name] = {"error": str(e)}

    print(f"\n\n{'='*70}")
    print("COMPARISON")
    print(f"{'='*70}")
    print(f"{'Approach':<20} {'FR':>4} {'TTMC':>5} {'SPO':>4}")
    print("-" * 40)
    for name, r in results.items():
        if "error" in r:
            print(f"{name:<20} {'ERR':>4}")
        else:
            print(f"{name:<20} {r.get('focus_ratio','?'):>4} {r.get('time_to_main_content','?'):>5} {r.get('sponsor_interruption_score','?'):>4}")


if __name__ == "__main__":
    main()
