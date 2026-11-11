"""HTTP service: web-app API (admin), OAuth callback, local-agent ingest API, Drive webhook, KB search."""

from __future__ import annotations

import logging
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from .. import __version__, accounts, db, ingest, queue
from ..accounts import AuthError, audit, resolve_key
from ..config import get_settings
from ..crypto import verify_state
from ..mcp import tools as T

log = logging.getLogger(__name__)

app = FastAPI(title="Nous Drive Connectors", version=__version__)


# ------------------------------------------------------------------ auth
def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return authorization.split(" ", 1)[1].strip()


def admin(authorization: str | None = Header(None)) -> dict:
    try:
        return resolve_key(_bearer(authorization), ("admin",))
    except AuthError as e:
        raise HTTPException(401, str(e)) from e


def reader(authorization: str | None = Header(None)) -> dict:
    try:
        return resolve_key(_bearer(authorization), ("admin", "session"))
    except AuthError as e:
        raise HTTPException(401, str(e)) from e


def device(authorization: str | None = Header(None)) -> dict:
    try:
        ident = resolve_key(_bearer(authorization), ("device",))
    except AuthError as e:
        raise HTTPException(401, str(e)) from e
    if not ident["device_id"]:
        raise HTTPException(401, "not a device token")
    return ident


def _ctx(ident: dict) -> T.Ctx:
    return T.Ctx(account_id=str(ident["account_id"]), session_id=f"web:{ident['key_id']}", actor=f"key:{ident['key_id']}")


