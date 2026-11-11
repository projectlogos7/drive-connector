"""Load test (M5): build a large index for one account and time MCP reads and indexer throughput.

    python scripts/loadtest.py --nodes 1000000 --sources 50

Uses NOUS_DRIVE_DATABASE_URL. Creates a separate account named "loadtest-<random>"; pass --cleanup to
delete it afterwards. Reported numbers are wall-clock per call (median of 5).
"""

from __future__ import annotations

import argparse
import random
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

from nous_drive import db
from nous_drive.accounts import create_account, create_source
from nous_drive.indexer import Indexer
from nous_drive.mcp import tools as T
from nous_drive.models import NodeEvent

EXTS = ["mov", "mp4", "wav", "jpg", "docx", "pdf", "xlsx", "txt", "prproj", "psd"]
CATS = {"mov": "video", "mp4": "video", "wav": "audio", "jpg": "image", "docx": "document", "pdf": "pdf",
        "xlsx": "spreadsheet", "txt": "text", "prproj": "project", "psd": "image"}


def build(account_id, n_nodes: int, n_sources: int, fanout: int = 30) -> list[dict]:
    per = n_nodes // n_sources
    sources = []
    now = datetime.now(timezone.utc)
    with db.scoped(account_id) as conn:
        for s in range(n_sources):
            sources.append(create_source(conn, account_id, type_="gdrive", root_kind="folder",
                                         root_ref=f"root-{s}", name=f"Source {s:02d}"))
    for src in sources:
        rows, events = [], []
        root_id = uuid.uuid4()
        rows.append((root_id, account_id, src["id"], f"root-{src['id']}", None, True, "folder", "folder",
                     src["name"], "/", 0, None, None, now))
        folders = [(root_id, "/", 0)]
        i = 0
        while i < per:
            parent_id, ppath, depth = random.choice(folders[-200:]) if len(folders) > 1 else folders[0]
            is_folder = random.random() < 1 / fanout and depth < 8
            nid = uuid.uuid4()
            if is_folder:
                name = f"Folder {i}"
                path = (ppath.rstrip("/") + "/" + name)
                rows.append((nid, account_id, src["id"], f"p{i}", parent_id, False, "folder", "folder", name, path,
                              depth + 1, None, None, now))
                folders.append((nid, path, depth + 1))
            else:
                ext = random.choice(EXTS)
                name = f"file_{i}_v{random.randint(1, 5)}.{ext}"
                path = ppath.rstrip("/") + "/" + name
                rows.append((nid, account_id, src["id"], f"p{i}", parent_id, False, "file", CATS[ext], name, path,
                              depth + 1, ext, random.randint(1_000, 50_000_000_000),
                              now - timedelta(minutes=random.randint(0, 60 * 24 * 365))))
                if random.random() < 0.05:
                    events.append((account_id, src["id"], nid, "added", "file", name, path,
                                   now - timedelta(minutes=random.randint(0, 60 * 48))))
            i += 1
        with db.system() as conn:
            with conn.cursor().copy(
                "COPY drive_index.nodes (id, account_id, source_id, provider_id, parent_id, is_root, kind, category, "
                "name, path, depth, ext, size, modified_at_src) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
            with conn.cursor().copy(
                "COPY drive_index.change_events (account_id, source_id, node_id, type, kind, name, path, detected_at) "
                "FROM STDIN") as cp:
                for e in events:
                    cp.write_row(e)
            conn.execute("UPDATE drive_index.sources SET status = 'live' WHERE id = %s", (src["id"],))
    with db.system() as conn:
        conn.execute("ANALYZE drive_index.nodes")
        conn.execute("ANALYZE drive_index.change_events")
    return sources


def timed(fn, *a, **kw) -> float:
    xs = []
    for _ in range(5):
        t = time.perf_counter()
        fn(*a, **kw)
        xs.append((time.perf_counter() - t) * 1000)
    return statistics.median(xs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=200_000)
    ap.add_argument("--sources", type=int, default=50)
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()
    acct, _ = create_account(f"loadtest-{uuid.uuid4().hex[:6]}")
    aid = str(acct["id"])
    t = time.perf_counter()
    sources = build(aid, args.nodes, args.sources)
    print(f"built {args.nodes:,} nodes in {args.sources} sources in {time.perf_counter() - t:.1f}s")
    ctx = T.Ctx(account_id=aid, session_id="load")
    s0 = sources[0]["name"]
    with db.scoped(aid) as conn:
        deep = conn.execute("SELECT path FROM drive_index.nodes WHERE source_id = %s AND kind = 'folder' "
                            "AND depth = 2 LIMIT 1", (sources[0]["id"],)).fetchone()["path"]
    results = {
        "list_sources": timed(T.list_sources, ctx),
        "get_sync_status(all)": timed(T.get_sync_status, ctx),
        "get_tree(depth 2, files)": timed(T.get_tree, ctx, "/", None, s0, 2, True),
        "list_files(folder)": timed(T.list_files, ctx, deep, None, s0),
        "list_files(recursive .mov > 2GB)": timed(T.list_files, ctx, "/", None, s0, True, None, "mov", 2 * 1024 ** 3),
        "list_files(account-wide video)": timed(T.list_files, ctx, None, None, None, False, "video"),
        "search_files('file_12') worst case": timed(T.search_files, ctx, "file_12"),
        "search_files('file_123456')": timed(T.search_files, ctx, "file_123456"),
        "search_files('folder 12 v3')": timed(T.search_files, ctx, "folder 12 v3"),
        "whats_new(since 24h, peek)": timed(T.whats_new, ctx, "24h", None, None, False, True, True),
        "list_changes(24h)": timed(T.list_changes, ctx, "24h"),
    }
    for k, v in results.items():
        print(f"  {k:<36} {v:8.1f} ms")
    # Indexer throughput through the real code path
    src = sources[-1]
    evs = [NodeEvent(provider_id=f"new{i}", name=f"new_{i}.mov", parent_provider_id=f"root-{src['id']}", size=i)
           for i in range(5000)]
    t = time.perf_counter()
    with db.scoped(aid) as conn:
        Indexer(conn, aid, src).apply(evs)
    dt = time.perf_counter() - t
    print(f"  indexer: {len(evs) / dt:,.0f} events/s ({len(evs)} upserts)")
    if args.cleanup:
        with db.system() as conn:
            conn.execute("DELETE FROM drive_index.accounts WHERE id = %s", (aid,))
        print("cleaned up")


if __name__ == "__main__":
    main()
