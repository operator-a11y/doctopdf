"""A native AppKit Preferences window for DocToPDF.

Built programmatically (no nib). Populated from the controller's current config;
on Save it hands a new config dict back to the app, which persists and applies it
live. Watched docs are managed separately via "Add Doc or Folder…".
"""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSButton,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSMakeRect,
    NSSwitchButton,
    NSTextField,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

W, H = 520, 470


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
        content = self.window.contentView()

        def label(text, x, y, w=150):
            lbl = NSTextField.labelWithString_(text)
            lbl.setFrame_(NSMakeRect(x, y, w, 18))
            content.addSubview_(lbl)
            return lbl

        def field(value, x, y, w=320):
            f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 22))
            f.setStringValue_("" if value is None else str(value))
            content.addSubview_(f)
            return f

        def check(text, val, x, y, w=330):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 20))
            b.setButtonType_(NSSwitchButton)
            b.setTitle_(text)
            b.setState_(NSControlStateValueOn if val else NSControlStateValueOff)
            content.addSubview_(b)
            return b

        head = label("DocToPDF Preferences", 20, H - 40, 300)
        head.setFont_(head.font().fontWithSize_(15) if head.font() else None)

        y = H - 80
        label("Poll interval (s):", 20, y + 2)
        self.poll = field(cfg.get("poll_interval", 10), 175, y, 70)
        y -= 34
        label("Output folder:", 20, y + 2)
        self.outdir = field(cfg.get("output_dir", "~/Desktop"), 175, y)
        y -= 34
        label("Formats:", 20, y + 2)
        self.formats = field(", ".join(cfg.get("formats") or ["pdf"]), 175, y)
        label("e.g. pdf, docx, xlsx, pptx, md", 175, y - 18, 320)
        y -= 52
        self.timestamped = check("Keep a timestamped copy of every version", cfg.get("timestamped"), 20, y)
        y -= 28
        label("Rolling — keep last N (0 = off):", 20, y + 2, 240)
        self.keep = field(cfg.get("keep_versions", 0), 260, y, 60)
        y -= 38
        self.notify = check("Show a notification on each export", cfg.get("notify"), 20, y)
        y -= 28
        self.ai = check("AI change summary (local Ollama — opt-in)", cfg.get("ai_summary"), 20, y, 300)
        label("Model:", 325, y + 1, 50)
        self.model = field(cfg.get("ollama_model", "llama3"), 375, y - 1, 120)
        y -= 32
        self.git = check("Git version history", bool(cfg.get("git_repo")), 20, y, 150)
        self.gitrepo = field(cfg.get("git_repo") or "~/Documents/DocToPDF-history", 175, y)
        y -= 34
        label("Post-export cmd:", 20, y + 2)
        self.hook = field(cfg.get("post_export_cmd"), 175, y)

        save = NSButton.alloc().initWithFrame_(NSMakeRect(W - 110, 18, 90, 30))
        save.setTitle_("Save")
        save.setBezelStyle_(1)  # rounded
        save.setKeyEquivalent_("\r")
        save.setTarget_(self)
        save.setAction_("save:")
        content.addSubview_(save)

        cancel = NSButton.alloc().initWithFrame_(NSMakeRect(W - 205, 18, 90, 30))
        cancel.setTitle_("Cancel")
        cancel.setBezelStyle_(1)
        cancel.setTarget_(self)
        cancel.setAction_("cancel:")
        content.addSubview_(cancel)

        self.window.center()

    @objc.python_method
    def show(self) -> None:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def cancel_(self, _sender) -> None:
        self.window.close()

    def save_(self, _sender) -> None:
        cfg = dict(self.app._config)

        def as_int(textfield, default, lo):
            try:
                return max(lo, int(textfield.stringValue().strip()))
            except (TypeError, ValueError):
                return default

        cfg["poll_interval"] = as_int(self.poll, 10, 3)
        cfg["output_dir"] = self.outdir.stringValue().strip() or "~/Desktop"
        cfg["formats"] = [f.strip().lower() for f in self.formats.stringValue().split(",")
                          if f.strip()] or ["pdf"]
        cfg["timestamped"] = bool(self.timestamped.state())
        cfg["keep_versions"] = as_int(self.keep, 0, 0)
        cfg["notify"] = bool(self.notify.state())
        cfg["ai_summary"] = bool(self.ai.state())
        cfg["ollama_model"] = self.model.stringValue().strip() or "llama3"
        cfg["git_repo"] = (self.gitrepo.stringValue().strip() or None) if self.git.state() else None
        cfg["post_export_cmd"] = self.hook.stringValue().strip() or None

        self.app.apply_prefs(cfg)
        self.window.close()
