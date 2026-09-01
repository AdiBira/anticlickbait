"""
Configuration variables for the anticlickbait project.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")

# YouTube API key
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

# OpenAI API configuration (loaded from .env file)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o-mini"  # Options: "gpt-4o-mini", "gpt-4o", "gpt-5" (when available)

# Retry settings for OpenAI API calls
OPENAI_MAX_RETRIES = int(os.environ.get("OPENAI_MAX_RETRIES", "3"))
OPENAI_RETRY_DELAY_SECONDS = float(os.environ.get("OPENAI_RETRY_DELAY_SECONDS", "2"))  # Base delay, will use exponential backoff

# Number of videos to fetch per channel (can be changed later)
VIDEOS_PER_CHANNEL = 15

# Minimum video duration in seconds (0 = no minimum; Shorts filtered by #shorts tag only)
MIN_VIDEO_DURATION_SECONDS = 0

# Thumbnail analysis settings
THUMBNAIL_CACHE_DIR = _project_root / "data" / "thumbnails"
THUMBNAIL_PROMPT_PATH = _project_root / "prompts" / "thumbnail_prompt.txt"
ENABLE_THUMBNAIL_ANALYSIS = os.environ.get("ENABLE_THUMBNAIL_ANALYSIS", "false").lower() == "true"

# Temporary long-form skip policy (park long-form until methodology is redesigned)
ENABLE_LONG_FORM_SKIP = os.environ.get("ENABLE_LONG_FORM_SKIP", "true").lower() == "true"
LONG_FORM_VIDEO_DURATION_SECONDS = int(os.environ.get("LONG_FORM_VIDEO_DURATION_SECONDS", "5400"))  # 90 min
LONG_FORM_CHANNEL_SKIP_RATIO = float(os.environ.get("LONG_FORM_CHANNEL_SKIP_RATIO", "0.5"))

# If too many early videos in a channel fail transcript retrieval, short-circuit the channel
# to avoid spending hours on obviously non-eligible/blocked channels.
MAX_CONSECUTIVE_TRANSCRIPT_MISSES = int(os.environ.get("MAX_CONSECUTIVE_TRANSCRIPT_MISSES", "5"))


# Video sampling methodology (transparent + deterministic)
# Final selected set per channel is a mix of latest + popular videos.
VIDEO_SELECTION_LATEST_RATIO = float(os.environ.get("VIDEO_SELECTION_LATEST_RATIO", "0.5"))

# Local YouTube API cache to avoid repeated quota burn across supervisor passes
YOUTUBE_CACHE_TTL_HOURS = int(os.environ.get("YOUTUBE_CACHE_TTL_HOURS", "6"))

# Fallback discovery path when YouTube Data API quota is exhausted:
# use yt-dlp to extract channel video pools without Data API units.
YTDLP_CHANNEL_DISCOVERY_FALLBACK = os.environ.get("YTDLP_CHANNEL_DISCOVERY_FALLBACK", "true").lower() == "true"
