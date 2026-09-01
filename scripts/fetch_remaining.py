"""
Fetch all remaining transcripts:
1. Re-fetch @mlb 15 videos
2. Backfill 31 channels with <15 transcripts from pool cache
Uses Tor for timedtext, list() with 3s delay.
"""

import json, os, sys, time, random, string, html as html_mod, math, sqlite3, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from youtube_transcript_api import YouTubeTranscriptApi
import requests

FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
AUDIT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'audit', 'channel_audit.json')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'anticlickbait.db')
POOL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'cache', 'youtube_api', 'channel_videos_pool')
TOR_PROXY = "socks5h://{user}:{pwd}@127.0.0.1:9050"

REPLACED_HANDLES = {'@autotopnl','@dropout','@rivermonsters','@technogamerzofficial',
    '@ANInewsIndia','@petcollective','@mrgearofficial','@nba','@beinsports',
    '@fcbarcelona','@realmadrid','@wildfilmsindia'}

REPL_CIDS = {
    '@1320video':'UC0PXqiud6dbwOAk8RvslgpQ','@keyandpeele':'UCdN4aXTrHAtfgbVG9HjBmxQ',
    '@cryingman':'UCGShu88Lh2ZAtXX0qbV9fXA','@coryxkenshin':'UCiYcA0gJzg855iSKMrX3oHg',
    '@vicenews':'UCZaT_X_mc0BI-djXOlfhqWQ','@rightchoiceshearing':'UC2YvLBsduOMcStpJbZav-mw',
    '@hydraulicpresschannel':'UCcMDMoNu66_1Hwi5-MeiQgw','@mlb':'UCoLrcjPV5PbUrUyXq5mjc_A',
    '@espn':'UCiWLfSweyRNmLpgEHekhoAg','@cricketcomau':'UCkBY0aHJP9BwjZLDYxAQrKg',
    '@formula1':'UCB_qr75-ydFVKSF9Dmo6izg','@greatwhitesharkking':'UCfBZ5ps6L-bov4nJL0mPVHg',
}

save_lock = threading.Lock()


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
    return (segments, "ok") if segments else (None, "empty_segments")


def get_pool(cid):
    for suffix in ['__pool__min0.json', '__pool.json']:
        path = os.path.join(POOL_DIR, f'{cid}{suffix}')
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f).get('data', [])
    return []


def fetch_videos_for_channel(handle, cid, needed, exclude_vids, yt_api, fetched):
    """Fetch `needed` videos for a channel using 50/50 latest/popular from pool."""
    pool = get_pool(cid)
    if not pool:
        print(f"  {handle}: NO POOL", flush=True)
        return 0

    available = [v for v in pool if v['video_id'] not in exclude_vids]
    if not available:
        print(f"  {handle}: no available videos in pool", flush=True)
        return 0

    # 50/50 interleave
    by_date = sorted(available, key=lambda x: x.get('published_at', '') or '', reverse=True)
    by_views = sorted(available, key=lambda x: x.get('view_count', 0) or 0, reverse=True)
    ordered = []
    seen = set()
    di, vi = 0, 0
    pick_latest = True
    while len(ordered) < len(available):
        if pick_latest:
            while di < len(by_date) and by_date[di]['video_id'] in seen: di += 1
            if di < len(by_date):
                ordered.append(by_date[di]); seen.add(by_date[di]['video_id']); di += 1
        else:
            while vi < len(by_views) and by_views[vi]['video_id'] in seen: vi += 1
            if vi < len(by_views):
                ordered.append(by_views[vi]); seen.add(by_views[vi]['video_id']); vi += 1
        pick_latest = not pick_latest
        if di >= len(by_date) and vi >= len(by_views): break

    filled = 0
    consec_fail = 0
    for v in ordered:
        if filled >= needed: break
        if consec_fail >= 15:
            print(f"  {handle}: 15 consecutive failures, stopping", flush=True)
            break
        vid = v['video_id']
        try:
            tlist = yt_api.list(vid)
            en_track = None
            for t in tlist:
                if t.language_code == 'en' or t.language_code.startswith('en-'):
                    en_track = t; break
            if not en_track or not en_track._url:
                consec_fail += 1
                time.sleep(3.0); continue

            segments, status = fetch_timedtext_via_tor(en_track._url)
            if status == "ok" and segments:
                with save_lock:
                    fetched[vid] = {
                        'video_id': vid, 'channel_id': cid,
                        'title': v.get('title', ''),
                        'duration_seconds': v.get('duration_seconds'),
                        'view_count': v.get('view_count'),
                        'published_at': v.get('published_at'),
                        'thumbnail_url': v.get('thumbnail_url'),
                        'segments': segments,
                    }
                filled += 1
                consec_fail = 0
                print(f"  {handle}: OK {vid} [{filled}/{needed}]", flush=True)
            else:
                consec_fail += 1
        except Exception as e:
            consec_fail += 1
            if 'IpBlocked' in str(e):
                print(f"  {handle}: IP BLOCKED!", flush=True)
                return filled
        time.sleep(3.0)

    return filled


