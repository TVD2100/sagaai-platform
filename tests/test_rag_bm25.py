# -*- coding: utf-8 -*-
"""
tests/test_rag_bm25.py - unit tests for the own BM25 lexical index.

Covers core.rag_bm25 directly on explicit database paths:
  - Unicode tokenization (ru/en, ё folding, punctuation);
  - deterministic ranking on a hand-built index (manual score check);
  - lazy one-time rebuild for legacy databases;
  - incremental maintenance hooks (add / update / delete);
  - top_k / min_score, blank query, missing database;
  - stats() and drop_index().
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.rag_bm25 as rag_bm25  # noqa: E402


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_db(tmp_path, name="idx.db") -> str:
    """Create a minimal chunks table (schema-compatible with rag_index)."""
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chunks (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               text TEXT NOT NULL,
               source TEXT DEFAULT '',
               chunk_index INTEGER DEFAULT 0,
               created_at TEXT DEFAULT ''
           )"""
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS embeddings (
               chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id)
               ON DELETE CASCADE,
               vector BLOB NOT NULL
           )""")
    conn.commit()
    conn.close()
    return db


def _add_raw(db, text, source="", chunk_index=0) -> int:
    """Insert a chunk row directly and return its id."""
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO chunks(text, source, chunk_index, created_at)"
            " VALUES(?, ?, ?, '')",
            (text, source, int(chunk_index)),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ─── tokenization ────────────────────────────────────────────────────────────

class TestTokenize:
    def test_basic_ru_and_latin(self):
        toks = rag_bm25.tokenize("Привет, World! 42 раза")
        assert toks == ["привет", "world", "42", "раза"]

    def test_lowercase_normalization(self):
        assert rag_bm25.tokenize("API API api") == ["api", "api", "api"]

    def test_yo_folding(self):
        assert rag_bm25.tokenize("ещё Ёлка") == ["еще", "елка"]
        # Query with 'е' matches a document that spells 'ё' (both fold to 'е').
        assert rag_bm25.tokenize("еще") == rag_bm25.tokenize("ещё")

    def test_punctuation_and_underscore_are_separators(self):
        toks = rag_bm25.tokenize("snake_case - foo-bar (v2.1)")
        assert toks == ["snake", "case", "foo", "bar", "v2", "1"]

    def test_empty_and_none(self):
        assert rag_bm25.tokenize("") == []
        assert rag_bm25.tokenize(None) == []
        assert rag_bm25.tokenize("   !!! ---") == []


# ─── rebuild / readiness ─────────────────────────────────────────────────────

class TestRebuildAndReady:
    def test_missing_db_not_ready_and_search_empty(self, tmp_path):
        missing = str(tmp_path / "nope.db")
        assert rag_bm25.is_ready(missing) is False
        assert rag_bm25.ensure_ready(missing) is False
        assert rag_bm25.search(missing, "query") == []

    def test_rebuild_makes_ready(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, "gamma delta")
        _add_raw(db, "epsilon")
        assert rag_bm25.is_ready(db) is False
        out = rag_bm25.rebuild_index(db)
        assert out["ok"] is True
        assert out["docs"] == 2
        assert rag_bm25.is_ready(db) is True
        # Idempotent second run.
        out2 = rag_bm25.rebuild_index(db)
        assert out2["ok"] is True and out2["docs"] == 2

    def test_rebuild_skips_empty_texts_in_counters(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, "word")
        _add_raw(db, "   ")
        _add_raw(db, "")
        out = rag_bm25.rebuild_index(db)
        assert out["ok"] is True
        assert out["docs"] == 1
        stats = rag_bm25.stats(db)
        assert stats["ready"] is True
        assert stats["docs"] == 1
        assert stats["tokens"] == 1
        assert stats["posting_rows"] == 1


# ─── search semantics ────────────────────────────────────────────────────────

class TestSearch:
    def _base(self, tmp_path):
        db = _make_db(tmp_path)
        self.id_a = _add_raw(db, "кошка собака кот", source="a.md", chunk_index=0)
        self.id_b = _add_raw(db, "собака корм для собак", source="b.md", chunk_index=0)
        self.id_c = _add_raw(db, "векторный поиск эмбеддинги", source="c.md", chunk_index=0)
        rag_bm25.rebuild_index(db)
        return db

    def test_ranking_prefers_matching_chunk(self, tmp_path):
        db = self._base(tmp_path)
        hits = rag_bm25.search(db, "кошка", top_k=3)
        assert hits, "search must return hits"
        assert hits[0]["chunk_id"] == self.id_a
        assert hits[0]["source"] == "a.md"
        assert hits[0]["score"] > 0

    def test_unrelated_chunk_not_returned(self, tmp_path):
        db = self._base(tmp_path)
        hits = rag_bm25.search(db, "эмбеддинги", top_k=3)
        assert [h["chunk_id"] for h in hits] == [self.id_c]

    def test_top_k_limits_results(self, tmp_path):
        db = self._base(tmp_path)
        hits = rag_bm25.search(db, "собака", top_k=1)
        assert len(hits) == 1
        hits_all = rag_bm25.search(db, "собака", top_k=10)
        assert len(hits_all) == 2  # a.md and b.md both contain 'собака'

    def test_min_score_filters(self, tmp_path):
        db = self._base(tmp_path)
        hits = rag_bm25.search(db, "собака", top_k=10)
        assert len(hits) == 2
        top = hits[0]["score"]
        high = rag_bm25.search(db, "собака", top_k=10, min_score=top)
        assert len(high) == 1
        assert high[0]["score"] >= top

    def test_blank_query_and_no_match(self, tmp_path):
        db = self._base(tmp_path)
        assert rag_bm25.search(db, "") == []
        assert rag_bm25.search(db, "    ") == []
        assert rag_bm25.search(db, "ёжик") == []

    def test_hits_shape_matches_vector_search(self, tmp_path):
        db = self._base(tmp_path)
        hits = rag_bm25.search(db, "кошка", top_k=1)
        assert set(hits[0]) == {"chunk_id", "text", "source", "chunk_index", "score"}

    def test_manual_score_matches_lucene_formula(self, tmp_path):
        """Score of a one-term query must equal the closed-form BM25 value."""
        import math
        db = _make_db(tmp_path)
        # N=2 docs, one matching doc with tf=2, dl=5; other dl=5.
        _add_raw(db, "alpha alpha beta gamma delta")  # dl=5
        _add_raw(db, "beta gamma delta epsilon zeta")   # dl=5
        rag_bm25.rebuild_index(db)
        hits = rag_bm25.search(db, "alpha", top_k=1)
        assert len(hits) == 1
        n_docs, df, tf, dl, avgdl = 2, 1, 2, 5, 5.0
        k1, b = rag_bm25.DEFAULT_K1, rag_bm25.DEFAULT_B
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        expected = idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
        assert abs(hits[0]["score"] - expected) < 1e-9

    def test_legacy_lazy_rebuild_on_first_search(self, tmp_path):
        """A base with chunks but no BM25 tables is rebuilt transparently."""
        db = _make_db(tmp_path)
        _add_raw(db, "ленивый ребилд работает")
        assert rag_bm25.is_ready(db) is False
        hits = rag_bm25.search(db, "ребилд", top_k=3)
        assert len(hits) == 1
        assert rag_bm25.is_ready(db) is True

    def test_top_k_junk_falls_back_to_five(self, tmp_path):
        db = self._base(tmp_path)
        hits = rag_bm25.search(db, "собака", top_k="junk")
        assert len(hits) == 2


# ─── incremental hooks ───────────────────────────────────────────────────────

class TestIncrementalHooks:
    def test_hook_noop_when_not_ready(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, "текст")
        assert rag_bm25.on_chunk_added(db, 1, "текст") is False
        assert rag_bm25.is_ready(db) is False

    def test_added_chunk_becomes_searchable(self, tmp_path):
        db = _make_db(tmp_path)
        cid = _add_raw(db, "исходный текст")
        rag_bm25.rebuild_index(db)
        assert rag_bm25.search(db, "исходный") != []
        # Simulate the rag_index.add_chunk hook.
        cid2 = _add_raw(db, "дополнительный фрагмент")
        assert rag_bm25.on_chunk_added(db, cid2, "дополнительный фрагмент") is True
        hits = rag_bm25.search(db, "фрагмент", top_k=5)
        assert [h["chunk_id"] for h in hits] == [cid2]
        stats = rag_bm25.stats(db)
        assert stats["docs"] == 2

    def test_updated_chunk_replaces_postings(self, tmp_path):
        db = _make_db(tmp_path)
        cid = _add_raw(db, "старое слово")
        rag_bm25.rebuild_index(db)
        assert rag_bm25.on_chunk_updated(db, cid, "новое слово") is True
        assert rag_bm25.search(db, "старое") == []
        hits = rag_bm25.search(db, "новое", top_k=3)
        assert [h["chunk_id"] for h in hits] == [cid]
        # Counters must not double-count the same chunk.
        assert rag_bm25.stats(db)["docs"] == 1

    def test_deleting_hook_adjusts_counters(self, tmp_path):
        db = _make_db(tmp_path)
        cid = _add_raw(db, "удаляемый текст")
        _add_raw(db, "остающийся текст")
        rag_bm25.rebuild_index(db)
        assert rag_bm25.stats(db)["docs"] == 2
        assert rag_bm25.on_chunk_deleting(db, cid) is True
        # Simulate the actual chunk DELETE (FK cascade removes postings).
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
        conn.commit()
        conn.close()
        stats = rag_bm25.stats(db)
        assert stats["docs"] == 1
        assert rag_bm25.search(db, "удаляемый") == []
        assert rag_bm25.search(db, "остающийся") != []

    def test_drop_index_resets_state(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, "текст")
        rag_bm25.rebuild_index(db)
        assert rag_bm25.is_ready(db) is True
        rag_bm25.drop_index(db)
        assert rag_bm25.is_ready(db) is False
        assert rag_bm25.stats(db)["docs"] == 0
        # Tables were dropped: the next search transparently rebuilds them.
        assert rag_bm25.search(db, "текст") != []
        assert rag_bm25.is_ready(db) is True
        assert rag_bm25.stats(db)["docs"] == 1
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
