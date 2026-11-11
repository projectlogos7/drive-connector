"""Indexer worker: claims jobs from the queue and runs them; also drives the scheduler."""

from __future__ import annotations

import logging
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import queue, scheduler
from .config import get_settings

log = logging.getLogger(__name__)


def dispatch(job: dict) -> None:
    kind = job["kind"]
    if kind in ("apply_events", "finish_scan"):
        from .ingest import apply_job
        apply_job(job)
    elif kind.startswith("drive_"):
        from .connectors.gdrive.sync import run_job
        run_job(job)
    elif kind == "content_process":
        from .content.pipeline import run_job
        run_job(job)
    elif kind == "rebuild_paths":
        from . import db
        from .accounts import get_source
        from .indexer import Indexer
        with db.scoped(job["account_id"]) as conn:
            Indexer(conn, job["account_id"], get_source(conn, job["source_id"])).rebuild_paths()
    else:
        raise ValueError(f"unknown job kind {kind}")


def run_one(job: dict) -> bool:
    try:
        dispatch(job)
    except Exception as e:
        outcome = queue.fail(job, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
        log.warning("job %s (%s) failed -> %s: %s", job["id"], job["kind"], outcome, e)
        return False
    queue.complete(job["id"])
    return True


def drain(max_jobs: int = 10_000, kinds: list[str] | None = None) -> int:
    """Run jobs synchronously until the queue is empty (used by tests and the CLI)."""
    n = 0
    while n < max_jobs:
        jobs = queue.claim(1, kinds)
        if not jobs:
            break
        run_one(jobs[0])
        n += 1
    return n


class Worker:
    def __init__(self, concurrency: int | None = None, with_scheduler: bool = True):
        s = get_settings()
        self.concurrency = concurrency or s.worker_concurrency
        self.with_scheduler = with_scheduler
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix="job")
        self.inflight = threading.Semaphore(self.concurrency)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        last_tick = 0.0
        poll = get_settings().worker_poll_seconds
        log.info("worker started (concurrency=%d)", self.concurrency)
        while not self.stop.is_set():
            if self.with_scheduler and time.monotonic() - last_tick > 10:
                try:
                    did = scheduler.tick()
                    if did and any(v for k, v in did.items() if k != "skipped"):
                        log.info("scheduler: %s", did)
                except Exception:
                    log.exception("scheduler tick failed")
                last_tick = time.monotonic()
            claimed = 0
            while self.inflight.acquire(blocking=False):
                jobs = queue.claim(1)
                if not jobs:
                    self.inflight.release()
                    break
                claimed += 1
                self.pool.submit(self._run, jobs[0])
            if not claimed:
                self.stop.wait(poll)
        self.pool.shutdown(wait=True)

    def _run(self, job: dict) -> None:
        try:
            run_one(job)
        finally:
            self.inflight.release()
