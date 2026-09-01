"""
YouTube Data API v3 wrapper for fetching channel and video information.

Notes:
- Uses local cache to reduce repeated quota burn across supervisor passes.
- Video selection is a deterministic latest+popular mix from a recent candidate pool.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import yt_dlp

from config import (
    YOUTUBE_API_KEY,
    VIDEOS_PER_CHANNEL,
    MIN_VIDEO_DURATION_SECONDS,
    VIDEO_SELECTION_LATEST_RATIO,
    YOUTUBE_CACHE_TTL_HOURS,
    YTDLP_CHANNEL_DISCOVERY_FALLBACK,
)
from categories import get_category_name


_CACHE_ROOT = Path(__file__).parent.parent / "data" / "cache" / "youtube_api"
_DB_PATH = Path(__file__).parent.parent / "data" / "anticlickbait.db"


def _cache_path(kind: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9@._-]+", "_", key)
    return _CACHE_ROOT / kind / f"{safe}.json"


def _load_cache(kind: str, key: str, ttl_hours: int = YOUTUBE_CACHE_TTL_HOURS) -> dict | list | None:
    path = _cache_path(kind, key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    fetched_at = float(payload.get("fetched_at", 0))
    data = payload.get("data")
    if data is None:
        return None

    if ttl_hours <= 0:
        return data

    age_hours = (time.time() - fetched_at) / 3600.0
    if age_hours <= ttl_hours:
        return data
    return None


def _load_cache_stale(kind: str, key: str) -> dict | list | None:
    path = _cache_path(kind, key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload.get("data")


def _save_cache(kind: str, key: str, data: dict | list) -> None:
    path = _cache_path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=True),
        encoding="utf-8",
    )


def _is_quota_error(err: Exception) -> bool:
    text = str(err).lower()
    return "quotaexceeded" in text or "youtube.quota" in text or "exceeded your" in text


def get_youtube_client():
    """Create and return a YouTube Data API client."""
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def parse_duration(duration_str: str) -> int:
    """
    Parse ISO 8601 duration string to seconds.

    Args:
        duration_str: Duration in ISO 8601 format (e.g., "PT4M13S", "PT1H2M3S")

    Returns:
        Duration in seconds
    """
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    match = re.match(pattern, duration_str)

    if not match:
        return 0

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)

    return hours * 3600 + minutes * 60 + seconds


def _normalize_handle(handle: str) -> str:
    return handle if handle.startswith("@") else f"@{handle}"


def _uploads_playlist_id_from_channel_id(channel_id: str) -> str | None:
    # For canonical channel IDs, uploads playlist is deterministic.
    if isinstance(channel_id, str) and channel_id.startswith("UC") and len(channel_id) >= 3:
        return f"UU{channel_id[2:]}"
    return None


def _video_published_key(video: dict) -> str:
    return video.get("published_at") or ""


def _select_mixed_videos(videos: list[dict], max_videos: int, latest_ratio: float) -> list[dict]:
    if max_videos <= 0 or not videos:
        return []

    latest_ratio = max(0.0, min(1.0, float(latest_ratio)))
    latest_target = int(math.ceil(max_videos * latest_ratio))
    latest_target = min(max_videos, max(1, latest_target))
    popular_target = max_videos - latest_target

    # Partition by _pool_source tag when present; fall back to date-based sort.
    tagged = any("_pool_source" in v for v in videos)
    if tagged:
        latest_candidates = [v for v in videos if v.get("_pool_source") != "popular"]
        popular_candidates = [v for v in videos if v.get("_pool_source") == "popular"]
    else:
        latest_candidates = videos
        popular_candidates = videos

    latest_sorted = sorted(latest_candidates, key=_video_published_key, reverse=True)
    popular_sorted = sorted(
        popular_candidates,
        key=lambda v: (int(v.get("view_count") or 0), _video_published_key(v)),
        reverse=True,
    )

    selected: list[dict] = []
    seen: set[str] = set()

    def _append_unique(candidates: list[dict], target_add: int) -> int:
        added = 0
        for item in candidates:
            if added >= target_add:
                break
            vid = item.get("video_id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            selected.append(item)
            added += 1
        return added

    _append_unique(latest_sorted, latest_target)
    _append_unique(popular_sorted, popular_target)

    if len(selected) < max_videos:
        # Duplicate overlap between latest and popular gets filled by more latest first.
        _append_unique(latest_sorted, max_videos - len(selected))
    if len(selected) < max_videos:
        _append_unique(popular_sorted, max_videos - len(selected))

    return selected[:max_videos]


def _load_video_pool_from_db(channel_id: str, min_duration_seconds: int) -> list[dict]:
    """
    Fallback candidate pool from local DB when YouTube Data API quota is exhausted.
    """
    if not _DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                  video_id,
                  title,
                  description,
                  published_at,
                  channel_id,
                  category_id,
                  duration_seconds,
                  view_count,
                  like_count,
                  comment_count,
                  thumbnail_url
                FROM video_evaluations
                WHERE channel_id = ?
                ORDER BY published_at DESC
                LIMIT 400
                """,
                (channel_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"DB fallback unavailable for {channel_id}: {e}")
        return []

    pool = []
    for row in rows:
        duration_seconds = int(row["duration_seconds"] or 0)
        if duration_seconds < min_duration_seconds:
            continue
        category_id = int(row["category_id"] or 0)
        pool.append(
            {
                "video_id": row["video_id"],
                "title": row["title"],
                "description": row["description"],
                "published_at": row["published_at"],
                "channel_id": row["channel_id"],
                "channel_title": None,
                "default_language": None,
                "default_audio_language": None,
                "category_id": category_id,
                "category_name": get_category_name(category_id),
                "duration_seconds": duration_seconds,
                "view_count": int(row["view_count"] or 0),
                "like_count": int(row["like_count"] or 0),
                "comment_count": int(row["comment_count"] or 0),
                "thumbnail_url": row["thumbnail_url"],
            }
        )
    return pool


