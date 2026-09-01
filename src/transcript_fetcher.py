"""
Transcript fetcher using Invidious API (primary) with yt-dlp fallback.

Hardening goals:
- Fail fast on terminal transcript-missing cases (avoid expensive fallback work).
- Bound Invidious retries per video.
- Gate yt-dlp fallback concurrency to avoid stampede/rate-limit collapse at high worker counts.
"""

import os
import re
import random
import time
import threading

import requests
import yt_dlp


# Public Invidious instances. These proxy YouTube from their own IPs.
# Full list: https://docs.invidious.io/instances/
INVIDIOUS_INSTANCES = [
    "https://invidious.nerdvpn.de",
    "https://yt.artemislena.eu",
    "https://invidious.fdn.fr",
    "https://invidious.privacyredirect.com",
    "https://yewtu.be",
    "https://inv.nadeko.net",
    "https://iv.melmac.space",
    "https://invidious.lunar.icu",
    "https://invidious.namazso.eu",
    "https://invidious.io",
]


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return max(minimum, value)
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return max(minimum, value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


INSTANCE_TIMEOUT = _env_float("INVIDIOUS_INSTANCE_TIMEOUT_SECONDS", 4.0, minimum=0.5)
MAX_INVIDIOUS_INSTANCES_PER_VIDEO = _env_int("TRANSCRIPT_INVIDIOUS_MAX_INSTANCES", 3)
INVIDIOUS_ENABLED = _env_bool("TRANSCRIPT_INVIDIOUS_ENABLED", True)

LANGUAGE_PRESETS = {
    "english": ["en", "en-US", "en-GB"],
    "hindi": ["hi", "en", "en-US", "en-GB"],
    "india": ["hi", "en", "en-US", "en-GB"],
}

# yt-dlp fallback config (from env)
_proxy_list = [p.strip() for p in os.environ.get("TRANSCRIPT_PROXY_LIST", "").split(",") if p.strip()]
_single_proxy = os.environ.get("TRANSCRIPT_PROXY", "").strip()
if _single_proxy and _single_proxy not in _proxy_list:
    _proxy_list.append(_single_proxy)

_browser_cookies = os.environ.get("TRANSCRIPT_COOKIES_BROWSER", "").strip()
if _browser_cookies.lower() in {"", "0", "false", "off", "none"}:
    _browser_cookies = ""
_cookies_file = os.environ.get("TRANSCRIPT_COOKIES_FILE", "").strip()

# Safety default: browser-cookie mode is disabled unless explicitly opted in.
# This avoids macOS keychain prompts (e.g., Chrome Safe Storage) during unattended runs.
_allow_browser_cookies = _env_bool("TRANSCRIPT_ENABLE_BROWSER_COOKIES", False)
if not _allow_browser_cookies:
    _browser_cookies = ""
    _cookies_file = ""

YTDLP_MAX_CONCURRENCY = _env_int("TRANSCRIPT_YTDLP_MAX_CONCURRENCY", 4)
YTDLP_MAX_RETRIES = _env_int("TRANSCRIPT_YTDLP_MAX_RETRIES", 2)
YTDLP_RETRY_BACKOFF_SECONDS = _env_float("TRANSCRIPT_YTDLP_RETRY_BACKOFF_SECONDS", 2.0, minimum=0.1)
_YTDLP_SEMAPHORE = threading.Semaphore(YTDLP_MAX_CONCURRENCY)

ERROR_NO_TRANSCRIPT = "no_transcript_available"
ERROR_NO_REQUESTED_LANGUAGE = "no_requested_language"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_VIDEO_UNAVAILABLE = "video_unavailable"


def _get_proxy() -> str | None:
    return random.choice(_proxy_list) if _proxy_list else None


# ---------------------------------------------------------------------------
# VTT parser (shared between Invidious and yt-dlp paths)
# ---------------------------------------------------------------------------

def _parse_vtt(content: str) -> tuple[list[dict], str]:
    """
    Parse VTT subtitle content into segments and full text.
    Deduplicates YouTube's rolling auto-generated captions.
    """
    timestamp_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
    )

    def _ts_to_sec(ts: str) -> float:
        ts = ts.replace(",", ".")
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    def _clean(raw: str) -> str:
        import html
        return " ".join(html.unescape(re.sub(r"<[^>]+>", "", raw)).split())

    raw_segments = []

    for block in re.split(r"\n\s*\n", content):
        lines = [l.strip() for l in block.strip().splitlines()]
        ts_line, ts_idx = None, None
        for i, line in enumerate(lines):
            if timestamp_re.search(line):
                ts_line, ts_idx = line, i
                break
        if ts_line is None:
            continue

        m = timestamp_re.search(ts_line)
        start = _ts_to_sec(m.group(1))
        duration = round(_ts_to_sec(m.group(2)) - start, 3)
        text = _clean(" ".join(lines[ts_idx + 1 :]))

        if not text:
            continue

        raw_segments.append({"start": start, "duration": duration, "text": text})

    # Deduplicate rolling-window auto-captions.
    # Pattern: consecutive segments at the same timestamp where the longer
    # line extends the shorter one. Keep only the longest version per timestamp.
    # Then remove cross-timestamp overlaps where line N+1's text is fully
    # contained within line N's text.
    merged = []
    for seg in raw_segments:
        if merged and abs(merged[-1]["start"] - seg["start"]) < 0.01:
            prev = merged[-1]["text"]
            cur = seg["text"]
            if cur.startswith(prev) or prev.startswith(cur):
                # Keep the longer one
                if len(cur) >= len(prev):
                    merged[-1] = seg
                continue
        merged.append(seg)

    # Remove cross-timestamp overlaps from rolling-window auto-captions.
    # Two cases:
    #   1. cur is a substring of prev → leftover fragment, skip cur
    #   2. prev is a prefix of cur → rolling extension, replace prev with cur
    segments = []
    for seg in merged:
        if segments:
            prev = segments[-1]["text"]
            cur = seg["text"]
            if cur == prev or cur in prev:
                continue
            if prev and cur.startswith(prev):
                segments[-1] = seg
                continue
        segments.append(seg)

    text = "\n".join(f"[{s['start']:.1f}s] {s['text']}" for s in segments)
    return segments, text


