#!/usr/bin/env python3
"""
Build a launch-quality channel list for public rankings.

Goals:
- Start from large category-ranked candidate pools (youtubers.me by default).
- Keep selection country-agnostic while enforcing English-primary accessibility.
- Include Indian English channels (country is a hint, not a gate).
- Export a reviewable final list and candidate audit CSVs.

This script is designed to be extensible:
- Automated discovery: youtubers.me (implemented)
- Manual CSV imports: Social Blade / vidIQ / other charts (generic parser implemented)

The final JSON output keeps the project-compatible shape:
  {"channels": [{"handle": "@...", "category": "...", ...}, ...]}
with extra metadata fields used for review and future tuning.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from build_youtubers_top10_categories import (
    BASE as YOUTUBERS_BASE,
    extract_category_links,
    extract_top_rows,
    extract_video_ids_from_youtuber_stats,
    fetch as youtubers_fetch,
    normalize_handle,
    resolve_channel_from_video_ids,
)
from transcript_fetcher import get_transcript, list_available_transcripts
from youtube_api import get_channel_info, get_channel_info_by_handle, get_channel_videos


DEFAULT_TARGET_CATEGORIES = [
    "Autos Vehicles",
    "Comedy",
    "Education",
    "Entertainment",
    "Film Animation",
    "Gaming",
    "Howto Style",
    "News Politics",
    "Pets Animals",
    "Science Technology",
    "Sports",
    "Travel Events",
]

DEFAULT_PREFERRED_COUNTRIES = ["US", "GB", "CA", "AU", "NZ", "IE", "SG", "IN"]
DEFAULT_TITLE_EXCLUDE_KEYWORDS = [
    # Strongly family/kids/nursery-skewed mega channels often irrelevant to the target launch audience.
    "nursery rhyme",
    "nursery rhymes",
    "kids songs",
    "baby shark",
    "cocomelon",
]
DEFAULT_HARD_EXCLUDE_TITLE_KEYWORDS = [
    "kids",
    "kid ",
    "baby",
    "nursery",
    "cartoon",
    "toys",
    "toy ",
    "playhouse",
    "rhymes",
    "music",
    "vevo",
    "official audio",
    "nursery rhyme",
    "nursery rhymes",
    "kids songs",
    "baby shark",
    "cocomelon",
    "lullaby",
    "rhymes for kids",
]
DEFAULT_HARD_EXCLUDE_CATEGORY_KEYWORDS = [
    "music",
    "nursery",
    "kids",
]


def _clean_spaces(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _slugish(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _category_norm(label: str | None) -> str:
    raw = _clean_spaces(label)
    if not raw:
        return ""
    mapping = {
        "film & animation": "Film Animation",
        "film and animation": "Film Animation",
        "autos & vehicles": "Autos Vehicles",
        "autos and vehicles": "Autos Vehicles",
        "news & politics": "News Politics",
        "news and politics": "News Politics",
        "howto & style": "Howto Style",
        "howto and style": "Howto Style",
        "pets & animals": "Pets Animals",
        "pets and animals": "Pets Animals",
        "science & technology": "Science Technology",
        "science and technology": "Science Technology",
        "travel & events": "Travel Events",
        "travel and events": "Travel Events",
    }
    key = raw.lower()
    if key in mapping:
        return mapping[key]
    return raw.title().replace("&", "").replace("  ", " ")


def _lang_prefix(lang: str | None) -> str | None:
    if not lang:
        return None
    lang = lang.strip().lower()
    if not lang:
        return None
    if lang.startswith("zh-"):
        return "zh"
    return lang.split("-")[0]


def _extract_handle_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"youtube\.com/@([A-Za-z0-9._-]+)", url)
    if m:
        return "@" + m.group(1)
    return None


def _extract_channel_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"youtube\.com/channel/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


def _parse_int(value) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"[^\d]", "", s)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _safe_handle(handle: str | None) -> str | None:
    if not handle:
        return None
    handle = handle.strip()
    if not handle:
        return None
    if not handle.startswith("@"):
        handle = "@" + handle.lstrip("@")
    return handle


def _contains_keyword(text: str | None, keywords: list[str]) -> str | None:
    low = (text or "").lower()
    for kw in keywords:
        if kw and kw.lower() in low:
            return kw
    return None


def _hard_exclusion_reasons(
    candidate: dict,
    *,
    title_keywords: list[str],
    category_keywords: list[str],
) -> list[str]:
    reasons: list[str] = []

    title_kw = _contains_keyword(candidate.get("title"), title_keywords)
    if title_kw:
        reasons.append(f"title:{title_kw}")

    handle_kw = _contains_keyword(candidate.get("handle"), title_keywords)
    if handle_kw:
        reasons.append(f"handle:{handle_kw}")

    category_values: list[str] = []
    if candidate.get("category"):
        category_values.append(str(candidate["category"]))
    for cat in candidate.get("categories_seen", []) or []:
        category_values.append(str(cat))
    for ev in candidate.get("source_evidence", []) or []:
        if ev.get("category"):
            category_values.append(str(ev["category"]))

    for value in category_values:
        cat_kw = _contains_keyword(value, category_keywords)
        if cat_kw:
            reasons.append(f"category:{cat_kw}")
            break

    return reasons


def _generic_csv_aliases() -> dict[str, list[str]]:
    return {
        "title": ["title", "name", "channel", "channel_name", "channel_title"],
        "handle": ["handle", "youtube_handle", "yt_handle"],
        "channel_id": ["channel_id", "youtube_channel_id"],
        "channel_url": ["channel_url", "youtube_url", "url", "link"],
        "category": ["category", "category_name"],
        "country": ["country", "country_code"],
        "subscriber_count": ["subscriber_count", "subscribers", "subs"],
        "source_rank": ["rank", "source_rank", "position"],
    }


def _pick_row_value(row: dict, aliases: list[str]) -> str | None:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in aliases:
        if alias in lowered:
            v = lowered[alias]
            if v is not None and str(v).strip():
                return str(v).strip()
    return None


def load_generic_csv_candidates(path: Path, source_name: str) -> list[dict]:
    aliases = _generic_csv_aliases()
    out: list[dict] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = _pick_row_value(row, aliases["title"])
            category = _category_norm(_pick_row_value(row, aliases["category"]))
            handle = _safe_handle(_pick_row_value(row, aliases["handle"]))
            channel_url = _pick_row_value(row, aliases["channel_url"])
            if not handle:
                handle = _extract_handle_from_url(channel_url)
            channel_id = _pick_row_value(row, aliases["channel_id"]) or _extract_channel_id_from_url(channel_url)
            candidate = {
                "title": title,
                "handle": handle,
                "channel_id": channel_id,
                "category": category,
                "country": (_pick_row_value(row, aliases["country"]) or "").upper() or None,
                "source": source_name,
                "source_rank": _parse_int(_pick_row_value(row, aliases["source_rank"])),
                "source_subscriber_count": _parse_int(_pick_row_value(row, aliases["subscriber_count"])),
                "raw_source_row": row,
            }
            if not (candidate["channel_id"] or candidate["handle"]):
                continue
            out.append(candidate)
    return out


def load_existing_source_json(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out: list[dict] = []
    for row in data.get("channels", []):
        out.append(
            {
                "title": row.get("title"),
                "handle": _safe_handle(row.get("handle")),
                "channel_id": row.get("channel_id"),
                "category": _category_norm(row.get("category")),
                "country": (row.get("country") or "").upper() or None,
                "source": row.get("source") or "existing_json",
                "source_rank": row.get("source_rank"),
                "source_subscriber_count": row.get("source_subscriber_count") or row.get("resolved_subscriber_count"),
                "raw_source_row": row,
            }
        )
    return out


def discover_youtubers_candidates(
    *,
    top_n_target_per_category: int,
    overfetch_multiplier: int,
    top_page_size: int,
    categories: set[str] | None,
    seed_page: str,
    sleep_seconds: float,
) -> tuple[list[dict], dict]:
    """
    Discover candidates from youtubers.me category charts. Overfetches rows and does not apply
    country/language filtering. Channel resolution still uses YouTube API via sample video IDs.
    """
    def _fetch_with_retries(url: str, retries: int = 3, base_sleep: float = 0.4) -> str:
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                return youtubers_fetch(url)
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(base_sleep * attempt)
                    continue
                raise last_err

    print(f"[discover:youtubers] Seed page: {seed_page}")
    seed_html = _fetch_with_retries(seed_page, retries=4)
    category_links = extract_category_links(seed_html)
    if categories:
        category_links = [c for c in category_links if _category_norm(c["label"]) in categories or c["slug"] in categories]
    dedup = {}
    for c in category_links:
        dedup.setdefault(c["slug"], c)
    category_links = list(dedup.values())

    stats = Counter()
    out: list[dict] = []
    for cat in category_links:
        category_label = _category_norm(cat["label"])
        category_url = (
            f"{YOUTUBERS_BASE}/global/{cat['slug']}/top-{top_page_size}-youtube-channels"
            if top_page_size else YOUTUBERS_BASE + cat["href"]
        )
        try:
            html = _fetch_with_retries(category_url, retries=4)
        except Exception as e:
            print(f"  [discover:youtubers] Failed category {cat['slug']}: {e}")
            stats["category_fetch_fail"] += 1
            continue
        time.sleep(sleep_seconds)

        max_rows = max(top_n_target_per_category * overfetch_multiplier, top_n_target_per_category)
        rows = extract_top_rows(html, top_n=max_rows)
        stats["categories_seen"] += 1
        stats["rows_parsed_total"] += len(rows)
        print(f"  [discover:youtubers] {category_label}: parsed {len(rows)} rows")

        for row in rows:
            stats["rows_considered"] += 1
            ytm_slug = row["youtubers_slug"]
            stats_url = f"{YOUTUBERS_BASE}/{ytm_slug}/youtuber-stats"
            try:
                ytm_html = _fetch_with_retries(stats_url, retries=2, base_sleep=0.2)
            except Exception:
                stats["stats_fetch_fail"] += 1
                continue
            time.sleep(sleep_seconds)

            video_ids = extract_video_ids_from_youtuber_stats(ytm_html)
            if not video_ids:
                stats["no_video_ids"] += 1
                continue

            resolved = resolve_channel_from_video_ids(video_ids)
            if not resolved:
                stats["video_resolve_fail"] += 1
                continue

            channel_info = get_channel_info(resolved["channel_id"])
            if not channel_info:
                stats["channel_info_fail"] += 1
                continue

            handle = normalize_handle(channel_info.get("custom_url"))
            if not handle:
                stats["missing_handle"] += 1
                continue

            out.append(
                {
                    "title": channel_info.get("title") or row["display_name"],
                    "handle": _safe_handle(handle),
                    "channel_id": channel_info["channel_id"],
                    "category": category_label,
                    "country": (channel_info.get("country") or "").upper() or None,
                    "source": "youtubers.me",
                    "source_rank": row.get("rank"),
                    "source_subscriber_count": row.get("subscriber_count_source"),
                    "resolved_subscriber_count": channel_info.get("subscriber_count"),
                    "sample_video_id_for_resolution": resolved.get("sample_video_id"),
                    "raw_source_row": row,
                }
            )
            stats["candidates_discovered"] += 1

    return out, dict(stats)


def merge_candidates(candidates: Iterable[dict]) -> list[dict]:
    """
    Merge duplicate candidates across sources/categories while preserving source evidence.
    Dedupe key priority: channel_id > handle > normalized title.
    """
    merged: dict[str, dict] = {}
    for c in candidates:
        key = (
            c.get("channel_id")
            or c.get("handle")
            or f"title::{_slugish(c.get('title'))}"
        )
        if not key:
            continue
        if key not in merged:
            merged[key] = {
                "title": c.get("title"),
                "handle": _safe_handle(c.get("handle")),
                "channel_id": c.get("channel_id"),
                "country": c.get("country"),
                "categories_seen": [],
                "source_evidence": [],
                "source_subscriber_count_max": c.get("source_subscriber_count") or c.get("resolved_subscriber_count"),
                "resolved_subscriber_count": c.get("resolved_subscriber_count"),
            }
        m = merged[key]
        if c.get("title") and (not m.get("title") or len(str(c.get("title"))) > len(str(m.get("title") or ""))):
            m["title"] = c.get("title")
        if c.get("handle") and not m.get("handle"):
            m["handle"] = _safe_handle(c.get("handle"))
        if c.get("channel_id") and not m.get("channel_id"):
            m["channel_id"] = c.get("channel_id")
        if c.get("country") and not m.get("country"):
            m["country"] = c.get("country")

        cat = _category_norm(c.get("category"))
        if cat and cat not in m["categories_seen"]:
            m["categories_seen"].append(cat)

        src_ev = {
            "source": c.get("source"),
            "category": cat,
            "source_rank": c.get("source_rank"),
            "source_subscriber_count": c.get("source_subscriber_count"),
        }
        if src_ev not in m["source_evidence"]:
            m["source_evidence"].append(src_ev)

        for sub_key in ["source_subscriber_count", "resolved_subscriber_count"]:
            val = c.get(sub_key)
            if isinstance(val, int):
                cur_key = "source_subscriber_count_max" if sub_key == "source_subscriber_count" else "resolved_subscriber_count"
                cur = m.get(cur_key)
                if not isinstance(cur, int) or val > cur:
                    m[cur_key] = val
    return list(merged.values())


def _choose_primary_category(candidate: dict, target_categories: set[str]) -> str | None:
    """
    Prefer the best-ranked source category among target categories.
    """
    best = None
    best_rank = None
    for ev in candidate.get("source_evidence", []):
        cat = _category_norm(ev.get("category"))
        if not cat or cat not in target_categories:
            continue
        rank = ev.get("source_rank")
        rank_key = rank if isinstance(rank, int) else 10**9
        if best is None or rank_key < best_rank:
            best = cat
            best_rank = rank_key
    if best:
        return best
    for cat in candidate.get("categories_seen", []):
        cat = _category_norm(cat)
        if cat in target_categories:
            return cat
    return None


def verify_and_enrich_candidate(candidate: dict) -> dict:
    """
    Fill canonical channel metadata from YouTube API using channel_id or handle.
    """
    out = dict(candidate)
    channel_info = None
    if out.get("channel_id"):
        channel_info = get_channel_info(out["channel_id"])
    if not channel_info and out.get("handle"):
        channel_info = get_channel_info_by_handle(out["handle"])

    if not channel_info:
        out["verification_status"] = "channel_not_found"
        return out

    out["verification_status"] = "ok"
    out["channel_id"] = channel_info["channel_id"]
    out["title"] = channel_info.get("title") or out.get("title")
    out["handle"] = _safe_handle(normalize_handle(channel_info.get("custom_url")) or out.get("handle"))
    out["country"] = (channel_info.get("country") or out.get("country") or "").upper() or None
    out["subscriber_count"] = channel_info.get("subscriber_count")
    out["video_count"] = channel_info.get("video_count")
    out["view_count"] = channel_info.get("view_count")
    out["uploads_playlist_id"] = channel_info.get("uploads_playlist_id")
    return out


def sample_channel_language_profile(
    *,
    channel_id: str,
    sample_target_videos: int,
    sample_pool_videos: int,
    language_check_mode: str,
    sleep_seconds: float,
) -> dict:
    """
    Build a lightweight language profile using recent long-form videos.

    Modes:
    - none: no network transcript checks (returns unknown profile)
    - tracks: uses transcript track listing (fastest)
    - transcript: fetches English transcript for confirmation (slower, stronger)
    """
    profile = {
        "method": language_check_mode,
        "sample_target_videos": sample_target_videos,
        "sample_pool_videos": sample_pool_videos,
        "sampled_videos": 0,
        "videos_with_declared_lang": 0,
        "videos_with_transcript_tracks": 0,
        "english_declared_ratio": None,
        "english_caption_track_ratio": None,
        "english_transcript_fetch_ratio": None,
        "primary_language_hint": None,
        "declared_language_counts": {},
        "caption_track_language_counts": {},
        "sample_rows": [],
        "english_primary": False,
        "error": None,
    }

    if language_check_mode == "none":
        profile["error"] = "language check disabled"
        return profile

    videos = get_channel_videos(channel_id, max_videos=max(sample_target_videos, sample_pool_videos))
    if not videos:
        profile["error"] = "no eligible videos for sampling"
        return profile

    declared_counts = Counter()
    caption_lang_counts = Counter()
    declared_en = 0
    declared_den = 0
    caption_en = 0
    caption_den = 0
    fetched_en = 0
    fetched_den = 0

    sampled_rows: list[dict] = []
    for video in videos[:sample_pool_videos]:
        if len(sampled_rows) >= sample_target_videos:
            break
        row = {
            "video_id": video.get("video_id"),
            "title": video.get("title"),
            "duration_seconds": video.get("duration_seconds"),
            "published_at": video.get("published_at"),
            "default_audio_language": video.get("default_audio_language"),
            "default_language": video.get("default_language"),
            "declared_lang_prefix": None,
            "has_english_caption_track": None,
            "english_transcript_fetch_success": None,
            "transcript_language_code": None,
        }
        declared = _lang_prefix(video.get("default_audio_language")) or _lang_prefix(video.get("default_language"))
        if declared:
            row["declared_lang_prefix"] = declared
            declared_counts[declared] += 1
            declared_den += 1
            if declared == "en":
                declared_en += 1
        sampled_rows.append(row)

    # Fast path: if enough declared language metadata exists, avoid transcript-track probing entirely.
    need_caption_checks = True
    if declared_den >= max(2, sample_target_videos - 1):
        need_caption_checks = False

    if need_caption_checks and language_check_mode in {"tracks", "transcript"}:
        for row in sampled_rows:
            track_result = list_available_transcripts(row["video_id"])
            if track_result.get("success"):
                profile["videos_with_transcript_tracks"] += 1
                tracks = track_result.get("transcripts", [])
                has_en_track = False
                for t in tracks:
                    code = _lang_prefix(t.get("language_code"))
                    label = (t.get("language") or "").lower()
                    if code:
                        caption_lang_counts[code] += 1
                    if code == "en" or ("english" in label):
                        has_en_track = True
                row["has_english_caption_track"] = has_en_track
                caption_den += 1
                if has_en_track:
                    caption_en += 1
            else:
                row["has_english_caption_track"] = False

            if language_check_mode == "transcript":
                tx = get_transcript(row["video_id"], language_codes=["en", "en-US", "en-GB"])
                fetched_den += 1
                row["english_transcript_fetch_success"] = bool(tx.get("success"))
                row["transcript_language_code"] = tx.get("language_code")
                if tx.get("success") and _lang_prefix(tx.get("language_code")) == "en":
                    fetched_en += 1
                time.sleep(sleep_seconds)

            time.sleep(sleep_seconds)

    profile["sample_rows"] = sampled_rows
    profile["sampled_videos"] = len(sampled_rows)

    profile["declared_language_counts"] = dict(declared_counts)
    profile["caption_track_language_counts"] = dict(caption_lang_counts)
    profile["videos_with_declared_lang"] = declared_den
    profile["english_declared_ratio"] = (declared_en / declared_den) if declared_den else None
    profile["english_caption_track_ratio"] = (caption_en / caption_den) if caption_den else None
    profile["english_transcript_fetch_ratio"] = (fetched_en / fetched_den) if fetched_den else None

    primary_hint = None
    if declared_counts:
        primary_hint = declared_counts.most_common(1)[0][0]
    elif caption_lang_counts:
        primary_hint = caption_lang_counts.most_common(1)[0][0]
    profile["primary_language_hint"] = primary_hint
    return profile


def classify_english_primary(profile: dict, *, english_ratio_threshold: float, min_sample_videos: int) -> tuple[bool, str]:
    """
    Conservative classification:
    1) Prefer declared audio/default language ratio when available
    2) Fall back to English caption track ratio (tracks mode)
    3) Fall back to transcript fetch ratio (transcript mode)
    """
    sampled = int(profile.get("sampled_videos") or 0)
    if sampled < min_sample_videos:
        return False, f"insufficient_samples:{sampled}"

    declared_ratio = profile.get("english_declared_ratio")
    if declared_ratio is not None:
        if declared_ratio >= english_ratio_threshold:
            return True, f"declared_ratio:{declared_ratio:.2f}"
        return False, f"declared_ratio:{declared_ratio:.2f}"

    caption_ratio = profile.get("english_caption_track_ratio")
    if caption_ratio is not None:
        if caption_ratio >= english_ratio_threshold:
            return True, f"caption_track_ratio:{caption_ratio:.2f}"
        return False, f"caption_track_ratio:{caption_ratio:.2f}"

    fetch_ratio = profile.get("english_transcript_fetch_ratio")
    if fetch_ratio is not None:
        if fetch_ratio >= english_ratio_threshold:
            return True, f"transcript_fetch_ratio:{fetch_ratio:.2f}"
        return False, f"transcript_fetch_ratio:{fetch_ratio:.2f}"

    return False, "no_language_signal"


def compute_launch_relevance_score(
    candidate: dict,
    *,
    preferred_countries: set[str],
    suppress_keywords: list[str],
    prefer_mainstream_titles: bool,
) -> tuple[float, dict]:
    """
    Heuristic ranking score for launch ordering within a category.
    Transparent and intentionally simple.
    """
    breakdown = {}
    subs = candidate.get("subscriber_count") or candidate.get("source_subscriber_count_max") or 0
    # Log scale makes 1M vs 10M matter, while not letting 100M channels dominate everything.
    subs_score = min(60.0, max(0.0, math.log10(max(1, subs)) * 10.0))
    breakdown["subscriber_log_score"] = round(subs_score, 3)

    source_evidence = candidate.get("source_evidence", [])
    source_count_score = min(12.0, float(len({(ev.get("source"), ev.get("category")) for ev in source_evidence})) * 3.0)
    breakdown["source_presence_score"] = round(source_count_score, 3)

    rank_scores = []
    for ev in source_evidence:
        r = ev.get("source_rank")
        if isinstance(r, int) and r > 0:
            # rank 1 -> 10, rank 50 -> 2, rank 100 -> 1
            rank_scores.append(max(0.0, 12.0 / math.sqrt(r)))
    rank_score = (sum(rank_scores) / len(rank_scores)) if rank_scores else 0.0
    breakdown["source_rank_score"] = round(rank_score, 3)

    country = (candidate.get("country") or "").upper()
    country_score = 3.0 if country in preferred_countries else 0.0
    # Extra explicit boost for India so English-speaking Indian channels aren't crowded out by the previous bias.
    if country == "IN":
        country_score += 2.0
    breakdown["country_score"] = round(country_score, 3)

    title = (candidate.get("title") or "").lower()
    keyword_penalty = 0.0
    keyword_hits = []
    if prefer_mainstream_titles:
        for kw in suppress_keywords:
            if kw in title:
                keyword_penalty -= 6.0
                keyword_hits.append(kw)
    breakdown["title_keyword_penalty"] = round(keyword_penalty, 3)
    if keyword_hits:
        breakdown["title_keyword_hits"] = keyword_hits

    score = subs_score + source_count_score + rank_score + country_score + keyword_penalty
    return round(score, 4), breakdown


def export_review_csvs(*, review_prefix: Path, selected_channels: list[dict], all_candidates: list[dict]) -> dict[str, str]:
    review_prefix.parent.mkdir(parents=True, exist_ok=True)
    category_pivot_path = review_prefix.parent / f"{review_prefix.name}_category_pivot.csv"
    selected_rows_path = review_prefix.parent / f"{review_prefix.name}_selected_channels.csv"
    all_rows_path = review_prefix.parent / f"{review_prefix.name}_all_candidates.csv"

    # Category pivot
    cat_groups = defaultdict(list)
    for c in selected_channels:
        cat_groups[c.get("category")].append(c)
    with open(category_pivot_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "category", "selected_channels", "channel_names", "countries",
            "english_primary_count", "avg_subscriber_count",
        ])
        for cat in sorted(cat_groups):
            rows = cat_groups[cat]
            names = " | ".join(c.get("title") or c.get("handle") or c.get("channel_id") for c in rows)
            countries = " | ".join(sorted({(c.get("country") or "—") for c in rows}))
            en_count = sum(1 for c in rows if c.get("language_profile", {}).get("english_primary"))
            subs = [c.get("subscriber_count") for c in rows if isinstance(c.get("subscriber_count"), int)]
            avg_subs = round(sum(subs) / len(subs)) if subs else ""
            w.writerow([cat, len(rows), names, countries, en_count, avg_subs])

    fieldnames = [
        "selected_rank_in_category",
        "selection_tier",
        "category",
        "title",
        "handle",
        "channel_id",
        "country",
        "subscriber_count",
        "video_count",
        "primary_language_hint",
        "english_primary",
        "english_primary_reason",
        "sampled_videos",
        "english_declared_ratio",
        "english_caption_track_ratio",
        "english_transcript_fetch_ratio",
        "launch_relevance_score",
        "source_count",
        "source_evidence_compact",
        "verification_status",
    ]

    def _write_channel_rows(path: Path, rows: list[dict], include_selected_rank: bool):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for c in rows:
                lp = c.get("language_profile", {}) or {}
                row = {
                    "selected_rank_in_category": c.get("selected_rank_in_category") if include_selected_rank else "",
                    "selection_tier": c.get("selection_tier", ""),
                    "category": c.get("category"),
                    "title": c.get("title"),
                    "handle": c.get("handle"),
                    "channel_id": c.get("channel_id"),
                    "country": c.get("country"),
                    "subscriber_count": c.get("subscriber_count"),
                    "video_count": c.get("video_count"),
                    "primary_language_hint": lp.get("primary_language_hint"),
                    "english_primary": lp.get("english_primary"),
                    "english_primary_reason": lp.get("english_primary_reason"),
                    "sampled_videos": lp.get("sampled_videos"),
                    "english_declared_ratio": lp.get("english_declared_ratio"),
                    "english_caption_track_ratio": lp.get("english_caption_track_ratio"),
                    "english_transcript_fetch_ratio": lp.get("english_transcript_fetch_ratio"),
                    "launch_relevance_score": c.get("launch_relevance_score"),
                    "source_count": len(c.get("source_evidence", [])),
                    "source_evidence_compact": " | ".join(
                        f"{ev.get('source')}:{ev.get('category')}#{ev.get('source_rank')}"
                        for ev in c.get("source_evidence", [])
                    ),
                    "verification_status": c.get("verification_status"),
                }
                w.writerow(row)

    _write_channel_rows(selected_rows_path, selected_channels, include_selected_rank=True)
    _write_channel_rows(all_rows_path, all_candidates, include_selected_rank=False)

    return {
        "category_pivot_csv": str(category_pivot_path),
        "selected_channels_csv": str(selected_rows_path),
        "all_candidates_csv": str(all_rows_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build launch channel list (global + India, English-primary)")
    parser.add_argument("--top-n-per-category", type=int, default=15)
    parser.add_argument(
        "--buffer-count-per-category",
        type=int,
        default=10,
        help="Additional sequential buffer slots after the strict top-N. Final exported target per category = top_n_per_category + buffer_count_per_category.",
    )
    parser.add_argument("--output", type=Path, default=Path(__file__).parent.parent / "data" / "channels_top15_per_category_launch.json")
    parser.add_argument(
        "--review-prefix",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "benchmarks" / "launch_channel_list",
        help="Prefix (directory + base filename) for review CSV exports",
    )

    # Discovery sources
    parser.add_argument("--skip-youtubers-discovery", action="store_true")
    parser.add_argument("--youtubers-top-page-size", type=int, default=300)
    parser.add_argument("--youtubers-overfetch-multiplier", type=int, default=25)
    parser.add_argument(
        "--youtubers-seed-page",
        default=f"{YOUTUBERS_BASE}/global/gaming/top-1000-gaming-most_subscribed-youtube-channels",
    )
    parser.add_argument(
        "--source-json",
        action="append",
        default=[],
        help="Existing channels JSON payload(s) to import as candidate sources (repeatable)",
    )
    parser.add_argument(
        "--csv-source",
        action="append",
        default=[],
        help="Generic CSV source import in form source_name=/path/to/file.csv (repeatable)",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help="Category labels or youtubers slugs to include. Default is standard 12 launch categories.",
    )

    # Language eligibility
    parser.add_argument("--language-sample-videos", type=int, default=5)
    parser.add_argument("--language-sample-pool-videos", type=int, default=8)
    parser.add_argument(
        "--language-check-mode",
        choices=["none", "tracks", "transcript"],
        default="tracks",
        help="tracks = caption-track listing (faster), transcript = fetch English transcripts (stronger)",
    )
    parser.add_argument("--english-ratio-threshold", type=float, default=0.70)
    parser.add_argument("--min-language-sample-videos", type=int, default=3)
    parser.add_argument(
        "--allow-buffer-fill-from-non-english",
        action="store_true",
        default=True,
        help="If strict English-primary candidates are insufficient, fill remaining buffer slots (and if necessary top-N slots) from the highest-ranked non-English/insufficient candidates, clearly tagged as buffer/fallback.",
    )
    parser.add_argument(
        "--no-allow-buffer-fill-from-non-english",
        dest="allow_buffer_fill_from_non_english",
        action="store_false",
    )

    # Ranking / relevance
    parser.add_argument(
        "--preferred-countries",
        nargs="*",
        default=DEFAULT_PREFERRED_COUNTRIES,
        help="Country codes that get a mild launch relevance boost (inclusion is NOT gated on this)",
    )
    parser.add_argument("--prefer-mainstream-titles", action="store_true", default=True)
    parser.add_argument(
        "--suppress-title-keywords",
        nargs="*",
        default=DEFAULT_TITLE_EXCLUDE_KEYWORDS,
        help="Low-penalty keyword list to de-prioritize channels mismatched to the launch audience.",
    )
    parser.add_argument("--no-prefer-mainstream-titles", dest="prefer_mainstream_titles", action="store_false")
    parser.add_argument(
        "--hard-exclude-title-keywords",
        nargs="*",
        default=DEFAULT_HARD_EXCLUDE_TITLE_KEYWORDS,
        help="Hard exclude channels if title/handle contains any of these keywords.",
    )
    parser.add_argument(
        "--hard-exclude-category-keywords",
        nargs="*",
        default=DEFAULT_HARD_EXCLUDE_CATEGORY_KEYWORDS,
        help="Hard exclude channels if source/category labels contain any of these keywords.",
    )
    parser.add_argument(
        "--disable-hard-exclusions",
        action="store_true",
        help="Disable hard exclusion gates (not recommended).",
    )

    parser.add_argument("--request-sleep-seconds", type=float, default=0.3)
    parser.add_argument("--max-candidates-per-category", type=int, default=120, help="Cap candidates retained per category before language checks")
    parser.add_argument("--skip-verification", action="store_true", help="Skip YouTube API verification (faster offline schema tests)")
    parser.add_argument("--dry-run", action="store_true", help="Skip transcript-language sampling and only build/verify candidate pool")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_categories = set(args.categories or DEFAULT_TARGET_CATEGORIES)

    all_raw_candidates: list[dict] = []
    stats = Counter()
    source_stats: dict[str, dict] = {}

    discovery_target_per_category = args.top_n_per_category + args.buffer_count_per_category

    if not args.skip_youtubers_discovery:
        cands, y_stats = discover_youtubers_candidates(
            top_n_target_per_category=discovery_target_per_category,
            overfetch_multiplier=args.youtubers_overfetch_multiplier,
            top_page_size=args.youtubers_top_page_size,
            categories=target_categories,
            seed_page=args.youtubers_seed_page,
            sleep_seconds=args.request_sleep_seconds,
        )
        source_stats["youtubers.me"] = y_stats
        all_raw_candidates.extend(cands)
        stats["raw_candidates_total"] += len(cands)

    for path_str in args.source_json:
        path = Path(path_str)
        cands = load_existing_source_json(path)
        all_raw_candidates.extend(cands)
        source_stats[f"source_json:{path.name}"] = {"candidates_loaded": len(cands)}
        stats["raw_candidates_total"] += len(cands)

    for spec in args.csv_source:
        if "=" not in spec:
            print(f"Invalid --csv-source value (expected source=path): {spec}", file=sys.stderr)
            return 2
        source_name, path_str = spec.split("=", 1)
        path = Path(path_str)
        cands = load_generic_csv_candidates(path, source_name=source_name)
        all_raw_candidates.extend(cands)
        source_stats[f"csv:{source_name}:{path.name}"] = {"candidates_loaded": len(cands)}
        stats["raw_candidates_total"] += len(cands)

    if not all_raw_candidates:
        print("No candidates discovered/loaded. Provide at least one source.")
        return 1

    merged_candidates = merge_candidates(all_raw_candidates)
    stats["merged_candidates"] = len(merged_candidates)
    print(f"Merged candidates: {len(merged_candidates)} (from {len(all_raw_candidates)} raw rows)")

    # Attach primary category for ranking/selecting.
    prefiltered = []
    for c in merged_candidates:
        primary_category = _choose_primary_category(c, target_categories)
        if not primary_category:
            stats["no_target_category"] += 1
            continue
        c["category"] = primary_category
        prefiltered.append(c)

    # Limit verification workload by category using subscriber/rank priors before API verification.
    cat_buckets = defaultdict(list)
    for c in prefiltered:
        subs = c.get("source_subscriber_count_max") or 0
        best_rank = min(
            [ev.get("source_rank") for ev in c.get("source_evidence", []) if isinstance(ev.get("source_rank"), int)],
            default=10**9,
        )
        c["_prefilter_sort"] = (-(subs or 0), best_rank, (c.get("title") or c.get("handle") or ""))
        cat_buckets[c["category"]].append(c)

    verification_queue: list[dict] = []
    for cat in sorted(cat_buckets):
        rows = sorted(cat_buckets[cat], key=lambda x: x["_prefilter_sort"])[: args.max_candidates_per_category]
        for r in rows:
            r.pop("_prefilter_sort", None)
        verification_queue.extend(rows)
        print(f"[verify-queue] {cat}: {len(rows)} candidates")

    # Verify and enrich
    verified_candidates: list[dict] = []
    if args.skip_verification:
        print("[verify] skipped (--skip-verification)")
        for c in verification_queue:
            c = dict(c)
            c["verification_status"] = "skipped"
            # Promote best available subscriber count to subscriber_count for ranking and review.
            if not c.get("subscriber_count"):
                c["subscriber_count"] = c.get("resolved_subscriber_count") or c.get("source_subscriber_count_max")
            if c.get("handle"):
                verified_candidates.append(c)
            else:
                stats["missing_handle_after_verify"] += 1
        stats["verified_candidates"] = len(verified_candidates)
    else:
        for idx, c in enumerate(verification_queue, 1):
            print(f"[verify] {idx}/{len(verification_queue)} {c.get('title') or c.get('handle') or c.get('channel_id')}")
            enriched = verify_and_enrich_candidate(c)
            if enriched.get("verification_status") != "ok":
                stats["channel_verify_fail"] += 1
                continue
            if not enriched.get("handle"):
                stats["missing_handle_after_verify"] += 1
                continue
            verified_candidates.append(enriched)
            time.sleep(args.request_sleep_seconds)
    stats["verified_candidates"] = len(verified_candidates)
    print(f"Verified candidates: {len(verified_candidates)}")

    # Dedupe again by canonical channel_id after verification (important if multiple source rows mapped late).
    dedup_verified = {}
    for c in verified_candidates:
        key = c.get("channel_id") or c.get("handle")
        if key not in dedup_verified:
            dedup_verified[key] = c
        else:
            # Merge source evidence and categories.
            existing = dedup_verified[key]
            for ev in c.get("source_evidence", []):
                if ev not in existing.get("source_evidence", []):
                    existing.setdefault("source_evidence", []).append(ev)
            for cat in c.get("categories_seen", []):
                if cat not in existing.get("categories_seen", []):
                    existing.setdefault("categories_seen", []).append(cat)
            # Prefer the better-ranked category if needed.
            existing["category"] = _choose_primary_category(existing, target_categories) or existing.get("category")
            if (c.get("subscriber_count") or 0) > (existing.get("subscriber_count") or 0):
                existing["subscriber_count"] = c.get("subscriber_count")
            if not existing.get("country") and c.get("country"):
                existing["country"] = c.get("country")
            if not existing.get("title") and c.get("title"):
                existing["title"] = c.get("title")
            if not existing.get("handle") and c.get("handle"):
                existing["handle"] = c.get("handle")
    verified_candidates = list(dedup_verified.values())
    stats["verified_candidates_deduped"] = len(verified_candidates)

    # Hard exclusion gates for categories/titles explicitly out of scope.
    if not args.disable_hard_exclusions:
        kept: list[dict] = []
        hard_excluded_samples: list[dict] = []
        for c in verified_candidates:
            reasons = _hard_exclusion_reasons(
                c,
                title_keywords=args.hard_exclude_title_keywords,
                category_keywords=args.hard_exclude_category_keywords,
            )
            if reasons:
                c["hard_exclusion_reasons"] = reasons
                stats["hard_excluded_candidates"] += 1
                if len(hard_excluded_samples) < 20:
                    hard_excluded_samples.append(
                        {
                            "title": c.get("title"),
                            "handle": c.get("handle"),
                            "category": c.get("category"),
                            "reasons": reasons,
                        }
                    )
                continue
            kept.append(c)
        verified_candidates = kept
        if hard_excluded_samples:
            print("[hard-exclude] sample excluded channels:")
            for s in hard_excluded_samples:
                print(
                    f"  - {s.get('title') or s.get('handle')} | "
                    f"{s.get('category')} | reasons={','.join(s.get('reasons') or [])}"
                )
    stats["verified_candidates_after_hard_exclusions"] = len(verified_candidates)

    # Language sampling / eligibility
    for idx, c in enumerate(verified_candidates, 1):
        label = c.get("title") or c.get("handle") or c.get("channel_id")
        if args.dry_run:
            lp = {
                "method": "none",
                "sample_target_videos": args.language_sample_videos,
                "sample_pool_videos": args.language_sample_pool_videos,
                "sampled_videos": 0,
                "english_primary": True,  # keep all candidates in dry-run for ranking review
                "english_primary_reason": "dry_run_assumed_true",
                "primary_language_hint": None,
            }
            c["language_profile"] = lp
            continue

        print(f"[lang] {idx}/{len(verified_candidates)} {label}")
        lp = sample_channel_language_profile(
            channel_id=c["channel_id"],
            sample_target_videos=args.language_sample_videos,
            sample_pool_videos=args.language_sample_pool_videos,
            language_check_mode=args.language_check_mode,
            sleep_seconds=args.request_sleep_seconds,
        )
        english_primary, reason = classify_english_primary(
            lp,
            english_ratio_threshold=args.english_ratio_threshold,
            min_sample_videos=args.min_language_sample_videos,
        )
        lp["english_primary"] = english_primary
        lp["english_primary_reason"] = reason
        c["language_profile"] = lp
        if english_primary:
            stats["english_primary_candidates"] += 1
        else:
            stats["non_english_or_insufficient"] += 1

    preferred_countries = set([c.upper() for c in args.preferred_countries])

    # Score and select
    strict_by_cat = defaultdict(list)
    fallback_by_cat = defaultdict(list)
    all_candidates_for_review: list[dict] = []
    for c in verified_candidates:
        lp = c.get("language_profile", {}) or {}
        c["english_primary"] = bool(lp.get("english_primary"))
        score, breakdown = compute_launch_relevance_score(
            c,
            preferred_countries=preferred_countries,
            suppress_keywords=args.suppress_title_keywords,
            prefer_mainstream_titles=args.prefer_mainstream_titles,
        )
        c["launch_relevance_score"] = score
        c["launch_relevance_breakdown"] = breakdown
        all_candidates_for_review.append(c)
        if c["english_primary"]:
            strict_by_cat[c["category"]].append(c)
        else:
            fallback_by_cat[c["category"]].append(c)

    selected_channels: list[dict] = []
    category_counts = {}
    strict_category_counts = {}
    buffer_category_counts = {}
    underfilled_categories = {}
    total_per_category_target = args.top_n_per_category + args.buffer_count_per_category
    for cat in sorted(target_categories):
        strict_rows = strict_by_cat.get(cat, [])
        strict_rows = sorted(
            strict_rows,
            key=lambda c: (
                -(c.get("launch_relevance_score") or 0.0),
                -(c.get("subscriber_count") or 0),
                (c.get("title") or ""),
            ),
        )
        fallback_rows = sorted(
            fallback_by_cat.get(cat, []),
            key=lambda c: (
                # Prefer insufficient/unknown over explicitly non-English for manual review buffers.
                0 if str(c.get("language_profile", {}).get("english_primary_reason", "")).startswith("insufficient_samples") else 1,
                -(c.get("launch_relevance_score") or 0.0),
                -(c.get("subscriber_count") or 0),
                (c.get("title") or ""),
            ),
        )

        selected: list[dict] = []
        # Fill strict top-N first
        for c in strict_rows[: args.top_n_per_category]:
            c = dict(c)
            c["selection_tier"] = "strict_top"
            selected.append(c)

        # If strict top-N underfilled, optionally fill from fallback to protect the user's minimum target.
        if args.allow_buffer_fill_from_non_english and len(selected) < args.top_n_per_category:
            need = args.top_n_per_category - len(selected)
            for c in fallback_rows[:need]:
                c = dict(c)
                c["selection_tier"] = "fallback_top"
                selected.append(c)
            fallback_rows = fallback_rows[need:]

        # Add buffer slots after top-N
        if args.allow_buffer_fill_from_non_english:
            remaining_slots = max(0, total_per_category_target - len(selected))
            strict_buffer_candidates = strict_rows[args.top_n_per_category :]
            # strict buffers first, then fallback buffers
            for c in strict_buffer_candidates[:remaining_slots]:
                c = dict(c)
                c["selection_tier"] = "strict_buffer"
                selected.append(c)
            remaining_slots = max(0, total_per_category_target - len(selected))
            if remaining_slots > 0:
                for c in fallback_rows[:remaining_slots]:
                    c = dict(c)
                    c["selection_tier"] = "fallback_buffer"
                    selected.append(c)
        else:
            # Strict-only mode
            for c in strict_rows[args.top_n_per_category : total_per_category_target]:
                c = dict(c)
                c["selection_tier"] = "strict_buffer"
                selected.append(c)

        category_counts[cat] = len(selected)
        strict_category_counts[cat] = sum(1 for c in selected if str(c.get("selection_tier", "")).startswith("strict"))
        buffer_category_counts[cat] = sum(1 for c in selected if "buffer" in str(c.get("selection_tier", "")))
        if len(selected) < total_per_category_target:
            underfilled_categories[cat] = len(selected)
        for i, c in enumerate(selected, 1):
            c["selected_rank_in_category"] = i
            selected_channels.append(c)

    # Build project-compatible output payload
    output_channels = []
    for c in sorted(selected_channels, key=lambda x: (x.get("category") or "", x.get("selected_rank_in_category") or 10**6)):
        out = {
            "handle": c.get("handle"),
            "category": c.get("category"),
            "channel_id": c.get("channel_id"),
            "title": c.get("title"),
            "country": c.get("country"),
            "source": "launch_builder",
            "subscriber_count": c.get("subscriber_count"),
            "video_count": c.get("video_count"),
            "view_count": c.get("view_count"),
            "selected_rank_in_category": c.get("selected_rank_in_category"),
            "selection_tier": c.get("selection_tier"),
            "launch_relevance_score": c.get("launch_relevance_score"),
            "source_evidence": c.get("source_evidence"),
            "language_profile": c.get("language_profile"),
        }
        output_channels.append(out)

    review_outputs = export_review_csvs(
        review_prefix=args.review_prefix,
        selected_channels=selected_channels,
        all_candidates=sorted(
            all_candidates_for_review,
            key=lambda c: (
                c.get("category") or "",
                not bool(c.get("english_primary")),
                -(c.get("launch_relevance_score") or 0.0),
                -(c.get("subscriber_count") or 0),
            ),
        ),
    )

    payload = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "builder": "build_launch_channel_list.py",
            "target_categories": sorted(target_categories),
            "top_n_per_category": args.top_n_per_category,
            "buffer_count_per_category": args.buffer_count_per_category,
            "total_per_category_target": total_per_category_target,
            "language_check_mode": args.language_check_mode if not args.dry_run else "none (dry-run)",
            "english_ratio_threshold": args.english_ratio_threshold,
            "min_language_sample_videos": args.min_language_sample_videos,
            "preferred_countries_boost_only": sorted(preferred_countries),
            "note": "Country is not a gate. English-primary classification comes from transcript-language sampling heuristics.",
            "hard_exclusions_enabled": not bool(args.disable_hard_exclusions),
            "hard_exclude_title_keywords": args.hard_exclude_title_keywords,
            "hard_exclude_category_keywords": args.hard_exclude_category_keywords,
        },
        "stats": dict(stats),
        "source_stats": source_stats,
        "category_counts": category_counts,
        "strict_category_counts": strict_category_counts,
        "buffer_category_counts": buffer_category_counts,
        "underfilled_categories": underfilled_categories,
        "review_exports": review_outputs,
        "channels": output_channels,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"\nSaved final launch list to: {args.output}")
    print(f"Selected channels: {len(output_channels)}")
    print(f"Per-category selected counts (target {total_per_category_target} = top {args.top_n_per_category} + buffer {args.buffer_count_per_category}):")
    for cat in sorted(category_counts):
        print(
            f"  {cat}: {category_counts[cat]} "
            f"(strict={strict_category_counts.get(cat,0)}, buffer_slots={buffer_category_counts.get(cat,0)})"
        )
    if underfilled_categories:
        print("Underfilled categories (need more candidates / source coverage / language-eligible channels):")
        for cat, n in sorted(underfilled_categories.items()):
            print(f"  {cat}: {n}/{total_per_category_target}")
    print("Review exports:")
    for k, v in review_outputs.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
