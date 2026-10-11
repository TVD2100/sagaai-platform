"""
core.rag_search - search over a RAG knowledge base (BM25 / vector / hybrid).

Every base carries a ``search_mode`` in its manifest:

  * ``bm25``   - own lexical BM25 index (core.rag_bm25). No embeddings and
    no API keys required; the DEFAULT mode, so every base is searchable
    out of the box (legacy bases get one lazy BM25 rebuild on first query);
  * ``vector`` - semantic cosine similarity over stored embeddings
    (core.rag_index.search_similar); the query is vectorized remotely via
    ``core.rag_embeddings.embed_query`` (BYOK) and the base must be indexed
    with an embedding model;
  * ``hybrid`` - both lists are fused with Reciprocal Rank Fusion
    (RRF, k=60). When embeddings are unavailable the result gracefully
    falls back to the pure BM25 ranking.

Search flow:

  1. validate the base and resolve its search mode (missing field -> bm25);
  2. run the mode-specific retrieval;
  3. return top-k hits with metadata and scores (hits carry ``mode``).

Only the query itself is sent to the embedding provider in vector mode;
all document/chunk data stays on disk.

No streamlit imports.
"""
from core import rag
from core.rag_embeddings import embed_query
from core.rag_index import fetch_chunks, search_similar
from core import rag_bm25


DEFAULT_TOP_K = 5
MAX_FETCH_CHUNKS = 20

# Reciprocal Rank Fusion constant (standard RRF damping, k=60).
RRF_K = 60


class RagSearchError(Exception):
    """Raised when search cannot run (missing base/credential/etc)."""


def get_search_mode(base: dict) -> str:
    """Return the effective search mode of a base manifest dict.

    Missing or unknown values fall back to ``bm25`` (the default mode of
    every base), so manifests written by older versions keep working.
    """
    mode = str((base or {}).get("search_mode") or "").strip().lower()
    return mode if mode in rag.SEARCH_MODES else rag.DEFAULT_SEARCH_MODE


def _resolve_top_k(top_k) -> int:
    """Coerce *top_k* to a positive int (default on junk)."""
    try:
        return max(1, int(top_k))
    except (TypeError, ValueError):
        return DEFAULT_TOP_K


def _apply_min_score(hits: list, min_score) -> list:
    """Filter hits by a numeric floor (no-op for non-positive floors)."""
    try:
        floor = float(min_score or 0.0)
    except (TypeError, ValueError):
        floor = 0.0
    if not floor:
        return hits
    return [h for h in hits if float(h.get("score") or 0.0) >= floor]


def _bm25_hits(slug: str, query: str, top_k: int, min_score=0.0) -> list:
    """Run the own BM25 retrieval over the base index."""
    hits = rag_bm25.search(
        rag.index_db_path(slug), query, top_k=top_k, min_score=min_score,
    )
    for hit in hits:
        hit["mode"] = "bm25"
    return hits


def _vector_hits(slug: str, query: str, top_k: int, min_score=0.0) -> list:
    """Run the semantic retrieval; raises RagSearchError without credentials.

    Queries are vectorized with the query-side model (text-search-query),
    while the base stores document-side vectors (text-search-doc). The
    stored embedding_model only describes the indexed side, so it is not
    reused for the query.
    """
    try:
        vector = embed_query(str(query))
    except Exception as e:
        raise RagSearchError(str(e)) from e
    if not vector:
        return []
    results = search_similar(rag.index_db_path(slug), vector, top_k=top_k)
    results = _apply_min_score(results, min_score)
    for hit in results:
        hit["mode"] = "vector"
    return results


def _rrf_fuse(bm_hits: list, vec_hits: list, top_k: int,
              min_score=0.0) -> list:
    """Fuse BM25 and vector rankings with Reciprocal Rank Fusion.

    Each ranking contributes ``1 / (RRF_K + rank)`` (rank starts at 1) to
    its chunks; chunks present in both lists get the sum. The returned hits
    keep the BM25 text metadata and carry ``mode: "hybrid"``.
    """
    fused = {}
    for rank, hit in enumerate(bm_hits, start=1):
        cid = hit.get("chunk_id")
        if cid is None:
            continue
        entry = fused.setdefault(cid, {"hit": dict(hit)})
        entry["hit"].setdefault("text", hit.get("text"))
        entry["score"] = entry.get("score", 0.0) + 1.0 / (RRF_K + rank)
    for rank, hit in enumerate(vec_hits, start=1):
        cid = hit.get("chunk_id")
        if cid is None:
            continue
        entry = fused.setdefault(cid, {"hit": dict(hit)})
        entry["score"] = entry.get("score", 0.0) + 1.0 / (RRF_K + rank)
    ranked = sorted(
        fused.values(), key=lambda e: -float(e.get("score") or 0.0)
    )[:top_k]
    out = []
    for entry in ranked:
        hit = dict(entry["hit"])
        hit["score"] = float(entry.get("score") or 0.0)
        hit["mode"] = "hybrid"
        out.append(hit)
    return _apply_min_score(out, min_score)


