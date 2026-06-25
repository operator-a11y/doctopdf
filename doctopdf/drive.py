"""Google Drive auth + metadata/export helpers for DocToPDF.

Everything here is plain, synchronous, and side-effect-light so it can be called
from a background worker thread. UI concerns live in ``app.py``.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config

# A Google Docs export to PDF is capped by the Drive API at 10 MB.
EXPORT_SIZE_LIMIT = 10 * 1024 * 1024

# Matches the file id in a typical Doc URL, e.g.
#   https://docs.google.com/document/d/<ID>/edit
_URL_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)")
# A bare id is a run of URL-safe id characters.
_BARE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


class DriveError(Exception):
    """A user-facing Drive problem with a short message suitable for the menu."""


class AuthFlowError(DriveError):
    """The interactive OAuth flow ran and failed (e.g. the user closed the window).

    Distinct from a plain :class:`DriveError` so the app can stop re-launching the
    browser on every poll and instead wait for an explicit user retry.
    """


def parse_doc_id(text: str) -> Optional[str]:
    """Extract a Google Doc id from a URL or accept a bare id.

    Returns ``None`` if nothing id-shaped can be found.
    """
    if not text:
        return None
    text = text.strip()
    match = _URL_ID_RE.search(text)
    if match:
        return match.group(1)
    if _BARE_ID_RE.match(text):
        return text
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _save_token(creds: Credentials) -> None:
    """Write credentials to ``token.json`` with 0600 perms."""
    # Create with restrictive perms from the start to avoid a brief world-readable
    # window between write and chmod.
    fd = os.open(config.TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
    finally:
        # Ensure perms even if the file pre-existed with looser perms.
        try:
            os.chmod(config.TOKEN_PATH, 0o600)
        except OSError:
            pass


def get_credentials() -> Credentials:
    """Return valid OAuth credentials, running the loopback flow if needed.

    On first run this opens a browser to authorize read-only Drive access; the
    resulting token is cached to ``token.json`` and refreshed automatically on
    subsequent runs. Raises :class:`DriveError` with a clear message on failure.
    """
    creds: Optional[Credentials] = None

    if config.TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(config.TOKEN_PATH), config.SCOPES
            )
        except (ValueError, OSError):
            creds = None  # corrupt token — fall through to a fresh flow

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            # Refresh failed (revoked/expired refresh token) — re-auth from scratch.
            creds = None

    # No usable token — run the interactive installed-app flow.
    if not config.CLIENT_SECRET_PATH.exists():
        raise DriveError(
            "Missing client_secret.json — see the README to create OAuth credentials."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config.CLIENT_SECRET_PATH), config.SCOPES
        )
        creds = flow.run_local_server(port=0)
    except Exception as exc:  # noqa: BLE001 — surface any flow failure as a clean message
        raise AuthFlowError(f"Authorization failed: {exc}") from exc

    _save_token(creds)
    return creds


def maybe_persist_refreshed_token(creds: Credentials) -> None:
    """Persist the token if the API client refreshed it under us.

    The googleapiclient transport auto-refreshes an expired access token in
    memory but does not write it back to disk; call this after a successful
    request so a freshly minted access token survives a restart.
    """
    try:
        on_disk = ""
        if config.TOKEN_PATH.exists():
            with open(config.TOKEN_PATH, "r", encoding="utf-8") as fh:
                on_disk = fh.read()
        if creds.to_json() != on_disk:
            _save_token(creds)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Drive operations
# ---------------------------------------------------------------------------


def build_service(creds: Credentials):
    """Build a Drive v3 service client."""
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_file_metadata(service, file_id: str) -> dict:
    """Return ``{id, name, modifiedTime, mimeType}`` for the file.

    Raises :class:`DriveError` with a friendly message on HTTP errors.
    """
    try:
        return (
            service.files()
            .get(fileId=file_id, fields="id, name, modifiedTime, mimeType")
            .execute()
        )
    except HttpError as exc:
        raise DriveError(_http_message(exc, file_id)) from exc


def export_pdf(service, file_id: str) -> bytes:
    """Export the Google Doc to PDF bytes.

    Raises :class:`DriveError` on HTTP errors (including the 10 MB export cap).
    """
    try:
        data = service.files().export(fileId=file_id, mimeType="application/pdf").execute()
    except HttpError as exc:
        raise DriveError(_http_message(exc, file_id)) from exc

    if not isinstance(data, (bytes, bytearray)):
        raise DriveError("Export returned no data.")
    if len(data) > EXPORT_SIZE_LIMIT:
        raise DriveError("Doc is too large to export (>10 MB cap).")
    return bytes(data)


def _http_message(exc: HttpError, file_id: str) -> str:
    """Translate an HttpError into a short, human message for the menu."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    # google-api-client exposes a parsed reason on some errors.
    reason = ""
    try:
        reason = (exc.error_details[0].get("reason", "") if exc.error_details else "")  # type: ignore[attr-defined]
    except Exception:
        reason = ""

    if status == 404:
        return "Doc not found — check the URL/ID and sharing."
    if status == 403:
        if "exportSizeLimit" in str(exc):
            return "Doc is too large to export (>10 MB cap)."
        return "Access denied — is the Doc shared with this account?"
    if status == 401:
        return "Auth expired — re-authorizing…"
    if status and 500 <= status < 600:
        return "Google server error — will retry."
    short = reason or (str(status) if status else "request failed")
    return f"Drive error ({short})."
