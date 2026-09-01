#!/usr/bin/env python3
"""
Main entry point for the anticlickbait pipeline.

Usage:
    python main.py                          # Run full pipeline with defaults
    python main.py --max-videos 5           # Limit videos per channel
    python main.py --dry-run                # Collect data only, no evaluation
    python main.py --skip-thumbnail         # Skip thumbnail analysis
    python main.py --channel @MrBeast       # Evaluate a single channel
    python main.py --show-rankings          # Show current rankings from database
"""

import argparse
import concurrent.futures
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from youtube_api import get_channel_info, get_channel_info_by_handle, get_channel_videos
from transcript_fetcher import get_transcript
from text_evaluator import evaluate_video, compute_video_score, compute_channel_score
from thumbnail_downloader import download_thumbnail
from thumbnail_evaluator import evaluate_thumbnail, compute_thumbnail_score
from database import (
    DEFAULT_DB_PATH,
    init_database,
    insert_channel,
    insert_video_evaluation,
    update_channel_score,
    get_database_stats,
    get_ranking_global_english,
)
from config import (
    VIDEOS_PER_CHANNEL,
    ENABLE_THUMBNAIL_ANALYSIS,
    ENABLE_LONG_FORM_SKIP,
    LONG_FORM_VIDEO_DURATION_SECONDS,
    LONG_FORM_CHANNEL_SKIP_RATIO,
    MAX_CONSECUTIVE_TRANSCRIPT_MISSES,
    VIDEO_SELECTION_LATEST_RATIO,
)


def load_channels(channels_file: Path) -> list[dict]:
    """Load channels from JSON file. Supports both old flat format and new categorized format."""
    if not channels_file.exists():
        print(f"Error: Channels file not found: {channels_file}")
        sys.exit(1)

    with open(channels_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # New format: {"categories": {"Category": {"main": [...], "buffer": [...]}}}
    if "categories" in data:
        channels = []
        for category, group in data["categories"].items():
            for entry in group.get("main", []):
                channels.append({**entry, "category": category})
        return channels

    # Old flat format: {"channels": [...]}
    return data.get("channels", [])


def compute_combined_score(text_score: float, thumb_score: float) -> float | None:
    """Compute combined score: 70% text + 30% thumbnail."""
    if text_score is not None and thumb_score is not None:
        return 0.70 * text_score + 0.30 * thumb_score
    elif text_score is not None:
        return text_score
    elif thumb_score is not None:
        return thumb_score
    return None


def _is_long_form_video(video: dict, duration_threshold_seconds: int) -> bool:
    duration = int(video.get("duration_seconds") or 0)
    return duration > duration_threshold_seconds


def _apply_long_form_skip_policy(
    videos: list[dict],
    *,
    duration_threshold_seconds: int,
    channel_skip_ratio: float,
) -> tuple[list[dict], dict]:
    """
    Filter long-form videos and optionally skip the full channel if long-form dominates.

    Returns:
        (eligible_videos, policy_meta)
    """
    if not videos:
        return [], {
            "enabled": ENABLE_LONG_FORM_SKIP,
            "duration_threshold_seconds": duration_threshold_seconds,
            "channel_skip_ratio": channel_skip_ratio,
            "total_videos": 0,
            "long_form_videos": 0,
            "long_form_ratio": 0.0,
            "channel_skipped": False,
            "skipped_video_ids": [],
        }

    long_videos = [v for v in videos if _is_long_form_video(v, duration_threshold_seconds)]
    long_ratio = len(long_videos) / len(videos)
    channel_skipped = long_ratio > channel_skip_ratio

    eligible_videos = [] if channel_skipped else [v for v in videos if v not in long_videos]
    skipped_video_ids = [v.get("video_id") for v in long_videos if v.get("video_id")]

    return eligible_videos, {
        "enabled": ENABLE_LONG_FORM_SKIP,
        "duration_threshold_seconds": duration_threshold_seconds,
        "channel_skip_ratio": channel_skip_ratio,
        "total_videos": len(videos),
        "long_form_videos": len(long_videos),
        "long_form_ratio": round(long_ratio, 3),
        "channel_skipped": channel_skipped,
        "skipped_video_ids": skipped_video_ids,
    }


def _get_existing_successful_video_ids(video_ids: list[str], db_path: Path = DEFAULT_DB_PATH) -> set[str]:
    """Return video_ids already successfully evaluated and stored."""
    if not video_ids:
        return set()
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join(["?"] * len(video_ids))
        query = (
            f"SELECT video_id FROM video_evaluations "
            f"WHERE evaluation_success = 1 AND video_id IN ({placeholders})"
        )
        rows = conn.execute(query, video_ids).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def _get_channel_score_snapshot_from_db(channel_id: str, db_path: Path = DEFAULT_DB_PATH) -> tuple[float | None, int]:
    """Recompute channel score from all successfully stored videos for the channel."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT AVG(video_score) as avg_score, COUNT(*) as cnt
            FROM video_evaluations
            WHERE channel_id = ? AND evaluation_success = 1 AND video_score IS NOT NULL
            """,
            (channel_id,),
        ).fetchone()
        if not row:
            return None, 0
        avg_score = row[0]
        count = int(row[1] or 0)
        return (float(avg_score) if avg_score is not None else None, count)
    finally:
        conn.close()


