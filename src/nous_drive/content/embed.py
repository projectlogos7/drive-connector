"""Embedding providers. All return 1024-dimensional, L2-normalised vectors (schema: vector(1024)).

* ``fastembed`` (default) — an open-source multilingual model run in-house on CPU via ONNX (no API, no cost).
               Default model: paraphrase-multilingual-MiniLM-L12-v2 (384 dims, ~220 MB, covers English,
               Hindi, Urdu and 50+ languages). Smaller vectors are zero-padded to 1024, which leaves cosine
               similarity unchanged, so any model up to 1024 dims fits the same column.
* ``hash``   — deterministic feature hashing (no model download); keyword-ish similarity only.
* ``voyage`` — optional hosted embeddings (``voyage-3``), only if you choose to pay for them.
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


def _pad(v, dim: int = DIM) -> list[float]:
    v = [float(x) for x in v]
    if len(v) > dim:
        raise ValueError(f"embedding has {len(v)} dims; the column holds at most {dim}")
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v] + [0.0] * (dim - len(v))


class FastEmbedEmbedder:
    """In-house embeddings: an open model run locally with fastembed (ONNX Runtime, CPU)."""

    _models: dict = {}

    def __init__(self, model: str, cache_dir: str | None = None):
        self.model_name = model
        self.cache_dir = cache_dir
        self.name = f"fastembed:{model}"

    def _model(self):
        if self.model_name not in self._models:
            from fastembed import TextEmbedding
            self._models[self.model_name] = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._models[self.model_name]

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        if "e5" in self.model_name.lower():  # e5 models expect role prefixes
            texts = [("query: " if kind == "query" else "passage: ") + t for t in texts]
        return [_pad(v) for v in self._model().embed(texts, batch_size=32)]


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


_warned = False


def get_embedder() -> Embedder:
    global _warned
    s = get_settings()
    if s.embedding_provider == "voyage":
        if not s.voyage_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is not set")
        return VoyageEmbedder(s.voyage_api_key, s.voyage_model)
    if s.embedding_provider == "fastembed":
        try:
            import fastembed  # noqa: F401
            return FastEmbedEmbedder(s.embedding_model, s.embedding_cache_dir)
        except ImportError:
            if not _warned:
                import logging
                logging.getLogger(__name__).warning(
                    "fastembed is not installed (pip install 'nous-drive[content]'); using hash embeddings")
                _warned = True
    return LocalHashEmbedder()


def to_pgvector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
