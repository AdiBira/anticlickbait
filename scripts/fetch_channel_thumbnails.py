"""
Fetch and store channel thumbnails for all channels in channels_final.json.

Storage:
  data/thumbnails/channels/{category_slug}/{handle_slug}.jpg
  data/thumbnails/channels/manifest.json

Highest quality available: high (800x800) > medium (240x240) > default (88x88)
"""

import json
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
CHANNELS_FILE = _ROOT / "data" / "channels_final.json"
OUT_DIR = _ROOT / "data" / "thumbnails" / "channels"
MANIFEST_FILE = OUT_DIR / "manifest.json"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def best_thumbnail(thumbnails: dict) -> tuple[str, int, int]:
    """Return (url, width, height) for the highest quality available."""
    for size in ("high", "medium", "default"):
        t = thumbnails.get(size)
        if t and t.get("url"):
            return t["url"], t.get("width", 0), t.get("height", 0)
    return None, 0, 0


def fetch_channel_thumbnail_url(handle: str) -> tuple[str, int, int]:
    """Call YouTube Data API channels.list and return best thumbnail info."""
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "snippet",
        "forHandle": handle.lstrip("@"),
        "key": YOUTUBE_API_KEY,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    if not items:
        return None, 0, 0
    thumbnails = items[0]["snippet"].get("thumbnails", {})
    return best_thumbnail(thumbnails)


def download_image(url: str, dest: Path) -> bool:
    resp = requests.get(url, timeout=15, stream=True)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    return True


def main():
    if not YOUTUBE_API_KEY:
        print("Error: YOUTUBE_API_KEY not set in .env")
        sys.exit(1)

    with open(CHANNELS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    # Load existing manifest if present (to skip already fetched)
    manifest = {}
    if MANIFEST_FILE.exists():
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            manifest = json.load(f)

    categories = data.get("categories", {})
    total = sum(len(g.get("main", [])) for g in categories.values())
    done = 0
    skipped = 0
    failed = []

    for category, group in categories.items():
        cat_slug = slugify(category)
        for entry in group.get("main", []):
            handle = entry["handle"]
            channel = entry["channel"]
            handle_slug = slugify(handle.lstrip("@"))
            rel_path = f"{cat_slug}/{handle_slug}.jpg"
            dest = OUT_DIR / cat_slug / f"{handle_slug}.jpg"

            done += 1
            print(f"[{done}/{total}] {channel} ({handle})", end=" ... ", flush=True)

            if handle in manifest and dest.exists():
                print("skipped (cached)")
                skipped += 1
                continue

            try:
                thumb_url, width, height = fetch_channel_thumbnail_url(handle)
                if not thumb_url:
                    print("SKIP (not found via API)")
                    failed.append({"handle": handle, "channel": channel, "reason": "not found"})
                    continue

                download_image(thumb_url, dest)
                manifest[handle] = {
                    "file": rel_path,
                    "category": category,
                    "channel": channel,
                    "handle": handle,
                    "width": width,
                    "height": height,
                    "source_url": thumb_url,
                }
                print(f"OK ({width}x{height})")

            except Exception as e:
                print(f"FAILED: {e}")
                failed.append({"handle": handle, "channel": channel, "reason": str(e)})

            # Save manifest after each channel so progress survives interruption
            MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)

            time.sleep(0.1)  # gentle rate limiting

    print(f"\nDone. {len(manifest)} thumbnails saved, {skipped} skipped, {len(failed)} failed.")
    if failed:
        print("Failed channels:")
        for f in failed:
            print(f"  {f['handle']} — {f['reason']}")


if __name__ == "__main__":
    main()
