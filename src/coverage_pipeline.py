#!/usr/bin/env python3
"""
Deterministic target/coverage pipeline for locked 15-video channel evaluation.

Stages:
- targets-build: create immutable 15-video target manifest with locked latest/popular buckets.
- subtitles-fetch: fetch original subtitles for manifest videos with auditable attempts.
- coverage-report: compute strict coverage/ratio integrity + ETA model.
- coverage-check: hard release gate; fails if any channel is not fully compliant.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import MIN_VIDEO_DURATION_SECONDS, VIDEO_SELECTION_LATEST_RATIO
from database import (
    DEFAULT_DB_PATH,
    get_connection,
    init_database,
    log_subtitle_attempt,
    upsert_channel_coverage_status,
    upsert_subtitle_resolution,
)
from transcript_fetcher import fetch_transcript_from_source
from youtube_api import get_channel_info_by_handle, get_channel_video_pool


PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TARGETS_DIR = DATA_DIR / "targets"
COVERAGE_DIR = DATA_DIR / "coverage"
SUBTITLES_DIR = DATA_DIR / "subtitles"
DEFAULT_MANIFEST_PATH = TARGETS_DIR / "manifest_v1.jsonl"
DEFAULT_MANIFEST_SUMMARY_PATH = TARGETS_DIR / "manifest_summary.json"
DEFAULT_REPLACEMENTS_PATH = TARGETS_DIR / "manifest_replacements_v1.jsonl"
DEFAULT_COVERAGE_REPORT_PATH = COVERAGE_DIR / "coverage_report.json"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


SELECTION_RULE_VERSION = "v1_locked_15_latest8_popular7_dedupe_same_bucket_first"
TERMINAL_FAILURE_CODES = {"video_unavailable", "no_transcript_available", "no_requested_language"}
RETRY_EXHAUSTION_ATTEMPTS = _env_int("SUBTITLE_RETRY_EXHAUSTION_ATTEMPTS", 12, minimum=1)
RATE_LIMIT_PROMOTION_ATTEMPTS = _env_int("SUBTITLE_RATE_LIMIT_PROMOTION_ATTEMPTS", 4, minimum=1)
BACKFILL_HYDRATE_MAX_VIDEOS = _env_int("SUBTITLE_BACKFILL_HYDRATE_MAX_VIDEOS", 240, minimum=120)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def _load_channels(channels_file: Path) -> list[dict]:
    payload = _read_json(channels_file)
    channels = payload.get("channels", [])
    if not isinstance(channels, list):
        return []
    return channels


def _published_key(video: dict) -> str:
    return video.get("published_at") or ""


def _build_locked_targets_for_pool(
    pool: list[dict],
    *,
    channel_id: str,
    handle: str,
    category: str | None,
    target_total: int,
    latest_target: int,
    popular_target: int,
    selection_rule_version: str,
) -> tuple[list[dict], dict]:
    """
    Deterministic latest+popular target selection with transparent replacement logs.
    """
    latest_sorted = sorted(pool, key=_published_key, reverse=True)
    popular_sorted = sorted(
        pool,
        key=lambda v: (_safe_int(v.get("view_count"), 0), _published_key(v)),
        reverse=True,
    )

    selected: list[dict] = []
    seen: set[str] = set()
    replacement_decisions: list[dict] = []

    def _pick(
        *,
        bucket: str,
        queue: list[dict],
        take_n: int,
        reason: str | None,
    ) -> int:
        added = 0
        for idx, item in enumerate(queue, start=1):
            vid = item.get("video_id")
            if not vid or vid in seen:
                continue
            selected.append(
                {
                    "channel_id": channel_id,
                    "handle": handle,
                    "category": category,
                    "video_id": vid,
                    "title": item.get("title"),
                    "published_at": item.get("published_at"),
                    "view_count": _safe_int(item.get("view_count"), 0),
                    "duration_seconds": _safe_int(item.get("duration_seconds"), 0),
                    "bucket": bucket,
                    "bucket_rank": idx,
                    "selected_rank": len(selected) + 1,
                    "selection_rule_version": selection_rule_version,
                    "replacement_reason": reason,
                }
            )
            seen.add(vid)
            added += 1
            if added >= take_n:
                break
        return added

    _pick(bucket="latest", queue=latest_sorted, take_n=latest_target, reason=None)

    popular_added = _pick(bucket="popular", queue=popular_sorted, take_n=popular_target, reason=None)
    missing_popular = max(0, popular_target - popular_added)
    if missing_popular:
        replacement_decisions.append(
            {
                "kind": "popular_shortfall",
                "missing_count": missing_popular,
                "resolution": "filled_from_latest_then_popular",
            }
        )

    if len(selected) < target_total:
        _pick(
            bucket="latest",
            queue=latest_sorted,
            take_n=target_total - len(selected),
            reason="post_bucket_fill_latest",
        )
    if len(selected) < target_total:
        _pick(
            bucket="popular",
            queue=popular_sorted,
            take_n=target_total - len(selected),
            reason="post_bucket_fill_popular",
        )

    selected_ids = {row["video_id"] for row in selected}
    latest_backfill_queue = []
    popular_backfill_queue = []

    for idx, item in enumerate(latest_sorted, start=1):
        vid = item.get("video_id")
        if not vid or vid in selected_ids:
            continue
        latest_backfill_queue.append(
            {
                "video_id": vid,
                "title": item.get("title"),
                "published_at": item.get("published_at"),
                "view_count": _safe_int(item.get("view_count"), 0),
                "duration_seconds": _safe_int(item.get("duration_seconds"), 0),
                "bucket_rank": idx,
                "bucket": "latest",
            }
        )

    for idx, item in enumerate(popular_sorted, start=1):
        vid = item.get("video_id")
        if not vid or vid in selected_ids:
            continue
        popular_backfill_queue.append(
            {
                "video_id": vid,
                "title": item.get("title"),
                "published_at": item.get("published_at"),
                "view_count": _safe_int(item.get("view_count"), 0),
                "duration_seconds": _safe_int(item.get("duration_seconds"), 0),
                "bucket_rank": idx,
                "bucket": "popular",
            }
        )

    latest_count = sum(1 for row in selected if row["bucket"] == "latest")
    popular_count = sum(1 for row in selected if row["bucket"] == "popular")

    meta = {
        "pool_size": len(pool),
        "target_total": target_total,
        "required_latest": latest_target,
        "required_popular": popular_target,
        "selected_total": len(selected),
        "selected_latest": latest_count,
        "selected_popular": popular_count,
        "ratio_drift": abs(latest_count - latest_target) + abs(popular_count - popular_target),
        "replacement_decisions": replacement_decisions,
        "latest_backfill_queue": latest_backfill_queue,
        "popular_backfill_queue": popular_backfill_queue,
    }
    return selected[:target_total], meta


def _resolve_channel_seed(channel: dict) -> dict | None:
    handle = channel.get("handle")
    if channel.get("channel_id"):
        return {
            "channel_id": channel.get("channel_id"),
            "handle": handle,
            "title": channel.get("title") or handle,
            "category": channel.get("category"),
            "country": channel.get("country"),
            "subscriber_count": _safe_int(channel.get("subscriber_count"), 0),
        }

    if not handle:
        return None
    info = get_channel_info_by_handle(handle)
    if not info:
        return None
    return {
        "channel_id": info.get("channel_id"),
        "handle": handle,
        "title": info.get("title") or handle,
        "category": channel.get("category"),
        "country": channel.get("country") or info.get("country"),
        "subscriber_count": _safe_int(channel.get("subscriber_count") or info.get("subscriber_count"), 0),
    }


def _compute_bucket_targets(target_total: int, latest_ratio: float) -> tuple[int, int]:
    latest_target = int(math.ceil(target_total * latest_ratio))
    latest_target = min(target_total, max(1, latest_target))
    popular_target = max(0, target_total - latest_target)
    return latest_target, popular_target


def build_targets_manifest(
    *,
    channels_file: Path,
    manifest_path: Path,
    summary_path: Path,
    target_videos: int,
    latest_ratio: float,
    min_duration_seconds: int,
    selection_rule_version: str,
) -> dict:
    channels = _load_channels(channels_file)
    latest_target, popular_target = _compute_bucket_targets(target_videos, latest_ratio)

    rows_out: list[dict] = []
    summary_channels: dict[str, dict] = {}

    for idx, channel in enumerate(channels, start=1):
        seed = _resolve_channel_seed(channel)
        if not seed or not seed.get("channel_id"):
            continue

        channel_id = seed["channel_id"]
        handle = seed.get("handle") or channel.get("handle")
        category = seed.get("category")

        pool = get_channel_video_pool(
            channel_id,
            max_videos=max(target_videos * 5, 60),
            min_duration_seconds=min_duration_seconds,
        )
        # If strict duration filter cannot supply enough candidates,
        # relax only for manifest-fill coverage recovery (Shorts are still filtered upstream).
        if len(pool) < target_videos:
            relaxed_pool = get_channel_video_pool(
                channel_id,
                max_videos=max(target_videos * 8, 120),
                min_duration_seconds=max(61, min_duration_seconds // 2),
            )
            if relaxed_pool:
                seen_ids = {v.get("video_id") for v in pool if v.get("video_id")}
                for item in relaxed_pool:
                    vid = item.get("video_id")
                    if not vid or vid in seen_ids:
                        continue
                    seen_ids.add(vid)
                    pool.append(item)

        selected_rows, meta = _build_locked_targets_for_pool(
            pool,
            channel_id=channel_id,
            handle=handle,
            category=category,
            target_total=target_videos,
            latest_target=latest_target,
            popular_target=popular_target,
            selection_rule_version=selection_rule_version,
        )

        for row in selected_rows:
            row["channel_title"] = seed.get("title")
            row["country"] = seed.get("country")
            row["subscriber_count"] = _safe_int(seed.get("subscriber_count"), 0)
            row["manifest_generated_at"] = _utc_now()
            rows_out.append(row)

        summary_channels[channel_id] = {
            "channel_id": channel_id,
            "handle": handle,
            "channel_title": seed.get("title"),
            "category": category,
            "country": seed.get("country"),
            "subscriber_count": _safe_int(seed.get("subscriber_count"), 0),
            "required_videos": target_videos,
            "required_latest": latest_target,
            "required_popular": popular_target,
            "selected_videos": len(selected_rows),
            "selected_video_ids": [r["video_id"] for r in selected_rows],
            "ratio_drift": meta["ratio_drift"],
            "pool_size": meta["pool_size"],
            "replacement_decisions": meta["replacement_decisions"],
            "backfill_by_bucket": {
                "latest": meta["latest_backfill_queue"],
                "popular": meta["popular_backfill_queue"],
            },
        }

        if idx % 20 == 0:
            print(f"targets-build: processed {idx}/{len(channels)} channels")

    rows_out = sorted(rows_out, key=lambda r: (r.get("category") or "", r.get("handle") or "", int(r.get("selected_rank") or 0)))
    _write_jsonl(manifest_path, rows_out)

    channels_total = len(summary_channels)
    channels_full = sum(1 for c in summary_channels.values() if c.get("selected_videos") == target_videos)

    summary_payload = {
        "generated_at": _utc_now(),
        "channels_file": str(channels_file),
        "manifest_path": str(manifest_path),
        "selection_rule_version": selection_rule_version,
        "target_videos_per_channel": target_videos,
        "required_latest": latest_target,
        "required_popular": popular_target,
        "channels_total": channels_total,
        "channels_with_full_targets": channels_full,
        "channels_underfilled": channels_total - channels_full,
        "rows_written": len(rows_out),
        "channels": summary_channels,
    }
    _write_json(summary_path, summary_payload)

    print(f"targets-build complete: rows={len(rows_out)} channels={channels_total} full={channels_full}")
    print(f"manifest: {manifest_path}")
    print(f"summary:  {summary_path}")
    return summary_payload


def _hash_transcript_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _video_status_map(video_ids: list[str], db_path: Path) -> dict[str, dict]:
    if not video_ids:
        return {}
    unique_ids = sorted(set(video_ids))
    conn = get_connection(db_path)
    try:
        placeholders = ",".join(["?"] * len(unique_ids))
        rows = conn.execute(
            f"""
            SELECT video_id, status, winning_source, language_code, fetched_at,
                   content_hash, attempts, last_failure_code, last_failure_detail
            FROM subtitle_resolution
            WHERE video_id IN ({placeholders})
            """,
            unique_ids,
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, dict] = {}
    for row in rows:
        out[row["video_id"]] = dict(row)
    return out


def _video_attempt_count_map(video_ids: list[str], db_path: Path) -> dict[str, int]:
    if not video_ids:
        return {}
    unique_ids = sorted(set(video_ids))
    conn = get_connection(db_path)
    try:
        placeholders = ",".join(["?"] * len(unique_ids))
        rows = conn.execute(
            f"""
            SELECT video_id, COUNT(*) AS n
            FROM subtitle_attempts
            WHERE video_id IN ({placeholders})
            GROUP BY video_id
            """,
            unique_ids,
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, int] = {}
    for row in rows:
        out[row["video_id"]] = _safe_int(row["n"], 0)
    return out


def _next_attempt_no(video_id: str, db_path: Path) -> int:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM subtitle_attempts WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        return _safe_int(row["n"] if row else 0, 0) + 1
    finally:
        conn.close()


def _persist_transcript_artifact(video_id: str, channel_id: str, result: dict) -> str:
    SUBTITLES_DIR.mkdir(parents=True, exist_ok=True)
    transcript_text = result.get("transcript_text") or ""
    content_hash = _hash_transcript_text(transcript_text)
    payload = {
        "video_id": video_id,
        "channel_id": channel_id,
        "source": result.get("source"),
        "auth_mode": result.get("auth_mode"),
        "language": result.get("language"),
        "language_code": result.get("language_code"),
        "is_generated": result.get("is_generated"),
        "fetched_at": _utc_now(),
        "content_hash": content_hash,
        "transcript_text": transcript_text,
        "segments": result.get("segments") or [],
    }
    out_path = SUBTITLES_DIR / f"{video_id}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    return content_hash


def _attempt_subtitle_resolution(
    row: dict,
    *,
    source_tiers: list[str],
    retries_per_source: int,
    language_codes: list[str],
    auth_cookies_file: str | None,
    auth_cookies_browser: str | None,
    db_path: Path,
) -> tuple[str, dict]:
    """
    Returns tuple(status, detail):
      - status: resolved | unresolved
      - detail: metadata for reporting/replacements
    """
    video_id = row.get("video_id")
    channel_id = row.get("channel_id")
    if not video_id:
        return "unresolved", {"reason": "missing_video_id"}

    attempt_no = _next_attempt_no(video_id, db_path)
    attempts_used = 0
    last_failure_code = None
    last_failure_detail = None
    strongest_failure_code = None
    strongest_failure_detail = None
    terminal_stop = False

    def _failure_priority(code: str | None) -> int:
        if code == "video_unavailable":
            return 5
        if code in {"no_transcript_available", "no_requested_language"}:
            return 4
        if code == "rate_limited":
            return 3
        if code:
            return 2
        return 1

    for source in source_tiers:
        for _ in range(max(1, retries_per_source)):
            started_at = _utc_now()
            result = fetch_transcript_from_source(
                video_id,
                source,
                language_codes,
                auth_cookies_file=auth_cookies_file,
                auth_cookies_browser=auth_cookies_browser,
            )
            ended_at = _utc_now()
            success = bool(result.get("success"))
            attempts_used += 1

            content_hash = _hash_transcript_text(result.get("transcript_text") or "") if success else None
            log_subtitle_attempt(
                video_id=video_id,
                channel_id=channel_id,
                attempt_no=attempt_no,
                source=source,
                auth_mode=result.get("auth_mode") or "public",
                started_at=started_at,
                ended_at=ended_at,
                success=success,
                failure_code=None if success else result.get("error_code"),
                failure_detail=None if success else result.get("error"),
                transcript_language=result.get("language_code"),
                content_hash=content_hash,
                db_path=db_path,
            )
            attempt_no += 1

            if success:
                content_hash = _persist_transcript_artifact(video_id, channel_id, result)
                upsert_subtitle_resolution(
                    video_id=video_id,
                    channel_id=channel_id,
                    status="resolved",
                    winning_source=source,
                    language_code=result.get("language_code"),
                    fetched_at=_utc_now(),
                    content_hash=content_hash,
                    attempts=attempts_used,
                    last_failure_code=None,
                    last_failure_detail=None,
                    db_path=db_path,
                )
                return "resolved", {
                    "video_id": video_id,
                    "channel_id": channel_id,
                    "source": source,
                    "attempts": attempts_used,
                    "language_code": result.get("language_code"),
                }

            last_failure_code = result.get("error_code")
            last_failure_detail = result.get("error")
            if _failure_priority(last_failure_code) >= _failure_priority(strongest_failure_code):
                strongest_failure_code = last_failure_code
                strongest_failure_detail = last_failure_detail

            # Terminal failures should stop cross-source retries for this video attempt.
            if last_failure_code in {"video_unavailable", "no_transcript_available", "no_requested_language"}:
                terminal_stop = True
                break

            # Backoff for transient rate/network failures.
            if last_failure_code in {"rate_limited"}:
                time.sleep(min(5.0, 0.5 * attempts_used))
        if terminal_stop:
            break

    upsert_subtitle_resolution(
        video_id=video_id,
        channel_id=channel_id,
        status="unresolved",
        winning_source=None,
        language_code=None,
        fetched_at=None,
        content_hash=None,
        attempts=attempts_used,
        last_failure_code=strongest_failure_code or last_failure_code,
        last_failure_detail=strongest_failure_detail or last_failure_detail,
        db_path=db_path,
    )
    return "unresolved", {
        "video_id": video_id,
        "channel_id": channel_id,
        "reason": strongest_failure_code or last_failure_code or "unknown",
        "detail": strongest_failure_detail or last_failure_detail,
        "attempts": attempts_used,
    }


def _index_manifest_by_channel(rows: list[dict]) -> dict[str, list[dict]]:
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        channel_id = row.get("channel_id")
        if not channel_id:
            continue
        by_channel[channel_id].append(row)
    for channel_id in by_channel:
        by_channel[channel_id].sort(key=lambda r: int(r.get("selected_rank") or 0))
    return by_channel


def _load_replacements_index(replacements_path: Path) -> dict[str, list[dict]]:
    rows = _read_jsonl(replacements_path)
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        channel_id = row.get("channel_id")
        if not channel_id:
            continue
        out[channel_id].append(row)
    for channel_id in out:
        out[channel_id].sort(key=lambda r: int(r.get("selected_rank") or 0))
    return out


def _promote_replacement(
    *,
    channel_id: str,
    bucket: str,
    original_video_id: str,
    manifest_rows_by_channel: dict[str, list[dict]],
    replacements_index: dict[str, list[dict]],
    summary_channels: dict[str, dict],
    replacements_path: Path,
) -> dict | None:
    channel_summary = summary_channels.get(channel_id) or {}
    backfill_map = channel_summary.get("backfill_by_bucket") or {}
    backfill = backfill_map.get(bucket) or []
    if not backfill and not channel_summary.get("_backfill_hydrated"):
        required_total = _safe_int(channel_summary.get("required_videos"), 15)
        required_latest = _safe_int(channel_summary.get("required_latest"), 8)
        required_popular = _safe_int(channel_summary.get("required_popular"), 7)
        refresh_pool = get_channel_video_pool(
            channel_id,
            max_videos=max(required_total * 8, BACKFILL_HYDRATE_MAX_VIDEOS),
            min_duration_seconds=0,
        )
        if refresh_pool:
            _rows, refreshed_meta = _build_locked_targets_for_pool(
                refresh_pool,
                channel_id=channel_id,
                handle=channel_summary.get("handle"),
                category=channel_summary.get("category"),
                target_total=required_total,
                latest_target=required_latest,
                popular_target=required_popular,
                selection_rule_version=SELECTION_RULE_VERSION,
            )
            channel_summary["backfill_by_bucket"] = {
                "latest": refreshed_meta.get("latest_backfill_queue") or [],
                "popular": refreshed_meta.get("popular_backfill_queue") or [],
            }
        channel_summary["_backfill_hydrated"] = True
        summary_channels[channel_id] = channel_summary
        backfill_map = channel_summary.get("backfill_by_bucket") or {}
        backfill = backfill_map.get(bucket) or []
    if not backfill:
        return None

    # Avoid creating multiple replacements for the same failed target slot.
    for row in replacements_index.get(channel_id, []):
        if row.get("replaces_video_id") == original_video_id:
            return None

    taken: set[str] = set()
    for row in manifest_rows_by_channel.get(channel_id, []):
        vid = row.get("video_id")
        if vid:
            taken.add(vid)
    for row in replacements_index.get(channel_id, []):
        vid = row.get("video_id")
        if vid:
            taken.add(vid)

    next_rank = 1
    all_rows = manifest_rows_by_channel.get(channel_id, []) + replacements_index.get(channel_id, [])
    if all_rows:
        next_rank = max(_safe_int(r.get("selected_rank"), 0) for r in all_rows) + 1

    for candidate in backfill:
        vid = candidate.get("video_id")
        if not vid or vid in taken:
            continue

        replacement_row = {
            "channel_id": channel_id,
            "handle": channel_summary.get("handle"),
            "channel_title": channel_summary.get("channel_title"),
            "category": channel_summary.get("category"),
            "country": channel_summary.get("country"),
            "subscriber_count": _safe_int(channel_summary.get("subscriber_count"), 0),
            "video_id": vid,
            "title": candidate.get("title"),
            "published_at": candidate.get("published_at"),
            "view_count": _safe_int(candidate.get("view_count"), 0),
            "duration_seconds": _safe_int(candidate.get("duration_seconds"), 0),
            "bucket": bucket,
            "bucket_rank": _safe_int(candidate.get("bucket_rank"), 0),
            "selected_rank": next_rank,
            "selection_rule_version": SELECTION_RULE_VERSION,
            "replacement_reason": "subtitle_unresolved_same_bucket",
            "replaces_video_id": original_video_id,
            "manifest_generated_at": _utc_now(),
        }
        _append_jsonl(replacements_path, replacement_row)
        replacements_index[channel_id].append(replacement_row)
        replacements_index[channel_id].sort(key=lambda r: int(r.get("selected_rank") or 0))
        return replacement_row

    return None


def _replacement_eligible(status_row: dict, total_attempts: int) -> bool:
    if (status_row or {}).get("status") != "unresolved":
        return False
    last_failure_code = (status_row or {}).get("last_failure_code")
    if last_failure_code in TERMINAL_FAILURE_CODES:
        return True
    if last_failure_code == "rate_limited" and total_attempts >= RATE_LIMIT_PROMOTION_ATTEMPTS:
        return True
    if total_attempts >= RETRY_EXHAUSTION_ATTEMPTS:
        return True
    return False


def fetch_subtitles_for_manifest(
    *,
    manifest_path: Path,
    summary_path: Path,
    replacements_path: Path,
    db_path: Path,
    workers: int,
    waves: int,
    wave_delay_seconds: int,
    source_tiers: list[str],
    retries_per_source: int,
    language_codes: list[str],
    allow_replacements: bool,
    auth_cookies_file: str | None,
    auth_cookies_browser: str | None,
) -> dict:
    init_database(db_path)

    summary = _read_json(summary_path)
    summary_channels = summary.get("channels", {})
    base_rows = _read_jsonl(manifest_path)
    manifest_rows_by_channel = _index_manifest_by_channel(base_rows)
    replacements_index = _load_replacements_index(replacements_path)

    run_stats = {
        "started_at": _utc_now(),
        "waves": [],
        "workers": workers,
        "source_tiers": source_tiers,
        "language_codes": language_codes,
    }

    for wave in range(1, max(1, waves) + 1):
        all_rows = base_rows + [r for rows in replacements_index.values() for r in rows]
        video_ids = [r.get("video_id") for r in all_rows if r.get("video_id")]
        status_map = _video_status_map(video_ids, db_path)
        attempt_count_map = _video_attempt_count_map(video_ids, db_path)

        terminal_promoted = 0
        if allow_replacements:
            # For already-terminal unresolved targets, promote replacement immediately
            # so retries are spent on new candidates instead of dead slots.
            for row in all_rows:
                vid = row.get("video_id")
                if not vid:
                    continue
                status_row = status_map.get(vid) or {}
                total_attempts = _safe_int(attempt_count_map.get(vid), 0)
                if not _replacement_eligible(status_row, total_attempts):
                    continue
                channel_id = row.get("channel_id")
                bucket = row.get("bucket")
                if not channel_id or bucket not in {"latest", "popular"}:
                    continue
                replacement = _promote_replacement(
                    channel_id=channel_id,
                    bucket=bucket,
                    original_video_id=vid,
                    manifest_rows_by_channel=manifest_rows_by_channel,
                    replacements_index=replacements_index,
                    summary_channels=summary_channels,
                    replacements_path=replacements_path,
                )
                if replacement:
                    terminal_promoted += 1

            if terminal_promoted:
                # Refresh with newly added replacement rows.
                all_rows = base_rows + [r for rows in replacements_index.values() for r in rows]
                video_ids = [r.get("video_id") for r in all_rows if r.get("video_id")]
                status_map = _video_status_map(video_ids, db_path)
                attempt_count_map = _video_attempt_count_map(video_ids, db_path)

        pending_rows = []
        for row in all_rows:
            vid = row.get("video_id")
            if not vid:
                continue
            status_row = status_map.get(vid) or {}
            status = status_row.get("status")
            if status == "resolved":
                continue
            total_attempts = _safe_int(attempt_count_map.get(vid), 0)
            if _replacement_eligible(status_row, total_attempts):
                # Keep these as blocked for coverage, but don't burn retries every wave.
                continue
            pending_rows.append(row)

        if not pending_rows:
            run_stats["waves"].append({"wave": wave, "pending": 0, "resolved": 0, "unresolved": 0})
            print(f"subtitles-fetch wave {wave}: no pending videos")
            break

        print(f"subtitles-fetch wave {wave}: pending={len(pending_rows)} workers={workers}")

        unresolved_rows: list[dict] = []
        resolved_count = 0

        lock = threading.Lock()

        def _task(row: dict) -> tuple[str, dict, dict]:
            status, detail = _attempt_subtitle_resolution(
                row,
                source_tiers=source_tiers,
                retries_per_source=retries_per_source,
                language_codes=language_codes,
                auth_cookies_file=auth_cookies_file,
                auth_cookies_browser=auth_cookies_browser,
                db_path=db_path,
            )
            return status, detail, row

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = [ex.submit(_task, row) for row in pending_rows]
            for fut in concurrent.futures.as_completed(futures):
                status, _detail, row = fut.result()
                if status == "resolved":
                    with lock:
                        resolved_count += 1
                else:
                    with lock:
                        unresolved_rows.append(row)

        promoted = 0
        if allow_replacements and unresolved_rows:
            unresolved_ids = [r.get("video_id") for r in unresolved_rows if r.get("video_id")]
            unresolved_status_map = _video_status_map(unresolved_ids, db_path)
            unresolved_attempt_counts = _video_attempt_count_map(unresolved_ids, db_path)
            for row in unresolved_rows:
                channel_id = row.get("channel_id")
                bucket = row.get("bucket")
                video_id = row.get("video_id")
                if not channel_id or bucket not in {"latest", "popular"} or not video_id:
                    continue
                status_row = unresolved_status_map.get(video_id) or {}
                total_attempts = _safe_int(unresolved_attempt_counts.get(video_id), 0)
                if not _replacement_eligible(status_row, total_attempts):
                    continue
                replacement = _promote_replacement(
                    channel_id=channel_id,
                    bucket=bucket,
                    original_video_id=video_id,
                    manifest_rows_by_channel=manifest_rows_by_channel,
                    replacements_index=replacements_index,
                    summary_channels=summary_channels,
                    replacements_path=replacements_path,
                )
                if replacement:
                    promoted += 1

        wave_stats = {
            "wave": wave,
            "pending": len(pending_rows),
            "resolved": resolved_count,
            "unresolved": len(unresolved_rows),
            "promoted_from_terminal": terminal_promoted,
            "promoted_replacements": promoted,
            "timestamp": _utc_now(),
        }
        run_stats["waves"].append(wave_stats)
        print(
            f"subtitles-fetch wave {wave} done: resolved={resolved_count} unresolved={len(unresolved_rows)} "
            f"promoted_replacements={promoted}"
        )

        if wave < waves and wave_delay_seconds > 0:
            time.sleep(wave_delay_seconds)

    run_stats["ended_at"] = _utc_now()
    return run_stats


def _fetch_eval_status_map(video_ids: list[str], db_path: Path) -> dict[str, dict]:
    if not video_ids:
        return {}
    unique_ids = sorted(set(video_ids))
    conn = get_connection(db_path)
    try:
        placeholders = ",".join(["?"] * len(unique_ids))
        rows = conn.execute(
            f"""
            SELECT video_id, evaluation_success, evaluated_at
            FROM video_evaluations
            WHERE video_id IN ({placeholders})
            """,
            unique_ids,
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, dict] = {}
    for row in rows:
        out[row["video_id"]] = {
            "evaluation_success": _safe_int(row["evaluation_success"], 0),
            "evaluated_at": row["evaluated_at"],
        }
    return out


def _throughput_per_minute(
    *,
    db_path: Path,
    table: str,
    ts_col: str,
    success_where: str,
    horizon_hours: int,
) -> float:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT {ts_col} AS ts
            FROM {table}
            WHERE {success_where}
              AND {ts_col} IS NOT NULL
            ORDER BY {ts_col} DESC
            LIMIT 5000
            """
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        return 0.0

    cutoff = datetime.now(UTC).timestamp() - (horizon_hours * 3600)
    times: list[float] = []
    for row in rows:
        ts = row["ts"]
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        t = dt.timestamp()
        if t >= cutoff:
            times.append(t)
    if len(times) < 2:
        return 0.0

    span_minutes = max(1.0, (max(times) - min(times)) / 60.0)
    return len(times) / span_minutes


