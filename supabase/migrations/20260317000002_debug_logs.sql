-- Debug logs for Vercel function tracing
CREATE TABLE IF NOT EXISTS debug_logs (
    id BIGSERIAL PRIMARY KEY,
    video_id TEXT,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_debug_logs_video ON debug_logs (video_id);
CREATE INDEX IF NOT EXISTS idx_debug_logs_created ON debug_logs (created_at DESC);

-- Public read for debugging, service_role write
ALTER TABLE debug_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service write debug_logs"
    ON debug_logs FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "authenticated read debug_logs"
    ON debug_logs FOR SELECT TO authenticated USING (true);
