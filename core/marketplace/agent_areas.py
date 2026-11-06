"""
core.marketplace.agent_areas - the hashing area of an installed agent.

An agent consists of the files the platform generates from the package:
``orchestrator.json``/``manifest.json``, the system prompt, the instruction
files and the custom functions (or the attachments of a helper). Everything
else a user keeps in the same folder - notes, exports, logs - is outside the
area, so it never marks the installation as modified and an update never
touches it.

The area is what the registry row records and what an update compares against,
so these helpers are the single place that decides which files belong to an
entity.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from core.marketplace import registry


def _orchestrator_area_files(slug: str) -> Dict[str, str]:
    """Return ``{relative path: sha256}`` for the hashing area of an agent.

    The area is the agent part of the orchestrator folder: the bundle, the
    prompt, the instruction files and the custom functions (D-04). Anything
    else the user keeps in that folder - notes, exports, logs - is not part
    of the entity and never marks the installation as modified.
    """
    from core.orchestrator_folders import get_orchestrator_dir

    folder = get_orchestrator_dir(slug)
    if not os.path.isdir(folder):
        return {}
    manifest: Dict[str, str] = {}
    for root, dirs, names in os.walk(folder):
        dirs[:] = [
            name for name in dirs
            if name not in ("__pycache__", ".git")
            and os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/") in (
                "instructions", "functions"
            )
        ]
        for name in sorted(names):
            full = os.path.join(root, name)
            rel_path = os.path.relpath(full, folder).replace(os.sep, "/")
            if rel_path in ("orchestrator.json", "system_prompt.md"):
                manifest[rel_path] = registry._sha256_file(full)
            elif rel_path.startswith(("instructions/", "functions/")) and rel_path.endswith((".md", ".py")):
                manifest[rel_path] = registry._sha256_file(full)
    return manifest


def _assistant_area_files(slug: str, declared: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return ``{relative path: sha256}`` for the hashing area of a helper.

    A helper consists of its manifest, its prompt and the attachment files
    the package ships. Attachments a user adds themselves are outside the
    area: an update must not touch them and must not report them as a
    modification of the entity.

    With *declared* the manifest of the installed version decides the area,
    which is what keeps a user-added file out of it. Without it - a legacy
    folder the registry never recorded - the folder is walked instead, which
    is the best evidence available at that point.
    """
    from core.assistant_folders import get_assistant_dir

    folder = get_assistant_dir(slug)
    if not os.path.isdir(folder):
        return {}
    if declared:
        manifest: Dict[str, str] = {}
        for rel_path in declared:
            full = os.path.join(folder, rel_path)
            manifest[rel_path] = registry._sha256_file(full) if os.path.isfile(full) else ""
        return manifest
    manifest = {}
    for name in ("manifest.json", "prompt.md"):
        full = os.path.join(folder, name)
        if os.path.isfile(full):
            manifest[name] = registry._sha256_file(full)
    files_dir = os.path.join(folder, "files")
    if os.path.isdir(files_dir):
        for name in sorted(os.listdir(files_dir)):
            full = os.path.join(files_dir, name)
            if os.path.isfile(full):
                manifest[f"files/{name}"] = registry._sha256_file(full)
    return manifest


def area_files(
    slug: str,
    agent_kind: str = "",
    declared: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return ``{relative path: sha256}`` for the agent part of a folder.

    Public entry point for callers outside the installer (the migration and
    the update check): an agent consists of the files the platform generates
    for its kind plus what the package ships, never of the notes or exports a
    user keeps in the same folder. The kind comes from the registry when the
    caller does not know it, so a helper is not hashed as an employee.

    *declared* is the file manifest of the installed version; when given it
    decides which paths take part, which keeps a file the user added out of
    the area.
    """
    kind = str(agent_kind or "").strip() or registry.installed_agent_kind(slug)
    if not kind:
        # No registry row yet (a legacy orphan or a migration): the folder
        # that actually exists decides, so a helper is never hashed as an
        # employee.
        orchestrator_dir = os.path.join(registry.agents_root(), slug)
        kind = "orchestrator" if os.path.isdir(orchestrator_dir) else "assistant"
    if kind == "assistant":
        return _assistant_area_files(slug, declared)
    return _orchestrator_area_files(slug)


def _remove_stale_area_files(slug: str, declared: Dict[str, str], agent_kind: str = "") -> List[str]:
    """Delete area files the new version does not declare; return their paths.

    An update replaces the files of the entity, it does not add to them: an
    instruction, a function or an attachment the new version dropped has to
    leave the folder too, otherwise it stays on disk, enters the recorded
    manifest and keeps working as part of an agent that no longer ships it.
    Only the hashing area is touched - the notes a user keeps in the same
    folder and the attachments they added themselves are theirs.
    """
    kind = str(agent_kind or "").strip() or registry.installed_agent_kind(slug)
    folder = registry.agent_folder(slug, kind)
    if not folder or not os.path.isdir(folder):
        return []
    removed: List[str] = []
    for rel_path in sorted(area_files(slug, kind)):
        if rel_path in declared:
            continue
        full = os.path.join(folder, rel_path)
        if not os.path.isfile(full):
            continue
        try:
            os.remove(full)
        except OSError:
            continue
        removed.append(rel_path)
    if kind == "assistant":
        # A helper has no instruction tree to prune; its whole area is the
        # manifest, the prompt and the attachments.
        return removed
    # An empty directory left behind by a dropped instruction reads as a
    # broken entity, so the two area folders are pruned of empty subfolders.
    for area in ("instructions", "functions"):
        area_dir = os.path.join(folder, area)
        if not os.path.isdir(area_dir):
            continue
        for name in sorted(os.listdir(area_dir)):
            sub = os.path.join(area_dir, name)
            if os.path.isdir(sub) and not os.listdir(sub):
                try:
                    os.rmdir(sub)
                except OSError:
                    pass
    return removed


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
