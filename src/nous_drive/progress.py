"""One progress view per source for the web app and MCP: phase, percent, done/total and a label."""

from __future__ import annotations

PHASE_LABELS = {
    "queued": "Waiting to start",
    "starting": "Starting",
    "counting": "Counting files",
    "listing": "Indexing",
    "catching_up": "Catching up on changes made during the scan",
    "live": "Up to date — watching for changes",
    "paused": "Paused",
    "error": "Needs attention",
    "waiting_for_device": "Waiting for the computer to scan",
}


def view(source: dict, cursor: dict | None, pending_crawl: bool = False) -> dict:
    sp = (cursor or {}).get("scan_progress") or {}
    status = source["status"]
    running = sp.get("state") == "running"
    out: dict = {"running": running, "percent": None, "done": None, "total": None}
    if status == "paused":
        out.update(phase="paused", percent=None)
    elif status == "error":
        out.update(phase="error")
    elif running:
        phase = sp.get("phase") or "listing"
        if source["type"] == "gdrive":
            done = (sp.get("files") or 0) + (sp.get("folders") or 0)
            total = sp.get("total")
            if phase == "counting":
                out.update(done=sp.get("counted") or 0, total=None)
            else:
                out.update(done=done, total=total)
        else:
            out.update(done=sp.get("processed") or 0, total=sp.get("total"))
        if phase in ("listing",) and out["total"]:
            out["percent"] = min(99, int(100 * out["done"] / max(out["total"], 1)))
        elif phase == "catching_up":
            out["percent"] = 99
        elif phase in ("counting", "starting"):
            out["percent"] = 0
        out["phase"] = phase
    elif status in ("connecting", "initial_scan"):
        out.update(phase="waiting_for_device" if source["type"] == "local" and not pending_crawl else "queued",
                   percent=0)
    else:
        out.update(phase="live", percent=100)
    label = PHASE_LABELS.get(out["phase"], out["phase"])
    if out["phase"] == "counting" and out["done"]:
        label += f" — {out['done']:,} found so far"
    elif out["phase"] == "listing" and out["total"]:
        label += f" — {out['done']:,} of {out['total']:,}"
    elif out["phase"] == "listing" and out["done"]:
        label += f" — {out['done']:,} so far"
    out["label"] = label
    if sp.get("started_at"):
        out["started_at"] = sp["started_at"]
    if sp.get("finished_at"):
        out["finished_at"] = sp["finished_at"]
    return out
