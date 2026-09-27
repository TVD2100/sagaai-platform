# -*- coding: utf-8 -*-
"""
Tests for chunk-level RAG operations and preset skill auto-registration.

Covers:
  - core.rag_index chunk get/list/search/update/delete helpers,
  - core.rag chunk wrappers (list/get/update/delete by base slug),
  - core.default_imports.ensure_default_skills seeds a defaults/skills preset
    into an already non-empty runtime library and marks it with source=...,
    and does not resurrect a deleted preset (removed_defaults).
"""
import importlib
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR isolating RAG bases, skills and the DB.

    Uses tests._test_isolation.isolated_app_modules so import-time constants
    (RAG_BASES_DIR, DEFAULTS_DIR) always come from the temporary DATA_DIR and
    stale package attributes (e.g. core.rag) never leak between tests.
    """
    tmp = tempfile.mkdtemp(prefix="sagaai_test_rag_chunks_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    from tests._test_isolation import isolated_app_modules as _iso_app_modules
    with _iso_app_modules():
        import core.paths as paths_mod
        importlib.reload(paths_mod)
        import storage.db as db_mod
        importlib.reload(db_mod)
        db_mod.reset_engine()
        db_mod.reset_devagent_engine()

        yield tmp

        db_mod.reset_engine()
        db_mod.reset_devagent_engine()

    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    shutil.rmtree(tmp, ignore_errors=True)


def _make_index_db(tmp: str) -> str:
    """Create a small index DB and return its path."""
    from core.rag_index import create_index_db, add_chunk
    db = os.path.join(tmp, "idx.db")
    assert create_index_db(db, dimension=3, provider="YandexAI",
                           embedding_model="text-search-doc")
    add_chunk(db, "First text about apples", source="a.md", chunk_index=0,
              vector=[1.0, 0.0, 0.0])
    add_chunk(db, "Second text about bananas", source="b.md", chunk_index=0,
              vector=[0.0, 1.0, 0.0])
    add_chunk(db, "Third text about cherries", source="c.md", chunk_index=1,
              vector=[0.0, 0.0, 1.0])
    return db


class TestRagIndexChunkOps:
    def test_get_chunk(self, isolated_data_dir):
        from core.rag_index import get_chunk
        db = _make_index_db(isolated_data_dir)
        c = get_chunk(db, 2)
        assert c["chunk_id"] == 2
        assert "bananas" in c["text"]
        assert c["has_embedding"] is True

    def test_get_chunk_missing(self, isolated_data_dir):
        from core.rag_index import get_chunk
        db = _make_index_db(isolated_data_dir)
        assert get_chunk(db, 999) == {}
        assert get_chunk(os.path.join(isolated_data_dir, "missing.db"), 1) == {}

    def test_list_chunks_pagination(self, isolated_data_dir):
        from core.rag_index import list_chunks
        db = _make_index_db(isolated_data_dir)
        page = list_chunks(db, limit=2, offset=0)
        assert page["total"] == 3
        assert len(page["chunks"]) == 2
        assert page["chunks"][0]["chunk_id"] == 1
        assert page["chunks"][1]["chunk_id"] == 2
        assert page["chunks"][0]["has_embedding"] is True

    def test_search_chunks_text_filters_and_escapes(self, isolated_data_dir):
        from core.rag_index import search_chunks_text
        db = _make_index_db(isolated_data_dir)
        page = search_chunks_text(db, "banana")
        assert page["total"] == 1
        assert page["chunks"][0]["source"] == "b.md"
        # wildcards are matched literally
        page2 = search_chunks_text(db, "%")
        assert page2["total"] == 0

    def test_update_chunk_text_resets_embedding(self, isolated_data_dir):
        from core.rag_index import update_chunk_text, get_chunk, add_embedding
        db = _make_index_db(isolated_data_dir)
        assert update_chunk_text(db, 2, "Updated banana text")
        c = get_chunk(db, 2)
        assert "Updated banana" in c["text"]
        assert c["has_embedding"] is False
        assert add_embedding(db, 2, [0.5, 0.5, 0.0])
        assert get_chunk(db, 2)["has_embedding"] is True

    def test_update_chunk_missing_returns_false(self, isolated_data_dir):
        from core.rag_index import update_chunk_text
        db = _make_index_db(isolated_data_dir)
        assert update_chunk_text(db, 999, "x") is False

    def test_delete_chunk_and_embedding(self, isolated_data_dir):
        from core.rag_index import delete_chunk, get_chunk, count_chunks, index_stats
        db = _make_index_db(isolated_data_dir)
        assert delete_chunk(db, 1)
        assert get_chunk(db, 1) == {}
        assert count_chunks(db) == 2
        stats = index_stats(db)
        assert stats["embeddings"] == 2
        assert delete_chunk(db, 1) is False


class TestRagFetchChunks:
    def test_fetch_by_ids_preserves_order(self, isolated_data_dir):
        from core.rag_index import fetch_chunks
        db = _make_index_db(isolated_data_dir)
        res = fetch_chunks(db, chunk_ids=[3, 1])
        assert [c["chunk_id"] for c in res["chunks"]] == [3, 1]
        assert res["missing"] == []
        assert "cherries" in res["chunks"][0]["text"]
        assert res["chunks"][1]["has_embedding"] is True

    def test_fetch_by_source_and_indices(self, isolated_data_dir):
        from core.rag_index import fetch_chunks
        db = _make_index_db(isolated_data_dir)
        res = fetch_chunks(db, source="c.md", chunk_indices=[1, 0])
        assert [c["source"] for c in res["chunks"]] == ["c.md"]
        assert res["chunks"][0]["chunk_index"] == 1
        assert res["missing"] == [0]

    def test_fetch_reports_missing_and_bad_values(self, isolated_data_dir):
        from core.rag_index import fetch_chunks
        db = _make_index_db(isolated_data_dir)
        res = fetch_chunks(db, chunk_ids=[2, 999, "abc", "-1"])
        assert [c["chunk_id"] for c in res["chunks"]] == [2]
        assert res["missing"] == [999, "abc", "-1"]

    def test_fetch_accepts_string_ids_and_deduplicates(self, isolated_data_dir):
        from core.rag_index import fetch_chunks
        db = _make_index_db(isolated_data_dir)
        assert fetch_chunks(db, chunk_ids="2")["chunks"][0]["chunk_id"] == 2
        dedup = fetch_chunks(db, chunk_ids=[1, 1, 1])
        assert [c["chunk_id"] for c in dedup["chunks"]] == [1]

    def test_fetch_empty_input_and_missing_db(self, isolated_data_dir):
        from core.rag_index import fetch_chunks
        db = _make_index_db(isolated_data_dir)
        assert fetch_chunks(db) == {"chunks": [], "missing": []}
        assert fetch_chunks(db, source="c.md")["chunks"] == []
        absent = os.path.join(isolated_data_dir, "none.db")
        assert fetch_chunks(absent, chunk_ids=[1]) == {"chunks": [], "missing": []}

    def test_fetch_ids_mode_takes_priority_over_source(self, isolated_data_dir):
        from core.rag_index import fetch_chunks
        db = _make_index_db(isolated_data_dir)
        res = fetch_chunks(db, chunk_ids=[1], source="c.md", chunk_indices=[1])
        assert [c["chunk_id"] for c in res["chunks"]] == [1]
        assert res["chunks"][0]["source"] == "a.md"


class TestRagSearchGetChunks:
    def _make_ready_base(self, tmp):
        from core import rag
        from core.rag_index import add_chunk
        base = rag.create_base(name="Chunks KB", provider="YandexAI",
                               embedding_model="text-search-doc",
                               chunk_size=200, chunk_overlap=10)
        slug = base["slug"]
        rag.set_status(slug, "ready")
        db = rag.index_db_path(slug)
        add_chunk(db, "part one", source="doc.md", chunk_index=0,
                  vector=[0.1, 0.2])
        add_chunk(db, "part two", source="doc.md", chunk_index=1)
        add_chunk(db, "part three", source="doc.md", chunk_index=2)
        return slug

    def test_get_chunks_by_ids(self, isolated_data_dir):
        from core.rag_search import get_chunks
        slug = self._make_ready_base(isolated_data_dir)
        res = get_chunks(slug, chunk_ids=[3, 1])
        assert [c["chunk_index"] for c in res["chunks"]] == [2, 0]
        assert res["chunks"][0]["text"] == "part three"
        assert res["missing"] == []

    def test_get_chunks_by_source_window(self, isolated_data_dir):
        from core.rag_search import get_chunks
        slug = self._make_ready_base(isolated_data_dir)
        res = get_chunks(slug, source="doc.md", chunk_indices=[2, 1])
        assert [c["text"] for c in res["chunks"]] == ["part three", "part two"]

    def test_get_chunks_caps_request_size(self, isolated_data_dir):
        from core.rag_search import MAX_FETCH_CHUNKS, get_chunks
        slug = self._make_ready_base(isolated_data_dir)
        res = get_chunks(slug, chunk_ids=list(range(1, MAX_FETCH_CHUNKS + 10)))
        assert len(res["chunks"]) == 3
        assert len(res["missing"]) == MAX_FETCH_CHUNKS - 3

    def test_get_chunks_ignores_bad_values(self, isolated_data_dir):
        from core.rag_search import get_chunks
        slug = self._make_ready_base(isolated_data_dir)
        res = get_chunks(slug, chunk_ids=["x", "2", None])
        assert [c["chunk_id"] for c in res["chunks"]] == [2]
        assert res["missing"] == []

    def test_get_chunks_raises_for_missing_or_draft_base(self, isolated_data_dir):
        import pytest
        from core import rag
        from core.rag_search import RagSearchError, get_chunks
        with pytest.raises(RagSearchError):
            get_chunks("no_such_base", chunk_ids=[1])
        draft = rag.create_base(name="Draft KB", provider="YandexAI")
        with pytest.raises(RagSearchError):
            get_chunks(draft["slug"], chunk_ids=[1])


class TestBuildSearchContextHeaders:
    def test_headers_carry_chunk_ids_and_score(self):
        from core.rag_search import build_search_context
        ctx = build_search_context([
            {"chunk_id": 7, "source": "a.md", "chunk_index": 3,
             "score": 0.125, "text": "hello"},
            {"chunk_id": 8, "source": "a.md", "chunk_index": 4,
             "text": "no score"},
        ])
        assert "--- [a.md (chunk 3, id 7), score 0.125]" in ctx
        assert "--- [a.md (chunk 4, id 8)]" in ctx

    def test_headers_without_chunk_id_keep_old_shape(self):
        from core.rag_search import build_search_context
        ctx = build_search_context([
            {"source": "b.md", "chunk_index": 0, "score": 0.5, "text": "x"},
        ])
        assert "--- [b.md (chunk 0), score 0.500]" in ctx


class TestRagChunkWrappers:
    def _make_base(self, tmp):
        from core import rag
        base = rag.create_base(
            name="Test KB", provider="YandexAI",
            embedding_model="text-search-doc", chunk_size=200, chunk_overlap=10,
        )
        slug = base["slug"]
        from core.rag_index import add_chunk
        db = rag.index_db_path(slug)
        add_chunk(db, "alpha content", source="a.md", chunk_index=0)
        add_chunk(db, "beta content", source="b.md", chunk_index=0,
                  vector=[0.1, 0.2])
        return slug

    def test_list_and_get_chunk(self, isolated_data_dir):
        from core import rag
        slug = self._make_base(isolated_data_dir)
        page = rag.list_chunks(slug, limit=10)
        assert page["total"] == 2
        cid = page["chunks"][0]["chunk_id"]
        assert rag.get_chunk(slug, cid)["chunk_id"] == cid
        assert rag.get_chunk(slug, 999) == {}

    def test_list_chunks_with_query(self, isolated_data_dir):
        from core import rag
        slug = self._make_base(isolated_data_dir)
        page = rag.list_chunks(slug, query="beta", limit=10)
        assert page["total"] == 1
        assert page["chunks"][0]["source"] == "b.md"

    def test_update_chunk_simple(self, isolated_data_dir):
        from core import rag
        slug = self._make_base(isolated_data_dir)
        page = rag.list_chunks(slug, limit=10)
        cid = page["chunks"][1]["chunk_id"]
        outcome = rag.update_chunk(slug, cid, "new beta text")
        assert outcome["ok"] is True
        assert outcome["reembedded"] is False
        assert "embedding" in outcome["warning"].lower()
        c = rag.get_chunk(slug, cid)
        assert "new beta" in c["text"]
        assert c["has_embedding"] is False

    def test_update_chunk_missing(self, isolated_data_dir):
        from core import rag
        slug = self._make_base(isolated_data_dir)
        outcome = rag.update_chunk(slug, 999, "x")
        assert outcome["ok"] is False

    def test_delete_chunk(self, isolated_data_dir):
        from core import rag
        slug = self._make_base(isolated_data_dir)
        page = rag.list_chunks(slug, limit=10)
        cid = page["chunks"][0]["chunk_id"]
        assert rag.delete_chunk(slug, cid)
        assert rag.delete_chunk(slug, cid) is False


class TestPresetSkillAutoRegistration:
    def _make_preset(self, tmp):
        """Create a defaults/skills-like preset inside tmp and return its path."""
        preset = os.path.join(tmp, "defaults_skills", "test_preset")
        os.makedirs(preset, exist_ok=True)
        with open(os.path.join(preset, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# Test preset skill\n")
        return preset

    def test_seeds_into_nonempty_library(self, isolated_data_dir, monkeypatch):
        import core.skills_library as sl
        from core.default_imports import ensure_default_skills
        import core.defaults as defaults_mod

        # Seed one existing skill to make the library non-empty.
        src = os.path.join(isolated_data_dir, "existing_skill")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# Existing\n")
        res = sl.import_skill_from_folder(src, name="Existing Skill")
        assert res["ok"]
        assert len(sl.list_skills()) == 1

        # Point defaults/skills at a custom preset folder.
        preset = self._make_preset(isolated_data_dir)
        monkeypatch.setattr(defaults_mod, "skills_dir", lambda: os.path.dirname(preset))
        result = ensure_default_skills()
        assert "test_preset" in result
        sid = result["test_preset"]
        assert len(sid) == 8

        # The new skill exists and is marked with source=defaults/test_preset.
        rec = sl.get_skill(sid)
        assert rec is not None
        registry = sl._load_registry()
        assert registry[sid]["source"] == "defaults/test_preset"

        # Second run is idempotent (reports exists, does not duplicate).
        result2 = ensure_default_skills()
        assert result2["test_preset"] == "exists"
        assert len(sl.list_skills()) == 2

    def test_deleted_preset_is_not_resurrected(self, isolated_data_dir, monkeypatch):
        import core.skills_library as sl
        from core.default_imports import ensure_default_skills
        import core.defaults as defaults_mod

        preset = self._make_preset(isolated_data_dir)
        monkeypatch.setattr(defaults_mod, "skills_dir", lambda: os.path.dirname(preset))
        result = ensure_default_skills()
        sid = result["test_preset"]

        # Simulate the user deleting the skill: delete_skill records the marker.
        assert sl.delete_skill(sid)
        assert "defaults/test_preset" in sl._load_removed_defaults()

        # A new run must not re-import it.
        result2 = ensure_default_skills()
        assert "test_preset" in result2
        assert result2["test_preset"] == "exists"
        assert len(sl.list_skills()) == 0
