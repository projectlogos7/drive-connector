"""Postgres-backed job queue (index_jobs) with SKIP LOCKED claiming, backoff and a dead-letter state."""

from __future__ import annotations

import logging
import socket
import os
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from . import db

log = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# Priorities: lower runs first.
PRIORITY = {
    "apply_events": 10,
    "finish_scan": 10,  # same as apply_events: strict FIFO per source
    "drive_pull_changes": 30,
    "drive_renew_channel": 40,
    "drive_initial_crawl": 50,
    "drive_reconcile": 60,
    "rebuild_paths": 60,
    "content_process": 90,
}


def enqueue(conn: Connection, account_id: UUID | str, kind: str, source_id: UUID | str | None = None,
            payload: dict[str, Any] | None = None, dedupe_key: str | None = None,
            delay_seconds: float = 0, max_attempts: int = 8) -> int | None:
    """Add a job. With ``dedupe_key`` a second pending job with the same key is not created."""
    row = conn.execute(
        """INSERT INTO drive_index.index_jobs
             (account_id, source_id, kind, payload, priority, dedupe_key, run_after, max_attempts)
           VALUES (%s,%s,%s,%s,%s,%s, now() + make_interval(secs => %s), %s)
           ON CONFLICT (dedupe_key) WHERE status = 'pending' AND dedupe_key IS NOT NULL DO NOTHING
           RETURNING id""",
        (str(account_id), str(source_id) if source_id else None, kind, Jsonb(payload or {}),
         PRIORITY.get(kind, 100), dedupe_key, delay_seconds, max_attempts),
    ).fetchone()
    return row["id"] if row else None


def claim(limit: int = 1, kinds: list[str] | None = None) -> list[dict]:
    """Claim ready jobs. Jobs of a source run one at a time, so per-source order is kept."""
    with db.system() as conn:
        rows = conn.execute(
            """UPDATE drive_index.index_jobs j SET status = 'running', locked_by = %s, locked_at = now(),
                   attempts = attempts + 1
               WHERE j.id IN (
                 SELECT id FROM drive_index.index_jobs c
                 WHERE c.status = 'pending' AND c.run_after <= now()
                   AND (%s::text[] IS NULL OR c.kind = ANY(%s::text[]))
                   AND (c.source_id IS NULL OR NOT EXISTS (
                         SELECT 1 FROM drive_index.index_jobs r
                         WHERE r.source_id = c.source_id AND r.status = 'running' AND r.kind <> 'content_process'
                           AND c.kind <> 'content_process'))
                 ORDER BY c.priority, c.id
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s)
               RETURNING j.*""",
            (WORKER_ID, kinds, kinds, limit),
        ).fetchall()
    return rows


def complete(job_id: int) -> None:
    with db.system() as conn:
        conn.execute(
            "UPDATE drive_index.index_jobs SET status = 'done', finished_at = now(), error = NULL WHERE id = %s",
            (job_id,),
        )


def fail(job: dict, error: str, retry_in: float | None = None) -> str:
    """Record a failure. Retries with exponential backoff; moves to 'dead' after max_attempts."""
    attempts = job["attempts"]
    dead = attempts >= job["max_attempts"]
    delay = retry_in if retry_in is not None else min(3600.0, 10.0 * (2 ** (attempts - 1)))
    with db.system() as conn:
        if dead:
            conn.execute(
                "UPDATE drive_index.index_jobs SET status = 'dead', error = %s, finished_at = now() WHERE id = %s",
                (error[:4000], job["id"]),
            )
            if job.get("source_id"):
                conn.execute(
                    """UPDATE drive_index.sources SET last_error = %s, last_error_at = now(),
                         status = CASE WHEN status IN ('live','initial_scan') THEN 'degraded' ELSE status END,
                         status_reason = CASE WHEN status IN ('live','initial_scan')
                                              THEN 'job failed repeatedly: ' || %s ELSE status_reason END,
                         status_since = CASE WHEN status IN ('live','initial_scan') THEN now() ELSE status_since END
                       WHERE id = %s""",
                    (error[:1000], job["kind"], job["source_id"]),
                )
        else:
            conn.execute(
                """UPDATE drive_index.index_jobs SET status = 'pending', error = %s, locked_by = NULL,
                     locked_at = NULL, run_after = now() + make_interval(secs => %s) WHERE id = %s""",
                (error[:4000], delay, job["id"]),
            )
            if job.get("source_id"):
                conn.execute(
                    "UPDATE drive_index.sources SET last_error = %s, last_error_at = now() WHERE id = %s",
                    (error[:1000], job["source_id"]),
                )
    return "dead" if dead else "retry"


def requeue_stale(max_age_seconds: int = 900) -> int:
    """Jobs left 'running' by a crashed worker go back to pending."""
    with db.system() as conn:
        cur = conn.execute(
            """UPDATE drive_index.index_jobs SET status = 'pending', locked_by = NULL, locked_at = NULL
               WHERE status = 'running' AND locked_at < now() - make_interval(secs => %s)""",
            (max_age_seconds,),
        )
        return cur.rowcount


def retry_dead(conn: Connection, source_id: UUID | str) -> int:
    cur = conn.execute(
        """UPDATE drive_index.index_jobs SET status = 'pending', attempts = 0, run_after = now(), error = NULL
           WHERE source_id = %s AND status = 'dead'""",
        (str(source_id),),
    )
    return cur.rowcount


def touch(job_id: int | None) -> None:
    """Heartbeat for long jobs so they are not mistaken for abandoned ones."""
    if not job_id:
        return
    with db.system() as conn:
        conn.execute("UPDATE drive_index.index_jobs SET locked_at = now() WHERE id = %s", (job_id,))
