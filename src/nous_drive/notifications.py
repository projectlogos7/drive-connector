"""New-file notifications (FR-33..36) and health alerts.

Change events are batched per source: a notification goes out once the oldest un-notified change is
older than the batching window, so a 200-file upload becomes one message with folder and counts.
Delivery: rows in ``notifications`` (read by the MCP server, which pushes resource-updated events to
connected clients) plus optional Slack, email and generic webhook channels.
"""

from __future__ import annotations

import json
import logging
import posixpath
import smtplib
from collections import Counter, defaultdict
from datetime import timedelta
from email.message import EmailMessage

import httpx
from psycopg.types.json import Jsonb

from . import db
from .accounts import merged_settings
from .config import get_settings
from .util import utcnow

log = logging.getLogger(__name__)

VERBS = {"added": "added", "modified": "changed", "moved": "moved", "renamed": "renamed",
         "deleted": "deleted", "trashed": "trashed", "restored": "restored", "removed": "removed"}


def _quiet(path: str | None, quiet: list[str]) -> bool:
    if not path:
        return False
    for q in quiet:
        q = "/" + q.strip("/")
        if path == q or path.startswith(q + "/"):
            return True
    return False


def compose(source: dict, events: list[dict], device_name: str | None) -> tuple[str, str, dict]:
    by_type = Counter(e["type"] for e in events)
    folders: dict[str, Counter] = defaultdict(Counter)
    for e in events:
        folders[posixpath.dirname(e["path"] or "/") or "/"][e["type"]] += 1
    parts = [f"{n} {'file' if n == 1 else 'files'} {VERBS.get(t, t)}" for t, n in by_type.most_common()]
    where = f"{source['name']}" + (f" ({device_name})" if device_name else "")
    title = f"{', '.join(parts)} in {where}"
    lines = []
    for folder, cnt in sorted(folders.items(), key=lambda kv: -sum(kv[1].values()))[:8]:
        desc = ", ".join(f"{n} {VERBS.get(t, t)}" for t, n in cnt.most_common())
        lines.append(f"• {folder} — {desc}")
    if len(folders) > 8:
        lines.append(f"• …and {len(folders) - 8} more folders")
    sample = [e["name"] for e in events if e["type"] == "added"][:5]
    if sample:
        lines.append("New: " + ", ".join(sample) + ("…" if by_type.get("added", 0) > 5 else ""))
    lines.append('Ask Claude: "what\'s new?" (whats_new) to see and describe these files.')
    payload = {
        "source_id": str(source["id"]), "source": source["name"], "device": device_name,
        "counts": dict(by_type), "folders": {k: dict(v) for k, v in folders.items()},
        "first_event_id": min(e["id"] for e in events), "last_event_id": max(e["id"] for e in events),
        "since": min(e["detected_at"] for e in events).isoformat(),
        "follow_up": {"tool": "whats_new", "arguments": {"source": str(source["id"]),
                                                          "since": min(e["detected_at"] for e in events).isoformat()}},
    }
    return title, "\n".join(lines), payload


def deliver(note: dict) -> dict:
    """Send a notification to the configured out-of-MCP channels. Returns a delivery report."""
    s = get_settings()
    report: dict[str, str] = {"mcp": "queued"}
    text = f"*{note['title']}*\n{note['body']}"
    if s.slack_webhook_url:
        try:
            r = httpx.post(s.slack_webhook_url, json={"text": text}, timeout=10)
            report["slack"] = "ok" if r.status_code < 300 else f"http {r.status_code}"
        except Exception as e:
            report["slack"] = f"error: {e}"
    if s.notify_webhook_url:
        try:
            r = httpx.post(s.notify_webhook_url, json={k: (v.isoformat() if hasattr(v, "isoformat") else v)
                                                      for k, v in note.items()}, timeout=10)
            report["webhook"] = "ok" if r.status_code < 300 else f"http {r.status_code}"
        except Exception as e:
            report["webhook"] = f"error: {e}"
    if s.smtp_host and s.notify_email_to:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[Nous Drive] {note['title']}"
            msg["From"] = s.smtp_from or s.smtp_user
            msg["To"] = s.notify_email_to
            msg.set_content(note["body"])
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password or "")
                smtp.send_message(msg)
            report["email"] = "ok"
        except Exception as e:
            report["email"] = f"error: {e}"
    return report


