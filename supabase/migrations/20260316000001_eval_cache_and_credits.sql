-- ============================================================
-- Eval cache + user credits for user-submitted evaluations
-- ============================================================

-- Global eval cache (shared across all users)
CREATE TABLE IF NOT EXISTS eval_cache (
    video_id                        TEXT PRIMARY KEY,
    title                           TEXT NOT NULL,
    duration_seconds                INTEGER,
    status                          TEXT NOT NULL DEFAULT 'pending',
    video_score                     REAL,
    title_content_similarity_score  REAL,
    deception_score                 REAL,
    focus_ratio                     REAL,
    time_to_main_content            REAL,
    sponsor_interruption_score      REAL,
    title_analysis_reasoning        TEXT,
    deception_reasoning             TEXT,
    focus_reasoning                 TEXT,
    time_reasoning                  TEXT,
    sponsor_reasoning               TEXT,
    evaluation_error                TEXT,
    eval_model                      TEXT DEFAULT 'gpt-4.1-nano',
    requested_by                    UUID REFERENCES auth.users(id),
    created_at                      TIMESTAMPTZ DEFAULT NOW(),
    completed_at                    TIMESTAMPTZ
);

-- Lifetime credit tracking
CREATE TABLE IF NOT EXISTS user_eval_credits (
    user_id         UUID PRIMARY KEY REFERENCES auth.users(id),
    credits_used    INTEGER NOT NULL DEFAULT 0,
    credits_total   INTEGER NOT NULL DEFAULT 30,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_eval_cache_status ON eval_cache (status);
CREATE INDEX IF NOT EXISTS idx_eval_cache_requested_by ON eval_cache (requested_by);

-- ============================================================
-- RPCs for credit management
-- ============================================================

-- Check if user has enough credits (called before starting eval)
-- Returns remaining credits, or -1 if insufficient
CREATE OR REPLACE FUNCTION check_credits(p_user_id UUID, p_count INTEGER DEFAULT 1)
RETURNS INTEGER AS $$
DECLARE
    remaining INTEGER;
BEGIN
    INSERT INTO user_eval_credits (user_id)
    VALUES (p_user_id)
    ON CONFLICT (user_id) DO NOTHING;

    SELECT credits_total - credits_used INTO remaining
    FROM user_eval_credits
    WHERE user_id = p_user_id;

    IF remaining < p_count THEN
        RETURN -1;
    END IF;

    RETURN remaining;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Deduct credits (called by Edge Function on successful eval)
-- Returns remaining credits after deduction
CREATE OR REPLACE FUNCTION use_credits(p_user_id UUID, p_count INTEGER DEFAULT 1)
RETURNS INTEGER AS $$
DECLARE
    remaining INTEGER;
BEGIN
    UPDATE user_eval_credits
    SET credits_used = credits_used + p_count, updated_at = NOW()
    WHERE user_id = p_user_id;

    SELECT credits_total - credits_used INTO remaining
    FROM user_eval_credits
    WHERE user_id = p_user_id;

    RETURN remaining;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ============================================================
-- RLS
-- ============================================================

ALTER TABLE eval_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_eval_credits ENABLE ROW LEVEL SECURITY;

-- eval_cache: anyone can read, only service_role can write
CREATE POLICY "public read eval_cache"
    ON eval_cache FOR SELECT TO anon, authenticated USING (true);

CREATE POLICY "service write eval_cache"
    ON eval_cache FOR ALL TO service_role USING (true) WITH CHECK (true);

-- user_eval_credits: authenticated users can read own row only
CREATE POLICY "users read own credits"
    ON user_eval_credits FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "service write credits"
    ON user_eval_credits FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Grant RPC execute to authenticated users (check_credits)
-- and service_role (use_credits)
GRANT EXECUTE ON FUNCTION check_credits(UUID, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION check_credits(UUID, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION use_credits(UUID, INTEGER) TO service_role;
