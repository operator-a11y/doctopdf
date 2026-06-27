import type { Metadata } from "next";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: `Set up Google access — ${site.name}`,
  description: `One-time setup: create your own Google OAuth client so ${site.name} can read your Drive.`,
};

const steps = [
  {
    title: "Create a Google Cloud project",
    body: (
      <>
        Go to the{" "}
        <a href="https://console.cloud.google.com/projectcreate" className="lnk">
          Google Cloud Console
        </a>{" "}
        and create a new project (any name).
      </>
    ),
  },
  {
    title: "Enable the Google Drive API",
    body: (
      <>
        <span className="text-neutral-200">APIs &amp; Services → Library</span>, search{" "}
        <span className="text-neutral-200">&ldquo;Google Drive API&rdquo;</span>, and click{" "}
        <span className="text-neutral-200">Enable</span>.
      </>
    ),
  },
  {
    title: "Configure the OAuth consent screen",
    body: (
      <>
        <span className="text-neutral-200">APIs &amp; Services → OAuth consent screen</span>.
        Choose user type <span className="text-neutral-200">External</span>; leave publishing
        status on <span className="text-neutral-200">Testing</span>. Under{" "}
        <span className="text-neutral-200">Test users</span>, add every Google account you plan
        to watch (otherwise sign-in is blocked).
      </>
    ),
  },
  {
    title: "Create the OAuth client",
    body: (
      <>
        <span className="text-neutral-200">APIs &amp; Services → Credentials → Create
        Credentials → OAuth client ID</span>. Application type:{" "}
        <span className="text-neutral-200">Desktop app</span>. Click{" "}
        <span className="text-neutral-200">Download JSON</span> and rename the file to{" "}
        <code className="code">client_secret.json</code>.
      </>
    ),
  },
  {
    title: "Drop client_secret.json where the app looks",
    body: (
      <>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          <li>
            <span className="text-neutral-200">Downloaded app:</span> put it in{" "}
            <code className="code">~/Library/Application&nbsp;Support/DocToPDF/</code>. In the
            app&apos;s menu, <span className="text-neutral-200">Set up Google access… →
            Reveal Folder</span> opens exactly this folder.
          </li>
          <li>
            <span className="text-neutral-200">Running from source:</span> put it in the
            project root (next to the README).
          </li>
        </ul>
      </>
    ),
  },
  {
    title: "Launch & authorize",
    body: (
      <>
        Open DocToPDF. It opens your browser to authorize{" "}
        <span className="text-neutral-200">read-only Drive</span> access — approve it (click
        through the &ldquo;unverified app&rdquo; notice, since it&apos;s your own test app).
        You&apos;re done; it won&apos;t ask again.
      </>
    ),
  },
];

export default function Setup() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-20">
      <a href="/" className="text-sm text-indigo-400 hover:text-indigo-300">← {site.name}</a>
      <h1 className="mt-6 text-3xl font-bold tracking-tight text-white sm:text-4xl">
        Set up Google access
      </h1>
      <p className="mt-4 text-[15px] leading-relaxed text-neutral-400">
        {site.name} uses <span className="text-neutral-200">your own</span> Google credentials
        — nothing is shared with us, and your data stays on your Mac. This is a one-time setup
        (~5 minutes).
      </p>

      <ol className="mt-10 space-y-8">
        {steps.map((s, i) => (
          <li key={s.title} className="flex gap-4">
            <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full border border-indigo-500/30 bg-indigo-500/10 font-mono text-sm font-semibold text-indigo-400">
              {i + 1}
            </span>
            <div>
              <h2 className="font-semibold text-white">{s.title}</h2>
              <div className="mt-1 text-[15px] leading-relaxed text-neutral-400">{s.body}</div>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-12 rounded-2xl border border-amber-500/20 bg-amber-500/5 p-6 text-sm text-neutral-300">
        <p className="font-semibold text-amber-200">Good to know</p>
        <ul className="mt-3 list-disc space-y-2 pl-5 text-neutral-400">
          <li>
            Because the app stays in <span className="text-neutral-200">Testing</span>, add each
            account under Test users, and Google expires those authorizations after ~7 days —
            DocToPDF silently re-authorizes when that happens.
          </li>
          <li>
            Need the full install + every feature?{" "}
            <a href={site.readme} className="lnk">See the README.</a>
          </li>
        </ul>
      </div>
    </main>
  );
}
