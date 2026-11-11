from __future__ import annotations

import os
import posixpath
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def join_path(parent_path: str, name: str) -> str:
    if parent_path in ("", "/"):
        return "/" + name
    return parent_path + "/" + name


def ext_of(name: str, kind: str) -> str | None:
    if kind != "file":
        return None
    base, ext = os.path.splitext(name)
    if not ext or not base:
        return None
    return ext[1:].lower()


def normalize_user_path(p: str) -> str:
    p = (p or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    p = posixpath.normpath(p)
    return "/" if p in (".", "//") else p


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def human_size(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


_CATEGORY_BY_MIME_PREFIX = (("video/", "video"), ("audio/", "audio"), ("image/", "image"), ("text/", "text"))
_CATEGORY_BY_MIME = {
    "application/pdf": "pdf",
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation",
    "application/vnd.google-apps.folder": "folder",
    "application/vnd.google-apps.shortcut": "shortcut",
    "application/msword": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.ms-excel": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "application/vnd.ms-powerpoint": "presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "presentation",
    "application/zip": "archive",
}
_CATEGORY_BY_EXT = {
    **{e: "video" for e in ("mov", "mp4", "m4v", "mkv", "avi", "mxf", "braw", "r3d", "webm", "mts", "prores")},
    **{e: "audio" for e in ("wav", "mp3", "aac", "m4a", "flac", "aif", "aiff", "ogg")},
    **{e: "image" for e in ("jpg", "jpeg", "png", "gif", "tif", "tiff", "heic", "webp", "psd", "raw", "cr3", "nef", "arw", "dng", "svg")},
    **{e: "document" for e in ("doc", "docx", "rtf", "odt", "pages", "fdx", "fountain")},
    **{e: "spreadsheet" for e in ("xls", "xlsx", "csv", "ods", "numbers", "tsv")},
    **{e: "presentation" for e in ("ppt", "pptx", "key", "odp")},
    **{e: "text" for e in ("txt", "md", "markdown", "json", "srt", "vtt", "xml", "yaml", "yml", "html", "htm")},
    "pdf": "pdf",
    **{e: "archive" for e in ("zip", "rar", "7z", "tar", "gz", "tgz")},
    **{e: "project" for e in ("prproj", "fcpxml", "drp", "aep", "fcpbundle", "veg", "edl", "xml")},
}


def category_of(kind: str, mime: str | None, ext: str | None) -> str:
    if kind == "folder":
        return "folder"
    if kind == "shortcut":
        return "shortcut"
    if ext and ext in _CATEGORY_BY_EXT:
        return _CATEGORY_BY_EXT[ext]
    if mime:
        if mime in _CATEGORY_BY_MIME:
            return _CATEGORY_BY_MIME[mime]
        for prefix, cat in _CATEGORY_BY_MIME_PREFIX:
            if mime.startswith(prefix):
                return cat
        if mime.startswith("application/vnd.google-apps."):
            return "google_" + mime.rsplit(".", 1)[-1]
    return "other"
