# -*- coding: utf-8 -*-
"""
core.orchestrator_builtin - built-in orchestrators: seeding, backfill and the DevAgent config bridge.

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
import uuid
from typing import Any, Dict, List, Optional

from core.config import get_default_strong_max_tokens
from core.naming import DEVAGENT_NAME_FIELDS
from core.orchestrator_defaults import (
    DEFAULT_WEB_SEARCH_PROMPT,
    DEVAGENT_SLUG,
    PROMPT_USER_EDITED_KEY,
    _default_economy_cache_enabled,
    _default_economy_cache_multiplier,
    _default_economy_tail_messages,
)
from core.orchestrator_links import get_enabled_skills
from core.orchestrator_models import (
    get_economy_cache_enabled,
    get_economy_cache_multiplier,
    get_economy_tail_messages,
)
from core.orchestrator_shared import late
from storage.models import DEFAULT_MAX_STEPS
from storage.repository import repo_create_orchestrator

# Resolved on the facade when called, never bound at import time.
_sync_orchestrator_folder = late("_sync_orchestrator_folder")
get_orchestrator = late("get_orchestrator")
get_web_search_prompt = late("get_web_search_prompt")
repo_get_orchestrator_with_text = late("repo_get_orchestrator_with_text")
repo_update_orchestrator = late("repo_update_orchestrator")


def _ensure_default_orchestrators() -> Dict[str, str]:
    """Create default orchestrators from defaults/orchestrators/*.

    Imported lazily to avoid a circular import with core.default_imports.

    The marketplace master switch is read here because both branches of
    ``ensure_builtin_orchestrators`` reach the bundled defaults through
    this single call. With the switch off the bundled folder stays the
    only source and the marketplace keeps its hands off the defaults.
    """
    from core.default_imports import ensure_default_orchestrators
    from core.marketplace import is_enabled as marketplace_enabled

    results = ensure_default_orchestrators()
    if marketplace_enabled():
        _refresh_marketplace_registry_cache()
    return results


def _refresh_marketplace_registry_cache() -> None:
    """Export the marketplace registry cache after the defaults are seeded.

    Bundled defaults are local entities, not catalog ones, so nothing is
    written to ``marketplace_installed`` here; what this keeps in step is
    the exported cache the marketplace pages read. A failure is ignored on
    purpose: a stale cache must never block the start-up of defaults.
    """
    try:
        from core.marketplace import registry as marketplace_registry

        marketplace_registry.export_registry_cache()
    except Exception:
        pass


def _load_bundled_prompt_text() -> str:
    """Return the bundled DevAgent system prompt (dev_agent/system_prompt.md)."""
    from pathlib import Path

    prompt_file = (
        Path(__file__).resolve().parent.parent
        / "dev_agent" / "system_prompt.md"
    )
    try:
        return prompt_file.read_text(encoding="utf-8")
    except Exception:
        return "You are DevAgent, the embedded developer of SagaAI."


def _first_boot_config(legacy: Dict[str, Any]) -> Dict[str, Any]:
    """Build the DevAgent config on first boot from the legacy KV settings."""
    return {
        "strong_service": legacy.get("strong_service", "") or "DeepSeek",
        "strong_model": legacy.get("strong_model", "") or "deepseek-flash",
        "strong_temperature": float(legacy.get("strong_temperature", 0.4) or 0.4),
        "strong_max_tokens": int(legacy.get("strong_max_tokens") or get_default_strong_max_tokens()) or get_default_strong_max_tokens(),
        "strong_reasoning_effort": "xhigh",
        "vision_service": legacy.get("vision_service", "") or "DeepSeek",
        "vision_model": legacy.get("vision_model", "") or "deepseek-flash",
        "image_service": legacy.get("image_service", "") or "",
        "image_model": legacy.get("image_model", "") or "",
        "search_service": legacy.get("search_service", "") or "DeepSeek",
        "search_model": legacy.get("search_model", "") or "deepseek-flash",
        "search_temperature": float(legacy.get("search_temperature", 0.4) or 0.4),
        "search_max_tool_calls": int(legacy.get("search_max_tool_calls", 3) or 3),
        "search_reasoning_effort": "high",
        "web_search_prompt": str(legacy.get("web_search_prompt", "") or DEFAULT_WEB_SEARCH_PROMPT),
        "fetch_service": legacy.get("fetch_service", "") or "",
        "fetch_model": legacy.get("fetch_model", "") or "",
        "economy_tail_messages": int(legacy.get("economy_tail_messages", _default_economy_tail_messages())
                                     or _default_economy_tail_messages()),
        "economy_cache_enabled": _default_economy_cache_enabled(),
        "economy_cache_multiplier": _default_economy_cache_multiplier(),
        "enabled_skills": [],
        "enabled_connections": [],
        "disabled_tools": [],
    }


def _builtin_tool_names() -> List[str]:
    """Return the tool names the built-in DevAgent starts with."""
    try:
        from dev_agent.tool_executor import TOOL_CATALOG as CORE_TOOLS
        from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
        return [t["name"] for t in CORE_TOOLS] + [t["name"] for t in WORKSPACE_TOOL_CATALOG]
    except Exception:
        return []


def _create_builtin_orchestrator(prompt_text: str) -> Dict[str, str]:
    """Create the built-in DevAgent record on first boot.

    Settings are migrated from the legacy KV store (devagent.* keys) when it
    has them; the bundle written afterwards matches the clean-install state.
    """
    from core.config import load_devagent_config as load_legacy_dev_cfg

    config = _first_boot_config(load_legacy_dev_cfg())
    ok = repo_create_orchestrator(
        orchestrator_id=uuid.uuid4().hex[:8],
        slug=DEVAGENT_SLUG,
        name="",
        description="Universal developer orchestrator - creates, debugs and maintains software projects.",
        prompt_text=prompt_text,
        config=config,
        tools=_builtin_tool_names(),
        max_steps=DEFAULT_MAX_STEPS,
        auto_apply=True,
        is_builtin=True,
        sort_order=200,  # after user orchestrators
        **DEVAGENT_NAME_FIELDS,
    )
    if ok:
        # Write the fresh bundle (orchestrator.json) with current defaults
        # so the folder on disk matches the clean-install state.
        _sync_orchestrator_folder(DEVAGENT_SLUG)
    result = {DEVAGENT_SLUG: "created" if ok else "error"}
    result.update(_ensure_default_orchestrators())
    return result


def _backfill_builtin_config(config: Dict[str, Any]) -> bool:
    """Backfill config fields added after the record was created.

    Returns True when at least one field changed. Existing user-chosen values
    are never overwritten: a field is filled only while it is missing, empty,
    or holds the sentinel that means "unset" for that particular key.
    """
    from core.builtin_config_defaults import builtin_config_defaults

    config_defaults = builtin_config_defaults()

    backfilled = False

    # Add the list-valued keys introduced after the first configs were written.
    for list_key in ("enabled_skills", "enabled_connections", "disabled_tools"):
        if list_key not in config:
            config[list_key] = []
            backfilled = True

    # An existing installation must not need a manual edit before the setting
    # can be chosen in the UI.
    if "verifier_agent" not in config:
        config["verifier_agent"] = ""
        backfilled = True

    # Legacy configs (created before cache-friendly economy mode) stored
    # economy_tail_messages=15 but have no cache fields. Treat that as the
    # old built-in default and upgrade it to the new default.
    has_cache_fields = ("economy_cache_enabled" in config) or ("economy_cache_multiplier" in config)
    if not has_cache_fields and config.get("economy_tail_messages") == 15:
        config["economy_tail_messages"] = _default_economy_tail_messages()
        backfilled = True

    for k, v in config_defaults.items():
        cur = config.get(k)
        if cur is None or cur == "":
            config[k] = v
            backfilled = True
        elif k == "strong_max_tokens":
            # A stored 0 means "use the model default", but the required
            # default for the built-in DevAgent is 384000 output tokens.
            # Treat 0 as unset here so buggy first-boot configs are fixed.
            try:
                if int(cur) <= 0:
                    config[k] = v
                    backfilled = True
            except (TypeError, ValueError):
                config[k] = v
                backfilled = True

    # Single-model migration: drop legacy weak_* keys from the stored config
    # so the canonical config carries one main model. Reads stay tolerant:
    # the compatibility views map the weak aliases onto the main model.
    for legacy_key in ("weak_service", "weak_model", "weak_temperature",
                       "weak_max_tokens", "weak_reasoning_effort"):
        if legacy_key in config:
            config.pop(legacy_key, None)
            backfilled = True

    return backfilled


def _migrate_builtin_max_steps(config: Dict[str, Any], existing: Dict[str, Any]) -> bool:
    """Migrate the legacy 100-step default; return True when it applied."""
    if config.get("max_steps_default_migrated") or existing.get("max_steps") != 100:
        return False
    config["max_steps_default_migrated"] = True
    return True


def ensure_builtin_orchestrators() -> Dict[str, str]:
    """Ensure the built-in 'dev_agent' orchestrator exists in the DB.

    On first run, migrates settings from KV-store (devagent.* keys) and
    system_prompt.md into the orchestrators table.  On subsequent runs,
    always updates the prompt_text from the bundled system_prompt.md file
    so it stays current with the code, and backfills missing config fields
    from the current defaults without overwriting user-chosen values.

    After the built-in DevAgent is ensured, bundled DEFAULT orchestrators
    (defaults/orchestrators/*) are created if their slugs do not exist yet.

    Returns a status dict, e.g. {"dev_agent": "created",
    "ya_agent": "created"} or {"dev_agent": "updated"}.
    """
    prompt_text = _load_bundled_prompt_text()
    existing = repo_get_orchestrator_with_text(DEVAGENT_SLUG)

    if existing is None:
        return _create_builtin_orchestrator(prompt_text)

    # Existing orchestrator - update prompt_text and backfill missing config fields.
    config = existing.get("config", {})
    if not isinstance(config, dict):
        config = {}

    backfilled = _backfill_builtin_config(config)
    migrate_max_steps = _migrate_builtin_max_steps(config, existing)
    if migrate_max_steps:
        backfilled = True

    # Localized names: a record created before translations existed carries the
    # shipped name in the user-defined slot. That string was never the owner's
    # choice, so it moves into a translation and the user-defined slot is
    # cleared - the interface then follows its language. A renamed DevAgent is
    # recognised as not shipped and keeps its name.
    from core.naming_backfill import builtin_name_backfill

    name_fields = builtin_name_backfill(existing, DEVAGENT_SLUG)

    update_kwargs: Dict[str, Any] = {}
    if backfilled:
        update_kwargs["config"] = config
    if migrate_max_steps:
        update_kwargs["max_steps"] = DEFAULT_MAX_STEPS
    if name_fields:
        update_kwargs.update(name_fields)
    # Honour a user-edited system prompt: while the prompt_user_edited marker is
    # set, keep the stored text instead of overwriting it with the bundled
    # dev_agent/system_prompt.md. The shipped prompt is re-applied (and the
    # marker cleared) through reset_builtin_prompt().
    if config.get(PROMPT_USER_EDITED_KEY) is True:
        repo_update_orchestrator(existing["id"], **update_kwargs)
    else:
        repo_update_orchestrator(existing["id"], prompt_text=prompt_text, **update_kwargs)
    if backfilled:
        _sync_orchestrator_folder(DEVAGENT_SLUG)
    result = {DEVAGENT_SLUG: "updated"}
    result.update(_ensure_default_orchestrators())
    return result


def load_devagent_config(orch_slug: str = DEVAGENT_SLUG) -> Dict[str, str]:
    """Read DevAgent-compatible config dict from the orchestrator store.

    Returns a flat dict with string values, compatible with the legacy
    ``core.config.load_devagent_config()`` contract.
    """
    orch = get_orchestrator(orch_slug)
    cfg = orch.get("config", {}) if orch else {}
    return {
        "service": cfg.get("strong_service", "") or "DeepSeek",
        "model": cfg.get("strong_model", "") or "deepseek-flash",
        "temperature": str(cfg.get("strong_temperature", 0.2) or 0.2),
        "prompt_text": orch.get("prompt_text", "") if orch else "",
        "strong_service": cfg.get("strong_service", "") or "DeepSeek",
        "strong_model": cfg.get("strong_model", "") or "deepseek-flash",
        "strong_temperature": str(cfg.get("strong_temperature", 0.4) or 0.4),
        "strong_max_tokens": str(cfg.get("strong_max_tokens", 0) or 0),
        # Single-model mode: the weak_* keys are legacy READ aliases of the
        # main model (tolerant read; never written back).
        "weak_service": cfg.get("strong_service", "") or "DeepSeek",
        "weak_model": cfg.get("strong_model", "") or "deepseek-flash",
        "weak_temperature": str(cfg.get("strong_temperature", 0.4) or 0.4),
        "weak_max_tokens": str(cfg.get("strong_max_tokens", 0) or 0),
        "vision_service": cfg.get("vision_service", "") or "",
        "vision_model": cfg.get("vision_model", "") or "",
        "image_service": cfg.get("image_service", "") or "",
        "image_model": cfg.get("image_model", "") or "",
        "search_service": cfg.get("search_service", "") or "DeepSeek",
        "search_model": cfg.get("search_model", "") or "deepseek-flash",
        "search_temperature": str(cfg.get("search_temperature", 0.4) or 0.4),
        "search_max_tool_calls": str(cfg.get("search_max_tool_calls", 3) or 3),
        "web_search_prompt": get_web_search_prompt(orch_slug),
        "economy_tail_messages": str(get_economy_tail_messages(orch_slug)),
        "economy_cache_enabled": str(get_economy_cache_enabled(orch_slug)),
        "economy_cache_multiplier": str(get_economy_cache_multiplier(orch_slug)),
        "enabled_skills": repr(get_enabled_skills(orch_slug)),
    }


def save_devagent_config(
    service: str = "", model: str = "", temperature: float = 0.2,
    prompt_text: str = "",
    strong_service: str = "", strong_model: str = "",
    strong_temperature: float = 0.2,
    strong_max_tokens: int = 0,
    weak_service: str = "", weak_model: str = "",
    weak_temperature: float = 0.5,
    weak_max_tokens: int = 0,
    search_service: str = "", search_model: str = "",
    search_temperature: float = 0.3,
    search_max_tool_calls: int = 3,
    vision_service: Optional[str] = None,
    vision_model: Optional[str] = None,
    image_service: Optional[str] = None,
    image_model: Optional[str] = None,
    web_search_prompt: Optional[str] = None,
    economy_tail_messages: Optional[int] = None,
    economy_cache_enabled: Optional[bool] = None,
    economy_cache_multiplier: Optional[int] = None,
) -> bool:
    """Save DevAgent configuration to the orchestrator store.

    Single-model mode: the weak_* parameters are tolerated for backward
    compatibility but are never persisted. vision_/image_* fields are
    stored only when passed (None keeps the current value).
    """
    orch = get_orchestrator(DEVAGENT_SLUG)
    if orch is None:
        return False
    config = orch.get("config", {})
    config["strong_service"] = strong_service or service
    config["strong_model"] = strong_model or model
    config["strong_temperature"] = strong_temperature or temperature
    config["strong_max_tokens"] = strong_max_tokens or get_default_strong_max_tokens()
    # Single-model mode: weak_* values are never persisted; the legacy
    # parameters stay in the signature for backward compatibility.
    for _legacy_key in ("weak_service", "weak_model", "weak_temperature",
                        "weak_max_tokens", "weak_reasoning_effort"):
        config.pop(_legacy_key, None)
    if vision_service is not None:
        config["vision_service"] = vision_service
    if vision_model is not None:
        config["vision_model"] = vision_model
    if image_service is not None:
        config["image_service"] = image_service
    if image_model is not None:
        config["image_model"] = image_model
    config["search_service"] = search_service or ""
    config["search_model"] = search_model or ""
    config["search_temperature"] = search_temperature
    config["search_max_tool_calls"] = search_max_tool_calls
    if web_search_prompt is not None:
        config["web_search_prompt"] = web_search_prompt
    if economy_tail_messages is not None:
        config["economy_tail_messages"] = economy_tail_messages
    if economy_cache_enabled is not None:
        config["economy_cache_enabled"] = economy_cache_enabled
    if economy_cache_multiplier is not None:
        config["economy_cache_multiplier"] = max(1, int(economy_cache_multiplier))

    # Record a user edit of the built-in prompt so the boot-time refresh in
    # ensure_builtin_orchestrators() keeps it instead of restoring the bundled
    # dev_agent/system_prompt.md.
    if prompt_text and prompt_text.strip():
        config[PROMPT_USER_EDITED_KEY] = True

    ok = repo_update_orchestrator(
        orch["id"],
        prompt_text=prompt_text,
        config=config,
    )
    if ok:
        _sync_orchestrator_folder(DEVAGENT_SLUG)
    return ok

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
