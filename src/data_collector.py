"""
Raw data collector for the anticlickbait project.
Collects channel info, video metadata, and transcripts.
Does NOT perform any analysis - only data collection.
"""

import json
from datetime import datetime
from pathlib import Path

from youtube_api import get_channel_info, get_channel_info_by_handle, get_channel_videos
from transcript_fetcher import get_transcript
from config import VIDEOS_PER_CHANNEL


def collect_channel_data(
    channel_identifier: str,
    max_videos: int = VIDEOS_PER_CHANNEL,
    is_handle: bool = False
) -> dict:
    """
    Collect all data for a single channel: info, videos, and transcripts.
    
    Args:
        channel_identifier: Channel ID or handle
        max_videos: Maximum videos to fetch per channel
        is_handle: If True, treat identifier as a handle (e.g., "@MrBeast")
        
    Returns:
        Dictionary with channel data, videos, transcripts, and stats
    """
    print(f"Collecting data for channel: {channel_identifier}")
    
    # Get channel info
    if is_handle:
        channel_info = get_channel_info_by_handle(channel_identifier)
    else:
        channel_info = get_channel_info(channel_identifier)
    
    if not channel_info:
        return {
            "success": False,
            "error": f"Could not find channel: {channel_identifier}",
            "channel_info": None,
            "videos": [],
            "stats": {},
        }
    
    print(f"  Found channel: {channel_info['title']}")
    
    # Get videos
    print(f"  Fetching up to {max_videos} videos...")
    videos = get_channel_videos(channel_info["channel_id"], max_videos=max_videos)
    print(f"  Found {len(videos)} videos (after filtering shorts)")
    
    # Get transcripts for each video
    print(f"  Fetching transcripts...")
    videos_with_transcripts = []
    empty_transcript_count = 0
    
    for i, video in enumerate(videos):
        video_id = video["video_id"]
        print(f"    [{i+1}/{len(videos)}] {video['title'][:50]}...")
        
        transcript_result = get_transcript(video_id)
        
        video_data = {
            **video,
            "transcript": transcript_result,
        }
        videos_with_transcripts.append(video_data)
        
        if not transcript_result["success"] or not transcript_result["transcript_text"]:
            empty_transcript_count += 1
    
    # Calculate stats
    stats = {
        "total_videos_fetched": len(videos),
        "videos_with_transcripts": len(videos) - empty_transcript_count,
        "videos_without_transcripts": empty_transcript_count,
        "collection_timestamp": datetime.utcnow().isoformat(),
    }
    
    print(f"  Stats: {stats['videos_with_transcripts']}/{stats['total_videos_fetched']} videos have transcripts")
    
    return {
        "success": True,
        "error": None,
        "channel_info": channel_info,
        "videos": videos_with_transcripts,
        "stats": stats,
    }


def collect_multiple_channels(
    channels: list[dict],
    max_videos_per_channel: int = VIDEOS_PER_CHANNEL
) -> dict:
    """
    Collect data for multiple channels.
    
    Args:
        channels: List of dicts with either "channel_id" or "handle" key
                  Example: [{"handle": "MrBeast"}, {"channel_id": "UC..."}]
        max_videos_per_channel: Maximum videos to fetch per channel
        
    Returns:
        Dictionary with all channel data and aggregate stats
    """
    results = []
    aggregate_stats = {
        "total_channels": len(channels),
        "successful_channels": 0,
        "failed_channels": 0,
        "total_videos": 0,
        "total_videos_with_transcripts": 0,
        "total_videos_without_transcripts": 0,
        "channels_with_empty_transcripts": [],  # Channels where some videos have no transcript
    }
    
    for channel in channels:
        # Determine if it's a handle or channel ID
        if "handle" in channel:
            identifier = channel["handle"]
            is_handle = True
        elif "channel_id" in channel:
            identifier = channel["channel_id"]
            is_handle = False
        else:
            print(f"Invalid channel entry: {channel}")
            continue
        
        # Collect data
        result = collect_channel_data(
            identifier,
            max_videos=max_videos_per_channel,
            is_handle=is_handle
        )
        results.append(result)
        
        # Update aggregate stats
        if result["success"]:
            aggregate_stats["successful_channels"] += 1
            aggregate_stats["total_videos"] += result["stats"]["total_videos_fetched"]
            aggregate_stats["total_videos_with_transcripts"] += result["stats"]["videos_with_transcripts"]
            aggregate_stats["total_videos_without_transcripts"] += result["stats"]["videos_without_transcripts"]
            
            if result["stats"]["videos_without_transcripts"] > 0:
                aggregate_stats["channels_with_empty_transcripts"].append({
                    "channel_name": result["channel_info"]["title"],
                    "empty_transcript_count": result["stats"]["videos_without_transcripts"],
                })
        else:
            aggregate_stats["failed_channels"] += 1
    
    return {
        "channels": results,
        "aggregate_stats": aggregate_stats,
        "collection_timestamp": datetime.utcnow().isoformat(),
    }


def save_collected_data(data: dict, output_path: str = "data/collected_data.json") -> str:
    """
    Save collected data to a JSON file.
    
    Args:
        data: Data dictionary to save
        output_path: Path to output file
        
    Returns:
        Path to saved file
    """
    # Create directory if it doesn't exist
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"Data saved to: {output_file}")
    return str(output_file)


def load_collected_data(input_path: str = "data/collected_data.json") -> dict | None:
    """
    Load previously collected data from a JSON file.
    
    Args:
        input_path: Path to input file
        
    Returns:
        Data dictionary, or None if file doesn't exist
    """
    input_file = Path(input_path)
    
    if not input_file.exists():
        print(f"File not found: {input_file}")
        return None
    
    with open(input_file, "r", encoding="utf-8") as f:
        return json.load(f)


def print_empty_transcript_summary(data: dict) -> None:
    """
    Print a summary of channels with empty transcripts.
    
    Args:
        data: Collected data dictionary from collect_multiple_channels
    """
    print("\n" + "=" * 60)
    print("EMPTY TRANSCRIPT SUMMARY")
    print("=" * 60)
    
    stats = data.get("aggregate_stats", {})
    
    print(f"\nTotal channels: {stats.get('total_channels', 0)}")
    print(f"Total videos: {stats.get('total_videos', 0)}")
    print(f"Videos with transcripts: {stats.get('total_videos_with_transcripts', 0)}")
    print(f"Videos without transcripts: {stats.get('total_videos_without_transcripts', 0)}")
    
    empty_channels = stats.get("channels_with_empty_transcripts", [])
    
    if empty_channels:
        print(f"\nChannels with missing transcripts ({len(empty_channels)}):")
        print("-" * 40)
        for channel in sorted(empty_channels, key=lambda x: x["empty_transcript_count"], reverse=True):
            print(f"  {channel['channel_name']}: {channel['empty_transcript_count']} videos without transcripts")
    else:
        print("\nAll videos have transcripts!")
    
    print("=" * 60)

