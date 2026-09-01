#!/usr/bin/env python3
"""Tests for _parse_vtt rolling-window deduplication."""

from transcript_fetcher import _parse_vtt


def test_exact_duplicate_removal():
    """Consecutive identical lines should be collapsed."""
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello world

00:00:03.000 --> 00:00:05.000
Hello world

00:00:05.000 --> 00:00:07.000
Something new
"""
    segments, text = _parse_vtt(vtt)
    assert len(segments) == 2
    assert segments[0]["text"] == "Hello world"
    assert segments[1]["text"] == "Something new"


def test_rolling_window_same_timestamp():
    """Same-timestamp rolling window: short then long version. Keep longest."""
    vtt = """WEBVTT

00:00:03.000 --> 00:00:05.000
Peter was the person who told me this

00:00:03.000 --> 00:00:06.000
Peter was the person who told me this really pithy

00:00:05.000 --> 00:00:08.000
really pithy

00:00:05.000 --> 00:00:09.000
really pithy quote in a world that's changing so

00:00:08.000 --> 00:00:11.000
quote in a world that's changing so

00:00:08.000 --> 00:00:12.000
quote in a world that's changing so quickly the biggest risk
"""
    segments, text = _parse_vtt(vtt)
    # Should have 3 segments (one per distinct timestamp group), not 6
    assert len(segments) == 3, f"Expected 3, got {len(segments)}: {[s['text'][:40] for s in segments]}"
    assert "really pithy" in segments[0]["text"]
    assert "quote in a world" in segments[1]["text"] or "quote in a world" in segments[2]["text"]


def test_cross_timestamp_substring_removal():
    """Fragment at new timestamp that's a substring of previous should be removed."""
    vtt = """WEBVTT

00:00:01.000 --> 00:00:04.000
the biggest risk you can take is not taking any risk

00:00:04.000 --> 00:00:06.000
not taking any risk
"""
    segments, text = _parse_vtt(vtt)
    # "not taking any risk" is a substring of the previous line
    assert len(segments) == 1
    assert segments[0]["text"] == "the biggest risk you can take is not taking any risk"


def test_normal_non_overlapping_captions():
    """Normal captions with no overlap should pass through unchanged."""
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
Welcome to the show

00:00:02.000 --> 00:00:04.000
Today we're talking about AI

00:00:04.000 --> 00:00:06.000
Let's get started
"""
    segments, text = _parse_vtt(vtt)
    assert len(segments) == 3
    assert text == "Welcome to the show Today we're talking about AI Let's get started"


def test_realistic_yt_dlp_pattern():
    """Realistic yt-dlp word-level auto-caption pattern."""
    vtt = """WEBVTT

00:00:02.000 --> 00:00:05.000
you know Richard feeman right he's the

00:00:02.000 --> 00:00:06.000
you know Richard feeman right he's the super amazing physicist you know famous

00:00:05.000 --> 00:00:09.000
super amazing physicist you know famous

00:00:05.000 --> 00:00:10.000
super amazing physicist you know famous for picking locks staring at an atomic

00:00:09.000 --> 00:00:12.000
for picking locks staring at an atomic

00:00:09.000 --> 00:00:13.000
for picking locks staring at an atomic blast being the only physicist to ever
"""
    segments, text = _parse_vtt(vtt)
    # Should collapse to 3 segments (one per timestamp group)
    assert len(segments) == 3, f"Expected 3, got {len(segments)}: {[s['text'][:50] for s in segments]}"
    # Each should be the longer version
    assert "super amazing" in segments[0]["text"]
    assert "for picking locks" in segments[1]["text"]
    assert "blast being" in segments[2]["text"]


def test_html_tags_stripped():
    """VTT with inline HTML tags should have them stripped."""
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
<c.colorE5E5E5>Hello</c> <c.colorCCCCCC>world</c>
"""
    segments, text = _parse_vtt(vtt)
    assert len(segments) == 1
    assert segments[0]["text"] == "Hello world"


def test_empty_content():
    """Empty or header-only VTT returns empty."""
    segments, text = _parse_vtt("WEBVTT\n\n")
    assert len(segments) == 0
    assert text == ""


def test_segment_count_reduction():
    """A full rolling-window transcript should roughly halve in segment count."""
    # Build 100 rolling-window pairs with unique words per group
    blocks = ["WEBVTT\n"]
    for i in range(100):
        mins = (i * 2) // 60
        secs = (i * 2) % 60
        t1 = f"00:{mins:02d}:{secs:02d}.000"
        secs2 = secs + 2
        mins2 = mins + secs2 // 60
        secs2 = secs2 % 60
        t2 = f"00:{mins2:02d}:{secs2:02d}.000"
        secs3 = secs + 3
        mins3 = mins + secs3 // 60
        secs3 = secs3 % 60
        t3 = f"00:{mins3:02d}:{secs3:02d}.000"
        # Use unique sentences that won't be substrings of each other
        short_text = f"sentence number {i} begins here"
        long_text = f"sentence number {i} begins here and continues with unique content x{i}y{i}"
        blocks.append(f"\n{t1} --> {t2}\n{short_text}\n")
        blocks.append(f"\n{t1} --> {t3}\n{long_text}\n")

    vtt = "\n".join(blocks)
    segments, text = _parse_vtt(vtt)
    # Should be ~100 segments (one per timestamp), not 200
    assert len(segments) <= 110, f"Expected ~100, got {len(segments)}"
    assert len(segments) >= 90, f"Expected ~100, got {len(segments)}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  PASS: {test_fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test_fn.__name__} — {e}")
        except Exception as e:
            print(f"  ERROR: {test_fn.__name__} — {e}")
    print(f"\n{passed}/{len(tests)} passed")
