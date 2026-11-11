"""Accounts, API keys, devices/pairing, sources and the audit log."""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from . import db
from .crypto import hash_key, new_key
from .util import utcnow

SOURCE_STATUSES = ("connecting", "initial_scan", "live", "degraded", "paused", "error")

DEFAULT_SOURCE_SETTINGS: dict[str, Any] = {
    "ignore": [],
    "notifications": {"enabled": True, "types": ["added"], "quiet_folders": []},
    "content": {"enabled": True, "extensions": [], "max_bytes": None, "skip_folders": []},
}


class AuthError(Exception):
    pass


# ------------------------------------------------------------------ accounts & keys
def create_account(name: str, email: str | None = None, org_id: str | None = None) -> tuple[dict, str]:
    """Create an account (provisions its index space) and return it with a first admin key."""
    with db.system() as conn:
        acct = conn.execute(
            "INSERT INTO drive_index.accounts (name, email, org_id) VALUES (%s,%s,%s) RETURNING *",
            (name, email, org_id),
        ).fetchone()
    with db.scoped(acct["id"]) as conn:
        key = issue_key(conn, acct["id"], "admin", "initial admin key")
        if email:
            add_member(conn, acct["id"], email, name=name, role="admin", actor="system")
        audit(conn, acct["id"], "system", "account.created", str(acct["id"]), {"name": name})
    return acct, key


def normalize_email(email: str) -> str:
    return email.strip().lower()


def add_member(conn: Connection, account_id, email: str, *, name: str | None = None,
               role: str = "admin", actor: str = "admin") -> dict:
    email = normalize_email(email)
    if "@" not in email:
        raise ValueError("invalid email")
    if role not in ("admin", "member"):
        raise ValueError("role must be admin or member")
    row = conn.execute(
        """INSERT INTO drive_index.members (account_id, email, name, role)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (account_id, email) DO UPDATE
             SET name = COALESCE(EXCLUDED.name, drive_index.members.name),
                 role = EXCLUDED.role,
                 deactivated_at = NULL
           RETURNING *""",
        (str(account_id), email, name, role),
    ).fetchone()
    audit(conn, account_id, actor, "member.added", str(row["id"]), {"email": email, "role": role})
    return row


def list_members(conn: Connection) -> list[dict]:
    return conn.execute(
        """SELECT id, email, name, role, created_at, deactivated_at
           FROM drive_index.members ORDER BY created_at"""
    ).fetchall()


def deactivate_member(conn: Connection, account_id, member_id, actor: str) -> None:
    conn.execute(
        "UPDATE drive_index.members SET deactivated_at = now() WHERE id = %s AND deactivated_at IS NULL",
        (str(member_id),),
    )
    audit(conn, account_id, actor, "member.deactivated", str(member_id))


def find_active_members_by_email(email: str) -> list[dict]:
    """System lookup: every active membership for this address (for magic-link)."""
    email = normalize_email(email)
    with db.system() as conn:
        return conn.execute(
            """SELECT m.*, a.name AS account_name
               FROM drive_index.members m
               JOIN drive_index.accounts a ON a.id = m.account_id
               WHERE lower(m.email) = %s AND m.deactivated_at IS NULL
               ORDER BY m.created_at""",
            (email,),
        ).fetchall()


def create_login_token(member: dict, ttl_minutes: int = 30) -> str:
    """Create a one-time login token; return the raw secret (shown once / emailed)."""
    raw = secrets.token_urlsafe(32)
    with db.system() as conn:
        conn.execute(
            """INSERT INTO drive_index.login_tokens
                 (account_id, member_id, email, token_hash, expires_at)
               VALUES (%s,%s,%s,%s,%s)""",
            (str(member["account_id"]), str(member["id"]), member["email"],
             hash_key(raw), utcnow() + timedelta(minutes=ttl_minutes)),
        )
    return raw


def redeem_login_token(token: str) -> tuple[dict, str]:
    """Mark a login token used and issue a fresh admin web key. Returns (member, key)."""
    if not token or len(token) < 10:
        raise AuthError("invalid sign-in link")
    with db.system() as conn:
        row = conn.execute(
            """SELECT * FROM drive_index.login_tokens
               WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
               FOR UPDATE""",
            (hash_key(token),),
        ).fetchone()
        if not row:
            raise AuthError("That sign-in link has expired or was already used")
        member = conn.execute(
            "SELECT * FROM drive_index.members WHERE id = %s AND deactivated_at IS NULL",
            (row["member_id"],),
        ).fetchone()
        if not member:
            raise AuthError("That sign-in link has expired or was already used")
        updated = conn.execute(
            "UPDATE drive_index.login_tokens SET used_at = now() WHERE id = %s AND used_at IS NULL RETURNING id",
            (row["id"],),
        ).fetchone()
        if not updated:
            raise AuthError("That sign-in link has expired or was already used")
        account_id = member["account_id"]
        email = member["email"]
    with db.scoped(account_id) as conn:
        # One durable web key per email: revoke prior web sessions for this member.
        conn.execute(
            """UPDATE drive_index.api_keys SET revoked_at = now()
               WHERE account_id = %s AND kind = 'admin' AND name = %s AND revoked_at IS NULL""",
            (str(account_id), f"web:{email}"),
        )
        key = issue_key(conn, account_id, "admin", f"web:{email}")
        audit(conn, account_id, f"member:{member['id']}", "auth.login", str(member["id"]), {"email": email})
    return member, key


