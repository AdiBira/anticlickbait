"""POST /api/evaluate - Vercel Python coordinator for video evaluation.

Flow:
1. Parse video_id from URL
2. Verify JWT, extract user_id
3. Check eval_cache / video_evaluations for cache hit
4. Check credits
5. Fetch transcript via youtube-transcript-api + residential proxy
6. Store transcript in eval_cache (pending row)
7. Trigger Edge Function with just video_id (tiny POST, <1s)
8. Return { status: "pending", video_id }

Edge Function reads transcript from eval_cache, runs LLM eval, writes result.
Frontend polls eval_cache directly via Supabase client.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(__file__))

import time as _time

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
_PROXY_HOST = os.environ.get("RESIDENTIAL_PROXY_HOST", "").strip()


def _log(video_id, step, status, detail=None, duration_ms=None):
    """Write debug log to Supabase. Best effort - never blocks or throws."""
    try:
        httpx.post(
            f"{_SUPABASE_URL}/rest/v1/debug_logs",
            json={
                "video_id": video_id,
                "step": step,
                "status": status,
                "detail": str(detail)[:500] if detail else None,
                "duration_ms": duration_ms,
            },
            headers={
                "apikey": _SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            timeout=3,
        )
    except Exception:
        pass
_PROXY_USER = os.environ.get("RESIDENTIAL_PROXY_USER", "").strip()
_PROXY_PASS = os.environ.get("RESIDENTIAL_PROXY_PASS", "").strip()


def _srk_headers():
    return {
        "apikey": _SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _parse_video_id(url_or_id):
    if not url_or_id:
        return None
    s = url_or_id.strip()
    if re.match(r"^[A-Za-z0-9_-]{11}$", s):
        return s
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    m = re.search(r"(?:shorts|embed)/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    return None


def _fetch_title_oembed(video_id):
    """Fetch video title from YouTube oEmbed. No API key needed."""
    try:
        r = httpx.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("title", "")
    except Exception:
        pass
    return ""


def _verify_jwt(auth_header):
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    r = httpx.get(
        f"{_SUPABASE_URL}/auth/v1/user",
        headers={
            "apikey": _ANON_KEY,
            "Authorization": f"Bearer {token}",
        },
        timeout=10,
    )
    if r.status_code != 200:
        return None
    return r.json().get("id")


def _check_eval_cache(video_id):
    r = httpx.get(
        f"{_SUPABASE_URL}/rest/v1/eval_cache",
        params={
            "video_id": f"eq.{video_id}",
            "select": "video_id,status,video_score,title_content_similarity_score,deception_score,focus_ratio,time_to_main_content,sponsor_interruption_score,title_analysis_reasoning,deception_reasoning,focus_reasoning,time_reasoning,sponsor_reasoning,evaluation_error",
        },
        headers=_srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        data = r.json()
        if data:
            return data[0]
    return None


def _check_video_evaluations(video_id):
    r = httpx.get(
        f"{_SUPABASE_URL}/rest/v1/video_evaluations",
        params={
            "video_id": f"eq.{video_id}",
            "select": "video_id,title,video_score,title_content_similarity_score,deception_score,focus_ratio,time_to_main_content,sponsor_interruption_score,title_analysis_reasoning,deception_reasoning,focus_reasoning,time_reasoning,sponsor_reasoning",
            "evaluation_success": "eq.true",
        },
        headers=_srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        data = r.json()
        if data:
            return data[0]
    return None


def _check_credits(user_id):
    r = httpx.post(
        f"{_SUPABASE_URL}/rest/v1/rpc/check_credits",
        json={"p_user_id": user_id, "p_count": 1},
        headers=_srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()
    return -1


def _deduct_credit(user_id):
    r = httpx.post(
        f"{_SUPABASE_URL}/rest/v1/rpc/use_credits",
        json={"p_user_id": user_id, "p_count": 1},
        headers=_srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()
    return -1


def _fetch_transcript(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig

    if not _PROXY_HOST or not _PROXY_USER:
        raise Exception("Residential proxy not configured")

    proxy_url = f"http://{_PROXY_USER}:{_PROXY_PASS}@{_PROXY_HOST}"
    api = YouTubeTranscriptApi(proxy_config=GenericProxyConfig(
        http_url=proxy_url,
        https_url=proxy_url,
    ))

    import time as _time
    last_err = None
    for attempt in range(3):
        try:
            # Try English first, then fall back to any available language
            transcript = None
            try:
                transcript = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
            except Exception:
                # fetch() without languages defaults to English too, so
                # we must list available transcripts and pick the first one
                tl = api.list(video_id)
                available = list(tl)
                if not available:
                    raise
                transcript = available[0].fetch()
            language_code = getattr(transcript, "language_code", "unknown")
            segments = [{"start": s.start, "text": s.text} for s in transcript]
            # Attach language info for LLM prompt
            if segments:
                segments[0]["_language"] = language_code
            return segments
        except Exception as e:
            last_err = e
            err_str = str(e)
            if "No transcript" in err_str or "no element" in err_str:
                raise
            if "not available" in err_str.lower() or "unavailable" in err_str.lower():
                raise
            if attempt < 2:
                _time.sleep(2 * (attempt + 1))
    raise last_err


def _format_transcript_text(segments):
    lines = []
    # Prepend language marker if available
    if segments and segments[0].get("_language"):
        lines.append(f"_language:{segments[0]['_language']}")
    for s in segments:
        lines.append(f"[{s['start']:.1f}s] {s['text']}")
    return "\n".join(lines)


def _insert_pending_with_transcript(video_id, title, duration, transcript_text, user_id, channel_id="", channel_name=""):
    row = {
        "video_id": video_id,
        "title": title or video_id,
        "duration_seconds": duration,
        "status": "pending",
        "transcript_text": transcript_text,
        "requested_by": user_id,
    }
    if channel_id:
        row["channel_id"] = channel_id
    if channel_name:
        row["channel_name"] = channel_name
    r = httpx.post(
        f"{_SUPABASE_URL}/rest/v1/eval_cache",
        json=row,
        headers={**_srk_headers(), "Prefer": "return=minimal"},
        timeout=10,
    )
    return r.status_code in (200, 201)


def _trigger_edge_function(video_id):
    """Send tiny trigger to Edge Function. Just video_id - it reads transcript from DB."""
    try:
        r = httpx.post(
            f"{_SUPABASE_URL}/functions/v1/evaluate-video",
            json={"video_id": video_id},
            headers={
                "Authorization": f"Bearer {_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except Exception:
            return self._json(400, {"error": "Invalid JSON body"})

        t_start = _time.time()

        video_url = body.get("video_url", "")
        video_id = _parse_video_id(video_url)
        if not video_id:
            return self._json(400, {"error": "Invalid or missing YouTube URL"})

        # Fetch title server-side - never trust the frontend
        title = _fetch_title_oembed(video_id) or video_id
        channel_id = body.get("channel_id", "")
        channel_name = body.get("channel_name", "")

        # Step 1: Auth
        t0 = _time.time()
        user_id = _verify_jwt(self.headers.get("Authorization"))
        _log(video_id, "auth", "ok" if user_id else "fail", duration_ms=int((_time.time() - t0) * 1000))
        if not user_id:
            return self._json(401, {"error": "Authentication required"})

        # Step 2: Cache check
        t0 = _time.time()
        cached = _check_eval_cache(video_id)
        _log(video_id, "cache_check", cached["status"] if cached else "miss", duration_ms=int((_time.time() - t0) * 1000))
        if cached:
            if cached["status"] == "complete":
                remaining = _deduct_credit(user_id)
                if remaining == -1:
                    return self._json(403, {"error": "Insufficient credits"})
                cached["credits_remaining"] = remaining
                return self._json(200, cached)
            if cached["status"] == "pending":
                return self._json(200, {"status": "pending", "video_id": video_id})

        curated = _check_video_evaluations(video_id)
        if curated:
            _log(video_id, "curated_hit", "ok")
            remaining = _deduct_credit(user_id)
            if remaining == -1:
                return self._json(403, {"error": "Insufficient credits"})
            curated["status"] = "complete"
            curated["cached"] = True
            curated["credits_remaining"] = remaining
            return self._json(200, curated)

        # Step 3: Credits
        remaining = _check_credits(user_id)
        if remaining == -1:
            _log(video_id, "credits", "insufficient")
            return self._json(403, {"error": "Insufficient credits"})

        # Step 4: Transcript fetch
        t0 = _time.time()
        try:
            segments = _fetch_transcript(video_id)
            _log(video_id, "transcript", "ok", detail=f"{len(segments)} segs", duration_ms=int((_time.time() - t0) * 1000))
        except Exception as e:
            err = str(e)
            _log(video_id, "transcript", "fail", detail=err[:200], duration_ms=int((_time.time() - t0) * 1000))
            if "IP" in err or "blocking" in err.lower():
                return self._json(502, {"error": "Transcript fetch blocked. Please try again."})
            if "No transcript" in err or "no element" in err:
                return self._json(404, {"error": "No English transcript available for this video"})
            return self._json(502, {"error": f"Transcript fetch failed: {err[:150]}"})

        if not segments:
            _log(video_id, "transcript", "empty")
            return self._json(404, {"error": "No English transcript available for this video"})

        transcript_text = _format_transcript_text(segments)
        duration = int(segments[-1]["start"]) + 10 if segments else 0

        if duration > 5400:
            _log(video_id, "duration_cap", "rejected", detail=f"{duration}s")
            return self._json(400, {"error": "Videos over 90 minutes are not supported"})

        # Step 5: Delete old error row if exists
        if cached and cached.get("status") == "error":
            httpx.delete(
                f"{_SUPABASE_URL}/rest/v1/eval_cache",
                params={"video_id": f"eq.{video_id}"},
                headers=_srk_headers(),
                timeout=10,
            )

        # Step 6: Insert pending row
        t0 = _time.time()
        ok = _insert_pending_with_transcript(video_id, title, duration, transcript_text, user_id, channel_id, channel_name)
        _log(video_id, "db_insert", "ok" if ok else "fail", duration_ms=int((_time.time() - t0) * 1000))
        if not ok:
            return self._json(500, {"error": "Failed to create eval job"})

        # Step 7: Trigger Edge Function
        t0 = _time.time()
        ef_result = _trigger_edge_function(video_id)
        ef_duration = int((_time.time() - t0) * 1000)
        ef_status = ef_result.get("status") if ef_result else "no_response"
        _log(video_id, "edge_trigger", ef_status, detail=str(ef_result)[:200] if ef_result else "timeout/error", duration_ms=ef_duration)

        total_ms = int((_time.time() - t_start) * 1000)
        _log(video_id, "total", "complete", detail=f"ef={ef_status}", duration_ms=total_ms)

        if ef_result and ef_result.get("status") == "complete":
            ef_result["credits_remaining"] = remaining - 1
            return self._json(200, ef_result)

        return self._json(200, {
            "status": "pending",
            "video_id": video_id,
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
