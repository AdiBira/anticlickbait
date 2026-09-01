from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from _supabase import get_client


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        video_id = params.get("video_id", [None])[0]

        if not video_id:
            return self._json(400, {"error": "video_id is required"})

        client = get_client()
        result = (
            client.from_("video_evaluations")
            .select("video_id, transcript_text, transcript_language")
            .eq("video_id", video_id)
            .limit(1)
            .execute()
        )

        if not result.data:
            return self._json(404, {"error": f"Video not found: {video_id}"})

        self._json(200, result.data[0])

    def _json(self, status: int, body):
        payload = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass
