import pytest

from nous_drive import db, queue
from nous_drive.config import reset_settings
from nous_drive.connectors.gdrive.mapping import SHARED_WITH_ME
from nous_drive.connectors.gdrive.sync import DriveSync, handle_notification
from nous_drive.crypto import hash_key
from nous_drive.worker import drain

from .conftest import events, make_source, nodes
from .fake_drive import FakeDrive, install


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    install(monkeypatch, fake)
    monkeypatch.setenv("NOUS_DRIVE_DRIVE_PAGE_SIZE", "3")  # force paging
    reset_settings()
    return fake


def _conn_id(account_id):
    with db.scoped(account_id) as conn:
        return conn.execute("INSERT INTO drive_index.drive_connections (account_id, scopes) VALUES (%s, %s) RETURNING id",
                            (account_id, ["https://www.googleapis.com/auth/drive.metadata.readonly"])).fetchone()["id"]


def folder_source(account, drive, root="PROJ"):
    drive.add(root, "Projects", drive.root_id, folder=True, record=False)
    return make_source(account["id"], root_ref=root, name="Projects", connection_id=_conn_id(account["id"]))


def build_tree(drive):
    drive.add("mad", "Madarasa", "PROJ", folder=True, record=False)
    drive.add("ep2", "Episode 2", "mad", folder=True, record=False)
    for i in range(5):
        drive.add(f"f{i}", f"clip_{i}.mov", "ep2", size=1000 + i, record=False)
    drive.add("doc", "Unmaking_Citizens_v4.docx", "mad", record=False)
    drive.add("gdoc", "Notes", "mad", size=None, mimeType="application/vnd.google-apps.document", record=False)
    drive.add("sc", "shortcut to clip", "mad", size=None, mimeType="application/vnd.google-apps.shortcut",
              shortcutDetails={"targetId": "f1", "targetMimeType": "video/quicktime"}, record=False)
    drive.add("outside", "elsewhere.txt", drive.root_id, record=False)


def test_initial_crawl_folder_root(account, drive):
    src = folder_source(account, drive)
    build_tree(drive)
    p = DriveSync(account["id"], src["id"]).crawl()
    n = nodes(account["id"])
    assert "outside" not in n
    assert n["f3"]["path"] == "/Madarasa/Episode 2/clip_3.mov"
    assert n["gdoc"]["size"] is None and n["gdoc"]["category"] == "document"
    assert n["sc"]["kind"] == "shortcut" and n["sc"]["shortcut_target"] == "f1"
    assert p["files"] == 8 and p["folders"] == 2 and p["state"] == "done"
    with db.scoped(account["id"]) as conn:
        s = conn.execute("SELECT status FROM drive_index.sources").fetchone()
        c = conn.execute("SELECT * FROM drive_index.sync_cursors").fetchone()
    assert s["status"] == "live"
    assert c["drive_page_token"] and c["channel_id"]  # push channel opened (https base url in tests)


def test_changes_during_crawl_are_not_lost(account, drive, monkeypatch):
    src = folder_source(account, drive)
    build_tree(drive)
    sync = DriveSync(account["id"], src["id"])
    orig = sync._crawl_bfs

    def crawl_and_change(progress):
        orig(progress)
        drive.add("late", "late.wav", "ep2")       # added after it was listed
        drive.update("f0", name="clip_0_final.mov")
    monkeypatch.setattr(sync, "_crawl_bfs", crawl_and_change)
    sync.crawl()
    n = nodes(account["id"])
    assert n["late"]["status"] == "active"
    assert n["f0"]["name"] == "clip_0_final.mov"


def test_pull_changes_add_rename_trash_delete_move_out_and_in(account, drive):
    src = folder_source(account, drive)
    build_tree(drive)
    DriveSync(account["id"], src["id"]).crawl()
    before = len(events(account["id"]))
    drive.add("new", "Bengal Partition.pdf", "ep2")
    drive.update("doc", name="Unmaking_Citizens_v5.docx")
    drive.update("f1", trashed=True)
    drive.delete("f2")
    drive.update("f3", parent=drive.root_id)          # moved out of the watched folder
    # A folder with content moved in from outside: its children are not in the feed.
    drive.add("ext", "Research", drive.root_id, folder=True, record=False)
    drive.add("ext1", "paper.pdf", "ext", record=False)
    drive.update("ext", parent="mad")
    DriveSync(account["id"], src["id"]).pull()
    n = nodes(account["id"])
    types = [e["type"] for e in events(account["id"])[before:]]
    assert n["new"]["path"] == "/Madarasa/Episode 2/Bengal Partition.pdf"
    assert n["doc"]["name"] == "Unmaking_Citizens_v5.docx" and "renamed" in types
    assert n["f1"]["status"] == "trashed"
    assert n["f2"]["status"] == "deleted"
    assert n["f3"]["status"] == "removed" and "moved out" in n["f3"]["status_reason"]
    assert n["ext1"]["path"] == "/Madarasa/Research/paper.pdf"
    # replaying the same token range changes nothing
    with db.scoped(account["id"]) as conn:
        conn.execute("UPDATE drive_index.sync_cursors SET drive_page_token = '1'")
    after = len(events(account["id"]))
    DriveSync(account["id"], src["id"]).pull()
    assert len(events(account["id"])) == after


