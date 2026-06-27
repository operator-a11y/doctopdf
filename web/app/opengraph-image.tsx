import { ImageResponse } from "next/og";
import { site } from "@/lib/site";

// Next.js auto-wires this into the page's OG/Twitter image metadata.
export const alt = `${site.name} — know the moment your docs change`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          height: "100%",
          width: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: 88,
          background: "#0a0a0a",
          color: "white",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 14, color: "#a5b4fc", fontSize: 28 }}>
          <div style={{ width: 14, height: 14, borderRadius: 99, background: "#818cf8" }} />
          macOS menu-bar app · local-first
        </div>
        <div
          style={{
            fontSize: 88,
            fontWeight: 700,
            marginTop: 28,
            lineHeight: 1.05,
            letterSpacing: -2,
            whiteSpace: "pre",
          }}
        >
          {"Know the moment\nyour docs change."}
        </div>
        <div style={{ fontSize: 32, color: "#a3a3a3", marginTop: 30, maxWidth: 940 }}>
          {site.tagline}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            marginTop: 52,
            fontSize: 32,
            fontWeight: 600,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              width: 44,
              height: 44,
              borderRadius: 12,
              background: "rgba(129,140,248,0.18)",
            }}
          >
            <div style={{ width: 18, height: 18, borderRadius: 5, background: "#a5b4fc" }} />
          </div>
          DocToPDF
        </div>
      </div>
    ),
    { ...size }
  );
}
