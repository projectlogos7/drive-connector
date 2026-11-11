"""Index MCP tools as plain functions over a scoped (RLS) connection.

Every function takes an ``Ctx`` carrying the authenticated account and the MCP session id; the server
module wraps them as MCP tools. Results are paged and size-limited (FR-30).
"""

from __future__ import annotations

import base64
import json
import posixpath
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db, queue
from ..accounts import audit, get_source, merged_settings, set_status
from ..config import get_settings
from ..progress import view as progress_view
from ..util import escape_like, human_size, normalize_user_path, utcnow


class ToolError(Exception):
    pass


@dataclass
class Ctx:
    account_id: str
    session_id: str = "default"
    actor: str = "mcp"


# ------------------------------------------------------------------ helpers
def _limit(limit: int | None, default: int = 50) -> int:
    cap = get_settings().mcp_page_limit
    return max(1, min(int(limit or default), cap))


def _enc_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _dec_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"])
    except Exception as e:
        raise ToolError("invalid cursor") from e


COUNT_CAP = 10_000
SEARCH_CANDIDATES = 20_000  # per search: name-prefix matches + path matches, each capped


def _capped_count(conn, sql_from_where: str, params: list) -> tuple[int, bool]:
    """Exact count up to COUNT_CAP; beyond that report 'COUNT_CAP+' to keep huge listings fast."""
    n = conn.execute(f"SELECT count(*) AS n FROM (SELECT 1 {sql_from_where} LIMIT {COUNT_CAP + 1}) t",
                     params).fetchone()["n"]
    return min(n, COUNT_CAP), n > COUNT_CAP


def _ts(v) -> str | None:
    return v.isoformat() if v else None


