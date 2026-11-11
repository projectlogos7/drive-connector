"""Backend for the web app: default My Drive on connect, selection changes, progress reporting."""

import pytest
from fastapi.testclient import TestClient

from nous_drive import db
from nous_drive.api.app import add_default_my_drive, app
from nous_drive.config import reset_settings
from nous_drive.connectors.gdrive.sync import DriveSync
from nous_drive.mcp import tools as T
from nous_drive.worker import drain

from .fake_drive import FakeDrive, install


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    install(monkeypatch, fake)
    monkeypatch.setenv("NOUS_DRIVE_DRIVE_PAGE_SIZE", "2")
    reset_settings()
    return fake


@pytest.fixture
def api():
    return TestClient(app, base_url="https://drive-index.test")


def H(k):
    return {"Authorization": f"Bearer {k}"}


def _connection(account_id):
    with db.scoped(account_id) as conn:
        return str(conn.execute("INSERT INTO drive_index.drive_connections (account_id, google_email) "
                                "VALUES (%s, 'nous@example.org') RETURNING id", (account_id,)).fetchone()["id"])


def test_connect_adds_my_drive_and_starts_indexing(account, drive, api):
    drive.add("a", "Madarasa", drive.root_id, folder=True, record=False)
    drive.add("b", "script.docx", "a", record=False)
    cid = _connection(account["id"])
    src = add_default_my_drive(account["id"], cid, "nous@example.org")
    assert src["root_kind"] == "my_drive" and src["root_ref"] == drive.root_id
    assert add_default_my_drive(account["id"], cid) is None  # not twice
    sel = api.get(f"/api/connections/{cid}/selection", headers=H(account["admin_key"])).json()
    assert [r["kind"] for r in sel["roots"]] == ["my_drive"]
    st = api.get("/api/status", headers=H(account["admin_key"])).json()["sources"][0]["progress"]
    assert st["phase"] == "queued" and st["percent"] == 0
    drain()  # the crawl job queued on connect
    st = api.get("/api/status", headers=H(account["admin_key"])).json()["sources"][0]
    assert st["status"] == "live" and st["progress"]["phase"] == "live" and st["progress"]["percent"] == 100
    assert st["scan_progress"]["total"] == 2


def test_selection_switches_whole_drive_to_folders(account, drive, api):
    k = account["admin_key"]
    drive.add("f1", "Footage", drive.root_id, folder=True, record=False)
    drive.add("f2", "Scripts", drive.root_id, folder=True, record=False)
    drive.drives["SD"] = "Team"
    cid = _connection(account["id"])
    add_default_my_drive(account["id"], cid)
    roots = api.get(f"/api/connections/{cid}/roots", headers=H(k)).json()
    assert roots["my_drive"]["id"] == drive.root_id and roots["shared_drives"][0]["name"] == "Team"
    kids = api.get(f"/api/connections/{cid}/roots?parent={drive.root_id}", headers=H(k)).json()
    assert [f["name"] for f in kids["folders"]] == ["Footage", "Scripts"]
    r = api.put(f"/api/connections/{cid}/selection", headers=H(k), json={"roots": [
        {"kind": "folder", "id": "f1", "name": "Footage"}, {"kind": "shared_drive", "id": "SD", "name": "Team"}]}).json()
    assert {a["name"] for a in r["added"]} == {"Footage", "Team"} and [x["name"] for x in r["removed"]] == ["My Drive"]
    sel = api.get(f"/api/connections/{cid}/selection", headers=H(k)).json()["roots"]
    assert {x["kind"] for x in sel} == {"folder", "shared_drive"}
    with db.scoped(account["id"]) as conn:  # connection kept: credentials not revoked by the switch
        assert conn.execute("SELECT revoked_at FROM drive_index.drive_connections").fetchone()["revoked_at"] is None
        assert conn.execute("SELECT count(*) AS n FROM drive_index.index_jobs WHERE kind = 'drive_initial_crawl' "
                            "AND status = 'pending'").fetchone()["n"] == 2
    assert api.put(f"/api/connections/{cid}/selection", headers=H(k), json={"roots": []}).status_code == 400


def test_progress_percent_during_drive_crawl(account, drive, monkeypatch):
    for i in range(10):
        drive.add(f"x{i}", f"f{i}.mov", drive.root_id, record=False)
    cid = _connection(account["id"])
    src = add_default_my_drive(account["id"], cid)
    sync = DriveSync(account["id"], src["id"])
    seen = []
    orig = sync._apply

    def spy(events, cursor=None, **kw):
        orig(events, cursor=cursor, **kw)
        if cursor and "scan_progress" in cursor:
            st = T.get_sync_status(T.Ctx(account_id=account["id"]))["sources"][0]["progress"]
            seen.append((st["phase"], st["percent"]))
    monkeypatch.setattr(sync, "_apply", spy)
    sync.crawl()
    phases = [p for p, _ in seen]
    assert "counting" in phases and "listing" in phases
    listing = [pct for p, pct in seen if p == "listing"]
    assert listing == sorted(listing) and 0 < listing[-1] < 100


def test_local_scan_progress(account, api, tmp_path, monkeypatch):
    k = account["admin_key"]
    code = api.post("/api/devices/pairing-codes", json={"name": "Mac"}, headers=H(k)).json()["code"]
    tok = api.post("/agent/pair", json={"code": code, "machine_id": "m1"}).json()["token"]
    sid = api.post("/agent/sources", json={"path": "/Users/n/Footage"}, headers=H(tok)).json()["id"]
    p = T.get_sync_status(T.Ctx(account_id=account["id"]))["sources"][0]["progress"]
    assert p["phase"] == "waiting_for_device"
    api.post(f"/agent/sources/{sid}/scan/begin", json={"scan_id": "s1", "full": True, "total": 4}, headers=H(tok))
    ev = [{"provider_id": f"p{i}", "name": f"{i}.mov", "parent_provider_id": "__root__", "scan_id": "s1"} for i in range(2)]
    api.post(f"/agent/sources/{sid}/events", json={"events": ev, "scan_id": "s1"}, headers=H(tok))
    drain()
    p = T.get_sync_status(T.Ctx(account_id=account["id"]))["sources"][0]["progress"]
    assert (p["phase"], p["done"], p["total"], p["percent"]) == ("listing", 2, 4, 50)
    assert "2 of 4" in p["label"]
