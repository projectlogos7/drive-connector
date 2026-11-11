"""Database access.

Two kinds of connections:

* ``scoped(account_id)`` — every request/job that touches account data. The
  transaction switches to the restricted ``drive_index_app`` role and sets
  ``app.account_id``; Postgres row-level security then guarantees only that
  account's rows are visible or writable.
* ``system()`` — internal cross-account bookkeeping only (claiming jobs,
  resolving an API key or webhook channel to its account, the scheduler).
  Never used to serve data to a caller.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from importlib import resources
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=20,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def system() -> Iterator[psycopg.Connection]:
    """Owner-role transaction (bypasses RLS). Internal use only."""
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def scoped(account_id: UUID | str) -> Iterator[psycopg.Connection]:
    """Transaction restricted to one account by row-level security."""
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE drive_index_app")
            conn.execute("SELECT set_config('app.account_id', %s, true)", (str(account_id),))
            yield conn


def migrate(conn_str: str | None = None) -> list[str]:
    """Apply pending SQL migrations in order. Returns the names applied."""
    applied: list[str] = []
    url = conn_str or get_settings().database_url
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS drive_index")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS drive_index.schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM drive_index.schema_migrations")}
        files = sorted(
            p for p in resources.files("nous_drive.migrations").iterdir() if p.name.endswith(".sql")
        )
        for f in files:
            if f.name in done:
                continue
            log.info("applying migration %s", f.name)
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("INSERT INTO drive_index.schema_migrations(name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied


import atexit as _atexit

_atexit.register(close_pool)
