"""
Seed script — inserts test data into Supabase for local API testing.
Run from project root: python supabase/seeds/seed.py
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from supabase import create_client

# Seed requires service role key to bypass RLS
client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])


# ── Categories ────────────────────────────────────────────────────────────────

CATEGORIES = [
    {"category_id": 1,  "category_name": "Film Animation"},
    {"category_id": 17, "category_name": "Sports"},
    {"category_id": 20, "category_name": "Gaming"},
    {"category_id": 22, "category_name": "People Blogs"},
    {"category_id": 23, "category_name": "Comedy"},
    {"category_id": 24, "category_name": "Entertainment"},
    {"category_id": 25, "category_name": "News Politics"},
    {"category_id": 26, "category_name": "Science Technology"},
    {"category_id": 27, "category_name": "Education"},
    {"category_id": 28, "category_name": "Science Technology"},
]

client.from_("categories").upsert(CATEGORIES, on_conflict="category_id").execute()
print("Seeded categories")


# ── Channels ──────────────────────────────────────────────────────────────────

CHANNELS = [
    {
        "channel_id": "UCX6OQ3DkcsbYNE6H8uQQuVA",
        "handle": "@MrBeast",
        "title": "MrBeast",
        "description": "I do crazy things on the internet.",
        "country": "US",
        "language": "en",
        "category_id": 24,
        "subscriber_count": 452000000,
        "video_count": 800,
        "view_count": 50000000000,
        "channel_score": 68.4,
        "videos_evaluated": 20,
    },
    {
        "channel_id": "UCBcRF18a7Qf58cCRy5xuWwQ",
        "handle": "@MarkRober",
        "title": "Mark Rober",
        "description": "Former NASA engineer. I make cool stuff.",
        "country": "US",
        "language": "en",
        "category_id": 26,
        "subscriber_count": 71900000,
        "video_count": 120,
        "view_count": 4200000000,
        "channel_score": 81.2,
        "videos_evaluated": 20,
    },
]

client.from_("channels").upsert(CHANNELS, on_conflict="channel_id").execute()
print("Seeded channels")


# ── Videos ────────────────────────────────────────────────────────────────────

VIDEOS = [
    # MrBeast
    {
        "video_id": "VIDEO_MB_001",
        "channel_id": "UCX6OQ3DkcsbYNE6H8uQQuVA",
        "title": "I Spent 7 Days Buried Alive",
        "thumbnail_url": "https://img.youtube.com/vi/VIDEO_MB_001/hqdefault.jpg",
        "duration_seconds": 743,
        "view_count": 145000000,
        "published_at": "2024-03-10T18:00:00Z",
        "video_score": 72.1,
        "title_content_similarity_score": 8,
        "title_sensationalism_score": 6,
        "sensationalism_mismatch_score": 1,
        "deception_score": 0,
        "focus_ratio_pct": 74,
        "time_to_main_content_seconds": 38.0,
        "time_to_main_content_fraction": 0.051,
        "sponsor_interruption": "minor",
        "reasoning_similarity": "Title accurately reflects the burial challenge shown throughout the video.",
        "reasoning_sensationalism": "Mild hype — typical for the format, not excessive.",
        "reasoning_mismatch": "Title excitement matches the genuinely extreme nature of the challenge.",
        "reasoning_deception": "No false claims; the 7-day burial is exactly what is delivered.",
        "reasoning_focus": "Most runtime is focused on the challenge with brief filler sections.",
        "reasoning_time_to_main": "Challenge begins at 38s after a short intro sequence.",
        "reasoning_sponsor": "One mid-roll sponsor break around the 4-minute mark.",
        "transcript_text": "[0.0s] Alright so today I am actually going to be buried alive\n[3.2s] We dug a hole that is six feet deep\n[8.5s] I will be inside a box for seven full days\n[15.0s] Let's go",
        "transcript_language": "en",
        "evaluation_success": True,
    },
    {
        "video_id": "VIDEO_MB_002",
        "channel_id": "UCX6OQ3DkcsbYNE6H8uQQuVA",
        "title": "World's Largest Lego Tower!",
        "thumbnail_url": "https://img.youtube.com/vi/VIDEO_MB_002/hqdefault.jpg",
        "duration_seconds": 612,
        "view_count": 98000000,
        "published_at": "2024-01-20T18:00:00Z",
        "video_score": 64.7,
        "title_content_similarity_score": 7,
        "title_sensationalism_score": 5,
        "sensationalism_mismatch_score": 3,
        "deception_score": 2,
        "focus_ratio_pct": 68,
        "time_to_main_content_seconds": 52.0,
        "time_to_main_content_fraction": 0.085,
        "sponsor_interruption": "minor",
        "reasoning_similarity": "Video does build a very large Lego tower but world record claim is unverified.",
        "reasoning_sensationalism": "Exclamation point and superlative but fairly standard for the channel.",
        "reasoning_mismatch": "Slight mismatch as world record status is not confirmed in the video.",
        "reasoning_deception": "Minor: the world record claim lacks on-screen verification.",
        "reasoning_focus": "Good focus on the build with some tangential friend interactions.",
        "reasoning_time_to_main": "Building begins around 52s after intro and sponsor.",
        "reasoning_sponsor": "One sponsor segment mid-video.",
        "transcript_text": "[0.0s] Today we are building the world's largest Lego tower\n[4.1s] We have over two million Lego bricks\n[10.3s] This is going to take days",
        "transcript_language": "en",
        "evaluation_success": True,
    },
    # Mark Rober
    {
        "video_id": "VIDEO_MR_001",
        "channel_id": "UCBcRF18a7Qf58cCRy5xuWwQ",
        "title": "How to Escape Alcatraz With Basic Engineering",
        "thumbnail_url": "https://img.youtube.com/vi/VIDEO_MR_001/hqdefault.jpg",
        "duration_seconds": 1247,
        "view_count": 34000000,
        "published_at": "2024-02-15T18:00:00Z",
        "video_score": 85.3,
        "title_content_similarity_score": 9,
        "title_sensationalism_score": 3,
        "sensationalism_mismatch_score": 0,
        "deception_score": 0,
        "focus_ratio_pct": 88,
        "time_to_main_content_seconds": 22.0,
        "time_to_main_content_fraction": 0.018,
        "sponsor_interruption": "none",
        "reasoning_similarity": "Video precisely covers engineering methods relevant to the Alcatraz escape, matching the title exactly.",
        "reasoning_sensationalism": "Title is informative and calm — no hype language.",
        "reasoning_mismatch": "No mismatch; the content is as fascinating as a neutral title implies.",
        "reasoning_deception": "No deceptive claims; the engineering analysis is accurate and well-sourced.",
        "reasoning_focus": "Highly focused on the escape engineering topic throughout.",
        "reasoning_time_to_main": "Main topic begins at 22s after a brief hook.",
        "reasoning_sponsor": "No sponsor interruptions.",
        "transcript_text": "[0.0s] In 1962 three men escaped from Alcatraz\n[4.5s] No one knows if they survived\n[8.2s] But as an engineer I want to understand exactly how they did it\n[14.0s] Let's start with the vent cover",
        "transcript_language": "en",
        "evaluation_success": True,
    },
    {
        "video_id": "VIDEO_MR_002",
        "channel_id": "UCBcRF18a7Qf58cCRy5xuWwQ",
        "title": "I Made the World's Squirtiest Squirt Gun",
        "thumbnail_url": "https://img.youtube.com/vi/VIDEO_MR_002/hqdefault.jpg",
        "duration_seconds": 892,
        "view_count": 61000000,
        "published_at": "2023-08-01T18:00:00Z",
        "video_score": 77.1,
        "title_content_similarity_score": 8,
        "title_sensationalism_score": 4,
        "sensationalism_mismatch_score": 1,
        "deception_score": 0,
        "focus_ratio_pct": 82,
        "time_to_main_content_seconds": 18.0,
        "time_to_main_content_fraction": 0.020,
        "sponsor_interruption": "none",
        "reasoning_similarity": "The video delivers exactly what the title promises: an engineering-built high-powered squirt gun.",
        "reasoning_sensationalism": "Playful title but appropriate for the content.",
        "reasoning_mismatch": "Minimal mismatch — the gun is genuinely impressive.",
        "reasoning_deception": "No false claims; the squirt gun performs as described.",
        "reasoning_focus": "Strong focus on the build and testing process.",
        "reasoning_time_to_main": "Build begins immediately after a short 18s hook.",
        "reasoning_sponsor": "No mid-roll sponsor interruptions.",
        "transcript_text": "[0.0s] I spent three months building the world's most powerful squirt gun\n[5.0s] It can shoot water at 272 miles per hour\n[10.2s] Here is how I did it",
        "transcript_language": "en",
        "evaluation_success": True,
    },
]

client.from_("video_evaluations").upsert(VIDEOS, on_conflict="video_id").execute()
print("Seeded videos")

# Update channel scores
client.from_("channels").update({"channel_score": 68.4, "videos_evaluated": 2}).eq("channel_id", "UCX6OQ3DkcsbYNE6H8uQQuVA").execute()
client.from_("channels").update({"channel_score": 81.2, "videos_evaluated": 2}).eq("channel_id", "UCBcRF18a7Qf58cCRy5xuWwQ").execute()

print("Done. Supabase seeded with 2 channels, 4 videos.")
