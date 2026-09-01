-- Add channel_id and channel_name to eval_cache for grouping channel evals in history
ALTER TABLE eval_cache ADD COLUMN IF NOT EXISTS channel_id TEXT;
ALTER TABLE eval_cache ADD COLUMN IF NOT EXISTS channel_name TEXT;
