"""Fetch + eval 1 video for @rightchoiceshearing to reach 15/15."""
import json, os, sys, time, random, string, sqlite3, requests
import html as html_mod
from xml.etree import ElementTree as ET
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
import text_evaluator
from config import OPENAI_API_KEY, OPENAI_MODEL

RCS_CID = 'UC2YvLBsduOMcStpJbZav-mw'
DB = os.path.join(os.path.dirname(__file__), '..', 'data', 'anticlickbait.db')
POOL_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'cache', 'youtube_api', 'channel_videos_pool')
FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
TOR_PROXY = 'socks5h://{user}:{pwd}@127.0.0.1:9050'

conn = sqlite3.connect(DB)
evaluated = set(r[0] for r in conn.execute(
    'SELECT video_id FROM video_evaluations WHERE evaluation_success=1 AND channel_id=?', (RCS_CID,)).fetchall())
conn.close()
fetched = json.load(open(FETCHED_PATH))
exclude = evaluated | set(fetched.keys())
print(f'@rightchoiceshearing: {len(evaluated)} evaluated')

pool = []
for suffix in ['__pool__min0.json', '__pool.json']:
    path = os.path.join(POOL_DIR, f'{RCS_CID}{suffix}')
    if os.path.exists(path):
        pool = json.load(open(path)).get('data', [])
        break

available = [v for v in pool if v['video_id'] not in exclude]
print(f'Pool: {len(pool)} total, {len(available)} available')

yt_api = YouTubeTranscriptApi()
for v in available:
    vid = v['video_id']
    print(f'Trying {vid} "{v.get("title","")[:50]}"...', end=' ', flush=True)
    try:
        tlist = yt_api.list(vid)
        en_track = None
        for t in tlist:
            if t.language_code == 'en' or t.language_code.startswith('en-'):
                en_track = t
                break
        if not en_track or not en_track._url:
            print('no EN track')
            time.sleep(3)
            continue

        cred = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        proxies = {
            'http': TOR_PROXY.format(user=cred, pwd=cred),
            'https': TOR_PROXY.format(user=cred, pwd=cred),
        }
        resp = requests.get(en_track._url, proxies=proxies, timeout=30)
        if resp.status_code != 200:
            print(f'http {resp.status_code}')
            time.sleep(3)
            continue
        root = ET.fromstring(resp.text)
        segments = []
        for t_el in root.findall('.//text'):
            s = float(t_el.get('start', 0))
            d = float(t_el.get('dur', 0))
            c = html_mod.unescape(t_el.text or '').strip()
            if c:
                segments.append({'start': s, 'text': c, 'duration': d})
        if not segments:
            print('empty segments')
            time.sleep(3)
            continue

        print(f'OK ({len(segments)} segments)', flush=True)

        # Eval
        transcript = '\n'.join(f'[{s["start"]:.1f}s] {s["text"]}' for s in segments)
        if len(transcript) > 50000:
            transcript = transcript[:50000] + '\n[TRUNCATED]'
        duration = v.get('duration_seconds', 0) or 0
        client = OpenAI(api_key=OPENAI_API_KEY)

        p1 = text_evaluator.TITLE_ANALYSIS_PROMPT.format(title=v['title'], transcript=transcript)
        r1 = client.chat.completions.create(model=OPENAI_MODEL, messages=[{'role': 'user', 'content': p1}],
                                            temperature=0.1, response_format={'type': 'json_object'})
        result1 = json.loads(r1.choices[0].message.content)

        p2 = text_evaluator.CONTENT_ANALYSIS_PROMPT.format(title=v['title'], transcript=transcript, duration_seconds=duration)
        r2 = client.chat.completions.create(model=OPENAI_MODEL, messages=[{'role': 'user', 'content': p2}],
                                            temperature=0.1, response_format={'type': 'json_object'})
        result2 = json.loads(r2.choices[0].message.content)

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

        transcript_text = '\n'.join(f'[{s["start"]:.1f}s] {s["text"]}' for s in segments)
        conn = sqlite3.connect(DB)
        conn.execute("""INSERT INTO video_evaluations (
            video_id, channel_id, title, thumbnail_url, duration_seconds, view_count, published_at,
            title_content_similarity_score, deception_score, focus_ratio, time_to_main_content,
            sponsor_interruption_score, video_score, title_analysis_reasoning, deception_reasoning,
            focus_reasoning, time_reasoning, sponsor_reasoning, llm_explanation, transcript_text,
            evaluation_success, evaluated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,datetime('now'))
        ON CONFLICT(video_id) DO UPDATE SET video_score=excluded.video_score, evaluation_success=1
        """, (vid, RCS_CID, v['title'], v.get('thumbnail_url'), v.get('duration_seconds'),
              v.get('view_count'), v.get('published_at'), r['title_content_similarity_score'],
              r['deception_score'], r['focus_ratio'], r['time_to_main_content'],
              r['sponsor_interruption_score'], score, r['title_analysis_reasoning'],
              r['deception_reasoning'], r['focus_reasoning'], r['time_reasoning'],
              r['sponsor_reasoning'], r['llm_explanation'], transcript_text))
        conn.commit()
        new_count = conn.execute(
            'SELECT COUNT(*) FROM video_evaluations WHERE evaluation_success=1 AND channel_id=?',
            (RCS_CID,)).fetchone()[0]
        conn.close()
        print(f'SAVED score={score} | @rightchoiceshearing now at {new_count}/15')
        break
    except Exception as e:
        print(f'error: {str(e)[:60]}')
        time.sleep(3)
