"""Embedding providers. All return 1024-dimensional, L2-normalised vectors (schema: vector(1024)).

* ``local``  — deterministic feature hashing (no network, no cost); good for keyword-ish similarity
               and for development/tests.
* ``voyage`` — Voyage AI embeddings (``voyage-3``, 1024 dims), recommended for semantic search.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx

from ..config import get_settings

DIM = 1024
_WORD = re.compile(r"\w+", re.UNICODE)


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]: ...


class LocalHashEmbedder:
    name = "local-hash-1024"

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * DIM
        words = [w.lower() for w in _WORD.findall(text)]
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        for f in feats:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % DIM
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.name = f"voyage:{model}"

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            r = httpx.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"input": texts[i:i + 64], "model": self.model, "input_type": kind,
                      "output_dimension": DIM},
                timeout=60,
            )
            r.raise_for_status()
            out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
        return out


def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "voyage":
        if not s.voyage_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is not set")
        return VoyageEmbedder(s.voyage_api_key, s.voyage_model)
    return LocalHashEmbedder()


def to_pgvector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
