"""A native, tabbed AppKit Preferences window for DocToPDF.

Built programmatically (no nib). Populated from the controller's current config;
on Save it hands a new config dict back to the app, which persists and applies it
live. Empty fields fall back to defaults, and any setting not shown here (e.g. the
watch list, managed via "Add Doc or Folder…") is preserved untouched.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSMakeRect,
    NSPopUpButton,
    NSSecureTextField,
    NSSwitchButton,
    NSTabView,
    NSTabViewItem,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from . import config, launchagent

W, H = 580, 470
from .summarize import SEVERITIES  # noqa: E402


def _label(view, text, x, y, w=160, size=None, bold=False):
    lbl = NSTextField.labelWithString_(text)
    lbl.setFrame_(NSMakeRect(x, y, w, 18))
    if bold:
        lbl.setFont_(NSFont.boldSystemFontOfSize_(size or 12))
    elif size:
        lbl.setFont_(NSFont.systemFontOfSize_(size))
    view.addSubview_(lbl)
    return lbl


def _field(view, value, x, y, w=330, secure=False):
    cls = NSSecureTextField if secure else NSTextField
    f = cls.alloc().initWithFrame_(NSMakeRect(x, y, w, 22))
    f.setStringValue_("" if value is None else str(value))
    view.addSubview_(f)
    return f


def _check(view, text, val, x, y, w=360):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 20))
    b.setButtonType_(NSSwitchButton)
    b.setTitle_(text)
    b.setState_(NSControlStateValueOn if val else NSControlStateValueOff)
    view.addSubview_(b)
    return b


def _popup(view, options, current, x, y, w=150):
    p = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(x, y, w, 24), False)
    p.addItemsWithTitles_(options)
    if current in options:
        p.selectItemWithTitle_(current)
    view.addSubview_(p)
    return p


class PreferencesController(NSObject):
    def initWithApp_(self, app):
        self = objc.super(PreferencesController, self).init()
        if self is None:
            return None
        self.app = app
        self._build()
        return self

    @objc.python_method
    def _tab(self, tabs, title):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 36, H - 96))
        item = NSTabViewItem.alloc().initWithIdentifier_(title)
        item.setLabel_(title)
        item.setView_(view)
        tabs.addTabViewItem_(item)
        return view

    @objc.python_method
    def _build(self) -> None:
        cfg = dict(self.app._config)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("DocToPDF Preferences")
        self.window.setReleasedWhenClosed_(False)
        root = self.window.contentView()

        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(12, 56, W - 24, H - 70))
        root.addSubview_(tabs)
        self.tabs = tabs

        # ---- General ----
        v = self._tab(tabs, "General")
        y = H - 130
        _label(v, "Poll interval (s):", 14, y + 2); self.poll = _field(v, cfg.get("poll_interval", 10), 175, y, 70); y -= 32
        _label(v, "Output folder:", 14, y + 2); self.outdir = _field(v, cfg.get("output_dir", "~/Desktop"), 175, y, 350); y -= 32
        _label(v, "Formats:", 14, y + 2); self.formats = _field(v, ", ".join(cfg.get("formats") or ["pdf"]), 175, y, 350); y -= 18
        _label(v, "Docs: pdf docx md … · Sheets: pdf xlsx csv · Slides: pdf pptx", 175, y, 360, size=9); y -= 30
        self.notify = _check(v, "Show a notification on each export", cfg.get("notify"), 14, y); y -= 28
        self.login = _check(v, "Start DocToPDF at login", launchagent.is_installed(), 14, y)

        # ---- Versions ----
        v = self._tab(tabs, "Versions")
        y = H - 130
        self.timestamped = _check(v, "Keep a timestamped copy of every version", cfg.get("timestamped"), 14, y); y -= 30
        _label(v, "Rolling — keep last N (0 = off):", 14, y + 2, 240); self.keep = _field(v, cfg.get("keep_versions", 0), 255, y, 60); y -= 34
        self.git = _check(v, "Git version history", bool(cfg.get("git_repo")), 14, y, 160)
        self.gitrepo = _field(v, cfg.get("git_repo") or "~/Documents/DocToPDF-history", 180, y, 345); y -= 28
        self.gitsnap = _check(v, "…also commit a text snapshot (for real diffs)", cfg.get("git_snapshot_text", True), 34, y, 400)

        # ---- Change alerts ----
        v = self._tab(tabs, "Change Alerts")
        y = H - 122
        self.ai = _check(v, "AI change summary + classify (local Ollama)", cfg.get("ai_summary"), 14, y, 320)
        _label(v, "Model:", 330, y + 1, 46); self.model = _field(v, cfg.get("ollama_model", "llama3"), 378, y - 1, 150); y -= 28
        _label(v, "Ollama URL:", 14, y + 2); self.ollama = _field(v, cfg.get("ollama_url", "http://localhost:11434"), 175, y, 350); y -= 30
        _label(v, "Alert threshold:", 14, y + 2); self.severity = _popup(v, list(SEVERITIES), cfg.get("min_severity", "cosmetic"), 175, y - 2, 150)
        _label(v, "(material = most important)", 335, y + 1, 200, size=9); y -= 34
        _label(v, "Webhook URL(s):", 14, y + 2); self.webhooks = _field(v, ", ".join(cfg.get("webhook_urls") or []), 175, y, 350); y -= 16
        _label(v, "Slack / Discord / generic — comma-separated", 175, y, 360, size=9); y -= 28
        _label(v, "Email to:", 14, y + 2); self.email_to = _field(v, cfg.get("email_to"), 175, y, 150)
        _label(v, "From:", 340, y + 2, 40); self.email_from = _field(v, cfg.get("email_from"), 385, y, 140); y -= 30
        _label(v, "SMTP host:", 14, y + 2); self.smtp_host = _field(v, cfg.get("smtp_host"), 175, y, 150)
        _label(v, "Port:", 340, y + 2, 40); self.smtp_port = _field(v, cfg.get("smtp_port", 587), 385, y, 60); y -= 30
        _label(v, "SMTP user:", 14, y + 2); self.smtp_user = _field(v, cfg.get("smtp_user"), 175, y, 150)
        _label(v, "Pass:", 340, y + 2, 40); self.smtp_pass = _field(v, cfg.get("smtp_pass"), 385, y, 140, secure=True); y -= 32
        _label(v, "Digest:", 14, y + 2); self.digest = _popup(v, ["off", "daily", "weekly"], cfg.get("digest", "off"), 175, y - 2, 110)
        _label(v, "at hour:", 300, y + 2, 50); self.digest_hour = _field(v, cfg.get("digest_hour", 9), 355, y, 50)

        # ---- Advanced ----
        v = self._tab(tabs, "Advanced")
        y = H - 130
        _label(v, "Post-export cmd:", 14, y + 2); self.hook = _field(v, cfg.get("post_export_cmd"), 175, y, 350); y -= 18
        _label(v, "Runs after each export. $1 = file path; $DOCTOPDF_FILES, $DOCTOPDF_DOC_NAME set.", 175, y, 370, size=9)

        save = NSButton.alloc().initWithFrame_(NSMakeRect(W - 102, 14, 88, 32))
        save.setTitle_("Save"); save.setBezelStyle_(1); save.setKeyEquivalent_("\r")
        save.setTarget_(self); save.setAction_("save:")
        root.addSubview_(save)
        cancel = NSButton.alloc().initWithFrame_(NSMakeRect(W - 196, 14, 88, 32))
        cancel.setTitle_("Cancel"); cancel.setBezelStyle_(1); cancel.setKeyEquivalent_("\x1b")
        cancel.setTarget_(self); cancel.setAction_("cancel:")
        root.addSubview_(cancel)

        self.window.center()

    @objc.python_method
    def show(self) -> None:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def cancel_(self, _sender) -> None:
        self.window.close()

    def save_(self, _sender) -> None:
        cfg = dict(self.app._config)  # preserves keys not shown here (watch, etc.)
        d = config.DEFAULT_CONFIG

        def as_int(tf, default, lo):
            try:
                return max(lo, int(tf.stringValue().strip()))
            except (TypeError, ValueError):
                return default

        def text(tf, default=None):
            return tf.stringValue().strip() or default

        cfg["poll_interval"] = as_int(self.poll, cfg.get("poll_interval", d["poll_interval"]), 3)
        cfg["output_dir"] = text(self.outdir, d["output_dir"])
        cfg["formats"] = [f.strip().lower() for f in self.formats.stringValue().split(",")
                          if f.strip()] or list(d["formats"])
        cfg["notify"] = bool(self.notify.state())
        cfg["timestamped"] = bool(self.timestamped.state())
        cfg["keep_versions"] = as_int(self.keep, cfg.get("keep_versions", 0), 0)
        cfg["git_repo"] = (text(self.gitrepo, "~/Documents/DocToPDF-history")
                           if self.git.state() else None)
        cfg["git_snapshot_text"] = bool(self.gitsnap.state())
        cfg["ai_summary"] = bool(self.ai.state())
        cfg["ollama_model"] = text(self.model, d["ollama_model"])
        cfg["ollama_url"] = text(self.ollama, d["ollama_url"])
        cfg["min_severity"] = self.severity.titleOfSelectedItem() or "cosmetic"
        cfg["webhook_urls"] = [u.strip() for u in self.webhooks.stringValue().replace("\n", ",").split(",")
                               if u.strip()]
        cfg["email_to"] = text(self.email_to)
        cfg["email_from"] = text(self.email_from)
        cfg["smtp_host"] = text(self.smtp_host)
        cfg["smtp_port"] = as_int(self.smtp_port, cfg.get("smtp_port", 587), 1)
        cfg["smtp_user"] = text(self.smtp_user)
        cfg["smtp_pass"] = self.smtp_pass.stringValue() or None
        cfg["digest"] = self.digest.titleOfSelectedItem() or "off"
        cfg["digest_hour"] = min(23, as_int(self.digest_hour, cfg.get("digest_hour", 9), 0))
        cfg["post_export_cmd"] = self.hook.stringValue().strip() or None

        # Launch-at-login is managed via the LaunchAgent, not config.
        want_login = bool(self.login.state())
        try:
            if want_login and not launchagent.is_installed():
                launchagent.install()
            elif not want_login and launchagent.is_installed():
                launchagent.uninstall()
        except Exception:  # noqa: BLE001
            pass

        self.app.apply_prefs(cfg)
        self.window.close()
