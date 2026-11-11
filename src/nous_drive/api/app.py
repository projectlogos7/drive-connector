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


def _web_dir():
    """The built Next.js app (web/out). NOUS_DRIVE_WEB_DIR overrides; packaged builds ship it in nous_drive/web."""
    import os
    from pathlib import Path
    candidates = [os.environ.get("NOUS_DRIVE_WEB_DIR"),
                  str(Path(__file__).resolve().parent.parent / "web"),
                  str(Path(__file__).resolve().parents[3] / "web" / "out")]
    for c in candidates:
        if c and (Path(c) / "index.html").exists():
            return c
    return None


WEB_DIR = _web_dir()


@app.get("/app", include_in_schema=False)
def web_app():
    if WEB_DIR:
        return RedirectResponse("/app/")
    # Frontend not built: serve the simple built-in page.
    return HTMLResponse(resources.files("nous_drive.api.static").joinpath("index.html").read_text())


if WEB_DIR:
    from fastapi.staticfiles import StaticFiles
    app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="web")


# ------------------------------------------------------------------ email magic-link auth
class RequestLink(BaseModel):
    email: str


@app.post("/api/auth/request-link")
def request_link(body: RequestLink) -> dict:
    """Email a one-time sign-in link. Same response whether or not the address is known."""
    from ..mail import send_sign_in
    email = accounts.normalize_email(body.email)
    if "@" not in email:
        raise HTTPException(400, "invalid email")
    s = get_settings()
    members = accounts.find_active_members_by_email(email)
    links: list[dict] = []
    for m in members:
        raw = accounts.create_login_token(m)
        url = f"{s.public_base_url.rstrip('/')}/auth/email/callback?token={raw}"
        if s.dev_links:
            log.info("dev sign-in link for %s (%s): %s", email, m["account_name"], url)
        send_sign_in(email, m["account_name"] or "Nous Drive", url)
        links.append({"account": m["account_name"], "url": url})
    out: dict[str, Any] = {"sent": True}
    if s.dev_links:
        out["dev_links"] = links
    return out


@app.get("/auth/email/callback", include_in_schema=False)
def email_callback(token: str | None = None):
    """Redeem a magic link and hand the browser an admin web key via the hash."""
    try:
        member, key = accounts.redeem_login_token(token or "")
    except AuthError as e:
        return RedirectResponse(f"/app/login/#error={str(e)}" if WEB_DIR else f"/app#error={str(e)}")
    # Key goes in the hash so it never hits server access logs as a query param again.
    return RedirectResponse(f"/app/#session={key}" if WEB_DIR else f"/app#session={key}")


class InviteMember(BaseModel):
    email: str
    name: str | None = None
    role: str = Field("admin", pattern="^(admin|member)$")
    send_invite: bool = True


