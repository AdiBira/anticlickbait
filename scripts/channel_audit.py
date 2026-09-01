"""
Comprehensive channel audit: validates title, thumbnail, category, region,
subscriber count, and fetches 15 videos per channel with titles, thumbnails,
and hyperlinks. Outputs an HTML report for visual review.
"""

import json
import math
import os
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

import requests
from youtube_api import get_channel_info_by_handle, get_channel_videos
from categories import get_category_name

CHANNELS_FILE = _ROOT / "data" / "channels_final.json"
THUMBNAILS_MANIFEST = _ROOT / "data" / "thumbnails" / "channels" / "manifest.json"
OUT_DIR = _ROOT / "data" / "audit"
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_JSON = OUT_DIR / "channel_audit.json"
AUDIT_HTML = OUT_DIR / "channel_audit.html"

YT_CATEGORIES = {
    1: "Film & Animation", 2: "Autos & Vehicles", 10: "Music",
    15: "Pets & Animals", 17: "Sports", 19: "Travel & Events",
    20: "Gaming", 22: "People & Blogs", 23: "Comedy", 24: "Entertainment",
    25: "News & Politics", 26: "Howto & Style", 27: "Education",
    28: "Science & Technology", 29: "Nonprofits & Activism",
}


def fmt_subs(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.0f}K"
    return str(n)


def fmt_duration(s):
    if s >= 3600: return f"{s//3600}h {(s%3600)//60}m"
    return f"{s//60}m {s%60}s"


def fmt_views(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.0f}K"
    return str(n)


def load_channels():
    data = json.loads(CHANNELS_FILE.read_text())
    channels = []
    for category, group in data["categories"].items():
        for entry in group.get("main", []):
            channels.append({**entry, "our_category": category})
    return channels


def build_audit():
    channels = load_channels()
    manifest = json.loads(THUMBNAILS_MANIFEST.read_text())
    results = []
    total = len(channels)

    for i, ch in enumerate(channels):
        handle = ch["handle"]
        our_category = ch["our_category"]
        print(f"[{i+1}/{total}] {ch['channel']} ({handle})", flush=True)

        # Fetch live channel info
        info = get_channel_info_by_handle(handle)
        if not info:
            print(f"  WARNING: channel info not found")
            info = {}

        actual_title = info.get("title") or ch["channel"]
        actual_subs = info.get("subscriber_count") or ch["subs"]
        country = info.get("country") or "—"
        channel_id = info.get("channel_id", "")

        # Fetch videos
        videos = []
        if channel_id:
            raw = get_channel_videos(channel_id, max_videos=15)
            for v in raw:
                videos.append({
                    "video_id": v["video_id"],
                    "title": v["title"],
                    "thumbnail_url": v.get("thumbnail_url"),
                    "duration_seconds": v.get("duration_seconds", 0),
                    "view_count": v.get("view_count", 0),
                    "published_at": (v.get("published_at") or "")[:10],
                    "yt_category": YT_CATEGORIES.get(v.get("category_id", 0), "Unknown"),
                    "pool_source": v.get("_pool_source") or "latest",
                    "url": f"https://www.youtube.com/watch?v={v['video_id']}",
                })

        # Channel thumbnail path
        thumb_info = manifest.get(handle, {})
        thumb_file = thumb_info.get("file", "")
        thumb_path = f"../thumbnails/channels/{thumb_file}" if thumb_file else ""

        results.append({
            "handle": handle,
            "our_name": ch["channel"],
            "actual_title": actual_title,
            "our_category": our_category,
            "country": country,
            "our_subs": ch["subs"],
            "actual_subs": actual_subs,
            "channel_id": channel_id,
            "thumb_path": thumb_path,
            "video_count": len(videos),
            "videos": videos,
        })

        print(f"  {actual_title} | {country} | {fmt_subs(actual_subs)} subs | {len(videos)} videos")
        time.sleep(0.05)

    AUDIT_JSON.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nAudit data saved to {AUDIT_JSON}")
    return results


