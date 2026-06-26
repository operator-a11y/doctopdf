"""Export pipeline for DocToPDF: multi-format export, output writing (overwrite /
timestamped / rolling-last-N), git version history with a text snapshot, and a
post-export shell hook.

Everything here is plain and synchronous so it runs on the background worker
thread. It never touches AppKit. ``run_export`` is the single entry point.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import config, drive

# Google Docs export targets: format key -> (export MIME type, file extension).
GOOGLE_PREFIX = "application/vnd.google-apps."

# Per Google-native type, the export targets: format key -> (export MIME, ext).
# A file's type is derived from its mimeType (document/spreadsheet/presentation/
# drawing). "pdf" works for all; type-specific formats only apply to that type.
FORMATS_BY_TYPE: dict[str, dict[str, tuple[str, str]]] = {
    "document": {
        "pdf": ("application/pdf", "pdf"),
        "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
        "odt": ("application/vnd.oasis.opendocument.text", "odt"),
        "rtf": ("application/rtf", "rtf"),
        "txt": ("text/plain", "txt"),
        "html": ("text/html", "html"),
        "md": ("text/markdown", "md"),
        "epub": ("application/epub+zip", "epub"),
    },
    "spreadsheet": {
        "pdf": ("application/pdf", "pdf"),
        "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
        "ods": ("application/vnd.oasis.opendocument.spreadsheet", "ods"),
        "csv": ("text/csv", "csv"),
        "tsv": ("text/tab-separated-values", "tsv"),
    },
    "presentation": {
        "pdf": ("application/pdf", "pdf"),
        "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
        "odp": ("application/vnd.oasis.opendocument.presentation", "odp"),
        "txt": ("text/plain", "txt"),
    },
    "drawing": {
        "pdf": ("application/pdf", "pdf"),
        "png": ("image/png", "png"),
        "jpg": ("image/jpeg", "jpg"),
        "svg": ("image/svg+xml", "svg"),
    },
}

# Sensible default formats per type when the config requests none valid for it.
DEFAULT_FORMATS_BY_TYPE = {
    "document": ["pdf"], "spreadsheet": ["pdf"],
    "presentation": ["pdf"], "drawing": ["pdf"],
}

# Formats that diff well as plain text (used for the git snapshot), per type.
TEXT_FORMATS = ("md", "txt", "html", "csv", "tsv")

# All export-format keys (any type) — for surfacing/validation.
EXPORT_FORMATS = {f for table in FORMATS_BY_TYPE.values() for f in table}

MAX_NAME_LEN = 200
HOOK_TIMEOUT = 120  # seconds


def google_type(mime_type: Optional[str]) -> Optional[str]:
    """Short Google-native type ('document'/'spreadsheet'/...) or None."""
    if mime_type and mime_type.startswith(GOOGLE_PREFIX):
        return mime_type[len(GOOGLE_PREFIX):]
    return None


def is_exportable(mime_type: Optional[str]) -> bool:
    """True if this Google file type can be exported (has a format table)."""
    return google_type(mime_type) in FORMATS_BY_TYPE


def sanitize_filename(name: str) -> str:
    """Make a doc name safe to use as a filename on macOS.

    Strips path separators, the historically-awkward ``:``, and control chars,
    collapses whitespace, and trims leading/trailing dots. Returns ``""`` if
    nothing usable remains (the caller falls back to the doc id).
    """
    if not name:
        return ""
    name = name.replace("/", "-").replace(":", "-").replace("\\", "-")
    name = "".join(ch for ch in name if ord(ch) >= 32)  # drop control chars
    name = " ".join(name.split())                       # collapse whitespace
    name = name.strip().strip(".").strip()
    if not name.strip(". "):
        return ""
    return name[:MAX_NAME_LEN]


def resolve_formats(cfg: dict, gtype: str = "document") -> list[str]:
    """Return the de-duplicated output formats valid for ``gtype`` (never empty).

    The configured ``formats`` may list formats for several types at once (e.g.
    ['pdf', 'docx', 'xlsx']); each file keeps only those valid for its own type,
    so a combined list "just works" across Docs/Sheets/Slides. Falls back to the
    type's default when none of the requested formats apply.
    """
    table = FORMATS_BY_TYPE.get(gtype, {"pdf": ("application/pdf", "pdf")})
    raw = cfg.get("formats") or []
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        raw = []
    seen, out = set(), []
    for f in raw:
        f = str(f).lower().lstrip(".")
        if f in table and f not in seen:
            seen.add(f)
            out.append(f)
    return out or list(DEFAULT_FORMATS_BY_TYPE.get(gtype, ["pdf"]))


# ---------------------------------------------------------------------------
# Atomic file writing
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_output(out_dir: Path, base: str, ext: str, data: bytes,
                 timestamped: bool, keep_n: int) -> Path:
    """Write one export to the output dir, honoring overwrite/timestamped/rolling.

    - ``keep_n > 0`` (rolling) or ``timestamped`` → ``<base> <YYYY-MM-DD HHMMSS>.<ext>``.
    - otherwise → ``<base>.<ext>`` (overwrite).
    Rolling additionally prunes to the newest ``keep_n`` files for this base/ext.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rolling = keep_n > 0
    if timestamped or rolling:
        stamp = time.strftime("%Y-%m-%d %H%M%S")
        path = out_dir / f"{base} {stamp}.{ext}"
        # Two exports in the same wall-clock second would otherwise overwrite each
        # other — disambiguate with a counter so no version is lost.
        n = 2
        while path.exists():
            path = out_dir / f"{base} {stamp}-{n}.{ext}"
            n += 1
    else:
        path = out_dir / f"{base}.{ext}"
    _atomic_write(path, data)
    if rolling:
        _prune_versions(out_dir, base, ext, keep_n)
    return path


