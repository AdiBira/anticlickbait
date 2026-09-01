-- ============================================================
-- Extension v1 pool: video_scores + ext_usage + ext_flags
-- New tables only. Nothing existing is modified.
-- Spec §4.1. Contract column list is the source of truth.
-- ============================================================

-- Global score pool (anon-readable, service-role writes)
CREATE TABLE IF NOT EXISTS video_scores (
    video_id            TEXT PRIMARY KEY,
    status              TEXT NOT NULL CHECK (status IN ('scored', 'unscoreable')),
    unscoreable_reason  TEXT,                       -- music|live|too_long|no_captions|low_density
    scorer_version      INT NOT NULL DEFAULT 2,
    model               TEXT,
    language            TEXT,
    title               TEXT,                       -- original, fetched server-side
    honest_title        TEXT,                       -- null unless retitle-triggered
    verdict_line        TEXT,
    video_score         NUMERIC,
    tcs                 NUMERIC,
    deception           NUMERIC,
    focus               NUMERIC,
    ttmc                NUMERIC,
    sponsor             NUMERIC,
    reasonings          JSONB,                      -- null until detail pass
    main_content_start_s INT,
    duration_s          INT,
    transcript_hash     TEXT,                       -- SHA-256, transcript text NOT stored
    words_per_min       NUMERIC,
    caption_coverage    NUMERIC,
    flags_count         INT NOT NULL DEFAULT 0,
    requested_by        UUID REFERENCES auth.users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detail_at           TIMESTAMPTZ
);

-- Per-user daily quota counters
CREATE TABLE IF NOT EXISTS ext_usage (
    user_id      UUID NOT NULL REFERENCES auth.users(id),
    day          DATE NOT NULL,
    fresh_evals  INT NOT NULL DEFAULT 0,
    expands      INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- User flags on scores (idempotent per user+video)
CREATE TABLE IF NOT EXISTS ext_flags (
    video_id    TEXT NOT NULL,
    user_id     UUID NOT NULL REFERENCES auth.users(id),
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (video_id, user_id)
);

-- ============================================================
-- RLS
-- ============================================================

ALTER TABLE video_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE ext_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE ext_flags ENABLE ROW LEVEL SECURITY;

-- video_scores: anyone reads (bulk PostgREST); only service_role writes
CREATE POLICY "public read video_scores"
    ON video_scores FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "service write video_scores"
    ON video_scores FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ext_usage: user reads own row; only service_role writes
CREATE POLICY "users read own usage"
    ON ext_usage FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "service write usage"
    ON ext_usage FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ext_flags: user reads own flags; only service_role writes
CREATE POLICY "users read own flags"
    ON ext_flags FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "service write flags"
    ON ext_flags FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ------------------------------------------------------------
-- Column-level privacy on video_scores.
-- RLS controls which ROWS a role sees, NOT which COLUMNS. The pool is meant
-- to be world-readable, but requested_by (a user's auth UUID) and
-- transcript_hash must never be exposed to anon/authenticated, or anyone with
-- the public anon key could dump "which user evaluated which videos, when".
-- Column GRANTs are the Postgres mechanism that enforces this.
-- ------------------------------------------------------------
REVOKE SELECT ON video_scores FROM anon, authenticated;
GRANT SELECT (
    video_id, status, unscoreable_reason, scorer_version, model, language,
    title, honest_title, verdict_line, video_score,
    tcs, deception, focus, ttmc, sponsor, reasonings,
    main_content_start_s, duration_s, words_per_min, caption_coverage,
    flags_count, created_at, detail_at
) ON video_scores TO anon, authenticated;

-- ============================================================
-- Atomic counter RPCs (called by Edge Functions via service role)
-- ============================================================

-- Increment today's fresh_evals for a user; returns the new count.
CREATE OR REPLACE FUNCTION ext_bump_fresh(p_user_id UUID)
RETURNS INT AS $$
DECLARE v INT;
BEGIN
    INSERT INTO ext_usage (user_id, day, fresh_evals)
    VALUES (p_user_id, CURRENT_DATE, 1)
    ON CONFLICT (user_id, day) DO UPDATE
        SET fresh_evals = ext_usage.fresh_evals + 1
    RETURNING fresh_evals INTO v;
    RETURN v;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Increment today's expands for a user; returns the new count.
CREATE OR REPLACE FUNCTION ext_bump_expands(p_user_id UUID)
RETURNS INT AS $$
DECLARE v INT;
BEGIN
    INSERT INTO ext_usage (user_id, day, expands)
    VALUES (p_user_id, CURRENT_DATE, 1)
    ON CONFLICT (user_id, day) DO UPDATE
        SET expands = ext_usage.expands + 1
    RETURNING expands INTO v;
    RETURN v;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Bump a score's flags_count; returns the new count.
CREATE OR REPLACE FUNCTION ext_bump_flag(p_video_id TEXT)
RETURNS INT AS $$
DECLARE v INT;
BEGIN
    UPDATE video_scores SET flags_count = flags_count + 1
    WHERE video_id = p_video_id
    RETURNING flags_count INTO v;
    RETURN v;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

GRANT EXECUTE ON FUNCTION ext_bump_fresh(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION ext_bump_expands(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION ext_bump_flag(TEXT) TO service_role;
