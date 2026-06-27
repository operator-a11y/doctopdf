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

import hashlib
import queue
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
    NSAlertSecondButtonReturn,
    NSAlertThirdButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSMenu,
    NSMenuItem,
    NSPopUpButton,
    NSStatusBar,
    NSTextField,
    NSTimer,
    NSVariableStatusItemLength,
)
from Foundation import NSMakeRect, NSObject

from . import (accounts, alerts, audit, config, digest, drive, launchagent,
               pipeline, prefs, publish, rag, summarize, web)
from .accounts import AccountAuthError
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

RAG_RETRY_BASE = 30          # seconds before the first embed retry
RAG_RETRY_CAP = 600          # max backoff while the embedder stays down

PUB_RETRY_BASE = 30          # seconds before the first publish (push) retry
PUB_RETRY_CAP = 900          # max backoff while a push keeps failing
RECENT_PUB_MAX = 15          # entries kept in the "Recent publishes" list


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
        self._pending: dict = {}            # doc name -> latest (old,new,cfg,who) queued mid-flight
        self._web_next: dict = {}           # web url -> monotonic time of next poll
        self._web_backoff: dict = {}        # web url -> current backoff seconds (bot-block)
        self._entry_names: dict = {}        # watch-entry id -> menu label (decorated)
        self._source_names: dict = {}       # target id -> clean display name (for publish)
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
            "rag_count": None,              # indexed chunk count (None = RAG off)
            "rag_last_sync": None,          # last time the index changed
            "rag_note": None,              # degraded note (embedder down / reindex)
        }

        # --- RAG / vector sync plumbing ------------------------------------
        # A single worker thread drains a queue of sync/delete tasks, so store
        # writes are serialized and the embedder is never hammered. RAG is
        # strictly additive: failures queue + surface, never block the watcher.
        self._rag = None                    # RagStore, or None when disabled
        self._rag_q: queue.Queue = queue.Queue()
        self._rag_pending: dict = {}        # target_id -> latest task awaiting retry
        self._rag_retry_at = 0.0            # monotonic time of next embed retry
        self._rag_backoff = RAG_RETRY_BASE
        self._rag_dim_mismatch = False      # set on an embedder swap; needs reindex
        # Target ids we've sent to the index, and ones seen absent once. A target
        # must be missing for TWO consecutive clean cycles before its vectors are
        # purged, so a transient incomplete folder listing can't churn them.
        self._rag_indexed: set = set()
        self._rag_drop_pending: set = set()
        if (self._config.get("rag") or {}).get("enabled"):
            try:
                self._rag = rag.RagStore(self._config)
            except Exception:  # noqa: BLE001 — never let RAG setup break startup
                self._rag = None

        # --- Publishing plumbing -------------------------------------------
        # A single worker serializes git push / render off the watch loop, so a
        # slow push never blocks watching and concurrent git can't corrupt a
        # working copy. Publishing is additive: failures surface + retry.
        self._pub_q: queue.Queue = queue.Queue()
        self._pub_status: dict = {}         # target_key -> status dict (menu)
        self._pub_pending: dict = {}        # target_key -> (target, name, md) awaiting Approve
        self._pub_retry: dict = {}          # target_key -> (task, due_monotonic) after a push fail
        self._pub_backoff: dict = {}        # target_key -> current retry backoff seconds
        self._recent_pub: deque = deque(maxlen=RECENT_PUB_MAX)
        self._pub_sig = None                # last-rendered Publishing submenu signature

        # Recent exports for the submenu (guarded by _lock; _recent_dirty tells
        # the UI timer to rebuild the submenu).
        self._recent: deque = deque(maxlen=RECENT_MAX)
        self._recent_dirty = True

        # --- worker plumbing ----------------------------------------------
        # Per-account credential/service caches, keyed by account email. Built
        # lazily by _service_for and dropped per-account on a stale token, so one
        # account's auth problem never blanks out the others. (Worker-thread
        # owned; _account_errors is read by the UI tick under _lock.)
        self._svc: dict = {}                # account email -> Drive service
        self._svc_creds: dict = {}          # account email -> Credentials
        self._account_errors: dict = {}     # account email -> last auth error msg
        # Serializes the interactive OAuth flow so the worker's bootstrap and a
        # menu "Add account…" can't pop two browser windows at once.
        self._auth_lock = threading.Lock()
        self._accounts_sig = None           # last-rendered Accounts submenu signature
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = threading.Thread(target=self._run_worker, name="watch", daemon=True)

        # If digests are already enabled in the loaded config but no digest has
        # ever been delivered, seed the baseline now — events accumulate
        # continuously for the audit log, so an un-seeded first digest would
        # dump the whole retained backlog. (The live off->on path is seeded in
        # apply_prefs; this covers a config that loads already-on.)
        if (self._config.get("digest") or "off").lower() in ("daily", "weekly"):
            try:
                digest.seed_if_unset(datetime.now())
            except Exception:  # noqa: BLE001 — best-effort seed
                pass

        self._cur_title = None
        self._prefs = None                  # retained Preferences window controller
        self._build_status_item()

        self._worker.start()
        # Dedicated RAG worker: serializes embed/upsert/delete off the watch loop.
        if self._rag is not None:
            self._rag_thread = threading.Thread(
                target=self._rag_worker, name="doctopdf-rag", daemon=True)
            self._rag_thread.start()
        # Dedicated publish worker: serializes git push / render off the watch loop.
        self._pub_thread = threading.Thread(
            target=self._pub_worker, name="doctopdf-publish", daemon=True)
        self._pub_thread.start()
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
        self._mi_rag = disabled("")         # vector index status, hidden unless RAG on
        self._mi_rag.setHidden_(True)

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

        # "Publishing ▸" submenu: per-target status; click to approve/open/retry.
        self._publish_menu = NSMenu.alloc().init()
        self._publish_menu.setAutoenablesItems_(False)
        self._mi_publish = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Publishing", None, "")
        self._mi_publish.setSubmenu_(self._publish_menu)
        self._mi_publish.setHidden_(True)   # shown only when publish targets exist
        menu.addItem_(self._mi_publish)

        menu.addItem_(NSMenuItem.separatorItem())
        self._mi_pause = action("Pause", "onTogglePause:")
        action("Add Doc or Folder…", "onAddTarget:")

        # "Accounts ▸" submenu: authorized Google accounts (✓ = default; click to
        # set default), plus Add / Remove account. Rebuilt by the UI timer.
        self._accounts_menu = NSMenu.alloc().init()
        self._accounts_menu.setAutoenablesItems_(False)
        self._mi_accounts = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Accounts", None, "")
        self._mi_accounts.setSubmenu_(self._accounts_menu)
        menu.addItem_(self._mi_accounts)

        self._mi_pubnow = action("Publish now", "onPublishNow:")
        self._mi_pubnow.setHidden_(True)    # shown only when publish targets exist
        action("Change history…", "onAuditHistory:")
        self._mi_reindex = action("Rebuild index", "onRebuildIndex:")
        self._mi_reindex.setHidden_(True)   # shown only when RAG is enabled
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

            # Ensure at least one Google account is authorized before polling, so
            # the app prompts on first launch and sits ready in the menu bar. On
            # first run a legacy single-account token.json migrates silently;
            # otherwise the interactive flow adds the first account. Skip while
            # idle-paused with nothing queued so we don't pop a browser.
            if not accounts.list_accounts() and not (paused and not force_pending):
                if blocked:
                    if paused:
                        self._update_state(kind="paused")
                    self._sleep_or_wake(interval)
                    continue
                if not config.CLIENT_SECRET_PATH.exists():
                    self._set_error("Missing client_secret.json — see README.")
                    self._sleep_or_wake(max(interval, 10))
                    continue
                if not self._bootstrap_account():   # migrate or interactive add
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
                # Safety net: a stale credential slipped past the per-account
                # handling in resolve/poll. Drop all cached services so the next
                # cycle rebuilds/refreshes them (per-account, lazily).
                with self._lock:
                    self._svc.clear()
                    self._svc_creds.clear()
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
    def _bootstrap_account(self) -> bool:
        """Ensure ≥1 account exists: migrate a legacy token, else interactive add.

        Returns True once an account is available. Migration is idempotent and
        only deletes the legacy token after the migrated copy is confirmed
        written, so a failed/offline migration safely falls through (and retries
        next cycle) without losing the working token.
        """
        self._update_state(kind="authorizing")
        try:
            if accounts.migrate_legacy_token():
                return True
        except Exception:  # noqa: BLE001 — migration must never crash the worker
            pass
        if accounts.list_accounts():
            return True
        try:
            accounts.authorize_new_account()
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
    def _service_for(self, account_key):
        """Return a cached Drive service for an account (``None`` → default).

        Builds and caches the service + credentials on first use. Raises
        :class:`AccountAuthError` if that account's token is missing or
        unrecoverable — callers handle it per-target so one bad account never
        blocks the others.
        """
        key = account_key or accounts.default_key()
        if key is None:
            raise AccountAuthError("No Google account authorized — add one under Accounts.")
        with self._lock:
            svc = self._svc.get(key)
        if svc is not None:
            return svc
        # Build outside the lock — credentials_for may block on a token refresh.
        creds = accounts.credentials_for(key)
        svc = drive.build_service(creds)
        with self._lock:
            self._svc[key] = svc
            self._svc_creds[key] = creds
            self._account_errors.pop(key, None)
        return svc

    @objc.python_method
    def _drop_account(self, account_key, msg=None) -> None:
        """Forget an account's cached service/creds (e.g. after a stale-token
        error) so the next cycle rebuilds + refreshes it; record the error for
        the menu so the user can re-authorize just that account."""
        key = account_key or accounts.default_key()
        if key is None:
            return
        with self._lock:
            self._svc.pop(key, None)
            self._svc_creds.pop(key, None)
            if msg:
                self._account_errors[key] = msg

    @objc.python_method
    def _resolve_targets(self, watch: list):
        """Expand the watch list into concrete file targets (folders → their
        direct children, non-recursive). Returns ``(targets, errors)``.

        Each target is ``{id, name, modified, gtype, overrides, account}``. Each
        Google entry is fetched with its own account's credential; a single bad
        entry (deleted/unshared/network, or a stale account token) is caught and
        reported per-target, never aborting the others. Targets are de-duplicated
        by id, and colliding display names are disambiguated.
        """
        targets, entry_names, errors, seen = [], {}, [], set()
        known_accounts = {a.get("email") for a in accounts.list_accounts()}

        def add(fid, name, modified, gtype, overrides, who=None, rag_flag=None, account=None):
            if fid in seen:
                return
            seen.add(fid)
            targets.append({"kind": "drive", "id": fid, "name": name or fid,
                            "modified": modified, "gtype": gtype, "overrides": overrides,
                            "who": who, "rag": rag_flag, "account": account})

        def editor(m):
            return (m.get("lastModifyingUser") or {}).get("displayName")

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
                    "overrides": overrides, "rag": entry.get("rag"),
                })
                entry_names[url] = f"🌐 {wname}"
                continue

            fid = entry.get("id")
            if not fid:
                continue
            overrides = {k: entry[k] for k in ("output_dir", "formats") if k in entry}
            # Fetch this entry with its own account's credential (None → default).
            acct_key = entry.get("account")
            # Orphaned target: its bound account was removed. Don't fetch it under
            # a different identity — flag it clearly so the user can re-add or
            # reassign it (Accounts ▸), and move on without aborting the others.
            if acct_key and acct_key not in known_accounts:
                msg = f"account {acct_key} no longer authorized — re-add or reassign"
                entry_names[fid] = f"⚠️ {fid[:8]}…: {msg}"
                errors.append(f"{fid[:8]}…: {msg}")
                continue
            try:
                service = self._service_for(acct_key)
            except AccountAuthError as exc:
                entry_names[fid] = f"⚠️ {fid[:8]}…: {exc}"
                errors.append(f"{fid[:8]}…: {exc}")
                continue
            key = acct_key or accounts.default_key()
            try:
                meta = drive.get_file_metadata(service, fid)
                with self._lock:
                    creds = self._svc_creds.get(key)
                if creds is not None:
                    accounts.persist_if_refreshed(key, creds)
                gtype = pipeline.google_type(meta.get("mimeType"))
                label = meta.get("name") or fid
                if gtype == "folder":
                    n = 0
                    # Folder children inherit the parent entry's account.
                    for child in drive.list_folder(service, fid):
                        cgt = pipeline.google_type(child.get("mimeType"))
                        if cgt in pipeline.FORMATS_BY_TYPE:
                            add(child["id"], child.get("name"), child.get("modifiedTime"),
                                cgt, overrides, editor(child), entry.get("rag"), acct_key)
                            n += 1
                    entry_names[fid] = f"📁 {label} ({n})"
                elif gtype in pipeline.FORMATS_BY_TYPE:
                    add(fid, label, meta.get("modifiedTime"), gtype, overrides,
                        editor(meta), entry.get("rag"), acct_key)
                    entry_names[fid] = label
                else:
                    entry_names[fid] = f"{label} (unsupported)"
            except ReauthRequired as exc:
                # This account's session went stale mid-resolve. Drop its cached
                # service so the next cycle refreshes it, and report per-target —
                # never bubble up and disturb the other accounts.
                self._drop_account(key, str(exc))
                entry_names[fid] = f"⚠️ {fid[:8]}…: {exc}"
                errors.append(f"{fid[:8]}…: {exc}")
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
                # Drive ids are unique in 6 chars; a web id is a URL (all share
                # the "https:" prefix), so hash it for a distinguishing suffix.
                suffix = (t["id"][:6] if t.get("kind") == "drive"
                          else hashlib.md5(t["id"].encode()).hexdigest()[:6])
                t["name"] = f"{t['name']} ({suffix})"

        with self._lock:
            self._entry_names = entry_names
            # Clean (un-decorated) names, so a manual Publish-now/retry titles the
            # page the same way an auto publish (which uses the poll's name) does.
            self._source_names = {t["id"]: t["name"] for t in targets}
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
        rag_drops = []
        with self._lock:
            # Bound per-target state to currently-resolved targets, but only on a
            # fully clean cycle so a transient failure doesn't drop a baseline.
            if completed and not notes:
                self._modified = {k: v for k, v in self._modified.items() if k in live}
                self._prev_text = {k: v for k, v in self._prev_text.items() if k in live}
                self._web_next = {k: v for k, v in self._web_next.items() if k in live}
                self._web_backoff = {k: v for k, v in self._web_backoff.items() if k in live}
                # Vectors for indexed targets that are now absent — but only purge
                # after a SECOND consecutive clean cycle of absence, so a transient
                # incomplete listing doesn't delete (then re-embed) a live target.
                if self._rag is not None:
                    rag_drops = self._rag_drops_for(live)
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

        # Purge vectors for removed targets/folder-children (outside the lock).
        if self._rag is not None and rag_drops:
            for tid in rag_drops:
                self._rag_enqueue_delete(tid)

    @objc.python_method
    def _poll_file(self, t: dict, force: bool):
        """Export one target if changed. Returns a non-fatal warning string or None."""
        fid, name, modified, gtype = t["id"], t["name"], t["modified"], t["gtype"]
        if not (force or modified != self._modified.get(fid)):
            return None

        self._update_state(kind="exporting")
        cfg = dict(self._config)
        cfg.update(t.get("overrides") or {})  # per-target output_dir/formats override
        # Export with this target's own account credential (None → default).
        try:
            service = self._service_for(t.get("account"))
            result = pipeline.run_export(cfg, service, fid, name, gtype)
        except AccountAuthError as exc:
            return str(exc)
        except ReauthRequired as exc:
            self._drop_account(t.get("account"), str(exc))
            return str(exc)

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

        # RAG: keep the vector store current. Fires on baseline and change (the
        # store reconciles by hash, so this is incremental); skipped per-target
        # via an explicit ``rag: false``.
        if self._rag is not None and new_text and t.get("rag") is not False:
            self._rag_enqueue_sync(fid, name, rag.kind_for(gtype),
                                   rag.google_link(gtype, fid), new_text)

        # Plain export-confirmation notification only when AI classification is
        # OFF. When it's ON, the change handler notifies *only* on changes that
        # pass the severity filter — which is what kills notification fatigue.
        if primary and cfg.get("notify") and not cfg.get("ai_summary"):
            fmts = ", ".join(result.get("written", {}).keys()) or "pdf"
            self._notify(f"{name}", f"Exported {fmts} · {now}")

        # Change intelligence: classify → filter → menu/notify/alert/log (async).
        if has_diff:
            self._handle_change(name, old_text, new_text, dict(cfg), who=t.get("who"))
            # Publishing: re-publish the (stable) changed Markdown to bound targets.
            for pt in self._pub_targets_for(fid):
                self._pub_enqueue(pt, name, new_text)
        return result.get("warning")

    @objc.python_method
    def _poll_web(self, t: dict, force: bool):
        """Fetch + denoise a web target; on a real content change, hand the diff
        to the SAME pipeline a Doc change uses. Returns a warning string or None;
        never raises (web failures self-handle with backoff)."""
        url, name = t["url"], t["name"]
        try:
            interval = max(WEB_MIN_INTERVAL, int(t.get("poll_seconds") or WEB_DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            interval = WEB_DEFAULT_INTERVAL  # malformed config must never raise here
        nowm = time.monotonic()
        with self._lock:
            blocked = url in self._web_backoff
            nxt = self._web_next.get(url, 0.0)
            # Skip if not due — and even on 'force' don't hammer a bot-blocked site
            # before its backoff elapses.
            if (not force or blocked) and nowm < nxt:
                return None

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
                # Don't shrink an active backoff (e.g. block followed by a timeout).
                self._web_next[url] = max(self._web_next.get(url, 0.0), nowm + interval)
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
            # RAG sync on baseline + change (web target_id == url); the store
            # reconciles by hash so this stays incremental.
            if self._rag is not None and t.get("rag") is not False:
                self._rag_enqueue_sync(url, name, "web", url, snap)
            if changed:
                if cfg.get("notify") and not cfg.get("ai_summary"):
                    self._notify(name, "Page changed")
                self._handle_change(name, old, snap, cfg)  # reuse downstream pipeline
                for pt in self._pub_targets_for(url):
                    self._pub_enqueue(pt, name, snap)
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

    # ------------------------------------------------------ RAG / vector sync
    @objc.python_method
    def _rag_enqueue_sync(self, target_id, name, kind, link, text) -> None:
        """Queue a target's latest snapshot for incremental (re-)indexing.

        Gated by the global flag and the per-target ``rag`` opt-out at the call
        sites. Fires on baseline *and* change — the store reconciles by content
        hash, so an unchanged poll is a no-op and a one-line edit re-embeds one
        chunk. Never blocks: it just puts a task on the RAG worker's queue.
        """
        if self._rag is None or not (text and text.strip()):
            return
        self._rag_indexed.add(target_id)
        self._rag_q.put(("sync", target_id, name, kind, link or "", text))

    @objc.python_method
    def _rag_enqueue_delete(self, target_id) -> None:
        """Queue removal of a target's chunks (target/folder-child removed)."""
        if self._rag is None:
            return
        self._rag_q.put(("delete", target_id, None, None, None, None))

    @objc.python_method
    def _rag_drops_for(self, live) -> list:
        """Two-strike removal: return indexed target ids that have been absent for
        a SECOND consecutive clean cycle, updating the strike sets. A single-cycle
        absence (transient incomplete listing) is remembered but not purged — it
        clears the moment the target reappears. Caller holds ``self._lock``."""
        absent = self._rag_indexed - live
        drops = list(absent & self._rag_drop_pending)
        self._rag_drop_pending = absent - set(drops)
        self._rag_indexed -= set(drops)
        return drops

    @objc.python_method
    def _rag_worker(self) -> None:
        """Drain RAG tasks serially; retry embedder failures with backoff."""
        # Publish an initial chunk count so the menu shows state before any sync.
        self._rag_refresh_stats()
        while not self._stop.is_set():
            try:
                task = self._rag_q.get(timeout=1.0)
            except queue.Empty:
                self._rag_retry_pending()
                continue
            try:
                self._rag_run(task)
            finally:
                self._rag_q.task_done()

    @objc.python_method
    def _rag_run(self, task) -> None:
        """Execute one sync/delete task, handling degradation."""
        action, target_id, name, kind, link, text = task
        try:
            if action == "reindex":
                self._rag_reindex()
                return
            if action == "delete":
                self._rag.delete_target(target_id)
                self._rag_pending.pop(target_id, None)
            else:  # sync
                if self._rag_dim_mismatch:
                    # Refuse partial sync until a reindex; keep it pending so a
                    # reindex (which clears the flag) picks it back up.
                    self._rag_pending[target_id] = task
                    return
                self._rag.sync(target_id, name, kind, link, text)
                self._rag_pending.pop(target_id, None)
                self._rag_backoff = RAG_RETRY_BASE
            self._rag_refresh_stats()
        except rag.DimensionMismatch as exc:
            self._rag_dim_mismatch = True
            self._rag_pending[target_id] = task
            self._rag_set_note(f"Vector index: {exc}")
        except rag.EmbedError as exc:
            # Embedder down — keep the latest snapshot for this target and retry.
            self._rag_pending[target_id] = task
            self._rag_retry_at = time.monotonic() + self._rag_backoff
            self._rag_backoff = min(RAG_RETRY_CAP, self._rag_backoff * 2)
            self._rag_set_note(f"Vector index paused — embedder offline "
                               f"({len(self._rag_pending)} queued). {exc}")
        except rag.RagUnavailable as exc:
            # Store layer is out — disable RAG for this session and surface it.
            self._rag = None
            self._rag_set_note(f"Vector index disabled — {exc}")
        except Exception as exc:  # noqa: BLE001 — never let the RAG worker die
            self._rag_set_note(f"Vector index error: {exc}")

    @objc.python_method
    def _rag_retry_pending(self) -> None:
        """Re-run queued syncs once their backoff elapses (embedder recovered)."""
        if self._rag is None or not self._rag_pending:
            return
        if self._rag_dim_mismatch:
            return  # only a reindex clears this
        if time.monotonic() < self._rag_retry_at:
            return
        for tid, task in list(self._rag_pending.items()):
            if self._stop.is_set():
                return
            self._rag_run(task)
            # If it's still pending, the embedder is still down — stop this round
            # rather than hammering it once per queued target (and bumping the
            # backoff 2^N). _rag_run already scheduled the next retry window.
            if tid in self._rag_pending:
                break

    @objc.python_method
    def _rag_refresh_stats(self) -> None:
        """Read store stats onto the shared state for the menu (RAG thread only)."""
        if self._rag is None:
            return
        try:
            s = self._rag.stats()
        except Exception:  # noqa: BLE001 — status must never raise
            return
        with self._lock:
            self._state["rag_count"] = s.get("count")
            self._state["rag_last_sync"] = s.get("last_sync")
            # Clear a stale degraded note once we're healthy again.
            if not self._rag_pending and not self._rag_dim_mismatch:
                self._state["rag_note"] = None

    @objc.python_method
    def _rag_set_note(self, note) -> None:
        with self._lock:
            self._state["rag_note"] = note

    @objc.python_method
    def _rag_reindex(self) -> None:
        """Clear the store and re-sync every target (menu “Rebuild index”)."""
        if self._rag is None:
            return
        try:
            self._rag.reindex()
        except Exception as exc:  # noqa: BLE001
            self._rag_set_note(f"Reindex failed: {exc}")
            return
        self._rag_dim_mismatch = False
        self._rag_pending.clear()
        self._rag_backoff = RAG_RETRY_BASE
        self._rag_indexed.clear()
        self._rag_drop_pending.clear()
        self._rag_set_note(None)
        self._rag_refresh_stats()
        with self._lock:
            # Re-baseline EVERY target so the forced poll re-embeds all of them.
            # Without clearing _prev_text, an unchanged web page (old == snapshot)
            # would be skipped by _poll_web's change gate and silently vanish from
            # the rebuilt index. Clearing it makes old_text None → a clean baseline
            # (no spurious change alerts); clearing the web schedule forces an
            # immediate re-fetch rather than waiting out the poll interval.
            self._prev_text.clear()
            self._modified.clear()
            self._web_next.clear()
            self._web_backoff.clear()
            self._force = True      # next poll re-captures + re-embeds every target
        self._wake.set()

    # ------------------------------------------------------- publishing
    @objc.python_method
    def _pub_targets_for(self, source_id) -> list:
        """Publish targets bound to a watched source id (from config)."""
        with self._lock:
            return [t for t in (self._config.get("publish") or [])
                    if t.get("source_id") == source_id]

    @objc.python_method
    def _pub_enqueue(self, target, name, md_text, manual_ok=False) -> None:
        """Queue a publish of ``md_text`` for ``target``. ``manual_ok`` bypasses
        the approval gate (an explicit Publish-now / Approve)."""
        self._pub_q.put((target, publish.target_key(target), name, md_text, manual_ok))

    @objc.python_method
    def _pub_worker(self) -> None:
        """Drain publish tasks serially; retry failed pushes with backoff."""
        while not self._stop.is_set():
            try:
                task = self._pub_q.get(timeout=1.0)
            except queue.Empty:
                self._pub_retry_due()
                continue
            try:
                self._pub_run(task)
            except Exception:  # noqa: BLE001 — a task must never kill the worker
                pass
            finally:
                self._pub_q.task_done()

    @objc.python_method
    def _pub_run(self, task) -> None:
        """Execute one publish task: approval gate → render/push → status/retry."""
        target, key, name, md_text, manual_ok = task
        approval = (target.get("approval") or "auto").lower()
        site = target.get("site_url")

        # Manual approval: a non-explicit change is held as pending (rendered on
        # Approve), never pushed live mid-review.
        if approval == "manual" and not manual_ok:
            if not (md_text and md_text.strip()):
                return
            with self._lock:
                self._pub_pending[key] = (target, name, md_text)
            self._pub_set_status(key, name=name, type=target.get("type"),
                                 site_url=site, status="pending")
            self._notify(f"{name} — pending",
                         "Site has pending changes — review & publish.")
            return

        self._pub_set_status(key, name=name, type=target.get("type"),
                             site_url=site, status="publishing")
        try:
            res = publish.publish(target, name, md_text)
        except publish.PublishSkip:
            return  # empty snapshot — never publish a blank page or wipe a live one
        except publish.PublishError as exc:
            self._pub_set_status(key, status="error", error=str(exc))
            bo = self._pub_backoff.get(key, PUB_RETRY_BASE)
            self._pub_retry[key] = (task, time.monotonic() + bo)
            self._pub_backoff[key] = min(PUB_RETRY_CAP, bo * 2)
            self._notify(f"Publish failed — {name}", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — never let the publish worker die
            self._pub_set_status(key, status="error", error=str(exc))
            return

        # Success — clear pending/retry, record status.
        with self._lock:
            self._pub_pending.pop(key, None)
        self._pub_retry.pop(key, None)
        self._pub_backoff[key] = PUB_RETRY_BASE
        now = time.strftime("%H:%M:%S")
        url = res.get("url") or site
        pushed = res.get("status") == "published"   # vs 'unchanged' (no diff)
        fields = {"name": name, "type": target.get("type"), "site_url": site,
                  "status": "published", "url": url, "warning": res.get("warning"),
                  "error": None}
        if pushed:
            fields["last_published"] = now
        self._pub_set_status(key, **fields)
        if pushed:
            with self._lock:
                self._recent_pub.appendleft({"time": now, "name": name, "url": url})
            body = url or "done"
            if res.get("warning"):
                body += f" · {res['warning']}"
            self._notify(f"Published {name}", body)

    @objc.python_method
    def _pub_retry_due(self) -> None:
        """Re-run failed publishes whose backoff has elapsed (push recovered).
        Drops retries for targets that have since been removed from config."""
        if not self._pub_retry:
            return
        with self._lock:
            live = {publish.target_key(t) for t in (self._config.get("publish") or [])}
        now = time.monotonic()
        for key, (task, due) in list(self._pub_retry.items()):
            if self._stop.is_set():
                return
            if key not in live:
                self._pub_retry.pop(key, None)        # target removed — stop retrying
                self._pub_backoff.pop(key, None)
                continue
            if now >= due:
                self._pub_retry.pop(key, None)
                self._pub_run(task)

    @objc.python_method
    def _pub_set_status(self, key, **fields) -> None:
        with self._lock:
            self._pub_status.setdefault(key, {}).update(fields)

    @objc.python_method
    def _pub_approve(self, key) -> None:
        """Approve a pending manual target → publish its held snapshot."""
        with self._lock:
            pend = self._pub_pending.get(key)
        if pend:
            target, name, md = pend
            self._pub_enqueue(target, name, md, manual_ok=True)

    @objc.python_method
    def _pub_publish_all(self) -> None:
        """Publish every target's current snapshot now (explicit user action)."""
        with self._lock:
            pubs = list(self._config.get("publish") or [])
            snaps = dict(self._prev_text)
            names = dict(self._source_names)
        for t in pubs:
            sid = t.get("source_id")
            md = snaps.get(sid)
            if md:
                self._pub_enqueue(t, names.get(sid) or sid, md, manual_ok=True)

    @objc.python_method
    def _handle_change(self, name, old_text, new_text, cfg, who=None) -> None:
        """Classify a change, record it, then surface + alert above threshold.

        Runs on a daemon thread (local model + webhooks/email are network I/O);
        never blocks the watch loop. Coalesces per doc so a fast series of edits
        can't pile up overlapping model/alert threads — but the latest pending
        change is *queued*, not dropped, so its diff (and 'who') is never lost.
        """
        with self._lock:
            if name in self._inflight:
                self._pending[name] = (old_text, new_text, cfg, who)
                return  # a change for this doc is mid-flight; re-processed on finish
            self._inflight.add(name)

        def go(o, n, c, w):
            try:
                while True:
                    self._process_change(name, o, n, c, w)
                    with self._lock:
                        nxt = self._pending.pop(name, None)
                        if nxt is None:
                            self._inflight.discard(name)
                            return
                    o, n, c, w = nxt  # drain the change that arrived mid-flight
            except BaseException:
                with self._lock:
                    self._pending.pop(name, None)
                    self._inflight.discard(name)
                raise

        threading.Thread(target=go, args=(old_text, new_text, cfg, who),
                         name="doctopdf-change", daemon=True).start()

    @objc.python_method
    def _process_change(self, name, old_text, new_text, cfg, who) -> None:
        """Classify one change, log it to the audit trail, and alert if it passes
        the severity threshold. Synchronous; called from the change worker."""
        summary = severity = category = None
        degraded = False
        if cfg.get("ai_summary"):
            cls = summarize.classify_change(old_text, new_text, cfg)
            if cls:
                summary, severity, category = cls["summary"], cls["severity"], cls["category"]
            else:
                # Model enabled but unreachable/failed. We can't classify, so we
                # must NOT go silent (that hides real changes from a user relying
                # on alerts). Fail open: 'substantive' for external gating, and
                # always surface locally + warn.
                severity, degraded = "substantive", True

        # A model outage is always surfaced locally + on the warning line,
        # regardless of threshold — otherwise a raised threshold + outage = total
        # silence on changes that might be material.
        if degraded:
            self._notify(f"{name} — changed",
                         "Local model offline — couldn't classify this change.")
            with self._lock:
                self._state["alert_warning"] = "Local model unavailable — change not classified."

        now = datetime.now()
        event = {"time": now.isoformat(timespec="seconds"), "doc": name,
                 "summary": summary, "severity": severity, "category": category,
                 "who": who}
        # The audit trail records EVERY detected change, regardless of the alert
        # threshold — the Change-history dashboard is the full compliance record;
        # min_severity only gates who gets *alerted* (below). A degraded change is
        # logged too, so an outage never leaves a hole in the trail.
        try:
            digest.append(event, now)
        except Exception:  # noqa: BLE001 — logging is best-effort
            pass

        passed = alerts.passes(severity, cfg.get("min_severity"))
        warns = []
        if passed:
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

        # Clear/refresh the warning line on a clean (non-degraded) result.
        if not degraded:
            note = (warns[0] if len(warns) == 1 else
                    f"{len(warns)} alert issues (e.g. {warns[0]})") if warns else None
            with self._lock:
                self._state["alert_warning"] = note

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

        # "Accounts ▸" submenu; rebuild only when the set / default / errors change.
        accts = accounts.list_accounts()
        with self._lock:
            acct_errors = dict(self._account_errors)
        a_sig = repr([(a.get("email"), a.get("is_default")) for a in accts]
                     + sorted(acct_errors))
        if a_sig != self._accounts_sig:
            self._rebuild_accounts_menu(accts, acct_errors)
            self._accounts_sig = a_sig

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

        # Vector index status (chunk count + last sync, or a degraded note).
        if self._rag is not None:
            note = st.get("rag_note")
            if note:
                line = f"🧠 {note if len(note) <= 88 else note[:86] + '…'}"
            else:
                cnt = st.get("rag_count")
                last = st.get("rag_last_sync")
                when = (last or "").replace("T", " ")[:16]
                line = (f"🧠 Indexed {cnt} chunk(s)" if cnt is not None
                        else "🧠 Index starting…")
                if when:
                    line += f" · synced {when}"
            self._mi_rag.setTitle_(line)
            self._mi_rag.setHidden_(False)
            self._mi_reindex.setHidden_(False)
        else:
            self._mi_rag.setHidden_(True)
            self._mi_reindex.setHidden_(True)

        # Publishing submenu: show only when targets exist; rebuild on change.
        with self._lock:
            pub_targets = list(self._config.get("publish") or [])
            pub_status = {k: dict(v) for k, v in self._pub_status.items()}
        has_pub = bool(pub_targets)
        self._mi_publish.setHidden_(not has_pub)
        self._mi_pubnow.setHidden_(not has_pub)
        if has_pub:
            sig = repr([(publish.target_key(t),
                         pub_status.get(publish.target_key(t), {}).get("status"),
                         pub_status.get(publish.target_key(t), {}).get("last_published"))
                        for t in pub_targets])
            if sig != self._pub_sig:
                self._rebuild_publish_menu(pub_targets, pub_status)
                self._pub_sig = sig

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
        # Bind a Google target to an account: pick when >1 is authorized, use the
        # only one silently, or leave it for the default if none is set up yet.
        # (Web targets have no account.)
        if entry.get("kind") != "web":
            accts = accounts.list_accounts()
            if len(accts) > 1:
                chosen = self._pick_account(accts, accounts.default_key())
                if chosen is None:
                    return  # cancelled the account picker
                entry["account"] = chosen
            elif len(accts) == 1:
                entry["account"] = accts[0].get("email")
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
    def _pick_account(self, accts, default_email):
        """Modal popup to choose which account a new Google target belongs to.

        Returns the chosen account email, or ``None`` if cancelled.
        """
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Which account?")
        alert.setInformativeText_("Choose the Google account that can open this item.")
        alert.addButtonWithTitle_("Add")
        alert.addButtonWithTitle_("Cancel")
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 0, 300, 26), False)
        emails = [a.get("email") for a in accts]
        for e in emails:
            popup.addItemWithTitle_(e)
        if default_email in emails:
            popup.selectItemWithTitle_(default_email)
        alert.setAccessoryView_(popup)
        if alert.runModal() != NSAlertFirstButtonReturn:
            return None
        return popup.titleOfSelectedItem()

    @objc.python_method
    def _target_entry_from(self, text):
        """Turn pasted text into a watch entry: a Google id/URL → Drive entry; a
        non-Google http(s) URL → web entry. Returns None if neither."""
        if not text:
            return None
        from urllib.parse import urlparse
        is_url = text.lower().startswith(("http://", "https://"))
        host = (urlparse(text).hostname or "").lower() if is_url else ""
        google = host == "google.com" or host.endswith((".google.com",))
        gid = drive.parse_doc_id(text)
        if gid and (not is_url or google):
            return {"id": gid}
        if is_url:
            p = urlparse(text)
            nm = (p.netloc + p.path).rstrip("/")[:60] or text
            # id == url so web targets show in the Watching submenu and can be removed.
            return {"kind": "web", "id": text, "url": text, "name": nm,
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

    def onAccountClicked_(self, sender) -> None:
        """Click an account row → set it as default, or re-authorize it if its
        token has gone stale (the row shows ⚠️)."""
        email = sender.representedObject()
        if not email:
            return
        with self._lock:
            errored = email in self._account_errors
        if errored:
            self._authorize_account_async("Account re-authorized")
            return
        accounts.set_default(email)
        with self._lock:
            self._accounts_sig = None   # force the submenu to re-render the ✓
        self._wake.set()

    def onAddAccount_(self, _sender) -> None:
        """Authorize an additional Google account (interactive)."""
        self._authorize_account_async("Account added")

    @objc.python_method
    def _authorize_account_async(self, ok_title) -> None:
        """Run the interactive OAuth flow OFF the main thread so the menu never
        freezes during the browser round-trip; report the result via a
        notification and wake the worker. Serialized with the worker bootstrap
        (one browser window at a time). Dedupe-by-permissionId means re-adding /
        re-authorizing an existing account refreshes its token in place — which
        is also how a stale account recovers — rather than duplicating it.
        Managed/Workspace or admin-blocked sign-ins surface as a clear
        notification instead of crashing.
        """
        def go():
            if not self._auth_lock.acquire(blocking=False):
                self._notify(ok_title, "Authorization already in progress.")
                return
            try:
                acct = accounts.authorize_new_account()
            except AuthFlowError as exc:
                self._notify("Authorization failed", str(exc))
                return
            except DriveError as exc:
                self._notify("Authorization failed", str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self._notify("Authorization failed", f"Error: {exc}")
                return
            finally:
                self._auth_lock.release()
            email = acct.get("email")
            with self._lock:
                self._auth_blocked = False
                self._account_errors.pop(email, None)   # clear any stale-token flag
                self._svc.pop(email, None)              # reload with the fresh token
                self._svc_creds.pop(email, None)
                self._accounts_sig = None
                self._force = True
            self._notify(ok_title, f"Authorized {email}.")
            self._wake.set()

        threading.Thread(target=go, name="doctopdf-authaccount", daemon=True).start()

    def onRemoveAccount_(self, sender) -> None:
        email = sender.representedObject()
        if not email:
            return
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Remove account?")
        alert.setInformativeText_(
            f"Remove “{email}”? Its saved authorization is deleted; you can "
            "re-add it later.")
        alert.addButtonWithTitle_("Remove")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        remaining = accounts.remove_account(email)
        with self._lock:
            self._svc.pop(email, None)
            self._svc_creds.pop(email, None)
            self._account_errors.pop(email, None)
            self._accounts_sig = None
        # Deal with any watch targets that were bound to the removed account.
        self._reassign_or_remove_orphans(email, remaining)
        self._wake.set()

    @objc.python_method
    def _reassign_or_remove_orphans(self, email, remaining) -> None:
        """After removing ``email``, deal with watch targets still bound to it:
        reassign to another account, remove them, or leave them (they'll show an
        error until re-added/reassigned). Never silently drops a target."""
        with self._lock:
            orphans = [e for e in self._watch if e.get("account") == email]
        if not orphans:
            return
        n = len(orphans)
        plural = "target" if n == 1 else "targets"
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        if remaining:
            alert.setMessageText_(f"Reassign {n} {plural}?")
            alert.setInformativeText_(
                f"“{email}” had {n} watched {plural}. Reassign to another account, "
                "remove them, or keep them (they'll show an error until reassigned).")
            alert.addButtonWithTitle_("Reassign…")
            alert.addButtonWithTitle_("Remove targets")
            alert.addButtonWithTitle_("Keep")
            choice = alert.runModal()
            if choice == NSAlertFirstButtonReturn:
                dest = self._pick_account(remaining, remaining[0].get("email"))
                if dest is not None:
                    self._reassign_orphans(email, dest)
            elif choice == NSAlertSecondButtonReturn:
                self._remove_orphan_entries(email)
            # NSAlertThirdButtonReturn ("Keep") → leave them; they error on resolve.
        else:
            alert.setMessageText_(f"{n} {plural} orphaned")
            alert.setInformativeText_(
                f"Removing “{email}” leaves {n} {plural} with no account. Remove "
                "them, or keep them (they'll show an error until you add an account).")
            alert.addButtonWithTitle_("Remove targets")
            alert.addButtonWithTitle_("Keep")
            if alert.runModal() == NSAlertFirstButtonReturn:
                self._remove_orphan_entries(email)

    @objc.python_method
    def _reassign_orphans(self, email, dest) -> None:
        """Rebind every watch target on ``email`` to account ``dest``."""
        with self._lock:
            for e in self._watch:
                if e.get("account") == email:
                    e["account"] = dest
            self._config["watch"] = list(self._watch)
        config.save_config(self._config)

    @objc.python_method
    def _remove_orphan_entries(self, email) -> None:
        """Drop every watch target bound to ``email`` from the watch list."""
        with self._lock:
            self._watch = [e for e in self._watch if e.get("account") != email]
            self._config["watch"] = list(self._watch)
        config.save_config(self._config)

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
        # The audit log records every change; the digest stays a curated rollup,
        # so apply the same severity threshold the real-time alerts use. (The
        # full record lives in the Change-history dashboard.)
        threshold = cfg.get("min_severity")
        events = [e for e in digest.peek_since(now)         # read without marking yet
                  if alerts.passes(e.get("severity"), threshold)]
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

    def onAuditHistory_(self, _sender) -> None:
        try:
            path = audit.write_report(digest.all_events(), time.strftime("%Y-%m-%d %H:%M:%S"))
            subprocess.run(["open", str(path)], check=False)
        except Exception as exc:  # noqa: BLE001
            self._alert("Couldn't open history", str(exc))

    def onRebuildIndex_(self, _sender) -> None:
        if self._rag is None:
            return
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Rebuild knowledge index?")
        alert.setInformativeText_("Clears the vector store and re-embeds every "
                                  "watched source on the next cycle. Use this after "
                                  "changing the embedder model.")
        alert.addButtonWithTitle_("Rebuild")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        # Route through the RAG worker so it's serialized with syncs/deletes
        # (no concurrent collection mutation).
        self._rag_set_note("Rebuilding index…")
        self._rag_q.put(("reindex", None, None, None, None, None))

    def onPublishTarget_(self, sender) -> None:
        key = sender.representedObject()
        with self._lock:
            st = dict(self._pub_status.get(key, {}))
            pending = key in self._pub_pending
        state = st.get("status")
        if state == "pending" or pending:
            self._pub_approve(key)                 # approve & publish held snapshot
        elif st.get("url") and str(st["url"]).startswith("http"):
            subprocess.run(["open", str(st["url"])], check=False)   # open live site
        elif state == "error":
            self._pub_publish_one(key)             # retry now
        elif st.get("url"):
            subprocess.run(["open", str(st["url"])], check=False)   # local PDF path

    @objc.python_method
    def _pub_publish_one(self, key) -> None:
        """Re-publish the current snapshot for a single target (retry)."""
        with self._lock:
            target = next((t for t in (self._config.get("publish") or [])
                           if publish.target_key(t) == key), None)
            snaps = dict(self._prev_text)
            names = dict(self._source_names)
        if target:
            sid = target.get("source_id")
            if snaps.get(sid):
                self._pub_enqueue(target, names.get(sid) or sid, snaps[sid], manual_ok=True)

    def onPublishNow_(self, _sender) -> None:
        with self._lock:
            empty = not (self._config.get("publish") or [])
        if empty:
            self._alert("No publish targets", "Add a publish target in Preferences first.")
            return
        self._pub_publish_all()

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
        # Switching digests on: seed the baseline to now so the *first* digest
        # covers only changes from here forward. Events accumulate continuously
        # (for the audit log), so without this the first digest would dump the
        # entire retained backlog (up to 90 days).
        was_off = (self._config.get("digest") or "off").lower() not in ("daily", "weekly")
        now_on = (cfg.get("digest") or "off").lower() in ("daily", "weekly")
        if was_off and now_on:
            try:
                digest.mark_sent(datetime.now())
            except Exception:  # noqa: BLE001 — best-effort seed
                pass
        # Same guard when change recording is switched on: if no digest baseline
        # exists yet, seed one so a later digest doesn't dump the backlog that
        # recording now begins to build. seed_if_unset never clobbers a live one.
        if not self._config.get("audit_log", True) and cfg.get("audit_log", True):
            try:
                digest.seed_if_unset(datetime.now())
            except Exception:  # noqa: BLE001 — best-effort seed
                pass
        # Newly-bound publish targets: publish the source's current snapshot now
        # (auto → live, manual → pending), so binding takes effect without waiting
        # for the next edit.
        old_keys = {publish.target_key(t) for t in (self._config.get("publish") or [])}
        live_keys = {publish.target_key(t) for t in (cfg.get("publish") or [])}
        new_targets = [t for t in (cfg.get("publish") or [])
                       if publish.target_key(t) not in old_keys]
        with self._lock:
            self._config.update(cfg)
            try:
                self._base_interval = max(MIN_INTERVAL, int(cfg.get("poll_interval", 10)))
            except (TypeError, ValueError):
                pass
            self._interval = self._base_interval
            if changed:
                self._force = True  # re-export so new formats/outputs take effect now
            snaps = dict(self._prev_text)
            names = dict(self._source_names)
            # Drop status/pending for removed targets so no stale menu line lingers.
            # (_pub_retry/_pub_backoff are worker-owned; the worker drops removed
            # targets in _pub_retry_due, avoiding a cross-thread mutation here.)
            for d in (self._pub_status, self._pub_pending):
                for k in [k for k in d if k not in live_keys]:
                    d.pop(k, None)
        for t in new_targets:
            sid = t.get("source_id")
            if snaps.get(sid):
                self._pub_enqueue(t, names.get(sid) or sid, snaps[sid])
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
    def _rebuild_accounts_menu(self, accts, errors) -> None:
        """Rebuild the Accounts submenu (main thread, from refreshUI_).

        Lists each authorized account (✓ marks the default; ⚠️ marks one whose
        token has gone stale) with a click-to-set-default action, then an
        "Add account…" item and a "Remove account ▸" submenu.
        """
        m = self._accounts_menu
        m.removeAllItems()
        if not accts:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "No accounts authorized", None, "")
            item.setEnabled_(False)
            m.addItem_(item)
        else:
            for a in accts:
                email = a.get("email")
                if errors.get(email):
                    title = f"⚠️ {email}"
                    tip = errors.get(email)
                else:
                    title = f"{'✓ ' if a.get('is_default') else '   '}{email}"
                    tip = ("Default account for new targets" if a.get("is_default")
                           else "Click to make this the default account")
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, "onAccountClicked:", "")
                item.setTarget_(self)
                item.setRepresentedObject_(email)
                item.setToolTip_(tip)
                m.addItem_(item)
        m.addItem_(NSMenuItem.separatorItem())
        add = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Add account…", "onAddAccount:", "")
        add.setTarget_(self)
        m.addItem_(add)
        if accts:
            remove_menu = NSMenu.alloc().init()
            remove_menu.setAutoenablesItems_(False)
            for a in accts:
                email = a.get("email")
                ri = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    email, "onRemoveAccount:", "")
                ri.setTarget_(self)
                ri.setRepresentedObject_(email)
                remove_menu.addItem_(ri)
            mi_remove = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Remove account", None, "")
            mi_remove.setSubmenu_(remove_menu)
            m.addItem_(mi_remove)

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
    def _rebuild_publish_menu(self, targets, status) -> None:
        """Rebuild the Publishing submenu (main thread, from refreshUI_).

        Each target shows its status; clicking: a *pending* target approves &
        publishes it; a *published* one opens its site URL; an *errored* one
        retries. A trailing hint explains the click behavior.
        """
        self._publish_menu.removeAllItems()
        _GLYPH = {"published": "✅", "pending": "🟡", "error": "⚠️",
                  "publishing": "🔄", None: "•"}
        for t in targets:
            key = publish.target_key(t)
            st = status.get(key, {})
            name = st.get("name") or t.get("source_id") or "?"
            state = st.get("status")
            if state == "published":
                extra = f"published {st.get('last_published', '')}".strip()
            elif state == "pending":
                extra = "pending — click to approve"
            elif state == "error":
                extra = f"error: {st.get('error', '')}"[:60]
            elif state == "publishing":
                extra = "publishing…"
            else:
                extra = t.get("type") or "git_markdown"
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"{_GLYPH.get(state, '•')} {name} — {extra}", "onPublishTarget:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(key)
            self._publish_menu.addItem_(item)
        self._publish_menu.addItem_(NSMenuItem.separatorItem())
        hint = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Click: approve pending · open site · retry errors", None, "")
        hint.setEnabled_(False)
        self._publish_menu.addItem_(hint)

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
