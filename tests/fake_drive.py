"""An in-memory fake of the parts of the Google Drive v3 API the connector uses."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

FOLDER = "application/vnd.google-apps.folder"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.changes: list[dict] = []  # index i -> change visible from token str(i+1)
        self.drives: dict[str, str] = {}
        self.channels: dict[str, dict] = {}
        self.fail_next: list[int] = []  # status codes to return before succeeding
        self.requests: list[str] = []
        self.root_id = "MYROOT"
        self.files[self.root_id] = {"id": self.root_id, "name": "My Drive", "mimeType": FOLDER, "parents": []}

    # ---------------------------------------------------------------- mutations
    def add(self, fid, name, parent, folder=False, size=10, md5=None, drive_id=None, record=True, **extra):
        f = {"id": fid, "name": name, "mimeType": FOLDER if folder else extra.pop("mimeType", "application/octet-stream"),
             "parents": [parent] if parent else [], "modifiedTime": now_iso(), "createdTime": now_iso(),
             "trashed": False, "webViewLink": f"https://drive.google.com/{fid}",
             "owners": [{"emailAddress": "nous@example.org"}], "version": "1"}
        if not folder and size is not None:
            f["size"] = str(size)
            f["md5Checksum"] = md5 or f"md5-{fid}-{size}"
        if drive_id:
            f["driveId"] = drive_id
        f.update(extra)
        self.files[fid] = f
        if record:
            self._change(fid)
        return f

    def update(self, fid, record=True, **fields):
        f = self.files[fid]
        if "parent" in fields:
            f["parents"] = [fields.pop("parent")]
        f.update(fields)
        if "_content" in fields and "md5Checksum" in f:
            import hashlib
            f["md5Checksum"] = hashlib.md5(fields["_content"]).hexdigest()
        f["modifiedTime"] = now_iso()
        f["version"] = str(int(f.get("version", "1")) + 1)
        if record:
            self._change(fid)

    def delete(self, fid):
        self.files.pop(fid, None)
        self.changes.append({"changeType": "file", "fileId": fid, "removed": True, "time": now_iso()})

    def _change(self, fid):
        self.changes.append({"changeType": "file", "fileId": fid, "removed": False, "time": now_iso(),
                             "file": {k: v for k, v in self.files[fid].items() if not k.startswith("_")}})

    # ---------------------------------------------------------------- transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if self.fail_next:
            code = self.fail_next.pop(0)
            body = {"error": {"code": code, "message": "slow down",
                              "errors": [{"reason": "userRateLimitExceeded" if code == 403 else "rateLimitExceeded"}]}}
            return httpx.Response(code, json=body)
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path.split("/drive/v3", 1)[-1]
        if path == "/about":
            return httpx.Response(200, json={"user": {"emailAddress": "nous@example.org", "permissionId": "p1"}})
        if path == "/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.changes) + 1)})
        if path == "/changes" and request.method == "GET":
            start = int(q["pageToken"]) - 1
            size = int(q.get("pageSize", 1000))
            sel = self.changes[start:start + size]
            if q.get("driveId"):
                sel = [c for c in sel if c.get("removed") or c["file"].get("driveId") == q["driveId"]]
            body = {"changes": sel}
            if start + size < len(self.changes):
                body["nextPageToken"] = str(start + size + 1)
            else:
                body["newStartPageToken"] = str(len(self.changes) + 1)
            return httpx.Response(200, json=body)
        if path == "/changes/watch":
            body = json.loads(request.content)
            self.channels[body["id"]] = body
            return httpx.Response(200, json={"id": body["id"], "resourceId": "res-" + body["id"][:8],
                                             "expiration": body["expiration"]})
        if path == "/channels/stop":
            body = json.loads(request.content)
            self.channels.pop(body["id"], None)
            return httpx.Response(204)
        if path == "/drives":
            return httpx.Response(200, json={"drives": [{"id": k, "name": v} for k, v in self.drives.items()]})
        if path == "/files" and request.method == "GET":
            return self._list(q)
        m = re.fullmatch(r"/files/([^/]+)(/export)?", path)
        if m:
            fid = self.root_id if m.group(1) == "root" else m.group(1)
            f = self.files.get(fid)
            if not f:
                return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})
            if m.group(2) or q.get("alt") == "media":
                return httpx.Response(200, content=f.get("_content", b"hello world"))
            return httpx.Response(200, json=f)
        return httpx.Response(404, json={"error": {"code": 404, "message": f"no route {path}"}})

    def _list(self, q: dict) -> httpx.Response:
        items = [f for f in self.files.values() if f["id"] != self.root_id]
        query = q.get("q", "")
        parents = re.findall(r"'([^']+)' in parents", query)
        if parents:
            items = [f for f in items if set(f.get("parents", [])) & set(parents)]
        if "mimeType = '" in query:
            mt = re.search(r"mimeType = '([^']+)'", query).group(1)
            items = [f for f in items if f["mimeType"] == mt]
        if "trashed = false" in query:
            items = [f for f in items if not f.get("trashed")]
        if q.get("driveId"):
            items = [f for f in items if f.get("driveId") == q["driveId"]]
        items.sort(key=lambda f: f["id"])
        start = int(q.get("pageToken") or 0)
        size = int(q.get("pageSize", 100))
        body = {"files": [{k: v for k, v in f.items() if not k.startswith("_")} for f in items[start:start + size]]}
        if start + size < len(items):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)


class StaticTokens:
    def token(self):
        return "test-token"

    def refresh(self):
        return "test-token"


def install(monkeypatch, fake: FakeDrive):
    from nous_drive.connectors.gdrive import auth
    from nous_drive.connectors.gdrive.client import DriveClient

    def factory(account_id, connection_id):
        return DriveClient(StaticTokens(), http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                           base_url="https://drive.test/drive/v3", sleep=lambda s: None)

    monkeypatch.setattr(auth, "client_factory", factory)
    monkeypatch.setattr(auth, "has_content_scope", lambda a, c: True)
