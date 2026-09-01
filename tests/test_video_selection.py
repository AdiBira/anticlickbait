"""
Tests for _select_mixed_videos: 50/50 latest+popular selection logic.

Verifies:
- Correct bucket split (latest_target latest, popular_target popular)
- No duplicates in output
- Overlap fill: when a video appears in both pools, the gap is filled from the other bucket
- Pool source tag drives bucket assignment (not fake date sentinel)
- No year-2000 sentinel dates in output
- Edge cases: empty pool, small pool, single bucket only
- Latest bucket = most recent by published_at
- Popular bucket = highest view_count
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from youtube_api import _select_mixed_videos


def make_latest(video_id: str, published_at: str, view_count: int = 0) -> dict:
    return {
        "video_id": video_id,
        "published_at": published_at,
        "_pool_source": "latest",
        "view_count": view_count,
        "title": f"Latest {video_id}",
    }


def make_popular(video_id: str, view_count: int, published_at: str | None = None) -> dict:
    return {
        "video_id": video_id,
        "published_at": published_at,
        "_pool_source": "popular",
        "view_count": view_count,
        "title": f"Popular {video_id}",
    }


def test_clean_split():
    """No overlap: 8 latest + 7 popular = exactly 15."""
    latest = [make_latest(f"L{i}", f"2025-01-{i+1:02d}T00:00:00Z") for i in range(8)]
    popular = [make_popular(f"P{i}", view_count=(100 - i) * 1000) for i in range(7)]
    result = _select_mixed_videos(latest + popular, max_videos=15, latest_ratio=0.5)

    assert len(result) == 15
    ids = [v["video_id"] for v in result]
    assert len(ids) == len(set(ids)), "Duplicates found"
    assert sum(1 for v in result if v["_pool_source"] == "latest") == 8
    assert sum(1 for v in result if v["_pool_source"] == "popular") == 7
    print("PASS test_clean_split")


def test_overlap_fill():
    """3 videos in both pools. Gap filled from whichever bucket has extras. Total still 15."""
    shared = [make_latest("S1", "2025-01-01T00:00:00Z", 2_000_000),
              make_latest("S2", "2025-01-02T00:00:00Z", 1_500_000),
              make_latest("S3", "2025-01-03T00:00:00Z", 1_000_000)]
    shared_pop = [make_popular("S1", 2_000_000), make_popular("S2", 1_500_000), make_popular("S3", 1_000_000)]
    extra_latest = [make_latest(f"L{i}", f"2025-02-{i+1:02d}T00:00:00Z") for i in range(5)]
    extra_popular = [make_popular(f"P{i}", view_count=(100 - i) * 1000) for i in range(7)]

    pool = shared + extra_latest + shared_pop + extra_popular
    result = _select_mixed_videos(pool, max_videos=15, latest_ratio=0.5)

    assert len(result) == 15
    ids = [v["video_id"] for v in result]
    assert len(ids) == len(set(ids)), "Duplicates found"
    print("PASS test_overlap_fill")


def test_no_sentinel_dates():
    """No video in output should have the old 2000-01-01 sentinel date."""
    latest = [make_latest(f"L{i}", f"2024-06-{i+1:02d}T00:00:00Z") for i in range(8)]
    popular = [make_popular(f"P{i}", view_count=(50 - i) * 1000) for i in range(7)]
    result = _select_mixed_videos(latest + popular, max_videos=15, latest_ratio=0.5)

    for v in result:
        pub = v.get("published_at") or ""
        assert not pub.startswith("2000-"), f"Sentinel date in output for {v['video_id']}: {pub}"
    print("PASS test_no_sentinel_dates")


def test_small_pool():
    """Pool smaller than max_videos: return all available without error."""
    pool = [make_latest(f"L{i}", f"2025-01-{i+1:02d}T00:00:00Z") for i in range(5)]
    result = _select_mixed_videos(pool, max_videos=15, latest_ratio=0.5)
    assert len(result) == 5
    print("PASS test_small_pool")


def test_empty_pool():
    result = _select_mixed_videos([], max_videos=15, latest_ratio=0.5)
    assert result == []
    print("PASS test_empty_pool")


def test_latest_are_most_recent():
    """The latest bucket must contain the N most recent videos by published_at."""
    latest = [make_latest(f"L{i}", f"202{3 + i // 5}-0{(i % 5) + 1}-01T00:00:00Z") for i in range(15)]
    popular = [make_popular(f"P{i}", view_count=(100 - i) * 1000) for i in range(15)]

    result = _select_mixed_videos(latest + popular, max_videos=10, latest_ratio=0.5)
    # 5 latest + 5 popular
    latest_in = [v for v in result if v["_pool_source"] == "latest"]
    assert len(latest_in) == 5

    expected_dates = sorted([v["published_at"] for v in latest], reverse=True)[:5]
    selected_dates = sorted([v["published_at"] for v in latest_in], reverse=True)
    assert selected_dates == expected_dates, f"Wrong videos in latest bucket:\n  got {selected_dates}\n  want {expected_dates}"
    print("PASS test_latest_are_most_recent")


def test_popular_are_highest_views():
    """The popular bucket must contain the N highest view-count videos."""
    latest = [make_latest(f"L{i}", f"2025-01-{i+1:02d}T00:00:00Z") for i in range(8)]
    popular = [make_popular(f"P{i}", view_count=(15 - i) * 1_000_000) for i in range(15)]

    result = _select_mixed_videos(latest + popular, max_videos=15, latest_ratio=0.5)
    popular_in = [v for v in result if v["_pool_source"] == "popular"]
    assert len(popular_in) == 7

    top7 = sorted([v["view_count"] for v in popular], reverse=True)[:7]
    selected = sorted([v["view_count"] for v in popular_in], reverse=True)
    assert selected == top7, f"Wrong videos in popular bucket:\n  got {selected}\n  want {top7}"
    print("PASS test_popular_are_highest_views")


def test_popular_only_pool():
    """If pool has only popular-tagged videos, return as many as possible."""
    popular = [make_popular(f"P{i}", view_count=(100 - i) * 1000) for i in range(15)]
    result = _select_mixed_videos(popular, max_videos=15, latest_ratio=0.5)
    assert len(result) == 15
    ids = [v["video_id"] for v in result]
    assert len(ids) == len(set(ids))
    print("PASS test_popular_only_pool")


if __name__ == "__main__":
    test_clean_split()
    test_overlap_fill()
    test_no_sentinel_dates()
    test_small_pool()
    test_empty_pool()
    test_latest_are_most_recent()
    test_popular_are_highest_views()
    test_popular_only_pool()
    print("\nAll tests passed.")
