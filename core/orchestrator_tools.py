# -*- coding: utf-8 -*-
"""
core.orchestrator_tools - tool catalog and prompt block for orchestrators.

This module is the single source of truth for the list of tools an
orchestrator may call: the protected core tool catalog, the
workspace/orchestrator-layer catalog, enabled connection tools (GitHub REST)
and the orchestrator's custom Python functions. It applies the
``disabled_tools`` config (blacklist; empty = everything enabled), renders the
English ``## Available tools`` block appended to the orchestrator system
prompt, and provides the data used by the "System functions" checkboxes on
the orchestrator settings page.

The same name-resolution helpers are reused by
``dev_agent.universal_agent`` dispatch to actually block disabled tools.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("core.orchestrator_tools")


# ─── Name resolution (legacy aliases + disabled-tool checks) ────────────────


def _legacy_aliases() -> Dict[str, str]:
    """Return the core dispatcher's legacy alias table (best effort)."""
    try:
        from dev_agent.tool_executor import _LEGACY_TOOL_ALIASES  # noqa: F401
        return dict(_LEGACY_TOOL_ALIASES)
    except Exception:
        return {}


def resolve_tool_name(tool_name: str) -> str:
    """Return the canonical tool name, resolving legacy aliases.

    e.g. ``list_skills`` -> ``list_assistants``. Unknown names pass through
    unchanged.
    """
    name = (tool_name or "").strip()
    return _legacy_aliases().get(name, name)


def normalize_disabled(names: Optional[list]) -> List[str]:
    """Normalise a disabled-tools list to canonical names.

    Legacy aliases are resolved, non-strings and empty entries are dropped and
    duplicates are removed (order preserved).
    """
    result: List[str] = []
    for raw in (names or []):
        if not isinstance(raw, str):
            continue
        val = raw.strip()
        if not val:
            continue
        canonical = resolve_tool_name(val)
        if canonical not in result:
            result.append(canonical)
    return result


def is_tool_disabled(tool_name: str, disabled: Optional[Set[str]]) -> bool:
    """Return True when a tool is disabled by its own name or an alias.

    The check covers both directions: disabling ``list_skills`` blocks
    ``list_assistants`` and disabling ``list_assistants`` blocks
    ``list_skills``.
    """
    if not disabled:
        return False
    name = (tool_name or "").strip()
    if not name:
        return False
    if name in disabled:
        return True
    canon = resolve_tool_name(name)
    if canon in disabled:
        return True
    # A raw alias stored in the disabled list (e.g. 'list_skills') also
    # blocks its canonical counterpart ('list_assistants').
    return any(resolve_tool_name(dn) == canon for dn in disabled)


# ─── Capability-gated tools ─────────────────────────────────────────────────

#: Tools that exist only while the orchestrator has the matching capability
#: switched on. ``url_fetch`` downloads a web page or a PDF through a
#: provider's Search/Fetch API, or renders the page with Selenium, so it
#: appears in the catalog (and in the prompt block and the system-functions
#: list) only after a url-fetch model is assigned or Selenium is enabled.
_CONDITIONAL_TOOLS = {"url_fetch": "fetch"}

#: Delegation tools and their availability rule. ``spawn_agent`` is always
#: registered: self-delegation needs no configuration, and hiding it behind
#: an allowlist would require the user to add an agent to its OWN list to
#: switch on the standard scenario. ``invoke_assistant`` is registered only
#: when ``allowed_assistants`` is non-empty - there are no assistants by
#: default, unlike the right to call yourself.
_DELEGATION_TOOLS = ("spawn_agent", "invoke_assistant", "subagent_status",
                     "read_subagent_state", "resume_subagent", "sleep")


def _delegation_tool_enabled(tool_name: str,
                             orchestrator_slug: str = "dev_agent") -> bool:
    """Return True when a delegation tool may be offered to this orchestrator.

    ``spawn_agent`` - always. ``invoke_assistant`` - only with a non-empty
    ``allowed_assistants``. A config read failure hides ``invoke_assistant``
    (nobody is allowed by default) and keeps ``spawn_agent`` (self-
    delegation is structural).
    """
    name = (tool_name or "").strip()
    if name == "spawn_agent":
        return True
    if name == "sleep":
        # Waiting for a child is part of the delegation surface: it launches
        # nothing and cancels nothing, so it needs no configuration.
        return True
    if name == "invoke_assistant":
        try:
            from core.orchestrators import get_allowed_assistants
            return bool(get_allowed_assistants(orchestrator_slug))
        except Exception:
            return False
    return True


