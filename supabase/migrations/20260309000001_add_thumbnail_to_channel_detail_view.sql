-- Add thumbnail_url to channel_detail view
DROP VIEW IF EXISTS channel_detail;

CREATE VIEW channel_detail AS
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

GRANT SELECT ON channel_detail TO anon, authenticated;
