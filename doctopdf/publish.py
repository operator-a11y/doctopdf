"""Continuous publishing pipeline for DocToPDF.

When a watched source changes, its Markdown snapshot (already captured by the
engine — no re-fetch) is published to a destination bound to that source:

- ``git_markdown`` — write the Markdown to a target repo + push.
- ``git_pages``    — render Markdown → sanitized HTML → themed page, push to a
                     dedicated branch (GitHub Pages / Netlify / Vercel auto-deploys).
- ``pdf_template`` — render to a branded HTML/CSS template, then Playwright
                     ``page.pdf()`` (reuses the Chromium already used for web monitoring).

Safe git: the app owns a *dedicated branch* and a working copy under app data;
it pull-rebases before pushing and only ever ``--force-with-lease`` on that
app-owned branch — never the user's ``main``. Git auth is the user's existing
SSH/credential setup; the app stores no tokens and surfaces push errors.

This module is pure logic (no AppKit); the menu/worker wiring lives in app.py,
where publishing is registered as another downstream consumer of the change event.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import markdown as _markdown
import nh3

from . import config

PUBLISH_DIR = config.APP_SUPPORT_DIR / "publish"     # app-owned git working copies
THEMES_DIR = Path(__file__).resolve().parent / "themes"
GIT_TIMEOUT = 120                                    # seconds per git invocation

# Sanitizer allowlist = nh3 defaults minus <img>: images can't be resolved on the
# published host yet (see strip_images), so we drop them rather than emit broken
# links. Scripts / event handlers / javascript: URLs are stripped by nh3 anyway.
_ALLOWED_TAGS = set(nh3.ALLOWED_TAGS) - {"img"}

# Registered publishers, keyed by target ``type``. (Notion / Confluence / generic
# webhook destinations are intentionally left as future extension points — see
# the stub at the bottom of this module.)
PUBLISHERS: dict = {}


class PublishError(Exception):
    """A publish failed (git push/auth/render). Carries a short message."""


class PublishSkip(Exception):
    """Nothing to publish (empty snapshot) — never wipes a live destination."""


# ---------------------------------------------------------------------------
# Markdown → sanitized, themed HTML
# ---------------------------------------------------------------------------

_IMG_INLINE = re.compile(r"!\[[^\]]*\]\([^)]*\)")        # ![alt](src)
_IMG_REF = re.compile(r"!\[[^\]]*\]\[[^\]]*\]")          # ![alt][ref]


def strip_images(md_text: str) -> tuple:
    """Remove Markdown image syntax and return ``(text, count)``.

    Doc-embedded images aren't exported to the publish host yet, so we strip them
    (with a surfaced warning) instead of publishing broken links — consistent with
    the "don't silently mangle" rule. Text, headings, lists, links, tables stay.
    """
    count = len(_IMG_INLINE.findall(md_text)) + len(_IMG_REF.findall(md_text))
    text = _IMG_REF.sub("", _IMG_INLINE.sub("", md_text))
    return text, count


def render_markdown(md_text: str) -> str:
    """Markdown → HTML with tables, fenced code, and sane lists."""
    return _markdown.markdown(
        md_text or "",
        extensions=["extra", "sane_lists", "tables", "fenced_code", "nl2br"],
        output_format="html",
    )


def sanitize_html(html: str) -> str:
    """Strip scripts / event handlers / dangerous URLs (and images) from rendered
    HTML, so a Doc embedding raw HTML can't inject anything into the published page."""
    return nh3.clean(html, tags=_ALLOWED_TAGS)


def load_template(template: Optional[str]) -> str:
    """Resolve a template: a built-in theme name, a path to a custom HTML file, or
    the default. The template uses ``{{ title }}`` and ``{{ content }}`` markers."""
    name = (template or "default").strip()
    if name and (os.sep in name or name.endswith(".html")) and Path(os.path.expanduser(name)).is_file():
        return Path(os.path.expanduser(name)).read_text(encoding="utf-8")
    builtin = THEMES_DIR / f"{name}.html"
    if builtin.is_file():
        return builtin.read_text(encoding="utf-8")
    return (THEMES_DIR / "default.html").read_text(encoding="utf-8")


def apply_template(template_html: str, title: str, content_html: str) -> str:
    """Substitute ``{{ title }}`` / ``{{ content }}`` (with or without spaces).

    The title is HTML-escaped (it lands in <title>/<h1>); the content is already
    sanitized HTML and is inserted verbatim.
    """
    safe_title = html_mod.escape(title or "Untitled")
    out = template_html
    for marker in ("{{ title }}", "{{title}}"):
        out = out.replace(marker, safe_title)
    for marker in ("{{ content }}", "{{content}}"):
        out = out.replace(marker, content_html)
    return out


