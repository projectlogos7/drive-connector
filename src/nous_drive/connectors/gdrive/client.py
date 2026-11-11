"""Thin Google Drive v3 client: OAuth token refresh, client-side rate limiting, retries with backoff."""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from ...config import get_settings

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."

FILE_FIELDS = (
    "id,name,mimeType,parents,size,md5Checksum,createdTime,modifiedTime,"
    "owners(emailAddress,displayName),lastModifyingUser(emailAddress,displayName),"
    "webViewLink,trashed,explicitlyTrashed,shortcutDetails(targetId,targetMimeType),"
    "driveId,version,headRevisionId,fileExtension,originalFilename"
)
LIGHT_FILE_FIELDS = "id,name,mimeType,parents,size,md5Checksum,modifiedTime,trashed,driveId,version,shortcutDetails(targetId,targetMimeType)"

SCOPE_METADATA = "https://www.googleapis.com/auth/drive.metadata.readonly"
SCOPE_READONLY = "https://www.googleapis.com/auth/drive.readonly"


class DriveError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(f"Drive API {status}: {message}")
        self.status = status
        self.reason = reason


class AuthExpired(DriveError):
    pass


class _RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                time.sleep(self.next_at - now)
                now = self.next_at
            self.next_at = now + self.interval


_limiter: _RateLimiter | None = None


def _get_limiter() -> _RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = _RateLimiter(get_settings().drive_max_requests_per_second)
    return _limiter


class TokenSource:
    """Holds a refresh token and mints access tokens as needed."""

    def __init__(self, refresh_token: str, access_token: str | None = None, expires_at: datetime | None = None,
                 http: httpx.Client | None = None, on_refresh=None):
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.expires_at = expires_at
        self.http = http or httpx.Client(timeout=30)
        self.on_refresh = on_refresh

    def token(self) -> str:
        if self.access_token and self.expires_at and self.expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
            return self.access_token
        return self.refresh()

    def refresh(self) -> str:
        s = get_settings()
        r = self.http.post(s.google_token_url, data={
            "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        })
        if r.status_code != 200:
            raise AuthExpired(r.status_code, f"token refresh failed: {r.text[:300]}", "invalid_grant")
        data = r.json()
        self.access_token = data["access_token"]
        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
        if self.on_refresh:
            self.on_refresh(self.access_token, self.expires_at)
        return self.access_token


class DriveClient:
    RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}

    def __init__(self, tokens: TokenSource, http: httpx.Client | None = None, base_url: str | None = None,
                 max_retries: int = 8, sleep=time.sleep):
        self.tokens = tokens
        self.http = http or httpx.Client(timeout=60)
        self.base = (base_url or get_settings().drive_api_base).rstrip("/")
        self.max_retries = max_retries
        self.sleep = sleep

    # ------------------------------------------------------------------ transport
    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                raw: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        attempt = 0
        refreshed = False
        while True:
            _get_limiter().wait()
            headers = {"Authorization": f"Bearer {self.tokens.token()}"}
            try:
                r = self.http.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as e:
                if attempt >= self.max_retries:
                    raise DriveError(0, f"network error: {e}") from e
                self._backoff(attempt)
                attempt += 1
                continue
            if r.status_code < 300:
                if raw:
                    return r
                return r.json() if r.content else {}
            reason = None
            try:
                err = r.json().get("error", {})
                errors = err.get("errors") or []
                reason = errors[0].get("reason") if errors else err.get("status")
                message = err.get("message", r.text[:300])
            except Exception:
                message = r.text[:300]
            if r.status_code == 401 and not refreshed:
                self.tokens.refresh()
                refreshed = True
                continue
            if r.status_code == 401:
                raise AuthExpired(401, message, reason)
            retryable = r.status_code in (429, 500, 502, 503, 504) or (
                r.status_code == 403 and reason in self.RETRY_REASONS)
            if retryable and attempt < self.max_retries:
                ra = r.headers.get("Retry-After")
                self._backoff(attempt, float(ra) if ra and ra.isdigit() else None)
                attempt += 1
                continue
            raise DriveError(r.status_code, message, reason)

    def _backoff(self, attempt: int, at_least: float | None = None) -> None:
        delay = min(64.0, (2 ** attempt) + random.random())
        if at_least:
            delay = max(delay, at_least)
        log.warning("Drive API backoff %.1fs (attempt %d)", delay, attempt + 1)
        self.sleep(delay)

    # ------------------------------------------------------------------ API
    def about(self) -> dict:
        return self.request("GET", "/about", params={"fields": "user(emailAddress,displayName,permissionId)"})

    def get_file(self, file_id: str, fields: str = FILE_FIELDS) -> dict:
        return self.request("GET", f"/files/{file_id}", params={"fields": fields, "supportsAllDrives": "true"})

    def list_files(self, *, q: str | None = None, page_token: str | None = None, drive_id: str | None = None,
                   corpora: str | None = None, fields: str = FILE_FIELDS, page_size: int | None = None) -> dict:
        params: dict[str, Any] = {
            "fields": f"nextPageToken,incompleteSearch,files({fields})",
            "pageSize": page_size or get_settings().drive_page_size,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        if drive_id:
            params["driveId"] = drive_id
            params["corpora"] = "drive"
        elif corpora:
            params["corpora"] = corpora
        return self.request("GET", "/files", params=params)

    def iter_files(self, **kw) -> Iterator[list[dict]]:
        token = None
        while True:
            page = self.list_files(page_token=token, **kw)
            yield page.get("files", [])
            token = page.get("nextPageToken")
            if not token:
                break

    def list_drives(self) -> list[dict]:
        out, token = [], None
        while True:
            params = {"pageSize": 100, "fields": "nextPageToken,drives(id,name)"}
            if token:
                params["pageToken"] = token
            page = self.request("GET", "/drives", params=params)
            out.extend(page.get("drives", []))
            token = page.get("nextPageToken")
            if not token:
                return out

    def start_page_token(self, drive_id: str | None = None) -> str:
        params: dict[str, Any] = {"supportsAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        return self.request("GET", "/changes/startPageToken", params=params)["startPageToken"]

    def list_changes(self, page_token: str, drive_id: str | None = None, fields: str = FILE_FIELDS) -> dict:
        params: dict[str, Any] = {
            "pageToken": page_token,
            "pageSize": 1000,
            "includeRemoved": "true",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": f"nextPageToken,newStartPageToken,changes(changeType,removed,fileId,time,file({fields}))",
        }
        if drive_id:
            params["driveId"] = drive_id
        return self.request("GET", "/changes", params=params)

    def watch_changes(self, page_token: str, channel_id: str, address: str, token: str,
                      expiration_ms: int, drive_id: str | None = None) -> dict:
        params: dict[str, Any] = {"pageToken": page_token, "supportsAllDrives": "true",
                                  "includeItemsFromAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        body = {"id": channel_id, "type": "web_hook", "address": address, "token": token,
                "expiration": str(expiration_ms)}
        return self.request("POST", "/changes/watch", params=params, json=body)

    def stop_channel(self, channel_id: str, resource_id: str) -> None:
        try:
            self.request("POST", "/channels/stop", json={"id": channel_id, "resourceId": resource_id})
        except DriveError as e:
            if e.status != 404:
                raise

    # Phase 2: content
    def download(self, file_id: str) -> bytes:
        return self.request("GET", f"/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                            raw=True).content

    def export(self, file_id: str, mime: str) -> bytes:
        return self.request("GET", f"/files/{file_id}/export", params={"mimeType": mime}, raw=True).content
