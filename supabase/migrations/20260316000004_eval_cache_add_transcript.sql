-- Add transcript_text column to eval_cache
-- Vercel function stores transcript here after IPRoyal fetch.
-- Edge Function reads it from here instead of receiving it in the POST body.
ALTER TABLE eval_cache ADD COLUMN IF NOT EXISTS transcript_text TEXT;
