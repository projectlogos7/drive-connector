"""Content search (Phase 2): hybrid of Postgres full-text rank and vector similarity."""

from __future__ import annotations

from psycopg import Connection

from .embed import get_embedder, to_pgvector


def search_content(conn: Connection, query: str, limit: int = 20, source_ids: list[str] | None = None,
                   mode: str = "hybrid") -> list[dict]:
    """Return nodes ranked by relevance with the best-matching chunk as a snippet.

    ``mode``: 'keyword' (full text), 'semantic' (vectors) or 'hybrid' (reciprocal-rank fusion of both).
    """
    keyword: list[dict] = []
    semantic: list[dict] = []
    src_filter = "AND c.source_id = ANY(%(sources)s)" if source_ids else ""
    params = {"q": query, "lim": limit * 4, "sources": source_ids}
    if mode in ("keyword", "hybrid"):
        keyword = conn.execute(
            f"""SELECT c.id AS chunk_id, c.node_id, c.text,
                       ts_rank(c.tsv, websearch_to_tsquery('simple', %(q)s)) AS score
                FROM drive_index.chunks c JOIN drive_index.nodes n ON n.id = c.node_id
                WHERE c.tsv @@ websearch_to_tsquery('simple', %(q)s) AND n.status = 'active'
                  AND c.content_version = n.content_version {src_filter}
                ORDER BY score DESC LIMIT %(lim)s""",
            params,
        ).fetchall()
    if mode in ("semantic", "hybrid"):
        embedder = get_embedder()
        vec = embedder.embed([query], "query")[0]
        semantic = conn.execute(
            f"""SELECT c.id AS chunk_id, c.node_id, c.text, 1 - (e.embedding <=> %(v)s::vector) AS score
                FROM drive_index.embeddings e JOIN drive_index.chunks c ON c.id = e.chunk_id
                JOIN drive_index.nodes n ON n.id = c.node_id
                WHERE n.status = 'active' AND c.content_version = n.content_version AND e.model = %(model)s
                  {src_filter}
                ORDER BY e.embedding <=> %(v)s::vector LIMIT %(lim)s""",
            dict(params, v=to_pgvector(vec), model=embedder.name),
        ).fetchall()
    fused: dict[str, dict] = {}
    for lst in (keyword, semantic):
        for rank, r in enumerate(lst):
            nid = str(r["node_id"])
            entry = fused.setdefault(nid, {"node_id": nid, "score": 0.0, "snippet": r["text"], "best": 0.0})
            entry["score"] += 1.0 / (60 + rank)
            if r["score"] and r["score"] > entry["best"]:
                entry["best"], entry["snippet"] = r["score"], r["text"]
    ranked = sorted(fused.values(), key=lambda e: e["score"], reverse=True)[:limit]
    for e in ranked:
        e["snippet"] = _snippet(e["snippet"], query)
        e.pop("best", None)
    return ranked


def _snippet(text: str, query: str, width: int = 280) -> str:
    low = text.lower()
    pos = -1
    for w in query.lower().split():
        pos = low.find(w)
        if pos >= 0:
            break
    start = max(0, pos - width // 3) if pos >= 0 else 0
    s = text[start:start + width].replace("\n", " ")
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")
