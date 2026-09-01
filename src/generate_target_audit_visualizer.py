#!/usr/bin/env python3
"""Generate a category/channel/video audit UI from target manifests + DB state.

Purpose:
- Transparent verification of selected channels and their target video mix.
- Show transcript + evaluation status per video.
- Allow category-wise browsing and channel drill-down pages.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "anticlickbait.db"
DEFAULT_MANIFEST = ROOT / "data" / "targets" / "manifest_v1.jsonl"
DEFAULT_REPLACEMENTS = ROOT / "data" / "targets" / "manifest_replacements_v1.jsonl"
DEFAULT_CHANNELS_FILE = ROOT / "data" / "channels_top25_per_category_launch_final_merged.json"
DEFAULT_CRITERIA_FILE = ROOT / "data" / "channels_final.json"
DEFAULT_OUT = ROOT / "data" / "target_audit_visualizer.html"
DEFAULT_CHANNEL_DIR = ROOT / "data" / "target_audit_channels"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _html(value: Any) -> str:
    s = "" if value is None else str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _fmt_int(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{int(value):,}"
    except Exception:
        return _html(value)


def _load_status_maps(db_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    # Read-only open avoids journal writes when DB is outside writable sandbox roots.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        subtitle_rows = conn.execute(
            """
            SELECT video_id, status, winning_source, language_code, fetched_at, last_failure_code, last_failure_detail
            FROM subtitle_resolution
            """
        ).fetchall()
        eval_rows = conn.execute(
            """
            SELECT video_id, evaluation_success, video_score, transcript_language, evaluated_at, evaluation_error
            FROM video_evaluations
            """
        ).fetchall()
    finally:
        conn.close()

    subtitle_map: dict[str, dict[str, Any]] = {}
    for r in subtitle_rows:
        subtitle_map[r["video_id"]] = dict(r)

    eval_map: dict[str, dict[str, Any]] = {}
    for r in eval_rows:
        eval_map[r["video_id"]] = dict(r)
    return subtitle_map, eval_map


def _channel_metadata(channels_file: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(channels_file)
    out: dict[str, dict[str, Any]] = {}
    for ch in payload.get("channels", []):
        cid = ch.get("channel_id")
        if not cid:
            continue
        out[cid] = {
            "channel_id": cid,
            "category": ch.get("category"),
            "title": ch.get("title"),
            "handle": ch.get("handle"),
            "country": ch.get("country"),
            "subscriber_count": ch.get("subscriber_count"),
            "selected_rank_in_category": ch.get("selected_rank_in_category"),
            "selection_tier": ch.get("selection_tier"),
            "launch_relevance_score": ch.get("launch_relevance_score"),
        }
    return out


def _channel_page_filename(channel_id: str) -> str:
    return f"channel_{channel_id}.html"


def _load_criteria(criteria_file: Path) -> dict[str, Any]:
    if not criteria_file.exists():
        return {"filter_criteria": [], "notes": [], "source": None, "extracted_date": None}
    payload = _read_json(criteria_file)
    return {
        "filter_criteria": payload.get("filter_criteria") or [],
        "notes": payload.get("notes") or [],
        "source": payload.get("source"),
        "extracted_date": payload.get("extracted_date"),
    }


def _build_rows(
    manifest_rows: list[dict[str, Any]],
    replacement_rows: list[dict[str, Any]],
    channel_meta: dict[str, dict[str, Any]],
    subtitle_map: dict[str, dict[str, Any]],
    eval_map: dict[str, dict[str, Any]],
    limit_channel_ids: set[str] | None = None,
    cap_slots_per_channel: int | None = 15,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], list[str]]:
    by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    channel_stats: dict[str, dict[str, Any]] = {}
    categories: set[str] = set()

    all_rows = manifest_rows + replacement_rows
    all_rows.sort(
        key=lambda r: (
            r.get("channel_id") or "",
            int(r.get("selected_rank") or 10**9),
        )
    )
    per_channel_seen: dict[str, int] = defaultdict(int)
    for r in all_rows:
        video_id = r.get("video_id")
        channel_id = r.get("channel_id")
        if not video_id or not channel_id:
            continue
        if limit_channel_ids is not None and channel_id not in limit_channel_ids:
            continue
        if cap_slots_per_channel is not None:
            if per_channel_seen[channel_id] >= cap_slots_per_channel:
                continue
            per_channel_seen[channel_id] += 1

        sub = subtitle_map.get(video_id, {})
        ev = eval_map.get(video_id, {})

        subtitle_ok = sub.get("status") == "resolved"
        eval_ok = bool(ev.get("evaluation_success"))
        clean_ok = subtitle_ok and eval_ok

        row = {
            "video_id": video_id,
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "title": r.get("title"),
            "bucket": r.get("bucket"),
            "bucket_rank": r.get("bucket_rank"),
            "selected_rank": r.get("selected_rank"),
            "published_at": r.get("published_at"),
            "view_count": r.get("view_count"),
            "duration_seconds": r.get("duration_seconds"),
            "replacement_reason": r.get("replacement_reason"),
            "replaces_video_id": r.get("replaces_video_id"),
            "subtitle_status": sub.get("status") or "missing",
            "subtitle_failure_code": sub.get("last_failure_code"),
            "subtitle_language": sub.get("language_code"),
            "eval_success": eval_ok,
            "video_score": ev.get("video_score"),
            "eval_error": ev.get("evaluation_error"),
            "eval_transcript_language": ev.get("transcript_language"),
            "clean_ok": clean_ok,
        }
        by_channel[channel_id].append(row)

        meta = channel_meta.get(channel_id, {})
        category = r.get("category") or meta.get("category") or "Uncategorized"
        categories.add(category)

        st = channel_stats.setdefault(
            channel_id,
            {
                "channel_id": channel_id,
                "title": r.get("channel_title") or meta.get("title") or channel_id,
                "handle": r.get("handle") or meta.get("handle"),
                "category": category,
                "country": r.get("country") or meta.get("country"),
                "subscriber_count": r.get("subscriber_count") or meta.get("subscriber_count"),
                "selected_rank_in_category": meta.get("selected_rank_in_category"),
                "selection_tier": meta.get("selection_tier"),
                "launch_relevance_score": meta.get("launch_relevance_score"),
                "total_targets": 0,
                "latest_targets": 0,
                "popular_targets": 0,
                "subtitle_ok": 0,
                "eval_ok": 0,
                "clean_ok": 0,
            },
        )

        st["total_targets"] += 1
        if row["bucket"] == "latest":
            st["latest_targets"] += 1
        if row["bucket"] == "popular":
            st["popular_targets"] += 1
        if subtitle_ok:
            st["subtitle_ok"] += 1
        if eval_ok:
            st["eval_ok"] += 1
        if clean_ok:
            st["clean_ok"] += 1

    for cid, rows in by_channel.items():
        rows.sort(key=lambda x: int(x.get("selected_rank") or 0))
        st = channel_stats[cid]
        st["all_clean"] = st["clean_ok"] == st["total_targets"] and st["total_targets"] > 0

    return by_channel, channel_stats, sorted(categories)


def _render_index(
    channels: list[dict[str, Any]],
    categories: list[str],
    clean_only: bool,
    channel_dir_name: str,
) -> str:
    tabs = ['<button class="tab active" data-category="ALL">All</button>']
    for cat in categories:
        tabs.append(f'<button class="tab" data-category="{_html(cat)}">{_html(cat)}</button>')

    rows_html = []
    for ch in channels:
        if clean_only and not ch.get("all_clean"):
            continue
        clean_ratio = f'{ch["clean_ok"]}/{ch["total_targets"]}'
        sub_ratio = f'{ch["subtitle_ok"]}/{ch["total_targets"]}'
        eval_ratio = f'{ch["eval_ok"]}/{ch["total_targets"]}'
        mix = f'latest {ch["latest_targets"]} / popular {ch["popular_targets"]}'
        rows_html.append(
            "<tr "
            f'data-category="{_html(ch["category"])}" '
            f'data-clean="{str(bool(ch.get("all_clean"))).lower()}">'
            f"<td>{_html(ch.get('selected_rank_in_category') or '—')}</td>"
            f"<td>{_html(ch['category'])}</td>"
            f"<td><a href=\"{_html(channel_dir_name)}/{_html(_channel_page_filename(ch['channel_id']))}\">{_html(ch['title'])}</a></td>"
            f"<td>{_html(ch.get('handle') or '—')}</td>"
            f"<td>{_html(ch.get('country') or '—')}</td>"
            f"<td>{_fmt_int(ch.get('subscriber_count'))}</td>"
            f"<td>{_html(mix)}</td>"
            f"<td>{_html(clean_ratio)}</td>"
            f"<td>{_html(sub_ratio)}</td>"
            f"<td>{_html(eval_ratio)}</td>"
            "</tr>"
        )

    mode_text = "Clean-only channels (all target videos have transcript + evaluation)." if clean_only else "All channels."
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AntiClickbait Target Audit</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 28px; color:#111; background:#fff; }}
    h1 {{ margin: 0 0 10px; font-size: 28px; }}
    .meta {{ color:#555; margin-bottom:16px; }}
    .tabs {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }}
    .tab {{ border:1px solid #ddd; background:#fff; padding:8px 12px; border-radius:999px; cursor:pointer; }}
    .tab.active {{ background:#111; color:#fff; border-color:#111; }}
    table {{ width:100%; border-collapse: collapse; font-size:14px; }}
    th, td {{ border-bottom:1px solid #eee; text-align:left; padding:10px 8px; vertical-align: top; }}
    th {{ font-size:12px; color:#444; text-transform: uppercase; letter-spacing:.03em; }}
    tr.hidden {{ display:none; }}
    a {{ color:#0b57d0; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <h1>Target Audit Visualizer</h1>
  <div class="meta">{_html(mode_text)}</div>
  <div class="tabs">
    {"".join(tabs)}
  </div>
  <table>
    <thead>
      <tr>
        <th>Rank</th>
        <th>Category</th>
        <th>Channel</th>
        <th>Handle</th>
        <th>Country</th>
        <th>Subscribers</th>
        <th>Target Mix</th>
        <th>Clean</th>
        <th>Transcript</th>
        <th>Eval</th>
      </tr>
    </thead>
    <tbody id="rows">
      {"".join(rows_html)}
    </tbody>
  </table>
  <script>
    const tabs = [...document.querySelectorAll(".tab")];
    const rows = [...document.querySelectorAll("#rows tr")];
    function apply(category) {{
      for (const row of rows) {{
        const cat = row.dataset.category;
        const show = category === "ALL" || cat === category;
        row.classList.toggle("hidden", !show);
      }}
      tabs.forEach(t => t.classList.toggle("active", t.dataset.category === category));
    }}
    tabs.forEach(tab => tab.addEventListener("click", () => apply(tab.dataset.category)));
    apply("ALL");
  </script>
</body>
</html>"""


