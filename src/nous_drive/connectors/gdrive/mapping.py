"""Map Drive API file resources to normalized NodeEvents."""

from __future__ import annotations

from datetime import datetime

from ...models import NodeEvent
from .client import FOLDER_MIME, SHORTCUT_MIME

SHARED_WITH_ME = "__shared_with_me__"


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _who(u: dict | None) -> str | None:
    if not u:
        return None
    return u.get("emailAddress") or u.get("displayName")


def file_to_event(f: dict, source: dict, scan_id: str | None = None, occurred_at: datetime | None = None,
                  parent_override: str | None = None) -> NodeEvent:
    mime = f.get("mimeType")
    kind = "folder" if mime == FOLDER_MIME else "shortcut" if mime == SHORTCUT_MIME else "file"
    parents = f.get("parents") or []
    # My Drive's root_ref is the real id of the user's root folder; a shared drive's root is its drive id,
    # so parents line up with root_ref without aliasing.
    parent = parent_override or (parents[0] if parents else None)
    if source["root_kind"] == "my_drive" and not parents:
        parent = SHARED_WITH_ME  # shared with the user but not added to My Drive
    alt = list(parents[1:])  # legacy multi-parent files
    sc = f.get("shortcutDetails") or {}
    owners = f.get("owners") or []
    size = f.get("size")
    extra = {k: f[k] for k in ("version", "headRevisionId", "driveId", "originalFilename") if f.get(k)}
    if sc.get("targetMimeType"):
        extra["target_mime"] = sc["targetMimeType"]
    if f.get("explicitlyTrashed") is not None:
        extra["explicitly_trashed"] = f["explicitlyTrashed"]
    return NodeEvent(
        op="upsert",
        provider_id=f["id"],
        kind=kind,
        name=f.get("name") or "(untitled)",
        parent_provider_id=parent,
        alt_parent_provider_ids=alt,
        mime=mime,
        size=int(size) if size is not None else None,
        checksum=f.get("md5Checksum"),
        created_at_src=_ts(f.get("createdTime")),
        modified_at_src=_ts(f.get("modifiedTime")),
        owner=_who(owners[0]) if owners else None,
        last_modified_by=_who(f.get("lastModifyingUser")),
        web_link=f.get("webViewLink"),
        shortcut_target=sc.get("targetId"),
        trashed=bool(f.get("trashed")),
        extra=extra,
        occurred_at=occurred_at or _ts(f.get("modifiedTime")),
        scan_id=scan_id,
    )