def search_base(slug: str, query: str, top_k: int = DEFAULT_TOP_K,
                min_score: float = 0.0) -> list:
    """Search base *slug* for *query* using the base's configured mode.

    Modes: ``bm25`` (default; own lexical index, no credentials needed),
    ``vector`` (Yandex embeddings cosine similarity) and ``hybrid``
    (RRF fusion; falls back to BM25 when embeddings are unavailable).

    Returns a list of hits sorted by descending score:
    ``{"chunk_id", "text", "source", "chunk_index", "score", "mode"}``.

    Raises RagSearchError when the base is missing or not ready, when the
    vector mode is selected but the embedding provider credentials are
    unavailable, or when the provider request fails.
    """
    base = rag.get_base(slug, with_stats=False)
    if not base:
        raise RagSearchError("RAG base does not exist")
    if base.get("status") not in ("ready",):
        raise RagSearchError("RAG base is not indexed yet")
    if not query or not str(query).strip():
        return []

    q = str(query).strip()
    limit = _resolve_top_k(top_k)
    mode = get_search_mode(base)

    if mode == "vector":
        return _vector_hits(slug, q, limit, min_score)

    if mode == "hybrid":
        bm = _bm25_hits(slug, q, limit)
        try:
            vec = _vector_hits(slug, q, limit)
        except Exception:
            vec = []
        if not vec:
            # Graceful degradation: no embeddings/credentials -> pure BM25.
            return _apply_min_score(bm, min_score)
        return _rrf_fuse(bm, vec, limit, min_score)

    # Default: bm25.
    return _bm25_hits(slug, q, limit, min_score)


def _clean_chunk_values(raw_values) -> list:
    """Normalise user-supplied chunk ids/positions for get_chunks.

    Accepts a scalar or an iterable, keeps only non-negative integers,
    removes duplicates (order preserved) and truncates the result to
    ``MAX_FETCH_CHUNKS``. Non-integer junk is dropped silently.
    """
    if raw_values is None:
        return []
    if not isinstance(raw_values, (list, tuple, set, frozenset)):
        raw_values = [raw_values]
    cleaned = []
    for raw in raw_values:
        try:
            num = int(raw)
        except (TypeError, ValueError):
            continue
        if num >= 0 and num not in cleaned:
            cleaned.append(num)
        if len(cleaned) >= MAX_FETCH_CHUNKS:
            break
    return cleaned


def get_chunks(slug: str, chunk_ids=None, source: str = "",
               chunk_indices=None) -> dict:
    """Fetch specific chunks of base *slug* (no embeddings involved).

    Supports the same two addressing modes as ``core.rag_index.fetch_chunks``:
    global chunk ids (e.g. taken from search hits) or 0-based chunk positions
    inside one source file (useful for pulling the neighbours of a hit).

    Returns ``{"chunks": [...], "missing": [...]}``; chunk dicts carry
    ``chunk_id``, ``text``, ``source``, ``chunk_index`` and ``has_embedding``.

    Raises RagSearchError when the base is missing or not indexed yet.

    At most ``MAX_FETCH_CHUNKS`` (20) ids/positions are processed per call;
    extra values are dropped. Non-integer values are ignored.
    """
    base = rag.get_base(slug, with_stats=False)
    if not base:
        raise RagSearchError("RAG base does not exist")
    if base.get("status") not in ("ready",):
        raise RagSearchError("RAG base is not indexed yet")
    ids = _clean_chunk_values(chunk_ids)
    if ids:
        return fetch_chunks(rag.index_db_path(slug), chunk_ids=ids)
    src = str(source or "")
    indices = _clean_chunk_values(chunk_indices) if src else []
    if not indices:
        return {"chunks": [], "missing": []}
    return fetch_chunks(rag.index_db_path(slug), source=src,
                        chunk_indices=indices)


def build_search_context(results: list, max_chars: int = 4000) -> str:
    """Format search hits into a context block for prompt injection.

    Each hit becomes ``--- [source (chunk N), score S, mode M]\ntext``. The
    block is truncated to *max_chars* (hits are dropped from the tail until
    it fits). The mode part is omitted when a hit carries no ``mode`` key.
    """
    if not results:
        return ""
    parts = []
    total = 0
    for hit in results:
        score = hit.get("score")
        score_part = ""
        if score is not None:
            if isinstance(score, float):
                score_text = f"{score:.3f}"
            else:
                try:
                    score_text = f"{float(score):.3f}"
                except (TypeError, ValueError):
                    score_text = "0"
            score_part = f", score {score_text}"
        mode = str(hit.get("mode") or "").strip()
        mode_part = f", {mode}" if mode else ""
        chunk_id = hit.get("chunk_id")
        id_part = f", id {chunk_id}" if chunk_id is not None else ""
        header = (
            f"--- [{hit.get('source') or 'file'}"
            f" (chunk {hit.get('chunk_index', 0)}{id_part}){score_part}{mode_part}]"
        )
        block = header + "\n" + (hit.get("text") or "")
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block) + 2
    return "\n\n".join(parts)


def chat_context(slug: str, query: str, top_k: int = DEFAULT_TOP_K,
                 max_chars: int = 4000) -> str:
    """Return a ready-to-inject RAG context block for the chat request.

    Uses the base's configured search mode (BM25 by default). ``""`` when
    the base is missing/empty or the query is blank. Never raises: failures
    are swallowed so chat continues with an empty context.
    """
    try:
        hits = search_base(slug, query, top_k=top_k)
    except Exception:
        return ""
    ctx = build_search_context(hits, max_chars=max_chars)
    if not ctx.strip():
        return ""
    return (
        "**Материалы из базы знаний (RAG):**\n"
        f"{ctx}\n"
        "**Используй эти материалы при ответе и ссылайся на них.**"
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
