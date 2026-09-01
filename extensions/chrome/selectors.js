// Per-surface DOM selectors, isolated here so YouTube DOM changes touch one file.
// Covers: home, search results, watch sidebar, subscriptions, channel videos tab.

// Renderer elements that wrap a single feed video thumbnail across surfaces.
// (old ytd-* renderers + the newer yt-lockup-view-model layout)
export const THUMB_RENDERERS = [
  "ytd-rich-item-renderer",       // home / subscriptions / channel grid
  "ytd-video-renderer",           // search results
  "ytd-compact-video-renderer",   // watch sidebar
  "ytd-grid-video-renderer",      // legacy channel grid
  "yt-lockup-view-model",         // unified lockup (2025+)
];

export const RENDERER_SELECTOR = THUMB_RENDERERS.join(",");

// Shorts markers - never processed.
const SHORTS_RENDERERS = [
  "ytd-reel-item-renderer",
  "ytm-shorts-lockup-view-model",
  "ytd-reel-shelf-renderer",
  "ytd-rich-shelf-renderer[is-shorts]",
];

export function isShorts(el) {
  if (SHORTS_RENDERERS.some((s) => el.matches?.(s) || el.closest?.(s))) return true;
  const a = el.querySelector?.('a[href*="/shorts/"]');
  return !!a;
}

// Find the video id for a renderer, or null if none / shorts.
export function getVideoId(el) {
  if (isShorts(el)) return null;
  const anchors = el.querySelectorAll('a[href]');
  for (const a of anchors) {
    const href = a.getAttribute("href") || "";
    if (href.includes("/shorts/")) return null;
    const m = href.match(/[?&]v=([\w-]{11})/) || href.match(/youtu\.be\/([\w-]{11})/);
    if (m) return m[1];
  }
  return null;
}

// The anchor / element the badge is positioned over (the thumbnail box).
export function getThumbAnchor(el) {
  return (
    el.querySelector("a#thumbnail") ||
    el.querySelector("a.yt-lockup-view-model-wiz__content-image") ||
    el.querySelector(".yt-lockup-view-model__content-image") ||
    el.querySelector('a[href*="watch?v="]') ||
    el
  );
}

// The title text element (for retitle).
// Class names in the lockup view model rotate with YouTube experiments, so
// after the stable ids we fall back to STRUCTURE: the title always lives in
// an h3 (h3 > a > span in current markup). Return the innermost text holder.
export function getTitleEl(el) {
  const direct = el.querySelector("#video-title") || el.querySelector("#video-title-link");
  if (direct) return direct;
  for (const sel of ["h3 a span", "h3 span", "h3 a", "h3"]) {
    const cand = el.querySelector(sel);
    if (cand && (cand.textContent || "").trim()) return cand;
  }
  return null;
}
