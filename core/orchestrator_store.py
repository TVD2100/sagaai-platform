# -*- coding: utf-8 -*-
"""
core.orchestrator_store - persistence of an orchestrator and of its folder.

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.orchestrator_defaults import (
    DEVAGENT_SLUG,
    PROMPT_USER_EDITED_KEY,
)
from core.orchestrator_folders import (
    list_orchestrator_folder_slugs,
    load_orchestrator_bundle,
    load_orchestrator_prompt_file,
    orchestrator_folder_exists,
    remove_orchestrator_dir,
    safe_orchestrator_slug,
    save_orchestrator_bundle,
    sync_orchestrator_instructions,
)
from core.orchestrator_shared import late
from core.orchestrator_transfer import (
    _EXPORT_FORMAT,
    _validate_imported_tools,
)
from storage.models import DEFAULT_MAX_STEPS
from storage.repository import (
    repo_create_orchestrator,
    repo_delete_all_orchestrator_instructions,
    repo_delete_orchestrator,
    repo_get_orchestrator_by_slug,
    repo_list_orchestrators,
)

# Resolved on the facade when called, never bound at import time.
_sync_orchestrator_folder = late("_sync_orchestrator_folder")
repo_get_orchestrator_with_text = late("repo_get_orchestrator_with_text")
repo_update_orchestrator = late("repo_update_orchestrator")


def list_orchestrators_impl() -> List[Dict[str, Any]]:
    """Return all orchestrators (without prompt_text), for navigation."""
    return repo_list_orchestrators()


def get_orchestrator_impl(slug: str) -> Optional[Dict[str, Any]]:
    """Return a full orchestrator dict including prompt_text and config."""
    return repo_get_orchestrator_with_text(slug)


def get_orchestrator_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """Return orchestrator metadata (no prompt_text), or None."""
    return repo_get_orchestrator_by_slug(slug)


def create_orchestrator(slug: str, name: str, description: str = "",
                        prompt_text: str = "", config: dict = None,
                        tools: list = None, max_steps: int = DEFAULT_MAX_STEPS,
                        auto_apply: bool = True) -> Optional[str]:
    """Create a new orchestrator. Returns the id on success, None on failure.

    Also creates the orchestrator's personal folder on disk and writes an
    initial bundle (orchestrator.json).
    """
    # Normalise slug to [a-z0-9_]+ so it is safe as a DB slug and a folder
    # name. Empty input (or a slug made only of invalid characters) is
    # rejected instead of silently creating an 'unnamed' folder.
    slug = safe_orchestrator_slug(slug)
    if not slug:
        return None
    # Ensure no duplicate slug.
    existing = repo_get_orchestrator_by_slug(slug)
    if existing:
        return None
    orch_id = uuid.uuid4().hex[:8]
    ok = repo_create_orchestrator(
        orchestrator_id=orch_id,
        slug=slug,
        name=name.strip(),
        description=description,
        prompt_text=prompt_text,
        config=config or {},
        tools=tools or [],
        max_steps=max_steps,
        auto_apply=auto_apply,
        is_builtin=False,
        sort_order=100,  # user-created orchestrators go before DevAgent
    )
    if ok:
        _sync_orchestrator_folder(slug)
    return orch_id if ok else None


def _mark_prompt_edited(orch: Dict[str, Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Record the prompt_user_edited marker when the system prompt changes.

    Called by save_orchestrator() and save_devagent_config() so a user edit of
    the built-in orchestrator's prompt survives the boot-time refresh in
    ensure_builtin_orchestrators(). The marker lives in the config dict and is
    only set for built-in orchestrators whose prompt_text is actually updated.
    Returns the (possibly copied) kwargs dict.
    """
    if "prompt_text" not in kwargs or not orch.get("is_builtin"):
        return kwargs
    # Prefer a config dict passed in the same call; otherwise take the stored
    # one. Never drop a caller-provided config.
    if isinstance(kwargs.get("config"), dict):
        cfg = dict(kwargs["config"])
    else:
        cfg = orch.get("config", {})
        if not isinstance(cfg, dict):
            cfg = {}
        cfg = dict(cfg)
    if cfg.get(PROMPT_USER_EDITED_KEY) is True:
        return kwargs
    cfg[PROMPT_USER_EDITED_KEY] = True
    kwargs = dict(kwargs)
    kwargs["config"] = cfg
    return kwargs


