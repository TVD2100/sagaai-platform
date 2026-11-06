"""Argument validation and rights checks for the workspace tools.

This module is the pure part of the dispatcher: it decides whether a tool
call may run, but never performs it. Keeping it free of the dispatcher state
breaks the import cycle that used to force ``dev_agent.loop`` to reach these
names lazily through the facade with an ``ImportError`` guard - a guard that
silently switched validation off instead of failing loudly.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .tool_catalog_data import WORKSPACE_TOOL_ARGS
from .tools.args import _UNKNOWN_ARGS_ERROR


def _workspace_usage(tool_name: str, spec: Dict[str, set]) -> str:
    """Build a short usage/signature hint from a WORKSPACE_TOOL_ARGS spec."""
    parts = sorted(spec["required"])
    parts += [f"[{name}]" for name in sorted(spec["optional"])]
    if not parts:
        return f"Usage: {tool_name}() - this tool takes no arguments."
    return f"Usage: {tool_name}({', '.join(parts)})."


def _disabled_refusal(name: str, orchestrator_slug: str,
                      extra_disabled: Optional[set]) -> Optional[Dict[str, Any]]:
    """Return the refusal payload of a disabled tool, or None when it may run.

    ``disabled_tools`` comes from the orchestrator settings and may be
    extended by *extra_disabled* - the set a parent imposes on a child.
    """
    try:
        from core.orchestrator_tools import is_tool_disabled
        from core.orchestrators import get_disabled_tools
        disabled = set(get_disabled_tools(orchestrator_slug))
    except Exception:
        disabled = set()
    if extra_disabled:
        disabled |= set(extra_disabled)
    if is_tool_disabled(name, disabled):
        return {
            "ok": False,
            "error": (
                f"Tool '{name}' is disabled for orchestrator "
                f"'{orchestrator_slug}' via its 'disabled_tools' setting. "
                "Do not call it."
            ),
            "disabled": True,
        }
    return None


def _unknown_args_refusal(name: str, spec: Dict[str, set],
                          safe_args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the refusal payload for unexpected argument names, or None."""
    allowed = spec["required"] | spec["optional"]
    unknown = sorted(k for k in safe_args if k not in allowed)
    if not unknown:
        return None
    return {
        "ok": False,
        "error": _UNKNOWN_ARGS_ERROR.format(tool=name, unknown=", ".join(unknown)),
        "unknown_args": unknown,
        "suggestion": _workspace_usage(name, spec),
    }


def _rights_refusal(name: str, orchestrator_slug: str,
                    safe_args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the refusal payload of the delegation rights check, or None.

    Only ``spawn_agent`` and ``invoke_assistant`` carry rights; every other
    tool passes. The ephemeral form of ``invoke_assistant`` addresses no
    assistant, so it involves no assistant rights.
    """
    if name == "spawn_agent":
        try:
            from core.orchestration.tools import check_agent_allowed
            return check_agent_allowed(
                orchestrator_slug, str(safe_args.get("agent_slug") or ""))
        except Exception:
            return None
    if name == "invoke_assistant":
        aid = str(safe_args.get("assistant_id") or "").strip()
        if not aid:
            return None
        try:
            from core.orchestration.tools import check_assistant_allowed
            return check_assistant_allowed(orchestrator_slug, aid)
        except Exception:
            return None
    return None


def validate_tool_call(tool_name: str, args: Dict[str, Any],
                       orchestrator_slug: str = "dev_agent",
                       extra_disabled: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """Return a refusal payload for a tool call, or None when it may run.

    Three checks in a fixed order (cheap local ones first, rights last):

    1. ``disabled_tools`` of the orchestrator, plus *extra_disabled* - the set
       a parent imposes on a child;
    2. argument names against ``WORKSPACE_TOOL_ARGS``;
    3. rights on the executor: ``allowed_agents`` for ``spawn_agent`` and
       ``allowed_assistants`` for ``invoke_assistant``, including
       self-delegation and the ``self_only`` hint.

    None of the three reads a module-level global, which is what makes the
    function safe to call from the agent-loop branch that runs OUTSIDE
    ``thread_context``. ``workspace_guard_error`` is deliberately not part of
    it: that check reads ``config.WORKSPACE_SELECTED``, which is only reliable
    inside ``thread_context``. Orchestration tools do not touch project files,
    so they never need it - the two concerns are separated by nature, not
    collected into one function.

    The rights check is the last one because its outcome depends on an
    argument (``agent_slug``/``assistant_id``) that is already validated by
    name and type at that point.
    """
    name = (tool_name or "").strip()
    if not name:
        return {"ok": False, "error": "Tool name is empty.",
                "error_code": "invalid_args"}
    safe_args = args if isinstance(args, dict) else {}

    refusal = _disabled_refusal(name, orchestrator_slug, extra_disabled)
    if refusal is not None:
        return refusal

    spec = WORKSPACE_TOOL_ARGS.get(name)
    if spec is not None:
        refusal = _unknown_args_refusal(name, spec, safe_args)
        if refusal is not None:
            return refusal

    return _rights_refusal(name, orchestrator_slug, safe_args)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
