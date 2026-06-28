"""Lightweight diagnostic log of the watch loop's per-poll decisions.

Writes to ``~/Library/Logs/DocToPDF/poll.log`` (size-bounded, rotated) so that
if exports ever seem to "stop", the log shows whether the loop is *still polling*
and what ``modifiedTime`` Google reports for each doc. That distinguishes the two
very different situations that look identical from the outside:

- **Google's revision-flush lag** — the log keeps ticking every cycle with the
  *same* ``mtime`` and ``(no change)``. The app is fine; Google just hasn't
  flushed a new revision yet (it batches edits and tends to flush when you pause
  or click out of the doc). This is expected, not a bug.
- **An actual stall** — the log goes silent. The loop stopped polling, which is
  a real bug worth chasing.

This is purely additive observability: it does **not** change how changes are
detected. It never raises into the caller (logging failures are swallowed), and
rotation caps total disk use at a few MB.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path.home() / "Library" / "Logs" / "DocToPDF"
LOG_PATH = LOG_DIR / "poll.log"

_logger: logging.Logger | None = None


def _get() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    lg = logging.getLogger("doctopdf.poll")
    lg.setLevel(logging.INFO)
    lg.propagate = False  # keep it out of any root/stderr handlers
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    except Exception:  # noqa: BLE001 — unwritable log dir must never break polling
        handler = logging.NullHandler()
    lg.addHandler(handler)
    _logger = lg
    return lg


def log(message: str) -> None:
    """Append one line to the poll log. Never raises."""
    try:
        _get().info(message)
    except Exception:  # noqa: BLE001 — diagnostics must never disturb the worker
        pass
