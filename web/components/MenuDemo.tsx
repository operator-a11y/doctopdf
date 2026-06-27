"use client";

import { useEffect, useState } from "react";

// An illustrative animation of DocToPDF's real menu-bar dropdown cycling through
// its states — not a screen recording. Each scene is the menu's actual content.
const SCENES = [
  {
    bar: "DocToPDF",
    lines: [
      " ├─ Watching: 5 items",
      " ├─ Last export: 14:32:01",
      " ├─ Export now",
      " ├─ Accounts ▸   personal ✓ · work",
      " └─ Quit",
    ],
  },
  {
    bar: "🔄 DocToPDF",
    lines: [
      " ├─ Exporting… (Pricing)",
      " ├─ Last export: 14:32:01",
      " ├─ Export now",
      " ├─ Accounts ▸   personal ✓ · work",
      " └─ Quit",
    ],
  },
  {
    bar: "DocToPDF",
    lines: [
      " ├─ Watching: 5 items",
      " ├─ Last export: 14:32:07",
      " ├─ 💡 Pricing: [material] $20 → $35/mo",
      " ├─ Alerted Slack · material",
      " └─ Quit",
    ],
  },
];

export default function MenuDemo() {
  const [i, setI] = useState(0);

  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const t = setInterval(() => setI((n) => (n + 1) % SCENES.length), 2200);
    return () => clearInterval(t);
  }, []);

  const scene = SCENES[i];

  return (
    <>
      <span className="sr-only">
        Animated example of the DocToPDF menu-bar dropdown cycling through watching
        five items, exporting a doc, and a detected material pricing change that
        was alerted to Slack.
      </span>
      <div
        aria-hidden
        className="overflow-hidden rounded-xl border border-white/10 bg-neutral-900/80 shadow-2xl shadow-black/40"
      >
        <div className="flex items-center gap-1.5 border-b border-white/5 px-4 py-2.5">
          <span className="h-2.5 w-2.5 rounded-full bg-red-400/80" />
          <span className="h-2.5 w-2.5 rounded-full bg-yellow-400/80" />
          <span className="h-2.5 w-2.5 rounded-full bg-green-400/80" />
        </div>
        <pre
          key={i}
          className="demo-fade min-h-[148px] overflow-x-auto px-5 py-4 font-mono text-[13px] leading-relaxed text-neutral-300"
        >
{`${scene.bar}\n${scene.lines.join("\n")}`}
        </pre>
      </div>
    </>
  );
}
