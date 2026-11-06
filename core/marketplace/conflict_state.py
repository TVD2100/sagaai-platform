"""
core.marketplace.conflict_state - what an installation looks like right now.

Every conflict decision starts from the same question: which catalog id is
installed, where its folder lives, and did the user edit a file the package
declares. This module answers it and owns the conflict error type shared by
the resolver and the fork paths.

Two ideas keep the rules honest:

  * identity is the catalog ``id`` in ``marketplace_installed``, never the
    folder name and never the display name;
  * "modified" means "a file the package declares has different content" -
    a note the user drops next to ``SKILL.md`` is theirs and is invisible to
    the detector, because the hashing area is the installed file manifest
    (D-04).
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from core.marketplace import registry, store


class ConflictError(store.MarketplaceError):
    """Raised when a conflict cannot be resolved as requested."""


def entity_folder(type_: str, runtime_slug: str) -> Optional[str]:
    """Return the folder an entity occupies or would occupy, existing or not."""
    if not runtime_slug:
        return None
    if type_ == "skill":
        return os.path.join(registry.skills_root(), runtime_slug)
    if type_ == "rag":
        return os.path.join(registry.rag_root(), runtime_slug)
    if type_ == "agent":
        # An agent lives either in its orchestrator folder or in its helper
        # folder under ``assistants/``; the registry knows which one and the
        # defaults importer writes to the same place - one location, one
        # source of truth.
        return registry.agent_folder(runtime_slug)
    return None


def _slug_owner(type_: str, runtime_slug: str) -> Optional[dict]:
    """Return the registry row that owns *runtime_slug*, or None.

    This is the lookup that separates "the same entity, registry lost"
    (adopt) from "another entity already sits there" (suffix): matching by
    folder name alone cannot tell them apart.
    """
    if not runtime_slug:
        return None
    for row in registry.list_installed(type_):
        if str(row.get("runtime_slug") or "") == runtime_slug:
            return row
    return None


def detect_local_modifications(type_: str, runtime_slug: str) -> dict:
    """Compare the files on disk with the manifest recorded at install time.

    Only files the package declares take part: ``SKILL.md`` and friends for
    a skill. Files outside the manifest are user data - adding one is not a
    modification, and a file listed in the manifest but gone from disk is
    reported as changed with an empty hash so the caller can tell.

    Returns ``{"modified": bool, "files": [rel_path, ...], "missing": [...]}``.
    """
    row = registry.get_installed(type_, registry.runtime_slug_id(type_, runtime_slug))
    if row is None or not row.get("files_json"):
        return {"modified": False, "files": [], "missing": []}
    recorded: Dict[str, str] = dict(row.get("files_json") or {})
    actual = registry.entity_file_manifest(type_, runtime_slug, recorded)
    changed: List[str] = []
    missing: List[str] = []
    for rel_path, digest in recorded.items():
        current = actual.get(rel_path)
        if current == digest:
            continue
        changed.append(rel_path)
        if not current:
            missing.append(rel_path)
    user_marked = list(row.get("user_modified_files_json") or [])
    for rel_path in user_marked:
        if rel_path not in changed:
            changed.append(rel_path)
    return {
        "modified": bool(changed),
        "files": sorted(changed),
        "missing": sorted(missing),
    }


def local_state(entity: dict, *, type_: str = "skill") -> dict:
    """Return the local state of a catalog entity.

    The result carries exactly what the matrix needs: whether the catalog id
    is installed and at which version, whether its folder exists without a
    registry row (a legacy orphan worth adopting), whether the folder is
    taken by another entity and how badly the files were edited.
    """
    entity_id = str(entity.get("id") or "")
    catalog_version = str(entity.get("version") or "0.0.0")
    row = registry.get_installed(type_, entity_id) if entity_id else None
    runtime_slug = str((row or {}).get("runtime_slug") or entity.get("runtime_slug") or entity_id)
    folder = entity_folder(type_, runtime_slug)
    folder_exists = bool(folder and os.path.isdir(folder))
    owner = _slug_owner(type_, runtime_slug)

    changes = {"modified": False, "files": [], "missing": []}
    if row is not None:
        changes = detect_local_modifications(type_, runtime_slug)

    return {
        "id": entity_id,
        "catalog_version": catalog_version,
        "installed": row is not None,
        "row": row,
        "installed_version": str((row or {}).get("version") or "") or None,
        "runtime_slug": runtime_slug,
        "folder_exists": folder_exists,
        "slug_owner": owner,
        "slug_taken_by_other": bool(owner is not None and str(owner.get("id")) != entity_id),
        "user_modified": bool((row or {}).get("user_modified")) or changes["modified"],
        "modified_files": changes["files"],
        "missing_files": changes["missing"],
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