def is_conditional_tool_enabled(tool_name: str,
                                orchestrator_slug: str = "dev_agent") -> bool:
    """Return True when a capability-gated tool may be offered.

    Tools outside :data:`_CONDITIONAL_TOOLS` are always enabled. For
    ``url_fetch`` the answer follows the orchestrator's url-fetch
    configuration: the tool is offered when a fetch model is assigned OR
    the Selenium option is enabled, because either switch can download a
    page. A configuration read failure hides the tool instead of exposing
    an unusable entry.
    """
    name = (tool_name or "").strip()
    if name in _DELEGATION_TOOLS:
        return _delegation_tool_enabled(name, orchestrator_slug)
    capability = _CONDITIONAL_TOOLS.get(name)
    if capability is None:
        return True
    if capability == "fetch":
        try:
            from core.orchestrators import get_fetch_config
            return bool(get_fetch_config(orchestrator_slug).get("configured"))
        except Exception:
            return False
    return True


# ─── Catalog sources ────────────────────────────────────────────────────────


def _core_catalog() -> List[Dict[str, str]]:
    """Return the protected core tool catalog (copied, never mutated)."""
    try:
        from dev_agent.tool_executor import TOOL_CATALOG
        return [dict(t) for t in TOOL_CATALOG]
    except Exception:
        return []


def _workspace_catalog() -> List[Dict[str, str]]:
    """Return the workspace/orchestrator-layer catalog (copied)."""
    try:
        from dev_agent.universal_agent import WORKSPACE_TOOL_CATALOG
        return [dict(t) for t in WORKSPACE_TOOL_CATALOG]
    except Exception:
        return []


def _delegation_catalog() -> List[Dict[str, str]]:
    """Return the delegation tools (``spawn_agent``, ``invoke_assistant``).

    Descriptions come from ``core.orchestration.tools`` so the boundary text
    lives next to the implementation: the one-shot assistant call must state
    that tools and knowledge bases are not applied, and ``spawn_agent`` must
    state that the child gets a clean history. Availability is decided later
    by :func:`is_conditional_tool_enabled`; this function only supplies the
    entries.
    """
    try:
        from core.orchestration.tools import (
            spawn_tool_entry, invoke_tool_entry, subagent_status_tool_entry,
            read_subagent_state_tool_entry, resume_subagent_tool_entry,
            sleep_tool_entry,
        )
        return [spawn_tool_entry(), subagent_status_tool_entry(),
                read_subagent_state_tool_entry(), resume_subagent_tool_entry(),
                sleep_tool_entry(), invoke_tool_entry()]
    except Exception:
        return []


def _connection_catalog(orchestrator_slug: str) -> List[Dict[str, str]]:
    """Return connection tools for the services the orchestrator enabled.

    Only tools of enabled services are returned: GitHub REST tools (ghr_*)
    for github_rest connections, SSH tools (ssh_*) for ssh connections and
    MCP tools (static mcp_list_tools / mcp_call_tool plus the dynamic
    ``mcp__<conn_id>__<tool_name>`` tools) for mcp connections. A broken or
    unavailable MCP server contributes no dynamic tools (a warning is
    logged); missing or unknown services yield no entries at all.
    """
    try:
        from core.orchestrators import get_enabled_connections
        from core.connectors import get_connection
        services: Set[str] = set()
        for conn_id in get_enabled_connections(orchestrator_slug):
            conn = get_connection(conn_id)
            if isinstance(conn, dict):
                services.add(str(conn.get("service") or ""))
        out: List[Dict[str, str]] = []
        if "github_rest" in services:
            from core.github_tools_rest import get_tools as get_github_rest_tools
            out.extend(dict(t) for t in get_github_rest_tools())
        if "ssh" in services:
            from core.ssh_tools import get_tools as get_ssh_tools
            out.extend(dict(t) for t in get_ssh_tools())
        if "mcp" in services:
            try:
                from core.mcp_tools import collect_catalog_entries
                out.extend(collect_catalog_entries(orchestrator_slug))
            except Exception as e:
                log.warning(
                    "MCP catalog skipped for orchestrator %s: %s",
                    orchestrator_slug, e,
                )
        return out
    except Exception:
        return []


