# -*- coding: utf-8 -*-
"""tests/scenarios/test_rag_perf_scenarios.py - end-to-end scenarios for the RAG index-cost fix.

With large knowledge bases every full SQLite COUNT scan over the
chunks/embeddings tables used to stall page renders. The fix caches the
counters in the index meta table (maintained at every write point) and
switched hot render/context paths to ``with_stats=False`` so they never
open index.db.

Scenarios (given -> when -> then):

  1. "index lifecycle" - a base is created, a file is indexed (embeddings
     mocked) and every later read serves cached counters.
  2. "hot paths over a large base" - the assistant/orchestrator UI renders,
     the DevAgent RAG tools list bases and the orchestrator prompt is
     extended without opening a single index.db.
  3. "legacy index" - an index written by an older release (no counters) is
     backfilled once and served from cache afterwards.
  4. "chunk maintenance" - editing/deleting/adding chunks keeps the cached
     counters consistent with the real tables through the core.rag API.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def rag_data(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh DATA_DIR with core/storage/ui re-imported on top of it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    yield data_dir


def _invoke(fn):
    try:
        fn()
    except StopRerun:
        pass


def _st():
    """Fresh Streamlit mock with any cached ui.* modules removed."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    return install_streamlit_mock()


def _track_index_opens():
    """Record every path opened through core.rag_index._connect."""
    import core.rag_index as rag_index

    opened = []
    original = rag_index._connect

    def traced(path):
        opened.append(path)
        return original(path)

    return patch.object(rag_index, "_connect", traced), opened


def _real_counts(db):
    """Oracle: actual chunk/embedding row counts (COUNT allowed in tests)."""
    import core.rag_index as rag_index

    conn = rag_index._connect(db)
    try:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM chunks),"
            " (SELECT COUNT(*) FROM embeddings)"
        ).fetchone()
        return int(row[0]), int(row[1])
    finally:
        conn.close()


def test_index_lifecycle_serves_cached_counters(rag_data):
    """S1: create -> index -> stats come from cached meta counters.

    given: a new base and one source file that splits into several chunks
    when: the file is indexed (embeddings mocked locally) and the base is
          read back through the public API
    then: index_stats matches the real tables, with_stats=False drops the
          block and never touches the index
    """
    import core.rag as rag
    import core.rag_index as rag_index
    import core.rag_indexer as indexer

    base = rag.create_base(
        name="Scenario KB",
        description="perf scenario",
        provider="YandexAI",
        embedding_model="text-search-doc",
        chunk_size=80,
        chunk_overlap=0,
    )
    slug = base["slug"]
    text = ("knowledge base chunk text " * 20).strip()

    tracker, opened = _track_index_opens()
    with tracker, \
         patch("core.rag_indexer.get_yandex_embedding_credentials",
               return_value=("api-key", "folder-id")), \
         patch("core.rag_embeddings.embed_text",
               return_value=[0.25] * 256):
        rag.add_file(slug, "guide.txt", text.encode("utf-8"))
        result = indexer.index_base(slug)

    assert result["status"] == "ready"
    stats = rag.get_base(slug)["index_stats"]
    assert stats["chunks"] == stats["embeddings"] > 0
    assert stats["chunks"] == rag_index.count_chunks(rag.index_db_path(slug))

    # The stats-less read keeps the manifest and drops the stats block.
    tracker2, opened2 = _track_index_opens()
    with tracker2:
        light = rag.get_base(slug, with_stats=False)
    assert light["slug"] == slug
    assert "index_stats" not in light
    assert not any(p.endswith("index.db") for p in opened2)


