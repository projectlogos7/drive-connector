"""Text extraction per file type. Pure functions: bytes in, text out."""

from __future__ import annotations

import csv
import html
import io
import json
import re


class UnsupportedType(Exception):
    pass


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", s)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "") for p in reader.pages)


def _docx(data: bytes) -> str:
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


def _xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"## {ws.title}")
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v) for v in row]
            if any(vals):
                out.append(", ".join(vals))
    return "\n".join(out)


def _pptx(data: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"## Slide {i}")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text:
                out.append(shape.text_frame.text)
    return "\n".join(out)


def _rtf(data: bytes) -> str:
    s = _decode(data)
    s = re.sub(r"\\par[d]?", "\n", s)
    s = re.sub(r"\\'[0-9a-f]{2}", "", s)
    s = re.sub(r"\\[a-z]+-?\d* ?", "", s)
    return s.replace("{", "").replace("}", "")


def extract_text(data: bytes, mime: str | None, ext: str | None) -> tuple[str, str]:
    """Return (text, extractor_name). Raises UnsupportedType."""
    ext = (ext or "").lower()
    mime = mime or ""
    if ext == "pdf" or mime == "application/pdf":
        return _pdf(data), "pypdf"
    if ext == "docx" or mime.endswith("wordprocessingml.document"):
        return _docx(data), "python-docx"
    if ext == "xlsx" or mime.endswith("spreadsheetml.sheet"):
        return _xlsx(data), "openpyxl"
    if ext == "pptx" or mime.endswith("presentationml.presentation"):
        return _pptx(data), "python-pptx"
    if ext in ("html", "htm") or mime == "text/html":
        return _html_to_text(_decode(data)), "html"
    if ext == "rtf" or mime == "application/rtf":
        return _rtf(data), "rtf"
    if ext == "json" or mime == "application/json":
        try:
            return json.dumps(json.loads(_decode(data)), indent=1, ensure_ascii=False), "json"
        except ValueError:
            return _decode(data), "text"
    if ext in ("csv", "tsv") or mime in ("text/csv", "text/tab-separated-values"):
        text = _decode(data)
        delim = "\t" if ext == "tsv" else ","
        rows = csv.reader(io.StringIO(text), delimiter=delim)
        return "\n".join(", ".join(r) for r in rows), "csv"
    if mime.startswith("text/") or ext in ("txt", "md", "markdown", "srt", "vtt", "xml", "yaml", "yml", "fountain"):
        return _decode(data), "text"
    raise UnsupportedType(f"no extractor for mime={mime!r} ext={ext!r}")
