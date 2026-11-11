"""Ignore rules: system/temp files by default plus user patterns per source (FR-19)."""

from __future__ import annotations

import fnmatch

DEFAULT_IGNORES = [
    ".DS_Store", "._*", ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100",
    "Thumbs.db", "desktop.ini", "ehthumbs.db", "$RECYCLE.BIN", "System Volume Information",
    "~$*", ".~lock.*#", "*.tmp", "*.temp", "*.swp", "*.swx", "*~", "*.crdownload", "*.part", "*.download",
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".cache", "Cache", "Caches", ".Trash",
    "*.lck", ".nous-sync",
]


class Ignorer:
    def __init__(self, patterns: list[str] | None = None, use_defaults: bool = True):
        pats = (DEFAULT_IGNORES if use_defaults else []) + list(patterns or [])
        self.name_patterns = [p for p in pats if "/" not in p]
        self.path_patterns = [p.strip("/") for p in pats if "/" in p]

    def ignored(self, rel: str) -> bool:
        """``rel`` is a POSIX path relative to the source root."""
        parts = rel.split("/")
        for part in parts:
            for p in self.name_patterns:
                if fnmatch.fnmatchcase(part, p):
                    return True
        for p in self.path_patterns:
            if fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(rel, p + "/*"):
                return True
        return False