def test_hot_paths_do_not_open_index(rag_data):
    """S2: a large index; hot render/prompt paths never open index.db.

    given: a base whose index holds hundreds of embedded chunks
    when: the assistants page and the orchestrator RAG section render, the
          orchestrator prompt block is built, the DevAgent list_rag_bases
          tool runs and the auto-RAG chat context is resolved
    then: none of these paths opens a single index.db
    """
    import core.rag as rag
    import core.rag_index as rag_index

    base = rag.create_base(name="Large KB", provider="YandexAI",
                           embedding_model="text-search-doc")
    slug = base["slug"]
    db = rag.index_db_path(slug)
    for i in range(300):
        rag_index.add_chunk(db, f"bulk chunk {i}", source="bulk.txt",
                            chunk_index=i, vector=[0.1] * 256)

    tracker, opened = _track_index_opens()
    with tracker:
        # DevAgent RAG listing tool (real core.rag under the hood).
        from dev_agent.tool_executor import ToolExecutor

        result = ToolExecutor().list_rag_bases()
        assert result["ok"] is True
        assert slug in [b["slug"] for b in result["bases"]]

        # Orchestrator prompt block.
        from core.orchestrators import _extend_prompt_with_rag_bases

        prompt = _extend_prompt_with_rag_bases("System prompt", "dev_agent")
        assert "Available RAG knowledge bases" in prompt
        assert slug in prompt

        # Assistants page renders the RAG multiselect from real manifests.
        from tests.test_ui_tooltips import SAMPLE_SERVICE

        with _st() as st:
            st.session_state.update({
                "ui_lang": "English",
                "show_assistant_form": True,
                "show_skill_form": True,
                "edit_assistant_id": None,
                "edit_skill_id": None,
            })
            import ui.pages.assistants as assistants_mod

            with patch.object(assistants_mod, "get_services",
                              return_value={"TestSvc": SAMPLE_SERVICE}), \
                 patch.object(assistants_mod, "list_tool_definitions",
                              return_value=[]), \
                 patch.object(assistants_mod, "service_supported_tools",
                              return_value=["web_search"]):
                _invoke(assistants_mod.page_assistants)

        # Orchestrator settings RAG section (real core.rag).
        with _st() as st:
            st.session_state.update({"ui_lang": "English"})
            import ui.pages.orchestrator as orch_mod

            _invoke(lambda: orch_mod._render_orch_rag_bases("dev_agent",
                                                            "English"))

        # Auto-RAG chat context for an assistant without bound bases.
        import core.api_layer as api

        ctx = api._assistant_rag_context(
            {"id": "a1", "slug": "assist1", "name": "Assistant"}, "hello")
        assert ctx == ""

    assert not any(p.endswith("index.db") for p in opened)


def test_legacy_index_backfilled_once(rag_data):
    """S3: an index without cached counters is backfilled exactly once.

    given: a base whose meta table lacks the counters but whose tables hold
           data (simulating an older release)
    when: the storage stats are requested through the public API
    then: one backfill happens, the counters are stored in meta and the
          next read performs no COUNT scan
    """
    import core.rag as rag
    import core.rag_index as rag_index

    base = rag.create_base(name="Legacy KB", provider="YandexAI",
                           embedding_model="text-search-doc")
    slug = base["slug"]
    db = rag.index_db_path(slug)
    rag_index.add_chunk(db, "old one", source="old.md",
                        vector=[0.5] * 256)
    rag_index.add_chunk(db, "old two", source="old.md")

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "DELETE FROM meta WHERE k IN ('chunks_count', 'embeddings_count')"
        )
        conn.commit()
    finally:
        conn.close()

    stats = rag.get_base(slug)["index_stats"]
    assert stats["chunks"] == 2
    assert stats["embeddings"] == 1
    meta = rag_index.read_meta(db)
    assert meta["chunks_count"] == "2"
    assert meta["embeddings_count"] == "1"

    # The second read is served from the cache: no COUNT statements run.
    statements = []
    original = rag_index._connect

    def traced(path):
        conn_ = original(path)
        conn_.set_trace_callback(lambda stmt: statements.append(stmt))
        return conn_

    with patch.object(rag_index, "_connect", traced):
        again = rag.get_base(slug)["index_stats"]
    assert again["chunks"] == 2
    assert not [s for s in statements if "COUNT" in s.upper()]


def test_chunk_maintenance_keeps_counters_in_sync(rag_data):
    """S4: update/delete/add chunk flows keep cached counters accurate.

    given: a base with three embedded chunks
    when: one chunk text is edited (its embedding invalidated), another is
          deleted and a fresh one is added through the UI-facing API
    then: after every step index_stats equals the real table counts
    """
    import core.rag as rag
    import core.rag_index as rag_index

    base = rag.create_base(name="Sync KB", provider="YandexAI",
                           embedding_model="text-search-doc")
    slug = base["slug"]
    db = rag.index_db_path(slug)
    ids = [
        rag_index.add_chunk(db, f"chunk {i}", source="s.md",
                            chunk_index=i,
                            vector=[float(i + 1)] + [0.0] * 255)
        for i in range(3)
    ]

    outcome = rag.update_chunk(slug, ids[0], "edited text")
    assert outcome["ok"] is True
    assert (rag_index.index_stats(db)["chunks"],
            rag_index.index_stats(db)["embeddings"]) == _real_counts(db)

    assert rag.delete_chunk(slug, ids[1]) is True
    assert (rag_index.index_stats(db)["chunks"],
            rag_index.index_stats(db)["embeddings"]) == _real_counts(db)

    assert rag_index.add_chunk(db, "brand new", vector=[0.3] * 256) > 0
    assert (rag_index.index_stats(db)["chunks"],
            rag_index.index_stats(db)["embeddings"]) == _real_counts(db)
    assert rag.get_base(slug)["index_stats"]["chunks"] == rag_index.index_stats(db)["chunks"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
