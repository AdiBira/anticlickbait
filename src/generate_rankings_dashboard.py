#!/usr/bin/env python3
"""Generate a static anticlickbait rankings site from anticlickbait.db.

Outputs:
- rankings dashboard index (global / India / category rankings)
- one channel page per ranked channel with full video-level evidence

Key transparency rules implemented:
- Channel score displayed in the UI is recomputed from ALL stored scored videos shown on that channel page.
- Metric labels are human-readable (no raw field names in UI).
- Each metric has an info button with explanation, calculation, and prompt source.
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "anticlickbait.db"
DEFAULT_OUT = ROOT / "data" / "rankings_dashboard.html"
CHANNEL_DIR = ROOT / "data" / "channels"
ASSETS_DIR = ROOT / "data" / "assets"
CHANNEL_AVATAR_DIR = ASSETS_DIR / "channel_avatars"
VIDEO_THUMB_DIR = ASSETS_DIR / "video_thumbs"
CHANNEL_AVATAR_MANIFEST = ASSETS_DIR / "channel_avatars_manifest.json"
TEXT_EVALUATOR_PATH = ROOT / "src" / "text_evaluator.py"
CURATED_CHANNELS_PATH = ROOT / "data" / "channels_final.json"


def get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _norm_key(value: Any) -> str:
    import re

    s = str(value or "").lower().strip().replace("@", "")
    return re.sub(r"[^a-z0-9]+", "", s)


def extract_prompts_from_text_evaluator(path: Path) -> dict[str, str]:
    prompts = {"title": "", "content": ""}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return prompts

    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        key = None
        if target.id == "TITLE_ANALYSIS_PROMPT":
            key = "title"
        elif target.id == "CONTENT_ANALYSIS_PROMPT":
            key = "content"
        if not key:
            continue
        try:
            prompts[key] = ast.literal_eval(node.value)
        except Exception:
            pass
    return prompts


def channel_page_filename(channel_id: str) -> str:
    return f"channel_{channel_id}.html"


def format_count(v: Any) -> str:
    if v in (None, ""):
        return "—"
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def compute_channel_aggregates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = q(
        conn,
        """
        SELECT
            c.channel_id,
            c.title,
            c.custom_url,
            c.country,
            c.language,
            c.category_id,
            cat.category_name,
            c.subscriber_count,
            c.video_count,
            c.view_count,
            ROUND(AVG(CASE WHEN v.evaluation_success = 1 AND v.video_score IS NOT NULL THEN v.video_score END), 6) AS channel_score_ui,
            SUM(CASE WHEN v.evaluation_success = 1 AND v.video_score IS NOT NULL THEN 1 ELSE 0 END) AS videos_in_score,
            COUNT(v.video_id) AS total_video_rows,
            MAX(v.evaluated_at) AS last_evaluated_at
        FROM channels c
        LEFT JOIN categories cat ON cat.category_id = c.category_id
        LEFT JOIN video_evaluations v ON v.channel_id = c.channel_id
        GROUP BY
            c.channel_id, c.title, c.custom_url, c.country, c.language,
            c.category_id, cat.category_name, c.subscriber_count, c.video_count, c.view_count
        """,
    )
    for r in rows:
        r["videos_in_score"] = int(r.get("videos_in_score") or 0)
        r["total_video_rows"] = int(r.get("total_video_rows") or 0)
    return rows


def fetch_videos_by_channel(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = q(
        conn,
        """
        SELECT
            video_id,
            channel_id,
            title,
            description,
            published_at,
            category_id,
            duration_seconds,
            view_count,
            like_count,
            comment_count,
            title_content_similarity_score,
            focus_ratio_pct,
            time_to_main_content_seconds,
            time_to_main_content_fraction,
            title_sensationalism_score,
            deception_flag,
            llm_explanation,
            evaluation_success,
            evaluation_error,
            transcript_language,
            evaluated_at,
            thumbnail_url,
            video_score,
            combined_score
        FROM video_evaluations
        ORDER BY channel_id, published_at DESC, evaluated_at DESC, created_at DESC
        """,
    )

    by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        row["evaluation_success"] = bool(row.get("evaluation_success"))
        row["deception_flag"] = bool(row.get("deception_flag")) if row.get("deception_flag") is not None else None
        by_channel[row["channel_id"]].append(row)
    return by_channel


def load_channel_avatar_manifest(path: Path = CHANNEL_AVATAR_MANIFEST) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}
    except Exception:
        return {}


def sort_ranked(rows: list[dict[str, Any]], reverse: bool = True) -> list[dict[str, Any]]:
    filtered = [r for r in rows if r.get("videos_in_score", 0) > 0 and r.get("channel_score_ui") is not None]
    filtered.sort(key=lambda r: (r["channel_score_ui"], r.get("subscriber_count") or 0), reverse=reverse)
    for idx, row in enumerate(filtered, start=1):
        row["rank"] = idx
    return filtered


def category_rankings(rows: list[dict[str, Any]], limit_per_category: int = 0) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (row.get("category_id"), row.get("category_name") or "Uncategorized")
        groups[key].append(row)
    out: list[dict[str, Any]] = []
    for (category_id, category_name), group_rows in sorted(groups.items(), key=lambda x: x[0][1]):
        ranked = sort_ranked(group_rows, reverse=True)
        selected = ranked if not limit_per_category or limit_per_category < 1 else ranked[:limit_per_category]
        for i, row in enumerate(selected, start=1):
            item = dict(row)
            item["category_rank"] = i
            item["category_id"] = category_id
            item["category_name"] = category_name
            out.append(item)
    return out


def build_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    channels = compute_channel_aggregates(conn)
    videos_by_channel = fetch_videos_by_channel(conn)
    avatar_manifest = load_channel_avatar_manifest()

    for ch in channels:
        vids = videos_by_channel.get(ch["channel_id"], [])
        scored = [v for v in vids if v.get("evaluation_success") and v.get("video_score") is not None]
        # UI contract: every visible scored video contributes to the displayed channel score.
        ch["videos_scored_visible"] = len(scored)
        ch["channel_score_ui"] = round(sum(v["video_score"] for v in scored) / len(scored), 6) if scored else None
        ch["avg_title_match_ui"] = _avg([_to_float(v.get("title_content_similarity_score")) for v in scored])
        ch["avg_focus_ratio_ui"] = _avg([_to_float(v.get("focus_ratio_pct")) for v in scored])
        ch["avg_time_to_main_fraction_ui"] = _avg([_to_float(v.get("time_to_main_content_fraction")) for v in scored])
        ch["avg_penalty_points_ui"] = _avg([derive_total_penalty_points(v) for v in scored])
        ch["penalty_breakdown_available_ui"] = False
        avatar = avatar_manifest.get(ch["channel_id"], {})
        ch["channel_avatar_url"] = avatar.get("url") or None

    global_english_pool = [c for c in channels if c.get("language") == "en"]
    india_pool = [c for c in channels if c.get("country") == "IN" and c.get("language") in ("en", "hi")]

    global_ranked = sort_ranked([dict(c) for c in global_english_pool], reverse=True)
    india_ranked = sort_ranked([dict(c) for c in india_pool], reverse=True)
    global_categories = category_rankings([dict(c) for c in global_english_pool])
    india_categories = category_rankings([dict(c) for c in india_pool])

    stats = {
        "total_channels": len(channels),
        "scored_channels": sum(1 for c in channels if c.get("channel_score_ui") is not None),
        "india_channels": sum(1 for c in channels if c.get("country") == "IN"),
        "total_videos": sum(len(v) for v in videos_by_channel.values()),
        "successful_evaluations": sum(
            1 for vids in videos_by_channel.values() for v in vids if v.get("evaluation_success") and v.get("video_score") is not None
        ),
    }

    prompts = extract_prompts_from_text_evaluator(TEXT_EVALUATOR_PATH)
    unresolved_curated: list[dict[str, Any]] = []

    if CURATED_CHANNELS_PATH.exists():
        try:
            curated_data = json.loads(CURATED_CHANNELS_PATH.read_text(encoding="utf-8"))
            curated_items = curated_data.get("channels", [])
            db_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for ch in channels:
                for key in (_norm_key(ch.get("title")), _norm_key(ch.get("custom_url"))):
                    if key:
                        db_index[key].append(ch)

            for item in curated_items:
                name = item.get("name") or item.get("title") or ""
                handle = item.get("handle") or ""
                candidates = []
                for key in {_norm_key(name), _norm_key(handle)}:
                    candidates.extend(db_index.get(key, []))
                matched = {c["channel_id"]: c for c in candidates}
                matched_rows = list(matched.values())
                if any(m.get("channel_score_ui") is not None for m in matched_rows):
                    continue
                unresolved_curated.append(
                    {
                        "name": name,
                        "handle": handle,
                        "category": item.get("category"),
                        "status": "unscored_in_db" if matched_rows else "missing_in_db",
                        "db_titles": [m.get("title") for m in matched_rows],
                    }
                )
        except Exception:
            unresolved_curated = []

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "rankings": {
            "global_ranked": global_ranked,
            "india_ranked": india_ranked,
            "global_categories": global_categories,
            "india_categories": india_categories,
        },
        "channels": {c["channel_id"]: c for c in channels},
        "videos_by_channel": videos_by_channel,
        "prompts": prompts,
        "unresolved_curated": unresolved_curated,
        "selection_policy": {
            "channel_score_basis": "average of all stored successful video scores shown on that channel page",
            "video_visibility": "all stored evaluated videos for that channel",
        },
    }


def metric_docs(prompts: dict[str, str]) -> dict[str, dict[str, str]]:
    title_prompt = prompts.get("title") or "Prompt unavailable"
    content_prompt = prompts.get("content") or "Prompt unavailable"
    return {
        "video_score": {
            "label": "Final Video Score",
            "what": "Overall title honesty / anti-clickbait score for a single video. Higher means the title better matches the actual content.",
            "calc": "Average of Title-Content Match, Focus on Promised Topic, and Time to Main Topic (inverted), plus penalties for deception, sensationalism mismatch, and sponsor interruptions.",
            "prompt": "No single prompt. Deterministic formula is applied on top of the title-analysis and content-analysis outputs.",
        },
        "title_content_similarity_score": {
            "label": "Title-Content Match",
            "what": "How accurately the title describes what the video actually delivers (0 to 10).",
            "calc": "LLM judges whether the title promise is clearly delivered, partially delivered, or misleading, based on the transcript.",
            "prompt": title_prompt,
        },
        "focus_ratio_pct": {
            "label": "Focus on Promised Topic",
            "what": "Estimated percentage of the runtime spent on the titled topic (including necessary context).",
            "calc": "LLM estimates what share of the video is on-topic vs filler/off-topic content.",
            "prompt": content_prompt,
        },
        "time_to_main_content_seconds": {
            "label": "Time to Main Topic (Seconds)",
            "what": "How long viewers wait before the video starts sustained discussion of the promised topic.",
            "calc": "LLM scans timestamped transcript segments and marks where the main topic truly begins.",
            "prompt": content_prompt,
        },
        "time_to_main_content_fraction": {
            "label": "Time to Main Topic (% of Video)",
            "what": "The wait time to main topic as a fraction of total duration. Lower is better.",
            "calc": "time_to_main_content_seconds / duration_seconds.",
            "prompt": content_prompt,
        },
        "title_sensationalism_score": {
            "label": "Title Sensationalism Score",
            "what": "How emotionally manipulative or exaggerated the title language is (0 to 10).",
            "calc": "LLM looks at title wording only (not content) for dramatic/vague/manipulative phrasing.",
            "prompt": title_prompt,
        },
        "deception_flag": {
            "label": "Deception Flag",
            "what": "Whether the title makes a concrete claim that the video contradicts.",
            "calc": "Boolean flag set by the title-analysis LLM output.",
            "prompt": title_prompt,
        },
        "sensationalism_mismatch": {
            "label": "Sensationalism Mismatch",
            "what": "Whether the title uses extreme hype while the content itself is ordinary / not equally dramatic.",
            "calc": "Boolean flag from title analysis comparing tone vs transcript reality.",
            "prompt": title_prompt,
        },
        "sponsor_interruption": {
            "label": "Sponsor Interruption",
            "what": "Whether sponsor segments interrupt the main content and how severe that interruption is.",
            "calc": "LLM categorizes sponsor presence as none / minor / excessive from transcript structure.",
            "prompt": content_prompt,
        },
        "penalty_score_aggregate": {
            "label": "Penalty Score (Aggregate)",
            "what": "Total penalty points applied by the scoring formula. More negative means stronger penalty impact.",
            "calc": "Derived as Final Video Score minus the pre-penalty base score. Base score = average of (Title-Content Match*10, Focus on Promised Topic, and inverted Time to Main Topic%).",
            "prompt": "Deterministic derivation from stored metrics and final score. Historical DB rows currently do not persist full per-penalty breakdown fields.",
        },
        "llm_explanation": {
            "label": "Model Explanation",
            "what": "Short natural-language summary explaining the major reasons behind the scores.",
            "calc": "Combined from title-analysis and content-analysis explanations.",
            "prompt": "Generated from the same title and content analysis prompts above.",
        },
    }


def base_style() -> str:
    return """
