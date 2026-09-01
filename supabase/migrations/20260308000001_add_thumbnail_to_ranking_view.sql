-- Add thumbnail_url to ranking_global view (must DROP + CREATE since column order changed)
DROP VIEW IF EXISTS ranking_global;

CREATE VIEW ranking_global AS
SELECT
    ROW_NUMBER() OVER (ORDER BY c.channel_score DESC) AS rank,
    c.channel_id,
    c.handle,
    c.title,
    c.country,
    c.language,
    c.category_id,
    cat.category_name,
    c.subscriber_count,
    c.channel_score,
    c.thumbnail_url,
    c.videos_evaluated,
    c.last_evaluated_at
FROM channels c
LEFT JOIN categories cat ON c.category_id = cat.category_id
WHERE c.channel_score IS NOT NULL
ORDER BY c.channel_score DESC;

-- Re-grant RLS access (views inherit from base tables, but re-grant select for anon)
GRANT SELECT ON ranking_global TO anon, authenticated;
