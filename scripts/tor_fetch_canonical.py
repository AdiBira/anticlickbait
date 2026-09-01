"""
Tor fetch canonical missing videos from data/canonical_missing.json.
Saves to data/fetched_transcripts.json incrementally.
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
MISSING_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'canonical_missing.json')
TOR_PROXY = "socks5h://{user}:{pwd}@127.0.0.1:9050"

TOR_WORKERS = 5
SAVE_EVERY = 10
LIST_DELAY = 2.0
BATCH = 20


def random_cred():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))


def fetch_timedtext_via_tor(url):
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
        content = html_mod.unescape(text_elem.text or '').strip()
        if content:
            segments.append({'start': start, 'text': content, 'duration': dur})

    if not segments:
        return None, "empty_segments"
    return segments, "ok"


def main():
    with open(MISSING_PATH) as f:
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

    for batch_start in range(0, len(todo), BATCH):
        batch = todo[batch_start:batch_start + BATCH]
        print(f"\n--- Batch {batch_start // BATCH + 1}: listing {len(batch)} videos ---", flush=True)

        url_map = {}
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
                    print(f"  SKIP {vid}: no english track", flush=True)
            except Exception as e:
                err = str(e)[:80]
                failed += 1
                if 'IpBlocked' in err:
                    print(f"  LOCAL IP BLOCKED! Stopping.", flush=True)
                    break
                print(f"  LIST_ERR {vid}: {err}", flush=True)
            time.sleep(LIST_DELAY)

        if not url_map:
            continue

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
