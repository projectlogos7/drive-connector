"""The Index MCP server: read tools over the index, a few control tools, and change notifications.

Transports:
* stdio — ``nous-drive mcp`` with NOUS_DRIVE_API_KEY set to a session (or admin) key;
* streamable HTTP — ``nous-drive mcp --http``; clients send ``Authorization: Bearer <session key>``.

The caller is always authenticated and every query runs under row-level security for that
account only (FR-31). Adding a Drive or pairing a device is done in the web app (FR-32);
``get_connect_link`` returns the link.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import anyio
from pydantic import Field

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.shared.subscriptions import ResourceUpdated

from .. import db
from ..accounts import AuthError, resolve_key
from ..config import get_settings
from . import tools as T

log = logging.getLogger(__name__)

NOTIFICATIONS_URI = "nous-drive://notifications"

INSTRUCTIONS = """\
Nous Drive Index: a live index of the user's Google Drive and local folders (file and folder structure,
metadata and a change log; with content search and summaries where content indexing is enabled).
Typical flow: list_sources -> get_tree / list_files / search_files -> get_file / describe_file.
For "what's new?" call whats_new with no arguments: it returns what arrived since this session last asked.
Paths are per source and start at '/', e.g. path="/Madarasa/Episode 2" source="Projects".
Large results are paged: pass next_cursor back as cursor."""


class KeyVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            info = await anyio.to_thread.run_sync(lambda: resolve_key(token, ("session", "admin")))
        except AuthError:
            return None
        return AccessToken(token=token, client_id=str(info["key_id"]), scopes=[info["kind"]],
                           subject=str(info["account_id"]))


_stdio_identity: dict | None = None


def _ctx(session: str | None = None) -> T.Ctx:
    tok = get_access_token()
    if tok is not None:
        return T.Ctx(account_id=tok.subject, session_id=session or f"key:{tok.client_id}", actor=f"mcp:{tok.client_id}")
    global _stdio_identity
    if _stdio_identity is None:
        key = os.environ.get("NOUS_DRIVE_API_KEY")
        if not key:
            raise T.ToolError("not authenticated: set NOUS_DRIVE_API_KEY (stdio) or send a bearer token (HTTP)")
        _stdio_identity = resolve_key(key, ("session", "admin"))
    sid = session or os.environ.get("NOUS_DRIVE_SESSION") or f"key:{_stdio_identity['key_id']}"
    return T.Ctx(account_id=str(_stdio_identity["account_id"]), session_id=sid,
                 actor=f"mcp:{_stdio_identity['key_id']}")


def _run(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except T.ToolError as e:
        return {"error": str(e)}


def build_server(http: bool = False) -> MCPServer:
    s = get_settings()
    kwargs: dict[str, Any] = {}
    if http:
        base = s.public_base_url.rstrip("/")
        kwargs = dict(token_verifier=KeyVerifier(),
                      auth=AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp",
                                        validate_token_resource=False))

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        task = asyncio.create_task(_listen_notifications(server))
        try:
            yield {}
        finally:
            task.cancel()

    mcp = MCPServer("nous-drive-index", instructions=INSTRUCTIONS, version="1.0.0", lifespan=lifespan, **kwargs)

    # ---------------------------------------------------------------- read tools
    @mcp.tool(description="Sources on the account (Drive roots and local folders) with type, root, status, "
                          "last sync time, file and folder counts.")
    def list_sources() -> dict:
        return _run(T.list_sources, _ctx())

    @mcp.tool(description="Sync health per source: live or not, lag, last error, queue and dead letters, "
                          "push channel (Drive) or device online status (local).")
    def get_sync_status(source: Annotated[str | None, Field(description="Source id or name; all if omitted")] = None) -> dict:
        return _run(T.get_sync_status, _ctx(), source)

    @mcp.tool(description="Folder tree from a path or node id, with a depth limit and optional file listing. "
                          "Returns an indented text view plus structured nodes.")
    def get_tree(path: Annotated[str | None, Field(description="Folder path within the source, e.g. /Madarasa/Episode 2")] = None,
                 source: Annotated[str | None, Field(description="Source id or name")] = None,
                 node_id: str | None = None,
                 depth: Annotated[int, Field(ge=1, le=10)] = 2,
                 include_files: bool = False,
                 max_entries: Annotated[int, Field(ge=1, le=1000)] = 300) -> dict:
        return _run(T.get_tree, _ctx(), path, node_id, source, depth, include_files, max_entries)

    @mcp.tool(description="Files in a folder or source, filterable by type (video, audio, image, document, "
                          "spreadsheet, presentation, pdf, text, archive, project, other), extension, size, "
                          "modified/added date and status. Paged.")
    def list_files(path: str | None = None, source: str | None = None, node_id: str | None = None,
                   recursive: bool = False, type: str | None = None,
                   ext: Annotated[str | None, Field(description="One or more extensions, comma separated, e.g. 'mov,mp4'")] = None,
                   min_size: Annotated[int | None, Field(description="Bytes, e.g. 2147483648 for 2 GB")] = None,
                   max_size: int | None = None,
                   modified_after: Annotated[str | None, Field(description="ISO time, 'today', 'yesterday', or relative like '7d'")] = None,
                   modified_before: str | None = None, added_after: str | None = None,
                   status: Literal["active", "trashed", "deleted", "removed", "unavailable", "any"] = "active",
                   include_folders: bool = False,
                   sort: Literal["name", "modified", "size", "added", "path"] = "name",
                   limit: Annotated[int, Field(ge=1, le=200)] = 50, cursor: str | None = None) -> dict:
        return _run(T.list_files, _ctx(), path, node_id, source, recursive, type, ext, min_size, max_size,
                    modified_after, modified_before, added_after, status, include_folders, sort, limit, cursor)

    @mcp.tool(description="Search files and folders by name and path (all words must match). With content "
                          "indexing on, mode='content' (hybrid), 'keyword' or 'semantic' searches inside files.")
    def search_files(query: str, source: str | None = None, type: str | None = None, ext: str | None = None,
                     include_folders: bool = True,
                     status: Literal["active", "trashed", "deleted", "removed", "unavailable", "any"] = "active",
                     mode: Literal["name", "content", "keyword", "semantic", "hybrid"] = "name",
                     limit: Annotated[int, Field(ge=1, le=200)] = 30, cursor: str | None = None) -> dict:
        return _run(T.search_files, _ctx(), query, source, type, ext, include_folders, status, mode, limit, cursor)

    @mcp.tool(description="Search inside file contents and by name at once. Filter to one source, only inside a folder "
                          "(path within the source), by type/extension, or by modified/added time. Results say whether they "
                          "matched by content or name and include a snippet and summary.")
    def search(query: str,
               mode: Literal["all", "content", "name"] = "all",
               source: Annotated[str | None, Field(description="Source id or name")] = None,
               folder: Annotated[str | None, Field(description="Only inside this folder path (needs source)")] = None,
               type: str | None = None, ext: str | None = None,
               modified_after: Annotated[str | None, Field(description="ISO time, 'today', 'yesterday' or relative like '7d'")] = None,
               modified_before: str | None = None, added_after: str | None = None,
               limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
        return _run(T.search, _ctx(), query, mode, source, folder, type, ext, modified_after, modified_before,
                    added_after, limit)

    @mcp.tool(description="Full metadata for one file or folder: path, links, owner, sizes, device, shortcut "
                          "target, other parents, content summary (if extracted) and its own change history.")
    def get_file(node_id: str | None = None, path: str | None = None, source: str | None = None) -> dict:
        return _run(T.get_file, _ctx(), node_id, path, source)

    @mcp.tool(description="Files and folders added since a time, or — with no 'since' — since this session "
                          "last asked (so asking twice returns only what arrived in between). Grouped by "
                          "source and folder.")
    def whats_new(since: Annotated[str | None, Field(description="ISO time, 'today', 'yesterday' or relative like '24h'; omit to use this session's checkpoint")] = None,
                  source: str | None = None,
                  folder: Annotated[str | None, Field(description="Only under this folder path")] = None,
                  include_modified: bool = False, include_folders: bool = True,
                  peek: Annotated[bool, Field(description="Do not move this session's checkpoint")] = False,
                  session: Annotated[str | None, Field(description="Checkpoint name; defaults to this client's session")] = None,
                  limit: Annotated[int, Field(ge=1, le=200)] = 100) -> dict:
        return _run(T.whats_new, _ctx(session), since, source, folder, include_modified, include_folders, peek, limit)

    @mcp.tool(description="The change log for a time range (added, modified, renamed, moved, trashed, restored, "
                          "deleted, removed, unavailable), filterable by source, folder and change type. Paged.")
    def list_changes(since: str | None = "24h", until: str | None = None, source: str | None = None,
                     folder: str | None = None,
                     types: list[Literal["added", "modified", "renamed", "moved", "trashed", "restored",
                                         "deleted", "removed", "unavailable", "available"]] | None = None,
                     limit: Annotated[int, Field(ge=1, le=200)] = 100, cursor: str | None = None) -> dict:
        return _run(T.list_changes, _ctx(), since, until, source, folder, types, limit, cursor)

    @mcp.tool(description="Plain-language description of a file built from metadata (type, size, where it "
                          "sits, siblings, who added it, earlier versions with similar names), plus a content "
                          "summary when content indexing has processed it.")
    def describe_file(node_id: str | None = None, path: str | None = None, source: str | None = None) -> dict:
        return _run(T.describe_file, _ctx(), node_id, path, source)

    @mcp.tool(description="Recent notifications (batched new-file announcements and sync alerts).")
    def list_notifications(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
        return _run(_list_notifications, _ctx(), limit)

    # ---------------------------------------------------------------- control tools
    @mcp.tool(description="Start a full reconcile of one source (Drive) or a full rescan (local agent). "
                          "Also retries dead-lettered jobs.")
    def resync_source(source: str) -> dict:
        return _run(T.resync_source, _ctx(), source)

    @mcp.tool(description="Stop watching a source (its index stays).")
    def pause_source(source: str) -> dict:
        return _run(T.pause_source, _ctx(), source)

    @mcp.tool(description="Restart watching a paused source; it catches up on what changed meanwhile.")
    def resume_source(source: str) -> dict:
        return _run(T.resume_source, _ctx(), source)

    @mcp.tool(description="Link to connect a Google Drive or pair a computer. These need a browser sign-in or a "
                          "device step, so they happen in the web app, not through MCP.")
    def get_connect_link(kind: Literal["gdrive", "device"] = "gdrive") -> dict:
        return _run(T.get_connect_link, _ctx(), kind)

    @mcp.resource(NOTIFICATIONS_URI, name="notifications", mime_type="application/json",
                  description="Latest notifications; subscribe to be told when new files arrive.")
    def notifications_resource() -> str:
        return json.dumps(_list_notifications(_ctx(), 20), default=str)

    return mcp


def _list_notifications(ctx: T.Ctx, limit: int = 20) -> dict:
    with db.scoped(ctx.account_id) as conn:
        rows = conn.execute(
            "SELECT id, kind, title, body, payload, created_at FROM drive_index.notifications ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return {"notifications": [{**r, "created_at": r["created_at"].isoformat()} for r in rows]}


async def _listen_notifications(server: MCPServer) -> None:
    """Forward new notifications (pg NOTIFY) to MCP clients as resource-updated events."""
    import psycopg
    while True:
        try:
            aconn = await psycopg.AsyncConnection.connect(get_settings().database_url, autocommit=True)
            async with aconn:
                await aconn.execute("LISTEN drive_index_notifications")
                async for _ in aconn.notifies():
                    await server._subscriptions.publish(ResourceUpdated(uri=NOTIFICATIONS_URI))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # reconnect on DB hiccups
            log.warning("notification listener error: %s; retrying", e)
            await asyncio.sleep(5)


def main(http: bool = False, host: str = "127.0.0.1", port: int = 8090) -> None:
    server = build_server(http=http)
    if http:
        server.run("streamable-http", host=host, port=port)
    else:
        server.run("stdio")
