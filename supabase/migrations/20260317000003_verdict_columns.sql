-- Add verdict-related columns
-- content_summary: factual 1-sentence summary of what the video covers (from Call 1)
-- verdict: 3-4 sentence editorial assessment (from new verdict LLM call)
-- channel_summary: 3-5 sentence channel pattern analysis (from new channel LLM call)

ALTER TABLE eval_cache ADD COLUMN IF NOT EXISTS content_summary TEXT;
ALTER TABLE eval_cache ADD COLUMN IF NOT EXISTS verdict TEXT;

ALTER TABLE video_evaluations ADD COLUMN IF NOT EXISTS content_summary TEXT;
ALTER TABLE video_evaluations ADD COLUMN IF NOT EXISTS verdict TEXT;

ALTER TABLE channels ADD COLUMN IF NOT EXISTS channel_summary TEXT;
