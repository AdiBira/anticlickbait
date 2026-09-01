import { Suspense } from "react";
import RankingsList from "../components/RankingsList";
import Header from "../components/Header";
import { fetchRankings } from "../lib/api";

export default async function RankingsPage() {
  const channels = await fetchRankings();

  return (
    <>
      <Header />

      <section className="rankings-section">
        <div className="container">
          <div className="rankings-top">
            <div className="rankings-title">Channel Rankings</div>
            <div className="rankings-updated">March 2026</div>
          </div>

          <Suspense>
            <RankingsList channels={channels} />
          </Suspense>
        </div>
      </section>
    </>
  );
}
