"""
core.rag_bm25 - own BM25 lexical index for RAG knowledge bases.

Every RAG base can be searched lexically (BM25, pure Python, no API keys).
The postings live in the base's existing local SQLite index
(``<base_dir>/index.db``) in two extra tables:

  bm25_terms(term TEXT, chunk_id INTEGER, tf INTEGER)  -- per-chunk term freq
  bm25_docs(chunk_id INTEGER PRIMARY KEY, dl INTEGER)  -- document lengths

Cached counters live in the shared ``meta`` table (bm25_ready,
bm25_docs_count, bm25_total_len), mirroring the style of core.rag_index.

Lifecycle:

  * a base indexed by older versions has no BM25 tables: the first search
    triggers one full lazy rebuild (``ensure_ready``), after which the index
    is maintained incrementally by the hooks below (called from the
    core.rag_index write functions);
  * tokenization is Unicode-aware (lowercase, ``ё`` folds to ``е``) and
    covers Russian and English out of the box.

Scoring (Lucene-style, k1=1.5, b=0.75 by default):

  idf(t)   = ln(1 + (N - df + 0.5) / (df + 0.5))
  score(d) = sum_t idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl/avgdl))

No streamlit imports; every function takes an explicit database path.
"""
import math
import os
import re
import sqlite3


# Unicode word tokens: letters and digits of any script, underscore excluded.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Standard Lucene-style BM25 parameters (validated against the bundled
# bases: k1=1.5 / b=0.75 keeps the best top-1 hit among the tested grid).
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

# meta keys of the cached BM25 state (shared meta table).
_READY_KEY = "bm25_ready"
_DOCS_KEY = "bm25_docs_count"
_LEN_KEY = "bm25_total_len"


# ─── tokenization ────────────────────────────────────────────────────────────

def tokenize(text: str) -> list:
    """Return lowercase Unicode word tokens of *text*.

    Punctuation and underscores are separators; ``ё`` is folded to ``е`` so
    queries like ``еще`` also match ``ещё`` in the documents.
    """
    normalized = str(text or "").lower().replace("ё", "е")
    return TOKEN_RE.findall(normalized)


# ─── low-level helpers ───────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled.

    Kept local to this module so it stays standalone; mirrors the connection
    settings of core.rag_index (ON DELETE CASCADE support).
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the BM25 tables (idempotent). Caller owns the transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bm25_docs (
               chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id)
               ON DELETE CASCADE,
               dl INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bm25_terms (
               term TEXT NOT NULL,
               chunk_id INTEGER NOT NULL REFERENCES chunks(id)
               ON DELETE CASCADE,
               tf INTEGER NOT NULL,
               PRIMARY KEY (term, chunk_id)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bm25_terms_chunk "
        "ON bm25_terms(chunk_id)"
    )
    return True


def _read_meta_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    """Read one integer meta value (default on missing/unreadable)."""
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return int(row["v"])
    except (TypeError, ValueError):
        return default


def _write_meta(conn: sqlite3.Connection, key: str, value) -> None:
    """Upsert one meta value."""
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, str(value)),
    )


def _init_counter(conn: sqlite3.Connection, key: str) -> None:
    """Make sure a counter row exists before an incremental bump."""
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, '0') ON CONFLICT(k) DO NOTHING",
        (key,),
    )


def _bump_meta(conn: sqlite3.Connection, key: str, delta: int) -> None:
    """Adjust one cached counter in meta (row must exist)."""
    conn.execute(
        "UPDATE meta SET v = CAST(v AS INTEGER) + ? WHERE k = ?",
        (int(delta), key),
    )


# ─── readiness / lifecycle ───────────────────────────────────────────────────

def is_ready(db_path: str) -> bool:
    """True when the BM25 index of the base is built and up to date."""
    if not os.path.exists(db_path):
        return False
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT v FROM meta WHERE k = ?", (_READY_KEY,)
            ).fetchone()
            return row is not None and str(row["v"]) == "1"
        finally:
            conn.close()
    except Exception:
        return False


