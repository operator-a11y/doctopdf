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
  // The "Download for macOS" button points here. Publish a signed .dmg to GitHub
  // Releases and this becomes a true one-click download.
  download: "https://github.com/operator-a11y/doctopdf/releases/latest",
  readme: "https://github.com/operator-a11y/doctopdf#readme",
  setup: "https://github.com/operator-a11y/doctopdf#one-time-google-cloud-setup-you-must-do-this-once",
} as const;
