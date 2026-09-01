-- Remove anon read access from eval_cache
-- Option D: all eval features require sign-in
DROP POLICY IF EXISTS "public read eval_cache" ON eval_cache;

CREATE POLICY "authenticated read eval_cache"
    ON eval_cache FOR SELECT TO authenticated USING (true);
