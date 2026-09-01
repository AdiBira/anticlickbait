#!/usr/bin/env python3
"""
Empirically probe practical LLM worker concurrency using real video evaluations.

This script:
1) Collects a sample of unevaluated (or any) videos from the curated channels list.
2) Fetches transcripts sequentially and records transcript fetch timings.
3) Replays text evaluation at increasing worker counts to observe throughput and 429s.

Notes:
- Uses the same evaluate_video() path as the main pipeline.
- Does not write evaluation results to the SQLite DB.
- Intended for controlled experiments; costs real OpenAI API tokens.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sqlite3
import statistics
import time
from pathlib import Path

from main import load_channels
from youtube_api import get_channel_info_by_handle, get_channel_videos
from transcript_fetcher import get_transcript
from text_evaluator import evaluate_video


def _is_long_form(video: dict, threshold_seconds: int) -> bool:
    return int(video.get("duration_seconds") or 0) > threshold_seconds


def _load_existing_video_ids(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT video_id FROM video_evaluations").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def _collect_sample_tasks(
    channels_file: Path,
    db_path: Path,
    max_channels: int,
    max_videos_per_channel: int,
    max_sample_videos: int,
    long_form_duration_seconds: int,
    long_form_channel_skip_ratio: float,
    allow_already_evaluated: bool,
) -> tuple[list[dict], dict]:
    existing_video_ids = _load_existing_video_ids(db_path)
    channels = load_channels(channels_file)
    tasks: list[dict] = []
    stats = {
        "channels_considered": 0,
        "channels_resolved": 0,
        "channels_skipped_long_form": 0,
        "channels_failed_lookup": 0,
        "videos_seen": 0,
        "videos_existing_in_db_skipped": 0,
        "videos_long_form_skipped": 0,
        "videos_selected_for_transcript_fetch": 0,
    }

    for channel in channels:
        if len(tasks) >= max_sample_videos or stats["channels_considered"] >= max_channels:
            break

        handle = channel.get("handle")
        if not handle:
            continue

        stats["channels_considered"] += 1
        channel_info = get_channel_info_by_handle(handle)
        if not channel_info:
            stats["channels_failed_lookup"] += 1
            continue
        stats["channels_resolved"] += 1

        channel_id = channel_info["channel_id"]
        channel_title = channel_info["title"]
        videos = get_channel_videos(channel_id, max_videos=max_videos_per_channel)
        if not videos:
            continue

        long_form_videos = [v for v in videos if _is_long_form(v, long_form_duration_seconds)]
        long_ratio = len(long_form_videos) / len(videos)
        if long_ratio > long_form_channel_skip_ratio:
            stats["channels_skipped_long_form"] += 1
            continue

        for video in videos:
            if len(tasks) >= max_sample_videos:
                break
            stats["videos_seen"] += 1

            if _is_long_form(video, long_form_duration_seconds):
                stats["videos_long_form_skipped"] += 1
                continue

            video_id = video["video_id"]
            if (not allow_already_evaluated) and video_id in existing_video_ids:
                stats["videos_existing_in_db_skipped"] += 1
                continue

            tasks.append(
                {
                    "video_id": video_id,
                    "channel_id": channel_id,
                    "channel_title": channel_title,
                    "title": video["title"],
                    "duration_seconds": int(video.get("duration_seconds") or 0),
                }
            )
            stats["videos_selected_for_transcript_fetch"] += 1

    return tasks, stats


def _prefetch_transcripts(tasks: list[dict]) -> tuple[list[dict], dict]:
    ready_tasks: list[dict] = []
    timings = []
    failures = []

    for idx, task in enumerate(tasks, start=1):
        start = time.perf_counter()
        transcript_result = get_transcript(task["video_id"])
        elapsed = time.perf_counter() - start
        timings.append(elapsed)

        if not transcript_result.get("success"):
            failures.append(
                {
                    "video_id": task["video_id"],
                    "title": task["title"],
                    "error": transcript_result.get("error"),
                    "fetch_seconds": round(elapsed, 3),
                }
            )
            continue

        segments = transcript_result.get("segments") or []
        if not segments:
            failures.append(
                {
                    "video_id": task["video_id"],
                    "title": task["title"],
                    "error": "Empty transcript",
                    "fetch_seconds": round(elapsed, 3),
                }
            )
            continue

        task_copy = dict(task)
        task_copy["transcript_segments"] = segments
        task_copy["transcript_language"] = transcript_result.get("language_code")
        task_copy["transcript_segment_count"] = len(segments)
        task_copy["transcript_fetch_seconds"] = elapsed
        ready_tasks.append(task_copy)
        print(
            f"[transcript {idx}/{len(tasks)}] ok {task['video_id']} "
            f"segments={len(segments)} time={elapsed:.1f}s"
        )

    timing_summary = {
        "count": len(timings),
        "success_count": len(ready_tasks),
        "failure_count": len(failures),
        "avg_seconds": round(sum(timings) / len(timings), 3) if timings else None,
        "median_seconds": round(statistics.median(timings), 3) if timings else None,
        "p95_seconds": round(sorted(timings)[max(0, int(len(timings) * 0.95) - 1)], 3) if timings else None,
    }
    return ready_tasks, {"timing": timing_summary, "failures": failures}


def _looks_like_rate_limit(err: str | None) -> bool:
    if not err:
        return False
    t = str(err).lower()
    return "rate limit" in t or "429" in t or "tokens per min" in t or "requests per min" in t


def _eval_one(task: dict) -> dict:
    start = time.perf_counter()
    try:
        result = evaluate_video(
            video_id=task["video_id"],
            channel_id=task["channel_id"],
            title=task["title"],
            duration_seconds=task["duration_seconds"],
            transcript_segments=task["transcript_segments"],
        )
        elapsed = time.perf_counter() - start
        return {
            "video_id": task["video_id"],
            "title": task["title"],
            "elapsed_seconds": elapsed,
            "success": bool(result.get("evaluation_success")),
            "error": result.get("error"),
            "route": result.get("evaluation_route"),
        }
    except Exception as e:
        elapsed = time.perf_counter() - start
        return {
            "video_id": task["video_id"],
            "title": task["title"],
            "elapsed_seconds": elapsed,
            "success": False,
            "error": f"probe_exception: {e}",
            "route": None,
        }


def _run_worker_level(tasks: list[dict], workers: int) -> dict:
    start = time.perf_counter()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_eval_one, task) for task in tasks]
        for i, fut in enumerate(concurrent.futures.as_completed(futures), start=1):
            item = fut.result()
            results.append(item)
            status = "ok" if item["success"] else "fail"
            print(f"[workers={workers}] {i}/{len(tasks)} {status} {item['video_id']} {item['elapsed_seconds']:.1f}s")

    elapsed = time.perf_counter() - start
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    rate_limited = [r for r in failures if _looks_like_rate_limit(r.get("error"))]
    eval_times = [r["elapsed_seconds"] for r in results]

    return {
        "workers": workers,
        "video_count": len(tasks),
        "elapsed_seconds": round(elapsed, 3),
        "videos_per_minute": round((len(tasks) / elapsed) * 60, 2) if elapsed > 0 else None,
        "success_count": len(successes),
        "failure_count": len(failures),
        "rate_limit_failure_count": len(rate_limited),
        "avg_eval_seconds": round(sum(eval_times) / len(eval_times), 3) if eval_times else None,
        "median_eval_seconds": round(statistics.median(eval_times), 3) if eval_times else None,
        "routes": {
            "two_call_parallel": sum(1 for r in results if r.get("route") == "two_call_parallel"),
            "chunked_full_transcript": sum(1 for r in results if r.get("route") == "chunked_full_transcript"),
            "unknown": sum(1 for r in results if not r.get("route")),
        },
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description="Probe practical worker limit with real LLM evaluations")
    parser.add_argument(
        "--channels-file",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "channels_final.json",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "anticlickbait.db",
    )
    parser.add_argument("--max-channels", type=int, default=20, help="How many curated channels to scan for sample tasks")
    parser.add_argument("--max-videos-per-channel", type=int, default=8, help="Fetch up to N videos/channel while building sample")
    parser.add_argument("--max-sample-videos", type=int, default=8, help="How many transcripts/evals to run per worker level")
    parser.add_argument(
        "--worker-levels",
        type=int,
        nargs="+",
        default=[1, 2, 4, 8],
        help="Worker counts to test (each worker evaluates one video at a time; each video internally makes up to 2 LLM calls)",
    )
    parser.add_argument(
        "--long-form-video-duration-seconds",
        type=int,
        default=5400,
        help="Skip videos longer than this while probing",
    )
    parser.add_argument(
        "--long-form-channel-skip-ratio",
        type=float,
        default=0.5,
        help="Skip channels where > this ratio of fetched videos exceed long-form threshold",
    )
    parser.add_argument(
        "--allow-already-evaluated",
        action="store_true",
        help="Allow reusing videos already in DB if sample is hard to fill",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "benchmarks" / "worker_limit_probe.json",
    )

    args = parser.parse_args()
    args.output_json.parent.mkdir(parents=True, exist_ok=True)

    print("Collecting sample tasks...")
    tasks, sample_stats = _collect_sample_tasks(
        channels_file=args.channels_file,
        db_path=args.db_path,
        max_channels=args.max_channels,
        max_videos_per_channel=args.max_videos_per_channel,
        max_sample_videos=args.max_sample_videos,
        long_form_duration_seconds=args.long_form_video_duration_seconds,
        long_form_channel_skip_ratio=args.long_form_channel_skip_ratio,
        allow_already_evaluated=args.allow_already_evaluated,
    )
    print(json.dumps(sample_stats, indent=2))
    if not tasks:
        raise SystemExit("No sample tasks found. Try --allow-already-evaluated or increase max channels/videos.")

    print(f"\nPrefetching transcripts for {len(tasks)} videos...")
    ready_tasks, transcript_summary = _prefetch_transcripts(tasks)
    print(json.dumps(transcript_summary["timing"], indent=2))
    if transcript_summary["failures"]:
        print("Transcript failures:")
        print(json.dumps(transcript_summary["failures"], indent=2))

    if not ready_tasks:
        raise SystemExit("No transcripts fetched successfully for probe.")

    runs = []
    print(f"\nRunning worker levels on {len(ready_tasks)} ready tasks...")
    for workers in args.worker_levels:
        print(f"\n=== Worker probe: {workers} workers ===")
        run_summary = _run_worker_level(ready_tasks, workers)
        print(json.dumps({k: v for k, v in run_summary.items() if k != "failures"}, indent=2))
        if run_summary["failures"]:
            print("Failures:")
            print(json.dumps(run_summary["failures"], indent=2))
        runs.append(run_summary)
        if run_summary["rate_limit_failure_count"] > 0:
            print(f"Stopping ramp after rate-limit failures at workers={workers}")
            break

    report = {
        "sample_stats": sample_stats,
        "transcript_summary": transcript_summary,
        "worker_runs": runs,
        "notes": {
            "worker_definition": "One worker evaluates one video concurrently; normal route internally makes 2 parallel LLM calls.",
            "rate_limit_detection": "Counts final evaluation failures whose error string contains 429/rate limit signals. Transient retries that eventually succeed are not counted.",
        },
    }

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved probe report to {args.output_json}")


if __name__ == "__main__":
    main()
