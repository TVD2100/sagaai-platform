"""
core.marketplace.conflict_fork - keeping both copies of an edited entity.

When the user edited a skill and the catalog has a different version, one of
the three conflict paths is ``fork``: the edited folder stays exactly as it
is under a new id and folder name, while the original is brought to the
catalog version. Neither copy overwrites the other, which is the whole point
of the path.

The module also owns the reverse action - putting the user's files back
after a ``take_catalog`` run replaced them. That action reads the run backup
directly instead of undoing the whole run, because the user wants their
edits back, not the installation undone.
"""
from __future__ import annotations

import os
import shutil
from typing import List, Optional

from core.marketplace import installer, registry, store
from core.marketplace import source as source_mod
from core.marketplace.conflict_state import (
    ConflictError,
    entity_folder,
    local_state,
)


def next_fork_id(entity_id: str) -> str:
    """Return the first free ``<id>#forkN`` for an entity."""
    counter = 1
    while True:
        candidate = f"{entity_id}#fork{counter}"
        if registry.get_installed("skill", candidate) is None:
            return candidate
        counter += 1


def next_fork_slug(runtime_slug: str) -> str:
    """Return the first free ``<slug>_fork_NN`` folder name."""
    counter = 1
    while True:
        candidate = f"{runtime_slug}_fork_{counter:02d}"
        if not os.path.isdir(os.path.join(registry.skills_root(), candidate)):
            return candidate
        counter += 1


def _fork(
    entity: dict,
    *,
    channel: str,
    source: Optional[source_mod.MarketplaceSource],
    allow_code: Optional[bool],
    accept_license: bool,
) -> dict:
    """Copy the edited installation aside, then bring the original to the catalog.

    The fork keeps the user's files under a new id and folder; the original
    is resolved by ``take_catalog``, so afterwards both the catalog version
    and the edited copy exist and neither overwrote the other.
    """
    # Imported here: the conflict resolver owns the three paths and imports
    # this module for the fork one, so a module-level import would be a cycle.
    from core.marketplace import conflicts as conflicts_mod

    state = local_state(entity, type_="skill")
    if not state["installed"]:
        raise ConflictError(
            "not_installed",
            "Forking applies to an installed entity",
            {"id": state["id"]},
        )
    original_slug = state["runtime_slug"]
    original_folder = entity_folder("skill", original_slug)
    if not original_folder or not os.path.isdir(original_folder):
        raise ConflictError("not_installed", "The installation folder is missing")

    fork_id = next_fork_id(state["id"])
    fork_slug = next_fork_slug(original_slug)
    fork_folder = entity_folder("skill", fork_slug)
    shutil.copytree(original_folder, fork_folder)

    files = registry.entity_file_manifest("skill", fork_slug, installer._entity_files(entity))
    tree = registry.tree_sha256("skill", fork_slug, files)
    now = store.isoformat_utc()
    fork_skill_id = _clone_skill_record(original_slug, fork_slug, fork_id)
    fork_row = dict(state["row"] or {})
    fork_row.update({
        "id": fork_id,
        "runtime_slug": fork_slug,
        "runtime_skill_id": fork_skill_id,
        "fork_of": state["id"],
        "files_json": files,
        "tree_sha256": tree,
        "installed_at": now,
        "updated_at": now,
        "user_modified": True,
        "user_modified_files_json": list(state["modified_files"]),
        "pinned": True,
        "install_id": None,
    })
    registry.upsert_installed(fork_row)

    try:
        applied = conflicts_mod._apply_choice(
            "take_catalog",
            entity,
            channel=channel,
            source=source,
            allow_code=allow_code,
            accept_license=accept_license,
        )
    except Exception:
        # The fork is already a separate installation; leaving it in place is
        # better than removing the user's only copy of their edits.
        registry.export_registry_cache()
        raise
    return {
        "ok": True,
        "choice": "fork",
        "id": state["id"],
        "fork_id": fork_id,
        "fork_slug": fork_slug,
        "version": state["catalog_version"],
        "install_id": applied.get("install_id"),
        "fork_install_id": None,
    }


def _clone_skill_record(original_slug: str, fork_slug: str, fork_id: str) -> Optional[str]:
    """Register a second ``skills.json`` record for the fork folder.

    The skills library keys records by an 8-hex id and ``register_skill``
    takes the display fields plus the folder, so the fork gets its own id
    while keeping the original name (marked as a fork) and description.
    """
    from core.skills_library import _load_registry, register_skill

    data = _load_registry()
    for skill_id, record in data.items():
        if str(record.get("folder") or "") != original_slug:
            continue
        name = str(record.get("name") or fork_slug)
        try:
            created = register_skill(f"{name} (fork)", str(record.get("description") or ""), fork_slug)
        except Exception:
            return skill_id
        return str(created or skill_id)
    return None


def restore_user_version(install_id: str) -> dict:
    """Put the files a ``take_catalog`` run backed up back in place.

    This is the "bring my version back" action of the UI. It reads the run
    backup directly instead of going through ``rollback_run``, because the
    user wants their edited files back - not the whole run undone.
    """
    meta = store.read_run_meta(install_id)
    if meta is None:
        raise ConflictError(
            "missing_run_meta",
            f"No backup metadata found for run {install_id}",
            {"install_id": install_id},
        )
    runtime_slug = str(meta.get("runtime_slug") or "")
    folder = entity_folder(str(meta.get("type") or "skill"), runtime_slug)
    if not folder:
        raise ConflictError("not_installed", "The entity folder is unknown")
    restored: List[str] = []
    missing: List[str] = []
    for rel_path in meta.get("user_backup_files") or []:
        source_file = os.path.join(store.backup_dir(install_id), "files", rel_path)
        if not os.path.isfile(source_file):
            missing.append(rel_path)
            continue
        target = os.path.join(folder, rel_path)
        os.makedirs(os.path.dirname(target) or folder, exist_ok=True)
        shutil.copy2(source_file, target)
        restored.append(rel_path)

    if restored:
        row = registry.get_installed(str(meta.get("type") or "skill"), str(meta.get("id") or ""))
        if row is not None:
            marked = sorted(set(list(row.get("user_modified_files_json") or []) + restored))
            registry.update_installed(
                row["type"],
                row["id"],
                user_modified=True,
                user_modified_files_json=marked,
            )
    return {
        "ok": not missing,
        "install_id": install_id,
        "restored_files": restored,
        "missing": missing,
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
