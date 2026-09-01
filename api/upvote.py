from http.server import BaseHTTPRequestHandler
import json
from _supabase import get_client

TABLE = "site_upvotes"
ROW_ID = 1


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        sb = get_client()
        row = sb.table(TABLE).select("upvote_count,downvote_count").eq("id", ROW_ID).single().execute()
        self._respond(row.data)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}
        direction = body.get("direction", "up")

        sb = get_client()
        fn = "increment_upvote" if direction == "up" else "increment_downvote"
        sb.rpc(fn, {}).execute()
        row = sb.table(TABLE).select("upvote_count,downvote_count").eq("id", ROW_ID).single().execute()
        self._respond(row.data)

    def _respond(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
