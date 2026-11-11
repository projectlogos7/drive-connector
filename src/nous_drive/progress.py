"""Progress per source for the web app, the live stream and MCP.

Four stages, each with done/total and a state (pending · active · done · skipped):

  discover  finding what is there      (Drive: metadata-only count; computer: the agent's local scan)
  index     indexing files and folders (names, sizes, dates, paths)
  content   reading contents           (text extraction, Phase 2)
  embed     making it searchable       (in-house embeddings, Phase 2)

``overall`` combines them into one percent and a label. ``current`` is the file a worker is on right now
(with its size), taken from the live progress rows workers write while they run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

STAGES = [("discover", "Finding files"), ("index", "Indexing files"), ("content", "Reading contents"),
          ("embed", "Making searchable")]
WEIGHTS = {"discover": 5, "index": 50, "content": 30, "embed": 15}
FRESH = timedelta(seconds=20)


def _parse(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fresh(stage_data: dict | None, now: datetime) -> bool:
    if not stage_data:
        return False
    t = _parse(stage_data.get("updated_at"))
    return bool(t and now - t < FRESH)


def content_counts(conn, source_ids: list) -> dict[str, dict]:
    """Per source: files waiting for content, files done, and embedding jobs still to run."""
    if not source_ids:
        return {}
    rows = conn.execute(
        """SELECT source_id,
                  count(*) FILTER (WHERE content_status = 'queued') AS queued,
                  count(*) FILTER (WHERE content_status IN ('extracted','failed','skipped')) AS done,
                  count(*) FILTER (WHERE content_status = 'extracted') AS extracted
           FROM drive_index.nodes
           WHERE source_id = ANY(%s) AND kind = 'file' AND status = 'active' AND content_status <> 'not_requested'
           GROUP BY source_id""", (source_ids,)).fetchall()
    out = {str(r["source_id"]): dict(r) for r in rows}
    for r in conn.execute(
            """SELECT source_id, count(*) AS n FROM drive_index.index_jobs
               WHERE source_id = ANY(%s) AND kind = 'content_embed' AND status IN ('pending','running')
               GROUP BY source_id""", (source_ids,)):
        out.setdefault(str(r["source_id"]), {"queued": 0, "done": 0, "extracted": 0})["embed_pending"] = r["n"]
    return out


def build(src: dict, cur: dict | None, live: dict | None, counts: dict | None, jobs: dict | None,
          content_enabled: bool = True, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cur, live, counts, jobs = cur or {}, live or {}, counts or {}, jobs or {}
    sp = cur.get("scan_progress") or {}
    running = sp.get("state") == "running"
    phase = sp.get("phase") or ("listing" if running else None)
    status = src["status"]
    is_drive = src["type"] == "gdrive"
    st: dict[str, dict] = {k: {"key": k, "label": lbl, "state": "pending", "done": None, "total": None}
                           for k, lbl in STAGES}

    # -- discover + index (the file index)
    index_done = not running and status not in ("connecting", "initial_scan")
    total = sp.get("total")
    if is_drive:
        idx_done_count = (sp.get("files") or 0) + (sp.get("folders") or 0)
    else:
        idx_done_count = sp.get("processed") or 0
    li = live.get("index") or {}
    if running and _fresh(li, now) and li.get("done") is not None:
        idx_done_count = max(idx_done_count, li["done"])  # in-flight batch, not yet committed
    if index_done:
        st["discover"].update(state="done")
        st["index"].update(state="done", done=sp.get("total") or idx_done_count or None, total=sp.get("total"))
    elif running and phase in ("starting", "counting"):
        ld = live.get("discover") or {}
        st["discover"].update(state="active", done=max(sp.get("counted") or 0, ld.get("done") or 0))
    elif running:
        st["discover"].update(state="done", done=total, total=total)
        st["index"].update(state="active", done=idx_done_count, total=total)
        if phase == "catching_up":
            st["index"]["label"] = "Catching up on recent changes"
    elif status in ("connecting", "initial_scan"):
        st["discover"]["label"] = "Waiting to start" if is_drive or jobs.get("pending") else "Waiting for the computer"

    # -- content + embed (Phase 2)
    c = counts
    has_content = content_enabled and (c.get("queued") or c.get("done") or c.get("embed_pending"))
    if not content_enabled:
        st["content"]["state"] = st["embed"]["state"] = "skipped"
    elif has_content:
        ctotal = (c.get("queued") or 0) + (c.get("done") or 0)
        st["content"].update(done=c.get("done") or 0, total=ctotal,
                             state="active" if c.get("queued") else ("done" if index_done else "pending"))
        pend = c.get("embed_pending") or 0
        etotal = c.get("extracted") or 0
        st["embed"].update(done=max(etotal - pend, 0), total=etotal,
                           state="active" if pend else ("done" if index_done and not c.get("queued") else "pending"))
    elif index_done:
        st["content"]["state"] = st["embed"]["state"] = "done"

    # -- current file (whatever a worker touched most recently)
    current = None
    for key in ("index", "sync", "content", "embed"):
        d = live.get(key) or {}
        if d.get("active") is not False and _fresh(d, now) and (d.get("current") or {}).get("name"):
            t = _parse(d.get("updated_at"))
            if current is None or t > _parse(current["at"]):
                stage = "index" if key == "sync" else key
                current = {**d["current"], "stage": stage, "at": d["updated_at"],
                           "action": {"index": "Indexing", "content": "Reading", "embed": "Making searchable",
                                      "sync": "Updating"}[key]}

    # -- overall
    def frac(s: dict) -> float:
        if s["state"] in ("done", "skipped"):
            return 1.0
        if s["state"] == "pending" or not s.get("total"):
            return 0.0
        return min(1.0, (s.get("done") or 0) / max(s["total"], 1))
    weights = {k: (0 if st[k]["state"] == "skipped" else w) for k, w in WEIGHTS.items()}
    percent = round(100 * sum(frac(st[k]) * w for k, w in weights.items()) / max(sum(weights.values()), 1))
    active = next((st[k] for k, _ in STAGES if st[k]["state"] == "active"), None)
    busy = bool(active) or status in ("connecting", "initial_scan")
    if status == "paused":
        label, busy = "Paused", False
    elif status == "error":
        label, busy = src.get("status_reason") or "Needs attention", False
    elif active:
        n, t = active.get("done"), active.get("total")
        label = active["label"] + (f" — {n:,} of {t:,}" if t else (f" — {n:,} found" if n else ""))
    elif busy:
        label = st["discover"]["label"]
    else:
        label, percent = "Up to date — watching for changes", 100
    if busy:
        percent = min(percent, 99)
    step = next((i + 1 for i, (k, _) in enumerate(STAGES) if active and k == active["key"]), None)
    return {
        "source_id": str(src["id"]), "status": status, "busy": busy, "percent": percent, "label": label,
        "step": step, "steps": len([1 for k, _ in STAGES if st[k]["state"] != "skipped"]),
        "stages": [st[k] for k, _ in STAGES], "current": current,
        "started_at": sp.get("started_at") if running else None,
        # compatibility with the earlier progress block
        "running": running,
        "phase": status if status in ("paused", "error") else (
            "live" if not busy else (phase if running else (active["key"] if active else "queued"))),
        "done": active.get("done") if active else None, "total": active.get("total") if active else None,
    }


def snapshot(conn, account_id=None) -> list[dict]:
    """Progress for every source of the (RLS-scoped) account in a handful of queries."""
    from .config import get_settings
    from .accounts import merged_settings
    srcs = conn.execute("SELECT * FROM drive_index.sources ORDER BY created_at").fetchall()
    if not srcs:
        return []
    ids = [s["id"] for s in srcs]
    curs = {str(r["source_id"]): r for r in conn.execute(
        "SELECT source_id, scan_progress, stats FROM drive_index.sync_cursors WHERE source_id = ANY(%s)", (ids,))}
    live = {str(r["source_id"]): r["data"] for r in conn.execute(
        "SELECT source_id, data FROM drive_index.live_progress WHERE source_id = ANY(%s)", (ids,))}
    jobs: dict[str, dict] = {}
    for r in conn.execute(
            """SELECT source_id, status, count(*) AS n FROM drive_index.index_jobs
               WHERE source_id = ANY(%s) AND status IN ('pending','running') GROUP BY 1, 2""", (ids,)):
        jobs.setdefault(str(r["source_id"]), {})[r["status"]] = r["n"]
    counts = content_counts(conn, ids)
    g = get_settings().content_enabled
    out = []
    for s in srcs:
        sid = str(s["id"])
        enabled = g and bool(merged_settings(s["settings"]).get("content", {}).get("enabled"))
        p = build(s, curs.get(sid), live.get(sid), counts.get(sid), jobs.get(sid), content_enabled=enabled)
        st = (curs.get(sid) or {}).get("stats") or {}
        p.update(name=s["name"], type=s["type"], files=st.get("files"), folders=st.get("folders"),
                 bytes=st.get("bytes"))
        out.append(p)
    return out


def view(source: dict, cursor: dict | None, pending_crawl: bool = False) -> dict:
    """Single-source progress without live/content data (kept for callers that only have the cursor)."""
    return build(source, cursor, None, None, {"pending": 1} if pending_crawl else None)
