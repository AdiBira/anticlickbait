"""
Upload channel thumbnails to Supabase Storage and update channels table.
Usage: python scripts/upload_thumbnails.py
"""

import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
client = create_client(SUPABASE_URL, os.environ["SUPABASE_SERVICE_ROLE_KEY"])

THUMBS_DIR = Path(__file__).parent.parent / ".workspace/worktrees/core-web/data/thumbnails/channels"
MANIFEST = THUMBS_DIR / "manifest.json"
BUCKET = "thumbnails"


def create_bucket():
    try:
        client.storage.create_bucket(BUCKET, options={"public": True})
        print(f"Created bucket: {BUCKET}")
    except Exception as e:
        if "already exists" in str(e).lower() or "Duplicate" in str(e):
            print(f"Bucket '{BUCKET}' already exists")
        else:
            raise


def upload_and_update():
    manifest = json.loads(MANIFEST.read_text())
    total = len(manifest)
    uploaded = 0
    errors = []

    for handle, info in manifest.items():
        clean_handle = handle.lstrip("@")
        storage_path = f"channels/{clean_handle}.jpg"
        local_file = THUMBS_DIR / info["file"]

        if not local_file.exists():
            errors.append(f"Missing file: {local_file}")
            continue

        # Upload to storage
        with open(local_file, "rb") as f:
            try:
                client.storage.from_(BUCKET).upload(
                    storage_path, f.read(),
                    file_options={"content-type": "image/jpeg", "upsert": "true"}
                )
            except Exception as e:
                if "Duplicate" in str(e) or "already exists" in str(e).lower():
                    pass  # Already uploaded
                else:
                    errors.append(f"{handle}: {e}")
                    continue

        # Build public URL
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{storage_path}"

        # Update channel row
        client.from_("channels").update({"thumbnail_url": public_url}).eq("handle", handle).execute()

        uploaded += 1
        if uploaded % 25 == 0:
            print(f"  Uploaded {uploaded}/{total}")

    print(f"\nDone. Uploaded {uploaded}/{total} thumbnails.")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    create_bucket()
    upload_and_update()
