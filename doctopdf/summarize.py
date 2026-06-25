"""Local-model AI change summaries for DocToPDF.

On each change, diff the doc's text and ask a LOCAL model (Ollama by default —
no cloud API key) to summarize what changed in one sentence. Degrades gracefully
to ``None`` whenever no local model is reachable, so watching is never affected.

Uses only the stdlib (urllib) — no extra dependency.
"""

from __future__ import annotations

import difflib
import json
import urllib.error
import urllib.request
from typing import Optional

DIFF_CHAR_CAP = 6000   # keep the prompt small/fast for a local model
HTTP_TIMEOUT = 90      # local generation can be slow on first load

_PROMPT = (
    "You write terse changelog lines. Given a unified diff of edits to a "
    "document, summarize what changed in ONE short sentence (max ~15 words), "
    "plainly, no preamble. If only formatting changed, say so.\n\nDiff:\n"
)


def summarize_change(old_text: Optional[str], new_text: Optional[str], cfg: dict) -> Optional[str]:
    """Return a one-line summary of old→new, or ``None`` (no diff / model down)."""
    if not new_text or old_text is None or old_text == new_text:
        return None
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile="before", tofile="after", lineterm="",
    ))
    if not diff.strip():
        return None
    if len(diff) > DIFF_CHAR_CAP:
        diff = diff[:DIFF_CHAR_CAP] + "\n…(truncated)"

    url = (cfg.get("ollama_url") or "http://localhost:11434").rstrip("/")
    model = cfg.get("ollama_model") or "llama3"
    body = json.dumps({
        "model": model,
        "prompt": _PROMPT + diff,
        "stream": False,
        "options": {"temperature": 0.2},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None  # no local model reachable — silently skip
    summary = (data.get("response") or "").strip().replace("\n", " ")
    return summary or None
