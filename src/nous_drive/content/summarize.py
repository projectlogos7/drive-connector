"""Short per-file summaries.

Providers (NOUS_DRIVE_SUMMARY_PROVIDER):
* ``extractive`` (default) — the opening sentences of the text. Free, no model, no network.
* ``ollama``  — an open model on your own machine/server via Ollama (in-house, no per-use cost).
* ``claude``  — Anthropic API, opt-in. Defaults to the cheapest model (claude-haiku-5-5) and sends at most
                SUMMARY_MAX_INPUT_CHARS characters per file (~6k tokens by default).
Any failure falls back to the extractive summary, so indexing never stops on a summary error.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

SYSTEM = (
    "You write short factual summaries of files for a personal knowledge base index. "
    "Write 2-4 sentences in English: what the file is, its main subject, and anything that identifies it "
    "(project, episode, people, dates). No preamble, no markdown."
)


def _fit(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head, a slice of the middle and the tail when a file is longer than the budget."""
    if len(text) <= limit:
        return text, False
    third = limit // 3
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


def _prompt(text: str, path: str, limit: int) -> str:
    body, sampled = _fit(text, limit)
    note = " (long file: excerpts from beginning, middle and end)" if sampled else ""
    return f"File: {path}{note}\n\n<file_text>\n{body}\n</file_text>"


def _ollama(text: str, path: str) -> tuple[str, str]:
    s = get_settings()
    r = httpx.post(f"{s.ollama_url.rstrip('/')}/api/chat", timeout=300, json={
        "model": s.ollama_model, "stream": False, "options": {"temperature": 0.2, "num_predict": 300},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": _prompt(text, path, s.summary_max_input_chars)}],
    })
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), f"ollama:{s.ollama_model}"


def _claude(text: str, path: str) -> tuple[str, str]:
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic()
    kwargs = dict(model=s.summary_model, max_tokens=1024, system=SYSTEM,
                  output_config={"effort": "low"},
                  messages=[{"role": "user", "content": _prompt(text, path, s.summary_max_input_chars)}])
    if any(f in s.summary_model for f in ("opus", "sonnet", "fable")):
        # Server-side refusal fallback exists for these models (not for Haiku).
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("summary declined")
    summary = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not summary:
        raise RuntimeError("empty summary")
    return summary, resp.model


def summarize(text: str, name: str, path: str) -> tuple[str, str]:
    """Return (summary, model_name)."""
    provider = get_settings().summary_provider
    try:
        if provider == "ollama":
            return _ollama(text, path)
        if provider == "claude":
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                raise RuntimeError("SUMMARY_PROVIDER=claude but ANTHROPIC_API_KEY is not set")
            return _claude(text, path)
    except Exception as e:
        log.warning("%s summary failed for %s (%s); using extractive summary", provider, path, e)
    return extractive_summary(text), "extractive"
