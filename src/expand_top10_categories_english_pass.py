#!/usr/bin/env python3
"""
Fill underfilled categories in the youtubers.me-derived top-channel list using transcript-language sampling.

This is a refinement pass over the existing filtered list:
- targets only categories with count < target_n
- scans deeper subscriber-ranked rows from youtubers.me category pages
- resolves real YouTube channels from sample video IDs
- runs a lightweight transcript-language sampling check (no LLM calls)
- appends accepted channels and rewrites the list file
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from build_youtubers_top10_categories import (
    BASE,
    extract_category_links,
    extract_top_rows,
    extract_video_ids_from_youtuber_stats,
    fetch,
    normalize_handle,
    resolve_channel_from_video_ids,
)
from config import LONG_FORM_VIDEO_DURATION_SECONDS
from transcript_fetcher import get_transcript
from youtube_api import get_channel_info, get_channel_videos


def _lang_is_english(code: str | None) -> bool:
    if not code:
        return False
    return code.lower().startswith("en")


def _sample_channel_english_primary(
    channel_id: str,
    *,
    sample_videos: int,
    long_form_duration_seconds: int,
    min_transcript_samples: int,
    min_english_ratio: float,
) -> tuple[bool, dict]:
    """
    Transcript-only channel screening (no LLM).
    """
    videos = get_channel_videos(channel_id, max_videos=max(sample_videos * 2, sample_videos))
    sampled = []
    transcript_langs = []
    stats = {
        "videos_fetched": len(videos),
        "videos_long_form_skipped": 0,
        "transcript_failures": 0,
        "transcript_successes": 0,
        "english_transcript_successes": 0,
    }

    for video in videos:
        if len(sampled) >= sample_videos:
            break
        if int(video.get("duration_seconds") or 0) > long_form_duration_seconds:
            stats["videos_long_form_skipped"] += 1
            continue

        tr = get_transcript(video["video_id"])
        if not tr.get("success") or not (tr.get("segments") or []):
            stats["transcript_failures"] += 1
            continue

        lang = tr.get("language_code")
        stats["transcript_successes"] += 1
        if _lang_is_english(lang):
            stats["english_transcript_successes"] += 1
        transcript_langs.append(lang)
        sampled.append(
            {
                "video_id": video["video_id"],
                "title": video.get("title"),
                "duration_seconds": video.get("duration_seconds"),
                "transcript_language": lang,
            }
        )

    denom = stats["transcript_successes"]
    english_ratio = (stats["english_transcript_successes"] / denom) if denom else 0.0
    passed = denom >= min_transcript_samples and english_ratio >= min_english_ratio

    return passed, {
        **stats,
        "english_ratio": round(english_ratio, 3),
        "sampled_videos": sampled,
        "transcript_languages": transcript_langs,
        "screening_rule": {
            "sample_videos": sample_videos,
            "min_transcript_samples": min_transcript_samples,
            "min_english_ratio": min_english_ratio,
            "long_form_duration_seconds": long_form_duration_seconds,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Expansion pass to fill underfilled categories with English-primary channels")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "channels_top10_per_category_youtubers.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Defaults to overwriting --input",
    )
    parser.add_argument("--target-n", type=int, default=10)
    parser.add_argument("--scan-depth", type=int, default=80, help="How many top-ranked rows to scan per underfilled category")
    parser.add_argument("--sample-videos", type=int, default=3, help="Transcript-language sample size per candidate channel")
    parser.add_argument("--min-transcript-samples", type=int, default=2, help="Minimum successful transcript samples to decide")
    parser.add_argument("--min-english-ratio", type=float, default=0.67, help="Min English transcript ratio to accept channel")
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--long-form-duration-seconds", type=int, default=LONG_FORM_VIDEO_DURATION_SECONDS)
    args = parser.parse_args()
    if args.output is None:
        args.output = args.input

    with open(args.input, "r", encoding="utf-8") as f:
        payload = json.load(f)

    channels = payload.get("channels", [])
    by_category = defaultdict(list)
    selected_channel_ids = set()
    for ch in channels:
        by_category[ch["category"]].append(ch)
        if ch.get("channel_id"):
            selected_channel_ids.add(ch["channel_id"])

    # Build category slug map from youtubers.me navbar.
    seed_html = fetch(f"{BASE}/global/gaming/top-1000-gaming-most_subscribed-youtube-channels")
    category_links = extract_category_links(seed_html)
    label_to_cat = {}
    for c in category_links:
        label_to_cat[c["label"]] = c

    underfilled = [cat for cat, items in sorted(by_category.items()) if len(items) < args.target_n]
    # Also include categories entirely missing from file if any.
    for label in sorted(label_to_cat):
        if label not in by_category and label not in underfilled:
            underfilled.append(label)

    print(f"Underfilled categories: {len(underfilled)}")
    for cat in underfilled:
        print(f"  - {cat}: {len(by_category.get(cat, []))}/{args.target_n}")

    expansion_report = {"categories": {}, "summary": {}}

    for cat_label in underfilled:
        cat_meta = label_to_cat.get(cat_label)
        if not cat_meta:
            print(f"\nSkipping {cat_label}: no slug mapping found")
            continue

        need = args.target_n - len(by_category.get(cat_label, []))
        if need <= 0:
            continue

        category_url = BASE + cat_meta["href"]
        print(f"\n=== Expanding {cat_label} (need {need}) ===")
        print(f"Fetching {category_url}")
        cat_html = fetch(category_url)
        time.sleep(args.sleep_seconds)
        rows = extract_top_rows(cat_html, top_n=args.scan_depth)
        print(f"  Candidate rows parsed: {len(rows)}")

        cat_report = {
            "needed_start": need,
            "scan_depth": args.scan_depth,
            "rows_considered": 0,
            "accepted": [],
            "rejected": [],
        }

        for row in rows:
            if need <= 0:
                break
            cat_report["rows_considered"] += 1
            ytm_slug = row["youtubers_slug"]
            stats_url = f"{BASE}/{ytm_slug}/youtuber-stats"
            print(f"    [{row['rank']}] {row['display_name']} ({row['subscriber_count_source']:,})")

            try:
                ytm_html = fetch(stats_url)
            except Exception as e:
                print(f"      stats fetch failed: {e}")
                cat_report["rejected"].append({"rank": row["rank"], "name": row["display_name"], "reason": f"stats_fetch_failed: {e}"})
                continue
            time.sleep(args.sleep_seconds)

            video_ids = extract_video_ids_from_youtuber_stats(ytm_html)
            if not video_ids:
                print("      no sample video ids on stats page")
                cat_report["rejected"].append({"rank": row["rank"], "name": row["display_name"], "reason": "no_sample_video_ids"})
                continue

            resolved = resolve_channel_from_video_ids(video_ids)
            if not resolved:
                print("      could not resolve channel from sample videos")
                cat_report["rejected"].append({"rank": row["rank"], "name": row["display_name"], "reason": "video_resolution_failed"})
                continue

            channel_info = get_channel_info(resolved["channel_id"])
            if not channel_info:
                print("      get_channel_info failed")
                cat_report["rejected"].append({"rank": row["rank"], "name": row["display_name"], "reason": "channel_info_failed"})
                continue

            channel_id = channel_info["channel_id"]
            if channel_id in selected_channel_ids:
                print("      duplicate channel already selected")
                cat_report["rejected"].append({"rank": row["rank"], "name": row["display_name"], "reason": "duplicate_channel"})
                continue

            handle = normalize_handle(channel_info.get("custom_url"))
            if not handle:
                print("      missing handle/custom_url")
                cat_report["rejected"].append({"rank": row["rank"], "name": row["display_name"], "reason": "missing_handle"})
                continue

            passed, screening = _sample_channel_english_primary(
                channel_id,
                sample_videos=args.sample_videos,
                long_form_duration_seconds=args.long_form_duration_seconds,
                min_transcript_samples=args.min_transcript_samples,
                min_english_ratio=args.min_english_ratio,
            )

            if not passed:
                print(
                    "      transcript-language reject "
                    f"(samples={screening['transcript_successes']}, en_ratio={screening['english_ratio']})"
                )
                cat_report["rejected"].append(
                    {
                        "rank": row["rank"],
                        "name": row["display_name"],
                        "reason": "transcript_language_screen_failed",
                        "screening": screening,
                    }
                )
                continue

            new_item = {
                "handle": handle,
                "category": cat_label,
                "channel_id": channel_id,
                "title": channel_info.get("title"),
                "country": channel_info.get("country"),
                "source": "youtubers.me",
                "source_category_slug": cat_meta["slug"],
                "source_rank": row["rank"],
                "source_subscriber_count": row["subscriber_count_source"],
                "resolved_subscriber_count": channel_info.get("subscriber_count", 0),
                "sample_video_id_for_resolution": resolved["sample_video_id"],
                "english_screening": screening,
            }
            channels.append(new_item)
            by_category[cat_label].append(new_item)
            selected_channel_ids.add(channel_id)
            need -= 1
            print(f"      accepted -> {channel_info.get('title')} ({handle})")
            cat_report["accepted"].append(
                {
                    "channel_id": channel_id,
                    "title": channel_info.get("title"),
                    "handle": handle,
                    "rank": row["rank"],
                    "country": channel_info.get("country"),
                    "screening": screening,
                }
            )

        cat_report["needed_end"] = max(0, need)
        cat_report["final_count"] = len(by_category.get(cat_label, []))
        expansion_report["categories"][cat_label] = cat_report

    channels.sort(key=lambda c: (c.get("category", ""), c.get("source_rank", 10**9), c.get("title", "")))

    category_counts = {cat: len(items) for cat, items in sorted(by_category.items())}
    payload["channels"] = channels
    payload["category_counts"] = category_counts
    payload.setdefault("meta", {})
    payload["meta"]["english_expansion_pass"] = {
        "applied": True,
        "target_n": args.target_n,
        "scan_depth": args.scan_depth,
        "sample_videos": args.sample_videos,
        "min_transcript_samples": args.min_transcript_samples,
        "min_english_ratio": args.min_english_ratio,
        "long_form_duration_seconds": args.long_form_duration_seconds,
    }
    payload["expansion_report"] = expansion_report

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"\nSaved expanded list to {args.output}")
    print("Category counts:")
    for cat, cnt in category_counts.items():
        print(f"  {cat}: {cnt}")


if __name__ == "__main__":
    main()
