#!/usr/bin/env python3
"""
Test script for v1.1 scoring prompts.
Accepts YouTube video URLs or IDs, fetches transcript, runs evaluation, prints results.

Usage:
    python test_scoring_v11.py VIDEO_URL_OR_ID [VIDEO_URL_OR_ID ...]

Examples:
    python test_scoring_v11.py https://www.youtube.com/watch?v=dQw4w9WgXcQ
    python test_scoring_v11.py dQw4w9WgXcQ abc123 "https://youtu.be/xyz"
"""

import sys
import re
import json

import yt_dlp

from text_evaluator import evaluate_video, compute_video_score
from transcript_fetcher import get_transcript


def extract_video_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    patterns = [
        r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url_or_id)
        if m:
            return m.group(1)
    # Assume it's a bare video ID
    if re.match(r"^[a-zA-Z0-9_-]{11}$", url_or_id):
        return url_or_id
    return url_or_id


def get_video_metadata(video_id: str) -> dict:
    """Fetch title + duration via yt-dlp (no API quota cost)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title", "Unknown"),
        "duration": int(info.get("duration", 0)),
        "channel": info.get("channel", "Unknown"),
        "channel_id": info.get("channel_id", ""),
    }


def print_result(video_id: str, meta: dict, result: dict, score: float | None):
    print("\n" + "=" * 80)
    print(f"VIDEO: {meta['title']}")
    print(f"  Channel: {meta['channel']}  |  Duration: {meta['duration']}s  |  ID: {video_id}")
    print("-" * 80)

    if not result.get("evaluation_success"):
        print(f"  EVALUATION FAILED: {result.get('error')}")
        return

    tcs = result.get("title_content_similarity_score", -1)
    deception = result.get("deception_score", -1)
    fr = result.get("focus_ratio", -1)
    ttmc = result.get("time_to_main_content", -1)
    sponsor = result.get("sponsor_interruption_score", -1)

    # Score breakdown
    if score is not None:
        base = (0.50 * max(0, tcs) + 0.30 * max(0, fr) + 0.20 * max(0, ttmc)) * 10
        d_pen = -(max(0, deception) / 10) * 20
        s_pen = -(max(0, sponsor) / 10) * 10
        band = "GREEN" if score >= 80 else "AMBER" if score >= 60 else "ORANGE" if score >= 40 else "RED"
        print(f"  FINAL SCORE: {score:.1f} [{band}]")
        print(f"    base={base:.1f}  deception_penalty={d_pen:.1f}  sponsor_penalty={s_pen:.1f}")
    else:
        print("  FINAL SCORE: N/A (metrics incomplete)")

    print()
    print(f"  SUB-METRICS (all 0-10):")
    print(f"    title_content_similarity : {tcs}  (weight 50%)")
    print(f"    focus_ratio              : {fr}  (weight 30%)")
    print(f"    time_to_main_content     : {ttmc}  (weight 20%)")
    print(f"    deception_score          : {deception}  (penalty: max -20)")
    print(f"    sponsor_interruption     : {sponsor}  (penalty: max -10)")

    print()
    print(f"  REASONING:")
    for key in ["title_analysis_reasoning", "deception_reasoning", "focus_reasoning", "time_reasoning", "sponsor_reasoning"]:
        val = result.get(key, "")
        if val:
            label = key.replace("_reasoning", "").replace("_", " ").title()
            print(f"    [{label}] {val}")

    print("=" * 80)


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_scoring_v11.py VIDEO_URL_OR_ID [VIDEO_URL_OR_ID ...]")
        sys.exit(1)

    video_inputs = sys.argv[1:]
    results_summary = []

    for raw_input in video_inputs:
        video_id = extract_video_id(raw_input)
        print(f"\n>>> Processing: {video_id}")

        # 1. Get metadata
        try:
            meta = get_video_metadata(video_id)
            print(f"    Title: {meta['title']}")
            print(f"    Duration: {meta['duration']}s")
        except Exception as e:
            print(f"    ERROR fetching metadata: {e}")
            continue

        # 2. Get transcript
        print("    Fetching transcript...")
        transcript_result = get_transcript(video_id)
        if not transcript_result.get("success"):
            print(f"    ERROR: Transcript failed — {transcript_result.get('error')}")
            continue

        segments = transcript_result.get("segments", [])
        print(f"    Transcript: {len(segments)} segments")

        # 3. Evaluate
        print("    Running evaluation (2 parallel LLM calls)...")
        result = evaluate_video(
            video_id=video_id,
            channel_id=meta.get("channel_id", ""),
            title=meta["title"],
            duration_seconds=meta["duration"],
            transcript_segments=segments,
        )

        # 4. Score
        score = compute_video_score(result)

        # 5. Print
        print_result(video_id, meta, result, score)

        results_summary.append({
            "video_id": video_id,
            "title": meta["title"],
            "channel": meta["channel"],
            "score": round(score, 1) if score is not None else None,
            "tcs": result.get("title_content_similarity_score"),
            "fr": result.get("focus_ratio"),
            "ttmc": result.get("time_to_main_content"),
            "deception": result.get("deception_score"),
            "sponsor": result.get("sponsor_interruption_score"),
        })

    # Summary table
    if len(results_summary) > 1:
        print("\n\n" + "=" * 80)
        print("SUMMARY")
        print("-" * 80)
        print(f"{'Score':>6} | {'TCS':>4} {'FR':>4} {'TTMC':>4} {'DEC':>4} {'SPO':>4} | {'Title'}")
        print("-" * 80)
        for r in sorted(results_summary, key=lambda x: x["score"] if x["score"] is not None else -1, reverse=True):
            s = f"{r['score']:5.1f}" if r["score"] is not None else "  N/A"
            title_short = r["title"][:50] + "..." if len(r["title"]) > 50 else r["title"]
            def _fmt(v):
                return f"{v:>3}" if v is not None else "N/A"
            print(f"{s}  |  {_fmt(r['tcs'])}  {_fmt(r['fr'])}  {_fmt(r['ttmc'])}  {_fmt(r['deception'])}  {_fmt(r['sponsor'])}  | {title_short}")
        print("=" * 80)


if __name__ == "__main__":
    main()