def render_html(results):
    # Group by category
    by_category = {}
    channels_json = json.loads(CHANNELS_FILE.read_text())
    for ch in results:
        cat = ch["our_category"]
        by_category.setdefault(cat, []).append(ch)

    category_order = list(channels_json["categories"].keys())

    sections = ""
    for cat in category_order:
        channels = by_category.get(cat, [])
        if not channels:
            continue

        channel_cards = ""
        for ch in channels:
            title_match = ch["our_name"].lower().strip() == ch["actual_title"].lower().strip()
            title_flag = "" if title_match else f' <span class="flag">⚠ "{ch["actual_title"]}"</span>'

            subs_pct = abs(ch["actual_subs"] - ch["our_subs"]) / max(ch["our_subs"], 1) * 100
            subs_flag = f' <span class="flag">⚠ actual: {fmt_subs(ch["actual_subs"])}</span>' if subs_pct > 15 else ""

            video_flag = f' <span class="flag">⚠ only {ch["video_count"]}/15</span>' if ch["video_count"] < 15 else ""

            thumb_html = (
                f'<img src="{ch["thumb_path"]}" class="ch-thumb" alt="">'
                if ch["thumb_path"]
                else '<div class="ch-thumb-missing">?</div>'
            )

            video_rows = ""
            for j, v in enumerate(ch["videos"]):
                vthumb = f'<img src="{v["thumbnail_url"]}" class="v-thumb">' if v.get("thumbnail_url") else '<div class="v-thumb-missing"></div>'
                video_rows += f"""
                <tr>
                  <td class="vnum">{j+1}</td>
                  <td>{vthumb}</td>
                  <td><a href="{v['url']}" target="_blank">{v['title']}</a></td>
                  <td class="meta">{fmt_duration(v['duration_seconds'])}</td>
                  <td class="meta">{fmt_views(v['view_count'])}</td>
                  <td class="meta">{v['published_at']}</td>
                  <td class="meta">{v['yt_category']}</td>
                  <td class="meta pool-{v['pool_source']}">{v['pool_source']}</td>
                </tr>"""

            channel_cards += f"""
            <div class="channel-card">
              <div class="ch-header">
                {thumb_html}
                <div class="ch-meta">
                  <div class="ch-name">{ch['our_name']}{title_flag}</div>
                  <div class="ch-detail">
                    <span class="tag">{ch['our_category']}</span>
                    <span class="tag country">{ch['country']}</span>
                    <span>{fmt_subs(ch['our_subs'])} subs{subs_flag}</span>
                    <span class="{'ok' if ch['video_count'] == 15 else 'warn'}">{ch['video_count']}/15 videos{video_flag}</span>
                  </div>
                  <div class="ch-handle">{ch['handle']}</div>
                </div>
              </div>
              <details>
                <summary>{ch['video_count']} videos selected</summary>
                <table class="video-table">
                  <thead><tr><th>#</th><th></th><th>Title</th><th>Duration</th><th>Views</th><th>Published</th><th>YT Category</th><th>Pool</th></tr></thead>
                  <tbody>{video_rows}</tbody>
                </table>
              </details>
            </div>"""

        sections += f"""
        <section>
          <h2>{cat} <span class="count">{len(channels)} channels</span></h2>
          {channel_cards}
        </section>"""

    total_channels = len(results)
    total_videos = sum(ch["video_count"] for ch in results)
    flags = sum(1 for ch in results if ch["video_count"] < 15)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Anticlickbait Channel Audit</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; }}
  header {{ padding: 24px 32px; background: #1a1a1a; border-bottom: 1px solid #333; display: flex; align-items: baseline; gap: 16px; }}
  header h1 {{ font-size: 20px; font-weight: 600; color: #fff; }}
  .stats {{ font-size: 13px; color: #888; }}
  .stats span {{ color: #aaa; font-weight: 500; }}
  main {{ max-width: 1200px; margin: 0 auto; padding: 24px 32px; }}
  section {{ margin-bottom: 40px; }}
  h2 {{ font-size: 16px; font-weight: 600; color: #fff; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #2a2a2a; }}
  .count {{ font-size: 12px; color: #666; font-weight: 400; margin-left: 6px; }}
  .channel-card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; margin-bottom: 10px; overflow: hidden; }}
  .ch-header {{ display: flex; align-items: center; gap: 14px; padding: 12px 16px; }}
  .ch-thumb {{ width: 52px; height: 52px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }}
  .ch-thumb-missing {{ width: 52px; height: 52px; border-radius: 50%; background: #333; display: flex; align-items: center; justify-content: center; color: #666; flex-shrink: 0; }}
  .ch-meta {{ flex: 1; }}
  .ch-name {{ font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 4px; }}
  .ch-detail {{ font-size: 12px; color: #888; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 3px; }}
  .ch-handle {{ font-size: 11px; color: #555; }}
  .tag {{ background: #252525; border: 1px solid #333; padding: 1px 7px; border-radius: 12px; font-size: 11px; color: #aaa; }}
  .tag.country {{ background: #1e2a1e; border-color: #2a4a2a; color: #6a9a6a; }}
  .flag {{ color: #f0a020; font-size: 11px; font-weight: 400; }}
  .ok {{ color: #4caf50; }}
  .warn {{ color: #f0a020; }}
  details summary {{ padding: 8px 16px; font-size: 12px; color: #666; cursor: pointer; background: #141414; border-top: 1px solid #222; }}
  details summary:hover {{ color: #aaa; }}
  details[open] summary {{ color: #aaa; }}
  .video-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .video-table th {{ padding: 6px 10px; text-align: left; color: #555; font-weight: 500; border-bottom: 1px solid #222; background: #111; }}
  .video-table td {{ padding: 6px 10px; border-bottom: 1px solid #1a1a1a; vertical-align: middle; }}
  .video-table tr:last-child td {{ border-bottom: none; }}
  .video-table a {{ color: #60a0e0; text-decoration: none; }}
  .video-table a:hover {{ text-decoration: underline; }}
  .v-thumb {{ width: 80px; height: 45px; object-fit: cover; border-radius: 3px; display: block; }}
  .v-thumb-missing {{ width: 80px; height: 45px; background: #222; border-radius: 3px; }}
  .vnum {{ color: #444; width: 24px; }}
  .meta {{ color: #666; white-space: nowrap; }}
  .pool-latest {{ color: #4a8aba; }}
  .pool-popular {{ color: #a06ab4; }}
</style>
</head>
<body>
<header>
  <h1>Anticlickbait Channel Audit</h1>
  <div class="stats">
    <span>{total_channels}</span> channels &nbsp;·&nbsp;
    <span>{total_videos}</span> videos selected &nbsp;·&nbsp;
    <span class="{'warn' if flags else 'ok'}">{flags} channels under 15 videos</span>
  </div>
</header>
<main>{sections}</main>
</body>
</html>"""

    AUDIT_HTML.write_text(html, encoding="utf-8")
    print(f"HTML report saved to {AUDIT_HTML}")


if __name__ == "__main__":
    # Load existing audit data if present, skip re-fetch
    if AUDIT_JSON.exists() and "--refresh" not in sys.argv:
        print(f"Loading existing audit data from {AUDIT_JSON}")
        print("Run with --refresh to re-fetch from API")
        results = json.loads(AUDIT_JSON.read_text())
    else:
        results = build_audit()

    render_html(results)
    print(f"\nOpen in browser: file://{AUDIT_HTML}")
