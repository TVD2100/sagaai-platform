"""
core.rag_index - local SQLite vector index for RAG bases.

Layout of one index database (``<base_dir>/index.db``):

  meta(k TEXT PRIMARY KEY, v TEXT)          -- dimension, provider, embedding_model
  chunks(id INTEGER PK, text TEXT, source TEXT,
         chunk_index INTEGER, created_at TEXT)
  embeddings(chunk_id INTEGER PK REFERENCES chunks(id), vector BLOB)

Vectors are stored as little-endian float32 BLOBs. Cosine similarity is
computed in pure Python (no numpy dependency); 256-dim vectors are fast
enough for typical self-hosted bases.

The module also exposes chunk-level operations (get, search, update, delete)
used by the Storage UI and by the RAG Base Creator skill.

No streamlit imports. All functions take an explicit database path.
"""
import json
import math
import os
import sqlite3
import struct
from datetime import datetime, timezone


def _now() -> str:
    """Return the current UTC timestamp as an ISO string."""
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    """Open (and create directories for) a SQLite connection."""
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # Enable foreign keys so ON DELETE CASCADE removes embeddings rows
    # when their chunk is deleted (SQLite disables this by default).
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def create_index_db(db_path: str, dimension: int, provider: str = "",
                    embedding_model: str = "") -> bool:
    """Create the index schema in *db_path* and record its metadata.

    Existing tables are preserved: repeated calls are idempotent and simply
    re-store the metadata rows. Returns True on success.
    """
    try:
        conn = _connect(db_path)
        try:
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
            conn.execute(
                """CREATE TABLE IF NOT EXISTS embeddings (
                       chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id)
                       ON DELETE CASCADE,
                       vector BLOB NOT NULL
                   )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)"
            )
            for key, value in (
                ("dimension", str(int(dimension))),
                ("provider", str(provider or "")),
                ("embedding_model", str(embedding_model or "")),
            ):
                conn.execute(
                    "INSERT INTO meta(k, v) VALUES(?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (key, value),
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def pack_vector(vec) -> bytes:
    """Pack a list/tuple of floats into a little-endian float32 BLOB."""
    return struct.pack(f"<{len(vec)}f", *[float(v) for v in vec])


def unpack_vector(blob: bytes) -> tuple:
    """Unpack a float32 BLOB back into a tuple of floats."""
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def reset_index(db_path: str) -> bool:
    """Drop all chunks and embeddings (keep metadata). Returns True on success."""
    try:
        conn = _connect(db_path)
        try:
            conn.execute("DROP TABLE IF EXISTS embeddings")
            conn.execute("DROP TABLE IF EXISTS chunks")
            conn.commit()
        finally:
            conn.close()
        # Recreate the chunks/embeddings tables (metadata is preserved).
        meta = read_meta(db_path)
        return create_index_db(
            db_path,
            dimension=meta.get("dimension", 256),
            provider=meta.get("provider", ""),
            embedding_model=meta.get("embedding_model", ""),
        )
    except Exception:
        return False


def read_meta(db_path: str) -> dict:
    """Return all meta rows as a dict. Empty dict when the DB is absent."""
    if not os.path.exists(db_path):
        return {}
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT k, v FROM meta").fetchall()
            return {row["k"]: row["v"] for row in rows}
        finally:
            conn.close()
    except Exception:
        return {}


def add_chunk(db_path: str, text: str, source: str = "",
              chunk_index: int = 0, vector=None) -> int:
    """Insert a chunk (optionally with its embedding) and return its id.

    Returns -1 on failure. When *vector* is None the chunk is added without
    an embedding (e.g. when the provider has no embedding model yet).
    """
    try:
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO chunks(text, source, chunk_index, created_at) "
                "VALUES(?, ?, ?, ?)",
                (text, source, int(chunk_index), _now()),
            )
            chunk_id = cur.lastrowid
            if vector is not None:
                conn.execute(
                    "INSERT INTO embeddings(chunk_id, vector) VALUES(?, ?)",
                    (chunk_id, pack_vector(vector)),
                )
            conn.commit()
            return int(chunk_id)
        finally:
            conn.close()
    except Exception:
        return -1


def add_embedding(db_path: str, chunk_id: int, vector) -> bool:
    """Attach or replace the embedding vector for an existing chunk."""
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO embeddings(chunk_id, vector) VALUES(?, ?) "
                "ON CONFLICT(chunk_id) DO UPDATE SET vector = excluded.vector",
                (int(chunk_id), pack_vector(vector)),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def count_chunks(db_path: str) -> int:
    """Return the number of chunks in the index (0 when absent/unreadable)."""
    if not os.path.exists(db_path):
        return 0
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
            return int(row["n"])
        finally:
            conn.close()
    except Exception:
        return 0


def get_chunk(db_path: str, chunk_id: int) -> dict:
    """Return one chunk as a dict, or {} when missing/unreadable.

    The returned dict: ``{"chunk_id", "text", "source", "chunk_index",
    "created_at", "has_embedding"}``.
    """
    if not os.path.exists(db_path):
        return {}
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                """SELECT c.id, c.text, c.source, c.chunk_index, c.created_at,
                          (e.chunk_id IS NOT NULL) AS has_embedding
                   FROM chunks c LEFT JOIN embeddings e ON e.chunk_id = c.id
                   WHERE c.id = ?""",
                (int(chunk_id),),
            ).fetchone()
            if row is None:
                return {}
            return {
                "chunk_id": int(row["id"]),
                "text": row["text"],
                "source": row["source"] or "",
                "chunk_index": int(row["chunk_index"] or 0),
                "created_at": row["created_at"] or "",
                "has_embedding": bool(row["has_embedding"]),
            }
        finally:
            conn.close()
    except Exception:
        return {}


def list_chunks(db_path: str, limit: int = 20, offset: int = 0) -> dict:
    """Return a page of chunks (without vectors).

    Returns ``{"total", "chunks"}`` where chunks is a list of dicts sorted by
    id ascending: ``{"chunk_id", "text", "source", "chunk_index",
    "created_at", "has_embedding"}``. ``total`` is the full chunk count so
    the UI can render pagination.
    """
    if not os.path.exists(db_path):
        return {"total": 0, "chunks": []}
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
            total = int(row["n"])
            rows = conn.execute(
                """SELECT c.id, c.text, c.source, c.chunk_index, c.created_at,
                          (e.chunk_id IS NOT NULL) AS has_embedding
                   FROM chunks c LEFT JOIN embeddings e ON e.chunk_id = c.id
                   ORDER BY c.id
                   LIMIT ? OFFSET ?""",
                (int(limit), int(offset)),
            ).fetchall()
            chunks = [
                {
                    "chunk_id": int(r["id"]),
                    "text": r["text"],
                    "source": r["source"] or "",
                    "chunk_index": int(r["chunk_index"] or 0),
                    "created_at": r["created_at"] or "",
                    "has_embedding": bool(r["has_embedding"]),
                }
                for r in rows
            ]
            return {"total": total, "chunks": chunks}
        finally:
            conn.close()
    except Exception:
        return {"total": 0, "chunks": []}


def search_chunks_text(db_path: str, query: str, limit: int = 20,
                       offset: int = 0) -> dict:
    """Return chunks whose text contains *query* (case-insensitive substring).

    Uses SQLite's LIKE with escaped wildcards so user input is matched
    literally. Returns ``{"total", "chunks"}`` with the same chunk dict shape
    as :func:`list_chunks`.
    """
    if not os.path.exists(db_path):
        return {"total": 0, "chunks": []}
    q = str(query or "").strip()
    if not q:
        return list_chunks(db_path, limit=limit, offset=offset)
    escaped = (
        q.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE text LIKE ? ESCAPE '\\'",
                (pattern,),
            ).fetchone()
            total = int(row["n"])
            rows = conn.execute(
                """SELECT c.id, c.text, c.source, c.chunk_index, c.created_at,
                          (e.chunk_id IS NOT NULL) AS has_embedding
                   FROM chunks c LEFT JOIN embeddings e ON e.chunk_id = c.id
                   WHERE c.text LIKE ? ESCAPE '\\'
                   ORDER BY c.id
                   LIMIT ? OFFSET ?""",
                (pattern, int(limit), int(offset)),
            ).fetchall()
            chunks = [
                {
                    "chunk_id": int(r["id"]),
                    "text": r["text"],
                    "source": r["source"] or "",
                    "chunk_index": int(r["chunk_index"] or 0),
                    "created_at": r["created_at"] or "",
                    "has_embedding": bool(r["has_embedding"]),
                }
                for r in rows
            ]
            return {"total": total, "chunks": chunks}
        finally:
            conn.close()
    except Exception:
        return {"total": 0, "chunks": []}


def update_chunk_text(db_path: str, chunk_id: int, text: str) -> bool:
    """Update the text of one chunk and invalidate its embedding.

    The old embedding is removed because it no longer matches the new text;
    re-embedding is performed by the caller. Returns True on success.
    """
    new_text = str(text or "")
    try:
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                "UPDATE chunks SET text = ? WHERE id = ?",
                (new_text, int(chunk_id)),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "DELETE FROM embeddings WHERE chunk_id = ?",
                (int(chunk_id),),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def delete_chunk(db_path: str, chunk_id: int) -> bool:
    """Delete a chunk and its embedding. Returns True on success."""
    try:
        conn = _connect(db_path)
        try:
            cur = conn.execute("DELETE FROM chunks WHERE id = ?", (int(chunk_id),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def delete_embedding(db_path: str, chunk_id: int) -> bool:
    """Delete only the embedding vector of a chunk."""
    try:
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                "DELETE FROM embeddings WHERE chunk_id = ?", (int(chunk_id),)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def _cosine(a: tuple, b: tuple) -> float:
    """Cosine similarity between two equal-length numeric tuples."""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def search_similar(db_path: str, query_vector, top_k: int = 5) -> list:
    """Find the *top_k* chunks most similar to *query_vector*.

    Returns a list of dicts sorted by descending score:
    ``{"chunk_id", "text", "source", "chunk_index", "score"}``.
    Returns [] when the index is absent or contains no embeddings.
    """
    if not os.path.exists(db_path):
        return []
    results = []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                """SELECT c.id AS chunk_id, c.text, c.source, c.chunk_index,
                          e.vector
                   FROM chunks c JOIN embeddings e ON e.chunk_id = c.id"""
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    q = tuple(float(v) for v in query_vector)
    for row in rows:
        try:
            vec = unpack_vector(bytes(row["vector"]))
        except Exception:
            continue
        if len(vec) != len(q):
            continue
        score = _cosine(q, vec)
        results.append(
            {
                "chunk_id": int(row["chunk_id"]),
                "text": row["text"],
                "source": row["source"] or "",
                "chunk_index": int(row["chunk_index"] or 0),
                "score": score,
            }
        )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[: int(top_k)]


def index_stats(db_path: str) -> dict:
    """Return diagnostic stats for an index database."""
    stats = {
        "chunks": 0,
        "embeddings": 0,
        "dimension": None,
        "provider": "",
        "embedding_model": "",
    }
    try:
        meta = read_meta(db_path)
        stats["provider"] = meta.get("provider", "")
        stats["embedding_model"] = meta.get("embedding_model", "")
        dim = meta.get("dimension")
        stats["dimension"] = int(dim) if dim else None
    except Exception:
        pass
    stats["chunks"] = count_chunks(db_path)
    if os.path.exists(db_path) and stats["chunks"] > 0:
        try:
            conn = _connect(db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM embeddings"
                ).fetchone()
                stats["embeddings"] = int(row["n"])
            finally:
                conn.close()
        except Exception:
            pass
    return stats


def dump_chunks(db_path: str) -> list:
    """Return all chunks (without vectors) as a list of dicts.

    Useful for debugging and for tests.
    """
    if not os.path.exists(db_path):
        return []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id, text, source, chunk_index, created_at FROM chunks "
                "ORDER BY id"
            ).fetchall()
            return [
                {
                    "chunk_id": int(r["id"]),
                    "text": r["text"],
                    "source": r["source"] or "",
                    "chunk_index": int(r["chunk_index"] or 0),
                    "created_at": r["created_at"] or "",
                }
                for r in rows
            ]
        finally:
            conn.close()
    except Exception:
        return []
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