def reset_builtin_prompt_impl(slug: str = DEVAGENT_SLUG) -> bool:
    """Drop the prompt_user_edited marker and restore the shipped prompt.

    Clears the marker so the next ensure_builtin_orchestrators() boot refresh
    re-applies the bundled dev_agent/system_prompt.md, and refreshes the prompt
    immediately for the running session. Only built-in orchestrators are
    affected. Returns True on success.
    """
    orch = repo_get_orchestrator_with_text(slug)
    if orch is None or not orch.get("is_builtin"):
        return False
    cfg = orch.get("config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = dict(cfg)
    cfg.pop(PROMPT_USER_EDITED_KEY, None)
    kwargs: Dict[str, Any] = {"config": cfg}
    if slug == DEVAGENT_SLUG:
        try:
            from pathlib import Path
            prompt_file = (
                Path(__file__).resolve().parent.parent
                / "dev_agent" / "system_prompt.md"
            )
            kwargs["prompt_text"] = prompt_file.read_text(encoding="utf-8")
        except Exception:
            pass
    ok = repo_update_orchestrator(orch["id"], **kwargs)
    if ok:
        _sync_orchestrator_folder(slug)
    return ok


def save_orchestrator(slug: str, **kwargs) -> bool:
    """Update an existing orchestrator by slug.

    Supported kwargs: name, description, prompt_text, config, tools,
    max_steps, auto_apply, sort_order, memory_dir.

    ``memory_dir`` is a convenience key: it is written into ``config`` (the
    stored location of the setting) instead of becoming a column, so the
    working-data-folder resolver keeps a single source of truth.

    Returns True on success.
    """
    orch = repo_get_orchestrator_with_text(slug)
    if orch is None:
        return False
    if "memory_dir" in kwargs:
        raw_dir = kwargs.pop("memory_dir")
        base_cfg = kwargs.get("config")
        if not isinstance(base_cfg, dict):
            stored = orch.get("config", {})
            base_cfg = dict(stored) if isinstance(stored, dict) else {}
        base_cfg = dict(base_cfg)
        text = str(raw_dir or "").strip()
        if text:
            base_cfg["memory_dir"] = text
        else:
            # An empty value means "use the default": drop the key instead of
            # storing an empty string the resolver would have to special-case.
            base_cfg.pop("memory_dir", None)
        kwargs["config"] = base_cfg
    kwargs = _mark_prompt_edited(orch, kwargs)
    ok = repo_update_orchestrator(orch["id"], **kwargs)
    if ok:
        # The folder bundle only mirrors the DB record. A failed mirror write
        # (read-only volume, permissions, a stale bundle) must not turn a
        # successful update into an exception: the caller would lose the
        # confirmation and the user would see "nothing happened".
        try:
            _sync_orchestrator_folder(slug)
        except Exception:
            pass
    return ok


def delete_orchestrator_impl(slug: str) -> bool:
    """Delete an orchestrator.  Built-in orchestrators cannot be deleted.

    Also removes the orchestrator's personal folder from disk.
    """
    orch = repo_get_orchestrator_with_text(slug)
    if orch is None:
        return False
    if orch.get("is_builtin"):
        return False
    ok = repo_delete_orchestrator(orch["id"])
    if ok:
        # The orchestrator_instructions cache rows would otherwise survive
        # the deletion and could leak into a future orchestrator created
        # with the same slug.
        repo_delete_all_orchestrator_instructions(slug)
        remove_orchestrator_dir(slug)
        _mark_marketplace_removed(slug, orch)
        # The personal memory of the employee is archived (never silently
        # destroyed) so an accidental deletion stays recoverable.
        try:
            from core import personal_memory as pm
            pm.delete_memory(
                pm.format_scope(pm.SCOPE_ORCHESTRATOR, slug),
                archived_by="delete_orchestrator",
            )
        except Exception:
            pass
    return ok


def _mark_marketplace_removed(slug: str, orch: Optional[Dict[str, Any]] = None) -> None:
    """Record a deliberate deletion so nothing resurrects the orchestrator.

    The bundled default import recreates any orchestrator folder that is
    missing, and the marketplace keeps its own record of removals. Without
    this marker a deleted catalog agent comes back on the next start with a
    fresh prompt and settings the user did not choose.

    The catalog id comes from the ``marketplace`` marker the installer wrote
    into the orchestrator config; an orchestrator created by hand has no such
    marker and is simply a local deletion the marketplace never sees.
    """
    config = (orch or {}).get("config")
    marker = config.get("marketplace") if isinstance(config, dict) else None
    entity_id = str((marker or {}).get("id") or "") if isinstance(marker, dict) else ""
    try:
        from core.marketplace import registry

        if not entity_id:
            # An adopted entity carries no installer marker: its catalog id is
            # the registry row that owns this folder.
            for row in registry.list_installed("agent"):
                if str(row.get("runtime_slug") or "") == slug:
                    entity_id = str(row.get("id") or "")
                    break
        if not entity_id:
            return
        registry.remove_installed("agent", entity_id)
        registry.mark_removed("agent", entity_id, slug, via="user")
    except Exception:
        pass


def _sync_orchestrator_folder_impl(slug: str) -> None:
    """Write the current orchestrator record as orchestrator.json bundle.

    Uses get_orchestrator() so the bundle always has the latest prompt_text
    and config from the DB.  Does not overwrite the functions/ or
    instructions/ parts of an existing folder (they are edited by
    DevAgent / the user separately). Instructions cache is rebuilt too.
    """
    orch = repo_get_orchestrator_with_text(slug)
    if orch is None:
        return
    bundle = {
        "format": _EXPORT_FORMAT,
        "slug": orch.get("slug", slug),
        "name": orch.get("name", ""),
        "description": orch.get("description", ""),
        "prompt_text": orch.get("prompt_text", ""),
        "config": orch.get("config", {}),
        "tools": orch.get("tools", []),
        "max_steps": orch.get("max_steps", DEFAULT_MAX_STEPS),
        "auto_apply": orch.get("auto_apply", True),
        "exported_at": datetime.now().isoformat(),
    }
    save_orchestrator_bundle(slug, bundle)
    # Keep the instruction cache in sync too (rebuild from md files).
    try:
        sync_orchestrator_instructions(slug)
    except Exception:
        pass


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

    existing = repo_get_orchestrator_with_text(slug)
    if existing is not None:
        update_kwargs: Dict[str, Any] = dict(
            name=str(bundle.get("name") or slug),
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

    orch_id = uuid.uuid4().hex[:8]
    ok = repo_create_orchestrator(
        orchestrator_id=orch_id,
        slug=slug,
        name=str(bundle.get("name") or slug),
        description=str(bundle.get("description") or ""),
        prompt_text=prompt_text,
        config=config,
        tools=tools,
        max_steps=int(bundle.get("max_steps", DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS),
        auto_apply=bool(bundle.get("auto_apply", True)),
        is_builtin=False,
        sort_order=int(bundle.get("sort_order", 100) or 100),
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
