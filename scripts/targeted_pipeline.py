"""
Targeted pipeline: fetch ONLY the 83 missing canonical transcripts + eval ONLY the ~225 pending.
No wasted work.
"""

import json, os, sys, time, random, string, html as html_mod, sqlite3, threading
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

REPLACED_HANDLES = {'@autotopnl','@dropout','@rivermonsters','@technogamerzofficial',
    '@ANInewsIndia','@petcollective','@mrgearofficial','@nba','@beinsports',
    '@fcbarcelona','@realmadrid','@wildfilmsindia'}

REPL_CIDS = {'UC0PXqiud6dbwOAk8RvslgpQ','UCdN4aXTrHAtfgbVG9HjBmxQ','UCGShu88Lh2ZAtXX0qbV9fXA',
    'UCiYcA0gJzg855iSKMrX3oHg','UCZaT_X_mc0BI-djXOlfhqWQ','UC2YvLBsduOMcStpJbZav-mw',
    'UCcMDMoNu66_1Hwi5-MeiQgw','UCoLrcjPV5PbUrUyXq5mjc_A',
    'UCiWLfSweyRNmLpgEHekhoAg','UCkBY0aHJP9BwjZLDYxAQrKg',
    'UCB_qr75-ydFVKSF9Dmo6izg','UCfBZ5ps6L-bov4nJL0mPVHg'}

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


def eval_one(vid, v, client):
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

    conn = sqlite3.connect(DB_PATH)
    evaluated = set(r[0] for r in conn.execute(
        'SELECT video_id FROM video_evaluations WHERE evaluation_success = 1'
    ).fetchall())
    conn.close()

    # ===== PHASE 1: FETCH ONLY MISSING CANONICAL (not evaluated, not fetched) =====
    need_fetch = []
    for ch in audit:
        if ch['handle'] in REPLACED_HANDLES:
            continue
        for v in ch['videos']:
            vid = v['video_id']
            if vid not in evaluated and vid not in fetched:
                need_fetch.append({
                    'video_id': vid, 'channel_id': ch['channel_id'],
                    'title': v.get('title', ''),
                    'duration_seconds': v.get('duration_seconds'),
                    'view_count': v.get('view_count'),
                    'published_at': v.get('published_at'),
                    'thumbnail_url': v.get('thumbnail_url'),
                })

    print(f"=== PHASE 1: FETCH {len(need_fetch)} MISSING CANONICAL ===", flush=True)

    if need_fetch:
        yt_api = YouTubeTranscriptApi()
        success = fail = 0
        BATCH = 20

        for batch_start in range(0, len(need_fetch), BATCH):
            batch = need_fetch[batch_start:batch_start + BATCH]
            print(f"\n  Batch {batch_start//BATCH+1}: listing {len(batch)} videos", flush=True)

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
                        print(f"  IP BLOCKED!", flush=True)
                        break
                time.sleep(3.0)

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
                            print(f"    OK {vid} [{success}/{success+fail}]", flush=True)
                        else:
                            fail += 1
                    except Exception:
                        fail += 1

            with save_lock:
                with open(FETCHED_PATH, 'w') as f:
                    json.dump(fetched, f)

        print(f"  Fetch done: {success} OK, {fail} fail", flush=True)

    # ===== PHASE 2: EVAL ALL PENDING =====
    # Re-read evaluated set
    conn = sqlite3.connect(DB_PATH)
    evaluated = set(r[0] for r in conn.execute(
        'SELECT video_id FROM video_evaluations WHERE evaluation_success = 1'
    ).fetchall())
    conn.close()

    # Build valid CID set (canonical non-replaced + replacement channels)
    valid_cids = set()
    for ch in audit:
        if ch['handle'] not in REPLACED_HANDLES:
            valid_cids.add(ch['channel_id'])
    valid_cids.update(REPL_CIDS)

    to_eval = [(vid, v) for vid, v in fetched.items()
               if vid not in evaluated and v.get('segments') and v.get('channel_id') in valid_cids]

    print(f"\n=== PHASE 2: EVAL {len(to_eval)} PENDING (3 RPM) ===", flush=True)

    client = OpenAI(api_key=OPENAI_API_KEY)
    ok = fail = 0
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
