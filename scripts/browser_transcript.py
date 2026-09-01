"""
Fetch YouTube transcripts via Playwright browser automation.

Bypasses IP-based 429 blocks on YouTube's timedtext endpoint by:
1. Using a real Chromium browser (handles BotGuard/PoTokens automatically)
2. Opening the transcript panel and scraping rendered segments
3. Supporting HTTP/SOCKS5 proxy to route around IP bans

Usage:
    from scripts.browser_transcript import fetch_transcript

    # Direct (works when IP is not blocked)
    segments = fetch_transcript("video_id")

    # Via proxy (when IP is blocked)
    segments = fetch_transcript("video_id", proxy="socks5://user:pass@host:port")

    # Each segment: {"start": float, "duration": float, "text": str}
"""

import os
import re
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout


def fetch_transcript(
    video_id: str,
    lang: str = "en",
    headless: bool = True,
    proxy: str | None = None,
    timeout_ms: int = 30000,
    max_wait_seconds: int = 20,
) -> list[dict]:
    """
    Fetch transcript segments for a YouTube video using Playwright.

    Args:
        video_id: YouTube video ID
        lang: Preferred language code
        headless: Run browser headless
        proxy: Optional proxy URL (http://, socks5://)
        timeout_ms: Page navigation timeout
        max_wait_seconds: Max seconds to wait for transcript segments to render

    Returns:
        List of dicts with keys: start (float), duration (float), text (str)

    Raises:
        RuntimeError: If no transcript can be extracted
    """
    if proxy is None:
        proxy = os.environ.get("TRANSCRIPT_PROXY", "").strip() or None

    url = f"https://www.youtube.com/watch?v={video_id}"
    intercepted_segments = []

    with sync_playwright() as p:
        launch_args = ['--disable-blink-features=AutomationControlled']
        launch_kwargs = dict(headless=headless, args=launch_args)
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # Intercept successful timedtext / get_transcript responses
        def on_response(response):
            u = response.url
            try:
                if 'get_transcript' in u and response.status == 200:
                    segs = _parse_innertube_transcript(response.json())
                    if segs:
                        intercepted_segments.extend(segs)
                elif 'timedtext' in u and response.status == 200:
                    segs = _parse_timedtext_xml(response.text())
                    if segs:
                        intercepted_segments.extend(segs)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(5000)

            # Open transcript panel
            _open_transcript_panel(page)

            # Check intercepted data first (fastest path)
            if intercepted_segments:
                return intercepted_segments

            # Poll for rendered segments
            for _ in range(max_wait_seconds):
                page.wait_for_timeout(1000)
                if intercepted_segments:
                    return intercepted_segments
                segments = _scrape_transcript_panel(page)
                if segments:
                    return segments

            # Final attempt: direct timedtext fetch
            segments = _try_direct_timedtext(page, lang)
            if segments:
                return segments

            raise RuntimeError(
                f"Could not extract transcript for video {video_id}. "
                "YouTube's timedtext API is likely returning 429 (IP blocked). "
                "Set TRANSCRIPT_PROXY env var or pass proxy= argument."
            )

        finally:
            browser.close()


def _open_transcript_panel(page) -> bool:
    """Open the YouTube transcript panel via UI clicks."""
    try:
        expand = page.locator('#expand').first
        expand.scroll_into_view_if_needed(timeout=3000)
        expand.click(timeout=5000)
        page.wait_for_timeout(1500)
    except PwTimeout:
        pass

    try:
        btn = page.locator('button[aria-label="Show transcript"]').first
        btn.scroll_into_view_if_needed(timeout=3000)
        btn.click(timeout=5000)
        return True
    except PwTimeout:
        return False


def _scrape_transcript_panel(page) -> list[dict] | None:
    """Scrape transcript segments from the rendered transcript panel."""
    segments_data = page.evaluate("""
        () => {
            const segments = [];
            const els = document.querySelectorAll('ytd-transcript-segment-renderer');
            for (const el of els) {
                const timeEl = el.querySelector('.segment-timestamp');
                const textEl = el.querySelector('yt-formatted-string.segment-text');
                if (timeEl && textEl) {
                    segments.push({
                        timeStr: timeEl.textContent.trim(),
                        text: textEl.textContent.trim(),
                    });
                }
            }
            if (segments.length > 0) return segments;

            // Fallback: broader selectors
            const els2 = document.querySelectorAll(
                'ytd-transcript-segment-renderer, yt-transcript-segment-renderer'
            );
            for (const el of els2) {
                const timeEl = el.querySelector('[class*="timestamp"]');
                const textEl = el.querySelector('[class*="segment-text"], yt-formatted-string');
                if (timeEl && textEl) {
                    segments.push({
                        timeStr: timeEl.textContent.trim(),
                        text: textEl.textContent.trim(),
                    });
                }
            }
            return segments.length > 0 ? segments : null;
        }
    """)

    if not segments_data:
        return None

    result = []
    for seg in segments_data:
        start = _parse_timestamp(seg["timeStr"])
        text = seg["text"].replace("\n", " ").strip()
        if text:
            result.append({"start": start, "duration": 0.0, "text": text})

    # Estimate durations from consecutive start times
    for i in range(len(result) - 1):
        result[i]["duration"] = round(result[i + 1]["start"] - result[i]["start"], 3)
    if result:
        result[-1]["duration"] = 5.0

    return result if result else None


