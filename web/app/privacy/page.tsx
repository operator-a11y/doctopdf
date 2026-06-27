import type { Metadata } from "next";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: `Privacy Policy — ${site.name}`,
  description: `How ${site.name} handles your data and Google account information.`,
};

export default function Privacy() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-20">
      <a href="/" className="text-sm text-indigo-400 hover:text-indigo-300">← {site.name}</a>
      <h1 className="mt-6 text-3xl font-bold tracking-tight text-white sm:text-4xl">
        Privacy Policy
      </h1>
      <p className="mt-3 text-sm text-neutral-400">Last updated: June 27, 2026</p>

      <div className="mt-10 space-y-8 text-[15px] leading-relaxed text-neutral-300">
        <section>
          <p>
            {site.name} is a <span className="text-white">local-first macOS application</span>{" "}
            that you run on your own Mac. It has no backend service operated by us — we
            do not host, collect, or have access to your files, your Google account, or
            your credentials. This policy explains what the app accesses on your device
            and when, if ever, data leaves your machine.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-white">Information the app accesses</h2>
          <ul className="mt-3 list-disc space-y-2 pl-5 text-neutral-400">
            <li>
              <span className="text-neutral-200">Google Drive (read-only).</span> With your
              authorization, the app uses the{" "}
              <code className="rounded bg-white/10 px-1 py-0.5 font-mono text-xs">drive.readonly</code>{" "}
              scope to read and export the specific Docs, Sheets, Slides, Drawings, and
              Drive folders <em>you choose to watch</em>, and to read their metadata
              (name, modified time, and last-modifying user) to detect changes.
            </li>
            <li>
              <span className="text-neutral-200">Your account identity.</span> The app reads
              your account&apos;s email and a stable account id (via Drive&apos;s
              <code className="rounded bg-white/10 px-1 py-0.5 font-mono text-xs"> about</code>{" "}
              endpoint) to label and tell apart multiple authorized accounts.
            </li>
            <li>
              <span className="text-neutral-200">Web pages you add.</span> Any public web
              page URL you choose to monitor is fetched directly by the app.
            </li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-white">Where your data is stored</h2>
          <p className="mt-3 text-neutral-400">
            Everything stays on your Mac: exported files are written to a folder you
            choose, version history to a local git repository you point to, the optional
            knowledge-base index to a local vector store, and OAuth tokens to your user
            Library folder (readable only by your account). AI change summaries are
            produced by a <span className="text-neutral-200">local model</span> by default —
            no document content is sent to any cloud AI service.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-white">When data leaves your machine</h2>
          <p className="mt-3 text-neutral-400">
            Only to destinations <span className="text-neutral-200">you explicitly
            configure</span>, and only then:
          </p>
          <ul className="mt-3 list-disc space-y-2 pl-5 text-neutral-400">
            <li>Slack / Discord / generic webhooks and SMTP email — if you set up change alerts.</li>
            <li>A git remote you specify — if you enable the publishing pipeline.</li>
            <li>
              A cloud embedding provider (e.g. OpenAI) — <em>only</em> if you opt out of the
              default local embedder for the knowledge base.
            </li>
          </ul>
          <p className="mt-3 text-neutral-400">
            If you configure none of these, no document content ever leaves your Mac.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-white">
            Google API Services — Limited Use
          </h2>
          <p className="mt-3 text-neutral-400">
            {site.name}&apos;s use and transfer of information received from Google APIs
            adheres to the{" "}
            <a
              href="https://developers.google.com/terms/api-services-user-data-policy"
              className="text-indigo-400 underline underline-offset-2 hover:text-indigo-300"
            >
              Google API Services User Data Policy
            </a>
            , including the Limited Use requirements. Specifically, Google user data is
            used only to provide and improve the app&apos;s features at your direction; it
            is <span className="text-neutral-200">never sold</span>, never transferred to
            others except as needed to provide a feature you enabled, never used for
            advertising, and not read by humans except as you direct or as required by law.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-white">Revoking access &amp; deleting data</h2>
          <p className="mt-3 text-neutral-400">
            Revoke the app&apos;s access at any time at{" "}
            <a
              href="https://myaccount.google.com/permissions"
              className="text-indigo-400 underline underline-offset-2 hover:text-indigo-300"
            >
              myaccount.google.com/permissions
            </a>
            , or remove an account from within the app (which deletes its stored token).
            Because all data lives on your device, deleting the exported files, the local
            stores, and the app removes it entirely.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-white">Analytics</h2>
          <p className="mt-3 text-neutral-400">
            The app contains no analytics, telemetry, or third-party tracking.
          </p>
        </section>

        <section>
          <h2 className="text-xl font-semibold text-white">Contact</h2>
          <p className="mt-3 text-neutral-400">
            Questions about this policy? Open an issue at{" "}
            <a
              href={`${site.repo}/issues`}
              className="text-indigo-400 underline underline-offset-2 hover:text-indigo-300"
            >
              the project&apos;s GitHub
            </a>
            .
          </p>
        </section>
      </div>
    </main>
  );
}