def evaluate_single_channel(
    handle: str,
    max_videos: int = VIDEOS_PER_CHANNEL,
    dry_run: bool = False,
    skip_thumbnail: bool = False,
    required_transcript_language_prefixes: tuple[str, ...] | None = None,
    enable_long_form_skip: bool = ENABLE_LONG_FORM_SKIP,
    long_form_video_duration_seconds: int = LONG_FORM_VIDEO_DURATION_SECONDS,
    long_form_channel_skip_ratio: float = LONG_FORM_CHANNEL_SKIP_RATIO,
    channel_seed: dict | None = None,
) -> dict:
    """
    Evaluate a single channel by handle.

    Args:
        handle: Channel handle (with or without @)
        max_videos: Maximum videos to evaluate
        dry_run: If True, collect data but skip evaluation
        skip_thumbnail: If True, skip thumbnail analysis
        required_transcript_language_prefixes: If set, only evaluate transcripts whose language_code starts with one of these prefixes
        enable_long_form_skip: If True, skip long-form videos and long-form-dominant channels
        long_form_video_duration_seconds: Duration threshold for long-form skip
        long_form_channel_skip_ratio: Skip channel when ratio of long-form videos exceeds this
        channel_seed: Optional pre-resolved metadata from channels file (avoids extra YouTube API lookups)

    Returns:
        Dictionary with channel results
    """
    # Normalize handle
    if not handle.startswith("@"):
        handle = f"@{handle}"

    print(f"\n{'='*60}")
    print(f"Processing channel: {handle}")
    print(f"{'='*60}")

    # Get channel info
    channel_info = None
    if channel_seed and channel_seed.get("channel_id"):
        channel_info = {
            "channel_id": channel_seed.get("channel_id"),
            "title": channel_seed.get("title") or handle,
            "description": channel_seed.get("description"),
            "custom_url": (channel_seed.get("custom_url") or handle.lstrip("@")),
            "country": channel_seed.get("country"),
            "subscriber_count": int(channel_seed.get("subscriber_count") or 0),
            "video_count": int(channel_seed.get("video_count") or 0),
            "view_count": int(channel_seed.get("view_count") or 0),
        }
        print("Using seeded channel metadata from channels file")
    else:
        print("Fetching channel info...")
        channel_info = get_channel_info_by_handle(handle)

    if not channel_info:
        print(f"  Error: Could not find channel {handle}")
        return {"success": False, "error": f"Channel not found: {handle}"}

    channel_id = channel_info["channel_id"]
    channel_title = channel_info.get("title") or handle
    print(f"  Found: {channel_title} ({int(channel_info.get('subscriber_count') or 0):,} subscribers)")

    # Insert/update channel in database
    insert_channel(
        channel_id=channel_id,
        title=channel_title,
        description=channel_info.get("description"),
        custom_url=channel_info.get("custom_url"),
        country=channel_info.get("country"),
        subscriber_count=channel_info.get("subscriber_count", 0),
        video_count=channel_info.get("video_count", 0),
        view_count=channel_info.get("view_count", 0),
    )

    # Get videos
    print(f"Fetching up to {max_videos} videos...")
    print(
        "  Selection method: latest+popular mix "
        f"(latest target {VIDEO_SELECTION_LATEST_RATIO:.0%}, dedupe fills from latest first)"
    )
    videos = get_channel_videos(
        channel_id,
        max_videos=max_videos,
        latest_ratio=VIDEO_SELECTION_LATEST_RATIO,
    )
    print(f"  Found {len(videos)} videos (after filtering shorts)")

    if not videos:
        print("  No videos found")
        return {"success": False, "error": "No videos found"}

    # Temporary long-form skip policy (park long-form until methodology is redesigned)
    policy_meta = None
    if enable_long_form_skip:
        eligible_videos, policy_meta = _apply_long_form_skip_policy(
            videos,
            duration_threshold_seconds=long_form_video_duration_seconds,
            channel_skip_ratio=long_form_channel_skip_ratio,
        )
        if policy_meta["channel_skipped"]:
            print(
                "  Skipping channel (long-form dominant): "
                f"{policy_meta['long_form_videos']}/{policy_meta['total_videos']} videos exceed "
                f"{long_form_video_duration_seconds}s ({policy_meta['long_form_ratio']:.0%} > {long_form_channel_skip_ratio:.0%})"
            )
            return {
                "success": True,
                "channel_id": channel_id,
                "channel_title": channel_title,
                "skipped": True,
                "skip_reason": "LONG_FORM_DOMINANT_CHANNEL",
                "skip_policy": policy_meta,
            }

        skipped_count = policy_meta["long_form_videos"]
        if skipped_count:
            print(
                "  Long-form skip policy: "
                f"skipping {skipped_count}/{policy_meta['total_videos']} videos > {long_form_video_duration_seconds}s"
            )
        videos = eligible_videos
        print(f"  Eligible videos after long-form skip: {len(videos)}")

        if not videos:
            print("  No eligible videos after long-form skip policy")
            return {
                "success": True,
                "channel_id": channel_id,
                "channel_title": channel_title,
                "skipped": True,
                "skip_reason": "NO_ELIGIBLE_VIDEOS_AFTER_LONG_FORM_SKIP",
                "skip_policy": policy_meta,
            }

    # Backfill/persist a channel-level category from fetched videos (YouTube category ID).
    channel_category_id = next((v.get("category_id") for v in videos if v.get("category_id") is not None), None)
    if channel_category_id is not None:
        insert_channel(
            channel_id=channel_id,
            title=channel_title,
            description=channel_info.get("description"),
            custom_url=channel_info.get("custom_url"),
            country=channel_info.get("country"),
            category_id=channel_category_id,
            subscriber_count=channel_info.get("subscriber_count", 0),
            video_count=channel_info.get("video_count", 0),
            view_count=channel_info.get("view_count", 0),
        )

    # Determine if thumbnail analysis is enabled
    do_thumbnail = ENABLE_THUMBNAIL_ANALYSIS and not skip_thumbnail

    selected_video_ids = [v["video_id"] for v in videos if v.get("video_id")]
    existing_success_ids = _get_existing_successful_video_ids(selected_video_ids)
    if existing_success_ids:
        print(f"  Existing successful evaluations in DB: {len(existing_success_ids)} (will skip re-eval)")

    # Process each video
    evaluation_results = []
    per_channel_counts = Counter()
    per_channel_counts["videos_total"] = len(videos)
    transcript_misses: list[dict] = []

    for idx, video in enumerate(videos):
        video_id = video["video_id"]
        title = video["title"]
        duration = video["duration_seconds"]
        thumbnail_url = video.get("thumbnail_url")

        print(f"\n  [{idx+1}/{len(videos)}] {title[:50]}...")

        if video_id in existing_success_ids:
            print("    Skipping: already successfully evaluated in DB")
            per_channel_counts["skipped_already_successful_in_db"] += 1
            continue

        # Get transcript
        transcript_result = get_transcript(video_id)

        if not transcript_result["success"]:
            print(f"    Skipping: {transcript_result['error']}")
            per_channel_counts["skipped_transcript_unavailable"] += 1
            transcript_misses.append({
                "video_id": video_id,
                "title": title,
                "view_count": video.get("view_count", 0),
            })
            # Fast-fail guard: channel is likely non-eligible or blocked for transcript retrieval.
            misses = per_channel_counts["skipped_transcript_unavailable"]
            if (
                misses >= MAX_CONSECUTIVE_TRANSCRIPT_MISSES
                and per_channel_counts["evaluated_success"] == 0
            ):
                print(
                    f"    Channel fast-fail: {misses} transcript misses with 0 successful evals. "
                    "Skipping remaining videos for this channel."
                )
                per_channel_counts["channel_fast_failed_transcript_unavailable"] += 1
                break
            continue

        transcript_segments = transcript_result["segments"]
        transcript_text = transcript_result.get("transcript_text", "")
        transcript_language = transcript_result.get("language_code")
        print(f"    Transcript: {len(transcript_segments)} segments ({transcript_language})")

        if required_transcript_language_prefixes:
            lang = (transcript_language or "").lower()
            if not any(lang.startswith(prefix) for prefix in required_transcript_language_prefixes):
                print(
                    "    Skipping: transcript language "
                    f"{transcript_language!r} not in {required_transcript_language_prefixes}"
                )
                per_channel_counts["skipped_transcript_language_mismatch"] += 1
                continue

        if dry_run:
            print("    [DRY RUN] Skipping evaluation")
            per_channel_counts["skipped_dry_run"] += 1
            continue

        # Text evaluation
        print("    Evaluating text with OpenAI...")
        eval_result = evaluate_video(
            video_id=video_id,
            channel_id=channel_id,
            title=title,
            duration_seconds=duration,
            transcript_segments=transcript_segments,
        )

        video_score = None
        if eval_result.get("evaluation_success"):
            video_score = compute_video_score(eval_result)
            print(f"    Text Score: {video_score:.1f}")
            print(f"    Title-Content: {eval_result.get('title_content_similarity_score', -1):.1f}/10")
            print(f"    Focus: {eval_result.get('focus_ratio', -1):.1f}/10")
            print(f"    Deception: {eval_result.get('deception_score', -1):.1f}/10")
        else:
            print(f"    Text evaluation failed: {eval_result.get('error')}")
            per_channel_counts["text_eval_failed"] += 1

        # Thumbnail evaluation
        thumb_result = None
        thumb_score = None
        if do_thumbnail and thumbnail_url:
            print("    Downloading thumbnail...")
            dl_result = download_thumbnail(video_id, thumbnail_url)

            if dl_result["success"]:
                cache_info = "(cached)" if dl_result["from_cache"] else ""
                print(f"    Evaluating thumbnail... {cache_info}")
                thumb_result = evaluate_thumbnail(
                    video_id=video_id,
                    thumbnail_path=dl_result["path"],
                    video_title=title,
                )

                if thumb_result.get("evaluation_success"):
                    thumb_score = compute_thumbnail_score(thumb_result)
                    print(f"    Thumb Score: {thumb_score:.1f}")
                    print(f"    Thumb Sensationalism: {thumb_result.get('thumbnail_sensationalism_score', 0):.1f}/10")
                else:
                    print(f"    Thumbnail eval failed: {thumb_result.get('error')}")
            else:
                print(f"    Thumbnail download failed: {dl_result['error']}")

        # Compute combined score
        combined_score = compute_combined_score(video_score, thumb_score)
        if combined_score is not None and video_score is not None and thumb_score is not None:
            print(f"    Combined Score: {combined_score:.1f}")

        evaluation_results.append(eval_result)
        per_channel_counts["evaluated_attempted"] += 1
        if eval_result.get("evaluation_success"):
            per_channel_counts["evaluated_success"] += 1

        # Store in database
        insert_video_evaluation(
            video_id=video_id,
            channel_id=channel_id,
            title=title,
            duration_seconds=duration,
            evaluation_result=eval_result,
            video_score=video_score,
            description=video.get("description"),
            published_at=video.get("published_at"),
            category_id=video.get("category_id"),
            view_count=video.get("view_count", 0),
            like_count=video.get("like_count", 0),
            comment_count=video.get("comment_count", 0),
            transcript_text=transcript_text,
            transcript_language=transcript_language,
            thumbnail_url=thumbnail_url,
            thumbnail_result=thumb_result,
            thumbnail_score=thumb_score,
            combined_score=combined_score,
        )

    # Transcript miss summary
    if transcript_misses:
        total_selected = len(videos)
        miss_count = len(transcript_misses)
        evaluated_count = per_channel_counts["evaluated_success"]
        print(f"\n  Transcript miss summary: {miss_count}/{total_selected} videos missing transcript "
              f"({evaluated_count} evaluated successfully)")
        top_missed = sorted(transcript_misses, key=lambda v: v["view_count"], reverse=True)[:5]
        print("  Top missed videos by view count:")
        for i, v in enumerate(top_missed, 1):
            views = v["view_count"]
            views_str = f"{views/1_000_000:.1f}M" if views >= 1_000_000 else f"{views/1_000:.0f}K"
            print(f"    {i}. {v['title'][:60]} — {views_str} views ({v['video_id']})")

    # Compute and update channel score
    if not dry_run:
        # Recompute from DB so duplicate skips do not corrupt channel averages/counts.
        channel_score, videos_evaluated = _get_channel_score_snapshot_from_db(channel_id)

        if channel_score is not None:
            update_channel_score(channel_id, channel_score, videos_evaluated)
            print(f"\n  Channel Score: {channel_score:.1f}")
            print(f"  Videos Evaluated: {videos_evaluated}/{len(videos)}")
        if per_channel_counts:
            printable = {k: int(v) for k, v in per_channel_counts.items() if k != "videos_total"}
            print(f"  Channel Processing Summary: total={per_channel_counts['videos_total']} details={printable}")

        return {
            "success": True,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "channel_score": channel_score,
            "videos_evaluated": videos_evaluated,
            "results": evaluation_results,
            "skip_policy": policy_meta,
            "processing_counts": dict(per_channel_counts),
        }

    return {
        "success": True,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "videos_found": len(videos),
        "dry_run": dry_run,
        "skip_policy": policy_meta,
        "processing_counts": dict(per_channel_counts),
    }


