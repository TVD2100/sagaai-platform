# -*- coding: utf-8 -*-
"""
core.entity_sync - startup and on-demand folder <-> DB synchronisation.

Implements the "folders first" model:

    DATA_DIR/assistants/<slug>/      -> assistants table (runtime cache)
    DATA_DIR/orchestrators/<slug>/   -> orchestrators table (runtime cache)
    DATA_DIR/orchestrators/<slug>/instructions/*.md -> orchestrator_instructions table

Direction at startup / Sync button:
    1. Every assistant folder on disk without a DB record is imported.
    2. Every DB assistant without a folder is exported to a folder
       (one-time migration from the legacy system_prompts layout).
    3. Every orchestrator folder is reloaded into the DB.
    4. The instruction cache is rebuilt for every orchestrator folder.

The hot path afterwards reads only from the database; folders are refreshed
whenever content changes through the UI or through DevAgent.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from core.assistant_folders import (
    list_assistant_folder_names,
    assistant_folder_exists,
    sync_assistant_to_folder,
    normalize_slug,
)
from storage.repository import (
    repo_load_assistants,
    repo_get_assistant_by_slug,
    repo_set_assistant_slug,
)


def ensure_entity_folders_sync() -> Dict[str, Any]:
    """Run the full folders-first sync: assistants + orchestrators.

    Idempotent; safe to call on every startup and from the settings page.
    Returns a status dict:
        {"assistants": {slug: action}, "orchestrators": {slug: action}}
    """
    return {
        "assistants": sync_assistants(),
        "orchestrators": sync_orchestrators(),
    }


def sync_assistants() -> Dict[str, str]:
    """Import assistant folders into the DB and migrate legacy DB records.

    Returns per-slug status: "imported" | "created_folder" | "backfilled_slug"
    | "skipped" | "error: ...".
    """
    from core.assistants import reload_assistant_from_folder

    results: Dict[str, str] = {}

    # 1) Folders first: every folder on disk that has no DB record is imported.
    for slug in list_assistant_folder_names():
        if repo_get_assistant_by_slug(slug):
            continue
        try:
            res = reload_assistant_from_folder(slug)
            results[slug] = res.get("action", "error") if res.get("ok") else "error: " + str(res.get("error", ""))
        except Exception as exc:
            results[slug] = "error: " + str(exc)

    # 2) Backfill: every DB assistant must have a slug and a folder.
    for assistant in repo_load_assistants():
        aid = assistant.get("id", "")
        slug = assistant.get("slug")
        if not slug:
            slug = _backfill_slug(aid, assistant)
            assistant["slug"] = slug
        if not assistant_folder_exists(slug):
            try:
                from core.assistants import get_assistant_by_id
                full = get_assistant_by_id(aid)
                if full:
                    sync_assistant_to_folder(full)
                    results[slug] = "created_folder"
            except Exception as exc:
                results[slug] = "error: " + str(exc)

    return results


def sync_orchestrators() -> Dict[str, str]:
    """Reload every orchestrator folder into the DB and rebuild instruction cache.

    Returns per-slug status.
    """
    try:
        from core.orchestrators import sync_all_orchestrator_folders
        return sync_all_orchestrator_folders()
    except Exception as exc:
        return {"_all": "error: " + str(exc)}


def _backfill_slug(assistant_id: str, assistant: Dict[str, Any]) -> str:
    """Generate and store a slug for a legacy DB assistant without one."""
    name = assistant.get("name", "") or "assistant"
    base = normalize_slug(name)
    candidate = base
    suffix = 2
    while repo_get_assistant_by_slug(candidate):
        candidate = f"{base}_{suffix}"
        suffix += 1
    repo_set_assistant_slug(assistant_id, candidate)
    return candidate
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
