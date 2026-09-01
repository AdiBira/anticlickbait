"""
Migrate SQLite data to Supabase.
Usage: python scripts/migrate_to_supabase.py
"""

import os
import sys
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from supabase import create_client

SQLITE_PATH = Path(__file__).parent.parent / ".workspace/worktrees/core-web/data/anticlickbait.db"
BATCH_SIZE = 100

client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


def fetch_all(conn, table):
    c = conn.cursor()
    c.execute(f"SELECT * FROM [{table}]")
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, row)) for row in c.fetchall()]


def upsert_batched(table, rows, conflict_col, transform=None):
    if not rows:
        print(f"  {table}: 0 rows, skipping")
        return
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        if transform:
            batch = [transform(r) for r in batch]
        client.from_(table).upsert(batch, on_conflict=conflict_col).execute()
        print(f"  {table}: upserted {min(i + BATCH_SIZE, len(rows))}/{len(rows)}")


def transform_channel(row):
    row["handle"] = row.pop("custom_url", None)
    return row


def transform_video(row):
    # Remove SQLite-only fields not in Supabase
    row.pop("thumbnail_visual_elements", None)  # JSONB needs proper handling
    # Convert thumbnail_visual_elements if present
    return row


def main():
    if not SQLITE_PATH.exists():
        print(f"SQLite DB not found: {SQLITE_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(SQLITE_PATH))

    print("Migrating categories...")
    categories = fetch_all(conn, "categories")
    upsert_batched("categories", categories, "category_id")

    print("Migrating channels...")
    channels = fetch_all(conn, "channels")
    upsert_batched("channels", channels, "channel_id", transform=transform_channel)

    print("Migrating video_evaluations...")
    videos = fetch_all(conn, "video_evaluations")
    upsert_batched("video_evaluations", videos, "video_id", transform=transform_video)

    conn.close()
    print(f"\nDone. Migrated {len(categories)} categories, {len(channels)} channels, {len(videos)} videos.")


if __name__ == "__main__":
    main()
