"""Audit dashboard for DocToPDF — render the change-event log as a browsable,
self-contained HTML report (no external assets), opened in the default browser.

The data is the same `events.json` log the digests use; this just surfaces it as
"what changed, when, by whom, how severe" with client-side search + sort.
"""

from __future__ import annotations

import html
import os
import time
from pathlib import Path
from typing import Optional

from . import config
from .alerts import SEVERITY_RANK

REPORT_PATH = config.APP_SUPPORT_DIR / "change-history.html"

_SEV_COLOR = {"material": "#d92d20", "substantive": "#b54708", "cosmetic": "#667085"}

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
       background: Canvas; color: CanvasText; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: #667085; margin: 0 0 16px; }
.stats { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
.stat { padding: 8px 14px; border-radius: 10px; background: rgba(127,127,127,.12); font-weight: 600; }
.stat b { font-size: 18px; }
input { width: 100%; padding: 10px 12px; font-size: 14px; border-radius: 8px;
        border: 1px solid rgba(127,127,127,.35); background: transparent; color: inherit;
        margin-bottom: 14px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid rgba(127,127,127,.18);
         vertical-align: top; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #667085;
     cursor: pointer; user-select: none; position: sticky; top: 0; background: Canvas; }
td.time, td.who { white-space: nowrap; color: #667085; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px; color: #fff;
         font-size: 12px; font-weight: 700; }
.cat { color: #667085; font-size: 12px; }
.empty { padding: 40px; text-align: center; color: #667085; }
"""

_SCRIPT = """
const q = document.getElementById('q'), rows = [...document.querySelectorAll('tbody tr')];
q.addEventListener('input', () => {
  const t = q.value.toLowerCase();
  rows.forEach(r => r.style.display = r.textContent.toLowerCase().includes(t) ? '' : 'none');
});
document.querySelectorAll('th[data-k]').forEach((th, i) => th.addEventListener('click', () => {
  const tb = document.querySelector('tbody'), asc = th.dataset.asc !== 'true';
  th.dataset.asc = asc;
  [...tb.querySelectorAll('tr')].sort((a, b) => {
    const x = a.cells[i].dataset.s || a.cells[i].textContent;
    const y = b.cells[i].dataset.s || b.cells[i].textContent;
    return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
  }).forEach(r => tb.appendChild(r));
}));
"""


def build_report(events: list, generated: str) -> str:
    counts = {"material": 0, "substantive": 0, "cosmetic": 0}
    for e in events:
        counts[e.get("severity") or "substantive"] = counts.get(e.get("severity") or "substantive", 0) + 1
    events = sorted(events, key=lambda e: e.get("time") or "", reverse=True)

    def esc(v):
        return html.escape(str(v if v is not None else ""))

    rows = []
    for e in events:
        sev = e.get("severity") or "substantive"
        color = _SEV_COLOR.get(sev, "#667085")
        when = (e.get("time") or "").replace("T", " ")[:19]
        rank = SEVERITY_RANK.get(sev, 1)
        rows.append(
            f"<tr><td class='time'>{esc(when)}</td>"
            f"<td>{esc(e.get('doc'))}</td>"
            f"<td data-s='{rank}'><span class='badge' style='background:{color}'>{esc(sev)}</span></td>"
            f"<td class='cat'>{esc(e.get('category'))}</td>"
            f"<td>{esc(e.get('summary') or 'changed')}</td>"
            f"<td class='who'>{esc(e.get('who') or '—')}</td></tr>"
        )
    body = "".join(rows) or ""
    table = (
        "<table><thead><tr>"
        "<th data-k>When</th><th data-k>Source</th><th data-k>Severity</th>"
        "<th data-k>Category</th><th data-k>What changed</th><th data-k>By</th>"
        "</tr></thead><tbody>" + body + "</tbody></table>"
        if events else "<div class='empty'>No changes recorded yet.</div>"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>DocToPDF — Change history</title><style>{_STYLE}</style></head><body>
<h1>DocToPDF — Change history</h1>
<p class="sub">{len(events)} change(s) · generated {esc(generated)}</p>
<div class="stats">
  <div class="stat" style="color:{_SEV_COLOR['material']}"><b>{counts['material']}</b> material</div>
  <div class="stat" style="color:{_SEV_COLOR['substantive']}"><b>{counts['substantive']}</b> substantive</div>
  <div class="stat" style="color:{_SEV_COLOR['cosmetic']}"><b>{counts['cosmetic']}</b> cosmetic</div>
</div>
<input id="q" placeholder="Filter changes…" autofocus>
{table}
<script>{_SCRIPT}</script></body></html>"""


def write_report(events: list, generated: str) -> Path:
    """Render + write the report; return its path."""
    config.APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_PATH.with_suffix(".html.tmp")
    tmp.write_text(build_report(events, generated), encoding="utf-8")
    os.replace(tmp, REPORT_PATH)
    return REPORT_PATH
