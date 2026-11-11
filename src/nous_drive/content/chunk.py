"""Split text into overlapping chunks along paragraph/sentence boundaries."""

from __future__ import annotations

import re


def chunk_text(text: str, max_chars: int = 1800, overlap: int = 200) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= max_chars:
            pieces.append(p)
            continue
        sentences = re.split(r"(?<=[.!?।۔])\s+", p)
        buf = ""
        for s in sentences:
            while len(s) > max_chars:
                pieces.append(s[:max_chars])
                s = s[max_chars - overlap:]
            if len(buf) + len(s) + 1 > max_chars and buf:
                pieces.append(buf)
                buf = buf[-overlap:] + " " + s if overlap else s
            else:
                buf = f"{buf} {s}".strip()
        if buf:
            pieces.append(buf)
    chunks: list[str] = []
    buf = ""
    for p in pieces:
        if len(buf) + len(p) + 2 > max_chars and buf:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = (tail + "\n\n" + p) if tail and len(tail) + len(p) + 2 <= max_chars else p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks
