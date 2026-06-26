"""Web-page monitoring for DocToPDF — fetch, extract/denoise, snapshot.

A web target is fetched (static or JS-rendered), reduced to a clean **content
snapshot**, and handed to the existing change pipeline (diff → classify →
severity → alert → digest → git) exactly like a Google Doc's text snapshot.
This module is *only* fetch + extract + denoise; everything after the snapshot
is reuse.

Network/parse failures raise the exceptions below so the caller can degrade
gracefully (warn + back off), never alerting a phantom change.
"""

from __future__ import annotations

import re
from typing import Optional

import requests

UA = ("DocToPDF/1.0 (+https://github.com/operator-a11y/doctopdf; "
      "personal change monitor)")
STATIC_TIMEOUT = 20      # seconds (connect, read)
MAX_BYTES = 8 * 1024 * 1024   # cap a fetch so a huge/streaming page can't OOM us
BROWSER_TIMEOUT = 25     # seconds (cap render wait so JS pages can't hang us)
RETRYABLE_STATUS = (403, 429, 503)   # bot-block / overload → back off, don't hammer


class WebError(Exception):
    """A web fetch/extract problem, surfaced as a non-fatal target warning."""


class WebSkip(WebError):
    """Soft skip (e.g. selector matched nothing) — warn, do NOT diff/alert."""


class BotBlocked(WebError):
    """HTTP 403/429 — back off hard, never tight-loop a blocked site."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_static(url: str) -> bytes:
    """Fetch up to MAX_BYTES of a page as raw bytes (so the parsers can sniff the
    page's own <meta charset>, not requests' latin-1 header fallback)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=STATIC_TIMEOUT,
                         stream=True)
    except requests.RequestException as exc:
        raise WebError(f"fetch failed: {exc}") from exc
    try:
        if r.status_code in RETRYABLE_STATUS:
            raise BotBlocked(f"blocked (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise WebError(f"HTTP {r.status_code}")
        chunks, total = [], 0
        try:
            for chunk in r.iter_content(64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise WebError(f"page too large (>{MAX_BYTES // (1024 * 1024)} MB)")
        except requests.RequestException as exc:
            raise WebError(f"fetch failed mid-stream: {exc}") from exc
        return b"".join(chunks)
    finally:
        r.close()


def fetch_browser(url: str, selector: Optional[str]) -> str:
    """Render with Playwright Chromium (launch-on-demand, always close)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise WebError("Playwright not installed — run: pip install playwright "
                       "&& playwright install chromium") from exc
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001 — usually "chromium not installed"
                raise WebError(f"browser launch failed (run 'playwright install "
                               f"chromium'): {exc}") from exc
            try:
                page = browser.new_page(user_agent=UA)
                page.goto(url, timeout=BROWSER_TIMEOUT * 1000, wait_until="domcontentloaded")
                try:
                    if selector:
                        page.wait_for_selector(selector, timeout=8000)
                    else:
                        page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001 — never settled; use what rendered
                    pass
                return page.content()
            finally:
                browser.close()
    except WebError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WebError(f"browser fetch failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Extract + denoise
# ---------------------------------------------------------------------------

# Collapse runs of any unicode whitespace (incl. nbsp \xa0) to a single space —
# otherwise a page swapping a space for an nbsp creates a phantom diff.
_WS = re.compile(r"[^\S\n]+")
# Attributes that change every load and would create phantom HTML diffs.
_VOLATILE_ATTRS = ("nonce", "csrf", "csrf-token", "integrity", "data-nonce",
                   "data-csrf", "data-turbo-track")


def _normalize_text(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = _WS.sub(" ", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def _clean_html(node) -> str:
    from bs4 import Comment
    for tag in node(["script", "style", "noscript", "template", "svg", "iframe"]):
        tag.decompose()
    for c in node.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for tag in node.find_all(True):
        for attr in list(tag.attrs):
            if attr in _VOLATILE_ATTRS or attr.startswith("on"):
                del tag[attr]
    # Structural empty check: a node stripped to no real text is almost always a
    # transient broken/empty render — skip rather than report a mass deletion.
    if not node.get_text(strip=True):
        raise WebSkip("rendered content was empty")
    return _normalize_text(node.prettify())


def extract(html, selector: Optional[str], mode: str) -> str:
    """Reduce raw HTML (bytes or str) to a clean content snapshot. ``html`` is
    passed to the parsers as bytes when available so they sniff the page's own
    <meta charset>. Raises WebSkip if a selector matches nothing (so a redesign
    warns instead of faking a mass deletion)."""
    if selector or mode == "html":
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        if selector:
            el = soup.select_one(selector)
            if el is None:
                raise WebSkip(f"selector '{selector}' matched nothing (page redesigned?)")
            return _clean_html(el) if mode == "html" else _normalize_text(el.get_text("\n"))
        for tag in soup(["nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        return _clean_html(soup.body or soup)

    # mode == "text": main-content extraction with boilerplate removal. Use ONE
    # extractor (trafilatura) so the snapshot is stable; if it can't extract,
    # skip with a warning (a second extractor would cause a one-off false diff).
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                   favor_recall=True)
    except Exception as exc:  # noqa: BLE001
        raise WebError(f"extraction failed: {exc}") from exc
    if not text or not text.strip():
        raise WebSkip("couldn't extract main content — add a CSS 'selector'")
    return _normalize_text(text)


def snapshot(target: dict) -> str:
    """Fetch + extract a target into its clean content snapshot string."""
    url = target.get("url")
    if not url:
        raise WebError("no url")
    if not str(url).lower().startswith(("http://", "https://")):
        raise WebError("only http(s) URLs are supported")
    render = (target.get("render") or "static").lower()
    selector = target.get("selector") or None
    mode = (target.get("mode") or "text").lower()
    html = fetch_browser(url, selector) if render == "browser" else fetch_static(url)
    snap = extract(html, selector, mode)
    # Optional per-target regex to drop volatile lines (timestamps, counters, …).
    ignore = target.get("ignore")
    if ignore:
        try:
            pat = re.compile(ignore)
            snap = "\n".join(ln for ln in snap.splitlines() if not pat.search(ln))
        except re.error:
            pass  # bad pattern — ignore the filter rather than fail the poll
    if not snap.strip():
        raise WebSkip("extracted content was empty")
    return snap