@app.get("/api/members")
def members(ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        return {"members": accounts.list_members(conn)}


@app.post("/api/members")
def invite_member(body: InviteMember, ident: dict = Depends(admin)) -> dict:
    from ..mail import send_sign_in
    s = get_settings()
    with db.scoped(ident["account_id"]) as conn:
        member = accounts.add_member(
            conn, ident["account_id"], body.email, name=body.name, role=body.role,
            actor=f"key:{ident['key_id']}",
        )
        acct = conn.execute("SELECT name FROM drive_index.accounts WHERE id = %s",
                            (str(ident["account_id"]),)).fetchone()
    out: dict[str, Any] = {"member": {k: member[k] for k in ("id", "email", "name", "role", "created_at")}}
    if body.send_invite:
        raw = accounts.create_login_token(member)
        url = f"{s.public_base_url.rstrip('/')}/auth/email/callback?token={raw}"
        send_sign_in(member["email"], (acct or {}).get("name") or "Nous Drive", url)
        out["invite_sent"] = True
        if s.dev_links:
            out["dev_link"] = url
    return out


@app.delete("/api/members/{member_id}")
def remove_member(member_id: str, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        accounts.deactivate_member(conn, ident["account_id"], member_id, f"key:{ident['key_id']}")
    return {"deactivated": member_id}

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


@app.get("/api/progress")
def progress_now(ident: dict = Depends(reader)) -> dict:
    from ..progress import snapshot
    with db.scoped(ident["account_id"]) as conn:
        return {"sources": snapshot(conn), "server_time": conn.execute("SELECT now() AS t").fetchone()["t"]}


@app.get("/api/live")
async def live(request: Request, ident: dict = Depends(reader)):
    """Server-sent events for the web app: ``progress`` (every source, when anything moved),
    ``changes`` (new change-log entries) and ``notification``. Clients read it with fetch() so the
    bearer token stays in a header."""
    import asyncio
    import json as _json

    import anyio
    from fastapi.responses import StreamingResponse

    from ..progress import snapshot

    account_id = ident["account_id"]

    def poll(last_event: int | None, last_note: int | None):
        with db.scoped(account_id) as conn:
            snap = snapshot(conn)
            if last_event is None:
                last_event = conn.execute("SELECT coalesce(max(id), 0) AS m FROM drive_index.change_events").fetchone()["m"]
                last_note = conn.execute("SELECT coalesce(max(id), 0) AS m FROM drive_index.notifications").fetchone()["m"]
                return snap, [], [], last_event, last_note
            ch = conn.execute(
                """SELECT e.id, e.type, e.kind, e.name, e.path, e.detected_at, e.before->>'path' AS from_path,
                          s.name AS source, d.name AS device, n.size
                   FROM drive_index.change_events e JOIN drive_index.sources s ON s.id = e.source_id
                   LEFT JOIN drive_index.devices d ON d.id = e.device_id
                   LEFT JOIN drive_index.nodes n ON n.id = e.node_id
                   WHERE e.id > %s ORDER BY e.id DESC LIMIT 50""", (last_event,)).fetchall()
            notes = conn.execute("SELECT * FROM drive_index.notifications WHERE id > %s ORDER BY id", (last_note,)).fetchall()
        return snap, ch, notes, max([last_event] + [c["id"] for c in ch]), max([last_note] + [n["id"] for n in notes])

    def sse(event: str, data) -> str:
        return f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"

    async def stream():
        last_event = last_note = None
        last_snap = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                snap, ch, notes, last_event, last_note = await anyio.to_thread.run_sync(poll, last_event, last_note)
            except Exception as e:  # keep the stream alive across DB hiccups
                log.warning("live stream poll failed: %s", e)
                await asyncio.sleep(3)
                continue
            key = _json.dumps(snap, default=str, sort_keys=True)
            if key != last_snap:
                last_snap = key
                yield sse("progress", {"sources": snap})
            if ch:
                yield sse("changes", {"changes": list(reversed(ch))})
            for n in notes:
                yield sse("notification", n)
            idle += 1
            if idle % 15 == 0:
                yield ": keep-alive\n\n"
            busy = any(p["busy"] for p in snap)
            await asyncio.sleep(1.0 if busy else 3.0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    _delete_source(ident, source_id, revoke_if_last=True)
    return {"deleted": source_id}


def _delete_source(ident: dict, source_id: str, revoke_if_last: bool) -> None:
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
        if last_on_connection and revoke_if_last:
            revoke_remote(ident["account_id"], src["connection_id"])
    with db.scoped(ident["account_id"]) as conn:
        accounts.remove_source(conn, ident["account_id"], source_id, f"key:{ident['key_id']}")


# ---- Google Drive connect flow
@app.get("/api/oauth/google/start")
def google_start(content: bool | None = None, connection_id: str | None = None, ident: dict = Depends(admin)) -> dict:
    from ..connectors.gdrive.auth import authorization_url
    try:
        return {"url": authorization_url(ident["account_id"], content=content, connection_id=connection_id)}
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e


@app.get("/oauth/google/callback", include_in_schema=False)
def google_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    from ..connectors.gdrive.auth import exchange_code, make_client, set_connection_identity, store_connection
    if error:
        return RedirectResponse(f"/app/drive/#error={error}" if WEB_DIR else f"/app#error={error}")
    try:
        st = verify_state(state or "")
    except ValueError as e:
        raise HTTPException(400, f"bad OAuth state: {e}") from e
    tokens = exchange_code(code or "")
    conn_row = store_connection(st["a"], tokens, connection_id=st.get("conn"))
    email = None
    try:
        about = make_client(st["a"], conn_row["id"]).about().get("user", {})
        email = about.get("emailAddress")
        set_connection_identity(st["a"], conn_row["id"], email, about.get("permissionId"))
    except Exception as e:
        log.warning("could not read Drive identity: %s", e)
    if st.get("conn"):
        # Re-authorised an existing connection: resume any stuck sources.
        try:
            _resume_after_reconnect(st["a"], conn_row["id"])
        except Exception as e:
            log.warning("could not resume sources after reconnect: %s", e)
    else:
        try:
            add_default_my_drive(st["a"], conn_row["id"], email)
        except Exception as e:
            log.warning("could not add My Drive by default: %s", e)
    return RedirectResponse(f"/app/drive/#connected={conn_row['id']}" if WEB_DIR
                            else f"/app#drive/connected={conn_row['id']}")


def _resume_after_reconnect(account_id, connection_id) -> None:
    """Clear auth-error status and re-queue initial crawl / pull for this connection's sources."""
    with db.scoped(account_id) as conn:
        rows = conn.execute(
            "SELECT id, status FROM drive_index.sources WHERE connection_id = %s",
            (str(connection_id),),
        ).fetchall()
        for row in rows:
            if row["status"] in ("error", "connecting", "degraded"):
                accounts.set_status(conn, row["id"], "connecting", None)
            queue.enqueue(
                conn, account_id, "drive_initial_crawl", row["id"],
                dedupe_key=f"crawl:{row['id']}",
            )


def add_default_my_drive(account_id, connection_id, email: str | None = None) -> dict | None:
    """A new Drive connection watches the whole My Drive by default and starts indexing at once.
    The user can narrow it to folders (or add shared drives) afterwards."""
    from ..connectors.gdrive.auth import make_client
    root = make_client(account_id, connection_id).get_file("root", fields="id,name")
    with db.scoped(account_id) as conn:
        if conn.execute("SELECT 1 FROM drive_index.sources WHERE connection_id = %s", (str(connection_id),)).fetchone():
            return None
        others = conn.execute("SELECT count(*) AS n FROM drive_index.sources WHERE type = 'gdrive'").fetchone()["n"]
        name = f"My Drive · {email}" if (others and email) else "My Drive"
        src = accounts.create_source(conn, account_id, type_="gdrive", root_kind="my_drive", root_ref=root["id"],
                                     name=name, connection_id=connection_id, actor="oauth")
        queue.enqueue(conn, account_id, "drive_initial_crawl", src["id"], dedupe_key=f"crawl:{src['id']}")
    return src


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
    from ..connectors.gdrive.client import AuthExpired, DriveError, FOLDER_MIME
    try:
        client = make_client(ident["account_id"], connection_id)
        if parent:
            folders = []
            for page in client.iter_files(q=f"'{parent}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
                                          corpora="allDrives", fields="id,name,driveId"):
                folders += [{"kind": "folder", "id": f["id"], "name": f["name"]} for f in page]
            return {"parent": parent, "folders": sorted(folders, key=lambda f: f["name"].lower())}
        my = client.get_file("root", fields="id,name")
        # Shared-drive listing needs the same metadata scope but some Workspace
        # policies still 403 it; My Drive + folder browse must still work.
        try:
            drives = client.list_drives()
        except DriveError as e:
            log.warning("list_drives unavailable (%s); continuing with My Drive only", e)
            drives = []
        return {"my_drive": {"kind": "my_drive", "id": my["id"], "name": "My Drive"},
                "shared_drives": [{"kind": "shared_drive", "id": d["id"], "name": d["name"]} for d in drives]}
    except AuthExpired as e:
        raise HTTPException(
            401,
            "Google access expired or was revoked. Click Reconnect on this account and sign in again.",
        ) from e


@app.get("/api/connections/{connection_id}/selection")
def get_selection(connection_id: str, ident: dict = Depends(admin)) -> dict:
    with db.scoped(ident["account_id"]) as conn:
        rows = conn.execute(
            """SELECT id, root_kind, root_ref, name, status FROM drive_index.sources WHERE connection_id = %s
               ORDER BY created_at""", (connection_id,)).fetchall()
    return {"roots": [{"source_id": str(r["id"]), "kind": r["root_kind"], "id": r["root_ref"], "name": r["name"],
                       "status": r["status"]} for r in rows]}


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


@app.put("/api/connections/{connection_id}/selection")
def set_selection(connection_id: str, body: CreateSources, ident: dict = Depends(admin)) -> dict:
    """Make the watched roots of this Google account exactly ``roots``: add new ones (indexing starts
    immediately) and stop watching the rest. Adds happen first so the connection is never left empty."""
    if not body.roots:
        raise HTTPException(400, "choose My Drive, at least one folder, or a shared drive")
    wanted = {r.id: r for r in body.roots}
    with db.scoped(ident["account_id"]) as conn:
        current = {r["root_ref"]: r for r in conn.execute(
            "SELECT id, root_ref, name FROM drive_index.sources WHERE connection_id = %s", (connection_id,))}
    added = create_drive_sources(connection_id, CreateSources(
        roots=[r for rid, r in wanted.items() if rid not in current], settings=body.settings), ident)["created"]
    removed = []
    for rid, row in current.items():
        if rid not in wanted:
            _delete_source(ident, str(row["id"]), revoke_if_last=False)
            removed.append({"id": str(row["id"]), "name": row["name"]})
    return {"added": added, "removed": removed}


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


@app.get("/api/search/all")
def search_all(q: str, mode: str = "all", source: str | None = None, folder: str | None = None,
               type: str | None = None, ext: str | None = None, modified_after: str | None = None,
               modified_before: str | None = None, added_after: str | None = None, limit: int = 30,
               ident: dict = Depends(reader)) -> dict:
    """Search page: contents + names with source/folder/type/time filters."""
    return _tool(T.search, _ctx(ident), q, mode, source, folder, type, ext, modified_after, modified_before,
                 added_after, limit)


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
    total: int | None = None
    stats: dict[str, Any] | None = None
    snapshot_ref: str | None = None


@app.post("/agent/sources/{source_id}/scan/begin")
def agent_scan_begin(source_id: str, body: ScanBody, ident: dict = Depends(device)) -> dict:
    try:
        ingest.begin_scan(ident["account_id"], ident["device_id"], source_id, body.scan_id, body.full, body.total)
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
