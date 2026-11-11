from datetime import datetime, timezone

import pytest

from nous_drive import db
from nous_drive.mcp import tools as T
from nous_drive.models import NodeEvent

from .conftest import apply, make_source

GB = 1024 ** 3


@pytest.fixture
def indexed(account):
    src = make_source(account["id"], name="Projects")
    apply(account["id"], src, [
        dict(provider_id="mad", name="Madarasa", parent_provider_id="ROOT", kind="folder"),
        dict(provider_id="ep2", name="Episode 2", parent_provider_id="mad", kind="folder"),
        dict(provider_id="ep1", name="Episode 1", parent_provider_id="mad", kind="folder"),
        dict(provider_id="big", name="A001.mov", parent_provider_id="ep2", size=3 * GB, mime="video/quicktime",
             modified_at_src=datetime(2026, 11, 1, tzinfo=timezone.utc)),
        dict(provider_id="small", name="B002.mov", parent_provider_id="ep2", size=1 * GB),
        dict(provider_id="v3", name="Unmaking_Citizens_v3.docx", parent_provider_id="ep2", size=2000,
             modified_at_src=datetime(2026, 10, 1, tzinfo=timezone.utc)),
        dict(provider_id="v4", name="Unmaking_Citizens_v4.docx", parent_provider_id="ep2", size=2100, owner="ali@nous",
             modified_at_src=datetime(2026, 11, 10, tzinfo=timezone.utc)),
    ])
    return T.Ctx(account_id=account["id"], session_id="s1"), src


def test_list_sources_and_status(indexed):
    ctx, src = indexed
    out = T.list_sources(ctx)
    assert out["sources"][0]["files"] == 4 and out["sources"][0]["folders"] == 3
    st = T.get_sync_status(ctx, "Projects")
    assert st["sources"][0]["queue"]["dead_letter"] == 0 and "push_channel" in st["sources"][0]


def test_get_tree_depth_and_text(indexed):
    ctx, _ = indexed
    t = T.get_tree(ctx, path="/Madarasa", source="Projects", depth=2, include_files=True)
    assert "Episode 2/  (4 files, 0 folders)" in t["text"]
    assert "A001.mov" in t["text"]
    t1 = T.get_tree(ctx, path="Projects/Madarasa", depth=1)  # source name as first segment
    assert [c["name"] for c in t1["tree"]["children"]] == ["Episode 1", "Episode 2"]


def test_list_files_filters_and_paging(indexed):
    ctx, _ = indexed
    out = T.list_files(ctx, path="/Madarasa", source="Projects", recursive=True, ext="mov", min_size=2 * GB)
    assert [f["name"] for f in out["files"]] == ["A001.mov"]
    page1 = T.list_files(ctx, path="/Madarasa/Episode 2", limit=2, sort="name")
    page2 = T.list_files(ctx, path="/Madarasa/Episode 2", limit=2, cursor=page1["next_cursor"])
    assert page1["total"] == 4 and len(page2["files"]) == 2 and page2["next_cursor"] is None
    assert T.list_files(ctx, source="Projects", type="video")["total"] == 2


def test_search_and_get_file_and_describe(indexed):
    ctx, _ = indexed
    r = T.search_files(ctx, "unmaking v4")
    assert r["results"][0]["name"] == "Unmaking_Citizens_v4.docx"
    f = T.get_file(ctx, path="/Madarasa/Episode 2/Unmaking_Citizens_v4.docx", source="Projects")
    assert f["history"][0]["type"] == "added" and f["owner"] == "ali@nous"
    d = T.describe_file(ctx, node_id=f["id"])
    assert "Unmaking_Citizens_v3.docx" in d["description"]
    assert "document file" in d["description"]
    with pytest.raises(T.ToolError):
        T.get_file(ctx, path="/nope", source="Projects")


def test_whats_new_checkpoint_per_session(indexed, account):
    ctx, src = indexed
    first = T.whats_new(ctx)
    assert first["total"] == 7 and first["checkpoint_saved"]
    assert T.whats_new(ctx)["total"] == 0          # nothing new in between
    apply(account["id"], src, [dict(provider_id="n1", name="new.wav", parent_provider_id="ep1")])
    again = T.whats_new(ctx)
    assert again["total"] == 1 and again["groups"][0]["folder"] == "/Madarasa/Episode 1"
    other = T.whats_new(T.Ctx(account_id=account["id"], session_id="s2"))
    assert other["total"] == 8                     # a different session has its own checkpoint
    assert T.whats_new(ctx, since="1h", peek=True)["total"] == 8


def test_list_changes_filters(indexed, account):
    ctx, src = indexed
    apply(account["id"], src, [dict(provider_id="small", name="B002.mov", parent_provider_id="ep1", size=GB)])
    apply(account["id"], src, [NodeEvent(op="delete", provider_id="v3")])
    ch = T.list_changes(ctx, types=["moved", "deleted"])
    assert {c["type"] for c in ch["changes"]} == {"moved", "deleted"}
    moved = [c for c in ch["changes"] if c["type"] == "moved"][0]
    assert moved["from"] == "/Madarasa/Episode 2/B002.mov"
    assert T.list_changes(ctx, folder="/Madarasa/Episode 1")["total"] >= 1


def test_control_tools(indexed):
    ctx, src = indexed
    assert T.pause_source(ctx, "Projects")["status"] == "paused"
    with db.scoped(ctx.account_id) as conn:
        conn.execute("UPDATE drive_index.sync_cursors SET drive_page_token = '5'")
    assert T.resume_source(ctx, "Projects")["status"] == "live"
    assert "reconcile" in T.resync_source(ctx, "Projects")["result"]
    assert T.get_connect_link(ctx, "device")["url"].endswith("/app#devices")


def test_isolation_in_tools(indexed, other_account):
    ctx2 = T.Ctx(account_id=other_account["id"])
    assert T.list_sources(ctx2)["count"] == 0
    assert T.search_files(ctx2, "mov")["total"] == 0
    with pytest.raises(T.ToolError):
        T.get_tree(ctx2, path="/Madarasa", source="Projects")
