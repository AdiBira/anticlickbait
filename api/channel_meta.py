"""GET /api/channel_meta?url=CHANNEL_URL - Fetch channel name + thumbnail.

Unauthenticated endpoint for pre-login channel teaser.
Uses YouTube Data API (1 quota unit per call).
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

_YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()

# Simple in-memory cache (persists within same Vercel instance)
_cache = {}


def _parse_channel_identifier(url):
    """Parse channel handle, ID, or custom name from various URL formats.
    Returns (type, value) where type is 'handle', 'id', 'custom', 'user'."""
    if not url:
        return None, None
    s = url.strip()

    # /@handle
    m = re.search(r"/@([A-Za-z0-9_.-]+)", s)
    if m:
        return "handle", m.group(1)

    # /channel/UCID
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", s)
    if m:
        return "id", m.group(1)

    # /c/customname
    m = re.search(r"/c/([A-Za-z0-9_.-]+)", s)
    if m:
        return "custom", m.group(1)

    # /user/username
    m = re.search(r"/user/([A-Za-z0-9_.-]+)", s)
    if m:
        return "user", m.group(1)

    # Bare handle (no URL)
    if re.match(r"^@?[A-Za-z0-9_.-]+$", s):
        handle = s.lstrip("@")
        return "handle", handle

    return None, None


def _fetch_channel_info(id_type, value):
    """Fetch channel name + thumbnail via YouTube Data API."""
    import httpx

    if not _YOUTUBE_API_KEY:
        return None

    # Check cache
    cache_key = f"{id_type}:{value}"
    if cache_key in _cache:
        return _cache[cache_key]

    params = {"part": "snippet,statistics", "key": _YOUTUBE_API_KEY}

    if id_type == "handle":
        params["forHandle"] = value
    elif id_type == "id":
        params["id"] = value
    elif id_type == "custom":
        params["forHandle"] = value
    elif id_type == "user":
        params["forUsername"] = value
    else:
        return None

    try:
        r = httpx.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params=params,
            timeout=10,
        )
        if r.status_code != 200:
            return None

        data = r.json()
        items = data.get("items", [])
        if not items:
            return None

        snippet = items[0]["snippet"]
        statistics = items[0].get("statistics", {})
        result = {
            "channel_id": items[0]["id"],
            "title": snippet.get("title", ""),
            "thumbnail_url": (snippet.get("thumbnails", {}).get("high") or snippet.get("thumbnails", {}).get("medium") or snippet.get("thumbnails", {}).get("default") or {}).get("url", ""),
            "description": snippet.get("description", "")[:200],
            "subscriber_count": int(statistics.get("subscriberCount", 0)) if statistics.get("subscriberCount") else None,
        }

        _cache[cache_key] = result
        return result
    except Exception:
        return None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        url = params.get("url", [None])[0]

        if not url:
            return self._json(400, {"error": "Missing ?url= parameter"})

        id_type, value = _parse_channel_identifier(url)
        if not id_type or not value:
            return self._json(400, {"error": "Invalid channel URL"})

        info = _fetch_channel_info(id_type, value)
        if not info:
            return self._json(404, {"error": "Channel not found"})

        return self._json(200, info)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
