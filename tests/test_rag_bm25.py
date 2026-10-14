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
# ─── text analysis: stemming and stop-words ─────────────────────────────────

class TestStemming:
    def test_russian_word_forms_share_one_stem(self):
        """The audit defect: 'бренд'/'бренда'/'брендов' must merge."""
        forms = [rag_bm25.stem_token(w)
                 for w in ("бренд", "бренда", "брендов")]
        assert len(set(forms)) == 1
        assert forms[0] == "бренд"

    def test_client_forms_share_one_stem(self):
        forms = [rag_bm25.stem_token(w)
                 for w in ("клиент", "клиента", "клиентами")]
        assert len(set(forms)) == 1
        assert forms[0] == "клиент"

    def test_marketing_forms_share_one_stem(self):
        assert rag_bm25.stem_token("маркетинга") == "маркетинг"
        assert rag_bm25.stem_token("маркетинг") == "маркетинг"

    def test_short_words_are_untouched(self):
        for word in ("кот", "дом", "оси", "код"):
            assert rag_bm25.stem_token(word) == word

    def test_latin_and_numeric_tokens_untouched(self):
        for token in ("marketing", "testing", "api", "2026"):
            assert rag_bm25.stem_token(token) == token

    def test_stem_never_shorter_than_min(self):
        for word in ("оси", "ели", "море", "поле", "синие"):
            assert len(rag_bm25.stem_token(word)) >= min(len(word), 4)


class TestStopwords:
    def test_function_words_are_dropped(self):
        terms = rag_bm25.content_terms("что такое бренд для компании")
        assert "что" not in terms
        assert "такое" not in terms
        assert "для" not in terms
        assert "бренд" in terms

    def test_stopwords_matched_by_stem(self):
        """An inflected function word ('которого') is dropped too."""
        assert "которого" not in rag_bm25.content_terms("которого нет")

    def test_english_stopwords_dropped(self):
        terms = rag_bm25.content_terms("the search and the index")
        assert "the" not in terms
        assert "and" not in terms
        assert "search" in terms

    def test_query_of_only_stopwords_returns_nothing(self, tmp_path):
        """Function words are absent from the index, so such a query is empty.

        The query still runs (no exception) and honestly reports no hits:
        there is nothing meaningful to search for.
        """
        db = _make_db(tmp_path)
        _add_raw(db, "что такое маркетинг")
        rag_bm25.rebuild_index(db)
        assert rag_bm25.search(db, "что такое", top_k=3) == []

    def test_mixed_query_uses_only_content_terms(self, tmp_path):
        """'что такое бренд' behaves exactly like the single term 'бренд'."""
        db = _make_db(tmp_path)
        _add_raw(db, "сильный бренд компании")
        _add_raw(db, "совсем другой текст про скидки")
        rag_bm25.rebuild_index(db)
        noisy = rag_bm25.search(db, "что такое бренд", top_k=5)
        clean = rag_bm25.search(db, "бренд", top_k=5)
        assert [h["chunk_id"] for h in noisy] == [h["chunk_id"] for h in clean] != []


# ─── chunk metadata separation ───────────────────────────────────────────────

class TestMetadataSeparation:
    HEADER = ("Документ: docs/a.txt\nПродукт: Маркетинг\n"
              "Заголовок: Бренд-менеджмент\nФрагмент: 1/9\n\nтело про скидки")

    def test_split_metadata_extracts_header(self):
        body, meta = rag_bm25.split_metadata(self.HEADER)
        assert meta["Документ"] == "docs/a.txt"
        assert meta["Продукт"] == "Маркетинг"
        assert meta["Заголовок"] == "Бренд-менеджмент"
        assert body.strip() == "тело про скидки"

    def test_plain_text_has_no_metadata(self):
        body, meta = rag_bm25.split_metadata("обычный текст без шапки")
        assert meta == {}
        assert body == "обычный текст без шапки"

    def test_repeated_product_header_does_not_destroy_idf(self, tmp_path):
        """The measured IDF collapse: df must stay below N.

        Every chunk carries 'Продукт: Маркетинг'; a naive index gives
        df('маркетинг') == N and idf ~ 0. The separated index must keep
        'маркетинг' discriminative and still find the one chunk whose BODY
        speaks about marketing goals.
        """
        db = _make_db(tmp_path)
        for i in range(5):
            _add_raw(db, (f"Документ: docs/{i}.txt\nПродукт: Маркетинг\n"
                          f"Заголовок: Тема {i}\nФрагмент: 1/1\n\n"
                          f"тело документа номер {i}"))
        target = _add_raw(db, ("Документ: docs/goal.txt\nПродукт: Маркетинг\n"
                               "Заголовок: Цели\nФрагмент: 1/1\n\n"
                               "цели маркетинга на год"))
        rag_bm25.rebuild_index(db)
        stats = rag_bm25.stats(db)
        hits = rag_bm25.search(db, "маркетинга", top_k=5)
        assert hits, "the single body occurrence must be found"
        assert hits[0]["chunk_id"] == target
        assert hits[0]["score"] > 0.1, "IDF must stay discriminative"
        assert stats["docs"] == 6

    def test_title_is_still_indexed_with_a_boost(self, tmp_path):
        """A title term is searchable even when absent from the body."""
        db = _make_db(tmp_path)
        cid = _add_raw(db, ("Документ: docs/t.txt\nПродукт: X\n"
                            "Заголовок: Сегментация рынка\nФрагмент: 1/1\n\n"
                            "тело без этого слова"))
        rag_bm25.rebuild_index(db)
        hits = rag_bm25.search(db, "сегментация", top_k=3)
        assert [h["chunk_id"] for h in hits] == [cid]

    def test_body_length_excludes_the_header(self, tmp_path):
        """Doc length counts body tokens only (header must not distort b)."""
        db = _make_db(tmp_path)
        _add_raw(db, ("Документ: docs/l.txt\nПродукт: Маркетинг\n"
                      "Заголовок: Заголовок\nФрагмент: 1/1\n\nодно слово"))
        rag_bm25.rebuild_index(db)
        assert rag_bm25.stats(db)["tokens"] == 2  # 'одно', 'слово'


