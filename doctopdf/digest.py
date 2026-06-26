"""Change-event log + scheduled digests for DocToPDF.

Every change that passes the severity threshold is appended to a small JSON log
(``~/Library/Application Support/DocToPDF/events.json``). A daily/weekly digest
compiles the events since the last digest, ranked by severity, into a rollup.

The log doubles as the data for a future audit view. Stdlib only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Optional

from . import config
from .alerts import SEVERITY_RANK

EVENTS_PATH = config.APP_SUPPORT_DIR / "events.json"
PRUNE_DAYS = 90        # drop events older than this
MAX_EVENTS = 5000      # hard cap


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
    last = _parse(_load().get("last_digest"))
    if last is None:
        return True
    interval = timedelta(days=1 if mode == "daily" else 7)
    # Require a new calendar day AND the minimum interval elapsed.
    return now.date() != last.date() and (now - last) >= interval


def collect_and_mark(now: datetime) -> list[dict]:
    """Return events since the last digest and record that a digest was sent."""
    data = _load()
    last = _parse(data.get("last_digest"))
    events = [
        e for e in data["events"]
        if last is None or (_parse(e.get("time")) or now) > last
    ]
    data["last_digest"] = now.isoformat(timespec="seconds")
    _save(data)
    return events


def build_text(events: list[dict], period: str) -> str:
    """Format a digest, ranked by severity (material first)."""
    if not events:
        return f"DocToPDF {period} digest: no changes."
    events = sorted(
        events,
        key=lambda e: (-SEVERITY_RANK.get((e.get("severity") or "substantive"), 1),
                       e.get("time") or ""),
    )
    counts = {"material": 0, "substantive": 0, "cosmetic": 0}
    for e in events:
        counts[e.get("severity") or "substantive"] = counts.get(e.get("severity") or "substantive", 0) + 1
    head = (f"DocToPDF {period} digest — {len(events)} change(s): "
            f"{counts['material']} material, {counts['substantive']} substantive, "
            f"{counts['cosmetic']} cosmetic.")
    lines = [head, ""]
    for e in events:
        sev = (e.get("severity") or "?")
        when = (e.get("time") or "")[:16].replace("T", " ")
        cat = e.get("category")
        tag = f"[{sev}{'/' + cat if cat else ''}]"
        lines.append(f"• {tag} {e.get('doc', '?')}: {e.get('summary') or 'changed'}  ({when})")
    return "\n".join(lines)
