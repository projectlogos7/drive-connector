"""Live progress from workers: which stage, how far, and the exact file being processed.

Writes are throttled (a few per second per source and stage) and go through their own short
transaction, so the web app sees movement inside a long batch, not only when the batch commits.
"""

from __future__ import annotations

import time

from psycopg.types.json import Jsonb

from . import db
from .util import utcnow

_last: dict[tuple[str, str], float] = {}


def report(account_id, source_id, stage: str, *, done: int | None = None, total: int | None = None,
           current: dict | None = None, force: bool = False, interval: float = 0.4, **extra) -> None:
    key = (str(source_id), stage)
    now = time.monotonic()
    if not force and now - _last.get(key, 0) < interval:
        return
    _last[key] = now
    payload: dict = {"updated_at": utcnow().isoformat(), **extra}
    if done is not None:
        payload["done"] = done
    if total is not None:
        payload["total"] = total
    if current is not None:
        payload["current"] = current
    try:
        with db.system() as conn:
            conn.execute(
                """INSERT INTO drive_index.live_progress (source_id, account_id, data, updated_at)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (source_id) DO UPDATE SET
                     data = drive_index.live_progress.data || EXCLUDED.data, updated_at = now()""",
                (str(source_id), str(account_id), Jsonb({stage: payload})),
            )
    except Exception:
        pass  # progress is best effort; never fail indexing because of it


def clear(account_id, source_id, stage: str) -> None:
    report(account_id, source_id, stage, force=True, current={}, active=False)


def file_info(name: str | None, size: int | None = None, path: str | None = None, kind: str | None = None) -> dict:
    return {"name": name, "size": size, "path": path, "kind": kind}