class TestBM25FFields:
    def test_field_terms_split_header_from_body(self):
        """Body and meta terms are separated; the position marker is skipped."""
        text = ("Документ: docs/a.md\nПродукт: Маркетинг\n"
                "Заголовок: Бренд-менеджмент\nФрагмент: 1/1\n\nпро скидки")
        fields = rag_bm25.field_terms(text)
        assert "маркетинг" in fields[rag_bm25.FIELD_META]
        assert "бренд" in fields[rag_bm25.FIELD_META]
        assert "скидк" in fields[rag_bm25.FIELD_BODY]
        assert "маркетинг" not in fields[rag_bm25.FIELD_BODY]
        assert "фрагмент" not in fields[rag_bm25.FIELD_META]

    def test_product_name_only_in_header_is_searchable(self, tmp_path):
        """A product name living only in the header must be found (BM25F)."""
        db = _make_db(tmp_path)
        cid = _add_raw(db, ("Документ: docs/atelier/a.md\n"
                            "Продукт: Yandex Atelier\n"
                            "Заголовок: Справочник по агентам\nФрагмент: 1/1\n\n"
                            "функция tool_call вызывает инструмент"))
        _add_raw(db, ("Документ: docs/gigachat/g.md\nПродукт: GigaChat\n"
                      "Заголовок: Function calling\nФрагмент: 1/1\n\n"
                      "GigaChat поддерживает tool_call"))
        rag_bm25.rebuild_index(db)
        hits = rag_bm25.search(db, "Yandex Atelier", top_k=5)
        assert [h["chunk_id"] for h in hits] == [cid]

    def test_multi_product_query_ranks_correct_product_first(self, tmp_path):
        """The reported scenario: 'tool_call <product>' must favour it."""
        db = _make_db(tmp_path)
        atelier = _add_raw(db, ("Документ: docs/a/x.md\n"
                                "Продукт: Yandex Atelier\n"
                                "Заголовок: Агенты\nФрагмент: 1/1\n\n"
                                "функция tool_call вызывает инструмент"))
        _add_raw(db, ("Документ: docs/g/y.md\nПродукт: GigaChat\n"
                      "Заголовок: Функции\nФрагмент: 1/1\n\n"
                      "GigaChat поддерживает tool_call"))
        _add_raw(db, ("Документ: docs/s/z.md\nПродукт: AI Studio\n"
                      "Заголовок: API\nФрагмент: 1/1\n\n"
                      "Responses API поле tool_call"))
        rag_bm25.rebuild_index(db)
        hits = rag_bm25.search(db, "tool_call Yandex Atelier", top_k=5)
        assert hits[0]["chunk_id"] == atelier

    def test_repeated_header_term_keeps_body_match_discriminative(self, tmp_path):
        """The P0 defect must stay fixed: meta term must not blind the body."""
        db = _make_db(tmp_path)
        for i in range(5):
            _add_raw(db, (f"Документ: docs/{i}.txt\nПродукт: Маркетинг\n"
                          f"Заголовок: Тема {i}\nФрагмент: 1/1\n\n"
                          f"тело документа номер {i}"))
        target = _add_raw(db, ("Документ: docs/g.txt\nПродукт: Маркетинг\n"
                               "Заголовок: Цели\nФрагмент: 1/1\n\n"
                               "цели маркетинга на год"))
        rag_bm25.rebuild_index(db)
        hits = rag_bm25.search(db, "маркетинга", top_k=6)
        assert hits[0]["chunk_id"] == target
        assert hits[0]["score"] > 0.3

    def test_fragment_marker_is_not_indexed(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, ("Документ: docs/a.md\nПродукт: X\nЗаголовок: Y\n"
                      "Фрагмент: 2/5\n\nтело текст"))
        rag_bm25.rebuild_index(db)
        assert rag_bm25.search(db, "фрагмент") == []

    def test_stats_reports_body_and_meta_tokens(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, ("Документ: docs/a.md\nПродукт: Маркетинг\n"
                      "Заголовок: Тема\nФрагмент: 1/1\n\nодно слово"))
        rag_bm25.rebuild_index(db)
        stats = rag_bm25.stats(db)
        assert stats["tokens"] == 2
        assert stats["meta_tokens"] >= 3

    def test_legacy_unfielded_tables_trigger_rebuild(self, tmp_path):
        """Legacy tables without the 'field' column are rebuilt in place."""
        db = _make_db(tmp_path)
        _add_raw(db, "клиент клиента")
        rag_bm25.rebuild_index(db)
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TABLE bm25_terms")
            conn.execute("DROP TABLE bm25_docs")
            conn.execute("CREATE TABLE bm25_terms (term TEXT, chunk_id INTEGER,"
                         " tf INTEGER, PRIMARY KEY (term, chunk_id))")
            conn.execute("CREATE TABLE bm25_docs (chunk_id INTEGER PRIMARY KEY,"
                         " dl INTEGER)")
            conn.execute("UPDATE meta SET v = '2' WHERE k = 'bm25_version'")
            conn.commit()
        finally:
            conn.close()
        assert rag_bm25.is_ready(db) is False
        assert rag_bm25.search(db, "клиент") != []
        assert rag_bm25.stats(db)["version"] == rag_bm25.SCHEMA_VERSION


