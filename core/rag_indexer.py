"""
core.rag_indexer - index source files of a RAG base into a local vector index.

Pipeline (blocking, called from the UI action layer; run there is sequential):

  1. read every source file from ``<base>/files/`` as UTF-8 text;
  2. split text into chunks (core.rag_chunker);
  3. write chunks into the base index (core.rag_index);
  4. for ``vector``/``hybrid`` modes request embeddings per chunk
     (core.rag_embeddings; remote BYOK API, stored only locally) and
     attach the vectors;
  5. build the lexical BM25 index (core.rag_bm25) - every base is
     lexically searchable regardless of the mode, no API keys needed.

Status flow on the base manifest: ``indexing`` → ``ready`` (or ``error``).
The index is recreated on every run, so repeated calls are safe. Progress is
reported through an optional callback.

No streamlit imports.
"""
import os

from core import rag
from core.rag_chunker import chunk_text
from core.rag_embeddings import get_yandex_embedding_credentials
from core.rag_index import (
    add_chunk, create_index_db, read_meta, reset_index,
)


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_DIMENSION = 256
DEFAULT_EMBEDDING_MODEL = "text-search-doc"


class IndexingError(Exception):
    """Raised when indexing cannot start (e.g. credentials missing)."""


def extract_text(path: str) -> str:
    """Read a source file as UTF-8 text (binary-safe fallback)."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _manifest_int(base: dict, key: str, default: int) -> int:
    try:
        return int(base.get(key) or default)
    except (TypeError, ValueError):
        return default


def index_base(
    slug: str,
    *,
    progress_callback=None,
) -> dict:
    """Index all files of base *slug* into its local SQLite index.

    Args:
        slug: base slug.
        progress_callback: optional ``callable(stage, done, total)`` where
            stage is ``"embedding"`` or ``"write"``. Used by the UI to show
            progress messages.

    Returns:
        The base manifest after indexing (status ``ready`` or ``error``).

    Raises:
        IndexingError when the base does not exist, has no files, or when the
        embedding provider credentials are unavailable.
    """
    base = rag.get_base(slug, with_stats=False)
    if not base:
        raise IndexingError("RAG base does not exist")

    files = rag.list_files(slug)
    if not files:
        raise IndexingError("RAG base has no files to index")

    rag.set_status(slug, "indexing")

    # BM25 mode needs no remote credentials at all; vector/hybrid do.
    mode = str(base.get("search_mode") or "bm25").strip().lower()
    want_vectors = mode in ("vector", "hybrid")
    if want_vectors:
        try:
            get_yandex_embedding_credentials()
        except Exception:
            rag.set_status(slug, "error")
            raise

    chunk_size = _manifest_int(base, "chunk_size", DEFAULT_CHUNK_SIZE)
    chunk_overlap = _manifest_int(base, "chunk_overlap", DEFAULT_CHUNK_OVERLAP)
    embedding_model = base.get("embedding_model") or DEFAULT_EMBEDDING_MODEL
    provider = base.get("provider") or "yandex"
    dimension = DEFAULT_DIMENSION

    db_path = rag.index_db_path(slug)
    reset_index(db_path)
    create_index_db(
        db_path,
        dimension=dimension,
        provider=provider,
        embedding_model=embedding_model,
    )

    # 1. Build chunk list from all files.
    chunks: list = []
    for file_info in files:
        text = extract_text(os.path.join(rag.files_dir(slug), file_info["path"]))
        if not text.strip():
            continue
        file_chunks = chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        for idx, chunk in enumerate(file_chunks):
            chunks.append(
                {
                    "text": chunk,
                    "source": file_info["path"],
                    "chunk_index": idx,
                }
            )

    if not chunks:
        rag.set_status(slug, "error")
        raise IndexingError("No indexable text found in the base files")

    total = len(chunks)

    if want_vectors:
        # 2a. Embed chunks (sequential; provider quota allows local rate).
        from core.rag_embeddings import embed_text

        api_key, folder_id = get_yandex_embedding_credentials()
        for i, chunk in enumerate(chunks):
            try:
                vector = embed_text(
                    chunk["text"],
                    model=embedding_model,
                    dimension=dimension,
                    api_key=api_key,
                    folder_id=folder_id,
                )
            except Exception:
                rag.set_status(slug, "error")
                raise
            if not vector:
                continue
            add_chunk(
                db_path,
                chunk["text"],
                source=chunk["source"],
                chunk_index=chunk["chunk_index"],
                vector=vector,
            )
            if progress_callback is not None:
                try:
                    progress_callback("embedding", i + 1, total)
                except Exception:
                    pass
    else:
        # 2b. BM25 mode: store chunks without vectors (no provider calls).
        for i, chunk in enumerate(chunks):
            add_chunk(
                db_path,
                chunk["text"],
                source=chunk["source"],
                chunk_index=chunk["chunk_index"],
            )
            if progress_callback is not None:
                try:
                    progress_callback("write", i + 1, total)
                except Exception:
                    pass

    # 3. Build the lexical BM25 index over the stored chunks (all modes).
    try:
        from core.rag_bm25 import rebuild_index
        rebuild_index(db_path)
    except Exception:
        pass

    rag.set_status(slug, "ready")
    return rag.get_base(slug)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
