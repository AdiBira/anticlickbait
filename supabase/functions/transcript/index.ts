// Supabase Edge Function: fetch YouTube transcript via Deno Deploy's IP
// Temporary — delete after backfill is complete.
// Usage: GET /transcript?v=VIDEO_ID

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

function parseSrv3(
  xml: string
): Array<{ start: number; text: string; duration: number }> {
  const segments: Array<{ start: number; text: string; duration: number }> = [];
  const regex = /<text start="([^"]+)" dur="([^"]+)"[^>]*>([\s\S]*?)<\/text>/g;
  let match;
  while ((match = regex.exec(xml)) !== null) {
    segments.push({
      start: parseFloat(match[1]),
      duration: parseFloat(match[2]),
      text: match[3]
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/<[^>]+>/g, "")
        .trim(),
    });
  }
  return segments.filter((s) => s.text);
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  const videoId = url.searchParams.get("v");
  const debug = url.searchParams.get("debug") === "1";

  if (!videoId) {
    return new Response(JSON.stringify({ error: "missing ?v= param" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const debugInfo: Record<string, unknown> = {};

  try {
    // Strategy: Use InnerTube WEB client which embeds captions in the
    // player response without needing a separate timedtext fetch.
    // Try multiple client variants.

    const clients = [
      { clientName: "WEB", clientVersion: "2.20250301.00.00" },
      { clientName: "MWEB", clientVersion: "2.20250301.00.00" },
      { clientName: "ANDROID", clientVersion: "20.10.38" },
      { clientName: "IOS", clientVersion: "20.10.38" },
      { clientName: "TV_EMBEDDED", clientVersion: "2.0" },
      { clientName: "MEDIA_CONNECT_FRONTEND", clientVersion: "0.1" },
    ];

    for (const client of clients) {
      try {
        const playerResp = await fetch(
          "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "User-Agent": UA,
              Origin: "https://www.youtube.com",
              Referer: `https://www.youtube.com/watch?v=${videoId}`,
            },
            body: JSON.stringify({
              context: { client },
              videoId,
            }),
          }
        );

        const data = await playerResp.json();
        const status = data?.playabilityStatus?.status;
        const tracks =
          data?.captions?.playerCaptionsTracklistRenderer?.captionTracks ?? [];

        if (debug) {
          debugInfo[client.clientName] = {
            status,
            reason: data?.playabilityStatus?.reason?.slice(0, 80),
            tracks: tracks.length,
            langs: tracks.map(
              (t: { languageCode: string }) => t.languageCode
            ),
          };
        }

        // Find English track
        for (const t of tracks) {
          if (
            t.languageCode === "en" ||
            t.languageCode?.startsWith("en-")
          ) {
            const captionUrl = t.baseUrl.replace("&fmt=srv3", "");
            const captionResp = await fetch(captionUrl);

            if (debug) {
              debugInfo[`${client.clientName}_fetch`] = captionResp.status;
            }

            if (captionResp.ok) {
              const xml = await captionResp.text();
              const segments = parseSrv3(xml);
              if (segments.length > 0) {
                return new Response(
                  JSON.stringify({
                    segments,
                    count: segments.length,
                    method: client.clientName,
                    ...(debug ? { debug: debugInfo } : {}),
                  }),
                  { headers: { "Content-Type": "application/json" } }
                );
              }
            }
            break;
          }
        }
      } catch (e) {
        if (debug) {
          debugInfo[`${client.clientName}_error`] = String(e).slice(0, 80);
        }
      }
    }

    // Fallback: try page HTML extraction
    const pageResp = await fetch(
      `https://www.youtube.com/watch?v=${videoId}`,
      {
        headers: {
          "User-Agent": UA,
          "Accept-Language": "en-US,en;q=0.9",
        },
      }
    );
    const html = await pageResp.text();
    const trackMatch = html.match(/"captionTracks":\s*(\[.*?\])/);

    if (trackMatch) {
      const pageTracks = JSON.parse(trackMatch[1]);
      for (const t of pageTracks) {
        if (t.languageCode === "en" || t.languageCode?.startsWith("en-")) {
          const captionResp = await fetch(
            t.baseUrl.replace("&fmt=srv3", "")
          );
          if (debug) {
            debugInfo.pageFetch = captionResp.status;
          }
          if (captionResp.ok) {
            const xml = await captionResp.text();
            const segments = parseSrv3(xml);
            if (segments.length > 0) {
              return new Response(
                JSON.stringify({
                  segments,
                  count: segments.length,
                  method: "page_html",
                  ...(debug ? { debug: debugInfo } : {}),
                }),
                { headers: { "Content-Type": "application/json" } }
              );
            }
          }
        }
      }
    }

    return new Response(
      JSON.stringify({
        error: "no english captions found or all fetches failed",
        ...(debug ? { debug: debugInfo } : {}),
      }),
      { status: 404, headers: { "Content-Type": "application/json" } }
    );
  } catch (e) {
    return new Response(
      JSON.stringify({ error: String(e).slice(0, 300) }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
});
