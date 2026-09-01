"use client";

import { useState } from "react";
import Link from "next/link";
import { type RankedChannel, formatSubscribers, scoreColor } from "../lib/api";

export default function ChannelRow({
  channel,
  rank,
}: {
  channel: RankedChannel;
  rank: number;
}) {
  const handle = channel.handle?.replace("@", "") || channel.channel_id;
  const [imgError, setImgError] = useState(false);

  return (
    <Link href={`/channel/${handle}`} className="channel-row" prefetch={true}>
      <div className="rank">
        {String(rank).padStart(2, "0")}
      </div>

      <div className="thumb-container">
        {channel.thumbnail_url && !imgError ? (
          /* eslint-disable-next-line @next/next/no-img-element */
          <img
            src={channel.thumbnail_url}
            alt={channel.title}
            className="thumb"
            onError={() => setImgError(true)}
          />
        ) : (
          <div className="thumb thumb-initial">{channel.title?.[0]?.toUpperCase() ?? "?"}</div>
        )}
      </div>

      <div className="channel-info">
        <span className="channel-name">{channel.title}</span>
        <span className="channel-cat">{channel.country || "\u2014"}</span>
      </div>

      <div className="stat-cell stat-subs">
        {formatSubscribers(channel.subscriber_count)}
      </div>

      <div className="stat-cell stat-category">
        {channel.category_name}
      </div>

      <div className="score-cell" style={{ color: scoreColor(channel.channel_score) }}>
        {Math.round(channel.channel_score)}
      </div>
    </Link>
  );
}
