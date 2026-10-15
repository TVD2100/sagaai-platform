# -*- coding: utf-8 -*-
"""tests/scenarios/test_rag_search_modes_scenario.py - search modes end to end.

User-level scenarios for the per-base RAG search modes (bm25 default,
vector, hybrid RRF):

  1. "offline base" - a base is created in the default bm25 mode and indexed
     with NO provider credentials at all; keyword search returns hits and the
     chat context block carries the material.
  2. "mode switch" - the mode is switched through vector (mocked embeddings)
     to hybrid: vector hits come back with mode "vector", hybrid fuses both
     rankings, a failing embedding provider degrades hybrid to pure BM25
     without raising, and an invalid mode value is rejected.
  3. "error path" - searching a missing base raises RagSearchError while
     chat_context swallows the failure and returns an empty string.

Every scenario walks the public API (core.rag / core.rag_indexer /
core.rag_search) the same way the UI and the rag_search tool call it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


@pytest.fixture()
def rag_data(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh DATA_DIR with core/storage/ui re-imported on top of it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    yield data_dir


def test_offline_bm25_base_end_to_end(rag_data):
    """S1: bm25 base -> index without keys -> search -> chat context.

    given: a new base created without an explicit search mode
    when: a file is indexed and searched while every credential helper is
          patched to fail on any call
    then: indexing/search succeed, hits carry mode "bm25" and chat_context
          injects the material
    """
    import core.rag as rag
    import core.rag_indexer as indexer
    import core.rag_search as rag_search

    base = rag.create_base(name="Offline KB", description="bm25 scenario")
    slug = base["slug"]
    assert base["search_mode"] == "bm25"

    text = ("The installation wizard asks for the API key. "
            "The storage page lists every knowledge base.")
    rag.add_file(slug, "guide.txt", text.encode("utf-8"))

    with patch("core.rag_indexer.get_yandex_embedding_credentials",
               side_effect=AssertionError("bm25 indexing needs no keys")):
        result = indexer.index_base(slug)
    assert result["status"] == "ready"

    with patch.object(rag_search, "embed_query",
                      side_effect=AssertionError("bm25 search needs no keys")):
        hits = rag_search.search_base(slug, "wizard")

    assert hits and hits[0]["source"] == "guide.txt"
    assert hits[0]["mode"] == "bm25"

    ctx = rag_search.chat_context(slug, "wizard")
    assert "Материалы из базы знаний" in ctx
    assert "guide.txt" in ctx
    assert ", bm25" in ctx


def test_mode_switch_vector_hybrid_and_degradation(rag_data):
    """S2: vector indexing (mocked) -> vector hits -> hybrid -> fallback.

    given: a base created directly in vector mode
    when: it is indexed with mocked embeddings, searched, switched to hybrid
          and searched again with a failing query-embedding provider
    then: vector hits, then fused RRF hits, then a pure-BM25 degradation;
          an unknown mode value is rejected with ValueError
    """
    import core.rag as rag
    import core.rag_indexer as indexer
    import core.rag_search as rag_search

    base = rag.create_base(name="Switch KB", provider="YandexAI",
                           embedding_model="text-search-doc",
                           search_mode="vector")
    slug = base["slug"]
    rag.add_file(slug, "faq.txt",
                 b"The wizard stores the API key and reindexes the base.")

    with patch("core.rag_indexer.get_yandex_embedding_credentials",
               return_value=("api-key", "folder-id")), \
         patch("core.rag_embeddings.embed_text",
               return_value=[0.25] * 256):
        result = indexer.index_base(slug)
    assert result["status"] == "ready"

    with patch.object(rag_search, "embed_query",
                      return_value=[0.25] * 256):
        hits = rag_search.search_base(slug, "wizard")
    assert hits and hits[0]["mode"] == "vector"

    rag.update_base(slug, {"search_mode": "hybrid"})
    with patch.object(rag_search, "embed_query",
                      return_value=[0.25] * 256):
        fused = rag_search.search_base(slug, "wizard")
    assert fused and fused[0]["mode"] == "hybrid"
    assert fused[0]["chunk_id"] == hits[0]["chunk_id"]

    with patch.object(rag_search, "embed_query",
                      side_effect=RuntimeError("provider down")):
        degraded = rag_search.search_base(slug, "wizard")
    assert degraded and degraded[0]["mode"] == "bm25"

    with pytest.raises(ValueError):
        rag.update_base(slug, {"search_mode": "magic"})


def test_error_path_missing_base(rag_data):
    """S3: a missing base raises in search_base but never in chat_context.

    given: no bases exist in a fresh data dir
    when: search_base and chat_context are called for an unknown slug
    then: search_base raises RagSearchError while chat_context returns ""
    """
    import core.rag_search as rag_search

    with pytest.raises(rag_search.RagSearchError):
        rag_search.search_base("no_such_base", "hello")
    assert rag_search.chat_context("no_such_base", "hello") == ""
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
