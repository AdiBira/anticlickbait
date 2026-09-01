"""
Parallel blitz: backfill fetch + eval all unevaluated, fully parallelized.
- 5 Tor fetch workers for backfill
- 10 OpenAI eval workers for all unevaluated transcripts
- list() at 0.5s delay (speed over caution)
"""

import json, os, sys, time, random, string, html as html_mod, math, sqlite3, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from youtube_transcript_api import YouTubeTranscriptApi
from text_evaluator import evaluate_video, compute_video_score
import requests

AUDIT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'audit', 'channel_audit.json')
FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
HANDLE_CID_PATH = '/tmp/handle_to_cid.json'
POOL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'cache', 'youtube_api', 'channel_videos_pool')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'anticlickbait.db')
TOR_PROXY = "socks5h://{user}:{pwd}@127.0.0.1:9050"

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

fetched_lock = threading.Lock()
db_lock = threading.Lock()


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


def eval_video(v, fetched):
    """Evaluate a single video from fetched transcripts."""
    vid = v['video_id']
    segments = v['segments']
    transcript_text = "\n".join(f"[{s['start']:.1f}s] {s['text']}" for s in segments)

    try:
        eval_result = evaluate_video(
            vid, v['channel_id'], v['title'],
            v.get('duration_seconds', 0) or 0, segments
        )
    except Exception as e:
        return vid, "eval_error", str(e)[:80]

    if not eval_result or eval_result.get('evaluation_success') is False:
        err = eval_result.get('evaluation_error', 'unknown') if eval_result else 'null'
        return vid, "eval_fail", err

    score = compute_video_score(eval_result)

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO video_evaluations (
                video_id, channel_id, title, thumbnail_url, duration_seconds,
                view_count, published_at,
                title_content_similarity_score, deception_score, focus_ratio,
                time_to_main_content, sponsor_interruption_score, video_score,
                title_analysis_reasoning, deception_reasoning, focus_reasoning,
                time_reasoning, sponsor_reasoning, llm_explanation,
                transcript_text, evaluation_success, evaluated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,datetime('now'))
            ON CONFLICT(video_id) DO UPDATE SET
                title_content_similarity_score = excluded.title_content_similarity_score,
                deception_score = excluded.deception_score,
                focus_ratio = excluded.focus_ratio,
                time_to_main_content = excluded.time_to_main_content,
                sponsor_interruption_score = excluded.sponsor_interruption_score,
                video_score = excluded.video_score,
                title_analysis_reasoning = excluded.title_analysis_reasoning,
                deception_reasoning = excluded.deception_reasoning,
                focus_reasoning = excluded.focus_reasoning,
                time_reasoning = excluded.time_reasoning,
                sponsor_reasoning = excluded.sponsor_reasoning,
                llm_explanation = excluded.llm_explanation,
                transcript_text = excluded.transcript_text,
                evaluation_success = 1, evaluation_error = NULL,
                evaluated_at = datetime('now')
        """, (
            vid, v['channel_id'], v['title'], v.get('thumbnail_url'),
            v.get('duration_seconds'), v.get('view_count'), v.get('published_at'),
            eval_result.get('title_content_similarity_score'),
            eval_result.get('deception_score'),
            eval_result.get('focus_ratio'),
            eval_result.get('time_to_main_content'),
            eval_result.get('sponsor_interruption_score'),
            score,
            eval_result.get('title_analysis_reasoning'),
            eval_result.get('deception_reasoning'),
            eval_result.get('focus_reasoning'),
            eval_result.get('time_reasoning'),
            eval_result.get('sponsor_reasoning'),
            eval_result.get('llm_explanation', ''),
            transcript_text
        ))
        conn.commit()
        conn.close()
    return vid, "success", f"{score:.1f}"


def backfill_worker(handle, needed, cid, existing_ids, fetched, yt_api):
    """Backfill one channel: list+fetch needed videos."""
    pool_file = os.path.join(POOL_DIR, f'{cid}__pool__min0.json')
    if not os.path.exists(pool_file):
        pool_file = os.path.join(POOL_DIR, f'{cid}__pool.json')
    if not os.path.exists(pool_file):
        return []

    with open(pool_file) as f:
        pool_data = json.load(f)

    pool_videos = pool_data.get('data', [])
    with fetched_lock:
        fetched_ids = set(fetched.keys())
    available = [v for v in pool_videos if v['video_id'] not in existing_ids and v['video_id'] not in fetched_ids]

    # 50/50 selection with 4x buffer
    by_date = sorted(available, key=lambda x: x.get('published_at', '') or '', reverse=True)
    by_views = sorted(available, key=lambda x: x.get('view_count', 0) or 0, reverse=True)
    n = needed * 4
    latest_n = math.ceil(n * 0.5)
    candidates = []
    seen = set()
    for v in by_date[:latest_n]:
        if v['video_id'] not in seen:
            candidates.append(v)
            seen.add(v['video_id'])
    for v in by_views:
        if len(candidates) >= n:
            break
        if v['video_id'] not in seen:
            candidates.append(v)
            seen.add(v['video_id'])

    results = []
    for v in candidates:
        if len(results) >= needed:
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
                continue

            segments, status = fetch_timedtext_via_tor(en_track._url)
            if status == "ok" and segments:
                entry = {
                    'video_id': vid, 'channel_id': cid,
                    'title': v.get('title', ''),
                    'duration_seconds': v.get('duration_seconds'),
                    'view_count': v.get('view_count'),
                    'published_at': v.get('published_at'),
                    'thumbnail_url': v.get('thumbnail_url'),
                    'segments': segments,
                }
                with fetched_lock:
                    fetched[vid] = entry
                results.append(entry)
                print(f"  BACKFILL {handle} OK {vid} [{len(results)}/{needed}]", flush=True)
        except Exception:
            continue
        time.sleep(0.5)

    return results


def main():
    with open(AUDIT_PATH) as f:
        audit = json.load(f)
    with open(HANDLE_CID_PATH) as f:
        handle_to_cid = json.load(f)
    with open(FETCHED_PATH) as f:
        fetched = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    evaluated_vids = set(r[0] for r in conn.execute(
        'SELECT video_id FROM video_evaluations WHERE evaluation_success = 1'
    ).fetchall())
    conn.close()

    existing_by_handle = {}
    for ch in audit:
        existing_by_handle[ch['handle']] = set(v['video_id'] for v in ch['videos'])

    # ========== PHASE 1: PARALLEL BACKFILL (5 workers) ==========
    print("=== PHASE 1: BACKFILL (5 workers) ===", flush=True)
    backfill_items = []
    for handle, needed in BACKFILL_CHANNELS.items():
        cid = handle_to_cid.get(handle)
        if not cid:
            continue
        # Check how many we already backfilled
        existing = existing_by_handle.get(handle, set())
        already_fetched = sum(1 for v in fetched.values() if v['channel_id'] == cid and v['video_id'] not in existing)
        still_needed = needed - already_fetched
        if still_needed > 0:
            backfill_items.append((handle, still_needed, cid, existing))

    print(f"Channels to backfill: {len(backfill_items)}, total videos: {sum(x[1] for x in backfill_items)}", flush=True)

    yt_api = YouTubeTranscriptApi()
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(backfill_worker, h, n, c, e, fetched, yt_api): h
            for h, n, c, e in backfill_items
        }
        for future in as_completed(futures):
            handle = futures[future]
            try:
                results = future.result()
                if results:
                    print(f"  {handle}: backfilled {len(results)}", flush=True)
            except Exception as e:
                print(f"  {handle}: ERROR {str(e)[:60]}", flush=True)

    # Save fetched
    with open(FETCHED_PATH, 'w') as f:
        json.dump(fetched, f)
    print(f"Fetched saved: {len(fetched)}", flush=True)

    # ========== PHASE 2: PARALLEL EVAL (10 workers) ==========
    print("\n=== PHASE 2: EVAL (10 workers) ===", flush=True)

    # Re-read evaluated
    conn = sqlite3.connect(DB_PATH)
    evaluated_vids = set(r[0] for r in conn.execute(
        'SELECT video_id FROM video_evaluations WHERE evaluation_success = 1'
    ).fetchall())
    conn.close()

    to_eval = [v for v in fetched.values() if v['video_id'] not in evaluated_vids and v.get('segments')]
    print(f"Videos to eval: {len(to_eval)}", flush=True)

    eval_ok = 0
    eval_fail = 0

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(eval_video, v, fetched): v['video_id'] for v in to_eval}
        for future in as_completed(futures):
            vid = futures[future]
            try:
                vid, status, detail = future.result()
                if status == "success":
                    eval_ok += 1
                    if eval_ok % 10 == 0:
                        print(f"  Eval progress: {eval_ok} OK, {eval_fail} fail", flush=True)
                else:
                    eval_fail += 1
                    print(f"  EVAL FAIL {vid}: {detail[:60]}", flush=True)
            except Exception as e:
                eval_fail += 1
                print(f"  EVAL ERROR {vid}: {str(e)[:60]}", flush=True)

    conn = sqlite3.connect(DB_PATH)
    total = conn.execute('SELECT COUNT(*) FROM video_evaluations WHERE evaluation_success = 1').fetchone()[0]
    conn.close()

    print(f"\n=== DONE ===", flush=True)
    print(f"Eval: {eval_ok} OK, {eval_fail} fail", flush=True)
    print(f"Total evaluated in DB: {total}", flush=True)


if __name__ == '__main__':
    main()
