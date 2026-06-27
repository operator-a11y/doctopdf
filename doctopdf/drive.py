"""Google Drive auth + metadata/export helpers for DocToPDF.

Everything here is plain, synchronous, and side-effect-light so it can be called
from a background worker thread. UI concerns live in ``app.py``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import config

# A Google Docs export to PDF is capped by the Drive API at 10 MB.
EXPORT_SIZE_LIMIT = 10 * 1024 * 1024

# Matches the file/folder id in a Google URL, e.g.
#   https://docs.google.com/document/d/<ID>/edit
#   https://drive.google.com/drive/folders/<ID>
_URL_ID_RE = re.compile(r"/(?:d|folders)/([a-zA-Z0-9_-]+)")
# The id=… query form, e.g. https://drive.google.com/open?id=<ID>
_QUERY_ID_RE = re.compile(r"[?&]id=([a-zA-Z0-9_-]+)")
# A bare id is a run of URL-safe id characters.
_BARE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


class DriveError(Exception):
    """A user-facing Drive problem with a short message suitable for the menu."""


class AuthFlowError(DriveError):
    """The interactive OAuth flow ran and failed (e.g. the user closed the window).

    Distinct from a plain :class:`DriveError` so the app can stop re-launching the
    browser on every poll and instead wait for an explicit user retry.
    """


class ReauthRequired(DriveError):
    """Credentials went stale mid-session (refresh failed / HTTP 401).

    Signals the app to drop the built service and re-run auth on the next cycle —
    a silent refresh if possible, otherwise a fresh interactive flow. Without
    this, the transport's failed auto-refresh would loop forever behind an
    already-built service object.
    """


def parse_doc_id(text: str) -> Optional[str]:
    """Extract a Google file/folder id from a Doc/Sheet/Slides/Drive URL, the
    ``open?id=`` form, or accept a bare id. Returns ``None`` if none found.
    """
    if not text:
        return None
    text = text.strip()
    for regex in (_URL_ID_RE, _QUERY_ID_RE):
        match = regex.search(text)
        if match:
            return match.group(1)
    if _BARE_ID_RE.match(text):
        return text
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a rename inside it is durable.

    ``os.replace`` makes a swap atomic but not necessarily durable across a crash
    until the *directory* entry is flushed; callers that delete a source file
    after writing a replacement (e.g. token migration) rely on this so a power
    loss can't leave the replacement's directory entry unwritten.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_token(path: Path, creds: Credentials) -> None:
    """Atomically write credentials to ``path`` with 0600 perms.

    Writes to a fresh, exclusively-created temp file (mode 0600 from birth) and
    ``os.replace`` it into place. The destination inherits the temp file's tight
    perms, so there is never a world-readable window — even when overwriting a
    pre-existing token that had looser perms. ``path`` is parameterized so the
    single-token (legacy) and per-account (``tokens/<email>.json``) callers share
    one hardened writer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # O_EXCL: never reuse a leftover temp with unknown perms.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)  # make the rename durable before any source delete
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_token(creds: Credentials) -> None:
    """Persist credentials to the legacy single-token path (back-compat)."""
    write_token(config.TOKEN_PATH, creds)


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
    creds = run_install_flow()
    _save_token(creds)
    return creds


def run_install_flow() -> Credentials:
    """Run the interactive loopback OAuth flow and return fresh credentials.

    Opens a browser to authorize read-only Drive access using the shared
    ``client_secret.json`` app identity — the same identity authorizes any number
    of accounts, so this is reused verbatim when adding each account. Raises
    :class:`DriveError` if the client secret is missing and :class:`AuthFlowError`
    if the flow itself fails (e.g. the user closes the window). The caller is
    responsible for persisting the returned token.
    """
    if not config.CLIENT_SECRET_PATH.exists():
        raise DriveError(
            "Missing client_secret.json — see the README to create OAuth credentials."
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config.CLIENT_SECRET_PATH), config.SCOPES
        )
        return flow.run_local_server(port=0)
    except Exception as exc:  # noqa: BLE001 — surface any flow failure as a clean message
        raise AuthFlowError(f"Authorization failed: {exc}") from exc


