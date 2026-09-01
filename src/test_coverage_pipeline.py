#!/usr/bin/env python3
"""
Focused deterministic tests for coverage_pipeline helpers.

Run with:
  PYTHONPATH=src python -B src/test_coverage_pipeline.py
"""

from coverage_pipeline import _build_channel_final_coverage, _build_locked_targets_for_pool


PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"PASS {name}")
        PASS += 1
    else:
        print(f"FAIL {name}")
        if detail:
            print(f"  {detail}")
        FAIL += 1


def _sample_pool(n: int = 40) -> list[dict]:
    pool = []
    for i in range(n):
        pool.append(
            {
                "video_id": f"vid_{i:03d}",
                "title": f"Video {i}",
                "published_at": f"2026-02-{(i % 27) + 1:02d}T12:00:00Z",
                "view_count": (n - i) * 1000 + (i % 3),
                "duration_seconds": 400 + i,
            }
        )
    return pool


def test_deterministic_selection():
    pool = _sample_pool(50)
    one, meta_one = _build_locked_targets_for_pool(
        pool,
        channel_id="UC1",
        handle="@foo",
        category="Science",
        target_total=15,
        latest_target=8,
        popular_target=7,
        selection_rule_version="test-v1",
    )
    two, meta_two = _build_locked_targets_for_pool(
        pool,
        channel_id="UC1",
        handle="@foo",
        category="Science",
        target_total=15,
        latest_target=8,
        popular_target=7,
        selection_rule_version="test-v1",
    )
    ids_one = [r["video_id"] for r in one]
    ids_two = [r["video_id"] for r in two]
    check("deterministic_ids", ids_one == ids_two, f"{ids_one} != {ids_two}")
    check("deterministic_meta", meta_one["ratio_drift"] == meta_two["ratio_drift"])


def test_overlap_keeps_ratio():
    # Make latest and popular heavily overlap in front of both queues.
    pool = []
    for i in range(30):
        pool.append(
            {
                "video_id": f"ov_{i:03d}",
                "title": f"Overlap {i}",
                "published_at": f"2026-01-{(30 - i):02d}T00:00:00Z",
                "view_count": 1_000_000 - i * 10,
                "duration_seconds": 600,
            }
        )

    selected, _meta = _build_locked_targets_for_pool(
        pool,
        channel_id="UC2",
        handle="@bar",
        category="Tech",
        target_total=15,
        latest_target=8,
        popular_target=7,
        selection_rule_version="test-v1",
    )
    latest_count = sum(1 for r in selected if r["bucket"] == "latest")
    popular_count = sum(1 for r in selected if r["bucket"] == "popular")
    check("selected_15", len(selected) == 15, str(len(selected)))
    check("ratio_8_7", latest_count == 8 and popular_count == 7, f"{latest_count}/{popular_count}")


def test_coverage_gate_fail_then_pass():
    channel_rows = []
    for i in range(15):
        channel_rows.append(
            {
                "video_id": f"v{i}",
                "bucket": "latest" if i < 8 else "popular",
                "selected_rank": i + 1,
                "title": f"V{i}",
            }
        )
    replacements = []

    # 14/15 resolved should fail.
    subtitle_status_fail = {}
    for i in range(14):
        subtitle_status_fail[f"v{i}"] = {"status": "resolved", "content_hash": f"h{i}"}
    report_fail = _build_channel_final_coverage(
        channel_id="UC3",
        channel_rows=channel_rows,
        channel_replacements=replacements,
        channel_summary={"required_videos": 15, "required_latest": 8, "required_popular": 7},
        subtitle_status=subtitle_status_fail,
    )
    check("fail_on_14_of_15", report_fail["status"] == "blocked", str(report_fail))

    # 15/15 resolved with exact 8/7 should pass.
    subtitle_status_pass = {}
    for i in range(15):
        subtitle_status_pass[f"v{i}"] = {"status": "resolved", "content_hash": f"h{i}"}
    report_pass = _build_channel_final_coverage(
        channel_id="UC3",
        channel_rows=channel_rows,
        channel_replacements=replacements,
        channel_summary={"required_videos": 15, "required_latest": 8, "required_popular": 7},
        subtitle_status=subtitle_status_pass,
    )
    check("pass_on_15_of_15", report_pass["status"] == "pass", str(report_pass))
    check("pass_ratio_exact", report_pass["ratio_drift"] == 0, str(report_pass["ratio_drift"]))


def main():
    test_deterministic_selection()
    test_overlap_keeps_ratio()
    test_coverage_gate_fail_then_pass()
    print(f"\nPassed: {PASS}")
    print(f"Failed: {FAIL}")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
