"""
Eval-only: evaluate all pending videos in parallel. No fetching.
10 workers, rate-limited to ~6 req/s (360 RPM, under 500 RPM Tier 1 limit).
"""

import json, os, sys, time, re, sqlite3, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from openai import OpenAI
import text_evaluator
from config import OPENAI_API_KEY, OPENAI_MODEL

FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
AUDIT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'audit', 'channel_audit.json')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'anticlickbait.db')

REPLACED_HANDLES = {'@autotopnl','@dropout','@rivermonsters','@technogamerzofficial',
    '@ANInewsIndia','@petcollective','@mrgearofficial','@nba','@beinsports',
    '@fcbarcelona','@realmadrid','@wildfilmsindia'}

REPL_CIDS = {'UC0PXqiud6dbwOAk8RvslgpQ','UCdN4aXTrHAtfgbVG9HjBmxQ','UCGShu88Lh2ZAtXX0qbV9fXA',
    'UCiYcA0gJzg855iSKMrX3oHg','UCZaT_X_mc0BI-djXOlfhqWQ','UC2YvLBsduOMcStpJbZav-mw',
    'UCcMDMoNu66_1Hwi5-MeiQgw','UCoLrcjPV5PbUrUyXq5mjc_A',
    'UCiWLfSweyRNmLpgEHekhoAg','UCkBY0aHJP9BwjZLDYxAQrKg',
    'UCB_qr75-ydFVKSF9Dmo6izg','UCfBZ5ps6L-bov4nJL0mPVHg'}

db_lock = threading.Lock()
counters = {'ok': 0, 'fail': 0}
counter_lock = threading.Lock()

# Rate limiter: max N requests per second
rate_lock = threading.Lock()
request_times = []
MAX_RPS = 6  # 360 RPM, well under 500 RPM limit


def rate_limit():
    """Block until we can make a request within MAX_RPS."""
    while True:
        with rate_lock:
            now = time.time()
            # Remove timestamps older than 1 second
            while request_times and request_times[0] < now - 1.0:
                request_times.pop(0)
            if len(request_times) < MAX_RPS:
                request_times.append(now)
                return
        time.sleep(0.05)


def api_call_with_retry(client, prompt, max_retries=8):
    for attempt in range(max_retries):
        rate_limit()
        try:
            r = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.1,
                response_format={'type': 'json_object'},
            )
            return json.loads(r.choices[0].message.content), None
        except Exception as e:
            err = str(e)
            if '429' in err or 'Connection' in err:
                wait = 2 ** attempt + 1
                m = re.search(r'try again in (\d+(\.\d+)?)s', err)
                if m:
                    wait = max(wait, float(m.group(1)) + 0.5)
                time.sleep(min(wait, 60))
            else:
                return None, err[:80]
    return None, "max retries exceeded"


def eval_one(vid, v, client):
    segments = v['segments']
    transcript = '\n'.join(f'[{s["start"]:.1f}s] {s["text"]}' for s in segments)
    if len(transcript) > 50000:
        transcript = transcript[:50000] + '\n[TRUNCATED]'
    duration = v.get('duration_seconds', 0) or 0

    prompt1 = text_evaluator.TITLE_ANALYSIS_PROMPT.format(title=v['title'], transcript=transcript)
    result1, err = api_call_with_retry(client, prompt1)
    if err:
        return None, f"call1: {err}"

    prompt2 = text_evaluator.CONTENT_ANALYSIS_PROMPT.format(
        title=v['title'], transcript=transcript, duration_seconds=duration
    )
    result2, err = api_call_with_retry(client, prompt2)
    if err:
        return None, f"call2: {err}"

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
        'evaluation_success': True,
    }
    score = text_evaluator.compute_video_score(r)
    return r, score


def save_eval(vid, v, r, score):
    transcript_text = '\n'.join(f'[{s["start"]:.1f}s] {s["text"]}' for s in v['segments'])
    with db_lock:
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


def process_video(vid, v, client, total):
    r, score = eval_one(vid, v, client)
    if r and r.get('title_content_similarity_score', -1) >= 0:
        score = score if score is not None else 0.0
        save_eval(vid, v, r, score)
        with counter_lock:
            counters['ok'] += 1
            n = counters['ok'] + counters['fail']
            print(f"OK {vid} score={score:.1f} [{counters['ok']}/{n}] ({n}/{total})", flush=True)
    else:
        with counter_lock:
            counters['fail'] += 1
            n = counters['ok'] + counters['fail']
            print(f"FAIL {vid}: {str(score)[:80]} ({n}/{total})", flush=True)


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

    valid_cids = set()
    for ch in audit:
        if ch['handle'] not in REPLACED_HANDLES:
            valid_cids.add(ch['channel_id'])
    valid_cids.update(REPL_CIDS)

    to_eval = [(vid, v) for vid, v in fetched.items()
               if vid not in evaluated and v.get('segments') and v.get('channel_id') in valid_cids]

    total = len(to_eval)
    print(f"=== EVAL {total} PENDING (10 workers, {MAX_RPS} req/s) ===", flush=True)

    client = OpenAI(api_key=OPENAI_API_KEY)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(process_video, vid, v, client, total): vid
                   for vid, v in to_eval}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                vid = futures[future]
                with counter_lock:
                    counters['fail'] += 1
                print(f"EXCEPTION {vid}: {str(e)[:80]}", flush=True)

    conn = sqlite3.connect(DB_PATH)
    total_db = conn.execute('SELECT COUNT(*) FROM video_evaluations WHERE evaluation_success = 1').fetchone()[0]
    conn.close()
    print(f"\nDONE: ok={counters['ok']} fail={counters['fail']} total_in_db={total_db}", flush=True)


if __name__ == '__main__':
    main()
