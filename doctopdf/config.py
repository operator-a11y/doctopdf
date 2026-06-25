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
    # Google Doc file id to watch (``None`` until the user sets one).
    "doc_id": None,
    # Where exported PDFs are written. ``~`` is expanded on use.
    "output_dir": "~/Desktop",
    # Poll interval in seconds.
    "poll_interval": 10,
    # When True, write ``<name> <timestamp>.pdf`` instead of overwriting.
    "timestamped": False,
}


def load_config() -> dict[str, Any]:
    """Load config from disk, merged over defaults. Never raises on a bad file."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            config.update({k: stored[k] for k in DEFAULT_CONFIG if k in stored})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # Missing or corrupt config — fall back to defaults.
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
