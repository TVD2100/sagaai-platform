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

from typing import Any, Dict, List, Optional, Set


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


def _connection_catalog(orchestrator_slug: str) -> List[Dict[str, str]]:
    """Return GitHub REST tools, only when a github_rest connection is enabled."""
    try:
        from core.orchestrators import get_enabled_connections
        from core.connectors import get_connection
        services: Set[str] = set()
        for conn_id in get_enabled_connections(orchestrator_slug):
            conn = get_connection(conn_id)
            if isinstance(conn, dict):
                services.add(str(conn.get("service") or ""))
        if "github_rest" not in services:
            return []
        from core.github_tools_rest import get_tools as get_github_rest_tools
        return [dict(t) for t in get_github_rest_tools()]
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
    if include_connections:
        for t in _connection_catalog(orchestrator_slug):
            _add(t)
    for t in _custom_function_catalog(orchestrator_slug):
        _add(t)

    if disabled:
        order = [n for n in order if not is_tool_disabled(n, disabled)]
    return [entries[n] for n in order]


def list_system_tools(orchestrator_slug: str = "dev_agent") -> List[Dict[str, str]]:
    """Return system tools selectable in the settings UI.

    System tools = core + workspace/orchestrator-layer + connection tools.
    Custom orchestrator functions are excluded (they are managed in their own
    list and deleted explicitly).
    """
    entries: Dict[str, Dict[str, str]] = {}
    order: List[str] = []
    for t in _core_catalog() + _workspace_catalog() + _connection_catalog(orchestrator_slug):
        name = (t.get("name") or "").strip()
        if not name or name in entries:
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
# SPDX-License-Identifier: MIT