def _render_channel_page(channel: dict[str, Any], rows: list[dict[str, Any]], back_href: str) -> str:
    video_rows = []
    for r in rows:
        status_text = "clean" if r["clean_ok"] else "issue"
        eval_text = "ok" if r["eval_success"] else (r.get("eval_error") or "missing")
        sub_text = r["subtitle_status"]
        if r.get("subtitle_failure_code"):
            sub_text += f' ({r["subtitle_failure_code"]})'
        video_rows.append(
            "<tr>"
            f"<td>{_html(r.get('selected_rank') or '—')}</td>"
            f"<td>{_html(r.get('bucket') or '—')}</td>"
            f"<td><a href=\"{_html(r['video_url'])}\" target=\"_blank\" rel=\"noreferrer\">{_html(r['title'] or r['video_id'])}</a></td>"
            f"<td>{_html(r.get('published_at') or '—')}</td>"
            f"<td>{_fmt_int(r.get('view_count'))}</td>"
            f"<td>{_html(sub_text)}</td>"
            f"<td>{_html(eval_text)}</td>"
            f"<td>{_html('—' if r.get('video_score') is None else round(float(r['video_score']), 2))}</td>"
            f"<td>{_html(status_text)}</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_html(channel["title"])} - Target Audit</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 28px; color:#111; background:#fff; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    .meta {{ color:#555; margin-bottom:14px; }}
    table {{ width:100%; border-collapse: collapse; font-size:14px; }}
    th, td {{ border-bottom:1px solid #eee; text-align:left; padding:10px 8px; vertical-align: top; }}
    th {{ font-size:12px; color:#444; text-transform: uppercase; letter-spacing:.03em; }}
    a {{ color:#0b57d0; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <div><a href="../{_html(back_href)}">← Back to category/channel list</a></div>
  <h1>{_html(channel["title"])}</h1>
  <div class="meta">
    category: {_html(channel.get("category") or "—")} |
    subscribers: {_fmt_int(channel.get("subscriber_count"))} |
    mix: latest {channel.get("latest_targets", 0)} / popular {channel.get("popular_targets", 0)} |
    clean: {channel.get("clean_ok", 0)}/{channel.get("total_targets", 0)}
  </div>
  <table>
    <thead>
      <tr>
        <th>Slot</th>
        <th>Bucket</th>
        <th>Video</th>
        <th>Published</th>
        <th>Views</th>
        <th>Transcript</th>
        <th>Eval</th>
        <th>Score</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      {"".join(video_rows)}
    </tbody>
  </table>
</body>
</html>"""


def _render_single_html(
    channels: list[dict[str, Any]],
    categories: list[str],
    by_channel: dict[str, list[dict[str, Any]]],
    criteria: dict[str, Any],
    clean_only: bool,
) -> str:
    tabs = ['<button class="tab active" data-category="ALL">All</button>']
    for cat in categories:
        tabs.append(f'<button class="tab" data-category="{_html(cat)}">{_html(cat)}</button>')

    criteria_items = "".join(
        f"<li>{_html(item)}</li>" for item in (criteria.get("filter_criteria") or [])
    )
    note_items = "".join(f"<li>{_html(item)}</li>" for item in (criteria.get("notes") or []))

    channel_blocks: list[str] = []
    for ch in channels:
        if clean_only and not ch.get("all_clean"):
            continue
        cid = ch["channel_id"]
        rows = by_channel.get(cid, [])
        video_rows: list[str] = []
        for r in rows:
            eval_text = "ok" if r["eval_success"] else (r.get("eval_error") or "missing")
            sub_text = r["subtitle_status"]
            if r.get("subtitle_failure_code"):
                sub_text += f' ({r["subtitle_failure_code"]})'
            status = "clean" if r["clean_ok"] else "issue"
            video_rows.append(
                "<tr>"
                f"<td>{_html(r.get('selected_rank') or '—')}</td>"
                f"<td>{_html(r.get('bucket') or '—')}</td>"
                f"<td><a href=\"{_html(r['video_url'])}\" target=\"_blank\" rel=\"noreferrer\">{_html(r.get('title') or r['video_id'])}</a></td>"
                f"<td>{_html(r.get('published_at') or '—')}</td>"
                f"<td>{_fmt_int(r.get('view_count'))}</td>"
                f"<td>{_html(sub_text)}</td>"
                f"<td>{_html(eval_text)}</td>"
                f"<td>{_html('—' if r.get('video_score') is None else round(float(r['video_score']), 2))}</td>"
                f"<td>{_html(status)}</td>"
                "</tr>"
            )

        mix = f'latest {ch["latest_targets"]} / popular {ch["popular_targets"]}'
        channel_blocks.append(
            "<section "
            f'class="channel-card" data-category="{_html(ch.get("category") or "Uncategorized")}">'
            "<details>"
            "<summary>"
            f"<span class='rank'>#{_html(ch.get('selected_rank_in_category') or '—')}</span>"
            f"<span class='title'>{_html(ch.get('title') or ch['channel_id'])}</span>"
            f"<span class='meta'>{_html(ch.get('category') or 'Uncategorized')} | {_html(ch.get('country') or '—')} | {_fmt_int(ch.get('subscriber_count'))} subs | {mix} | clean {ch.get('clean_ok', 0)}/{ch.get('total_targets', 0)}</span>"
            "</summary>"
            "<div class='table-wrap'>"
            "<table>"
            "<thead><tr>"
            "<th>Slot</th><th>Bucket</th><th>Video</th><th>Published</th><th>Views</th><th>Transcript</th><th>Eval</th><th>Score</th><th>Status</th>"
            "</tr></thead>"
            f"<tbody>{''.join(video_rows)}</tbody>"
            "</table>"
            "</div>"
            "</details>"
            "</section>"
        )

    mode_text = "Clean-only channels (all target videos have transcript + evaluation)." if clean_only else "All channels."
    criteria_source = criteria.get("source") or "channels_final.json"
    criteria_date = criteria.get("extracted_date") or "unknown"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AntiClickbait Target Audit (Single File)</title>
  <style>
    :root {{
      --bg:#fff;
      --ink:#111;
      --muted:#555;
      --line:#e7e7e7;
      --pill:#f3f3f3;
    }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 26px; color:var(--ink); background:var(--bg); }}
    h1 {{ margin: 0 0 6px; font-size: 28px; }}
    .meta {{ color:var(--muted); margin-bottom:14px; }}
    .criteria {{
      border:1px solid var(--line);
      border-radius:12px;
      padding:14px 16px;
      margin-bottom:16px;
      background:#fcfcfc;
    }}
    .criteria h2 {{ margin:0 0 8px; font-size:15px; }}
    .criteria .sub {{ margin:0 0 8px; color:var(--muted); font-size:13px; }}
    .criteria ul {{ margin: 0 0 10px 18px; padding:0; }}
    .criteria li {{ margin: 3px 0; }}
    .tabs {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }}
    .tab {{ border:1px solid var(--line); background:var(--pill); padding:8px 12px; border-radius:999px; cursor:pointer; }}
    .tab.active {{ background:#111; color:#fff; border-color:#111; }}
    .channels {{ display:grid; gap:10px; }}
    .channel-card {{ border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
    details {{ background:#fff; }}
    summary {{ list-style:none; cursor:pointer; padding:12px 14px; display:grid; gap:4px; }}
    summary::-webkit-details-marker {{ display:none; }}
    .rank {{ font-size:12px; color:var(--muted); }}
    .title {{ font-size:17px; font-weight:600; line-height:1.2; }}
    .meta {{ font-size:13px; color:var(--muted); margin:0; }}
    .table-wrap {{ overflow:auto; border-top:1px solid var(--line); }}
    table {{ width:100%; border-collapse: collapse; font-size:13px; min-width:980px; }}
    th, td {{ border-bottom:1px solid #f1f1f1; text-align:left; padding:9px 8px; vertical-align:top; }}
    th {{ font-size:11px; color:#666; text-transform: uppercase; letter-spacing:.03em; background:#fafafa; position:sticky; top:0; }}
    a {{ color:#0b57d0; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    .hidden {{ display:none; }}
  </style>
</head>
<body>
  <h1>Target Audit Visualizer</h1>
  <div class="meta">{_html(mode_text)}</div>

  <section class="criteria">
    <h2>Original Channel Criteria (Project Docs)</h2>
    <p class="sub">Source: {_html(criteria_source)} | Extracted: {_html(criteria_date)}</p>
    <ul>{criteria_items}</ul>
    <h2 style="margin-top:8px;">Notes</h2>
    <ul>{note_items}</ul>
  </section>

  <div class="tabs">
    {"".join(tabs)}
  </div>

  <main id="channels" class="channels">
    {"".join(channel_blocks)}
  </main>

  <script>
    const tabs = [...document.querySelectorAll(".tab")];
    const cards = [...document.querySelectorAll(".channel-card")];
    function apply(category) {{
      for (const card of cards) {{
        const cat = card.dataset.category || "";
        const show = category === "ALL" || cat === category;
        card.classList.toggle("hidden", !show);
      }}
      for (const t of tabs) {{
        t.classList.toggle("active", t.dataset.category === category);
      }}
    }}
    for (const t of tabs) {{
      t.addEventListener("click", () => apply(t.dataset.category));
    }}
    apply("ALL");
  </script>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate target audit visualizer (category -> channel -> videos).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--replacements", type=Path, default=DEFAULT_REPLACEMENTS)
    parser.add_argument("--channels-file", type=Path, default=DEFAULT_CHANNELS_FILE)
    parser.add_argument("--criteria-file", type=Path, default=DEFAULT_CRITERIA_FILE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--channel-dir", type=Path, default=DEFAULT_CHANNEL_DIR)
    parser.add_argument("--single-file", action="store_true", help="Write one self-contained HTML (category tabs + nested channel->videos).")
    parser.add_argument("--clean-only", action="store_true", help="Show only fully clean channels on index page.")
    parser.add_argument(
        "--limit-to-channel-file",
        action="store_true",
        help="Limit index/channel pages to channel IDs present in --channels-file.",
    )
    parser.add_argument(
        "--cap-slots-per-channel",
        type=int,
        default=15,
        help="Only include the first N selected slots per channel from manifest+replacements.",
    )
    args = parser.parse_args()

    manifest_rows = _read_jsonl(args.manifest)
    replacement_rows = _read_jsonl(args.replacements)
    channel_meta = _channel_metadata(args.channels_file)
    criteria = _load_criteria(args.criteria_file)
    subtitle_map, eval_map = _load_status_maps(args.db)

    by_channel, channel_stats, categories = _build_rows(
        manifest_rows=manifest_rows,
        replacement_rows=replacement_rows,
        channel_meta=channel_meta,
        subtitle_map=subtitle_map,
        eval_map=eval_map,
        limit_channel_ids=(set(channel_meta.keys()) if args.limit_to_channel_file else None),
        cap_slots_per_channel=(None if args.cap_slots_per_channel <= 0 else args.cap_slots_per_channel),
    )

    channels = list(channel_stats.values())
    channels.sort(
        key=lambda c: (
            c.get("category") or "",
            int(c.get("selected_rank_in_category") or 10**6),
            -(int(c.get("subscriber_count") or 0)),
            c.get("title") or "",
        )
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if not args.single_file:
        args.channel_dir.mkdir(parents=True, exist_ok=True)

    if args.single_file:
        args.out.write_text(
            _render_single_html(
                channels=channels,
                categories=categories,
                by_channel=by_channel,
                criteria=criteria,
                clean_only=bool(args.clean_only),
            ),
            encoding="utf-8",
        )
        print(f"Wrote single-file visualizer: {args.out}")
        return 0

    args.out.write_text(
        _render_index(
            channels,
            categories,
            clean_only=bool(args.clean_only),
            channel_dir_name=args.channel_dir.name,
        ),
        encoding="utf-8",
    )

    page_count = 0
    for ch in channels:
        rows = by_channel.get(ch["channel_id"], [])
        page = _render_channel_page(ch, rows, back_href=args.out.name)
        out_path = args.channel_dir / _channel_page_filename(ch["channel_id"])
        out_path.write_text(page, encoding="utf-8")
        page_count += 1

    print(f"Wrote index: {args.out}")
    print(f"Wrote channel pages: {page_count} in {args.channel_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
