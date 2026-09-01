"use client";

import { useState, useEffect } from "react";
import Link from "next/link";

const VOTE_KEY = "anticlickbait_vote";
const SB_URL = process.env.NEXT_PUBLIC_SUPABASE_URL || "https://qhiwvwlbtlevltfblxso.supabase.co";
const SB_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFoaXd2d2xidGxldmx0ZmJseHNvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI3MDkxNDMsImV4cCI6MjA4ODI4NTE0M30.wsgdjkTi0xILWld5Bg46aRsSLZ-tdYBmqdbRTa1mEkk";

const sbHeaders = {
  apikey: SB_KEY,
  Authorization: `Bearer ${SB_KEY}`,
  "Content-Type": "application/json",
};

async function getCounts() {
  const res = await fetch(
    `${SB_URL}/rest/v1/site_upvotes?select=upvote_count,downvote_count&id=eq.1`,
    { headers: sbHeaders }
  );
  const data = await res.json();
  return data[0] || { upvote_count: 0, downvote_count: 0 };
}

async function callRpc(fn: string) {
  const res = await fetch(`${SB_URL}/rest/v1/rpc/${fn}`, {
    method: "POST",
    headers: sbHeaders,
    body: JSON.stringify({}),
  });
  return res.json();
}

export default function Footer() {
  const [upCount, setUpCount] = useState<number | null>(null);
  const [downCount, setDownCount] = useState<number | null>(null);
  const [vote, setVote] = useState<"up" | "down" | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem(VOTE_KEY);
    if (saved === "up" || saved === "down") setVote(saved);
    getCounts()
      .then((d) => { setUpCount(d.upvote_count); setDownCount(d.downvote_count); })
      .catch(() => { setUpCount(0); setDownCount(0); });
  }, []);

  const handleVote = async (direction: "up" | "down") => {
    if (busy) return;
    setBusy(true);
    try {
      if (vote === direction) {
        const d = await callRpc(direction === "up" ? "decrement_upvote" : "decrement_downvote");
        setUpCount(d.upvote_count);
        setDownCount(d.downvote_count);
        setVote(null);
        localStorage.removeItem(VOTE_KEY);
      } else if (vote === null) {
        const d = await callRpc(direction === "up" ? "increment_upvote" : "increment_downvote");
        setUpCount(d.upvote_count);
        setDownCount(d.downvote_count);
        setVote(direction);
        localStorage.setItem(VOTE_KEY, direction);
      } else {
        const old = vote;
        await callRpc(old === "up" ? "decrement_upvote" : "decrement_downvote");
        const d = await callRpc(direction === "up" ? "increment_upvote" : "increment_downvote");
        setUpCount(d.upvote_count);
        setDownCount(d.downvote_count);
        setVote(direction);
        localStorage.setItem(VOTE_KEY, direction);
      }
    } catch { /* silent */ }
    setBusy(false);
  };

  return (
    <footer className="site-footer">
      <div className="footer-inner">
        <div className="footer-left">
          <Link href="/how-it-works" className="footer-link">How it works</Link>
          <span className="footer-sep">/</span>
          <Link href="/rankings" className="footer-link">Rankings</Link>
          <span className="footer-sep">/</span>
          <Link href="/privacy" className="footer-link">Privacy</Link>
          <span className="footer-sep">/</span>
          <a href="mailto:aditya.biradar001@gmail.com" className="footer-link">aditya.biradar001@gmail.com</a>
          <span className="footer-sep">/</span>
          <a href="https://x.com/BiradarAditya01" target="_blank" rel="noopener noreferrer" className="footer-link">@BiradarAditya01</a>
        </div>

        <div className="vote-group">
          <button
            className={`vote-btn vote-btn-up ${vote === "up" ? "vote-btn-active" : ""}`}
            onClick={() => handleVote("up")}
            disabled={busy}
          >
            <svg width="10" height="8" viewBox="0 0 10 8" fill="none">
              <path d="M5 0L9.33 7.5H0.67L5 0Z" fill="currentColor" />
            </svg>
            <span>{vote === "up" ? "Upvoted" : "Upvote"}</span>
            <span className="vote-count">{upCount ?? "—"}</span>
          </button>
          <button
            className={`vote-btn vote-btn-down ${vote === "down" ? "vote-btn-active" : ""}`}
            onClick={() => handleVote("down")}
            disabled={busy}
          >
            <svg width="10" height="8" viewBox="0 0 10 8" fill="none">
              <path d="M5 7.5L0.67 0H9.33L5 7.5Z" fill="currentColor" />
            </svg>
            <span>{vote === "down" ? "Downvoted" : "Downvote"}</span>
            <span className="vote-count">{downCount ?? "—"}</span>
          </button>
        </div>
      </div>
    </footer>
  );
}
