-- ============================================================
-- Anticlickbait — Supabase Schema
-- PostgreSQL
-- ============================================================


-- ============================================================
-- TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS categories (
    category_id   INTEGER PRIMARY KEY,
    category_name TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id        TEXT        PRIMARY KEY,
    handle            TEXT,
    title             TEXT        NOT NULL,
    description       TEXT,
    custom_url        TEXT,
    country           TEXT,
    language          TEXT        NOT NULL DEFAULT 'en',
    category_id       INTEGER     REFERENCES categories (category_id),
    subscriber_count  BIGINT      DEFAULT 0,
    video_count       INTEGER     DEFAULT 0,
    view_count        BIGINT      DEFAULT 0,
    channel_score     REAL,
    videos_evaluated  INTEGER     DEFAULT 0,
    last_evaluated_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS video_evaluations (
    video_id          TEXT        PRIMARY KEY,
    channel_id        TEXT        NOT NULL REFERENCES channels (channel_id),
    title             TEXT        NOT NULL,
    description       TEXT,
    published_at      TIMESTAMPTZ,
    category_id       INTEGER     REFERENCES categories (category_id),
    duration_seconds  INTEGER,
    view_count        BIGINT      DEFAULT 0,
    like_count        INTEGER     DEFAULT 0,
    comment_count     INTEGER     DEFAULT 0,
    thumbnail_url     TEXT,

    -- Core text evaluation metrics (v1.1, all 0–10)
    title_content_similarity_score  REAL,   -- 0–10
    deception_score                 REAL,   -- 0–10 (0=honest, 10=deceptive)
    focus_ratio                     REAL,   -- 0–10
    time_to_main_content            REAL,   -- 0–10
    sponsor_interruption_score      REAL,   -- 0–10
    video_score                     REAL,   -- 0–100 final score

    -- Per-metric LLM reasoning
    title_analysis_reasoning  TEXT,
    deception_reasoning       TEXT,
    focus_reasoning           TEXT,
    time_reasoning            TEXT,
    sponsor_reasoning         TEXT,
    llm_explanation           TEXT,

    -- Transcript
    transcript_text      TEXT,
    transcript_language  TEXT,

    -- Thumbnail (reserved for future use)
    thumbnail_content_match         REAL,
    thumbnail_sensationalism_score  REAL,
    thumbnail_deception_flag        BOOLEAN DEFAULT FALSE,
    thumbnail_visual_elements       JSONB,
    thumbnail_explanation           TEXT,
    thumbnail_score                 REAL,
    thumbnail_evaluation_success    BOOLEAN DEFAULT FALSE,

    -- Combined score (text + thumbnail when both available)
    combined_score  REAL,

    -- Metadata
    evaluation_success  BOOLEAN     DEFAULT FALSE,
    evaluation_error    TEXT,
    evaluated_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_channels_score      ON channels (channel_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_channels_country    ON channels (country);
CREATE INDEX IF NOT EXISTS idx_channels_language   ON channels (language);
CREATE INDEX IF NOT EXISTS idx_channels_category   ON channels (category_id);

CREATE INDEX IF NOT EXISTS idx_videos_channel      ON video_evaluations (channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_score        ON video_evaluations (video_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_videos_channel_score ON video_evaluations (channel_id, video_score DESC NULLS LAST);


-- ============================================================
-- VIEWS (used by API endpoints)
-- ============================================================

-- Global English rankings (all categories)
CREATE OR REPLACE VIEW ranking_global AS
SELECT
    ROW_NUMBER() OVER (ORDER BY c.channel_score DESC NULLS LAST) AS rank,
    c.channel_id,
    c.handle,
    c.title,
    c.country,
    c.language,
    c.category_id,
    cat.category_name,
    c.subscriber_count,
    c.channel_score,
    c.videos_evaluated,
    c.last_evaluated_at
FROM channels c
LEFT JOIN categories cat ON cat.category_id = c.category_id
WHERE c.language = 'en'
  AND c.channel_score IS NOT NULL
ORDER BY c.channel_score DESC NULLS LAST;


-- Per-category rankings (English, global)
CREATE OR REPLACE VIEW ranking_by_category AS
SELECT
    ROW_NUMBER() OVER (PARTITION BY c.category_id ORDER BY c.channel_score DESC NULLS LAST) AS category_rank,
    c.channel_id,
    c.handle,
    c.title,
    c.country,
    c.category_id,
    cat.category_name,
    c.subscriber_count,
    c.channel_score,
    c.videos_evaluated
FROM channels c
LEFT JOIN categories cat ON cat.category_id = c.category_id
WHERE c.language = 'en'
  AND c.channel_score IS NOT NULL
ORDER BY c.category_id, c.channel_score DESC NULLS LAST;


-- Channel detail view (joins channel + aggregated video stats)
CREATE OR REPLACE VIEW channel_detail AS
SELECT
    c.channel_id,
    c.handle,
    c.title,
    c.description,
    c.country,
    c.language,
    c.category_id,
    cat.category_name,
    c.subscriber_count,
    c.video_count,
    c.view_count,
    c.channel_score,
    c.videos_evaluated,
    c.last_evaluated_at,
    c.thumbnail_url,
    -- Aggregated video stats
    AVG(v.title_content_similarity_score)  AS avg_similarity,
    AVG(v.focus_ratio)                     AS avg_focus,
    AVG(v.deception_score)                 AS avg_deception,
    AVG(v.sponsor_interruption_score)      AS avg_sponsor,
    COUNT(v.video_id)                      AS total_videos_evaluated
FROM channels c
LEFT JOIN categories cat ON cat.category_id = c.category_id
LEFT JOIN video_evaluations v
    ON v.channel_id = c.channel_id
    AND v.evaluation_success = TRUE
GROUP BY c.channel_id, cat.category_name;


-- ============================================================
-- ROW LEVEL SECURITY
-- Public read-only (no auth required for rankings site)
-- ============================================================

ALTER TABLE categories        ENABLE ROW LEVEL SECURITY;
ALTER TABLE channels          ENABLE ROW LEVEL SECURITY;
ALTER TABLE video_evaluations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public read categories"
    ON categories FOR SELECT TO anon USING (true);

CREATE POLICY "public read channels"
    ON channels FOR SELECT TO anon USING (true);

CREATE POLICY "public read video_evaluations"
    ON video_evaluations FOR SELECT TO anon USING (true);
