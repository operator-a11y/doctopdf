import Link from "next/link";
import {
  ArrowRight,
  Bell,
  Cpu,
  Database,
  Download,
  FileStack,
  GitBranch,
  Globe,
  Rocket,
  Sparkles,
  Users,
} from "lucide-react";
import { site } from "@/lib/site";

const features = [
  {
    icon: FileStack,
    title: "Multi-format export",
    desc: "PDF, DOCX, XLSX, PPTX, Markdown and more — automatically filtered to each file's type.",
  },
  {
    icon: Sparkles,
    title: "Change intelligence",
    desc: "Every edit is classified cosmetic, substantive, or material, with a severity threshold that cuts the noise.",
  },
  {
    icon: Cpu,
    title: "Local AI summaries",
    desc: "A local model (Ollama) writes a one-line summary of each change. No cloud key — nothing leaves your Mac.",
  },
  {
    icon: Bell,
    title: "Alerts & digests",
    desc: "Route changes to Slack, Discord, webhooks, or email — plus scheduled daily or weekly rollups.",
  },
  {
    icon: GitBranch,
    title: "Version history",
    desc: "Commit every revision to a git repo with real text diffs, not just opaque binary PDFs.",
  },
  {
    icon: Globe,
    title: "Web page monitoring",
    desc: "Track competitor pricing, ToS, and changelogs — fetched, denoised, and diffed just like a doc.",
  },
  {
    icon: Database,
    title: "Knowledge base",
    desc: "A local vector store plus an MCP server, so your AI agents always have the current content.",
  },
  {
    icon: Users,
    title: "Multiple accounts",
    desc: "Watch sources across your personal and work Google accounts at the same time.",
  },
  {
    icon: Rocket,
    title: "Publishing pipeline",
    desc: "Turn a Doc into a live website, Markdown, or a branded PDF on every change.",
  },
];

const steps = [
  {
    n: "1",
    title: "Get DocToPDF",
    desc: "Download the app, or clone the repo and run it from source on your Mac.",
  },
  {
    n: "2",
    title: "Connect Google",
    desc: "A one-time browser authorization with read-only Drive access. Add as many accounts as you like.",
  },
  {
    n: "3",
    title: "Add what to watch",
    desc: "Paste a Doc, Sheet, Slides, Drive folder, or any web page URL. Exports and alerts start immediately.",
  },
];

function GithubIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden className={className}>
      <path d="M12 .5C5.73.5.5 5.73.5 12a11.5 11.5 0 0 0 7.86 10.92c.58.11.79-.25.79-.56 0-.27-.01-1-.02-1.96-3.2.7-3.88-1.54-3.88-1.54-.52-1.33-1.28-1.68-1.28-1.68-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.55-.29-5.23-1.28-5.23-5.7 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.8 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.43-2.69 5.41-5.25 5.69.41.36.78 1.07.78 2.16 0 1.56-.01 2.82-.01 3.2 0 .31.21.68.8.56A11.5 11.5 0 0 0 23.5 12C23.5 5.73 18.27.5 12 .5Z" />
    </svg>
  );
}

function Mark() {
  return (
    <span className="flex items-center gap-2 font-semibold text-white">
      <span className="grid h-7 w-7 place-items-center rounded-lg bg-indigo-500/15 text-indigo-400 ring-1 ring-indigo-500/30">
        <FileStack className="h-4 w-4" />
      </span>
      {site.name}
    </span>
  );
}

