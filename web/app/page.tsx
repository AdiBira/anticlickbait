import Link from "next/link";
import Header from "./components/Header";
import EvalBanner from "./components/EvalBanner";
import { fetchRankings, scoreColor } from "./lib/api";

// Chrome Web Store listing URL. Swap this in once the extension is published.
const CHROME_STORE_URL = "#";

export default async function HomePage() {
  const channels = await fetchRankings();
  const topChannels = [...channels]
    .sort((a, b) => b.channel_score - a.channel_score)
    .slice(0, 3);

  return (
    <>
      <Header />

      <section className="hero">
        <div className="hero-grid"></div>
        <div className="hero-glow"></div>
        <div className="container hero-content">
          <span className="hero-eyebrow">Chrome Extension</span>
          <h1 className="hero-title">
            Honest scores on<br />
            <span className="hero-title-accent">every thumbnail.</span>
          </h1>
          <p className="hero-subtitle">
            AntiClickbait scores every YouTube video for how honest its title is,
            and rewrites the baited ones - right on the thumbnail, before you click.
          </p>

          <div className="hero-cta-row">
            <a href={CHROME_STORE_URL} data-placeholder="chrome-store" className="cta-primary">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 8h8.5" /><path d="M8 12l-4.2 7.3" /><path d="M15.5 15.5L11.3 8.2" />
                <circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="3" />
              </svg>
              Add to Chrome
            </a>
            <span className="cta-soon">Coming soon</span>
          </div>

          <div className="hero-mock" aria-hidden="true">
            <div className="hero-mock-card">
              <div className="hero-mock-thumb">
                <span
                  className="hero-mock-score"
                  style={{ background: scoreColor(82) }}
                >
                  82
                </span>
                <span className="hero-mock-duration">14:03</span>
              </div>
              <div className="hero-mock-titles">
                <span className="hero-mock-title-new">
                  <svg className="hero-mock-marker" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                  Reviewer tests three budget laptops and picks one
                </span>
                <span className="hero-mock-title-old">
                  I CAN&apos;T BELIEVE THIS $300 LAPTOP... (SHOCKING)
                </span>
              </div>
            </div>
          </div>

          <div className="hero-secondary">
            <span className="hero-secondary-label">Or check a single video on the web</span>
            <EvalBanner />
            <div className="hero-links">
              <Link href="/rankings" className="hero-link">Browse channel rankings</Link>
              <span className="hero-link-sep">/</span>
              <Link href="/evaluate" className="hero-link">Score a video or channel</Link>
            </div>
          </div>
        </div>
      </section>

      {channels.length > 0 && (
        <section className="rankings-teaser">
          <div className="container">
            <Link href="/rankings" className="rankings-teaser-card">
              <div className="rankings-teaser-top">
                <div>
                  <div className="rankings-teaser-title">Channel Rankings</div>
                  <div className="rankings-teaser-sub">
                    {channels.length} channels ranked by title honesty
                  </div>
                </div>
                <span className="rankings-teaser-link">View all &rarr;</span>
              </div>

              <div className="rankings-teaser-rows">
                {topChannels.map((c, i) => (
                  <div className="rankings-teaser-row" key={c.channel_id}>
                    <span className="rankings-teaser-rank">{String(i + 1).padStart(2, "0")}</span>
                    {c.thumbnail_url ? (
                      /* eslint-disable-next-line @next/next/no-img-element */
                      <img src={c.thumbnail_url} alt="" className="rankings-teaser-thumb" />
                    ) : (
                      <span className="rankings-teaser-thumb thumb-initial">
                        {c.title?.[0]?.toUpperCase() ?? "?"}
                      </span>
                    )}
                    <span className="rankings-teaser-name">{c.title}</span>
                    <span
                      className="rankings-teaser-score"
                      style={{ color: scoreColor(c.channel_score) }}
                    >
                      {Math.round(c.channel_score)}
                    </span>
                  </div>
                ))}
              </div>
            </Link>
          </div>
        </section>
      )}
    </>
  );
}
