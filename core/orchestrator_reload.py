# -*- coding: utf-8 -*-
"""
core.orchestrator_reload - the "folders first" direction of the sync.

An orchestrator folder is the source of truth: ``orchestrator.json`` and
``system_prompt.md`` survive a database reset and are what the user edits by
hand. This module reads a folder back into the database, at startup and from
the settings Sync button. The write direction lives in
``core.orchestrator_store``.

Part of ``core.orchestrators``, which is the public surface of this package.
A name the tests may replace through the facade is resolved late, at call
time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
import uuid
from typing import Any, Dict

from core.naming import names_from_bundle
from core.orchestrator_folders import (
    list_orchestrator_folder_slugs,
    load_orchestrator_bundle,
    load_orchestrator_prompt_file,
    orchestrator_folder_exists,
    sync_orchestrator_instructions,
)
from core.orchestrator_shared import late
from core.orchestrator_transfer import _validate_imported_tools
from storage.models import DEFAULT_MAX_STEPS
from storage.repository import repo_create_orchestrator

# Resolved on the facade when called, never bound at import time.
repo_get_orchestrator_with_text = late("repo_get_orchestrator_with_text")
repo_update_orchestrator = late("repo_update_orchestrator")


def _name_kwargs(bundle: Dict[str, Any], slug: str) -> Dict[str, Any]:
    """Return the repository fields that carry the four name slots.

    A hand-edited or marketplace-authored ``orchestrator.json`` may carry the
    translations as ``name_en`` family keys or as one name mapping; the
    helper accepts both shapes and keeps the user-defined slot separate.
    """
    fields = names_from_bundle(bundle, slug)
    return {"name": fields["name"], "name_en": fields["name_en"],
            "name_ru": fields["name_ru"], "name_zh": fields["name_zh"]}


def reload_orchestrator_from_folder(slug: str) -> Dict[str, Any]:
    """Reload an orchestrator from its folder into the DB.

    Reads orchestrator.json (and system_prompt.md when present) and updates
    the DB record. This is the "folders first" direction used at startup and
    by the settings Sync button. Instructions are rebuilt into the cache.

    Returns a status dict: {ok, slug, action, error}.
    """
    if not orchestrator_folder_exists(slug):
        return {"ok": False, "slug": slug, "action": "skip", "error": "folder missing"}
    bundle = load_orchestrator_bundle(slug)
    if bundle is None:
        return {"ok": False, "slug": slug, "action": "skip", "error": "unreadable orchestrator.json"}

    prompt_text = str(bundle.get("prompt_text") or "")
    prompt_file_text = load_orchestrator_prompt_file(slug)
    if prompt_file_text.strip():
        prompt_text = prompt_file_text

    config = bundle.get("config")
    if not isinstance(config, dict):
        config = {}
    raw_tools = bundle.get("tools")
    if not isinstance(raw_tools, list):
        raw_tools = []
    tools, _rejected = _validate_imported_tools(raw_tools)
    names = _name_kwargs(bundle, slug)

    existing = repo_get_orchestrator_with_text(slug)
    if existing is not None:
        update_kwargs: Dict[str, Any] = dict(
            names,
            description=str(bundle.get("description") or ""),
            config=config,
            tools=tools,
            max_steps=int(bundle.get("max_steps", existing.get("max_steps", DEFAULT_MAX_STEPS)) or DEFAULT_MAX_STEPS),
            auto_apply=bool(bundle.get("auto_apply", existing.get("auto_apply", True))),
        )
        if not existing.get("is_builtin"):
            update_kwargs["prompt_text"] = prompt_text
        repo_update_orchestrator(existing["id"], **update_kwargs)
        sync_orchestrator_instructions(slug)
        return {"ok": True, "slug": slug, "action": "updated"}

    ok = repo_create_orchestrator(
        orchestrator_id=uuid.uuid4().hex[:8],
        slug=slug,
        description=str(bundle.get("description") or ""),
        prompt_text=prompt_text,
        config=config,
        tools=tools,
        max_steps=int(bundle.get("max_steps", DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS),
        auto_apply=bool(bundle.get("auto_apply", True)),
        is_builtin=False,
        sort_order=int(bundle.get("sort_order", 100) or 100),
        **names,
    )
    if ok:
        sync_orchestrator_instructions(slug)
        return {"ok": True, "slug": slug, "action": "created"}
    return {"ok": False, "slug": slug, "action": "skip", "error": "db create failed"}


def sync_all_orchestrator_folders() -> Dict[str, Any]:
    """Reload every existing orchestrator folder into the DB.

    Called at startup and by the settings Sync button. Returns per-slug status.
    """
    results: Dict[str, Any] = {}
    for slug in list_orchestrator_folder_slugs():
        try:
            res = reload_orchestrator_from_folder(slug)
            results[slug] = res.get("action", "error") + ("" if res.get("ok") else ": " + str(res.get("error", "")))
        except Exception as exc:
            results[slug] = "error: " + str(exc)
    return results


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