def _insert_and_deliver(conn, account_id, source_id, kind: str, title: str, body: str, payload: dict) -> dict:
    note = conn.execute(
        """INSERT INTO drive_index.notifications (account_id, source_id, kind, title, body, payload)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
        (str(account_id), str(source_id) if source_id else None, kind, title, body, Jsonb(payload)),
    ).fetchone()
    report = deliver(note)
    conn.execute("UPDATE drive_index.notifications SET deliveries = %s WHERE id = %s", (Jsonb(report), note["id"]))
    conn.execute("SELECT pg_notify('drive_index_notifications', %s)",
                 (json.dumps({"account_id": str(account_id), "id": note["id"]}),))
    return note


def flush(force: bool = False) -> int:
    """Send batched change notifications for every source whose batch window has elapsed."""
    s = get_settings()
    with db.system() as conn:
        sources = conn.execute(
            """SELECT s.id, s.account_id FROM drive_index.sources s
               JOIN drive_index.sync_cursors c ON c.source_id = s.id
               WHERE s.status IN ('live','degraded') AND (c.notified_xmin IS NULL OR EXISTS (
                 SELECT 1 FROM drive_index.change_events e WHERE e.source_id = s.id AND e.tx_id >= c.notified_xmin))"""
        ).fetchall()
    sent = 0
    for row in sources:
        with db.scoped(row["account_id"]) as conn:
            sent += _flush_source(conn, row["account_id"], row["id"], s.notify_window_seconds, force)
    return sent


def _flush_source(conn, account_id, source_id, window: int, force: bool) -> int:
    src = conn.execute("SELECT * FROM drive_index.sources WHERE id = %s", (source_id,)).fetchone()
    cur = conn.execute("SELECT * FROM drive_index.sync_cursors WHERE source_id = %s FOR UPDATE",
                       (source_id,)).fetchone()
    xmin = conn.execute("SELECT pg_snapshot_xmin(pg_current_snapshot()) AS x").fetchone()["x"]
    settings = merged_settings(src["settings"]).get("notifications", {})
    device = None
    if src["device_id"]:
        d = conn.execute("SELECT name FROM drive_index.devices WHERE id = %s", (src["device_id"],)).fetchone()
        device = d["name"] if d else None
    if cur["notified_xmin"] is None:
        # First flush after the initial index: announce it once, then notify only new changes.
        counts = conn.execute(
            """SELECT count(*) FILTER (WHERE kind = 'file') AS files, count(*) FILTER (WHERE kind = 'folder') AS folders
               FROM drive_index.nodes WHERE source_id = %s AND status = 'active'""",
            (source_id,),
        ).fetchone()
        if settings.get("enabled", True):
            where = src["name"] + (f" ({device})" if device else "")
            _insert_and_deliver(conn, account_id, source_id, "changes", f"Index ready: {where}",
                                f"{counts['files']} files in {counts['folders']} folders are now indexed and watched.",
                                {"source_id": str(source_id), "initial_index": True, **counts})
        conn.execute("UPDATE drive_index.sync_cursors SET notified_xmin = %s WHERE source_id = %s", (xmin, source_id))
        return 1
    events = conn.execute(
        """SELECT id, type, kind, name, path, detected_at FROM drive_index.change_events
           WHERE source_id = %s AND tx_id >= %s AND tx_id < %s ORDER BY id""",
        (source_id, cur["notified_xmin"], xmin),
    ).fetchall()
    if not events:
        conn.execute("UPDATE drive_index.sync_cursors SET notified_xmin = %s WHERE source_id = %s", (xmin, source_id))
        return 0
    oldest = min(e["detected_at"] for e in events)
    if not force and oldest > utcnow() - timedelta(seconds=window):
        return 0  # keep batching
    wanted = set(settings.get("types") or ["added"])
    quiet = settings.get("quiet_folders") or []
    relevant = [e for e in events if e["type"] in wanted and e["kind"] != "folder" and not _quiet(e["path"], quiet)]
    sent = 0
    if relevant and settings.get("enabled", True):
        title, body, payload = compose(src, relevant, device)
        _insert_and_deliver(conn, account_id, source_id, "changes", title, body, payload)
        sent = 1
    conn.execute("UPDATE drive_index.sync_cursors SET notified_xmin = %s WHERE source_id = %s", (xmin, source_id))
    return sent


def check_alerts() -> int:
    """Alert when a source is degraded > N minutes, or its device is offline for a long time while live."""
    s = get_settings()
    with db.system() as conn:
        rows = conn.execute(
            """SELECT s.*, d.name AS device_name, d.last_seen_at FROM drive_index.sources s
               LEFT JOIN drive_index.devices d ON d.id = s.device_id
               WHERE (s.status IN ('degraded','error') AND s.status_since < now() - make_interval(mins => %s))
                  OR (s.type = 'local' AND s.status NOT IN ('paused') AND d.last_seen_at < now() - make_interval(hours => %s))""",
            (s.degraded_alert_minutes, s.device_offline_alert_hours),
        ).fetchall()
    n = 0
    for src in rows:
        incident = f"{src['status']}:{src['status_since'].isoformat()}"
        with db.scoped(src["account_id"]) as conn:
            exists = conn.execute(
                """SELECT 1 FROM drive_index.notifications WHERE source_id = %s AND kind = 'alert'
                   AND payload->>'incident' = %s""",
                (src["id"], incident),
            ).fetchone()
            if exists:
                continue
            if src["status"] in ("degraded", "error"):
                title = f"Sync problem: {src['name']} is {src['status']}"
                body = f"Since {src['status_since']:%Y-%m-%d %H:%M} UTC. Reason: {src['status_reason'] or src['last_error'] or 'unknown'}."
            else:
                title = f"Device offline: {src['device_name']}"
                body = f"'{src['device_name']}' has not checked in since {src['last_seen_at']:%Y-%m-%d %H:%M} UTC; {src['name']} is not being watched."
            _insert_and_deliver(conn, src["account_id"], src["id"], "alert", title, body,
                                {"incident": incident, "source_id": str(src["id"])})
            n += 1
    return n
