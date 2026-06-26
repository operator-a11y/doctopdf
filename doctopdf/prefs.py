"""A native AppKit Preferences window for DocToPDF.

Built programmatically (no nib). Populated from the controller's current config;
on Save it hands a new config dict back to the app, which persists and applies it
live. Empty fields fall back to sensible defaults, and any setting not shown here
(e.g. the watch list, managed via "Add Doc or Folder…") is preserved untouched.
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
    NSSwitchButton,
    NSTextField,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from . import config, launchagent

W, H = 540, 600


class PreferencesController(NSObject):
    def initWithApp_(self, app):
        self = objc.super(PreferencesController, self).init()
        if self is None:
            return None
        self.app = app
        self._build()
        return self

    @objc.python_method
    def _build(self) -> None:
        cfg = dict(self.app._config)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("DocToPDF Preferences")
        self.window.setReleasedWhenClosed_(False)
        view = self.window.contentView()

        def label(text, x, y, w=160, size=None, bold=False):
            lbl = NSTextField.labelWithString_(text)
            lbl.setFrame_(NSMakeRect(x, y, w, 18))
            if bold:
                lbl.setFont_(NSFont.boldSystemFontOfSize_(size or 12))
            elif size:
                lbl.setFont_(NSFont.systemFontOfSize_(size))
            view.addSubview_(lbl)
            return lbl

        def field(value, x, y, w=330):
            f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 22))
            f.setStringValue_("" if value is None else str(value))
            view.addSubview_(f)
            return f

        def check(text, val, x, y, w=360):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 20))
            b.setButtonType_(NSSwitchButton)
            b.setTitle_(text)
            b.setState_(NSControlStateValueOn if val else NSControlStateValueOff)
            view.addSubview_(b)
            return b

        y = H - 36
        label("DocToPDF Preferences", 20, y, 320, size=15, bold=True)
        y -= 30

        # ---- Watching ----
        label("Watching", 18, y, 200, size=12, bold=True); y -= 26
        label("Poll interval (s):", 20, y + 2); self.poll = field(cfg.get("poll_interval", 10), 185, y, 70); y -= 30
        label("Output folder:", 20, y + 2); self.outdir = field(cfg.get("output_dir", "~/Desktop"), 185, y, 335); y -= 30
        label("Formats:", 20, y + 2); self.formats = field(", ".join(cfg.get("formats") or ["pdf"]), 185, y, 335); y -= 18
        label("Docs: pdf docx odt rtf txt html md epub · Sheets: pdf xlsx ods csv tsv · Slides: pdf pptx odp txt",
              185, y, 340, size=9); y -= 26

        # ---- Versions ----
        label("Versions", 18, y, 200, size=12, bold=True); y -= 26
        self.timestamped = check("Keep a timestamped copy of every version", cfg.get("timestamped"), 20, y); y -= 26
        label("Rolling — keep last N (0 = off):", 20, y + 2, 235); self.keep = field(cfg.get("keep_versions", 0), 260, y, 60); y -= 28
        self.git = check("Git version history", bool(cfg.get("git_repo")), 20, y, 160)
        self.gitrepo = field(cfg.get("git_repo") or "~/Documents/DocToPDF-history", 185, y, 335); y -= 26
        self.gitsnap = check("…also commit a text snapshot (for real diffs)", cfg.get("git_snapshot_text", True), 40, y, 380); y -= 26

        # ---- Notifications & AI ----
        label("Notifications & AI", 18, y, 220, size=12, bold=True); y -= 26
        self.notify = check("Show a notification on each export", cfg.get("notify"), 20, y); y -= 26
        self.ai = check("AI change summary (local Ollama — opt-in)", cfg.get("ai_summary"), 20, y, 300)
        label("Model:", 330, y + 1, 46); self.model = field(cfg.get("ollama_model", "llama3"), 378, y - 1, 142); y -= 28
        label("Ollama URL:", 20, y + 2); self.ollama = field(cfg.get("ollama_url", "http://localhost:11434"), 185, y, 335); y -= 30

        # ---- Automation ----
        label("Automation", 18, y, 200, size=12, bold=True); y -= 26
        label("Post-export cmd:", 20, y + 2); self.hook = field(cfg.get("post_export_cmd"), 185, y, 335); y -= 18
        label("Runs after each export. $1 = file path; $DOCTOPDF_FILES, $DOCTOPDF_DOC_NAME also set.",
              185, y, 340, size=9); y -= 26
        self.login = check("Start DocToPDF at login", launchagent.is_installed(), 20, y, 300)

        save = NSButton.alloc().initWithFrame_(NSMakeRect(W - 110, 18, 90, 32))
        save.setTitle_("Save"); save.setBezelStyle_(1); save.setKeyEquivalent_("\r")
        save.setTarget_(self); save.setAction_("save:")
        view.addSubview_(save)
        cancel = NSButton.alloc().initWithFrame_(NSMakeRect(W - 205, 18, 90, 32))
        cancel.setTitle_("Cancel"); cancel.setBezelStyle_(1); cancel.setKeyEquivalent_("\x1b")
        cancel.setTarget_(self); cancel.setAction_("cancel:")
        view.addSubview_(cancel)

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

        def text(tf, default):
            return tf.stringValue().strip() or default

        cfg["poll_interval"] = as_int(self.poll, cfg.get("poll_interval", d["poll_interval"]), 3)
        cfg["output_dir"] = text(self.outdir, d["output_dir"])
        cfg["formats"] = [f.strip().lower() for f in self.formats.stringValue().split(",")
                          if f.strip()] or list(d["formats"])
        cfg["timestamped"] = bool(self.timestamped.state())
        cfg["keep_versions"] = as_int(self.keep, 0, 0)
        cfg["git_repo"] = (text(self.gitrepo, "~/Documents/DocToPDF-history")
                           if self.git.state() else None)
        cfg["git_snapshot_text"] = bool(self.gitsnap.state())
        cfg["notify"] = bool(self.notify.state())
        cfg["ai_summary"] = bool(self.ai.state())
        cfg["ollama_model"] = text(self.model, d["ollama_model"])
        cfg["ollama_url"] = text(self.ollama, d["ollama_url"])
        cfg["post_export_cmd"] = self.hook.stringValue().strip() or None

        # Launch-at-login is managed via the LaunchAgent, not config.
        want_login = bool(self.login.state())
        try:
            if want_login and not launchagent.is_installed():
                launchagent.install()
            elif not want_login and launchagent.is_installed():
                launchagent.uninstall()
        except Exception:  # noqa: BLE001 — don't let a login-toggle error block Save
            pass

        self.app.apply_prefs(cfg)
        self.window.close()
