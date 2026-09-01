// All extension constants live here (contract §6). Single source of truth.

// Supabase project (public anon key by design - baked from repo root .env).
export const SUPABASE_URL = "https://qhiwvwlbtlevltfblxso.supabase.co";
export const SUPABASE_ANON_KEY =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFoaXd2d2xidGxldmx0ZmJseHNvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI3MDkxNDMsImV4cCI6MjA4ODI4NTE0M30.wsgdjkTi0xILWld5Bg46aRsSLZ-tdYBmqdbRTa1mEkk";

// MOCK mode: sw.js serves deterministic fake rows so the whole UI is testable
// with no backend. Default false. Tests flip this to true.
export const MOCK = false; // live: hits the deployed Supabase backend

// Endpoints derived from SUPABASE_URL.
export const REST_URL = `${SUPABASE_URL}/rest/v1/video_scores`;
export const FN_SCORE = `${SUPABASE_URL}/functions/v1/score-video`;
export const FN_EXPAND = `${SUPABASE_URL}/functions/v1/expand-video`;
export const FN_FLAG = `${SUPABASE_URL}/functions/v1/flag-video`;
export const AUTH_AUTHORIZE = `${SUPABASE_URL}/auth/v1/authorize`;
export const AUTH_TOKEN = `${SUPABASE_URL}/auth/v1/token`;

// Bulk read select (contract §1).
export const BULK_SELECT =
  "video_id,status,unscoreable_reason,video_score,tcs,deception,focus,ttmc,sponsor,honest_title,verdict_line,main_content_start_s,language,scorer_version,reasonings";

// Behaviour constants.
export const DWELL_MS = 600; // visible-in-viewport before a video becomes an eval candidate
export const BULK_DEBOUNCE_MS = 300;
export const BULK_MAX_IDS = 50;
export const DIM_THRESHOLD = 40; // video_score < this -> dim thumbnail

// Cache TTLs (ms).
export const TTL_SCORED = 24 * 60 * 60 * 1000; // 24h
export const TTL_UNSCOREABLE = 7 * 24 * 60 * 60 * 1000; // 7d

// Storage keys.
export const K_SETTINGS = "settings";
export const K_AUTH = "auth";
export const K_QUOTA = "quota";
export const K_KILLED = "killed";
export const CACHE_PREFIX = "score:";

// Default settings.
export const DEFAULT_SETTINGS = {
  enabled: true,
  retitleMode: "bad", // "off" | "bad" | "aggressive"
  dim: true,
  showDots: false, // placeholder dot on unscoreable videos
};

// Message namespace (window.postMessage bridge to MAIN world).
export const MSG_FETCH = "ACB_FETCH_TRANSCRIPT";
export const MSG_RESULT = "ACB_TRANSCRIPT_RESULT";

// Human-readable unscoreable reasons for tooltips.
export const REASON_LABEL = {
  music: "Music video",
  live: "Live stream",
  too_long: "Too long to score",
  no_captions: "No captions",
  low_density: "Not enough spoken content",
  kill: "Disabled",
};

// Ported from web/app/lib/api.ts scoreColor().
export function scoreColor(score) {
  const s = Math.max(0, Math.min(100, score));
  const stops = [
    [248, 81, 73],
    [230, 100, 40],
    [210, 153, 34],
    [200, 170, 40],
    [63, 185, 80],
    [46, 160, 67],
  ];
  const breakpoints = [0, 20, 50, 65, 80, 100];
  let i = 0;
  for (i = 0; i < breakpoints.length - 2; i++) {
    if (s <= breakpoints[i + 1]) break;
  }
  const t = (s - breakpoints[i]) / (breakpoints[i + 1] - breakpoints[i]);
  const r = Math.round(stops[i][0] + (stops[i + 1][0] - stops[i][0]) * t);
  const g = Math.round(stops[i][1] + (stops[i + 1][1] - stops[i][1]) * t);
  const b = Math.round(stops[i][2] + (stops[i + 1][2] - stops[i][2]) * t);
  return `rgb(${r},${g},${b})`;
}