def _fetch_video_pool_via_ytdlp(
    channel_id: str,
    *,
    fetch_limit: int,
    min_duration_seconds: int,
    sort_by_views: bool = False,
) -> list[dict]:
    """
    Pool extraction via yt-dlp channel page.

    Notes:
    - sort_by_views=False: latest-first ordering (default, used as fallback for latest fetch).
    - sort_by_views=True: popular-first ordering via ?sort=p; published_at set to epoch so
      these videos don't contaminate the latest bucket in _select_mixed_videos.
    """
    if sort_by_views:
        url = f"https://www.youtube.com/channel/{channel_id}/videos?sort=p"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/videos"
    ydl_opts = {
        "extract_flat": True,
        "ignoreconfig": True,
        "playlistend": int(fetch_limit),
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"yt-dlp fetch failed for {channel_id}: {e}")
        return []

    entries = info.get("entries") or []
    if not entries:
        return []

    channel_title = info.get("channel") or info.get("title")
    now = datetime.now(UTC)
    pool: list[dict] = []
    for idx, entry in enumerate(entries):
        vid = entry.get("id")
        if not vid:
            continue
        title = entry.get("title")
        title_lc = (title or "").lower()
        if "#shorts" in title_lc:
            continue

        duration_seconds = int(float(entry.get("duration") or 0))
        if duration_seconds < min_duration_seconds:
            continue

        # Determine real published_at if available, synthesize for latest ordering.
        raw_upload_date = entry.get("upload_date")  # YYYYMMDD string from yt-dlp
        if raw_upload_date and len(raw_upload_date) == 8:
            real_date = f"{raw_upload_date[:4]}-{raw_upload_date[4:6]}-{raw_upload_date[6:8]}T00:00:00Z"
        else:
            real_date = None

        if sort_by_views:
            published_at = real_date  # None if yt-dlp didn't return it
            pool_source = "popular"
        else:
            # Synthesize published_at by rank to preserve latest ordering.
            published_at = real_date or (now - timedelta(minutes=idx)).isoformat().replace("+00:00", "Z")
            pool_source = "latest"

        thumb = None
        thumbs = entry.get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs:
            thumb = thumbs[-1].get("url")

        pool.append(
            {
                "video_id": vid,
                "title": title,
                "description": entry.get("description"),
                "published_at": published_at,
                "_pool_source": pool_source,
                "channel_id": channel_id,
                "channel_title": channel_title,
                "default_language": None,
                "default_audio_language": None,
                "category_id": 0,
                "category_name": get_category_name(0),
                "duration_seconds": duration_seconds,
                "view_count": int(entry.get("view_count") or 0),
                "like_count": int(entry.get("like_count") or 0),
                "comment_count": int(entry.get("comment_count") or 0),
                "thumbnail_url": thumb,
            }
        )

    return pool


