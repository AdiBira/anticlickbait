/**
 * Local eval cache - localStorage wrapper for eval results.
 *
 * Pattern:
 *   On mount: read localStorage first (instant), then DB if needed
 *   On eval completion: write to localStorage + DB already has it
 *   On retry: clear localStorage entry, re-trigger eval
 *
 * Keys:
 *   eval:{video_id} -> { status, video_score, metrics, reasoning, timestamp }
 *   channel:{channel_url} -> { channel, videos, timestamp }
 *   credits -> { remaining, timestamp }
 *   video_meta:{video_id} -> { title, thumbnail, timestamp }
 */

import type { EvalResult } from "./api";

const CACHE_PREFIX = "acb_";
const EVAL_TTL = 24 * 60 * 60 * 1000; // 24 hours
const META_TTL = 7 * 24 * 60 * 60 * 1000; // 7 days
const CREDITS_TTL = 60 * 1000; // 60 seconds
const CHANNEL_TTL = Infinity; // never expire - scored channels don't change

interface CachedItem<T> {
  data: T;
  timestamp: number;
}

function getItem<T>(key: string, ttl: number): T | null {
  try {
    const raw = localStorage.getItem(CACHE_PREFIX + key);
    if (!raw) return null;
    const cached: CachedItem<T> = JSON.parse(raw);
    if (Date.now() - cached.timestamp > ttl) {
      localStorage.removeItem(CACHE_PREFIX + key);
      return null;
    }
    return cached.data;
  } catch {
    return null;
  }
}

function setItem<T>(key: string, data: T): void {
  try {
    const cached: CachedItem<T> = { data, timestamp: Date.now() };
    localStorage.setItem(CACHE_PREFIX + key, JSON.stringify(cached));
  } catch {
    // QuotaExceededError - clear oldest entries
    clearOldest();
    try {
      const cached: CachedItem<T> = { data, timestamp: Date.now() };
      localStorage.setItem(CACHE_PREFIX + key, JSON.stringify(cached));
    } catch {
      // Still failing, give up silently
    }
  }
}

function removeItem(key: string): void {
  try {
    localStorage.removeItem(CACHE_PREFIX + key);
  } catch {
    // Ignore
  }
}

function clearOldest(): void {
  try {
    const entries: { key: string; timestamp: number }[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith(CACHE_PREFIX)) {
        try {
          const raw = localStorage.getItem(key);
          if (raw) {
            const parsed = JSON.parse(raw);
            entries.push({ key, timestamp: parsed.timestamp || 0 });
          }
        } catch {
          entries.push({ key, timestamp: 0 });
        }
      }
    }
    // Remove oldest 20%
    entries.sort((a, b) => a.timestamp - b.timestamp);
    const toRemove = Math.max(1, Math.floor(entries.length * 0.2));
    for (let i = 0; i < toRemove; i++) {
      localStorage.removeItem(entries[i].key);
    }
  } catch {
    // Ignore
  }
}

// ─── Video eval cache ────────────────────────────────────────────

export function getCachedEval(videoId: string): EvalResult | null {
  return getItem<EvalResult>(`eval:${videoId}`, EVAL_TTL);
}

export function setCachedEval(videoId: string, result: EvalResult): void {
  setItem(`eval:${videoId}`, result);
}

export function clearCachedEval(videoId: string): void {
  removeItem(`eval:${videoId}`);
}

// ─── Video meta cache ────────────────────────────────────────────

interface VideoMeta {
  title: string;
  thumbnail: string;
  channelName?: string;
}

export function getCachedVideoMeta(videoId: string): VideoMeta | null {
  return getItem<VideoMeta>(`meta:${videoId}`, META_TTL);
}

export function setCachedVideoMeta(videoId: string, meta: VideoMeta): void {
  setItem(`meta:${videoId}`, meta);
}

// ─── Channel cache ───────────────────────────────────────────────

interface ChannelCache {
  channel: { channel_id: string; title: string; thumbnail_url: string };
  videos: Array<{ video_id: string; title: string; duration_seconds: number; view_count: number; pool: string; status: string; cached: boolean }>;
  total_videos: number;
  uncached_count: number;
  credits_needed: number;
}

export function getCachedChannel(channelUrl: string): ChannelCache | null {
  return getItem<ChannelCache>(`channel:${channelUrl}`, CHANNEL_TTL);
}

export function setCachedChannel(channelUrl: string, data: ChannelCache): void {
  setItem(`channel:${channelUrl}`, data);
}

export function clearCachedChannel(channelUrl: string): void {
  removeItem(`channel:${channelUrl}`);
}

// ─── Credits cache ───────────────────────────────────────────────

export function getCachedCredits(): number | null {
  return getItem<number>("credits", CREDITS_TTL);
}

export function setCachedCredits(remaining: number): void {
  setItem("credits", remaining);
}

export function invalidateCredits(): void {
  removeItem("credits");
}
