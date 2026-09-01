-- Fix: revoke default PUBLIC execute on credit RPCs
-- PostgreSQL grants EXECUTE to PUBLIC by default on new functions.
-- Our SECURITY DEFINER functions bypass RLS, so access must be explicit.

REVOKE EXECUTE ON FUNCTION check_credits(UUID, INTEGER) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION check_credits(UUID, INTEGER) FROM anon;
GRANT EXECUTE ON FUNCTION check_credits(UUID, INTEGER) TO authenticated;
GRANT EXECUTE ON FUNCTION check_credits(UUID, INTEGER) TO service_role;

REVOKE EXECUTE ON FUNCTION use_credits(UUID, INTEGER) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION use_credits(UUID, INTEGER) FROM anon;
GRANT EXECUTE ON FUNCTION use_credits(UUID, INTEGER) TO service_role;

-- Fix: use_credits should not go negative
CREATE OR REPLACE FUNCTION use_credits(p_user_id UUID, p_count INTEGER DEFAULT 1)
RETURNS INTEGER AS $$
DECLARE
    remaining INTEGER;
BEGIN
    SELECT credits_total - credits_used INTO remaining
    FROM user_eval_credits
    WHERE user_id = p_user_id;

    IF remaining IS NULL OR remaining < p_count THEN
        RETURN -1;
    END IF;

    UPDATE user_eval_credits
    SET credits_used = credits_used + p_count, updated_at = NOW()
    WHERE user_id = p_user_id;

    RETURN remaining - p_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Re-apply grants after CREATE OR REPLACE (resets to PUBLIC default)
REVOKE EXECUTE ON FUNCTION use_credits(UUID, INTEGER) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION use_credits(UUID, INTEGER) FROM anon;
GRANT EXECUTE ON FUNCTION use_credits(UUID, INTEGER) TO service_role;

-- Fix: eval_cache needs explicit deny for anon writes
-- Current RLS has service_role policy for ALL, but no explicit deny for anon/authenticated writes.
-- PostgREST returns 204 on UPDATE/DELETE with no matching rows (silent no-op).
-- Add explicit authenticated write policy scoped to own requested_by rows (future-proofing).
-- anon has no INSERT/UPDATE/DELETE policy, so RLS blocks by default. The 204 is correct behavior
-- (0 rows matched the RLS filter).