# ─── coordination and schema version ─────────────────────────────────────────

class TestCoordination:
    def test_chunk_with_all_terms_outranks_partial_match(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, "ценовые скидки и акции")            # only one term
        full = _add_raw(db, "ценовые стратегии компании")  # both terms
        rag_bm25.rebuild_index(db)
        hits = rag_bm25.search(db, "ценовые стратегии", top_k=5)
        assert hits[0]["chunk_id"] == full

    def test_coordination_does_not_drop_partial_matches(self, tmp_path):
        db = _make_db(tmp_path)
        partial = _add_raw(db, "ценовые скидки")
        _add_raw(db, "ценовые стратегии")
        rag_bm25.rebuild_index(db)
        ids = [h["chunk_id"] for h in rag_bm25.search(db, "ценовые стратегии", top_k=5)]
        assert partial in ids

    def test_single_term_query_has_no_coordination(self, tmp_path):
        db = _make_db(tmp_path)
        import math
        _add_raw(db, "бренд бренд")
        rag_bm25.rebuild_index(db)
        hits = rag_bm25.search(db, "бренд", top_k=1)
        n_docs, df, tf, dl = 1, 1, 2, 2
        k1, b = rag_bm25.DEFAULT_K1, rag_bm25.DEFAULT_B
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        expected = idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / dl))
        assert abs(hits[0]["score"] - expected) < 1e-9


class TestSchemaVersion:
    def test_rebuild_records_schema_version(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, "текст документа")
        rag_bm25.rebuild_index(db)
        stats = rag_bm25.stats(db)
        assert stats["version"] == rag_bm25.SCHEMA_VERSION
        assert stats["current_version"] == rag_bm25.SCHEMA_VERSION
        assert stats["ready"] is True

    def test_older_schema_version_forces_one_rebuild(self, tmp_path):
        """An index built by the previous analysis is rebuilt transparently."""
        db = _make_db(tmp_path)
        _add_raw(db, "старого вида индекс содержит слово бренда")
        rag_bm25.rebuild_index(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE meta SET v = '1' WHERE k = 'bm25_version'")
        conn.commit()
        conn.close()
        assert rag_bm25.is_ready(db) is False
        assert rag_bm25.search(db, "бренд", top_k=3) != []
        assert rag_bm25.is_ready(db) is True
        assert rag_bm25.stats(db)["version"] == rag_bm25.SCHEMA_VERSION

    def test_dropped_index_rebuilds_on_search(self, tmp_path):
        db = _make_db(tmp_path)
        _add_raw(db, "текст для пересборки")
        rag_bm25.rebuild_index(db)
        rag_bm25.drop_index(db)
        assert rag_bm25.is_ready(db) is False
        assert rag_bm25.search(db, "пересборка", top_k=3) != []


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
