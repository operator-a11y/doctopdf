"""py2app build for the DocToPDF macOS menu-bar app.

Build a standalone ``.app`` bundle:

    .venv/bin/python setup.py py2app

It's a menu-bar (agent) app — ``LSUIElement`` hides the Dock icon. Core export,
change alerts, git history, multi-account, publishing, and web-page monitoring
(static + HTML, via trafilatura/bs4/lxml) are all bundled. Still EXCLUDED to keep
the bundle building and reasonably small: the local RAG vector store
(``chromadb`` + its heavy native deps) and the Playwright browser renderer
(needs separate browser binaries). The app degrades gracefully without them, so
RAG, the MCP server, and JS-rendered (browser) web monitoring are disabled in the
packaged build (run from source for those).

NOTE: this produces an *unsigned* bundle. macOS Gatekeeper will quarantine it on
other Macs (right-click → Open, or notarize with an Apple Developer account for a
clean one-click open). And like the source app, the bundle still needs your own
``client_secret.json`` OAuth credentials to authorize Google Drive.
"""
import glob
import os

from setuptools import setup

APP = ["packaging/launcher.py"]

# charset_normalizer (a trafilatura dependency) ships a mypyc-compiled shared
# module at the site-packages ROOT — a sibling of the package, named
# "<hash>__mypyc.cpython-*.so" — which `packages` does not grab. Its compiled
# submodules import it, so without it web extraction crashes at runtime. Find it
# dynamically (the hash is version-specific) and force-include it.
MYPYC_MODULES = []
try:
    import charset_normalizer  # noqa: E402 — build-time dependency probe
    _site = os.path.dirname(os.path.dirname(charset_normalizer.__file__))
    MYPYC_MODULES = [os.path.basename(p).split(".")[0]
                     for p in glob.glob(os.path.join(_site, "*__mypyc.cpython-*.so"))]
except Exception:  # noqa: BLE001 — pure-python charset_normalizer needs nothing extra
    MYPYC_MODULES = []

# Embed the OAuth client into the bundle when one is present at build time (the
# release CI writes it from the GOOGLE_CLIENT_SECRET_JSON secret). Lands in
# Contents/Resources/, where config._resolve_client_secret_path() finds it — so
# the distributed app ships its own OAuth client and end users do no Google
# setup. Without it, the build is a "bring-your-own client_secret.json" app.
RESOURCES = [f for f in ["client_secret.json"] if os.path.exists(f)]

OPTIONS = {
    "plist": {
        "CFBundleName": "DocToPDF",
        "CFBundleDisplayName": "DocToPDF",
        "CFBundleIdentifier": "com.doctopdf.app",
        "CFBundleShortVersionString": "0.1.8",
        "CFBundleVersion": "0.1.8",
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
        # Web-page monitoring (static + HTML extraction). Listed as full packages
        # so their data files / native modules come along. charset_normalizer
        # ships mypyc-compiled .so accelerators that must be copied whole.
        "trafilatura",
        "bs4",
        "lxml",
        "charset_normalizer",
        # justext is trafilatura's fallback extractor (favor_recall path); it
        # reads stoplist files from its own dir, so it must be a real package
        # dir, not zipped.
        "justext",
    ],
    "includes": [
        "nh3",
        "requests",
        "google.auth",
        "google.auth.transport.requests",
        "google.oauth2.credentials",
        "google_auth_httplib2",
        *MYPYC_MODULES,   # charset_normalizer's mypyc shared module(s)
    ],
    # Optional/heavy deps the app imports lazily and tolerates missing:
    #   chromadb/onnxruntime → RAG (huge native deps); playwright → browser
    #   renderer (needs separate browser binaries); mcp → CLI-only server.
    "excludes": [
        "chromadb",
        "playwright",
        "onnxruntime",
        "mcp",
        "tkinter",
        "pytest",
    ],
    # Embedded OAuth client (only when present at build time).
    "resources": RESOURCES,
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