def test_new_folder_and_child_in_same_page(account, drive):
    src = folder_source(account, drive)
    DriveSync(account["id"], src["id"]).crawl()
    drive.add("kid", "kid.txt", "nf", record=False)
    drive.add("nf", "New Folder", "PROJ", folder=True)
    drive._change("kid")
    # child change listed before its parent
    drive.changes[-2], drive.changes[-1] = drive.changes[-1], drive.changes[-2]
    DriveSync(account["id"], src["id"]).pull()
    assert nodes(account["id"])["kid"]["path"] == "/New Folder/kid.txt"


def test_my_drive_and_shared_drive_roots(account, drive):
    cid = _conn_id(account["id"])
    drive.add("a", "a.txt", drive.root_id, record=False)
    drive.add("shared", "from a colleague.pdf", None, record=False)
    drive.drives["SD1"] = "Team Footage"
    drive.add("sdf", "rush.mov", "SD1", drive_id="SD1", record=False)
    my = make_source(account["id"], root_kind="my_drive", root_ref=drive.root_id, name="My Drive", connection_id=cid)
    sd = make_source(account["id"], root_kind="shared_drive", root_ref="SD1", name="Team Footage", connection_id=cid)
    DriveSync(account["id"], my["id"]).crawl()
    DriveSync(account["id"], sd["id"]).crawl()
    mine = nodes(account["id"], my["id"])
    assert "sdf" not in mine and mine["a"]["path"] == "/a.txt"
    assert mine["shared"]["path"] == "/Shared with me/from a colleague.pdf"
    assert SHARED_WITH_ME in mine
    assert nodes(account["id"], sd["id"])["sdf"]["path"] == "/rush.mov"


def test_rate_limits_are_retried(account, drive):
    src = folder_source(account, drive)
    build_tree(drive)
    drive.fail_next = [429, 403, 503]
    DriveSync(account["id"], src["id"]).crawl()
    assert nodes(account["id"])["f4"]["status"] == "active"


def test_reconcile_fixes_drift(account, drive):
    src = folder_source(account, drive)
    build_tree(drive)
    DriveSync(account["id"], src["id"]).crawl()
    # Drift: changes that never reached us through the feed
    drive.files.pop("f4")
    drive.update("f0", record=False, name="renamed_silently.mov")
    DriveSync(account["id"], src["id"]).crawl(reconcile=True)
    n = nodes(account["id"])
    assert n["f4"]["status"] == "deleted"
    assert n["f0"]["name"] == "renamed_silently.mov"


def test_webhook_verifies_token_and_queues_pull(account, drive):
    src = folder_source(account, drive)
    DriveSync(account["id"], src["id"]).crawl()
    with db.scoped(account["id"]) as conn:
        c = conn.execute("SELECT channel_id FROM drive_index.sync_cursors").fetchone()
        conn.execute("UPDATE drive_index.sync_cursors SET channel_token_hash = %s", (hash_key("tok"),))
    with pytest.raises(PermissionError):
        handle_notification(c["channel_id"], "wrong", "change")
    assert handle_notification(c["channel_id"], "tok", "sync") is False
    assert handle_notification(c["channel_id"], "tok", "change") is True
    assert handle_notification(c["channel_id"], "tok", "change") is True  # deduped
    with db.scoped(account["id"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM drive_index.index_jobs WHERE kind = 'drive_pull_changes'"
                            ).fetchone()["n"] == 1


def test_jobs_through_worker(account, drive):
    src = folder_source(account, drive)
    build_tree(drive)
    with db.scoped(account["id"]) as conn:
        queue.enqueue(conn, account["id"], "drive_initial_crawl", src["id"], dedupe_key="crawl")
    assert drain() >= 1
    drive.add("w", "via worker.txt", "PROJ")
    with db.scoped(account["id"]) as conn:
        queue.enqueue(conn, account["id"], "drive_pull_changes", src["id"])
    drain()
    assert nodes(account["id"])["w"]["status"] == "active"


def test_scheduler_polls_renews_and_does_not_double_crawl(account, drive):
    from nous_drive import scheduler
    src = folder_source(account, drive)
    scheduler.tick()
    with db.scoped(account["id"]) as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM drive_index.index_jobs")]
    assert kinds == ["drive_initial_crawl"]
    job = queue.claim(1)[0]  # crawl now "running"
    with db.system() as conn:
        conn.execute("DELETE FROM drive_index.scheduler_state")
    scheduler.tick()
    with db.scoped(account["id"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM drive_index.index_jobs").fetchone()["n"] == 1
    from nous_drive.worker import run_one
    run_one(job)
    with db.system() as conn:
        conn.execute("DELETE FROM drive_index.scheduler_state")
        conn.execute("UPDATE drive_index.sync_cursors SET last_poll_at = now() - interval '1 hour', "
                     "channel_expires_at = now() + interval '1 minute'")
    scheduler.tick()
    with db.scoped(account["id"]) as conn:
        pending = {r["kind"] for r in conn.execute("SELECT kind FROM drive_index.index_jobs WHERE status = 'pending'")}
    assert pending == {"drive_pull_changes", "drive_renew_channel"}
    drain()
    with db.scoped(account["id"]) as conn:
        c = conn.execute("SELECT channel_expires_at > now() + interval '1 day' AS fresh FROM drive_index.sync_cursors").fetchone()
    assert c["fresh"]