def _version_pattern(base: str, ext: str) -> "re.Pattern[str]":
    """Match exactly this base/ext's timestamped versions: ``<base> <ts>[-N].<ext>``.

    Anchored so a different doc whose name merely *starts with* ``base`` (e.g.
    'Report Q3' vs 'Report') is never matched — which would otherwise let one
    doc's prune silently delete another doc's version history.
    """
    return re.compile(
        rf"^{re.escape(base)} \d{{4}}-\d{{2}}-\d{{2}} \d{{6}}(?:-\d+)?\.{re.escape(ext)}$"
    )


def _prune_versions(out_dir: Path, base: str, ext: str, keep_n: int) -> None:
    pattern = _version_pattern(base, ext)
    try:
        versions = [p for p in out_dir.iterdir() if pattern.match(p.name)]
    except OSError:
        return
    versions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in versions[keep_n:]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Git version history
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check, capture_output=True, text=True,
    )


def commit_history(repo_dir: Path, doc_name: str, files: dict[str, bytes]) -> Optional[str]:
    """Write ``files`` (filename -> bytes) into a git repo and commit them.

    Uses stable filenames (overwriting) so git tracks each export as one evolving
    file with real diffs. Returns the short commit hash, or ``None`` if nothing
    changed. Raises on git failure (caller treats it as a non-fatal warning).
    """
    repo = Path(os.path.expanduser(str(repo_dir)))
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").is_dir():
        _git(repo, "init", "-q")

    names = list(files)
    for fname, data in files.items():
        _atomic_write(repo / fname, data)

    # Scope every operation to ONLY our snapshot files, so a shared/existing repo
    # (or a git_repo that contains the output dir) never sweeps unrelated files
    # into our commits and our "nothing changed" check stays accurate.
    _git(repo, "add", "--", *names)
    if _git(repo, "diff", "--cached", "--quiet", "--", *names, check=False).returncode == 0:
        return None  # our files are unchanged — not an error, just no new version
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _git(
        repo,
        "-c", "user.name=DocToPDF",
        "-c", "user.email=doctopdf@localhost",
        "commit", "-q", "-m", f"{doc_name} — {ts}", "--", *names,
    )
    return _git(repo, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or None


# ---------------------------------------------------------------------------
# Post-export hook
# ---------------------------------------------------------------------------


def run_hook(cmd: str, primary: Optional[Path], doc_name: str, files: list[Path]) -> None:
    """Run the user's post-export command **fire-and-forget** on a daemon thread.

    ``$1`` and ``$DOCTOPDF_PRIMARY`` are the primary file path; ``$DOCTOPDF_FILES``
    lists all written files; ``$DOCTOPDF_DOC_NAME`` is the doc name. The command
    runs detached from the watch loop (with stdio to /dev/null and a hard
    timeout), so a slow or hanging hook never stalls polling.
    """
    primary_s = str(primary) if primary else ""
    env = os.environ.copy()
    env["DOCTOPDF_DOC_NAME"] = doc_name or ""
    env["DOCTOPDF_PRIMARY"] = primary_s
    env["DOCTOPDF_FILES"] = "\n".join(str(p) for p in files)

    def _run() -> None:
        try:
            subprocess.run(
                ["/bin/sh", "-c", cmd, "doctopdf", primary_s],
                env=env, check=False, timeout=HOOK_TIMEOUT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001 — fire-and-forget; never affect watching
            pass

    threading.Thread(target=_run, name="doctopdf-hook", daemon=True).start()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_export(cfg: dict, service, file_id: str, name: str, gtype: str = "document") -> dict:
    """Export the file in all configured formats valid for ``gtype``, write
    outputs, optionally commit to git history and run the post-export hook.

    Returns ``{primary, written: {fmt: Path}, commit, warning}``. Format export
    and output writing raise on failure (the caller treats those as real export
    errors); git/hook problems are caught and returned as ``warning``.
    """
    table = FORMATS_BY_TYPE.get(gtype, {"pdf": ("application/pdf", "pdf")})
    formats = resolve_formats(cfg, gtype)
    base = sanitize_filename(name) or file_id
    out_dir = config.resolve_output_dir(cfg)
    timestamped = bool(cfg.get("timestamped", False))
    try:
        keep_n = max(0, int(cfg.get("keep_versions", 0) or 0))
    except (TypeError, ValueError):
        keep_n = 0
    git_repo = cfg.get("git_repo")

    # Decide every format we must export: the requested outputs, plus a text
    # format (valid for this type) so git history, AI summaries, and change
    # detection (for alerts/digests) have real text to diff. The audit log
    # (Change history dashboard) is always present, so by default we always
    # capture text — otherwise a plain PDF-only setup records nothing to diff
    # and the dashboard stays permanently empty.
    want_text = bool(
        (git_repo and cfg.get("git_snapshot_text", True))
        or cfg.get("ai_summary")
        or cfg.get("webhook_urls")
        or cfg.get("email_to")
        or (cfg.get("digest", "off") not in (None, "off"))
        or cfg.get("audit_log", True)
    )
    needed = list(formats)
    if want_text and not any(f in TEXT_FORMATS for f in needed):
        # Prefer by TEXT_FORMATS order (md for Docs, csv for Sheets, …).
        snap = next((f for f in TEXT_FORMATS if f in table), None)
        if snap:
            needed.append(snap)

    blobs: dict[str, tuple[str, bytes]] = {}
    for fmt in needed:
        mime, ext = table[fmt]
        blobs[fmt] = (ext, drive.export(service, file_id, mime))

    # Plain-text form of the doc (first text format exported), for AI summaries.
    text_fmt = next((f for f in needed if f in TEXT_FORMATS), None)
    text = blobs[text_fmt][1].decode("utf-8", "replace") if text_fmt else None

    written: dict[str, Path] = {}
    for fmt in formats:
        ext, data = blobs[fmt]
        written[fmt] = write_output(out_dir, base, ext, data, timestamped, keep_n)

    primary = written.get("pdf") or next(iter(written.values()), None)

    warning = None
    commit = None
    if git_repo:
        try:
            files = {f"{base}.{ext}": data for (ext, data) in blobs.values()}
            commit = commit_history(Path(os.path.expanduser(str(git_repo))), name, files)
        except Exception as exc:  # noqa: BLE001 — git is best-effort, never fail the export
            warning = f"Git history failed: {exc}"

    cmd = cfg.get("post_export_cmd")
    if cmd:
        try:
            run_hook(cmd, primary, name, list(written.values()))  # fire-and-forget
        except Exception as exc:  # noqa: BLE001 — couldn't even launch the hook
            warning = warning or f"Post-export hook couldn't start: {exc}"

    return {"primary": primary, "written": written, "commit": commit,
            "warning": warning, "text": text}