def _fetch_channel_video_pool_from_api(
    channel_id: str,
    *,
    min_duration_seconds: int,
    fetch_limit: int,
    stale_pool: list[dict] | None,
) -> list[dict]:
    """Fetch and normalize a channel video candidate pool via YouTube Data API."""
    youtube = get_youtube_client()

    uploads_playlist_id = _uploads_playlist_id_from_channel_id(channel_id)
    if not uploads_playlist_id:
        channel_info = get_channel_info(channel_id)
        if not channel_info:
            print(f"Could not find channel: {channel_id}")
            return stale_pool or []
        uploads_playlist_id = channel_info.get("uploads_playlist_id")

    video_ids: list[str] = []
    next_page_token = None

    try:
        while len(video_ids) < fetch_limit:
            playlist_response = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=min(50, fetch_limit - len(video_ids)),
                pageToken=next_page_token,
            ).execute()

            for item in playlist_response.get("items", []):
                vid = (item.get("contentDetails") or {}).get("videoId")
                if vid:
                    video_ids.append(vid)

            next_page_token = playlist_response.get("nextPageToken")
            if not next_page_token:
                break
    except HttpError as e:
        print(f"Error fetching playlist items: {e}")
        if _is_quota_error(e):
            if stale_pool:
                print(f"Using stale video pool cache for {channel_id} due quotaExceeded")
                return stale_pool
            db_pool = _load_video_pool_from_db(channel_id, min_duration_seconds)
            if db_pool:
                print(f"Using DB video pool fallback for {channel_id} due quotaExceeded")
                return db_pool
        return []
    except Exception as e:
        print(f"Unexpected error fetching playlist items: {e}")
        if stale_pool:
            print(f"Using stale video pool cache for {channel_id} after unexpected error")
            return stale_pool
        db_pool = _load_video_pool_from_db(channel_id, min_duration_seconds)
        if db_pool:
            print(f"Using DB video pool fallback for {channel_id} after unexpected error")
            return db_pool
        return []

    videos: list[dict] = []
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i : i + 50]
        try:
            video_response = youtube.videos().list(
                part="snippet,contentDetails,statistics",
                id=",".join(batch_ids),
            ).execute()
        except HttpError as e:
            print(f"Error fetching video details: {e}")
            if _is_quota_error(e):
                break
            continue
        except Exception as e:
            print(f"Unexpected error fetching video details: {e}")
            continue

        for video in video_response.get("items", []):
            snippet = video["snippet"]
            content_details = video["contentDetails"]
            statistics = video.get("statistics", {})

            duration_seconds = parse_duration(content_details.get("duration", "PT0S"))
            if duration_seconds < min_duration_seconds:
                continue

            title_lc = (snippet.get("title") or "").lower()
            desc_lc = (snippet.get("description") or "").lower()
            if "#shorts" in title_lc or "#shorts" in desc_lc:
                continue

            category_id = int(snippet.get("categoryId", 0))
            thumbnails = snippet.get("thumbnails", {})
            thumbnail_url = (
                thumbnails.get("maxres", {}).get("url")
                or thumbnails.get("high", {}).get("url")
                or thumbnails.get("medium", {}).get("url")
                or thumbnails.get("default", {}).get("url")
            )

            videos.append(
                {
                    "video_id": video["id"],
                    "title": snippet.get("title"),
                    "description": snippet.get("description"),
                    "published_at": snippet.get("publishedAt"),
                    "_pool_source": "latest",
                    "channel_id": snippet.get("channelId"),
                    "channel_title": snippet.get("channelTitle"),
                    "default_language": snippet.get("defaultLanguage"),
                    "default_audio_language": snippet.get("defaultAudioLanguage"),
                    "category_id": category_id,
                    "category_name": get_category_name(category_id),
                    "duration_seconds": duration_seconds,
                    "view_count": int(statistics.get("viewCount", 0)),
                    "like_count": int(statistics.get("likeCount", 0)),
                    "comment_count": int(statistics.get("commentCount", 0)),
                    "thumbnail_url": thumbnail_url,
                }
            )

    if not videos:
        if stale_pool:
            print(f"Using stale video pool cache for {channel_id} (empty API result)")
            return stale_pool
        db_pool = _load_video_pool_from_db(channel_id, min_duration_seconds)
        if db_pool:
            print(f"Using DB video pool fallback for {channel_id} (empty API result)")
            return db_pool
        return []
    return sorted(videos, key=_video_published_key, reverse=True)


