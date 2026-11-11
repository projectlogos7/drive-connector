"""Native file events (FSEvents / ReadDirectoryChangesW / inotify via watchdog) with debouncing.

A path is processed only after it has been quiet for ``settle`` seconds AND its size/mtime are
unchanged across two checks, so a 40 GB render or a copy in progress is reported once, when complete.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)


class Debouncer:
    def __init__(self, settle: float, on_path: Callable[[str], None], on_move: Callable[[str, str], None]):
        self.settle = settle
        self.on_path = on_path
        self.on_move = on_move
        self.pending: dict[str, float] = {}
        self.sigs: dict[str, tuple | None] = {}
        self.moves: list[tuple[float, str, str]] = []
        self.cv = threading.Condition()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="debouncer", daemon=True)
        self.thread.start()

    def touch(self, path: str) -> None:
        with self.cv:
            self.pending[path] = time.monotonic()
            self.cv.notify()

    def move(self, src: str, dest: str) -> None:
        with self.cv:
            self.moves.append((time.monotonic(), src, dest))
            self.pending.pop(src, None)
            self.cv.notify()

    @staticmethod
    def _sig(path: str) -> tuple | None:
        try:
            s = os.lstat(path)
            return (s.st_size, s.st_mtime_ns)
        except OSError:
            return None

    def _loop(self) -> None:
        while not self.stop.is_set():
            with self.cv:
                if not self.pending and not self.moves:
                    self.cv.wait(timeout=5)
                    continue
                now = time.monotonic()
                moves = [m for m in self.moves if now - m[0] >= min(0.5, self.settle)]
                self.moves = [m for m in self.moves if m not in moves]
                ready = [p for p, t in self.pending.items() if now - t >= self.settle]
            for _, src, dest in moves:
                self._safe(self.on_move, src, dest)
            for p in ready:
                sig = self._sig(p)
                with self.cv:
                    prev = self.sigs.get(p, "unset")
                    if prev == "unset" or prev != sig:
                        # Still changing (or first look): check again after another quiet period.
                        self.sigs[p] = sig
                        self.pending[p] = time.monotonic() - self.settle + 1.0
                        continue
                    self.pending.pop(p, None)
                    self.sigs.pop(p, None)
                self._safe(self.on_path, p)
            time.sleep(0.25)

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("processing %s failed", args)

    def close(self) -> None:
        self.stop.set()
        with self.cv:
            self.cv.notify()


class _Handler(FileSystemEventHandler):
    def __init__(self, deb: Debouncer):
        self.deb = deb

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        src = os.fsdecode(event.src_path)
        if event.event_type == "moved":
            self.deb.move(src, os.fsdecode(event.dest_path))
        else:
            self.deb.touch(src)
            if event.event_type == "closed":
                self.deb.touch(src)


class FolderWatcher:
    def __init__(self, root: str, settle: float, on_path: Callable[[str], None], on_move: Callable[[str, str], None]):
        self.root = root
        self.debouncer = Debouncer(settle, on_path, on_move)
        self.observer = Observer()
        self.observer.schedule(_Handler(self.debouncer), root, recursive=True)

    def start(self) -> None:
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()
        self.debouncer.close()
        try:
            self.observer.join(timeout=5)
        except RuntimeError:
            pass
