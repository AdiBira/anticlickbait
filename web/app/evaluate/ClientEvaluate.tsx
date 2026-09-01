"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import Link from "next/link";
import { useAuth } from "../components/AuthProvider";
import { GoogleIcon } from "../components/AuthButton";
import Header from "../components/Header";
import EvalBanner from "../components/EvalBanner";
import EvalStepper, { stageToStep, type StepConfig } from "../components/EvalStepper";
import EvalSkeleton from "../components/EvalSkeleton";
import { evaluateVideo, evaluateChannel, fetchChannelMeta, scoreColor, formatSubscribers, EVAL_CACHE_FIELDS_VIDEO, EVAL_CACHE_FIELDS_FULL } from "../lib/api";
import type { EvalResult, ChannelMeta, ChannelEvalResponse, ChannelVideo, VideoEvaluation } from "../lib/api";
import VideoRow from "../components/VideoRow";
import { supabase } from "../lib/supabase";
import { getCachedEval, setCachedEval, clearCachedEval, getCachedVideoMeta, setCachedVideoMeta, getCachedChannel, setCachedChannel, setCachedCredits } from "../lib/evalCache";

type Stage = "loading_meta" | "awaiting_auth" | "fetching_transcript" | "analyzing" | "computing" | "complete" | "error";

const STAGE_MESSAGES: Record<string, string> = {
  loading_meta: "Checking video...",
  fetching_transcript: "Fetching transcript...",
  analyzing: "Analyzing content...",
  computing: "Computing score...",
};

