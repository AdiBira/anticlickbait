#!/usr/bin/env python3
"""
Test: Backend ignores frontend-supplied title

Verifies that /api/evaluate does NOT store the frontend's title parameter
in eval_cache. The backend should fetch the title independently (oEmbed)
so a stale frontend title can never corrupt the LLM evaluation.

Usage: python tests/test_backend_title_ignored.py
Requires: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY env vars (or reads from web/.env.local)
"""

import json
import os
import sys

import httpx

# Load env from web/.env.local if not set
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", "web", ".env.local")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    if key not in os.environ:
                        os.environ[key] = val

_load_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
SRK = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
API_BASE = os.environ.get("NEXT_PUBLIC_API_BASE", "https://anticlickbait.vercel.app")

CANARY_TITLE = "WRONG_TITLE_CANARY_12345"
# Use a short, well-known video that has a transcript
TEST_VIDEO_ID = "jNQXAC9IVRw"  # "Me at the zoo"
TEST_VIDEO_URL = f"https://youtube.com/watch?v={TEST_VIDEO_ID}"


def srk_headers():
    return {
        "apikey": SRK,
        "Authorization": f"Bearer {SRK}",
        "Content-Type": "application/json",
    }


def cleanup_test_row():
    """Delete any existing eval_cache row for the test video."""
    r = httpx.delete(
        f"{SUPABASE_URL}/rest/v1/eval_cache",
        params={"video_id": f"eq.{TEST_VIDEO_ID}"},
        headers=srk_headers(),
        timeout=10,
    )
    return r.status_code in (200, 204)


def check_stored_title():
    """Read the title stored in eval_cache for the test video."""
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/eval_cache",
        params={
            "video_id": f"eq.{TEST_VIDEO_ID}",
            "select": "video_id,title,status",
        },
        headers=srk_headers(),
        timeout=10,
    )
    if r.status_code == 200:
        data = r.json()
        if data:
            return data[0]
    return None


def get_test_token():
    """Get an auth token. This test requires a logged-in user.
    For CI, you'd use a service account. For local, we check if there's a session."""
    print("  NOTE: This test requires a valid auth token.")
    print("  If running locally, log in at the app first, then provide the token.")
    token = os.environ.get("TEST_AUTH_TOKEN", "")
    if not token:
        print("  Set TEST_AUTH_TOKEN env var to a valid Supabase JWT.")
        print("  You can get one from browser DevTools: supabase.auth.getSession()")
        return None
    return token


def main():
    if not SUPABASE_URL or not SRK:
        print("FAIL: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        return 1

    print(f"[1/5] Cleaning up existing eval_cache row for {TEST_VIDEO_ID}")
    cleanup_test_row()

    print(f"[2/5] Verifying row is deleted")
    row = check_stored_title()
    if row:
        print(f"  FAIL: Row still exists after cleanup: {row}")
        return 1
    print("  OK: No existing row")

    token = get_test_token()
    if not token:
        print("SKIP: No auth token available")
        return 0

    print(f"[3/5] Calling /api/evaluate with canary title: '{CANARY_TITLE}'")
    try:
        r = httpx.post(
            f"{API_BASE}/api/evaluate",
            json={
                "video_url": TEST_VIDEO_URL,
                "title": CANARY_TITLE,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        print(f"  Response: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  Request failed: {e}")
        return 1

    print(f"[4/5] Checking stored title in eval_cache")
    row = check_stored_title()
    if not row:
        print("  FAIL: No row created in eval_cache")
        return 1

    stored_title = row.get("title", "")
    print(f"  Stored title: '{stored_title}'")

    if stored_title == CANARY_TITLE:
        print(f"  FAIL: Backend stored the frontend's canary title verbatim!")
        print(f"  The backend must fetch the title independently, not trust the frontend.")
        # Cleanup
        cleanup_test_row()
        return 1

    if stored_title == TEST_VIDEO_ID:
        print(f"  WARN: Title is just the video_id (oEmbed may have failed)")
    else:
        print(f"  OK: Title does NOT match canary. Backend fetched independently.")

    print(f"[5/5] Cleaning up test row")
    cleanup_test_row()

    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
