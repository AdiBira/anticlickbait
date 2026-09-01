"""
Video backfill: for channels with <8 transcript failures, pick replacement
videos from pool cache using 50/50 latest/popular criteria.
Checks English transcript availability via list(), fetches via Tor.
Outputs updated canonical_missing list and saves transcripts.
"""

import json, os, sys, time, random, string, html as html_mod, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from xml.etree import ElementTree as ET
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from youtube_transcript_api import YouTubeTranscriptApi
import requests

AUDIT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'audit', 'channel_audit.json')
FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
HANDLE_CID_PATH = '/tmp/handle_to_cid.json'
POOL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'cache', 'youtube_api', 'channel_videos_pool')
TOR_PROXY = "socks5h://{user}:{pwd}@127.0.0.1:9050"

LIST_DELAY = 1.5

BACKFILL_CHANNELS = {
    '@quantumtech': 7, '@linustechtips': 7,
    '@motomadness': 5, '@a4a4a4a4': 5,
    '@woodyandkleiny': 4, '@alphageographi': 4, '@mistermainer': 4,
    '@fifa': 4, '@Matt.Larose': 4,
    '@themanniishow': 3, '@danrhodes': 3, '@jacksepticeye': 3,
    '@cnn': 3, '@skynewsaustralia': 3, '@thedodo': 3, '@wwe': 3,
    '@drpimplepopper': 2, '@cnnnews18': 2, '@dontstopmeowing': 2,
    '@tech___zone': 1, '@pandaboi': 1, '@mavigadgets': 1,
    '@loganpaulvlogs': 1, '@bbcnews': 1, '@hammyandolivia': 1,
    '@businessinsider': 1, '@redbull': 1, '@nickpro': 1, '@ufc': 1,
    '@nasdaily': 1, '@insiderfood': 1,
}


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
    for t in root.findall('.//text'):
        s = float(t.get('start', 0))
        d = float(t.get('dur', 0))
        c = html_mod.unescape(t.text or '').strip()
        if c:
            segments.append({'start': s, 'text': c, 'duration': d})
    if not segments:
        return None, "empty_segments"
    return segments, "ok"


def select_50_50(videos, n):
    """Select n videos using 50/50 latest/popular split."""
    latest_n = math.ceil(n * 0.5)
    popular_n = n - latest_n

    by_date = sorted(videos, key=lambda x: x.get('published_at', '') or '', reverse=True)
    by_views = sorted(videos, key=lambda x: x.get('view_count', 0) or 0, reverse=True)

    selected = []
    selected_ids = set()

    # Latest first
    for v in by_date:
        if len(selected) >= latest_n:
            break
        if v['video_id'] not in selected_ids:
            selected.append(v)
            selected_ids.add(v['video_id'])

    # Then popular
    for v in by_views:
        if len(selected) >= n:
            break
        if v['video_id'] not in selected_ids:
            selected.append(v)
            selected_ids.add(v['video_id'])

    # Fill remainder from date order
    for v in by_date:
        if len(selected) >= n:
            break
        if v['video_id'] not in selected_ids:
            selected.append(v)
            selected_ids.add(v['video_id'])

    return selected


def main():
    with open(AUDIT_PATH) as f:
        audit = json.load(f)
    with open(HANDLE_CID_PATH) as f:
        handle_to_cid = json.load(f)

    if os.path.exists(FETCHED_PATH):
        with open(FETCHED_PATH) as f:
            fetched = json.load(f)
    else:
        fetched = {}

    # Get existing video IDs per channel from audit
    existing_by_handle = {}
    channel_info = {}
    for ch in audit:
        existing_by_handle[ch['handle']] = set(v['video_id'] for v in ch['videos'])
        channel_info[ch['handle']] = ch

    yt_api = YouTubeTranscriptApi()
    total_backfilled = 0
    total_skipped = 0
    backfill_results = []  # new videos to add to audit

    for handle, needed in sorted(BACKFILL_CHANNELS.items(), key=lambda x: -x[1]):
        cid = handle_to_cid.get(handle)
        if not cid:
            print(f"\n{handle}: NO CID MAPPING, skipping", flush=True)
            continue

        pool_file = os.path.join(POOL_DIR, f'{cid}__pool__min0.json')
        if not os.path.exists(pool_file):
            pool_file = os.path.join(POOL_DIR, f'{cid}__pool.json')
        if not os.path.exists(pool_file):
            print(f"\n{handle}: NO POOL, skipping", flush=True)
            continue

        with open(pool_file) as f:
            pool_data = json.load(f)

        pool_videos = pool_data.get('data', [])
        existing = existing_by_handle.get(handle, set())
        # Also exclude already-fetched
        available = [v for v in pool_videos if v['video_id'] not in existing and v['video_id'] not in fetched]

        # Apply 50/50 selection on a larger pool, then check transcripts
        # Get 3x candidates to have enough after transcript filtering
        candidates = select_50_50(available, needed * 3)

        print(f"\n{handle}: need {needed}, pool={len(pool_videos)}, candidates={len(candidates)}", flush=True)

        filled = 0
        for v in candidates:
            if filled >= needed:
                break

            vid = v['video_id']
            try:
                tlist = yt_api.list(vid)
                en_track = None
                for t in tlist:
                    if t.language_code == 'en' or t.language_code.startswith('en-'):
                        en_track = t
                        break

                if not en_track or not en_track._url:
                    total_skipped += 1
                    continue

                # Fetch via Tor
                segments, status = fetch_timedtext_via_tor(en_track._url)
                if status == "ok" and segments:
                    fetched[vid] = {
                        'video_id': vid,
                        'channel_id': cid,
                        'title': v.get('title', ''),
                        'duration_seconds': v.get('duration_seconds'),
                        'view_count': v.get('view_count'),
                        'published_at': v.get('published_at'),
                        'thumbnail_url': v.get('thumbnail_url'),
                        'segments': segments,
                    }
                    filled += 1
                    total_backfilled += 1
                    backfill_results.append({
                        'handle': handle,
                        'video_id': vid,
                        'title': v.get('title', ''),
                        'view_count': v.get('view_count'),
                        'published_at': v.get('published_at'),
                        'duration_seconds': v.get('duration_seconds'),
                        'thumbnail_url': v.get('thumbnail_url'),
                    })
                    print(f"  OK {vid} ({len(segments)} segs) [{filled}/{needed}]", flush=True)
                else:
                    total_skipped += 1

            except Exception as e:
                err = str(e)[:60]
                total_skipped += 1
                if 'IpBlocked' in err:
                    print(f"  LOCAL IP BLOCKED!", flush=True)
                    break

            time.sleep(LIST_DELAY)

        if filled < needed:
            print(f"  WARNING: only filled {filled}/{needed}", flush=True)

        # Save incrementally
        with open(FETCHED_PATH, 'w') as f:
            json.dump(fetched, f)

    # Save backfill results
    results_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'backfill_results.json')
    with open(results_path, 'w') as f:
        json.dump(backfill_results, f, indent=2)

    print(f"\n=== DONE ===", flush=True)
    print(f"Backfilled: {total_backfilled}", flush=True)
    print(f"Skipped (no transcript): {total_skipped}", flush=True)
    print(f"Results saved to data/backfill_results.json", flush=True)
    print(f"Fetched transcripts: {len(fetched)}", flush=True)


if __name__ == '__main__':
    main()
