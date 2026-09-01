"""
Try multiple URL strategies to fetch thumbnails for the 5 channels
that failed handle lookup.
"""

import json
import re
import time
from pathlib import Path

import yt_dlp
import requests
from dotenv import load_dotenv
import os

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
OUT_DIR = _ROOT / "data" / "thumbnails" / "channels"
MANIFEST_FILE = OUT_DIR / "manifest.json"

# Try multiple URL/handle candidates per channel, in priority order
CANDIDATES = {
    "@motortrendchannel": {
        "channel": "MotorTrend Channel",
        "category": "Autos Vehicles",
        "urls": [
            "https://www.youtube.com/motortrend",
            "https://www.youtube.com/c/motortrend",
            "https://www.youtube.com/c/MotorTrend",
            "https://www.youtube.com/@motortrend",
        ],
        "api_handles": ["motortrend"],
        "api_usernames": ["motortrend"],
    },
    "@LoganPaul": {
        "channel": "Logan Paul",
        "category": "Entertainment",
        "urls": [
            "https://www.youtube.com/c/LoganPaul",
            "https://www.youtube.com/loganpaul",
            "https://www.youtube.com/@loganpaul",
            "https://www.youtube.com/user/LoganPaulVlogs",
        ],
        "api_handles": ["loganpaul"],
        "api_usernames": ["LoganPaulVlogs"],
    },
    "@fazerug": {
        "channel": "FaZe Rug",
        "category": "Gaming",
        "urls": [
            "https://www.youtube.com/c/FaZeRug",
            "https://www.youtube.com/@FaZeRug",
            "https://www.youtube.com/fazerug",
            "https://www.youtube.com/user/fazerug",
        ],
        "api_handles": ["FaZeRug", "fazerug"],
        "api_usernames": ["fazerug"],
    },
    "@hacksmithindustries": {
        "channel": "Hacksmith Industries",
        "category": "Science Technology",
        "urls": [
            "https://www.youtube.com/c/theHacksmith",
            "https://www.youtube.com/@theHacksmith",
            "https://www.youtube.com/hacksmith",
            "https://www.youtube.com/@hacksmith",
            "https://www.youtube.com/user/theHacksmith",
        ],
        "api_handles": ["theHacksmith", "hacksmith"],
        "api_usernames": ["theHacksmith"],
    },
    "@mrgear": {
        "channel": "MrGear",
        "category": "Science Technology",
        "urls": [
            "https://www.youtube.com/c/MrGear",
            "https://www.youtube.com/@MrGear",
            "https://www.youtube.com/mrgear",
            "https://www.youtube.com/user/MisterGear",
            "https://www.youtube.com/user/mrgear",
        ],
        "api_handles": ["MrGear", "mrgear"],
        "api_usernames": ["MisterGear", "mrgear"],
    },
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def try_api_handle(handle: str) -> tuple[str, int, int]:
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "snippet", "forHandle": handle.lstrip("@"), "key": YOUTUBE_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            for size in ("high", "medium", "default"):
                t = items[0]["snippet"].get("thumbnails", {}).get(size)
                if t and t.get("url"):
                    return t["url"], t.get("width", 0), t.get("height", 0)
    except Exception:
        pass
    return None, 0, 0


def try_api_username(username: str) -> tuple[str, int, int]:
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "snippet", "forUsername": username, "key": YOUTUBE_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            for size in ("high", "medium", "default"):
                t = items[0]["snippet"].get("thumbnails", {}).get(size)
                if t and t.get("url"):
                    return t["url"], t.get("width", 0), t.get("height", 0)
    except Exception:
        pass
    return None, 0, 0


def try_ytdlp_url(channel_url: str) -> tuple[str, int, int]:
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": 1,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
        thumbnails = info.get("thumbnails") or []
        if thumbnails:
            best = thumbnails[-1]
            return best.get("url"), best.get("width", 0), best.get("height", 0)
    except Exception:
        pass
    return None, 0, 0


def download_image(url: str, dest: Path) -> bool:
    resp = requests.get(url, timeout=15, stream=True)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    return True


def main():
    with open(MANIFEST_FILE, encoding="utf-8") as f:
        manifest = json.load(f)

    still_failed = []

    for original_handle, meta in CANDIDATES.items():
        channel = meta["channel"]
        category = meta["category"]
        cat_slug = slugify(category)
        handle_slug = slugify(original_handle.lstrip("@"))
        dest = OUT_DIR / cat_slug / f"{handle_slug}.jpg"

        print(f"\n{channel} ({original_handle})")
        thumb_url, width, height, source = None, 0, 0, None

        # 1. Try API forHandle variants
        for h in meta.get("api_handles", []):
            print(f"  API forHandle={h}", end=" ... ", flush=True)
            thumb_url, width, height = try_api_handle(h)
            if thumb_url:
                source = f"API handle={h}"
                print("OK")
                break
            print("miss")

        # 2. Try API forUsername variants
        if not thumb_url:
            for u in meta.get("api_usernames", []):
                print(f"  API forUsername={u}", end=" ... ", flush=True)
                thumb_url, width, height = try_api_username(u)
                if thumb_url:
                    source = f"API username={u}"
                    print("OK")
                    break
                print("miss")

        # 3. Try yt-dlp with various URL formats
        if not thumb_url:
            for url in meta.get("urls", []):
                print(f"  yt-dlp {url}", end=" ... ", flush=True)
                thumb_url, width, height = try_ytdlp_url(url)
                if thumb_url:
                    source = f"yt-dlp {url}"
                    print("OK")
                    break
                print("miss")

        if not thumb_url:
            print(f"  FAILED all strategies")
            still_failed.append(original_handle)
            continue

        try:
            download_image(thumb_url, dest)
            manifest[original_handle] = {
                "file": f"{cat_slug}/{handle_slug}.jpg",
                "category": category,
                "channel": channel,
                "handle": original_handle,
                "width": width,
                "height": height,
                "source_url": thumb_url,
                "fetch_source": source,
            }
            print(f"  Saved ({width}x{height})")
        except Exception as e:
            print(f"  Download failed: {e}")
            still_failed.append(original_handle)

        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        time.sleep(0.15)

    print(f"\n{'='*50}")
    print(f"Resolved: {len(CANDIDATES) - len(still_failed)}/{len(CANDIDATES)}")
    if still_failed:
        print("Still failed:")
        for h in still_failed:
            print(f"  {h}")
    else:
        print("All resolved!")


if __name__ == "__main__":
    main()
