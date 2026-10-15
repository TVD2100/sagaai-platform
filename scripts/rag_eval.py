#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rag_eval.py - offline quality metrics for the RAG lexical search (BM25).

The platform ships tests for the MECHANICS of the lexical index (score
formula, lazy rebuild, incremental hooks) but nothing measures the QUALITY
of its output. Without such a measurement every search change (stemming,
metadata separation, stop-words) is judged by taste, and an external search
engine cannot be evaluated against a baseline either.

This script closes that gap. It indexes a benchmark corpus with the real
``core.rag_bm25`` implementation and scores a ground-truth query set with
three standard metrics:

  * Recall@k - share of the marked relevant chunks found in the top-k;
  * MRR      - reciprocal rank of the first relevant hit;
  * nDCG@k   - rank-weighted quality with binary gains.

Relevance is expressed as ``(source, chunk_index)`` pairs, so the same query
set works against a corpus fixture or against a real base index.

Usage:

    python scripts/rag_eval.py                      # bundled fixture
    python scripts/rag_eval.py --per-query          # per-query breakdown
    python scripts/rag_eval.py --json               # machine-readable
    python scripts/rag_eval.py --index-db <index.db>  # evaluate a real base

Exit code 0 on success, 1 when the fixtures are missing or invalid.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


DEFAULT_CORPUS = os.path.join(
    REPO_ROOT, "tests", "fixtures", "rag_eval", "corpus.json")
DEFAULT_QUERIES = os.path.join(
    REPO_ROOT, "tests", "fixtures", "rag_eval", "queries.json")

DEFAULT_RECALL_K = 5
DEFAULT_NDCG_K = 10


# ─── fixtures ────────────────────────────────────────────────────────────────

def _read_json(path: str):
    """Read a JSON file (raises RuntimeError with a readable message)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise RuntimeError(f"cannot read {path}: {e}") from e
    except ValueError as e:
        raise RuntimeError(f"invalid JSON in {path}: {e}") from e


def load_corpus(path: str) -> list:
    """Return the corpus chunk list (``[{source, chunk_index, text}]``)."""
    data = _read_json(path)
    chunks = data.get("chunks") if isinstance(data, dict) else None
    if not isinstance(chunks, list) or not chunks:
        raise RuntimeError(f"no 'chunks' list in {path}")
    out = []
    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or not str(chunk.get("text") or ""):
            raise RuntimeError(f"chunk #{i} has no text in {path}")
        out.append({
            "source": str(chunk.get("source") or ""),
            "chunk_index": int(chunk.get("chunk_index") or 0),
            "text": str(chunk["text"]),
        })
    return out


def load_queries(path: str) -> list:
    """Return the query list (``[{id, query, relevant: [[src, idx], ...]}]``)."""
    data = _read_json(path)
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list) or not queries:
        raise RuntimeError(f"no 'queries' list in {path}")
    out = []
    for i, item in enumerate(queries):
        if not isinstance(item, dict) or not str(item.get("query") or "").strip():
            raise RuntimeError(f"query #{i} has no text in {path}")
        relevant = []
        for pair in (item.get("relevant") or []):
            try:
                relevant.append((str(pair[0]), int(pair[1])))
            except (TypeError, ValueError, IndexError):
                continue
        out.append({
            "id": str(item.get("id") or f"q{i}"),
            "query": str(item["query"]).strip(),
            "relevant": relevant,
        })
    return out


# ─── index construction ──────────────────────────────────────────────────────

def build_index(corpus: list, db_path: str) -> None:
    """Index *corpus* into a fresh SQLite index at *db_path*.

    Uses the real production path (core.rag_index create/add + the
    core.rag_bm25 rebuild), so the benchmark measures shipped code.
    """
    from core.rag_index import add_chunk, create_index_db
    from core.rag_bm25 import rebuild_index

    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    create_index_db(db_path, dimension=256, provider="fixture",
                    embedding_model="")
    for chunk in corpus:
        add_chunk(db_path, chunk["text"], source=chunk["source"],
                  chunk_index=chunk["chunk_index"])
    rebuild_index(db_path)


def chunk_keys(db_path: str) -> dict:
    """Return ``{chunk_id: (source, chunk_index)}`` for a base index."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, source, chunk_index FROM chunks").fetchall()
    finally:
        conn.close()
    return {int(r[0]): (str(r[1] or ""), int(r[2] or 0)) for r in rows}


# ─── metrics ─────────────────────────────────────────────────────────────────

def recall_at_k(ranked: list, relevant: set, k: int) -> float:
    """Share of the relevant items found within the first *k* positions."""
    if not relevant:
        return 0.0
    found = len([key for key in ranked[:k] if key in relevant])
    return found / len(relevant)


def reciprocal_rank(ranked: list, relevant: set) -> float:
    """Reciprocal rank of the FIRST relevant hit (0.0 when none)."""
    for pos, key in enumerate(ranked, start=1):
        if key in relevant:
            return 1.0 / pos
    return 0.0


