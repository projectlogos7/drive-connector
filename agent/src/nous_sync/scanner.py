"""Walk a folder (without following symlinks) and build normalized metadata entries."""

from __future__ import annotations

import mimetypes
import os
import stat as st
from dataclasses import dataclass
from datetime import datetime, timezone

from .ignore import Ignorer


@dataclass
class Entry:
    rel: str
    kind: str  # file | folder | shortcut (symlink)
    size: int | None
    mtime_ns: int
    ctime: float | None
    fs_key: str
    target: str | None = None


def fs_key(s: os.stat_result) -> str | None:
    if not s.st_ino:
        return None
    return f"{s.st_dev}:{s.st_ino}"


def entry_from_stat(rel: str, full: str, s: os.stat_result) -> Entry:
    if st.S_ISLNK(s.st_mode):
        try:
            target = os.readlink(full)
        except OSError:
            target = None
        return Entry(rel, "shortcut", None, s.st_mtime_ns, None, fs_key(s), target)
    kind = "folder" if st.S_ISDIR(s.st_mode) else "file"
    birth = getattr(s, "st_birthtime", None)
    if birth is None and os.name == "nt":
        birth = s.st_ctime
    return Entry(rel, kind, s.st_size if kind == "file" else None, s.st_mtime_ns, birth, fs_key(s))


def stat_entry(root: str, rel: str) -> Entry | None:
    full = os.path.join(root, *rel.split("/"))
    try:
        s = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    if not (st.S_ISDIR(s.st_mode) or st.S_ISREG(s.st_mode) or st.S_ISLNK(s.st_mode)):
        return None  # sockets, fifos, devices
    return entry_from_stat(rel, full, s)


def scan(root: str, ignorer: Ignorer, under: str = "") -> dict[str, Entry]:
    """Return rel-path -> Entry for everything below ``root`` (or below ``root/under``)."""
    out: dict[str, Entry] = {}
    stack = [under]
    while stack:
        rel_dir = stack.pop()
        full_dir = os.path.join(root, *rel_dir.split("/")) if rel_dir else root
        try:
            it = os.scandir(full_dir)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        with it:
            for de in it:
                rel = f"{rel_dir}/{de.name}" if rel_dir else de.name
                if ignorer.ignored(rel):
                    continue
                try:
                    s = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not (st.S_ISDIR(s.st_mode) or st.S_ISREG(s.st_mode) or st.S_ISLNK(s.st_mode)):
                    continue
                e = entry_from_stat(rel, de.path, s)
                out[rel] = e
                if e.kind == "folder":
                    stack.append(rel)
    return out


def _iso_ns(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def to_event(root: str, e: Entry, provider_id: str, parent_provider_id: str, scan_id: str | None = None) -> dict:
    name = e.rel.rsplit("/", 1)[-1]
    mime = None
    if e.kind == "file":
        mime = mimetypes.guess_type(name)[0]
    return {
        "op": "upsert", "provider_id": provider_id, "kind": e.kind, "name": name,
        "parent_provider_id": parent_provider_id, "mime": mime, "size": e.size,
        "modified_at_src": _iso_ns(e.mtime_ns), "created_at_src": _iso(e.ctime),
        "local_path": os.path.join(root, *e.rel.split("/")), "fs_id": e.fs_key,
        "shortcut_target": e.target, "occurred_at": _iso_ns(e.mtime_ns), "scan_id": scan_id,
    }
