# DocToPDF

A macOS **menu-bar app** that watches Google **Docs, Sheets, Slides** — whole
**Drive folders** — and **web pages** (competitor pricing, ToS, changelogs), and
tells you what changed. Google sources re-export to your Desktop on change; web
pages are fetched, denoised, and diffed. Every change flows through the same
intelligence layer: classified (cosmetic/substantive/material), severity-filtered,
routed to Slack/email/webhooks, digested, and committed to a git audit trail.

Also: **multi-format export** (pdf/docx/xlsx/pptx/md/…), **git version history**
with text diffs, **rolling versions**, a **post-export shell hook**, macOS
**notifications**, **launch-at-login**, a tabbed **Preferences** window, and —
the part that makes it a *monitoring* tool, not just an exporter — **AI change
intelligence**: a local model summarizes **and classifies** each change
(cosmetic / substantive / material), you set a **severity threshold** to cut
noise, and alerts go to **Slack / Discord / webhooks / email** plus **scheduled
digests**. All AI runs locally (no cloud key).

```
DocToPDF
 ├─ Watching: My Document          (or "Watching: 5 items")
 ├─ Last export: 14:32:07
 ├─ Watching ▸                     (list of watched docs/folders; click to remove)
 ├─ Export now
 ├─ Open Export
 ├─ Reveal in Finder
 ├─ Recent exports ▸
 ├─ Pause
 ├─ Add Doc or Folder…
 ├─ ✓ Launch at Login
 └─ Quit
```

---

## One-time Google Cloud setup (you must do this once)

This cannot be automated — Google requires you to create your own OAuth client.