def rebuild_index(db_path: str) -> dict:
    """Rebuild the whole BM25 index from the chunks table.

    Returns ``{"ok": True, "docs", "tokens", "terms"}`` or
    ``{"ok": False, "error": ...}``. Idempotent; the ready flag is set
    only when the rebuild fully succeeded.
    """
    if not os.path.exists(db_path):
        return {"ok": False, "error": "index database not found"}
    try:
        conn = _connect(db_path)
        try:
            ensure_schema(conn)
            conn.execute("DELETE FROM bm25_terms")
            conn.execute("DELETE FROM bm25_docs")
            rows = conn.execute("SELECT id, text FROM chunks").fetchall()
            docs = 0
            total_len = 0
            term_rows = 0
            for row in rows:
                toks = tokenize(row["text"])
                if not toks:
                    continue
                counts: dict = {}
                for token in toks:
                    counts[token] = counts.get(token, 0) + 1
                cid = int(row["id"])
                conn.executemany(
                    "INSERT OR REPLACE INTO bm25_terms(term, chunk_id, tf)"
                    " VALUES(?, ?, ?)",
                    [(t, cid, tf) for t, tf in counts.items()],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO bm25_docs(chunk_id, dl) "
                    "VALUES(?, ?)",
                    (cid, len(toks)),
                )
                docs += 1
                total_len += len(toks)
                term_rows += len(counts)
            _write_meta(conn, _DOCS_KEY, docs)
            _write_meta(conn, _LEN_KEY, total_len)
            _write_meta(conn, _READY_KEY, 1)
            conn.commit()
            return {"ok": True, "docs": docs, "tokens": total_len,
                    "terms": term_rows}
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ensure_ready(db_path: str) -> bool:
    """Ensure the BM25 index exists (lazy one-time rebuild for legacy bases)."""
    if is_ready(db_path):
        return True
    return bool(rebuild_index(db_path).get("ok"))


def drop_index_conn(conn: sqlite3.Connection) -> None:
    """Drop the BM25 tables inside an existing connection (no commit).

    Used by core.rag_index.reset_index; the caller owns the transaction.
    """
    conn.execute("DROP TABLE IF EXISTS bm25_terms")
    conn.execute("DROP TABLE IF EXISTS bm25_docs")
    conn.execute(
        "DELETE FROM meta WHERE k IN (?, ?, ?)",
        (_READY_KEY, _DOCS_KEY, _LEN_KEY),
    )


def drop_index(db_path: str) -> None:
    """Drop the BM25 tables and cached state of a base (never raises)."""
    if not os.path.exists(db_path):
        return
    try:
        conn = _connect(db_path)
        try:
            drop_index_conn(conn)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ─── incremental maintenance (called from core.rag_index) ────────────────────

def _remove_chunk_rows(conn: sqlite3.Connection, chunk_id: int) -> bool:
    """Drop postings/doc row of one chunk and adjust the counters."""
    cid = int(chunk_id)
    row = conn.execute(
        "SELECT dl FROM bm25_docs WHERE chunk_id = ?", (cid,)
    ).fetchone()
    conn.execute("DELETE FROM bm25_terms WHERE chunk_id = ?", (cid,))
    conn.execute("DELETE FROM bm25_docs WHERE chunk_id = ?", (cid,))
    if row is None:
        return False
    # Positional access: this helper also runs on foreign connections
    # (core.rag_index) whose row factory may be the default tuple.
    dl = int(row[0] or 0)
    _init_counter(conn, _DOCS_KEY)
    _init_counter(conn, _LEN_KEY)
    _bump_meta(conn, _DOCS_KEY, -1)
    _bump_meta(conn, _LEN_KEY, -dl)
    return True


def index_chunk(conn: sqlite3.Connection, chunk_id: int, text: str) -> int:
    """(Re)index one chunk inside an open connection. Returns term count.

    The caller commits. An existing entry for the same chunk id is replaced
    (the old doc length is subtracted from the cached counters).
    """
    cid = int(chunk_id)
    _remove_chunk_rows(conn, cid)
    toks = tokenize(text)
    if not toks:
        return 0
    counts: dict = {}
    for token in toks:
        counts[token] = counts.get(token, 0) + 1
    conn.executemany(
        "INSERT OR REPLACE INTO bm25_terms(term, chunk_id, tf) VALUES(?, ?, ?)",
        [(t, cid, tf) for t, tf in counts.items()],
    )
    conn.execute(
        "INSERT OR REPLACE INTO bm25_docs(chunk_id, dl) VALUES(?, ?)",
        (cid, len(toks)),
    )
    _init_counter(conn, _DOCS_KEY)
    _init_counter(conn, _LEN_KEY)
    _bump_meta(conn, _DOCS_KEY, 1)
    _bump_meta(conn, _LEN_KEY, len(toks))
    return len(counts)


def on_chunk_added(db_path: str, chunk_id: int, text: str) -> bool:
    """Keep the BM25 index in sync after a chunk write (never raises).

    No-op when the base was never BM25-indexed yet (legacy base): the first
    search performs one full rebuild instead of thousands of tiny writes.
    """
    try:
        if not is_ready(db_path):
            return False
        conn = _connect(db_path)
        try:
            ensure_schema(conn)
            index_chunk(conn, chunk_id, text)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def on_chunk_updated(db_path: str, chunk_id: int, text: str) -> bool:
    """Re-index a chunk whose text changed (same upsert as on_chunk_added)."""
    return on_chunk_added(db_path, chunk_id, text)


