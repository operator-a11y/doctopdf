// Central place for the URLs the site links to, so the download/source buttons
// stay correct as releases are published.
export const site = {
  name: "DocToPDF",
  tagline: "Watch your Google Docs. Get the PDF, the diff, and the alert — automatically.",
  description:
    "A macOS menu-bar app that watches Google Docs, Sheets, Slides, Drive folders, and web pages — re-exports them on every change and tells you what changed. Classified, alerted, versioned, and queryable. AI runs locally by default.",
  // Public site URL — used for metadataBase (OG image / canonical).
  url: "https://doctopdf-pi.vercel.app",
  repo: "https://github.com/operator-a11y/doctopdf",
  // Direct download of the latest macOS build (the asset name is stable across
  // releases, so this always points at the newest one).
  download: "https://github.com/operator-a11y/doctopdf/releases/latest/download/DocToPDF-macos.zip",
  // The releases listing page (all versions + notes).
  releases: "https://github.com/operator-a11y/doctopdf/releases",
  readme: "https://github.com/operator-a11y/doctopdf#readme",
  // The click-by-click Google OAuth walkthrough (a page on this site).
  setup: "/setup",
} as const;
