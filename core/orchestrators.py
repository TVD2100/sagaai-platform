# -*- coding: utf-8 -*-
"""
core.orchestrators - public API for orchestrator management.

Orchestrators are self-contained autonomous agents (like DevAgent).
Each orchestrator has its own system prompt, model configuration
(main/search + optional vision/image), tool set, economy settings and
instructions. They appear as separate pages in the sidebar navigation and
can be exported/imported as JSON.

Since the Orchestrators feature, every orchestrator also has a personal
folder under DATA_DIR/orchestrators/<slug>/ that stores the export bundle,
the system prompt, its instructions and its custom functions. The folder is
the source of truth; a DB cache holds the instructions for the runtime hot
path and is rebuilt at startup and after every write.

The implementation is split by theme across the sibling modules:

* ``orchestrator_defaults`` -- defaults and static configuration,
* ``orchestrator_store``    -- reading, writing and folder synchronisation,
* ``orchestrator_links``    -- skills, connections, RAG bases and delegation,
* ``orchestrator_prompt``   -- assembly of the system prompt,
* ``orchestrator_models``   -- model, web-search, fetch and economy settings,
* ``orchestrator_transfer`` -- export and import of a bundle,
* ``orchestrator_instr``    -- instructions and custom functions,
* ``orchestrator_builtin``  -- built-in orchestrators and the DevAgent bridge.

This module stays the single public surface. A name the tests replace
through the module object is a thin wrapper over ``<name>_impl`` in its
satellite, and the satellites resolve such names late through this module,
so a patch installed here reaches the implementation wherever it lives.
"""
import sys as _sys

from core.orchestrator_builtin import (
    _backfill_builtin_config,
    _builtin_tool_names,
    _create_builtin_orchestrator,
    _ensure_default_orchestrators,
    _first_boot_config,
    _load_bundled_prompt_text,
    _migrate_builtin_max_steps,
    _refresh_marketplace_registry_cache,
    ensure_builtin_orchestrators,
    load_devagent_config,
    save_devagent_config,
)
from core.orchestrator_defaults import (
    _DEVAGENT_DEFAULT_CONFIG,
    _MAX_PROMPT_TEXT_LENGTH,
    DEFAULT_WEB_SEARCH_PROMPT,
    DEVAGENT_SLUG,
    PROMPT_USER_EDITED_KEY,
    UNIVERSAL_AGENT_CONFIG_KEY,
    UNIVERSAL_AGENT_SLUG,
    _default_economy_cache_enabled,
    _default_economy_cache_multiplier,
    _default_economy_tail_messages,
    _devagent_default_config,
    _get_known_tool_names,
    _invalidate_known_tool_names,
)
from core.orchestrator_folders import (
    get_orchestrator_function,
    get_orchestrator_instruction,
)
from core.orchestrator_instr import (
    orch_delete_function,
    orch_delete_instruction,
    orch_get_function,
    orch_get_instruction_impl,
    orch_list_functions,
    orch_list_instructions,
    orch_save_function,
    orch_save_instruction,
)
from core.orchestrator_links import (
    DELEGATION_LIMIT_KEYS,
    get_allowed_agents_impl,
    get_allowed_assistants_impl,
    get_delegation_limits,
    get_disabled_tools_impl,
    get_effective_agents_impl,
    get_enabled_connections_impl,
    get_enabled_skills,
    get_orchestrator_rag_bases,
    get_universal_agent_enabled,
    get_verifier_agent_impl,
    set_allowed_agents,
    set_allowed_assistants,
    set_delegation_limits,
    set_disabled_tools,
    set_enabled_connections,
    set_enabled_skills,
    set_orchestrator_rag_bases,
    set_universal_agent_enabled,
    set_verifier_agent,
)
from core.orchestrator_models import (
    get_economy_cache_enabled,
    get_economy_cache_multiplier,
    get_economy_config,
    get_economy_tail_messages,
    get_fetch_config,
    get_web_search_config,
    get_web_search_prompt_impl,
)
from core.orchestrator_prompt import (
    _DIALOG_RULES_CHILD,
    _DIALOG_RULES_PARENT,
    _MONTH_NAMES,
    _WEEKDAY_NAMES,
    _WEEKDAY_NAMES_RU,
    _agent_delegation_lines,
    _assistant_delegation_lines,
    _current_date_line,
    _delegation_availability,
    _extend_prompt_with_connections_impl,
    _extend_prompt_with_current_date,
    _extend_prompt_with_delegation_impl,
    _extend_prompt_with_dialog,
    _extend_prompt_with_instructions_impl,
    _extend_prompt_with_memory_impl,
    _extend_prompt_with_personal_card_impl,
    _extend_prompt_with_rag_bases_impl,
    _extend_prompt_with_skills_impl,
    _extend_prompt_with_tools_impl,
    _extend_prompt_with_working_memory_impl,
    _format_utc_offset,
    _verify_delegation_lines,
    build_assistant_dicts_impl,
)
from core.orchestrator_shared import bind_facade as _bind_facade
from core.orchestrator_store import (
    _mark_marketplace_removed,
    _mark_prompt_edited,
    _sync_orchestrator_folder_impl,
    create_orchestrator,
    delete_orchestrator_impl,
    get_orchestrator_by_slug,
    get_orchestrator_impl,
    list_orchestrators_impl,
    reload_orchestrator_from_folder,
    reset_builtin_prompt_impl,
    save_orchestrator,
    sync_all_orchestrator_folders,
)
from core.orchestrator_transfer import (
    _EXPORT_FORMAT,
    _import_functions,
    _import_instructions,
    _validate_imported_tools,
    export_orchestrator,
    import_orchestrator,
)
from core.services import get_services as _get_services
from storage.repository import (
    repo_create_orchestrator,
    repo_get_orchestrator_with_text,
    repo_update_orchestrator,
)

