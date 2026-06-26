"""Change-event log + scheduled digests for DocToPDF.

Every change that passes the severity threshold is appended to a small JSON log
(``~/Library/Application Support/DocToPDF/events.json``). A daily/weekly digest
compiles the events since the last digest, ranked by severity, into a rollup.

The log doubles as the data for a future audit view. Stdlib only.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from typing import Optional

from . import config
from .alerts import SEVERITY_RANK

EVENTS_PATH = config.APP_SUPPORT_DIR / "events.json"
PRUNE_DAYS = 90        # drop events older than this
MAX_EVENTS = 5000      # hard cap

# Serializes every read-modify-write of events.json (append runs on change
# threads; the digest tick reads/marks on the main thread).
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        with open(EVENTS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("events", [])
            data.setdefault("last_digest", None)
            return data
    except (OSError, ValueError):
        pass
    return {"events": [], "last_digest": None}


def _save(data: dict) -> None:
    config.APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EVENTS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, EVENTS_PATH)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def append(event: dict, now: datetime) -> None:
    """Append a change event (``{time, doc, summary, severity, category}``)."""
    with _LOCK:
        data = _load()
        data["events"].append(event)
        cutoff = now - timedelta(days=PRUNE_DAYS)
        data["events"] = [
            e for e in data["events"]
            if (_parse(e.get("time")) or now) >= cutoff
        ][-MAX_EVENTS:]
        _save(data)


def due(cfg: dict, now: datetime) -> bool:
    """Whether a digest should be sent now (daily/weekly at the configured hour)."""
    mode = (cfg.get("digest") or "off").lower()
    if mode not in ("daily", "weekly"):
        return False
    if now.hour < int(cfg.get("digest_hour", 9) or 0):
        return False
    with _LOCK:
        last = _parse(_load().get("last_digest"))
    if last is None:
        return True
    if mode == "daily":
        return now.date() != last.date()         # once per calendar day, post-hour
    return (now.date() - last.date()).days >= 7   # weekly


def all_events() -> list[dict]:
    """Return the full change-event log (for the audit dashboard)."""
    with _LOCK:
        return list(_load().get("events", []))


def peek_since(now: datetime) -> list[dict]:
    """Return events since the last digest WITHOUT marking sent."""
    with _LOCK:
        data = _load()
    last = _parse(data.get("last_digest"))
    return [
        e for e in data["events"]
        if last is None or (_parse(e.get("time")) or now) > last
    ]


def mark_sent(now: datetime) -> None:
    """Record that a digest was delivered (called only after a successful send)."""
    with _LOCK:
        data = _load()
        data["last_digest"] = now.isoformat(timespec="seconds")
        _save(data)


def build_text(events: list[dict], period: str) -> str:
    """Format a digest, ranked by severity (material first)."""
    if not events:
        return f"DocToPDF {period} digest: no changes."
    def sev_of(e):  # normalize a missing/None severity consistently
        return e.get("severity") or "substantive"

    events = sorted(events, key=lambda e: (-SEVERITY_RANK.get(sev_of(e), 1),
                                           e.get("time") or ""))
    counts = {"material": 0, "substantive": 0, "cosmetic": 0}
    for e in events:
        counts[sev_of(e)] = counts.get(sev_of(e), 0) + 1
    head = (f"DocToPDF {period} digest — {len(events)} change(s): "
            f"{counts['material']} material, {counts['substantive']} substantive, "
            f"{counts['cosmetic']} cosmetic.")
    lines = [head, ""]
    for e in events:
        when = (e.get("time") or "")[:16].replace("T", " ")
        cat = e.get("category")
        tag = f"[{sev_of(e)}{'/' + cat if cat else ''}]"
        lines.append(f"• {tag} {e.get('doc', '?')}: {e.get('summary') or 'changed'}  ({when})")
    return "\n".join(lines)
