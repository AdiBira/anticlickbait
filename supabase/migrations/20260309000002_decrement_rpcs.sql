CREATE OR REPLACE FUNCTION decrement_upvote()
RETURNS void AS $$
BEGIN
    UPDATE site_upvotes SET upvote_count = GREATEST(upvote_count - 1, 0) WHERE id = 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION decrement_downvote()
RETURNS void AS $$
BEGIN
    UPDATE site_upvotes SET downvote_count = GREATEST(downvote_count - 1, 0) WHERE id = 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

GRANT EXECUTE ON FUNCTION decrement_upvote() TO anon;
GRANT EXECUTE ON FUNCTION decrement_downvote() TO anon;