interface VideoMeta {
  title: string;
  thumbnail: string;
  videoId: string;
  channelName?: string;
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatViews(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M views`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}K views`;
  return `${count} views`;
}

async function fetchVideoMeta(videoId: string): Promise<VideoMeta> {
  const cached = getCachedVideoMeta(videoId);
  if (cached) return { ...cached, videoId };

  const thumbnail = `https://i.ytimg.com/vi/${videoId}/maxresdefault.jpg`;
  let title = "";
  let channelName = "";

  try {
    const res = await fetch(
      `https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=${videoId}&format=json`
    );
    if (res.ok) {
      const data = await res.json();
      title = data.title || "";
      channelName = data.author_name || "";
    }
  } catch {
    // oEmbed failed
  }

  setCachedVideoMeta(videoId, { title, thumbnail, channelName });
  return { title, thumbnail, videoId, channelName };
}

export default function ClientEvaluatePage({ videoId, channelUrl }: { videoId: string; channelUrl: string }) {
  if (channelUrl) {
    return <ChannelEvaluatePage channelUrl={channelUrl} />;
  }

  return <VideoEvaluatePage videoId={videoId} />;
}

function VideoEvaluatePage({ videoId }: { videoId: string }) {
  const { user, loading: authLoading, signIn } = useAuth();

  const [meta, setMeta] = useState<VideoMeta | null>(null);
  const [stage, setStage] = useState<Stage>("loading_meta");
  const [result, setResult] = useState<EvalResult | null>(null);
  const [error, setError] = useState("");
  const [pollCount, setPollCount] = useState(0);
  const [scoreAnimating, setScoreAnimating] = useState(false);
  const [animatedScore, setAnimatedScore] = useState(0);
  const [creditsRequested, setCreditsRequested] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("acb_credits_requested") === "true";
    }
    return false;
  });
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const evalStartedRef = useRef(false);
  const evalGenRef = useRef(0);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // Step 1: Fetch video meta + reset all state from previous video
  useEffect(() => {
    if (!videoId) return;
    stopPolling();
    setResult(null);
    setError("");
    setPollCount(0);
    setScoreAnimating(false);
    setMeta(null);
    evalStartedRef.current = false;
    evalGenRef.current++;
    setStage("loading_meta");
    fetchVideoMeta(videoId).then(setMeta);
  }, [videoId, stopPolling]);

  // Step 2: When meta is loaded, check auth -> cache -> DB -> eval
  useEffect(() => {
    if (!meta) return;
    if (!user && !authLoading) {
      if (stage === "loading_meta") setStage("awaiting_auth");
      return;
    }
    if (!user || authLoading) return;
    if (stage === "awaiting_auth" || stage === "loading_meta") {
      // 1. Check localStorage first (instant)
      const cached = getCachedEval(videoId);
      if (cached && cached.status === "complete" && cached.video_score != null) {
        setResult(cached);
        setStage("complete");
        return;
      }
      if (cached && cached.status === "error") {
        setError(cached.evaluation_error || "Evaluation failed");
        setStage("error");
        return;
      }

      // 2. Check DB (eval_cache)
      const gen = evalGenRef.current;
      checkExistingEval(videoId).then((existing) => {
        if (existing) return;
        if (gen !== evalGenRef.current) return; // videoId changed, abort
        if (evalStartedRef.current) return;
        evalStartedRef.current = true;
        setStage("fetching_transcript");
        startEval();
      });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, authLoading, meta, stage]);

  async function checkExistingEval(vid: string): Promise<boolean> {
    try {
      const { data } = await supabase
        .from("eval_cache")
        .select(EVAL_CACHE_FIELDS_VIDEO)
        .eq("video_id", vid)
        .limit(1);

      if (!data || data.length === 0) return false;

      const row = data[0];
      if (row.status === "complete" && row.video_score != null) {
        const evalResult = row as EvalResult;
        setCachedEval(vid, evalResult);
        setResult(evalResult);
        setStage("complete");
        return true;
      }
      if (row.status === "error") {
        setCachedEval(vid, { status: "error", video_id: vid, evaluation_error: row.evaluation_error } as EvalResult);
        setError(row.evaluation_error || "Evaluation failed");
        setStage("error");
        return true;
      }
      if (row.status === "pending") {
        setStage("analyzing");
        startPolling(vid);
        return true;
      }
    } catch {
      // DB check failed, proceed with eval
    }
    return false;
  }

  async function getToken(): Promise<string> {
    const { data } = await supabase.auth.getSession();
    const token = data.session?.access_token;
    if (!token) throw new Error("AUTH_REQUIRED");
    return token;
  }

  async function startEval() {
    setStage("fetching_transcript");
    setError("");

    try {
      const token = await getToken();

      const res = await evaluateVideo(
        `https://youtube.com/watch?v=${videoId}`,
        token
      );

      if (res.status === "complete") {
        setCachedEval(videoId, res);
        if (res.credits_remaining != null) setCachedCredits(res.credits_remaining);
        setResult(res);
        setStage("complete");
        return;
      }

      if (res.status === "pending") {
        setStage("analyzing");
        startPolling(videoId);
        return;
      }

      if (res.status === "error") {
        setError(res.evaluation_error || "Evaluation failed");
        setStage("error");
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg === "AUTH_REQUIRED") {
        setStage("awaiting_auth");
        return;
      }
      if (msg === "NO_CREDITS") {
        setError("No evaluation credits remaining");
        setStage("error");
        return;
      }
      setError(msg);
      setStage("error");
    }
  }

  function startPolling(vid: string) {
    const MAX_POLLS = 45; // 90s max
    let localPollCount = 0;
    let errorCount = 0;
    setPollCount(0);

    pollRef.current = setInterval(async () => {
      localPollCount++;
      setPollCount(localPollCount);
      if (localPollCount >= 3) setStage("computing");

      if (localPollCount >= MAX_POLLS) {
        setError("Evaluation is taking too long. Please try again.");
        setStage("error");
        stopPolling();
        return;
      }

      try {
        // Poll Supabase directly (no Vercel middleman)
        const { data, error: queryError } = await supabase
          .from("eval_cache")
          .select(EVAL_CACHE_FIELDS_VIDEO)
          .eq("video_id", vid)
          .single();

        if (queryError || !data) {
          errorCount++;
          if (errorCount >= 5) {
            setError("Unable to check evaluation status. Please try again.");
            setStage("error");
            stopPolling();
          }
          return;
        }

        errorCount = 0;

        if (data.status === "complete") {
          const evalResult = data as EvalResult;
          setCachedEval(vid, evalResult);
          setResult(evalResult);
          stopPolling();
          if (evalResult.video_score != null) {
            animateScore(evalResult.video_score);
          } else {
            setStage("complete");
          }
        } else if (data.status === "error") {
          setCachedEval(vid, { status: "error", video_id: vid, evaluation_error: data.evaluation_error } as EvalResult);
          setError(data.evaluation_error || "Evaluation failed");
          setStage("error");
          stopPolling();
        }
      } catch {
        errorCount++;
        if (errorCount >= 5) {
          setError("Unable to check evaluation status. Please try again.");
          setStage("error");
          stopPolling();
        }
      }
    }, 2000);
  }

  function animateScore(finalScore: number) {
    setScoreAnimating(true);
    setAnimatedScore(0);
    const target = Math.round(finalScore);
    const duration = 1200;
    const start = performance.now();

    function tick(now: number) {
      const elapsed = now - start;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setAnimatedScore(Math.floor(target * eased));
      if (progress < 1) {
        requestAnimationFrame(tick);
      } else {
        setAnimatedScore(target);
        setScoreAnimating(false);
        setStage("complete");
      }
    }

    requestAnimationFrame(tick);
  }

  function handleRetry() {
    clearCachedEval(videoId);
    evalStartedRef.current = false;
    setResult(null);
    setError("");
    setPollCount(0);
    setScoreAnimating(false);
    if (meta) {
      evalStartedRef.current = true;
      setStage("fetching_transcript");
      startEval();
    } else {
      setStage("loading_meta");
    }
  }

  const isCreditsError = /credits|NO_CREDITS/i.test(error);

  function handleRequestCredits() {
    const userEmail = user?.email || "unknown";
    const subject = encodeURIComponent("Anticlickbait - Request more credits");
    const body = encodeURIComponent(`Hi, I've used all my evaluation credits and would like to request more.\n\nAccount email: ${userEmail}`);
    window.open(`mailto:aditya.biradar001@gmail.com?subject=${subject}&body=${body}`, "_self");
    localStorage.setItem("acb_credits_requested", "true");
    setCreditsRequested(true);
  }

  if (!videoId) {
    return (
      <>
        <Header hideAuth={!user && !authLoading} />
        <div className="container eval-page">
        {!user && !authLoading && (
          <div className="eval-auth-prompt">
            <h2 className="dash-section-title">Score any YouTube video or channel</h2>
            <p className="eval-text" style={{ marginBottom: '16px' }}>We analyze transcripts with AI to measure how honestly titles represent content. 5 metrics, scored 0-100.</p>
            <button className="eval-auth-btn auth-btn-google" onClick={signIn}><GoogleIcon /> Sign in</button>
            <p className="auth-permissions-note" style={{ marginTop: '12px' }}>30 free evaluations. Only your email is stored.</p>
          </div>
        )}
        {user && (
          <>
            <div className="eval-new-section">
              <h2 className="dash-section-title">Evaluate</h2>
              <EvalBanner />
            </div>
            <EvalHistoryPage userId={user.id} />
          </>
        )}
      </div>
    </>
    );
  }

  return (
    <>
      <Header hideAuth={stage === "awaiting_auth"} />
      <div className="container eval-page">
      <nav className="eval-breadcrumb">
        <Link href="/evaluate" className="eval-breadcrumb-link">Your Evaluations</Link>
        <span className="eval-breadcrumb-sep">/</span>
        <span className="eval-breadcrumb-current">{meta?.title ? meta.title.slice(0, 50) + (meta.title.length > 50 ? '...' : '') : 'Video'}</span>
      </nav>

      {/* Video header - always visible once meta loads */}
      {meta ? (
        <div className="vr-header">
          <div className="vr-thumb">
            <img
              src={meta.thumbnail}
              alt={meta.title || "Video thumbnail"}
              onError={(e) => { (e.target as HTMLImageElement).src = `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`; }}
            />
          </div>
          <div className="vr-info">
            <h1 className="vr-title">{meta.title || videoId}</h1>
            {(meta.channelName || (stage === "complete" && result)) && (
              <div className="vr-meta-row">
                {meta.channelName && <span className="vr-meta-tag">{meta.channelName}</span>}
                {stage === "complete" && result?.duration_seconds ? (
                  <span className="vr-meta-tag">{formatDuration(result.duration_seconds)}</span>
                ) : null}
              </div>
            )}
            {/* Score display - animated or final */}
            {(stage === "complete" || scoreAnimating) && result && result.video_score != null && (
              <div className="vr-score" style={{ color: scoreColor(scoreAnimating ? animatedScore : result.video_score) }}>
                {scoreAnimating ? animatedScore : Math.round(result.video_score)}
              </div>
            )}
            {stage === "awaiting_auth" && (
              <div className="eval-auth-prompt">
                <button className="eval-auth-btn auth-btn-google" onClick={signIn}><GoogleIcon /> Sign in to score</button>
              </div>
            )}
            {stage === "error" && (
              isCreditsError ? (
                <div className="credits-exhausted">
                  <p className="credits-exhausted-title">You've used all 30 evaluation credits</p>
                  {creditsRequested ? (
                    <p className="credits-request-success">Request sent - we'll get back to you within 24 hours</p>
                  ) : (
                    <button className="credits-request-btn" onClick={handleRequestCredits}>Request more credits</button>
                  )}
                </div>
              ) : (
                <div className="eval-error-block">
                  <div className="eval-error">{error}</div>
                  <p style={{ fontFamily: 'var(--font-data)', fontSize: '0.7rem', color: 'var(--ink-tertiary)', textTransform: 'uppercase', letterSpacing: '0.04em', marginTop: '4px' }}>No credits were used</p>
                  <div style={{ display: 'flex', gap: '12px', marginTop: '8px' }}>
                    <button className="eval-retry-btn" onClick={handleRetry}>Try again</button>
                    <Link href="/evaluate" className="eval-retry-btn" style={{ textDecoration: 'none' }}>Back to evaluations</Link>
                  </div>
                </div>
              )
            )}
          </div>
        </div>
      ) : stage === "loading_meta" ? (
        <div className="eval-stage-msg">
          <div className="eval-spinner" />
          <span>{STAGE_MESSAGES.loading_meta}</span>
        </div>
      ) : null}

      {/* Stepper + skeleton during eval */}
      {(stage === "fetching_transcript" || stage === "analyzing" || stage === "computing") && (
        <>
          <EvalStepper currentStep={stageToStep(stage, pollCount)} />
          <EvalSkeleton />
        </>
      )}

      {stage === "complete" && result && result.video_score != null && (
        <div className="eval-result">

          {/* Verdict - only show if verdict exists */}
          {result.verdict && (
            <div className="vr-verdict" style={{
              background: `linear-gradient(135deg, ${scoreColor(result.video_score)}18 0%, ${scoreColor(result.video_score)}05 100%)`,
              border: `1px solid ${scoreColor(result.video_score)}20`,
            }}>
              <div className="vr-verdict-label">Verdict</div>
              <p className="vr-verdict-text">{result.verdict}</p>
            </div>
          )}

          {/* Metric cards - 2x2, no reasoning inside */}
          <div className="channel-metrics-grid">
            <div className="channel-metric-card channel-metric-card-positive" data-tooltip={"Does the video deliver what the title promises?\nHigher = better"}>
              <span className="channel-metric-card-label">Title-Content Similarity</span>
              <div className="channel-metric-card-value-row">
                <span className="channel-metric-card-value" style={{ color: scoreColor((result.title_content_similarity_score ?? 0) * 10) }}>{result.title_content_similarity_score?.toFixed(1) ?? "-"}</span>
                <span className="channel-metric-card-scale">/10</span>
              </div>
            </div>
            <div className="channel-metric-card channel-metric-card-positive" data-tooltip={"What % of the video stays on topic, and how quickly it gets there.\nHigher = better"}>
              <span className="channel-metric-card-label">Focus + Time to Content</span>
              <div className="channel-metric-card-value-row">
                <span className="channel-metric-card-value" style={{ color: scoreColor((result.focus_ratio ?? 0) * 10) }}>{result.focus_ratio?.toFixed(1) ?? "-"}</span>
                <span className="channel-metric-card-scale">/10</span>
              </div>
            </div>
            <div className="channel-metric-card channel-metric-card-penalty" data-tooltip={"Does the title make claims the video contradicts or never addresses?\nLower = better"}>
              <span className="channel-metric-card-label">Deception</span>
              <div className="channel-metric-card-value-row">
                <span className="channel-metric-card-value" style={{ color: scoreColor((10 - (result.deception_score ?? 0)) * 10) }}>{result.deception_score?.toFixed(1) ?? "-"}</span>
                <span className="channel-metric-card-scale">/10</span>
              </div>
            </div>
            <div className="channel-metric-card channel-metric-card-penalty" data-tooltip={"How much sponsor content interrupts the video?\nLower = better"}>
              <span className="channel-metric-card-label">Sponsor Interruption</span>
              <div className="channel-metric-card-value-row">
                <span className="channel-metric-card-value" style={{ color: scoreColor((10 - (result.sponsor_interruption_score ?? 0)) * 10) }}>{result.sponsor_interruption_score?.toFixed(1) ?? "-"}</span>
                <span className="channel-metric-card-scale">/10</span>
              </div>
            </div>
          </div>

          {/* Video AI Analysis - reasoning section */}
          {(result.title_analysis_reasoning || result.focus_reasoning || result.deception_reasoning || result.sponsor_reasoning) && (
            <div className="eval-reasoning-section">
              <h3 className="eval-reasoning-header">Video AI Analysis</h3>
              <div className="eval-reasoning-list">
                {result.title_analysis_reasoning && (
                  <div className="video-metric-item">
                    <div className="video-metric-header">
                      <span className="video-metric-label">Title-Content Similarity</span>
                      <span className="video-metric-value">{Math.round(result.title_content_similarity_score ?? 0)}<span className="video-metric-scale">/10</span></span>
                      <div className="video-bar-bg"><div className="video-bar-fill" style={{ width: `${(result.title_content_similarity_score ?? 0) * 10}%`, background: scoreColor((result.title_content_similarity_score ?? 0) * 10) }} /></div>
                    </div>
                    <p className="video-metric-reasoning">{result.title_analysis_reasoning}</p>
                  </div>
                )}
                {result.focus_reasoning && (
                  <div className="video-metric-item">
                    <div className="video-metric-header">
                      <span className="video-metric-label">Focus % + Time to Main Content</span>
                      <span className="video-metric-value">{Math.round(result.focus_ratio ?? 0)}<span className="video-metric-scale">/10</span></span>
                      <div className="video-bar-bg"><div className="video-bar-fill" style={{ width: `${(result.focus_ratio ?? 0) * 10}%`, background: scoreColor((result.focus_ratio ?? 0) * 10) }} /></div>
                    </div>
                    <p className="video-metric-reasoning">{result.focus_reasoning}</p>
                  </div>
                )}
                {result.deception_reasoning && (
                  <div className="video-metric-item">
                    <div className="video-metric-header">
                      <span className="video-metric-label">Deception Penalty</span>
                      <span className="video-metric-value">{Math.round(result.deception_score ?? 0)}<span className="video-metric-scale">/10</span></span>
                      <div className="video-bar-bg"><div className="video-bar-fill" style={{ width: `${(10 - (result.deception_score ?? 0)) * 10}%`, background: scoreColor((10 - (result.deception_score ?? 0)) * 10) }} /></div>
                    </div>
                    <p className="video-metric-reasoning">{result.deception_reasoning}</p>
                  </div>
                )}
                {result.sponsor_reasoning && (
                  <div className="video-metric-item">
                    <div className="video-metric-header">
                      <span className="video-metric-label">Sponsor Penalty</span>
                      <span className="video-metric-value">{Math.round(result.sponsor_interruption_score ?? 0)}<span className="video-metric-scale">/10</span></span>
                      <div className="video-bar-bg"><div className="video-bar-fill" style={{ width: `${(10 - (result.sponsor_interruption_score ?? 0)) * 10}%`, background: scoreColor((10 - (result.sponsor_interruption_score ?? 0)) * 10) }} /></div>
                    </div>
                    <p className="video-metric-reasoning">{result.sponsor_reasoning}</p>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Transcript */}
          <TranscriptBlock videoId={videoId} />
        </div>
      )}

    </div>
    </>
  );
}

function TranscriptBlock({ videoId }: { videoId: string }) {
  const [transcript, setTranscript] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [show, setShow] = useState(false);

  async function handleToggle() {
    if (show) { setShow(false); return; }
    if (transcript !== null) { setShow(true); return; }

    setLoading(true);
    try {
      const { data } = await supabase
        .from("eval_cache")
        .select("transcript_text")
        .eq("video_id", videoId)
        .limit(1);
      if (data && data[0]?.transcript_text) {
        setTranscript(data[0].transcript_text);
      } else {
        setTranscript("Transcript not available.");
      }
    } catch {
      setTranscript("Failed to load transcript.");
    }
    setLoading(false);
    setShow(true);
  }

  return (
    <div>
      <button className="transcript-btn" onClick={handleToggle}>
        {loading ? "Loading..." : show ? "Hide Transcript" : "Show Transcript"}
      </button>
      {show && transcript && (
        <pre className="transcript-block">{transcript}</pre>
      )}
    </div>
  );
}

// ─── Channel Evaluate Page ───────────────────────────────────────

type ChannelStage = "loading_meta" | "awaiting_auth" | "loading_videos" | "countdown" | "evaluating" | "computing" | "summarizing" | "complete" | "error";

function ChannelEvaluatePage({ channelUrl }: { channelUrl: string }) {
  const { user, loading: authLoading, signIn } = useAuth();
  const [channelMeta, setChannelMeta] = useState<ChannelMeta | null>(null);
  const [stage, setStage] = useState<ChannelStage>("loading_meta");
  const [channelData, setChannelData] = useState<ChannelEvalResponse | null>(null);
  const [videoResults, setVideoResults] = useState<Record<string, EvalResult>>({});
  const [channelSummary, setChannelSummary] = useState("");
  const [summaryFailed, setSummaryFailed] = useState(false);
  const [videoStatuses, setVideoStatuses] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const evalRunningRef = useRef(false);
  const stopRequestedRef = useRef(false);
  const unmountedRef = useRef(false);
  const countdownRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const summaryRunningRef = useRef(false);
  const channelMetaGenRef = useRef(0);
  const [countdown, setCountdown] = useState(3);

  useEffect(() => {
    unmountedRef.current = false;
    return () => { unmountedRef.current = true; };
  }, []);

  // Unified mount effect: DB is the source of truth for stepper position
  useEffect(() => {
    // Always fetch meta early for quick title/thumbnail display
    channelMetaGenRef.current++;
    const gen = channelMetaGenRef.current;
    fetchChannelMeta(channelUrl).then(m => { if (m && gen === channelMetaGenRef.current) setChannelMeta(m); });

    if (!user && !authLoading) {
      setStage("awaiting_auth");
      return;
    }
    if (!user || authLoading) return;
    initChannelEval();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, authLoading, channelUrl]);

  const CHANNEL_STEPS: StepConfig[] = [
    { label: "Selecting videos", subMessages: ["Picking 50% latest + 50% most popular...", "Filtering out Shorts and videos over 90 min..."] },
    { label: "Scoring videos", subMessages: ["Fetching transcript...", "Analyzing content..."] },
    { label: "Computing channel score", subMessages: ["Averaging scores across all videos...", "Applying penalties..."] },
    { label: "Generating summary", subMessages: ["Writing channel assessment...", "Finalizing..."] },
  ];

  // Auto-start eval after 3s countdown
  useEffect(() => {
    if (stage !== "countdown" || !channelData) return;

    setCountdown(3);
    let remaining = 3;

    const tick = setInterval(() => {
      remaining--;
      setCountdown(remaining);
      if (remaining <= 0) {
        clearInterval(tick);
        startChannelEval();
      }
    }, 1000);

    countdownRef.current = tick as unknown as ReturnType<typeof setTimeout>;

    return () => clearInterval(tick);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, channelData]);

  // Step 2 -> 3 transition: computing renders, then after 600ms trigger summary
  useEffect(() => {
    if (stage !== "computing" || !channelData?.channel?.channel_id) return;
    const data = channelData;
    const channelId = data.channel!.channel_id;
    const timer = setTimeout(() => {
      setStage("summarizing");
      generateChannelSummary(data, channelId);
    }, 600);
    return () => clearTimeout(timer);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, channelData]);

  function cancelEval() {
    if (countdownRef.current) {
      clearInterval(countdownRef.current as unknown as ReturnType<typeof setInterval>);
      countdownRef.current = null;
    }
    setStage("complete");
  }

  function stopEval() {
    stopRequestedRef.current = true;
    evalRunningRef.current = false;
    setStage("complete");
  }

  async function getToken(): Promise<string> {
    const { data } = await supabase.auth.getSession();
    const token = data.session?.access_token;
    if (!token) throw new Error("AUTH_REQUIRED");
    return token;
  }

  async function fetchChannelEvalState(channelId: string, videoIds: string[]) {
    const [evalRes, channelRes] = await Promise.all([
      supabase
        .from("eval_cache")
        .select("video_id,status")
        .eq("channel_id", channelId)
        .in("video_id", videoIds),
      supabase
        .from("channels")
        .select("channel_summary")
        .eq("channel_id", channelId)
        .limit(1),
    ]);

    const completedIds: string[] = [];
    const pendingIds: string[] = [];
    const errorIds: string[] = [];
    const foundIds = new Set<string>();

    if (evalRes.data) {
      for (const row of evalRes.data) {
        foundIds.add(row.video_id);
        if (row.status === "complete") completedIds.push(row.video_id);
        else if (row.status === "pending") pendingIds.push(row.video_id);
        else if (row.status === "error") errorIds.push(row.video_id);
      }
    }

    for (const id of videoIds) {
      if (!foundIds.has(id)) pendingIds.push(id);
    }

    return {
      completedIds,
      pendingIds,
      errorIds,
      channelSummary: channelRes.data?.[0]?.channel_summary || "",
    };
  }

  async function initChannelEval() {
    // Fast path: if localStorage has completed channel, show instantly (same as video eval)
    const cached = getCachedChannel(channelUrl) as unknown as ChannelEvalResponse | null;
    if (cached && cached.uncached_count === 0) {
      const results: Record<string, EvalResult> = {};
      let allResolved = true;
      for (const v of cached.videos) {
        const cachedEval = getCachedEval(v.video_id);
        if (cachedEval && cachedEval.status === "complete" && cachedEval.video_score != null) {
          results[v.video_id] = cachedEval;
        } else {
          allResolved = false;
          break;
        }
      }
      if (allResolved) {
        setChannelData(cached);
        if (cached.channel) setChannelMeta(cached.channel as ChannelMeta);
        setVideoResults(results);
        setStage("complete");
        // Background: fetch summary from DB (non-blocking)
        if (cached.channel?.channel_id) {
          supabase
            .from("channels")
            .select("channel_summary")
            .eq("channel_id", cached.channel.channel_id)
            .limit(1)
            .then(({ data: rows }) => {
              if (rows?.[0]?.channel_summary) setChannelSummary(rows[0].channel_summary);
            });
        }
        return;
      }
    }

    // Slow path: clear stale state, query DB for current eval state
    setVideoResults({});
    setChannelSummary("");
    setVideoStatuses({});
    setError("");

    // 1. Get video list from localStorage or API
    let data = cached;

    if (!data) {
      setStage("loading_videos");
      try {
        const token = await getToken();
        data = await evaluateChannel(channelUrl, token);
        setCachedChannel(channelUrl, data as unknown as Parameters<typeof setCachedChannel>[1]);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg === "AUTH_REQUIRED") { setStage("awaiting_auth"); return; }
        if (msg === "NO_CREDITS") { setError("Insufficient credits"); setStage("error"); return; }
        setError(msg);
        setStage("error");
        return;
      }
    }

    setChannelData(data);
    if (data.channel) setChannelMeta(data.channel as ChannelMeta);

    // 2. Query DB for current eval state
    const videoIds = data.videos.map(v => v.video_id);
    const channelId = data.channel?.channel_id;
    if (!channelId) { setError("No channel ID"); setStage("error"); return; }

    const dbState = await fetchChannelEvalState(channelId, videoIds);

    // 3. Populate completed video results from DB
    if (dbState.completedIds.length > 0) {
      await refreshResultsFromDB(dbState.completedIds);
    }

    // 4. Derive step from DB state
    const allDone = dbState.pendingIds.length === 0;

    if (allDone && dbState.channelSummary) {
      setChannelSummary(dbState.channelSummary);
      setCachedChannel(channelUrl, { ...data, uncached_count: 0 } as Parameters<typeof setCachedChannel>[1]);
      setStage("complete");
    } else if (allDone) {
      // All videos done, need summary -> step 3
      setStage("summarizing");
      generateChannelSummary(data, channelId);
    } else if (dbState.completedIds.length === 0 && dbState.errorIds.length === 0) {
      // Nothing started -> countdown
      setStage("countdown");
    } else {
      // Partial progress -> resume directly (skip countdown)
      const doneIds = new Set([...dbState.completedIds, ...dbState.errorIds]);
      startChannelEval(data, doneIds);
    }
  }

  async function generateChannelSummary(data: ChannelEvalResponse, channelId: string) {
    if (summaryRunningRef.current) return;
    summaryRunningRef.current = true;
    let gotSummary = false;
    try {
      const videoIds = data.videos.map(v => v.video_id);
      const { data: freshResults } = await supabase
        .from("eval_cache")
        .select("video_id,title,video_score,duration_seconds,content_summary,verdict")
        .in("video_id", videoIds)
        .eq("status", "complete");

      const { data: session } = await supabase.auth.getSession();
      const token = session?.session?.access_token;

      if (freshResults && freshResults.length > 0 && token) {
        const summaryResp = await fetch(
          `${process.env.NEXT_PUBLIC_SUPABASE_URL}/functions/v1/evaluate-channel-summary`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${token}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              channel_id: channelId,
              channel_name: data.channel?.title,
              thumbnail_url: data.channel?.thumbnail_url,
              video_results: freshResults.map(r => ({
                video_id: r.video_id,
                title: r.title || "",
                video_score: r.video_score || 0,
                duration_seconds: r.duration_seconds || 0,
                content_summary: r.content_summary || "",
                verdict: r.verdict || "",
              })),
            }),
          }
        );
        if (summaryResp.ok) {
          const summaryData = await summaryResp.json();
          if (summaryData.channel_summary) {
            setChannelSummary(summaryData.channel_summary);
            gotSummary = true;
          }
        }
      }
    } catch {
      // fall through
    }

    if (!gotSummary) setSummaryFailed(true);
    summaryRunningRef.current = false;
    setCachedChannel(channelUrl, { ...data, uncached_count: 0 } as Parameters<typeof setCachedChannel>[1]);
    setStage("complete");
  }

  async function refreshResultsFromDB(videoIds: string[]): Promise<Record<string, EvalResult>> {
    const { data } = await supabase
      .from("eval_cache")
      .select(EVAL_CACHE_FIELDS_FULL)
      .in("video_id", videoIds);

    const results: Record<string, EvalResult> = {};
    if (data) {
      for (const row of data) {
        if (row.status === "complete" && row.video_score != null) {
          results[row.video_id] = row as unknown as EvalResult;
        }
      }
      setVideoResults((prev) => ({ ...prev, ...results }));
    }
    return results;
  }

  async function startChannelEval(dataOverride?: ChannelEvalResponse, skipIds?: Set<string>) {
    const data = dataOverride || channelData;
    if (!data || evalRunningRef.current) return;
    evalRunningRef.current = true;
    stopRequestedRef.current = false;
    setStage("evaluating");

    const uncached = data.videos.filter((v) => {
      if (skipIds?.has(v.video_id)) return false;
      if (v.cached || videoResults[v.video_id]) return false;
      const cachedEval = getCachedEval(v.video_id);
      if (cachedEval && cachedEval.status === "error") return false;
      return true;
    });
    try {
      const token = await getToken();

      for (let i = 0; i < uncached.length; i++) {
        if (stopRequestedRef.current) break;
        const v = uncached[i];

        setVideoStatuses((prev) => ({ ...prev, [v.video_id]: "evaluating" }));

        try {
          const res = await evaluateVideo(
            `https://youtube.com/watch?v=${v.video_id}`,
            token,
            data.channel?.channel_id,
            data.channel?.title
          );

          if (res.status === "pending") {
            setVideoStatuses((prev) => ({ ...prev, [v.video_id]: "analyzing" }));
            const result = await pollForResult(v.video_id);
            if (result) {
              setCachedEval(v.video_id, result);
              setVideoResults((prev) => ({ ...prev, [v.video_id]: result }));
              setVideoStatuses((prev) => ({ ...prev, [v.video_id]: "complete" }));
            } else {
              setCachedEval(v.video_id, { status: "error", video_id: v.video_id } as EvalResult);
              setVideoStatuses((prev) => ({ ...prev, [v.video_id]: "error" }));
            }
          } else if (res.status === "complete") {
            setCachedEval(v.video_id, res);
            setVideoResults((prev) => ({ ...prev, [v.video_id]: res }));
            setVideoStatuses((prev) => ({ ...prev, [v.video_id]: "complete" }));
          }
        } catch {
          setCachedEval(v.video_id, { status: "error", video_id: v.video_id } as EvalResult);
          setVideoStatuses((prev) => ({ ...prev, [v.video_id]: "error" }));
        }
      }

      evalRunningRef.current = false;
      if (stopRequestedRef.current) return;

      // Transition to step 2 - useEffect handles step 3 + summary
      setStage("computing");
    } catch (e: unknown) {
      evalRunningRef.current = false;
      setError(e instanceof Error ? e.message : String(e));
      setStage("error");
    }
  }

  async function pollForResult(videoId: string): Promise<EvalResult | null> {
    for (let i = 0; i < 30; i++) {
      if (unmountedRef.current || stopRequestedRef.current) return null;
      await new Promise((r) => setTimeout(r, 2000));
      if (unmountedRef.current || stopRequestedRef.current) return null;
      const { data } = await supabase
        .from("eval_cache")
        .select(EVAL_CACHE_FIELDS_FULL)
        .eq("video_id", videoId)
        .limit(1);
      if (data && data[0]?.status === "complete") return data[0] as EvalResult;
      if (data && data[0]?.status === "error") return null;
    }
    return null;
  }

  // Compute aggregate score
  const allScores = channelData?.videos
    .map((v) => videoResults[v.video_id]?.video_score)
    .filter((s): s is number => s != null) || [];
  const aggregateScore = allScores.length > 0
    ? Math.round(allScores.reduce((a, b) => a + b, 0) / allScores.length)
    : null;

  // Compute aggregate metrics from individual video results
  const completedResults = Object.values(videoResults).filter(
    (r) => r.video_score != null && r.title_content_similarity_score != null
  );
  const avgMetric = (fn: (r: EvalResult) => number | undefined) => {
    const vals = completedResults.map(fn).filter((v): v is number => v != null);
    return vals.length > 0 ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
  };
  const avgSimilarity = avgMetric((r) => r.title_content_similarity_score);
  const avgFocus = avgMetric((r) => r.focus_ratio);
  const avgDeception = avgMetric((r) => r.deception_score);
  const avgSponsor = avgMetric((r) => r.sponsor_interruption_score);

  // Build VideoEvaluation objects for completed videos (for VideoRow)
  const buildVideoEval = (v: ChannelVideo): VideoEvaluation | null => {
    const r = videoResults[v.video_id];
    if (!r || r.video_score == null) return null;
    return {
      video_id: v.video_id,
      title: v.title || r.title || v.video_id,
      video_score: r.video_score,
      duration_seconds: r.duration_seconds,
      title_content_similarity_score: r.title_content_similarity_score ?? 0,
      deception_score: r.deception_score ?? 0,
      focus_ratio: r.focus_ratio ?? 0,
      time_to_main_content: r.time_to_main_content ?? 0,
      sponsor_interruption_score: r.sponsor_interruption_score ?? 0,
      title_analysis_reasoning: r.title_analysis_reasoning ?? "",
      deception_reasoning: r.deception_reasoning ?? "",
      focus_reasoning: r.focus_reasoning ?? "",
      time_reasoning: r.time_reasoning ?? "",
      sponsor_reasoning: r.sponsor_reasoning ?? "",
    };
  };

  const hideAuth = stage === "awaiting_auth";

  return (
    <>
      <Header hideAuth={hideAuth} />
      <div className="container">

      {/* Channel header - same structure as ChannelPage */}
      {channelMeta ? (
        <div className="channel-header">
          <div className="channel-header-left">
            <div className="channel-thumb-large">
              {channelMeta.thumbnail_url ? (
                <img src={channelMeta.thumbnail_url} alt={channelMeta.title} />
              ) : (
                <div className="thumb thumb-initial thumb-initial-lg">{channelMeta.title?.[0]?.toUpperCase() ?? "?"}</div>
              )}
            </div>
            <div className="channel-header-info">
              <div className="channel-mobile-top-row">
                <h1 className="channel-page-title">{channelMeta.title}</h1>
                {stage === "complete" && aggregateScore != null && (
                  <span className="channel-score-inline" style={{ color: scoreColor(aggregateScore) }}>{aggregateScore}</span>
                )}
              </div>
              <div className="channel-tags-grid">
                <div className="channel-tags-row">
                  {channelMeta.subscriber_count && <span className="channel-tag-secondary">{formatSubscribers(channelMeta.subscriber_count)} subscribers</span>}
                  {channelData && <span className="channel-tag-secondary">{channelData.total_videos} videos</span>}
                  {completedResults.length > 0 && <span className="channel-tag-secondary">{completedResults.length} evaluated</span>}
                </div>
              </div>
              {channelMeta.description && !channelData && (
                <p style={{ fontFamily: 'var(--font-body)', fontSize: '0.82rem', color: 'var(--ink-tertiary)', lineHeight: '1.5', maxWidth: '500px' }}>{channelMeta.description}</p>
              )}

              {/* States - inline within header info */}
              {stage === "awaiting_auth" && (
                <div className="eval-auth-prompt">
                  <button className="eval-auth-btn auth-btn-google" onClick={signIn}><GoogleIcon /> Sign in to score</button>
                </div>
              )}
              {stage === "loading_videos" && (
                <div className="eval-stage-msg"><div className="eval-spinner" /><span>Fetching channel videos...</span></div>
              )}
              {stage === "error" && (
                <div className="eval-error-block">
                  <div className="eval-error">{error}</div>
                </div>
              )}
            </div>
          </div>
          {stage === "complete" && aggregateScore != null && (
            <div className="channel-header-score" style={{ color: scoreColor(aggregateScore) }}>
              {aggregateScore}
            </div>
          )}
        </div>
      ) : (
        <div className="eval-stage-msg"><div className="eval-spinner" /><span>Loading channel...</span></div>
      )}

      {/* Progress + skeleton during channel eval */}
      {(stage === "evaluating" || stage === "countdown" || stage === "computing" || stage === "summarizing") && channelData && (() => {
        const activeVideo = channelData.videos.find(v => {
          const s = videoStatuses[v.video_id];
          return s === "evaluating" || s === "analyzing";
        });
        const activeStatus = activeVideo ? videoStatuses[activeVideo.video_id] : null;

        const total = channelData.videos.length;
        const done = channelData.videos.filter(v => videoResults[v.video_id]?.video_score != null).length;

        // Step 0: selecting videos (countdown)
        // Step 1: scoring videos (main loop)
        // Step 2: computing channel score
        // Step 3: generating summary
        const channelStep = stage === "countdown" ? 0
          : stage === "computing" ? 2
          : stage === "summarizing" ? 3
          : done < total ? 1 : 2;

        // Dynamic sub-message for the active step
        let activeSub: string | undefined;
        if (stage === "countdown") {
          activeSub = `Starting in ${countdown}s...`;
        } else if (stage === "computing" || stage === "summarizing") {
          activeSub = undefined; // use default sub-messages from step config
        } else if (done < total) {
          const videoLabel = activeStatus === "evaluating"
            ? `Fetching transcript for "${activeVideo?.title?.slice(0, 35)}..."`
            : activeStatus === "analyzing"
            ? `Analyzing "${activeVideo?.title?.slice(0, 35)}..."`
            : "Preparing next video...";
          activeSub = `${done} of ${total} scored - ${videoLabel}`;
        }

        return (
          <div className="eval-channel-progress">
            <EvalStepper currentStep={channelStep} steps={CHANNEL_STEPS} activeSubMessage={activeSub} />
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginTop: '8px' }}>
              {stage === "countdown" && (
                <>
                  <span style={{ fontFamily: 'var(--font-data)', fontSize: '0.7rem', color: 'var(--ink-tertiary)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>Uses ~15 credits</span>
                  <button className="eval-cancel-btn" onClick={cancelEval}>cancel</button>
                </>
              )}
              {stage === "evaluating" && (
                <button className="eval-cancel-btn" onClick={stopEval}>stop</button>
              )}
            </div>
          </div>
        );
      })()}

      {/* Summary failure message */}
      {summaryFailed && !channelSummary && stage === "complete" && (
        <p style={{ fontFamily: 'var(--font-data)', fontSize: '0.7rem', color: 'var(--ink-tertiary)', textTransform: 'uppercase', letterSpacing: '0.04em', marginTop: '16px' }}>Channel summary unavailable</p>
      )}

      {/* Verdict - only for user-submitted evals */}
      {channelSummary && aggregateScore != null && (
        <div className="vr-verdict" style={{
          background: `linear-gradient(135deg, ${scoreColor(aggregateScore)}18 0%, ${scoreColor(aggregateScore)}05 100%)`,
          border: `1px solid ${scoreColor(aggregateScore)}20`,
        }}>
          <div className="vr-verdict-label">Channel Summary</div>
          <p className="vr-verdict-text">{channelSummary}</p>
        </div>
      )}

      {/* Metrics grid - same as ChannelPage */}
      {stage === "complete" && completedResults.length > 0 && (
        <div className="channel-metrics-grid">
          <div className="channel-metric-card channel-metric-card-positive" data-tooltip={"Does the video deliver what the title promises?\nHigher = better"}>
            <span className="channel-metric-card-label">Title-Content Similarity</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor(avgSimilarity * 10) }}>{avgSimilarity.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
          <div className="channel-metric-card channel-metric-card-positive" data-tooltip={"What % of the video stays on topic, and how quickly it gets there.\nHigher = better"}>
            <span className="channel-metric-card-label">Focus + Time to Content</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor(avgFocus * 10) }}>{avgFocus.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
          <div className="channel-metric-card channel-metric-card-penalty" data-tooltip={"Does the title make claims the video contradicts or never addresses?\nLower = better"}>
            <span className="channel-metric-card-label">Deception</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor((10 - avgDeception) * 10) }}>{avgDeception.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
          <div className="channel-metric-card channel-metric-card-penalty" data-tooltip={"How much sponsor content interrupts the video?\nLower = better"}>
            <span className="channel-metric-card-label">Sponsor Interruption</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor((10 - avgSponsor) * 10) }}>{avgSponsor.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
        </div>
      )}

      {/* Video list - same as ChannelPage */}
      {channelData && (stage === "complete" || stage === "evaluating" || stage === "countdown") && (
        <div className="videos-section">
          <h2 className="section-title">Evaluated Videos</h2>
          {channelData.videos
            .filter(v => {
              const liveStatus = videoStatuses[v.video_id];
              if (liveStatus === "error") return false;
              return true;
            })
            .map((v) => {
              const videoEval = buildVideoEval(v);
              const liveStatus = videoStatuses[v.video_id];
              const isActive = liveStatus === "evaluating" || liveStatus === "analyzing";

              // Completed video - use VideoRow (same as ChannelPage)
              if (videoEval) {
                return <VideoRow key={v.video_id} video={videoEval} />;
              }

              // In-progress or pending video - show as pending row
              const statusLabel = isActive
                ? (liveStatus === "evaluating" ? "Fetching transcript..." : "Analyzing...")
                : "Pending";

              return (
                <div key={v.video_id} className="video-item">
                  <div className={`video-row ${isActive ? "video-row-active" : ""}`}>
                    <div className="video-thumb-container">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={`https://img.youtube.com/vi/${v.video_id}/sddefault.jpg`}
                        alt={v.title}
                        className="video-thumb"
                        onError={(e) => { (e.target as HTMLImageElement).src = `https://img.youtube.com/vi/${v.video_id}/mqdefault.jpg`; }}
                      />
                      {isActive && <div className="play-overlay"><div className="eval-spinner" /></div>}
                    </div>
                    <div className="video-info">
                      <span className="video-title-link">{v.title}</span>
                      <span className="video-meta">
                        {statusLabel}
                        {v.duration_seconds > 0 && ` - ${formatDuration(v.duration_seconds)}`}
                        {v.view_count > 0 && ` - ${formatViews(v.view_count)}`}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })}
        </div>
      )}
    </div>
    </>
  );
}