export default function Home() {
  return (
    <>
      {/* Header */}
      <header className="sticky top-0 z-50 border-b border-white/5 bg-neutral-950/80 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
          <Mark />
          <nav className="hidden items-center gap-8 text-sm text-neutral-400 sm:flex">
            <a href="#features" className="transition-colors hover:text-white">Features</a>
            <a href="#how" className="transition-colors hover:text-white">How it works</a>
            <a href="#download" className="transition-colors hover:text-white">Download</a>
            <a href={site.repo} className="flex items-center gap-1.5 transition-colors hover:text-white">
              <GithubIcon className="h-4 w-4" /> GitHub
            </a>
          </nav>
          <a
            href="#download"
            className="rounded-full bg-white px-4 py-1.5 text-sm font-medium text-neutral-950 transition-opacity hover:opacity-90"
          >
            Download
          </a>
        </div>
      </header>

      <main className="flex-1">
        {/* Hero */}
        <section className="relative overflow-hidden">
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 -top-40 h-[500px] bg-[radial-gradient(closest-side,rgba(99,102,241,0.18),transparent)]"
          />
          <div className="mx-auto max-w-6xl px-6 pt-20 pb-16 sm:pt-28 sm:pb-24">
            <div className="mx-auto max-w-3xl text-center">
              <span className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs font-medium text-neutral-300">
                <span className="h-1.5 w-1.5 rounded-full bg-indigo-400" />
                macOS menu-bar app · local-first
              </span>
              <h1 className="mt-6 text-balance text-4xl font-bold tracking-tight text-white sm:text-6xl">
                Know the moment your docs change.
              </h1>
              <p className="mx-auto mt-6 max-w-2xl text-pretty text-lg leading-relaxed text-neutral-400">
                {site.name} watches your Google Docs, Sheets, Slides, Drive folders — even
                web pages — and re-exports them automatically when they change. Every edit is
                summarized, classified, alerted, versioned, and made searchable.{" "}
                <span className="text-neutral-200">
                  AI runs locally by default — nothing leaves your Mac unless you opt into a
                  cloud embedder.
                </span>
              </p>
              <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
                <a
                  href={site.download}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-full bg-indigo-500 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-indigo-500/20 transition-colors hover:bg-indigo-400 sm:w-auto"
                >
                  <Download className="h-4 w-4" /> Download for macOS
                </a>
                <a
                  href={site.repo}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-full border border-white/15 bg-white/5 px-6 py-3 text-sm font-semibold text-white transition-colors hover:bg-white/10 sm:w-auto"
                >
                  <GithubIcon className="h-4 w-4" /> View source
                </a>
              </div>
              <p className="mt-5 text-xs text-neutral-400">
                Free &amp; open source · No cloud key required · Works with private docs
              </p>
            </div>

            {/* Menu-bar preview (decorative — described for screen readers) */}
            <div className="mx-auto mt-16 max-w-md">
              <span className="sr-only">
                Example of the DocToPDF menu-bar dropdown: watching 5 items, the last export
                time, a material pricing change summary, Export now, an Accounts submenu, and
                Change history.
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
                <pre className="overflow-x-auto px-5 py-4 font-mono text-[13px] leading-relaxed text-neutral-300">
{`DocToPDF
 ├─ Watching: 5 items
 ├─ Last export: 14:32:07
 ├─ 💡 Pricing: [material] $20 → $35/mo
 ├─ Export now
 ├─ Accounts ▸   personal ✓ · work
 ├─ Change history…
 └─ Quit`}
                </pre>
              </div>
            </div>
          </div>
        </section>

        {/* Features */}
        <section id="features" className="border-t border-white/5 py-20 sm:py-28">
          <div className="mx-auto max-w-6xl px-6">
            <div className="max-w-2xl">
              <h2 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">
                Not just an exporter — a monitoring tool.
              </h2>
              <p className="mt-4 text-lg text-neutral-400">
                Every change flows through one pipeline: export, classify, filter, alert,
                version, index, and publish.
              </p>
            </div>
            <div className="mt-14 grid grid-cols-1 gap-px overflow-hidden rounded-2xl border border-white/10 bg-white/10 sm:grid-cols-2 lg:grid-cols-3">
              {features.map((f) => (
                <div key={f.title} className="bg-neutral-950 p-7 transition-colors hover:bg-neutral-900/60">
                  <span className="grid h-10 w-10 place-items-center rounded-lg bg-indigo-500/10 text-indigo-400 ring-1 ring-indigo-500/20">
                    <f.icon className="h-5 w-5" />
                  </span>
                  <h3 className="mt-5 font-semibold text-white">{f.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-neutral-400">{f.desc}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* How it works */}
        <section id="how" className="border-t border-white/5 py-20 sm:py-28">
          <div className="mx-auto max-w-6xl px-6">
            <h2 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">
              Up and running in three steps.
            </h2>
            <div className="mt-14 grid grid-cols-1 gap-8 md:grid-cols-3">
              {steps.map((s) => (
                <div key={s.n}>
                  <span className="grid h-9 w-9 place-items-center rounded-full border border-indigo-500/30 bg-indigo-500/10 font-mono text-sm font-semibold text-indigo-400">
                    {s.n}
                  </span>
                  <h3 className="mt-5 text-lg font-semibold text-white">{s.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-neutral-400">{s.desc}</p>
                </div>
              ))}
            </div>

            {/* Honest requirements callout */}
            <div className="mt-14 rounded-2xl border border-amber-500/20 bg-amber-500/5 p-6">
              <h3 className="font-semibold text-amber-200">Before you start</h3>
              <ul className="mt-3 space-y-2 text-sm text-neutral-300">
                <li>· Runs on <span className="text-white">macOS</span> — it&apos;s a native menu-bar app.</li>
                <li>
                  · Needs your own <span className="text-white">Google Cloud OAuth client</span>{" "}
                  (<code className="rounded bg-white/10 px-1 py-0.5 font-mono text-xs">client_secret.json</code>) —
                  a one-time setup covered step by step in the{" "}
                  <Link href={site.setup} className="text-amber-200 underline underline-offset-2 hover:text-amber-100">setup guide</Link>.
                </li>
                <li>· Optional: <span className="text-white">Ollama</span> for local AI summaries and the knowledge base.</li>
              </ul>
            </div>
          </div>
        </section>

        {/* Download */}
        <section id="download" className="border-t border-white/5 py-20 sm:py-28">
          <div className="mx-auto max-w-6xl px-6">
            <div className="max-w-2xl">
              <h2 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">Get {site.name}.</h2>
              <p className="mt-4 text-lg text-neutral-400">
                Grab the macOS app, or run it from source — both are free and open.
              </p>
            </div>
            <div className="mt-12 grid grid-cols-1 gap-6 lg:grid-cols-2">
              {/* App download */}
              <div className="flex flex-col rounded-2xl border border-white/10 bg-neutral-900/40 p-8">
                <h3 className="text-lg font-semibold text-white">Download the app</h3>
                <p className="mt-2 flex-1 text-sm leading-relaxed text-neutral-400">
                  Grab the latest macOS build — <span className="text-neutral-200">universal2,
                  native on Apple silicon &amp; Intel</span>. It&apos;s an unsigned preview, so on
                  first open right-click the app → Open. You also add your own Google OAuth
                  client once — see the{" "}
                  <Link href={site.setup} className="text-neutral-200 underline underline-offset-2 hover:text-white">setup guide</Link>.
                </p>
                <a
                  href={site.download}
                  className="mt-6 inline-flex items-center justify-center gap-2 rounded-full bg-indigo-500 px-6 py-3 text-sm font-semibold text-white transition-colors hover:bg-indigo-400"
                >
                  <Download className="h-4 w-4" /> Download for macOS
                </a>
                <p className="mt-3 text-xs text-neutral-400">
                  universal2 · macOS 11+ ·{" "}
                  <a href={site.releases} className="underline underline-offset-2 hover:text-white">all releases</a>
                </p>
              </div>

              {/* From source */}
              <div className="flex flex-col rounded-2xl border border-white/10 bg-neutral-900/40 p-8">
                <h3 className="text-lg font-semibold text-white">Run from source</h3>
                <p className="mt-2 text-sm leading-relaxed text-neutral-400">
                  Prefer to build it yourself? Clone and run with Python 3.11+.
                </p>
                <pre className="mt-4 flex-1 overflow-x-auto rounded-lg border border-white/10 bg-black/40 px-4 py-3 font-mono text-xs leading-relaxed text-neutral-300">
{`git clone ${site.repo}.git
cd doctopdf
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m doctopdf`}
                </pre>
                <a
                  href={site.readme}
                  className="mt-6 inline-flex items-center justify-center gap-2 rounded-full border border-white/15 bg-white/5 px-6 py-3 text-sm font-semibold text-white transition-colors hover:bg-white/10"
                >
                  Open the README <ArrowRight className="h-4 w-4" />
                </a>
              </div>
            </div>
          </div>
        </section>
      </main>

      {/* Footer */}
      <footer className="border-t border-white/5 py-12">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-6 px-6 sm:flex-row">
          <div>
            <Mark />
            <p className="mt-2 max-w-sm text-sm text-neutral-400">{site.tagline}</p>
          </div>
          <div className="flex items-center gap-6 text-sm text-neutral-400">
            <a href={site.repo} className="transition-colors hover:text-white">GitHub</a>
            <a href={site.readme} className="transition-colors hover:text-white">Docs</a>
            <a href={site.releases} className="transition-colors hover:text-white">Releases</a>
            <Link href="/setup" className="transition-colors hover:text-white">Setup</Link>
            <Link href="/privacy" className="transition-colors hover:text-white">Privacy</Link>
          </div>
        </div>
        <p className="mt-8 text-center text-xs text-neutral-400">
          © 2026 {site.name} · Open source · Built with Next.js
        </p>
      </footer>
    </>
  );
}
