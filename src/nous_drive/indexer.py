"""Apply normalized events to the node table and write the change log.

Guarantees:
* idempotent — the same event applied twice yields one change at most;
* paths of descendants follow folder renames/moves (one bulk statement);
* deletes are soft (tombstones), cascaded to descendants;
* parents that arrive after their children are adopted when they appear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from .models import NodeEvent
from .util import category_of, ext_of, join_path, utcnow

log = logging.getLogger(__name__)

TRACKED = ("name", "parent_provider_id", "size", "checksum", "modified_at_src", "mime", "status", "path")


@dataclass
class ApplyStats:
    applied: int = 0
    changes: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    content_queued: list[str] = field(default_factory=list)

    def bump(self, t: str) -> None:
        self.changes += 1
        self.by_type[t] = self.by_type.get(t, 0) + 1


def _snap(row: dict | None) -> dict | None:
    if not row:
        return None
    out = {}
    for k in TRACKED:
        v = row.get(k)
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


def _content_changed(old: dict, new: dict) -> bool:
    for f in ("size", "mime"):
        if old.get(f) != new.get(f):
            return True
    if old.get("checksum") and new.get("checksum") and old["checksum"] != new["checksum"]:
        return True
    om, nm = old.get("modified_at_src"), new.get("modified_at_src")
    if (om is None) != (nm is None):
        return True
    if om and nm and abs((nm - om).total_seconds()) >= 1:
        return True
    oh, nh = old.get("content_hash"), new.get("content_hash")
    return bool(oh and nh and oh != nh and not oh.startswith("sm:") and not nh.startswith("sm:"))


def _content_hash_for(ev: NodeEvent, row: dict | None) -> str | None:
    if ev.kind != "file":
        return None
    if ev.content_hash:
        return ev.content_hash
    if ev.checksum:
        return f"md5:{ev.checksum}"
    ver = ev.extra.get("version") if ev.extra else None
    if ver:
        return f"v:{ver}"
    if ev.size is not None or ev.modified_at_src is not None:
        mt = ev.modified_at_src.isoformat() if ev.modified_at_src else ""
        return f"sm:{ev.size}:{mt}"
    return row.get("content_hash") if row else None


class Indexer:
    def __init__(self, conn: Connection, account_id: UUID | str, source: dict[str, Any],
                 device_id: UUID | str | None = None, content_policy=None, on_event=None):
        self.conn = conn
        self.account_id = str(account_id)
        self.source = source
        self.source_id = str(source["id"])
        self.device_id = str(device_id) if device_id else (str(source["device_id"]) if source.get("device_id") else None)
        self.content_policy = content_policy
        self.on_event = on_event  # called as on_event(event, index) for live progress
        self.stats = ApplyStats()

    # ------------------------------------------------------------------ helpers
    def _get(self, provider_id: str, lock: bool = True) -> dict | None:
        q = "SELECT * FROM drive_index.nodes WHERE source_id = %s AND provider_id = %s"
        if lock:
            q += " FOR UPDATE"
        return self.conn.execute(q, (self.source_id, provider_id)).fetchone()

    def _event(self, type_: str, node: dict | None, before: dict | None, after: dict | None,
               occurred_at=None, idem: str | None = None, extra_after: dict | None = None) -> None:
        if extra_after:
            after = dict(after or {}, **extra_after)
        self.conn.execute(
            """INSERT INTO drive_index.change_events
                 (account_id, source_id, node_id, device_id, type, kind, name, path, before, after,
                  occurred_at, idempotency_key)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s, now()),%s)
               ON CONFLICT DO NOTHING""",
            (
                self.account_id, self.source_id, node["id"] if node else None, self.device_id, type_,
                node.get("kind") if node else None, node.get("name") if node else None,
                node.get("path") if node else None,
                Jsonb(before) if before is not None else None,
                Jsonb(after) if after is not None else None,
                occurred_at, idem,
            ),
        )
        self.stats.bump(type_)

    def ensure_root(self, name: str, provider_id: str, web_link: str | None = None,
                    local_path: str | None = None) -> dict:
        row = self._get(provider_id)
        if row:
            if not row["is_root"] or row["path"] != "/" or row["status"] != "active":
                row = self.conn.execute(
                    """UPDATE drive_index.nodes SET is_root = true, path = '/', depth = 0, status = 'active',
                         parent_id = NULL, deleted_at = NULL WHERE id = %s RETURNING *""",
                    (row["id"],),
                ).fetchone()
            return row
        return self.conn.execute(
            """INSERT INTO drive_index.nodes (account_id, source_id, provider_id, is_root, kind, category, name,
                 path, depth, web_link, local_path, device_id)
               VALUES (%s,%s,%s,true,'folder','folder',%s,'/',0,%s,%s,%s) RETURNING *""",
            (self.account_id, self.source_id, provider_id, name, web_link, local_path, self.device_id),
        ).fetchone()

    def refresh_subtree(self, node_id) -> int:
        """Re-derive path/depth of every descendant of ``node_id`` from its parent chain."""
        cur = self.conn.execute(
            """WITH RECURSIVE t(id, path, depth) AS (
                   SELECT id, path, depth FROM drive_index.nodes WHERE id = %(id)s
                 UNION ALL
                   SELECT c.id,
                          CASE WHEN t.path = '/' THEN '/' || c.name ELSE t.path || '/' || c.name END,
                          t.depth + 1
                   FROM drive_index.nodes c JOIN t ON c.parent_id = t.id
                   WHERE t.depth < 256 AND NOT c.is_root
               )
               UPDATE drive_index.nodes n SET path = t.path, depth = t.depth,
                      local_path = CASE WHEN %(root)s::text IS NULL OR n.is_root THEN n.local_path
                                        ELSE %(root)s || replace(t.path, '/', %(sep)s) END
               FROM t WHERE n.id = t.id AND (n.path <> t.path OR n.depth <> t.depth
                     OR (%(root)s::text IS NOT NULL AND NOT n.is_root
                         AND n.local_path IS DISTINCT FROM %(root)s || replace(t.path, '/', %(sep)s)))""",
            {"id": node_id, **self._local_root()},
        )
        return cur.rowcount

    def _local_root(self) -> dict:
        """For local sources, descendants' absolute paths follow folder renames/moves too."""
        if self.source.get("type") != "local":
            return {"root": None, "sep": "/"}
        root = self.source["root_ref"]
        sep = "\\" if ("\\" in root and "/" not in root) else "/"
        return {"root": root.rstrip("/\\"), "sep": sep}

    def _adopt_children(self, folder: dict) -> None:
        self.conn.execute(
            """UPDATE drive_index.nodes SET parent_id = %s
               WHERE source_id = %s AND parent_provider_id = %s AND parent_id IS DISTINCT FROM %s
                 AND NOT is_root""",
            (folder["id"], self.source_id, folder["provider_id"], folder["id"]),
        )

    def _descendant_ids(self, node_id) -> list:
        rows = self.conn.execute(
            """WITH RECURSIVE d(id, lvl) AS (
                   SELECT id, 0 FROM drive_index.nodes WHERE parent_id = %s
                 UNION ALL
                   SELECT c.id, d.lvl + 1 FROM drive_index.nodes c JOIN d ON c.parent_id = d.id WHERE d.lvl < 256
               ) SELECT id FROM d""",
            (node_id,),
        ).fetchall()
        return [r["id"] for r in rows]

    def _set_status_subtree(self, node_id, status: str, reason: str | None, only_from: tuple[str, ...]) -> int:
        ids = self._descendant_ids(node_id)
        if not ids:
            return 0
        deleted_at = "now()" if status in ("deleted", "removed") else "NULL"
        cur = self.conn.execute(
            f"""UPDATE drive_index.nodes SET status = %s, status_reason = %s, deleted_at = {deleted_at},
                     indexed_at = now(), version = version + 1
                WHERE id = ANY(%s) AND status = ANY(%s)""",
            (status, reason, ids, list(only_from)),
        )
        return cur.rowcount

    # ------------------------------------------------------------------ public
    def apply(self, events: list[NodeEvent]) -> ApplyStats:
        for i, ev in enumerate(events):
            if self.on_event:
                self.on_event(ev, i)
            if ev.op == "upsert":
                self._upsert(ev)
            elif ev.op in ("delete", "remove"):
                self._delete(ev)
            elif ev.op == "unavailable":
                self._source_unavailable(ev)
            elif ev.op == "available":
                self._source_available(ev)
            self.stats.applied += 1
        # Touch the source's "last synced" without waiting on a row another job holds (avoids deadlocks
        # between parallel jobs of one source); a skipped touch is caught up by the next batch.
        self.conn.execute(
            """UPDATE drive_index.sources SET last_sync_at = now(), updated_at = now()
               WHERE id = (SELECT id FROM drive_index.sources WHERE id = %s FOR NO KEY UPDATE SKIP LOCKED)""",
            (self.source_id,),
        )
        return self.stats

    def _upsert(self, ev: NodeEvent) -> None:
        assert ev.provider_id and ev.name is not None, "upsert needs provider_id and name"
        existing = self._get(ev.provider_id)
        if existing and existing["is_root"]:
            # Roots keep path '/', but pick up metadata.
            self.conn.execute(
                "UPDATE drive_index.nodes SET last_seen_scan = coalesce(%s, last_seen_scan), web_link = coalesce(%s, web_link) WHERE id = %s",
                (ev.scan_id, ev.web_link, existing["id"]),
            )
            return

        parent = self._get(ev.parent_provider_id, lock=False) if ev.parent_provider_id else None
        if parent:
            path, depth, parent_id = join_path(parent["path"], ev.name), parent["depth"] + 1, parent["id"]
        else:
            # Parent not indexed yet: keep it as an orphan; it is adopted when the parent arrives.
            path, depth, parent_id = "/" + ev.name, 1, None

        new_status = "trashed" if ev.trashed else "active"
        content_hash = _content_hash_for(ev, existing)
        ext = ext_of(ev.name, ev.kind)
        values = dict(
            parent_id=parent_id, parent_provider_id=ev.parent_provider_id,
            alt_parent_provider_ids=ev.alt_parent_provider_ids, kind=ev.kind, name=ev.name, path=path,
            depth=depth, ext=ext, category=category_of(ev.kind, ev.mime, ext), mime=ev.mime, size=ev.size, checksum=ev.checksum,
            created_at_src=ev.created_at_src, modified_at_src=ev.modified_at_src, owner=ev.owner,
            last_modified_by=ev.last_modified_by, web_link=ev.web_link, local_path=ev.local_path,
            fs_id=ev.fs_id, shortcut_target=ev.shortcut_target, status=new_status,
            status_reason=None, extra=Jsonb(ev.extra or {}), device_id=self.device_id,
            content_hash=content_hash,
        )

        types: list[str] = []
        if existing is None or existing["status"] in ("deleted", "removed"):
            types.append("added" if new_status == "active" else "trashed")
        else:
            prev = existing["status"]
            if prev == "trashed" and new_status == "active":
                types.append("restored")
            elif prev in ("active", "unavailable") and new_status == "trashed":
                types.append("trashed")
            if existing["name"] != ev.name:
                # Case-only renames are still renames.
                types.append("renamed")
            if existing["parent_provider_id"] != ev.parent_provider_id:
                types.append("moved")
            if ev.kind == "file" and _content_changed(existing, values):
                types.append("modified")

        if existing is None:
            node = self.conn.execute(
                """INSERT INTO drive_index.nodes (account_id, source_id, provider_id, parent_id, parent_provider_id,
                     alt_parent_provider_ids, kind, name, path, depth, ext, category, mime, size, checksum, created_at_src,
                     modified_at_src, owner, last_modified_by, web_link, local_path, fs_id, shortcut_target,
                     status, status_reason, extra, device_id, content_hash, last_seen_scan)
                   VALUES (%(account_id)s, %(source_id)s, %(provider_id)s, %(parent_id)s, %(parent_provider_id)s,
                     %(alt_parent_provider_ids)s, %(kind)s, %(name)s, %(path)s, %(depth)s, %(ext)s, %(category)s, %(mime)s,
                     %(size)s, %(checksum)s, %(created_at_src)s, %(modified_at_src)s, %(owner)s,
                     %(last_modified_by)s, %(web_link)s, %(local_path)s, %(fs_id)s, %(shortcut_target)s,
                     %(status)s, %(status_reason)s, %(extra)s, %(device_id)s, %(content_hash)s, %(scan_id)s)
                   RETURNING *""",
                dict(values, account_id=self.account_id, source_id=self.source_id,
                     provider_id=ev.provider_id, scan_id=ev.scan_id),
            ).fetchone()
        else:
            sets = ", ".join(f"{k} = %({k})s" for k in values)
            bump = ", version = version + 1, indexed_at = now()" if types else ""
            node = self.conn.execute(
                f"""UPDATE drive_index.nodes SET {sets}, deleted_at = NULL,
                       last_seen_scan = coalesce(%(scan_id)s, last_seen_scan){bump}
                    WHERE id = %(id)s RETURNING *""",
                dict(values, id=existing["id"], scan_id=ev.scan_id),
            ).fetchone()

        before = _snap(existing) if existing and existing["status"] not in ("deleted", "removed") else None
        after = _snap(node)
        for t in types:
            self._event(t, node, before, after, ev.occurred_at or ev.modified_at_src,
                        f"{ev.idempotency_key}:{t}" if ev.idempotency_key else None)

        # Folder appeared, or its position changed: adopt waiting children, cascade paths.
        if node["kind"] == "folder" and (existing is None or existing["path"] != node["path"]
                                         or existing["parent_id"] != node["parent_id"]):
            self._adopt_children(node)
            self.refresh_subtree(node["id"])
        if node["kind"] == "folder" and "trashed" in types:
            self._set_status_subtree(node["id"], "trashed", "parent trashed", ("active",))
        if node["kind"] == "folder" and "restored" in types:
            self._set_status_subtree(node["id"], "active", None, ("trashed",))

        # Phase 2 hook: queue content extraction when the content changed.
        if self.content_policy and node["kind"] == "file" and node["status"] == "active":
            changed = existing is None or existing.get("content_hash") != node["content_hash"] \
                or existing.get("content_status") in ("not_requested",)
            if changed and self.content_policy(self.source, node):
                self.conn.execute(
                    "UPDATE drive_index.nodes SET content_status = 'queued', content_error = NULL WHERE id = %s",
                    (node["id"],),
                )
                self.stats.content_queued.append(str(node["id"]))

    def _delete(self, ev: NodeEvent) -> None:
        existing = self._get(ev.provider_id) if ev.provider_id else None
        if not existing or existing["is_root"] or existing["status"] in ("deleted", "removed"):
            return
        status = "deleted" if ev.op == "delete" else "removed"
        node = self.conn.execute(
            """UPDATE drive_index.nodes SET status = %s, status_reason = %s, deleted_at = now(),
                 indexed_at = now(), version = version + 1 WHERE id = %s RETURNING *""",
            (status, ev.reason, existing["id"]),
        ).fetchone()
        n_desc = 0
        if node["kind"] == "folder":
            n_desc = self._set_status_subtree(node["id"], status, "parent " + status,
                                              ("active", "trashed", "unavailable"))
        self._event(status, node, _snap(existing), _snap(node), ev.occurred_at,
                    f"{ev.idempotency_key}:{status}" if ev.idempotency_key else None,
                    extra_after={"descendants": n_desc, "reason": ev.reason} if (n_desc or ev.reason) else None)

    def _source_unavailable(self, ev: NodeEvent) -> None:
        cur = self.conn.execute(
            """UPDATE drive_index.nodes SET status = 'unavailable', status_reason = %s
               WHERE source_id = %s AND status = 'active' AND NOT is_root""",
            (ev.reason or "volume not mounted", self.source_id),
        )
        if cur.rowcount:
            root = self.conn.execute(
                "SELECT * FROM drive_index.nodes WHERE source_id = %s AND is_root", (self.source_id,)
            ).fetchone()
            self._event("unavailable", root, None, {"nodes": cur.rowcount, "reason": ev.reason}, ev.occurred_at)

    def _source_available(self, ev: NodeEvent) -> None:
        cur = self.conn.execute(
            """UPDATE drive_index.nodes SET status = 'active', status_reason = NULL
               WHERE source_id = %s AND status = 'unavailable'""",
            (self.source_id,),
        )
        if cur.rowcount:
            root = self.conn.execute(
                "SELECT * FROM drive_index.nodes WHERE source_id = %s AND is_root", (self.source_id,)
            ).fetchone()
            self._event("available", root, None, {"nodes": cur.rowcount}, ev.occurred_at)

    # ------------------------------------------------------------------ scans
    def finish_scan(self, scan_id: str, started_at=None, reason: str = "not found in full scan") -> int:
        """Mark-and-sweep: anything not seen during scan ``scan_id`` is gone.

        Nodes indexed after the scan started (live events racing the scan) are never swept.
        """
        missing = self.conn.execute(
            """SELECT * FROM drive_index.nodes
               WHERE source_id = %s AND NOT is_root AND status IN ('active','trashed','unavailable')
                 AND last_seen_scan IS DISTINCT FROM %s
                 AND (%s::timestamptz IS NULL OR indexed_at < %s::timestamptz)
               ORDER BY depth""",
            (self.source_id, scan_id, started_at, started_at),
        ).fetchall()
        n = 0
        for row in missing:
            fresh = self._get(row["provider_id"])
            if fresh["status"] in ("deleted", "removed"):
                continue  # already swept with its parent
            node = self.conn.execute(
                """UPDATE drive_index.nodes SET status = 'deleted', status_reason = %s, deleted_at = now(),
                     version = version + 1, indexed_at = now() WHERE id = %s RETURNING *""",
                (reason, row["id"]),
            ).fetchone()
            self._event("deleted", node, _snap(row), _snap(node), extra_after={"reason": reason})
            n += 1
        return n

    def rebuild_paths(self) -> int:
        """Re-link parents and recompute every path in the source (after bulk crawls)."""
        self.conn.execute(
            """UPDATE drive_index.nodes c SET parent_id = p.id
               FROM drive_index.nodes p
               WHERE c.source_id = %s AND p.source_id = c.source_id AND NOT c.is_root
                 AND p.provider_id = c.parent_provider_id AND c.parent_id IS DISTINCT FROM p.id""",
            (self.source_id,),
        )
        root = self.conn.execute(
            "SELECT id FROM drive_index.nodes WHERE source_id = %s AND is_root", (self.source_id,)
        ).fetchone()
        return self.refresh_subtree(root["id"]) if root else 0