def run_full_pipeline(
    channels_file: Path,
    max_videos: int = VIDEOS_PER_CHANNEL,
    dry_run: bool = False,
    skip_thumbnail: bool = False,
    workers: int = 1,
    required_transcript_language_prefixes: tuple[str, ...] | None = None,
    enable_long_form_skip: bool = ENABLE_LONG_FORM_SKIP,
    long_form_video_duration_seconds: int = LONG_FORM_VIDEO_DURATION_SECONDS,
    long_form_channel_skip_ratio: float = LONG_FORM_CHANNEL_SKIP_RATIO,
) -> dict:
    """
    Run the full pipeline on all channels in the channels file.

    Args:
        channels_file: Path to channels JSON file
        max_videos: Maximum videos per channel
        dry_run: If True, collect data but skip evaluation
        skip_thumbnail: If True, skip thumbnail analysis

    Returns:
        Dictionary with aggregate results
    """
    # Initialize database
    print("Initializing database...")
    init_database()

    # Load channels
    channels = load_channels(channels_file)
    print(f"Loaded {len(channels)} channels from {channels_file}")

    if not channels:
        print("No channels to process")
        return {"success": False, "error": "No channels in file"}

    # Process each channel
    channel_inputs = []
    for channel in channels:
        handle = channel.get("handle")
        if not handle:
            print(f"Skipping channel without handle: {channel}")
            continue
        channel_inputs.append({"handle": handle, "seed": channel})

    def _run_channel(channel_input: dict) -> dict:
        return evaluate_single_channel(
            handle=channel_input["handle"],
            max_videos=max_videos,
            dry_run=dry_run,
            skip_thumbnail=skip_thumbnail,
            required_transcript_language_prefixes=required_transcript_language_prefixes,
            enable_long_form_skip=enable_long_form_skip,
            long_form_video_duration_seconds=long_form_video_duration_seconds,
            long_form_channel_skip_ratio=long_form_channel_skip_ratio,
            channel_seed=channel_input.get("seed"),
        )

    results = []
    if workers <= 1:
        for item in channel_inputs:
            results.append(_run_channel(item))
    else:
        print(f"Running with {workers} channel workers")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_channel = {executor.submit(_run_channel, item): item for item in channel_inputs}
            for future in concurrent.futures.as_completed(future_to_channel):
                item = future_to_channel[future]
                handle = item.get("handle")
                try:
                    results.append(future.result())
                except Exception as e:
                    print(f"\nWorker failed for {handle}: {e}")
                    results.append({"success": False, "error": str(e), "handle": handle})

    # Print summary
    print("\n" + "="*60)
    print("PIPELINE COMPLETE")
    print("="*60)

    successful = sum(1 for r in results if r.get("success"))
    skipped = sum(1 for r in results if r.get("skipped"))
    print(f"Channels processed: {successful}/{len(results)}")
    if skipped:
        print(f"Channels skipped by policy: {skipped}")

    aggregate_counts = Counter()
    for r in results:
        for k, v in (r.get("processing_counts") or {}).items():
            aggregate_counts[k] += int(v or 0)
    if aggregate_counts:
        print("\nProcessing summary (aggregated):")
        for key in sorted(aggregate_counts):
            print(f"  {key}: {aggregate_counts[key]}")

    if not dry_run:
        stats = get_database_stats()
        print(f"\nDatabase stats:")
        print(f"  Total channels: {stats['total_channels']}")
        print(f"  Evaluated channels: {stats['evaluated_channels']}")
        print(f"  Total videos: {stats['total_videos']}")
        print(f"  Successful evaluations: {stats['successful_evaluations']}")

    return {
        "success": True,
        "channels_processed": len(results),
        "channels_successful": successful,
        "results": results,
    }


