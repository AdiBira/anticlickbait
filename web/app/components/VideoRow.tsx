"use client";

import { useState } from "react";
import { type VideoEvaluation, fetchTranscript, scoreColor } from "../lib/api";

function formatDuration(s: number): string {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function formatViews(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M views`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K views`;
  return `${n} views`;
}

function formatDate(d: string): string {
  return new Date(d).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

const BASE_METRICS: { key: string; label: string; max: number; reasoning: string; normalize?: number }[] = [
  { key: "title_content_similarity_score", label: "Title-Content Similarity", max: 10, reasoning: "title_analysis_reasoning" },
  { key: "focus_ratio", label: "Focus % + Time to Main Content", max: 10, reasoning: "focus_reasoning" },
];

const PENALTY_METRICS: { key: string; label: string; max: number; reasoning: string }[] = [
  { key: "deception_score", label: "Deception Penalty", max: 10, reasoning: "deception_reasoning" },
  { key: "sponsor_interruption_score", label: "Sponsor Penalty", max: 10, reasoning: "sponsor_reasoning" },
];

export default function VideoRow({ video }: { video: VideoEvaluation }) {
  const [expanded, setExpanded] = useState(false);
  const [transcript, setTranscript] = useState<string | null>(null);
  const [loadingTranscript, setLoadingTranscript] = useState(false);
  const [showTranscript, setShowTranscript] = useState(false);
  const handleTranscript = async () => {
    if (showTranscript) {
      setShowTranscript(false);
      return;
    }
    if (transcript !== null) {
      setShowTranscript(true);
      return;
    }
    setLoadingTranscript(true);
    try {
      const res = await fetchTranscript(video.video_id);
      setTranscript(res.transcript_text || "Transcript not available.");
    } catch {
      setTranscript("Failed to load transcript.");
    }
    setLoadingTranscript(false);
    setShowTranscript(true);
  };

  const getVal = (key: string, normalize?: number): number => {
    const val = (video as unknown as Record<string, unknown>)[key] as number;
    return normalize ? val / normalize : val;
  };

  const renderMetric = (m: typeof BASE_METRICS[number], isBase: boolean) => {
    const raw = getVal(m.key, m.normalize);
    const barW = (raw / m.max) * 100;
    const reasoning = (video as unknown as Record<string, unknown>)[m.reasoning] as string;
    return (
      <div key={m.key} className={`video-metric-item ${isBase ? "" : "video-metric-penalty"}`}>
        <div className="video-metric-header">
          <span className="video-metric-label">{m.label}</span>
          <span className="video-metric-value">{Math.round(raw)}<span className="video-metric-scale">/10</span></span>
          <div className="video-bar-bg">
            <div className="video-bar-fill" style={{ width: `${Math.min(100, barW)}%` }} />
          </div>
        </div>
        <p className="video-metric-reasoning">{reasoning}</p>
      </div>
    );
  };

  return (
    <div className="video-item">
      <div className="video-row" role="button" tabIndex={0} aria-expanded={expanded} onClick={() => setExpanded(!expanded)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded); } }}>
        <a
          href={`https://www.youtube.com/watch?v=${video.video_id}`}
          target="_blank"
          rel="noopener noreferrer"
          className="video-thumb-container"
          onClick={(e) => e.stopPropagation()}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={`https://img.youtube.com/vi/${video.video_id}/sddefault.jpg`}
            alt={video.title}
            className="video-thumb"
            onError={(e) => { (e.target as HTMLImageElement).src = `https://img.youtube.com/vi/${video.video_id}/mqdefault.jpg`; }}
          />
          <div className="play-overlay">
            <div className="play-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="white" style={{ marginLeft: "3px" }}>
                <polygon points="5,3 19,12 5,21" />
              </svg>
            </div>
          </div>
        </a>

        <div className="video-info">
          <span className="video-title-link">
            {video.title}
          </span>
          <span className="video-meta">
            {[
              video.published_at && formatDate(video.published_at),
              video.view_count != null && formatViews(video.view_count),
              video.duration_seconds != null && formatDuration(video.duration_seconds),
            ].filter(Boolean).join(" \u00b7 ")}
          </span>
        </div>

        <div className="video-score" style={{ color: scoreColor(video.video_score) }}>
          {Math.round(video.video_score)}
        </div>

        <div className="video-expand-icon" aria-hidden="true">
          {expanded ? "\u2212" : "+"}
        </div>
      </div>

      {expanded && (
        <div className="video-detail">
          <div className="video-metrics-grid">
            <div className="video-metric-group">
              <span className="video-metric-group-label">Base Score <span className="video-metric-group-hint video-metric-group-hint-positive">higher = better</span></span>
              {BASE_METRICS.map((m) => renderMetric(m, true))}
            </div>
            <div className="video-metric-group video-metric-group-penalty">
              <span className="video-metric-group-label">Penalties <span className="video-metric-group-hint video-metric-group-hint-penalty">lower = better</span></span>
              {PENALTY_METRICS.map((m) => renderMetric(m, false))}
            </div>
          </div>

          <button className="transcript-btn" onClick={handleTranscript}>
            {loadingTranscript ? "Loading..." : showTranscript ? "Hide Transcript" : "Show Transcript"}
          </button>

          {showTranscript && transcript && (
            <pre className="transcript-block">{transcript}</pre>
          )}
        </div>
      )}
    </div>
  );
}
