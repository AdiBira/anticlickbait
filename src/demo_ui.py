#!/usr/bin/env python3
"""Simple HTML generator for video evaluation results."""

import re
import webbrowser
from pathlib import Path
from transcript_fetcher import get_transcript
from text_evaluator import evaluate_video, compute_video_score, _format_transcript


def extract_video_id(url_or_id: str) -> str:
    """Extract video ID from URL or return as-is."""
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return url_or_id


def generate_html(result: dict, score: float, transcript: str, title: str) -> str:
    """Generate HTML report for evaluation result."""

    score_color = "#22c55e" if score >= 70 else "#f97316" if score >= 40 else "#ef4444"

    # Calculate score breakdown
    a = result["title_content_similarity_score"] * 10
    b = result["focus_ratio_pct"]
    c = (1 - result["time_to_main_content_fraction"]) * 100
    deception_pen = -20 if result.get("deception_flag") else 0
    mismatch_pen = -10 if result.get("sensationalism_mismatch") else 0
    sponsor_val = result.get("sponsor_interruption", "none")
    sponsor_pen = -5 if sponsor_val == "minor" else -10 if sponsor_val == "excessive" else 0

    sens_mismatch_html = '<span style="color:#ef4444">YES (-10)</span>' if result.get("sensationalism_mismatch") else '<span style="color:#22c55e">No</span>'
    deception_html = '<span style="color:#ef4444">YES (-20)</span>' if result.get("deception_flag") else '<span style="color:#22c55e">No</span>'

    sponsor = result.get("sponsor_interruption", "none")
    if sponsor == "excessive":
        sponsor_html = '<span style="color:#ef4444">Excessive (-10)</span>'
    elif sponsor == "minor":
        sponsor_html = '<span style="color:#f97316">Minor (-5)</span>'
    else:
        sponsor_html = '<span style="color:#22c55e">None</span>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Anticlickbait - {result['video_id']}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: #eee; padding: 20px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{ color: #fff; margin-bottom: 10px; }}
        h2 {{ color: #aaa; font-size: 14px; margin-bottom: 20px; font-weight: normal; }}
        .score-box {{ background: linear-gradient(135deg, #16213e, #1a1a2e); border: 2px solid {score_color}; border-radius: 12px; padding: 30px; text-align: center; margin-bottom: 20px; }}
        .score {{ font-size: 72px; font-weight: bold; color: {score_color}; }}
        .score-label {{ color: #888; font-size: 14px; margin-top: 5px; }}
        .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 20px; }}
        .metric {{ background: #16213e; border-radius: 8px; padding: 20px; }}
        .metric-value {{ font-size: 28px; font-weight: bold; color: #fff; }}
        .metric-label {{ color: #888; font-size: 12px; margin-top: 5px; }}
        .flags {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin-bottom: 20px; }}
        .flag {{ background: #16213e; border-radius: 8px; padding: 15px; }}
        .flag-label {{ color: #888; font-size: 12px; }}
        .flag-value {{ font-size: 18px; margin-top: 5px; }}
        .reasoning {{ background: #16213e; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
        .reasoning h3 {{ color: #fff; margin-bottom: 10px; font-size: 14px; }}
        .reasoning p {{ color: #ccc; line-height: 1.6; }}
        .breakdown {{ background: #16213e; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
        .breakdown h3 {{ color: #fff; margin-bottom: 15px; font-size: 14px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #333; }}
        th {{ color: #888; font-size: 12px; }}
        td {{ color: #fff; }}
        .transcript {{ background: #16213e; border-radius: 8px; padding: 20px; }}
        .transcript h3 {{ color: #fff; margin-bottom: 10px; font-size: 14px; }}
        .transcript pre {{ color: #aaa; font-size: 12px; white-space: pre-wrap; max-height: 300px; overflow-y: auto; }}
        .formula {{ color: #888; font-size: 12px; margin-top: 15px; padding: 10px; background: #0f0f1a; border-radius: 4px; }}
    </style>
</head>
<body>
    <div class="container">
        <img src="https://img.youtube.com/vi/{result['video_id']}/hqdefault.jpg" style="width:100%; max-width:480px; border-radius:8px; margin-bottom:20px;">
        <h1>{title}</h1>
        <h2>Video ID: {result['video_id']}</h2>

        <div class="score-box">
            <div class="score">{score:.1f}</div>
            <div class="score-label">ANTICLICKBAIT SCORE</div>
        </div>

        <div class="grid">
            <div class="metric">
                <div class="metric-value">{result['title_content_similarity_score']}/10</div>
                <div class="metric-label">Title-Content Similarity</div>
            </div>
            <div class="metric">
                <div class="metric-value">{result['focus_ratio_pct']}%</div>
                <div class="metric-label">Focus Ratio</div>
            </div>
            <div class="metric">
                <div class="metric-value">{result['time_to_main_content_seconds']}s</div>
                <div class="metric-label">Time to Main Content</div>
            </div>
            <div class="metric">
                <div class="metric-value">{result['title_sensationalism_score']}/10</div>
                <div class="metric-label">Title Sensationalism</div>
            </div>
            <div class="metric">
                <div class="metric-value">{result['time_to_main_content_fraction']*100:.1f}%</div>
                <div class="metric-label">Time to Main (Fraction)</div>
            </div>
            <div class="metric">
                <div class="metric-value">{len(transcript):,}</div>
                <div class="metric-label">Transcript Characters</div>
            </div>
        </div>

        <div class="flags" style="grid-template-columns: repeat(3, 1fr);">
            <div class="flag">
                <div class="flag-label">SENSATIONALISM MISMATCH</div>
                <div class="flag-value">{sens_mismatch_html}</div>
            </div>
            <div class="flag">
                <div class="flag-label">DECEPTION FLAG</div>
                <div class="flag-value">{deception_html}</div>
            </div>
            <div class="flag">
                <div class="flag-label">SPONSOR INTERRUPTION</div>
                <div class="flag-value">{sponsor_html}</div>
            </div>
        </div>

        <div class="reasoning">
            <h3>LLM REASONING</h3>
            <h4 style="color: #888; font-size: 12px; margin-bottom: 5px;">CALL 1: Title Analysis</h4>
            <p>{result.get('title_analysis_reasoning', 'No reasoning provided')}</p>
            <h4 style="color: #888; font-size: 12px; margin-top: 15px; margin-bottom: 5px;">CALL 2: Content Analysis</h4>
            <p>{result.get('content_analysis_reasoning', 'No reasoning provided')}</p>
        </div>

        <div class="breakdown">
            <h3>SCORE BREAKDOWN</h3>
            <table>
                <tr><th>Component</th><th>Raw</th><th>Normalized (0-100)</th><th>Weight</th></tr>
                <tr><td>Title-Content Similarity</td><td>{result['title_content_similarity_score']}/10</td><td>{a:.1f}</td><td>33.33%</td></tr>
                <tr><td>Focus Ratio</td><td>{result['focus_ratio_pct']}%</td><td>{b:.1f}</td><td>33.33%</td></tr>
                <tr><td>Time to Main (inverted)</td><td>{result['time_to_main_content_fraction']*100:.1f}%</td><td>{c:.1f}</td><td>33.33%</td></tr>
            </table>
            <div class="formula">
                ({a:.1f} + {b:.1f} + {c:.1f}) / 3 = {(a+b+c)/3:.1f}<br>
                Penalties: Deception {deception_pen}, Mismatch {mismatch_pen}, Sponsor {sponsor_pen}<br>
                <strong>Final: {(a+b+c)/3:.1f} + {deception_pen} + {mismatch_pen} + {sponsor_pen} = {score:.1f}</strong>
            </div>
        </div>

        <div class="transcript">
            <h3>FULL TRANSCRIPT ({len(transcript):,} chars)</h3>
            <pre>{transcript}</pre>
        </div>
    </div>
</body>
</html>"""


def evaluate_and_show(url_or_id: str, title: str = None):
    """Evaluate a video and open HTML report in browser."""
    video_id = extract_video_id(url_or_id)
    print(f"Video ID: {video_id}")

    # Fetch transcript
    print("Fetching transcript...")
    transcript_result = get_transcript(video_id)

    if not transcript_result["success"]:
        print(f"ERROR: {transcript_result['error']}")
        return

    segments = transcript_result["segments"]
    formatted = _format_transcript(segments)
    duration = int(segments[-1]["start"]) if segments else 300

    print(f"Transcript: {len(segments)} segments, {len(formatted):,} chars")

    # Use provided title or placeholder
    if not title:
        title = f"Video {video_id}"

    # Evaluate
    print("Running evaluation...")
    result = evaluate_video(
        video_id=video_id,
        channel_id="demo",
        title=title,
        duration_seconds=duration,
        transcript_segments=segments,
    )

    if not result.get("evaluation_success"):
        print(f"ERROR: {result.get('error')}")
        return

    score = compute_video_score(result)
    print(f"Score: {score:.1f}")

    # Generate HTML
    html = generate_html(result, score, formatted, title)

    # Save and open
    output_path = Path(__file__).parent.parent / "data" / f"report_{video_id}.html"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(html)

    print(f"Report saved: {output_path}")
    webbrowser.open(f"file://{output_path.absolute()}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python demo_ui.py <youtube_url_or_id> [title]")
        print("Example: python demo_ui.py rurhk1hadp8 'BANE airplane scene'")
        sys.exit(1)

    url = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else None

    evaluate_and_show(url, title)
