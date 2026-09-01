import { ImageResponse } from "next/og";

export const runtime = "edge";
export const alt = "Anticlickbait - YouTube clickbait scores";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default async function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: "80px",
          background: "#000",
          color: "#ededed",
        }}
      >
        <div
          style={{
            fontSize: 96,
            fontWeight: 900,
            textTransform: "uppercase",
            letterSpacing: "-0.02em",
            lineHeight: 1,
          }}
        >
          ANTICLICKBAIT
        </div>
        <div
          style={{
            fontSize: 28,
            color: "#999",
            marginTop: 28,
            lineHeight: 1.5,
            fontWeight: 400,
          }}
        >
          Scores YouTube channels on whether they deliver what they promise.
        </div>
        <div
          style={{
            display: "flex",
            gap: 32,
            marginTop: 48,
          }}
        >
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              padding: "20px 28px",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: 4,
              background: "rgba(255,255,255,0.04)",
            }}
          >
            <span style={{ fontSize: 14, color: "#999", textTransform: "uppercase", letterSpacing: "0.08em" }}>Channels</span>
            <span style={{ fontSize: 36, fontWeight: 900, marginTop: 4 }}>150+</span>
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              padding: "20px 28px",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: 4,
              background: "rgba(255,255,255,0.04)",
            }}
          >
            <span style={{ fontSize: 14, color: "#999", textTransform: "uppercase", letterSpacing: "0.08em" }}>Videos evaluated</span>
            <span style={{ fontSize: 36, fontWeight: 900, marginTop: 4 }}>2,200+</span>
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              padding: "20px 28px",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: 4,
              background: "rgba(255,255,255,0.04)",
            }}
          >
            <span style={{ fontSize: 14, color: "#999", textTransform: "uppercase", letterSpacing: "0.08em" }}>Score range</span>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginTop: 4 }}>
              <span style={{ fontSize: 36, fontWeight: 900, color: "rgb(248,81,73)" }}>0</span>
              <span style={{ fontSize: 20, color: "#666" }}>-</span>
              <span style={{ fontSize: 36, fontWeight: 900, color: "rgb(63,185,80)" }}>100</span>
            </div>
          </div>
        </div>
      </div>
    ),
    { ...size }
  );
}
