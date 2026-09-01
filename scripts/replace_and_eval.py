"""
Replace 12 channels + eval all pending. Runs in 3 phases:
1. Fetch video pools for 12 replacement channels via YouTube API
2. Select 15 videos per channel (50/50 latest/popular), fetch transcripts via Tor
3. Eval all pending transcripts at 3 RPM (sequential, 25s between calls)
"""

import json, os, sys, time, random, string, html as html_mod, math, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from xml.etree import ElementTree as ET
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
import requests
import text_evaluator
from config import OPENAI_API_KEY, OPENAI_MODEL

FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'anticlickbait.db')
TOR_PROXY = "socks5h://{user}:{pwd}@127.0.0.1:9050"

REPLACEMENTS = {
    '@autotopnl': '@1320video',
    '@dropout': '@keyandpeele',
    '@rivermonsters': '@cryingman',
    '@technogamerzofficial': '@coryxkenshin',
    '@ANInewsIndia': '@vicenews',
    '@petcollective': '@rightchoiceshearing',
    '@mrgearofficial': '@hydraulicpresschannel',
    '@nba': '@jackdoherty',
    '@beinsports': '@espn',
    '@fcbarcelona': '@omarrajaespn',
    '@realmadrid': '@formula1',
    '@wildfilmsindia': '@greatwhitesharkking',
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
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"
    root = ET.fromstring(resp.text)
    segments = []
    for t in root.findall('.//text'):
        s = float(t.get('start', 0))
        d = float(t.get('dur', 0))
        c = html_mod.unescape(t.text or '').strip()
        if c:
            segments.append({'start': s, 'text': c, 'duration': d})
    return (segments, "ok") if segments else (None, "empty")


def get_channel_id(handle):
    """Get channel ID from cache."""
    cache_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'cache', 'youtube_api', 'channel_info_by_handle')
    # Try exact and case variations
    for fname in os.listdir(cache_dir):
        if fname.lower() == f'{handle.lower()}.json':
            with open(os.path.join(cache_dir, fname)) as f:
                d = json.load(f)
            return d.get('data', {}).get('channel_id') or d.get('data', {}).get('id')
    return None


def get_pool_videos(cid):
    """Get video pool from cache."""
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
    for v in ordered:
        if len(selected) >= 15:
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
                time.sleep(0.5)
                continue

            segments, status = fetch_timedtext_via_tor(en_track._url)
            if status == "ok" and segments:
                selected.append((v, segments))
                print(f"    OK {vid} ({len(segments)} segs) [{len(selected)}/15]", flush=True)
            else:
                print(f"    skip {vid}: {status}", flush=True)
        except Exception as e:
            print(f"    skip {vid}: {str(e)[:50]}", flush=True)
        time.sleep(1.0)

    return selected


def eval_one(vid, v, client):
    """Eval one video with sequential LLM calls, respecting 3 RPM."""
    segments = v['segments']
    transcript_lines = [f'[{s["start"]:.1f}s] {s["text"]}' for s in segments]
    transcript = '\n'.join(transcript_lines)
    if len(transcript) > 50000:
        transcript = transcript[:50000] + '\n[TRUNCATED]'
    duration = v.get('duration_seconds', 0) or 0

    # Call 1
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

    time.sleep(22)  # Wait for RPM

    # Call 2
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

    yt_api = YouTubeTranscriptApi()
    client = OpenAI(api_key=OPENAI_API_KEY)

    # ===== PHASE 1: FETCH REPLACEMENT CHANNEL VIDEOS =====
    print("=== PHASE 1: REPLACEMENT CHANNELS ===", flush=True)

    for old_handle, new_handle in REPLACEMENTS.items():
        cid = get_channel_id(new_handle)
        if not cid:
            print(f"\n{new_handle}: NO CID, skipping", flush=True)
            continue

        pool = get_pool_videos(cid)
        if not pool:
            print(f"\n{new_handle}: NO POOL, skipping", flush=True)
            continue

        # Skip already-fetched videos for this channel
        existing = set(vid for vid, v in fetched.items() if v['channel_id'] == cid)
        available = [v for v in pool if v['video_id'] not in existing]

        print(f"\n{old_handle} → {new_handle} (cid={cid}, pool={len(pool)}, available={len(available)})", flush=True)

        if len(existing) >= 15:
            print(f"  Already have {len(existing)} videos, skipping", flush=True)
            continue

        needed = 15 - len(existing)
        selected = select_15_with_transcripts(available[:needed*4], yt_api)

        for v, segments in selected:
            fetched[v['video_id']] = {
                'video_id': v['video_id'], 'channel_id': cid,
                'title': v.get('title', ''), 'duration_seconds': v.get('duration_seconds'),
                'view_count': v.get('view_count'), 'published_at': v.get('published_at'),
                'thumbnail_url': v.get('thumbnail_url'), 'segments': segments,
            }

        # Save after each channel
        with open(FETCHED_PATH, 'w') as f:
            json.dump(fetched, f)
        print(f"  Saved. Total fetched: {len(fetched)}", flush=True)

    # ===== PHASE 2: EVAL ALL PENDING =====
    print("\n=== PHASE 2: EVAL (3 RPM) ===", flush=True)

    conn = sqlite3.connect(DB_PATH)
    evaluated = set(r[0] for r in conn.execute(
        'SELECT video_id FROM video_evaluations WHERE evaluation_success = 1'
    ).fetchall())
    conn.close()

    to_eval = [(vid, v) for vid, v in fetched.items() if vid not in evaluated and v.get('segments')]
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
            time.sleep(25)  # Extra wait on failure
        time.sleep(22)  # RPM spacing

    conn = sqlite3.connect(DB_PATH)
    total = conn.execute('SELECT COUNT(*) FROM video_evaluations WHERE evaluation_success = 1').fetchone()[0]
    conn.close()
    print(f"\nDONE: ok={ok} fail={fail} total_in_db={total}", flush=True)


if __name__ == '__main__':
    main()
