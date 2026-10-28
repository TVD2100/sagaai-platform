"""
core.marketplace.rollback - undo one marketplace run.

Every install, update and uninstall writes a run meta file into its backup
folder *before* touching the installation. The meta lists what the run is
about to create, replace and delete, plus a copy of the target registry
file. ``rollback_run()`` uses that description to restore the previous
state byte-for-byte, which is what both the user-facing "undo" and the
crash recovery path rely on.

Restoring covers four things, all of them required by the acceptance
criteria: entity files, the registry row in SQLite, the type-level
registry file (``skills.json`` for a skill) and the run log entry.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Optional

from core.marketplace import registry, store


class RollbackError(store.MarketplaceError):
    """Raised when a run cannot be undone from its backup."""


def _remove_path(path: str) -> None:
    """Remove a file or a directory tree, ignoring absent paths."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _entity_folder(type_: str, runtime_slug: str) -> Optional[str]:
    """Return the runtime folder of an entity even when it no longer exists.

    ``registry.runtime_folder`` returns None for a missing folder; rollback
    needs the path anyway to know what to delete, so the roots are rebuilt
    directly from the module constants.
    """
    if not runtime_slug:
        return None
    if type_ == "skill":
        return os.path.join(registry.skills_root(), runtime_slug)
    if type_ == "rag":
        return os.path.join(registry.rag_root(), runtime_slug)
    if type_ == "agent":
        return registry.agent_folder(runtime_slug)
    return None


def _restore_file(install_id: str, folder: str, rel_path: str) -> bool:
    """Copy one backed-up file back into the entity folder.

    Returns False when the backup does not hold the file - callers treat
    that as a partial rollback and report it instead of pretending success.
    """
    source = os.path.join(store.backup_dir(install_id), "files", rel_path)
    if not os.path.isfile(source):
        return False
    target = os.path.join(folder, rel_path)
    os.makedirs(os.path.dirname(target) or folder, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _restore_registry_backup(meta: dict) -> bool:
    """Restore the type-level registry file from its pre-run copy."""
    backup_rel = str(meta.get("registry_backup") or "")
    target_path = str(meta.get("registry_path") or "")
    install_id = str(meta.get("install_id") or "")
    if not backup_rel or not target_path or not install_id:
        return False
    source = os.path.join(store.backup_dir(install_id), backup_rel)
    if not os.path.isfile(source):
        return False
    with open(source, "rb") as handle:
        payload = handle.read()
    store.atomic_write_bytes(target_path, payload)
    return True


def rollback_run(install_id: str, *, reason: str = "user") -> dict:
    """Undo a run and return a report of what was restored.

    The function is deliberately tolerant: every step is attempted even if
    an earlier one failed, because a half-restored installation is worse
    than a fully-attempted one. Anything that could not be restored is
    listed in ``unrestored`` so the caller can surface it instead of
    claiming a clean rollback.
    """
    meta = store.read_run_meta(install_id)
    if meta is None:
        raise RollbackError(
            "missing_run_meta",
            f"No backup metadata found for run {install_id}",
            {"install_id": install_id},
        )

    report: Dict[str, object] = {
        "ok": True,
        "install_id": install_id,
        "type": meta.get("type"),
        "id": meta.get("id"),
        "removed_files": [],
        "restored_files": [],
        "unrestored": [],
    }

    type_ = str(meta.get("type") or "")
    runtime_slug = str(meta.get("runtime_slug") or "")
    folder = _entity_folder(type_, runtime_slug)

    # 1. Files.
    if folder:
        if meta.get("created_folder"):
            _remove_path(folder)
            report["removed_files"] = ["<folder>"]
        else:
            for rel_path in meta.get("created_files") or []:
                _remove_path(os.path.join(folder, rel_path))
                report["removed_files"].append(rel_path)
            restored_targets = list(meta.get("replaced_files") or [])
            restored_targets.extend(meta.get("removed_files") or [])
            for rel_path in restored_targets:
                if _restore_file(install_id, folder, rel_path):
                    report["restored_files"].append(rel_path)
                else:
                    report["unrestored"].append(rel_path)

    # 2. Registry row.
    previous_row = meta.get("previous_row")
    entity_id = str(meta.get("id") or "")
    try:
        if isinstance(previous_row, dict) and previous_row.get("id"):
            registry.upsert_installed(previous_row)
        elif entity_id:
            registry.remove_installed(type_, entity_id)
    except Exception as exc:
        report["unrestored"].append(f"registry row: {exc}")

    # 3. Type-level registry file (skills.json for a skill).
    if meta.get("registry_path"):
        try:
            if not _restore_registry_backup(meta):
                if meta.get("registry_existed") is False:
                    # The file did not exist before the run, so the correct
                    # restored state is "absent" - not "unrestored".
                    _remove_path(str(meta["registry_path"]))
                else:
                    report["unrestored"].append("type registry file")
        except Exception as exc:
            report["unrestored"].append(f"type registry file: {exc}")

    # 4. Type-level bookkeeping (a created skill id must disappear).
    created_skill_id = str(meta.get("created_skill_id") or "")
    if created_skill_id:
        try:
            if not meta.get("registry_path"):
                from core.skills_library import delete_skill

                delete_skill(created_skill_id)
        except Exception as exc:
            report["unrestored"].append(f"skill id {created_skill_id}: {exc}")

    # 4b. An agent installation creates an orchestrator record of its own;
    # the registry row handled above is only the marketplace's view of it.
    if meta.get("created_db_row") and type_ == "agent" and runtime_slug:
        kind = str(meta.get("agent_kind") or "").strip() or registry.installed_agent_kind(runtime_slug)
        task = f"assistant row {runtime_slug}" if kind == "assistant" else f"orchestrator row {runtime_slug}"
        try:
            if kind == "assistant":
                from core.assistants import delete_assistant, get_assistant_by_slug

                row = get_assistant_by_slug(runtime_slug) or {}
                if row.get("id"):
                    delete_assistant(str(row["id"]))
            else:
                from core.orchestrators import delete_orchestrator, get_orchestrator

                if get_orchestrator(runtime_slug) is not None:
                    delete_orchestrator(runtime_slug)
        except Exception as exc:
            report["unrestored"].append(f"{task}: {exc}")

    # 5. Run log and state.
    store.update_run(
        install_id,
        status="rolled_back",
        rolled_back_at=store.isoformat_utc(),
        rollback_reason=reason,
        unrestored=list(report["unrestored"]),
    )
    store.cleanup_run_dirs(install_id, keep_backup=True)
    state = store.get_state()
    if state and state.get("install_id") == install_id:
        store.clear_state()

    if report["unrestored"]:
        report["ok"] = False
    return report
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
