# DocToPDF — Web-Page Monitoring (Claude Code build increment)

Add **web page / URL monitoring** as a new source kind to the existing DocToPDF app. A web target is watched, fetched, denoised, and diffed; when its content meaningfully changes, it flows into the **existing** change pipeline you already built — AI classification (cosmetic / substantive / material), severity filtering, alert routing (Slack / email / webhook), scheduled digests, and the git audit trail — exactly the way a Google Doc change does. The only new code is fetch + extract + diff; everything after the diff is reuse.

## Step 0 — Read the existing codebase first
Map the current app: the watch-list config model, the target/poll loop, the text-snapshot + git diff history, the Ollama classification + severity logic, the Slack/email/webhook routing, and the digest code. **Conform to what exists** and reuse the downstream pipeline unchanged — web targets must enter the same path a Doc change enters after the diff step. If names/shapes differ from the assumptions below, adapt and list deviations in your plan. Then build phase by phase, committing in coherent steps; **no AI attribution in commits**.

## Why web is different from the Drive path (the whole reason this needs new code)
The Google path was clean because `modifiedTime` tells you *when* something changed and `export` returns clean content. Web pages give you **neither**: you fetch raw HTML, there's no change signal, and the page is full of noise (nav, ads, rotating banners, timestamps, CSRF tokens) that breaks naive diffing and fires false alerts. So web monitoring is three sub-problems the engine doesn't have yet — **fetch**, **extract + denoise**, **diff on cleaned content** — and then the existing pipeline takes over.

## Stack additions
- **`trafilatura`** (preferred) or `readability-lxml` for main-content extraction / boilerplate removal.
- **`playwright`** (Chromium) for JS-rendered pages — most modern pricing/ToS/changelog pages need it. Requires `playwright install chromium` (note in README).
- `requests` (already present) for static fetch; `beautifulsoup4` + `lxml` for selector scoping/normalization.
- Ask before adding anything else.

## Config — the `web` target shape
Extend the watch list with a new `kind: "web"`:

```json
{
  "kind": "web",
  "url": "https://competitor.com/pricing",
  "name": "Competitor pricing",
  "render": "static",            // static | browser  (browser = Playwright for JS pages)
  "selector": "#pricing-table",  // optional CSS scope; monitor just this region
  "mode": "text",                // text | html  (text = robust content monitoring; html = catch structural changes)
  "poll_seconds": 600,           // web defaults FAR higher than docs — be polite
  "severity_min": "substantive"  // reuse the existing severity filter
  // alert routing + digest inheritance are reused from the existing pipeline
}
```

## Build

### Phase 1 — Fetch (`doctopdf/web.py`)
- **Static:** `requests.get` with a descriptive custom User-Agent and a timeout.
- **Browser:** when `render: "browser"`, load via Playwright Chromium, wait for network-idle (or for `selector` to appear) so you capture the *rendered* state, then take the HTML. Launch on demand and close; for a handful of targets that's fine — don't hold a browser open per poll.
- **Politeness / safety:** web targets poll on their own (higher) interval, not the ~10s doc cadence; default ~10 min, configurable. Set a real User-Agent, respect timeouts, and back off hard on `403/429` (bot-blocking) — double the interval up to a cap and surface the state. Optionally honor `robots.txt` (config flag).
- **Cheap change fast-path (optional):** send `If-None-Match` / `If-Modified-Since` using stored `ETag` / `Last-Modified`; a `304` means skip without re-extracting. Treat this as an optimization only — the real signal is the content hash below, since many pages have no usable ETag.

### Phase 2 — Extract + denoise (`doctopdf/web.py`)
- If `selector` is set, scope to that element first (BeautifulSoup); **if the selector matches nothing, warn and skip — do NOT treat an empty match as "everything was deleted"** (that's the classic false-positive that wrecks trust).
- Otherwise run `trafilatura` (or readability) to pull main content and strip boilerplate (nav, ads, footers, scripts/styles).
- Normalize: collapse whitespace, drop volatile bits (timestamps, tokens, ad slots) where detectable, strip noisy attributes. For `mode: "text"` reduce to clean text; for `mode: "html"` keep normalized HTML.
- The result is the **content snapshot** — store it exactly like the existing Doc text snapshot so git diff and Ollama operate on it identically.

### Phase 3 — Diff + hand off to the existing pipeline
- **Change detection:** hash the cleaned snapshot; compare to the stored hash for that target. Only proceed if it changed (first run just stores the baseline + does an initial commit).
- **Diff:** unified diff of cleaned old vs new — the same diff your docs already produce.
- **Reuse everything downstream, unchanged:** feed that diff into the existing Ollama **classification** (cosmetic / substantive / material) → **severity filter** (`severity_min`) → **alert routing** (Slack / email / webhook) → **scheduled digest** → **git commit** of the snapshot with a timestamped message for the audit trail. A web change and a Doc change must be indistinguishable to this pipeline.
- Notifications/menu: web targets appear in the same status list, recent-changes submenu, and digests as doc targets, labeled by `name`.

## Edge cases
- Selector breakage (site redesign) → warn + skip, don't alert a phantom mass deletion.
- Bot-blocking / rate-limit (403/429) → exponential backoff + visible error state; never tight-loop a blocked site.
- JS pages that never settle → cap the render wait; fall back to whatever rendered, flagged.
- Browser lifecycle → launch-on-demand, always close; keep memory bounded with several targets.
- Keep web polls staggered so you don't fetch every target on the same tick.
- Privacy note (README): web page content is public, but the same local-first promise holds — content can be summarized locally via Ollama; only the alert/metadata leaves the machine if the user routes to Slack/email.

## Project layout (additions)
- `doctopdf/web.py` — fetch (static + Playwright), extract/denoise, snapshot, hash + diff.
- Reuse existing modules for classification, severity, routing, digests, git, config, and the menu app — wire `kind: "web"` into the existing target loop.

## Definition of done
- Adding a `web` target (try one static page and one JS-rendered page with a `selector`) starts monitoring on its own interval.
- Editing/observing a real change on a watched page produces: a denoised diff, an Ollama classification + severity, a routed alert (if above `severity_min`), an entry in the digest, and a git commit of the snapshot — all through the existing pipeline.
- Noise (rotating ads, timestamps) does **not** trigger alerts; selector-scoped targets only react to their region.
- Bot-blocking and selector-breakage degrade gracefully with visible status, no phantom alerts.
- README covers `playwright install chromium`, the `web` target config fields, and the politeness/interval defaults.

## Out of scope
Authenticated/paywalled pages, crawling beyond a single URL, screenshot/visual diffing, CAPTCHA solving, and any change to the existing downstream pipeline beyond wiring web targets into it.