def _try_direct_timedtext(page, lang: str) -> list[dict] | None:
    """Try fetching timedtext URL directly via JS fetch."""
    try:
        resp = page.evaluate(r"""
            async (lang) => {
                const pr = window.ytInitialPlayerResponse;
                if (!pr) return null;
                const tracks = pr?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
                if (!tracks || !tracks.length) return null;
                let url = null;
                for (const t of tracks) {
                    if (t.languageCode === lang) { url = t.baseUrl; break; }
                }
                if (!url) {
                    for (const t of tracks) {
                        if (t.languageCode?.startsWith(lang)) { url = t.baseUrl; break; }
                    }
                }
                if (!url) url = tracks[0].baseUrl;
                const r = await fetch(url, {credentials: 'include'});
                if (!r.ok) return null;
                return await r.text();
            }
        """, lang)
        if resp:
            return _parse_timedtext_xml(resp)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_innertube_transcript(data: dict) -> list[dict] | None:
    """Parse transcript from InnerTube get_transcript response."""
    try:
        for action in data.get("actions", []):
            panel = action.get("updateEngagementPanelAction", {}).get("content", {})

            # Structure 1: transcriptRenderer > cueGroups
            cue_groups = (
                panel.get("transcriptRenderer", {})
                .get("body", {})
                .get("transcriptBodyRenderer", {})
                .get("cueGroups", [])
            )
            if cue_groups:
                segments = []
                for group in cue_groups:
                    for cue in group.get("transcriptCueGroupRenderer", {}).get("cues", []):
                        r = cue.get("transcriptCueRenderer", {})
                        text = r.get("cue", {}).get("simpleText", "").strip()
                        if text:
                            segments.append({
                                "start": round(int(r.get("startOffsetMs", 0)) / 1000.0, 3),
                                "duration": round(int(r.get("durationMs", 0)) / 1000.0, 3),
                                "text": text,
                            })
                if segments:
                    return segments

            # Structure 2: transcriptSearchPanelRenderer > initialSegments
            initial = (
                panel.get("transcriptSearchPanelRenderer", {})
                .get("body", {})
                .get("transcriptSegmentListRenderer", {})
                .get("initialSegments", [])
            )
            if initial:
                segments = []
                for seg in initial:
                    r = seg.get("transcriptSegmentRenderer", {})
                    runs = r.get("snippet", {}).get("runs", [])
                    text = " ".join(run.get("text", "") for run in runs).strip()
                    if text:
                        start_ms = int(r.get("startMs", 0))
                        end_ms = int(r.get("endMs", 0))
                        segments.append({
                            "start": round(start_ms / 1000.0, 3),
                            "duration": round((end_ms - start_ms) / 1000.0, 3),
                            "text": text,
                        })
                if segments:
                    return segments
    except Exception:
        pass
    return None


def _parse_timedtext_xml(xml_text: str) -> list[dict] | None:
    """Parse YouTube timedtext XML into segment dicts."""
    import xml.etree.ElementTree as ET
    from html import unescape

    for tag in ["transcript", "timedtext"]:
        match = re.search(rf"<{tag}[\s>].*</{tag}>", xml_text, re.DOTALL)
        if match:
            xml_text = match.group(0)
            break

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    segments = []
    for elem in root.iter("text"):
        start = float(elem.get("start", 0))
        dur = float(elem.get("dur", 0))
        text = unescape(elem.text or "").replace("\n", " ").strip()
        if text:
            segments.append({
                "start": round(start, 3),
                "duration": round(dur, 3),
                "text": text,
            })
    return segments if segments else None


def _parse_timestamp(ts: str) -> float:
    """Parse '0:00', '1:23', '1:02:33' into seconds."""
    parts = [p.strip() for p in ts.strip().split(":")]
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        pass
    return 0.0


if __name__ == "__main__":
    import sys

    vid = sys.argv[1] if len(sys.argv) > 1 else "s2ZTZrghxvg"
    print(f"Fetching transcript for video: {vid}")

    try:
        segments = fetch_transcript(vid)
        print(f"\nGot {len(segments)} segments")
        print(f"\nFirst 5 segments:")
        for seg in segments[:5]:
            print(f"  [{seg['start']:>8.1f}s] {seg['text'][:80]}")
        print(f"\nLast 3 segments:")
        for seg in segments[-3:]:
            print(f"  [{seg['start']:>8.1f}s] {seg['text'][:80]}")

        full_text = " ".join(s["text"] for s in segments)
        print(f"\nTotal text length: {len(full_text)} chars")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
