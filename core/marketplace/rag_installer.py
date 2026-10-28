"""
core.marketplace.rag_installer - installation of a ``rag`` entity.

A RAG base is a folder with three parts: ``manifest.json`` (metadata and
search settings), ``files/`` (the source documents) and ``index.db`` (the
SQLite index the search reads). The catalog can ship the index, the sources
or both, so a distribution declares one of three modes:

  * ``prebuilt_index``   - only ``index.db`` is downloaded; the base is
    searchable at once, which is the only mode possible for a base whose
    sources were never published;
  * ``sources_only``     - only ``files/`` is downloaded and the index is
    built locally by :func:`core.rag_indexer.index_base`;
  * ``sources_plus_index`` - both, with the index optional: a BM25 base can
    rebuild it locally without any provider key.

The install is a staged replace. The new folder is assembled in staging, the
index is verified against its declared hash, size, schema and chunk count,
and only then does anything reach the live folder. Files of an existing base
that the new version does not ship are backed up and removed, files that both
versions carry are backed up and replaced, so a rollback restores the folder
byte-for-byte through the same mechanism every other entity type uses.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from typing import Dict, List, Optional, Set

from core import rag
from core.marketplace import config as marketplace_config
from core.marketplace import registry, rollback, source as source_mod, store, verify
from core.marketplace.installer import (
    InstallError,
    _entity_files,
    _licence_state,
    _platform_compatible,
)

#: Distribution modes a ``rag`` entity may declare.
DISTRIBUTION_MODES = ("prebuilt_index", "sources_only", "sources_plus_index")

#: Manifest file name inside a base folder.
MANIFEST_NAME = "manifest.json"

#: Index file name inside a base folder.
INDEX_NAME = "index.db"


# ─── Manifest and distribution ────────────────────────────────────────────

def _localized(value, lang: str = "ru") -> str:
    """Return a manifest text field for *lang* (catalog fields may be maps)."""
    if isinstance(value, dict):
        for key in (lang, "en", "ru"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
        for text in value.values():
            if isinstance(text, str) and text.strip():
                return text.strip()
        return ""
    return str(value or "").strip()


def distribution_of(entity: dict) -> dict:
    """Return the ``distribution`` block of an entity, or an empty dict."""
    block = entity.get("distribution")
    return dict(block) if isinstance(block, dict) else {}


def check_distribution(entity: dict) -> dict:
    """Validate the distribution block and return it normalised.

    Raises :class:`InstallError` with a machine-readable code rather than
    failing later on a missing key: a card that cannot be installed has to
    say why before the download starts.

    The index schema and the embedding dimension describe the local index
    format, so a base built for another format is refused instead of being
    installed and then degrading silently (a vector search skips vectors of
    a foreign dimension without a word).
    """
    from core import rag_bm25, rag_indexer

    entity_id = str(entity.get("id") or "")
    block = distribution_of(entity)
    mode = str(block.get("mode") or "").strip().lower()
    if mode not in DISTRIBUTION_MODES:
        raise InstallError(
            "invalid_distribution",
            f"Unknown distribution mode '{mode or '-'}'",
            {"id": entity_id, "mode": mode, "supported": list(DISTRIBUTION_MODES)},
        )

    files = _entity_files(entity)
    index_url = str(block.get("index_url") or entity.get("index_url") or "").strip()
    sources_url = str(block.get("sources_url") or entity.get("sources_url") or "").strip()

    if mode == "prebuilt_index" and not index_url:
        raise InstallError(
            "invalid_distribution",
            "A prebuilt_index distribution declares no index_url",
            {"id": entity_id},
        )
    if mode == "sources_only" and not files:
        raise InstallError(
            "invalid_distribution",
            "A sources_only distribution declares no files",
            {"id": entity_id},
        )
    if mode == "sources_plus_index" and not files and not index_url:
        raise InstallError(
            "invalid_distribution",
            "A sources_plus_index distribution ships neither files nor an index",
            {"id": entity_id},
        )

    if index_url:
        index_sha = str(block.get("index_sha256") or "").strip().lower()
        if not index_sha:
            raise InstallError(
                "missing_index_hash",
                "The index is declared without an index_sha256",
                {"id": entity_id, "index_url": index_url},
            )
        schema = block.get("index_schema")
        if schema is None:
            raise InstallError(
                "missing_index_schema",
                "The index is declared without an index_schema",
                {"id": entity_id},
            )
        if int(schema) != int(rag_bm25.SCHEMA_VERSION):
            raise InstallError(
                "index_schema_mismatch",
                f"Index schema {schema} is not supported by this build ({rag_bm25.SCHEMA_VERSION})",
                {"id": entity_id, "declared": int(schema), "local": int(rag_bm25.SCHEMA_VERSION)},
            )
        dimensions = block.get("dimensions")
        if dimensions is not None and int(dimensions) != int(rag_indexer.DEFAULT_DIMENSION):
            raise InstallError(
                "index_dimension_mismatch",
                f"Index dimension {dimensions} does not match the local {rag_indexer.DEFAULT_DIMENSION}",
                {
                    "id": entity_id,
                    "declared": int(dimensions),
                    "local": int(rag_indexer.DEFAULT_DIMENSION),
                },
            )

    search_mode = str((entity.get("manifest") or {}).get("search_mode") or "bm25").strip().lower()
    if search_mode in ("vector", "hybrid"):
        model = str(block.get("embedding_model") or "").strip()
        if not model:
            raise InstallError(
                "missing_embedding_model",
                "A vector base must declare its embedding_model",
                {"id": entity_id},
            )
        if model != str(rag_indexer.DEFAULT_EMBEDDING_MODEL):
            raise InstallError(
                "embedding_model_mismatch",
                f"Embedding model '{model}' is not the local {rag_indexer.DEFAULT_EMBEDDING_MODEL}",
                {
                    "id": entity_id,
                    "declared": model,
                    "local": str(rag_indexer.DEFAULT_EMBEDDING_MODEL),
                },
            )

    normalized = dict(block)
    normalized.update({
        "mode": mode,
        "index_url": index_url,
        "sources_url": sources_url,
        "index_sha256": str(block.get("index_sha256") or "").strip().lower(),
        "index_bytes": int(block.get("index_bytes") or 0),
        "chunks": int(block.get("chunks") or 0),
    })
    return normalized


def base_manifest(entity: dict, slug: str, previous: Optional[dict] = None) -> dict:
    """Build the ``manifest.json`` of a base from a catalog entity.

    The identity and the creation timestamp of an existing base are kept:
    they are what the rest of the platform (assistant slots, thread links)
    already points at, and an update must not look like a new base.
    """
    manifest_in = entity.get("manifest") if isinstance(entity.get("manifest"), dict) else {}
    dist = distribution_of(entity)
    previous = previous or {}
    now = store.isoformat_utc()
    mode = str(manifest_in.get("search_mode") or "bm25").strip().lower()
    if mode not in rag.SEARCH_MODES:
        raise InstallError(
            "invalid_search_mode",
            f"Unknown search mode '{mode}'",
            {"id": str(entity.get("id") or ""), "mode": mode},
        )
    chunks_declared = dist.get("chunks")
    return {
        "id": str(previous.get("id") or uuid.uuid4()),
        "slug": slug,
        # The type-specific block describes the base itself (chunking, search
        # mode, slot list); the entity-level fields are the catalog card and
        # only fill what the block leaves out.
        "name": _localized(manifest_in.get("name")) or _localized(entity.get("name")) or slug,
        "description": _localized(manifest_in.get("description"))
        or _localized(entity.get("description")),
        "type": "rag",
        "provider": str(manifest_in.get("provider") or dist.get("provider") or "yandex"),
        "embedding_model": str(
            manifest_in.get("embedding_model") or dist.get("embedding_model") or "text-search-doc"
        ),
        "chunk_size": int(manifest_in.get("chunk_size") or 1500),
        "chunk_overlap": int(manifest_in.get("chunk_overlap") or 0),
        "status": "ready",
        "search_mode": mode,
        "created_at": str(previous.get("created_at") or now),
        "updated_at": now,
        "rag_slots": list(manifest_in.get("rag_slots") or []),
        "marketplace": {
            "id": str(entity.get("id") or ""),
            "version": str(entity.get("version") or "0.0.0"),
            "chunks": int(chunks_declared or 0),
        },
    }


# ─── Slug selection ───────────────────────────────────────────────────────

def _safe_slug(value: str) -> str:
    """Normalise a folder name to the character set a base slug allows."""
    raw = str(value or "").strip().lower()
    cleaned = "".join(
        ch if ch in "abcdefghijklmnopqrstuvwxyz0123456789_-." else "_" for ch in raw
    )
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "base"


def _slug_taken(slug: str) -> bool:
    """True when a base folder or a registry row already uses *slug*."""
    if os.path.isdir(rag.base_dir(slug)):
        return True
    try:
        return any(
            str(row.get("runtime_slug") or "") == slug
            for row in registry.list_installed("rag")
        )
    except Exception:
        return False


def _pick_runtime_slug(entity: dict, requested: Optional[str] = None) -> str:
    """Return a free base slug, appending a suffix on collision (D-06)."""
    base = _safe_slug(requested or entity.get("slug") or entity.get("id") or "base")
    candidate = base
    counter = 2
    while _slug_taken(candidate):
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _resolve_slug(
    entity: dict,
    existing: Optional[dict],
    requested: Optional[str] = None,
) -> str:
    """Return the folder name this install must use.

    An update keeps the folder the entity already occupies: the registry row
    names it, and choosing a fresh free name instead would put a second copy
    of the same base next to the first one. A new entity gets a free name,
    with a suffix when the folder belongs to something else (D-06).
    """
    if requested:
        return _safe_slug(requested)
    installed_slug = str((existing or {}).get("runtime_slug") or "")
    if installed_slug:
        return installed_slug
    return _pick_runtime_slug(entity)


# ─── Placement helpers ────────────────────────────────────────────────────

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


# ─── Install ──────────────────────────────────────────────────────────────

def install_rag(
    entity: dict,
    *,
    channel: str,
    allow_code: Optional[bool] = None,
    source: Optional[source_mod.MarketplaceSource] = None,
    accept_license: bool = False,
    runtime_slug: Optional[str] = None,
) -> dict:
    """Install one RAG base from the catalog and return a report.

    Parameters:
        entity: parsed ``entity.json`` of the base.
        channel: release channel recorded on the registry row.
        allow_code: unused for a base (a base ships no executable files);
            accepted so every installer shares one call signature.
        source: catalog transport; ``None`` uses the configured catalog.
        accept_license: explicit licence acceptance when required.
        runtime_slug: folder name to use; ``None`` derives it from the
            manifest and appends a suffix when it is taken.

    Re-installing the same version is a no-op. Any failure after the first
    change rolls the run back: a created folder disappears, a replaced one
    returns to its previous bytes and the registry row never appears.
    """
    if not isinstance(entity, dict) or not entity.get("id"):
        raise InstallError("invalid_entity", "Entity manifest has no id")

    settings = marketplace_config.marketplace_settings()
    if not settings.get("enabled"):
        raise InstallError(
            "marketplace_disabled",
            "The marketplace is disabled in the settings",
        )

    entity_id = str(entity["id"])
    version = str(entity.get("version") or "0.0.0")
    dist = check_distribution(entity)
    declared = _entity_files(entity)

    existing = registry.get_installed("rag", entity_id)
    if existing is not None and str(existing.get("version")) == version:
        return {
            "ok": True,
            "noop": True,
            "id": entity_id,
            "type": "rag",
            "version": version,
            "runtime_slug": existing.get("runtime_slug"),
            "install_id": existing.get("install_id"),
        }

    compatible, reason = _platform_compatible(entity)
    if not compatible:
        raise InstallError(
            "platform_incompatible",
            reason or "Entity is not compatible with this platform build",
            {"id": entity_id, "version": version},
        )

    license_at = _licence_state(entity, accept_license)
    catalog = source or source_mod.default_source()

    with store.install_lock():
        install_id = store.new_install_id()
        staging, _backup = store.prepare_run_dirs(install_id)
        slug = ""
        store.append_run(
            {
                "install_id": install_id,
                "type": "rag",
                "id": entity_id,
                "version": version,
                "action": "install",
                "status": "applying",
            }
        )
        try:
            content = os.path.join(staging, "content")
            os.makedirs(content, exist_ok=True)
            package_sha = ""

            if declared:
                package = catalog.fetch_package(entity)
                package_sha = verify.verify_package(
                    package, str((entity.get("package") or {}).get("sha256") or "")
                )
                package_path = os.path.join(staging, "package.tar.gz")
                with open(package_path, "wb") as handle:
                    handle.write(package)
                try:
                    store.cache_package(package_sha, package)
                except OSError:
                    pass
                members = verify.validate_archive(package_path)
                verify.extract_archive(package_path, content, members)
                mismatched = verify.verify_files(content, declared)
                if mismatched:
                    raise InstallError(
                        "file_hash_mismatch",
                        "Unpacked files do not match the manifest",
                        {"files": mismatched[:10], "count": len(mismatched)},
                    )

            index_sha = ""
            if dist["index_url"]:
                index_bytes = catalog.fetch_bytes(dist["index_url"])
                index_sha = verify.verify_package(index_bytes, dist["index_sha256"])
                if dist["index_bytes"] and len(index_bytes) != dist["index_bytes"]:
                    raise InstallError(
                        "index_size_mismatch",
                        f"The index is {len(index_bytes)} bytes, the manifest declares {dist['index_bytes']}",
                        {"id": entity_id, "actual": len(index_bytes), "declared": dist["index_bytes"]},
                    )
                staged_index = os.path.join(content, INDEX_NAME)
                with open(staged_index, "wb") as handle:
                    handle.write(index_bytes)
                _check_index(staged_index, dist, entity_id)

            slug = _resolve_slug(entity, existing, runtime_slug)
            target = rag.base_dir(slug)
            previous_manifest = {}
            if os.path.isdir(target):
                try:
                    previous_manifest = rag.json_load(os.path.join(target, MANIFEST_NAME))
                except Exception:
                    previous_manifest = {}

            manifest = base_manifest(entity, slug, previous_manifest)
            _write_text(
                os.path.join(content, MANIFEST_NAME),
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )

            new_files = _files_under(content)
            old_files = _files_under(target) if os.path.isdir(target) else set()
            created_files = sorted(new_files - old_files)
            replaced_files = sorted(new_files & old_files)
            removed_files = sorted(old_files - new_files)

            # Metadata goes down BEFORE the first change: a rollback needs to
            # know which folder and which files this run touched.
            for rel_path in replaced_files + removed_files:
                store.copy_into_backup(os.path.join(target, rel_path), install_id, rel_path)
            store.write_run_meta(
                install_id,
                {
                    "install_id": install_id,
                    "type": "rag",
                    "id": entity_id,
                    "runtime_slug": slug,
                    "created_folder": not os.path.isdir(target),
                    "created_files": created_files,
                    "replaced_files": replaced_files,
                    "removed_files": removed_files,
                    "previous_row": existing,
                    "registry_path": "",
                    "registry_backup": "",
                    "registry_existed": False,
                    "created_db_row": False,
                },
            )

            for rel_path in sorted(new_files):
                source_path = os.path.join(content, rel_path)
                target_path = os.path.join(target, rel_path)
                os.makedirs(os.path.dirname(target_path) or target, exist_ok=True)
                # A search may be running against the live folder while an
                # update lands. Replacing each file atomically means such a
                # reader sees either the old index or the new one, never a
                # half-written database.
                store._atomic_replace(source_path, target_path)
            for rel_path in removed_files:
                try:
                    os.remove(os.path.join(target, rel_path))
                except OSError:
                    pass

            if not dist["index_url"]:
                _build_index(slug, entity_id)
                index_path = rag.index_db_path(slug)
                index_sha = registry._sha256_file(index_path) if os.path.isfile(index_path) else ""

            live_manifest = rag.get_base(slug, with_stats=False)
            if not live_manifest:
                raise InstallError(
                    "incomplete_install",
                    "The base folder carries no readable manifest after the install",
                    {"id": entity_id, "runtime_slug": slug},
                )
            files_now = registry.entity_file_manifest("rag", slug, declared or None)
            absent = sorted(path for path, digest in files_now.items() if not digest)
            if absent:
                raise InstallError(
                    "incomplete_install",
                    "The base folder is missing files the manifest declares",
                    {"id": entity_id, "files": absent[:10]},
                )
            tree = registry.manifest_sha256(files_now)

            now = store.isoformat_utc()
            registry.upsert_installed(
                {
                    "type": "rag",
                    "id": entity_id,
                    "runtime_slug": slug,
                    "runtime_db_id": None,
                    "runtime_skill_id": None,
                    "fork_of": entity.get("fork_of"),
                    "version": version,
                    "channel": channel,
                    "source_url": str(entity.get("source_url") or ""),
                    "package_sha256": package_sha,
                    "tree_sha256": tree,
                    "index_sha256": index_sha or None,
                    "files_json": files_now,
                    "skipped_code_files_json": [],
                    "installed_at": now,
                    "updated_at": now,
                    "installed_by": "marketplace",
                    "user_modified": False,
                    "user_modified_files_json": [],
                    "install_id": install_id,
                    "bundle_id": entity.get("bundle_id"),
                    "pinned": False,
                    "license_accepted_at": license_at,
                }
            )

            store.update_run(
                install_id,
                status="done",
                finished_at=store.isoformat_utc(),
                runtime_slug=slug,
            )
            store.cleanup_run_dirs(install_id, keep_backup=True)
            store.clear_state()
            try:
                registry.unmark_removed("rag", entity_id)
            except Exception:
                pass
            registry.export_registry_cache()

            return {
                "ok": True,
                "noop": False,
                "id": entity_id,
                "type": "rag",
                "version": version,
                "install_id": install_id,
                "runtime_slug": slug,
                "distribution": dist["mode"],
                "index_sha256": index_sha,
                "tree_sha256": tree,
                "chunks": int((live_manifest.get("index_stats") or {}).get("chunks") or 0),
            }
        except Exception as exc:
            _fail_run(install_id, slug, exc)
            if isinstance(exc, InstallError):
                raise
            if isinstance(exc, store.MarketplaceError):
                raise InstallError(exc.code, exc.message, exc.details) from exc
            raise InstallError(
                "install_failed",
                f"RAG base installation failed: {exc}",
                {"id": entity_id, "version": version},
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _fail_run(install_id: str, slug: str, exc: Exception) -> None:
    """Roll a failed base run back and record the failure.

    Rollback errors must not mask the original exception: they land in the
    run log, while the caller still sees the real cause. A run that never
    wrote its metadata has nothing to undo - the staging folder is removed
    by the caller's ``finally`` block.
    """
    rolled_back = False
    try:
        if store.read_run_meta(install_id) is not None:
            rollback.rollback_run(install_id, reason=f"rag install failed: {exc}")
            rolled_back = True
    except Exception:
        rolled_back = False
    if not rolled_back and slug:
        # No usable backup metadata: remove the folder directly so a failed
        # attempt does not leave a half-installed base behind.
        try:
            shutil.rmtree(rag.base_dir(slug), ignore_errors=True)
        except Exception:
            pass
    store.update_run(
        install_id,
        status="failed",
        failed_at=store.isoformat_utc(),
        failure_reason=str(exc),
        rolled_back=rolled_back,
    )
    store.clear_state()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
