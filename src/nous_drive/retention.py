"""Retention: change log and tombstones are kept for 90 days (configurable)."""

from __future__ import annotations

from . import db
from .config import get_settings


def purge() -> dict:
    days = get_settings().retention_days
    out = {}
    with db.system() as conn:
        out["change_events"] = conn.execute(
            "DELETE FROM drive_index.change_events WHERE detected_at < now() - make_interval(days => %s)", (days,)
        ).rowcount
        out["tombstones"] = conn.execute(
            """DELETE FROM drive_index.nodes WHERE status IN ('deleted','removed')
               AND deleted_at < now() - make_interval(days => %s)""", (days,)
        ).rowcount
        out["jobs"] = conn.execute(
            "DELETE FROM drive_index.index_jobs WHERE status = 'done' AND finished_at < now() - interval '7 days'"
        ).rowcount
        out["notifications"] = conn.execute(
            "DELETE FROM drive_index.notifications WHERE created_at < now() - make_interval(days => %s)", (days,)
        ).rowcount
        out["pairing_codes"] = conn.execute(
            "DELETE FROM drive_index.pairing_codes WHERE expires_at < now() - interval '1 day'"
        ).rowcount
    return out
