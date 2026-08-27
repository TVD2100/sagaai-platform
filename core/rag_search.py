"""
core.rag_search - semantic search over a RAG base (local index).

Search flow:

  1. validate the base and read its embedding provider + model;
  2. vectorize the query via the same provider embeddings API
     (core.rag_embeddings; BYOK);
  3. run cosine similarity over the local SQLite index
     (core.rag_index.search_similar);
  4. return top-k hits with metadata and scores.

The query is vectorized remotely; no user data is uploaded beyond the query
itself (all document/chunk data stays on disk).

No streamlit imports.
"""
from core import rag
from core.rag_embeddings import embed_query, get_yandex_embedding_credentials
from core.rag_index import search_similar


DEFAULT_TOP_K = 5


class RagSearchError(Exception):
    """Raised when search cannot run (missing base/credential/etc)."""


def search_base(slug: str, query: str, top_k: int = DEFAULT_TOP_K,
                min_score: float = 0.0) -> list:
    """Semantic search in base *slug* for *query*.

    Returns a list of hits sorted by descending score:
    ``{"text", "source", "chunk_index", "score"}``. The dimension mismatch
    between the query embedding and the stored vectors is handled by
    ``search_similar`` (mismatched vectors are skipped).

    Raises RagSearchError when the base is missing or not ready, or when the
    embedding provider credentials are unavailable.
    """
    base = rag.get_base(slug)
    if not base:
        raise RagSearchError("RAG base does not exist")
    if base.get("status") not in ("ready",):
        raise RagSearchError("RAG base is not indexed yet")
    if not query or not str(query).strip():
        return []

    # Queries are vectorized with the query-side model (text-search-query),
    # while the base stores document-side vectors (text-search-doc). The
    # stored embedding_model only describes the indexed side, so it is not
    # reused for the query.
    try:
        vector = embed_query(str(query))
    except Exception as e:
        raise RagSearchError(str(e)) from e
    if not vector:
        return []

    results = search_similar(rag.index_db_path(slug), vector, top_k=top_k)
    if min_score:
        results = [r for r in results if r.get("score", 0.0) >= min_score]
    return results


def build_search_context(results: list, max_chars: int = 4000) -> str:
    """Format search hits into a context block for prompt injection.

    Each hit becomes ``--- [source (chunk N), score S]\ntext``. The block is
    truncated to *max_chars* (hits are dropped from the tail until it fits).
    """
    if not results:
        return ""
    parts = []
    total = 0
    for hit in results:
        score = hit.get("score", 0.0)
        if isinstance(score, float):
            score_text = f"{score:.3f}"
        else:
            try:
                score_text = f"{float(score):.3f}"
            except (TypeError, ValueError):
                score_text = "0"
        header = f"--- [{hit.get('source') or 'file'} (chunk {hit.get('chunk_index', 0)}), score {score_text}]"
        block = header + "\n" + (hit.get("text") or "")
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block) + 2
    return "\n\n".join(parts)


def chat_context(slug: str, query: str, top_k: int = DEFAULT_TOP_K,
                 max_chars: int = 4000) -> str:
    """Return a ready-to-inject RAG context block for the chat request.

    ``""`` when the base is missing/empty or the query is blank. Never raises:
    failures are swallowed so chat continues with an empty context.
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