def _parse_time(v: str | None) -> datetime | None:
    if not v:
        return None
    v = v.strip()
    now = utcnow()
    rel = {"today": now.replace(hour=0, minute=0, second=0, microsecond=0),
           "yesterday": (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)}
    if v.lower() in rel:
        return rel[v.lower()]
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if v[:-1].isdigit() and v[-1].lower() in units:
        return now - timedelta(**{units[v[-1].lower()]: int(v[:-1])})
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"cannot parse time {v!r}; use ISO 8601, 'today', 'yesterday' or e.g. '24h', '7d'") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _devices(conn) -> dict[str, str]:
    return {str(r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM drive_index.devices")}


def _sources(conn) -> dict[str, dict]:
    return {str(r["id"]): r for r in conn.execute("SELECT * FROM drive_index.sources ORDER BY created_at")}


def resolve_source(conn, source: str | None) -> dict | None:
    if not source:
        return None
    srcs = _sources(conn)
    if source in srcs:
        return srcs[source]
    matches = [s for s in srcs.values() if s["name"].lower() == source.lower()]
    if not matches:
        matches = [s for s in srcs.values() if source.lower() in s["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolError(f"no source matches {source!r}; call list_sources to see sources")
    raise ToolError(f"{source!r} matches several sources: " + ", ".join(f"{s['name']} ({s['id']})" for s in matches))


def _node_brief(n: dict, srcs: dict, devs: dict) -> dict[str, Any]:
    src = srcs.get(str(n["source_id"]))
    out = {
        "id": str(n["id"]), "name": n["name"], "kind": n["kind"], "path": n["path"],
        "source": src["name"] if src else None, "source_id": str(n["source_id"]),
    }
    if n["kind"] != "folder":
        out.update(size=n["size"], size_human=human_size(n["size"]) if n["size"] is not None else None,
                   type=n.get("category"), ext=n["ext"], mime=n["mime"])
    out["modified_at"] = _ts(n["modified_at_src"])
    if n["status"] != "active":
        out["status"] = n["status"]
    if n.get("device_id") and str(n["device_id"]) in devs:
        out["device"] = devs[str(n["device_id"])]
    if n["web_link"]:
        out["web_link"] = n["web_link"]
    if n.get("local_path"):
        out["local_path"] = n["local_path"]
    return out


def resolve_node(conn, path: str | None = None, node_id: str | None = None, source: str | None = None) -> dict:
    if node_id:
        n = conn.execute("SELECT * FROM drive_index.nodes WHERE id::text = %s", (node_id,)).fetchone()
        if not n:
            raise ToolError(f"no file or folder with id {node_id}")
        return n
    src = resolve_source(conn, source)
    p = normalize_user_path(path or "/")
    if p == "/" and not src:
        raise ToolError("path '/' is ambiguous without a source; pass source or a path inside a source")
    candidates = _match_path(conn, p, src)
    if not candidates:
        # Allow the source name as the first path segment, e.g. "Footage/Day 3".
        parts = p.strip("/").split("/", 1)
        if not src and parts:
            for s in _sources(conn).values():
                if s["name"].lower() == parts[0].lower():
                    candidates = _match_path(conn, "/" + (parts[1] if len(parts) > 1 else ""), s)
                    if candidates:
                        break
    if not candidates:
        raise ToolError(f"nothing found at {p!r}" + (f" in {src['name']}" if src else "") +
                        "; try search_files")
    live = [c for c in candidates if c["status"] == "active"] or candidates
    if len(live) > 1:
        srcs = _sources(conn)
        raise ToolError(f"{p!r} exists in several sources: " +
                        ", ".join(srcs[str(c["source_id"])]["name"] for c in live) + "; pass source")
    return live[0]


def _match_path(conn, p: str, src: dict | None) -> list[dict]:
    where, params = "path = %s", [p]
    if src:
        where += " AND source_id = %s"
        params.append(str(src["id"]))
    rows = conn.execute(f"SELECT * FROM drive_index.nodes WHERE {where} ORDER BY status = 'active' DESC",
                        params).fetchall()
    if not rows:
        where = where.replace("path = %s", "lower(path) = lower(%s)")
        rows = conn.execute(f"SELECT * FROM drive_index.nodes WHERE {where}", params).fetchall()
    return rows


# ------------------------------------------------------------------ read tools
def list_sources(ctx: Ctx) -> dict:
    from .. import stats as S
    with db.scoped(ctx.account_id) as conn:
        devs = _devices(conn)
        cached = {str(r["source_id"]): r for r in conn.execute(
            "SELECT source_id, stats, stats_at FROM drive_index.sync_cursors")}
        out = []
        for s in _sources(conn).values():
            c = cached.get(str(s["id"]), {})
            st = c.get("stats") or S.refresh(conn, s["id"])
            out.append({
                "id": str(s["id"]), "name": s["name"], "type": s["type"], "root_kind": s["root_kind"],
                "root": s["root_ref"] if s["type"] == "local" else s["name"],
                "device": devs.get(str(s["device_id"])) if s["device_id"] else None,
                "status": s["status"], "status_reason": s["status_reason"],
                "last_sync_at": _ts(s["last_sync_at"]),
                "files": st["files"], "folders": st["folders"], "total_size": human_size(int(st["bytes"])),
                "counts_as_of": _ts(c.get("stats_at")) or _ts(utcnow()),
            })
    return {"sources": out, "count": len(out)}


def get_sync_status(ctx: Ctx, source: str | None = None) -> dict:
    s = get_settings()
    now = utcnow()
    with db.scoped(ctx.account_id) as conn:
        only = resolve_source(conn, source)
        srcs = [only] if only else list(_sources(conn).values())
        devices = {str(r["id"]): r for r in conn.execute("SELECT * FROM drive_index.devices")}
        out = []
        for src in srcs:
            sid = str(src["id"])
            cur = conn.execute("SELECT * FROM drive_index.sync_cursors WHERE source_id = %s", (sid,)).fetchone() or {}
            jobs = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM drive_index.index_jobs WHERE source_id = %s AND status <> 'done' GROUP BY status",
                (sid,))}
            dead = conn.execute(
                """SELECT id, kind, attempts, left(error, 300) AS error, finished_at FROM drive_index.index_jobs
                   WHERE source_id = %s AND status = 'dead' ORDER BY id DESC LIMIT 5""", (sid,)).fetchall()
            ev = conn.execute(
                """SELECT count(*) FILTER (WHERE detected_at > now() - interval '10 minutes') AS last_10m,
                          count(*) FILTER (WHERE detected_at > now() - interval '1 hour') AS last_1h,
                          max(detected_at) AS last_event_at
                   FROM drive_index.change_events WHERE source_id = %s""", (sid,)).fetchone()
            last_sync = src["last_sync_at"] or cur.get("last_poll_at")
            item = {
                "source_id": sid, "name": src["name"], "type": src["type"], "status": src["status"],
                "live": src["status"] == "live", "status_reason": src["status_reason"],
                "status_since": _ts(src["status_since"]),
                "last_successful_sync": _ts(last_sync),
                "lag_seconds": int((now - last_sync).total_seconds()) if last_sync else None,
                "events_per_minute_10m": round((ev["last_10m"] or 0) / 10, 2),
                "events_last_hour": ev["last_1h"], "last_event_at": _ts(ev["last_event_at"]),
                "last_error": src["last_error"], "last_error_at": _ts(src["last_error_at"]),
                "queue": {"pending": jobs.get("pending", 0), "running": jobs.get("running", 0),
                          "dead_letter": jobs.get("dead", 0)},
                "dead_letters": [{**d, "finished_at": _ts(d["finished_at"])} for d in dead],
                "last_full_scan_at": _ts(cur.get("last_full_scan_at")),
                "scan_progress": cur.get("scan_progress") or {},
                "progress": progress_view(src, cur, pending_crawl=jobs.get("pending", 0) + jobs.get("running", 0) > 0),
            }
            if src["type"] == "gdrive":
                exp = cur.get("channel_expires_at")
                item["push_channel"] = {"active": bool(cur.get("channel_id") and exp and exp > now),
                                        "expires_at": _ts(exp)}
                item["last_poll_at"] = _ts(cur.get("last_poll_at"))
            else:
                d = devices.get(str(src["device_id"])) if src["device_id"] else None
                seen = d["last_seen_at"] if d else None
                item["device"] = {
                    "name": d["name"] if d else None,
                    "online": bool(seen and seen > now - timedelta(seconds=s.device_offline_seconds)),
                    "last_seen_at": _ts(seen), "agent_version": d["agent_version"] if d else None,
                    "os": d["os"] if d else None, "reported": (d["last_status"] if d else {}) or {},
                }
            cstats = ((cur.get("stats") or {}).get("content") or {})
            if any(k != "not_requested" for k in cstats):
                item["content"] = cstats
            out.append(item)
    healthy = all(i["status"] == "live" and i["queue"]["dead_letter"] == 0 for i in out)
    return {"healthy": healthy, "sources": out}


def get_tree(ctx: Ctx, path: str | None = None, node_id: str | None = None, source: str | None = None,
             depth: int = 2, include_files: bool = False, max_entries: int = 300) -> dict:
    depth = max(1, min(int(depth or 2), 10))
    max_entries = _limit(max_entries, 300) if max_entries <= get_settings().mcp_page_limit else min(max_entries, 1000)
    with db.scoped(ctx.account_id) as conn:
        root = resolve_node(conn, path or "/", node_id, source)
        if root["kind"] != "folder":
            raise ToolError(f"{root['path']} is a file; use get_file")
        kinds = ("folder", "file", "shortcut") if include_files else ("folder",)
        rows = conn.execute(
            """WITH RECURSIVE t AS (
                   SELECT id, parent_id, name, kind, size, 0 AS lvl FROM drive_index.nodes WHERE id = %s
                 UNION ALL
                   SELECT c.id, c.parent_id, c.name, c.kind, c.size, t.lvl + 1 FROM drive_index.nodes c
                   JOIN t ON c.parent_id = t.id
                   WHERE t.lvl < %s AND c.status = 'active' AND c.kind = ANY(%s)
               ) SELECT * FROM t ORDER BY lvl, kind DESC, lower(name) LIMIT %s""",
            (root["id"], depth, list(kinds), max_entries + 1),
        ).fetchall()
        truncated = len(rows) > max_entries
        rows = rows[:max_entries]
        counts = {str(r["parent_id"]): r for r in conn.execute(
            """SELECT parent_id, count(*) FILTER (WHERE kind = 'file') AS files,
                      count(*) FILTER (WHERE kind = 'folder') AS folders
               FROM drive_index.nodes WHERE parent_id = ANY(%s) AND status = 'active' GROUP BY parent_id""",
            ([r["id"] for r in rows if r["kind"] == "folder"],))}
        src = get_source(conn, root["source_id"])
    nodes: dict[str, dict] = {}
    for r in rows:
        c = counts.get(str(r["id"]), {})
        nd = {"name": r["name"], "kind": r["kind"], "id": str(r["id"])}
        if r["kind"] == "folder":
            nd.update(files=c.get("files", 0), folders=c.get("folders", 0), children=[])
        else:
            nd["size"] = human_size(r["size"])
        nodes[str(r["id"])] = nd
        if r["lvl"] > 0 and str(r["parent_id"]) in nodes:
            nodes[str(r["parent_id"])].setdefault("children", []).append(nd)
    tree = nodes[str(root["id"])]
    lines: list[str] = []

    def render(n: dict, indent: int) -> None:
        if n["kind"] == "folder":
            lines.append("  " * indent + f"{n['name']}/  ({n['files']} files, {n['folders']} folders)")
        else:
            lines.append("  " * indent + f"{n['name']}  [{n.get('size')}]")
        for ch in n.get("children", []):
            render(ch, indent + 1)

    render(tree, 0)
    return {"source": src["name"], "path": root["path"], "depth": depth, "truncated": truncated,
            "text": "\n".join(lines), "tree": tree}


SORTS = {"name": "lower(name)", "modified": "modified_at_src DESC NULLS LAST", "size": "size DESC NULLS LAST",
         "added": "first_indexed_at DESC", "path": "path"}


def list_files(ctx: Ctx, path: str | None = None, node_id: str | None = None, source: str | None = None,
               recursive: bool = False, type: str | None = None, ext: str | None = None,
               min_size: int | None = None, max_size: int | None = None, modified_after: str | None = None,
               modified_before: str | None = None, added_after: str | None = None, status: str = "active",
               include_folders: bool = False, sort: str = "name", limit: int = 50,
               cursor: str | None = None) -> dict:
    lim, off = _limit(limit), _dec_cursor(cursor)
    with db.scoped(ctx.account_id) as conn:
        where, params = ["TRUE"], []
        scope = None
        if path or node_id:
            scope = resolve_node(conn, path, node_id, source)
            if scope["kind"] != "folder":
                raise ToolError(f"{scope['path']} is not a folder")
            if recursive:
                prefix = "/" if scope["path"] == "/" else scope["path"] + "/"
                where.append("source_id = %s AND path LIKE %s")
                params += [scope["source_id"], escape_like(prefix) + "%"]
            else:
                where.append("parent_id = %s")
                params.append(scope["id"])
        elif source:
            where.append("source_id = %s")
            params.append(resolve_source(conn, source)["id"])
        where.append("NOT is_root")
        where.append("kind <> 'folder'" if not include_folders else "TRUE")
        if status and status != "any":
            where.append("status = %s")
            params.append(status)
        if type:
            where.append("category = %s")
            params.append(type.lower())
        if ext:
            exts = [e.strip().lstrip(".").lower() for e in ext.split(",") if e.strip()]
            where.append("ext = ANY(%s)")
            params.append(exts)
        if min_size is not None:
            where.append("size >= %s")
            params.append(int(min_size))
        if max_size is not None:
            where.append("size <= %s")
            params.append(int(max_size))
        if modified_after:
            where.append("modified_at_src >= %s")
            params.append(_parse_time(modified_after))
        if modified_before:
            where.append("modified_at_src < %s")
            params.append(_parse_time(modified_before))
        if added_after:
            where.append("first_indexed_at >= %s")
            params.append(_parse_time(added_after))
        order = SORTS.get(sort, SORTS["name"])
        sql_where = " AND ".join(where)
        n_total, more = _capped_count(conn, f"FROM drive_index.nodes WHERE {sql_where}", params)
        total_size = None
        if not more:
            total_size = human_size(int(conn.execute(
                f"SELECT coalesce(sum(size),0) AS b FROM drive_index.nodes WHERE {sql_where}", params).fetchone()["b"]))
        rows = conn.execute(f"SELECT * FROM drive_index.nodes WHERE {sql_where} ORDER BY {order}, id "
                            f"LIMIT %s OFFSET %s", params + [lim, off]).fetchall()
        srcs, devs = _sources(conn), _devices(conn)
    nxt = off + lim if (off + lim < n_total or more) and len(rows) == lim else None
    return {"folder": scope["path"] if scope else None, "total": n_total, "total_is_lower_bound": more,
            "total_size": total_size, "returned": len(rows),
            "files": [_node_brief(r, srcs, devs) for r in rows],
            "next_cursor": _enc_cursor(nxt) if nxt is not None else None}


def search_files(ctx: Ctx, query: str, source: str | None = None, type: str | None = None,
                 ext: str | None = None, include_folders: bool = True, status: str = "active",
                 mode: str = "name", limit: int = 30, cursor: str | None = None) -> dict:
    """Search by name and path; with Phase 2 content, mode='content' or 'hybrid' searches inside files."""
    if not query or not query.strip():
        raise ToolError("query is required")
    lim, off = _limit(limit, 30), _dec_cursor(cursor)
    with db.scoped(ctx.account_id) as conn:
        src = resolve_source(conn, source)
        srcs, devs = _sources(conn), _devices(conn)
        if mode in ("content", "semantic", "keyword", "hybrid") and _content_available(conn):
            from ..content.search import search_content
            hits = search_content(conn, query, limit=lim, source_ids=[str(src["id"])] if src else None,
                                  mode={"content": "hybrid"}.get(mode, mode))
            nodes = {str(r["id"]): r for r in conn.execute(
                "SELECT * FROM drive_index.nodes WHERE id = ANY(%s)", ([h["node_id"] for h in hits],))}
            results = []
            for h in hits:
                n = nodes.get(h["node_id"])
                if n:
                    results.append({**_node_brief(n, srcs, devs), "snippet": h["snippet"], "score": round(h["score"], 4)})
            return {"query": query, "mode": mode, "results": results, "returned": len(results), "next_cursor": None}
        q = query.strip().lower()
        terms = [t for t in q.replace("/", " ").split() if t]
        where, params = ["NOT is_root"], []
        # Candidate ids come from the trigram-indexed matcher (see migration 004); RLS still applies here.
        where.append("id IN (SELECT node_id FROM drive_index.match_paths(%s, %s, %s))")
        params += [["%" + escape_like(t) + "%" for t in terms], escape_like(q) + "%", SEARCH_CANDIDATES]
        if src:
            where.append("source_id = %s")
            params.append(src["id"])
        if not include_folders:
            where.append("kind <> 'folder'")
        if status and status != "any":
            where.append("status = %s")
            params.append(status)
        if type:
            where.append("category = %s")
            params.append(type.lower())
        if ext:
            where.append("ext = ANY(%s)")
            params.append([e.strip().lstrip(".").lower() for e in ext.split(",")])
        w = " AND ".join(where)
        rank = ("CASE WHEN lower(name) = %s THEN 0 WHEN lower(name) LIKE %s THEN 1 "
                "WHEN lower(name) LIKE %s THEN 2 ELSE 3 END")
        rparams = [q, escape_like(q) + "%", "%" + escape_like(q) + "%"]
        total, more = _capped_count(conn, f"FROM drive_index.nodes WHERE {w}", params)
        rows = conn.execute(
            f"SELECT * FROM drive_index.nodes WHERE {w} ORDER BY {rank}, modified_at_src DESC NULLS LAST, id "
            f"LIMIT %s OFFSET %s", params + rparams + [lim, off]).fetchall()
    nxt = off + lim if (off + lim < total or more) and len(rows) == lim else None
    return {"query": query, "mode": "name", "total": total, "total_is_lower_bound": more, "returned": len(rows),
            "results": [_node_brief(r, srcs, devs) for r in rows],
            "next_cursor": _enc_cursor(nxt) if nxt is not None else None}


def _content_available(conn) -> bool:
    return bool(conn.execute(
        "SELECT to_regclass('drive_index.chunks') IS NOT NULL AS ok").fetchone()["ok"])


def _history(conn, node_id, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """SELECT e.id, e.type, e.path, e.before, e.after, e.occurred_at, e.detected_at, d.name AS device
           FROM drive_index.change_events e LEFT JOIN drive_index.devices d ON d.id = e.device_id
           WHERE e.node_id = %s ORDER BY e.id DESC LIMIT %s""",
        (node_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        item = {"type": r["type"], "at": _ts(r["occurred_at"]), "detected_at": _ts(r["detected_at"]), "path": r["path"]}
        b, a = r["before"] or {}, r["after"] or {}
        if r["type"] == "renamed":
            item["from"], item["to"] = b.get("name"), a.get("name")
        elif r["type"] == "moved":
            item["from"], item["to"] = b.get("path"), a.get("path")
        elif r["type"] == "modified":
            item["size_from"], item["size_to"] = b.get("size"), a.get("size")
        if r["device"]:
            item["device"] = r["device"]
        out.append(item)
    return out


def get_file(ctx: Ctx, node_id: str | None = None, path: str | None = None, source: str | None = None) -> dict:
    with db.scoped(ctx.account_id) as conn:
        n = resolve_node(conn, path, node_id, source)
        srcs, devs = _sources(conn), _devices(conn)
        src = srcs[str(n["source_id"])]
        parent = conn.execute("SELECT id, name, path FROM drive_index.nodes WHERE id = %s",
                              (n["parent_id"],)).fetchone() if n["parent_id"] else None
        out = _node_brief(n, srcs, devs)
        out.update({
            "status": n["status"], "status_reason": n["status_reason"],
            "parent": {"id": str(parent["id"]), "path": parent["path"]} if parent else None,
            "provider_id": n["provider_id"], "checksum": n["checksum"],
            "created_at": _ts(n["created_at_src"]), "owner": n["owner"], "last_modified_by": n["last_modified_by"],
            "first_indexed_at": _ts(n["first_indexed_at"]), "indexed_at": _ts(n["indexed_at"]),
            "deleted_at": _ts(n["deleted_at"]), "source_type": src["type"],
            "content_status": n["content_status"], "content_version": n["content_version"],
        })
        if n["kind"] == "shortcut":
            tgt = conn.execute("SELECT id, path FROM drive_index.nodes WHERE source_id = %s AND provider_id = %s",
                               (n["source_id"], n["shortcut_target"])).fetchone()
            out["shortcut_target"] = {"provider_id": n["shortcut_target"],
                                      "target_mime": (n["extra"] or {}).get("target_mime"),
                                      "indexed_as": {"id": str(tgt["id"]), "path": tgt["path"]} if tgt else None}
        if n["alt_parent_provider_ids"]:
            alts = conn.execute("SELECT path FROM drive_index.nodes WHERE source_id = %s AND provider_id = ANY(%s)",
                                (n["source_id"], n["alt_parent_provider_ids"])).fetchall()
            out["also_in_folders"] = [a["path"] for a in alts] or n["alt_parent_provider_ids"]
        if n["kind"] == "folder":
            out["contents"] = conn.execute(
                """SELECT count(*) FILTER (WHERE kind = 'file') AS files, count(*) FILTER (WHERE kind = 'folder') AS folders
                   FROM drive_index.nodes WHERE parent_id = %s AND status = 'active'""", (n["id"],)).fetchone()
        if n["content_status"] == "extracted" and _content_available(conn):
            s = conn.execute("SELECT summary, model FROM drive_index.summaries WHERE node_id = %s AND content_version = %s",
                             (n["id"], n["content_version"])).fetchone()
            if s:
                out["summary"] = s["summary"]
        out["history"] = _history(conn, n["id"])
    return out


def _since_window(conn, ctx: Ctx, since: str | None, peek: bool) -> tuple[str, list, dict]:
    """Return (where_sql, params, info) for whats_new using the session checkpoint or an explicit time."""
    xmin = conn.execute("SELECT pg_snapshot_xmin(pg_current_snapshot()) AS x").fetchone()["x"]
    info: dict[str, Any] = {}
    if since:
        t = _parse_time(since)
        info["since"] = _ts(t)
        where, params = "e.detected_at >= %s AND e.tx_id < %s", [t, xmin]
    else:
        cp = conn.execute("SELECT * FROM drive_index.mcp_checkpoints WHERE session_id = %s",
                          (ctx.session_id,)).fetchone()
        if cp and cp["last_xmin"] is not None:
            info["since"] = _ts(cp["checked_at"])
            info["since_last_check"] = True
            where, params = "e.tx_id >= %s AND e.tx_id < %s", [cp["last_xmin"], xmin]
        else:
            t = utcnow() - timedelta(hours=24)
            info["since"] = _ts(t)
            info["note"] = "first check in this session: showing the last 24 hours"
            where, params = "e.detected_at >= %s AND e.tx_id < %s", [t, xmin]
    if not peek:
        conn.execute(
            """INSERT INTO drive_index.mcp_checkpoints (account_id, session_id, last_xmin, last_event_id, checked_at)
               VALUES (%s, %s, %s, (SELECT max(id) FROM drive_index.change_events), now())
               ON CONFLICT (account_id, session_id) DO UPDATE SET last_xmin = EXCLUDED.last_xmin,
                 last_event_id = EXCLUDED.last_event_id, checked_at = now()""",
            (ctx.account_id, ctx.session_id, xmin),
        )
        info["checkpoint_saved"] = True
    return where, params, info


def whats_new(ctx: Ctx, since: str | None = None, source: str | None = None, folder: str | None = None,
              include_modified: bool = False, include_folders: bool = True, peek: bool = False,
              limit: int = 100) -> dict:
    """Files/folders added since a time, or since this session's last check (FR-29, FR-40)."""
    lim = _limit(limit, 100)
    with db.scoped(ctx.account_id) as conn:
        where, params, info = _since_window(conn, ctx, since, peek)
        types = ["added"] + (["modified"] if include_modified else [])
        where += " AND e.type = ANY(%s)"
        params.append(types)
        src = resolve_source(conn, source)
        if src:
            where += " AND e.source_id = %s"
            params.append(src["id"])
        if folder:
            f = normalize_user_path(folder)
            where += " AND e.path LIKE %s"
            params.append(escape_like(f.rstrip("/") + "/") + "%")
        if not include_folders:
            where += " AND e.kind <> 'folder'"
        total, _more = _capped_count(conn, f"FROM drive_index.change_events e WHERE {where}", params)
        raw = conn.execute(
            f"""SELECT n.*, e.id AS event_id, e.type, e.node_id, e.detected_at, e.occurred_at,
                       d.name AS device_name
                FROM drive_index.change_events e JOIN drive_index.nodes n ON n.id = e.node_id
                LEFT JOIN drive_index.devices d ON d.id = e.device_id
                WHERE {where} ORDER BY e.id DESC LIMIT %s""", params + [lim * 3 + 1]).fetchall()
        seen, rows = set(), []
        for r in raw:  # newest event per (file, change type)
            k = (r["node_id"], r["type"])
            if k not in seen:
                seen.add(k)
                rows.append(r)
        if len(raw) > lim * 3:
            rows.append(raw[-1])  # make sure truncation is reported
        srcs, devs = _sources(conn), _devices(conn)
    truncated = len(rows) > lim
    groups: dict[tuple[str, str], dict] = {}
    for r in rows[:lim]:
        src_row = srcs.get(str(r["source_id"]))
        folder_path = posixpath.dirname(r["path"]) or "/"
        key = (str(r["source_id"]), folder_path)
        g = groups.setdefault(key, {"source": src_row["name"] if src_row else None, "folder": folder_path,
                                    "device": r["device_name"], "items": []})
        item = _node_brief(r, srcs, devs)
        item.update(change=r["type"], detected_at=_ts(r["detected_at"]))
        if r["status"] != "active":
            item["now"] = r["status"]
        g["items"].append(item)
    return {**info, "total": total, "returned": min(len(rows), lim), "truncated": truncated,
            "hint": "more results: call list_changes with the same since/source" if truncated else None,
            "groups": sorted(groups.values(), key=lambda g: (g["source"] or "", g["folder"]))}


def list_changes(ctx: Ctx, since: str | None = "24h", until: str | None = None, source: str | None = None,
                 folder: str | None = None, types: list[str] | None = None, limit: int = 100,
                 cursor: str | None = None) -> dict:
    lim, off = _limit(limit, 100), _dec_cursor(cursor)
    with db.scoped(ctx.account_id) as conn:
        where, params = ["TRUE"], []
        if since:
            where.append("e.detected_at >= %s")
            params.append(_parse_time(since))
        if until:
            where.append("e.detected_at < %s")
            params.append(_parse_time(until))
        src = resolve_source(conn, source)
        if src:
            where.append("e.source_id = %s")
            params.append(src["id"])
        if folder:
            f = normalize_user_path(folder).rstrip("/")
            where.append("(e.path LIKE %s OR e.before->>'path' LIKE %s)")
            params += [escape_like(f + "/") + "%"] * 2
        if types:
            where.append("e.type = ANY(%s)")
            params.append(list(types))
        w = " AND ".join(where)
        total, more = _capped_count(conn, f"FROM drive_index.change_events e WHERE {w}", params)
        rows = conn.execute(
            f"""SELECT e.*, d.name AS device_name FROM drive_index.change_events e
                LEFT JOIN drive_index.devices d ON d.id = e.device_id
                WHERE {w} ORDER BY e.id DESC LIMIT %s OFFSET %s""", params + [lim, off]).fetchall()
        srcs = _sources(conn)
    out = []
    for r in rows:
        b, a = r["before"] or {}, r["after"] or {}
        item = {"event_id": r["id"], "type": r["type"], "kind": r["kind"], "name": r["name"], "path": r["path"],
                "source": srcs[str(r["source_id"])]["name"] if str(r["source_id"]) in srcs else None,
                "node_id": str(r["node_id"]) if r["node_id"] else None,
                "occurred_at": _ts(r["occurred_at"]), "detected_at": _ts(r["detected_at"])}
        if r["type"] in ("renamed", "moved"):
            item["from"] = b.get("path")
        if r["type"] == "modified" and b.get("size") != a.get("size"):
            item["size_from"], item["size_to"] = b.get("size"), a.get("size")
        if a.get("descendants"):
            item["descendants"] = a["descendants"]
        if a.get("reason"):
            item["reason"] = a["reason"]
        if r["device_name"]:
            item["device"] = r["device_name"]
        out.append(item)
    by_type: dict[str, int] = {}
    nxt = off + lim if (off + lim < total or more) and len(rows) == lim else None
    for i in out:
        by_type[i["type"]] = by_type.get(i["type"], 0) + 1
    return {"total": total, "total_is_lower_bound": more, "returned": len(out), "by_type_in_page": by_type,
            "changes": out,
            "next_cursor": _enc_cursor(nxt) if nxt is not None else None}


def describe_file(ctx: Ctx, node_id: str | None = None, path: str | None = None, source: str | None = None) -> dict:
    """A plain-language description from metadata (Phase 1) plus a content summary when extracted (Phase 2)."""
    f = get_file(ctx, node_id=node_id, path=path, source=source)
    with db.scoped(ctx.account_id) as conn:
        n = conn.execute("SELECT * FROM drive_index.nodes WHERE id = %s", (f["id"],)).fetchone()
        siblings = conn.execute(
            """SELECT name, kind, size, category FROM drive_index.nodes WHERE parent_id = %s AND id <> %s
               AND status = 'active' ORDER BY lower(name) LIMIT 200""", (n["parent_id"], n["id"])).fetchall() \
            if n["parent_id"] else []
        stem = _stem(n["name"])
        similar = conn.execute(
            """SELECT id, name, path, modified_at_src, status, size FROM drive_index.nodes
               WHERE source_id = %s AND id <> %s AND kind = 'file' AND lower(name) LIKE %s
               ORDER BY modified_at_src DESC NULLS LAST LIMIT 10""",
            (n["source_id"], n["id"], escape_like(stem.lower()) + "%")).fetchall() if stem and len(stem) >= 4 else []
        added = conn.execute(
            """SELECT e.detected_at, e.occurred_at, d.name AS device FROM drive_index.change_events e
               LEFT JOIN drive_index.devices d ON d.id = e.device_id
               WHERE e.node_id = %s AND e.type = 'added' ORDER BY e.id LIMIT 1""", (n["id"],)).fetchone()
    kind_word = {"folder": "folder", "shortcut": "shortcut"}.get(n["kind"]) or (
        f"{f.get('type') or 'file'} file" + (f" (.{n['ext']})" if n["ext"] else ""))
    parts = [f"“{n['name']}” is a {kind_word}"]
    if n["kind"] == "file":
        parts[-1] += f" of {human_size(n['size'])}" if n["size"] is not None else " (Google-native, no fixed size)"
    parts[-1] += f" in {f['source']} at {posixpath.dirname(n['path']) or '/'}."
    who = n["owner"] or n["last_modified_by"]
    if added:
        when = (added["occurred_at"] or added["detected_at"]).strftime("%Y-%m-%d %H:%M UTC")
        dev = f" on {added['device']}" if added["device"] else ""
        parts.append(f"It was added{f' by {who}' if who else ''}{dev} around {when}.")
    elif who:
        parts.append(f"Owner: {who}.")
    if n["modified_at_src"]:
        parts.append(f"Last modified {n['modified_at_src']:%Y-%m-%d %H:%M} UTC"
                     + (f" by {n['last_modified_by']}." if n["last_modified_by"] else "."))
    if siblings:
        cats: dict[str, int] = {}
        for sb in siblings:
            cats[sb["category"] or sb["kind"]] = cats.get(sb["category"] or sb["kind"], 0) + 1
        parts.append("It sits alongside " + ", ".join(f"{v} {k}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:5])
                     + " item(s) in the same folder.")
    earlier = [s for s in similar if not n["modified_at_src"] or not s["modified_at_src"] or s["modified_at_src"] <= n["modified_at_src"]]
    if earlier:
        parts.append("Earlier/related versions with similar names: " +
                     "; ".join(f"{s['name']} ({s['modified_at_src']:%Y-%m-%d})" if s["modified_at_src"] else s["name"]
                               for s in earlier[:5]) + ".")
    result = {"id": f["id"], "name": n["name"], "path": n["path"], "source": f["source"],
              "description": " ".join(parts),
              "related_versions": [{"id": str(s["id"]), "name": s["name"], "path": s["path"],
                                    "modified_at": _ts(s["modified_at_src"]), "status": s["status"]} for s in similar],
              "web_link": n["web_link"], "local_path": n["local_path"]}
    if f.get("summary"):
        result["content_summary"] = f["summary"]
    elif n["content_status"] in ("queued",):
        result["content_summary_status"] = "content extraction queued"
    return result


def _stem(name: str) -> str:
    import re
    base = name.rsplit(".", 1)[0]
    base = re.sub(r"([_\-\s]*(v|ver|version|final|draft|copy)[_\-\s]*\d*)+$", "", base, flags=re.I)
    base = re.sub(r"[_\-\s]*\(\d+\)$", "", base)
    return base.strip(" _-")


# ------------------------------------------------------------------ control tools
def resync_source(ctx: Ctx, source: str) -> dict:
    with db.scoped(ctx.account_id) as conn:
        src = resolve_source(conn, source)
        if not src:
            raise ToolError("source is required")
        revived = queue.retry_dead(conn, src["id"])
        if src["type"] == "gdrive":
            queue.enqueue(conn, ctx.account_id, "drive_reconcile", src["id"], dedupe_key=f"reconcile:{src['id']}")
            msg = "full reconcile queued"
        else:
            conn.execute("UPDATE drive_index.sources SET pending_command = 'full_rescan' WHERE id = %s", (src["id"],))
            msg = "full rescan requested; the agent picks it up on its next heartbeat"
        audit(conn, ctx.account_id, ctx.actor, "source.resync", str(src["id"]))
    return {"source": src["name"], "result": msg, "dead_jobs_retried": revived}


def pause_source(ctx: Ctx, source: str) -> dict:
    with db.scoped(ctx.account_id) as conn:
        src = resolve_source(conn, source)
        set_status(conn, src["id"], "paused", "paused by user")
        if src["type"] == "gdrive":
            queue.enqueue(conn, ctx.account_id, "drive_stop_channel", src["id"], dedupe_key=f"stop:{src['id']}")
        audit(conn, ctx.account_id, ctx.actor, "source.paused", str(src["id"]))
    return {"source": src["name"], "status": "paused"}


def resume_source(ctx: Ctx, source: str) -> dict:
    with db.scoped(ctx.account_id) as conn:
        src = resolve_source(conn, source)
        if src["status"] != "paused":
            return {"source": src["name"], "status": src["status"], "note": "source was not paused"}
        cur = conn.execute("SELECT drive_page_token, last_full_scan_at FROM drive_index.sync_cursors WHERE source_id = %s",
                           (src["id"],)).fetchone()
        if src["type"] == "gdrive":
            if cur and cur["drive_page_token"]:
                set_status(conn, src["id"], "live")
                queue.enqueue(conn, ctx.account_id, "drive_pull_changes", src["id"], dedupe_key=f"pull:{src['id']}")
            else:
                set_status(conn, src["id"], "connecting")
                queue.enqueue(conn, ctx.account_id, "drive_initial_crawl", src["id"], dedupe_key=f"crawl:{src['id']}")
        else:
            set_status(conn, src["id"], "live" if cur and cur["last_full_scan_at"] else "initial_scan")
            # The agent catches up with a diff scan when it sees the source resumed.
            conn.execute("UPDATE drive_index.sources SET pending_command = 'catch_up' WHERE id = %s", (src["id"],))
        audit(conn, ctx.account_id, ctx.actor, "source.resumed", str(src["id"]))
        status = get_source(conn, src["id"])["status"]
    return {"source": src["name"], "status": status}


def get_connect_link(ctx: Ctx, kind: str = "gdrive") -> dict:
    base = get_settings().public_base_url.rstrip("/")
    if kind == "gdrive":
        return {"url": f"{base}/app#connect-drive",
                "instructions": "Open this link in a browser, sign in with Google and choose My Drive, folders or shared drives to watch."}
    return {"url": f"{base}/app#devices",
            "instructions": "Open this link, create a pairing code, then on the computer run: nous-sync pair <CODE> and nous-sync add <folder>."}
