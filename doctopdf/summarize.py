"""Local-model change classification + summaries for DocToPDF.

On each change, diff the doc's text and ask a LOCAL model (Ollama — no cloud key)
to return a one-line summary **plus a severity and category**, so the app can
tell apart "a thing changed" from "something you need to act on changed".

Degrades gracefully to ``None`` whenever no local model is reachable, so watching
is never affected. Uses only the stdlib (urllib) — no extra dependency.
"""

from __future__ import annotations

import difflib
import json
import urllib.error
import urllib.request
from typing import Optional

DIFF_CHAR_CAP = 6000   # keep the prompt small/fast for a local model
HTTP_TIMEOUT = 90      # local generation can be slow on first load

SEVERITIES = ("cosmetic", "substantive", "material")

_PROMPT = (
    "You are a document-change analyst. You are given a unified diff of edits to a "
    "document. Respond with ONLY a JSON object of the form:\n"
    '{"summary": "<one terse sentence, max 15 words>", '
    '"severity": "cosmetic" | "substantive" | "material", '
    '"category": "<one or two words>"}\n'
    "Severity guide: cosmetic = formatting/typos/whitespace only; substantive = "
    "meaningful content added or reworded; material = changes affecting meaning, "
    "obligations, numbers, money, dates, names, or decisions. Category examples: "
    "wording, content, structure, formatting, pricing, legal, dates, data, status.\n\n"
    "Diff:\n"
)


def _ollama_json(prompt: str, cfg: dict) -> Optional[dict]:
    url = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    model = cfg.get("ollama_model") or "llama3"
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",                 # force valid JSON out of Ollama
        "options": {"temperature": 0.1},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            outer = json.loads(resp.read().decode("utf-8"))
        return json.loads(outer.get("response") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None  # no local model reachable, or unparseable — skip


def _diff(old_text: str, new_text: str) -> Optional[str]:
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile="before", tofile="after", lineterm="",
    ))
    if not diff.strip():
        return None
    if len(diff) > DIFF_CHAR_CAP:
        diff = diff[:DIFF_CHAR_CAP] + "\n…(truncated)"
    return diff


def classify_change(old_text: Optional[str], new_text: Optional[str], cfg: dict) -> Optional[dict]:
    """Return ``{"summary", "severity", "category"}`` for old→new, or ``None``
    (no diff / model unreachable). Severity is always one of SEVERITIES."""
    if not new_text or old_text is None or old_text == new_text:
        return None
    diff = _diff(old_text, new_text)
    if diff is None:
        return None
    data = _ollama_json(_PROMPT + diff, cfg)
    if not isinstance(data, dict):
        return None
    summary = str(data.get("summary") or "").strip().replace("\n", " ")
    if not summary:
        return None
    severity = str(data.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        severity = "substantive"  # unknown label → treat as a real change
    category = (str(data.get("category") or "change").strip().lower() or "change")[:24]
    return {"summary": summary, "severity": severity, "category": category}


def summarize_change(old_text, new_text, cfg) -> Optional[str]:
    """Back-compat: return just the summary string (or None)."""
    res = classify_change(old_text, new_text, cfg)
    return res["summary"] if res else None
