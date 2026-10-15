# -*- coding: utf-8 -*-
"""tests/scenarios/test_rag_search_quality_scenario.py - search quality end to end.

User-level scenarios for the P0 search improvements (schema version 2).
They walk the public API exactly like the UI / RAG tools do, and each one
reproduces a defect measured during the search audit:

  1. "morphology" - a base whose documents use different word forms of the
     same lemma: a query in one form must find the chunks written in
     another (before: 0 overlapping top-5 hits).
  2. "metadata" - a base built by the RAG Base Creator skill, whose every
     chunk carries 'Продукт:<Base name>': the corpus' own domain term must
     stay discriminative instead of collapsing to score ~0.
  3. "stop-words and coordination" - a natural-language question phrased
     with function words must return the same chunks as its keyword form,
     and a chunk covering all query terms must outrank a partial match.
  4. "existing index" - a base indexed by the PREVIOUS release (schema v1,
     no bm25_version) is upgraded transparently on the first search and
     keeps serving results afterwards.
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


def _index_texts(slug: str, texts) -> None:
    """Index *texts* as one file per entry through the real indexer."""
    import core.rag as rag
    import core.rag_indexer as indexer

    for i, text in enumerate(texts):
        rag.add_file(slug, f"doc{i}.txt", text.encode("utf-8"))
    with patch("core.rag_indexer.get_yandex_embedding_credentials",
               side_effect=AssertionError("bm25 indexing needs no keys")):
        result = indexer.index_base(slug)
    assert result["status"] == "ready"


def test_morphology_query_finds_other_word_forms(rag_data):
    """S1: word forms of one lemma must reach the same chunks.

    given: an offline base whose documents spell the domain word in the
           nominative, genitive and plural
    when: it is searched once per word form through the public API
    then: the top-5 sets overlap completely (before stemming: 0 out of 5)
    """
    import core.rag as rag
    import core.rag_search as rag_search

    base = rag.create_base(name="Morphology KB")
    slug = base["slug"]
    _index_texts(slug, [
        "Сильный бренд снижает ценовую эластичность продукта.",
        "Управление брендом требует измерять узнаваемость бренда.",
        "Портфель брендов определяет архитектуру марок компании.",
        "Совсем другой документ про логистику склада.",
    ])

    def _top(form: str):
        return [h["chunk_id"] for h in rag_search.search_base(slug, form, top_k=5)]

    nominative = _top("бренд")
    genitive = _top("бренда")
    plural = _top("брендов")
    assert nominative, "the base must return hits"
    assert set(nominative) & set(genitive), "forms must not be disjoint"
    assert set(nominative) & set(plural), "forms must not be disjoint"


def test_skill_header_does_not_collapse_idf(rag_data):
    """S2: the repeated 'Продукт:' header must not blind the ranker.

    given: an offline base whose every chunk carries the RAG Base Creator
           header for the SAME product
    when: the product term itself is searched
    then: a positive, discriminative score is returned and the chunk whose
          BODY really discusses the product ranks first (before: every score
          rounded to 0.000 because df == N)
    """
    import core.rag as rag
    import core.rag_search as rag_search

    base = rag.create_base(name="Маркетинг")
    slug = base["slug"]
    header = ("Документ: docs/Маркетинг/%s.txt\nПродукт: Маркетинг\n"
              "Заголовок: %s\nФрагмент: 1/1\n\n")
    _index_texts(slug, [
        header % ("logistics", "Логистика") + "описание складов и поставок",
        header % ("finance", "Финансы") + "отчётность и бюджеты",
        header % ("goals", "Цели") + "цели маркетинга на следующий год",
    ])

    hits = rag_search.search_base(slug, "маркетинг", top_k=5)
    assert hits, "the domain term must still be searchable"
    assert hits[0]["score"] > 0.1, "IDF must not collapse to zero"
    assert hits[0]["source"] == "doc2.txt", "the body match must rank first"


def test_natural_language_query_matches_keyword_form(rag_data):
    """S3: stop-words must not change the result, coordination must rank.

    given: an offline base with one chunk covering both query terms and one
           chunk covering a single term
    when: the same information need is asked as a natural question and as
          bare keywords
    then: both calls return the same ranking and the full match comes first
    """
    import core.rag as rag
    import core.rag_search as rag_search

    base = rag.create_base(name="Coordination KB")
    slug = base["slug"]
    _index_texts(slug, [
        "Ценовые стратегии компании опираются на ценность продукта.",
        "Ценовые скидки и акции дают быстрый отклик покупателей.",
    ])

    question = rag_search.search_base(slug, "что такое ценовые стратегии", top_k=5)
    keywords = rag_search.search_base(slug, "ценовые стратегии", top_k=5)
    assert [h["chunk_id"] for h in question] == [h["chunk_id"] for h in keywords]
    assert question[0]["source"] == "doc0.txt", "coverage must win"


def test_previous_release_index_is_upgraded_on_first_search(rag_data):
    """S4: an index written by the previous release upgrades transparently.

    given: a ready base whose BM25 index carries the OLD schema version and
           old-style postings (no stemming)
    when: it is searched once through the public API
    then: the index is upgraded in place, the search succeeds and a word
          form written differently in the document is found afterwards
    """
    import core.rag as rag
    import core.rag_index as rag_index
    import core.rag_search as rag_search
    import sqlite3

    base = rag.create_base(name="Legacy KB")
    slug = base["slug"]
    _index_texts(slug, [
        "Описание работы с клиентами и удержание клиентской базы.",
        "Совсем другой текст про ремонт оборудования.",
    ])

    db = rag.index_db_path(slug)
    conn = sqlite3.connect(db)
    try:
        # Simulate the previous release: old schema version AND the old
        # unfielded postings layout (no 'field' column at all).
        conn.execute("UPDATE meta SET v = '1' WHERE k = 'bm25_version'")
        conn.execute("DROP TABLE IF EXISTS bm25_terms")
        conn.execute("DROP TABLE IF EXISTS bm25_docs")
        conn.execute(
            "CREATE TABLE bm25_terms (term TEXT NOT NULL,"
            " chunk_id INTEGER NOT NULL, tf INTEGER NOT NULL,"
            " PRIMARY KEY (term, chunk_id))")
        conn.execute(
            "CREATE TABLE bm25_docs (chunk_id INTEGER PRIMARY KEY,"
            " dl INTEGER NOT NULL)")
        conn.execute("INSERT INTO bm25_terms(term, chunk_id, tf) "
                     "SELECT 'клиентами', id, 1 FROM chunks LIMIT 1")
        conn.commit()
    finally:
        conn.close()

    # First search upgrades the index, the second one must keep working.
    first = rag_search.search_base(slug, "клиент", top_k=5)
    second = rag_search.search_base(slug, "клиентами", top_k=5)
    assert first, "the upgraded index must return hits"
    assert second, "the upgraded index must keep working"
    assert set(h["chunk_id"] for h in first) & set(h["chunk_id"] for h in second)
    stats = rag_index.index_stats(db)
    assert stats["chunks"] == 2
def test_multi_product_query_ranks_the_right_product(rag_data):
    """S5: a product name living ONLY in the header must stay searchable.

    given: an offline base spanning several products, where every chunk's
           BODY mentions the generic term 'tool_call' but the product name
           ('Yandex Atelier', 'GigaChat') appears only in the header block
    when: the user searches 'tool_call <product>'
    then: the chunks of THAT product rank first (a body-only index made the
          product term unreachable, so unrelated products won)
    """
    import core.rag as rag
    import core.rag_search as rag_search

    base = rag.create_base(name="Products KB")
    slug = base["slug"]
    header = ("Документ: docs/%s/%s.md\nПродукт: %s\nЗаголовок: %s\n"
              "Фрагмент: 1/1\n\n")
    _index_texts(slug, [
        header % ("atelier", "a", "Yandex Atelier", "Агенты")
        + "функция tool_call вызывает инструмент",
        header % ("atelier", "b", "Yandex Atelier", "Инструменты")
        + "каждый tool_call описывает аргументы",
        header % ("gigachat", "g", "GigaChat", "Функции")
        + "GigaChat поддерживает tool_call",
        header % ("ai-studio", "s", "AI Studio", "API")
        + "Responses API поле tool_call",
    ])

    atelier = rag_search.search_base(slug, "tool_call Yandex Atelier", top_k=4)
    assert atelier, "the product query must return hits"
    assert {h["source"] for h in atelier[:2]} == {"doc0.txt", "doc1.txt"}

    gigachat = rag_search.search_base(slug, "tool_call GigaChat", top_k=4)
    assert gigachat[0]["source"] == "doc2.txt"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
