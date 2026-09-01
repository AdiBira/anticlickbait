// Playwright UI test for the AntiClickbait extension in MOCK mode.
//
// Loads the unpacked extension into a persistent Chromium context, serves a
// fixture page as https://www.youtube.com/ (so the content script injects on a
// controlled DOM), and asserts the full badge / retitle / dim / hover-card UX.
// Also attempts a non-fatal smoke check against the real youtube.com home.
//
// Run: NODE_PATH=../../extensions/spikes/transcript-poc/node_modules \
//        node test_extension.mjs
// (reuses the Playwright + cached chromium-1208 already present in the spike)

import { chromium } from "playwright";
import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const EXT_SRC = path.join(REPO, "extensions/chrome");
const FIXTURE = fs.readFileSync(path.join(__dirname, "fixture.html"), "utf8");
const EXE = path.join(
  os.homedir(),
  "Library/Caches/ms-playwright/chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
);

let pass = 0,
  fail = 0;
function check(name, cond) {
  if (cond) {
    pass++;
    console.log(`  PASS  ${name}`);
  } else {
    fail++;
    console.log(`  FAIL  ${name}`);
  }
}

// Build a temp copy of the extension with MOCK=true.
function buildMockExtension() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "acb-ext-"));
  fs.cpSync(EXT_SRC, tmp, { recursive: true });
  const cfgPath = path.join(tmp, "config.js");
  const cfg = fs.readFileSync(cfgPath, "utf8").replace("export const MOCK = false;", "export const MOCK = true;");
  fs.writeFileSync(cfgPath, cfg);
  return tmp;
}

async function launch(extDir, headless) {
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), "acb-prof-"));
  return chromium.launchPersistentContext(userDir, {
    headless,
    executablePath: EXE,
    viewport: { width: 1280, height: 900 },
    args: [`--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`],
  });
}

function parseRgb(s) {
  const m = s.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
  return m ? { r: +m[1], g: +m[2], b: +m[3] } : null;
}

async function run() {
  const extDir = buildMockExtension();
  let ctx;
  let mode = "headless(new)";
  ctx = await launch(extDir, true);
  // If the extension service worker never registers, headless didn't load it.
  await new Promise((r) => setTimeout(r, 1500));
  if (ctx.serviceWorkers().length === 0) {
    console.log("[info] no service worker in headless - relaunching headed (extensions need a full browser).");
    await ctx.close();
    ctx = await launch(extDir, false);
    mode = "headed";
    await new Promise((r) => setTimeout(r, 1500));
  }
  console.log(`[info] launch mode: ${mode}; serviceWorkers=${ctx.serviceWorkers().length}`);

  const page = await ctx.newPage();

  // --- Non-fatal smoke against real youtube.com ---
  try {
    await page.goto("https://www.youtube.com/", { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForTimeout(4000);
    const real = await page.locator(".pill").count();
    console.log(`[smoke] real youtube.com home: ${real} badge pill(s) rendered (mock scores).`);
  } catch (e) {
    console.log(`[smoke] real youtube.com blocked/failed (${String(e).split("\n")[0]}) - using fixture only.`);
  }

  // --- Authoritative assertions on the routed fixture ---
  await page.route("https://www.youtube.com/**", (route) => {
    if (route.request().resourceType() === "document") {
      route.fulfill({ status: 200, contentType: "text/html", body: FIXTURE });
    } else {
      route.abort();
    }
  });
  await page.goto("https://www.youtube.com/", { waitUntil: "domcontentloaded" });
  // Dwell (600ms) + mock round-trip.
  await page.waitForTimeout(2500);

  console.log("\nFixture assertions:");

  // Badges render on the (fixture) youtube home.
  const pills = await page.locator(".pill").count();
  check("badges render on youtube home (>=3 pills)", pills >= 3);

  // Correct number + colour per score.
  const rend = (id) => page.locator(`ytd-rich-item-renderer:has(a[href*="${id}"])`);
  const pill15 = rend("mock0000015").locator(".pill");
  check("mock 15 pill shows '15'", (await pill15.textContent())?.trim() === "15");
  const c15 = parseRgb(await pill15.evaluate((e) => getComputedStyle(e).color));
  check("mock 15 pill is red (r>g)", c15 && c15.r > c15.g);

  const pill85 = rend("mock0000085").locator(".pill");
  check("mock 85 pill shows '85'", (await pill85.textContent())?.trim() === "85");
  const c85 = parseRgb(await pill85.evaluate((e) => getComputedStyle(e).color));
  check("mock 85 pill is green (g>r)", c85 && c85.g > c85.r);

  // Evaluating dot for the never-resolving mock.
  const dot = await rend("mockeval001").locator(".dot.eval").count();
  check("evaluating pulsing dot renders", dot === 1);

  // Unscoreable shows nothing (no pill).
  const musicPill = await rend("mockmusic01").locator(".pill").count();
  check("unscoreable (music) shows no pill", musicPill === 0);

  // Shorts ignored (no badge host inside the shorts renderer).
  const shortHost = await page.locator('ytd-rich-item-renderer:has(a[href*="mockshort01"]) .acb-host').count();
  check("shorts item is ignored (no badge)", shortHost === 0);

  // Retitle applied on the clickbait video.
  const title15 = rend("mock0000015").locator("#video-title");
  const retitled = (await title15.textContent())?.trim();
  check("retitle replaced the clickbait title", retitled === "What the video actually shows, plainly stated");
  const marker = await rend("mock0000015").locator(".acb-marker").count();
  check("retitle marker icon present", marker === 1);

  // Toggle restores original.
  await rend("mock0000015").locator(".acb-marker").click();
  await page.waitForTimeout(200);
  const restored = (await title15.textContent())?.trim();
  check("marker toggle restores original title", restored === "You WON'T BELIEVE What Happened Next");

  // Dim applied to score < 40.
  const dimmed = await rend("mock0000015").evaluate((e) => e.classList.contains("acb-dim"));
  check("dim applied to low scorer", dimmed === true);
  const dim85 = await rend("mock0000085").evaluate((e) => e.classList.contains("acb-dim"));
  check("dim NOT applied to high scorer", dim85 === false);

  // Hover card opens with metrics + verdict.
  await rend("mock0000015").locator(".acb-host").dispatchEvent("mouseenter");
  await page.waitForTimeout(300);
  const cardCount = await page.locator(".card").count();
  check("hover card opens", cardCount >= 1);
  const bars = await page.locator(".card .mrow").count();
  check("hover card shows metric bars", bars >= 4);
  const verdict = (await page.locator(".card .verdict").textContent().catch(() => "")) || "";
  check("hover card shows a verdict line", verdict.length > 0);

  await ctx.close();

  console.log(`\n${pass} passed, ${fail} failed  (launch: ${mode})`);
  process.exit(fail === 0 ? 0 : 1);
}

run().catch((e) => {
  console.error("FATAL", e);
  process.exit(1);
});
