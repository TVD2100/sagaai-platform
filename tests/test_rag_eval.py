# -*- coding: utf-8 -*-
"""tests/test_rag_eval.py - unit tests for scripts/rag_eval.py.

Covers the offline search-quality harness:
  - the three metrics on hand-written rankings whose values are known;
  - fixture loading and validation errors;
  - an end-to-end run over the bundled corpus/query set;
  - the recorded quality baseline, so a future search change that drops
    relevance is caught by the suite instead of being noticed in production.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.rag_eval as rag_eval  # noqa: E402


FIXTURE_CORPUS = Path(__file__).resolve().parent / "fixtures" / "rag_eval" / "corpus.json"
FIXTURE_QUERIES = Path(__file__).resolve().parent / "fixtures" / "rag_eval" / "queries.json"

# Quality baseline measured before the P0 search work (see CHANGELOG):
#   recall@5 0.8472, mrr 1.0000, ndcg@10 0.8879.
# The floor is set slightly below the baseline so the test fails on a real
# regression, not on harmless reordering.
BASELINE_RECALL_AT_5 = 0.80
BASELINE_MRR = 0.95
BASELINE_NDCG_AT_10 = 0.85


# ─── metric correctness ──────────────────────────────────────────────────────

class TestMetrics:
    def test_recall_at_k_perfect_and_partial(self):
        relevant = {("a", 0), ("b", 0)}
        ranked = [("a", 0), ("b", 0), ("c", 0)]
        assert rag_eval.recall_at_k(ranked, relevant, 5) == 1.0
        assert rag_eval.recall_at_k(ranked, relevant, 1) == 0.5
        assert rag_eval.recall_at_k([("c", 0)], relevant, 5) == 0.0

    def test_recall_at_k_without_relevant_is_zero(self):
        assert rag_eval.recall_at_k([("a", 0)], set(), 5) == 0.0

    def test_reciprocal_rank_first_hit_position(self):
        relevant = {("b", 0)}
        assert rag_eval.reciprocal_rank([("b", 0)], relevant) == 1.0
        assert rag_eval.reciprocal_rank([("a", 0), ("b", 0)], relevant) == 0.5
        assert rag_eval.reciprocal_rank([("a", 0)], relevant) == 0.0

    def test_ndcg_perfect_ranking_is_one(self):
        relevant = {("a", 0), ("b", 0)}
        ranked = [("a", 0), ("b", 0)]
        assert rag_eval.ndcg_at_k(ranked, relevant, 10) == pytest.approx(1.0)

    def test_ndcg_penalises_lower_rank(self):
        relevant = {("a", 0), ("b", 0)}
        perfect = rag_eval.ndcg_at_k([("a", 0), ("b", 0)], relevant, 10)
        worse = rag_eval.ndcg_at_k([("x", 0), ("a", 0), ("b", 0)], relevant, 10)
        assert worse < perfect

    def test_ndcg_matches_hand_computed_value(self):
        """One relevant item ranked 2nd: DCG = 1/log2(3), IDCG = 1."""
        import math
        relevant = {("a", 0)}
        ranked = [("x", 0), ("a", 0)]
        assert rag_eval.ndcg_at_k(ranked, relevant, 10) == pytest.approx(
            1.0 / math.log2(3))


# ─── fixture loading ─────────────────────────────────────────────────────────

class TestFixtureLoading:
    def test_load_corpus_and_queries(self):
        corpus = rag_eval.load_corpus(str(FIXTURE_CORPUS))
        queries = rag_eval.load_queries(str(FIXTURE_QUERIES))
        assert len(corpus) >= 10
        assert len(queries) >= 10
        assert all(c["text"].strip() for c in corpus)
        assert all(q["query"].strip() and q["relevant"] for q in queries)

    def test_missing_file_raises_runtime_error(self, tmp_path):
        with pytest.raises(RuntimeError):
            rag_eval.load_corpus(str(tmp_path / "nope.json"))

    def test_invalid_json_raises_runtime_error(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError):
            rag_eval.load_queries(str(bad))

    def test_empty_chunks_rejected(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text('{"chunks": []}', encoding="utf-8")
        with pytest.raises(RuntimeError):
            rag_eval.load_corpus(str(empty))


# ─── end-to-end over the bundled fixture ─────────────────────────────────────

class TestBundledBenchmark:
    def test_evaluate_runs_and_reports_metrics(self):
        report = rag_eval.evaluate(
            corpus_path=str(FIXTURE_CORPUS),
            queries_path=str(FIXTURE_QUERIES),
        )
        assert report["ok"] is True
        assert report["queries"] >= 10
        assert set(report["metrics"]) == {"recall@5", "mrr", "ndcg@10"}
        for value in report["metrics"].values():
            assert 0.0 <= value <= 1.0

    def test_quality_not_below_recorded_baseline(self):
        """Guard the recorded search-quality baseline against regressions."""
        report = rag_eval.evaluate(
            corpus_path=str(FIXTURE_CORPUS),
            queries_path=str(FIXTURE_QUERIES),
        )
        metrics = report["metrics"]
        assert metrics["recall@5"] >= BASELINE_RECALL_AT_5
        assert metrics["mrr"] >= BASELINE_MRR
        assert metrics["ndcg@10"] >= BASELINE_NDCG_AT_10

    def test_word_forms_of_same_lemma_reach_the_same_chunks(self):
        """A nominative and a genitive query must overlap (anti-split probe).

        Before stemming these two queries were fully disjoint; the metric
        harness lets us assert the merge instead of eyeballing it.
        """
        report = rag_eval.evaluate(
            corpus_path=str(FIXTURE_CORPUS),
            queries_path=str(FIXTURE_QUERIES),
        )
        by_id = {row["id"]: row for row in report["rows"]}
        assert by_id["brand_nominative"]["recall"] > 0
        assert by_id["brand_genitive"]["recall"] > 0

    def test_cli_returns_zero_on_success(self):
        rc = rag_eval.main(["--corpus", str(FIXTURE_CORPUS),
                            "--queries", str(FIXTURE_QUERIES),
                            "--json"])
        assert rc == 0

    def test_cli_returns_one_on_missing_fixture(self, tmp_path):
        rc = rag_eval.main(["--corpus", str(tmp_path / "nope.json"),
                            "--queries", str(FIXTURE_QUERIES)])
        assert rc == 1
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
