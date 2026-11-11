"""Ingest API service layer for the local agent (Nous Sync) — metadata only in Phase 1."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from . import db, queue
from .accounts import audit, create_source, get_source, merged_settings, set_status
from .models import NodeEvent

MAX_EVENTS_PER_JOB = 1000


class IngestError(Exception):
    pass


def _device_source(conn, device_id, source_id) -> dict:
    src = get_source(conn, source_id)
    if not src or str(src["device_id"]) != str(device_id):
        raise IngestError("source does not belong to this device")
    return src


def register_local_source(account_id, device_id, path: str, name: str | None = None,
                          ignore: list[str] | None = None) -> dict:
    with db.scoped(account_id) as conn:
        existing = conn.execute(
            "SELECT * FROM drive_index.sources WHERE device_id = %s AND root_ref = %s", (str(device_id), path)
        ).fetchone()
        if existing:
            return existing
        src = create_source(conn, account_id, type_="local", root_kind="local_folder", root_ref=path,
                            name=name or path.rstrip("/\\").split("/")[-1].split("\\")[-1] or path,
                            device_id=device_id, settings={"ignore": ignore or []},
                            actor=f"device:{device_id}")
        set_status(conn, src["id"], "initial_scan", "waiting for the agent's first scan")
        return get_source(conn, src["id"])


def submit_events(account_id, device_id, source_id, events: list[dict[str, Any]], scan_id: str | None = None) -> int:
    """Validate and queue a batch. Returns the number of jobs queued."""
    parsed = [NodeEvent.model_validate(e) for e in events]
    with db.scoped(account_id) as conn:
        src = _device_source(conn, device_id, source_id)
        if src["status"] == "paused":
            raise IngestError("source is paused")
        n = 0
        for i in range(0, len(parsed), MAX_EVENTS_PER_JOB):
            chunk = [e.model_dump(mode="json") for e in parsed[i:i + MAX_EVENTS_PER_JOB]]
            if scan_id:
                for e in chunk:
                    e["scan_id"] = e.get("scan_id") or scan_id
            queue.enqueue(conn, account_id, "apply_events", source_id,
                          {"events": chunk, "device_id": str(device_id)})
            n += 1
    return n


def begin_scan(account_id, device_id, source_id, scan_id: str, full: bool, total: int | None = None) -> None:
    with db.scoped(account_id) as conn:
        _device_source(conn, device_id, source_id)
        conn.execute(
            """UPDATE drive_index.sync_cursors SET current_scan_id = %s,
                 scan_progress = %s WHERE source_id = %s""",
            (scan_id, Jsonb({"state": "running", "scan_id": scan_id, "full": full, "phase": "listing",
                             "total": total, "processed": 0, "started_at": db_now(conn)}), str(source_id)),
        )
        conn.execute("UPDATE drive_index.sources SET pending_command = NULL WHERE id = %s",
                     (str(source_id),))


def db_now(conn) -> str:
    return conn.execute("SELECT now() AS t").fetchone()["t"].isoformat()


def complete_scan(account_id, device_id, source_id, scan_id: str, full: bool, stats: dict | None = None,
                  snapshot_ref: str | None = None) -> None:
    with db.scoped(account_id) as conn:
        _device_source(conn, device_id, source_id)
        cur = conn.execute("SELECT scan_progress FROM drive_index.sync_cursors WHERE source_id = %s",
                           (str(source_id),)).fetchone()
        started = (cur["scan_progress"] or {}).get("started_at")
        queue.enqueue(conn, account_id, "finish_scan", source_id,
                      {"scan_id": scan_id, "full": full, "started_at": started, "stats": stats or {},
                       "snapshot_ref": snapshot_ref, "device_id": str(device_id)})


def heartbeat(account_id, device_id, agent_version: str | None, status: dict | None) -> dict:
    """Record the device as online; return per-source commands and settings for the agent."""
    with db.scoped(account_id) as conn:
        conn.execute(
            """UPDATE drive_index.devices SET last_seen_at = now(), agent_version = coalesce(%s, agent_version),
                 last_status = %s WHERE id = %s""",
            (agent_version, Jsonb(status or {}), str(device_id)),
        )
        conn.execute(
            """UPDATE drive_index.sources SET status = 'live', status_reason = NULL, status_since = now()
               WHERE device_id = %s AND status = 'degraded' AND status_reason = 'device offline'""",
            (str(device_id),),
        )
        rows = conn.execute(
            "SELECT * FROM drive_index.sources WHERE device_id = %s ORDER BY created_at", (str(device_id),)
        ).fetchall()
        dev = conn.execute("SELECT name FROM drive_index.devices WHERE id = %s", (str(device_id),)).fetchone()
        pending_content = conn.execute(
            """SELECT count(*) AS n FROM drive_index.nodes n JOIN drive_index.sources s ON s.id = n.source_id
               WHERE s.device_id = %s AND n.content_status = 'queued'""",
            (str(device_id),),
        ).fetchone()["n"]
    return {
        "device": {"id": str(device_id), "name": dev["name"] if dev else None},
        "sources": [{
            "id": str(r["id"]), "path": r["root_ref"], "name": r["name"], "status": r["status"],
            "paused": r["status"] == "paused", "command": r["pending_command"],
            "ignore": merged_settings(r["settings"]).get("ignore", []),
        } for r in rows],
        "content_requests": pending_content,
    }


def content_requests(account_id, device_id, limit: int = 20) -> list[dict]:
    with db.scoped(account_id) as conn:
        rows = conn.execute(
            """SELECT n.id, n.source_id, n.provider_id, n.path, n.local_path, n.content_hash, n.ext, n.mime, n.size
               FROM drive_index.nodes n JOIN drive_index.sources s ON s.id = n.source_id
               WHERE s.device_id = %s AND n.content_status = 'queued' AND n.status = 'active'
               ORDER BY n.indexed_at LIMIT %s""",
            (str(device_id), limit),
        ).fetchall()
    return [{k: (str(v) if k in ("id", "source_id") else v) for k, v in r.items()} for r in rows]


def submit_content(account_id, device_id, node_id: str, text: str | None, extractor: str,
                   content_hash: str | None, error: str | None = None) -> dict:
    from .content.pipeline import mark_failed, store_text
    with db.scoped(account_id) as conn:
        node = conn.execute(
            """SELECT n.* FROM drive_index.nodes n JOIN drive_index.sources s ON s.id = n.source_id
               WHERE n.id = %s AND s.device_id = %s""",
            (node_id, str(device_id)),
        ).fetchone()
    if not node:
        raise IngestError("node not found for this device")
    from .reporter import file_info, report
    report(account_id, node["source_id"], "content", active=True, interval=0.2,
           current=file_info(node["name"], node["size"], node["path"], "file"))
    if error:
        mark_failed(account_id, node_id, error, status="skipped" if error.startswith("unsupported") else "failed")
        return {"status": "recorded"}
    return store_text(account_id, node_id, text or "", extractor, expected_hash=content_hash)


def apply_job(job: dict) -> dict:
    """Worker side of apply_events / finish_scan for local sources."""
    from .content.policy import should_extract
    from .indexer import Indexer
    p = job["payload"]
    with db.scoped(job["account_id"]) as conn:
        src = get_source(conn, job["source_id"])
        if not src:
            return {}
        from .reporter import file_info, report
        cur = conn.execute("SELECT current_scan_id, scan_progress FROM drive_index.sync_cursors WHERE source_id = %s",
                           (job["source_id"],)).fetchone() or {}
        sp = cur.get("scan_progress") or {}
        in_scan = any(e.get("scan_id") and e.get("scan_id") == cur.get("current_scan_id") for e in p.get("events", []))
        base, total, stage = (sp.get("processed") or 0, sp.get("total"), "index") if in_scan else \
            (0, len(p.get("events", [])), "sync")

        last = len(p.get("events", [])) - 1

        def on_event(ev, i):
            if ev.op == "upsert" or i == last:
                report(job["account_id"], job["source_id"], stage, done=base + i + 1, total=total, active=True,
                       force=i == last, current=file_info(ev.name, ev.size, None, ev.kind))
        ix = Indexer(conn, job["account_id"], src, device_id=p.get("device_id"), content_policy=should_extract,
                     on_event=on_event if job["kind"] == "apply_events" else None)
        if src["type"] == "local":
            ix.ensure_root(src["name"], "__root__", local_path=src["root_ref"])
        if job["kind"] == "apply_events":
            events = [NodeEvent.model_validate(e) for e in p["events"]]
            ix.apply(events)
            scan_ids = {e.scan_id for e in events if e.scan_id}
            if scan_ids:
                conn.execute(
                    """UPDATE drive_index.sync_cursors SET scan_progress = jsonb_set(scan_progress, '{processed}',
                         to_jsonb(coalesce((scan_progress->>'processed')::int, 0) + %s))
                       WHERE source_id = %s AND current_scan_id = ANY(%s)""",
                    (len(events), job["source_id"], list(scan_ids)))
            return {"applied": ix.stats.applied, "changes": ix.stats.changes}
        # finish_scan
        swept = 0
        if p.get("full"):
            swept = ix.finish_scan(p["scan_id"], started_at=p.get("started_at"))
        ix.rebuild_paths()
        conn.execute(
            """UPDATE drive_index.sync_cursors SET current_scan_id = NULL, last_full_scan_at = CASE WHEN %s THEN now()
                 ELSE last_full_scan_at END, local_snapshot_ref = coalesce(%s, local_snapshot_ref),
                 scan_progress = scan_progress || %s WHERE source_id = %s""",
            (bool(p.get("full")), p.get("snapshot_ref"),
             Jsonb({"state": "done", "phase": "done", "finished_at": db_now(conn), **(p.get("stats") or {})}),
             job["source_id"]),
        )
        if src["status"] in ("connecting", "initial_scan") or (src["status"] == "degraded"):
            set_status(conn, src["id"], "live")
        report(job["account_id"], job["source_id"], "index", active=False, current={}, force=True)
        from .stats import refresh
        refresh(conn, src["id"])
        audit(conn, job["account_id"], f"device:{p.get('device_id')}", "scan.completed", str(src["id"]),
              {"scan_id": p["scan_id"], "full": p.get("full"), "swept": swept})
        return {"swept": swept}
