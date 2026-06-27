"""Configuration, paths, and persistence for DocToPDF.

- The OAuth app secret (``client_secret.json``) is resolved (in priority order)
  from the app-support dir, the packaged ``.app`` bundle's Resources, or the
  project root — see :func:`_resolve_client_secret_path`. Cached credentials live
  in the project root: a legacy single ``token.json`` (auto-migrated) and, for
  multi-account, a ``tokens/`` dir + an ``accounts.json`` index (see
  :mod:`accounts`). All are gitignored.
- User config (watched doc id, output dir, poll interval, …) is persisted to
  ``~/Library/Application Support/DocToPDF/config.json``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Project root = the directory that contains the ``doctopdf`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "DocToPDF"
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"


def _resolve_client_secret_path() -> Path:
    """Locate the OAuth ``client_secret.json``, in priority order:

    1. A user-supplied copy in the app-support dir — lets anyone drop in their own
       OAuth client without touching the bundle.
    2. The copy embedded inside the packaged ``.app`` (``Contents/Resources``) —
       so a distributed build ships its own OAuth client and end users do no
       Google setup.
    3. The project root — for running from source.

    Returns the first that exists; otherwise the project-root path (so the
    "Missing client_secret.json" guidance still points somewhere sensible).
    """
    user = APP_SUPPORT_DIR / "client_secret.json"
    if user.exists():
        return user
    if getattr(sys, "frozen", False):  # py2app bundle
        bundled = (Path(sys.executable).resolve().parent.parent
                   / "Resources" / "client_secret.json")
        if bundled.exists():
            return bundled
    return PROJECT_ROOT / "client_secret.json"


CLIENT_SECRET_PATH = _resolve_client_secret_path()

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
    # Record every change to the audit log (Change history dashboard). Requires a
    # text snapshot to diff, so this also enables text capture for plain setups.
    "audit_log": True,
    # --- RAG / vector sync -----------------------------------------------
    # A continuously-synced, change-aware local vector store over every watched
    # source, queryable by CLI and an MCP server. Embeddings are local by
    # default (Ollama) so content never leaves the machine. The store directory
    # is a local artifact (gitignored), separate from the git audit trail.
    "rag": {
        "enabled": True,
        "store_path": "~/Documents/DocExports/.vectorstore",
        "embedder": {
            "provider": "ollama",            # ollama | openai
            "model": "nomic-embed-text",
            # Provider-agnostic: each provider applies its own default base URL
            # (Ollama → localhost:11434, OpenAI → api.openai.com) when this is
            # null, so switching provider doesn't misroute to the other's host.
            "url": None,
            # "api_key": null,               # openai only; falls back to $OPENAI_API_KEY
        },
        "chunk": {"size": 1000, "overlap": 150},
        "mcp": {"enabled": True},
    },
    # --- Publishing pipeline ---------------------------------------------
    # Each entry binds a watched source to a destination and re-publishes its
    # Markdown snapshot on every stable change. Git auth is the user's own
    # SSH/credential setup; the app stores no tokens. See README for fields.
    #   {"source_id", "type": git_markdown|git_pages|pdf_template, "repo",
    #    "branch", "path", "template", "approval": auto|manual, "site_url"}
    "publish": [],
}


def _deep_fill(value: Any, default: Any) -> Any:
    """Recursively backfill missing keys in a nested-dict config value from its
    default, so adding a sub-key (e.g. ``rag.mcp``) doesn't get dropped when an
    older stored config supplies only part of the object. Keys the user supplied
    that aren't in the default (e.g. ``rag.embedder.api_key``) are preserved."""
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return dict(default)
        merged = dict(value)            # keep user-supplied extras not in the default
        for k in default:
            merged[k] = _deep_fill(value.get(k, default[k]), default[k])
        return merged
    return value


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
    # Backfill nested-dict sections (e.g. ``rag``) so partial stored objects keep
    # the defaults for any sub-keys they omit.
    for key, dval in DEFAULT_CONFIG.items():
        if isinstance(dval, dict):
            config[key] = _deep_fill(config.get(key), dval)
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
    # Config can hold an SMTP password — keep it owner-only.
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def resolve_output_dir(config: dict[str, Any]) -> Path:
    """Return the configured output directory as an expanded, absolute Path."""
    raw = config.get("output_dir") or DEFAULT_CONFIG["output_dir"]
    return Path(os.path.expanduser(str(raw))).resolve()
