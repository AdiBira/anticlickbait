"""
Verify that filtered channels have working transcripts.
Tests 1 video per channel to confirm English transcripts are available.
"""

import json
from pathlib import Path
from youtube_api import get_channel_info_by_handle, get_channel_videos
from transcript_fetcher import get_transcript

# Load channels from channels_final.json
_data_path = Path(__file__).parent.parent / "data" / "channels_final.json"
with open(_data_path) as f:
    _channel_data = json.load(f)
CHANNELS = [{"name": c["name"], "handle": c["handle"]} for c in _channel_data["channels"]]


def verify_channel(name: str, handle: str) -> dict:
    """Verify a channel exists and has transcripts available."""
    print(f"\n{'='*50}")
    print(f"Verifying: {name} (@{handle})")
    print('='*50)
    
    result = {
        "name": name,
        "handle": handle,
        "channel_found": False,
        "channel_id": None,
        "video_found": False,
        "video_id": None,
        "video_title": None,
        "transcript_available": False,
        "transcript_length": 0,
        "error": None,
    }
    
    # Step 1: Get channel info
    print(f"  [1/3] Fetching channel info...")
    channel_info = get_channel_info_by_handle(handle)
    
    if not channel_info:
        result["error"] = "Channel not found"
        print(f"  ❌ Channel not found")
        return result
    
    result["channel_found"] = True
    result["channel_id"] = channel_info["channel_id"]
    print(f"  ✅ Channel found: {channel_info['title']} ({channel_info['subscriber_count']:,} subs)")
    
    # Step 2: Get 1 video
    print(f"  [2/3] Fetching 1 video (>3min)...")
    videos = get_channel_videos(channel_info["channel_id"], max_videos=1)
    
    if not videos:
        result["error"] = "No videos found (>3 min)"
        print(f"  ❌ No videos found")
        return result
    
    video = videos[0]
    result["video_found"] = True
    result["video_id"] = video["video_id"]
    result["video_title"] = video["title"]
    print(f"  ✅ Video found: {video['title'][:50]}...")
    print(f"     Duration: {video['duration_seconds']//60}m {video['duration_seconds']%60}s")
    
    # Step 3: Get transcript
    print(f"  [3/3] Fetching English transcript...")
    transcript = get_transcript(video["video_id"])
    
    if not transcript["success"]:
        result["error"] = f"Transcript failed: {transcript['error']}"
        print(f"  ❌ Transcript failed: {transcript['error']}")
        return result
    
    result["transcript_available"] = True
    result["transcript_length"] = len(transcript["transcript_text"])
    print(f"  ✅ Transcript available: {len(transcript['transcript_text']):,} chars")
    print(f"     Language: {transcript['language']} ({transcript['language_code']})")
    
    return result


def main():
    print("\n" + "#"*60)
    print("# CHANNEL TRANSCRIPT VERIFICATION")
    print("#"*60)
    
    results = []
    
    for channel in CHANNELS:
        result = verify_channel(channel["name"], channel["handle"])
        results.append(result)
    
    # Summary
    print("\n" + "#"*60)
    print("# VERIFICATION SUMMARY")
    print("#"*60)
    
    passed = [r for r in results if r["transcript_available"]]
    failed = [r for r in results if not r["transcript_available"]]
    
    print(f"\n✅ Passed: {len(passed)}/{len(results)}")
    for r in passed:
        print(f"   - {r['name']}")
    
    if failed:
        print(f"\n❌ Failed: {len(failed)}/{len(results)}")
        for r in failed:
            print(f"   - {r['name']}: {r['error']}")
    
    # Save results
    output = {
        "total_channels": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "results": results,
    }
    
    with open("../data/channel_verification.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\nResults saved to: data/channel_verification.json")
    
    return len(failed)


if __name__ == "__main__":
    exit(main())

