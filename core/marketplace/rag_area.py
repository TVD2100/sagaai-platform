"""
core.marketplace.rag_area - files and index of a ``rag`` base folder.

The live folder of a base holds three parts: ``manifest.json``,
``files/`` with the source documents and ``index.db`` with the SQLite
index the search reads. This module owns the low-level work on that
folder: walking it, writing text into it, and checking or building the
index itself.

Nothing here decides what an install should do - the caller in
:mod:`core.marketplace.rag_installer` plans the placement and this module
performs the steps it is given.
"""
from __future__ import annotations

import os
from typing import Set

from core.marketplace.installer import InstallError


def _files_under(root: str) -> Set[str]:
    """Return ``{relative path}`` of every file under *root*."""
    result: Set[str] = set()
    if not os.path.isdir(root):
        return result
    for current, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in names:
            full = os.path.join(current, name)
            result.add(os.path.relpath(full, root).replace(os.sep, "/"))
    return result


def _write_text(path: str, text: str) -> None:
    """Write *text* as UTF-8, creating the parent folder."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _check_index(path: str, dist: dict, entity_id: str) -> dict:
    """Validate a downloaded index and return its meta rows.

    A file that is not a SQLite index of the expected shape would install
    cleanly and then answer every search with nothing, so the check happens
    while the bytes are still in staging.
    """
    from core import rag_index

    if not os.path.isfile(path):
        raise InstallError("invalid_index", "The downloaded index is empty", {"id": entity_id})
    meta = rag_index.read_meta(path)
    if not meta:
        raise InstallError(
            "invalid_index",
            "The downloaded file is not a readable index database",
            {"id": entity_id},
        )
    declared_chunks = int(dist.get("chunks") or 0)
    actual_chunks = rag_index.count_chunks(path)
    if declared_chunks and actual_chunks != declared_chunks:
        raise InstallError(
            "index_chunk_mismatch",
            f"The index holds {actual_chunks} chunks, the manifest declares {declared_chunks}",
            {"id": entity_id, "actual": actual_chunks, "declared": declared_chunks},
        )
    if not actual_chunks:
        raise InstallError(
            "empty_index",
            "The index holds no chunks",
            {"id": entity_id},
        )
    return meta


def _build_index(slug: str, entity_id: str) -> None:
    """Build the index of a freshly placed base from its source files."""
    from core import rag_indexer

    try:
        rag_indexer.index_base(slug)
    except Exception as exc:
        raise InstallError(
            "index_build_failed",
            f"The index could not be built from the sources: {exc}",
            {"id": entity_id, "slug": slug},
        ) from exc


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
