"""DocToPDF menu-bar app: native AppKit status item, menu, and the watch loop.

Why native AppKit instead of rumps
----------------------------------
rumps configures its status item with APIs deprecated since macOS 10.10
(``setHighlightMode_``/``setTitle_``/``setImage_`` on ``NSStatusItem``) and its
``NSMenu`` does not render on macOS 26's ``NSSceneStatusItem`` — the icon shows
but the menu never drops down. This module drives ``NSStatusItem``/``NSMenu``
directly (title on the button, ``setMenu_`` for click-to-open), which works on
macOS 26.

Threading model
---------------
All Drive network/file I/O runs on a dedicated background worker thread that only
mutates a lock-guarded ``self._state`` snapshot — it never touches AppKit. A
repeating main-thread ``NSTimer`` (1 s) reads that snapshot and updates the menu
and status-item title. Every UI mutation stays on the main thread, and the menu
stays responsive during multi-second exports and the interactive auth flow.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSTextField,
    NSTimer,
    NSVariableStatusItemLength,
)
from Foundation import NSMakeRect, NSObject

from . import config, drive, pipeline
from .drive import AuthFlowError, DriveError, ReauthRequired
from .pipeline import sanitize_filename  # re-exported for convenience

# Status-item title: a visible text label (easy to find in a crowded menu bar)
# plus a leading state glyph. Pure emoji renders as a faint monochrome glyph
# that is hard to spot, so we keep the "DocToPDF" word always present.
LABEL = "DocToPDF"
GLYPH_IDLE = ""
GLYPH_EXPORTING = "🔄 "
GLYPH_ERROR = "⚠️ "
GLYPH_PAUSED = "⏸ "

MIN_INTERVAL = 3      # never poll faster than this, whatever the config says
MAX_INTERVAL = 60     # backoff ceiling


class DocToPDFController(NSObject):
    # ------------------------------------------------------------------ init
    def init(self):
        self = objc.super(DocToPDFController, self).init()
        if self is None:
            return None
        self._setup()
        return self

    @objc.python_method
    def _setup(self) -> None:
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
            "error_msg": None,              # fatal: shown with the ⚠️ menu-bar glyph
            "warning": None,                # non-fatal (git/hook): export still succeeded
            "last_pdf_path": None,
        }

        # --- worker plumbing ----------------------------------------------
        self.creds = None
        self.service = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, name="watch", daemon=True)

        self._cur_title = None
        self._build_status_item()

        self._worker.start()
        # Repeating main-thread timer renders state onto the menu.
        self._uitimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "refreshUI:", None, True
        )
        self.refreshUI_(None)

    @objc.python_method
    def _build_status_item(self) -> None:
        bar = NSStatusBar.systemStatusBar()
        self.statusitem = bar.statusItemWithLength_(NSVariableStatusItemLength)
        self.statusitem.button().setTitle_(LABEL)

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)  # we manage enabled state ourselves

        def disabled(title):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            item.setEnabled_(False)
            menu.addItem_(item)
            return item

        def action(title, selector):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
            item.setTarget_(self)
            menu.addItem_(item)
            return item

        self._mi_status = disabled("Starting…")
        self._mi_last = disabled("Last export: —")
        self._mi_warn = disabled("")        # non-fatal warning line, hidden unless set
        self._mi_warn.setHidden_(True)
        menu.addItem_(NSMenuItem.separatorItem())
        action("Export now", "onExportNow:")
        action("Open Export", "onOpenPDF:")
        action("Reveal in Finder", "onReveal:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._mi_pause = action("Pause", "onTogglePause:")
        action("Set Google Doc…", "onSetDoc:")
        menu.addItem_(NSMenuItem.separatorItem())
        action("Quit", "onQuit:")

        self.statusitem.setMenu_(menu)

    # ----------------------------------------------------------- state util
    @objc.python_method
    def _update_state(self, **kwargs) -> None:
        with self._lock:
            self._state.update(kwargs)

    @objc.python_method
    def _set_error(self, msg: str) -> None:
        self._update_state(kind="error", error_msg=msg)

    @objc.python_method
    def _reset_interval(self) -> None:
        with self._lock:
            self._interval = self._base_interval

    @objc.python_method
    def _backoff(self) -> None:
        with self._lock:
            # Ceiling is at least the configured interval, so a base poll interval
            # set above MAX_INTERVAL never gets *sped up* by backoff.
            ceiling = max(MAX_INTERVAL, self._base_interval)
            self._interval = min(ceiling, max(self._base_interval, self._interval) * 2)

    @objc.python_method
    def _sleep_or_wake(self, timeout: float) -> None:
        if self._wake.wait(timeout):
            self._wake.clear()

    # -------------------------------------------------------------- worker
    @objc.python_method
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

    @objc.python_method
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

    @objc.python_method
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
        # Export all configured formats, write outputs, and (if configured) commit
        # to git history and run the post-export hook.
        result = pipeline.run_export(self._config, self.service, doc_id, name)

        primary = result.get("primary")
        warning = result.get("warning")
        # Publish baseline + status atomically. The metadata fetch + export are
        # unlocked network calls; if the user switched docs mid-cycle, don't
        # clobber the new doc's baseline/status with this (now-stale) result —
        # onSetDoc already reset things and queued a fresh forced poll. git/hook
        # problems are NON-fatal: stay "watching" (no ⚠️ menu-bar glyph) and
        # surface them on a dedicated warning line.
        with self._lock:
            if self.doc_id != doc_id:
                self._state["kind"] = "watching"  # clear the transient exporting glyph
                return
            self._last_modified = modified
            self._state.update(
                kind="watching",
                error_msg=None,
                warning=warning,
                last_export_time=time.strftime("%H:%M:%S"),
                last_pdf_path=str(primary) if primary else None,
            )

    # ----------------------------------------------------------- UI refresh
    def refreshUI_(self, _timer) -> None:
        with self._lock:
            st = dict(self._state)
            paused = self._paused

        kind = st["kind"]
        name = st["doc_name"]
        err = st["error_msg"]

        if paused:
            title, status = GLYPH_PAUSED + LABEL, "Paused"
        elif kind == "error":
            title, status = GLYPH_ERROR + LABEL, f"Error: {err}" if err else "Error"
        elif kind == "needs_doc":
            title, status = GLYPH_IDLE + LABEL, "No Doc set — choose one"
        elif kind == "authorizing":
            title, status = GLYPH_IDLE + LABEL, "Authorizing in browser…"
        elif kind == "exporting":
            title, status = GLYPH_EXPORTING + LABEL, f"Exporting: {name or '…'}"
        elif kind == "watching":
            title, status = GLYPH_IDLE + LABEL, f"Watching: {name or '…'}"
        else:  # starting
            title, status = GLYPH_IDLE + LABEL, "Starting…"

        if title != self._cur_title:
            self.statusitem.button().setTitle_(title)
            self._cur_title = title
        self._mi_status.setTitle_(status)

        last = st["last_export_time"]
        self._mi_last.setTitle_(f"Last export: {last}" if last else "Last export: —")
        self._mi_pause.setTitle_("Resume" if paused else "Pause")

        # Non-fatal git/hook warning: shown on its own line, not as the error glyph.
        warn = st.get("warning")
        if warn:
            self._mi_warn.setTitle_(f"⚠️ {warn}")
            self._mi_warn.setHidden_(False)
        else:
            self._mi_warn.setHidden_(True)

    # ------------------------------------------------------------- actions
    def onSetDoc_(self, _sender) -> None:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Set Google Doc")
        alert.setInformativeText_("Paste a Google Doc URL or ID:")
        alert.addButtonWithTitle_("Set")
        alert.addButtonWithTitle_("Cancel")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 24))
        field.setStringValue_(self.doc_id or "")
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)

        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        doc_id = drive.parse_doc_id(field.stringValue())
        if not doc_id:
            self._alert("Invalid Doc", "Couldn't find a Google Doc ID in that text.")
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

    def onExportNow_(self, _sender) -> None:
        if not self.doc_id:
            self._alert("No Doc", "Set a Google Doc first.")
            return
        with self._lock:
            self._force = True
            self._auth_blocked = False
            self._interval = self._base_interval
        self._wake.set()

    def onOpenPDF_(self, _sender) -> None:
        path = self._current_pdf_path()
        if path and path.exists():
            subprocess.run(["open", str(path)], check=False)
        else:
            self._alert("No export yet", "Nothing has been exported yet.")

    def onReveal_(self, _sender) -> None:
        path = self._current_pdf_path()
        if path and path.exists():
            subprocess.run(["open", "-R", str(path)], check=False)
        else:
            self._alert("No export yet", "Nothing has been exported yet.")

    def onTogglePause_(self, _sender) -> None:
        with self._lock:
            self._paused = not self._paused
            if not self._paused:
                self._auth_blocked = False  # resuming counts as a retry
        self._wake.set()

    def onQuit_(self, _sender) -> None:
        self._stop.set()
        self._wake.set()
        # The worker is a daemon thread and may be parked in a blocking network
        # call or the interactive auth server, so only briefly yield to let an
        # in-flight file write finish — never freeze the menu waiting on it.
        self._worker.join(timeout=0.5)
        NSApplication.sharedApplication().terminate_(None)

    # -------------------------------------------------------------- helpers
    @objc.python_method
    def _current_pdf_path(self):
        with self._lock:
            p = self._state.get("last_pdf_path")
        return Path(p) if p else None

    @objc.python_method
    def _alert(self, title: str, message: str) -> None:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()


# Module-level strong reference so the controller isn't garbage-collected.
_CONTROLLER = None


def main() -> None:
    global _CONTROLLER
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _CONTROLLER = DocToPDFController.alloc().init()
    app.activateIgnoringOtherApps_(True)
    app.run()


if __name__ == "__main__":
    main()