def build_page(md_text: str, title: str, template: Optional[str]) -> tuple:
    """Full Doc-Markdown → published HTML page. Returns ``(html, image_warning)``."""
    stripped, n_imgs = strip_images(md_text)
    content = sanitize_html(render_markdown(stripped))
    page = apply_template(load_template(template), title, content)
    warn = (f"{n_imgs} image(s) not published (images aren't supported yet)"
            if n_imgs else None)
    return page, warn


# ---------------------------------------------------------------------------
# Safe git push (app-owned branch only)
# ---------------------------------------------------------------------------

def _git_env() -> dict:
    """Non-interactive git: fail fast on auth instead of hanging the worker on a
    credential/passphrase prompt."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


def _git(cwd: Optional[Path], *args: str, check: bool = True) -> subprocess.CompletedProcess:
    base = ["git"] + (["-C", str(cwd)] if cwd else [])
    return subprocess.run(base + list(args), check=check, capture_output=True,
                          text=True, env=_git_env(), timeout=GIT_TIMEOUT)


def _fail(action: str, res: subprocess.CompletedProcess) -> PublishError:
    msg = (res.stderr or res.stdout or "").strip().splitlines()
    return PublishError(f"git {action} failed: {msg[-1] if msg else res.returncode}")


def workdir_for(repo: str, branch: str) -> Path:
    key = hashlib.sha1(f"{repo}\x00{branch}".encode("utf-8")).hexdigest()[:16]
    return PUBLISH_DIR / key


def _checkout_branch(workdir: Path, branch: str) -> None:
    """Put the working copy on ``branch``: track the remote branch if it exists,
    else create a clean **orphan** branch (so a dedicated pages branch doesn't
    inherit the default branch's history/files)."""
    _git(workdir, "fetch", "origin", branch, check=False)
    if _git(workdir, "rev-parse", "--verify", f"origin/{branch}", check=False).returncode == 0:
        if _git(workdir, "checkout", "-B", branch, f"origin/{branch}", check=False).returncode != 0:
            raise _fail("checkout", _git(workdir, "checkout", "-B", branch, f"origin/{branch}", check=False))
    elif _git(workdir, "rev-parse", "--verify", branch, check=False).returncode == 0:
        _git(workdir, "checkout", branch)
    else:
        _git(workdir, "checkout", "--orphan", branch)
        _git(workdir, "rm", "-rf", "--", ".", check=False)   # clean tree from default checkout


def _ensure_clone(workdir: Path, repo: str, branch: str) -> None:
    """Ensure an app-owned working copy of ``repo`` exists at ``workdir`` on ``branch``."""
    if (workdir / ".git").is_dir():
        cur = _git(workdir, "remote", "get-url", "origin", check=False).stdout.strip()
        if cur == repo:
            _checkout_branch(workdir, branch)
            return
        shutil.rmtree(workdir, ignore_errors=True)   # remote changed — start fresh

    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(workdir, ignore_errors=True)
    # Prefer cloning just the target branch; fall back to a full clone if the
    # branch doesn't exist remotely yet (first publish to a fresh pages branch).
    res = _git(None, "clone", "--branch", branch, "--single-branch", repo, str(workdir), check=False)
    if res.returncode != 0:
        res2 = _git(None, "clone", repo, str(workdir), check=False)
        if res2.returncode != 0:
            raise _fail("clone", res2)
        _checkout_branch(workdir, branch)


def _push(workdir: Path, branch: str) -> None:
    """Push the app-owned branch. On rejection (it moved), re-fetch and
    ``--force-with-lease`` — only ever on this branch, never the user's main."""
    res = _git(workdir, "push", "origin", branch, check=False)
    if res.returncode == 0:
        return
    _git(workdir, "fetch", "origin", branch, check=False)
    res2 = _git(workdir, "push", "--force-with-lease", "origin", branch, check=False)
    if res2.returncode != 0:
        raise _fail("push", res2)


def git_publish(repo: str, branch: str, files: dict, message: str) -> Optional[str]:
    """Write ``files`` (relative path -> bytes) into the app-owned working copy of
    ``repo``/``branch``, commit, and push. Returns the short commit hash, or
    ``None`` if nothing changed. Raises :class:`PublishError` on git failure.

    The working tree of the app-owned branch is wholly ours, so we ``add -A``
    (picking up deletions too) — but we never touch any other branch.
    """
    if not repo or not branch:
        raise PublishError("publish target needs both 'repo' and 'branch'")
    workdir = workdir_for(repo, branch)
    _ensure_clone(workdir, repo, branch)
    _git(workdir, "pull", "--rebase", "origin", branch, check=False)  # best-effort fast-forward

    for rel, data in files.items():
        dest = workdir / rel
        if PUBLISH_DIR not in dest.resolve().parents and dest.resolve() != (workdir / rel).resolve():
            raise PublishError(f"unsafe publish path: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

    _git(workdir, "add", "-A")
    if _git(workdir, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None  # nothing changed — not an error
    _git(workdir, "-c", "user.name=DocToPDF", "-c", "user.email=doctopdf@localhost",
         "commit", "-q", "-m", message)
    _push(workdir, branch)
    return _git(workdir, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or None


# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------

def _require(target: dict, *keys) -> None:
    missing = [k for k in keys if not target.get(k)]
    if missing:
        raise PublishError(f"publish target missing: {', '.join(missing)}")


def _commit_message(name: str) -> str:
    import time
    return f"Publish {name} — {time.strftime('%Y-%m-%d %H:%M:%S')}"


def publish_git_markdown(target: dict, name: str, md_text: str) -> dict:
    """Write the raw Markdown snapshot to the target repo and push."""
    _require(target, "repo", "branch")
    path = target.get("path") or f"{_slug(name)}.md"
    commit = git_publish(target["repo"], target["branch"],
                         {path: md_text.encode("utf-8")}, _commit_message(name))
    return {"commit": commit, "url": target.get("site_url"), "warning": None,
            "status": "unchanged" if commit is None else "published"}


def publish_git_pages(target: dict, name: str, md_text: str) -> dict:
    """Render the Markdown to a themed, sanitized HTML page and push it."""
    _require(target, "repo", "branch")
    path = target.get("path") or "index.html"
    page, warn = build_page(md_text, name, target.get("template"))
    commit = git_publish(target["repo"], target["branch"],
                         {path: page.encode("utf-8")}, _commit_message(name))
    return {"commit": commit, "url": target.get("site_url"), "warning": warn,
            "status": "unchanged" if commit is None else "published"}


def html_to_pdf(html: str) -> bytes:
    """Render an HTML string to PDF bytes via Playwright Chromium (reused from web
    monitoring). Runs synchronously on the caller's worker thread."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PublishError("Playwright not installed — run: pip install playwright "
                           "&& playwright install chromium") from exc
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                return page.pdf(format="A4", print_background=True,
                                margin={"top": "18mm", "bottom": "18mm",
                                        "left": "16mm", "right": "16mm"})
            finally:
                browser.close()
    except PublishError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PublishError(f"PDF render failed: {exc}") from exc


def publish_pdf_template(target: dict, name: str, md_text: str) -> dict:
    """Render Markdown → branded HTML template → PDF (Playwright). Write to the
    output dir, and also push if a repo/branch is configured."""
    page, warn = build_page(md_text, name, target.get("template"))
    pdf = html_to_pdf(page)
    out_path = None
    out_dir = target.get("output_dir")
    if out_dir:
        d = Path(os.path.expanduser(str(out_dir)))
        d.mkdir(parents=True, exist_ok=True)
        out_path = d / (target.get("path") or f"{_slug(name)}.pdf")
        tmp = out_path.with_name(out_path.name + ".tmp")
        tmp.write_bytes(pdf)
        os.replace(tmp, out_path)
    commit = None
    if target.get("repo") and target.get("branch"):
        commit = git_publish(target["repo"], target["branch"],
                             {target.get("path") or f"{_slug(name)}.pdf": pdf},
                             _commit_message(name))
    return {"commit": commit, "url": str(out_path) if out_path else target.get("site_url"),
            "warning": warn, "status": "published"}


PUBLISHERS.update({
    "git_markdown": publish_git_markdown,
    "git_pages": publish_git_pages,
    "pdf_template": publish_pdf_template,
})

# --- Future destination stubs (extension point — do NOT implement here) -----
# To add Notion / Confluence / a generic webhook-or-CMS POST destination, write a
# ``publish_<type>(target, name, md_text) -> {commit,url,warning,status}`` function
# and register it in PUBLISHERS under its ``type`` key. The change-event wiring,
# approval gate, status, and retries in app.py are destination-agnostic and need
# no changes.
# PUBLISHERS["notion"] = publish_notion        # TODO
# PUBLISHERS["webhook"] = publish_webhook       # TODO


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "page").strip()).strip("-")
    return s or "page"


def target_key(target: dict) -> str:
    """Stable id for a publish target (for status/menu/dedup)."""
    raw = "\x00".join(str(target.get(k, "")) for k in
                      ("source_id", "type", "repo", "branch", "path"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def publish(target: dict, name: str, md_text: str) -> dict:
    """Dispatch to the publisher for ``target['type']``. Raises :class:`PublishSkip`
    on an empty snapshot (never publish a blank page / wipe a live one) and
    :class:`PublishError` on failure."""
    if not (md_text and md_text.strip()):
        raise PublishSkip("empty snapshot — nothing to publish")
    ptype = target.get("type") or "git_markdown"
    fn = PUBLISHERS.get(ptype)
    if fn is None:
        raise PublishError(f"unknown publish type: {ptype}")
    return fn(target, name, md_text)
