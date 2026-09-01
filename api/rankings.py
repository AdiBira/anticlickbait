from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from _supabase import get_client


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        category = params.get("category", [None])[0]
        country = params.get("country", [None])[0]
        sort = params.get("sort", ["score"])[0]

        client = get_client()
        query = client.from_("ranking_global").select(
            "rank, channel_id, handle, title, country, "
            "category_name, subscriber_count, channel_score, "
            "videos_evaluated, thumbnail_url"
        )

        if category:
            query = query.eq("category_name", category)
        if country:
            query = query.eq("country", country)

        if sort == "subscribers":
            query = query.order("subscriber_count", desc=True)

        result = query.execute()
        self._json(200, result.data)

    def _json(self, status: int, body):
        payload = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass
