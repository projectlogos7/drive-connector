"""Outbound email via Resend (magic-link sign-in)."""

from __future__ import annotations

import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


def send_mail(*, to: str, subject: str, text: str, html: str) -> bool:
    s = get_settings()
    if not s.resend_api_key:
        log.warning("RESEND_API_KEY not set; not sending mail to %s (%s)", to, subject)
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {s.resend_api_key}", "Content-Type": "application/json"},
            json={"from": s.email_from, "to": [to], "subject": subject, "text": text, "html": html},
            timeout=20,
        )
    except Exception as e:
        log.error("resend request failed: %s", e)
        return False
    if r.status_code >= 300:
        log.error("resend %s: %s", r.status_code, r.text[:400])
        return False
    return True


def send_sign_in(to: str, account_name: str, url: str) -> bool:
    subject = f"Sign in to {account_name}"
    lead = f"Sign in to {account_name}. This link works once and expires in 30 minutes."
    text = f"{lead}\n\n{url}\n\nIf you didn't ask for this, you can ignore this email."
    html = (
        f'<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5;color:#1c1917">'
        f"<p>{_esc(lead)}</p>"
        f'<p><a href="{_esc(url)}">Sign in</a></p>'
        f'<p style="color:#57534e">If you didn\'t ask for this, you can ignore this email.</p></div>'
    )
    return send_mail(to=to, subject=subject, text=text, html=html)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
