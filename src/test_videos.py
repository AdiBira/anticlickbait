#!/usr/bin/env python3
"""Test script for evaluating specific videos."""

import re
from transcript_fetcher import get_transcript
from text_evaluator import evaluate_video, compute_video_score, _format_transcript

# Test videos
TEST_VIDEOS = [
    ("rurhk1hadp8", "BANE airplane scene | The Dark Knight Rises [IMAX]"),
    ("Mzw2ttJD2qQ", "The Odyssey | Official Trailer"),
    ("NSQ0thmvSg0", "Harkonnen' Floating | Dune Part Two"),
    ("VwLT3oNqBSU", "CNC Lathe vs CNC Mill: What's the Difference"),
    ("3UbOG0ERCwM", "Crossing English Channel on Flyboard Air Jet Hoverboard"),
    ("GDoOuubNPOA", "Tata Steel Chess India 2026 | Day 3 Rapid"),
    ("vvvpU7Kr0bs", "DRDO Joins The Race to Build Exoskeleton"),
    ("rFZrL1RiuVI", "Peter Thiel: Going from Zero to One"),
    ("rTCnOD_szRw", "Jeffrey Sachs Blasts US Power Grab Over Venezuela"),
    ("Xik34jh-doc", "Deepinder Goyal: Zomato, 10-Min Delivery"),
    ("RqlQYBcsq54", "No Soup For You! | The Soup Nazi | Seinfeld"),
    ("VYFOau_tOpE", "The game that made Gukesh the World Championship Challenger"),
    ("EAXXW9WytKY", "Using Hikaru to CHEAT at Chess"),
    ("0e3GPea1Tyg", "$456,000 Squid Game In Real Life!"),
    ("lR8bDt636vc", "SHOCKING Company Call Goes VIRAL... This is TERRIFYING!"),
    ("61TBVn3SdiU", "Germany's Shocking Shift On Russia"),
]


def test_transcripts():
    """Fetch and display transcript info for all test videos."""
    print("=" * 80)
    print("TRANSCRIPT VERIFICATION")
    print("=" * 80)

    results = []
    for video_id, title in TEST_VIDEOS:
        print(f"\n[{video_id}] {title[:50]}...")

        transcript_result = get_transcript(video_id)

        if not transcript_result["success"]:
            print(f"  ERROR: {transcript_result['error']}")
            results.append((video_id, title, None, None, transcript_result['error']))
            continue

        segments = transcript_result["segments"]
        formatted = _format_transcript(segments)

        num_segments = len(segments)
        total_chars = len(formatted)
        duration_covered = segments[-1]["start"] if segments else 0

        print(f"  Segments: {num_segments}")
        print(f"  Characters: {total_chars:,}")
        print(f"  Duration covered: {duration_covered:.1f}s")
        print(f"  Language: {transcript_result['language_code']}")

        results.append((video_id, title, num_segments, total_chars, None))

    return results


def test_evaluations(max_videos=3):
    """Run evaluations on a subset of videos."""
    print("\n" + "=" * 80)
    print(f"EVALUATION TEST (first {max_videos} videos with transcripts)")
    print("=" * 80)

    evaluated = 0
    for video_id, title in TEST_VIDEOS:
        if evaluated >= max_videos:
            break

        print(f"\n[{video_id}] {title[:50]}...")

        # Get transcript
        transcript_result = get_transcript(video_id)
        if not transcript_result["success"]:
            print(f"  Skipping: {transcript_result['error']}")
            continue

        segments = transcript_result["segments"]
        formatted = _format_transcript(segments)

        # Show transcript sample
        print(f"  Transcript length: {len(formatted):,} chars")
        print(f"  First 200 chars: {formatted[:200]}...")
        print(f"  Last 200 chars: ...{formatted[-200:]}")

        # Run evaluation
        print("\n  Running evaluation...")
        result = evaluate_video(
            video_id=video_id,
            channel_id="test",
            title=title,
            duration_seconds=int(segments[-1]["start"]) if segments else 300,
            transcript_segments=segments,
        )

        if result.get("evaluation_success"):
            score = compute_video_score(result)
            print(f"\n  RESULTS:")
            print(f"    Title-Content Similarity: {result['title_content_similarity_score']}/10")
            print(f"    Title Sensationalism: {result['title_sensationalism_score']}/10")
            print(f"    Sensationalism Mismatch: {result['sensationalism_mismatch']}")
            print(f"    Deception Flag: {result['deception_flag']}")
            print(f"    Focus Ratio: {result['focus_ratio_pct']}%")
            print(f"    Time to Main Content: {result['time_to_main_content_seconds']}s")
            print(f"    FINAL SCORE: {score:.1f}")
            print(f"    Reasoning: {result['llm_explanation']}")
        else:
            print(f"  Evaluation failed: {result.get('error')}")

        evaluated += 1

    print(f"\n\nEvaluated {evaluated} videos.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--eval":
        max_vids = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        test_evaluations(max_vids)
    else:
        test_transcripts()
