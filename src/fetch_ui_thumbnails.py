#!/usr/bin/env python3
"""Fetch channel avatars and video thumbnails for the static rankings UI.

Outputs:
- data/assets/channel_avatars/<channel_id>.<ext>
- data/assets/video_thumbs/<video_id>.<ext>
- data/assets/channel_avatars_manifest.json

Notes:
- Safe to rerun (skips existing files by default).
- Does not modify scoring results or DB rows.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from youtube_api import get_youtube_client

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "anticlickbait.db"
ASSETS_DIR = ROOT / "data" / "assets"
CHANNEL_DIR = ASSETS_DIR / "channel_avatars"
VIDEO_DIR = ASSETS_DIR / "video_thumbs"
MANIFEST_PATH = ASSETS_DIR / "channel_avatars_manifest.json"


def get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def ensure_dirs() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    CHANNEL_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)


def load_manifest() -> dict[str, dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}
    except Exception:
        return {}


def save_manifest(data: dict[str, dict[str, Any]]) -> None:
    MANIFEST_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def file_ext_from_url(url: str, *, default: str = ".jpg") -> str:
    try:
        path = urllib.parse.urlparse(url).path or ""
        suffix = Path(path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            return ".jpg" if suffix == ".jpeg" else suffix
    except Exception:
        pass
    return default


def download_file(url: str, dest: Path, *, timeout: int = 20, retries: int = 2) -> bool:
    if not url:
        return False
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    ssl_ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        },
    )
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as r:
                if getattr(r, "status", 200) >= 400:
                    raise RuntimeError(f"HTTP {r.status}")
                data = r.read()
            if not data:
                raise RuntimeError("Empty response")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return True
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    print(f"  download failed: {url} -> {dest.name} ({last_err})", file=sys.stderr)
    if tmp.exists():
        try:
            tmp.unlink()
        except Exception:
            pass
    return False


def fetch_channel_avatar_urls(channel_ids: list[str]) -> dict[str, str]:
    yt = get_youtube_client()
    out: dict[str, str] = {}
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        if not batch:
            continue
        resp = yt.channels().list(part="snippet", id=",".join(batch), maxResults=min(50, len(batch))).execute()
        for item in resp.get("items", []):
            cid = item.get("id")
            thumbs = item.get("snippet", {}).get("thumbnails", {})
            url = (
                thumbs.get("high", {}).get("url")
                or thumbs.get("medium", {}).get("url")
                or thumbs.get("default", {}).get("url")
            )
            if cid and url:
                out[cid] = url
        time.sleep(0.1)
    return out


def select_channel_ids(conn: sqlite3.Connection, max_channels: int | None = None) -> list[str]:
    rows = q(
        conn,
        """
        SELECT c.channel_id
        FROM channels c
        LEFT JOIN video_evaluations v ON v.channel_id = c.channel_id
        GROUP BY c.channel_id
        ORDER BY
          COALESCE(SUM(CASE WHEN v.evaluation_success=1 THEN 1 ELSE 0 END), 0) DESC,
          COALESCE(c.subscriber_count, 0) DESC
        """,
    )
    ids = [r["channel_id"] for r in rows if r.get("channel_id")]
    return ids[:max_channels] if max_channels else ids


def select_video_thumbnails(conn: sqlite3.Connection, max_videos: int | None = None) -> list[tuple[str, str]]:
    rows = q(
        conn,
        """
        SELECT video_id, thumbnail_url
        FROM video_evaluations
        WHERE thumbnail_url IS NOT NULL AND TRIM(thumbnail_url) <> ''
        ORDER BY COALESCE(evaluated_at, created_at) DESC
        """,
    )
    pairs = [(r["video_id"], r["thumbnail_url"]) for r in rows if r.get("video_id") and r.get("thumbnail_url")]
    if max_videos:
        pairs = pairs[:max_videos]
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch channel/video thumbnails for the static UI")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--max-channels", type=int, default=0, help="Limit channel avatar fetches (0 = no limit)")
    ap.add_argument("--max-videos", type=int, default=0, help="Limit video thumbnail downloads (0 = no limit)")
    ap.add_argument("--skip-existing", action="store_true", default=True, help="Skip files already present (default: true)")
    ap.add_argument("--no-skip-existing", dest="skip_existing", action="store_false")
    ap.add_argument("--channels-only", action="store_true")
    ap.add_argument("--videos-only", action="store_true")
    args = ap.parse_args()

    ensure_dirs()
    manifest = load_manifest()

    conn = get_conn(args.db)
    try:
        channel_ids = select_channel_ids(conn, args.max_channels or None) if not args.videos_only else []
        video_pairs = select_video_thumbnails(conn, args.max_videos or None) if not args.channels_only else []
    finally:
        conn.close()

    print(f"Channel candidates: {len(channel_ids)}")
    print(f"Video thumbnail candidates: {len(video_pairs)}")

    channel_downloaded = 0
    channel_skipped = 0
    channel_failed = 0

    if channel_ids:
        print("Fetching channel avatar URLs from YouTube API...")
        url_map = fetch_channel_avatar_urls(channel_ids)
        print(f"Resolved channel avatar URLs: {len(url_map)}/{len(channel_ids)}")
        for idx, cid in enumerate(channel_ids, start=1):
            url = url_map.get(cid)
            entry = manifest.get(cid, {})
            if url:
                entry["url"] = url
            if not url:
                channel_failed += 1
                continue
            ext = file_ext_from_url(url, default=".jpg")
            local_filename = f"{cid}{ext}"
            local_path = CHANNEL_DIR / local_filename
            entry["local_rel"] = f"assets/channel_avatars/{local_filename}"
            manifest[cid] = entry
            if args.skip_existing and local_path.exists():
                channel_skipped += 1
                continue
            ok = download_file(url, local_path)
            if ok:
                channel_downloaded += 1
            else:
                channel_failed += 1
            if idx % 25 == 0:
                print(f"  channels: {idx}/{len(channel_ids)} processed")
        save_manifest(manifest)

    video_downloaded = 0
    video_skipped = 0
    video_failed = 0
    if video_pairs:
        for idx, (video_id, url) in enumerate(video_pairs, start=1):
            ext = file_ext_from_url(url, default=".jpg")
            dest = VIDEO_DIR / f"{video_id}{ext}"
            if args.skip_existing and dest.exists():
                video_skipped += 1
                continue
            ok = download_file(url, dest)
            if ok:
                video_downloaded += 1
            else:
                video_failed += 1
            if idx % 50 == 0:
                print(f"  videos: {idx}/{len(video_pairs)} processed")

    print("\nDone.")
    print(f"Channels downloaded/skipped/failed: {channel_downloaded}/{channel_skipped}/{channel_failed}")
    print(f"Videos downloaded/skipped/failed:   {video_downloaded}/{video_skipped}/{video_failed}")
    print(f"Manifest: {MANIFEST_PATH}")
    print(f"Channel dir: {CHANNEL_DIR}")
    print(f"Video dir:   {VIDEO_DIR}")


if __name__ == "__main__":
    main()
