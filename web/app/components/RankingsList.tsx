"use client";

import { useState, useMemo, useCallback, useEffect } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import FilterBar from "./FilterBar";
import ChannelRow from "./ChannelRow";
import { type RankedChannel } from "../lib/api";

type SortKey = "score" | "subscribers";
type SortDir = "asc" | "desc";

const SortArrow = ({ active, dir }: { active: boolean; dir: SortDir }) => (
  <svg
    className="sort-arrow"
    viewBox="0 0 24 24"
    fill="none"
    stroke={active ? "#ededed" : "currentColor"}
    strokeWidth="3"
    strokeLinecap="square"
    strokeLinejoin="miter"
  >
    {dir === "desc" ? (
      <polyline points="6 9 12 15 18 9" />
    ) : (
      <polyline points="6 15 12 9 18 15" />
    )}
  </svg>
);

const PAGE_SIZE = 15;

export default function RankingsList({ channels }: { channels: RankedChannel[] }) {
  const searchParams = useSearchParams();
  const router = useRouter();

  // Initialize state from URL params
  const [category, setCategory] = useState(searchParams.get("category") || "");
  const [country, setCountry] = useState(searchParams.get("country") || "");
  const [search, setSearch] = useState(searchParams.get("q") || "");
  const [sortKey, setSortKey] = useState<SortKey>(
    searchParams.get("sort") === "score" ? "score" : "subscribers"
  );
  const [sortDir, setSortDir] = useState<SortDir>(
    searchParams.get("dir") === "asc" ? "asc" : "desc"
  );
  const [page, setPage] = useState(() => {
    const p = parseInt(searchParams.get("page") || "1", 10);
    return p > 0 ? p : 1;
  });

  // Sync state to URL params
  useEffect(() => {
    const params = new URLSearchParams();
    if (category) params.set("category", category);
    if (country) params.set("country", country);
    if (search) params.set("q", search);
    if (sortKey !== "subscribers") params.set("sort", sortKey);
    if (sortDir !== "desc") params.set("dir", sortDir);
    if (page > 1) params.set("page", String(page));
    const qs = params.toString();
    const newUrl = qs ? `/?${qs}` : "/";
    router.replace(newUrl, { scroll: false });
  }, [category, country, search, sortKey, sortDir, page, router]);

  const categories = useMemo(
    () => [...new Set(channels.map((c) => c.category_name).filter(Boolean))].sort(),
    [channels]
  );

  const countries = useMemo(
    () => [...new Set(channels.map((c) => c.country).filter(Boolean))].sort(),
    [channels]
  );

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === "desc" ? "asc" : "desc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const filtered = useMemo(() => {
    let result = [...channels];
    if (search) result = result.filter((c) => c.title.toLowerCase().includes(search.toLowerCase()));
    if (category) result = result.filter((c) => c.category_name === category);
    if (country) result = result.filter((c) => c.country === country);

    const mul = sortDir === "desc" ? 1 : -1;
    if (sortKey === "subscribers") {
      result.sort((a, b) => mul * (b.subscriber_count - a.subscriber_count));
    } else {
      result.sort((a, b) => mul * (b.channel_score - a.channel_score));
    }
    return result;
  }, [channels, search, category, country, sortKey, sortDir]);

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const paged = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const handleCategoryChange = useCallback((v: string) => { setCategory(v); setPage(1); }, []);
  const handleCountryChange = useCallback((v: string) => { setCountry(v); setPage(1); }, []);
  const handleSearchChange = useCallback((v: string) => { setSearch(v); setPage(1); }, []);

  const [tab, setTab] = useState<"all" | "honest" | "misleading">("all");

  const handleTabChange = (t: "all" | "honest" | "misleading") => {
    setTab(t);
    setPage(1);
    if (t === "all") {
      setSortKey("subscribers");
      setSortDir("desc");
    } else if (t === "honest") {
      setSortKey("score");
      setSortDir("desc");
    } else if (t === "misleading") {
      setSortKey("score");
      setSortDir("asc");
    }
  };

  return (
    <>
      <div className="tabs">
        <button className={`tab ${tab === "all" ? "tab-active" : ""}`} onClick={() => handleTabChange("all")}>All Channels</button>
        <button className={`tab ${tab === "honest" ? "tab-active" : ""}`} onClick={() => handleTabChange("honest")}>Most Honest</button>
        <button className={`tab ${tab === "misleading" ? "tab-active" : ""}`} onClick={() => handleTabChange("misleading")}>Most Misleading</button>
      </div>

      <FilterBar
        categories={categories}
        countries={countries}
        category={category}
        country={country}
        search={search}
        onCategoryChange={handleCategoryChange}
        onCountryChange={handleCountryChange}
        onSearchChange={handleSearchChange}
      />

      <div className="rankings-list">
        <div className="list-header">
          <span></span>
          <span></span>
          <span>Channel</span>
          <span className="sortable" onClick={() => toggleSort("subscribers")}>
            Subscribers
            <SortArrow active={sortKey === "subscribers"} dir={sortKey === "subscribers" ? sortDir : "desc"} />
          </span>
          <span>Category</span>
          <span className="sortable" onClick={() => toggleSort("score")} style={{ justifyContent: "center" }}>
            Score
            <SortArrow active={sortKey === "score"} dir={sortKey === "score" ? sortDir : "desc"} />
          </span>
        </div>

        {paged.map((channel, i) => (
          <ChannelRow
            key={channel.channel_id}
            channel={channel}
            rank={(page - 1) * PAGE_SIZE + i + 1}
          />
        ))}

        {filtered.length === 0 && (
          <div className="text-dim" style={{
            textAlign: "center",
            padding: "var(--spacing-xl)",
            fontFamily: "var(--font-data)",
            fontSize: "0.85rem",
            textTransform: "uppercase" as const,
          }}>
            No channels found
          </div>
        )}
      </div>

      {totalPages > 1 && (
        <div className="pagination">
          <button
            className="pagination-btn"
            disabled={page === 1}
            onClick={() => setPage(1)}
          >
            &laquo;
          </button>
          <button
            className="pagination-btn"
            disabled={page === 1}
            onClick={() => setPage(page - 1)}
          >
            &larr; Prev
          </button>
          <span className="pagination-info">
            {page} / {totalPages}
          </span>
          <button
            className="pagination-btn"
            disabled={page === totalPages}
            onClick={() => setPage(page + 1)}
          >
            Next &rarr;
          </button>
          <button
            className="pagination-btn"
            disabled={page === totalPages}
            onClick={() => setPage(totalPages)}
          >
            &raquo;
          </button>
        </div>
      )}
    </>
  );
}
