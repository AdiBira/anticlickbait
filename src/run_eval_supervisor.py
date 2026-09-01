#!/usr/bin/env python3
"""
Supervisor for repeated anticlickbait evaluation passes with auto-retry and convergence stop.

Why:
- `src/main.py` may finish a pass naturally (or exit unexpectedly) before all desired
  channels/videos are captured due skips/failures/rate limits.
- The pipeline is idempotent enough to rerun safely because successful video evaluations
  are skipped on subsequent passes.

The supervisor restarts passes until:
- max passes reached, or
- no DB progress for N consecutive passes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def db_progress(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) as total_rows,
              SUM(CASE WHEN evaluation_success=1 THEN 1 ELSE 0 END) as successful_evals,
              COUNT(DISTINCT channel_id) as channels_with_videos,
              MAX(evaluated_at) as last_eval
            FROM video_evaluations
            """
        ).fetchone()
        return {
            "total_rows": int(row[0] or 0),
            "successful_evals": int(row[1] or 0),
            "channels_with_videos": int(row[2] or 0),
            "last_eval": row[3],
        }
    finally:
        conn.close()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_recent_error_summary(db_path: Path, since_ts: str | None) -> dict:
    """
    Summarize recent failure modes since a timestamp.
    """
    conn = sqlite3.connect(db_path)
    try:
        if since_ts:
            rows = conn.execute(
                """
                SELECT COALESCE(evaluation_error, 'null') as err, COUNT(*) as c
                FROM video_evaluations
                WHERE evaluation_success=0
                  AND evaluated_at IS NOT NULL
                  AND evaluated_at >= ?
                GROUP BY err
                ORDER BY c DESC
                LIMIT 10
                """,
                (since_ts,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT COALESCE(evaluation_error, 'null') as err, COUNT(*) as c
                FROM video_evaluations
                WHERE evaluation_success=0
                GROUP BY err
                ORDER BY c DESC
                LIMIT 10
                """
            ).fetchall()
        top = [{"error": r[0], "count": int(r[1] or 0)} for r in rows]
        insufficient = sum(x["count"] for x in top if "insufficient_quota" in (x["error"] or ""))
        return {
            "top_errors": top,
            "insufficient_quota_count": insufficient,
        }
    finally:
        conn.close()


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def count_youtube_quota_errors(path: Path, start_offset: int) -> int:
    if not path.exists():
        return 0
    try:
        with open(path, "rb") as f:
            f.seek(max(0, int(start_offset or 0)))
            chunk = f.read().decode("utf-8", errors="ignore").lower()
    except Exception:
        return 0
    return chunk.count("quotaexceeded") + chunk.count("youtube.quota")


def main():
    parser = argparse.ArgumentParser(description="Auto-retry supervisor for anticlickbait evaluation pipeline")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "anticlickbait.db",
    )
    parser.add_argument(
        "--idle-pass-threshold",
        type=int,
        default=2,
        help="Stop after this many consecutive passes with no increase in successful_evals. Set 0 to disable idle-stop.",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=20,
        help="Safety cap on total passes",
    )
    parser.add_argument(
        "--restart-delay-seconds",
        type=int,
        default=10,
        help="Delay before restarting after a pass exits",
    )
    parser.add_argument(
        "--quota-retry-delay-seconds",
        type=int,
        default=300,
        help="Delay after a pass with detected insufficient_quota failures",
    )
    parser.add_argument(
        "--youtube-quota-retry-delay-seconds",
        type=int,
        default=1800,
        help="Delay after a pass that shows YouTube Data API quotaExceeded errors",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "logs",
        help="Directory for supervisor state and pass logs",
    )
    parser.add_argument(
        "pipeline_args",
        nargs=argparse.REMAINDER,
        help="Arguments to pass to src/main.py (prefix with --, e.g. -- --channels-file ...)",
    )
    args = parser.parse_args()

    pipeline_args = list(args.pipeline_args)
    if pipeline_args and pipeline_args[0] == "--":
        pipeline_args = pipeline_args[1:]

    # Sensible defaults if caller passes none.
    if not pipeline_args:
        pipeline_args = [
            "--channels-file", "data/channels_top10_per_category_youtubers.json",
            "--max-videos", "15",
            "--skip-thumbnail",
            "--workers", "2",
            "--require-transcript-language-prefixes", "en",
        ]

    python_bin = sys.executable
    main_py = Path(__file__).parent / "main.py"
    env = dict(**__import__("os").environ)
    env["TRANSCRIPT_COOKIES_BROWSER"] = ""
    env["TRANSCRIPT_PROXY"] = ""
    env.setdefault("OPENAI_MODEL", "gpt-4o-mini")
    env["PYTHONUNBUFFERED"] = "1"

    # Keep transcript stage stable under high worker counts.
    env.setdefault("TRANSCRIPT_INVIDIOUS_ENABLED", "0")
    env.setdefault("INVIDIOUS_INSTANCE_TIMEOUT_SECONDS", "3")
    env.setdefault("TRANSCRIPT_INVIDIOUS_MAX_INSTANCES", "1")
    env.setdefault("TRANSCRIPT_YTDLP_MAX_CONCURRENCY", "8")
    env.setdefault("TRANSCRIPT_YTDLP_MAX_RETRIES", "1")
    env.setdefault("TRANSCRIPT_YTDLP_RETRY_BACKOFF_SECONDS", "1")
    env.setdefault("MAX_CONSECUTIVE_TRANSCRIPT_MISSES", "2")

    log_dir = args.log_dir
    run_id = datetime.now(timezone.utc).strftime("eval_supervisor_%Y%m%dT%H%M%SZ")
    run_dir = log_dir / run_id
    events_jsonl = run_dir / "events.jsonl"
    state_json = run_dir / "state.json"
    pass_stdout_path = run_dir / "pass_stdout.log"
    pass_stderr_path = run_dir / "pass_stderr.log"

    print("=== Eval Supervisor Start ===")
    print(f"Python: {python_bin}")
    print(f"Main:   {main_py}")
    print(f"DB:     {args.db_path}")
    print(f"Args:   {' '.join(pipeline_args)}")
    print(f"Idle stop threshold: {args.idle_pass_threshold} passes")
    print(f"Max passes: {args.max_passes}")
    print(f"Log dir: {run_dir}")

    write_json(
        state_json,
        {
            "status": "starting",
            "started_at": iso_now(),
            "run_id": run_id,
            "python": python_bin,
            "main_py": str(main_py),
            "db_path": str(args.db_path),
            "pipeline_args": pipeline_args,
            "idle_pass_threshold": args.idle_pass_threshold,
            "max_passes": args.max_passes,
            "restart_delay_seconds": args.restart_delay_seconds,
            "quota_retry_delay_seconds": args.quota_retry_delay_seconds,
        },
    )
    append_jsonl(
        events_jsonl,
        {
            "ts": iso_now(),
            "event": "supervisor_start",
            "run_id": run_id,
            "db_path": str(args.db_path),
            "args": pipeline_args,
        },
    )

    idle_passes = 0
    last_success_count = None

    for pass_num in range(1, args.max_passes + 1):
        before = db_progress(args.db_path)
        before_last_eval = before.get("last_eval")
        print(f"\n=== Pass {pass_num}/{args.max_passes} starting ===")
        print(f"Before: {before}")
        append_jsonl(
            events_jsonl,
            {
                "ts": iso_now(),
                "event": "pass_start",
                "pass_num": pass_num,
                "before": before,
            },
        )
        write_json(
            state_json,
            {
                "status": "running",
                "run_id": run_id,
                "current_pass": pass_num,
                "idle_passes": idle_passes,
                "before": before,
                "updated_at": iso_now(),
            },
        )

        cmd = [python_bin, str(main_py), *pipeline_args]
        start = time.time()
        heartbeat_interval = 30
        stdout_start_offset = pass_stdout_path.stat().st_size if pass_stdout_path.exists() else 0
        stderr_start_offset = pass_stderr_path.stat().st_size if pass_stderr_path.exists() else 0
        with open(pass_stdout_path, "a", encoding="utf-8") as out, open(pass_stderr_path, "a", encoding="utf-8") as err:
            out.write(f"\n\n===== PASS {pass_num} START {iso_now()} =====\n")
            err.write(f"\n\n===== PASS {pass_num} START {iso_now()} =====\n")
            proc = subprocess.Popen(cmd, cwd=Path(__file__).parent.parent, env=env, stdout=out, stderr=err)
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                elapsed_live = time.time() - start
                write_json(
                    state_json,
                    {
                        "status": "running",
                        "run_id": run_id,
                        "current_pass": pass_num,
                        "idle_passes": idle_passes,
                        "before": before,
                        "child_pid": proc.pid,
                        "pass_elapsed_seconds": round(elapsed_live, 2),
                        "updated_at": iso_now(),
                    },
                )
                time.sleep(heartbeat_interval)
            returncode = rc
            out.write(f"===== PASS {pass_num} END rc={returncode} {iso_now()} =====\n")
            err.write(f"===== PASS {pass_num} END rc={returncode} {iso_now()} =====\n")
        elapsed = time.time() - start

        after = db_progress(args.db_path)
        delta_success = after["successful_evals"] - before["successful_evals"]
        err_summary = db_recent_error_summary(args.db_path, before_last_eval)
        quota_hits = int(err_summary.get("insufficient_quota_count") or 0)
        yt_quota_hits = count_youtube_quota_errors(pass_stdout_path, stdout_start_offset) + count_youtube_quota_errors(pass_stderr_path, stderr_start_offset)
        print(f"Pass exit code: {returncode} | elapsed: {elapsed:.1f}s")
        print(f"After:  {after}")
        print(f"Delta successful_evals: {delta_success}")
        if quota_hits:
            print(f"Recent insufficient_quota failures this pass window: {quota_hits}")
        if yt_quota_hits:
            print(f"Detected YouTube Data API quota errors in pass logs: {yt_quota_hits}")
        append_jsonl(
            events_jsonl,
            {
                "ts": iso_now(),
                "event": "pass_end",
                "pass_num": pass_num,
                "returncode": returncode,
                "elapsed_seconds": round(elapsed, 2),
                "before": before,
                "after": after,
                "delta_success": delta_success,
                "recent_error_summary": err_summary,
                "youtube_quota_hits": yt_quota_hits,
            },
        )

        if last_success_count is None:
            last_success_count = after["successful_evals"]

        if delta_success <= 0:
            idle_passes += 1
            print(f"No progress pass count: {idle_passes}/{args.idle_pass_threshold}")
        else:
            idle_passes = 0
            last_success_count = after["successful_evals"]

        if args.idle_pass_threshold > 0 and idle_passes >= args.idle_pass_threshold:
            print("Stopping supervisor: consecutive no-progress passes reached threshold.")
            append_jsonl(
                events_jsonl,
                {
                    "ts": iso_now(),
                    "event": "supervisor_stop",
                    "reason": "idle_threshold",
                    "idle_passes": idle_passes,
                    "threshold": args.idle_pass_threshold,
                    "pass_num": pass_num,
                },
            )
            write_json(
                state_json,
                {
                    "status": "stopped",
                    "reason": "idle_threshold",
                    "run_id": run_id,
                    "pass_num": pass_num,
                    "idle_passes": idle_passes,
                    "updated_at": iso_now(),
                },
            )
            return

        if returncode == 0 and delta_success <= 0:
            print("Pass ended cleanly but no new evaluations added; likely converging.")

        delay = args.restart_delay_seconds
        if quota_hits > 0:
            delay = max(delay, args.quota_retry_delay_seconds)
            print(f"Detected OpenAI quota failures; sleeping {delay}s before retry...")
        if yt_quota_hits > 0:
            delay = max(delay, args.youtube_quota_retry_delay_seconds)
            print(f"Detected YouTube API quota failures; sleeping {delay}s before retry...")
        if quota_hits <= 0 and yt_quota_hits <= 0:
            print(f"Sleeping {delay}s before next pass...")
        write_json(
            state_json,
            {
                "status": "sleeping",
                "run_id": run_id,
                "current_pass": pass_num,
                "idle_passes": idle_passes,
                "sleep_seconds": delay,
                "updated_at": iso_now(),
            },
        )
        time.sleep(delay)

    print("Stopping supervisor: max passes reached.")
    append_jsonl(
        events_jsonl,
        {
            "ts": iso_now(),
            "event": "supervisor_stop",
            "reason": "max_passes",
            "max_passes": args.max_passes,
        },
    )
    write_json(
        state_json,
        {
            "status": "stopped",
            "reason": "max_passes",
            "run_id": run_id,
            "max_passes": args.max_passes,
            "updated_at": iso_now(),
        },
    )


if __name__ == "__main__":
    main()
