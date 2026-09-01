"use client";

interface FilterBarProps {
  categories: string[];
  countries: string[];
  category: string;
  country: string;
  search: string;
  onCategoryChange: (v: string) => void;
  onCountryChange: (v: string) => void;
  onSearchChange: (v: string) => void;
}

export default function FilterBar({
  categories,
  countries,
  category,
  country,
  search,
  onCategoryChange,
  onCountryChange,
  onSearchChange,
}: FilterBarProps) {
  return (
    <section className="controls">
      <div className="filter-group">
        <span className="filter-label">Region</span>
        <select value={country} onChange={(e) => onCountryChange(e.target.value)}>
          <option value="">Global</option>
          {countries.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </div>

      <div className="filter-group">
        <span className="filter-label">Category</span>
        <select value={category} onChange={(e) => onCategoryChange(e.target.value)}>
          <option value="">All Categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </div>

      <div className="filter-group filter-group-search">
        <svg className="filter-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="square" strokeLinejoin="miter">
          <circle cx="10.5" cy="10.5" r="6.5" />
          <line x1="15.5" y1="15.5" x2="21" y2="21" />
        </svg>
        <input
          className="filter-search-input"
          type="text"
          placeholder="Channel name..."
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
        />
      </div>
    </section>
  );
}
