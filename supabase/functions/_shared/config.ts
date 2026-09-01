// Single source of truth for extension-pool thresholds + model config.
// PROVISIONAL values (final tuning happens at the end). Pure constants only -
// no runtime access here, so node unit tests can import this file directly.

export const GATES = {
  MUSIC_CATEGORY: "Music",
  MAX_DURATION_S: 5400,
  MIN_WPM: 40,
  MIN_COVERAGE: 0.6,
};

export const RETITLE = {
  MIN_DECEPTION: 5,
  MAX_TCS: 4,
};

export const QUOTA_LIMIT = 150;
export const EXPAND_LIMIT = 100;

// A verified public row drops out of the pool and must be re-corroborated once
// this many distinct users flag it (sockpuppet-verified-fake backstop).
export const FLAG_SUPPRESS_THRESHOLD = 3;

// A score is served to OTHER users only after this many DISTINCT users
// independently submit transcripts that agree (same outcome_hash).
export const CORROBORATION_THRESHOLD = 2;

// Sanitizer hard caps (chars) for stored, user-influenced text.
export const MAX_HONEST_TITLE = 100;
export const MAX_VERDICT_LINE = 240;
export const MAX_REASONING = 240;

export const SCORER_VERSION = 3;
// Ship model (2026-07-17): Gemini 3.5 Flash-Lite via its OpenAI-compatible endpoint.
// deepseek-chat was deprecated; deepseek-v4-flash was too slow (~10s). Gemini flash-lite
// is ~1s, $0.10/$0.40, free tier. Swappable via these two constants + LLM_KEY_ENV secret.
export const MODEL = "gemini-3.5-flash-lite";
export const LLM_BASE = "https://generativelanguage.googleapis.com/v1beta/openai";
export const LLM_KEY_ENV = "GEMINI_API_KEY";