def remove_chunk_conn(conn: sqlite3.Connection, chunk_id: int) -> bool:
    """Drop BM25 postings of a chunk inside an OPEN connection (never raises).

    Same-connection variant of :func:`on_chunk_deleting` for callers that
    already hold a transaction on the index database (core.rag_index).
    """
    try:
        return _remove_chunk_rows(conn, chunk_id)
    except Exception:
        return False


def on_chunk_deleting(db_path: str, chunk_id: int) -> bool:
    """Sync counters BEFORE a chunk row is deleted (never raises).

    After this call the FK cascade in the chunk DELETE removes any rows that
    might remain, so no stale postings survive.
    """
    try:
        if not is_ready(db_path):
            return False
        conn = _connect(db_path)
        try:
            _remove_chunk_rows(conn, chunk_id)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


# ─── search ──────────────────────────────────────────────────────────────────

def search(db_path: str, query: str, top_k: int = 5, min_score: float = 0.0,
           k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> list:
    """Run an own-BM25 search over the base index.

    Returns hits sorted by descending score with the same shape as the
    vector search (``chunk_id``, ``text``, ``source``, ``chunk_index``,
    ``score``). Empty list for a missing database, blank query or an empty
    index. Non-positive scores are never produced (idf >= 0).
    """
    if not os.path.exists(db_path):
        return []
    terms: list = []
    seen = set()
    for token in tokenize(query):
        if token not in seen:
            seen.add(token)
            terms.append(token)
    if not terms:
        return []
    if not ensure_ready(db_path):
        return []
    try:
        conn = _connect(db_path)
        try:
            n_docs = _read_meta_int(conn, _DOCS_KEY, 0)
            total_len = _read_meta_int(conn, _LEN_KEY, 0)
            if n_docs <= 0:
                return []
            avgdl = total_len / n_docs if n_docs else 0.0
            scores: dict = {}
            for term in terms:
                rows = conn.execute(
                    "SELECT t.chunk_id AS cid, t.tf AS tf,"
                    " COALESCE(d.dl, 0) AS dl"
                    " FROM bm25_terms t"
                    " LEFT JOIN bm25_docs d ON d.chunk_id = t.chunk_id"
                    " WHERE t.term = ?",
                    (term,),
                ).fetchall()
                df = len(rows)
                if not df:
                    continue
                idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                for row in rows:
                    tf = int(row["tf"] or 0)
                    if tf <= 0:
                        continue
                    dl = int(row["dl"] or 0)
                    denom = tf + k1 * (
                        1.0 - b + b * (dl / avgdl if avgdl else 0.0)
                    )
                    if denom <= 0:
                        continue
                    cid = int(row["cid"])
                    scores[cid] = scores.get(cid, 0.0) + (
                        idf * tf * (k1 + 1.0) / denom
                    )
            if not scores:
                return []
            try:
                limit = max(1, int(top_k))
            except (TypeError, ValueError):
                limit = 5
            ranked = sorted(
                scores.items(), key=lambda kv: (-kv[1], kv[0])
            )[:limit]
            ids = [cid for cid, _ in ranked]
            placeholders = ",".join("?" for _ in ids)
            chunk_rows = conn.execute(
                "SELECT id, text, source, chunk_index FROM chunks"
                f" WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            by_id = {int(r["id"]): r for r in chunk_rows}
        finally:
            conn.close()
    except Exception:
        return []

    hits = []
    try:
        floor = float(min_score or 0.0)
    except (TypeError, ValueError):
        floor = 0.0
    for cid, score in ranked:
        if floor and score < floor:
            continue
        row = by_id.get(cid)
        if row is None:
            continue
        hits.append({
            "chunk_id": cid,
            "text": row["text"],
            "source": row["source"] or "",
            "chunk_index": int(row["chunk_index"] or 0),
            "score": float(score),
        })
    return hits


def stats(db_path: str) -> dict:
    """Return diagnostic BM25 stats (sizes only; safe on missing files)."""
    out = {"ready": False, "docs": 0, "tokens": 0, "posting_rows": 0}
    if not os.path.exists(db_path):
        return out
    try:
        conn = _connect(db_path)
        try:
            out["ready"] = is_ready(db_path)
            out["docs"] = _read_meta_int(conn, _DOCS_KEY, 0)
            out["tokens"] = _read_meta_int(conn, _LEN_KEY, 0)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM bm25_terms"
                ).fetchone()
                out["posting_rows"] = int(row["n"])
            except Exception:
                out["posting_rows"] = 0
        finally:
            conn.close()
    except Exception:
        pass
    return out
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
