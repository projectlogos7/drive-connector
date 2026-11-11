"""Which files get their content extracted (per-source settings + global switch)."""

from __future__ import annotations

from ..accounts import merged_settings
from ..config import get_settings

EXTRACTABLE_EXT = {"pdf", "docx", "xlsx", "pptx", "txt", "md", "markdown", "csv", "tsv", "json", "html", "htm",
                   "srt", "vtt", "xml", "yaml", "yml", "rtf", "fountain"}
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def is_extractable(node: dict) -> bool:
    if node.get("mime") in GOOGLE_EXPORTS:
        return True
    if node.get("ext") in EXTRACTABLE_EXT:
        return True
    mime = node.get("mime") or ""
    return mime.startswith("text/")


def should_extract(source: dict, node: dict) -> bool:
    s = get_settings()
    if not s.content_enabled:
        return False
    cfg = merged_settings(source.get("settings")).get("content", {})
    if not cfg.get("enabled"):
        return False
    if not is_extractable(node):
        return False
    exts = cfg.get("extensions") or []
    if exts and (node.get("ext") or "") not in exts and node.get("mime") not in GOOGLE_EXPORTS:
        return False
    max_bytes = cfg.get("max_bytes") or s.content_max_bytes
    if node.get("size") and node["size"] > max_bytes:
        return False
    path = node.get("path") or ""
    for folder in cfg.get("skip_folders") or []:
        f = "/" + folder.strip("/")
        if path == f or path.startswith(f + "/"):
            return False
    return True
