// MAIN-world script. Ported from extensions/spikes/transcript-poc/index.js.
// Runs in the youtube.com page context so it can call InnerTube with the page's
// own visitorData. Uses the ANDROID client (WEB returns 0 caption tracks in the
// PoToken era); falls back to IOS. Parses srv3/json3/srv1 + rolling-window dedup.
//
// Bridged to the isolated-world content script via window.postMessage.
// Message strings mirror config.js MSG_FETCH / MSG_RESULT (kept as literals here
// because this is a classic script, not an ES module).

(() => {
  const MSG_FETCH = "ACB_FETCH_TRANSCRIPT";
  const MSG_RESULT = "ACB_TRANSCRIPT_RESULT";

  window.addEventListener("message", async (e) => {
    if (e.source !== window || !e.data || e.data.type !== MSG_FETCH) return;
    const { videoId, reqId } = e.data;
    let out;
    try {
      out = await fetchTranscript(videoId);
    } catch (err) {
      out = { error: String(err) };
    }
    window.postMessage({ type: MSG_RESULT, reqId, ...out }, "*");
  });

  async function fetchTranscript(videoId) {
    const cfg = window.ytcfg;
    const apiKey = cfg && cfg.get ? cfg.get("INNERTUBE_API_KEY") : null;
    const visitor = cfg && cfg.get ? cfg.get("VISITOR_DATA") : "";
    if (!apiKey) return { error: "no_api_key" };

    const clients = {
      ANDROID: { client: { clientName: "ANDROID", clientVersion: "20.10.38", androidSdkVersion: 34, hl: "en", gl: "US", visitorData: visitor } },
      IOS: { client: { clientName: "IOS", clientVersion: "20.10.38", deviceModel: "iPhone16,2", hl: "en", gl: "US", visitorData: visitor } },
    };

    let playerData = null;
    for (const ctx of Object.values(clients)) {
      let data;
      try {
        const resp = await fetch(`https://www.youtube.com/youtubei/v1/player?key=${apiKey}&prettyPrint=false`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Goog-Visitor-Id": visitor || "" },
          body: JSON.stringify({ context: ctx, videoId, contentCheckOk: true, racyCheckOk: true }),
        });
        data = await resp.json();
      } catch {
        continue;
      }
      if (data?.playabilityStatus?.status === "OK") {
        playerData = data;
        const tracks = data?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
        if (tracks.length) break;
      }
    }
    if (!playerData) return { error: "no_playable_client" };

    const details = playerData.videoDetails || {};
    const micro = playerData?.microformat?.playerMicroformatRenderer || {};
    const meta = {
      category: micro.category || null,
      is_live: !!(details.isLive || details.isLiveContent),
      channel_id: details.channelId || null,
    };
    const duration_s = details.lengthSeconds ? parseInt(details.lengthSeconds, 10) : null;
    const audioLang = micro.defaultAudioLanguage || null;

    const tracks = playerData?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
    if (tracks.length === 0) return { error: "no_caption_tracks", duration_s, meta };

    // Prefer manual over ASR; prefer the video's own language, else first track.
    const manual = tracks.filter((t) => t.kind !== "asr");
    const asr = tracks.filter((t) => t.kind === "asr");
    const prefLang = (audioLang || "en").split("-")[0];
    const byLang = (arr) => arr.find((t) => (t.languageCode || "").startsWith(prefLang)) || arr[0];
    const chosen = manual.length ? byLang(manual) : byLang(asr);
    const language = chosen.languageCode || null;

    const base = chosen.baseUrl;
    const url = base + (base.includes("?") ? "&" : "?") + "fmt=json3";
    let text;
    try {
      const cr = await fetch(url);
      if (!cr.ok) return { error: "caption_http_" + cr.status, duration_s, meta, language };
      text = await cr.text();
    } catch {
      return { error: "caption_fetch_threw", duration_s, meta, language };
    }

    const segments = parseAndDedup(text);
    if (segments.length === 0) return { error: "empty_after_parse", duration_s, meta, language };
    return { segments, duration_s, meta, language };
  }

  function parseAndDedup(text) {
    const decode = (s) =>
      s.replace(/<[^>]+>/g, "").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, " ").trim();

    const raw = [];
    const t = text.trim();
    if (t.startsWith("{") || t.startsWith(")]}")) {
      let data;
      try {
        data = JSON.parse(t.replace(/^\)\]\}'?\s*/, ""));
      } catch {
        return [];
      }
      for (const ev of data.events || []) {
        if (!ev.segs) continue;
        const line = ev.segs.map((s) => s.utf8 || "").join("").replace(/\s+/g, " ").trim();
        if (line) raw.push({ start: (ev.tStartMs || 0) / 1000, text: line });
      }
    } else if (/<p\b[^>]*\bt=/.test(t)) {
      const re = /<p\b[^>]*?\bt="([^"]+)"[^>]*>([\s\S]*?)<\/p>/g;
      let m;
      while ((m = re.exec(t)) !== null) {
        const line = decode(m[2]);
        if (line) raw.push({ start: (parseFloat(m[1]) || 0) / 1000, text: line });
      }
    } else {
      const re = /<text[^>]*\bstart="([^"]+)"[^>]*>([\s\S]*?)<\/text>/g;
      let m;
      while ((m = re.exec(t)) !== null) {
        const line = decode(m[2]);
        if (line) raw.push({ start: parseFloat(m[1]) || 0, text: line });
      }
    }

    // Rolling-window dedup (ported from src/transcript_fetcher.py:_parse_vtt).
    const merged = [];
    for (const seg of raw) {
      const prev = merged[merged.length - 1];
      if (prev && Math.abs(prev.start - seg.start) < 0.01) {
        if (seg.text.startsWith(prev.text) || prev.text.startsWith(seg.text)) {
          if (seg.text.length >= prev.text.length) merged[merged.length - 1] = seg;
          continue;
        }
      }
      merged.push(seg);
    }
    const out = [];
    for (const seg of merged) {
      const prev = out[out.length - 1];
      if (prev) {
        if (seg.text === prev.text || prev.text.includes(seg.text)) continue;
        if (prev.text && seg.text.startsWith(prev.text)) {
          out[out.length - 1] = seg;
          continue;
        }
      }
      out.push(seg);
    }
    // Contract shape: {t, text}.
    return out.map((s) => ({ t: +s.start.toFixed(2), text: s.text }));
  }
})();
