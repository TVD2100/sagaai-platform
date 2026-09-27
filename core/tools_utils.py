"""
core.tools_utils - utility helpers for listing available tools.

Used by the Skills page to show the list of tools a skill can request.
The source of truth for tool definitions is ``dev_agent.tool_executor.TOOL_CATALOG``.
"""
from __future__ import annotations

from typing import Any, Optional


def list_tool_definitions() -> list[dict[str, Any]]:
    """Return the catalog of available DevAgent tools.

    Each entry is a dict with at least ``name`` and ``desc`` keys.
    Falls back to an empty list if the catalog cannot be imported
    (e.g. during very early bootstrap in tests).
    """
    try:
        from dev_agent.tool_executor import TOOL_CATALOG
        return list(TOOL_CATALOG)
    except Exception:
        return []


def build_rag_search_tool(base_slugs: Optional[list] = None) -> dict:
    """Build the native ``rag_search`` function-tool definition (Responses API).

    *base_slugs* is an optional list of RAG base slugs bound to the assistant;
    when exactly one slug is given it is mentioned in the tool description as
    the suggested value. The platform-side access control
    (core.assistant_tools.execute_assistant_rag_search) remains the source of
    truth - the description is only a hint for the model.
    """
    slugs = [str(s).strip().lower() for s in (base_slugs or []) if str(s).strip()]
    hint = ""
    if len(slugs) == 1:
        hint = f" Use slug '{slugs[0]}'."
    elif len(slugs) > 1:
        hint = " Use one of: " + ", ".join(f"'{s}'" for s in slugs) + "."
    return {
        "type": "function",
        "name": "rag_search",
        "description": (
            "Search the assistant's local RAG knowledge base "
            "(semantic vector search over document chunks). Results carry "
            "the source file, the 0-based chunk position and the chunk id; "
            "pass them to rag_get_chunks to fetch neighbouring chunks when "
            "a snippet lacks context." + hint
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the knowledge base to search.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query related to the user's question.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of chunks to return (default 5).",
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum similarity score in [0, 1] (default 0).",
                },
            },
            "required": ["slug", "query"],
        },
    }


def build_rag_chunks_tool(base_slugs: Optional[list] = None) -> dict:
    """Build the native ``rag_get_chunks`` function-tool definition.

    Follow-up tool for ``rag_search``: lets the model restore context by
    fetching specific chunks of a base - either by chunk ids (visible in
    the search results) or by source file + 0-based chunk positions.

    *base_slugs* is an optional list of RAG base slugs bound to the
    assistant; when exactly one slug is given it is mentioned in the tool
    description as the suggested value. The platform-side access control
    (core.assistant_tools.execute_assistant_rag_chunks) remains the source
    of truth - the description is only a hint for the model.
    """
    slugs = [str(s).strip().lower() for s in (base_slugs or []) if str(s).strip()]
    hint = ""
    if len(slugs) == 1:
        hint = f" Use slug '{slugs[0]}'."
    elif len(slugs) > 1:
        hint = " Use one of: " + ", ".join(f"'{s}'" for s in slugs) + "."
    return {
        "type": "function",
        "name": "rag_get_chunks",
        "description": (
            "Fetch specific chunks of the local RAG knowledge base by their "
            "ids or by source file + chunk positions. Use it after "
            "rag_search to pull the neighbouring chunks around a hit "
            "(e.g. ids 13, 14, 16 around 15, or the next chunk_indices of "
            "the same source) and restore the full context." + hint
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the knowledge base to fetch from.",
                },
                "chunk_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Chunk ids to fetch - taken from rag_search "
                        "results (headers or the hits list)."
                    ),
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Source file path; use together with "
                        "chunk_indices instead of chunk_ids."
                    ),
                },
                "chunk_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "0-based chunk positions inside source "
                        "(alternative to chunk_ids)."
                    ),
                },
            },
            "required": ["slug"],
        },
    }


def service_supported_tools(service_def: Optional[dict], tool_defs: Optional[list] = None) -> list[str]:
    """Return the tool names supported by *service_def*.

    Reads ``tools_options`` from the provider's JSON definition. Each entry may
    be a dict with a ``key`` field (preferred) or a plain string. When
    *tool_defs* is provided (a list of tool-catalog dicts with ``name``), only
    tools present in that catalog are returned. Returns an empty list when the
    provider defines no tools or none of them are known.
    """
    if not service_def:
        return []
    options = service_def.get("tools_options", []) or []
    keys: list[str] = []
    for opt in options:
        if isinstance(opt, dict):
            key = opt.get("key")
            if key:
                keys.append(str(key))
        elif isinstance(opt, str) and opt.strip():
            keys.append(opt.strip())
    if tool_defs:
        known = {td.get("name") for td in tool_defs if isinstance(td, dict)}
        if known:
            keys = [k for k in keys if k in known]
    return keys
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
