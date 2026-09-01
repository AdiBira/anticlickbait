import type { Metadata } from "next";
import Header from "../components/Header";

export const metadata: Metadata = {
  title: "Privacy - Anticlickbait",
  description: "What the Anticlickbait site and Chrome extension collect, what they don't, and how to delete your data.",
};

export default function PrivacyPage() {
  return (
    <>
      <Header />

      <div className="page-hero">
        <div className="page-hero-grid"></div>
        <div className="page-hero-glow"></div>
        <div className="container page-hero-content">
          <h1 className="hiw-title">Privacy</h1>
        </div>
      </div>

      <div className="container">
        <div className="hiw-content">

          <section className="hiw-section">
            <p className="hiw-text">
              This covers both the Anticlickbait website and the Chrome extension.
              We collect the minimum needed to score videos and keep the service
              running. No trackers, no data sales.
            </p>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">What we collect</h2>
            <ul className="hiw-list">
              <li>Video IDs of thumbnails you dwell on. We use them to look up or generate a score for that video. We do not record which pages you were on or build a browsing history.</li>
              <li>Your account email, if you sign in with Google. Sign-in is only needed for fresh evaluations.</li>
              <li>Daily usage counters, so we can enforce free limits and understand load.</li>
              <li>An anonymous install ID, used only for coarse aggregate usage stats (for example, how many installs are active).</li>
            </ul>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">What we don&apos;t collect</h2>
            <ul className="hiw-list">
              <li>No browsing profiles. We don&apos;t track the sites you visit or link your activity into a profile.</li>
              <li>No selling or sharing of your data with advertisers.</li>
              <li>No third-party trackers or analytics SDKs inside the extension.</li>
              <li>Transcripts come from YouTube&apos;s public captions. We use them to score a video and do not store them.</li>
            </ul>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">Where your data lives</h2>
            <p className="hiw-text">
              Scores, account info, and usage counters are stored in Supabase
              (a hosted PostgreSQL database). Access is restricted by row-level
              security so accounts can only read their own account data.
            </p>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">Signed-out use</h2>
            <p className="hiw-text">
              The extension works without an account for videos that already have
              a score. You only need to sign in to request a fresh evaluation of a
              video that hasn&apos;t been scored yet.
            </p>
          </section>

          <section className="hiw-section">
            <h2 className="hiw-heading">Deleting your data</h2>
            <p className="hiw-text">
              Email{" "}
              <a href="mailto:aditya.biradar001@gmail.com" className="footer-link" style={{ color: "var(--ink)", borderBottom: "1px solid var(--border-medium)" }}>
                aditya.biradar001@gmail.com
              </a>{" "}
              and we&apos;ll delete your account and associated data on request.
            </p>
          </section>

        </div>
      </div>
    </>
  );
}