def main():
    with open(FETCHED_PATH) as f:
        fetched = json.load(f)
    with open(AUDIT_PATH) as f:
        audit = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    evaluated = set(r[0] for r in conn.execute(
        'SELECT video_id FROM video_evaluations WHERE evaluation_success = 1'
    ).fetchall())
    conn.close()

    yt_api = YouTubeTranscriptApi()

    # === 1. RE-FETCH @MLB ===
    mlb_cid = 'UCoLrcjPV5PbUrUyXq5mjc_A'
    mlb_have = sum(1 for v in fetched.values() if v.get('channel_id') == mlb_cid)
    if mlb_have < 15:
        print(f"=== MLB: need {15 - mlb_have} videos ===", flush=True)
        exclude = set(fetched.keys()) | evaluated
        filled = fetch_videos_for_channel('@mlb', mlb_cid, 15 - mlb_have, exclude, yt_api, fetched)
        with open(FETCHED_PATH, 'w') as f:
            json.dump(fetched, f)
        print(f"  MLB done: {filled} fetched, total={sum(1 for v in fetched.values() if v.get('channel_id')==mlb_cid)}", flush=True)

    # === 2. BACKFILL 31 CHANNELS ===
    print(f"\n=== VIDEO BACKFILL ===", flush=True)

    # Find channels below 15
    backfill_list = []
    for ch in audit:
        if ch['handle'] in REPLACED_HANDLES: continue
        cid = ch['channel_id']
        canonical_vids = set(v['video_id'] for v in ch['videos'])
        have = sum(1 for vid in canonical_vids if vid in evaluated or vid in fetched)
        if have < 15:
            backfill_list.append((ch['handle'], cid, 15 - have, canonical_vids))

    print(f"Channels needing backfill: {len(backfill_list)}, total videos needed: {sum(x[2] for x in backfill_list)}", flush=True)

    total_filled = 0
    for handle, cid, needed, canonical_vids in sorted(backfill_list, key=lambda x: -x[2]):
        # Exclude canonical vids + already fetched + evaluated
        exclude = canonical_vids | set(fetched.keys()) | evaluated
        filled = fetch_videos_for_channel(handle, cid, needed, exclude, yt_api, fetched)
        total_filled += filled

        # Save after each channel
        with open(FETCHED_PATH, 'w') as f:
            json.dump(fetched, f)

        if filled < needed:
            print(f"  {handle}: WARNING only {filled}/{needed}", flush=True)

    # === SUMMARY ===
    print(f"\n=== SUMMARY ===", flush=True)
    print(f"Total backfilled: {total_filled}", flush=True)
    print(f"Total fetched: {len(fetched)}", flush=True)

    # Recount coverage
    all_good = 0
    still_short = []
    for ch in audit:
        if ch['handle'] in REPLACED_HANDLES: continue
        cid = ch['channel_id']
        canonical_have = sum(1 for v in ch['videos'] if v['video_id'] in evaluated or v['video_id'] in fetched)
        backfill_have = sum(1 for v in fetched.values() if v.get('channel_id') == cid and v['video_id'] not in set(vv['video_id'] for vv in ch['videos']))
        total = canonical_have + backfill_have
        if total >= 15:
            all_good += 1
        else:
            still_short.append((ch['handle'], total))

    # Check replacement channels
    for handle, cid in REPL_CIDS.items():
        n = sum(1 for v in fetched.values() if v.get('channel_id') == cid)
        if n < 15:
            still_short.append((handle, n))
        else:
            all_good += 1

    print(f"Channels at 15: {all_good}/150", flush=True)
    if still_short:
        print(f"Still short:", flush=True)
        for h, n in sorted(still_short, key=lambda x: x[1]):
            print(f"  {h}: {n}/15", flush=True)


if __name__ == '__main__':
    main()
