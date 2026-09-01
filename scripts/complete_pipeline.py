"""
Complete pipeline: fetch all missing transcripts + eval all pending.
Phase 1: Tor-fetch transcripts for 12 replacement channels (15 each)
Phase 2: Tor-fetch transcripts for 83 missing canonical videos
Phase 3: Eval all unevaluated at 3 RPM (sequential, ~22s between LLM calls)
"""

import json, os, sys, time, random, string, html as html_mod, math, sqlite3, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
import requests
import text_evaluator
from config import OPENAI_API_KEY, OPENAI_MODEL

FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
AUDIT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'audit', 'channel_audit.json')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'anticlickbait.db')
TOR_PROXY = "socks5h://{user}:{pwd}@127.0.0.1:9050"

# 12 replacement channels: old_handle -> (new_handle, new_cid)
REPLACEMENTS = {
    '@autotopnl': ('@1320video', 'UC0PXqiud6dbwOAk8RvslgpQ'),
    '@dropout': ('@keyandpeele', 'UCdN4aXTrHAtfgbVG9HjBmxQ'),
    '@rivermonsters': ('@cryingman', 'UCGShu88Lh2ZAtXX0qbV9fXA'),
    '@technogamerzofficial': ('@coryxkenshin', 'UCiYcA0gJzg855iSKMrX3oHg'),
    '@ANInewsIndia': ('@vicenews', 'UCZaT_X_mc0BI-djXOlfhqWQ'),
    '@petcollective': ('@rightchoiceshearing', 'UC2YvLBsduOMcStpJbZav-mw'),
    '@mrgearofficial': ('@hydraulicpresschannel', 'UCcMDMoNu66_1Hwi5-MeiQgw'),
    '@nba': ('@onechampionship', 'UCiormkBf3jm6mfb7k0yPbKA'),
    '@beinsports': ('@espn', 'UCiWLfSweyRNmLpgEHekhoAg'),
    '@fcbarcelona': ('@cricketcomau', 'UCkBY0aHJP9BwjZLDYxAQrKg'),
    '@realmadrid': ('@formula1', 'UCB_qr75-ydFVKSF9Dmo6izg'),
    '@wildfilmsindia': ('@greatwhitesharkking', 'UCfBZ5ps6L-bov4nJL0mPVHg'),
}

REPLACED_HANDLES = set(REPLACEMENTS.keys())

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


def get_pool_videos(cid):
    pool_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'cache', 'youtube_api', 'channel_videos_pool')
    for suffix in ['__pool__min0.json', '__pool.json']:
        path = os.path.join(pool_dir, f'{cid}{suffix}')
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f).get('data', [])
    return []


def select_15_with_transcripts(videos, yt_api):
    """Select 15 videos using 50/50 latest/popular, skipping those without English transcripts."""
    by_date = sorted(videos, key=lambda x: x.get('published_at', '') or '', reverse=True)
    by_views = sorted(videos, key=lambda x: x.get('view_count', 0) or 0, reverse=True)

    # Interleave: pick from latest, then popular, alternating
    ordered = []
    seen = set()
    di, vi = 0, 0
    pick_latest = True
    while len(ordered) < len(videos):
        if pick_latest:
            while di < len(by_date) and by_date[di]['video_id'] in seen:
                di += 1
            if di < len(by_date):
                ordered.append(by_date[di])
                seen.add(by_date[di]['video_id'])
                di += 1
        else:
            while vi < len(by_views) and by_views[vi]['video_id'] in seen:
                vi += 1
            if vi < len(by_views):
                ordered.append(by_views[vi])
                seen.add(by_views[vi]['video_id'])
                vi += 1
        pick_latest = not pick_latest
        if di >= len(by_date) and vi >= len(by_views):
            break

    selected = []
    consec_fail = 0
    for v in ordered:
        if len(selected) >= 15:
            break
        if consec_fail >= 10:
            print(f"    10 consecutive failures, stopping this channel", flush=True)
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
                print(f"    skip {vid}: no english track", flush=True)
                consec_fail += 1
                time.sleep(3.0)
                continue

            segments, status = fetch_timedtext_via_tor(en_track._url)
            if status == "ok" and segments:
                selected.append((v, segments))
                consec_fail = 0
                print(f"    OK {vid} ({len(segments)} segs) [{len(selected)}/15]", flush=True)
            else:
                consec_fail += 1
                print(f"    skip {vid}: {status}", flush=True)
        except Exception as e:
            consec_fail += 1
            print(f"    skip {vid}: {str(e)[:50]}", flush=True)
        time.sleep(3.0)

    return selected