1. **Create a project** in the [Google Cloud Console](https://console.cloud.google.com/).
2. **Enable the Google Drive API**: APIs & Services → Library → search
   "Google Drive API" → **Enable**.
3. **Configure the OAuth consent screen**: APIs & Services → OAuth consent screen.
   - User type: **External**.
   - Publishing status: **Testing** is fine.
   - Add **your own Google account** under **Test users** (otherwise auth is blocked).
4. **Create OAuth client credentials**: APIs & Services → Credentials →
   **Create Credentials → OAuth client ID** → Application type **Desktop app**.
   - Click **Download JSON** and save it as **`client_secret.json`** in the
     project root (the folder containing this README). It is gitignored.

On first launch the app opens your browser to authorize **read-only Drive**
access. After you approve, a `token.json` is cached locally (chmod 600,
gitignored) and refreshed automatically from then on — you won't be asked again.

---

## Install & run

Requires **Python 3.11+** and macOS.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # only needed to monitor JS-rendered web pages
python -m doctopdf.app
```

> If `python3` is an older version, use an explicit one, e.g.
> `python3.12 -m venv .venv`.

The app shows a **DocToPDF** label in the menu bar. On first run it prompts for
Google authorization. Then:

1. Click the label → **Add Doc or Folder…**
2. Paste a Doc/Sheet/Slides **URL** or **ID** — or a Drive **folder** URL/ID to
   mirror everything in it. Add as many as you like.
3. That's it — the Desktop exports start updating automatically.

---

## Menu actions

| Item | What it does |
| --- | --- |
| **Watching: …** / **Last export: …** | Live status (or an error message). |
| **Watching ▸** | Lists each watched doc/folder; click one to stop watching it. |
| **Export now** | Force an immediate re-export of everything. |
| **Open Export** | Open the most recent export in your default viewer. |
| **Reveal in Finder** | Select the most recent export in Finder. |
| **Recent exports ▸** | The last 15 exports; click any to open it. |
| **Pause / Resume** | Stop/start the watch loop. |
| **Add Doc or Folder…** | Add a doc, sheet, slides, folder, or web page to the watch list. |
| **Change history…** | Open a browsable audit log: what changed, when, by whom, how severe. |
| **Preferences…** | A window to adjust all settings (no JSON editing needed). |
| **Launch at Login** | Toggle auto-start at login (✓ when on). |
| **Quit** | Exit the app. |

The menu-bar label gets a leading glyph by state: **🔄** exporting,
**⏸** paused, **⚠️** error (otherwise plain).

---

## Configuration

Settings persist to `~/Library/Application Support/DocToPDF/config.json`:

```json
{
  "doc_id": "1AbC...xyz",
  "output_dir": "~/Desktop",
  "poll_interval": 10,
  "timestamped": false
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `watch` | `[]` | Watch list — managed via the menu. Each entry is `{"id": <file/folder id>, "output_dir"?, "formats"?}`; per-entry keys override the globals. |
| `output_dir` | `~/Desktop` | Where exported files are written. |
| `poll_interval` | `10` | Seconds between change checks (min 3). |
| `timestamped` | `false` | If `true`, write `<Name> <timestamp>.<ext>` (keeps every version) instead of overwriting. |
| `formats` | `["pdf"]` | Formats to export each change. **Filtered per file type** (see below). Docs: `pdf, docx, odt, rtf, txt, html, md, epub`; Sheets: `pdf, xlsx, ods, csv, tsv`; Slides: `pdf, pptx, odp, txt`; Drawings: `pdf, png, jpg, svg`. |
| `keep_versions` | `0` | Rolling history: if `> 0`, write timestamped files and keep only the newest N **per format**. |
| `git_repo` | `null` | Path to a git repo. On each change the exports are committed there (full version history). |
| `git_snapshot_text` | `true` | When `git_repo` is set, also commit a text snapshot (md/csv/…) so history has real diffs. |
| `post_export_cmd` | `null` | Shell command run after each export (see below). |
| `notify` | `false` | Post a macOS notification on each export. |
| `ai_summary` | `false` | On each change, summarize **and classify** the edit with a **local** model (see below). |
| `ollama_url` | `http://localhost:11434` | Local Ollama server URL. |
| `ollama_model` | `"llama3"` | Local model to use for summaries/classification. |
| `min_severity` | `"cosmetic"` | Alert threshold: `cosmetic` < `substantive` < `material`. Raise it to cut noise. |
| `webhook_urls` | `[]` | Slack / Discord / generic webhook URLs to alert on passing changes. |
| `email_to` / `email_from` / `smtp_*` | `null` | Email alerts via SMTP (host, port, user, pass). |
| `digest` | `"off"` | Ranked rollup: `off` / `daily` / `weekly`. |
| `digest_hour` | `9` | Local hour (0–23) to send the digest. |

> `doc_id` (a legacy single-doc id) is auto-migrated into `watch` on first launch.

### Watch many docs, folders, and Sheets/Slides

Add docs/sheets/slides/folders via **Add Doc or Folder…** — or edit `watch`
directly. A folder mirrors every exportable Google file **directly inside it**
(non-recursive; subfolders aren't descended). `formats` may list formats for
several types at once; each file keeps only the ones valid for its type, so one
list "just works":

```jsonc
{
  "watch": [
    { "id": "1AbC...doc" },
    { "id": "1XyZ...folder", "output_dir": "~/Desktop/Reports" },
    { "id": "1Sh3...sheet", "formats": ["pdf", "xlsx"] }
  ],
  "formats": ["pdf", "docx", "xlsx", "pptx"]  // doc→pdf+docx, sheet→pdf+xlsx, slides→pdf+pptx
}
```

Most of these can be set from the **Preferences…** menu item (a window) — no
JSON editing needed; changes apply live. Editing the file directly also works
(relaunch to pick up changes).

### Export more

```jsonc
{
  "formats": ["pdf", "docx", "md"],   // export several formats at once
  "keep_versions": 10                 // keep the last 10 timestamped copies per format
}
```

### Version history (great for resume iteration)

Point `git_repo` at a folder and every change becomes a git commit with a
timestamp — a full, recoverable history. Because a markdown snapshot is committed
alongside, you get **real diffs** (PDFs don't diff, text does):

```jsonc
{ "formats": ["pdf"], "git_repo": "~/Documents/DocToPDF-history" }
```

```bash
cd ~/Documents/DocToPDF-history
git log --oneline           # every saved revision
git diff HEAD~5 -- "*.md"   # what changed across the last 5 saves
```

### Post-export hook (turn it into a platform)

`post_export_cmd` runs after each export. `$1` and `$DOCTOPDF_PRIMARY` are the
primary file path; `$DOCTOPDF_FILES` lists all written files (newline-separated);
`$DOCTOPDF_DOC_NAME` is the doc name.

```jsonc
{ "post_export_cmd": "lpr \"$1\"" }                                 // auto-print
{ "post_export_cmd": "curl -F file=@\"$1\" https://example.com/up" } // upload/webhook
{ "post_export_cmd": "cp \"$1\" ~/Dropbox/" }                       // sync elsewhere
```

### AI change summaries (local model — no cloud key)

With `"ai_summary": true`, each change is diffed and summarized by a **local
model** and posted as a notification — e.g. *"Updated job title from engineer to
senior engineer."* No data leaves your machine; nothing is sent to any cloud API.

Requires [Ollama](https://ollama.com) running locally with a model pulled:

```bash
brew install ollama && ollama serve      # if not already running
ollama pull llama3                        # or any model; set "ollama_model"
```

```jsonc
{ "ai_summary": true, "ollama_model": "llama3" }
```

If no local model is reachable, summaries are silently skipped — watching is
never affected. Runs asynchronously, so a slow model never delays exports.

### Change intelligence — classify, filter, alert, digest

This is what turns DocToPDF from "exports my docs" into "tells me what changed."
With `ai_summary` on, each change is also **classified**:

- **Severity** — `cosmetic` (formatting/typos), `substantive` (real content), or
  `material` (affects meaning, numbers, money, dates, obligations).
- **Category** — e.g. *pricing, legal, dates, wording, structure*.

Then:

- **Severity filtering** (`min_severity`) — only alert at/above a level. Set it
  to `material` and you only hear about changes that actually matter.
- **Alert destinations** — passing changes are pushed to Slack/Discord/generic
  **webhooks** and **email** (SMTP), not just a local notification:
  ```jsonc
  { "ai_summary": true, "min_severity": "substantive",
    "webhook_urls": ["https://hooks.slack.com/services/…"] }
  ```
- **Scheduled digests** (`digest`: `daily`/`weekly`) — a rollup of everything
  that changed across all watched sources, **ranked by severity**, sent at
  `digest_hour`, alongside (or instead of) real-time alerts.

Everything's configurable from the **Change Alerts** tab in Preferences. Every
change is recorded to `~/Library/Application Support/DocToPDF/events.json`, and
**Change history…** opens a browsable audit dashboard of it — what changed, when,
by whom (Drive's last editor), and how severe — filterable and sortable.

### Monitor web pages

Watch any web page the same way — paste a URL into **Add Doc or Folder…**, or add
a `web` entry to the watch list. The page is fetched, **denoised** (boilerplate /
nav / ads / volatile tokens stripped via `trafilatura` or a CSS `selector`), and
diffed; a real content change flows into the *exact same* pipeline as a Doc change
(classify → severity → alert → digest → git).

```jsonc
{
  "kind": "web",
  "url": "https://competitor.com/pricing",
  "name": "Competitor pricing",
  "render": "static",            // static | browser  (browser = Playwright, for JS pages)
  "selector": "#pricing-table",  // optional CSS scope — monitor just this region
  "mode": "text",                // text (robust) | html (catch structural changes)
  "poll_seconds": 600,           // web polls far slower than docs — be polite
  "severity_min": "substantive", // per-target severity threshold (optional)
  "ignore": "Updated \\d{4}-\\d{2}"  // optional regex — drop matching lines (volatile bits)
}
```

- **JS pages:** set `"render": "browser"` (needs `playwright install chromium`).
- **Politeness/safety:** web targets poll on their own higher interval (default
  10 min, staggered), with a descriptive User-Agent; on `403/429` they **back off**
  (doubling up to 1 h) and show the error — never tight-looping a blocked site.
- **No phantom alerts:** if a `selector` stops matching (site redesign), it
  **warns and skips** instead of reporting a mass deletion; rotating ads /
  timestamps are denoised away so they don't trigger alerts.
- **Privacy:** page content is public, and the local-first promise holds — it's
  summarized locally via Ollama; only the alert/metadata leaves your machine if
  you route to Slack/email.

---

### Knowledge base — RAG vector sync + MCP (always-current context for agents)

Every watched source (Doc / Sheet / Slides / Drive folder / web page) is chunked,
embedded, and kept current in a **local vector store**, so an LLM or agent always
has the *present* content. Because DocToPDF already detects changes, re-embedding
is **incremental** — a one-line edit re-embeds *one chunk*, not the whole document
(chunks are reconciled by content hash). Removing a target (or a folder child)
deletes its chunks.

```jsonc
"rag": {
  "enabled": true,
  "store_path": "~/Documents/DocExports/.vectorstore",   // local; gitignored
  "embedder": { "provider": "ollama", "model": "nomic-embed-text", "url": "http://localhost:11434" },
  "chunk": { "size": 1000, "overlap": 150 },
  "mcp": { "enabled": true }
}
```

Per-target opt-out: add `"rag": false` to any watch entry to watch it for alerts
but keep it out of the knowledge base.

**Setup (local embeddings):**

```bash
ollama pull nomic-embed-text     # the default local embedder
```

**Query it from the CLI** (top-k chunks with source + freshness):

```bash
python -m doctopdf query "what is the current Pro price?" -k 5
```

**Use it from an agent (MCP server).** It exposes one read-only tool,
`search_knowledge(query, k=5)`, returning current chunks with a citation (name,
link) and `updated_at` so the agent can say *"from <name>, updated <date>: …"*.
Register it with an MCP client (Claude Desktop / Claude Code):

```jsonc
{
  "mcpServers": {
    "doctopdf": {
      "command": "/absolute/path/to/doctopdf/.venv/bin/python",
      "args": ["-m", "doctopdf", "mcp"]
    }
  }
}
```

- **Always current:** the store tracks live content — on restart every target is
  re-baselined (and hash-reconciled), so the index self-heals; a sync that failed
  while the embedder was down is recovered then too.
- **Graceful + additive:** if Ollama is down or the model is missing, syncs queue
  and the menu shows it — exports and alerts are never blocked. The menu shows the
  indexed-chunk count, last sync, and a **Rebuild index** action.
- **Changing the embedder model** changes the vector dimension, which would corrupt
  search — DocToPDF refuses to mix and asks you to rebuild:
  ```bash
  python -m doctopdf rag reindex   # then the app re-embeds on its next cycle
  ```
- **Privacy:** with the default Ollama embedder + local Chroma store, *content
  never leaves the machine*. Selecting a cloud embedder
  (`"provider": "openai"`, `"text-embedding-3-small"`) is the only thing that
  sends document text off-box; it's opt-in.

---

### Publishing pipeline — Doc → live site / Markdown / branded PDF

Bind a watched source to a **publish target** and DocToPDF re-publishes its
Markdown on every *stable* change (the same debounced change event the alerts
use — never a mid-keystroke draft). Three destination types:

- `git_markdown` — write the raw Markdown to a repo and push.
- `git_pages` — render Markdown → sanitized, themed HTML and push to a dedicated
  branch, so GitHub Pages / Netlify / Vercel serves a live, auto-updating site
  (the "Google Docs as CMS" wedge).
- `pdf_template` — render to a branded HTML/CSS template, then a PDF (via the
  Playwright Chromium already used for web monitoring).

```jsonc
"publish": [
  {
    "source_id": "<watched target id>",   // whose content to publish
    "type": "git_pages",                  // git_markdown | git_pages | pdf_template
    "repo": "git@github.com:user/site.git",
    "branch": "gh-pages",                 // a dedicated, app-owned branch
    "path": "index.html",                 // or docs/page.md for git_markdown
    "template": "default",                // built-in theme, or a path to a custom HTML file
    "approval": "manual",                 // manual = hold for Approve | auto = on stable change
    "site_url": "https://user.github.io/site"
  }
]
```

A custom template is any HTML file using `{{ title }}` and `{{ content }}`
markers (no template engine needed).

**Git auth is yours.** The app shells out to `git` and relies on **your existing
SSH key or credential helper** for the target repo — it never stores tokens. Make
sure `git push` to the repo works from your shell first. Auth/conflict/offline
failures surface in the menu (⚠️) and retry with backoff; nothing is silently
dropped.

**Safe git.** DocToPDF maintains its own working copy under app data and only
ever writes the **dedicated branch** you name — never your `main`. It pull-rebases
before pushing, and if the branch moved under it, it rebases its single
regenerated commit onto the new tip and pushes normally (no force) — so a
concurrent push to that branch is preserved, not clobbered. Point `branch` at a
branch you've reserved for publishing (e.g. `gh-pages`).

**Approval.** `auto` publishes on every stable change. `manual` holds the change
as **pending** and notifies *"Site has pending changes — review & publish"*;
nothing goes live until you click the target under **Publishing ▸** (or use
**Publish now**). **Recommended: `manual` for any public site.** Default is `auto`.

**Status.** The **Publishing ▸** submenu shows each target's state (published +
time / pending / error) — click to approve a pending one, open the live site, or
retry an error. **Publish now** publishes the current snapshot of every target.

**Images are a known limitation.** Google Docs image data isn't exported to the
publish host yet, so embedded images are **stripped with a warning** rather than
published as broken links. Text, headings, lists, links, and tables publish
faithfully. (Designed for Docs; a non-Markdown source's snapshot is published
as-is.)

---

## Behavior & limits

- **Overwrite by default**: the same `<DocName>.pdf` is rewritten each change, so
  the Desktop copy is always current. Set `"timestamped": true` to keep history.
- **No redundant writes**: nothing is exported unless the Doc's `modifiedTime`
  advances.
- **Auto-recovery**: network/quota errors are shown in the menu and retried with
  exponential backoff (up to 60 s), resetting on success. Access tokens refresh
  automatically; if the refresh token itself goes stale (e.g. a Testing-mode
  token expiring, or a revoked grant), the app re-authorizes on its own and only
  re-prompts the browser if a silent refresh isn't possible.
- **Pause**: stops the *next* poll. An export already in flight when you pause
  completes (a single export is atomic), then no further exports happen until you
  Resume. **Export now** works even while paused — it performs one export without
  un-pausing.
- **10 MB export cap**: the Drive `files.export` endpoint caps PDFs at 10 MB;
  larger docs surface a clear error (streaming export is out of scope).
- **Many targets** — watch any number of docs/sheets/slides/folders at once;
  each tracked independently (only changed files re-export).

---

## Auto-start at login (optional)

Two options; neither is required.

**A. Login Items + py2app** — package the app with `py2app`, then add the built
`.app` under System Settings → General → Login Items.

**B. LaunchAgent** — a sample plist is included at
[`launchd/com.doctopdf.agent.plist`](launchd/com.doctopdf.agent.plist). Edit the
two absolute paths inside it to match this project, then:

```bash
mkdir -p ~/Library/LaunchAgents
cp launchd/com.doctopdf.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.doctopdf.agent.plist
# to stop auto-start later:
launchctl unload ~/Library/LaunchAgents/com.doctopdf.agent.plist
```

---

## Project layout

```
doctopdf/
  app.py         # native AppKit menu bar: status item, menu, multi-target watch loop
  drive.py       # Google auth + Drive get/list/export helpers
  pipeline.py    # export pipeline: type-aware formats, output modes, git history, hook
  web.py         # web-page monitoring: fetch (static/Playwright) + extract/denoise
  summarize.py   # local-model (Ollama) change summary + severity/category classify
  alerts.py      # severity gating + Slack/Discord/webhook/email dispatch
  digest.py      # change-event log + scheduled daily/weekly digests
  audit.py       # Change-history dashboard (HTML report over the event log)
  rag.py         # change-aware vector sync: chunk → embed → Chroma upsert/delete + query
  mcp_server.py  # read-only MCP server exposing search_knowledge over stdio
  publish.py     # publishing pipeline: md→HTML render/sanitize, safe git push, 3 publishers
  themes/default.html  # built-in static-site theme ({{ title }} / {{ content }})
  __main__.py    # CLI: python -m doctopdf [app | query | mcp | rag reindex]
  prefs.py       # native tabbed Preferences window
  launchagent.py # install/remove the launch-at-login LaunchAgent
  config.py      # config load/save + token/config paths
tests/test_rag.py
requirements.txt
launchd/com.doctopdf.agent.plist
README.md
```

---

## No-auth variant (only if the Doc is link-shared)

If the Doc is shared **"anyone with the link"**, you can skip Google Cloud and
OAuth entirely: the public PDF export endpoint
`https://docs.google.com/document/d/<ID>/export?format=pdf` is fetchable without
credentials, and you can detect changes by hashing the returned bytes. That
trades a little bandwidth (it downloads the PDF every poll) for zero setup, but
it can't see private docs. This build ships the **Drive-API** path (works for
private docs and only downloads when the doc actually changes); the no-auth
approach is a drop-in swap of the Drive calls behind the same menu/loop if you
ever need it.

---

## Troubleshooting

- **"Missing client_secret.json"** — download your OAuth Desktop-app credentials
  (step 4 above) into the project root.
- **"Access denied — is the Doc shared with this account?"** — the authorized
  Google account must be able to open the Doc.
- **Auth window says the app is unverified** — expected for a Testing OAuth
  app; proceed (you are the developer and a test user).
- **Browser didn't open / closed it by accident** — click **Export now** or
  **Add Doc or Folder…** to retry authorization.
