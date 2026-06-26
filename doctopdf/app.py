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
from collections import deque
from datetime import datetime
from pathlib import Path

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSTextField,
    NSTimer,
    NSVariableStatusItemLength,
)
from Foundation import NSMakeRect, NSObject

from . import alerts, config, digest, drive, launchagent, pipeline, prefs, summarize, web
from .drive import AuthFlowError, DriveError, ReauthRequired
from .pipeline import sanitize_filename  # re-exported for convenience

RECENT_MAX = 15  # entries kept in the "Recent exports" submenu

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
WEB_DEFAULT_INTERVAL = 600   # web targets poll far slower than docs — be polite
WEB_MIN_INTERVAL = 60        # floor for a web poll interval
WEB_BACKOFF_CAP = 3600       # max backoff for a bot-blocked site (1h)


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
        # Watch list of {id, output_dir?, formats?}. Migrate a legacy single
        # doc_id into it so existing configs keep working.
        self._watch = list(self._config.get("watch") or [])
        legacy = self._config.get("doc_id")
        if legacy and not any(e.get("id") == legacy for e in self._watch):
            self._watch.append({"id": legacy})
        if self._config.get("doc_id"):
            self._config["doc_id"] = None
            self._config["watch"] = self._watch
            config.save_config(self._config)
        self._modified: dict = {}           # file id -> last seen Drive modifiedTime
        self._prev_text: dict = {}          # target id/url -> last snapshot text (for diffs)
        self._inflight: set = set()         # doc names with a change being processed
        self._web_next: dict = {}           # web url -> monotonic time of next poll
        self._web_backoff: dict = {}        # web url -> current backoff seconds (bot-block)
        self._entry_names: dict = {}        # watch-entry id -> menu label
        self._watch_sig = None              # last-rendered watch submenu signature
        self._interval = self._base_interval
        self._paused = False
        self._force = False                 # force an export next cycle
        self._auth_blocked = False          # interactive auth failed; await user action
        self._state = {
            "kind": "starting",             # starting|authorizing|watching|exporting|error|needs_doc|paused
            "watch_count": 0,               # number of resolved targets
            "names": [],                    # resolved target display names
            "last_export_time": None,
            "error_msg": None,              # fatal: shown with the ⚠️ menu-bar glyph
            "warning": None,                # non-fatal (git/hook): export still succeeded
            "alert_warning": None,          # async alert/classify degradation (not poll-cleared)
            "last_summary": None,           # latest AI change summary (menu line)
            "last_pdf_path": None,
        }

        # Recent exports for the submenu (guarded by _lock; _recent_dirty tells
        # the UI timer to rebuild the submenu).
        self._recent: deque = deque(maxlen=RECENT_MAX)
        self._recent_dirty = True

        # --- worker plumbing ----------------------------------------------
        self.creds = None
        self.service = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, name="watch", daemon=True)

        self._cur_title = None
        self._prefs = None                  # retained Preferences window controller
        self._build_status_item()

        self._worker.start()
        # Repeating main-thread timer renders state onto the menu.
        self._uitimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "refreshUI:", None, True
        )
        # Periodic check for a due daily/weekly digest.
        self._digesttimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            300.0, self, "digestTick:", None, True
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
        self._mi_summary = disabled("")     # latest AI change summary, hidden unless set
        self._mi_summary.setHidden_(True)

        # "Watching ▸" submenu lists each watched doc/folder (click to remove).
        self._watch_menu = NSMenu.alloc().init()
        self._watch_menu.setAutoenablesItems_(False)
        self._mi_watch = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Watching", None, "")
        self._mi_watch.setSubmenu_(self._watch_menu)
        menu.addItem_(self._mi_watch)

        menu.addItem_(NSMenuItem.separatorItem())
        action("Export now", "onExportNow:")
        action("Open Export", "onOpenPDF:")
        action("Reveal in Finder", "onReveal:")

        # Recent-exports submenu (rebuilt by the UI timer from self._recent).
        self._recent_menu = NSMenu.alloc().init()
        self._recent_menu.setAutoenablesItems_(False)
        self._mi_recent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Recent exports", None, "")
        self._mi_recent.setSubmenu_(self._recent_menu)
        menu.addItem_(self._mi_recent)

        menu.addItem_(NSMenuItem.separatorItem())
        self._mi_pause = action("Pause", "onTogglePause:")
        action("Add Doc or Folder…", "onAddTarget:")
        action("Preferences…", "onPreferences:")
        self._mi_login = action("Launch at Login", "onToggleLogin:")
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
                watch = list(self._watch)
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

            if not watch:
                self._update_state(kind="needs_doc")
                self._sleep_or_wake(max(interval, 2))
                continue

            # Consume the force flag only now that we will actually poll — so a
            # request made while paused / unauthorized / doc-less is never lost.
            with self._lock:
                force = self._force
                self._force = False

            try:
                self._poll_all(watch, force)
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
    def _resolve_targets(self, watch: list):
        """Expand the watch list into concrete file targets (folders → their
        direct children, non-recursive). Returns ``(targets, errors)``.

        Each target is ``{id, name, modified, gtype, overrides}``. A single bad
        entry (deleted/unshared/network) is caught and reported, never aborting
        the others; ReauthRequired bubbles up so the worker can re-auth. Targets
        are de-duplicated by id, and colliding display names are disambiguated.
        """
        targets, entry_names, errors, seen = [], {}, [], set()

        def add(fid, name, modified, gtype, overrides):
            if fid in seen:
                return
            seen.add(fid)
            targets.append({"kind": "drive", "id": fid, "name": name or fid,
                            "modified": modified, "gtype": gtype, "overrides": overrides})

        for entry in watch:
            # Web target — no Drive call; the change signal is the content hash.
            if entry.get("kind") == "web":
                url = entry.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                wname = entry.get("name") or url
                overrides = {k: entry[k] for k in ("output_dir", "formats") if k in entry}
                if entry.get("severity_min"):
                    overrides["min_severity"] = entry["severity_min"]
                targets.append({
                    "kind": "web", "id": url, "name": wname, "url": url,
                    "render": (entry.get("render") or "static"),
                    "selector": entry.get("selector"),
                    "mode": (entry.get("mode") or "text"),
                    "poll_seconds": entry.get("poll_seconds", WEB_DEFAULT_INTERVAL),
                    "overrides": overrides,
                })
                entry_names[url] = f"🌐 {wname}"
                continue

            fid = entry.get("id")
            if not fid:
                continue
            overrides = {k: entry[k] for k in ("output_dir", "formats") if k in entry}
            try:
                meta = drive.get_file_metadata(self.service, fid)
                drive.maybe_persist_refreshed_token(self.creds)
                gtype = pipeline.google_type(meta.get("mimeType"))
                label = meta.get("name") or fid
                if gtype == "folder":
                    n = 0
                    for child in drive.list_folder(self.service, fid):
                        cgt = pipeline.google_type(child.get("mimeType"))
                        if cgt in pipeline.FORMATS_BY_TYPE:
                            add(child["id"], child.get("name"), child.get("modifiedTime"),
                                cgt, overrides)
                            n += 1
                    entry_names[fid] = f"📁 {label} ({n})"
                elif gtype in pipeline.FORMATS_BY_TYPE:
                    add(fid, label, meta.get("modifiedTime"), gtype, overrides)
                    entry_names[fid] = label
                else:
                    entry_names[fid] = f"{label} (unsupported)"
            except ReauthRequired:
                raise
            except DriveError as exc:
                entry_names[fid] = f"⚠️ {fid[:8]}…: {exc}"
                errors.append(f"{fid[:8]}…: {exc}")

        # Disambiguate distinct files that share a name (e.g. two "Untitled
        # document"s) so their outputs/versions/commits never collide.
        counts = {}
        for t in targets:
            base = pipeline.sanitize_filename(t["name"]) or t["id"]
            counts[base] = counts.get(base, 0) + 1
        for t in targets:
            base = pipeline.sanitize_filename(t["name"]) or t["id"]
            if counts[base] > 1:
                t["name"] = f"{t['name']} ({t['id'][:6]})"

        with self._lock:
            self._entry_names = entry_names
        return targets, errors

    @objc.python_method
    def _poll_all(self, watch: list, force: bool) -> None:
        targets, errors = self._resolve_targets(watch)
        notes = list(errors)  # resolve errors + per-file export errors + git/hook warnings
        completed = True
        for t in targets:
            if self._stop.is_set():
                return
            with self._lock:
                if self._paused:
                    completed = False
                    break
            try:
                if t.get("kind") == "web":
                    warning = self._poll_web(t, force)   # never raises; self-backs-off
                else:
                    warning = self._poll_file(t, force)
                if warning:
                    notes.append(warning)
            except ReauthRequired:
                raise  # bubble up so the worker drops the stale service
            except DriveError as exc:
                notes.append(f"{t['name']}: {exc}")

        live = {t["id"] for t in targets}
        with self._lock:
            # Bound per-target state to currently-resolved targets, but only on a
            # fully clean cycle so a transient failure doesn't drop a baseline.
            if completed and not notes:
                self._modified = {k: v for k, v in self._modified.items() if k in live}
                self._prev_text = {k: v for k, v in self._prev_text.items() if k in live}
                self._web_next = {k: v for k, v in self._web_next.items() if k in live}
                self._web_backoff = {k: v for k, v in self._web_backoff.items() if k in live}
            # Only publish batch status on a completed (non-paused) cycle, so a
            # mid-batch pause doesn't leave a partial count/warning.
            if completed:
                self._state["kind"] = "watching"
                self._state["error_msg"] = None
                self._state["watch_count"] = len(targets)
                self._state["names"] = [t["name"] for t in targets]
                if not notes:
                    self._state["warning"] = None
                elif len(notes) == 1:
                    self._state["warning"] = notes[0]
                else:
                    self._state["warning"] = f"{len(notes)} issues (e.g. {notes[0]})"

    @objc.python_method
    def _poll_file(self, t: dict, force: bool):
        """Export one target if changed. Returns a non-fatal warning string or None."""
        fid, name, modified, gtype = t["id"], t["name"], t["modified"], t["gtype"]
        if not (force or modified != self._modified.get(fid)):
            return None

        self._update_state(kind="exporting")
        cfg = dict(self._config)
        cfg.update(t.get("overrides") or {})  # per-target output_dir/formats override
        result = pipeline.run_export(cfg, self.service, fid, name, gtype)

        primary = result.get("primary")
        now = time.strftime("%H:%M:%S")
        with self._lock:
            self._modified[fid] = modified
            self._state["last_export_time"] = now
            if primary:
                self._state["last_pdf_path"] = str(primary)
                self._recent.appendleft({"time": now, "path": str(primary), "name": name})
                self._recent_dirty = True

        new_text = result.get("text")
        old_text = self._prev_text.get(fid)
        if new_text is not None:
            self._prev_text[fid] = new_text
        has_diff = bool(new_text and old_text is not None and old_text != new_text)

        # Plain export-confirmation notification only when AI classification is
        # OFF. When it's ON, the change handler notifies *only* on changes that
        # pass the severity filter — which is what kills notification fatigue.
        if primary and cfg.get("notify") and not cfg.get("ai_summary"):
            fmts = ", ".join(result.get("written", {}).keys()) or "pdf"
            self._notify(f"{name}", f"Exported {fmts} · {now}")

        # Change intelligence: classify → filter → menu/notify/alert/log (async).
        if has_diff:
            self._handle_change(name, old_text, new_text, dict(cfg))
        return result.get("warning")

    @objc.python_method
    def _poll_web(self, t: dict, force: bool):
        """Fetch + denoise a web target; on a real content change, hand the diff
        to the SAME pipeline a Doc change uses. Returns a warning string or None;
        never raises (web failures self-handle with backoff)."""
        url, name = t["url"], t["name"]
        interval = max(WEB_MIN_INTERVAL, int(t.get("poll_seconds") or WEB_DEFAULT_INTERVAL))
        nowm = time.monotonic()
        with self._lock:
            if not force and nowm < self._web_next.get(url, 0.0):
                return None  # not due yet (web polls far slower than docs)

        self._update_state(kind="exporting")
        cfg = dict(self._config)
        cfg.update(t.get("overrides") or {})  # per-target severity/output overrides
        try:
            snap = web.snapshot(t)
        except web.BotBlocked as exc:
            with self._lock:
                bo = min(WEB_BACKOFF_CAP, max(interval, self._web_backoff.get(url, interval)) * 2)
                self._web_backoff[url] = bo
                self._web_next[url] = nowm + bo
            return f"{name}: {exc} — backing off"
        except web.WebError as exc:  # WebSkip (selector empty) included — warn, no diff
            with self._lock:
                self._web_next[url] = nowm + interval
            return f"{name}: {exc}"

        # Success — reset backoff; jitter the next poll so targets stay staggered.
        with self._lock:
            self._web_backoff.pop(url, None)
            self._web_next[url] = nowm + interval + (hash(url) % max(1, interval // 10))

        old = self._prev_text.get(url)
        changed = old is not None and old != snap
        if old is None or changed:           # baseline or change → snapshot + commit
            path = self._write_web_snapshot(name, url, t.get("mode", "text"), snap, cfg)
            now = time.strftime("%H:%M:%S")
            with self._lock:
                self._prev_text[url] = snap
                self._state["last_export_time"] = now
                if path:
                    self._state["last_pdf_path"] = str(path)
                    self._recent.appendleft({"time": now, "path": str(path), "name": name})
                    self._recent_dirty = True
            if changed:
                if cfg.get("notify") and not cfg.get("ai_summary"):
                    self._notify(name, "Page changed")
                self._handle_change(name, old, snap, cfg)  # reuse downstream pipeline
        return None

    @objc.python_method
    def _write_web_snapshot(self, name, url, mode, snap, cfg):
        """Write the cleaned snapshot to the output dir + commit it to git (audit)."""
        ext = "html" if mode == "html" else "txt"
        base = pipeline.sanitize_filename(name) or pipeline.sanitize_filename(url) or "page"
        data = snap.encode("utf-8")
        path = None
        try:
            keep_n = max(0, int(cfg.get("keep_versions", 0) or 0))
            path = pipeline.write_output(config.resolve_output_dir(cfg), base, ext, data,
                                         bool(cfg.get("timestamped")), keep_n)
        except Exception:  # noqa: BLE001 — best-effort
            pass
        if cfg.get("git_repo"):
            try:
                pipeline.commit_history(cfg["git_repo"], name, {f"{base}.{ext}": data})
            except Exception:  # noqa: BLE001 — git is best-effort
                pass
        return path

    @objc.python_method
    def _handle_change(self, name, old_text, new_text, cfg) -> None:
        """Classify a change, gate it by severity, then surface + alert + log.

        Runs on a daemon thread (local model + webhooks/email are network I/O);
        never blocks the watch loop. Coalesces per doc so a fast series of edits
        can't pile up overlapping model/alert threads.
        """
        with self._lock:
            if name in self._inflight:
                return  # a change for this doc is already being processed
            self._inflight.add(name)

        def go():
            try:
                summary = severity = category = None
                degraded = False
                if cfg.get("ai_summary"):
                    cls = summarize.classify_change(old_text, new_text, cfg)
                    if cls:
                        summary, severity, category = cls["summary"], cls["severity"], cls["category"]
                    else:
                        # Model enabled but unreachable/failed. We can't classify,
                        # so we must NOT go silent (that hides real changes from a
                        # user relying on alerts). Fail open: 'substantive' for
                        # external gating, and always surface locally + warn.
                        severity, degraded = "substantive", True

                # A model outage is always surfaced locally + on the warning line,
                # regardless of threshold — otherwise a raised threshold + outage =
                # total silence on changes that might be material.
                if degraded:
                    self._notify(f"{name} — changed",
                                 "Local model offline — couldn't classify this change.")
                    with self._lock:
                        self._state["alert_warning"] = "Local model unavailable — change not classified."

                passed = alerts.passes(severity, cfg.get("min_severity"))
                warns = []
                if passed:
                    now = datetime.now()
                    event = {"time": now.isoformat(timespec="seconds"), "doc": name,
                             "summary": summary, "severity": severity, "category": category}
                    # The change notification (the plain export one was suppressed).
                    if cfg.get("ai_summary") and not degraded:
                        self._notify(f"{name} — {severity}", summary or "Document changed.")
                        if summary:
                            with self._lock:
                                self._state["last_summary"] = f"{name}: [{severity}] {summary}"
                    if alerts.any_destination(cfg):
                        tag = f" [{severity}]" if severity else ""
                        warns = alerts.dispatch(cfg, f"DocToPDF: {name} changed{tag}",
                                                summary or "Document changed.")
                    if (cfg.get("digest") or "off") != "off":
                        try:
                            digest.append(event, now)
                        except Exception:  # noqa: BLE001 — logging is best-effort
                            pass

                # Clear/refresh the warning line on a clean (non-degraded) result.
                if not degraded:
                    note = (warns[0] if len(warns) == 1 else
                            f"{len(warns)} alert issues (e.g. {warns[0]})") if warns else None
                    with self._lock:
                        self._state["alert_warning"] = note
            finally:
                with self._lock:
                    self._inflight.discard(name)

        threading.Thread(target=go, name="doctopdf-change", daemon=True).start()

    # ----------------------------------------------------------- UI refresh
    def refreshUI_(self, _timer) -> None:
        with self._lock:
            st = dict(self._state)
            paused = self._paused
            watch_entries = list(self._watch)
            entry_names = dict(self._entry_names)

        kind = st["kind"]
        err = st["error_msg"]
        names = st.get("names") or []
        count = st.get("watch_count", 0)
        # Concise description of what's being watched.
        if count == 0:
            watching = "…"
        elif count == 1:
            watching = names[0] if names else "1 item"
        else:
            watching = f"{count} items"

        if paused:
            title, status = GLYPH_PAUSED + LABEL, "Paused"
        elif kind == "error":
            title, status = GLYPH_ERROR + LABEL, f"Error: {err}" if err else "Error"
        elif kind == "needs_doc":
            title, status = GLYPH_IDLE + LABEL, "Nothing watched — add a doc"
        elif kind == "authorizing":
            title, status = GLYPH_IDLE + LABEL, "Authorizing in browser…"
        elif kind == "exporting":
            title, status = GLYPH_EXPORTING + LABEL, f"Exporting… ({watching})"
        elif kind == "watching":
            title, status = GLYPH_IDLE + LABEL, f"Watching: {watching}"
        else:  # starting
            title, status = GLYPH_IDLE + LABEL, "Starting…"

        if title != self._cur_title:
            self.statusitem.button().setTitle_(title)
            self._cur_title = title
        self._mi_status.setTitle_(status)

        last = st["last_export_time"]
        self._mi_last.setTitle_(f"Last export: {last}" if last else "Last export: —")
        self._mi_pause.setTitle_("Resume" if paused else "Pause")

        # "Watching ▸" submenu lists each watched entry; rebuild only on change.
        sig = repr([(e.get("id"), entry_names.get(e.get("id"))) for e in watch_entries])
        if sig != self._watch_sig:
            self._rebuild_watch_menu(watch_entries, entry_names)
            self._watch_sig = sig

        # Non-fatal warnings (git/hook + async alert/classify degradation), shown
        # on their own line — not the error glyph. alert_warning isn't cleared by
        # the poll loop, so it persists until the next successful change.
        warn = " · ".join(w for w in (st.get("warning"), st.get("alert_warning")) if w)
        if warn:
            self._mi_warn.setTitle_(f"⚠️ {warn}")
            self._mi_warn.setHidden_(False)
        else:
            self._mi_warn.setHidden_(True)

        # Latest AI change summary, pinned in the menu (hidden until one exists).
        summ = st.get("last_summary")
        if summ:
            self._mi_summary.setTitle_(f"💡 {summ if len(summ) <= 90 else summ[:88] + '…'}")
            self._mi_summary.setHidden_(False)
        else:
            self._mi_summary.setHidden_(True)

        # "Launch at Login" checkmark reflects whether the LaunchAgent is installed.
        self._mi_login.setState_(
            NSControlStateValueOn if launchagent.is_installed() else NSControlStateValueOff
        )

        # Rebuild the "Recent exports" submenu only when it changed.
        with self._lock:
            dirty = self._recent_dirty
            recent = list(self._recent) if dirty else None
            self._recent_dirty = False
        if dirty:
            self._rebuild_recent_menu(recent)

    # ------------------------------------------------------------- actions
    def onAddTarget_(self, _sender) -> None:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Add Doc, Folder, or Web Page")
        alert.setInformativeText_(
            "Paste a Google Doc / Sheet / Slides URL or ID, a Drive folder, or any "
            "web page URL (https://…) to monitor for changes.")
        alert.addButtonWithTitle_("Add")
        alert.addButtonWithTitle_("Cancel")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)

        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        text = field.stringValue().strip()
        entry = self._target_entry_from(text)
        if entry is None:
            self._alert("Invalid link", "Paste a Google file/folder URL or ID, or a web page URL.")
            return
        key = entry.get("url") or entry.get("id")
        with self._lock:
            if any((e.get("url") or e.get("id")) == key for e in self._watch):
                return  # already watched
            self._watch.append(entry)
            self._config["watch"] = list(self._watch)
            self._force = True
            self._auth_blocked = False
            self._interval = self._base_interval
        config.save_config(self._config)
        self._wake.set()

    @objc.python_method
    def _target_entry_from(self, text):
        """Turn pasted text into a watch entry: a Google id/URL → Drive entry; a
        non-Google http(s) URL → web entry. Returns None if neither."""
        if not text:
            return None
        is_url = text.lower().startswith(("http://", "https://"))
        google = is_url and "google.com" in text.lower()
        gid = drive.parse_doc_id(text)
        if gid and (not is_url or google):
            return {"id": gid}
        if is_url:
            from urllib.parse import urlparse
            p = urlparse(text)
            nm = (p.netloc + p.path).rstrip("/")[:60] or text
            return {"kind": "web", "url": text, "name": nm,
                    "render": "static", "mode": "text",
                    "poll_seconds": WEB_DEFAULT_INTERVAL}
        return None

    def onRemoveTarget_(self, sender) -> None:
        fid = sender.representedObject()
        with self._lock:
            label = self._entry_names.get(fid, fid)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Stop watching?")
        alert.setInformativeText_(f"Remove “{label}” from the watch list?\n"
                                  "(Already-exported files are kept.)")
        alert.addButtonWithTitle_("Remove")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        with self._lock:
            self._watch = [e for e in self._watch if e.get("id") != fid]
            self._config["watch"] = list(self._watch)
            self._entry_names.pop(fid, None)
        config.save_config(self._config)
        self._wake.set()

    def onExportNow_(self, _sender) -> None:
        with self._lock:
            empty = not self._watch
        if empty:
            self._alert("Nothing watched", "Add a Doc or Folder first.")
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

    def onOpenRecent_(self, sender) -> None:
        path = sender.representedObject()
        if path and Path(path).exists():
            subprocess.run(["open", str(path)], check=False)
        else:
            self._alert("File missing", "That export no longer exists on disk.")

    def digestTick_(self, _timer) -> None:
        cfg = dict(self._config)
        now = datetime.now()
        if not digest.due(cfg, now):
            return
        events = digest.peek_since(now)        # read without marking yet
        period = cfg.get("digest")

        def go():
            text = digest.build_text(events, period)
            self._notify("DocToPDF digest", text.splitlines()[0])
            # Mark the window sent only if external delivery succeeded (or there
            # are no external destinations), so a webhook/email outage retries.
            ok = True
            if alerts.any_destination(cfg):
                ok = not alerts.dispatch(cfg, f"DocToPDF {period} digest", text)
            if ok:
                digest.mark_sent(now)
        threading.Thread(target=go, name="doctopdf-digest", daemon=True).start()

    def onPreferences_(self, _sender) -> None:
        self._prefs = prefs.PreferencesController.alloc().initWithApp_(self)
        self._prefs.show()

    @objc.python_method
    def apply_prefs(self, cfg: dict) -> None:
        """Persist + apply preferences live (most settings the worker reads each
        poll; poll_interval needs the base interval updated)."""
        # Only re-export immediately if a setting affecting OUTPUT changed —
        # otherwise a Save would write spurious timestamped/rolling versions of
        # unchanged docs (and needlessly re-run git/hook).
        out_keys = ("formats", "output_dir", "timestamped", "keep_versions",
                    "git_repo", "git_snapshot_text")
        changed = any(self._config.get(k) != cfg.get(k) for k in out_keys)
        with self._lock:
            self._config.update(cfg)
            try:
                self._base_interval = max(MIN_INTERVAL, int(cfg.get("poll_interval", 10)))
            except (TypeError, ValueError):
                pass
            self._interval = self._base_interval
            if changed:
                self._force = True  # re-export so new formats/outputs take effect now
        config.save_config(self._config)
        if changed:
            self._wake.set()

    def onToggleLogin_(self, _sender) -> None:
        try:
            if launchagent.is_installed():
                launchagent.uninstall()
            else:
                launchagent.install()
                self._alert(
                    "Launch at Login enabled",
                    "DocToPDF will start automatically the next time you log in.",
                )
        except Exception as exc:  # noqa: BLE001
            self._alert("Couldn't change Login setting", str(exc))

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

    @objc.python_method
    def _rebuild_watch_menu(self, entries, entry_names) -> None:
        """Rebuild the Watching submenu (main thread, from refreshUI_)."""
        self._watch_menu.removeAllItems()
        if not entries:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Nothing watched", None, "")
            item.setEnabled_(False)
            self._watch_menu.addItem_(item)
            return
        for entry in entries:
            eid = entry.get("id")
            if not eid:
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                entry_names.get(eid, eid), "onRemoveTarget:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(eid)
            item.setToolTip_("Click to stop watching this item")
            self._watch_menu.addItem_(item)
        self._watch_menu.addItem_(NSMenuItem.separatorItem())
        hint = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Click an item to remove it", None, "")
        hint.setEnabled_(False)
        self._watch_menu.addItem_(hint)

    @objc.python_method
    def _rebuild_recent_menu(self, recent) -> None:
        """Rebuild the Recent-exports submenu (main thread, from refreshUI_)."""
        self._recent_menu.removeAllItems()
        if not recent:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "No exports yet", None, "")
            item.setEnabled_(False)
            self._recent_menu.addItem_(item)
            return
        for entry in recent:  # already newest-first (appendleft)
            label = f"{entry['time']}   {Path(entry['path']).name}"
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "onOpenRecent:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(entry["path"])
            self._recent_menu.addItem_(item)

    @objc.python_method
    def _notify(self, title: str, message: str) -> None:
        """Post a macOS notification (fire-and-forget via osascript)."""
        def esc(s):
            return str(s).replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{esc(message)}" with title "{esc(title)}"'
        threading.Thread(
            target=lambda: subprocess.run(
                ["osascript", "-e", script], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ),
            name="doctopdf-notify", daemon=True,
        ).start()


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
