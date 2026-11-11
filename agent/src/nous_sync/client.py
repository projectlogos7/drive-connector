"""HTTP client for the Nous Drive ingest API (TLS in production), with retries."""

from __future__ import annotations

import logging
import time

import httpx

from . import __version__

log = logging.getLogger(__name__)


class ServerError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status


class Client:
    def __init__(self, server: str, token: str | None = None, transport: httpx.BaseTransport | None = None):
        self.server = server.rstrip("/")
        self.token = token
        self.http = httpx.Client(base_url=self.server, timeout=60, transport=transport,
                                 headers={"User-Agent": f"nous-sync/{__version__}"})

    def _req(self, method: str, path: str, retries: int = 3, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        for attempt in range(retries + 1):
            try:
                r = self.http.request(method, path, headers=headers, **kw)
            except httpx.TransportError as e:
                if attempt >= retries:
                    raise ServerError(0, f"network: {e}") from e
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code >= 500 and attempt < retries:
                time.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                raise ServerError(r.status_code, str(detail))
            return r.json()
        raise ServerError(0, "unreachable")

    def pair(self, code: str, machine_id: str, hostname: str, os_name: str, device_name: str | None) -> dict:
        return self._req("POST", "/agent/pair", json={"code": code, "machine_id": machine_id, "hostname": hostname,
                                                       "os": os_name, "agent_version": __version__,
                                                       "device_name": device_name})

    def heartbeat(self, status: dict) -> dict:
        return self._req("POST", "/agent/heartbeat", json={"agent_version": __version__, "status": status}, retries=0)

    def add_source(self, path: str, name: str | None, ignore: list[str] | None) -> dict:
        return self._req("POST", "/agent/sources", json={"path": path, "name": name, "ignore": ignore or []})

    def remove_source(self, source_id: str) -> dict:
        return self._req("DELETE", f"/agent/sources/{source_id}")

    def begin_scan(self, source_id: str, scan_id: str, full: bool) -> dict:
        return self._req("POST", f"/agent/sources/{source_id}/scan/begin", json={"scan_id": scan_id, "full": full})

    def send_events(self, source_id: str, events: list[dict], scan_id: str | None = None) -> dict:
        return self._req("POST", f"/agent/sources/{source_id}/events", json={"events": events, "scan_id": scan_id})

    def complete_scan(self, source_id: str, scan_id: str, full: bool, stats: dict) -> dict:
        return self._req("POST", f"/agent/sources/{source_id}/scan/complete",
                         json={"scan_id": scan_id, "full": full, "stats": stats})

    def content_requests(self, limit: int = 20) -> list[dict]:
        return self._req("GET", "/agent/content/requests", params={"limit": limit})["requests"]

    def send_content(self, body: dict) -> dict:
        return self._req("POST", "/agent/content", json=body)
