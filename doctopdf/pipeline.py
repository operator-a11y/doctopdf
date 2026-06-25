"""Export pipeline for DocToPDF: multi-format export, output writing (overwrite /
timestamped / rolling-last-N), git version history with a text snapshot, and a
post-export shell hook.

Everything here is plain and synchronous so it runs on the background worker
thread. It never touches AppKit. ``run_export`` is the single entry point.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import config, drive

# Google Docs export targets: format key -> (export MIME type, file extension).
EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "pdf": ("application/pdf", "pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "odt": ("application/vnd.oasis.opendocument.text", "odt"),
    "rtf": ("application/rtf", "rtf"),
    "txt": ("text/plain", "txt"),
    "html": ("text/html", "html"),
    "md": ("text/markdown", "md"),
    "epub": ("application/epub+zip", "epub"),
}

# Formats that diff well as plain text (used for the git snapshot).
TEXT_FORMATS = ("md", "txt", "html")

MAX_NAME_LEN = 200
HOOK_TIMEOUT = 120  # seconds


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


def resolve_formats(cfg: dict) -> list[str]:
    """Return the validated, de-duplicated list of output formats (never empty)."""
    raw = cfg.get("formats") or ["pdf"]
    if isinstance(raw, str):
        raw = [raw]
    seen, out = set(), []
    for f in raw:
        f = str(f).lower().lstrip(".")
        if f in EXPORT_FORMATS and f not in seen:
            seen.add(f)
            out.append(f)
    return out or ["pdf"]


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
        fname = f"{base} {time.strftime('%Y-%m-%d %H%M%S')}.{ext}"
    else:
        fname = f"{base}.{ext}"
    path = out_dir / fname
    _atomic_write(path, data)
    if rolling:
        _prune_versions(out_dir, base, ext, keep_n)
    return path


def _prune_versions(out_dir: Path, base: str, ext: str, keep_n: int) -> None:
    prefix, suffix = f"{base} ", f".{ext}"
    try:
        versions = [
            p for p in out_dir.iterdir()
            if p.name.startswith(prefix) and p.name.endswith(suffix)
        ]
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

    for fname, data in files.items():
        _atomic_write(repo / fname, data)

    _git(repo, "add", "-A")
    # Nothing staged → nothing to commit (identical content); not an error.
    if not _git(repo, "status", "--porcelain").stdout.strip():
        return None
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _git(
        repo,
        "-c", "user.name=DocToPDF",
        "-c", "user.email=doctopdf@localhost",
        "commit", "-q", "-m", f"{doc_name} — {ts}",
    )
    return _git(repo, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or None


# ---------------------------------------------------------------------------
# Post-export hook
# ---------------------------------------------------------------------------


def run_hook(cmd: str, primary: Optional[Path], doc_name: str, files: list[Path]) -> None:
    """Run the user's post-export command. ``$1`` is the primary file path; also
    exposes DOCTOPDF_PRIMARY / DOCTOPDF_DOC_NAME / DOCTOPDF_FILES in the env."""
    primary_s = str(primary) if primary else ""
    env = os.environ.copy()
    env["DOCTOPDF_DOC_NAME"] = doc_name or ""
    env["DOCTOPDF_PRIMARY"] = primary_s
    env["DOCTOPDF_FILES"] = "\n".join(str(p) for p in files)
    subprocess.run(
        ["/bin/sh", "-c", cmd, "doctopdf", primary_s],
        env=env, check=False, timeout=HOOK_TIMEOUT,
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_export(cfg: dict, service, file_id: str, name: str) -> dict:
    """Export the doc in all configured formats, write outputs, optionally commit
    to git history and run the post-export hook.

    Returns ``{primary, written: {fmt: Path}, commit, warning}``. Format export
    and output writing raise on failure (the caller treats those as real export
    errors); git/hook problems are caught and returned as ``warning``.
    """
    formats = resolve_formats(cfg)
    base = sanitize_filename(name) or file_id
    out_dir = config.resolve_output_dir(cfg)
    timestamped = bool(cfg.get("timestamped", False))
    try:
        keep_n = max(0, int(cfg.get("keep_versions", 0) or 0))
    except (TypeError, ValueError):
        keep_n = 0
    git_repo = cfg.get("git_repo")

    # Decide every format we must export: the requested outputs, plus a text
    # format for the git snapshot so history has real diffs.
    needed = list(formats)
    if git_repo and cfg.get("git_snapshot_text", True):
        if not any(f in TEXT_FORMATS for f in needed):
            needed.append("md")

    blobs: dict[str, tuple[str, bytes]] = {}
    for fmt in needed:
        mime, ext = EXPORT_FORMATS[fmt]
        blobs[fmt] = (ext, drive.export(service, file_id, mime))

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
            run_hook(cmd, primary, name, list(written.values()))
        except Exception as exc:  # noqa: BLE001 — hook is best-effort
            warning = warning or f"Post-export hook failed: {exc}"

    return {"primary": primary, "written": written, "commit": commit, "warning": warning}
