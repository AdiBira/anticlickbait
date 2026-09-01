"""
Thumbnail downloader with caching.
Downloads YouTube thumbnails and caches locally.
"""

from pathlib import Path
import requests

from config import THUMBNAIL_CACHE_DIR


def get_thumbnail_path(video_id: str) -> Path:
    """Get cache path for a video's thumbnail."""
    return THUMBNAIL_CACHE_DIR / f"{video_id}.jpg"


def download_thumbnail(video_id: str, thumbnail_url: str, force: bool = False) -> dict:
    """
    Download and cache a thumbnail.

    Args:
        video_id: YouTube video ID
        thumbnail_url: URL to thumbnail
        force: Re-download even if cached

    Returns:
        dict with success, path, from_cache, error
    """
    cache_path = get_thumbnail_path(video_id)

    # Return cached if exists
    if not force and cache_path.exists():
        return {
            "success": True,
            "video_id": video_id,
            "path": cache_path,
            "from_cache": True,
            "error": None,
        }

    # Ensure cache dir exists
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        response = requests.get(thumbnail_url, timeout=30)
        response.raise_for_status()
        cache_path.write_bytes(response.content)

        return {
            "success": True,
            "video_id": video_id,
            "path": cache_path,
            "from_cache": False,
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "video_id": video_id,
            "path": None,
            "from_cache": False,
            "error": str(e),
        }


def clear_cache(video_id: str = None) -> int:
    """Clear thumbnail cache. Returns count of files deleted."""
    if video_id:
        path = get_thumbnail_path(video_id)
        if path.exists():
            path.unlink()
            return 1
        return 0

    count = 0
    if THUMBNAIL_CACHE_DIR.exists():
        for f in THUMBNAIL_CACHE_DIR.glob("*.jpg"):
            f.unlink()
            count += 1
    return count
