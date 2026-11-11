"""Agent configuration and data directory (~/.nous-sync or NOUS_SYNC_HOME)."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def home() -> Path:
    p = Path(os.environ.get("NOUS_SYNC_HOME") or Path.home() / ".nous-sync")
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class SourceConf:
    id: str
    path: str
    name: str
    ignore: list[str] = field(default_factory=list)
    paused: bool = False


@dataclass
class AgentConfig:
    server: str = ""
    token: str = ""
    device_id: str = ""
    device_name: str = ""
    machine_id: str = ""
    sources: list[SourceConf] = field(default_factory=list)
    settle_seconds: float = 2.0
    heartbeat_seconds: float = 30.0
    batch_size: int = 500
    content_max_bytes: int = 50 * 1024 * 1024

    @classmethod
    def load(cls) -> "AgentConfig":
        f = home() / "config.json"
        if not f.exists():
            cfg = cls(machine_id=machine_id())
            cfg.save()
            return cfg
        data = json.loads(f.read_text())
        data["sources"] = [SourceConf(**s) for s in data.get("sources", [])]
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if not cfg.machine_id:
            cfg.machine_id = machine_id()
            cfg.save()
        return cfg

    def save(self) -> None:
        f = home() / "config.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, f)
        try:
            os.chmod(f, 0o600)  # holds the device token
        except OSError:
            pass

    def source(self, source_id: str) -> SourceConf | None:
        return next((s for s in self.sources if s.id == source_id), None)


def machine_id() -> str:
    """A stable per-machine id so re-pairing (reinstall) keeps the same device and name."""
    system = platform.system()
    try:
        if system == "Darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                                 text=True, timeout=5).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return "mac:" + line.split('"')[-2]
        elif system == "Linux":
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if os.path.exists(p):
                    return "linux:" + Path(p).read_text().strip()
        elif system == "Windows":
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                return "win:" + winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        pass
    f = home() / "machine-id"
    if not f.exists():
        f.write_text("gen:" + uuid.uuid4().hex)
    return f.read_text().strip()
