-- ============================================================
-- Crowd-corroboration + atomic quota + M3 hardening
-- Fixes C2/C3 (a score reaches OTHER users only after 2 DISTINCT users submit
-- agreeing transcripts), H2 (atomic quota reserve), M3 (search_path on every
-- SECURITY DEFINER function). Builds on 20260716000001 (keep its H1 column grant).
-- ============================================================

-- Per-user, per-outcome corroboration ledger. PK (video_id, user_id) dedups so a
-- user corroborates only their latest outcome for a video.
CREATE TABLE IF NOT EXISTS video_submissions (
    video_id     TEXT NOT NULL,
    user_id      UUID NOT NULL REFERENCES auth.users(id),
    outcome_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (video_id, user_id)
);
CREATE INDEX IF NOT EXISTS video_submissions_hash_idx
    ON video_submissions (video_id, outcome_hash);

ALTER TABLE video_submissions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users read own submissions"
    ON video_submissions FOR SELECT TO authenticated
    USING (user_id = auth.uid());

CREATE POLICY "service write submissions"
    ON video_submissions FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Corroboration columns on the pool. `verified` gates the public read; `outcome_hash`
-- and `agree_count` are internal (NOT added to the anon column grant, so they stay
-- hidden like transcript_hash/requested_by).
ALTER TABLE video_scores ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE video_scores ADD COLUMN IF NOT EXISTS outcome_hash TEXT;
ALTER TABLE video_scores ADD COLUMN IF NOT EXISTS agree_count INT NOT NULL DEFAULT 1;

-- ------------------------------------------------------------
-- Serving: the public (anon/authenticated) bulk read now returns ONLY verified
-- rows. Provisional/unverified rows are invisible to everyone except the writing
-- Edge Functions (service_role bypasses RLS), which return a submitter their own
-- provisional/ephemeral result directly in the POST response.
-- ------------------------------------------------------------
DROP POLICY IF EXISTS "public read video_scores" ON video_scores;
CREATE POLICY "public read verified video_scores"
    ON video_scores FOR SELECT TO anon, authenticated USING (verified = true);

-- ============================================================
-- Atomic quota (H2): reserve BEFORE the LLM call, refund on failure. One statement,
-- so concurrent requests can never both slip past the daily cap.
-- ============================================================

-- Atomically +1 today's fresh_evals and return the NEW count, but only while under
-- p_limit. Returns -1 (no increment) when already at/over the limit.
CREATE OR REPLACE FUNCTION public.ext_reserve_fresh(p_user_id UUID, p_limit INT)
RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v INT;
BEGIN
    INSERT INTO public.ext_usage (user_id, day, fresh_evals)
    VALUES (p_user_id, CURRENT_DATE, 1)
    ON CONFLICT (user_id, day) DO UPDATE
        SET fresh_evals = public.ext_usage.fresh_evals + 1
        WHERE public.ext_usage.fresh_evals < p_limit
    RETURNING fresh_evals INTO v;
    IF v IS NULL THEN RETURN -1; END IF;  -- conflict update skipped: over limit
    RETURN v;
END;
$$;

-- Give back one fresh_eval (floor 0) when a reserved LLM call fails.
CREATE OR REPLACE FUNCTION public.ext_refund_fresh(p_user_id UUID)
RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v INT;
BEGIN
    UPDATE public.ext_usage
        SET fresh_evals = GREATEST(public.ext_usage.fresh_evals - 1, 0)
        WHERE user_id = p_user_id AND day = CURRENT_DATE
    RETURNING fresh_evals INTO v;
    RETURN COALESCE(v, 0);
END;
$$;

-- Same atomic reserve for expand-video against its own small daily cap.
CREATE OR REPLACE FUNCTION public.ext_reserve_expand(p_user_id UUID, p_limit INT)
RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v INT;
BEGIN
    INSERT INTO public.ext_usage (user_id, day, expands)
    VALUES (p_user_id, CURRENT_DATE, 1)
    ON CONFLICT (user_id, day) DO UPDATE
        SET expands = public.ext_usage.expands + 1
        WHERE public.ext_usage.expands < p_limit
    RETURNING expands INTO v;
    IF v IS NULL THEN RETURN -1; END IF;
    RETURN v;
END;
$$;

-- Give back one expand (floor 0) when a reserved detail-pass LLM call fails.
CREATE OR REPLACE FUNCTION public.ext_refund_expand(p_user_id UUID)
RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v INT;
BEGIN
    UPDATE public.ext_usage
        SET expands = GREATEST(public.ext_usage.expands - 1, 0)
        WHERE user_id = p_user_id AND day = CURRENT_DATE
    RETURNING expands INTO v;
    RETURN COALESCE(v, 0);
END;
$$;

-- ============================================================
-- M3: re-declare the existing SECURITY DEFINER helpers WITH search_path pinned
-- and schema-qualified. ext_bump_fresh/ext_bump_expands are superseded by the
-- reserve/refund RPCs above but kept (harmless) and hardened for completeness.
-- ============================================================

CREATE OR REPLACE FUNCTION public.ext_bump_fresh(p_user_id UUID)
RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v INT;
BEGIN
    INSERT INTO public.ext_usage (user_id, day, fresh_evals)
    VALUES (p_user_id, CURRENT_DATE, 1)
    ON CONFLICT (user_id, day) DO UPDATE
        SET fresh_evals = public.ext_usage.fresh_evals + 1
    RETURNING fresh_evals INTO v;
    RETURN v;
END;
$$;

CREATE OR REPLACE FUNCTION public.ext_bump_expands(p_user_id UUID)
RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v INT;
BEGIN
    INSERT INTO public.ext_usage (user_id, day, expands)
    VALUES (p_user_id, CURRENT_DATE, 1)
    ON CONFLICT (user_id, day) DO UPDATE
        SET expands = public.ext_usage.expands + 1
    RETURNING expands INTO v;
    RETURN v;
END;
$$;

CREATE OR REPLACE FUNCTION public.ext_bump_flag(p_video_id TEXT)
RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE v INT;
BEGIN
    UPDATE public.video_scores SET flags_count = flags_count + 1
    WHERE video_id = p_video_id
    RETURNING flags_count INTO v;
    RETURN v;
END;
$$;

-- Suppress a sockpuppet-verified fake: flip the row back to unverified and wipe its
-- corroboration so it drops out of the public pool and must be re-corroborated.
-- Called by flag-video once distinct flags reach the threshold.
CREATE OR REPLACE FUNCTION public.ext_suppress_video(p_video_id TEXT)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
    UPDATE public.video_scores
        SET verified = false, agree_count = 1
        WHERE video_id = p_video_id;
    DELETE FROM public.video_submissions WHERE video_id = p_video_id;
END;
$$;

GRANT EXECUTE ON FUNCTION public.ext_reserve_fresh(UUID, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.ext_refund_fresh(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.ext_reserve_expand(UUID, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.ext_refund_expand(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.ext_suppress_video(TEXT) TO service_role;
