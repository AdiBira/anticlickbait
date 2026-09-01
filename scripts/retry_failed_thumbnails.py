"""
Retry thumbnail fetch for failed channels using yt-dlp (more permissive handle resolution).
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
CHANNELS_FILE = _ROOT / "data" / "channels_final.json"

# Known handle corrections
HANDLE_OVERRIDES = {
    "@motortrendchannel":               "@motortrend",
    "@thetonightshowstarringjimmyfallon": "@FallonTonight",
    "@imsiowei":                        "@im_siowei",
    "@lawcrimenetwork":                 "@lawandcrime",
    "@mavigadget":                      None,  # non-English, skip
    "@LoganPaul":                       "@loganpaul",
    "@preston":                         "@PrestonPlayz",
    "@fazerug":                         "@FaZeRug",
    "@hindustantimes":                  "@HindustanTimesNews",
    "@thepetcollective":                "@petcollective",
    "@carlcunard":                      None,  # uncertain, skip
    "@nationalgeographic":              "@NatGeo",
    "@marquesbrownlee":                 "@mkbhd",
    "@quantumtechhd":                   None,  # uncertain, skip
    "@hacksmithindustries":             "@theHacksmith",
    "@mrgear":                          "@MrGear",
    "@pikespeakregionattractions":      None,  # wrong channel, skip
    "@dalephilip":                      None,  # no standard handle, skip
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def fetch_via_api(handle: str) -> tuple[str, int, int]:
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
    for size in ("high", "medium", "default"):
        t = thumbnails.get(size)
        if t and t.get("url"):
            return t["url"], t.get("width", 0), t.get("height", 0)
    return None, 0, 0


def fetch_via_ytdlp(handle: str) -> tuple[str, int, int]:
    url = f"https://www.youtube.com/{handle}"
    ydl_opts = {"extract_flat": True, "quiet": True, "no_warnings": True, "playlistend": 1}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        thumbnails = info.get("thumbnails") or []
        # yt-dlp returns thumbnails sorted smallest to largest
        if thumbnails:
            best = thumbnails[-1]
            return best.get("url"), best.get("width", 0), best.get("height", 0)
    except Exception as e:
        print(f"  yt-dlp failed: {e}")
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

    with open(CHANNELS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    # Build lookup of original handle → (channel name, category)
    channel_meta = {}
    for category, group in data["categories"].items():
        for entry in group.get("main", []):
            channel_meta[entry["handle"]] = (entry["channel"], category)

    failed_handles = [h for h in HANDLE_OVERRIDES if h not in manifest]
    print(f"Retrying {len(failed_handles)} failed handles\n")

    still_failed = []
    for original_handle in failed_handles:
        channel, category = channel_meta.get(original_handle, (original_handle, "unknown"))
        corrected = HANDLE_OVERRIDES[original_handle]
        cat_slug = slugify(category)
        handle_slug = slugify(original_handle.lstrip("@"))
        dest = OUT_DIR / cat_slug / f"{handle_slug}.jpg"

        print(f"{channel} ({original_handle})", end="")

        if corrected is None:
            print(" → SKIP (no valid handle)")
            still_failed.append(original_handle)
            continue

        print(f" → trying {corrected}", end=" ... ", flush=True)

        thumb_url, width, height = fetch_via_api(corrected)
        source = "API"

        if not thumb_url:
            print(f"API miss, trying yt-dlp", end=" ... ", flush=True)
            thumb_url, width, height = fetch_via_ytdlp(corrected)
            source = "yt-dlp"

        if not thumb_url:
            print("FAILED")
            still_failed.append(original_handle)
            continue

        try:
            download_image(thumb_url, dest)
            manifest[original_handle] = {
                "file": f"{cat_slug}/{handle_slug}.jpg",
                "category": category,
                "channel": channel,
                "handle": original_handle,
                "resolved_handle": corrected,
                "width": width,
                "height": height,
                "source_url": thumb_url,
                "fetch_source": source,
            }
            print(f"OK ({width}x{height}) via {source}")
        except Exception as e:
            print(f"DOWNLOAD FAILED: {e}")
            still_failed.append(original_handle)

        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        time.sleep(0.1)

    print(f"\nDone. {len(failed_handles) - len(still_failed)} newly fetched. Still failed: {len(still_failed)}")
    for h in still_failed:
        print(f"  {h}")


if __name__ == "__main__":
    main()