def show_rankings(limit: int = 20) -> None:
    """Display current rankings from database."""
    print("\n" + "="*60)
    print("CHANNEL RANKINGS (by Anticlickbait Score)")
    print("="*60)

    rankings = get_ranking_global_english(limit=limit)

    if not rankings:
        print("No rankings available. Run the pipeline first.")
        return

    print(f"\n{'Rank':<6} {'Channel':<30} {'Score':<8} {'Videos':<8}")
    print("-" * 60)

    for row in rankings:
        print(f"{row['rank']:<6} {row['title'][:28]:<30} {row['channel_score']:.1f}    {row['videos_evaluated']}")


def main():
    parser = argparse.ArgumentParser(
        description="Anticlickbait Pipeline - Evaluate YouTube channels for clickbait",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python main.py                          # Run full pipeline
    python main.py --max-videos 5           # Limit to 5 videos per channel
    python main.py --dry-run                # Collect data only, no evaluation
    python main.py --skip-thumbnail         # Skip thumbnail analysis
    python main.py --channel @MrBeast       # Evaluate single channel
    python main.py --show-rankings          # Show current rankings
        """,
    )

    parser.add_argument(
        "--channels-file",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "channels_final.json",
        help="Path to channels JSON file (default: data/channels_final.json)",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=VIDEOS_PER_CHANNEL,
        help=f"Maximum videos to evaluate per channel (default: {VIDEOS_PER_CHANNEL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect data only, skip evaluation (saves API costs)",
    )
    parser.add_argument(
        "--skip-thumbnail",
        action="store_true",
        help="Skip thumbnail analysis (text evaluation only)",
    )
    parser.add_argument(
        "--channel",
        type=str,
        help="Evaluate a single channel by handle (e.g., @MrBeast)",
    )
    parser.add_argument(
        "--show-rankings",
        action="store_true",
        help="Show current rankings from database and exit",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of channel workers for full pipeline mode (default: 1)",
    )
    parser.add_argument(
        "--require-transcript-language-prefixes",
        type=str,
        default=None,
        help="Comma-separated transcript language prefixes to evaluate (e.g., en,en-US). Others are skipped after transcript fetch.",
    )
    parser.add_argument(
        "--disable-long-form-skip",
        action="store_true",
        help="Disable temporary long-form skip policy (not recommended for now)",
    )
    parser.add_argument(
        "--long-form-video-duration-seconds",
        type=int,
        default=LONG_FORM_VIDEO_DURATION_SECONDS,
        help=f"Skip individual videos longer than this (default: {LONG_FORM_VIDEO_DURATION_SECONDS})",
    )
    parser.add_argument(
        "--long-form-channel-skip-ratio",
        type=float,
        default=LONG_FORM_CHANNEL_SKIP_RATIO,
        help=f"Skip channel if > this ratio of selected videos are long-form (default: {LONG_FORM_CHANNEL_SKIP_RATIO})",
    )

    args = parser.parse_args()

    # Show rankings and exit
    if args.show_rankings:
        init_database()
        show_rankings()
        return

    # Single channel mode
    if args.channel:
        init_database()
        evaluate_single_channel(
            handle=args.channel,
            max_videos=args.max_videos,
            dry_run=args.dry_run,
            skip_thumbnail=args.skip_thumbnail,
            enable_long_form_skip=not args.disable_long_form_skip,
            long_form_video_duration_seconds=args.long_form_video_duration_seconds,
            long_form_channel_skip_ratio=args.long_form_channel_skip_ratio,
        )
        return

    # Full pipeline mode
    required_lang_prefixes = None
    if args.require_transcript_language_prefixes:
        required_lang_prefixes = tuple(
            p.strip().lower() for p in args.require_transcript_language_prefixes.split(",") if p.strip()
        ) or None

    run_full_pipeline(
        channels_file=args.channels_file,
        max_videos=args.max_videos,
        dry_run=args.dry_run,
        skip_thumbnail=args.skip_thumbnail,
        workers=max(1, args.workers),
        required_transcript_language_prefixes=required_lang_prefixes,
        enable_long_form_skip=not args.disable_long_form_skip,
        long_form_video_duration_seconds=args.long_form_video_duration_seconds,
        long_form_channel_skip_ratio=args.long_form_channel_skip_ratio,
    )


if __name__ == "__main__":
    main()
