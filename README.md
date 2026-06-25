# DocToPDF

A tiny macOS **menu-bar app** that watches a single Google Doc and automatically
re-exports it to a PDF on your Desktop whenever the doc changes (polling every
~10 seconds). Edit the doc in Google Docs, and within ~10–20 seconds a fresh
`~/Desktop/<DocName>.pdf` appears, overwriting the previous copy.

```
📄  DocToPDF
 ├─ Watching: My Document
 ├─ Last export: 14:32:07
 ├─ Export now
 ├─ Open PDF
 ├─ Reveal in Finder
 ├─ Pause
 ├─ Set Google Doc…
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
python -m doctopdf.app
```

> If `python3` is an older version, use an explicit one, e.g.
> `python3.12 -m venv .venv`.

The app appears as a **📄** icon in the menu bar. On first run it prompts for
Google authorization. Then:

1. Click the icon → **Set Google Doc…**
2. Paste a Doc **URL** (`https://docs.google.com/document/d/<ID>/edit`) or a bare
   **Doc ID**.
3. That's it — the Desktop PDF starts updating automatically.

---

## Menu actions

| Item | What it does |
| --- | --- |
| **Watching: …** / **Last export: …** | Live status (or an error message). |
| **Export now** | Force an immediate re-export. |
| **Open PDF** | Open the exported PDF in your default viewer. |
| **Reveal in Finder** | Select the PDF in Finder. |
| **Pause / Resume** | Stop/start the watch loop. |
| **Set Google Doc…** | Change which Doc is watched. |
| **Quit** | Exit the app. |

The menu-bar icon reflects state: **📄** watching/idle, **🔄** exporting,
**⏸** paused, **⚠️** error.

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
| `doc_id` | `null` | The watched Google Doc ID (set via the menu). |
| `output_dir` | `~/Desktop` | Where PDFs are written. |
| `poll_interval` | `10` | Seconds between change checks (min 3). |
| `timestamped` | `false` | If `true`, write `<DocName> <timestamp>.pdf` (keeps history) instead of overwriting one file. |

Edit the file while the app is closed, then relaunch to pick up changes.

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
- **Single doc** by design — one Doc at a time.

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
  app.py       # native AppKit menu bar: status item, menu, watch loop (worker + UI timer)
  drive.py     # Google auth + Drive get/export helpers
  config.py    # config load/save + token/config paths
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
  **Set Google Doc…** to retry authorization.
