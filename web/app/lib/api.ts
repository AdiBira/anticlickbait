const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ||
  (process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : "");

// Shared eval_cache select fields - used by server component, client polling, and refresh queries
export const EVAL_CACHE_FIELDS_VIDEO = "video_id,status,video_score,title_content_similarity_score,deception_score,focus_ratio,time_to_main_content,sponsor_interruption_score,title_analysis_reasoning,deception_reasoning,focus_reasoning,time_reasoning,sponsor_reasoning,evaluation_error,content_summary,verdict";
export const EVAL_CACHE_FIELDS_FULL = "video_id,status,video_score,title,duration_seconds,title_content_similarity_score,deception_score,focus_ratio,time_to_main_content,sponsor_interruption_score,title_analysis_reasoning,deception_reasoning,focus_reasoning,time_reasoning,sponsor_reasoning,content_summary,verdict";

export interface RankedChannel {
  rank: number;
  channel_id: string;
  handle: string;
  title: string;
  country: string;
  category_name: string;
  subscriber_count: number;
  channel_score: number;
  videos_evaluated: number;
  thumbnail_url?: string | null;
}

export interface ChannelDetail {
  channel_id: string;
  handle: string;
  title: string;
  description: string;
  country: string;
  category_name: string;
  subscriber_count: number;
  channel_score: number;
  videos_evaluated: number;
  last_evaluated_at: string;
  avg_similarity: number;
  avg_focus: number;
  avg_deception: number;
  avg_sponsor: number;
  thumbnail_url?: string | null;
}

export interface VideoEvaluation {
  video_id: string;
  title: string;
  thumbnail_url?: string;
  duration_seconds?: number;
  view_count?: number;
  published_at?: string;
  video_score: number;
  title_content_similarity_score: number;
  deception_score: number;
  focus_ratio: number;
  time_to_main_content: number;
  sponsor_interruption_score: number;
  title_analysis_reasoning: string;
  deception_reasoning: string;
  focus_reasoning: string;
  time_reasoning: string;
  sponsor_reasoning: string;
}

export interface ChannelResponse {
  channel: ChannelDetail;
  videos: VideoEvaluation[];
}

export interface TranscriptResponse {
  video_id: string;
  transcript_text: string | null;
  transcript_language: string;
}

export async function fetchRankings(params?: {
  category?: string;
  country?: string;
  sort?: string;
}): Promise<RankedChannel[]> {
  const searchParams = new URLSearchParams();
  if (params?.category) searchParams.set("category", params.category);
  if (params?.country) searchParams.set("country", params.country);
  if (params?.sort) searchParams.set("sort", params.sort);
  const qs = searchParams.toString();
  const url = `${API_BASE}/api/rankings${qs ? `?${qs}` : ""}`;
  const res = await fetch(url, {
    next: { revalidate: 60 },
  });
  if (!res.ok) {
    console.error("[fetchRankings] HTTP", res.status, await res.text().catch(() => ""));
    return [];
  }
  return res.json();
}

export async function fetchChannel(handle: string): Promise<ChannelResponse> {
  const url = `${API_BASE}/api/channel/${handle}`;
  const res = await fetch(url, {
    next: { revalidate: 60 },
  });
  if (!res.ok) {
    console.error("[fetchChannel] HTTP", res.status);
    return { channel: {} as ChannelDetail, videos: [] };
  }
  return res.json();
}

export async function fetchTranscript(videoId: string): Promise<TranscriptResponse> {
  const res = await fetch(`${API_BASE}/api/video/${videoId}/transcript`, {
    cache: "no-store",
  });
  return res.json();
}

export function formatSubscribers(count: number): string {
  if (count >= 1_000_000_000) return `${(count / 1_000_000_000).toFixed(1)}B`;
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}K`;
  return String(count);
}

// ─── Eval API (authenticated) ────────────────────────────────────

export interface EvalResult {
  status: "pending" | "complete" | "error";
  video_id: string;
  video_score?: number;
  title?: string;
  duration_seconds?: number;
  title_content_similarity_score?: number;
  deception_score?: number;
  focus_ratio?: number;
  time_to_main_content?: number;
  sponsor_interruption_score?: number;
  title_analysis_reasoning?: string;
  deception_reasoning?: string;
  focus_reasoning?: string;
  time_reasoning?: string;
  sponsor_reasoning?: string;
  evaluation_error?: string;
  credits_remaining?: number;
  cached?: boolean;
  content_summary?: string;
  verdict?: string;
}

export async function evaluateVideo(
  videoUrl: string,
  accessToken: string,
  channelId?: string,
  channelName?: string
): Promise<EvalResult> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);

  try {
    const body: Record<string, string> = { video_url: videoUrl };
    if (channelId) body.channel_id = channelId;
    if (channelName) body.channel_name = channelName;
    const res = await fetch(`${API_BASE}/api/evaluate`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (res.status === 401) throw new Error("AUTH_REQUIRED");
    if (res.status === 403) throw new Error("NO_CREDITS");
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return res.json();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error("Request timed out. Please try again.");
    }
    throw e;
  } finally {
    clearTimeout(timeout);
  }
}


// ─── Channel eval API ────────────────────────────────────────────

export interface ChannelMeta {
  channel_id: string;
  title: string;
  thumbnail_url: string;
  description?: string;
  subscriber_count?: number;
}

export interface ChannelVideo {
  video_id: string;
  title: string;
  duration_seconds: number;
  view_count: number;
  pool: string;
  status: string;
  video_score?: number;
  cached: boolean;
}

export interface ChannelEvalResponse {
  channel: ChannelMeta;
  videos: ChannelVideo[];
  total_videos: number;
  uncached_count: number;
  credits_needed: number;
  credits_remaining: number;
}

export async function fetchChannelMeta(channelUrl: string): Promise<ChannelMeta | null> {
  try {
    const res = await fetch(
      `${API_BASE}/api/channel_meta?url=${encodeURIComponent(channelUrl)}`
    );
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function evaluateChannel(
  channelUrl: string,
  accessToken: string
): Promise<ChannelEvalResponse> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);

  try {
    const res = await fetch(`${API_BASE}/api/evaluate_channel`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify({ channel_url: channelUrl }),
      signal: controller.signal,
    });
    if (res.status === 401) throw new Error("AUTH_REQUIRED");
    if (res.status === 403) throw new Error("NO_CREDITS");
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return res.json();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error("Request timed out. Please try again.");
    }
    throw e;
  } finally {
    clearTimeout(timeout);
  }
}

export function scoreColor(score: number): string {
  const s = Math.max(0, Math.min(100, score));
  const stops: [number, number, number][] = [
    [248, 81, 73],    // 0:  bright red     #f85149
    [230, 100, 40],   // 20: red-orange     #e66428
    [210, 153, 34],   // 50: amber          #d29922
    [200, 170, 40],   // 65: warm gold      #c8aa28
    [63, 185, 80],    // 80: green          #3fb950
    [46, 160, 67],    // 100: deep green    #2ea043
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

