import Header from "../components/Header";
import VideoRow from "../components/VideoRow";
import { type VideoEvaluation, scoreColor, formatSubscribers } from "../lib/api";

interface ChannelData {
  channel_id: string;
  title: string;
  thumbnail_url: string | null;
  subscriber_count: number | null;
  channel_score: number;
  channel_summary: string;
  videos_evaluated: number;
}

interface VideoData {
  video_id: string;
  title: string;
  video_score: number;
  duration_seconds: number;
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

export default function CompletedChannelResults({ channel, videos }: { channel: ChannelData; videos: VideoData[] }) {
  const score = Math.round(channel.channel_score);

  const avg = (fn: (v: VideoData) => number) => {
    const vals = videos.map(fn);
    return vals.length > 0 ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
  };
  const avgSimilarity = avg(v => v.title_content_similarity_score);
  const avgFocus = avg(v => v.focus_ratio);
  const avgDeception = avg(v => v.deception_score);
  const avgSponsor = avg(v => v.sponsor_interruption_score);

  const videoEvals: VideoEvaluation[] = videos.map(v => ({
    video_id: v.video_id,
    title: v.title,
    video_score: v.video_score,
    duration_seconds: v.duration_seconds,
    title_content_similarity_score: v.title_content_similarity_score,
    deception_score: v.deception_score,
    focus_ratio: v.focus_ratio,
    time_to_main_content: v.time_to_main_content,
    sponsor_interruption_score: v.sponsor_interruption_score,
    title_analysis_reasoning: v.title_analysis_reasoning,
    deception_reasoning: v.deception_reasoning,
    focus_reasoning: v.focus_reasoning,
    time_reasoning: v.time_reasoning,
    sponsor_reasoning: v.sponsor_reasoning,
  }));

  return (
    <>
      <Header />
      <div className="container">

        <div className="channel-header">
          <div className="channel-header-left">
            <div className="channel-thumb-large">
              {channel.thumbnail_url ? (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img src={channel.thumbnail_url} alt={channel.title} />
              ) : (
                <div className="thumb thumb-initial thumb-initial-lg">{channel.title?.[0]?.toUpperCase() ?? "?"}</div>
              )}
            </div>
            <div className="channel-header-info">
              <div className="channel-mobile-top-row">
                <h1 className="channel-page-title">{channel.title}</h1>
                <span className="channel-score-inline" style={{ color: scoreColor(score) }}>{score}</span>
              </div>
              <div className="channel-tags-grid">
                <div className="channel-tags-row">
                  {channel.subscriber_count != null && channel.subscriber_count > 0 && (
                    <span className="channel-tag-secondary">{formatSubscribers(channel.subscriber_count)} subscribers</span>
                  )}
                  <span className="channel-tag-secondary">{videos.length} evaluated</span>
                </div>
              </div>
            </div>
          </div>
          <div className="channel-header-score" style={{ color: scoreColor(score) }}>
            {score}
          </div>
        </div>

        {channel.channel_summary && (
          <div className="vr-verdict" style={{
            background: `linear-gradient(135deg, ${scoreColor(score)}18 0%, ${scoreColor(score)}05 100%)`,
            border: `1px solid ${scoreColor(score)}20`,
          }}>
            <div className="vr-verdict-label">Channel Summary</div>
            <p className="vr-verdict-text">{channel.channel_summary}</p>
          </div>
        )}

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

        <div className="videos-section">
          <h2 className="section-title">Evaluated Videos</h2>
          {videoEvals.map(v => <VideoRow key={v.video_id} video={v} />)}
        </div>
      </div>
    </>
  );
}
