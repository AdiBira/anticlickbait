#!/usr/bin/env python3
"""
Export evaluation status pivots for the current target channel list.

Outputs:
- category-level frequency pivot CSV
- channel-level pivot CSV (names + metadata + video counts)
- summary JSON
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _norm_lang(code: str | None) -> str | None:
    if not code:
        return None
    return str(code).strip().lower()


def _primary_language(lang_counter: Counter) -> str | None:
    if not lang_counter:
        return None
    # deterministic tie-break: highest count, then alphabetically
    return sorted(lang_counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def main():
    parser = argparse.ArgumentParser(description="Export eval status pivot tables for target channels")
    parser.add_argument(
        "--channels-file",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "channels_top10_per_category_youtubers.json",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "anticlickbait.db",
    )
    parser.add_argument(
        "--target-videos-per-channel",
        type=int,
        default=15,
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "benchmarks",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(args.channels_file.read_text(encoding="utf-8"))
    target_channels = payload.get("channels", [])
    now_iso = datetime.now(timezone.utc).isoformat()

    target_by_id = {}
    target_by_category: dict[str, list[dict]] = defaultdict(list)
    for ch in target_channels:
        cid = ch.get("channel_id")
        if not cid:
            continue
        target_by_id[cid] = ch
        target_by_category[ch.get("category") or "Uncategorized"].append(ch)

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row

    # Channels table rows for target list.
    channel_rows: dict[str, sqlite3.Row] = {}
    if target_by_id:
        placeholders = ",".join(["?"] * len(target_by_id))
        for row in conn.execute(
            f"SELECT * FROM channels WHERE channel_id IN ({placeholders})",
            list(target_by_id.keys()),
        ):
            channel_rows[row["channel_id"]] = row

    # Video rows aggregated per target channel.
    video_rows_by_channel: dict[str, list[sqlite3.Row]] = defaultdict(list)
    if target_by_id:
        placeholders = ",".join(["?"] * len(target_by_id))
        for row in conn.execute(
            f"""
            SELECT
              channel_id, video_id, title, category_id, duration_seconds,
              evaluation_success, evaluation_error, video_score,
              transcript_language, evaluated_at
            FROM video_evaluations
            WHERE channel_id IN ({placeholders})
            """,
            list(target_by_id.keys()),
        ):
            video_rows_by_channel[row["channel_id"]].append(row)

    conn.close()

    channel_pivot_rows = []
    category_acc: dict[str, dict] = defaultdict(
        lambda: {
            "listed_channels": 0,
            "channels_in_db": 0,
            "channels_with_video_rows": 0,
            "scored_channels": 0,
            "channel_rows_missing": 0,
            "total_video_rows": 0,
            "successful_videos": 0,
            "failed_video_rows": 0,
            "pending_target_slots": 0,
            "lang_counter": Counter(),
            "subscriber_sum_known": 0,
            "subscriber_count_channels_known": 0,
            "names": [],
        }
    )

    for category in sorted(target_by_category):
        for target in sorted(
            target_by_category[category],
            key=lambda x: (x.get("source_rank", 10**9), x.get("title", "")),
        ):
            cid = target["channel_id"]
            crow = channel_rows.get(cid)
            vrows = video_rows_by_channel.get(cid, [])
            success_rows = [r for r in vrows if int(r["evaluation_success"] or 0) == 1]
            fail_rows = [r for r in vrows if int(r["evaluation_success"] or 0) != 1]

            lang_counter = Counter(_norm_lang(r["transcript_language"]) for r in success_rows if _norm_lang(r["transcript_language"]))
            primary_lang = _primary_language(lang_counter)
            lang_mix = ", ".join(f"{k}:{v}" for k, v in sorted(lang_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5])

            channel_score = crow["channel_score"] if crow is not None else None
            videos_evaluated = int(crow["videos_evaluated"] or 0) if crow is not None else 0
            successful_videos = len(success_rows)
            pending_slots = max(0, args.target_videos_per_channel - successful_videos)

            status_tags = []
            if crow is None:
                status_tags.append("missing_channel_row")
            if crow is not None and not vrows:
                status_tags.append("no_video_rows")
            if successful_videos == 0 and vrows:
                status_tags.append("no_successful_videos")
            if successful_videos < args.target_videos_per_channel:
                status_tags.append("target_not_reached")

            # Pull a readable handle to verify final list quality.
            display_handle = target.get("handle") or (f"@{crow['custom_url']}" if crow and crow["custom_url"] else None)
            subscriber_count = None
            if crow is not None and crow["subscriber_count"] is not None:
                subscriber_count = int(crow["subscriber_count"])
            elif target.get("resolved_subscriber_count") is not None:
                subscriber_count = int(target["resolved_subscriber_count"])

            row = {
                "category": category,
                "source_rank": target.get("source_rank"),
                "channel_id": cid,
                "target_title": target.get("title"),
                "handle": display_handle,
                "country": (crow["country"] if crow is not None else None) or target.get("country"),
                "subscriber_count": subscriber_count,
                "channel_language_field": crow["language"] if crow is not None else None,
                "primary_transcript_language": primary_lang,
                "transcript_language_mix": lang_mix,
                "channel_row_present": 1 if crow is not None else 0,
                "channel_score_present": 1 if (crow is not None and crow["channel_score"] is not None) else 0,
                "channel_score": round(float(channel_score), 4) if channel_score is not None else None,
                "videos_evaluated_field": videos_evaluated,
                "video_rows_total": len(vrows),
                "successful_videos": successful_videos,
                "failed_video_rows": len(fail_rows),
                "target_video_slots": args.target_videos_per_channel,
                "pending_target_slots": pending_slots,
                "last_video_evaluated_at": max((r["evaluated_at"] for r in vrows if r["evaluated_at"]), default=None),
                "last_channel_evaluated_at": crow["last_evaluated_at"] if crow is not None else None,
                "status_tags": "|".join(status_tags),
            }
            channel_pivot_rows.append(row)

            acc = category_acc[category]
            acc["listed_channels"] += 1
            acc["names"].append(f"{target.get('title')} ({display_handle or cid})")
            if crow is not None:
                acc["channels_in_db"] += 1
            else:
                acc["channel_rows_missing"] += 1
            if vrows:
                acc["channels_with_video_rows"] += 1
            if crow is not None and crow["channel_score"] is not None:
                acc["scored_channels"] += 1
            acc["total_video_rows"] += len(vrows)
            acc["successful_videos"] += successful_videos
            acc["failed_video_rows"] += len(fail_rows)
            acc["pending_target_slots"] += pending_slots
            acc["lang_counter"].update(lang_counter)
            if subscriber_count is not None:
                acc["subscriber_sum_known"] += subscriber_count
                acc["subscriber_count_channels_known"] += 1

    category_pivot_rows = []
    for category in sorted(category_acc):
        acc = category_acc[category]
        target_slots = acc["listed_channels"] * args.target_videos_per_channel
        completion_pct = (acc["successful_videos"] / target_slots * 100.0) if target_slots else 0.0
        primary_lang = _primary_language(acc["lang_counter"])
        category_pivot_rows.append(
            {
                "category": category,
                "listed_channels": acc["listed_channels"],
                "channels_in_db": acc["channels_in_db"],
                "channels_with_video_rows": acc["channels_with_video_rows"],
                "scored_channels": acc["scored_channels"],
                "channel_rows_missing": acc["channel_rows_missing"],
                "target_video_slots": target_slots,
                "video_rows_total": acc["total_video_rows"],
                "successful_videos": acc["successful_videos"],
                "failed_video_rows": acc["failed_video_rows"],
                "pending_target_slots": acc["pending_target_slots"],
                "completion_pct_of_target_slots": round(completion_pct, 2),
                "avg_successful_videos_per_listed_channel": round(acc["successful_videos"] / acc["listed_channels"], 2) if acc["listed_channels"] else 0.0,
                "avg_subscriber_count_known": (
                    round(acc["subscriber_sum_known"] / acc["subscriber_count_channels_known"])
                    if acc["subscriber_count_channels_known"]
                    else None
                ),
                "primary_transcript_language_mode": primary_lang,
                "top_transcript_languages": ", ".join(
                    f"{lang}:{count}" for lang, count in sorted(acc["lang_counter"].items(), key=lambda kv: (-kv[1], kv[0]))[:8]
                ),
                "channel_names_for_verification": " || ".join(acc["names"]),
            }
        )

    total_target_slots = len(target_by_id) * args.target_videos_per_channel
    total_success = sum(r["successful_videos"] for r in category_pivot_rows)
    summary = {
        "generated_at": now_iso,
        "channels_file": str(args.channels_file),
        "db_path": str(args.db_path),
        "target_channels": len(target_by_id),
        "target_videos_per_channel": args.target_videos_per_channel,
        "target_video_slots": total_target_slots,
        "successful_video_evals_in_target_list": total_success,
        "pending_target_slots": max(0, total_target_slots - total_success),
        "completion_pct_of_target_slots": round((total_success / total_target_slots * 100.0), 2) if total_target_slots else 0.0,
        "note": "Pending target slots are relative to target list × max-videos, not adjusted for long-form skips, transcript failures, or language filtering.",
    }

    category_csv = args.out_dir / "eval_status_category_pivot.csv"
    channel_csv = args.out_dir / "eval_status_channel_pivot.csv"
    summary_json = args.out_dir / "eval_status_summary.json"

    if category_pivot_rows:
        with open(category_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(category_pivot_rows[0].keys()))
            w.writeheader()
            w.writerows(category_pivot_rows)

    if channel_pivot_rows:
        with open(channel_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(channel_pivot_rows[0].keys()))
            w.writeheader()
            w.writerows(channel_pivot_rows)

    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"Wrote category pivot: {category_csv}")
    print(f"Wrote channel pivot: {channel_csv}")
    print(f"Wrote summary JSON:  {summary_json}")


if __name__ == "__main__":
    main()
