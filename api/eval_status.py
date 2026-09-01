"""GET /api/eval_status?v=VIDEO_ID - Check eval status."""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(__file__))

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()


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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        video_id = params.get("v", [None])[0]

        if not video_id:
            return self._json(400, {"error": "Missing ?v= parameter"})

        user_id = _verify_jwt(self.headers.get("Authorization"))
        if not user_id:
            return self._json(401, {"error": "Authentication required"})

        # Check eval_cache
        r = httpx.get(
            f"{_SUPABASE_URL}/rest/v1/eval_cache",
            params={
                "video_id": f"eq.{video_id}",
                "select": "video_id,status,video_score,title,duration_seconds,title_content_similarity_score,deception_score,focus_ratio,time_to_main_content,sponsor_interruption_score,title_analysis_reasoning,deception_reasoning,focus_reasoning,time_reasoning,sponsor_reasoning,evaluation_error,completed_at",
            },
            headers=_srk_headers(),
            timeout=10,
        )

        if r.status_code != 200:
            return self._json(500, {"error": "Database error"})

        data = r.json()
        if not data:
            return self._json(404, {"error": "Evaluation not found"})

        row = data[0]

        # Get remaining credits
        cr = httpx.post(
            f"{_SUPABASE_URL}/rest/v1/rpc/check_credits",
            json={"p_user_id": user_id, "p_count": 1},
            headers=_srk_headers(),
            timeout=10,
        )
        row["credits_remaining"] = cr.json() if cr.status_code == 200 else None

        return self._json(200, row)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
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
