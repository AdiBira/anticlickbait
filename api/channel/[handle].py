from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from _supabase import get_client

VIDEO_COLUMNS = (
    "video_id, title, thumbnail_url, duration_seconds, "
    "view_count, published_at, video_score, "
    "title_content_similarity_score, deception_score, "
    "focus_ratio, time_to_main_content, "
    "sponsor_interruption_score, "
    "title_analysis_reasoning, deception_reasoning, "
    "focus_reasoning, time_reasoning, sponsor_reasoning"
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        raw_handle = params.get("handle", [None])[0]

        if not raw_handle:
            return self._json(400, {"error": "handle is required"})

        handle = raw_handle if raw_handle.startswith("@") else f"@{raw_handle}"
        client = get_client()

        channel_res = (
            client.from_("channel_detail")
            .select("*")
            .eq("handle", handle)
            .limit(1)
            .execute()
        )

        if not channel_res.data:
            return self._json(404, {"error": f"Channel not found: {handle}"})

        channel = channel_res.data[0]

        videos_res = (
            client.from_("video_evaluations")
            .select(VIDEO_COLUMNS)
            .eq("channel_id", channel["channel_id"])
            .eq("evaluation_success", True)
            .order("video_score", desc=True)
            .execute()
        )

        self._json(200, {"channel": channel, "videos": videos_res.data})

    def _json(self, status: int, body):
        payload = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass
