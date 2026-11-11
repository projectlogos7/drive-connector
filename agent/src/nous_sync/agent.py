"""The background agent: catch up, watch, send, heartbeat, obey server commands."""

from __future__ import annotations

import logging
import os
import platform
import socket
import threading
import uuid

from .client import Client, ServerError
from .config import AgentConfig, SourceConf, home
from .engine import SourceEngine
from .ignore import Ignorer
from .state import State
from .watcher import FolderWatcher

log = logging.getLogger(__name__)


class SourceRunner:
    """One watched folder."""

    def __init__(self, agent: "Agent", conf: SourceConf):
        self.agent = agent
        self.conf = conf
        self.engine = SourceEngine(conf.id, conf.path, agent.state, conf.ignore)
        self.watcher: FolderWatcher | None = None
        self.available = os.path.isdir(conf.path)
        self.lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.conf.paused:
            return
        if not os.path.isdir(self.conf.path):
            self.mark_unavailable()
            return
        self.scan(full=self.agent.state.meta(f"scanned:{self.conf.id}") is None)
        self.watch()

    def watch(self) -> None:
        if self.watcher:
            return
        self.watcher = FolderWatcher(self.conf.path, self.agent.cfg.settle_seconds, self._on_path, self._on_move)
        self.watcher.start()
        log.info("watching %s", self.conf.path)

    def stop(self) -> None:
        if self.watcher:
            self.watcher.stop()
            self.watcher = None

    def scan(self, full: bool) -> dict:
        """Catch-up (diff) scan, or a full rescan when the server asks for one / on first run."""
        with self.lock:
            scan_id = uuid.uuid4().hex
            self.agent.client.begin_scan(self.conf.id, scan_id, full)
            stats = self.engine.full_scan(scan_id) if full else self.engine.catch_up()
            self.agent.flush(self.conf.id)
            self.agent.client.complete_scan(self.conf.id, scan_id, full, stats)
            self.agent.state.meta(f"scanned:{self.conf.id}", scan_id)
            log.info("%s scan of %s: %s", "full" if full else "catch-up", self.conf.path, stats)
            return stats

    # ------------------------------------------------------------ volumes (external drives)
    def mark_unavailable(self) -> None:
        if self.available or self.agent.state.meta(f"unavailable:{self.conf.id}") != "1":
            self.stop()
            with self.agent.state:
                self.agent.state.enqueue(self.conf.id, {"op": "unavailable", "reason": "folder or drive not available"})
            self.agent.state.meta(f"unavailable:{self.conf.id}", "1")
            self.available = False
            log.warning("%s is not available (drive unplugged?)", self.conf.path)

    def check_volume(self) -> None:
        present = os.path.isdir(self.conf.path)
        if not present and self.available:
            self.mark_unavailable()
        elif present and not self.available:
            self.available = True
            self.agent.state.meta(f"unavailable:{self.conf.id}", "0")
            with self.agent.state:
                self.agent.state.enqueue(self.conf.id, {"op": "available"})
            log.info("%s is back; catching up", self.conf.path)
            self.scan(full=False)
            self.watch()

    # ------------------------------------------------------------ live events
    def _on_path(self, path: str) -> None:
        rel = self.engine.rel_of(path)
        if rel is None or rel == "":
            if rel == "" and not os.path.isdir(self.conf.path):
                self.mark_unavailable()
            return
        if self.engine.process_path(rel):
            self.agent.wake_sender()

    def _on_move(self, src: str, dest: str) -> None:
        if self.engine.process_move(self.engine.rel_of(src), self.engine.rel_of(dest)):
            self.agent.wake_sender()


