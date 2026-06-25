"""DocToPDF menu-bar app: status item, menu, and the watch loop.

Threading model
---------------
Network + file I/O must never block the Cocoa run loop, so all Drive work runs
on a dedicated background worker thread. The worker only mutates a lock-guarded
``self._state`` snapshot; it never touches rumps/AppKit objects. A lightweight
``rumps.Timer`` (1 s, main thread) reads that snapshot and updates the menu and
status-item title. This keeps every UI mutation on the main thread while keeping
it responsive during multi-second exports and the interactive auth flow.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import rumps

from . import config, drive
from .drive import AuthFlowError, DriveError, ReauthRequired

# Status-item titles per state.
ICON_IDLE = "📄"
ICON_EXPORTING = "🔄"
ICON_ERROR = "⚠️"
ICON_PAUSED = "⏸"

MIN_INTERVAL = 3      # never poll faster than this, whatever the config says
MAX_INTERVAL = 60     # backoff ceiling
MAX_NAME_LEN = 200    # filesystem-friendly cap


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
    # A name made only of dots/spaces (e.g. "." or "..") would be a special
    # directory entry, not a file — reject it so the caller falls back to the id.
    if not name.strip(". "):
        return ""
    return name[:MAX_NAME_LEN]


class DocToPDFApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("DocToPDF", title=ICON_IDLE, quit_button=None)

        self._config = config.load_config()
        base = self._config.get("poll_interval", config.DEFAULT_CONFIG["poll_interval"])
        try:
            self._base_interval = max(MIN_INTERVAL, int(base))
        except (TypeError, ValueError):
            self._base_interval = config.DEFAULT_CONFIG["poll_interval"]

        # --- shared state (guarded by _lock) -------------------------------
        self._lock = threading.RLock()
        self.doc_id = self._config.get("doc_id")
        self._last_modified = None          # last seen Drive modifiedTime
        self._interval = self._base_interval
        self._paused = False
        self._force = False                 # force an export next cycle
        self._auth_blocked = False          # interactive auth failed; await user action
        self._state = {
            "kind": "starting",             # starting|authorizing|watching|exporting|error|needs_doc|paused
            "doc_name": None,
            "last_export_time": None,
            "error_msg": None,
            "last_pdf_path": None,
        }

        # --- worker plumbing ----------------------------------------------
        self.creds = None
        self.service = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, name="watch", daemon=True)

        # --- menu ----------------------------------------------------------
        self._mi_status = rumps.MenuItem("Starting…")        # disabled (no callback)
        self._mi_last = rumps.MenuItem("Last export: —")     # disabled
        self._mi_export = rumps.MenuItem("Export now", callback=self.on_export_now)
        self._mi_open = rumps.MenuItem("Open PDF", callback=self.on_open_pdf)
        self._mi_reveal = rumps.MenuItem("Reveal in Finder", callback=self.on_reveal)
        self._mi_pause = rumps.MenuItem("Pause", callback=self.on_toggle_pause)
        self._mi_setdoc = rumps.MenuItem("Set Google Doc…", callback=self.on_set_doc)
        self._mi_quit = rumps.MenuItem("Quit", callback=self.on_quit)
        self.menu = [
            self._mi_status,
            self._mi_last,
            None,
            self._mi_export,
            self._mi_open,
            self._mi_reveal,
            None,
            self._mi_pause,
            self._mi_setdoc,
            None,
            self._mi_quit,
        ]

        self._cur_title = ICON_IDLE
        self._ui_timer = rumps.Timer(self._refresh_ui, 1)

    # ------------------------------------------------------------------ run
    def start(self) -> None:
        self._worker.start()
        self._ui_timer.start()
        self.run()

    # ----------------------------------------------------------- state util
    def _update_state(self, **kwargs) -> None:
        with self._lock:
            self._state.update(kwargs)

    def _set_error(self, msg: str) -> None:
        self._update_state(kind="error", error_msg=msg)

    def _reset_interval(self) -> None:
        with self._lock:
            self._interval = self._base_interval

    def _backoff(self) -> None:
        with self._lock:
            # Ceiling is at least the configured interval, so a base poll interval
            # set above MAX_INTERVAL never gets *sped up* by backoff.
            ceiling = max(MAX_INTERVAL, self._base_interval)
            self._interval = min(ceiling, max(self._base_interval, self._interval) * 2)

    def _sleep_or_wake(self, timeout: float) -> None:
        if self._wake.wait(timeout):
            self._wake.clear()

    # -------------------------------------------------------------- worker
    def _run_worker(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                paused = self._paused
                doc_id = self.doc_id
                blocked = self._auth_blocked
                interval = self._interval
                force_pending = self._force      # peeked, NOT consumed yet

            # Authorize as early as possible (independent of whether a doc is set),
            # so the app prompts on first launch and sits ready in the menu bar.
            # Skip auth only when idle-paused with nothing explicitly queued, so we
            # don't pop a browser while the user has it paused.
            if self.service is None and not (paused and not force_pending):
                if blocked:
                    if paused:
                        self._update_state(kind="paused")
                    self._sleep_or_wake(interval)
                    continue
                if not config.CLIENT_SECRET_PATH.exists():
                    self._set_error("Missing client_secret.json — see README.")
                    self._sleep_or_wake(max(interval, 10))
                    continue
                if not self._authorize():        # sets state/flags on failure
                    self._sleep_or_wake(interval)
                    continue
                with self._lock:
                    self._force = True           # fresh auth → export immediately
                    force_pending = True

            # Paused with nothing forced: idle. A queued "Export now" still runs
            # one export below, without changing the paused state.
            if paused and not force_pending:
                self._update_state(kind="paused")
                self._sleep_or_wake(1.0)
                continue

            if not doc_id:
                self._update_state(kind="needs_doc")
                self._sleep_or_wake(max(interval, 2))
                continue

            # Consume the force flag only now that we will actually poll — so a
            # request made while paused / unauthorized / doc-less is never lost.
            with self._lock:
                force = self._force
                self._force = False

            try:
                self._poll_once(doc_id, force)
                self._reset_interval()
            except AuthFlowError as exc:
                with self._lock:
                    self._auth_blocked = True
                self._set_error(str(exc))
            except ReauthRequired as exc:
                # Credentials went stale mid-session. Drop the service so the next
                # cycle re-runs auth (silent refresh, or interactive on failure).
                self.service = None
                self.creds = None
                self._set_error(str(exc))
            except DriveError as exc:
                self._set_error(str(exc))
                self._backoff()
            except Exception as exc:  # noqa: BLE001 — never let the worker die
                self._set_error(f"Unexpected: {exc}")
                self._backoff()

            with self._lock:
                interval = self._interval
                if self._paused:
                    self._update_state(kind="paused")
            self._sleep_or_wake(interval)

    def _authorize(self) -> bool:
        """Acquire credentials/service. Returns True on success."""
        self._update_state(kind="authorizing")
        try:
            self.creds = drive.get_credentials()
            self.service = drive.build_service(self.creds)
            return True
        except AuthFlowError as exc:
            with self._lock:
                self._auth_blocked = True
            self._set_error(str(exc))
            return False
        except DriveError as exc:
            self._set_error(str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"Auth error: {exc}")
            return False

    def _poll_once(self, doc_id: str, force: bool) -> None:
        meta = drive.get_file_metadata(self.service, doc_id)
        drive.maybe_persist_refreshed_token(self.creds)

        name = meta.get("name") or doc_id
        modified = meta.get("modifiedTime")
        mime = meta.get("mimeType", "")
        self._update_state(doc_name=name)

        if mime and not mime.startswith("application/vnd.google-apps"):
            raise DriveError("Not an exportable Google Doc.")

        changed = force or (modified != self._last_modified)
        if not changed:
            self._update_state(kind="watching", error_msg=None)
            return

        self._update_state(kind="exporting", error_msg=None)
        data = drive.export_pdf(self.service, doc_id)
        path = self._write_pdf(name, doc_id, data)

        # The metadata fetch + export are unlocked network calls; if the user
        # switched docs mid-cycle, don't clobber the new doc's baseline/status
        # with this (now-stale) result. on_set_doc already reset things under
        # the lock and queued a fresh forced poll.
        with self._lock:
            if self.doc_id != doc_id:
                return
            self._last_modified = modified
        self._update_state(
            kind="watching",
            error_msg=None,
            last_export_time=time.strftime("%H:%M:%S"),
            last_pdf_path=str(path),
        )

    def _write_pdf(self, name: str, doc_id: str, data: bytes) -> Path:
        out_dir = config.resolve_output_dir(self._config)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = sanitize_filename(name) or doc_id
        if self._config.get("timestamped"):
            fname = f"{safe} {time.strftime('%Y-%m-%d %H%M%S')}.pdf"
        else:
            fname = f"{safe}.pdf"
        path = out_dir / fname
        tmp = out_dir / (fname + ".part")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)  # atomic overwrite — no half-written PDF on Desktop
        except BaseException:
            # On any failure (disk full, interrupt) don't leave a .part behind.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    # ----------------------------------------------------------- UI refresh
    def _refresh_ui(self, _timer) -> None:
        with self._lock:
            st = dict(self._state)
            paused = self._paused

        kind = st["kind"]
        name = st["doc_name"]
        err = st["error_msg"]

        # Status item + title icon.
        if paused:
            title, status = ICON_PAUSED, "Paused"
        elif kind == "error":
            title, status = ICON_ERROR, f"Error: {err}" if err else "Error"
        elif kind == "needs_doc":
            title, status = ICON_IDLE, "No Doc set — choose one"
        elif kind == "authorizing":
            title, status = ICON_IDLE, "Authorizing in browser…"
        elif kind == "exporting":
            title, status = ICON_EXPORTING, f"Exporting: {name or '…'}"
        elif kind == "watching":
            title, status = ICON_IDLE, f"Watching: {name or '…'}"
        else:  # starting
            title, status = ICON_IDLE, "Starting…"

        if title != self._cur_title:
            self.title = title
            self._cur_title = title
        self._mi_status.title = status

        last = st["last_export_time"]
        self._mi_last.title = f"Last export: {last}" if last else "Last export: —"
        self._mi_pause.title = "Resume" if paused else "Pause"

    # ------------------------------------------------------------- actions
    def on_set_doc(self, _sender) -> None:
        win = rumps.Window(
            message="Paste a Google Doc URL or ID:",
            title="Set Google Doc",
            default_text=self.doc_id or "",
            ok="Set",
            cancel="Cancel",
            dimensions=(360, 24),
        )
        response = win.run()
        if not response.clicked:
            return
        doc_id = drive.parse_doc_id(response.text)
        if not doc_id:
            rumps.alert("Invalid Doc", "Couldn't find a Google Doc ID in that text.")
            return
        with self._lock:
            self.doc_id = doc_id
            self._config["doc_id"] = doc_id
            self._last_modified = None
            self._force = True
            self._auth_blocked = False
            self._interval = self._base_interval
            self._state["doc_name"] = None
            self._state["error_msg"] = None
        config.save_config(self._config)
        self._wake.set()

    def on_export_now(self, _sender) -> None:
        if not self.doc_id:
            rumps.alert("No Doc", "Set a Google Doc first.")
            return
        with self._lock:
            self._force = True
            self._auth_blocked = False
            self._interval = self._base_interval
        self._wake.set()

    def on_open_pdf(self, _sender) -> None:
        path = self._current_pdf_path()
        if path and path.exists():
            subprocess.run(["open", str(path)], check=False)
        else:
            rumps.alert("No PDF yet", "Nothing has been exported yet.")

    def on_reveal(self, _sender) -> None:
        path = self._current_pdf_path()
        if path and path.exists():
            subprocess.run(["open", "-R", str(path)], check=False)
        else:
            rumps.alert("No PDF yet", "Nothing has been exported yet.")

    def on_toggle_pause(self, _sender) -> None:
        with self._lock:
            self._paused = not self._paused
            if not self._paused:
                self._auth_blocked = False  # resuming counts as a retry
        self._wake.set()

    def on_quit(self, _sender) -> None:
        self._stop.set()
        self._wake.set()
        # The worker is a daemon thread and may be parked in a blocking network
        # call or the interactive auth server, so only briefly yield to let an
        # in-flight file write finish — never freeze the menu waiting on it.
        self._worker.join(timeout=0.5)
        rumps.quit_application()

    # -------------------------------------------------------------- helpers
    def _current_pdf_path(self):
        with self._lock:
            p = self._state.get("last_pdf_path")
        return Path(p) if p else None


def main() -> None:
    DocToPDFApp().start()


if __name__ == "__main__":
    main()
