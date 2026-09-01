#!/usr/bin/env node
/**
 * Test: Video navigation resets all state
 *
 * Verifies that navigating from a completed video eval to a new video
 * does NOT show the previous video's verdict, metrics, or reasoning.
 *
 * Prerequisites: npx playwright install chromium
 * Usage: node tests/test_video_navigation_reset.mjs [base_url]
 */

import { chromium } from "playwright";

const BASE = process.argv[2] || "https://anticlickbait.vercel.app";

// Video A: a video that has a completed eval in the DB (use any previously evaluated video)
// Video B: a different video
// You can override these with env vars
const VIDEO_A_ID = process.env.TEST_VIDEO_A || "";
const VIDEO_B_ID = process.env.TEST_VIDEO_B || "";

async function run() {
  if (!VIDEO_A_ID || !VIDEO_B_ID) {
    console.log("SKIP: Set TEST_VIDEO_A and TEST_VIDEO_B env vars to two different video IDs");
    console.log("  TEST_VIDEO_A should be a previously evaluated video (has results in DB)");
    console.log("  TEST_VIDEO_B should be a different video");
    process.exit(0);
  }

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  let passed = true;

  // Step 1: Load video A and wait for results to appear
  console.log(`[1/5] Loading video A: ${VIDEO_A_ID}`);
  await page.goto(`${BASE}/evaluate?v=${VIDEO_A_ID}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3000);

  // Check if video A has results displayed
  const hasResultA = await page.locator(".eval-result, .vr-verdict").count();
  if (hasResultA === 0) {
    console.log("  WARN: Video A has no results displayed. Test may be inconclusive.");
    console.log("  Use a video ID that has a completed evaluation.");
  } else {
    console.log(`  OK: Video A shows ${hasResultA} result sections`);
  }

  // Capture video A's verdict text (if any)
  const verdictA = await page.locator(".vr-verdict-text").textContent().catch(() => "");
  if (verdictA) {
    console.log(`  Video A verdict: "${verdictA.slice(0, 80)}..."`);
  }

  // Step 2: Navigate to video B via client-side navigation
  console.log(`[2/5] Client-side navigating to video B: ${VIDEO_B_ID}`);
  // Use Next.js router to navigate (simulates clicking a link)
  await page.evaluate((url) => {
    window.history.pushState({}, "", url);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, `/evaluate?v=${VIDEO_B_ID}`);

  await page.waitForTimeout(2000);

  // Step 3: Check that video A's data is NOT visible
  console.log("[3/5] Checking video A's data is NOT visible on video B's page");

  // The verdict should either be empty or different from video A's
  const verdictB = await page.locator(".vr-verdict-text").textContent().catch(() => "");
  if (verdictA && verdictB && verdictA === verdictB) {
    console.error(`  FAIL: Video B shows Video A's verdict!`);
    console.error(`  Verdict: "${verdictB.slice(0, 80)}..."`);
    passed = false;
  } else {
    console.log("  OK: Verdict is not stale");
  }

  // Step 4: Check that the score display is not showing video A's score
  console.log("[4/5] Checking score is not stale");
  const scoreEl = await page.locator(".vr-score").textContent().catch(() => "");
  if (scoreEl && hasResultA > 0) {
    // If video B already shows a score, it should NOT be during loading_meta stage
    const stageMsg = await page.locator(".eval-stage-msg").count();
    if (stageMsg > 0 && scoreEl) {
      console.error(`  FAIL: Score visible while still in loading state`);
      passed = false;
    } else {
      console.log(`  OK: Score display is consistent with stage`);
    }
  } else {
    console.log("  OK: No stale score displayed");
  }

  // Step 5: Check that eval-result section (metrics, reasoning) is not from video A
  console.log("[5/5] Checking metrics/reasoning are not stale");
  const reasoningText = await page.locator(".video-metric-reasoning").first().textContent().catch(() => "");
  if (verdictA && reasoningText && verdictA.includes(reasoningText.slice(0, 30))) {
    console.error(`  FAIL: Reasoning text matches video A's verdict`);
    passed = false;
  } else {
    console.log("  OK: No stale reasoning");
  }

  console.log(`\n${passed ? "PASSED" : "FAILED"}`);

  await browser.close();
  process.exit(passed ? 0 : 1);
}

run().catch((e) => {
  console.error("Test crashed:", e.message);
  process.exit(1);
});
