"""Google Drive sync: initial crawl, changes feed, reconcile, push channels."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from psycopg.types.json import Jsonb

from ... import db, queue
from ...accounts import get_source, set_status
from ...config import get_settings
from ...crypto import hash_key
from ...indexer import Indexer
from ...models import NodeEvent
from ...reporter import file_info, report
from ...util import utcnow
from .auth import make_client
from .client import FOLDER_MIME, AuthExpired, DriveClient
from .mapping import SHARED_WITH_ME, _ts, file_to_event

log = logging.getLogger(__name__)

BFS_BATCH = 40  # folders per "'a' in parents or 'b' in parents ..." query


def _content_policy():
    from ...content.policy import should_extract
    return should_extract


class DriveSync:
    def __init__(self, account_id, source_id, job_id: int | None = None, client: DriveClient | None = None):
        self.account_id = str(account_id)
        self.source_id = str(source_id)
        self.job_id = job_id
        with db.scoped(self.account_id) as conn:
            self.source = get_source(conn, self.source_id)
            if not self.source:
                raise LookupError(f"source {source_id} not found")
            self.cursor = conn.execute(
                "SELECT * FROM drive_index.sync_cursors WHERE source_id = %s", (self.source_id,)
            ).fetchone()
        self.client = client or make_client(self.account_id, self.source["connection_id"])
        self.drive_id = self.source["root_ref"] if self.source["root_kind"] == "shared_drive" else None
        self._known_folders: set[str] | None = None

    # ------------------------------------------------------------------ persistence
    def _live(self, stage: str, base: int = 0, total: int | None = None, count: int | None = None):
        last = (count or 0) - 1

        def on_event(ev: NodeEvent, i: int) -> None:
            if ev.op == "upsert" or i == last:
                report(self.account_id, self.source_id, stage, done=base + i + 1, total=total, active=True,
                       force=i == last, current=file_info(ev.name, ev.size, None, ev.kind))
        return on_event

    def _apply(self, events: list[NodeEvent], cursor: dict | None = None, finish_scan: str | None = None,
               rebuild: bool = False, scan_started_at=None, on_event=None) -> None:
        with db.scoped(self.account_id) as conn:
            ix = Indexer(conn, self.account_id, self.source, content_policy=_content_policy(), on_event=on_event)
            if events:
                ix.apply(events)
            if rebuild:
                ix.rebuild_paths()
            if finish_scan:
                ix.finish_scan(finish_scan, started_at=scan_started_at)
                from ...stats import refresh
                refresh(conn, self.source_id)
            if cursor:
                sets = ", ".join(f"{k} = %({k})s" for k in cursor)
                params = {k: (Jsonb(v) if isinstance(v, dict) else v) for k, v in cursor.items()}
                conn.execute(f"UPDATE drive_index.sync_cursors SET {sets}, updated_at = now() "
                             f"WHERE source_id = %(sid)s", dict(params, sid=self.source_id))
                self.cursor.update(cursor)
            for node_id in ix.stats.content_queued:
                queue.enqueue(conn, self.account_id, "content_process", self.source_id,
                              {"node_id": node_id}, dedupe_key=f"content:{node_id}")
        queue.touch(self.job_id)

    def _status(self, status: str, reason: str | None = None) -> None:
        with db.scoped(self.account_id) as conn:
            set_status(conn, self.source_id, status, reason)

    # ------------------------------------------------------------------ roots
    def _ensure_roots(self, scan_id: str | None = None) -> None:
        with db.scoped(self.account_id) as conn:
            ix = Indexer(conn, self.account_id, self.source)
            link = None
            if self.source["root_kind"] == "folder":
                link = f"https://drive.google.com/drive/folders/{self.source['root_ref']}"
            ix.ensure_root(self.source["name"], self.source["root_ref"], web_link=link)
            if self.source["root_kind"] == "my_drive":
                ix.apply([NodeEvent(provider_id=SHARED_WITH_ME, kind="folder", name="Shared with me",
                                    parent_provider_id=self.source["root_ref"], scan_id=scan_id)])

    def known_folders(self) -> set[str]:
        if self._known_folders is None:
            with db.scoped(self.account_id) as conn:
                rows = conn.execute(
                    """SELECT provider_id FROM drive_index.nodes WHERE source_id = %s AND kind = 'folder'
                       AND status IN ('active','trashed','unavailable')""",
                    (self.source_id,),
                ).fetchall()
            self._known_folders = {r["provider_id"] for r in rows} | {self.source["root_ref"]}
        return self._known_folders

    # ------------------------------------------------------------------ crawl / reconcile
    def crawl(self, reconcile: bool = False) -> dict:
        """Full listing of the source. Resumable: progress is checkpointed after every page.

        The changes start token is stored *before* listing, then changes since that token are
        replayed after the listing so nothing that changed during a long crawl is lost (FR-6).
        """
        progress = dict(self.cursor.get("scan_progress") or {})
        resume = progress.get("state") == "running" and progress.get("reconcile", False) == reconcile
        if not resume:
            progress = {
                "state": "running", "reconcile": reconcile, "scan_id": uuid.uuid4().hex,
                "start_token": self.client.start_page_token(self.drive_id),
                "mode": "bfs" if self.source["root_kind"] == "folder" else "flat",
                "frontier": [self.source["root_ref"]], "batch": None, "page_token": None,
                "folders": 0, "files": 0, "total": None, "counted": 0, "phase": "starting",
                "started_at": utcnow().isoformat(),
            }
            if not reconcile:
                self._status("initial_scan", "first full index in progress")
            self._apply([], cursor={"scan_progress": progress, "current_scan_id": progress["scan_id"]})
        scan_id = progress["scan_id"]
        self._ensure_roots(scan_id)

        if progress.get("total") is None:
            progress["phase"] = "counting"
            progress["total"] = self._count_total(progress)
        progress["phase"] = "listing"
        if progress["mode"] == "flat":
            self._crawl_flat(progress)
        else:
            self._crawl_bfs(progress)

        # Replay changes that happened while we were listing, tagged as seen in this scan.
        progress["phase"] = "catching_up"
        self._apply([], cursor={"scan_progress": progress})
        token = self._drain_changes(progress["start_token"], scan_id=scan_id)
        progress.update(state="done", phase="done", finished_at=utcnow().isoformat(), frontier=[], batch=None,
                        page_token=None)
        self._apply([], rebuild=True, finish_scan=scan_id, scan_started_at=progress["started_at"], cursor={
            "drive_page_token": token, "last_full_scan_at": utcnow(), "scan_progress": progress,
            "current_scan_id": None, "last_poll_at": utcnow(),
        })
        report(self.account_id, self.source_id, "index", done=progress["files"] + progress["folders"],
               total=progress.get("total"), active=False, current={}, force=True)
        self._status("live")
        try:
            self.ensure_channel()
        except Exception as e:  # push is an optimisation; polling covers it
            log.warning("could not open push channel for %s: %s", self.source_id, e)
        return progress

    def _count_total(self, progress: dict) -> int:
        """Cheap metadata-only pre-count (ids only, 1000 per request) so progress can show a real percentage."""
        n = 0
        light = "id,mimeType,driveId"
        if progress["mode"] == "flat":
            corpora = None if self.drive_id else "user"
            for files in self.client.iter_files(drive_id=self.drive_id, corpora=corpora, fields=light, page_size=1000):
                n += sum(1 for f in files if self._in_flat_scope(f))
                progress["counted"] = n
                report(self.account_id, self.source_id, "discover", done=n, active=True, force=True)
                self._apply([], cursor={"scan_progress": progress})
            return n
        frontier = [self.source["root_ref"]]
        while frontier:
            batch, frontier = frontier[:BFS_BATCH], frontier[BFS_BATCH:]
            q = " or ".join(f"'{fid}' in parents" for fid in batch)
            for files in self.client.iter_files(q=q, corpora="allDrives", fields=light, page_size=1000):
                n += len(files)
                frontier += [f["id"] for f in files if f.get("mimeType") == FOLDER_MIME]
                progress["counted"] = n
                report(self.account_id, self.source_id, "discover", done=n, active=True, force=True)
                self._apply([], cursor={"scan_progress": progress})
        return n

    def _count(self, progress: dict, files: list[dict]) -> None:
        for f in files:
            if f.get("mimeType") == FOLDER_MIME:
                progress["folders"] += 1
            else:
                progress["files"] += 1

    def _crawl_flat(self, progress: dict) -> None:
        corpora = None if self.drive_id else "user"
        while True:
            page = self.client.list_files(page_token=progress.get("page_token"), drive_id=self.drive_id,
                                          corpora=corpora)
            files = [f for f in page.get("files", []) if self._in_flat_scope(f)]
            base = progress["files"] + progress["folders"]
            self._count(progress, files)
            events = [file_to_event(f, self.source, scan_id=progress["scan_id"]) for f in files]
            progress["page_token"] = page.get("nextPageToken")
            self._apply(events, cursor={"scan_progress": progress},
                        on_event=self._live("index", base, progress.get("total"), len(events)))
            if not progress["page_token"]:
                break

    def _in_flat_scope(self, f: dict) -> bool:
        if self.source["root_kind"] == "shared_drive":
            return f.get("driveId") == self.drive_id
        return not f.get("driveId")  # My Drive (and shared-with-me), not shared drives

    def _crawl_bfs(self, progress: dict) -> None:
        while progress["frontier"] or progress.get("batch"):
            if not progress.get("batch"):
                progress["batch"] = progress["frontier"][:BFS_BATCH]
                progress["frontier"] = progress["frontier"][BFS_BATCH:]
                progress["page_token"] = None
            batch = progress["batch"]
            q = " or ".join(f"'{fid}' in parents" for fid in batch)
            page = self.client.list_files(q=q, page_token=progress.get("page_token"), drive_id=None,
                                          corpora="allDrives")
            files = page.get("files", [])
            base = progress["files"] + progress["folders"]
            self._count(progress, files)
            batch_set = set(batch)
            events = []
            for f in files:
                parent = next((p for p in (f.get("parents") or []) if p in batch_set), None)
                events.append(file_to_event(f, self.source, scan_id=progress["scan_id"], parent_override=parent))
                if f.get("mimeType") == FOLDER_MIME:
                    progress["frontier"].append(f["id"])
            progress["page_token"] = page.get("nextPageToken")
            if not progress["page_token"]:
                progress["batch"] = None
            self._apply(events, cursor={"scan_progress": progress},
                        on_event=self._live("index", base, progress.get("total"), len(events)))

    def crawl_subtree(self, folder_id: str, scan_id: str | None = None) -> int:
        """Index everything below a folder that just entered the source (its children are not in the feed)."""
        frontier, n = [folder_id], 0
        while frontier:
            batch, frontier = frontier[:BFS_BATCH], frontier[BFS_BATCH:]
            q = " or ".join(f"'{fid}' in parents" for fid in batch)
            batch_set = set(batch)
            for files in self.client.iter_files(q=q, corpora="allDrives"):
                events = []
                for f in files:
                    parent = next((p for p in (f.get("parents") or []) if p in batch_set), None)
                    events.append(file_to_event(f, self.source, scan_id=scan_id, parent_override=parent))
                    if f.get("mimeType") == FOLDER_MIME:
                        frontier.append(f["id"])
                        self.known_folders().add(f["id"])
                n += len(events)
                self._apply(events)
        return n

    # ------------------------------------------------------------------ changes feed
    def pull(self) -> int:
        """Pull the changes feed from the stored token and advance it (FR-7)."""
        token = self.cursor.get("drive_page_token")
        if not token:
            return 0  # initial crawl not finished; it will replay changes itself
        n = 0
        while True:
            page = self.client.list_changes(token, drive_id=self.drive_id)
            events, entered = self._changes_to_events(page.get("changes", []))
            nxt = page.get("nextPageToken") or page.get("newStartPageToken")
            # Token advances in the same transaction that commits the batch (8.4).
            self._apply(events, cursor={"drive_page_token": nxt, "last_poll_at": utcnow()},
                        on_event=self._live("sync", 0, len(events), len(events)) if events else None)
            for fid in entered:
                n += self.crawl_subtree(fid)
            n += len(events)
            token = nxt
            if page.get("newStartPageToken") or not page.get("nextPageToken"):
                break
        if self.source["status"] in ("degraded", "error") and self.source["status_reason"] and \
                "job failed" in (self.source["status_reason"] or ""):
            self._status("live")
        return n

    def _drain_changes(self, token: str, scan_id: str | None) -> str:
        while True:
            page = self.client.list_changes(token, drive_id=self.drive_id)
            events, entered = self._changes_to_events(page.get("changes", []), scan_id=scan_id)
            self._apply(events)
            for fid in entered:
                self.crawl_subtree(fid, scan_id=scan_id)
            token = page.get("nextPageToken") or page.get("newStartPageToken")
            if page.get("newStartPageToken") or not page.get("nextPageToken"):
                return token

    def _membership(self, f: dict, known: set[str]) -> tuple[bool, str | None]:
        rk = self.source["root_kind"]
        parents = f.get("parents") or []
        if rk == "shared_drive":
            return f.get("driveId") == self.drive_id, None
        if rk == "my_drive":
            return not f.get("driveId"), None
        for p in parents:
            if p in known:
                return True, p
        return False, None

    def _existing(self, ids: Iterable[str]) -> dict[str, dict]:
        ids = list(ids)
        if not ids:
            return {}
        with db.scoped(self.account_id) as conn:
            rows = conn.execute(
                """SELECT provider_id, kind, status FROM drive_index.nodes WHERE source_id = %s
                   AND provider_id = ANY(%s)""",
                (self.source_id, ids),
            ).fetchall()
        return {r["provider_id"]: r for r in rows}

    def _changes_to_events(self, changes: list[dict], scan_id: str | None = None) -> tuple[list[NodeEvent], list[str]]:
        events: list[NodeEvent] = []
        entered: list[str] = []
        known = self.known_folders()
        files_changes = [c for c in changes if c.get("changeType", "file") == "file"]
        existing = self._existing(c["fileId"] for c in files_changes)
        unresolved: list[tuple[dict, dict]] = []
        for c in files_changes:
            f = c.get("file")
            if c.get("removed") or not f:
                if c["fileId"] in existing:
                    events.append(NodeEvent(op="delete", provider_id=c["fileId"], occurred_at=_ts(c.get("time")),
                                            reason="permanently deleted or access removed"))
                continue
            unresolved.append((c, f))
        # Resolve parents iteratively so a new folder and its new children in one page both land.
        progress = True
        while unresolved and progress:
            progress = False
            rest = []
            for c, f in unresolved:
                member, parent = self._membership(f, known)
                if not member:
                    rest.append((c, f))
                    continue
                progress = True
                events.append(file_to_event(f, self.source, scan_id=scan_id, occurred_at=_ts(c.get("time")),
                                            parent_override=parent))
                if f.get("mimeType") == FOLDER_MIME:
                    if f["id"] not in known and f["id"] not in existing:
                        entered.append(f["id"])
                    elif f["id"] in existing and existing[f["id"]]["status"] in ("removed", "deleted"):
                        entered.append(f["id"])
                    known.add(f["id"])
            unresolved = rest
        for c, f in unresolved:
            ex = existing.get(f["id"])
            if ex and ex["status"] not in ("deleted", "removed"):
                events.append(NodeEvent(op="remove", provider_id=f["id"], occurred_at=_ts(c.get("time")),
                                        reason="moved out of the watched folder"))
                known.discard(f["id"])
        return events, entered

    # ------------------------------------------------------------------ push channels
    def ensure_channel(self, force: bool = False) -> bool:
        s = get_settings()
        if not s.push_enabled or not s.public_base_url.startswith("https://"):
            return False
        token = self.cursor.get("drive_page_token")
        if not token:
            return False
        exp = self.cursor.get("channel_expires_at")
        if not force and exp and exp > utcnow() + timedelta(seconds=s.drive_channel_renew_before_seconds):
            return True
        channel_id = str(uuid.uuid4())
        secret = secrets.token_urlsafe(24)
        expiration = utcnow() + timedelta(seconds=s.drive_channel_ttl_seconds)
        resp = self.client.watch_changes(token, channel_id, s.public_base_url.rstrip("/") + "/webhooks/drive",
                                         secret, int(expiration.timestamp() * 1000), drive_id=self.drive_id)
        if resp.get("expiration"):
            expiration = datetime.fromtimestamp(int(resp["expiration"]) / 1000, tz=timezone.utc)
        old_id, old_res = self.cursor.get("channel_id"), self.cursor.get("channel_resource_id")
        self._apply([], cursor={"channel_id": channel_id, "channel_resource_id": resp.get("resourceId"),
                                "channel_token_hash": hash_key(secret), "channel_expires_at": expiration})
        if old_id and old_res:
            try:
                self.client.stop_channel(old_id, old_res)
            except Exception as e:
                log.info("stopping old channel failed: %s", e)
        return True

    def stop_channel(self) -> None:
        cid, res = self.cursor.get("channel_id"), self.cursor.get("channel_resource_id")
        if cid and res:
            self.client.stop_channel(cid, res)
        self._apply([], cursor={"channel_id": None, "channel_resource_id": None, "channel_expires_at": None,
                                "channel_token_hash": None})


def handle_notification(channel_id: str | None, token: str | None, resource_state: str | None) -> bool:
    """Webhook entry point. Returns True when a pull was queued. Verifies the channel token (8.5)."""
    if not channel_id:
        return False
    with db.system() as conn:
        row = conn.execute(
            """SELECT c.source_id, c.account_id, c.channel_token_hash, s.status FROM drive_index.sync_cursors c
               JOIN drive_index.sources s ON s.id = c.source_id WHERE c.channel_id = %s""",
            (channel_id,),
        ).fetchone()
    if not row or not token or hash_key(token) != row["channel_token_hash"]:
        raise PermissionError("unknown channel or bad channel token")
    if resource_state == "sync" or row["status"] == "paused":
        return False
    with db.scoped(row["account_id"]) as conn:
        # Short delay coalesces bursts of notifications into one pull.
        queue.enqueue(conn, row["account_id"], "drive_pull_changes", row["source_id"],
                      dedupe_key=f"pull:{row['source_id']}", delay_seconds=3)
    return True


def run_job(job: dict) -> None:
    kind = job["kind"]
    try:
        sync = DriveSync(job["account_id"], job["source_id"], job_id=job["id"])
        if sync.source["status"] == "paused" and kind != "drive_stop_channel":
            return
        if kind == "drive_initial_crawl":
            done = (sync.cursor.get("scan_progress") or {}).get("state") == "done"
            if done and sync.cursor.get("drive_page_token"):
                return  # already indexed (duplicate request)
            sync.crawl(reconcile=False)
        elif kind == "drive_reconcile":
            sync.crawl(reconcile=True)
        elif kind == "drive_pull_changes":
            sync.pull()
        elif kind == "drive_renew_channel":
            sync.ensure_channel(force=True)
        elif kind == "drive_stop_channel":
            sync.stop_channel()
        else:
            raise ValueError(f"unknown drive job {kind}")
    except AuthExpired as e:
        with db.scoped(job["account_id"]) as conn:
            set_status(conn, job["source_id"], "error", "Google access expired or was revoked — reconnect Drive")
        raise
