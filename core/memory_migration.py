# -*- coding: utf-8 -*-
"""
core.memory_migration - move legacy working-data folders to the central root.

Installations built before the central root existed created one
``<workspace>/<slug>_data`` folder per employee inside whichever project the
role was used from. The central layout keeps that data in
``DATA_DIR/agents_data/<slug>/`` instead, so the same role has the same memory
in every project.

Migrating is deliberately a TWO-STEP operation:

1. ``plan_migrations`` only READS the disk and returns one entry per legacy
   folder with a target path, a file count and a status. Nothing is written.
2. ``apply_migration`` copies the files into the target and verifies the copy
   before the source is left untouched - the legacy folder is NEVER deleted by
   this module. The owner removes it after checking the report.

A conflict (the target already holds files) is reported, never resolved by
merging: silently mixing two registries is how records get lost.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List, Optional

from core import orchestrator_folders as of

#: Status of one migration entry.
STATUS_READY = "ready"              # legacy has files, target empty/missing
STATUS_EMPTY = "empty"              # legacy folder has no files - nothing to do
STATUS_ALREADY = "already_migrated" # target already holds the same content
STATUS_CONFLICT = "conflict"        # both sides hold files - manual decision
STATUS_NO_SOURCE = "no_source"      # nothing found anywhere


def _files_below(folder: str) -> List[str]:
    """Return relative paths of every file below *folder* (sorted, bounded)."""
    found: List[str] = []
    if not folder or not os.path.isdir(folder):
        return found
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if name.startswith(".DS_Store"):
                continue
            full = os.path.join(root, name)
            found.append(os.path.relpath(full, folder).replace(os.sep, "/"))
            if len(found) > 5000:
                return sorted(found)
    return sorted(found)


def _same_tree(source: str, target: str) -> bool:
    """True when both folders hold the same relative files with the same size."""
    src = _files_below(source)
    dst = _files_below(target)
    if not src or src != dst:
        return False
    for rel in src:
        try:
            if os.path.getsize(os.path.join(source, rel)) != os.path.getsize(
                    os.path.join(target, rel)):
                return False
        except OSError:
            return False
    return True


def plan_migration(slug: str, workspace: str = "") -> Dict[str, Any]:
    """Describe how ONE employee's data would move to the central root.

    Read-only. The source is the legacy ``<workspace>/<slug>_data`` folder, the
    target is the folder the resolver reports for the role right now - so a
    role that deliberately keeps its data in the project is skipped instead of
    being "fixed".

    Args:
        slug: employee slug; normalised by the resolver.
        workspace: project folder that still holds the legacy data.

    Returns:
        A dict with ``slug``, ``source``, ``target``, ``files``, ``status``,
        ``target_files`` and ``reason`` (a human sentence for the report).
        ``source`` is empty when no legacy folder exists.
    """
    safe = of.safe_orchestrator_slug(slug) or "unnamed"
    ws = (workspace or "").strip()
    legacy = (os.path.join(ws, of.default_data_dir_name(safe)) if ws else "")
    # The central root carries the same ``_data`` suffix, so an installation
    # that runs from its own project root would otherwise report that root as
    # the legacy folder of an employee called "agents".
    if legacy and os.path.abspath(legacy) == os.path.abspath(of.agents_data_root()):
        legacy = ""
    resolved = of.resolve_data_dir(safe, ws)
    target = str(resolved.get("path") or "")

    source_files = _files_below(legacy)
    target_files = _files_below(target)

    entry: Dict[str, Any] = {
        "slug": safe,
        "source": legacy,
        "target": target,
        "files": len(source_files),
        "target_files": len(target_files),
        "status": STATUS_NO_SOURCE,
        "reason": "",
    }

    if not legacy or not os.path.isdir(legacy):
        entry["reason"] = "no legacy folder in this project"
        return entry
    if not source_files:
        entry["status"] = STATUS_EMPTY
        entry["reason"] = "legacy folder holds no files"
        return entry

    # A role whose CURRENT folder is the legacy folder keeps working as before:
    # moving it would change where the employee looks.
    if target and os.path.abspath(target) == os.path.abspath(legacy):
        entry["status"] = STATUS_ALREADY
        entry["reason"] = "the role already uses this folder"
        return entry

    if target_files:
        if target and _same_tree(legacy, target):
            entry["status"] = STATUS_ALREADY
            entry["reason"] = "the target already holds the same files"
        else:
            entry["status"] = STATUS_CONFLICT
            entry["reason"] = (
                f"target already holds {len(target_files)} file(s); "
                "merge manually"
            )
        return entry

    entry["status"] = STATUS_READY
    entry["reason"] = f"{len(source_files)} file(s) can move to the central folder"
    return entry


def plan_migrations(workspace: str = "", slugs: Optional[List[str]] = None,
                    include_known: bool = True) -> List[Dict[str, Any]]:
    """Return a migration plan for every employee visible from *workspace*.

    Sources of candidate slugs, in order: the passed *slugs*, the employee
    folders on disk, and the ``<name>_data`` folders that actually exist in the
    workspace. A legacy folder whose employee record was deleted is therefore
    still found and reported - its data must not be lost just because the role
    is gone.

    Args:
        workspace: project folder to scan for legacy ``<slug>_data`` folders.
        slugs: explicit employee slugs to include.
        include_known: also scan the stored employees and the folder names.

    Returns:
        A list of entries (see ``plan_migration``), sorted by slug; only
        entries that have a legacy folder are returned.
    """
    ws = (workspace or "").strip()
    candidates: List[str] = []

    def _add(value: str) -> None:
        safe = of.safe_orchestrator_slug(value)
        if safe and safe not in candidates:
            candidates.append(safe)

    for value in (slugs or []):
        _add(value)

    if include_known:
        try:
            from core.orchestrators import list_orchestrators
            for item in list_orchestrators() or []:
                slug = item.get("slug") if isinstance(item, dict) else ""
                if slug:
                    _add(str(slug))
        except Exception:
            pass
        try:
            for name in of.list_orchestrator_folder_slugs():
                _add(name)
        except Exception:
            pass

    if ws and os.path.isdir(ws):
        suffix = "_data"
        try:
            for name in sorted(os.listdir(ws)):
                full = os.path.join(ws, name)
                if (not os.path.isdir(full) or not name.endswith(suffix)
                        or name.startswith(".")):
                    continue
                # Derived from a folder name only: the slug itself is
                # normalised by the resolver, and a folder that is not an
                # employee-derived one is still reported rather than ignored.
                _add(name[:-len(suffix)])
        except OSError:
            pass

    plan: List[Dict[str, Any]] = []
    for slug in sorted(candidates):
        entry = plan_migration(slug, ws)
        if entry["source"] and os.path.isdir(entry["source"]):
            plan.append(entry)
    return plan


def apply_migration(entry: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Copy ONE planned migration into its central target.

    The copy is verified file by file (name and size) BEFORE the result is
    reported as success. The source folder is left in place in every case: the
    owner deletes it after checking the report, so a failure can never lose
    data.

    Args:
        entry: an entry produced by ``plan_migration``/``plan_migrations``.
        dry_run: report what would happen without writing anything.

    Returns:
        A dict with ``ok``, ``status``, ``copied``, ``source``, ``target`` and
        ``reason``. ``ok`` is False for a conflict, an empty plan or a copy
        failure; the reason explains which.
    """
    source = str(entry.get("source") or "")
    target = str(entry.get("target") or "")
    status = str(entry.get("status") or "")
    result: Dict[str, Any] = {
        "ok": False,
        "status": status,
        "copied": 0,
        "source": source,
        "target": target,
        "reason": "",
    }

    if status in (STATUS_ALREADY, STATUS_EMPTY, STATUS_NO_SOURCE):
        result["ok"] = True
        result["reason"] = str(entry.get("reason") or "nothing to do")
        return result
    if status == STATUS_CONFLICT:
        result["reason"] = (
            "target already holds files: merge manually, nothing was copied"
        )
        return result
    if status != STATUS_READY or not source or not target:
        result["reason"] = "entry is not ready for migration"
        return result
    if not os.path.isdir(source):
        result["reason"] = "source folder disappeared before the copy"
        return result

    if dry_run:
        result["ok"] = True
        result["reason"] = (
            f"dry run: {entry.get('files', 0)} file(s) would be copied from "
            f"{source} to {target}"
        )
        return result

    try:
        os.makedirs(target, exist_ok=True)
        for rel in _files_below(source):
            src_file = os.path.join(source, rel)
            dst_file = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dst_file) or target, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            result["copied"] += 1
    except OSError as exc:
        result["reason"] = f"copy failed after {result['copied']} file(s): {exc}"
        return result

    if not _same_tree(source, target):
        result["reason"] = (
            "copied files do not match the source: check the target before "
            "removing anything"
        )
        return result

    result["ok"] = True
    result["reason"] = (
        f"copied {result['copied']} file(s); the legacy folder is kept - "
        "remove it after checking"
    )
    return result


def apply_plan(plan: List[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
    """Apply a whole plan and summarise it.

    Args:
        plan: entries from ``plan_migrations``.
        dry_run: report without writing.

    Returns:
        A dict with ``ok``, ``dry_run``, ``total_copied``, ``applied`` (the
        per-entry results) and ``failed`` (entries whose ``ok`` is False).
    """
    applied: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    total = 0
    for entry in plan or []:
        outcome = apply_migration(entry, dry_run=dry_run)
        outcome["slug"] = entry.get("slug", "")
        applied.append(outcome)
        total += int(outcome.get("copied", 0) or 0)
        if not outcome.get("ok"):
            failed.append(outcome)
    return {
        "ok": not failed,
        "dry_run": bool(dry_run),
        "total_copied": total,
        "applied": applied,
        "failed": failed,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
