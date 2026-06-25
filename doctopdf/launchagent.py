"""Manage a per-user LaunchAgent so DocToPDF can start automatically at login.

macOS loads plists in ``~/Library/LaunchAgents`` at login automatically, so
installing is just writing the plist (no immediate launch → no duplicate menu-bar
icon while the app is already running). Uninstalling boots out any loaded copy
and removes the plist.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import config

LABEL = "com.doctopdf.agent"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_installed() -> bool:
    return PLIST_PATH.exists()


def _plist_xml() -> str:
    python = escape(sys.executable)
    project = escape(str(config.PROJECT_ROOT))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>doctopdf.app</string>
    </array>
    <key>WorkingDirectory</key><string>{project}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>/tmp/doctopdf.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/doctopdf.err.log</string>
</dict>
</plist>
"""


def install() -> None:
    """Write the LaunchAgent plist (it loads at the next login)."""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLIST_PATH.with_name(PLIST_PATH.name + ".tmp")
    tmp.write_text(_plist_xml(), encoding="utf-8")
    os.replace(tmp, PLIST_PATH)


def uninstall() -> None:
    """Boot out any loaded copy and remove the plist."""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, check=False,
    )
    try:
        PLIST_PATH.unlink()
    except FileNotFoundError:
        pass
