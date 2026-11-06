"""Knowledge-base listing and retrieval."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.prompt_guard import sanitize_tool_result_content

from .deps import ToolMixin


class RagTools(ToolMixin):
    """Knowledge-base listing and retrieval."""

    def _rag_slug(self, slug: str) -> str:
        """Normalize a knowledge-base slug to lowercase."""
        return str(slug or "").strip().lower()

    def _rag_access_allowed(self, slug: str) -> bool:
        """True when the active orchestrator may use the given base.

        DevAgent (default) may use every base. Other orchestrators may only
        use bases explicitly assigned in their config under ``rag_bases``;
        this list is injected into their system prompt as a metadata block.
        """
        orch_slug = getattr(self, "_orchestrator_slug", "dev_agent") or "dev_agent"
        if orch_slug == "dev_agent":
            return True
        try:
            from core.orchestrators import get_orchestrator_rag_bases
            return self._rag_slug(slug) in get_orchestrator_rag_bases(orch_slug)
        except Exception:
            return False

    def list_rag_bases(self) -> Dict[str, Any]:
        """List knowledge bases the active orchestrator may use.

        DevAgent sees all bases. Other orchestrators see only the bases
        assigned to them in their settings. Each entry includes slug, name,
        status, ``active`` (the base is searchable right now: False only
        for vector-mode bases without provider credentials; bm25/hybrid
        stay active) and a short description. The summary ``text`` block
        is sanitized and fenced as untrusted data.
        """
        try:
            from core.rag import list_bases_with_activity
            # with_stats=False: the listing tool only returns manifest
            # metadata; attaching index_stats would open every SQLite
            # index and stall on large bases.
            all_bases = list_bases_with_activity(with_stats=False)
        except Exception as e:
            return {"ok": False, "error": f"RAG base list failed: {e}"}
        orch_slug = getattr(self, "_orchestrator_slug", "dev_agent") or "dev_agent"
        if orch_slug != "dev_agent":
            try:
                from core.orchestrators import get_orchestrator_rag_bases
                allowed = set(get_orchestrator_rag_bases(orch_slug))
            except Exception:
                allowed = set()
            all_bases = [b for b in all_bases if self._rag_slug(b.get("slug")) in allowed]
        bases = [
            {
                "slug": str(b.get("slug") or ""),
                "name": str(b.get("name") or ""),
                "status": str(b.get("status") or "draft"),
                "active": bool(b.get("active", True)),
                "search_mode": str(b.get("search_mode") or "").strip().lower() or "bm25",
                "description": str(b.get("description") or "")[:200],
            }
            for b in all_bases
        ]
        lines = []
        for b in bases:
            flag = "" if b["active"] else " [INACTIVE: provider API keys required]"
            lines.append(f"- {b['slug']} — {b['name']} (status: {b['status']}, search: {b['search_mode']}){flag}")
        text = "\n".join(lines)
        safe = sanitize_tool_result_content(text, source="rag_base_list")
        return {"ok": True, "count": len(bases), "bases": bases, "text": safe}

    def rag_search(self, slug: Optional[str] = None, query: Optional[str] = None,
                   top_k: int = 5, min_score: float = 0.0,
                   **kwargs: Any) -> Dict[str, Any]:
        """Search a RAG knowledge base by slug.

        Runs the search in the base's configured mode: ``bm25`` (own lexical
        index, the default - no API keys needed), ``vector`` (Yandex
        embeddings, BYOK) or ``hybrid`` (RRF fusion; degrades to bm25 when
        embeddings are unavailable). Returns matching chunks (source,
        chunk_index, score, mode, text) and a ready-to-use context block.
        The context text is sanitized and fenced as untrusted data so
        document contents cannot inject instructions.

        Access control: the active orchestrator may only search bases that
        are assigned to it (DevAgent may search all bases). Searching a base
        that is not assigned returns an access-denied error even if the slug
        is known.

        Args:
            slug: knowledge-base slug (see the Storage page).
            query: natural-language search query.
            top_k: number of chunks to return (default 5).
            min_score: minimum relevance score to keep a hit (default 0.0).
        Returns:
            {"ok": True, "slug", "query", "count", "hits", "text"}
            or {"ok": False, "error": ...}.
            Unexpected argument names are rejected with a ``suggestion``
            containing the exact expected signature.
        """
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            return {
                "ok": False,
                "error": (
                    f"rag_search got unexpected argument(s): {unknown}. "
                    "Only 'slug', 'query', 'top_k' and 'min_score' are supported."
                ),
                "suggestion": _RAG_SEARCH_USAGE,
            }
        norm_slug = self._rag_slug(slug)
        if not norm_slug:
            return {
                "ok": False,
                "error": "Missing required argument 'slug'.",
                "suggestion": _RAG_SEARCH_USAGE,
            }
        if not query or not str(query).strip():
            return {
                "ok": False,
                "error": "Missing required argument 'query'.",
                "suggestion": _RAG_SEARCH_USAGE,
            }
        if not self._rag_access_allowed(norm_slug):
            return {
                "ok": False,
                "error": f"Access denied: knowledge base '{norm_slug}' is not assigned to this orchestrator.",
            }
        try:
            from core.rag_search import RagSearchError, build_search_context, search_base
            hits = search_base(
                norm_slug, str(query).strip(),
                top_k=int(top_k or 5), min_score=float(min_score or 0.0),
            )
        except RagSearchError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"RAG search failed: {e}"}
        if not hits:
            return {
                "ok": True, "slug": norm_slug, "query": str(query).strip(),
                "count": 0, "hits": [], "text": "",
            }
        ctx = build_search_context(hits, max_chars=4000)
        safe = sanitize_tool_result_content(ctx, source="rag_search")
        return {
            "ok": True, "slug": norm_slug, "query": str(query).strip(),
            "count": len(hits),
            "hits": [
                {
                    "chunk_id": h.get("chunk_id"),
                    "source": h.get("source"),
                    "chunk_index": h.get("chunk_index"),
                    "score": round(float(h.get("score", 0.0)), 3),
                    "mode": h.get("mode"),
                    "text": (h.get("text") or "")[:200],
                }
                for h in hits
            ],
            "text": safe,
        }

    def rag_get_chunks(self, slug: Optional[str] = None,
                       chunk_ids: Optional[List[int]] = None,
                       source: Optional[str] = None,
                       chunk_indices: Optional[List[int]] = None,
                       **kwargs: Any) -> Dict[str, Any]:
        """Fetch specific chunks of a RAG knowledge base (no embeddings involved).

        Follow-up to rag_search: pulls the neighbouring chunks around a hit so
        the model can restore the full context of a fragment. Two addressing
        modes:
          - ``chunk_ids``: exact chunk ids (shown in rag_search results);
          - ``source`` + ``chunk_indices``: 0-based positions inside one file.

        Access control is the same as rag_search: the active orchestrator may
        only fetch from bases assigned to it (DevAgent may use all bases).

        Args:
            slug: knowledge-base slug (see the Storage page).
            chunk_ids: list of chunk ids to fetch (mutually exclusive with source).
            source: source file path; use together with chunk_indices.
            chunk_indices: list of 0-based chunk positions inside source.
        Returns:
            {"ok": True, "slug", "count", "chunks", "missing", "text"}
            or {"ok": False, "error": ..., "suggestion": ...}.
            The context text is sanitized and fenced as untrusted data.
        """
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            return {
                "ok": False,
                "error": (
                    f"rag_get_chunks got unexpected argument(s): {unknown}. "
                    "Only 'slug', 'chunk_ids', 'source' and 'chunk_indices' "
                    "are supported."
                ),
                "suggestion": _RAG_CHUNKS_USAGE,
            }
        norm_slug = self._rag_slug(slug)
        if not norm_slug:
            return {
                "ok": False,
                "error": "Missing required argument 'slug'.",
                "suggestion": _RAG_CHUNKS_USAGE,
            }
        if not chunk_ids and not (source and chunk_indices):
            return {
                "ok": False,
                "error": (
                    "Provide 'chunk_ids' or 'source' together with "
                    "'chunk_indices'."
                ),
                "suggestion": _RAG_CHUNKS_USAGE,
            }
        if not self._rag_access_allowed(norm_slug):
            return {
                "ok": False,
                "error": f"Access denied: knowledge base '{norm_slug}' is not assigned to this orchestrator.",
            }
        try:
            from core.rag_search import RagSearchError, build_search_context, get_chunks
            result = get_chunks(
                norm_slug, chunk_ids=chunk_ids, source=str(source or ""),
                chunk_indices=chunk_indices,
            )
        except RagSearchError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"RAG chunk fetch failed: {e}"}
        chunks = result.get("chunks") or []
        ctx = build_search_context(chunks, max_chars=8000) if chunks else ""
        safe = sanitize_tool_result_content(ctx, source="rag_get_chunks") if ctx else ""
        return {
            "ok": True,
            "slug": norm_slug,
            "count": len(chunks),
            "chunks": [
                {
                    "chunk_id": c.get("chunk_id"),
                    "source": c.get("source"),
                    "chunk_index": c.get("chunk_index"),
                }
                for c in chunks
            ],
            "missing": result.get("missing") or [],
            "text": safe,
        }


_RAG_SEARCH_USAGE = (
    "Usage: rag_search(slug='<base_slug>', query='<search text>', "
    "[top_k=5], [min_score=0.0]). "
    "Search mode comes from the base settings: bm25 (own lexical index, "
    "the default - works without API keys), vector (embeddings) or "
    "hybrid (both rankings fused). "
    "Results carry the source, the 0-based chunk position and the chunk id. "
    "Use rag_get_chunks(slug='<base_slug>', chunk_ids=[...]) to pull "
    "neighbouring chunks when a snippet lacks context. "
    "List available bases with list_rag_bases(); use only bases from "
    "the 'Available RAG knowledge bases' block in the system prompt."
)


_RAG_CHUNKS_USAGE = (
    "Usage: rag_get_chunks(slug='<base_slug>', "
    "[chunk_ids=[...]] | [source='<file>', chunk_indices=[...]]). "
    "Fetch specific chunks by id or by source file + 0-based positions; "
    "use it after rag_search to restore the context around a hit. "
    "Only bases from the 'Available RAG knowledge bases' block are allowed."
)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