def issue_key(conn: Connection, account_id, kind: str, name: str, device_id=None) -> str:
    prefix = {"admin": "nd_admin", "session": "nd_sess", "device": "nd_dev"}[kind]
    key = new_key(prefix)
    conn.execute(
        """INSERT INTO drive_index.api_keys (account_id, kind, name, key_hash, key_prefix, device_id)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (str(account_id), kind, name, hash_key(key), key[:16], str(device_id) if device_id else None),
    )
    return key


def resolve_key(key: str, kinds: tuple[str, ...] | None = None) -> dict:
    """Map a bearer key to {account_id, key_id, kind, device_id, name}. System lookup by hash."""
    if not key:
        raise AuthError("missing credentials")
    with db.system() as conn:
        row = conn.execute(
            """UPDATE drive_index.api_keys SET last_used_at = now()
               WHERE key_hash = %s AND revoked_at IS NULL
               RETURNING id, account_id, kind, device_id, name""",
            (hash_key(key),),
        ).fetchone()
        if row and row["device_id"]:
            dev = conn.execute(
                "SELECT revoked_at FROM drive_index.devices WHERE id = %s", (row["device_id"],)
            ).fetchone()
            if not dev or dev["revoked_at"]:
                row = None
    if not row:
        raise AuthError("invalid or revoked key")
    if kinds and row["kind"] not in kinds:
        raise AuthError(f"key kind {row['kind']} not allowed here")
    return {"account_id": row["account_id"], "key_id": row["id"], "kind": row["kind"],
            "device_id": row["device_id"], "name": row["name"]}


def audit(conn: Connection, account_id, actor: str, action: str, target: str | None = None,
          detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO drive_index.audit_log (account_id, actor, action, target, detail) VALUES (%s,%s,%s,%s,%s)",
        (str(account_id), actor, action, target, Jsonb(detail or {})),
    )


# ------------------------------------------------------------------ devices & pairing
def create_pairing_code(conn: Connection, account_id, device_name: str | None = None,
                        ttl_minutes: int = 15) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    conn.execute(
        """INSERT INTO drive_index.pairing_codes (code_hash, account_id, device_name, expires_at)
           VALUES (%s,%s,%s,%s)""",
        (hash_key(code), str(account_id), device_name, utcnow() + timedelta(minutes=ttl_minutes)),
    )
    return f"{code[:4]}-{code[4:]}"


def redeem_pairing_code(code: str, machine_id: str, hostname: str | None, os_name: str | None,
                        agent_version: str | None, device_name: str | None = None) -> dict:
    """Exchange a one-time code for a device token. Re-pairing the same machine keeps its device row/name."""
    norm = code.replace("-", "").replace(" ", "").upper()
    with db.system() as conn:
        pc = conn.execute(
            """SELECT * FROM drive_index.pairing_codes WHERE code_hash = %s AND used_at IS NULL
               AND expires_at > now() FOR UPDATE""",
            (hash_key(norm),),
        ).fetchone()
        if not pc:
            raise AuthError("pairing code invalid or expired")
        account_id = pc["account_id"]
    with db.scoped(account_id) as conn:
        dev = conn.execute(
            "SELECT * FROM drive_index.devices WHERE machine_id = %s", (machine_id,)
        ).fetchone()
        if dev:
            dev = conn.execute(
                """UPDATE drive_index.devices SET hostname = %s, os = %s, agent_version = %s,
                     revoked_at = NULL, last_seen_at = now(), name = coalesce(%s, name)
                   WHERE id = %s RETURNING *""",
                (hostname, os_name, agent_version, pc["device_name"], dev["id"]),
            ).fetchone()
            # Old device tokens stop working once the machine re-pairs.
            conn.execute(
                "UPDATE drive_index.api_keys SET revoked_at = now() WHERE device_id = %s AND revoked_at IS NULL",
                (dev["id"],),
            )
            repaired = True
        else:
            name = pc["device_name"] or device_name or hostname or "New device"
            dev = conn.execute(
                """INSERT INTO drive_index.devices (account_id, name, machine_id, hostname, os, agent_version,
                     last_seen_at) VALUES (%s,%s,%s,%s,%s,%s, now()) RETURNING *""",
                (str(account_id), name, machine_id, hostname, os_name, agent_version),
            ).fetchone()
            repaired = False
        token = issue_key(conn, account_id, "device", f"device {dev['name']}", device_id=dev["id"])
        conn.execute(
            "UPDATE drive_index.pairing_codes SET used_at = now(), device_id = %s WHERE code_hash = %s",
            (dev["id"], hash_key(norm)),
        )
        audit(conn, account_id, f"device:{dev['id']}", "device.paired", str(dev["id"]),
              {"name": dev["name"], "repaired": repaired})
    return {"device": dev, "token": token, "repaired": repaired}


def revoke_device(conn: Connection, account_id, device_id, actor: str) -> None:
    conn.execute("UPDATE drive_index.devices SET revoked_at = now() WHERE id = %s", (str(device_id),))
    conn.execute(
        "UPDATE drive_index.api_keys SET revoked_at = now() WHERE device_id = %s AND revoked_at IS NULL",
        (str(device_id),),
    )
    audit(conn, account_id, actor, "device.revoked", str(device_id))


# ------------------------------------------------------------------ sources
def merged_settings(settings: dict | None) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULT_SOURCE_SETTINGS.items()}
    for k, v in (settings or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def create_source(conn: Connection, account_id, *, type_: str, root_kind: str, root_ref: str, name: str,
                  connection_id=None, device_id=None, settings: dict | None = None,
                  actor: str = "admin") -> dict:
    if type_ == "local":
        _check_local_overlap(conn, device_id, root_ref)
    src = conn.execute(
        """INSERT INTO drive_index.sources (account_id, type, root_kind, root_ref, name, connection_id,
             device_id, settings, status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'connecting') RETURNING *""",
        (str(account_id), type_, root_kind, root_ref, name, str(connection_id) if connection_id else None,
         str(device_id) if device_id else None, Jsonb(merged_settings(settings))),
    ).fetchone()
    conn.execute(
        "INSERT INTO drive_index.sync_cursors (source_id, account_id) VALUES (%s,%s)",
        (src["id"], str(account_id)),
    )
    audit(conn, account_id, actor, "source.added", str(src["id"]),
          {"type": type_, "root_kind": root_kind, "root_ref": root_ref, "name": name})
    return src


def _norm_local(p: str) -> str:
    p = p.replace("\\", "/").rstrip("/")
    return p.lower() if os.name == "nt" else p


def _check_local_overlap(conn: Connection, device_id, root_ref: str) -> None:
    """Two watched folders on one device must not nest (edge case: overlapping roots)."""
    new = _norm_local(root_ref)
    rows = conn.execute(
        "SELECT id, root_ref FROM drive_index.sources WHERE type = 'local' AND device_id IS NOT DISTINCT FROM %s::uuid",
        (str(device_id) if device_id else None,),
    ).fetchall()
    for r in rows:
        old = _norm_local(r["root_ref"])
        if new == old or new.startswith(old + "/") or old.startswith(new + "/"):
            raise ValueError(f"folder overlaps an already watched folder: {r['root_ref']}")


def set_status(conn: Connection, source_id, status: str, reason: str | None = None) -> None:
    assert status in SOURCE_STATUSES
    conn.execute(
        """UPDATE drive_index.sources SET status = %s, status_reason = %s, updated_at = now(),
             status_since = CASE WHEN status IS DISTINCT FROM %s THEN now() ELSE status_since END
           WHERE id = %s""",
        (status, reason, status, str(source_id)),
    )


def get_source(conn: Connection, source_id) -> dict | None:
    return conn.execute("SELECT * FROM drive_index.sources WHERE id = %s", (str(source_id),)).fetchone()


def update_source_settings(conn: Connection, account_id, source_id, patch: dict, actor: str) -> dict:
    src = get_source(conn, source_id)
    if not src:
        raise KeyError("source not found")
    new = merged_settings(src["settings"])
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(new.get(k), dict):
            new[k] = {**new[k], **v}
        else:
            new[k] = v
    row = conn.execute(
        "UPDATE drive_index.sources SET settings = %s, updated_at = now() WHERE id = %s RETURNING *",
        (Jsonb(new), str(source_id)),
    ).fetchone()
    audit(conn, account_id, actor, "source.settings", str(source_id), patch)
    return row


def remove_source(conn: Connection, account_id, source_id, actor: str) -> None:
    """FR-3: deletes its index rows and revokes stored tokens (if no other source uses the connection)."""
    src = get_source(conn, source_id)
    if not src:
        raise KeyError("source not found")
    conn.execute("DELETE FROM drive_index.sources WHERE id = %s", (str(source_id),))
    if src["connection_id"]:
        others = conn.execute(
            "SELECT count(*) AS n FROM drive_index.sources WHERE connection_id = %s", (src["connection_id"],)
        ).fetchone()["n"]
        if others == 0:
            conn.execute("DELETE FROM drive_index.credentials WHERE connection_id = %s", (src["connection_id"],))
            conn.execute(
                "UPDATE drive_index.drive_connections SET revoked_at = now() WHERE id = %s",
                (src["connection_id"],),
            )
    audit(conn, account_id, actor, "source.removed", str(source_id), {"name": src["name"], "type": src["type"]})
