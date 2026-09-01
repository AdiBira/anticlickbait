"""
Eval-only pass: reads fetched transcripts from data/fetched_transcripts.json,
evaluates with OpenAI, writes results to SQLite.
Re-reads the JSON file periodically to pick up new transcripts from the fetch task.
"""

import json, sqlite3, sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from text_evaluator import evaluate_video, compute_video_score

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'anticlickbait.db')
FETCHED_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fetched_transcripts.json')
WORKERS = 2
POLL_INTERVAL = 30  # seconds to wait before re-reading JSON for new transcripts


def get_evaluated_ids():
    conn = sqlite3.connect(DB_PATH)
    ids = set(r[0] for r in conn.execute(
        "SELECT video_id FROM video_evaluations WHERE evaluation_success = 1"
    ).fetchall())
    conn.close()
    return ids


def process_video(v):
    vid = v['video_id']
    segments = v['segments']
    transcript_text = "\n".join(f"[{s['start']:.1f}s] {s['text']}" for s in segments)

    eval_result = evaluate_video(vid, v['channel_id'], v['title'], v.get('duration_seconds', 0) or 0, segments)
    if not eval_result or eval_result.get('evaluation_success') == False:
        err = eval_result.get('evaluation_error', 'unknown') if eval_result else 'null'
        return vid, "eval_fail", err

    score = compute_video_score(eval_result)

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


def main():
    total_success = 0
    total_fail = 0
    passes = 0

    while True:
        passes += 1
        with open(FETCHED_PATH) as f:
            all_fetched = json.load(f)

        evaluated = get_evaluated_ids()
        pending = [v for v in all_fetched.values() if v['video_id'] not in evaluated and v.get('segments')]

        if not pending:
            if passes == 1:
                print("No unevaluated transcripts found. Waiting for fetch task...")
                time.sleep(POLL_INTERVAL)
                continue
            else:
                print(f"All caught up. Waiting {POLL_INTERVAL}s for more transcripts...")
                time.sleep(POLL_INTERVAL)
                # Re-check
                with open(FETCHED_PATH) as f:
                    all_fetched2 = json.load(f)
                evaluated2 = get_evaluated_ids()
                pending2 = [v for v in all_fetched2.values() if v['video_id'] not in evaluated2 and v.get('segments')]
                if not pending2:
                    print(f"\nDone. Total success={total_success}, fail={total_fail}")
                    break
                continue

        print(f"\n--- Pass {passes}: {len(pending)} videos to eval (fetched={len(all_fetched)}, evaluated={len(evaluated)}) ---")

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(process_video, v): v for v in pending}
            for future in as_completed(futures):
                try:
                    vid, status, detail = future.result()
                    if status == "success":
                        total_success += 1
                        print(f"  OK {vid} score={detail} (total: {total_success})")
                    else:
                        total_fail += 1
                        print(f"  FAIL {vid}: {detail}")
                except Exception as e:
                    total_fail += 1
                    print(f"  ERROR: {str(e)[:100]}")

        print(f"  Running total: success={total_success} fail={total_fail}")


if __name__ == '__main__':
    main()