def ndcg_at_k(ranked: list, relevant: set, k: int) -> float:
    """Normalised discounted cumulative gain with binary gains."""
    if not relevant:
        return 0.0
    dcg = 0.0
    for pos, key in enumerate(ranked[:k], start=1):
        if key in relevant:
            dcg += 1.0 / math.log2(pos + 1)
    ideal = 0.0
    for pos in range(1, min(k, len(relevant)) + 1):
        ideal += 1.0 / math.log2(pos + 1)
    if ideal <= 0.0:
        return 0.0
    return dcg / ideal


# ─── evaluation ──────────────────────────────────────────────────────────────

def evaluate(*, corpus_path: str = DEFAULT_CORPUS,
             queries_path: str = DEFAULT_QUERIES,
             index_db: str = "",
             recall_k: int = DEFAULT_RECALL_K,
             ndcg_k: int = DEFAULT_NDCG_K) -> dict:
    """Score the query set against the index and return the metric report.

    When *index_db* is empty, a temporary index is built from the corpus
    fixture; otherwise the given database is used as is (a real base index).
    Returns ``{"ok", "queries", "metrics", "rows", "index_db"}``.
    """
    from core import rag_bm25

    corpus = load_corpus(corpus_path)
    query_items = load_queries(queries_path)

    tmp_dir = ""
    try:
        if not index_db:
            tmp_dir = tempfile.mkdtemp(prefix="rag_eval_")
            index_db = os.path.join(tmp_dir, "index.db")
            build_index(corpus, index_db)
        keys = chunk_keys(index_db)
        fetch_k = max(int(recall_k), int(ndcg_k))

        rows = []
        for item in query_items:
            relevant = set(item["relevant"])
            if not relevant:
                continue
            hits = rag_bm25.search(index_db, item["query"], top_k=fetch_k)
            ranked = []
            for hit in hits:
                key = keys.get(int(hit.get("chunk_id") or -1))
                if key is not None:
                    ranked.append(key)
            rows.append({
                "id": item["id"],
                "query": item["query"],
                "relevant": sorted(relevant),
                "top": ranked[:fetch_k],
                "recall": recall_at_k(ranked, relevant, recall_k),
                "rr": reciprocal_rank(ranked, relevant),
                "ndcg": ndcg_at_k(ranked, relevant, ndcg_k),
            })
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _mean(key: str) -> float:
        if not rows:
            return 0.0
        return sum(r[key] for r in rows) / len(rows)

    return {
        "ok": True,
        "queries": len(rows),
        "index_db": index_db,
        "metrics": {
            f"recall@{int(recall_k)}": round(_mean("recall"), 4),
            "mrr": round(_mean("rr"), 4),
            f"ndcg@{int(ndcg_k)}": round(_mean("ndcg"), 4),
        },
        "rows": rows,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _print_report(report: dict, per_query: bool, recall_k: int,
                  ndcg_k: int) -> None:
    """Print a human-readable metric table."""
    print(f"Queries evaluated: {report['queries']}")
    print(f"Index: {report['index_db']}")
    print()
    print(f"| {'query':<24} | Recall@{recall_k} | MRR   | nDCG@{ndcg_k} |")
    print(f"| {'-' * 24} | {'-' * 9} | {'-' * 5} | {'-' * 8} |")
    for row in report["rows"]:
        print(f"| {row['id'][:24]:<24} | {row['recall']:>9.3f} | "
              f"{row['rr']:>5.3f} | {row['ndcg']:>8.3f} |")
    print()
    for name, value in report["metrics"].items():
        print(f"{name}: {value:.4f}")
    if per_query:
        print()
        for row in report["rows"]:
            print(f"--- {row['id']}: {row['query']!r}")
            print(f"    relevant: {row['relevant']}")
            print(f"    top-k:    {row['top']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=DEFAULT_CORPUS,
                        help="benchmark corpus JSON (default: bundled fixture)")
    parser.add_argument("--queries", default=DEFAULT_QUERIES,
                        help="ground-truth query set JSON (default: bundled fixture)")
    parser.add_argument("--index-db", default="",
                        help="evaluate an existing base index instead of the corpus")
    parser.add_argument("--recall-k", type=int, default=DEFAULT_RECALL_K)
    parser.add_argument("--ndcg-k", type=int, default=DEFAULT_NDCG_K)
    parser.add_argument("--per-query", action="store_true",
                        help="also dump the ranked keys per query")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable report on stdout")
    args = parser.parse_args(argv)

    try:
        report = evaluate(
            corpus_path=args.corpus,
            queries_path=args.queries,
            index_db=args.index_db,
            recall_k=args.recall_k,
            ndcg_k=args.ndcg_k,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report, args.per_query, args.recall_k, args.ndcg_k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
