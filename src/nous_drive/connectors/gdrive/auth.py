"""Google OAuth (offline access) and stored, encrypted credentials for a Drive connection."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx

from ... import db
from ...accounts import audit
from ...config import get_settings
from ...crypto import decrypt, encrypt, sign_state
from .client import SCOPE_METADATA, SCOPE_READONLY, DriveClient, TokenSource

log = logging.getLogger(__name__)


def phase_scopes(content: bool = False) -> list[str]:
    """Phase 1 asks only for metadata; content (Phase 2) is an explicit upgrade to read-only."""
    return [SCOPE_READONLY if content else SCOPE_METADATA, "openid", "email"]


def authorization_url(account_id: UUID | str, content: bool = False, connection_id: str | None = None) -> str:
    s = get_settings()
    if not s.google_client_id:
        raise RuntimeError("NOUS_DRIVE_GOOGLE_CLIENT_ID is not configured")
    state = sign_state({"a": str(account_id), "c": content, "conn": connection_id})
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(phase_scopes(content)),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{s.google_auth_url}?{urlencode(params)}"


def redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/oauth/google/callback"


def exchange_code(code: str, http: httpx.Client | None = None) -> dict:
    s = get_settings()
    http = http or httpx.Client(timeout=30)
    r = http.post(s.google_token_url, data={
        "code": code, "client_id": s.google_client_id, "client_secret": s.google_client_secret,
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
    })
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed: {r.text[:300]}")
    return r.json()


def store_connection(account_id, tokens: dict, connection_id: str | None = None, actor: str = "admin") -> dict:
    """Persist a connection and its encrypted tokens. Re-authorising an existing connection upgrades it."""
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    scopes = tokens.get("scope", "").split()
    with db.scoped(account_id) as conn:
        if connection_id:
            row = conn.execute(
                "UPDATE drive_index.drive_connections SET scopes = %s, revoked_at = NULL WHERE id = %s RETURNING *",
                (scopes, connection_id),
            ).fetchone()
            old = conn.execute(
                "SELECT encrypted_token FROM drive_index.credentials WHERE connection_id = %s", (connection_id,)
            ).fetchone()
            refresh = tokens.get("refresh_token") or (json.loads(decrypt(old["encrypted_token"]))["refresh_token"]
                                                     if old else None)
        else:
            row = conn.execute(
                "INSERT INTO drive_index.drive_connections (account_id, scopes) VALUES (%s,%s) RETURNING *",
                (str(account_id), scopes),
            ).fetchone()
            refresh = tokens.get("refresh_token")
        if not refresh:
            raise RuntimeError("Google did not return a refresh token (offline access was not granted)")
        blob = encrypt(json.dumps({"refresh_token": refresh, "access_token": tokens.get("access_token"),
                                   "expires_at": expires_at.isoformat()}))
        conn.execute(
            """INSERT INTO drive_index.credentials (account_id, connection_id, kind, encrypted_token, expires_at)
               VALUES (%s,%s,'google_oauth',%s,%s)
               ON CONFLICT (connection_id, kind) DO UPDATE SET encrypted_token = EXCLUDED.encrypted_token,
                 expires_at = EXCLUDED.expires_at, updated_at = now()""",
            (str(account_id), row["id"], blob, expires_at),
        )
        audit(conn, account_id, actor, "drive.connected", str(row["id"]), {"scopes": scopes})
    return row


def set_connection_identity(account_id, connection_id, email: str | None, user_id: str | None) -> None:
    with db.scoped(account_id) as conn:
        conn.execute(
            "UPDATE drive_index.drive_connections SET google_email = %s, google_user_id = %s WHERE id = %s",
            (email, user_id, str(connection_id)),
        )


def _load_tokens(account_id, connection_id) -> dict:
    with db.scoped(account_id) as conn:
        row = conn.execute(
            "SELECT encrypted_token FROM drive_index.credentials WHERE connection_id = %s AND kind = 'google_oauth'",
            (str(connection_id),),
        ).fetchone()
    if not row:
        raise RuntimeError("no stored credentials for this Drive connection")
    return json.loads(decrypt(row["encrypted_token"]))


def _save_access_token(account_id, connection_id, refresh_token: str):
    def cb(access_token: str, expires_at: datetime) -> None:
        blob = encrypt(json.dumps({"refresh_token": refresh_token, "access_token": access_token,
                                   "expires_at": expires_at.isoformat()}))
        with db.scoped(account_id) as conn:
            conn.execute(
                """UPDATE drive_index.credentials SET encrypted_token = %s, expires_at = %s, updated_at = now()
                   WHERE connection_id = %s AND kind = 'google_oauth'""",
                (blob, expires_at, str(connection_id)),
            )
    return cb


def default_client_factory(account_id, connection_id) -> DriveClient:
    t = _load_tokens(account_id, connection_id)
    exp = datetime.fromisoformat(t["expires_at"]) if t.get("expires_at") else None
    ts = TokenSource(t["refresh_token"], t.get("access_token"), exp,
                     on_refresh=_save_access_token(account_id, connection_id, t["refresh_token"]))
    return DriveClient(ts)


# Tests replace this with a factory returning a client bound to a fake Drive.
client_factory = default_client_factory


def make_client(account_id, connection_id) -> DriveClient:
    return client_factory(account_id, connection_id)


def revoke_remote(account_id, connection_id) -> None:
    """Best-effort revocation of the refresh token at Google (FR-3)."""
    try:
        t = _load_tokens(account_id, connection_id)
        httpx.post("https://oauth2.googleapis.com/revoke", params={"token": t["refresh_token"]}, timeout=10)
    except Exception as e:  # pragma: no cover - network best effort
        log.warning("could not revoke Google token: %s", e)


def has_content_scope(account_id, connection_id) -> bool:
    with db.scoped(account_id) as conn:
        row = conn.execute(
            "SELECT scopes FROM drive_index.drive_connections WHERE id = %s", (str(connection_id),)
        ).fetchone()
    return bool(row and SCOPE_READONLY in (row["scopes"] or []))
