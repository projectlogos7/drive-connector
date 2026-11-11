"""End to end: web API pairing -> Nous Sync agent -> ingest API -> queue -> indexer -> MCP tools."""

import os

import pytest
from fastapi.testclient import TestClient

from nous_drive import db, scheduler
from nous_drive.api.app import app
from nous_drive.mcp import tools as T
from nous_drive.worker import drain


@pytest.fixture
def api():
    return TestClient(app, base_url="https://drive-index.test")


def H(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def agent_env(tmp_path, monkeypatch, api, account):
    monkeypatch.setenv("NOUS_SYNC_HOME", str(tmp_path / "agent-home"))
    from nous_sync.client import Client
    from nous_sync.config import AgentConfig
    code = api.post("/api/devices/pairing-codes", json={"name": "Nous MacBook Pro"},
                    headers=H(account["admin_key"])).json()["code"]
    client = Client("https://drive-index.test")
    client.http = api
    cfg = AgentConfig.load()
    res = client.pair(code, cfg.machine_id, "macbook", "Darwin 25", None)
    cfg.server, cfg.token, cfg.device_id, cfg.device_name = "https://drive-index.test", res["token"], \
        res["device"]["id"], res["device"]["name"]
    cfg.save()
    client.token = res["token"]
    return cfg, client


def test_auth_required(api, account):
    assert api.get("/api/sources").status_code == 401
    assert api.get("/api/sources", headers=H("nd_admin_nope")).status_code == 401
    assert api.get("/api/sources", headers=H(account["admin_key"])).status_code == 200
    assert api.post("/agent/heartbeat", json={}, headers=H(account["admin_key"])).status_code == 401
    assert api.get("/healthz").json()["ok"]


def test_agent_end_to_end(tmp_path, agent_env, api, account):
    cfg, client = agent_env
    from nous_sync.agent import Agent, SourceRunner
    folder = tmp_path / "Footage"
    (folder / "Day 1").mkdir(parents=True)
    (folder / "Day 1" / "A001.mov").write_bytes(b"a" * 4096)
    (folder / "notes.txt").write_text("call list")
    agent = Agent(cfg, client)
    conf = agent.add_source(str(folder))
    assert conf.name == "Footage"
    # Nested folders on the same device are rejected
    from nous_sync.client import ServerError
    with pytest.raises(ServerError):
        agent.add_source(str(folder / "Day 1"))
    runner = SourceRunner(agent, conf)
    agent.runners[conf.id] = runner
    runner.scan(full=True)
    drain()
    ctx = T.Ctx(account_id=account["id"], session_id="claude-1")
    srcs = T.list_sources(ctx)["sources"]
    assert srcs[0]["status"] == "live" and srcs[0]["device"] == "Nous MacBook Pro" and srcs[0]["files"] == 2
    tree = T.get_tree(ctx, source="Footage", path="/", include_files=True)
    assert "A001.mov" in tree["text"]
    assert T.whats_new(ctx)["total"] == 3

    # Offline changes + catch-up diff
    os.rename(folder / "Day 1", folder / "Day 01")
    (folder / "notes.txt").unlink()
    (folder / "Day 01" / "A002.mov").write_bytes(b"b")
    runner.scan(full=False)
    drain()
    new = T.whats_new(ctx)
    assert new["total"] == 1 and new["groups"][0]["items"][0]["name"] == "A002.mov"
    assert new["groups"][0]["device"] == "Nous MacBook Pro"
    ch = {c["type"] for c in T.list_changes(ctx, source="Footage")["changes"]}
    assert {"renamed", "deleted", "added"} <= ch
    f = T.get_file(ctx, path="/Day 01/A001.mov", source="Footage")
    assert f["device"] == "Nous MacBook Pro" and f["local_path"].endswith("Day 01/A001.mov")

    # Heartbeat marks the device online; resync becomes a command for the agent
    T.resync_source(ctx, "Footage")
    resp = agent.heartbeat_once()
    assert resp["device"]["name"] == "Nous MacBook Pro"
    drain()
    st = T.get_sync_status(ctx, "Footage")["sources"][0]
    assert st["device"]["online"] and st["live"]

    # External drive unplugged -> unavailable, not deleted
    os.rename(folder, tmp_path / "Footage-unplugged")
    runner.check_volume()
    agent.flush()
    drain()
    assert T.list_files(ctx, source="Footage", status="unavailable", recursive=False)["total"] == 2
    os.rename(tmp_path / "Footage-unplugged", folder)
    runner.check_volume()
    drain()
    assert T.list_files(ctx, source="Footage")["total"] == 2


def test_repair_keeps_device_name(api, account, tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_SYNC_HOME", str(tmp_path / "h"))
    k = account["admin_key"]
    code = api.post("/api/devices/pairing-codes", json={"name": "Edit Suite iMac"}, headers=H(k)).json()["code"]
    first = api.post("/agent/pair", json={"code": code, "machine_id": "mac:1", "hostname": "imac"}).json()
    code2 = api.post("/api/devices/pairing-codes", json={}, headers=H(k)).json()["code"]
    second = api.post("/agent/pair", json={"code": code2, "machine_id": "mac:1", "hostname": "imac"}).json()
    assert second["repaired"] and second["device"]["name"] == "Edit Suite iMac"
    assert second["device"]["id"] == first["device"]["id"]
    assert api.post("/agent/heartbeat", json={}, headers=H(first["token"])).status_code == 401  # old token revoked
    assert api.post("/agent/heartbeat", json={}, headers=H(second["token"])).status_code == 200
    assert api.post("/agent/pair", json={"code": code2, "machine_id": "mac:2"}).status_code == 400  # single use


def test_device_offline_degrades_source(api, agent_env, account, tmp_path):
    cfg, client = agent_env
    from nous_sync.agent import Agent
    folder = tmp_path / "Docs"
    folder.mkdir()
    agent = Agent(cfg, client)
    conf = agent.add_source(str(folder))
    with db.system() as conn:
        conn.execute("UPDATE drive_index.sources SET status = 'live'")
        conn.execute("UPDATE drive_index.devices SET last_seen_at = now() - interval '1 hour'")
    scheduler.tick()
    ctx = T.Ctx(account_id=account["id"])
    assert T.get_sync_status(ctx, conf.id)["sources"][0]["status_reason"] == "device offline"
    agent.heartbeat_once()
    assert T.get_sync_status(ctx, conf.id)["sources"][0]["status"] == "live"


def test_web_api_keys_settings_and_delete(api, account):
    k = account["admin_key"]
    key = api.post("/api/keys", json={"name": "Claude Desktop"}, headers=H(k)).json()["key"]
    assert api.get("/api/sources", headers=H(key)).status_code == 200      # session keys can read
    assert api.post("/api/keys", json={"name": "x"}, headers=H(key)).status_code == 401  # but not administer
    from .conftest import make_source
    src = make_source(account["id"])
    r = api.patch(f"/api/sources/{src['id']}/settings", json={"notifications": {"types": ["added", "moved"]}},
                  headers=H(k))
    assert r.json()["settings"]["notifications"]["types"] == ["added", "moved"]
    assert api.post(f"/api/sources/{src['id']}/pause", headers=H(k)).json()["status"] == "paused"
    assert api.delete(f"/api/sources/{src['id']}", headers=H(k)).status_code == 200
    assert api.get("/api/sources", headers=H(k)).json()["count"] == 0
    audit = api.get("/api/audit", headers=H(k)).json()["audit"]
    assert {"source.added", "source.paused", "source.removed", "key.created"} <= {a["action"] for a in audit}
    assert "# TYPE nous_drive_source_live gauge" in api.get("/metrics", headers=H(k)).text
