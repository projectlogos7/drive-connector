"""The real MCP server binary over stdio, driven by the MCP client SDK."""

import asyncio
import json
import os
import shutil
import sys

import pytest
from mcp import Client, StdioServerParameters

from .conftest import apply, make_source


def _exe():
    return shutil.which("nous-drive", path=os.path.dirname(sys.executable))


@pytest.mark.skipif(not _exe(), reason="nous-drive entry point not installed")
def test_stdio_server_tools(account):
    src = make_source(account["id"], name="Projects")
    apply(account["id"], src, [dict(provider_id="a", name="Madarasa", parent_provider_id="ROOT", kind="folder"),
                               dict(provider_id="x", name="script.docx", parent_provider_id="a", size=10)])
    env = {**os.environ, "NOUS_DRIVE_API_KEY": account["admin_key"], "NOUS_DRIVE_SESSION": "stdio-test"}

    async def run():
        params = StdioServerParameters(command=_exe(), args=["mcp"], env=env)
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {"list_sources", "get_tree", "whats_new", "describe_file", "resync_source"} <= tools
            res = await client.call_tool("get_tree", {"source": "Projects", "path": "/", "include_files": True})
            data = res.structured_content or json.loads(res.content[0].text)
            assert "script.docx" in data["text"]
            new = await client.call_tool("whats_new", {})
            data = new.structured_content or json.loads(new.content[0].text)
            assert data["total"] == 2
            again = await client.call_tool("whats_new", {})
            data = again.structured_content or json.loads(again.content[0].text)
            assert data["total"] == 0
            err = await client.call_tool("get_file", {"path": "/missing", "source": "Projects"})
            data = err.structured_content or json.loads(err.content[0].text)
            assert "error" in data

    asyncio.run(run())


def test_stdio_requires_key(account, monkeypatch):
    from nous_drive.mcp import server as S
    monkeypatch.delenv("NOUS_DRIVE_API_KEY", raising=False)
    monkeypatch.setattr(S, "_stdio_identity", None)
    with pytest.raises(S.T.ToolError):
        S._ctx()
