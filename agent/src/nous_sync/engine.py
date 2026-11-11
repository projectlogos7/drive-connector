"""Turn file-system observations into index events, keeping a stable id per file.

* catch_up(): compare a fresh scan with the last known state and emit only the difference (FR-18);
* full_scan(): emit every entry under a scan id so the server can sweep what is gone (resync);
* process_path()/process_move(): live events after debouncing.

Moves and renames keep the file's id: they are detected from the watcher's move events and, for
changes that happened while the agent was not running, from the file-system id (inode / file id).
"""

from __future__ import annotations

import logging
import os
import uuid

from .ignore import Ignorer
from .scanner import Entry, scan, stat_entry, to_event
from .state import Rec, State

log = logging.getLogger(__name__)

ROOT_ID = "__root__"


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _depth(rel: str) -> int:
    return rel.count("/")


class SourceEngine:
    def __init__(self, source_id: str, root: str, state: State, ignore: list[str] | None = None):
        self.source_id = source_id
        self.root = os.path.abspath(root)
        self.state = state
        self.ignorer = Ignorer(ignore)

    # ---------------------------------------------------------------- helpers
    def _parent_pid(self, rel: str, pending: dict[str, str] | None = None) -> str:
        parent = _parent(rel)
        if not parent:
            return ROOT_ID
        if pending and parent in pending:
            return pending[parent]
        rec = self.state.get(self.source_id, parent)
        if rec:
            return rec.provider_id
        # Parent unknown (e.g. created between scans): register it first.
        e = stat_entry(self.root, parent)
        if e and e.kind == "folder":
            return self._upsert(e, pending=pending)
        return ROOT_ID

    def _upsert(self, e: Entry, provider_id: str | None = None, scan_id: str | None = None,
                pending: dict[str, str] | None = None) -> str:
        rec = self.state.get(self.source_id, e.rel)
        pid = provider_id or (rec.provider_id if rec else State.new_id())
        parent_pid = self._parent_pid(e.rel, pending)
        self.state.put(self.source_id, Rec(e.rel, pid, e.fs_key, e.kind, e.size, e.mtime_ns))
        self.state.enqueue(self.source_id, to_event(self.root, e, pid, parent_pid, scan_id), scan_id)
        if pending is not None:
            pending[e.rel] = pid
        return pid

    def _delete(self, rec: Rec, reason: str | None = None, scan_id: str | None = None) -> None:
        self.state.delete(self.source_id, rec.rel, subtree=rec.kind == "folder")
        self.state.enqueue(self.source_id, {"op": "delete", "provider_id": rec.provider_id, "reason": reason,
                                            "scan_id": scan_id}, scan_id)

    def _same_object(self, old: Rec, e: Entry, fresh: dict[str, Entry] | None = None) -> bool:
        """Is ``e`` really ``old`` moved/renamed? Inode numbers get reused after a delete, so a matching
        fs id alone is not enough: a renamed file keeps its size and mtime; a moved folder keeps its children."""
        if old.kind != e.kind or not e.fs_key or old.fs_key != e.fs_key:
            return False
        if e.kind == "file":
            return old.size == e.size and old.mtime_ns == e.mtime_ns
        if e.kind == "folder":
            prefix = old.rel + "/"
            old_children = {r.fs_key for rel, r in self.state.all(self.source_id).items()
                            if rel.startswith(prefix) and "/" not in rel[len(prefix):] and r.fs_key}
            if fresh is None:
                fresh = scan(self.root, self.ignorer, under=e.rel)
            new_prefix = e.rel + "/"
            new_children = {x.fs_key for rel, x in fresh.items()
                            if rel.startswith(new_prefix) and "/" not in rel[len(new_prefix):] and x.fs_key}
            if not old_children and not new_children:
                return True
            return bool(old_children & new_children)
        return True

    @staticmethod
    def _changed(rec: Rec, e: Entry) -> bool:
        if rec.kind != e.kind:
            return True
        if e.kind == "folder":
            return False
        return rec.size != e.size or rec.mtime_ns != e.mtime_ns

    # ---------------------------------------------------------------- scans
    def catch_up(self, scan_id: str | None = None) -> dict:
        """Diff a fresh scan against the stored snapshot and queue only the differences."""
        fresh = scan(self.root, self.ignorer)
        known = self.state.all(self.source_id)
        added = {rel: e for rel, e in fresh.items() if rel not in known}
        removed = {rel: r for rel, r in known.items() if rel not in fresh}
        stats = {"scanned": len(fresh), "added": 0, "modified": 0, "moved": 0, "deleted": 0}
        removed_by_key: dict[str, Rec] = {r.fs_key: r for r in removed.values() if r.fs_key}
        with self.state:
            pending: dict[str, str] = {}
            # Moves first (parents before children), so ids are kept.
            for rel in sorted(added, key=_depth):
                e = added[rel]
                old = removed_by_key.get(e.fs_key) if e.fs_key else None
                if old and old.rel in removed and self._same_object(old, e, fresh):
                    if e.kind == "folder":
                        self.state.move_subtree(self.source_id, old.rel, rel)
                    self.state.delete(self.source_id, old.rel)
                    del removed[old.rel]
                    self._upsert(e, provider_id=old.provider_id, pending=pending, scan_id=scan_id)
                    stats["moved"] += 1
                    # Descendants moved along with the folder: they are now known under the new path.
                    if e.kind == "folder":
                        for d_rel in [r for r in list(removed) if r.startswith(old.rel + "/")]:
                            del removed[d_rel]
                    continue
            known = self.state.all(self.source_id)
            for rel in sorted(fresh, key=_depth):
                e = fresh[rel]
                rec = known.get(rel)
                if rec is None:
                    self._upsert(e, pending=pending, scan_id=scan_id)
                    stats["added"] += 1
                elif self._changed(rec, e) or (rec.fs_key != e.fs_key and e.kind == "file"):
                    self._upsert(e, provider_id=rec.provider_id if rec.kind == e.kind else None, pending=pending,
                                 scan_id=scan_id)
                    stats["modified"] += 1
            # Deletes: only top-most removed paths (the server cascades to descendants).
            current = self.state.all(self.source_id)
            gone = [r for rel, r in current.items() if rel not in fresh]
            gone_rels = {r.rel for r in gone}
            for r in sorted(gone, key=lambda r: _depth(r.rel)):
                if _parent(r.rel) in gone_rels:
                    self.state.delete(self.source_id, r.rel, subtree=r.kind == "folder")
                    continue
                self._delete(r, scan_id=scan_id)
                stats["deleted"] += 1
        return stats

    def full_scan(self, scan_id: str) -> dict:
        """Report every entry under ``scan_id``; the server deletes what was not reported."""
        stats = self.catch_up(scan_id)
        fresh = self.state.all(self.source_id)
        with self.state:
            for rel in sorted(fresh, key=_depth):
                e = stat_entry(self.root, rel)
                if e is None or self.ignorer.ignored(rel):
                    continue
                rec = fresh[rel]
                parent_pid = ROOT_ID if not _parent(rel) else fresh[_parent(rel)].provider_id \
                    if _parent(rel) in fresh else self._parent_pid(rel)
                self.state.enqueue(self.source_id, to_event(self.root, e, rec.provider_id, parent_pid, scan_id), scan_id)
        stats["reported"] = len(fresh)
        return stats

    # ---------------------------------------------------------------- live events
    def rel_of(self, path: str) -> str | None:
        path = os.path.abspath(path)
        if path == self.root:
            return ""
        if not path.startswith(self.root + os.sep):
            return None
        return path[len(self.root) + 1:].replace(os.sep, "/")

    def process_path(self, rel: str) -> str | None:
        """Bring one path's state in line with the disk. Returns what happened."""
        if not rel or self.ignorer.ignored(rel):
            return None
        e = stat_entry(self.root, rel)
        rec = self.state.get(self.source_id, rel)
        with self.state:
            if e is None:
                if rec:
                    self._delete(rec)
                    return "deleted"
                return None
            if rec is None:
                # A file that reappears under a new name with a known fs id was moved/renamed.
                old = self.state.by_fs_key(self.source_id, e.fs_key) if e.fs_key else None
                if old and stat_entry(self.root, old.rel) is None and self._same_object(old, e):
                    if e.kind == "folder":
                        self.state.move_subtree(self.source_id, old.rel, rel)
                    self.state.delete(self.source_id, old.rel)
                    self._upsert(e, provider_id=old.provider_id)
                    return "moved"
                self._upsert(e)
                result = "added"
            elif self._changed(rec, e):
                self._upsert(e, provider_id=rec.provider_id if rec.kind == e.kind else None)
                result = "modified"
            else:
                return None
        if e.kind == "folder" and result == "added":
            # A folder copied/moved in from outside: index what is inside it too.
            for sub_rel in sorted(scan(self.root, self.ignorer, under=rel), key=_depth):
                self.process_path(sub_rel)
        return result

    def process_move(self, src_rel: str | None, dest_rel: str | None) -> str | None:
        src_ok = src_rel is not None and src_rel != "" and not self.ignorer.ignored(src_rel)
        dest_ok = dest_rel is not None and dest_rel != "" and not self.ignorer.ignored(dest_rel)
        if not src_ok and dest_ok:
            return self.process_path(dest_rel)          # moved in (or from an ignored name, e.g. temp save)
        if src_ok and not dest_ok:
            return self.process_path(src_rel)           # moved out of the source / to an ignored name
        if not src_ok:
            return None
        rec = self.state.get(self.source_id, src_rel)
        e = stat_entry(self.root, dest_rel)
        if rec is None or e is None:
            self.process_path(src_rel)
            return self.process_path(dest_rel)
        with self.state:
            existing = self.state.get(self.source_id, dest_rel)
            if existing and existing.provider_id != rec.provider_id:
                # Replaced an existing file (atomic save): the destination keeps its id, source goes away.
                self.state.delete(self.source_id, src_rel, subtree=rec.kind == "folder")
                self._upsert(e, provider_id=existing.provider_id)
                if rec.provider_id != existing.provider_id:
                    self.state.enqueue(self.source_id, {"op": "delete", "provider_id": rec.provider_id})
                return "replaced"
            if rec.kind == "folder":
                self.state.move_subtree(self.source_id, src_rel, dest_rel)
            self.state.delete(self.source_id, src_rel)
            self._upsert(e, provider_id=rec.provider_id)
        return "moved"
