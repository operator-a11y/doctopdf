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
STATIC_TIMEOUT = 20      # seconds
BROWSER_TIMEOUT = 25     # seconds (cap render wait so JS pages can't hang us)


class WebError(Exception):
    """A web fetch/extract problem, surfaced as a non-fatal target warning."""


class WebSkip(WebError):
    """Soft skip (e.g. selector matched nothing) — warn, do NOT diff/alert."""


class BotBlocked(WebError):
    """HTTP 403/429 — back off hard, never tight-loop a blocked site."""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_static(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=STATIC_TIMEOUT)
    except requests.RequestException as exc:
        raise WebError(f"fetch failed: {exc}") from exc
    if r.status_code in (403, 429):
        raise BotBlocked(f"blocked (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise WebError(f"HTTP {r.status_code}")
    return r.text


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

_WS = re.compile(r"[ \t]+")
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
    return _normalize_text(node.prettify())


def extract(html: str, selector: Optional[str], mode: str) -> str:
    """Reduce raw HTML to a clean content snapshot. Raises WebSkip if a selector
    matches nothing (so a site redesign warns instead of faking a mass deletion)."""
    if selector:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        el = soup.select_one(selector)
        if el is None:
            raise WebSkip(f"selector '{selector}' matched nothing (page redesigned?)")
        return _clean_html(el) if mode == "html" else _normalize_text(el.get_text("\n"))

    if mode == "html":
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        body = soup.body or soup
        return _clean_html(body)

    # mode == "text": main-content extraction with boilerplate removal.
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                   favor_recall=True)
    except Exception:  # noqa: BLE001 — fall back to a crude strip
        text = None
    if not text:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
        text = (soup.body or soup).get_text("\n")
    return _normalize_text(text)


def snapshot(target: dict) -> str:
    """Fetch + extract a target into its clean content snapshot string."""
    url = target.get("url")
    if not url:
        raise WebError("no url")
    render = (target.get("render") or "static").lower()
    selector = target.get("selector") or None
    mode = (target.get("mode") or "text").lower()
    html = fetch_browser(url, selector) if render == "browser" else fetch_static(url)
    snap = extract(html, selector, mode)
    if not snap.strip():
        raise WebSkip("extracted content was empty")
    return snap
