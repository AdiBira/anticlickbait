"""
Test script to validate all components.
"""

import sys

# Test results tracker
tests_passed = 0
tests_failed = 0


def test(name, condition, details=""):
    global tests_passed, tests_failed
    if condition:
        print(f"✅ PASS: {name}")
        tests_passed += 1
    else:
        print(f"❌ FAIL: {name}")
        if details:
            print(f"   Details: {details}")
        tests_failed += 1


def test_config():
    """Test config module"""
    print("\n" + "="*50)
    print("TESTING: config.py")
    print("="*50)
    
    from config import YOUTUBE_API_KEY, VIDEOS_PER_CHANNEL, MIN_VIDEO_DURATION_SECONDS
    
    test("API key is set", len(YOUTUBE_API_KEY) > 0)
    test("VIDEOS_PER_CHANNEL is 20", VIDEOS_PER_CHANNEL == 20)
    test("MIN_VIDEO_DURATION_SECONDS is 181", MIN_VIDEO_DURATION_SECONDS == 181)


def test_categories():
    """Test categories module"""
    print("\n" + "="*50)
    print("TESTING: categories.py")
    print("="*50)
    
    from categories import get_category_name, get_category_id, YOUTUBE_CATEGORIES
    
    test("Categories dict has entries", len(YOUTUBE_CATEGORIES) > 0)
    test("Category 28 is Science & Technology", get_category_name(28) == "Science & Technology")
    test("Category 20 is Gaming", get_category_name(20) == "Gaming")
    test("Category 24 is Entertainment", get_category_name(24) == "Entertainment")
    test("Unknown category returns 'Unknown'", get_category_name(9999) == "Unknown")
    test("get_category_id('Gaming') returns 20", get_category_id("Gaming") == 20)
    test("get_category_id('Unknown Category') returns None", get_category_id("Unknown Category") is None)


def test_youtube_api():
    """Test YouTube API wrapper"""
    print("\n" + "="*50)
    print("TESTING: youtube_api.py")
    print("="*50)
    
    from youtube_api import (
        parse_duration,
        get_channel_info,
        get_channel_info_by_handle,
        get_channel_videos
    )
    
    # Test duration parsing
    test("Parse PT4M13S = 253 seconds", parse_duration("PT4M13S") == 253)
    test("Parse PT1H2M3S = 3723 seconds", parse_duration("PT1H2M3S") == 3723)
    test("Parse PT30S = 30 seconds", parse_duration("PT30S") == 30)
    test("Parse PT1H = 3600 seconds", parse_duration("PT1H") == 3600)
    test("Parse PT5M = 300 seconds", parse_duration("PT5M") == 300)
    
    # Test channel info by ID (MrBeast)
    print("\n  Testing get_channel_info (MrBeast)...")
    mrbeast_id = "UCX6OQ3DkcsbYNE6H8uQQuVA"
    channel_info = get_channel_info(mrbeast_id)
    
    test("get_channel_info returns data", channel_info is not None)
    if channel_info:
        test("Channel title is 'MrBeast'", channel_info["title"] == "MrBeast")
        test("Channel has subscriber_count", channel_info["subscriber_count"] > 0)
        test("Channel has uploads_playlist_id", channel_info["uploads_playlist_id"] is not None)
        print(f"   → Subscribers: {channel_info['subscriber_count']:,}")
    
    # Test channel info by handle
    print("\n  Testing get_channel_info_by_handle (@MrBeast)...")
    channel_by_handle = get_channel_info_by_handle("MrBeast")
    
    test("get_channel_info_by_handle returns data", channel_by_handle is not None)
    if channel_by_handle:
        test("Handle lookup matches ID lookup", channel_by_handle["channel_id"] == mrbeast_id)
    
    # Test get_channel_videos (fetch only 3 to save API quota)
    print("\n  Testing get_channel_videos (fetching 3 videos)...")
    videos = get_channel_videos(mrbeast_id, max_videos=3)
    
    test("get_channel_videos returns list", isinstance(videos, list))
    test("get_channel_videos returns videos", len(videos) > 0)
    
    if videos:
        video = videos[0]
        test("Video has video_id", "video_id" in video)
        test("Video has title", "title" in video)
        test("Video has duration_seconds", "duration_seconds" in video)
        test("Video duration > 181s (not a short)", video["duration_seconds"] > 181)
        test("Video has category_name", "category_name" in video)
        
        print(f"\n   Sample video:")
        print(f"   → Title: {video['title'][:60]}...")
        print(f"   → Duration: {video['duration_seconds']}s ({video['duration_seconds']//60}m {video['duration_seconds']%60}s)")
        print(f"   → Category: {video['category_name']}")
        print(f"   → Views: {video['view_count']:,}")


