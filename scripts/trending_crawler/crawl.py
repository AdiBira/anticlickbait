"""
Phase 4 trending cache-seeding crawler for the AntiClickbait extension pool.

Pipeline per region:
  1. Discover trending/popular videos via YouTube Data API v3 (chart=mostPopular).
     (Why not InnerTube browse/yt-dlp: see "Trending source" in README.md - both
     the classic browseId FEtrending and /feed/explore now hard-redirect logged-out
     sessions to the empty personalized home feed; yt-dlp itself errors on this.)
  2. Skip videos already scored (bulk PostgREST read, anon key) or already seen in
     a prior run of this script (local progress log) or already caught by the
     cheap pre-gates (music / live / too_long - no need to fetch a transcript).
  3. Fetch the transcript via the InnerTube ANDROID client (player -> captionTracks
     -> timedtext), same approach proven in extensions/spikes/transcript-poc.
  4. POST {video_id, segments, duration_s, meta, source:"crawler"} to the
     score-video Edge Function with the service-role key (skips quota). Real
     posting requires --execute AND env var CRAWLER_ALLOW_EXECUTE=1; default is
     always a dry run.

Run: python crawl.py --dry-run --limit 5 --regions IN
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
ENV_PATH = REPO_ROOT / ".env"
PROGRESS_LOG = HERE / "progress.jsonl"

WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip"
ANDROID_CLIENT_VERSION = "20.10.38"

MAX_DURATION_S = 5400  # server gate too_long; skip early to avoid a wasted transcript fetch


# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------

def load_env() -> dict:
    env = dict(os.environ)
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env.setdefault(k.strip(), v.strip())
    return env


# ---------------------------------------------------------------------------
# step 1: trending discovery (YouTube Data API v3)
# ---------------------------------------------------------------------------

def fetch_category_map(client: httpx.Client, api_key: str, region: str) -> dict:
    r = client.get(
        "https://www.googleapis.com/youtube/v3/videoCategories",
        params={"part": "snippet", "regionCode": region, "key": api_key},
        timeout=15,
    )
    r.raise_for_status()
    return {item["id"]: item["snippet"]["title"] for item in r.json().get("items", [])}


def parse_iso8601_duration(duration: str) -> int:
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", duration or "")
    if not m:
        return 0
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def fetch_trending(client: httpx.Client, api_key: str, region: str, per_region: int) -> list[dict]:
    category_map = fetch_category_map(client, api_key, region)
    videos = []
    page_token = None
    while len(videos) < per_region:
        params = {
            "part": "snippet,contentDetails",
            "chart": "mostPopular",
            "regionCode": region,
            "maxResults": min(50, per_region - len(videos)),
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        r = client.get("https://www.googleapis.com/youtube/v3/videos", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        for item in data.get("items", []):
            sn = item["snippet"]
            videos.append(
                {
                    "video_id": item["id"],
                    "region": region,
                    "title": sn.get("title"),
                    "category": category_map.get(sn.get("categoryId"), sn.get("categoryId")),
                    "is_live": sn.get("liveBroadcastContent") in {"live", "upcoming"},
                    "channel_id": sn.get("channelId"),
                    "default_audio_language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                    "duration_s": parse_iso8601_duration(item["contentDetails"]["duration"]),
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return videos[:per_region]


# ---------------------------------------------------------------------------
# step 2: existing-pool check (PostgREST bulk read) + local progress log
# ---------------------------------------------------------------------------

def fetch_existing_ids(client: httpx.Client, supabase_url: str, anon_key: str, video_ids: list[str]) -> set:
    existing = set()
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        ids_param = "in.(" + ",".join(f'"{v}"' for v in batch) + ")"
        r = client.get(
            f"{supabase_url}/rest/v1/video_scores",
            params={"video_id": ids_param, "select": "video_id"},
            headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
            timeout=15,
        )
        if r.status_code == 404:
            # video_scores table not deployed yet (Phase 1 not live) - nothing is cached.
            print("[bulk-check] video_scores table not found yet (backend not deployed) - treating pool as empty")
            return existing
        r.raise_for_status()
        for row in r.json():
            existing.add(row["video_id"])
    return existing


def load_progress_log() -> set:
    if not PROGRESS_LOG.exists():
        return set()
    seen = set()
    for line in PROGRESS_LOG.read_text().splitlines():
        try:
            seen.add(json.loads(line)["video_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return seen


def append_progress(video_id: str, outcome: str, detail: dict | None = None):
    row = {"video_id": video_id, "outcome": outcome, "at": datetime.now(timezone.utc).isoformat()}
    if detail:
        row.update(detail)
    with PROGRESS_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# step 3: transcript fetch (InnerTube ANDROID client, plain httpx)
# ---------------------------------------------------------------------------

def make_innertube_session() -> tuple[httpx.Client, str, str]:
    """Warm a persistent client against youtube.com to get a real API key + visitor
    data + cookies. Cookies from this GET are required - the player endpoint
    returns 400 Precondition-check-failed for a fresh anonymous request with no
    session cookies, even with a valid key/visitorData pair."""
    client = httpx.Client(headers={"User-Agent": WEB_UA}, follow_redirects=True, timeout=15)
    r = client.get("https://www.youtube.com/")
    r.raise_for_status()
    html = r.text
    api_key_m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    visitor_m = re.search(r'"VISITOR_DATA":"([^"]+)"', html)
    assert api_key_m and visitor_m, "could not extract INNERTUBE_API_KEY/VISITOR_DATA from youtube.com homepage"
    return client, api_key_m.group(1), visitor_m.group(1)


def fetch_player(client: httpx.Client, api_key: str, visitor: str, video_id: str) -> dict:
    url = f"https://www.youtube.com/youtubei/v1/player?key={api_key}&prettyPrint=false"
    ctx = {
        "client": {
            "clientName": "ANDROID",
            "clientVersion": ANDROID_CLIENT_VERSION,
            "androidSdkVersion": 34,
            "hl": "en",
            "gl": "US",
            "visitorData": visitor,
        }
    }
    body = {
        "context": ctx,
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ANDROID_UA,
        "X-Goog-Visitor-Id": visitor,
        "X-YouTube-Client-Name": "3",
        "X-YouTube-Client-Version": ANDROID_CLIENT_VERSION,
        "Origin": "https://www.youtube.com",
    }
    r = client.post(url, json=body, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def pick_caption_track(tracks: list[dict], preferred_lang: str) -> dict | None:
    prefix = (preferred_lang or "en").split("-")[0].lower()
    manual = [t for t in tracks if t.get("kind") != "asr"]
    asr = [t for t in tracks if t.get("kind") == "asr"]
    by_lang = lambda arr: next((t for t in arr if (t.get("languageCode") or "").lower().startswith(prefix)), None)
    return by_lang(manual) or by_lang(asr) or (manual[0] if manual else None) or (asr[0] if asr else None)


_TAG_RE = re.compile(r"<[^>]+>")


def _decode_entities(s: str) -> str:
    return (
        s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )


def parse_caption_body(text: str) -> list[dict]:
    """Parse json3 | srv3 (<p t d>) | srv1 (<text start dur>) into raw segments."""
    t = text.strip()
    raw = []
    if t.startswith("{") or t.startswith(")]}"):
        data = json.loads(re.sub(r"^\)\]\}'?\s*", "", t))
        for ev in data.get("events", []):
            segs = ev.get("segs")
            if not segs:
                continue
            line = " ".join("".join(s.get("utf8", "") for s in segs).split())
            if not line:
                continue
            raw.append({"start": (ev.get("tStartMs") or 0) / 1000, "duration": (ev.get("dDurationMs") or 0) / 1000, "text": line})
    elif re.search(r"<p\b[^>]*\bt=", t):
        for m in re.finditer(r'<p\b[^>]*?\bt="([^"]+)"(?:[^>]*?\bd="([^"]+)")?[^>]*>([\s\S]*?)</p>', t):
            line = " ".join(_decode_entities(_TAG_RE.sub("", m.group(3))).split())
            if not line:
                continue
            raw.append({"start": (float(m.group(1) or 0)) / 1000, "duration": (float(m.group(2) or 0)) / 1000, "text": line})
    else:
        for m in re.finditer(r'<text[^>]*\bstart="([^"]+)"[^>]*?(?:\bdur="([^"]+)")?[^>]*>([\s\S]*?)</text>', t):
            line = " ".join(_decode_entities(_TAG_RE.sub("", m.group(3))).split())
            if not line:
                continue
            raw.append({"start": float(m.group(1) or 0), "duration": float(m.group(2) or 0), "text": line})
    return raw


def dedup_rolling_window(raw: list[dict]) -> list[dict]:
    """Same rolling-window dedup as src/transcript_fetcher.py:_parse_vtt, ported
    to operate on already-parsed {start, duration, text} segments."""
    merged = []
    for seg in raw:
        if merged and abs(merged[-1]["start"] - seg["start"]) < 0.01:
            prev, cur = merged[-1]["text"], seg["text"]
            if cur.startswith(prev) or prev.startswith(cur):
                if len(cur) >= len(prev):
                    merged[-1] = seg
                continue
        merged.append(seg)

    segments = []
    for seg in merged:
        if segments:
            prev, cur = segments[-1]["text"], seg["text"]
            if cur == prev or cur in prev:
                continue
            if prev and cur.startswith(prev):
                segments[-1] = seg
                continue
        segments.append(seg)
    return segments


def fetch_transcript(client: httpx.Client, api_key: str, visitor: str, video_id: str, preferred_lang: str) -> dict:
    """Returns {ok, reason, segments, wpm, coverage} - segments in {start,duration,text} shape."""
    player = fetch_player(client, api_key, visitor, video_id)
    play_status = player.get("playabilityStatus", {}).get("status")
    if play_status != "OK":
        return {"ok": False, "reason": f"player_not_ok:{play_status}"}

    tracks = player.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    if not tracks:
        return {"ok": False, "reason": "no_captions"}

    chosen = pick_caption_track(tracks, preferred_lang)
    if not chosen:
        return {"ok": False, "reason": "no_captions"}

    base = chosen["baseUrl"]
    url = base + ("&" if "?" in base else "?") + "fmt=json3"
    r = client.get(url, timeout=15)
    if r.status_code != 200:
        return {"ok": False, "reason": f"caption_http_{r.status_code}"}

    raw = parse_caption_body(r.text)
    if not raw:
        return {"ok": False, "reason": "empty_after_parse"}

    segments = dedup_rolling_window(raw)
    if not segments:
        return {"ok": False, "reason": "empty_after_dedup"}

    duration_s = int(player.get("videoDetails", {}).get("lengthSeconds") or 0)
    words = sum(len(s["text"].split()) for s in segments)
    first_start = segments[0]["start"]
    last_end = max(s["start"] + s.get("duration", 0) for s in segments)
    span_s = max(0.0, last_end - first_start)
    wpm = round(words / (duration_s / 60), 1) if duration_s else None
    coverage = round(span_s / duration_s, 3) if duration_s else None

    return {
        "ok": True,
        "segments": segments,
        "language": chosen.get("languageCode"),
        "words": words,
        "wpm": wpm,
        "coverage": coverage,
        "player_duration_s": duration_s,
    }


# ---------------------------------------------------------------------------
# step 4: POST to score-video
# ---------------------------------------------------------------------------

def build_payload(video: dict, transcript: dict) -> dict:
    return {
        "video_id": video["video_id"],
        "segments": [{"t": round(s["start"], 2), "text": s["text"]} for s in transcript["segments"]],
        "duration_s": video["duration_s"],
        "meta": {
            "category": video["category"],
            "is_live": video["is_live"],
            "channel_id": video["channel_id"],
        },
        "source": "crawler",
    }


def post_score_video(client: httpx.Client, supabase_url: str, auth_token: str, anon_key: str, payload: dict) -> dict:
    r = client.post(
        f"{supabase_url}/functions/v1/score-video",
        json=payload,
        headers={"apikey": anon_key, "Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"},
        timeout=30,
    )
    return {"status": r.status_code, "body": _safe_json(r)}


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return r.text[:500]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", default="IN,US", help="comma-separated region codes")
    parser.add_argument("--per-region", type=int, default=200)
    parser.add_argument("--sleep", type=float, default=2.0, help="seconds between per-video transcript fetches")
    parser.add_argument("--limit", type=int, default=None, help="cap total videos processed (smoke tests)")
    parser.add_argument("--execute", action="store_true", help="actually POST to score-video (also needs CRAWLER_ALLOW_EXECUTE=1)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="default; does everything except the POST")
    args = parser.parse_args()

    env = load_env()
    dry_run = not (args.execute and env.get("CRAWLER_ALLOW_EXECUTE") == "1")
    if args.execute and dry_run:
        print(
            "[safety] --execute given but CRAWLER_ALLOW_EXECUTE=1 is not set in the environment - "
            "staying in DRY RUN. Set CRAWLER_ALLOW_EXECUTE=1 to actually POST.",
        )

    youtube_api_key = env.get("YOUTUBE_API_KEY")
    supabase_url = env.get("SUPABASE_URL")
    anon_key = env.get("SUPABASE_ANON_KEY")
    service_role_key = env.get("SUPABASE_SERVICE_ROLE_KEY")
    # Edge Fn recognizes the crawler via CRAWLER_SECRET (see http.ts). Fall back to
    # the legacy service key only for local/mock stacks.
    crawler_auth = env.get("CRAWLER_SECRET") or service_role_key
    assert youtube_api_key, "YOUTUBE_API_KEY missing from .env"
    assert supabase_url and anon_key, "SUPABASE_URL / SUPABASE_ANON_KEY missing from .env"
    if not dry_run:
        assert service_role_key, "SUPABASE_SERVICE_ROLE_KEY missing from .env (required for --execute)"

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]

    api_client = httpx.Client()
    print(f"[discover] regions={regions} per_region={args.per_region}")
    videos = []
    for region in regions:
        region_videos = fetch_trending(api_client, youtube_api_key, region, args.per_region)
        print(f"[discover] {region}: {len(region_videos)} videos from chart=mostPopular")
        videos.extend(region_videos)

    # de-dupe across regions, keep first occurrence
    seen_ids = set()
    deduped = []
    for v in videos:
        if v["video_id"] in seen_ids:
            continue
        seen_ids.add(v["video_id"])
        deduped.append(v)
    videos = deduped
    print(f"[discover] {len(videos)} unique videos across all regions")

    already_scored = fetch_existing_ids(api_client, supabase_url, anon_key, [v["video_id"] for v in videos])
    already_logged = load_progress_log()
    print(f"[skip] {len(already_scored)} already in video_scores, {len(already_logged)} already in local progress log")

    candidates = [v for v in videos if v["video_id"] not in already_scored and v["video_id"] not in already_logged]
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"[process] {len(candidates)} candidate videos this run (mode={'DRY RUN' if dry_run else 'EXECUTE'})")

    innertube_client, api_key, visitor = make_innertube_session()

    counts = {"posted": 0, "would_post": 0, "skipped": 0, "error": 0}
    for i, video in enumerate(candidates, 1):
        vid = video["video_id"]
        print(f"\n[{i}/{len(candidates)}] {vid} region={video['region']} cat={video['category']} "
              f"dur={video['duration_s']}s live={video['is_live']} title={video['title'][:60]!r}")

        if video["category"] == "Music":
            print("  skip: music")
            append_progress(vid, "skip", {"reason": "music"})
            counts["skipped"] += 1
            continue
        if video["is_live"]:
            print("  skip: live")
            append_progress(vid, "skip", {"reason": "live"})
            counts["skipped"] += 1
            continue
        if video["duration_s"] > MAX_DURATION_S:
            print("  skip: too_long")
            append_progress(vid, "skip", {"reason": "too_long"})
            counts["skipped"] += 1
            continue

        try:
            transcript = fetch_transcript(innertube_client, api_key, visitor, vid, video["default_audio_language"])
        except Exception as e:
            print(f"  error fetching transcript: {e}")
            append_progress(vid, "error", {"detail": str(e)})
            counts["error"] += 1
            time.sleep(args.sleep)
            continue

        if not transcript["ok"]:
            print(f"  skip: {transcript['reason']}")
            append_progress(vid, "skip", {"reason": transcript["reason"]})
            counts["skipped"] += 1
            time.sleep(args.sleep)
            continue

        payload = build_payload(video, transcript)
        print(
            f"  transcript ok: lang={transcript['language']} segments={len(transcript['segments'])} "
            f"words={transcript['words']} wpm={transcript['wpm']} coverage={transcript['coverage']}"
        )
        preview = dict(payload)
        preview["segments"] = f"<{len(payload['segments'])} segments omitted, first: {payload['segments'][0] if payload['segments'] else None}>"
        print(f"  would POST: {json.dumps(preview, ensure_ascii=False, indent=2)}")

        if dry_run:
            append_progress(vid, "would_post", {"segments": len(transcript["segments"]), "wpm": transcript["wpm"], "coverage": transcript["coverage"]})
            counts["would_post"] += 1
        else:
            result = post_score_video(innertube_client, supabase_url, crawler_auth, anon_key, payload)
            print(f"  POST -> {result['status']}: {result['body']}")
            append_progress(vid, "posted", {"status": result["status"]})
            counts["posted"] += 1

        time.sleep(args.sleep)

    print(f"\n[done] {counts}")


if __name__ == "__main__":
    main()
