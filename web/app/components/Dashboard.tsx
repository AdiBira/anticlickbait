"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "./AuthProvider";
import { scoreColor } from "../lib/api";
import { supabase } from "../lib/supabase";

interface HistoryItem {
  video_id: string;
  title: string;
  video_score: number | null;
  status: string;
  created_at: string;
  channel_id: string | null;
  channel_name: string | null;
}

export default function Dashboard() {
  const { user, loading: authLoading } = useAuth();

  if (authLoading) return null;
  if (!user) return null;

  return (
    <section className="dashboard">
      <div className="container">
        <DashboardSearch />
        <DashboardHistory userId={user.id} />
        <div className="dashboard-footer">
          <Link href="/rankings" className="dashboard-rankings-link">
            View Channel Rankings
          </Link>
        </div>
      </div>
    </section>
  );
}

function DashboardSearch() {
  const [input, setInput] = useState("");
  const router = useRouter();

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const s = input.trim();
    if (!s) return;

    const vidMatch = s.match(/[?&]v=([A-Za-z0-9_-]{11})/) || s.match(/youtu\.be\/([A-Za-z0-9_-]{11})/) || s.match(/(?:shorts|embed)\/([A-Za-z0-9_-]{11})/);
    if (vidMatch) { router.push(`/evaluate?v=${vidMatch[1]}`); return; }
    if (/^[A-Za-z0-9_-]{11}$/.test(s)) { router.push(`/evaluate?v=${s}`); return; }

    if (s.match(/youtube\.com\/@|youtube\.com\/channel\//)) {
      router.push(`/evaluate?channel=${encodeURIComponent(s)}`);
      return;
    }
  }

  return (
    <div className="dash-search-section">
      <h2 className="dash-section-title">Your Evaluations</h2>
      <form className="dash-search" onSubmit={handleSubmit}>
        <input
          type="text"
          className="dash-search-input"
          placeholder="Paste YouTube video or channel URL to evaluate..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <button type="submit" className="dash-search-btn" disabled={!input.trim()}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
          </svg>
        </button>
      </form>
    </div>
  );
}

function DashboardHistory({ userId }: { userId: string }) {
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    supabase
      .from("eval_cache")
      .select("video_id, title, video_score, status, created_at, channel_id, channel_name")
      .eq("requested_by", userId)
      .order("created_at", { ascending: false })
      .limit(50)
      .then(({ data }) => {
        setHistory((data as HistoryItem[]) || []);
        setLoading(false);
      });
  }, [userId]);

  if (loading) {
    return (
      <div className="dash-history-loading">
        <div className="eval-spinner" />
      </div>
    );
  }

  if (history.length === 0) {
    return (
      <div className="dash-empty">
        <p className="dash-empty-text">No evaluations yet</p>
        <p className="dash-empty-sub">Paste a YouTube video or channel URL above to get started</p>
      </div>
    );
  }

  // Group channel items, keep standalone videos
  const channelGroups: Record<string, { name: string; items: HistoryItem[] }> = {};
  const standalone: HistoryItem[] = [];

  for (const item of history) {
    if (item.channel_id && item.channel_name) {
      if (!channelGroups[item.channel_id]) {
        channelGroups[item.channel_id] = { name: item.channel_name, items: [] };
      }
      channelGroups[item.channel_id].items.push(item);
    } else {
      standalone.push(item);
    }
  }

  type Entry =
    | { type: "video"; item: HistoryItem }
    | { type: "channel"; channelId: string; name: string; items: HistoryItem[]; latestDate: string };

  const entries: Entry[] = [];
  for (const [channelId, group] of Object.entries(channelGroups)) {
    entries.push({ type: "channel", channelId, name: group.name, items: group.items, latestDate: group.items[0]?.created_at || "" });
  }
  for (const item of standalone) {
    entries.push({ type: "video", item });
  }
  entries.sort((a, b) => {
    const da = a.type === "video" ? a.item.created_at : a.latestDate;
    const db = b.type === "video" ? b.item.created_at : b.latestDate;
    return db.localeCompare(da);
  });

  return (
    <div className="dash-history">
      <div className="dash-cards">
        {entries.map((entry) => {
          if (entry.type === "video") {
            const { item } = entry;
            return (
              <Link key={item.video_id} href={`/evaluate?v=${item.video_id}`} className="dash-card">
                <div className="dash-card-thumb">
                  <img
                    src={`https://i.ytimg.com/vi/${item.video_id}/sddefault.jpg`}
                    alt=""
                    onError={(e) => { (e.target as HTMLImageElement).src = `https://i.ytimg.com/vi/${item.video_id}/mqdefault.jpg`; }}
                  />
                  <span className="dash-card-badge">Video</span>
                </div>
                <div className="dash-card-body">
                  <span className="dash-card-title">{item.title || item.video_id}</span>
                  <div className="dash-card-meta">
                    <span>{new Date(item.created_at).toLocaleDateString()}</span>
                    {item.status === "complete" && item.video_score != null && (
                      <span className="dash-card-score" style={{ color: scoreColor(item.video_score) }}>
                        {Math.round(item.video_score)}
                      </span>
                    )}
                    {item.status !== "complete" && (
                      <span className="dash-card-status">{item.status}</span>
                    )}
                  </div>
                </div>
              </Link>
            );
          }

          // Channel entry
          const { channelId, name, items } = entry;
          const completed = items.filter(i => i.status === "complete" && i.video_score != null);
          const avgScore = completed.length > 0
            ? Math.round(completed.reduce((s, i) => s + (i.video_score || 0), 0) / completed.length)
            : null;
          const firstVideoId = items[0]?.video_id;

          return (
            <Link key={channelId} href={`/evaluate?channel=${encodeURIComponent(`https://youtube.com/channel/${channelId}`)}`} className="dash-card">
              <div className="dash-card-thumb dash-card-thumb-channel">
                {firstVideoId && (
                  <img
                    src={`https://i.ytimg.com/vi/${firstVideoId}/sddefault.jpg`}
                    alt=""
                    onError={(e) => { (e.target as HTMLImageElement).src = `https://i.ytimg.com/vi/${firstVideoId}/mqdefault.jpg`; }}
                  />
                )}
                <span className="dash-card-badge dash-card-badge-channel">Channel</span>
              </div>
              <div className="dash-card-body">
                <span className="dash-card-title">{name}</span>
                <div className="dash-card-meta">
                  <span>{items.length} videos - {new Date(items[0]?.created_at).toLocaleDateString()}</span>
                  {avgScore != null && (
                    <span className="dash-card-score" style={{ color: scoreColor(avgScore) }}>
                      {avgScore}
                    </span>
                  )}
                </div>
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