interface HistoryItem {
  video_id: string;
  title: string;
  video_score: number | null;
  status: string;
  created_at: string;
  channel_id: string | null;
  channel_name: string | null;
}

function EvalHistoryPage({ userId }: { userId: string }) {
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
    return <div className="dash-history-loading"><div className="eval-spinner" /></div>;
  }

  if (history.length === 0) {
    return (
      <div className="dash-empty">
        <p className="dash-empty-text">No evaluations yet</p>
        <p className="dash-empty-sub">Paste a YouTube video or channel URL on the <Link href="/">home page</Link> to get started</p>
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
    <div className="eval-history-page">
      <h2 className="dash-section-title">Your Evaluations</h2>
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
                    {item.status === "complete" && item.video_score != null ? (
                      <span className="dash-card-score" style={{ color: scoreColor(item.video_score) }}>{Math.round(item.video_score)}</span>
                    ) : (
                      <span className="dash-card-status">{item.status}</span>
                    )}
                  </div>
                </div>
              </Link>
            );
          }

          const { channelId, name, items } = entry;
          const completed = items.filter(i => i.status === "complete" && i.video_score != null);
          const avgScore = completed.length > 0
            ? Math.round(completed.reduce((s, i) => s + (i.video_score || 0), 0) / completed.length)
            : null;
          const firstVideoId = items[0]?.video_id;

          return (
            <Link key={channelId} href={`/evaluate?channel=${encodeURIComponent(`https://youtube.com/channel/${channelId}`)}`} className="dash-card">
              <div className="dash-card-thumb dash-card-thumb-channel">
                {firstVideoId && <img src={`https://i.ytimg.com/vi/${firstVideoId}/sddefault.jpg`} alt="" onError={(e) => { (e.target as HTMLImageElement).src = `https://i.ytimg.com/vi/${firstVideoId}/mqdefault.jpg`; }} />}
                <span className="dash-card-badge dash-card-badge-channel">Channel</span>
              </div>
              <div className="dash-card-body">
                <span className="dash-card-title">{name}</span>
                <div className="dash-card-meta">
                  <span>{items.length} videos - {new Date(items[0]?.created_at).toLocaleDateString()}</span>
                  {avgScore != null && (
                    <span className="dash-card-score" style={{ color: scoreColor(avgScore) }}>{avgScore}</span>
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

