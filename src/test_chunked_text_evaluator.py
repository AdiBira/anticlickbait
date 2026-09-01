#!/usr/bin/env python3
"""
Focused tests for the chunked full-transcript path helpers.

These are local, deterministic tests (no API calls).
Run with:
    PYTHONPATH=src python -B src/test_chunked_text_evaluator.py
"""

from text_evaluator import (
    _aggregate_chunk_content_metrics,
    _chunk_transcript_segments,
    _estimate_tokens_from_chars,
    _format_transcript,
)


PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        print(f"PASS {name}")
        PASS += 1
    else:
        print(f"FAIL {name}")
        if detail:
            print(f"  {detail}")
        FAIL += 1


def make_segments():
    return [
        {"start": 0.2, "text": "Hello!"},
        {"start": 1.1, "text": "Wait... what?"},
        {"start": 2.0, "text": "DUPLICATE"},
        {"start": 2.4, "text": "DUPLICATE"},
        {"start": 5.6, "text": "ALL CAPS?!"},
        {"start": 9.9, "text": "\"Quoted\" line."},
    ]


def flatten(chunks):
    out = []
    for chunk in chunks:
        out.extend(chunk)
    return out


def test_preserve_order_duplicates_and_punctuation():
    segs = make_segments()
    chunks = _chunk_transcript_segments(segs, max_chars=40)
    flat = flatten(chunks)

    check("chunks_created", len(chunks) >= 2, f"len(chunks)={len(chunks)}")
    check(
        "order_preserved_starts",
        [s["start"] for s in flat] == [s["start"] for s in segs],
        "start timestamps changed order",
    )
    check(
        "text_preserved_exactly",
        [s["text"] for s in flat] == [s["text"] for s in segs],
        "segment texts changed",
    )
    # Duplicate line must remain twice
    dup_count = sum(1 for s in flat if s["text"] == "DUPLICATE")
    check("duplicates_preserved", dup_count == 2, f"dup_count={dup_count}")

    formatted = _format_transcript(segs, use_integer_timestamps=True)
    check("punctuation_exclamation_kept", "Hello!" in formatted)
    check("punctuation_question_kept", "what?" in formatted)
    check("punctuation_quotes_kept", '"Quoted"' in formatted)
    check("integer_timestamp_format", "[0s] Hello!" in formatted, formatted.splitlines()[0])


def test_budget_behavior_and_single_huge_segment():
    segs = [{"start": 0, "text": "x" * 200}]
    chunks = _chunk_transcript_segments(segs, max_chars=20)
    check("huge_segment_not_dropped", len(chunks) == 1 and len(chunks[0]) == 1, str(chunks))

    segs2 = [
        {"start": 0, "text": "a" * 10},
        {"start": 1, "text": "b" * 10},
        {"start": 2, "text": "c" * 10},
    ]
    chunks2 = _chunk_transcript_segments(segs2, max_chars=25)
    flat2 = flatten(chunks2)
    check("all_segments_preserved_under_budget_split", len(flat2) == 3)


def test_deterministic_content_aggregation():
    chunk_results = [
        {
            "_chunk_start_seconds": 0,
            "_chunk_end_seconds": 100,
            "focus_ratio_pct": 40,
            "main_topic_starts_in_chunk": False,
            "main_topic_start_seconds": None,
            "midroll_sponsor_seconds": 0,
            "midroll_sponsor_interruptions": 0,
            "summary": "Intro and setup.",
        },
        {
            "_chunk_start_seconds": 100,
            "_chunk_end_seconds": 200,
            "focus_ratio_pct": 90,
            "main_topic_starts_in_chunk": True,
            "main_topic_start_seconds": 120,
            "midroll_sponsor_seconds": 20,
            "midroll_sponsor_interruptions": 1,
            "summary": "Main topic begins and continues.",
        },
        {
            "_chunk_start_seconds": 200,
            "_chunk_end_seconds": 300,
            "focus_ratio_pct": 80,
            "main_topic_starts_in_chunk": False,
            "main_topic_start_seconds": None,
            "midroll_sponsor_seconds": 50,
            "midroll_sponsor_interruptions": 1,
            "summary": "Main topic plus another sponsor block.",
        },
    ]
    content, reasoning = _aggregate_chunk_content_metrics(chunk_results, duration_seconds=300)
    # Weighted average = (40+90+80)/3 = 70 for equal spans
    check("focus_weighted_average", abs(content["focus_ratio_pct"] - 70) < 1e-6, str(content))
    check("earliest_main_topic_selected", content["time_to_main_content_seconds"] == 120, str(content))
    check("sponsor_class_excessive", content["sponsor_interruption"] == "excessive", str(content))
    check("reasoning_mentions_chunked", "Chunked full-transcript analysis" in reasoning)


def test_token_estimator():
    chars = 400
    est = _estimate_tokens_from_chars("x" * chars)
    check("char_to_token_estimator_basic", est == 135, f"est={est}")


def main():
    test_preserve_order_duplicates_and_punctuation()
    test_budget_behavior_and_single_huge_segment()
    test_deterministic_content_aggregation()
    test_token_estimator()

    print(f"\nPassed: {PASS}")
    print(f"Failed: {FAIL}")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