def _tool(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except T.ToolError as e:
        raise HTTPException(400, str(e)) from e


# ------------------------------------------------------------------ basics
@app.get("/healthz")
def healthz() -> dict:
    with db.system() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "version": __version__}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/app")


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def web_app() -> str:
    return resources.files("nous_drive.api.static").joinpath("index.html").read_text()


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(ident: dict = Depends(admin)) -> str:
    """Per-source metrics in Prometheus text format (lag, events/min, errors, queue depth, last sync)."""
    st = T.get_sync_status(_ctx(ident))
    lines = ["# TYPE nous_drive_source_live gauge", "# TYPE nous_drive_source_lag_seconds gauge",
             "# TYPE nous_drive_source_events_per_minute gauge", "# TYPE nous_drive_queue_depth gauge",
             "# TYPE nous_drive_dead_letters gauge"]
    for s in st["sources"]:
        lbl = f'source="{s["name"]}",type="{s["type"]}",id="{s["source_id"]}"'
        lines += [f"nous_drive_source_live{{{lbl}}} {1 if s['live'] else 0}",
                  f"nous_drive_source_lag_seconds{{{lbl}}} {s['lag_seconds'] if s['lag_seconds'] is not None else 'NaN'}",
                  f"nous_drive_source_events_per_minute{{{lbl}}} {s['events_per_minute_10m']}",
                  f"nous_drive_queue_depth{{{lbl}}} {s['queue']['pending'] + s['queue']['running']}",
                  f"nous_drive_dead_letters{{{lbl}}} {s['queue']['dead_letter']}"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ web app API (admin)
@app.get("/api/me")
def me(ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        acct = conn.execute("SELECT id, name, email, created_at FROM drive_index.accounts").fetchone()
    return {"account": acct, "key": {"id": ident["key_id"], "kind": ident["kind"]}}


@app.get("/api/sources")
def sources(ident: dict = Depends(reader)) -> dict:
    return T.list_sources(_ctx(ident))


@app.get("/api/status")
def status(source: str | None = None, ident: dict = Depends(reader)) -> dict:
    return _tool(T.get_sync_status, _ctx(ident), source)


@app.post("/api/sources/{source_id}/{action}")
def source_action(source_id: str, action: str, ident: dict = Depends(admin)) -> dict:
    fn = {"pause": T.pause_source, "resume": T.resume_source, "resync": T.resync_source}.get(action)
    if action == "backfill-content":
        from ..content.pipeline import request_backfill
        with db.scoped(ident["account_id"]) as conn:
            return {"queued": request_backfill(conn, ident["account_id"], source_id, f"key:{ident['key_id']}")}
    if not fn:
        raise HTTPException(404, "unknown action")
    return _tool(fn, _ctx(ident), source_id)


class SettingsPatch(BaseModel):
    ignore: list[str] | None = None
    notifications: dict[str, Any] | None = None
    content: dict[str, Any] | None = None


@app.patch("/api/sources/{source_id}/settings")
def patch_settings(source_id: str, body: SettingsPatch, ident: dict = Depends(admin)) -> dict:
    patch = body.model_dump(exclude_none=True)
    with db.scoped(ident["account_id"]) as conn:
        try:
            row = accounts.update_source_settings(conn, ident["account_id"], source_id, patch, f"key:{ident['key_id']}")
        except KeyError as e:
            raise HTTPException(404, "source not found") from e
    return {"id": str(row["id"]), "settings": row["settings"]}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        src = accounts.get_source(conn, source_id)
        if not src:
            raise HTTPException(404, "source not found")
        cur = conn.execute("SELECT channel_id, channel_resource_id FROM drive_index.sync_cursors WHERE source_id = %s",
                           (source_id,)).fetchone()
        last_on_connection = src["connection_id"] and conn.execute(
            "SELECT count(*) AS n FROM drive_index.sources WHERE connection_id = %s", (src["connection_id"],)
        ).fetchone()["n"] == 1
    if src["type"] == "gdrive":
        from ..connectors.gdrive.auth import make_client, revoke_remote
        try:
            if cur and cur["channel_id"]:
                make_client(ident["account_id"], src["connection_id"]).stop_channel(cur["channel_id"], cur["channel_resource_id"])
        except Exception as e:
            log.info("stop channel on delete failed: %s", e)
        if last_on_connection:
            revoke_remote(ident["account_id"], src["connection_id"])
    with db.scoped(ident["account_id"]) as conn:
        accounts.remove_source(conn, ident["account_id"], source_id, f"key:{ident['key_id']}")
    return {"deleted": source_id}


# ---- Google Drive connect flow
@app.get("/api/oauth/google/start")
def google_start(content: bool = False, connection_id: str | None = None, ident: dict = Depends(admin)) -> dict:
    from ..connectors.gdrive.auth import authorization_url
    try:
        return {"url": authorization_url(ident["account_id"], content=content, connection_id=connection_id)}
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e


@app.get("/oauth/google/callback", include_in_schema=False)
def google_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    from ..connectors.gdrive.auth import exchange_code, make_client, set_connection_identity, store_connection
    if error:
        return RedirectResponse(f"/app#error={error}")
    try:
        st = verify_state(state or "")
    except ValueError as e:
        raise HTTPException(400, f"bad OAuth state: {e}") from e
    tokens = exchange_code(code or "")
    conn_row = store_connection(st["a"], tokens, connection_id=st.get("conn"))
    try:
        about = make_client(st["a"], conn_row["id"]).about().get("user", {})
        set_connection_identity(st["a"], conn_row["id"], about.get("emailAddress"), about.get("permissionId"))
    except Exception as e:
        log.warning("could not read Drive identity: %s", e)
    return RedirectResponse(f"/app#pick-roots={conn_row['id']}")


@app.get("/api/connections")
def connections(ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        rows = conn.execute(
            """SELECT id, google_email, scopes, created_at, revoked_at FROM drive_index.drive_connections
               ORDER BY created_at""").fetchall()
    return {"connections": rows}


@app.get("/api/connections/{connection_id}/roots")
def pick_roots(connection_id: str, parent: str | None = None, ident: dict = Depends(admin)) -> dict:
    """Root picker: My Drive, shared drives, and folders (children of ``parent``)."""
    from ..connectors.gdrive.auth import make_client
    from ..connectors.gdrive.client import FOLDER_MIME
    client = make_client(ident["account_id"], connection_id)
    if parent:
        folders = []
        for page in client.iter_files(q=f"'{parent}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
                                      corpora="allDrives", fields="id,name,driveId"):
            folders += [{"kind": "folder", "id": f["id"], "name": f["name"]} for f in page]
        return {"parent": parent, "folders": sorted(folders, key=lambda f: f["name"].lower())}
    my = client.get_file("root", fields="id,name")
    drives = client.list_drives()
    return {"my_drive": {"kind": "my_drive", "id": my["id"], "name": "My Drive"},
            "shared_drives": [{"kind": "shared_drive", "id": d["id"], "name": d["name"]} for d in drives]}


class RootChoice(BaseModel):
    kind: str = Field(pattern="^(my_drive|folder|shared_drive)$")
    id: str
    name: str


class CreateSources(BaseModel):
    roots: list[RootChoice]
    settings: dict[str, Any] | None = None


@app.post("/api/connections/{connection_id}/sources")
def create_drive_sources(connection_id: str, body: CreateSources, ident: dict = Depends(admin)) -> dict:
    created = []
    with db.scoped(ident["account_id"]) as conn:
        for r in body.roots:
            dup = conn.execute("SELECT id FROM drive_index.sources WHERE connection_id = %s AND root_ref = %s",
                               (connection_id, r.id)).fetchone()
            if dup:
                continue
            src = accounts.create_source(conn, ident["account_id"], type_="gdrive", root_kind=r.kind, root_ref=r.id,
                                         name=r.name, connection_id=connection_id, settings=body.settings,
                                         actor=f"key:{ident['key_id']}")
            queue.enqueue(conn, ident["account_id"], "drive_initial_crawl", src["id"], dedupe_key=f"crawl:{src['id']}")
            created.append({"id": str(src["id"]), "name": src["name"], "root_kind": src["root_kind"]})
    return {"created": created}


# ---- devices & keys
class PairingRequest(BaseModel):
    name: str | None = None


@app.post("/api/devices/pairing-codes")
def pairing_code(body: PairingRequest, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        code = accounts.create_pairing_code(conn, ident["account_id"], body.name)
        audit(conn, ident["account_id"], f"key:{ident['key_id']}", "device.pairing_code", None, {"name": body.name})
    return {"code": code, "expires_in_minutes": 15,
            "instructions": f"On the computer: nous-sync pair {code} --server {get_settings().public_base_url}"}


@app.get("/api/devices")
def devices(ident: dict = Depends(admin)) -> dict:
    s = get_settings()
    with db.scoped(ident["account_id"]) as conn:
        rows = conn.execute(
            f"""SELECT id, name, hostname, os, agent_version, last_seen_at, revoked_at, created_at,
                  last_seen_at > now() - make_interval(secs => %s) AS online
               FROM drive_index.devices ORDER BY created_at""", (s.device_offline_seconds,)).fetchall()
    return {"devices": rows}


class DevicePatch(BaseModel):
    name: str


@app.patch("/api/devices/{device_id}")
def rename_device(device_id: str, body: DevicePatch, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        row = conn.execute("UPDATE drive_index.devices SET name = %s WHERE id = %s RETURNING id, name",
                           (body.name, device_id)).fetchone()
        if not row:
            raise HTTPException(404, "device not found")
        audit(conn, ident["account_id"], f"key:{ident['key_id']}", "device.renamed", device_id, {"name": body.name})
    return row


@app.delete("/api/devices/{device_id}")
def revoke_device(device_id: str, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        accounts.revoke_device(conn, ident["account_id"], device_id, f"key:{ident['key_id']}")
    return {"revoked": device_id}


class KeyRequest(BaseModel):
    name: str
    kind: str = Field("session", pattern="^(session|admin)$")


@app.post("/api/keys")
def create_key(body: KeyRequest, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        key = accounts.issue_key(conn, ident["account_id"], body.kind, body.name)
        audit(conn, ident["account_id"], f"key:{ident['key_id']}", "key.created", None, {"name": body.name, "kind": body.kind})
    return {"key": key, "kind": body.kind, "name": body.name, "note": "shown once; store it safely"}


@app.get("/api/keys")
def list_keys(ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        rows = conn.execute("""SELECT id, kind, name, key_prefix, created_at, last_used_at, revoked_at
                               FROM drive_index.api_keys ORDER BY created_at""").fetchall()
    return {"keys": rows}


@app.delete("/api/keys/{key_id}")
def revoke_key(key_id: str, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        conn.execute("UPDATE drive_index.api_keys SET revoked_at = now() WHERE id = %s", (key_id,))
        audit(conn, ident["account_id"], f"key:{ident['key_id']}", "key.revoked", key_id)
    return {"revoked": key_id}


@app.get("/api/notifications")
def notifications(limit: int = 50, ident: dict = Depends(reader)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        rows = conn.execute("SELECT * FROM drive_index.notifications ORDER BY id DESC LIMIT %s", (min(limit, 200),)).fetchall()
    return {"notifications": rows}


@app.get("/api/audit")
def audit_log(limit: int = 100, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        rows = conn.execute("SELECT * FROM drive_index.audit_log ORDER BY id DESC LIMIT %s", (min(limit, 500),)).fetchall()
    return {"audit": rows}


# ---- read API mirroring the MCP tools (for the web app and other Nous systems)
@app.get("/api/tree")
def tree(path: str = "/", source: str | None = None, depth: int = 2, include_files: bool = False,
         ident: dict = Depends(reader)) -> dict:
    return _tool(T.get_tree, _ctx(ident), path, None, source, depth, include_files)


@app.get("/api/changes")
def changes(since: str = "24h", source: str | None = None, folder: str | None = None, limit: int = 100,
            cursor: str | None = None, ident: dict = Depends(reader)) -> dict:
    return _tool(T.list_changes, _ctx(ident), since, None, source, folder, None, limit, cursor)


@app.get("/api/search")
def search(q: str, source: str | None = None, mode: str = "name", limit: int = 30,
           ident: dict = Depends(reader)) -> dict:
    return _tool(T.search_files, _ctx(ident), q, source, None, None, True, "active", mode, limit)


@app.get("/api/kb/search")
def kb_search(q: str, mode: str = "hybrid", limit: int = 10, source: str | None = None,
              ident: dict = Depends(reader)) -> dict:
    """Knowledge Base retrieval (Phase 2): best-matching chunks with file context and summary."""
    from ..content.search import search_content
    with db.scoped(ident["account_id"]) as conn:
        src = _tool(T.resolve_source, conn, source) if source else None
        hits = search_content(conn, q, limit=min(limit, 50), source_ids=[str(src["id"])] if src else None, mode=mode)
        ids = [h["node_id"] for h in hits]
        nodes = {str(r["id"]): r for r in conn.execute(
            """SELECT n.id, n.name, n.path, n.web_link, n.local_path, n.content_version, s.name AS source,
                      sm.summary FROM drive_index.nodes n JOIN drive_index.sources s ON s.id = n.source_id
               LEFT JOIN drive_index.summaries sm ON sm.node_id = n.id AND sm.content_version = n.content_version
               WHERE n.id = ANY(%s)""", (ids,))}
    return {"query": q, "results": [{**h, **{k: (str(v) if k == "id" else v) for k, v in nodes.get(h["node_id"], {}).items()}}
                                    for h in hits]}


# ------------------------------------------------------------------ Drive webhook
@app.post("/webhooks/drive", include_in_schema=False)
def drive_webhook(x_goog_channel_id: str | None = Header(None), x_goog_channel_token: str | None = Header(None),
                  x_goog_resource_state: str | None = Header(None)) -> dict:
    from ..connectors.gdrive.sync import handle_notification
    try:
        queued = handle_notification(x_goog_channel_id, x_goog_channel_token, x_goog_resource_state)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    return {"queued": queued}


# ------------------------------------------------------------------ local agent API
class PairBody(BaseModel):
    code: str
    machine_id: str
    hostname: str | None = None
    os: str | None = None
    agent_version: str | None = None
    device_name: str | None = None


@app.post("/agent/pair")
def agent_pair(body: PairBody) -> dict:
    try:
        res = accounts.redeem_pairing_code(body.code, body.machine_id, body.hostname, body.os, body.agent_version,
                                           body.device_name)
    except AuthError as e:
        raise HTTPException(400, str(e)) from e
    return {"token": res["token"], "device": {"id": str(res["device"]["id"]), "name": res["device"]["name"]},
            "repaired": res["repaired"]}


class HeartbeatBody(BaseModel):
    agent_version: str | None = None
    status: dict[str, Any] | None = None


@app.post("/agent/heartbeat")
def agent_heartbeat(body: HeartbeatBody, ident: dict = Depends(device)) -> dict:
    return ingest.heartbeat(ident["account_id"], ident["device_id"], body.agent_version, body.status)


class LocalSourceBody(BaseModel):
    path: str
    name: str | None = None
    ignore: list[str] | None = None


@app.post("/agent/sources")
def agent_add_source(body: LocalSourceBody, ident: dict = Depends(device)) -> dict:
    try:
        src = ingest.register_local_source(ident["account_id"], ident["device_id"], body.path, body.name, body.ignore)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"id": str(src["id"]), "name": src["name"], "path": src["root_ref"], "status": src["status"]}


@app.delete("/agent/sources/{source_id}")
def agent_remove_source(source_id: str, ident: dict = Depends(device)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        src = accounts.get_source(conn, source_id)
        if not src or str(src["device_id"]) != str(ident["device_id"]):
            raise HTTPException(404, "source not found")
        accounts.remove_source(conn, ident["account_id"], source_id, f"device:{ident['device_id']}")
    return {"deleted": source_id}


class ScanBody(BaseModel):
    scan_id: str
    full: bool = False
    stats: dict[str, Any] | None = None
    snapshot_ref: str | None = None


@app.post("/agent/sources/{source_id}/scan/begin")
def agent_scan_begin(source_id: str, body: ScanBody, ident: dict = Depends(device)) -> dict:
    try:
        ingest.begin_scan(ident["account_id"], ident["device_id"], source_id, body.scan_id, body.full)
    except ingest.IngestError as e:
        raise HTTPException(403, str(e)) from e
    return {"ok": True}


class EventsBody(BaseModel):
    events: list[dict[str, Any]]
    scan_id: str | None = None


@app.post("/agent/sources/{source_id}/events")
def agent_events(source_id: str, body: EventsBody, ident: dict = Depends(device)) -> dict:
    try:
        jobs = ingest.submit_events(ident["account_id"], ident["device_id"], source_id, body.events, body.scan_id)
    except ingest.IngestError as e:
        raise HTTPException(409 if "paused" in str(e) else 403, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"accepted": len(body.events), "jobs": jobs}


@app.post("/agent/sources/{source_id}/scan/complete")
def agent_scan_complete(source_id: str, body: ScanBody, ident: dict = Depends(device)) -> dict:
    try:
        ingest.complete_scan(ident["account_id"], ident["device_id"], source_id, body.scan_id, body.full,
                             body.stats, body.snapshot_ref)
    except ingest.IngestError as e:
        raise HTTPException(403, str(e)) from e
    return {"ok": True}


@app.get("/agent/content/requests")
def agent_content_requests(limit: int = 20, ident: dict = Depends(device)) -> dict:
    return {"requests": ingest.content_requests(ident["account_id"], ident["device_id"], min(limit, 100))}


class ContentBody(BaseModel):
    node_id: str
    text: str | None = None
    extractor: str = "agent"
    content_hash: str | None = None
    error: str | None = None


@app.post("/agent/content")
def agent_content(body: ContentBody, ident: dict = Depends(device)) -> dict:
    try:
        return ingest.submit_content(ident["account_id"], ident["device_id"], body.node_id, body.text,
                                     body.extractor, body.content_hash, body.error)
    except ingest.IngestError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        if type(e).__name__ == "ContentError":
            raise HTTPException(409, str(e)) from e
        raise
