"""Test fixtures. Needs a Postgres with pgvector; set NOUS_DRIVE_TEST_ADMIN_URL to a superuser URL
(default postgresql://postgres@localhost/postgres). A throwaway database and owner role are created."""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("NOUS_DRIVE_DRIVE_MAX_RPS", "0")
os.environ.setdefault("NOUS_DRIVE_SECRET_KEY", "test-secret")
os.environ.setdefault("NOUS_DRIVE_PUBLIC_BASE_URL", "https://drive-index.test")
os.environ.setdefault("NOUS_DRIVE_GOOGLE_CLIENT_ID", "cid")
os.environ.setdefault("NOUS_DRIVE_GOOGLE_CLIENT_SECRET", "csecret")
os.environ["NOUS_DRIVE_SUMMARY_PROVIDER"] = "extractive"
os.environ["NOUS_DRIVE_EMBEDDING_PROVIDER"] = "hash"  # no model download in tests
os.environ.pop("ANTHROPIC_API_KEY", None)

import psycopg
import pytest

ADMIN_URL = os.environ.get("NOUS_DRIVE_TEST_ADMIN_URL", "postgresql://postgres:postgres@localhost/postgres")
DB_NAME = "nous_drive_pytest"
OWNER = "nd_pytest_owner"


def _admin():
    try:
        return psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not available: {e}")


@pytest.fixture(scope="session", autouse=True)
def database():
    with _admin() as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
        c.execute(f"DROP ROLE IF EXISTS {OWNER}")
        c.execute(f"CREATE ROLE {OWNER} LOGIN CREATEROLE PASSWORD 'pw'")
        c.execute(f"CREATE DATABASE {DB_NAME} OWNER {OWNER}")
        # drive_index_app is cluster-wide; it may have been created by another owner earlier.
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'drive_index_app') "
                  "THEN CREATE ROLE drive_index_app NOLOGIN; END IF; END $$")
        c.execute(f"GRANT drive_index_app TO {OWNER} WITH ADMIN OPTION")
    base = ADMIN_URL.rsplit("/", 1)[0].split("@")[-1]
    with psycopg.connect(ADMIN_URL.rsplit("/", 1)[0] + f"/{DB_NAME}", autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")  # needs superuser locally; Neon allows the owner
    url = f"postgresql://{OWNER}:pw@{base}/{DB_NAME}"
    os.environ["NOUS_DRIVE_DATABASE_URL"] = url
    from nous_drive import config, db
    config.reset_settings()
    db.close_pool()
    db.migrate()
    yield url
    db.close_pool()


TABLES = ["embeddings", "chunks", "summaries", "file_contents", "notifications", "audit_log", "mcp_checkpoints",
          "index_jobs", "change_events", "sync_cursors", "nodes", "credentials", "sources", "drive_connections",
          "pairing_codes", "devices", "api_keys", "accounts", "scheduler_state"]


@pytest.fixture(autouse=True)
def clean(database):
    from nous_drive import config
    config.reset_settings()
    from nous_drive import db
    with db.system() as conn:
        conn.execute("TRUNCATE " + ", ".join(f"drive_index.{t}" for t in TABLES) + " CASCADE")
    yield


@pytest.fixture
def account():
    from nous_drive.accounts import create_account
    acct, key = create_account("Nous", "nous@example.org")
    return {"id": str(acct["id"]), "admin_key": key}


@pytest.fixture
def other_account():
    from nous_drive.accounts import create_account
    acct, key = create_account("Other")
    return {"id": str(acct["id"]), "admin_key": key}


def make_source(account_id, **kw):
    from nous_drive import db
    from nous_drive.accounts import create_source
    params = dict(type_="gdrive", root_kind="folder", root_ref="ROOT", name="Projects")
    params.update(kw)
    with db.scoped(account_id) as conn:
        return create_source(conn, account_id, **params)


def apply(account_id, source, events, device_id=None):
    from nous_drive import db
    from nous_drive.indexer import Indexer
    from nous_drive.models import NodeEvent
    with db.scoped(account_id) as conn:
        ix = Indexer(conn, account_id, source, device_id=device_id)
        ix.ensure_root(source["name"], source["root_ref"] if source["type"] == "gdrive" else "__root__")
        ix.apply([e if isinstance(e, NodeEvent) else NodeEvent(**e) for e in events])
        return ix.stats


def nodes(account_id, source_id=None):
    from nous_drive import db
    with db.scoped(account_id) as conn:
        q = "SELECT * FROM drive_index.nodes"
        rows = conn.execute(q + (" WHERE source_id = %s" if source_id else "") + " ORDER BY path",
                            ((str(source_id),) if source_id else ())).fetchall()
    return {r["provider_id"]: r for r in rows}


def events(account_id):
    from nous_drive import db
    with db.scoped(account_id) as conn:
        return conn.execute("SELECT * FROM drive_index.change_events ORDER BY id").fetchall()


def uid() -> str:
    return uuid.uuid4().hex
