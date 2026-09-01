"""
SQLite database for storing channel and video evaluation data.

Tables:
- channels: Channel metadata and scores
- video_evaluations: Individual video evaluation results
- categories: YouTube category reference table

Views:
- ranking_global_english: Top 100 English channels globally
- ranking_category_global_english: Top N per category (English, global)
- ranking_category_india: Top N per category (India, English + Hindi)
"""

import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional

from categories import YOUTUBE_CATEGORIES


# Default database path
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "anticlickbait.db"


def get_connection(db_path: Path = None) -> sqlite3.Connection:
    """
    Get a database connection.
    
    Args:
        db_path: Path to the database file. Defaults to data/anticlickbait.db
        
    Returns:
        SQLite connection object
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    
    # Ensure directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row  # Enable dict-like access to rows
    # Improve concurrent read/write behavior for low-worker batch runs.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_database(db_path: Path = None) -> None:
    """
    Initialize the database with all required tables and views.
    
    Args:
        db_path: Path to the database file
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # Create categories table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            category_id INTEGER PRIMARY KEY,
            category_name TEXT NOT NULL
        )
    """)
    
    # Populate categories from the existing mapping
    for cat_id, cat_name in YOUTUBE_CATEGORIES.items():
        cursor.execute("""
            INSERT OR IGNORE INTO categories (category_id, category_name)
            VALUES (?, ?)
        """, (cat_id, cat_name))
    
    # Create channels table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            custom_url TEXT,
            country TEXT,
            language TEXT DEFAULT 'en',
            category_id INTEGER,
            subscriber_count INTEGER DEFAULT 0,
            video_count INTEGER DEFAULT 0,
            view_count INTEGER DEFAULT 0,
            channel_score REAL,
            videos_evaluated INTEGER DEFAULT 0,
            last_evaluated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories (category_id)
        )
    """)
    
    # Create video_evaluations table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS video_evaluations (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            published_at TEXT,
            category_id INTEGER,
            duration_seconds INTEGER,
            view_count INTEGER DEFAULT 0,
            like_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,

            -- Text evaluation metrics (v1.1, all 0-10)
            title_content_similarity_score REAL,
            deception_score REAL,
            focus_ratio REAL,
            time_to_main_content REAL,
            sponsor_interruption_score REAL,
            video_score REAL,

            -- Per-metric LLM reasoning
            title_analysis_reasoning TEXT,
            deception_reasoning TEXT,
            focus_reasoning TEXT,
            time_reasoning TEXT,
            sponsor_reasoning TEXT,
            llm_explanation TEXT,

            -- Transcript
            transcript_text TEXT,
            transcript_language TEXT,

            -- Thumbnail evaluation metrics
            thumbnail_url TEXT,
            thumbnail_content_match REAL,
            thumbnail_sensationalism_score REAL,
            thumbnail_deception_flag INTEGER,
            thumbnail_visual_elements TEXT,  -- JSON string
            thumbnail_explanation TEXT,
            thumbnail_score REAL,
            thumbnail_evaluation_success INTEGER DEFAULT 0,

            -- Combined score
            combined_score REAL,

            -- Metadata
            evaluation_success INTEGER DEFAULT 0,
            evaluation_error TEXT,
            evaluated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (channel_id) REFERENCES channels (channel_id),
            FOREIGN KEY (category_id) REFERENCES categories (category_id)
        )
    """)

    # Subtitle fetch attempt log (all sources / retries)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            channel_id TEXT,
            attempt_no INTEGER NOT NULL,
            source TEXT NOT NULL,
            auth_mode TEXT,
            started_at TEXT,
            ended_at TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            failure_code TEXT,
            failure_detail TEXT,
            transcript_language TEXT,
            content_hash TEXT
        )
    """)

    # Final resolution state per target video.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_resolution (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('resolved', 'unresolved')),
            winning_source TEXT,
            language_code TEXT,
            fetched_at TEXT,
            content_hash TEXT,
            attempts INTEGER DEFAULT 0,
            last_failure_code TEXT,
            last_failure_detail TEXT,
            updated_at TEXT
        )
    """)

    # Channel-level coverage status cache populated by coverage report/check commands.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_coverage_status (
            channel_id TEXT PRIMARY KEY,
            handle TEXT,
            required_videos INTEGER NOT NULL DEFAULT 15,
            resolved_videos INTEGER NOT NULL DEFAULT 0,
            actual_latest_count INTEGER NOT NULL DEFAULT 0,
            actual_popular_count INTEGER NOT NULL DEFAULT 0,
            ratio_drift REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL,
            details_json TEXT,
            updated_at TEXT
        )
    """)
    
    # Create indexes for faster queries
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_channels_country ON channels (country)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_channels_language ON channels (language)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_channels_category ON channels (category_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_channels_score ON channels (channel_score DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_videos_channel ON video_evaluations (channel_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_videos_score ON video_evaluations (video_score DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_subtitle_attempts_video ON subtitle_attempts (video_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_subtitle_attempts_source ON subtitle_attempts (source, started_at)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_subtitle_resolution_status ON subtitle_resolution (status, updated_at)
    """)
    
    # Create ranking views
    
    # View 1: Top 100 English channels globally
    cursor.execute("""
        CREATE VIEW IF NOT EXISTS ranking_global_english AS
        SELECT 
            ROW_NUMBER() OVER (ORDER BY channel_score DESC) as rank,
            channel_id,
            title,
            country,
            category_id,
            (SELECT category_name FROM categories WHERE categories.category_id = channels.category_id) as category_name,
            subscriber_count,
            channel_score,
            videos_evaluated,
            last_evaluated_at
        FROM channels
        WHERE language = 'en' AND channel_score IS NOT NULL
        ORDER BY channel_score DESC
        LIMIT 100
    """)
    
    # View 2: Top N per category (English, global)
    cursor.execute("""
        CREATE VIEW IF NOT EXISTS ranking_category_global_english AS
        SELECT 
            category_id,
            (SELECT category_name FROM categories WHERE categories.category_id = ranked.category_id) as category_name,
            ROW_NUMBER() OVER (PARTITION BY category_id ORDER BY channel_score DESC) as category_rank,
            channel_id,
            title,
            country,
            subscriber_count,
            channel_score,
            videos_evaluated
        FROM channels as ranked
        WHERE language = 'en' AND channel_score IS NOT NULL
        ORDER BY category_id, channel_score DESC
    """)
    
    # View 3: Top N per category (India, English + Hindi)
    cursor.execute("""
        CREATE VIEW IF NOT EXISTS ranking_category_india AS
        SELECT 
            category_id,
            (SELECT category_name FROM categories WHERE categories.category_id = ranked.category_id) as category_name,
            ROW_NUMBER() OVER (PARTITION BY category_id ORDER BY channel_score DESC) as category_rank,
            channel_id,
            title,
            language,
            subscriber_count,
            channel_score,
            videos_evaluated
        FROM channels as ranked
        WHERE country = 'IN' AND language IN ('en', 'hi') AND channel_score IS NOT NULL
        ORDER BY category_id, channel_score DESC
    """)
    
    conn.commit()
    conn.close()
    
    print(f"Database initialized at: {db_path or DEFAULT_DB_PATH}")


def insert_channel(
    channel_id: str,
    title: str,
    description: str = None,
    custom_url: str = None,
    country: str = None,
    language: str = "en",
    category_id: int = None,
    subscriber_count: int = 0,
    video_count: int = 0,
    view_count: int = 0,
    db_path: Path = None,
) -> None:
    """
    Insert or update a channel in the database.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO channels (
            channel_id, title, description, custom_url, country, language,
            category_id, subscriber_count, video_count, view_count, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            custom_url = excluded.custom_url,
            country = excluded.country,
            language = excluded.language,
            category_id = excluded.category_id,
            subscriber_count = excluded.subscriber_count,
            video_count = excluded.video_count,
            view_count = excluded.view_count,
            updated_at = excluded.updated_at
    """, (
        channel_id, title, description, custom_url, country, language,
        category_id, subscriber_count, video_count, view_count,
        datetime.utcnow().isoformat()
    ))
    
    conn.commit()
    conn.close()


def insert_video_evaluation(
    video_id: str,
    channel_id: str,
    title: str,
    duration_seconds: int,
    evaluation_result: dict,
    video_score: float = None,
    description: str = None,
    published_at: str = None,
    category_id: int = None,
    view_count: int = 0,
    like_count: int = 0,
    comment_count: int = 0,
    transcript_text: str = None,
    transcript_language: str = None,
    thumbnail_url: str = None,
    thumbnail_result: dict = None,
    thumbnail_score: float = None,
    combined_score: float = None,
    db_path: Path = None,
) -> None:
    """
    Insert or update a video evaluation in the database.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Extract evaluation metrics
    success = evaluation_result.get("evaluation_success", False)

    # Extract thumbnail metrics
    thumb_success = thumbnail_result.get("evaluation_success", False) if thumbnail_result else False
    thumb_elements = None
    if thumbnail_result and thumbnail_result.get("thumbnail_visual_elements"):
        import json
        thumb_elements = json.dumps(thumbnail_result.get("thumbnail_visual_elements"))

    cursor.execute("""
        INSERT INTO video_evaluations (
            video_id, channel_id, title, description, published_at, category_id,
            duration_seconds, view_count, like_count, comment_count,
            title_content_similarity_score, deception_score, focus_ratio,
            time_to_main_content, sponsor_interruption_score, video_score,
            title_analysis_reasoning, deception_reasoning, focus_reasoning,
            time_reasoning, sponsor_reasoning, llm_explanation,
            transcript_text, transcript_language,
            thumbnail_url, thumbnail_content_match, thumbnail_sensationalism_score,
            thumbnail_deception_flag, thumbnail_visual_elements, thumbnail_explanation,
            thumbnail_score, thumbnail_evaluation_success, combined_score,
            evaluation_success, evaluation_error, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            title_content_similarity_score = excluded.title_content_similarity_score,
            deception_score = excluded.deception_score,
            focus_ratio = excluded.focus_ratio,
            time_to_main_content = excluded.time_to_main_content,
            sponsor_interruption_score = excluded.sponsor_interruption_score,
            video_score = excluded.video_score,
            title_analysis_reasoning = excluded.title_analysis_reasoning,
            deception_reasoning = excluded.deception_reasoning,
            focus_reasoning = excluded.focus_reasoning,
            time_reasoning = excluded.time_reasoning,
            sponsor_reasoning = excluded.sponsor_reasoning,
            llm_explanation = excluded.llm_explanation,
            transcript_text = excluded.transcript_text,
            transcript_language = excluded.transcript_language,
            thumbnail_url = excluded.thumbnail_url,
            thumbnail_content_match = excluded.thumbnail_content_match,
            thumbnail_sensationalism_score = excluded.thumbnail_sensationalism_score,
            thumbnail_deception_flag = excluded.thumbnail_deception_flag,
            thumbnail_visual_elements = excluded.thumbnail_visual_elements,
            thumbnail_explanation = excluded.thumbnail_explanation,
            thumbnail_score = excluded.thumbnail_score,
            thumbnail_evaluation_success = excluded.thumbnail_evaluation_success,
            combined_score = excluded.combined_score,
            evaluation_success = excluded.evaluation_success,
            evaluation_error = excluded.evaluation_error,
            evaluated_at = excluded.evaluated_at
    """, (
        video_id, channel_id, title, description, published_at, category_id,
        duration_seconds, view_count, like_count, comment_count,
        evaluation_result.get("title_content_similarity_score"),
        evaluation_result.get("deception_score"),
        evaluation_result.get("focus_ratio"),
        evaluation_result.get("time_to_main_content"),
        evaluation_result.get("sponsor_interruption_score"),
        video_score,
        evaluation_result.get("title_analysis_reasoning"),
        evaluation_result.get("deception_reasoning"),
        evaluation_result.get("focus_reasoning"),
        evaluation_result.get("time_reasoning"),
        evaluation_result.get("sponsor_reasoning"),
        evaluation_result.get("llm_explanation"),
        transcript_text,
        transcript_language,
        thumbnail_url,
        thumbnail_result.get("thumbnail_content_match") if thumbnail_result else None,
        thumbnail_result.get("thumbnail_sensationalism_score") if thumbnail_result else None,
        1 if thumbnail_result and thumbnail_result.get("thumbnail_deception_flag") else 0,
        thumb_elements,
        thumbnail_result.get("thumbnail_explanation") if thumbnail_result else None,
        thumbnail_score,
        1 if thumb_success else 0,
        combined_score,
        1 if success else 0,
        evaluation_result.get("error"),
        datetime.utcnow().isoformat()
    ))

    conn.commit()
    conn.close()


def update_channel_score(channel_id: str, channel_score: float, videos_evaluated: int, db_path: Path = None) -> None:
    """
    Update a channel's score and evaluation count.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE channels
        SET channel_score = ?,
            videos_evaluated = ?,
            last_evaluated_at = ?,
            updated_at = ?
        WHERE channel_id = ?
    """, (
        channel_score,
        videos_evaluated,
        datetime.utcnow().isoformat(),
        datetime.utcnow().isoformat(),
        channel_id
    ))
    
    conn.commit()
    conn.close()


def get_ranking_global_english(limit: int = 100, db_path: Path = None) -> list[dict]:
    """
    Get top English channels globally.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM ranking_global_english
        LIMIT ?
    """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_ranking_by_category_global(category_id: int = None, limit_per_category: int = 10, db_path: Path = None) -> list[dict]:
    """
    Get top channels by category (English, global).
    
    Args:
        category_id: Optional. If provided, filter to this category only.
        limit_per_category: Maximum channels per category
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    if category_id:
        cursor.execute("""
            SELECT * FROM ranking_category_global_english
            WHERE category_id = ? AND category_rank <= ?
        """, (category_id, limit_per_category))
    else:
        cursor.execute("""
            SELECT * FROM ranking_category_global_english
            WHERE category_rank <= ?
        """, (limit_per_category,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_ranking_india(category_id: int = None, limit_per_category: int = 10, db_path: Path = None) -> list[dict]:
    """
    Get top channels by category (India, English + Hindi).
    
    Args:
        category_id: Optional. If provided, filter to this category only.
        limit_per_category: Maximum channels per category
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    if category_id:
        cursor.execute("""
            SELECT * FROM ranking_category_india
            WHERE category_id = ? AND category_rank <= ?
        """, (category_id, limit_per_category))
    else:
        cursor.execute("""
            SELECT * FROM ranking_category_india
            WHERE category_rank <= ?
        """, (limit_per_category,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_channel(channel_id: str, db_path: Path = None) -> Optional[dict]:
    """
    Get a single channel by ID.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,))
    row = cursor.fetchone()
    conn.close()
    
    return dict(row) if row else None


def get_channel_videos(channel_id: str, db_path: Path = None) -> list[dict]:
    """
    Get all evaluated videos for a channel.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM video_evaluations
        WHERE channel_id = ?
        ORDER BY evaluated_at DESC
    """, (channel_id,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_all_channels(db_path: Path = None) -> list[dict]:
    """
    Get all channels.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM channels ORDER BY channel_score DESC NULLS LAST")
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_database_stats(db_path: Path = None) -> dict:
    """
    Get database statistics.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM channels")
    total_channels = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM channels WHERE channel_score IS NOT NULL")
    evaluated_channels = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM video_evaluations")
    total_videos = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM video_evaluations WHERE evaluation_success = 1")
    successful_evaluations = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        "total_channels": total_channels,
        "evaluated_channels": evaluated_channels,
        "total_videos": total_videos,
        "successful_evaluations": successful_evaluations,
    }


def log_subtitle_attempt(
    *,
    video_id: str,
    channel_id: str | None,
    attempt_no: int,
    source: str,
    auth_mode: str,
    started_at: str,
    ended_at: str,
    success: bool,
    failure_code: str | None = None,
    failure_detail: str | None = None,
    transcript_language: str | None = None,
    content_hash: str | None = None,
    db_path: Path = None,
) -> None:
    """Append one subtitle fetch attempt row."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO subtitle_attempts (
            video_id, channel_id, attempt_no, source, auth_mode,
            started_at, ended_at, success,
            failure_code, failure_detail, transcript_language, content_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            video_id,
            channel_id,
            int(attempt_no),
            source,
            auth_mode,
            started_at,
            ended_at,
            1 if success else 0,
            failure_code,
            failure_detail,
            transcript_language,
            content_hash,
        ),
    )
    conn.commit()
    conn.close()


def upsert_subtitle_resolution(
    *,
    video_id: str,
    channel_id: str | None,
    status: str,
    winning_source: str | None = None,
    language_code: str | None = None,
    fetched_at: str | None = None,
    content_hash: str | None = None,
    attempts: int = 0,
    last_failure_code: str | None = None,
    last_failure_detail: str | None = None,
    db_path: Path = None,
) -> None:
    """Create/update final subtitle resolution state for one video."""
    now = datetime.utcnow().isoformat()
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO subtitle_resolution (
            video_id, channel_id, status, winning_source, language_code,
            fetched_at, content_hash, attempts, last_failure_code, last_failure_detail, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            channel_id = excluded.channel_id,
            status = excluded.status,
            winning_source = excluded.winning_source,
            language_code = excluded.language_code,
            fetched_at = excluded.fetched_at,
            content_hash = excluded.content_hash,
            attempts = excluded.attempts,
            last_failure_code = excluded.last_failure_code,
            last_failure_detail = excluded.last_failure_detail,
            updated_at = excluded.updated_at
        """,
        (
            video_id,
            channel_id,
            status,
            winning_source,
            language_code,
            fetched_at,
            content_hash,
            int(attempts or 0),
            last_failure_code,
            last_failure_detail,
            now,
        ),
    )
    conn.commit()
    conn.close()


def upsert_channel_coverage_status(
    *,
    channel_id: str,
    handle: str | None,
    required_videos: int,
    resolved_videos: int,
    actual_latest_count: int,
    actual_popular_count: int,
    ratio_drift: float,
    status: str,
    details_json: str | None = None,
    db_path: Path = None,
) -> None:
    """Cache channel coverage status from coverage report/check."""
    now = datetime.utcnow().isoformat()
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO channel_coverage_status (
            channel_id, handle, required_videos, resolved_videos,
            actual_latest_count, actual_popular_count, ratio_drift,
            status, details_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            handle = excluded.handle,
            required_videos = excluded.required_videos,
            resolved_videos = excluded.resolved_videos,
            actual_latest_count = excluded.actual_latest_count,
            actual_popular_count = excluded.actual_popular_count,
            ratio_drift = excluded.ratio_drift,
            status = excluded.status,
            details_json = excluded.details_json,
            updated_at = excluded.updated_at
        """,
        (
            channel_id,
            handle,
            int(required_videos),
            int(resolved_videos),
            int(actual_latest_count),
            int(actual_popular_count),
            float(ratio_drift),
            status,
            details_json,
            now,
        ),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    # Initialize database when run directly
    init_database()
    print("\nDatabase stats:")
    print(get_database_stats())
