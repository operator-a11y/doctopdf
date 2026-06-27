"""Multi-account Google credential management for DocToPDF.

DocToPDF can authorize and watch sources across several Google accounts at once
(e.g. personal + school/work). The OAuth *app identity* (``client_secret.json``)
is shared — the same one authorizes any number of accounts — so only the **token**
is per-account. No new scope and no new Google Cloud setup: accounts are told
apart via the Drive ``about.get`` endpoint, which the existing ``drive.readonly``
scope already covers.

Storage (both in the project root, both gitignored):

- ``tokens/`` — one ``<sanitized-email>.json`` token file per account (0600).
- ``accounts.json`` — the index:
  ``{"accounts": [{email, permission_id, token_file, added_at, is_default}]}``.

This module owns the index and the per-account credential lifecycle; the Drive
flow primitives (interactive flow, atomic token writer, ``about.get`` identify)
live in :mod:`drive` and are reused here. UI lives in :mod:`app`.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from . import config, drive

# Per-account tokens + the index live next to the shared client_secret.json.
TOKENS_DIR = config.PROJECT_ROOT / "tokens"
ACCOUNTS_PATH = config.PROJECT_ROOT / "accounts.json"

# Serializes read-modify-write of the index so concurrent mutators (worker,
# menu, and the background auth thread) can't lose an update. Reads don't need
# it — _save swaps the file atomically (os.replace) so readers never tear.
_index_lock = threading.Lock()


class AccountAuthError(drive.DriveError):
    """An individual account's credentials are missing/unrecoverable.

    Subclasses :class:`drive.DriveError` (not :class:`drive.ReauthRequired`) so a
    single stale account surfaces as a *per-target* error the caller can report
    and offer to re-authorize — without bubbling up to blank out the other,
    healthy accounts.
    """


# ---------------------------------------------------------------------------
# Index persistence
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe(email: str) -> str:
    """A filesystem-safe basename derived from an email address."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", email or "")
    return base or "account"


def _alloc_token_file(email: str, permission_id: str, accounts: list[dict]) -> str:
    """Pick ``<sanitized-email>.json``, disambiguating only on a real collision
    with a *different* account (distinct accounts whose emails sanitize alike)."""
    name = f"{_safe(email)}.json"
    used = {a.get("token_file") for a in accounts if a.get("permission_id") != permission_id}
    if name in used:
        name = f"{_safe(email)}-{(permission_id or '')[:8]}.json"
    return name


def _load() -> list[dict]:
    """Return the accounts list, never raising on a missing/corrupt index."""
    try:
        with open(ACCOUNTS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    accounts = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(accounts, list):
        return []
    return [a for a in accounts
            if isinstance(a, dict) and a.get("email") and a.get("token_file")]


def _save(accounts: list[dict]) -> None:
    """Persist the index atomically (0600)."""
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_PATH.with_name(ACCOUNTS_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"accounts": accounts}, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, ACCOUNTS_PATH)
    drive.fsync_dir(ACCOUNTS_PATH.parent)  # make the rename durable before callers proceed
    try:
        os.chmod(ACCOUNTS_PATH, 0o600)
    except OSError:
        pass


def _ensure_one_default(accounts: list[dict]) -> None:
    """Guarantee exactly one ``is_default`` when there is ≥1 account."""
    if not accounts:
        return
    defaults = [a for a in accounts if a.get("is_default")]
    if len(defaults) == 1:
        return
    for a in accounts:
        a["is_default"] = False
    (defaults[0] if defaults else accounts[0])["is_default"] = True


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------


def list_accounts() -> list[dict]:
    return _load()


def get_account(email: Optional[str]) -> Optional[dict]:
    if not email:
        return None
    return next((a for a in _load() if a.get("email") == email), None)


def has_account(email: Optional[str]) -> bool:
    return get_account(email) is not None


def default_account() -> Optional[dict]:
    accounts = _load()
    if not accounts:
        return None
    return next((a for a in accounts if a.get("is_default")), accounts[0])


def default_key() -> Optional[str]:
    acct = default_account()
    return acct["email"] if acct else None


def token_path_for(email: str) -> Path:
    acct = get_account(email)
    name = acct["token_file"] if acct else f"{_safe(email)}.json"
    return TOKENS_DIR / name


def set_default(email: str) -> None:
    with _index_lock:
        accounts = _load()
        if not any(a.get("email") == email for a in accounts):
            return
        for a in accounts:
            a["is_default"] = (a.get("email") == email)
        _save(accounts)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def credentials_for(email: Optional[str] = None) -> Credentials:
    """Return valid credentials for one account, loading/refreshing its token.

    ``email`` selects the account; ``None`` uses the default. Refreshes a merely
    expired token (and persists it). Never runs the interactive flow — a missing
    or unrecoverable token raises :class:`AccountAuthError` so the caller can
    surface a per-account problem and offer targeted re-authorization. Each
    account's token is loaded independently, so one bad account doesn't disturb
    the others.
    """
    acct = get_account(email) if email else default_account()
    if acct is None:
        if email:
            raise AccountAuthError(f"{email}: not authorized — add this account under Accounts.")
        raise AccountAuthError("No Google account authorized — add one under Accounts.")
    path = TOKENS_DIR / acct["token_file"]
    if not path.exists():
        raise AccountAuthError(f"{acct['email']}: not authorized — re-authorize this account.")
    try:
        creds = Credentials.from_authorized_user_file(str(path), config.SCOPES)
    except (ValueError, OSError) as exc:
        raise AccountAuthError(
            f"{acct['email']}: stored token is unreadable — re-authorize this account.") from exc
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            drive.write_token(path, creds)
            return creds
        except RefreshError as exc:
            # The refresh token itself is revoked/expired — genuinely needs re-auth.
            raise AccountAuthError(
                f"{acct['email']}: session expired — re-authorize this account.") from exc
        except Exception as exc:  # noqa: BLE001 — transient network/transport: retry, don't re-auth
            raise drive.DriveError(
                f"{acct['email']}: network error — will retry.") from exc
    raise AccountAuthError(f"{acct['email']}: re-authorization required.")


