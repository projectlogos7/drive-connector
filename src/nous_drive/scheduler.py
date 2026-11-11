"""Periodic work: Drive polling fallback, channel renewal, nightly reconcile, device health,
notification batching, alerts, retention and stale-job recovery.

Runs inside each worker process, but a Postgres advisory lock makes only one instance active.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from . import db, notifications, queue, retention, stats
from .config import get_settings
from .util import utcnow

log = logging.getLogger(__name__)
LOCK_KEY = 74_101_202  # arbitrary, stable


def _due(conn, task: str, every_seconds: int) -> bool:
    row = conn.execute("SELECT last_run FROM drive_index.scheduler_state WHERE task = %s FOR UPDATE",
                       (task,)).fetchone()
    now = utcnow()
    if row and row["last_run"] > now - timedelta(seconds=every_seconds):
        return False
    conn.execute(
        """INSERT INTO drive_index.scheduler_state (task, last_run) VALUES (%s, %s)
           ON CONFLICT (task) DO UPDATE SET last_run = EXCLUDED.last_run""",
        (task, now),
    )
    return True


def _busy(conn, source_id, kind: str) -> bool:
    """A job of this kind is already pending or running for the source (dedupe covers only pending)."""
    return conn.execute(
        """SELECT 1 FROM drive_index.index_jobs WHERE source_id = %s AND kind = %s
           AND status IN ('pending','running') LIMIT 1""", (source_id, kind)).fetchone() is not None


def schedule_drive(conn) -> int:
    s = get_settings()
    n = 0
    rows = conn.execute(
        """SELECT s.id, s.account_id, s.status, c.drive_page_token, c.last_poll_at, c.channel_id,
                  c.channel_expires_at, c.last_full_scan_at, c.scan_progress
           FROM drive_index.sources s JOIN drive_index.sync_cursors c ON c.source_id = s.id
           WHERE s.type = 'gdrive' AND s.status <> 'paused'"""
    ).fetchall()
    now = utcnow()
    push_possible = s.push_enabled and s.public_base_url.startswith("https://")
    for r in rows:
        sid, aid = r["id"], r["account_id"]
        crawling = (r["scan_progress"] or {}).get("state") == "running"
        if r["status"] == "connecting" or (r["status"] == "initial_scan" and not r["drive_page_token"]):
            if not _busy(conn, sid, "drive_initial_crawl") and \
                    queue.enqueue(conn, aid, "drive_initial_crawl", sid, dedupe_key=f"crawl:{sid}"):
                n += 1
            continue
        if r["status"] == "error" or not r["drive_page_token"] or crawling:
            continue
        healthy_channel = r["channel_id"] and r["channel_expires_at"] and r["channel_expires_at"] > now
        interval = s.drive_poll_seconds * (6 if healthy_channel else 1)
        if not r["last_poll_at"] or r["last_poll_at"] < now - timedelta(seconds=interval):
            if queue.enqueue(conn, aid, "drive_pull_changes", sid, dedupe_key=f"pull:{sid}"):
                n += 1
        if push_possible and (not healthy_channel or r["channel_expires_at"] < now + timedelta(
                seconds=s.drive_channel_renew_before_seconds)):
            queue.enqueue(conn, aid, "drive_renew_channel", sid, dedupe_key=f"renew:{sid}")
        if (not r["last_full_scan_at"] or r["last_full_scan_at"] < now - timedelta(hours=s.reconcile_hours)) \
                and not _busy(conn, sid, "drive_reconcile"):
            queue.enqueue(conn, aid, "drive_reconcile", sid, dedupe_key=f"reconcile:{sid}")
    return n


def check_devices(conn) -> int:
    s = get_settings()
    return conn.execute(
        """UPDATE drive_index.sources s SET status = 'degraded', status_reason = 'device offline',
             status_since = now()
           FROM drive_index.devices d
           WHERE d.id = s.device_id AND s.type = 'local' AND s.status = 'live'
             AND (d.last_seen_at IS NULL OR d.last_seen_at < now() - make_interval(secs => %s))""",
        (s.device_offline_seconds,),
    ).rowcount


def tick() -> dict:
    """One scheduler pass. Returns what it did (for logs/tests)."""
    did: dict = {}
    with db.system() as conn:
        got = conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_KEY,)).fetchone()["ok"]
        if not got:
            return {"skipped": "another scheduler is active"}
        if _due(conn, "drive", 30):
            did["drive_jobs"] = schedule_drive(conn)
        if _due(conn, "devices", 30):
            did["devices_offline"] = check_devices(conn)
    if True:
        did["stale_jobs"] = queue.requeue_stale()
    with db.system() as conn:
        run_notify = _due(conn, "notify", 20)
        run_alerts = _due(conn, "alerts", 120)
        run_retention = _due(conn, "retention", 86400)
        run_stats = _due(conn, "stats", 30)
    if run_stats:
        did["stats"] = stats.refresh_due()
    if run_notify:
        did["notifications"] = notifications.flush()
    if run_alerts:
        did["alerts"] = notifications.check_alerts()
    if run_retention:
        did["retention"] = retention.purge()
    return did