# Names the API exposes; every import above is a deliberate re-export.
__all__ = [
    "DEFAULT_WEB_SEARCH_PROMPT",
    "DELEGATION_LIMIT_KEYS",
    "DEVAGENT_SLUG",
    "PROMPT_USER_EDITED_KEY",
    "UNIVERSAL_AGENT_CONFIG_KEY",
    "UNIVERSAL_AGENT_SLUG",
    "_DEVAGENT_DEFAULT_CONFIG",
    "_DIALOG_RULES_CHILD",
    "_DIALOG_RULES_PARENT",
    "_EXPORT_FORMAT",
    "_MAX_PROMPT_TEXT_LENGTH",
    "_MONTH_NAMES",
    "_WEEKDAY_NAMES",
    "_WEEKDAY_NAMES_RU",
    "_agent_delegation_lines",
    "_assistant_delegation_lines",
    "_backfill_builtin_config",
    "_builtin_tool_names",
    "_create_builtin_orchestrator",
    "_current_date_line",
    "_default_economy_cache_enabled",
    "_default_economy_cache_multiplier",
    "_default_economy_tail_messages",
    "_delegation_availability",
    "_devagent_default_config",
    "_ensure_default_orchestrators",
    "_extend_prompt_with_connections",
    "_extend_prompt_with_current_date",
    "_extend_prompt_with_delegation",
    "_extend_prompt_with_dialog",
    "_extend_prompt_with_instructions",
    "_extend_prompt_with_memory",
    "_extend_prompt_with_personal_card",
    "_extend_prompt_with_rag_bases",
    "_extend_prompt_with_skills",
    "_extend_prompt_with_tools",
    "_extend_prompt_with_working_memory",
    "_first_boot_config",
    "_format_utc_offset",
    "_get_known_tool_names",
    "_get_services",
    "_import_functions",
    "_import_instructions",
    "_invalidate_known_tool_names",
    "_load_bundled_prompt_text",
    "_mark_marketplace_removed",
    "_mark_prompt_edited",
    "_migrate_builtin_max_steps",
    "_refresh_marketplace_registry_cache",
    "_sync_orchestrator_folder",
    "_validate_imported_tools",
    "_verify_delegation_lines",
    "build_assistant_dicts",
    "build_skill_dicts",
    "create_orchestrator",
    "delete_orchestrator",
    "ensure_builtin_orchestrators",
    "export_orchestrator",
    "get_allowed_agents",
    "get_allowed_assistants",
    "get_delegation_limits",
    "get_disabled_tools",
    "get_economy_cache_enabled",
    "get_economy_cache_multiplier",
    "get_economy_config",
    "get_economy_tail_messages",
    "get_effective_agents",
    "get_enabled_connections",
    "get_enabled_skills",
    "get_fetch_config",
    "get_orchestrator",
    "get_orchestrator_by_slug",
    "get_orchestrator_function",
    "get_orchestrator_instruction",
    "get_orchestrator_rag_bases",
    "get_universal_agent_enabled",
    "get_verifier_agent",
    "get_web_search_config",
    "get_web_search_prompt",
    "import_orchestrator",
    "list_orchestrators",
    "load_devagent_config",
    "orch_delete_function",
    "orch_delete_instruction",
    "orch_get_function",
    "orch_get_instruction",
    "orch_list_functions",
    "orch_list_instructions",
    "orch_save_function",
    "orch_save_instruction",
    "reload_orchestrator_from_folder",
    "repo_create_orchestrator",
    "repo_get_orchestrator_with_text",
    "repo_update_orchestrator",
    "reset_builtin_prompt",
    "save_devagent_config",
    "save_orchestrator",
    "set_allowed_agents",
    "set_allowed_assistants",
    "set_delegation_limits",
    "set_disabled_tools",
    "set_enabled_connections",
    "set_enabled_skills",
    "set_orchestrator_rag_bases",
    "set_universal_agent_enabled",
    "set_verifier_agent",
    "sync_all_orchestrator_folders",
]