def maybe_persist_refreshed_token(creds: Credentials, path: Optional[Path] = None) -> None:
    """Persist the token if the API client refreshed it under us.

    The googleapiclient transport auto-refreshes an expired access token in
    memory but does not write it back to disk; call this after a successful
    request so a freshly minted access token survives a restart. ``path`` selects
    which token file to compare/update (per-account callers pass that account's
    ``tokens/<email>.json``); it defaults to the legacy single-token path.
    """
    path = path or config.TOKEN_PATH
    try:
        on_disk = ""
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                on_disk = fh.read()
        if creds.to_json() != on_disk:
            write_token(path, creds)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Drive operations
# ---------------------------------------------------------------------------


def build_service(creds: Credentials):
    """Build a Drive v3 service client."""
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_about_user(service) -> dict:
    """Identify the account behind ``service`` as ``{email, permission_id}``.

    Uses the Drive ``about.get`` endpoint, which is covered by the existing
    ``drive.readonly`` scope — so multiple accounts are distinguished without any
    new OAuth scope or Google Cloud setup. ``emailAddress`` is the human label /
    key; ``permissionId`` is a stable id used to dedupe the same account added
    twice. Refresh/401 failures surface as :class:`ReauthRequired`; transient
    network failures as a retryable :class:`DriveError`.
    """
    about = _call(
        lambda: service.about().get(fields="user(emailAddress,permissionId)").execute(),
        "account",
    )
    user = about.get("user") or {}
    return {"email": user.get("emailAddress"), "permission_id": user.get("permissionId")}


def _call(fn: Callable, file_id: str):
    """Execute a Drive API request, mapping every failure to a clean exception.

    - A failed token refresh (revoked/expired refresh token) or an HTTP 401
      becomes :class:`ReauthRequired` so the app can re-authorize and recover.
    - Other HTTP errors become a :class:`DriveError` with a short menu message.
    - Transport/network failures (timeouts, DNS, TLS, broken pipe) become a
      retryable ``DriveError`` instead of an opaque ``Unexpected: …``.
    """
    try:
        return fn()
    except RefreshError as exc:
        raise ReauthRequired("Re-authorizing…") from exc
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 401:
            raise ReauthRequired("Re-authorizing…") from exc
        raise DriveError(_http_message(exc, file_id)) from exc
    except DriveError:
        raise
    except Exception as exc:  # noqa: BLE001 — socket/ssl/httplib2/etc. are retryable
        raise DriveError("Network error — will retry.") from exc


def get_file_metadata(service, file_id: str) -> dict:
    """Return ``{id, name, modifiedTime, mimeType, lastModifyingUser(displayName)}``."""
    return _call(
        lambda: service.files()
        .get(fileId=file_id,
             fields="id, name, modifiedTime, mimeType, lastModifyingUser(displayName)")
        .execute(),
        file_id,
    )


def list_folder(service, folder_id: str) -> list[dict]:
    """List non-trashed files directly in a folder.

    Returns ``[{id, name, modifiedTime, mimeType, lastModifyingUser(displayName)}, …]``
    across all pages.
    """
    def _fetch():
        items, token = [], None
        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, modifiedTime, mimeType, "
                           "lastModifyingUser(displayName))",
                    pageSize=200,
                    orderBy="name",
                    pageToken=token,
                )
                .execute()
            )
            items.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return items

    return _call(_fetch, folder_id)


def export(service, file_id: str, mime_type: str) -> bytes:
    """Export the Google Doc to the given MIME type and return the bytes.

    Raises :class:`DriveError` on failure. The 10 MB cap is enforced server-side
    (a 403 ``exportSizeLimitExceeded`` raised before any bytes return); the
    ``len`` check below is a defensive backstop only.
    """
    data = _call(
        lambda: service.files().export(fileId=file_id, mimeType=mime_type).execute(),
        file_id,
    )
    if not isinstance(data, (bytes, bytearray)):
        raise DriveError("Export returned no data.")
    if len(data) > EXPORT_SIZE_LIMIT:
        raise DriveError("Doc is too large to export (>10 MB cap).")
    return bytes(data)


def export_pdf(service, file_id: str) -> bytes:
    """Export the Google Doc to PDF bytes."""
    return export(service, file_id, "application/pdf")


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
