# -*- coding: utf-8 -*-
"""
core.orchestrator_defaults - defaults and static configuration of an orchestrator.

Part of ``core.orchestrators``, which is the public surface of this
package. A name the tests may replace through the facade is resolved late,
at call time, so a patch installed on ``core.orchestrators`` reaches the
implementation here.
"""
import json
from typing import Any, Dict, Optional

from core.config import (
    get_default_economy_cache_enabled,
    get_default_economy_cache_multiplier,
    get_default_economy_tail_messages,
)

DEVAGENT_SLUG = "dev_agent"


UNIVERSAL_AGENT_SLUG = "universal_agent"


UNIVERSAL_AGENT_CONFIG_KEY = "universal_agent_enabled"


DEFAULT_WEB_SEARCH_PROMPT = (
    "You are a helpful research assistant. Use web search to find "
    "accurate, up-to-date information. Answer the user's query "
    "concisely based on the search results. Cite sources when possible."
)


PROMPT_USER_EDITED_KEY = "prompt_user_edited"


def _devagent_default_config() -> Dict[str, Any]:
    """Build the DevAgent orchestrator config from the canonical bundle.

    Reads ``core.config.get_devagent_defaults()`` (bundle config section with
    built-in fallback) and normalizes types to the shape expected by the
    orchestrator storage. This is the single source of truth for first-boot
    creation, backfill and the build_assistant_dicts fallback.
    """
    from core.config import get_devagent_defaults as _daf_defaults
    defaults = _daf_defaults()

    def _str1(key, fallback):
        val = defaults.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
        return fallback

    def _num(key, fallback, cast):
        try:
            return cast(defaults.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _truthy(key, fallback=False):
        """Normalize a boolean flag stored as bool, string or number."""
        val = defaults.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return fallback

    def _lst(key):
        val = defaults.get(key)
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    return {
        "strong_service": _str1("strong_service", "DeepSeek"),
        "strong_model": _str1("strong_model", "deepseek-flash"),
        "strong_temperature": _num("strong_temperature", 0.4, float),
        "strong_max_tokens": _num("strong_max_tokens", 384000, int),
        "strong_reasoning_effort": _str1("strong_reasoning_effort", "xhigh"),
        # Optional multimodal models (single-model mode): "" = not assigned.
        "vision_service": _str1("vision_service", "DeepSeek"),
        "vision_model": _str1("vision_model", "deepseek-flash"),
        "image_service": _str1("image_service", ""),
        "image_model": _str1("image_model", ""),
        "search_service": _str1("search_service", "DeepSeek"),
        "search_model": _str1("search_model", "deepseek-flash"),
        "search_temperature": _num("search_temperature", 0.4, float),
        "search_max_tool_calls": _num("search_max_tool_calls", 3, int),
        "search_reasoning_effort": _str1("search_reasoning_effort", "high"),
        "web_search_prompt": _str1("web_search_prompt", DEFAULT_WEB_SEARCH_PROMPT),
        # Optional url-fetch model: "" = not assigned (tool absent).
        "fetch_service": _str1("fetch_service", ""),
        "fetch_model": _str1("fetch_model", ""),
        # Selenium rendering of pages: an alternative to the provider fetch
        # API. Either the model or this flag keeps url_fetch available.
        "fetch_selenium": _truthy("fetch_selenium", False),
        "economy_tail_messages": get_default_economy_tail_messages(),
        "economy_cache_enabled": get_default_economy_cache_enabled(),
        "economy_cache_multiplier": get_default_economy_cache_multiplier(),
        "enabled_skills": _lst("enabled_skills"),
        "enabled_connections": _lst("enabled_connections"),
        "disabled_tools": _lst("disabled_tools"),
        # Agent that checks delegated results: "" = not appointed, and the
        # ``verify_result`` tool is then not offered at all.
        "verifier_agent": _str1("verifier_agent", ""),
    }


_DEVAGENT_DEFAULT_CONFIG: Dict[str, Any] = _devagent_default_config()


def _default_economy_tail_messages() -> int:
    """Return the default economy tail length from the canonical DevAgent
    orchestrator bundle or the built-in fallback (50). This is the single
    source of truth for the default value."""
    return get_default_economy_tail_messages()


def _default_economy_cache_enabled() -> bool:
    """Return the default cache-friendly economy mode flag."""
    return get_default_economy_cache_enabled()


def _default_economy_cache_multiplier() -> int:
    """Return the default cache-window multiplier (xN)."""
    return get_default_economy_cache_multiplier()


_KNOWN_TOOL_NAMES: Optional[set] = None


def _get_known_tool_names() -> set:
    """Return the set of all allowed tool names across core + workspace catalogs.

    Cached on first call; use ``_invalidate_known_tool_names()`` if the
    tool catalog changes at runtime (rare).
    """
    global _KNOWN_TOOL_NAMES
    if _KNOWN_TOOL_NAMES is not None:
        return _KNOWN_TOOL_NAMES
    try:
        from dev_agent.tool_executor import TOOL_CATALOG as CORE_TOOLS
        from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
        _KNOWN_TOOL_NAMES = (
        {t["name"] for t in CORE_TOOLS}
        | {t["name"] for t in WORKSPACE_TOOL_CATALOG}
        | {"list_skills", "get_skill_by_id", "update_skill_by_id",
           "create_skill_for_task", "detect_and_select_skill",
           "list_skills_library", "get_skill_folder", "get_skill_prompt", "get_skill_file"}
    )
    except Exception:
        # During early bootstrap or tests, the catalogs may not be importable.
        # Fall back to a minimal known set that covers the most common tools.
        _KNOWN_TOOL_NAMES = {
            "read_file", "list_files", "propose_file", "verify_file",
            "create_backup", "restore_backup", "show_history", "run_test",
            "run_code", "list_skills", "get_skill_by_id", "update_skill_by_id",
            "list_instructions", "get_instruction", "detect_and_select_skill",
            "create_skill_for_task",
            "list_skills_library", "get_skill_folder", "get_skill_prompt", "get_skill_file",
            "web_search", "get_history_index",
            "get_history_messages",
            "set_workspace", "set_target_file", "current_workspace",
            "scan_folder", "assess_workspace", "build_project_map",
            "write_project_map", "write_doc", "read_doc",
            "snapshot_all", "list_snapshots", "restore_all",
        }
    return _KNOWN_TOOL_NAMES


def _invalidate_known_tool_names() -> None:
    """Clear the cached known-tool-names set so it is rebuilt on next use."""
    global _KNOWN_TOOL_NAMES
    _KNOWN_TOOL_NAMES = None


_MAX_PROMPT_TEXT_LENGTH = 100_000

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