def _extend_prompt_with_connections(*args, **kwargs):
    """Call ``_extend_prompt_with_connections_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_connections_impl(*args, **kwargs)


def _extend_prompt_with_delegation(*args, **kwargs):
    """Call ``_extend_prompt_with_delegation_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_delegation_impl(*args, **kwargs)


def _extend_prompt_with_instructions(*args, **kwargs):
    """Call ``_extend_prompt_with_instructions_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_instructions_impl(*args, **kwargs)


def _extend_prompt_with_memory(*args, **kwargs):
    """Call ``_extend_prompt_with_memory_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_memory_impl(*args, **kwargs)


def _extend_prompt_with_personal_card(*args, **kwargs):
    """Call ``_extend_prompt_with_personal_card_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_personal_card_impl(*args, **kwargs)


def _extend_prompt_with_rag_bases(*args, **kwargs):
    """Call ``_extend_prompt_with_rag_bases_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_rag_bases_impl(*args, **kwargs)


def _extend_prompt_with_skills(*args, **kwargs):
    """Call ``_extend_prompt_with_skills_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_skills_impl(*args, **kwargs)


def _extend_prompt_with_tools(*args, **kwargs):
    """Call ``_extend_prompt_with_tools_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_tools_impl(*args, **kwargs)


def _extend_prompt_with_working_memory(*args, **kwargs):
    """Call ``_extend_prompt_with_working_memory_impl`` with the arguments the caller passed."""
    return _extend_prompt_with_working_memory_impl(*args, **kwargs)


def _sync_orchestrator_folder(*args, **kwargs):
    """Call ``_sync_orchestrator_folder_impl`` with the arguments the caller passed."""
    return _sync_orchestrator_folder_impl(*args, **kwargs)


def build_assistant_dicts(*args, **kwargs):
    """Call ``build_assistant_dicts_impl`` with the arguments the caller passed."""
    return build_assistant_dicts_impl(*args, **kwargs)


def delete_orchestrator(*args, **kwargs):
    """Call ``delete_orchestrator_impl`` with the arguments the caller passed."""
    return delete_orchestrator_impl(*args, **kwargs)


def get_allowed_agents(*args, **kwargs):
    """Call ``get_allowed_agents_impl`` with the arguments the caller passed."""
    return get_allowed_agents_impl(*args, **kwargs)


def get_allowed_assistants(*args, **kwargs):
    """Call ``get_allowed_assistants_impl`` with the arguments the caller passed."""
    return get_allowed_assistants_impl(*args, **kwargs)


def get_disabled_tools(*args, **kwargs):
    """Call ``get_disabled_tools_impl`` with the arguments the caller passed."""
    return get_disabled_tools_impl(*args, **kwargs)


def get_effective_agents(*args, **kwargs):
    """Call ``get_effective_agents_impl`` with the arguments the caller passed."""
    return get_effective_agents_impl(*args, **kwargs)


def get_enabled_connections(*args, **kwargs):
    """Call ``get_enabled_connections_impl`` with the arguments the caller passed."""
    return get_enabled_connections_impl(*args, **kwargs)


def get_orchestrator(*args, **kwargs):
    """Call ``get_orchestrator_impl`` with the arguments the caller passed."""
    return get_orchestrator_impl(*args, **kwargs)


def get_verifier_agent(*args, **kwargs):
    """Call ``get_verifier_agent_impl`` with the arguments the caller passed."""
    return get_verifier_agent_impl(*args, **kwargs)


def get_web_search_prompt(*args, **kwargs):
    """Call ``get_web_search_prompt_impl`` with the arguments the caller passed."""
    return get_web_search_prompt_impl(*args, **kwargs)


def list_orchestrators(*args, **kwargs):
    """Call ``list_orchestrators_impl`` with the arguments the caller passed."""
    return list_orchestrators_impl(*args, **kwargs)


def orch_get_instruction(*args, **kwargs):
    """Call ``orch_get_instruction_impl`` with the arguments the caller passed."""
    return orch_get_instruction_impl(*args, **kwargs)


def reset_builtin_prompt(*args, **kwargs):
    """Call ``reset_builtin_prompt_impl`` with the arguments the caller passed."""
    return reset_builtin_prompt_impl(*args, **kwargs)


# Legacy alias (old "skill" terminology): new code uses build_assistant_dicts.
build_skill_dicts = build_assistant_dicts


# The satellites resolve their late references through this object: bind it
# last, when every wrapper above is defined.
_bind_facade(_sys.modules[__name__])
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