# ---------------------------------------------------------------------------
# Primary: Invidious
# ---------------------------------------------------------------------------

def _fetch_via_invidious(video_id: str, language_codes: list[str]) -> dict:
    """
    Fetch transcript via a subset of Invidious instances.
    Returns terminal error codes for no-transcript/no-language cases.
    """
    empty = dict(
        video_id=video_id,
        transcript_text="",
        segments=[],
        language=None,
        language_code=None,
        is_generated=None,
    )

    if not INVIDIOUS_ENABLED:
        return {**empty, "success": False, "error": "Invidious disabled", "error_code": None}

    instances = INVIDIOUS_INSTANCES.copy()
    random.shuffle(instances)
    if MAX_INVIDIOUS_INSTANCES_PER_VIDEO > 0:
        instances = instances[:MAX_INVIDIOUS_INSTANCES_PER_VIDEO]

    last_error = "All Invidious instances failed"

    for instance in instances:
        try:
            # 1) List available caption tracks
            r = requests.get(
                f"{instance}/api/v1/captions/{video_id}",
                timeout=INSTANCE_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 429:
                last_error = f"Rate limited on {instance}"
                continue
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code} from {instance}"
                continue

            captions = r.json().get("captions", [])
            if not captions:
                return {
                    **empty,
                    "success": False,
                    "error": "No transcript available for this video",
                    "error_code": ERROR_NO_TRANSCRIPT,
                }

            # 2) Pick best language match
            chosen, chosen_lang, is_generated = None, None, False
            for lang_pref in language_codes:
                prefix = lang_pref.split("-")[0].lower()
                for cap in captions:
                    cap_lang = (cap.get("languageCode") or cap.get("language_code") or "").lower()
                    label = cap.get("label", "").lower()
                    if cap_lang.startswith(prefix) or prefix in label:
                        chosen = cap
                        chosen_lang = cap.get("languageCode") or cap.get("language_code") or lang_pref
                        is_generated = "auto" in label or "generated" in label
                        break
                if chosen:
                    break

            if not chosen:
                return {
                    **empty,
                    "success": False,
                    "error": f"No transcript in languages: {language_codes}",
                    "error_code": ERROR_NO_REQUESTED_LANGUAGE,
                }

            # 3) Fetch caption content as VTT
            cap_url = chosen.get("url", "")
            if cap_url.startswith("/"):
                cap_url = f"{instance}{cap_url}"
            if "format=" in cap_url:
                cap_url = re.sub(r"format=[^&]+", "format=vtt", cap_url)
            else:
                cap_url += ("&" if "?" in cap_url else "?") + "format=vtt"

            cr = requests.get(cap_url, timeout=INSTANCE_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            if cr.status_code != 200:
                last_error = f"Caption download failed: HTTP {cr.status_code} from {instance}"
                continue

            segments, full_text = _parse_vtt(cr.text)
            if not segments:
                last_error = f"Transcript empty after parsing ({instance})"
                continue

            return {
                "success": True,
                "video_id": video_id,
                "transcript_text": full_text,
                "segments": segments,
                "language": chosen_lang,
                "language_code": chosen_lang,
                "is_generated": is_generated,
                "error": None,
                "error_code": None,
            }

        except requests.Timeout:
            last_error = f"Timeout on {instance}"
        except requests.RequestException as e:
            last_error = f"Request error ({instance}): {e}"
        except Exception as e:
            last_error = f"Unexpected error ({instance}): {e}"

    return {**empty, "success": False, "error": last_error, "error_code": None}


# ---------------------------------------------------------------------------
# Fallback: yt-dlp
# ---------------------------------------------------------------------------

def _fetch_via_ytdlp(video_id: str, language_codes: list[str]) -> dict:
    """yt-dlp fallback — guarded by global semaphore + bounded retries."""
    return _fetch_via_ytdlp_mode(
        video_id,
        language_codes,
        source_name="yt_dlp_default",
        use_proxy_pool=True,
        force_proxy=None,
        cookies_browser=_browser_cookies or None,
        cookies_file=_cookies_file or None,
    )


def _fetch_via_ytdlp_mode(
    video_id: str,
    language_codes: list[str],
    *,
    source_name: str,
    use_proxy_pool: bool,
    force_proxy: str | None,
    cookies_browser: str | None,
    cookies_file: str | None,
) -> dict:
    """yt-dlp fetch variant used by explicit source-tier pipeline."""
    import tempfile
    from pathlib import Path

    empty = dict(
        video_id=video_id,
        transcript_text="",
        segments=[],
        language=None,
        language_code=None,
        is_generated=None,
        source=source_name,
        auth_mode="bot_session" if (cookies_browser or cookies_file) else "public",
    )

    url = f"https://www.youtube.com/watch?v={video_id}"

    with _YTDLP_SEMAPHORE:
        last_error = "yt-dlp fallback failed"

        for attempt in range(1, YTDLP_MAX_RETRIES + 1):
            proxy = force_proxy if force_proxy else (_get_proxy() if use_proxy_pool else None)
            with tempfile.TemporaryDirectory() as tmpdir:
                ydl_opts = {
                    "skip_download": True,
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": language_codes,
                    "subtitlesformat": "vtt",
                    # Do not inherit user/global yt-dlp config (can force unavailable formats).
                    "ignoreconfig": True,
                    "outtmpl": f"{tmpdir}/%(id)s.%(ext)s",
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                }
                if proxy:
                    ydl_opts["proxy"] = proxy
                if cookies_browser:
                    ydl_opts["cookiesfrombrowser"] = (cookies_browser,)
                if cookies_file:
                    ydl_opts["cookiefile"] = cookies_file

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                except yt_dlp.utils.DownloadError as e:
                    msg = str(e)
                    lower = msg.lower()

                    if "429" in msg or "too many requests" in lower:
                        last_error = (
                            "Rate limited by YouTube — set TRANSCRIPT_PROXY or TRANSCRIPT_COOKIES_BROWSER"
                        )
                        if attempt < YTDLP_MAX_RETRIES:
                            sleep_s = YTDLP_RETRY_BACKOFF_SECONDS * attempt
                            time.sleep(sleep_s)
                            continue
                        return {
                            **empty,
                            "success": False,
                            "error": last_error,
                            "error_code": ERROR_RATE_LIMITED,
                        }

                    if (
                        "video unavailable" in lower
                        or "this video is not available" in lower
                        or "not available" in lower
                        or "private video" in lower
                    ):
                        return {
                            **empty,
                            "success": False,
                            "error": msg,
                            "error_code": ERROR_VIDEO_UNAVAILABLE,
                        }

                    last_error = msg
                    if attempt < YTDLP_MAX_RETRIES:
                        time.sleep(YTDLP_RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    return {**empty, "success": False, "error": last_error, "error_code": None}
                except Exception as e:
                    last_error = str(e)
                    if attempt < YTDLP_MAX_RETRIES:
                        time.sleep(YTDLP_RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    return {**empty, "success": False, "error": last_error, "error_code": None}

                vtt_files = sorted(Path(tmpdir).glob("*.vtt"))
                if not vtt_files:
                    return {
                        **empty,
                        "success": False,
                        "error": f"No transcript found in languages: {language_codes}",
                        "error_code": ERROR_NO_REQUESTED_LANGUAGE,
                    }

                chosen = vtt_files[0]
                lang_code = chosen.stem.split(".")[-1] if "." in chosen.stem else "en"
                is_generated = lang_code not in (info or {}).get("subtitles", {})

                try:
                    segments, full_text = _parse_vtt(chosen.read_text(encoding="utf-8", errors="ignore"))
                except Exception as e:
                    last_error = f"VTT parse error: {e}"
                    if attempt < YTDLP_MAX_RETRIES:
                        time.sleep(YTDLP_RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    return {**empty, "success": False, "error": last_error, "error_code": None}

                if not segments:
                    last_error = "Transcript was empty after parsing"
                    if attempt < YTDLP_MAX_RETRIES:
                        time.sleep(YTDLP_RETRY_BACKOFF_SECONDS * attempt)
                        continue
                    return {**empty, "success": False, "error": last_error, "error_code": None}

                return {
                    "success": True,
                    "video_id": video_id,
                    "transcript_text": full_text,
                    "segments": segments,
                    "language": lang_code,
                    "language_code": lang_code,
                    "is_generated": is_generated,
                    "source": source_name,
                    "auth_mode": "bot_session" if (cookies_browser or cookies_file) else "public",
                    "error": None,
                    "error_code": None,
                }

        return {**empty, "success": False, "error": last_error, "error_code": None}


def _fetch_via_youtube_transcript_api(video_id: str, language_codes: list[str]) -> dict:
    """
    Optional source tier via youtube-transcript-api when installed.
    """
    empty = dict(
        video_id=video_id,
        transcript_text="",
        segments=[],
        language=None,
        language_code=None,
        is_generated=None,
        source="youtube_transcript_api",
        auth_mode="public",
    )
    try:
        from youtube_transcript_api import (  # type: ignore
            YouTubeTranscriptApi,
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
            VideoUnplayable,
            InvalidVideoId,
        )
    except Exception:
        return {**empty, "success": False, "error": "youtube-transcript-api not installed", "error_code": None}

    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=language_codes)
    except (NoTranscriptFound, TranscriptsDisabled):
        return {
            **empty,
            "success": False,
            "error": f"No transcript available for requested languages: {language_codes}",
            "error_code": ERROR_NO_TRANSCRIPT,
        }
    except (VideoUnavailable, VideoUnplayable, InvalidVideoId):
        return {
            **empty,
            "success": False,
            "error": f"Video unavailable or unplayable: {video_id}",
            "error_code": ERROR_VIDEO_UNAVAILABLE,
        }
    except Exception as e:
        msg = str(e).lower()
        if "no transcript" in msg:
            return {**empty, "success": False, "error": str(e), "error_code": ERROR_NO_TRANSCRIPT}
        if "language" in msg:
            return {**empty, "success": False, "error": str(e), "error_code": ERROR_NO_REQUESTED_LANGUAGE}
        if "video is not available" in msg or "video unavailable" in msg or "private video" in msg:
            return {**empty, "success": False, "error": str(e), "error_code": ERROR_VIDEO_UNAVAILABLE}
        return {**empty, "success": False, "error": str(e), "error_code": None}

    if not fetched:
        return {**empty, "success": False, "error": "No transcript available", "error_code": ERROR_NO_TRANSCRIPT}

    segments = []
    for item in fetched:
        text = " ".join(str(getattr(item, "text", "")).split())
        if not text:
            continue
        segments.append(
            {
                "start": float(getattr(item, "start", 0.0) or 0.0),
                "duration": float(getattr(item, "duration", 0.0) or 0.0),
                "text": text,
            }
        )
    if not segments:
        return {**empty, "success": False, "error": "Transcript empty after parsing", "error_code": None}
    return {
        "success": True,
        "video_id": video_id,
        "transcript_text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "language": getattr(fetched, "language", language_codes[0] if language_codes else "en"),
        "language_code": getattr(fetched, "language_code", language_codes[0] if language_codes else "en"),
        "is_generated": bool(getattr(fetched, "is_generated", False)),
        "source": "youtube_transcript_api",
        "auth_mode": "public",
        "error": None,
        "error_code": None,
    }


def _fetch_via_unsupported_source(video_id: str, source: str) -> dict:
    return {
        "success": False,
        "video_id": video_id,
        "transcript_text": "",
        "segments": [],
        "language": None,
        "language_code": None,
        "is_generated": None,
        "source": source,
        "auth_mode": "public",
        "error": f"Source '{source}' is not configured in this environment",
        "error_code": None,
    }


def fetch_transcript_from_source(
    video_id: str,
    source: str,
    language_codes: list[str] | None = None,
    *,
    auth_cookies_browser: str | None = None,
    auth_cookies_file: str | None = None,
    proxy_override: str | None = None,
) -> dict:
    """
    Fetch transcript from one explicit source tier.

    Supported sources:
    - yt_dlp_public
    - yt_dlp_proxy
    - invidious
    - youtube_transcript_api (optional dependency)
    - third_party_api (placeholder)
    - yt_dlp_auth
    """
    if language_codes is None:
        language_codes = LANGUAGE_PRESETS["english"]
    if source == "yt_dlp_public":
        return _fetch_via_ytdlp_mode(
            video_id,
            language_codes,
            source_name=source,
            use_proxy_pool=False,
            force_proxy=None,
            cookies_browser=None,
            cookies_file=None,
        )
    if source == "yt_dlp_proxy":
        return _fetch_via_ytdlp_mode(
            video_id,
            language_codes,
            source_name=source,
            use_proxy_pool=True,
            force_proxy=proxy_override,
            cookies_browser=None,
            cookies_file=None,
        )
    if source == "invidious":
        res = _fetch_via_invidious(video_id, language_codes)
        res["source"] = source
        res["auth_mode"] = "public"
        return res
    if source == "youtube_transcript_api":
        return _fetch_via_youtube_transcript_api(video_id, language_codes)
    if source == "third_party_api":
        return _fetch_via_unsupported_source(video_id, source)
    if source == "yt_dlp_auth":
        return _fetch_via_ytdlp_mode(
            video_id,
            language_codes,
            source_name=source,
            use_proxy_pool=True,
            force_proxy=proxy_override,
            cookies_browser=auth_cookies_browser or None,
            cookies_file=auth_cookies_file or None,
        )
    return _fetch_via_unsupported_source(video_id, source)


def fetch_transcript_with_source_stack(
    video_id: str,
    *,
    language_codes: list[str] | None = None,
    source_tiers: list[str] | None = None,
    auth_cookies_browser: str | None = None,
    auth_cookies_file: str | None = None,
) -> tuple[dict | None, list[dict]]:
    """
    Try multiple source tiers in order. Returns (winning_result, attempt_results).
    """
    if language_codes is None:
        language_codes = LANGUAGE_PRESETS["english"]
    if source_tiers is None:
        source_tiers = [
            "yt_dlp_public",
            "yt_dlp_proxy",
            "invidious",
            "youtube_transcript_api",
            "third_party_api",
            "yt_dlp_auth",
        ]

    attempts: list[dict] = []
    for source in source_tiers:
        result = fetch_transcript_from_source(
            video_id,
            source,
            language_codes,
            auth_cookies_browser=auth_cookies_browser,
            auth_cookies_file=auth_cookies_file,
        )
        attempts.append(result)
        if result.get("success"):
            return result, attempts

        if result.get("error_code") == ERROR_NO_TRANSCRIPT:
            # terminal for subtitle availability; keep tier traversal deterministic but stop early.
            return None, attempts

    return None, attempts


# ---------------------------------------------------------------------------
# Public API (same interface as before)
# ---------------------------------------------------------------------------

def get_transcript(video_id: str, language_codes: list[str] = None, prefer_hindi: bool = False) -> dict:
    """
    Fetch transcript for a YouTube video.
    Tries Invidious first, then yt-dlp fallback only when useful.

    Returns:
        {success, video_id, transcript_text, segments, language, language_code, is_generated, error}
    """
    if language_codes is None:
        language_codes = LANGUAGE_PRESETS["hindi" if prefer_hindi else "english"]

    result = _fetch_via_invidious(video_id, language_codes)
    if result["success"]:
        return result

    # Terminal cases should not trigger expensive fallback.
    if result.get("error_code") in {ERROR_NO_TRANSCRIPT, ERROR_NO_REQUESTED_LANGUAGE}:
        return result

    if result.get("error") != "Invidious disabled":
        print(f"  [transcript] Invidious failed ({result['error']}), trying yt-dlp fallback...")
    fallback = _fetch_via_ytdlp(video_id, language_codes)
    if fallback["success"]:
        return fallback

    # Keep the best-known terminal reason if fallback had a generic failure.
    return fallback


def get_transcript_for_indian_channel(video_id: str) -> dict:
    return get_transcript(video_id, prefer_hindi=True)


def list_available_transcripts(video_id: str) -> dict:
    """List available transcript languages via Invidious."""
    instances = INVIDIOUS_INSTANCES.copy()
    random.shuffle(instances)

    if MAX_INVIDIOUS_INSTANCES_PER_VIDEO > 0:
        instances = instances[:MAX_INVIDIOUS_INSTANCES_PER_VIDEO]

    for instance in instances:
        try:
            r = requests.get(
                f"{instance}/api/v1/captions/{video_id}",
                timeout=INSTANCE_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                continue

            captions = r.json().get("captions", [])
            transcripts = [
                {
                    "language_code": c.get("languageCode") or c.get("language_code"),
                    "language": c.get("label"),
                    "is_generated": "auto" in c.get("label", "").lower(),
                    "is_translatable": False,
                }
                for c in captions
            ]
            return {"success": True, "video_id": video_id, "transcripts": transcripts, "error": None}

        except Exception:
            continue

    return {
        "success": False,
        "video_id": video_id,
        "transcripts": [],
        "error": "All Invidious instances failed",
    }
