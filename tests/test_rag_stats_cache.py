# -*- coding: utf-8 -*-
"""
tests/test_rag_stats_cache.py - cached chunk/embedding counters in RAG indexes.

The counters (chunks_count / embeddings_count) live in the meta table of each
index.db and are maintained incrementally by every write function (add_chunk,
add_embedding, delete_chunk, delete_embedding, update_chunk_text, reset_index).
index_stats() therefore never scans the chunks/embeddings tables after the
one-time backfill performed for legacy databases created by older versions.

Covers:
  - counter seeding and incremental updates for every write path,
  - one-time backfill for legacy indexes without cached counters,
  - index_stats() reading cached values without COUNT table scans.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.rag_index as rag_index


_VEC4 = [1.0, 0.0, 0.0, 0.0]


def _make_db(tmp_path, chunks=3, with_vectors=True):
    """Create a small index DB with *chunks* chunks and return its path."""
    db = str(tmp_path / "idx.db")
    assert rag_index.create_index_db(
        db, dimension=4, provider="YandexAI",
        embedding_model="text-search-doc",
    )
    for i in range(chunks):
        vec = [float(i + 1), 0.0, 0.0, 0.0] if with_vectors else None
        rag_index.add_chunk(db, f"chunk {i}", source=f"s{i}.md",
                            chunk_index=i, vector=vec)
    return db


class TestCountersSeeding:
    def test_create_index_db_seeds_zero_counters(self, tmp_path):
        db = str(tmp_path / "fresh.db")
        assert rag_index.create_index_db(db, dimension=4)
        meta = rag_index.read_meta(db)
        assert meta.get("chunks_count") == "0"
        assert meta.get("embeddings_count") == "0"
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 0
        assert stats["embeddings"] == 0


class TestCounterIncrementalUpdates:
    def test_add_chunk_increments_chunks_and_embeddings(self, tmp_path):
        db = str(tmp_path / "idx.db")
        assert rag_index.create_index_db(db, dimension=4)
        rag_index.add_chunk(db, "one")
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 1
        assert stats["embeddings"] == 0
        rag_index.add_chunk(db, "two", vector=_VEC4)
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 2
        assert stats["embeddings"] == 1

    def test_add_embedding_upsert_counts_once(self, tmp_path):
        db = _make_db(tmp_path, chunks=1, with_vectors=False)
        assert rag_index.add_embedding(db, 1, _VEC4)
        assert rag_index.index_stats(db)["embeddings"] == 1
        # Replacing an existing vector must not increment the counter.
        assert rag_index.add_embedding(db, 1, [0.0, 1.0, 0.0, 0.0])
        assert rag_index.index_stats(db)["embeddings"] == 1

    def test_update_chunk_text_drops_embedding_counter(self, tmp_path):
        db = _make_db(tmp_path, chunks=2, with_vectors=True)
        assert rag_index.index_stats(db)["embeddings"] == 2
        assert rag_index.update_chunk_text(db, 1, "updated")
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 2
        assert stats["embeddings"] == 1

    def test_delete_chunk_decrements_both_counters(self, tmp_path):
        db = _make_db(tmp_path, chunks=3, with_vectors=True)
        assert rag_index.delete_chunk(db, 2)
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 2
        assert stats["embeddings"] == 2
        assert rag_index.delete_chunk(db, 2) is False

    def test_delete_chunk_without_embedding(self, tmp_path):
        db = _make_db(tmp_path, chunks=2, with_vectors=False)
        assert rag_index.delete_chunk(db, 1)
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 1
        assert stats["embeddings"] == 0

    def test_delete_embedding_decrements(self, tmp_path):
        db = _make_db(tmp_path, chunks=2, with_vectors=True)
        assert rag_index.delete_embedding(db, 1)
        assert rag_index.index_stats(db)["embeddings"] == 1
        assert rag_index.delete_embedding(db, 1) is False

    def test_reset_index_resets_counters(self, tmp_path):
        db = _make_db(tmp_path, chunks=4, with_vectors=True)
        assert rag_index.reset_index(db)
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 0
        assert stats["embeddings"] == 0
        meta = rag_index.read_meta(db)
        assert meta.get("provider") == "YandexAI"
        assert meta.get("chunks_count") == "0"
        assert meta.get("embeddings_count") == "0"


class TestLegacyBackfill:
    def test_legacy_db_backfilled_on_first_access(self, tmp_path):
        """A pre-existing index without counters is backfilled exactly once."""
        db = str(tmp_path / "legacy.db")
        assert rag_index.create_index_db(db, dimension=4)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "DELETE FROM meta WHERE k IN ('chunks_count', 'embeddings_count')"
            )
            conn.execute(
                "INSERT INTO chunks(text, source, chunk_index, created_at)"
                " VALUES('raw one', 'a.md', 0, '')"
            )
            conn.execute(
                "INSERT INTO chunks(text, source, chunk_index, created_at)"
                " VALUES('raw two', 'b.md', 0, '')"
            )
            conn.execute(
                "INSERT INTO embeddings(chunk_id, vector)"
                " VALUES(1, x'0000803f000000000000000000000000')"
            )
            conn.commit()
        finally:
            conn.close()
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 2
        assert stats["embeddings"] == 1
        meta = rag_index.read_meta(db)
        assert meta.get("chunks_count") == "2"
        assert meta.get("embeddings_count") == "1"
        # Second access stays consistent and uses the cached counters.
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 2
        assert stats["embeddings"] == 1

    def test_legacy_db_counter_backfilled_before_increments(self, tmp_path):
        """Writing through the API after backfill starts from real counts."""
        db = str(tmp_path / "legacy.db")
        assert rag_index.create_index_db(db, dimension=4)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "DELETE FROM meta WHERE k IN ('chunks_count', 'embeddings_count')"
            )
            conn.execute(
                "INSERT INTO chunks(text, source, chunk_index, created_at)"
                " VALUES('raw one', 'a.md', 0, '')"
            )
            conn.commit()
        finally:
            conn.close()
        rag_index.add_chunk(db, "new one")
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 2


class TestIndexStatsCheapness:
    def test_index_stats_uses_cached_counters_not_table_scans(
            self, tmp_path, monkeypatch):
        """After the counters exist, index_stats never runs a table scan."""
        db = _make_db(tmp_path, chunks=3, with_vectors=True)
        rag_index.index_stats(db)  # ensure counters are cached

        statements = []
        original_connect = rag_index._connect

        def traced_connect(path):
            conn = original_connect(path)
            def trace(stmt):
                statements.append(stmt)
            conn.set_trace_callback(trace)
            return conn

        monkeypatch.setattr(rag_index, "_connect", traced_connect)
        stats = rag_index.index_stats(db)
        assert stats["chunks"] == 3
        assert stats["embeddings"] == 3
        scans = [s for s in statements if "COUNT" in s.upper()]
        assert scans == []

    def test_index_stats_missing_db_returns_zeros(self, tmp_path):
        stats = rag_index.index_stats(str(tmp_path / "missing.db"))
        assert stats["chunks"] == 0
        assert stats["embeddings"] == 0
        assert stats["dimension"] is None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