def _build_channel_final_coverage(
    *,
    channel_id: str,
    channel_rows: list[dict],
    channel_replacements: list[dict],
    channel_summary: dict,
    subtitle_status: dict[str, dict],
) -> dict:
    required_videos = _safe_int(channel_summary.get("required_videos"), 15)
    required_latest = _safe_int(channel_summary.get("required_latest"), 8)
    required_popular = _safe_int(channel_summary.get("required_popular"), required_videos - required_latest)

    def _pick_bucket(bucket: str, need: int) -> tuple[list[dict], list[dict]]:
        rows = [r for r in (channel_rows + channel_replacements) if r.get("bucket") == bucket]
        rows.sort(key=lambda r: int(r.get("selected_rank") or 0))
        selected: list[dict] = []
        missing: list[dict] = []
        seen: set[str] = set()

        for row in rows:
            if len(selected) >= need:
                break
            vid = row.get("video_id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            status = subtitle_status.get(vid) or {}
            if status.get("status") == "resolved" and status.get("content_hash"):
                selected.append(row)
            else:
                missing.append(row)

        return selected, missing

    picked_latest, missing_latest = _pick_bucket("latest", required_latest)
    picked_popular, missing_popular = _pick_bucket("popular", required_popular)

    resolved_videos = len(picked_latest) + len(picked_popular)
    ratio_drift = abs(len(picked_latest) - required_latest) + abs(len(picked_popular) - required_popular)
    status = "pass" if resolved_videos == required_videos and ratio_drift == 0 else "blocked"

    reasons = []
    if len(picked_latest) < required_latest:
        reasons.append(f"latest_resolved={len(picked_latest)}/{required_latest}")
    if len(picked_popular) < required_popular:
        reasons.append(f"popular_resolved={len(picked_popular)}/{required_popular}")

    unresolved_rows = missing_latest + missing_popular
    unresolved_rows = unresolved_rows[: max(0, required_videos - resolved_videos)]

    return {
        "channel_id": channel_id,
        "handle": channel_summary.get("handle"),
        "channel_title": channel_summary.get("channel_title"),
        "category": channel_summary.get("category"),
        "country": channel_summary.get("country"),
        "required_videos": required_videos,
        "required_latest": required_latest,
        "required_popular": required_popular,
        "resolved_videos": resolved_videos,
        "actual_latest_count": len(picked_latest),
        "actual_popular_count": len(picked_popular),
        "ratio_drift": ratio_drift,
        "status": status,
        "fail_reasons": reasons,
        "unresolved_slots": [
            {
                "video_id": r.get("video_id"),
                "title": r.get("title"),
                "bucket": r.get("bucket"),
                "replacement_reason": r.get("replacement_reason"),
            }
            for r in unresolved_rows
        ],
    }


def build_coverage_report(
    *,
    manifest_path: Path,
    summary_path: Path,
    replacements_path: Path,
    db_path: Path,
    report_path: Path,
    strict_ratio_drift: float,
    eta_horizon_hours: int,
) -> dict:
    init_database(db_path)

    summary = _read_json(summary_path)
    manifest_rows = _read_jsonl(manifest_path)
    replacements_rows = _read_jsonl(replacements_path)

    by_channel = _index_manifest_by_channel(manifest_rows)
    replacements_by_channel: dict[str, list[dict]] = defaultdict(list)
    for row in replacements_rows:
        channel_id = row.get("channel_id")
        if channel_id:
            replacements_by_channel[channel_id].append(row)
    for channel_id in replacements_by_channel:
        replacements_by_channel[channel_id].sort(key=lambda r: int(r.get("selected_rank") or 0))

    all_video_ids = [r.get("video_id") for r in (manifest_rows + replacements_rows) if r.get("video_id")]
    subtitle_status = _video_status_map(all_video_ids, db_path)
    eval_status = _fetch_eval_status_map(all_video_ids, db_path)

    channel_reports: list[dict] = []
    failure_breakdown: Counter[str] = Counter()

    summary_channels = summary.get("channels", {})
    for channel_id, channel_summary in summary_channels.items():
        channel_rows = by_channel.get(channel_id, [])
        channel_replacements = replacements_by_channel.get(channel_id, [])
        report = _build_channel_final_coverage(
            channel_id=channel_id,
            channel_rows=channel_rows,
            channel_replacements=channel_replacements,
            channel_summary=channel_summary,
            subtitle_status=subtitle_status,
        )

        # Capture unresolved failure reasons from subtitle_resolution.
        for slot in report.get("unresolved_slots", []):
            vid = slot.get("video_id")
            status = subtitle_status.get(vid) or {}
            code = status.get("last_failure_code") or "unresolved"
            failure_breakdown[code] += 1

        upsert_channel_coverage_status(
            channel_id=channel_id,
            handle=report.get("handle"),
            required_videos=_safe_int(report.get("required_videos"), 15),
            resolved_videos=_safe_int(report.get("resolved_videos"), 0),
            actual_latest_count=_safe_int(report.get("actual_latest_count"), 0),
            actual_popular_count=_safe_int(report.get("actual_popular_count"), 0),
            ratio_drift=float(report.get("ratio_drift") or 0.0),
            status=report.get("status") or "blocked",
            details_json=json.dumps(
                {
                    "fail_reasons": report.get("fail_reasons", []),
                    "unresolved_slots": report.get("unresolved_slots", []),
                },
                ensure_ascii=True,
            ),
            db_path=db_path,
        )

        channel_reports.append(report)

    total_channels = len(channel_reports)
    passing_channels = sum(
        1
        for r in channel_reports
        if r.get("status") == "pass" and float(r.get("ratio_drift") or 0.0) <= strict_ratio_drift
    )

    total_required_slots = sum(_safe_int(r.get("required_videos"), 0) for r in channel_reports)
    total_resolved_slots = sum(_safe_int(r.get("resolved_videos"), 0) for r in channel_reports)
    pending_subtitle_slots = max(0, total_required_slots - total_resolved_slots)

    # Eval queue is defined on resolved target/replacement videos lacking successful eval.
    pending_eval_slots = 0
    resolved_target_video_ids = []
    for row in manifest_rows + replacements_rows:
        vid = row.get("video_id")
        if not vid:
            continue
        status = subtitle_status.get(vid) or {}
        if status.get("status") == "resolved" and status.get("content_hash"):
            resolved_target_video_ids.append(vid)
    for vid in set(resolved_target_video_ids):
        if _safe_int((eval_status.get(vid) or {}).get("evaluation_success"), 0) != 1:
            pending_eval_slots += 1

    subtitle_rate = _throughput_per_minute(
        db_path=db_path,
        table="subtitle_attempts",
        ts_col="ended_at",
        success_where="success = 1",
        horizon_hours=eta_horizon_hours,
    )
    eval_rate = _throughput_per_minute(
        db_path=db_path,
        table="video_evaluations",
        ts_col="evaluated_at",
        success_where="evaluation_success = 1",
        horizon_hours=eta_horizon_hours,
    )

    def _eta_minutes(pending: int, rate: float) -> float | None:
        if pending <= 0:
            return 0.0
        if rate <= 0:
            return None
        return pending / rate

    subtitle_eta = _eta_minutes(pending_subtitle_slots, subtitle_rate)
    eval_eta = _eta_minutes(pending_eval_slots, eval_rate)

    baseline_eta = None
    if subtitle_eta is not None and eval_eta is not None:
        baseline_eta = max(subtitle_eta, eval_eta)

    optimistic_eta = baseline_eta / 1.5 if baseline_eta is not None else None
    worst_eta = baseline_eta * 1.8 if baseline_eta is not None else None

    report = {
        "generated_at": _utc_now(),
        "manifest_path": str(manifest_path),
        "summary_path": str(summary_path),
        "replacements_path": str(replacements_path),
        "strict_ratio_drift": strict_ratio_drift,
        "channels_total": total_channels,
        "channels_passing": passing_channels,
        "channels_blocked": total_channels - passing_channels,
        "required_slots_total": total_required_slots,
        "resolved_slots_total": total_resolved_slots,
        "pending_subtitle_slots": pending_subtitle_slots,
        "pending_eval_slots": pending_eval_slots,
        "completion_pct": round((100.0 * total_resolved_slots / total_required_slots), 2)
        if total_required_slots
        else 0.0,
        "failure_breakdown": dict(failure_breakdown),
        "eta": {
            "horizon_hours": eta_horizon_hours,
            "subtitle_per_min": round(subtitle_rate, 4),
            "eval_per_min": round(eval_rate, 4),
            "optimistic_minutes": None if optimistic_eta is None else round(optimistic_eta, 1),
            "baseline_minutes": None if baseline_eta is None else round(baseline_eta, 1),
            "worst_minutes": None if worst_eta is None else round(worst_eta, 1),
        },
        "channels": channel_reports,
    }

    _write_json(report_path, report)
    return report


def coverage_check(
    *,
    manifest_path: Path,
    summary_path: Path,
    replacements_path: Path,
    db_path: Path,
    report_path: Path,
    strict_ratio_drift: float,
    eta_horizon_hours: int,
) -> int:
    report = build_coverage_report(
        manifest_path=manifest_path,
        summary_path=summary_path,
        replacements_path=replacements_path,
        db_path=db_path,
        report_path=report_path,
        strict_ratio_drift=strict_ratio_drift,
        eta_horizon_hours=eta_horizon_hours,
    )

    blocked_channels = []
    for channel in report.get("channels", []):
        if channel.get("status") != "pass":
            blocked_channels.append(channel)
            continue
        if float(channel.get("ratio_drift") or 0.0) > strict_ratio_drift:
            blocked_channels.append(channel)

    if blocked_channels:
        print(
            f"coverage-check FAILED: blocked_channels={len(blocked_channels)} "
            f"pending_subtitles={report.get('pending_subtitle_slots')} "
            f"pending_evals={report.get('pending_eval_slots')}"
        )
        for ch in blocked_channels[:30]:
            print(
                f"  {ch.get('handle') or ch.get('channel_id')}: "
                f"resolved={ch.get('resolved_videos')}/{ch.get('required_videos')} "
                f"latest={ch.get('actual_latest_count')}/{ch.get('required_latest')} "
                f"popular={ch.get('actual_popular_count')}/{ch.get('required_popular')}"
            )
        return 1

    print(
        "coverage-check PASS: "
        f"channels={report.get('channels_passing')}/{report.get('channels_total')} "
        f"slots={report.get('resolved_slots_total')}/{report.get('required_slots_total')}"
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Locked coverage pipeline for anticlickbait")
    sub = parser.add_subparsers(dest="command", required=True)

    p_targets = sub.add_parser("targets-build", help="Build immutable target manifest")
    p_targets.add_argument("--channels-file", type=Path, required=True)
    p_targets.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    p_targets.add_argument("--summary", type=Path, default=DEFAULT_MANIFEST_SUMMARY_PATH)
    p_targets.add_argument("--target-videos", type=int, default=15)
    p_targets.add_argument("--latest-ratio", type=float, default=VIDEO_SELECTION_LATEST_RATIO)
    p_targets.add_argument("--min-duration-seconds", type=int, default=MIN_VIDEO_DURATION_SECONDS)

    p_subs = sub.add_parser("subtitles-fetch", help="Fetch subtitles for manifest targets")
    p_subs.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    p_subs.add_argument("--summary", type=Path, default=DEFAULT_MANIFEST_SUMMARY_PATH)
    p_subs.add_argument("--replacements", type=Path, default=DEFAULT_REPLACEMENTS_PATH)
    p_subs.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    p_subs.add_argument("--workers", type=int, default=16)
    p_subs.add_argument("--waves", type=int, default=3)
    p_subs.add_argument("--wave-delay-seconds", type=int, default=300)
    p_subs.add_argument("--source-tiers", type=str, default="yt_dlp_public,yt_dlp_proxy,invidious,youtube_transcript_api,yt_dlp_auth")
    p_subs.add_argument("--retries-per-source", type=int, default=2)
    p_subs.add_argument("--language-codes", type=str, default="en,en-US,en-GB")
    p_subs.add_argument("--allow-replacements", action="store_true")
    p_subs.add_argument("--run-stats", type=Path, default=COVERAGE_DIR / "subtitles_fetch_last_run.json")

    p_report = sub.add_parser("coverage-report", help="Build coverage report JSON")
    p_report.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    p_report.add_argument("--summary", type=Path, default=DEFAULT_MANIFEST_SUMMARY_PATH)
    p_report.add_argument("--replacements", type=Path, default=DEFAULT_REPLACEMENTS_PATH)
    p_report.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    p_report.add_argument("--report", type=Path, default=DEFAULT_COVERAGE_REPORT_PATH)
    p_report.add_argument("--strict-ratio-drift", type=float, default=0.0)
    p_report.add_argument("--eta-horizon-hours", type=int, default=6)

    p_check = sub.add_parser("coverage-check", help="Fail unless all channels satisfy strict coverage")
    p_check.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    p_check.add_argument("--summary", type=Path, default=DEFAULT_MANIFEST_SUMMARY_PATH)
    p_check.add_argument("--replacements", type=Path, default=DEFAULT_REPLACEMENTS_PATH)
    p_check.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    p_check.add_argument("--report", type=Path, default=DEFAULT_COVERAGE_REPORT_PATH)
    p_check.add_argument("--strict-ratio-drift", type=float, default=0.0)
    p_check.add_argument("--eta-horizon-hours", type=int, default=6)

    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if args.command == "targets-build":
        build_targets_manifest(
            channels_file=args.channels_file,
            manifest_path=args.manifest,
            summary_path=args.summary,
            target_videos=max(1, args.target_videos),
            latest_ratio=max(0.0, min(1.0, args.latest_ratio)),
            min_duration_seconds=max(0, args.min_duration_seconds),
            selection_rule_version=SELECTION_RULE_VERSION,
        )
        return 0

    if args.command == "subtitles-fetch":
        source_tiers = [s.strip() for s in args.source_tiers.split(",") if s.strip()]
        language_codes = [s.strip() for s in args.language_codes.split(",") if s.strip()]
        run_stats = fetch_subtitles_for_manifest(
            manifest_path=args.manifest,
            summary_path=args.summary,
            replacements_path=args.replacements,
            db_path=args.db_path,
            workers=max(1, args.workers),
            waves=max(1, args.waves),
            wave_delay_seconds=max(0, args.wave_delay_seconds),
            source_tiers=source_tiers,
            retries_per_source=max(1, args.retries_per_source),
            language_codes=language_codes,
            allow_replacements=bool(args.allow_replacements),
            auth_cookies_file=os.environ.get("TRANSCRIPT_SERVICE_COOKIES_FILE", "").strip() or None,
            auth_cookies_browser=os.environ.get("TRANSCRIPT_SERVICE_COOKIES_BROWSER", "").strip() or None,
        )
        try:
            _write_json(args.run_stats, run_stats)
            print(f"subtitles-fetch run stats: {args.run_stats}")
        except OSError as e:
            print(f"warning: could not write run stats file {args.run_stats}: {e}")
        return 0

    if args.command == "coverage-report":
        report = build_coverage_report(
            manifest_path=args.manifest,
            summary_path=args.summary,
            replacements_path=args.replacements,
            db_path=args.db_path,
            report_path=args.report,
            strict_ratio_drift=max(0.0, args.strict_ratio_drift),
            eta_horizon_hours=max(1, args.eta_horizon_hours),
        )
        print(
            f"coverage-report: channels={report.get('channels_passing')}/{report.get('channels_total')} "
            f"resolved={report.get('resolved_slots_total')}/{report.get('required_slots_total')} "
            f"pending_subtitles={report.get('pending_subtitle_slots')} pending_evals={report.get('pending_eval_slots')}"
        )
        print(f"report: {args.report}")
        return 0

    if args.command == "coverage-check":
        return coverage_check(
            manifest_path=args.manifest,
            summary_path=args.summary,
            replacements_path=args.replacements,
            db_path=args.db_path,
            report_path=args.report,
            strict_ratio_drift=max(0.0, args.strict_ratio_drift),
            eta_horizon_hours=max(1, args.eta_horizon_hours),
        )

    print(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