def get_channel_video_pool(
    channel_id: str,
    *,
    max_videos: int = VIDEOS_PER_CHANNEL,
    min_duration_seconds: int = MIN_VIDEO_DURATION_SECONDS,
) -> list[dict]:
    """
    Fetch channel video candidate pool via dual fetch: latest from API + most-viewed from yt-dlp.

    Latest videos are fetched via playlistItems (chronological, exact count).
    Most-viewed are fetched via yt-dlp with ?sort=p (all-time popular, no quota cost).
    Combined pool is passed to _select_mixed_videos which handles dedup/fill.
    """
    pool_key = f"{channel_id}__pool__min{int(min_duration_seconds)}"
    cached_pool = _load_cache("channel_videos_pool", pool_key)
    if isinstance(cached_pool, list) and cached_pool:
        return cached_pool

    stale_pool = _load_cache_stale("channel_videos_pool", pool_key)
    stale_list = stale_pool if isinstance(stale_pool, list) else None

    latest_target = max(1, int(math.ceil(max_videos * VIDEO_SELECTION_LATEST_RATIO)))
    popular_target = max_videos - latest_target

    # Fetch latest from YouTube Data API — fetch full max_videos so we have a
    # big enough latest pool even after overlap dedup in _select_mixed_videos.
    latest_pool = _fetch_channel_video_pool_from_api(
        channel_id,
        min_duration_seconds=min_duration_seconds,
        fetch_limit=max_videos,
        stale_pool=stale_list,
    )

    # Fetch most-viewed from yt-dlp (no quota, sorts by all-time views).
    # Fetch 3× to compensate for duration-filter attrition and overlap dedup.
    popular_pool = []
    if popular_target > 0:
        popular_pool = _fetch_video_pool_via_ytdlp(
            channel_id,
            fetch_limit=max_videos * 3,
            min_duration_seconds=min_duration_seconds,
            sort_by_views=True,
        )

    pool = latest_pool + popular_pool

    if not pool:
        if stale_list:
            print(f"Using stale video pool cache for {channel_id} (empty dual fetch)")
            return stale_list
        db_pool = _load_video_pool_from_db(channel_id, min_duration_seconds)
        if db_pool:
            print(f"Using DB video pool fallback for {channel_id} (empty dual fetch)")
            return db_pool
        return []

    _save_cache("channel_videos_pool", pool_key, pool)
    return pool


def get_channel_info(channel_id: str) -> dict | None:
    """
    Fetch channel information by channel ID.

    Args:
        channel_id: YouTube channel ID (e.g., "UCX6OQ3DkcsbYNE6H8uQQuVA" for MrBeast)

    Returns:
        Dictionary with channel info, or None if not found
    """
    cached = _load_cache("channel_info", channel_id)
    if cached:
        return cached

    stale = _load_cache_stale("channel_info", channel_id)
    youtube = get_youtube_client()

    try:
        response = youtube.channels().list(part="snippet,statistics,contentDetails", id=channel_id).execute()

        if not response.get("items"):
            return None

        channel = response["items"][0]
        snippet = channel["snippet"]
        statistics = channel["statistics"]

        payload = {
            "channel_id": channel_id,
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "custom_url": snippet.get("customUrl"),
            "country": snippet.get("country"),
            "subscriber_count": int(statistics.get("subscriberCount", 0)),
            "video_count": int(statistics.get("videoCount", 0)),
            "view_count": int(statistics.get("viewCount", 0)),
            "uploads_playlist_id": channel["contentDetails"]["relatedPlaylists"]["uploads"],
        }
        _save_cache("channel_info", channel_id, payload)
        if payload.get("custom_url"):
            handle = _normalize_handle(payload["custom_url"])
            _save_cache("channel_info_by_handle", handle, payload)
        return payload

    except HttpError as e:
        print(f"Error fetching channel info: {e}")
        if _is_quota_error(e) and stale:
            print(f"Using stale channel cache for {channel_id} due quotaExceeded")
            return stale
        return None
    except Exception as e:
        print(f"Unexpected error fetching channel info: {e}")
        if stale:
            print(f"Using stale channel cache for {channel_id} after unexpected error")
            return stale
        return None


def get_channel_info_by_handle(handle: str) -> dict | None:
    """
    Fetch channel information by handle (e.g., "@MrBeast").

    Args:
        handle: YouTube channel handle (with or without @)

    Returns:
        Dictionary with channel info, or None if not found
    """
    handle = _normalize_handle(handle)

    cached = _load_cache("channel_info_by_handle", handle)
    if cached:
        return cached

    stale = _load_cache_stale("channel_info_by_handle", handle)
    youtube = get_youtube_client()

    try:
        response = youtube.channels().list(
            part="snippet,statistics,contentDetails",
            forHandle=handle.lstrip("@"),
        ).execute()

        if not response.get("items"):
            return None

        channel = response["items"][0]
        snippet = channel["snippet"]
        statistics = channel["statistics"]

        payload = {
            "channel_id": channel["id"],
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "custom_url": snippet.get("customUrl"),
            "country": snippet.get("country"),
            "subscriber_count": int(statistics.get("subscriberCount", 0)),
            "video_count": int(statistics.get("videoCount", 0)),
            "view_count": int(statistics.get("viewCount", 0)),
            "uploads_playlist_id": channel["contentDetails"]["relatedPlaylists"]["uploads"],
        }

        _save_cache("channel_info_by_handle", handle, payload)
        _save_cache("channel_info", payload["channel_id"], payload)
        return payload

    except HttpError as e:
        print(f"Error fetching channel info by handle: {e}")
        if _is_quota_error(e) and stale:
            print(f"Using stale handle cache for {handle} due quotaExceeded")
            return stale
        return None
    except Exception as e:
        print(f"Unexpected error fetching channel info by handle: {e}")
        if stale:
            print(f"Using stale handle cache for {handle} after unexpected error")
            return stale
        return None


def _backfill_video_metadata(videos: list[dict]) -> None:
    """
    Batch-fetch missing metadata (published_at, category_id, view_count) for
    popular-pool videos that yt-dlp couldn't provide. Updates dicts in-place.
    Costs 1 API quota unit for up to 50 video IDs.
    """
    needs_fill = [v for v in videos if not v.get("published_at") or not v.get("category_id")]
    if not needs_fill:
        return

    ids = [v["video_id"] for v in needs_fill]
    youtube = get_youtube_client()
    try:
        resp = youtube.videos().list(
            part="snippet,statistics",
            id=",".join(ids),
            maxResults=50,
        ).execute()
    except Exception:
        return

    meta = {}
    for item in resp.get("items", []):
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        meta[item["id"]] = {
            "published_at": snippet.get("publishedAt"),
            "category_id": int(snippet.get("categoryId", 0)),
            "category_name": get_category_name(int(snippet.get("categoryId", 0))),
            "view_count": int(stats.get("viewCount", 0)),
        }

    for v in needs_fill:
        filled = meta.get(v["video_id"])
        if not filled:
            continue
        if not v.get("published_at"):
            v["published_at"] = filled["published_at"]
        if not v.get("category_id"):
            v["category_id"] = filled["category_id"]
            v["category_name"] = filled["category_name"]
        if not v.get("view_count"):
            v["view_count"] = filled["view_count"]


def get_channel_videos(
    channel_id: str,
    max_videos: int = VIDEOS_PER_CHANNEL,
    min_duration_seconds: int = MIN_VIDEO_DURATION_SECONDS,
    latest_ratio: float = VIDEO_SELECTION_LATEST_RATIO,
) -> list[dict]:
    """
    Fetch videos from a channel and return latest+popular mixed sample.

    Method:
    - Build a recent candidate pool from uploads playlist.
    - Filter out shorts and too-short videos.
    - Select deterministic mix: latest portion + popular portion.
      If overlap exists, extra slots are filled from latest first, then popular.

    Args:
        channel_id: YouTube channel ID
        max_videos: Maximum number of videos to return
        min_duration_seconds: Minimum video duration in seconds
        latest_ratio: Fraction allocated to latest bucket (default 0.5)

    Returns:
        List of selected video dictionaries
    """
    pool = get_channel_video_pool(
        channel_id,
        max_videos=max_videos,
        min_duration_seconds=min_duration_seconds,
    )
    if not pool:
        return []
    selected = _select_mixed_videos(pool, max_videos=max_videos, latest_ratio=latest_ratio)
    _backfill_video_metadata(selected)
    return selected


def search_channels(query: str, max_results: int = 10) -> list[dict]:
    """
    Search for channels by query.

    Args:
        query: Search query string
        max_results: Maximum number of results to return

    Returns:
        List of channel dictionaries with basic info
    """
    youtube = get_youtube_client()

    try:
        response = youtube.search().list(
            part="snippet",
            q=query,
            type="channel",
            maxResults=max_results,
        ).execute()

        channels = []
        for item in response.get("items", []):
            snippet = item["snippet"]
            channels.append(
                {
                    "channel_id": item["id"]["channelId"],
                    "title": snippet.get("title"),
                    "description": snippet.get("description"),
                }
            )

        return channels

    except HttpError as e:
        print(f"Error searching channels: {e}")
        return []
