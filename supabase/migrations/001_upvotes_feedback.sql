-- Upvote/downvote counter (single row)
CREATE TABLE IF NOT EXISTS site_upvotes (
    id             INTEGER PRIMARY KEY DEFAULT 1,
    upvote_count   INTEGER NOT NULL DEFAULT 0,
    downvote_count INTEGER NOT NULL DEFAULT 0
);

INSERT INTO site_upvotes (id, upvote_count, downvote_count) VALUES (1, 0, 0) ON CONFLICT DO NOTHING;

ALTER TABLE site_upvotes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read upvotes" ON site_upvotes FOR SELECT TO anon USING (true);

CREATE OR REPLACE FUNCTION increment_upvote()
RETURNS void AS $$
BEGIN
    UPDATE site_upvotes SET upvote_count = upvote_count + 1 WHERE id = 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION increment_downvote()
RETURNS void AS $$
BEGIN
    UPDATE site_upvotes SET downvote_count = downvote_count + 1 WHERE id = 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

GRANT EXECUTE ON FUNCTION increment_upvote() TO anon;
GRANT EXECUTE ON FUNCTION increment_downvote() TO anon;

-- Feedback table
CREATE TABLE IF NOT EXISTS feedback (
    id         SERIAL PRIMARY KEY,
    message    TEXT NOT NULL,
    ip_hash    TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon insert feedback" ON feedback FOR INSERT TO anon WITH CHECK (true);
