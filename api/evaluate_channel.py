"""POST /api/evaluate_channel - Fetch channel video list for evaluation.

Flow:
1. Parse channel handle/ID from URL
2. Verify JWT, extract user_id
3. Fetch latest videos via YouTube Data API playlistItems (1 unit)
4. Fetch popular videos via YouTube Data API (sort by viewCount from same set)
5. Filter: exclude >90min, Shorts. Select 50/50 latest/popular, up to 15.
6. Check which already in eval_cache/video_evaluations
7. Return { channel, videos, uncached_count, credits_needed }

Frontend then calls /api/evaluate for each uncached video individually.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(__file__))

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
_YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()

MAX_VIDEOS = 15
MAX_DURATION_SECONDS = 5400  # 90 minutes


def _srk_headers():
    return {
        "apikey": _SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _verify_jwt(auth_header):
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    r = httpx.get(
        f"{_SUPABASE_URL}/auth/v1/user",
        headers={"apikey": _ANON_KEY, "Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    return r.json().get("id")


def _parse_channel_identifier(url):
    if not url:
        return None, None
    s = url.strip()
    m = re.search(r"/@([A-Za-z0-9_.-]+)", s)
    if m:
        return "handle", m.group(1)
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", s)
    if m:
        return "id", m.group(1)
    m = re.search(r"/c/([A-Za-z0-9_.-]+)", s)
    if m:
        return "custom", m.group(1)
    m = re.search(r"/user/([A-Za-z0-9_.-]+)", s)
    if m:
        return "user", m.group(1)
    return None, None


def _get_channel_id(id_type, value):
    """Resolve channel ID from handle/custom/user."""
    params = {"part": "snippet,contentDetails,statistics", "key": _YOUTUBE_API_KEY}
    if id_type == "id":
        params["id"] = value
    elif id_type == "handle" or id_type == "custom":
        params["forHandle"] = value
    elif id_type == "user":
        params["forUsername"] = value
    else:
        return None, None, None

    r = httpx.get("https://www.googleapis.com/youtube/v3/channels", params=params, timeout=10)
    if r.status_code != 200:
        return None, None, None

    items = r.json().get("items", [])
    if not items:
        return None, None, None

    channel = items[0]
    channel_id = channel["id"]
    title = channel["snippet"]["title"]
    thumbnails = channel["snippet"].get("thumbnails", {})
    thumbnail = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url", "")
    statistics = channel.get("statistics", {})
    sub_count = int(statistics.get("subscriberCount", 0)) if statistics.get("subscriberCount") else None
    # Uploads playlist: UC -> UU
    uploads_playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")

    return channel_id, {"title": title, "thumbnail_url": thumbnail, "channel_id": channel_id, "subscriber_count": sub_count}, uploads_playlist


def _fetch_upload_videos(uploads_playlist_id, max_results=50):
    """Fetch latest videos from uploads playlist (1 quota unit)."""
    r = httpx.get(
        "https://www.googleapis.com/youtube/v3/playlistItems",
        params={
            "part": "snippet",
            "playlistId": uploads_playlist_id,
            "maxResults": min(max_results, 50),
            "key": _YOUTUBE_API_KEY,
        },
        timeout=10,
    )
    if r.status_code != 200:
        return []

    items = r.json().get("items", [])
    return [
        {
            "video_id": item["snippet"]["resourceId"]["videoId"],
            "title": item["snippet"]["title"],
            "published_at": item["snippet"]["publishedAt"],
        }
        for item in items
        if item["snippet"]["resourceId"].get("videoId")
    ]


def _get_video_details(video_ids):
    """Fetch duration, view count, tags for videos (1 quota unit per 50 videos)."""
    if not video_ids:
        return {}

    r = httpx.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "contentDetails,statistics,snippet",
            "id": ",".join(video_ids),
            "key": _YOUTUBE_API_KEY,
        },
        timeout=10,
    )
    if r.status_code != 200:
        return {}

    result = {}
    for item in r.json().get("items", []):
        vid = item["id"]
        duration_str = item.get("contentDetails", {}).get("duration", "PT0S")
        duration = _parse_duration(duration_str)
        tags = item.get("snippet", {}).get("tags", [])
        view_count = int(item.get("statistics", {}).get("viewCount", 0))
        is_short = "#shorts" in [t.lower() for t in tags] or duration < 61
        result[vid] = {
            "duration_seconds": duration,
            "view_count": view_count,
            "is_short": is_short,
        }
    return result


def _parse_duration(iso_duration):
    """Parse ISO 8601 duration (PT1H2M3S) to seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mins * 60 + s


def _select_videos(videos, details, max_count=MAX_VIDEOS):
    """Select 50/50 latest/popular, excluding Shorts and >90min."""
    eligible = []
    for v in videos:
        vid = v["video_id"]
        d = details.get(vid, {})
        if d.get("is_short"):
            continue
        if d.get("duration_seconds", 0) > MAX_DURATION_SECONDS:
            continue
        eligible.append({**v, **d})

    if not eligible:
        return []

    # Sort by published_at desc for latest
    by_date = sorted(eligible, key=lambda x: x.get("published_at", ""), reverse=True)
    # Sort by view_count desc for popular
    by_views = sorted(eligible, key=lambda x: x.get("view_count", 0), reverse=True)

    import math
    latest_count = math.ceil(max_count * 0.5)
    popular_count = max_count - latest_count

    selected_ids = set()
    selected = []

    # Pick latest
    for v in by_date:
        if len(selected) >= latest_count:
            break
        if v["video_id"] not in selected_ids:
            selected_ids.add(v["video_id"])
            selected.append({**v, "pool": "latest"})

    # Pick popular (dedup)
    for v in by_views:
        if len(selected) >= max_count:
            break
        if v["video_id"] not in selected_ids:
            selected_ids.add(v["video_id"])
            selected.append({**v, "pool": "popular"})

    # Backfill if needed
    for v in by_date:
        if len(selected) >= max_count:
            break
        if v["video_id"] not in selected_ids:
            selected_ids.add(v["video_id"])
            selected.append({**v, "pool": "backfill"})

    return selected


def _check_existing(video_ids):
    """Check which videos already have results."""
    existing = {}

    # Check eval_cache
    ids_param = ",".join(video_ids)
    r = httpx.get(
        f"{_SUPABASE_URL}/rest/v1/eval_cache",
        params={"video_id": f"in.({ids_param})", "select": "video_id,status,video_score"},
        headers=_srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        for row in r.json():
            if row["status"] == "complete":
                existing[row["video_id"]] = {"status": "complete", "video_score": row["video_score"]}

    # Check video_evaluations (curated)
    r = httpx.get(
        f"{_SUPABASE_URL}/rest/v1/video_evaluations",
        params={"video_id": f"in.({ids_param})", "select": "video_id,video_score", "evaluation_success": "eq.true"},
        headers=_srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        for row in r.json():
            if row["video_id"] not in existing:
                existing[row["video_id"]] = {"status": "complete", "video_score": row["video_score"]}

    return existing


def _check_credits(user_id, count):
    r = httpx.post(
        f"{_SUPABASE_URL}/rest/v1/rpc/check_credits",
        json={"p_user_id": user_id, "p_count": count},
        headers=_srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()
    return -1


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except Exception:
            return self._json(400, {"error": "Invalid JSON body"})

        channel_url = body.get("channel_url", "")
        id_type, value = _parse_channel_identifier(channel_url)
        if not id_type or not value:
            return self._json(400, {"error": "Invalid channel URL"})

        user_id = _verify_jwt(self.headers.get("Authorization"))
        if not user_id:
            return self._json(401, {"error": "Authentication required"})

        # Get channel ID + info
        channel_id, channel_info, uploads_playlist = _get_channel_id(id_type, value)
        if not channel_id or not uploads_playlist:
            return self._json(404, {"error": "Channel not found"})

        # Fetch videos from uploads playlist
        raw_videos = _fetch_upload_videos(uploads_playlist)
        if not raw_videos:
            return self._json(404, {"error": "No videos found on this channel"})

        # Get details (duration, views, tags) for filtering
        video_ids = [v["video_id"] for v in raw_videos]
        details = _get_video_details(video_ids)

        # Select 50/50 latest/popular
        selected = _select_videos(raw_videos, details)
        if not selected:
            return self._json(404, {"error": "No eligible videos found (all Shorts or >90 min)"})

        # Check existing results
        selected_ids = [v["video_id"] for v in selected]
        existing = _check_existing(selected_ids)

        uncached_ids = [vid for vid in selected_ids if vid not in existing]
        credits_needed = len(uncached_ids)

        # Credit check
        remaining = _check_credits(user_id, credits_needed)
        if remaining == -1:
            return self._json(403, {"error": f"Insufficient credits. Need {credits_needed}, check your balance."})

        # Build response
        videos = []
        for v in selected:
            vid = v["video_id"]
            if vid in existing:
                videos.append({
                    "video_id": vid,
                    "title": v.get("title", ""),
                    "duration_seconds": v.get("duration_seconds", 0),
                    "view_count": v.get("view_count", 0),
                    "pool": v.get("pool", ""),
                    "status": "complete",
                    "video_score": existing[vid].get("video_score"),
                    "cached": True,
                })
            else:
                videos.append({
                    "video_id": vid,
                    "title": v.get("title", ""),
                    "duration_seconds": v.get("duration_seconds", 0),
                    "view_count": v.get("view_count", 0),
                    "pool": v.get("pool", ""),
                    "status": "pending",
                    "cached": False,
                })

        return self._json(200, {
            "channel": channel_info,
            "videos": videos,
            "total_videos": len(videos),
            "uncached_count": credits_needed,
            "credits_needed": credits_needed,
            "credits_remaining": remaining,
        })

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _json(self, status, body):
        payload = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass
