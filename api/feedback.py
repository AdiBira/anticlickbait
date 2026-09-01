from http.server import BaseHTTPRequestHandler
import json
from _supabase import get_client

TABLE = "feedback"


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}
        message = (body.get("message") or "").strip()

        if not message or len(message) > 2000:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid message"}).encode())
            return

        ip = self.headers.get("x-forwarded-for", "unknown").split(",")[0].strip()

        sb = get_client()

        # Rate limit: 5 submissions per IP
        existing = sb.table(TABLE).select("id", count="exact").eq("ip_hash", ip).execute()
        if existing.count and existing.count >= 5:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Rate limit exceeded"}).encode())
            return

        sb.table(TABLE).insert({"message": message, "ip_hash": ip}).execute()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
