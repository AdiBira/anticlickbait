import Link from "next/link";
import VideoRow from "../../components/VideoRow";
import Header from "../../components/Header";
import { type ChannelResponse, formatSubscribers, scoreColor } from "../../lib/api";

export default function ChannelPage({ data }: { data: ChannelResponse }) {
  const { channel, videos } = data;

  if (!channel?.channel_id) {
    return (
      <>
        <Header />
        <div className="container" style={{ padding: '48px 24px' }}>
          <p>Channel not found.</p>
        </div>
      </>
    );
  }

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
                <span className="channel-score-inline" style={{ color: scoreColor(channel.channel_score) }}>{Math.round(channel.channel_score)}</span>
              </div>
              <div className="channel-tags-grid">
                <div className="channel-tags-row">
                  <span className="channel-tag-primary">{channel.category_name}</span>
                  <span className="channel-tag-primary">{channel.country}</span>
                </div>
                <div className="channel-tags-row">
                  <span className="channel-tag-secondary">{formatSubscribers(channel.subscriber_count)} subscribers</span>
                  <span className="channel-tag-secondary">{channel.videos_evaluated} videos evaluated</span>
                </div>
              </div>
            </div>
          </div>
          <div className="channel-header-score" style={{ color: scoreColor(channel.channel_score) }}>
            {Math.round(channel.channel_score)}
          </div>
        </div>

        <div className="channel-metrics-grid">
          <div className="channel-metric-card channel-metric-card-positive" data-tooltip={"Does the video deliver what the title promises?\nHigher = better"}>
            <span className="channel-metric-card-label">Title-Content Similarity</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor(channel.avg_similarity * 10) }}>{channel.avg_similarity.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
          <div className="channel-metric-card channel-metric-card-positive" data-tooltip={"What % of the video stays on topic, and how quickly it gets there.\nHigher = better"}>
            <span className="channel-metric-card-label">Focus + Time to Content</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor(channel.avg_focus * 10) }}>{channel.avg_focus.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
          <div className="channel-metric-card channel-metric-card-penalty" data-tooltip={"Does the title make claims the video contradicts or never addresses?\nLower = better"}>
            <span className="channel-metric-card-label">Deception</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor((10 - channel.avg_deception) * 10) }}>{channel.avg_deception.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
          <div className="channel-metric-card channel-metric-card-penalty" data-tooltip={"How much sponsor content interrupts the video?\nLower = better"}>
            <span className="channel-metric-card-label">Sponsor Interruption</span>
            <div className="channel-metric-card-value-row">
              <span className="channel-metric-card-value" style={{ color: scoreColor((10 - channel.avg_sponsor) * 10) }}>{channel.avg_sponsor.toFixed(1)}</span>
              <span className="channel-metric-card-scale">/10</span>
            </div>
          </div>
        </div>

        <div className="videos-section">
          <h2 className="section-title">Evaluated Videos</h2>
          {videos.map((video) => (
            <VideoRow key={video.video_id} video={video} />
          ))}
        </div>
      </div>
    </>
  );
}