def test_transcript_fetcher():
    """Test transcript fetcher"""
    print("\n" + "="*50)
    print("TESTING: transcript_fetcher.py")
    print("="*50)
    
    from transcript_fetcher import get_transcript, list_available_transcripts
    
    # Test with known video (MrBeast: "100 People Vs World's Biggest Trap!")
    video_id = "3RmOvxilbPM"
    
    print(f"\n  Testing get_transcript for video: {video_id}...")
    result = get_transcript(video_id)
    
    test("get_transcript returns dict", isinstance(result, dict))
    test("Result has success field", "success" in result)
    test("Transcript fetch succeeded", result["success"] == True)
    
    if result["success"]:
        test("Has transcript_text", len(result["transcript_text"]) > 0)
        test("Has segments", len(result["segments"]) > 0)
        test("Has language info", result["language"] is not None)
        
        print(f"\n   Transcript stats:")
        print(f"   → Language: {result['language']} ({result['language_code']})")
        print(f"   → Auto-generated: {result['is_generated']}")
        print(f"   → Segments: {len(result['segments'])}")
        print(f"   → Total chars: {len(result['transcript_text']):,}")
        print(f"   → First 100 chars: {result['transcript_text'][:100]}...")
    
    # Test list_available_transcripts
    print(f"\n  Testing list_available_transcripts...")
    transcripts_result = list_available_transcripts(video_id)
    
    test("list_available_transcripts returns dict", isinstance(transcripts_result, dict))
    test("List transcripts succeeded", transcripts_result["success"] == True)
    
    if transcripts_result["success"]:
        print(f"   → Available transcripts: {len(transcripts_result['transcripts'])}")
        for t in transcripts_result["transcripts"][:3]:
            print(f"      - {t['language']} ({t['language_code']}) {'[auto]' if t['is_generated'] else ''}")


def test_data_collector_structure():
    """Test data collector module structure (without executing collection)"""
    print("\n" + "="*50)
    print("TESTING: data_collector.py (structure only)")
    print("="*50)
    
    from data_collector import (
        collect_channel_data,
        collect_multiple_channels,
        save_collected_data,
        load_collected_data,
        print_empty_transcript_summary
    )
    
    test("collect_channel_data is callable", callable(collect_channel_data))
    test("collect_multiple_channels is callable", callable(collect_multiple_channels))
    test("save_collected_data is callable", callable(save_collected_data))
    test("load_collected_data is callable", callable(load_collected_data))
    test("print_empty_transcript_summary is callable", callable(print_empty_transcript_summary))


def main():
    print("\n" + "#"*60)
    print("# ANTICLICKBAIT COMPONENT TESTS")
    print("#"*60)
    
    try:
        test_config()
        test_categories()
        test_youtube_api()
        test_transcript_fetcher()
        test_data_collector_structure()
    except Exception as e:
        print(f"\n❌ TEST SUITE CRASHED: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Summary
    print("\n" + "#"*60)
    print("# TEST SUMMARY")
    print("#"*60)
    print(f"\n✅ Passed: {tests_passed}")
    print(f"❌ Failed: {tests_failed}")
    print(f"📊 Total:  {tests_passed + tests_failed}")
    
    if tests_failed == 0:
        print("\n🎉 ALL TESTS PASSED!")
        return 0
    else:
        print(f"\n⚠️  {tests_failed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

