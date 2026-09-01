"use client";

import { useState } from "react";
import Header from "../components/Header";

const FEEDBACK_LIMIT = 5;
const FEEDBACK_KEY = "anticlickbait_feedback_count";
const SB_URL = process.env.NEXT_PUBLIC_SUPABASE_URL || "https://qhiwvwlbtlevltfblxso.supabase.co";
const SB_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFoaXd2d2xidGxldmx0ZmJseHNvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI3MDkxNDMsImV4cCI6MjA4ODI4NTE0M30.wsgdjkTi0xILWld5Bg46aRsSLZ-tdYBmqdbRTa1mEkk";

export default function HowItWorksPage() {
  const [message, setMessage] = useState("");
  const [status, setStatus] = useState<"idle" | "sending" | "sent" | "error" | "limit">("idle");

  const handleSubmit = async () => {
    if (!message.trim()) return;

    const count = parseInt(localStorage.getItem(FEEDBACK_KEY) || "0", 10);
    if (count >= FEEDBACK_LIMIT) {
      setStatus("limit");
      return;
    }

    setStatus("sending");
    try {
      const res = await fetch(`${SB_URL}/rest/v1/feedback`, {
        method: "POST",
        headers: {
          apikey: SB_KEY,
          Authorization: `Bearer ${SB_KEY}`,
          "Content-Type": "application/json",
          Prefer: "return=minimal",
        },
        body: JSON.stringify({ message: message.trim() }),
      });
      if (res.ok || res.status === 201) {
        localStorage.setItem(FEEDBACK_KEY, String(count + 1));
        setStatus("sent");
        setMessage("");
        setTimeout(() => setStatus("idle"), 3000);
      } else {
        setStatus("error");
      }
    } catch {
      setStatus("error");
    }
  };

  return (
    <>
      <Header />

      <div className="page-hero">
        <div className="page-hero-grid"></div>
        <div className="page-hero-glow"></div>
        <div className="container page-hero-content">
          <h1 className="hiw-title">How It Works</h1>
        </div>
      </div>

      <div className="container">
        <div className="hiw-content">

          <section className="hiw-section">
            <h2 className="hiw-heading">Scoring Formula</h2>
            <p className="hiw-text">
              Each sub-metric is scored 0-10 by the LLM. Final video score is 0-100.
            </p>
            <div className="hiw-formula">
              <code>
                base = (title_similarity x 5) + (focus_ratio x 3) + (time_to_content x 2)<br /><br />
                penalty = (deception x 2) + (sponsor x 1)<br /><br />
                <strong>score = base - penalty &nbsp;&nbsp;[0-100]</strong>
              </code>
            </div>
            <p className="hiw-text">
              Channel score = average of its evaluated video scores.
            </p>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">What the Metrics Mean</h2>
            <div className="hiw-metrics-list">
              <div className="hiw-metric hiw-metric-card">
                <span className="hiw-metric-name">Title-Content Similarity (0-10)</span>
                <span className="hiw-metric-desc">Does the video deliver what the title promises? 0 = complete bait-and-switch, 10 = precise match.</span>
              </div>
              <div className="hiw-metric hiw-metric-card">
                <span className="hiw-metric-name">Focus % + Time to Main Content (0-10)</span>
                <span className="hiw-metric-desc">What fraction of the video stays on topic, and how quickly it gets there. Higher = more focused, less preamble.</span>
              </div>
              <div className="hiw-metric hiw-metric-card">
                <span className="hiw-metric-name">Deception Penalty (0-10)</span>
                <span className="hiw-metric-desc">Whether the title makes factual claims the video contradicts or never addresses. Up to -20 points.</span>
              </div>
              <div className="hiw-metric hiw-metric-card">
                <span className="hiw-metric-name">Sponsor Penalty (0-10)</span>
                <span className="hiw-metric-desc">Proportion of sponsor/ad content relative to total video runtime. Up to -10 points.</span>
              </div>
            </div>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">LLM Evaluation Process</h2>
            <p className="hiw-text">
              Each video is evaluated using two parallel LLM calls:
            </p>
            <div className="hiw-steps">
              <div className="hiw-step">
                <span className="hiw-step-num">1</span>
                <div>
                  <strong>Title Analysis</strong> - The LLM reads the video title and full transcript together. It identifies what the title promises in plain language, then scores title-content similarity and deception.
                </div>
              </div>
              <div className="hiw-step">
                <span className="hiw-step-num">2</span>
                <div>
                  <strong>Content Analysis</strong> - The LLM analyzes the transcript with timestamps to measure focus ratio (% on-topic), time to main content, and sponsor interruption level.
                </div>
              </div>
            </div>
            <p className="hiw-text">
              For long transcripts that exceed the model&rsquo;s context window, we chunk the transcript and aggregate metrics deterministically across all chunks.
            </p>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">Video Selection</h2>
            <p className="hiw-text">
              15 videos are evaluated per channel - a mix of recent uploads and all-time popular videos, so the score reflects both current behaviour and historical patterns.
            </p>
            <ul className="hiw-list">
              <li>YouTube Shorts are excluded.</li>
              <li>Videos longer than 90 minutes are excluded.</li>
              <li>Visually-driven channels are excluded - transcript-based scoring isn&rsquo;t a fair measure for them.</li>
            </ul>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading hiw-heading-lg">Why This Exists</h2>
            <p className="hiw-text">
              Our feeds overstimulate us more than ever. Exaggerated titles, sensational previews, fake urgency - it all works amazingly well on our monkey brains. It&rsquo;s just a natural consequence of capitalism and how algorithms have evolved in rewarding our dopamine circuits quickly.
            </p>
            <p className="hiw-text">
              But there should be a counterforce. A platform that&rsquo;s trusted and keeps content creators accountable while rewarding the honest ones.
            </p>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">Current Limitations</h2>
            <ul className="hiw-list">
              <li>Transcripts are the only content input - visuals, tone, editing, and pacing are not evaluated.</li>
              <li>AI scoring can be inconsistent across runs.</li>
              <li>15 videos per channel is a small sample - outlier videos can skew a channel&rsquo;s score.</li>
            </ul>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">Feedback</h2>
            <p className="hiw-text">
              Found a bug? Disagree with a score? Have a feature suggestion?
            </p>
            <div className="hiw-feedback">
              <textarea
                className="hiw-textarea"
                placeholder="Your feedback..."
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                maxLength={2000}
                disabled={status === "sent" || status === "limit"}
              />
              <div className="hiw-feedback-actions">
                <button
                  className="hiw-submit-btn"
                  onClick={handleSubmit}
                  disabled={!message.trim() || status === "sending" || status === "sent" || status === "limit"}
                >
                  {status === "sending" ? "Sending..." : status === "sent" ? "Sent" : "Submit"}
                </button>
                {status === "sent" && <span className="hiw-feedback-status">Thanks for the feedback.</span>}
                {status === "error" && <span className="hiw-feedback-status hiw-feedback-error">Something went wrong. Try again.</span>}
                {status === "limit" && <span className="hiw-feedback-status">Submission limit reached.</span>}
              </div>
            </div>
          </section>

        </div>
      </div>
    </>
  );
}
