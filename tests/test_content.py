import io

import pytest

from nous_drive import db, queue
from nous_drive.config import reset_settings
from nous_drive.accounts import update_source_settings
from nous_drive.content.chunk import chunk_text
from nous_drive.content.embed import LocalHashEmbedder
from nous_drive.content.extract import UnsupportedType, extract_text
from nous_drive.content.pipeline import store_text
from nous_drive.content.search import search_content
from nous_drive.mcp import tools as T
from nous_drive.worker import drain

from .conftest import make_source, nodes
from .fake_drive import FakeDrive, install


def test_extractors():
    import docx
    d = docx.Document()
    d.add_paragraph("Partition of Bengal, 1905")
    buf = io.BytesIO()
    d.save(buf)
    assert "Bengal" in extract_text(buf.getvalue(), None, "docx")[0]
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["episode", "status"])
    wb.active.append(["Madarasa 2", "editing"])
    b2 = io.BytesIO()
    wb.save(b2)
    assert "Madarasa 2, editing" in extract_text(b2.getvalue(), None, "xlsx")[0]
    assert extract_text(b"<p>Hello<br>world</p>", "text/html", "html")[0].split() == ["Hello", "world"]
    with pytest.raises(UnsupportedType):
        extract_text(b"\x00", "video/quicktime", "mov")


def test_chunking_and_embedder():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 100 for i in range(40))
    chunks = chunk_text(text, max_chars=1500, overlap=100)
    assert len(chunks) > 5 and all(len(c) <= 1500 for c in chunks)
    e = LocalHashEmbedder()
    a, b, c = e.embed(["partition of bengal", "bengal partition history", "recipe for biryani"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert dot(a, b) > dot(a, c)


@pytest.fixture
def content_on(monkeypatch):
    monkeypatch.setenv("NOUS_DRIVE_CONTENT_ENABLED", "1")
    reset_settings()


def test_drive_content_pipeline_end_to_end(account, monkeypatch, content_on):
    fake = FakeDrive()
    install(monkeypatch, fake)
    fake.add("PROJ", "Projects", fake.root_id, folder=True, record=False)
    fake.add("g", "Script", "PROJ", size=None, mimeType="application/vnd.google-apps.document", record=False,
             _content=b"The Partition of Bengal in 1905 reshaped the province. Curzon announced it.")
    fake.add("t", "notes.txt", "PROJ", record=False, _content=b"Shooting schedule for Madarasa episode two.")
    fake.add("m", "rush.mov", "PROJ", record=False)
    with db.scoped(account["id"]) as conn:
        cid = conn.execute("INSERT INTO drive_index.drive_connections (account_id) VALUES (%s) RETURNING id",
                           (account["id"],)).fetchone()["id"]
    src = make_source(account["id"], root_ref="PROJ", connection_id=cid, settings={"content": {"enabled": True}})
    with db.scoped(account["id"]) as conn:
        queue.enqueue(conn, account["id"], "drive_initial_crawl", src["id"])
    drain()
    n = nodes(account["id"])
    assert n["g"]["content_status"] == "extracted" and n["g"]["content_version"] == 1
    assert n["t"]["content_status"] == "extracted"
    assert n["m"]["content_status"] == "not_requested"
    ctx = T.Ctx(account_id=account["id"])
    hits = T.search_files(ctx, "Curzon Bengal", mode="content")
    assert hits["results"][0]["name"] == "Script" and "Curzon" in hits["results"][0]["snippet"]
    d = T.describe_file(ctx, node_id=str(n["g"]["id"]))
    assert "Partition of Bengal" in d["content_summary"]
    # Unchanged files are not re-extracted; a change makes a new version.
    fake.update("t", _content=b"Revised schedule: Madarasa episode two moves to December.")
    with db.scoped(account["id"]) as conn:
        queue.enqueue(conn, account["id"], "drive_pull_changes", src["id"])
    drain()
    n = nodes(account["id"])
    assert n["t"]["content_version"] == 2 and n["g"]["content_version"] == 1
    with db.scoped(account["id"]) as conn:
        versions = conn.execute("SELECT count(*) AS n FROM drive_index.file_contents WHERE node_id = %s",
                                (n["t"]["id"],)).fetchone()["n"]
        assert versions == 2
        hits = search_content(conn, "December", mode="keyword")
    assert hits and hits[0]["node_id"] == str(n["t"]["id"])


def test_store_text_rejects_stale(account, content_on):
    src = make_source(account["id"])
    from .conftest import apply
    apply(account["id"], src, [dict(provider_id="x", name="x.txt", parent_provider_id="ROOT", size=1)])
    nid = str(nodes(account["id"])["x"]["id"])
    from nous_drive.content.pipeline import ContentError
    with pytest.raises(ContentError):
        store_text(account["id"], nid, "text", "t", expected_hash="something-else")


def test_phase2_on_by_default_with_queued_embedding_and_no_llm(account, monkeypatch):
    from nous_drive.config import get_settings
    from nous_drive.worker import run_one
    monkeypatch.delenv("NOUS_DRIVE_CONTENT_ENABLED", raising=False)
    monkeypatch.delenv("NOUS_DRIVE_SUMMARY_PROVIDER", raising=False)
    reset_settings()
    s = get_settings()
    assert s.content_enabled and s.summary_provider == "extractive"
    fake = FakeDrive()
    install(monkeypatch, fake)
    fake.add("PROJ", "Projects", fake.root_id, folder=True, record=False)
    fake.add("t", "notes.txt", "PROJ", record=False, _content=b"Shooting schedule for Madarasa episode two.")
    with db.scoped(account["id"]) as conn:
        cid = conn.execute("INSERT INTO drive_index.drive_connections (account_id) VALUES (%s) RETURNING id",
                           (account["id"],)).fetchone()["id"]
    src = make_source(account["id"], root_ref="PROJ", connection_id=cid)  # no content settings given
    with db.scoped(account["id"]) as conn:
        queue.enqueue(conn, account["id"], "drive_initial_crawl", src["id"])
    order = []
    while True:
        jobs = queue.claim(1)
        if not jobs:
            break
        order.append(jobs[0]["kind"])
        run_one(jobs[0])
    assert order == ["drive_initial_crawl", "content_process", "content_embed"]
    n = nodes(account["id"])["t"]
    with db.scoped(account["id"]) as conn:
        emb = conn.execute("SELECT DISTINCT model FROM drive_index.embeddings").fetchall()
        summ = conn.execute("SELECT model FROM drive_index.summaries WHERE node_id = %s", (n["id"],)).fetchone()
    assert n["content_status"] == "extracted"
    assert emb and summ["model"] == "extractive"   # no LLM used
    assert T.search_files(T.Ctx(account_id=account["id"]), "schedule Madarasa", mode="content")["results"]
