#!/usr/bin/env node
/**
 * Test: Frontend stale state on video navigation
 *
 * Verifies that navigating from video A to video B via client-side link
 * does NOT send video A's title or URL in the /api/evaluate POST request.
 *
 * This catches the bug where stale closure captures old meta/videoId and
 * sends the wrong title to the backend, corrupting the eval_cache row.
 *
 * Prerequisites: npx playwright install chromium
 * Usage: node tests/test_frontend_stale_state.mjs [base_url]
 */

import { chromium } from "playwright";

const BASE = process.argv[2] || "https://anticlickbait.vercel.app";

// Two known videos with different titles - use any two distinct video IDs
const VIDEO_A_ID = "dQw4w9WgXcQ"; // Rick Astley - well-known, always has a transcript
const VIDEO_B_ID = "jNQXAC9IVRw"; // "Me at the zoo" - first YouTube video

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  const evaluateCalls = [];

  // Intercept /api/evaluate POST requests
  await page.route("**/api/evaluate", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      try {
        const body = JSON.parse(request.postData() || "{}");
        evaluateCalls.push({
          video_url: body.video_url || "",
          title: body.title || "",
          timestamp: Date.now(),
        });
      } catch { /* ignore parse errors */ }
    }
    // Let the request through (or abort - we just need to capture it)
    await route.abort();
  });

  console.log(`[1/4] Loading video A: ${BASE}/evaluate?v=${VIDEO_A_ID}`);
  await page.goto(`${BASE}/evaluate?v=${VIDEO_A_ID}`, { waitUntil: "networkidle" });

  // Wait for meta to load (title appears)
  await page.waitForTimeout(2000);

  console.log(`[2/4] Navigating to video B via client-side: ${VIDEO_B_ID}`);
  // Simulate client-side navigation by changing the URL without full page load
  await page.evaluate((id) => {
    window.history.pushState({}, "", `/evaluate?v=${id}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, VIDEO_B_ID);

  // Wait for effects to fire and any eval requests to be made
  await page.waitForTimeout(5000);

  console.log(`[3/4] Checking intercepted /api/evaluate calls...`);
  console.log(`  Captured ${evaluateCalls.length} evaluate calls`);

  let passed = true;

  for (const call of evaluateCalls) {
    console.log(`  -> video_url: ${call.video_url}`);
    console.log(`     title: ${call.title.slice(0, 60)}...`);

    // After navigating to video B, no call should contain video A's ID
    if (call.video_url.includes(VIDEO_A_ID) && call.timestamp > 0) {
      // This is fine if it was the FIRST call (for video A before navigation)
      // But if it happened AFTER navigation, that's the bug
    }

    // The critical check: if any call has video B's URL but video A's title
    if (call.video_url.includes(VIDEO_B_ID)) {
      // This call is for video B - the title must NOT reference video A
      if (call.title.toLowerCase().includes("rick") || call.title.toLowerCase().includes("astley")) {
        console.error(`  FAIL: Video B's evaluate call has Video A's title!`);
        console.error(`  This is the stale closure bug - old meta.title sent for new video`);
        passed = false;
      } else {
        console.log(`  OK: Video B's call has correct title`);
      }
    }
  }

  // Also check: after navigation, no new call should be for video A
  const postNavCalls = evaluateCalls.filter(c => c.video_url.includes(VIDEO_A_ID));
  if (postNavCalls.length > 1) {
    console.error(`  FAIL: Multiple evaluate calls for video A (stale callback fired)`);
    passed = false;
  }

  console.log(`[4/4] ${passed ? "PASSED" : "FAILED"}`);

  await browser.close();
  process.exit(passed ? 0 : 1);
}

run().catch((e) => {
  console.error("Test crashed:", e.message);
  process.exit(1);
});