def _custom_function_catalog(orchestrator_slug: str) -> List[Dict[str, str]]:
    """Return custom Python functions of the orchestrator as catalog entries."""
    try:
        from core.orchestrator_folders import list_orchestrator_functions
        out: List[Dict[str, str]] = []
        for meta in list_orchestrator_functions(orchestrator_slug):
            out.append({
                "name": meta["name"],
                "desc": (
                    f"Custom function of orchestrator '{orchestrator_slug}'. "
                    "Accepts arbitrary keyword arguments; returns "
                    '{"ok": bool, ...}.'
                ),
            })
        return out
    except Exception:
        return []


# ─── Catalog assembly ───────────────────────────────────────────────────────


def build_tool_catalog(
        orchestrator_slug: str = "dev_agent",
        include_connections: bool = True,
        disabled: Optional[Set[str]] = None,
) -> List[Dict[str, str]]:
    """Return the ordered tool catalog of an orchestrator.

    Order: core tools, workspace/orchestrator-layer tools, connection tools,
    custom functions. A custom function whose name matches a system tool
    replaces that entry (dispatch shadows the system tool with the custom
    callable). Disabled tools are removed.
    """
    entries: Dict[str, Dict[str, str]] = {}
    order: List[str] = []

    def _add(entry: Dict[str, str]) -> None:
        name = (entry.get("name") or "").strip()
        if not name:
            return
        if name not in entries:
            order.append(name)
        entries[name] = dict(entry)

    for t in _core_catalog():
        _add(t)
    for t in _workspace_catalog():
        _add(t)
    for t in _delegation_catalog():
        _add(t)
    if include_connections:
        for t in _connection_catalog(orchestrator_slug):
            _add(t)
    for t in _custom_function_catalog(orchestrator_slug):
        _add(t)

    if disabled:
        order = [n for n in order if not is_tool_disabled(n, disabled)]
    order = [n for n in order
             if is_conditional_tool_enabled(n, orchestrator_slug)]
    return [entries[n] for n in order]


def list_system_tools(orchestrator_slug: str = "dev_agent") -> List[Dict[str, str]]:
    """Return system tools selectable in the settings UI.

    System tools = core + workspace/orchestrator-layer + connection tools.
    Custom orchestrator functions are excluded (they are managed in their own
    list and deleted explicitly).
    """
    entries: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    for t in (_core_catalog() + _workspace_catalog() + _delegation_catalog()
              + _connection_catalog(orchestrator_slug)):
        name = (t.get("name") or "").strip()
        if not name or name in entries:
            continue
        if not is_conditional_tool_enabled(name, orchestrator_slug):
            continue
        order.append(name)
        entries[name] = dict(t)
    return [entries[n] for n in order]


# ─── Prompt block rendering ─────────────────────────────────────────────────


def render_available_tools_block(
        orchestrator_slug: str = "dev_agent",
        disabled: Optional[Set[str]] = None,
) -> str:
    """Render the English '## Available tools' block for a system prompt.

    Lists every tool the orchestrator may call (disabled tools excluded) with
    the short invocation rules. Returns '' when the catalog is empty or
    unreadable (best effort - the orchestrator still works without the block).
    """
    entries = build_tool_catalog(orchestrator_slug, disabled=disabled)
    if not entries:
        return ""
    custom_names = {t["name"] for t in _custom_function_catalog(orchestrator_slug)}
    lines = [
        "## Available tools",
        "",
        "The following tools are available to you (system tools and custom",
        "functions of this orchestrator). Emit every call as a fenced JSON",
        'block: one short comment line, then {"tool": "<name>", "args": {...}}.',
        "Use only the documented argument names - never invent parameters,",
        "and never call a tool that is not listed here.",
        "",
    ]
    for t in entries:
        name = t["name"]
        suffix = " (custom function)" if name in custom_names else ""
        desc = str(t.get("desc") or "").strip()
        lines.append(f"- `{name}`{suffix} - {desc}")
    return "\n".join(lines)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
