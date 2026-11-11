"""Content job: fetch (Drive) or receive (local agent) text, then store, chunk, embed and summarise.

Only re-extracts when content_hash changes; every extracted version is kept in file_contents and
summaries, while chunks/embeddings are kept for the current version only.
"""

from __future__ import annotations

import logging

from .. import db
from ..accounts import audit
from ..config import get_settings
from .chunk import chunk_text
from .embed import get_embedder, to_pgvector
from .extract import UnsupportedType, extract_text
from .policy import GOOGLE_EXPORTS
from .summarize import summarize

log = logging.getLogger(__name__)


class ContentError(Exception):
    pass


def _fetch_drive_bytes(account_id, source: dict, node: dict) -> tuple[bytes, str | None, str | None]:
    from ..connectors.gdrive.auth import has_content_scope, make_client
    if not has_content_scope(account_id, source["connection_id"]):
        raise ContentError("scope_upgrade_required: reconnect Drive with read-only content access")
    client = make_client(account_id, source["connection_id"])
    if node["mime"] in GOOGLE_EXPORTS:
        export_mime = GOOGLE_EXPORTS[node["mime"]]
        ext = {"text/plain": "txt", "text/csv": "csv"}[export_mime]
        return client.export(node["provider_id"], export_mime), export_mime, ext
    return client.download(node["provider_id"]), node["mime"], node["ext"]


def store_text(account_id, node_id: str, text: str, extractor: str, expected_hash: str | None = None) -> dict:
    """Persist extracted text for a node and (re)build chunks, embeddings and summary."""
    s = get_settings()
    truncated = len(text) > s.content_max_chars
    text = text[: s.content_max_chars]
    chunks = chunk_text(text)
    embedder = get_embedder()
    vectors = embedder.embed(chunks, "document") if chunks else []
    with db.scoped(account_id) as conn:
        node = conn.execute("SELECT * FROM drive_index.nodes WHERE id = %s FOR UPDATE", (node_id,)).fetchone()
        if not node:
            raise ContentError("node not found")
        if expected_hash and node["content_hash"] and expected_hash != node["content_hash"]:
            raise ContentError("stale content: file changed again since this text was extracted")
        version = node["content_version"] + 1
        conn.execute(
            """INSERT INTO drive_index.file_contents (node_id, content_version, account_id, source_id,
                 content_hash, mime, extractor, text, char_count, truncated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (node_id, version, str(account_id), node["source_id"], node["content_hash"], node["mime"], extractor,
             text, len(text), truncated),
        )
        conn.execute("DELETE FROM drive_index.chunks WHERE node_id = %s", (node_id,))
        for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
            cid = conn.execute(
                """INSERT INTO drive_index.chunks (node_id, content_version, account_id, source_id, ord, text)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (node_id, version, str(account_id), node["source_id"], i, chunk),
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO drive_index.embeddings (chunk_id, account_id, node_id, model, embedding)
                   VALUES (%s,%s,%s,%s,%s::vector)""",
                (cid, str(account_id), node_id, embedder.name, to_pgvector(vec)),
            )
        conn.execute(
            """UPDATE drive_index.nodes SET content_status = 'extracted', content_version = %s, content_error = NULL
               WHERE id = %s""",
            (version, node_id),
        )
    summary, model = summarize(text, node["name"], node["path"]) if text.strip() else ("(empty file)", "none")
    with db.scoped(account_id) as conn:
        conn.execute(
            """INSERT INTO drive_index.summaries (node_id, content_version, account_id, summary, model)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT (node_id, content_version) DO UPDATE
               SET summary = EXCLUDED.summary, model = EXCLUDED.model""",
            (node_id, version, str(account_id), summary, model),
        )
    return {"node_id": node_id, "content_version": version, "chunks": len(chunks), "summary_model": model}


def mark_failed(account_id, node_id: str, error: str, status: str = "failed") -> None:
    with db.scoped(account_id) as conn:
        conn.execute(
            "UPDATE drive_index.nodes SET content_status = %s, content_error = %s WHERE id = %s",
            (status, error[:1000], node_id),
        )


def run_job(job: dict) -> None:
    """content_process job (Drive sources). Local sources upload text through the agent API instead."""
    account_id, node_id = job["account_id"], job["payload"]["node_id"]
    with db.scoped(account_id) as conn:
        node = conn.execute("SELECT * FROM drive_index.nodes WHERE id = %s", (node_id,)).fetchone()
        source = conn.execute("SELECT * FROM drive_index.sources WHERE id = %s", (job["source_id"],)).fetchone()
    if not node or node["content_status"] != "queued" or node["status"] != "active":
        return
    if source["type"] != "gdrive":
        return
    try:
        data, mime, ext = _fetch_drive_bytes(account_id, source, node)
        text, extractor = extract_text(data, mime, ext)
    except UnsupportedType as e:
        mark_failed(account_id, node_id, str(e), status="skipped")
        return
    except ContentError as e:
        mark_failed(account_id, node_id, str(e))
        return
    store_text(account_id, node_id, text, extractor, expected_hash=node["content_hash"])


def request_backfill(conn, account_id, source_id, actor: str) -> int:
    """Queue every eligible file of a source (after content is switched on for it)."""
    from .policy import should_extract
    src = conn.execute("SELECT * FROM drive_index.sources WHERE id = %s", (str(source_id),)).fetchone()
    rows = conn.execute(
        """SELECT * FROM drive_index.nodes WHERE source_id = %s AND kind = 'file' AND status = 'active'
           AND content_status IN ('not_requested','failed')""",
        (str(source_id),),
    ).fetchall()
    from .. import queue
    n = 0
    for node in rows:
        if should_extract(src, node):
            conn.execute("UPDATE drive_index.nodes SET content_status = 'queued' WHERE id = %s", (node["id"],))
            if src["type"] == "gdrive":
                queue.enqueue(conn, account_id, "content_process", source_id, {"node_id": str(node["id"])},
                              dedupe_key=f"content:{node['id']}")
            n += 1
    audit(conn, account_id, actor, "content.backfill", str(source_id), {"queued": n})
    return n