def fetch_canonical_missing(missing_vids, fetched, yt_api):
    """Fetch transcripts for canonical videos that are missing from fetched_transcripts.json."""
    if not missing_vids:
        return

    print(f"\nFetching {len(missing_vids)} canonical missing videos...", flush=True)
    success = 0
    fail = 0
    BATCH = 20

    for batch_start in range(0, len(missing_vids), BATCH):
        batch = missing_vids[batch_start:batch_start + BATCH]
        print(f"\n  Batch {batch_start // BATCH + 1}: listing {len(batch)} videos", flush=True)

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
                    fail += 1
            except Exception as e:
                fail += 1
                if 'IpBlocked' in str(e):
                    print(f"  IP BLOCKED! Stopping.", flush=True)
                    return
            time.sleep(2.0)

        if not url_map:
            continue

        def tor_worker(item):
            vid, (url, v) = item
            time.sleep(random.uniform(0.5, 2.0))
            return vid, v, fetch_timedtext_via_tor(url)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(tor_worker, item): item for item in url_map.items()}
            for future in as_completed(futures):
                try:
                    vid, v, (segments, status) = future.result()
                    if status == "ok" and segments:
                        with save_lock:
                            fetched[vid] = {
                                'video_id': vid, 'channel_id': v['channel_id'],
                                'title': v['title'],
                                'duration_seconds': v.get('duration_seconds'),
                                'view_count': v.get('view_count'),
                                'published_at': v.get('published_at'),
                                'thumbnail_url': v.get('thumbnail_url'),
                                'segments': segments,
                            }
                            success += 1
                        print(f"    OK {vid} ({len(segments)} segs) [{success}/{success+fail}]", flush=True)
                    else:
                        fail += 1
                        print(f"    FAIL {vid}: {status}", flush=True)
                except Exception as e:
                    fail += 1

        # Save after each batch
        with save_lock:
            with open(FETCHED_PATH, 'w') as f:
                json.dump(fetched, f)

    print(f"  Canonical fetch done: {success} OK, {fail} fail", flush=True)


def eval_one(vid, v, client):
    """Eval one video with 2 sequential LLM calls."""
    segments = v['segments']
    transcript_lines = [f'[{s["start"]:.1f}s] {s["text"]}' for s in segments]
    transcript = '\n'.join(transcript_lines)
    if len(transcript) > 50000:
        transcript = transcript[:50000] + '\n[TRUNCATED]'
    duration = v.get('duration_seconds', 0) or 0

    prompt1 = text_evaluator.TITLE_ANALYSIS_PROMPT.format(title=v['title'], transcript=transcript)
    try:
        r1 = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{'role': 'user', 'content': prompt1}],
            temperature=0.1,
            response_format={'type': 'json_object'},
        )
        result1 = json.loads(r1.choices[0].message.content)
    except Exception as e:
        return None, f"call1: {str(e)[:60]}"

    time.sleep(22)

    prompt2 = text_evaluator.CONTENT_ANALYSIS_PROMPT.format(
        title=v['title'], transcript=transcript, duration_seconds=duration
    )
    try:
        r2 = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{'role': 'user', 'content': prompt2}],
            temperature=0.1,
            response_format={'type': 'json_object'},
        )
        result2 = json.loads(r2.choices[0].message.content)
    except Exception as e:
        return None, f"call2: {str(e)[:60]}"

    r = {
        'title_content_similarity_score': result1.get('title_content_similarity', -1),
        'deception_score': result1.get('deception_score', 0),
        'focus_ratio': result2.get('focus_ratio', -1),
        'time_to_main_content': result2.get('time_to_main_content', -1),
        'sponsor_interruption_score': result2.get('sponsor_interruption_score', 0),
        'title_analysis_reasoning': result1.get('title_content_similarity_reasoning', ''),
        'deception_reasoning': result1.get('deception_score_reasoning', ''),
        'focus_reasoning': result2.get('focus_ratio_reasoning', ''),
        'time_reasoning': result2.get('time_to_main_content_reasoning', ''),
        'sponsor_reasoning': result2.get('sponsor_interruption_score_reasoning', ''),
        'llm_explanation': result1.get('title_promises', ''),
    }
    score = text_evaluator.compute_video_score(r)
    return r, score


