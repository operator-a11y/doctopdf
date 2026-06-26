"""Configuration, paths, and persistence for DocToPDF.

- OAuth secrets (``client_secret.json``) and the cached ``token.json`` live in the
  project root, next to this package. Both are gitignored.
- User config (watched doc id, output dir, poll interval, …) is persisted to
  ``~/Library/Application Support/DocToPDF/config.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Project root = the directory that contains the ``doctopdf`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CLIENT_SECRET_PATH = PROJECT_ROOT / "client_secret.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "DocToPDF"
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"

# OAuth scope — read-only Drive covers both metadata reads and PDF export.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# ---------------------------------------------------------------------------
# Config defaults & schema
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    # Watch list: each entry is {"id": <file or folder id>, "output_dir"?, "formats"?}.
    # A folder id mirrors all exportable files inside it. Per-entry output_dir /
    # formats override the globals below. Managed via the menu.
    "watch": [],
    # Legacy single-doc id; migrated into ``watch`` on load (kept for back-compat).
    "doc_id": None,
    # Where exported files are written. ``~`` is expanded on use.
    "output_dir": "~/Desktop",
    # Poll interval in seconds.
    "poll_interval": 10,
    # When True, write ``<name> <timestamp>.<ext>`` instead of overwriting.
    "timestamped": False,
    # Export formats written on each change (see pipeline.EXPORT_FORMATS):
    # any of pdf, docx, odt, rtf, txt, html, md, epub.
    "formats": ["pdf"],
    # Rolling history: if > 0, write timestamped files and keep only the newest N
    # (per format). 0 disables (overwrite or keep-all per ``timestamped``).
    "keep_versions": 0,
    # Git version history: path to a repo dir. On each change the exports are
    # written there (stable names) and committed. ``None`` disables.
    "git_repo": None,
    # When git_repo is set, also export a markdown snapshot so history has real
    # text diffs (PDFs/docx don't diff).
    "git_snapshot_text": True,
    # Shell command run after each export. ``$1`` and $DOCTOPDF_PRIMARY are the
    # primary file path; $DOCTOPDF_FILES lists all; $DOCTOPDF_DOC_NAME the name.
    "post_export_cmd": None,
    # Post a macOS notification on each export.
    "notify": False,
    # AI change summaries via a LOCAL model (no cloud key). On each change, diff
    # the doc's text and have the local model summarize + classify it.
    "ai_summary": False,
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3",
    # --- Change intelligence ---------------------------------------------
    # Only alert at/above this severity: cosmetic < substantive < material.
    # "cosmetic" = alert on everything; raise it to cut notification fatigue.
    "min_severity": "cosmetic",
    # External alert destinations for changes that pass the threshold.
    "webhook_urls": [],            # Slack / Discord / generic incoming webhooks
    "email_to": None,              # alert recipient (needs the SMTP settings below)
    "email_from": None,
    "smtp_host": None,
    "smtp_port": 587,
    "smtp_user": None,
    "smtp_pass": None,
    # Scheduled digest: a ranked rollup of changes. off | daily | weekly.
    "digest": "off",
    "digest_hour": 9,              # local hour (0–23) to send the digest
}


def load_config() -> dict[str, Any]:
    """Load config from disk, merged over defaults. Never raises on a bad file."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            config.update({k: stored[k] for k in DEFAULT_CONFIG if k in stored})
    except (OSError, ValueError):
        # Missing, unreadable, non-UTF8, or malformed config — fall back to
        # defaults. (json.JSONDecodeError and UnicodeDecodeError are both
        # ValueError; FileNotFoundError/permission errors are OSError.)
        pass
    return config


def save_config(config: dict[str, Any]) -> None:
    """Persist config to disk, creating the Application Support dir if needed."""
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    # Only persist known keys so we don't drift on schema changes.
    to_store = {k: config.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(to_store, fh, indent=2)
    os.replace(tmp_path, CONFIG_PATH)  # atomic on POSIX


def resolve_output_dir(config: dict[str, Any]) -> Path:
    """Return the configured output directory as an expanded, absolute Path."""
    raw = config.get("output_dir") or DEFAULT_CONFIG["output_dir"]
    return Path(os.path.expanduser(str(raw))).resolve()
