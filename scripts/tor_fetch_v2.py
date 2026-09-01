"""
Tor fetch v2: hybrid approach with rate-limited list() + Tor timedtext fetch.
- list() calls happen sequentially on local IP with 2s delay (avoid flagging)
- timedtext fetches go through Tor with unique circuit per request
- Saves to data/fetched_transcripts.json incrementally
"""

import json, os, sys, time, random, string, html as html_mod, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from youtube_transcript_api import YouTubeTranscriptApi
import requests

FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
REMAINING_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'remaining_to_fetch.json')
TOR_PROXY = "socks5h://{user}:{pwd}@127.0.0.1:9050"

TOR_WORKERS = 5       # parallel Tor fetches
SAVE_EVERY = 10
LIST_DELAY = 2.0      # seconds between list() calls (protect local IP)


def random_cred():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))


def fetch_timedtext_via_tor(url):
    """Fetch a signed timedtext URL through Tor with a unique circuit."""
    cred = random_cred()
    proxies = {
        'http': TOR_PROXY.format(user=cred, pwd=cred),
        'https': TOR_PROXY.format(user=cred, pwd=cred),
    }
    resp = requests.get(url, proxies=proxies, timeout=30)
    if resp.status_code == 429:
        return None, "tor_429"
    if resp.status_code != 200:
        return None, f"tor_http_{resp.status_code}"

    root = ET.fromstring(resp.text)
    segments = []
    for text_elem in root.findall('.//text'):
        start = float(text_elem.get('start', 0))
        dur = float(text_elem.get('dur', 0))
        content = text_elem.text or ''
        content = html_mod.unescape(content).strip()
        if content:
            segments.append({'start': start, 'text': content, 'duration': dur})

    if not segments:
        return None, "empty_segments"
    return segments, "ok"


def main():
    with open(REMAINING_PATH) as f:
        remaining = json.load(f)

    if os.path.exists(FETCHED_PATH):
        with open(FETCHED_PATH) as f:
            fetched = json.load(f)
    else:
        fetched = {}

    todo = [v for v in remaining if v['video_id'] not in fetched]
    print(f"Todo: {len(todo)} | Already fetched: {len(fetched)}", flush=True)

    if not todo:
        print("Nothing to do.")
        return

    yt_api = YouTubeTranscriptApi()
    success = 0
    failed = 0
    blocked = 0
    new_fetched = 0
    save_lock = threading.Lock()

    # Phase 1: Sequential list() to get signed URLs (rate-limited on local IP)
    # Phase 2: Parallel Tor fetch of timedtext URLs
    # We pipeline these: list a batch, submit to Tor pool, list next batch

    BATCH = 20  # list this many, then let Tor workers catch up

    for batch_start in range(0, len(todo), BATCH):
        batch = todo[batch_start:batch_start + BATCH]
        print(f"\n--- Batch {batch_start // BATCH + 1}: listing {len(batch)} videos ---", flush=True)

        # Get signed URLs via list()
        url_map = {}  # vid -> (url, video_info)
        for v in batch:
            vid = v['video_id']
            try:
                tlist = yt_api.list(vid)
                en_track = None
                for t in tlist:
                    if t.language_code == 'en' or t.language_code.startswith('en-'):
                        en_track = t
                        break
                if en_track and en_track._url:
                    url_map[vid] = (en_track._url, v)
                else:
                    failed += 1
                    print(f"  SKIP {vid}: no english track in list()", flush=True)
            except Exception as e:
                err = str(e)[:80]
                failed += 1
                if 'IpBlocked' in err or 'blocked' in err.lower():
                    print(f"  LOCAL IP BLOCKED on list()! Stopping list phase.", flush=True)
                    break
                print(f"  LIST_ERR {vid}: {err}", flush=True)
            time.sleep(LIST_DELAY)

        if not url_map:
            continue

        # Tor-fetch the signed URLs in parallel
        print(f"  Fetching {len(url_map)} via Tor...", flush=True)

        def tor_worker(item):
            vid, (url, v) = item
            time.sleep(random.uniform(0.5, 2.0))
            return vid, v, fetch_timedtext_via_tor(url)

        with ThreadPoolExecutor(max_workers=TOR_WORKERS) as executor:
            futures = {executor.submit(tor_worker, item): item for item in url_map.items()}
            for future in as_completed(futures):
                try:
                    vid, v, (segments, status) = future.result()
                    if status == "ok" and segments:
                        with save_lock:
                            fetched[vid] = {
                                'video_id': vid,
                                'channel_id': v['channel_id'],
                                'title': v['title'],
                                'duration_seconds': v.get('duration_seconds'),
                                'view_count': v.get('view_count'),
                                'published_at': v.get('published_at'),
                                'thumbnail_url': v.get('thumbnail_url'),
                                'segments': segments,
                            }
                            success += 1
                            new_fetched += 1
                        print(f"    OK {vid} ({len(segments)} segs) [{success}/{success + failed}]", flush=True)
                    elif status == "tor_429":
                        blocked += 1
                        failed += 1
                        print(f"    429 {vid}", flush=True)
                    else:
                        failed += 1
                        print(f"    FAIL {vid}: {status}", flush=True)

                    if new_fetched > 0 and new_fetched % SAVE_EVERY == 0:
                        with save_lock:
                            with open(FETCHED_PATH, 'w') as f:
                                json.dump(fetched, f)
                        print(f"  [saved] total={len(fetched)} new={new_fetched} blocked={blocked}", flush=True)

                except Exception as e:
                    failed += 1
                    print(f"    ERROR: {str(e)[:100]}", flush=True)

    # Final save
    with open(FETCHED_PATH, 'w') as f:
        json.dump(fetched, f)
    print(f"\nDONE. New={new_fetched} Failed={failed} Blocked={blocked} Total_fetched={len(fetched)}", flush=True)


if __name__ == '__main__':
    main()
