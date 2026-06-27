"""py2app build for the DocToPDF macOS menu-bar app.

Build a standalone ``.app`` bundle:

    .venv/bin/python setup.py py2app

It's a menu-bar (agent) app — ``LSUIElement`` hides the Dock icon. The heavy,
*optional* dependencies (the local RAG vector store via ``chromadb`` and the
Playwright browser renderer / ``trafilatura`` extractor) are EXCLUDED to keep the
bundle building and reasonably small; the app degrades gracefully without them —
core Doc/Sheet/Slides/Drawing export, change alerts, git history, multi-account,
and the publishing pipeline all still work. RAG, the MCP server, and JS-rendered
web monitoring are disabled in the packaged build (run from source for those).

NOTE: this produces an *unsigned* bundle. macOS Gatekeeper will quarantine it on
other Macs (right-click → Open, or notarize with an Apple Developer account for a
clean one-click open). And like the source app, the bundle still needs your own
``client_secret.json`` OAuth credentials to authorize Google Drive.
"""
from setuptools import setup

APP = ["packaging/launcher.py"]

OPTIONS = {
    "plist": {
        "CFBundleName": "DocToPDF",
        "CFBundleDisplayName": "DocToPDF",
        "CFBundleIdentifier": "com.doctopdf.app",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSUIElement": True,            # menu-bar agent: no Dock icon
        "NSHighResolutionCapable": True,
    },
    # Force-include packages that are namespace/dynamically imported so
    # modulegraph copies them whole.
    # NB: do NOT list the bare ``google`` namespace package here — it breaks
    # py2app's bootstrap resolver. modulegraph still follows drive.py's static
    # ``google.*`` imports; the concrete dirs below are the non-namespace ones.
    "packages": [
        "doctopdf",
        "google_auth_oauthlib",
        "googleapiclient",
        "markdown",
    ],
    "includes": [
        "nh3",
        "requests",
        "google.auth",
        "google.auth.transport.requests",
        "google.oauth2.credentials",
        "google_auth_httplib2",
    ],
    # Optional/heavy deps the app imports lazily and tolerates missing.
    "excludes": [
        "chromadb",
        "playwright",
        "onnxruntime",
        "trafilatura",
        "lxml",
        "bs4",
        "beautifulsoup4",
        "mcp",
        "tkinter",
        "pytest",
    ],
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