def save_eval(vid, v, r, score):
    transcript_text = '\n'.join(f'[{s["start"]:.1f}s] {s["text"]}' for s in v['segments'])
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""INSERT INTO video_evaluations (
        video_id, channel_id, title, thumbnail_url, duration_seconds, view_count, published_at,
        title_content_similarity_score, deception_score, focus_ratio, time_to_main_content,
        sponsor_interruption_score, video_score, title_analysis_reasoning, deception_reasoning,
        focus_reasoning, time_reasoning, sponsor_reasoning, llm_explanation, transcript_text,
        evaluation_success, evaluated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,datetime('now'))
    ON CONFLICT(video_id) DO UPDATE SET
        title_content_similarity_score=excluded.title_content_similarity_score,
        deception_score=excluded.deception_score, focus_ratio=excluded.focus_ratio,
        time_to_main_content=excluded.time_to_main_content,
        sponsor_interruption_score=excluded.sponsor_interruption_score,
        video_score=excluded.video_score, title_analysis_reasoning=excluded.title_analysis_reasoning,
        deception_reasoning=excluded.deception_reasoning, focus_reasoning=excluded.focus_reasoning,
        time_reasoning=excluded.time_reasoning, sponsor_reasoning=excluded.sponsor_reasoning,
        llm_explanation=excluded.llm_explanation, transcript_text=excluded.transcript_text,
        evaluation_success=1, evaluation_error=NULL, evaluated_at=datetime('now')
    """, (vid, v['channel_id'], v['title'], v.get('thumbnail_url'), v.get('duration_seconds'),
        v.get('view_count'), v.get('published_at'), r['title_content_similarity_score'],
        r['deception_score'], r['focus_ratio'], r['time_to_main_content'],
        r['sponsor_interruption_score'], score, r['title_analysis_reasoning'],
        r['deception_reasoning'], r['focus_reasoning'], r['time_reasoning'],
        r['sponsor_reasoning'], r['llm_explanation'], transcript_text))
    conn.commit()
    conn.close()


def main():
    with open(FETCHED_PATH) as f:
        fetched = json.load(f)
    with open(AUDIT_PATH) as f:
        audit = json.load(f)

    yt_api = YouTubeTranscriptApi()
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Build audit lookup
    audit_by_handle = {ch['handle']: ch for ch in audit}

    # ===== PHASE 1: FETCH REPLACEMENT CHANNEL VIDEOS =====
    print("=== PHASE 1: REPLACEMENT CHANNELS (12) ===", flush=True)

    new_audit_entries = []  # Will be appended to audit

    for old_handle, (new_handle, cid) in REPLACEMENTS.items():
        # Check if already fetched enough for this channel
        existing = [vid for vid, v in fetched.items() if v.get('channel_id') == cid]
        if len(existing) >= 15:
            print(f"\n{old_handle} -> {new_handle}: already have {len(existing)} videos, skipping fetch", flush=True)
            continue

        pool = get_pool_videos(cid)
        if not pool:
            print(f"\n{old_handle} -> {new_handle}: NO POOL, skipping", flush=True)
            continue

        available = [v for v in pool if v['video_id'] not in fetched]
        print(f"\n{old_handle} -> {new_handle} (cid={cid}, pool={len(pool)}, available={len(available)})", flush=True)

        needed = 15 - len(existing)
        selected = select_15_with_transcripts(available, yt_api)

        for v, segments in selected[:needed]:
            fetched[v['video_id']] = {
                'video_id': v['video_id'], 'channel_id': cid,
                'title': v.get('title', ''), 'duration_seconds': v.get('duration_seconds'),
                'view_count': v.get('view_count'), 'published_at': v.get('published_at'),
                'thumbnail_url': v.get('thumbnail_url'), 'segments': segments,
            }

        # Save after each channel
        with open(FETCHED_PATH, 'w') as f:
            json.dump(fetched, f)

        total_for_ch = sum(1 for v in fetched.values() if v.get('channel_id') == cid)
        print(f"  Saved. Channel total: {total_for_ch}, Global fetched: {len(fetched)}", flush=True)

    # ===== PHASE 2: FETCH MISSING CANONICAL TRANSCRIPTS =====
    print("\n=== PHASE 2: CANONICAL MISSING TRANSCRIPTS ===", flush=True)

    missing_canonical = []
    for ch in audit:
        if ch['handle'] in REPLACED_HANDLES:
            continue
        for v in ch['videos']:
            if v['video_id'] not in fetched:
                missing_canonical.append({
                    'video_id': v['video_id'],
                    'channel_id': ch['channel_id'],
                    'title': v.get('title', ''),
                    'duration_seconds': v.get('duration_seconds'),
                    'view_count': v.get('view_count'),
                    'published_at': v.get('published_at'),
                    'thumbnail_url': v.get('thumbnail_url'),
                })

    print(f"Missing canonical transcripts: {len(missing_canonical)}", flush=True)
    fetch_canonical_missing(missing_canonical, fetched, yt_api)

    # ===== PHASE 3: EVAL ALL PENDING =====
    print("\n=== PHASE 3: EVAL (3 RPM) ===", flush=True)

    conn = sqlite3.connect(DB_PATH)
    evaluated = set(r[0] for r in conn.execute(
        'SELECT video_id FROM video_evaluations WHERE evaluation_success = 1'
    ).fetchall())
    conn.close()

    # Build set of valid video IDs (canonical non-replaced + replacement channel videos)
    valid_cids = set()
    for ch in audit:
        if ch['handle'] not in REPLACED_HANDLES:
            valid_cids.add(ch['channel_id'])
    for _, (_, cid) in REPLACEMENTS.items():
        valid_cids.add(cid)

    to_eval = [(vid, v) for vid, v in fetched.items()
               if vid not in evaluated and v.get('segments') and v.get('channel_id') in valid_cids]
    print(f"To eval: {len(to_eval)}", flush=True)

    ok = 0
    fail = 0
    for i, (vid, v) in enumerate(to_eval):
        r, detail = eval_one(vid, v, client)
        if r and r.get('title_content_similarity_score', -1) >= 0:
            save_eval(vid, v, r, detail)
            ok += 1
            print(f"OK {vid} score={detail:.1f} [{ok}/{ok+fail}] ({i+1}/{len(to_eval)})", flush=True)
        else:
            fail += 1
            print(f"FAIL {vid}: {detail}", flush=True)
            time.sleep(25)
        time.sleep(22)

    conn = sqlite3.connect(DB_PATH)
    total = conn.execute('SELECT COUNT(*) FROM video_evaluations WHERE evaluation_success = 1').fetchone()[0]
    conn.close()
    print(f"\nDONE: ok={ok} fail={fail} total_in_db={total}", flush=True)


if __name__ == '__main__':
    main()
