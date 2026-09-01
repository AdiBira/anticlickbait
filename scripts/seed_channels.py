"""
Seed 150 main channels into Supabase with metadata from YouTube API cache + thumbnails.
Usage: python scripts/seed_channels.py
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
client = create_client(SUPABASE_URL, os.environ["SUPABASE_SERVICE_ROLE_KEY"])

BASE = Path(__file__).parent.parent / ".workspace/worktrees/core-web"
CACHE_DIR = BASE / "data/cache/youtube_api/channel_info_by_handle"
CHANNELS_FILE = BASE / "data/channels_final.json"
MANIFEST_FILE = BASE / "data/thumbnails/channels/manifest.json"

# Map channels_final.json category names -> Supabase category_id
CATEGORY_MAP = {
    "Autos Vehicles": 2,
    "Comedy": 23,
    "Education": 27,
    "Entertainment": 24,
    "Gaming": 20,
    "News Politics": 25,
    "Pets Animals": 15,
    "Science Technology": 28,
    "Sports": 17,
    "Travel Events": 19,
}


def main():
    with open(CHANNELS_FILE) as f:
        data = json.load(f)

    manifest = json.loads(MANIFEST_FILE.read_text())
    manifest_lower = {k.lower(): v for k, v in manifest.items()}

    rows = []
    for cat_name, info in data["categories"].items():
        category_id = CATEGORY_MAP.get(cat_name)
        if not category_id:
            print(f"WARNING: no category_id for '{cat_name}', skipping")
            continue

        for ch in info.get("main", []):
            handle = ch["handle"].lower()
            cache_file = CACHE_DIR / f"{handle}.json"

            if not cache_file.exists():
                print(f"  No cache for {handle}, skipping")
                continue

            api_data = json.loads(cache_file.read_text())["data"]

            # Thumbnail URL
            clean_handle = handle.lstrip("@")
            thumb_url = f"{SUPABASE_URL}/storage/v1/object/public/thumbnails/channels/{clean_handle}.jpg"

            # Check if thumbnail actually exists in manifest
            if handle not in manifest_lower:
                thumb_url = None

            rows.append({
                "channel_id": api_data["channel_id"],
                "handle": api_data.get("custom_url") or handle,
                "title": api_data["title"],
                "description": api_data.get("description"),
                "custom_url": api_data.get("custom_url"),
                "country": api_data.get("country"),
                "language": "en",
                "category_id": category_id,
                "subscriber_count": api_data.get("subscriber_count", 0),
                "video_count": api_data.get("video_count", 0),
                "view_count": api_data.get("view_count", 0),
                "thumbnail_url": thumb_url,
            })

    print(f"Inserting {len(rows)} channels...")

    # Batch upsert
    BATCH = 50
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        client.from_("channels").upsert(batch, on_conflict="channel_id").execute()
        print(f"  Upserted {min(i + BATCH, len(rows))}/{len(rows)}")

    print("Done.")


if __name__ == "__main__":
    main()
