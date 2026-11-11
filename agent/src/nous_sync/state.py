"""Local state (SQLite): last known snapshot per source and a durable outbox of events.

A file's snapshot row and its outgoing event are written in one transaction, so the snapshot never
claims something the server will not eventually receive, even across crashes or offline periods.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Rec:
    rel: str
    provider_id: str
    fs_key: str | None
    kind: str
    size: int | None
    mtime_ns: int | None


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.RLock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
              source_id TEXT NOT NULL, rel TEXT NOT NULL, provider_id TEXT NOT NULL, fs_key TEXT,
              kind TEXT NOT NULL, size INTEGER, mtime_ns INTEGER, PRIMARY KEY (source_id, rel));
            CREATE INDEX IF NOT EXISTS files_fskey ON files(source_id, fs_key);
            CREATE INDEX IF NOT EXISTS files_pid ON files(source_id, provider_id);
            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, scan_id TEXT, event TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """
        )

    # ---------------------------------------------------------------- transactions
    def __enter__(self):
        self.lock.acquire()
        self.db.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.lock.release()

    # ---------------------------------------------------------------- snapshot
    def all(self, source_id: str) -> dict[str, Rec]:
        with self.lock:
            rows = self.db.execute(
                "SELECT rel, provider_id, fs_key, kind, size, mtime_ns FROM files WHERE source_id = ?", (source_id,)
            ).fetchall()
        return {r[0]: Rec(*r) for r in rows}

    def get(self, source_id: str, rel: str) -> Rec | None:
        with self.lock:
            r = self.db.execute(
                "SELECT rel, provider_id, fs_key, kind, size, mtime_ns FROM files WHERE source_id = ? AND rel = ?",
                (source_id, rel),
            ).fetchone()
        return Rec(*r) if r else None

    def by_fs_key(self, source_id: str, fs_key: str) -> Rec | None:
        with self.lock:
            r = self.db.execute(
                "SELECT rel, provider_id, fs_key, kind, size, mtime_ns FROM files WHERE source_id = ? AND fs_key = ?",
                (source_id, fs_key),
            ).fetchone()
        return Rec(*r) if r else None

    def put(self, source_id: str, rec: Rec) -> None:
        self.db.execute(
            """INSERT INTO files (source_id, rel, provider_id, fs_key, kind, size, mtime_ns) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (source_id, rel) DO UPDATE SET provider_id = excluded.provider_id, fs_key = excluded.fs_key,
               kind = excluded.kind, size = excluded.size, mtime_ns = excluded.mtime_ns""",
            (source_id, rec.rel, rec.provider_id, rec.fs_key, rec.kind, rec.size, rec.mtime_ns),
        )

    def delete(self, source_id: str, rel: str, subtree: bool = False) -> None:
        self.db.execute("DELETE FROM files WHERE source_id = ? AND rel = ?", (source_id, rel))
        if subtree:
            prefix = rel + "/"
            self.db.execute("DELETE FROM files WHERE source_id = ? AND substr(rel, 1, ?) = ?",
                            (source_id, len(prefix), prefix))

    def move_subtree(self, source_id: str, old: str, new: str) -> None:
        old_p, new_p = old + "/", new + "/"
        self.db.execute(
            "UPDATE files SET rel = ? || substr(rel, ?) WHERE source_id = ? AND substr(rel, 1, ?) = ?",
            (new_p, len(old_p) + 1, source_id, len(old_p), old_p),
        )

    def drop_source(self, source_id: str) -> None:
        with self:
            self.db.execute("DELETE FROM files WHERE source_id = ?", (source_id,))
            self.db.execute("DELETE FROM outbox WHERE source_id = ?", (source_id,))

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    # ---------------------------------------------------------------- outbox
    def enqueue(self, source_id: str, event: dict, scan_id: str | None = None) -> None:
        self.db.execute("INSERT INTO outbox (source_id, scan_id, event) VALUES (?,?,?)",
                        (source_id, scan_id, json.dumps(event)))

    def peek(self, source_id: str, limit: int) -> list[tuple[int, str | None, dict]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT id, scan_id, event FROM outbox WHERE source_id = ? ORDER BY id LIMIT ?", (source_id, limit)
            ).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def ack(self, ids: list[int]) -> None:
        with self.lock:
            self.db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])

    def pending(self, source_id: str | None = None) -> int:
        with self.lock:
            if source_id:
                return self.db.execute("SELECT count(*) FROM outbox WHERE source_id = ?", (source_id,)).fetchone()[0]
            return self.db.execute("SELECT count(*) FROM outbox").fetchone()[0]

    def meta(self, k: str, v: str | None = None) -> str | None:
        with self.lock:
            if v is None:
                r = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
                return r[0] if r else None
            self.db.execute("INSERT INTO meta (k, v) VALUES (?,?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, v))
            return v
