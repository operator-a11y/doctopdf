"""Alert dispatch for DocToPDF — send change alerts where teams live.

Severity gating + delivery to Slack / Discord / generic webhooks and email
(SMTP). All delivery is best-effort: failures are returned as warning strings,
never raised, so watching is unaffected. Stdlib only (urllib, smtplib).
"""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Optional

SEVERITY_RANK = {"cosmetic": 0, "substantive": 1, "material": 2}
HTTP_TIMEOUT = 15
SMTP_TIMEOUT = 20


def passes(severity: Optional[str], threshold: Optional[str]) -> bool:
    """True if a change of ``severity`` should alert at the ``threshold``.

    Unclassified changes (severity None — e.g. AI off) pass only when the
    threshold is the lowest ("cosmetic" = alert on everything).
    """
    th = SEVERITY_RANK.get((threshold or "cosmetic").lower(), 0)
    if severity is None:
        return th == 0
    return SEVERITY_RANK.get(severity.lower(), 1) >= th


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT):
        pass


def _webhook_payload(url: str, subject: str, body: str) -> dict:
    u = url.lower()
    if "discord.com" in u or "discordapp.com" in u:
        return {"content": f"**{subject}**\n{body}"}     # Discord markdown
    if "hooks.slack.com" in u or "slack.com" in u:
        return {"text": f"*{subject}*\n{body}"}           # Slack mrkdwn
    return {"text": f"{subject}\n{body}"}                  # generic — no markup


def send_webhook(url: str, subject: str, body: str) -> Optional[str]:
    try:
        _post_json(url, _webhook_payload(url, subject, body))
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"webhook {url[:40]}…: {exc}"


# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------


def send_email(cfg: dict, subject: str, body: str) -> Optional[str]:
    host = cfg.get("smtp_host")
    to = cfg.get("email_to")
    if not host or not to:
        return None  # email not configured
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("email_from") or cfg.get("smtp_user") or to
    msg["To"] = to
    msg.set_content(body)
    try:
        port = int(cfg.get("smtp_port") or 587)
    except (TypeError, ValueError):
        port = 587
    user, pw = cfg.get("smtp_user"), cfg.get("smtp_pass")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT,
                                  context=ssl.create_default_context()) as s:
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as s:
                s.ehlo()
                secured = False
                try:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                    secured = True
                except smtplib.SMTPException:
                    pass  # server didn't offer STARTTLS
                # Never hand credentials to a cleartext channel (STARTTLS-strip).
                if user and not secured:
                    return ("email: refusing to send credentials unencrypted "
                            "(STARTTLS unavailable) — use port 465 or a TLS server")
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
        return None
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        return f"email: {exc}"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch(cfg: dict, subject: str, body: str) -> list[str]:
    """Send ``body`` to every configured destination. Returns warning strings
    for any that failed (never raises)."""
    warnings = []
    for url in (cfg.get("webhook_urls") or []):
        url = str(url).strip()
        if url:
            w = send_webhook(url, subject, body)
            if w:
                warnings.append(w)
    w = send_email(cfg, subject, body)
    if w:
        warnings.append(w)
    return warnings


def any_destination(cfg: dict) -> bool:
    """True if at least one external alert destination is configured."""
    return bool(cfg.get("webhook_urls")) or bool(cfg.get("smtp_host") and cfg.get("email_to"))
