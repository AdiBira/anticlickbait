#!/usr/bin/env python3
"""
Build a channels JSON file from youtubers.me global category charts (top subscribers).

Output format matches the project's channels JSON shape:
{
  "channels": [
    {"handle": "@MrBeast", "category": "Entertainment", ...},
    ...
  ]
}

Notes:
- Discovery source is youtubers.me category ranking pages (global, most subscribed).
- Channel resolution uses one public video ID from the youtubers.me stats page -> YouTube API video lookup -> channel info.
- "English content" is not perfectly inferable from ranking pages; this script records country and source metadata for later filtering.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from youtube_api import get_youtube_client, get_channel_info


BASE = "https://us.youtubers.me"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Exclude categories that don't fit this product scope for now.
EXCLUDED_CATEGORY_SLUGS = {
    "all",
    "music",
    "movies",
    "people-blogs",  # youtubers.me variant sometimes differs; keep separate categories instead
    "shows",
    "nonprofits-activism",
}

DEFAULT_ENGLISH_COUNTRY_WHITELIST = {
    "US", "GB", "CA", "AU", "NZ", "IE", "SG", "ZA"
}


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def slug_to_label(slug: str) -> str:
    return slug.replace("-", " ").replace("&", " & ").title().replace("  ", " ")


def extract_category_links(html: str) -> list[dict]:
    """
    Extract global category chart links from youtubers.me navbar.
    """
    links = re.findall(r'href="(/global/([a-z0-9\-&]+)/top-[^"]+-youtube-channels)"', html)
    out = []
    seen = set()
    for href, slug in links:
        if slug in EXCLUDED_CATEGORY_SLUGS:
            continue
        key = (slug, href)
        if key in seen:
            continue
        seen.add(key)
        out.append({"slug": slug, "href": href, "label": slug_to_label(slug)})
    return out


ROW_RE = re.compile(
    r"<tr>\s*"
    r"<td>\s*(?P<rank>\d+)\s*</td>\s*"
    r"<td>\s*<a href=\"/(?P<ytm_slug>[^\"]+)/youtuber-stats\">.*?>\s*(?P<name>[^<\n][^<]*?)\s*</a>\s*</td>\s*"
    r"<td>\s*(?P<subs>[0-9,]+)\s*</td>\s*"
    r"<td>\s*(?P<views>[0-9,]+)\s*</td>\s*"
    r"<td>\s*(?P<video_count>[0-9,]+)\s*</td>\s*"
    r"<td><a href=\"/global/(?P<cat_slug>[^\"]+)/top-[^\"]+-youtube-channels\">(?P<cat_name>[^<]+)</a></td>\s*"
    r"<td>\s*(?P<started>\d{4})\s*</td>\s*"
    r"</tr>",
    re.S,
)


def extract_top_rows(html: str, top_n: int) -> list[dict]:
    rows = []
    for m in ROW_RE.finditer(html):
        rows.append(
            {
                "rank": int(m.group("rank")),
                "youtubers_slug": m.group("ytm_slug").strip(),
                "display_name": re.sub(r"\s+", " ", m.group("name")).strip(),
                "subscriber_count_source": int(m.group("subs").replace(",", "")),
                "views_source": int(m.group("views").replace(",", "")),
                "video_count_source": int(m.group("video_count").replace(",", "")),
                "category_slug": m.group("cat_slug").strip(),
                "category_name_source": m.group("cat_name").strip(),
                "started_year_source": int(m.group("started")),
            }
        )
        if len(rows) >= top_n:
            break
    return rows


VIDEO_ID_RE = re.compile(r'data-src="([A-Za-z0-9_-]{11})"')


def extract_video_ids_from_youtuber_stats(html: str, limit: int = 8) -> list[str]:
    seen = set()
    out = []
    for vid in VIDEO_ID_RE.findall(html):
        if vid in seen:
            continue
        seen.add(vid)
        out.append(vid)
        if len(out) >= limit:
            break
    return out


def resolve_channel_from_video_ids(video_ids: list[str]) -> dict | None:
    youtube = get_youtube_client()
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        try:
            response = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
        except Exception:
            continue
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            channel_id = snippet.get("channelId")
            if not channel_id:
                continue
            return {
                "channel_id": channel_id,
                "channel_title_from_video": snippet.get("channelTitle"),
                "sample_video_id": item.get("id"),
            }
    return None


def normalize_handle(custom_url: str | None) -> str | None:
    if not custom_url:
        return None
    if custom_url.startswith("@"):
        return custom_url
    # In many cases customUrl is effectively the handle without '@'
    return f"@{custom_url.strip('/')}"


def main():
    parser = argparse.ArgumentParser(description="Build top-10-per-category channels list from youtubers.me")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--overfetch-multiplier",
        type=int,
        default=8,
        help="Parse up to top_n * multiplier rows per category before filtering/replacement",
    )
    parser.add_argument(
        "--top-page-size",
        type=int,
        default=None,
        help="Optional youtubers.me top-N page size to fetch per category (e.g. 100, 300). If omitted, uses the default category page.",
    )
    parser.add_argument(
        "--seed-page",
        default=f"{BASE}/global/gaming/top-1000-gaming-most_subscribed-youtube-channels",
        help="A youtubers.me page used to extract category links from navbar",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help="Optional category slugs to include (e.g. gaming education science-technology). Default: all scraped categories minus excludes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "channels_top10_per_category_youtubers.json",
    )
    parser.add_argument("--sleep-seconds", type=float, default=0.3, help="Delay between HTTP requests to be polite")
    parser.add_argument(
        "--enforce-country-whitelist",
        action="store_true",
        help="Require channel country to be in the English-country whitelist",
    )
    parser.add_argument(
        "--english-country-whitelist",
        nargs="*",
        default=sorted(DEFAULT_ENGLISH_COUNTRY_WHITELIST),
        help="Country codes considered English-primary enough for overnight global-English seeding",
    )
    args = parser.parse_args()
    args.english_country_whitelist = set(args.english_country_whitelist)

    print(f"Fetching category seed page: {args.seed_page}")
    seed_html = fetch(args.seed_page)
    category_links = extract_category_links(seed_html)
    if args.categories:
        wanted = set(args.categories)
        category_links = [c for c in category_links if c["slug"] in wanted]

    # Dedupe by category slug; prefer first occurrence from navbar.
    dedup = {}
    for c in category_links:
        dedup.setdefault(c["slug"], c)
    category_links = list(dedup.values())

    print(f"Found {len(category_links)} categories from youtubers.me navbar")
    for c in category_links:
        print(f"  - {c['slug']} ({c['label']})")

    channels_out = []
    seen_channel_ids = set()
    stats = defaultdict(int)
    per_category_counts = defaultdict(int)

    for cat in category_links:
        if args.top_page_size and args.top_page_size > 0:
            category_url = f"{BASE}/global/{cat['slug']}/top-{args.top_page_size}-youtube-channels"
        else:
            category_url = BASE + cat["href"]
        print(f"\n=== Category: {cat['label']} ({cat['slug']}) ===")
        print(f"Fetching {category_url}")

        try:
            cat_html = fetch(category_url)
        except urllib.error.URLError as e:
            print(f"  Failed to fetch category page: {e}")
            stats["category_fetch_fail"] += 1
            continue
        time.sleep(args.sleep_seconds)

        rows = extract_top_rows(cat_html, top_n=max(args.top_n * args.overfetch_multiplier, args.top_n))
        print(f"  Parsed {len(rows)} rows (target {args.top_n})")
        if not rows:
            stats["category_parse_fail"] += 1
            continue

        for row in rows:
            if per_category_counts[cat["label"]] >= args.top_n:
                break
            ytm_slug = row["youtubers_slug"]
            stats_url = f"{BASE}/{ytm_slug}/youtuber-stats"
            print(f"    [{row['rank']}] {row['display_name']} ({row['subscriber_count_source']:,})")

            try:
                ytm_html = fetch(stats_url)
            except urllib.error.URLError as e:
                print(f"      stats page fetch failed: {e}")
                stats["stats_fetch_fail"] += 1
                continue
            time.sleep(args.sleep_seconds)

            video_ids = extract_video_ids_from_youtuber_stats(ytm_html)
            if not video_ids:
                print("      no sample video ids found")
                stats["no_video_ids"] += 1
                continue

            resolved = resolve_channel_from_video_ids(video_ids)
            if not resolved:
                print("      could not resolve channel from sample video ids")
                stats["video_resolve_fail"] += 1
                continue

            channel_info = get_channel_info(resolved["channel_id"])
            if not channel_info:
                print("      get_channel_info failed")
                stats["channel_info_fail"] += 1
                continue

            channel_id = channel_info["channel_id"]
            if channel_id in seen_channel_ids:
                print("      duplicate channel across categories, skipping")
                stats["duplicate_channel_ids"] += 1
                continue

            country = channel_info.get("country")
            if args.enforce_country_whitelist and country not in args.english_country_whitelist:
                print(f"      country {country!r} not in English-country whitelist, skipping")
                stats["country_whitelist_skips"] += 1
                continue

            handle = normalize_handle(channel_info.get("custom_url"))
            if not handle:
                print("      missing custom_url/handle, skipping")
                stats["missing_handle"] += 1
                continue

            channels_out.append(
                {
                    "handle": handle,
                    "category": cat["label"],
                    "channel_id": channel_id,
                    "title": channel_info.get("title"),
                    "country": country,
                    "source": "youtubers.me",
                    "source_category_slug": cat["slug"],
                    "source_rank": row["rank"],
                    "source_subscriber_count": row["subscriber_count_source"],
                    "resolved_subscriber_count": channel_info.get("subscriber_count", 0),
                    "sample_video_id_for_resolution": resolved["sample_video_id"],
                }
            )
            seen_channel_ids.add(channel_id)
            per_category_counts[cat["label"]] += 1
            stats["channels_added"] += 1

        if per_category_counts[cat["label"]] < args.top_n:
            print(
                f"  Warning: only {per_category_counts[cat['label']]}/{args.top_n} "
                "channels passed current filter in this category"
            )
            stats["category_underfilled"] += 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "source": "youtubers.me global category charts",
            "top_n_target_per_category": args.top_n,
            "note": "Country whitelist is a quick proxy for English-primary channels; transcript-language filtering during evaluation remains the stronger gate.",
            "enforce_country_whitelist": args.enforce_country_whitelist,
            "english_country_whitelist": sorted(args.english_country_whitelist),
        },
        "channels": channels_out,
        "category_counts": dict(sorted(per_category_counts.items())),
        "stats": dict(stats),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"\nSaved {len(channels_out)} channels to {args.output}")
    print("Per-category counts:")
    for k, v in sorted(per_category_counts.items()):
        print(f"  {k}: {v}")
    print("Stats:")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
