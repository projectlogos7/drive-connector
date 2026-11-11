"""Phase 2 on the agent: extract text locally for files the server asks about (contents never leave
the machine in Phase 1; in Phase 2 only extracted text is sent, and only for requested files)."""

from __future__ import annotations

import os


def extract_file(path: str, ext: str | None, mime: str | None, max_bytes: int) -> tuple[str, str]:
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"unsupported: file larger than {max_bytes} bytes")
    with open(path, "rb") as f:
        data = f.read()
    try:
        from nous_drive.content.extract import UnsupportedType, extract_text
    except ImportError:
        from .extract import UnsupportedType, extract_text
    try:
        return extract_text(data, mime, ext)
    except UnsupportedType as e:
        raise ValueError(f"unsupported: {e}") from e
