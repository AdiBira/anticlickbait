"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { scoreColor } from "../lib/api";

interface ChannelSuggestion {
  handle: string;
  title: string;
  channel_score: number;
  thumbnail_url?: string | null;
}

function parseVideoId(input: string): string | null {
  const s = input.trim();
  if (!s) return null;
  if (/^[A-Za-z0-9_-]{11}$/.test(s)) return s;
  const m1 = s.match(/[?&]v=([A-Za-z0-9_-]{11})/);
  if (m1) return m1[1];
  const m2 = s.match(/youtu\.be\/([A-Za-z0-9_-]{11})/);
  if (m2) return m2[1];
  const m3 = s.match(/(?:shorts|embed)\/([A-Za-z0-9_-]{11})/);
  if (m3) return m3[1];
  return null;
}

function parseChannelUrl(input: string): string | null {
  const s = input.trim();
  if (s.match(/youtube\.com\/@[A-Za-z0-9_.-]+/)) return s;
  if (s.match(/youtube\.com\/channel\/UC[A-Za-z0-9_-]+/)) return s;
  if (s.match(/youtube\.com\/c\/[A-Za-z0-9_.-]+/)) return s;
  if (s.match(/youtube\.com\/user\/[A-Za-z0-9_.-]+/)) return s;
  return null;
}

function isUrl(input: string): boolean {
  const s = input.trim().toLowerCase();
  return s.includes("youtube.com") || s.includes("youtu.be");
}

export default function EvalBanner() {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [channels, setChannels] = useState<ChannelSuggestion[]>([]);
  const [suggestions, setSuggestions] = useState<ChannelSuggestion[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const router = useRouter();

  // Fetch channel list once on mount
  useEffect(() => {
    let cancelled = false;
    fetch("/api/rankings")
      .then((res) => (res.ok ? res.json() : []))
      .then((data: ChannelSuggestion[]) => {
        if (!cancelled) setChannels(data);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // Debounced filter
  const filterChannels = useCallback(
    (query: string) => {
      if (debounceRef.current) clearTimeout(debounceRef.current);

      if (!query.trim() || isUrl(query) || channels.length === 0) {
        setSuggestions([]);
        setShowDropdown(false);
        return;
      }

      debounceRef.current = setTimeout(() => {
        const q = query.trim().toLowerCase();
        const matches = channels.filter(
          (ch) =>
            ch.title.toLowerCase().includes(q) ||
            ch.handle.toLowerCase().includes(q)
        ).slice(0, 5);

        setSuggestions(matches);
        setShowDropdown(matches.length > 0);
      }, 200);
    },
    [channels]
  );

  // Click outside to close
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  function handleInputChange(value: string) {
    setUrl(value);
    setError("");
    filterChannels(value);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setShowDropdown(false);
    }
  }

  function handleSuggestionClick(handle: string) {
    setShowDropdown(false);
    setUrl("");
    router.push(`/channel/${handle}`);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setShowDropdown(false);
    const input = url.trim();
    if (!input) return;

    const videoId = parseVideoId(input);
    if (videoId) {
      setError("");
      router.push(`/evaluate?v=${videoId}`);
      return;
    }

    const channelUrl = parseChannelUrl(input);
    if (channelUrl) {
      setError("");
      router.push(`/evaluate?channel=${encodeURIComponent(channelUrl)}`);
      return;
    }

    setError("Enter a valid YouTube video or channel URL");
  }

  return (
    <div className="search-wrapper" ref={wrapperRef}>
      <form onSubmit={handleSubmit}>
        <input
          type="text"
          className="search-input"
          placeholder="Paste YouTube video or channel URL..."
          value={url}
          onChange={(e) => handleInputChange(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button type="submit" className="search-btn" disabled={!url.trim()}>
          <svg viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
        </button>
      </form>
      {showDropdown && suggestions.length > 0 && (
        <div className="autocomplete-dropdown">
          {suggestions.map((ch) => (
            <button
              key={ch.handle}
              className="autocomplete-item"
              type="button"
              onClick={() => handleSuggestionClick(ch.handle)}
            >
              <span className="autocomplete-title">{ch.title}</span>
              <span className="autocomplete-handle">{ch.handle}</span>
              <span
                className="autocomplete-score"
                style={{ color: scoreColor(ch.channel_score) }}
              >
                {Math.round(ch.channel_score)}
              </span>
            </button>
          ))}
        </div>
      )}
      {error && <div className="search-error">{error}</div>}
    </div>
  );
}
