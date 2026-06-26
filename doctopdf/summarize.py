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

# Common synonyms the model may emit instead of our three labels.
_SEVERITY_SYNONYMS = {
    "critical": "material", "high": "material", "major": "material",
    "important": "material", "urgent": "material", "significant": "material",
    "minor": "cosmetic", "low": "cosmetic", "trivial": "cosmetic",
    "info": "cosmetic", "informational": "cosmetic", "formatting": "cosmetic",
    "typo": "cosmetic", "style": "cosmetic", "whitespace": "cosmetic",
    "medium": "substantive", "moderate": "substantive", "normal": "substantive",
}

# A change touching at least this many diff lines can't be labelled "cosmetic"
# (defends against truncated diffs and prompt-injection that downgrades severity).
_FLOOR_CHANGED_LINES = 10

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
        data = json.loads(outer.get("response") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None  # no local model reachable, or unparseable — skip
    # format=json can yield an array/scalar; unwrap a single-object list.
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    return data if isinstance(data, dict) else None


def _norm_severity(raw) -> str:
    s = str(raw or "").strip().lower()
    if s in SEVERITIES:
        return s
    return _SEVERITY_SYNONYMS.get(s, "substantive")  # unknown → a real change


def _floor_severity(severity: str, diff: str) -> str:
    """A large or truncated diff can't be 'cosmetic' — bias upward."""
    if severity != "cosmetic":
        return severity
    changed = sum(
        1 for ln in diff.splitlines()
        if (ln[:1] in "+-") and not ln.startswith(("+++", "---"))
    )
    if changed >= _FLOOR_CHANGED_LINES or "…(truncated)" in diff:
        return "substantive"
    return severity


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
    if data is None:
        return None  # model failure — caller treats this as "couldn't classify"
    severity = _floor_severity(_norm_severity(data.get("severity")), diff)
    category = (str(data.get("category") or "change").strip().lower() or "change")[:24]
    # Keep a valid severity even if the model returned no summary text.
    summary = str(data.get("summary") or "").strip().replace("\n", " ") or "Document changed."
    return {"summary": summary, "severity": severity, "category": category}


def summarize_change(old_text, new_text, cfg) -> Optional[str]:
    """Back-compat: return just the summary string (or None)."""
    res = classify_change(old_text, new_text, cfg)
    return res["summary"] if res else None
