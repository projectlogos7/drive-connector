"""The normalized change event both connectors emit, and other shared shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Op = Literal["upsert", "delete", "remove", "unavailable", "available"]
Kind = Literal["file", "folder", "shortcut"]


class NodeEvent(BaseModel):
    """One observed state of a file/folder in a source, or a source-level signal.

    ``upsert`` carries the full current metadata; the indexer diffs it against
    the stored node to decide whether it was added, modified, renamed, moved,
    trashed or restored. Replaying the same event is a no-op.
    """

    op: Op = "upsert"
    provider_id: str | None = None  # None only for source-level ops (unavailable/available)
    kind: Kind = "file"
    name: str | None = None
    parent_provider_id: str | None = None
    alt_parent_provider_ids: list[str] = Field(default_factory=list)
    mime: str | None = None
    size: int | None = None
    checksum: str | None = None
    created_at_src: datetime | None = None
    modified_at_src: datetime | None = None
    owner: str | None = None
    last_modified_by: str | None = None
    web_link: str | None = None
    local_path: str | None = None
    fs_id: str | None = None
    shortcut_target: str | None = None
    trashed: bool = False
    reason: str | None = None  # for remove/unavailable
    content_hash: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    scan_id: str | None = None
    idempotency_key: str | None = None


class EventBatch(BaseModel):
    source_id: str
    events: list[NodeEvent]
    scan_id: str | None = None
