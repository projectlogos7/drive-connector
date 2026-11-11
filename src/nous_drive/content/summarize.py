"""Short per-file summaries. Claude when configured; an extractive fallback otherwise."""

from __future__ import annotations

import logging
import os
import re

from ..config import get_settings

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 400_000  # ~100k tokens; longer texts are summarised from the head plus a sampled middle/end

SYSTEM = (
    "You write short factual summaries of files for a personal knowledge base index. "
    "Write 2-4 sentences in English: what the file is, its main subject, and anything that identifies it "
    "(project, episode, people, dates). No preamble, no markdown."
)


def _fit(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_INPUT_CHARS:
        return text, False
    third = MAX_INPUT_CHARS // 3
    mid = len(text) // 2
    return (text[:third] + "\n\n[…]\n\n" + text[mid - third // 2: mid + third // 2]
            + "\n\n[…]\n\n" + text[-third:]), True


def extractive_summary(text: str, max_chars: int = 400) -> str:
    sentences = re.split(r"(?<=[.!?।۔])\s+", re.sub(r"\s+", " ", text).strip())
    out = ""
    for s in sentences:
        if len(out) + len(s) > max_chars:
            break
        out = f"{out} {s}".strip()
    return out or text[:max_chars]


def summarize(text: str, name: str, path: str) -> tuple[str, str]:
    """Return (summary, model_name)."""
    s = get_settings()
    has_creds = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if s.summary_provider != "claude" or not has_creds:
        return extractive_summary(text), "extractive"
    import anthropic

    body, sampled = _fit(text)
    note = " (long file: excerpts from beginning, middle and end)" if sampled else ""
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=s.summary_model,
            max_tokens=1024,
            system=SYSTEM,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": f"File: {path}{note}\n\n<file_text>\n{body}\n</file_text>"}],
        )
    except anthropic.RateLimitError:
        raise
    except anthropic.APIStatusError as e:
        log.warning("summary failed (%s); using extractive summary", e.status_code)
        return extractive_summary(text), "extractive"
    if resp.stop_reason == "refusal":
        return extractive_summary(text), "extractive"
    summary = "".join(b.text for b in resp.content if b.type == "text").strip()
    return summary or extractive_summary(text), resp.model