:root {
  --bg: #f5f5f7;
  --bg-2: #ffffff;
  --panel: #ffffff;
  --panel-2: #fbfbfd;
  --line: #e6e7eb;
  --line-soft: #eef0f4;
  --ink: #111216;
  --muted: #6e7381;
  --muted-2: #9197a5;
  --accent: #ff0033;
  --accent-soft: rgba(255, 0, 51, 0.08);
  --good: #1f9b56;
  --mid: #bb7a15;
  --bad: #d7374f;
  --shadow: 0 10px 36px rgba(17,18,22,.06);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  font-family: "SF Pro Text", "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background:
    radial-gradient(900px 520px at 92% -10%, rgba(255,0,51,.06), transparent 62%),
    radial-gradient(720px 460px at -10% -8%, rgba(0,0,0,.03), transparent 58%),
    linear-gradient(180deg, #f8f8fb 0%, #f3f4f7 100%);
}
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: none; }
.wrap { max-width: 1320px; margin: 0 auto; padding: 36px 24px 80px; }
.hero {
  border: 1px solid var(--line);
  border-radius: 28px;
  padding: 30px;
  background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(250,250,253,.98));
  box-shadow: var(--shadow);
}
.hero h1 { margin: 0; font-size: clamp(32px, 4.4vw, 56px); line-height: .98; letter-spacing: -.04em; font-weight: 700; }
.hero p { margin: 8px 0 0; color: var(--muted); font-size: 14px; line-height: 1.45; max-width: 76ch; }
.hero-subtle {
  margin-top: 14px;
  color: var(--muted-2);
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
}
.hero-subtle .dot::before { content: "• "; color: #c0c4ce; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 7px 12px;
  background: #fff;
  font-size: 12px;
  color: var(--muted);
}
.chip.accent {
  border-color: rgba(255,0,51,.2);
  color: #b11234;
  background: rgba(255,0,51,.08);
}
.panel {
  background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(250,250,253,.98));
  border: 1px solid var(--line);
  border-radius: 24px;
  padding: 22px;
  box-shadow: var(--shadow);
}
.grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; }
@media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
.score-pill { display:inline-block; min-width:62px; text-align:center; border:1px solid currentColor; border-radius:999px; padding:5px 10px; font-weight:800; font-size:12px; letter-spacing:.02em; }
.score-high { color: var(--good); background: rgba(67,209,123,.1); }
.score-mid { color: var(--mid); background: rgba(246,184,79,.1); }
.score-low { color: var(--bad); background: rgba(255,95,114,.1); }
.score-na { color: var(--muted-2); background: rgba(127,133,148,.1); }
table { width:100%; border-collapse: collapse; }
th, td { padding: 9px 6px; border-bottom: 1px solid var(--line-soft); text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
td { font-size: 13px; }
tr:last-child td { border-bottom: none; }
.tabs { display:flex; flex-wrap:wrap; gap:8px; margin: 14px 0; }
.tab {
  border:1px solid var(--line);
  background: rgba(255,255,255,.78);
  color: var(--muted);
  border-radius:999px;
  padding: 10px 14px;
  cursor:pointer;
  font-size:13px;
  font-weight: 600;
}
.tab.active { background: #111216; color: #fff; border-color: #111216; }
.view { display:none; }
.view.active { display:block; }
.category-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.category-card {
  background: linear-gradient(180deg, rgba(255,255,255,.99), rgba(250,250,253,.99));
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 14px;
}
.category-card h3 { margin: 0 0 10px; font-size: 15px; color: var(--ink); letter-spacing: -.02em; }
.category-list { display:grid; gap: 10px; }
.category-row {
  display:grid;
  grid-template-columns: 30px 1fr auto;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  padding: 10px 10px;
  border-radius: 12px;
  background: #fafbfe;
  border: 1px solid var(--line-soft);
}
.muted { color: var(--muted); }
.empty { color: var(--muted); font-style: italic; }
.btn-link {
  display:inline-flex;
  align-items:center;
  gap: 6px;
  border:1px solid var(--line);
  padding:8px 12px;
  border-radius:999px;
  background: #fff;
  color: var(--ink);
  font-size:12px;
  font-weight: 600;
}
.btn-link:hover { border-color: #d5d8e0; background: #fff; }
.table-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
  margin-bottom: 16px;
}
.toolbar-left h2 { margin: 0; font-size: 28px; letter-spacing: -.03em; font-weight: 650; }
.toolbar-left p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.filter-row { display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }
.filter {
  display: grid;
  gap: 4px;
  min-width: 180px;
}
.filter label { color: var(--muted-2); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; }
.filter select {
  appearance: none;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--ink);
  border-radius: 14px;
  padding: 11px 34px 11px 12px;
  font-size: 13px;
  font-weight: 500;
  background-image:
    linear-gradient(45deg, transparent 50%, var(--muted) 50%),
    linear-gradient(135deg, var(--muted) 50%, transparent 50%);
  background-position:
    calc(100% - 16px) calc(50% - 2px),
    calc(100% - 11px) calc(50% - 2px);
  background-size: 5px 5px, 5px 5px;
  background-repeat: no-repeat;
}
.ranking-list { display: grid; gap: 10px; }
.rank-card {
  display: grid;
  grid-template-columns: 64px minmax(260px, 1.8fr) minmax(92px, .6fr) repeat(4, minmax(92px, .7fr));
  gap: 14px;
  align-items: center;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: linear-gradient(180deg, rgba(255,255,255,.99), rgba(249,250,253,.99));
  transition: border-color .15s ease, transform .15s ease, background .15s ease;
}
.rank-card:hover {
  border-color: rgba(255,0,51,.22);
  background: #fff;
  transform: translateY(-2px);
}
@media (max-width: 1180px) {
  .rank-card {
    grid-template-columns: 56px minmax(200px,1.6fr) minmax(92px,.7fr) repeat(2, minmax(86px,.7fr));
  }
  .metric-col.hide-md { display:none; }
}
@media (max-width: 760px) {
  .rank-card {
    grid-template-columns: 44px 1fr auto;
    align-items: start;
    gap: 8px;
  }
  .metric-col, .main-metric-head { display:none; }
}
.rank-pos {
  width: 48px; height: 48px;
  border-radius: 14px;
  border: 1px solid var(--line);
  display: grid; place-items: center;
  color: #404552;
  font-weight: 700;
  background: #fafbfe;
}
.channel-col {
  display: grid;
  grid-template-columns: 56px minmax(0,1fr);
  gap: 12px;
  align-items: center;
  min-width: 0;
}
.channel-avatar {
  width: 56px; height: 56px;
  border-radius: 14px;
  border: 1px solid var(--line);
  object-fit: cover;
  background: #f1f3f7;
}
.channel-avatar.placeholder {
  display: grid; place-items: center;
  color: var(--muted);
  font-size: 11px;
  background: linear-gradient(180deg, #fafbfe, #f4f6fa);
}
.channel-main { min-width: 0; }
.channel-title-line {
  font-size: 17px;
  font-weight: 650;
  color: var(--ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  letter-spacing: -.02em;
}
.channel-subline {
  margin-top: 5px;
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.channel-tags {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.tiny-tag {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 8px;
  font-size: 11px;
  color: var(--muted);
  background: #fafbfe;
}
.score-col { text-align: center; }
.score-col .score-pill { min-width: 84px; font-size: 15px; padding: 9px 12px; border-width: 1px; }
.score-note { margin-top: 6px; color: var(--muted-2); font-size: 11px; }
.metric-col { text-align: center; }
.metric-label-subtle { color: var(--muted-2); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; }
.metric-value-subtle { margin-top: 5px; font-size: 15px; font-weight: 650; color: #1d2230; letter-spacing: -.01em; }
.metric-value-subtle.muted { color: var(--muted); font-weight: 600; }
.metric-head {
  display:grid;
  grid-template-columns: 64px minmax(260px, 1.8fr) minmax(92px, .6fr) repeat(4, minmax(92px, .7fr));
  gap:10px;
  padding: 0 12px 6px;
  color: var(--muted);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: .12em;
}
@media (max-width: 1180px) {
  .metric-head {
    grid-template-columns: 56px minmax(200px,1.6fr) minmax(92px,.7fr) repeat(2, minmax(86px,.7fr));
  }
  .metric-head .hide-md { display:none; }
}
@media (max-width: 760px) { .metric-head { display:none; } }
.metric-head > div { padding: 0 2px; }
.meta-strip {
  margin: 12px 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  color: var(--muted);
  font-size: 12px;
}
.subtle-refresh {
  color: var(--muted-2);
  font-size: 12px;
  margin: 2px 0 18px;
}
.subtle-refresh strong { color: var(--ink); font-weight: 600; }
.no-results {
  border: 1px dashed var(--line);
  border-radius: 16px;
  padding: 24px;
  color: var(--muted);
  background: #fafbfe;
  text-align: center;
}
/* metric info modal */
.metric-dialog {
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 0;
  max-width: min(900px, 94vw);
  width: 94vw;
  background: var(--panel);
  box-shadow: 0 26px 80px rgba(17,18,22,.18);
}
.metric-dialog::backdrop { background: rgba(15,12,10,.45); }
.metric-dialog .dialog-inner { padding: 20px; background: linear-gradient(180deg, var(--panel), var(--bg-2)); }
.metric-dialog h3 { margin: 0 0 6px; font-size: 18px; }
.metric-dialog h4 { margin: 12px 0 6px; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
.metric-dialog p { margin: 0; line-height: 1.6; font-size: 14px; color: #1b1f2a; }
.metric-dialog pre {
  margin: 0;
  white-space: pre-wrap;
  background: #f7f8fb;
  border:1px solid var(--line);
  color: #1d2230;
  border-radius: 10px;
  padding: 10px;
  font-size: 12px;
  line-height: 1.45;
  max-height: 260px;
  overflow: auto;
}
.i-btn {
  border:1px solid #d8dbe3; color:#6c7280; background:#fff; border-radius:999px; width:18px; height:18px;
  font-size:11px; line-height: 1; display:inline-flex; align-items:center; justify-content:center; cursor:pointer;
  margin-left: 6px; vertical-align: middle;
}
.i-btn:hover { border-color: #b8bdca; color: #1b1e28; }
details summary { list-style: none; }
details summary::-webkit-details-marker { display: none; }
"""


def score_class(score: Any) -> str:
    if score is None:
        return "score-na"
    try:
        s = float(score)
    except Exception:
        return "score-na"
    if s >= 70:
        return "score-high"
    if s >= 40:
        return "score-mid"
    return "score-low"


def score_pill(score: Any) -> str:
    if score is None:
        return '<span class="score-pill score-na">N/A</span>'
    return f'<span class="score-pill {score_class(score)}">{float(score):.1f}</span>'


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def format_date(v: Any) -> str:
    if not v:
        return "—"
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return esc(v)


def channel_href(channel_id: str) -> str:
    return f"channels/{channel_page_filename(channel_id)}"


def _to_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _avg(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 6)


def derive_total_penalty_points(video: dict[str, Any]) -> float | None:
    score = _to_float(video.get("video_score"))
    title_match = _to_float(video.get("title_content_similarity_score"))
    focus = _to_float(video.get("focus_ratio_pct"))
    ttm_frac = _to_float(video.get("time_to_main_content_fraction"))
    if None in (score, title_match, focus, ttm_frac):
        return None
    base = ((title_match * 10.0) + focus + ((1.0 - ttm_frac) * 100.0)) / 3.0
    return round(score - base, 4)


def compact_count(v: Any) -> str:
    n = _to_float(v)
    if n is None:
        return "—"
    n = int(n)
    for suffix, factor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(n) >= factor:
            return f"{n / factor:.1f}{suffix}".replace(".0", "")
    return f"{n}"


def fmt_pct_points(v: Any) -> str:
    x = _to_float(v)
    return "—" if x is None else f"{x:.1f}%"


def fmt_match(v: Any) -> str:
    x = _to_float(v)
    return "—" if x is None else f"{x:.1f}/10"


def fmt_time_fraction(v: Any) -> str:
    x = _to_float(v)
    return "—" if x is None else f"{x*100:.1f}%"


def fmt_penalty(v: Any) -> str:
    x = _to_float(v)
    if x is None:
        return "—"
    return f"{x:+.1f}"


def local_channel_avatar_src(channel_id: str, *, page: str) -> str | None:
    local_path = CHANNEL_AVATAR_DIR / f"{channel_id}.jpg"
    if not local_path.exists():
        return None
    prefix = "../" if page == "channel" else ""
    return f"{prefix}assets/channel_avatars/{channel_id}.jpg"


def local_video_thumb_src(video_id: str) -> str | None:
    local_path = VIDEO_THUMB_DIR / f"{video_id}.jpg"
    if not local_path.exists():
        return None
    return f"../assets/video_thumbs/{video_id}.jpg"


def channel_avatar_markup(channel: dict[str, Any], *, page: str, size_cls: str = "") -> str:
    cid = channel.get("channel_id")
    src = (local_channel_avatar_src(cid, page=page) if cid else None) or channel.get("channel_avatar_url")
    if src:
        cls = f"channel-avatar {size_cls}".strip()
        return f'<img class="{cls}" loading="lazy" src="{esc(src)}" alt="{esc(channel.get("title") or "Channel")} avatar" />'
    title = str(channel.get("title") or "?").strip()
    fallback = html.escape((title[:2] or "?").upper())
    return f'<div class="channel-avatar placeholder {size_cls}">{fallback}</div>'


def penalty_breakdown_tooltip_channel(row: dict[str, Any]) -> str:
    parts = [
        "Aggregate penalty points are derived from final score minus pre-penalty base metrics.",
        f"Average aggregate penalty per scored video: {fmt_penalty(row.get('avg_penalty_points_ui'))} points.",
        f"Stored deception flags available: yes (boolean).",
        "Historical DB rows do not store sensationalism mismatch and sponsor interruption fields, so per-penalty component breakdown is unavailable for many rows.",
    ]
    return " ".join(parts)


def render_index_table(rows: list[dict[str, Any]], *, show_lang: bool = False, id_prefix: str = "") -> str:
    del show_lang  # unified card rendering now; language remains available in filters/metadata
    if not rows:
        return '<div class="no-results">No ranked channels yet.</div>'
    country_id = f"{id_prefix}countryFilter"
    category_id = f"{id_prefix}categoryFilter"
    list_id = f"{id_prefix}rankingList"
    empty_id = f"{id_prefix}rankingEmpty"

    categories = sorted({(r.get("category_name") or "Uncategorized") for r in rows})
    countries = sorted({(r.get("country") or "Unknown") for r in rows})
    filter_category_options = "".join(
        f'<option value="{esc(cat)}">{esc(cat)}</option>' for cat in categories
    )
    filter_country_options = "".join(
        f'<option value="{esc(country)}">{esc(country)}</option>' for country in countries
    )

    cards = []
    for r in rows:
        category = r.get("category_name") or "Uncategorized"
        country = r.get("country") or "Unknown"
        channel_col = (
            f'<div class="channel-col">'
            f'{channel_avatar_markup(r, page="index")}'
            f'<div class="channel-main">'
            f'<div class="channel-title-line">{esc(r.get("title") or "Unknown Channel")}</div>'
            f'<div class="channel-subline">{esc(category)} • {esc(country)} • {compact_count(r.get("subscriber_count"))} subs</div>'
            f'</div>'
            f'</div>'
        )
        score_block = (
            f'<div class="score-col">'
            f'{score_pill(r.get("channel_score_ui"))}'
            f'<div class="score-note">n={int(r.get("videos_in_score") or 0)}</div>'
            f'</div>'
        )
        metric_cols = [
            ('Match', fmt_match(r.get("avg_title_match_ui")), ""),
            ('Focus', fmt_pct_points(r.get("avg_focus_ratio_ui")), ""),
            ('Start', fmt_time_fraction(r.get("avg_time_to_main_fraction_ui")), "hide-md"),
            (
                'Penalty',
                fmt_penalty(r.get("avg_penalty_points_ui")),
                'penalty'
            ),
        ]
        metric_html = []
        for label, value, extra in metric_cols:
            tooltip_attr = ""
            classes = "metric-col"
            if extra == "hide-md":
                classes += " hide-md"
            if extra == "penalty":
                tooltip_attr = f' title="{esc(penalty_breakdown_tooltip_channel(r))}"'
            metric_html.append(
                f'<div class="{classes}"{tooltip_attr}>'
                f'<div class="metric-label-subtle">{esc(label)}</div>'
                f'<div class="metric-value-subtle{" muted" if value == "—" else ""}">{esc(value)}</div>'
                f'</div>'
            )

        cards.append(
            f'<a class="rank-card" href="{channel_href(r["channel_id"])}" '
            f'data-category="{esc(category)}" data-country="{esc(country)}">'
            f'<div class="rank-pos">#{int(r["rank"])}</div>'
            f'{channel_col}'
            f'{score_block}'
            f'{"".join(metric_html)}'
            f'</a>'
        )

    return (
        "<div class='filter-row'>"
        f"<div class='filter'><label for='{esc(country_id)}'>Country</label>"
        f"<select id='{esc(country_id)}'><option value=''>All countries</option>"
        f"{filter_country_options}</select></div>"
        f"<div class='filter'><label for='{esc(category_id)}'>Category</label>"
        f"<select id='{esc(category_id)}'><option value=''>All categories</option>"
        f"{filter_category_options}</select></div>"
        "</div>"
        "<div class='metric-head'>"
        "<div>Rank</div><div>Channel</div><div>Final Score</div>"
        "<div>Match</div><div>Focus</div><div class='hide-md'>Start</div><div>Penalty</div>"
        "</div>"
        f"<div id='{esc(list_id)}' class='ranking-list'>{''.join(cards)}</div>"
        f"<div id='{esc(empty_id)}' class='no-results' style='display:none'>No channels match the selected filters yet.</div>"
    )


def render_category_cards(rows: list[dict[str, Any]], *, show_lang: bool = False) -> str:
    if not rows:
        return '<div class="empty">No category rankings available yet.</div>'
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r.get("category_name") or "Uncategorized"].append(r)
    cards = []
    for cat in sorted(groups):
        group = groups[cat]
        items = []
        for r in group:
            lang = f' <span class="muted">({esc(r.get("language"))})</span>' if show_lang and r.get("language") else ""
            items.append(
                f'<div class="category-row"><div class="muted">#{int(r["category_rank"])}</div>'
                f'<div><a href="{channel_href(r["channel_id"])}">{esc(r["title"])}</a>{lang}</div>'
                f'<div>{score_pill(r.get("channel_score_ui"))}</div></div>'
            )
        cards.append(f'<div class="category-card"><h3>{esc(cat)}</h3><div class="category-list">{"".join(items)}</div></div>')
    return f'<div class="category-grid">{"".join(cards)}</div>'


def render_index(snapshot: dict[str, Any]) -> str:
    rankings = snapshot["rankings"]
    stats = snapshot["stats"]
    generated_at = snapshot["generated_at"]
    generated_at_label = format_date(generated_at)
    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Anticlickbait Rankings</title>
  <style>{base_style()}</style>
</head>
<body>
  <div class=\"wrap\">
    <section class=\"hero\">
      <h1>Anticlickbait Rankings</h1>
      <div class=\"hero-subtle\">
        <span>{stats['scored_channels']} channels</span>
        <span class=\"dot\">{stats['successful_evaluations']} videos scored</span>
      </div>
    </section>

    <div class=\"tabs\" role=\"tablist\" aria-label=\"Rankings views\">
      <button class=\"tab active\" data-view=\"global\">Rankings</button>
      <button class=\"tab\" data-view=\"categories-global\">By Category</button>
      <button class=\"tab\" data-view=\"india\">India</button>
    </div>

    <section id=\"view-global\" class=\"view active\">
      <div class=\"panel\">
        <div class=\"table-toolbar\">
          <div class=\"toolbar-left\">
            <h2>Global English Rankings</h2>
          </div>
          <div></div>
        </div>
        <div class=\"subtle-refresh\">Updated <strong>{generated_at_label}</strong></div>
        {render_index_table(rankings['global_ranked'], id_prefix='global-')}
      </div>
    </section>

    <section id=\"view-india\" class=\"view\">
      <div class=\"panel\">
        <div class=\"table-toolbar\">
          <div class=\"toolbar-left\">
            <h2>India</h2>
          </div>
          <div></div>
        </div>
        <div class=\"subtle-refresh\">Updated <strong>{generated_at_label}</strong></div>
        {render_index_table(rankings['india_ranked'], id_prefix='india-')}
      </div>
    </section>

    <section id=\"view-categories-global\" class=\"view\">
      <div class=\"panel\">
        <div class=\"table-toolbar\">
          <div class=\"toolbar-left\">
            <h2>By Category</h2>
          </div>
          <div></div>
        </div>
        {render_category_cards(rankings['global_categories'])}
      </div>
    </section>
  </div>
  <script>
    document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {{
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('view-' + btn.dataset.view).classList.add('active');
    }}));

    document.querySelectorAll('.view').forEach(view => {{
      const country = view.querySelector('select[id$="countryFilter"]');
      const category = view.querySelector('select[id$="categoryFilter"]');
      const list = view.querySelector('div[id$="rankingList"]');
      const empty = view.querySelector('div[id$="rankingEmpty"]');
      if (!country || !category || !list || !empty) return;
      function apply() {{
        const cv = country.value;
        const kv = category.value;
        let visible = 0;
        list.querySelectorAll('.rank-card').forEach(card => {{
          const show = (!cv || card.dataset.country === cv) && (!kv || card.dataset.category === kv);
          card.style.display = show ? '' : 'none';
          if (show) visible += 1;
        }});
        empty.style.display = visible ? 'none' : '';
      }}
      country.addEventListener('change', apply);
      category.addEventListener('change', apply);
      apply();
    }});
  </script>
</body>
</html>"""


def metric_value_display(key: str, video: dict[str, Any]) -> str:
    val = video.get(key)
    if key in {"deception_flag", "sensationalism_mismatch"}:
        if val is None:
            return "—"
        return "Yes" if bool(val) else "No"
    if key == "sponsor_interruption":
        return esc(val or "—")
    if key in {"title_content_similarity_score", "title_sensationalism_score"}:
        return "—" if val is None else f"{float(val):.1f} / 10"
    if key == "focus_ratio_pct":
        return "—" if val is None else f"{float(val):.1f}%"
    if key == "time_to_main_content_seconds":
        return "—" if val is None else f"{float(val):.1f} sec"
    if key == "time_to_main_content_fraction":
        return "—" if val is None else f"{float(val) * 100:.1f}% of runtime"
    if key == "video_score":
        return "—" if val is None else f"{float(val):.1f}"
    if key == "llm_explanation":
        return esc(val or "")
    return esc(val)


def render_metric_rows(video: dict[str, Any], docs: dict[str, dict[str, str]]) -> str:
    order = [
        "video_score",
        "title_content_similarity_score",
        "focus_ratio_pct",
        "time_to_main_content_seconds",
        "time_to_main_content_fraction",
        "title_sensationalism_score",
        "deception_flag",
        "sensationalism_mismatch",
        "sponsor_interruption",
    ]
    rows = []
    for key in order:
        meta = docs[key]
        rows.append(
            f'<div class="metric-row"><div class="metric-label">{esc(meta["label"])}'
            f'<button class="i-btn" type="button" data-metric="{esc(key)}" aria-label="Explain {esc(meta["label"])}">i</button>'
            f'</div><div class="metric-value">{metric_value_display(key, video)}</div></div>'
        )
    return "".join(rows)


def render_channel_page(channel: dict[str, Any], videos: list[dict[str, Any]], docs: dict[str, dict[str, str]], prompts: dict[str, str]) -> str:
    scored = [v for v in videos if v.get("evaluation_success") and v.get("video_score") is not None]
    failed = [v for v in videos if not (v.get("evaluation_success") and v.get("video_score") is not None)]
    channel_score = round(sum(float(v["video_score"]) for v in scored) / len(scored), 3) if scored else None

    # newest first for readability/recency; every visible scored video contributes to the score
    scored.sort(key=lambda v: (v.get("published_at") or "", v.get("evaluated_at") or ""), reverse=True)

    metrics_json = json.dumps(docs, ensure_ascii=False)

    video_cards = []
    for i, v in enumerate(scored, start=1):
        yt_url = f"https://www.youtube.com/watch?v={v['video_id']}"
        report_path = f"../report_{v['video_id']}.html"
        rationale = esc(v.get("llm_explanation") or "No explanation stored")
        metric_rows = render_metric_rows(v, docs)
        transcript_block = (
            "<details class='transcript-box'><summary>Transcript used for scoring (not stored in this DB snapshot)</summary>"
            "<div class='muted' style='font-size:13px;line-height:1.5'>Transcript text was used to compute these scores, but this pilot snapshot only stores metrics and transcript language. "
            "Next step for full transparency is storing transcript text/segments (or a reproducible transcript artifact) per evaluation.</div></details>"
        )
        penalty_total = derive_total_penalty_points(v)
        video_thumb_src = local_video_thumb_src(v["video_id"]) or v.get("thumbnail_url")

        meta_bits = [
            f"{format_date(v.get('published_at'))}",
            f"{format_count(v.get('duration_seconds')) if v.get('duration_seconds') else '—'}s" if v.get('duration_seconds') else "—",
            f"{format_count(v.get('view_count'))} views",
            f"{esc(v.get('transcript_language') or '—')}",
        ]
        summary_metrics = (
            f'<span class="mini-metric"><span>Match</span><strong>{fmt_match(v.get("title_content_similarity_score"))}</strong></span>'
            f'<span class="mini-metric"><span>Focus</span><strong>{fmt_pct_points(v.get("focus_ratio_pct"))}</strong></span>'
            f'<span class="mini-metric"><span>Start</span><strong>{fmt_time_fraction(v.get("time_to_main_content_fraction"))}</strong></span>'
            f'<span class="mini-metric" title="{esc(docs["penalty_score_aggregate"]["calc"])}"><span>Penalty</span><strong>{fmt_penalty(penalty_total)}</strong></span>'
        )

        video_cards.append(
            f"""
            <details class=\"video-accordion\">
              <summary class=\"video-summary\">
                <div class=\"video-summary-grid\">
                  <div class=\"video-thumb-wrap\">
                    {f'<img class="video-thumb" loading="lazy" src="{esc(video_thumb_src)}" alt="{esc(v.get("title") or "Video")} thumbnail" />' if video_thumb_src else '<div class="video-thumb placeholder">No thumbnail</div>'}
                  </div>
                  <div class=\"video-summary-main\">
                    <div class=\"video-kicker\">Video #{i}</div>
                    <h3>{esc(v.get('title') or 'Untitled')}</h3>
                    <div class=\"video-meta-line\">{' • '.join(meta_bits)}</div>
                    <div class=\"summary-metrics\">{summary_metrics}</div>
                  </div>
                  <div class=\"video-summary-score\">
                    {score_pill(v.get('video_score'))}
                    <div class=\"accordion-hint\">Expand</div>
                  </div>
                </div>
              </summary>
              <div class=\"video-body\">
                <div class=\"video-links\">
                  <a class=\"btn-link\" href=\"{yt_url}\" target=\"_blank\" rel=\"noreferrer\">Open on YouTube</a>
                  <a class=\"btn-link\" href=\"{report_path}\" target=\"_blank\">Open local report (if available)</a>
                </div>
                <div class=\"metrics-grid\">{metric_rows}</div>
                <div class=\"explanation-box\">
                  <div class=\"explanation-label\">Model Explanation</div>
                  <div class=\"explanation-text\">{rationale}</div>
                </div>
                {transcript_block}
              </div>
            </details>
            """
        )

    failed_section = ""
    if failed:
        items = []
        for v in failed:
            items.append(
                f"<li><strong>{esc(v.get('title') or 'Untitled')}</strong> <span class='muted'>({format_date(v.get('published_at'))})</span>"
                f"<br><span class='muted'>Not included in channel score: {esc(v.get('evaluation_error') or 'Evaluation failed or score missing')}</span></li>"
            )
        failed_section = (
            "<section class='panel' style='margin-top:12px'>"
            "<h2 style='margin:0 0 8px;font-size:18px'>Evaluated / attempted videos not included in score</h2>"
            "<p class='muted' style='margin:0 0 10px;font-size:13px'>Shown for transparency. These are excluded because a valid final video score was not stored.</p>"
            f"<ul style='margin:0;padding-left:18px;display:grid;gap:8px'>{''.join(items)}</ul></section>"
        )

    page_css = base_style() + """
.channel-header { margin-top: 14px; }
.channel-header-grid { display:grid; grid-template-columns: 96px minmax(0,1fr); gap:18px; align-items:center; }
.channel-title { margin: 0; font-size: clamp(30px, 4vw, 48px); letter-spacing: -.04em; line-height: .98; font-weight: 700; }
.channel-subtitle { margin: 8px 0 0; color: var(--muted); font-size: 14px; line-height: 1.45; }
.channel-avatar.hero-avatar { width: 96px; height: 96px; border-radius: 22px; }
.summary-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap:12px; margin-top:18px; }
.summary-card { background: var(--panel); border:1px solid var(--line); border-radius: 16px; padding: 16px; }
.summary-card .k { color: var(--muted-2); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; }
.summary-card .v { font-size: 28px; font-weight: 650; margin-top: 6px; letter-spacing: -.03em; }
.video-list { display:grid; gap: 18px; margin-top: 22px; }
.video-accordion {
  background: linear-gradient(180deg, rgba(255,255,255,.99), rgba(250,250,253,.99));
  border:1px solid var(--line);
  border-radius: 20px;
  overflow: hidden;
  box-shadow: var(--shadow);
}
.video-accordion[open] { border-color: #d9dde6; }
.video-accordion h3 { margin: 2px 0 0; font-size: 20px; line-height: 1.18; letter-spacing: -.03em; font-weight: 650; }
.video-kicker { color: var(--muted-2); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; }
.video-summary { display:block; cursor:pointer; }
.video-summary-grid {
  display:grid;
  grid-template-columns: 176px minmax(0,1fr) auto;
  gap: 16px;
  align-items: start;
  padding: 18px;
}
@media (max-width: 820px) {
  .video-summary-grid { grid-template-columns: 120px minmax(0,1fr); }
  .video-summary-score { grid-column: 2 / -1; justify-self: start; margin-top: 2px; }
}
@media (max-width: 620px) {
  .video-summary-grid { grid-template-columns: 1fr; }
  .video-thumb-wrap { width: 100%; }
}
.video-thumb-wrap { width: 176px; }
.video-thumb {
  width: 100%;
  aspect-ratio: 16 / 9;
  object-fit: cover;
  border-radius: 14px;
  border: 1px solid var(--line);
  background: #f1f3f7;
}
.video-thumb.placeholder {
  display:grid; place-items:center;
  color: var(--muted);
  font-size: 12px;
  background: linear-gradient(180deg, #fafbfe, #f4f6fa);
}
.video-summary-main { min-width: 0; }
.video-meta-line { margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.45; }
.summary-metrics { margin-top: 12px; display:flex; flex-wrap:wrap; gap:8px; }
.mini-metric {
  border:1px solid var(--line);
  border-radius: 999px;
  padding: 6px 10px;
  background: #fafbfe;
  display:inline-flex;
  align-items:center;
  gap: 8px;
  font-size: 12px;
}
.mini-metric span { color: var(--muted-2); }
.mini-metric strong { font-weight: 700; color: var(--ink); }
.video-summary-score { display:grid; justify-items:end; gap:5px; }
.video-summary-score .score-pill { min-width: 88px; font-size: 16px; padding: 10px 12px; }
.accordion-hint { color: var(--muted-2); font-size: 11px; }
.video-body {
  border-top: 1px solid var(--line-soft);
  padding: 18px;
  background: rgba(255,255,255,.7);
}
.video-links { display:flex; flex-wrap:wrap; gap:10px; margin-top: 2px; }
.metrics-grid { margin-top: 18px; display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.metric-row { border:1px solid var(--line); border-radius: 14px; padding: 14px; background: #fbfbfd; }
.metric-label { font-size: 12px; color: var(--muted); line-height: 1.35; }
.metric-value { margin-top: 6px; font-size: 18px; font-weight: 650; line-height: 1.3; color: var(--ink); letter-spacing: -.02em; }
.explanation-box { margin-top: 14px; border:1px solid var(--line); border-radius: 14px; padding: 14px; background: #fbfbfd; }
.explanation-label { color: var(--muted-2); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; }
.explanation-text { margin-top: 8px; font-size: 14px; line-height: 1.6; color: #1b1f2a; }
.transcript-box { margin-top: 14px; border:1px dashed #d8dbe4; border-radius: 14px; padding: 12px 14px; background: #fafbfe; }
.transcript-box summary { cursor: pointer; font-weight: 600; }
"""

    methods_details = (
        "<details class='panel' style='margin-top:16px'><summary style='cursor:pointer;font-weight:650'>Method & prompts</summary>"
        "<div style='margin-top:10px;display:grid;gap:10px'>"
        "<div><div class='muted' style='font-size:12px;text-transform:uppercase;letter-spacing:.06em'>Title Analysis Prompt</div>"
        f"<pre style='margin-top:6px;white-space:pre-wrap;background:#f7f8fb;border:1px solid var(--line);border-radius:12px;padding:12px;font-size:12px;line-height:1.4;max-height:280px;overflow:auto;color:#1d2230'>{esc(prompts.get('title') or 'Prompt unavailable')}</pre></div>"
        "<div><div class='muted' style='font-size:12px;text-transform:uppercase;letter-spacing:.06em'>Content Analysis Prompt</div>"
        f"<pre style='margin-top:6px;white-space:pre-wrap;background:#f7f8fb;border:1px solid var(--line);border-radius:12px;padding:12px;font-size:12px;line-height:1.4;max-height:280px;overflow:auto;color:#1d2230'>{esc(prompts.get('content') or 'Prompt unavailable')}</pre></div>"
        "</div></details>"
    )

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{esc(channel.get('title') or 'Channel')} • Anticlickbait</title>
  <style>{page_css}</style>
</head>
<body>
  <div class=\"wrap\">
    <div style=\"margin-bottom:10px\"><a class=\"btn-link\" href=\"../rankings_dashboard.html\">← Back to Rankings</a></div>
    <section class=\"hero channel-header\">
      <div class=\"channel-header-grid\">
        {channel_avatar_markup(channel, page="channel", size_cls="hero-avatar")}
        <div>
          <h1 class=\"channel-title\">{esc(channel.get('title') or 'Unknown Channel')}</h1>
          <p class=\"channel-subtitle\">{esc(channel.get('category_name') or 'Uncategorized')} • {esc(channel.get('country') or 'Country unknown')} • {format_count(channel.get('subscriber_count'))} subscribers</p>
        </div>
      </div>
      <div class=\"chips\"><span class=\"chip\">{len(scored)} videos in score</span><span class=\"chip\">{len(videos)} total rows</span></div>
    </section>

    <section class=\"summary-grid\">
      <div class=\"summary-card\"><div class=\"k\">Score</div><div class=\"v\">{('N/A' if channel_score is None else f'{channel_score:.1f}')}</div></div>
      <div class=\"summary-card\"><div class=\"k\">Videos</div><div class=\"v\">{len(scored)}</div></div>
      <div class=\"summary-card\"><div class=\"k\">Subscribers</div><div class=\"v\">{compact_count(channel.get('subscriber_count'))}</div></div>
      <div class=\"summary-card\"><div class=\"k\">Updated</div><div class=\"v\" style=\"font-size:20px\">{format_date(channel.get('last_evaluated_at'))}</div></div>
    </section>

    {methods_details}

    <section class=\"video-list\">
      {''.join(video_cards) if video_cards else '<div class="panel empty">No scored videos available for this channel yet.</div>'}
    </section>
    {failed_section}
  </div>

  <dialog id=\"metricDialog\" class=\"metric-dialog\">
    <div class=\"dialog-inner\">
      <div style=\"display:flex;justify-content:space-between;gap:10px;align-items:center\">
        <h3 id=\"metricDialogTitle\">Metric</h3>
        <button class=\"btn-link\" type=\"button\" onclick=\"document.getElementById('metricDialog').close()\">Close</button>
      </div>
      <h4>What It Means</h4>
      <p id=\"metricDialogWhat\"></p>
      <h4>How It Is Calculated</h4>
      <p id=\"metricDialogCalc\"></p>
      <h4>Prompt Used (Exact)</h4>
      <pre id=\"metricDialogPrompt\"></pre>
    </div>
  </dialog>

  <script>
    const METRIC_DOCS = {metrics_json};
    const dialog = document.getElementById('metricDialog');
    document.querySelectorAll('[data-metric]').forEach(btn => {{
      btn.addEventListener('click', (e) => {{
        e.preventDefault();
        const key = btn.dataset.metric;
        const info = METRIC_DOCS[key];
        if (!info) return;
        document.getElementById('metricDialogTitle').textContent = info.label || key;
        document.getElementById('metricDialogWhat').textContent = info.what || '';
        document.getElementById('metricDialogCalc').textContent = info.calc || '';
        document.getElementById('metricDialogPrompt').textContent = info.prompt || 'Prompt unavailable';
        dialog.showModal();
      }});
    }});
    dialog.addEventListener('click', (e) => {{
      const rect = dialog.getBoundingClientRect();
      const inside = e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom;
      if (!inside) dialog.close();
    }});
    document.querySelectorAll('.video-accordion').forEach((d) => {{
      d.addEventListener('toggle', () => {{
        const hint = d.querySelector('.accordion-hint');
        if (hint) hint.textContent = d.open ? 'Collapse' : 'Expand';
      }});
    }});
  </script>
</body>
</html>"""


def write_channel_pages(snapshot: dict[str, Any], out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    channels = snapshot["channels"]
    videos_by_channel = snapshot["videos_by_channel"]
    docs = metric_docs(snapshot["prompts"])
    ranked_channel_ids = {
        r["channel_id"]
        for key in ("global_ranked", "india_ranked", "global_categories", "india_categories")
        for r in snapshot["rankings"][key]
    }
    count = 0
    for channel_id in sorted(ranked_channel_ids):
        channel = channels.get(channel_id)
        if not channel:
            continue
        page_html = render_channel_page(channel, list(videos_by_channel.get(channel_id, [])), docs, snapshot["prompts"])
        (out_dir / channel_page_filename(channel_id)).write_text(page_html, encoding="utf-8")
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate static anticlickbait rankings dashboard and channel pages")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--channel-dir", type=Path, default=CHANNEL_DIR)
    args = parser.parse_args()

    conn = get_conn(args.db)
    try:
        snapshot = build_snapshot(conn)
    finally:
        conn.close()

    args.out.write_text(render_index(snapshot), encoding="utf-8")
    page_count = write_channel_pages(snapshot, args.channel_dir)

    print(f"Wrote dashboard: {args.out}")
    print(f"Wrote channel pages: {page_count} in {args.channel_dir}")
    print(f"Scored channels: {snapshot['stats']['scored_channels']} / {snapshot['stats']['total_channels']}")


if __name__ == "__main__":
    main()
