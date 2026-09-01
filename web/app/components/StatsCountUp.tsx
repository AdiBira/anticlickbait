"use client";

import { useEffect, useRef } from "react";

interface StatItem {
  value: string;
  label: string;
  isStatic?: boolean;
}

interface StatsCountUpProps {
  channels: number;
  videos: number;
  hours: string;
  avgScore: number;
}

export default function StatsCountUp({ channels, videos, hours, avgScore }: StatsCountUpProps) {
  const STATS: StatItem[] = [
    { value: String(channels), label: "Channels" },
    { value: videos.toLocaleString(), label: "Videos analyzed" },
    { value: hours, label: "Hours of content" },
    { value: String(avgScore), label: "Avg channel score", isStatic: true },
  ];
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const els = containerRef.current.querySelectorAll<HTMLElement>("[data-count]");

    els.forEach((el) => {
      const text = el.getAttribute("data-count") || "";
      const hasPlus = text.includes("+");
      const hasComma = text.includes(",");
      const num = parseInt(text.replace(/[^0-9]/g, ""));
      if (isNaN(num)) return;

      const duration = 1500;
      const start = performance.now();
      el.textContent = "0";

      function tick(now: number) {
        const elapsed = now - start;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = Math.floor(num * eased);
        let display = hasComma ? current.toLocaleString() : String(current);
        if (hasPlus) display += "+";
        el.textContent = display;
        if (progress < 1) requestAnimationFrame(tick);
      }

      requestAnimationFrame(tick);
    });
  }, [channels, videos, hours, avgScore]);

  return (
    <div className="stats" ref={containerRef}>
      {STATS.map((s) => (
        <div className="stat" key={s.label}>
          <div
            className="stat-value"
            {...(s.isStatic ? {} : { "data-count": s.value })}
          >
            {s.value}
          </div>
          <div className="stat-label">{s.label}</div>
        </div>
      ))}
    </div>
  );
}
