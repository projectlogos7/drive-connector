"""Cached per-source counts (files, folders, bytes, content status) so MCP reads stay fast at 1M nodes."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from . import db


def compute(conn, source_id) -> dict:
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE kind = 'file') AS files,
                  count(*) FILTER (WHERE kind = 'folder' AND NOT is_root) AS folders,
                  coalesce(sum(size) FILTER (WHERE kind = 'file'), 0)::bigint AS bytes
           FROM drive_index.nodes WHERE source_id = %s AND status = 'active'""",
        (str(source_id),),
    ).fetchone()
    content = {r["content_status"]: r["n"] for r in conn.execute(
        """SELECT content_status, count(*) AS n FROM drive_index.nodes
           WHERE source_id = %s AND kind = 'file' AND status = 'active' GROUP BY 1""", (str(source_id),))}
    return {"files": row["files"], "folders": row["folders"], "bytes": int(row["bytes"]), "content": content}


def refresh(conn, source_id) -> dict:
    s = compute(conn, source_id)
    conn.execute("UPDATE drive_index.sync_cursors SET stats = %s, stats_at = now() WHERE source_id = %s",
                 (Jsonb(s), str(source_id)))
    return s


def refresh_due(max_age_seconds: int = 60) -> int:
    """Refresh stats of sources that changed since their stats were computed."""
    with db.system() as conn:
        rows = conn.execute(
            """SELECT s.id, s.account_id FROM drive_index.sources s
               JOIN drive_index.sync_cursors c ON c.source_id = s.id
               WHERE c.stats_at IS NULL OR (s.last_sync_at > c.stats_at
                     AND c.stats_at < now() - make_interval(secs => %s))""",
            (max_age_seconds,),
        ).fetchall()
    for r in rows:
        with db.scoped(r["account_id"]) as conn:
            refresh(conn, r["id"])
    return len(rows)
