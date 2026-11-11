"""Start-on-login integration: launchd (macOS), systemd --user (Linux), Task Scheduler (Windows)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "network.nous.sync"


def _cmd() -> list[str]:
    exe = shutil.which("nous-sync")
    return [exe, "run"] if exe else [sys.executable, "-m", "nous_sync.cli", "run"]


def install() -> str:
    system = platform.system()
    cmd = _cmd()
    if system == "Darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{a}</string>" for a in cmd)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardErrorPath</key><string>{Path.home()}/.nous-sync/launchd.err</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)
        return f"installed launch agent {plist}"
    if system == "Linux":
        unit = Path.home() / ".config/systemd/user/nous-sync.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=Nous Sync agent
After=network-online.target

[Service]
ExecStart={' '.join(cmd)}
Restart=always
RestartSec=10
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=default.target
""")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "nous-sync.service"], check=False)
        return f"installed systemd user unit {unit}"
    if system == "Windows":
        exe = cmd[0].replace("python.exe", "pythonw.exe") if cmd[0].endswith("python.exe") else cmd[0]
        tr = " ".join([f'"{exe}"'] + cmd[1:])
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "NousSync", "/TR", tr], check=False)
        return "installed scheduled task NousSync (runs at logon)"
    return f"unsupported platform {system}; run `nous-sync run` from your own startup mechanism"


def uninstall() -> str:
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
        return "removed launch agent"
    if system == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", "nous-sync.service"], check=False)
        (Path.home() / ".config/systemd/user/nous-sync.service").unlink(missing_ok=True)
        return "removed systemd user unit"
    if system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "NousSync"], check=False)
        return "removed scheduled task"
    return "nothing to do"


def restart() -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    elif system == "Linux":
        subprocess.run(["systemctl", "--user", "try-restart", "nous-sync.service"], capture_output=True)