def persist_if_refreshed(email: str, creds: Credentials) -> None:
    """Persist an in-memory-refreshed token to that account's file (best-effort)."""
    drive.maybe_persist_refreshed_token(creds, token_path_for(email))


def identify(creds: Credentials) -> dict:
    """Return ``{email, permission_id}`` for ``creds`` via Drive ``about.get``.

    Raises :class:`drive.DriveError` (incl. :class:`drive.ReauthRequired`) when
    the account can't be reached — callers treat that as "defer / retry", never
    as "discard the token".
    """
    info = drive.get_about_user(drive.build_service(creds))
    if not info.get("email") or not info.get("permission_id"):
        raise drive.DriveError("Couldn't identify the Google account.")
    return info


# ---------------------------------------------------------------------------
# Add / remove / migrate
# ---------------------------------------------------------------------------


def authorize_new_account() -> dict:
    """Run the interactive flow, identify the account, and register it.

    Reuses the shared ``client_secret.json`` flow. Dedupes by ``permission_id``:
    re-adding an already-known account refreshes its token in place rather than
    creating a duplicate entry. The first account added becomes the default.
    Returns the account dict. Propagates :class:`drive.AuthFlowError` (cancelled)
    and :class:`drive.DriveError` (e.g. offline identify / admin-blocked).
    """
    # The slow browser round-trip runs outside the index lock; only the
    # read-modify-write of the index below is serialized.
    creds = drive.run_install_flow()
    info = identify(creds)
    email, pid = info["email"], info["permission_id"]

    with _index_lock:
        accounts = _load()
        existing = next((a for a in accounts if a.get("permission_id") == pid), None)
        if existing is not None:
            drive.write_token(TOKENS_DIR / existing["token_file"], creds)
            existing["email"] = email           # display email may have changed
            acct = existing
        else:
            token_file = _alloc_token_file(email, pid, accounts)
            drive.write_token(TOKENS_DIR / token_file, creds)
            acct = {"email": email, "permission_id": pid, "token_file": token_file,
                    "added_at": _now(), "is_default": not accounts}
            accounts.append(acct)
        _ensure_one_default(accounts)
        _save(accounts)
    return acct


def remove_account(email: str) -> list[dict]:
    """Delete an account's token + index entry; promote a new default if needed.

    Returns the remaining accounts (the caller handles any orphaned targets).
    """
    with _index_lock:
        accounts = _load()
        acct = next((a for a in accounts if a.get("email") == email), None)
        if acct is None:
            return accounts
        _safe_unlink(TOKENS_DIR / acct["token_file"])
        remaining = [a for a in accounts if a.get("email") != email]
        # _ensure_one_default promotes a new default when the removed one was it.
        _ensure_one_default(remaining)
        _save(remaining)
    return remaining


def migrate_legacy_token() -> Optional[dict]:
    """Migrate a pre-multi-account ``token.json`` into ``tokens/<email>.json``.

    Idempotent and safe: identifies the legacy token (needs the account online),
    writes the per-account copy, registers it as the default, and only *then*
    removes the legacy file. If identification fails (offline / revoked), the
    legacy token is left untouched and migration is retried on the next run — the
    working token is never deleted before its migrated copy is confirmed written.
    Returns the migrated/known account, or ``None`` if there was nothing to do.
    """
    legacy = config.TOKEN_PATH
    if not legacy.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(legacy), config.SCOPES)
    except (ValueError, OSError):
        return None  # corrupt legacy token — nothing safe to migrate

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:  # noqa: BLE001 — defer; never delete an unconfirmed token
                return None
        else:
            return None

    try:
        info = identify(creds)
    except drive.DriveError:
        return None  # offline / transient — defer to the next successful run

    email, pid = info["email"], info["permission_id"]
    with _index_lock:
        accounts = _load()
        existing = next((a for a in accounts if a.get("permission_id") == pid), None)
        if existing is not None:
            # Already migrated on a prior run. Only drop the legacy file once a
            # per-account copy is confirmed on disk — if it went missing (and we
            # hold a valid legacy token), restore it first so we never delete the
            # last working credential.
            dest = TOKENS_DIR / existing["token_file"]
            if not dest.exists():
                drive.write_token(dest, creds)
            _safe_unlink(legacy)
            return existing

        token_file = _alloc_token_file(email, pid, accounts)
        drive.write_token(TOKENS_DIR / token_file, creds)  # atomic; confirmed on return
        acct = {"email": email, "permission_id": pid, "token_file": token_file,
                "added_at": _now(), "is_default": not accounts}
        accounts.append(acct)
        _ensure_one_default(accounts)
        _save(accounts)
        # Migrated copy is written + registered — now it's safe to drop the legacy.
        _safe_unlink(legacy)
    return acct