class Agent:
    def __init__(self, cfg: AgentConfig | None = None, client: Client | None = None):
        self.cfg = cfg or AgentConfig.load()
        if not self.cfg.token:
            raise SystemExit("not paired: run `nous-sync pair <CODE> --server <URL>` first")
        self.client = client or Client(self.cfg.server, self.cfg.token)
        self.state = State(home() / "state.sqlite3")
        self.runners: dict[str, SourceRunner] = {}
        self.stop_event = threading.Event()
        self.send_event = threading.Event()

    # ------------------------------------------------------------ sending
    def wake_sender(self) -> None:
        self.send_event.set()

    def flush(self, source_id: str | None = None) -> int:
        """Send queued events in batches; keep them queued if the server is unreachable."""
        sent = 0
        ids = [source_id] if source_id else [s.id for s in self.cfg.sources]
        for sid in ids:
            while True:
                batch = self.state.peek(sid, self.cfg.batch_size)
                if not batch:
                    break
                scan_id = batch[0][1]
                same = [b for b in batch if b[1] == scan_id]
                try:
                    self.client.send_events(sid, [b[2] for b in same], scan_id)
                except ServerError as e:
                    if e.status == 409:  # paused: hold events until resumed
                        log.info("source %s paused; holding %d events", sid, len(same))
                        break
                    if e.status == 403 or e.status == 404:
                        log.warning("server rejected events for %s (%s); dropping source", sid, e)
                        self.forget_source(sid)
                        break
                    raise
                self.state.ack([b[0] for b in same])
                sent += len(same)
        return sent

    def _sender_loop(self) -> None:
        while not self.stop_event.is_set():
            self.send_event.wait(timeout=10)
            self.send_event.clear()
            try:
                self.flush()
            except ServerError as e:
                log.warning("send failed, will retry: %s", e)
                self.stop_event.wait(15)

    # ------------------------------------------------------------ heartbeat & commands
    def status(self) -> dict:
        return {"hostname": socket.gethostname(), "os": f"{platform.system()} {platform.release()}",
                "queued_events": self.state.pending(),
                "sources": {sid: {"path": r.conf.path, "available": r.available, "watching": bool(r.watcher),
                                  "queued": self.state.pending(sid)} for sid, r in self.runners.items()}}

    def heartbeat_once(self) -> dict:
        resp = self.client.heartbeat(self.status())
        remote = {s["id"]: s for s in resp.get("sources", [])}
        for sid in [s.id for s in self.cfg.sources if s.id not in remote]:
            log.info("source %s was removed on the server", sid)
            self.forget_source(sid)
        for sid, rs in remote.items():
            conf = self.cfg.source(sid)
            if not conf:
                continue  # belongs to this device but was added elsewhere with a path we don't watch
            runner = self.runners.get(sid)
            if rs.get("ignore") is not None and rs["ignore"] != conf.ignore:
                conf.ignore = rs["ignore"]
                self.cfg.save()
                if runner:
                    runner.engine.ignorer = Ignorer(conf.ignore)
            if rs.get("paused") and not conf.paused:
                conf.paused = True
                self.cfg.save()
                if runner:
                    runner.stop()
            elif not rs.get("paused") and conf.paused:
                conf.paused = False
                self.cfg.save()
                if runner:
                    runner.start()
            cmd = rs.get("command")
            if runner and cmd == "full_rescan":
                runner.scan(full=True)
            elif runner and cmd == "catch_up":
                runner.scan(full=False)
        if resp.get("content_requests"):
            self.serve_content_requests()
        return resp

    def serve_content_requests(self) -> int:
        from .content import extract_file
        n = 0
        for req in self.client.content_requests(20):
            path = req.get("local_path")
            body = {"node_id": req["id"], "content_hash": req.get("content_hash")}
            try:
                if not path or not os.path.isfile(path):
                    raise ValueError("file not found on this machine")
                text, extractor = extract_file(path, req.get("ext"), req.get("mime"), self.cfg.content_max_bytes)
                body.update(text=text, extractor=extractor)
            except Exception as e:
                body.update(error=str(e))
            try:
                self.client.send_content(body)
                n += 1
            except ServerError as e:
                log.info("content upload for %s: %s", req["id"], e)
        return n

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.heartbeat_once()
            except ServerError as e:
                log.warning("heartbeat failed: %s", e)
            except Exception:
                log.exception("heartbeat error")
            for r in list(self.runners.values()):
                try:
                    r.check_volume()
                except Exception:
                    log.exception("volume check failed")
            self.stop_event.wait(self.cfg.heartbeat_seconds)

    # ------------------------------------------------------------ sources
    def add_source(self, path: str, name: str | None = None, ignore: list[str] | None = None) -> SourceConf:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            raise SystemExit(f"not a folder: {path}")
        resp = self.client.add_source(path, name, ignore)
        conf = self.cfg.source(resp["id"]) or SourceConf(id=resp["id"], path=path, name=resp["name"], ignore=ignore or [])
        if not self.cfg.source(conf.id):
            self.cfg.sources.append(conf)
            self.cfg.save()
        return conf

    def forget_source(self, source_id: str) -> None:
        r = self.runners.pop(source_id, None)
        if r:
            r.stop()
        self.cfg.sources = [s for s in self.cfg.sources if s.id != source_id]
        self.cfg.save()
        self.state.drop_source(source_id)

    # ------------------------------------------------------------ main
    def run(self) -> None:
        log.info("Nous Sync running as %s (%d folders)", self.cfg.device_name, len(self.cfg.sources))
        threading.Thread(target=self._sender_loop, name="sender", daemon=True).start()
        try:
            self.heartbeat_once()
        except ServerError as e:
            log.warning("server not reachable yet (%s); working offline, events are queued", e)
        for conf in list(self.cfg.sources):
            runner = SourceRunner(self, conf)
            self.runners[conf.id] = runner
            try:
                runner.start()
            except ServerError as e:
                log.warning("could not start %s now (%s); will catch up later", conf.path, e)
                runner.watch()
        hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()
        try:
            while not self.stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.stop_event.set()
        for r in self.runners.values():
            r.stop()
        try:
            self.flush()
        except ServerError:
            pass
