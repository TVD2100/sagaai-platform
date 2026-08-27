"""
dev_agent.llm_utils - unified LLM-call helpers for DevAgent internals.

DevAgent receives an ``send_request_fn`` from the UI layer. Historically
that callable had an IMPLICIT contract:

    fn(user_message: str, system: str = "", history: list = None) -> str

created by ``ui.pages.orchestrator._make_send_adapter``. The docstrings in
agent_loop / tool_executor described it as ``core.api_layer.send_request``
which has a DIFFERENT signature (assistant dict, not ``system=``/``history=``).
That mismatch made assistant auto-creation fail with a cryptic
"LLM call to Assistant Creator failed" whenever the injected callable
happened not to accept ``system``/``history``.

This module makes the contract EXPLICIT and resilient: callers use
``call_llm_with_system`` and the helper inspects the callable's signature,
supporting BOTH the adapter form and the direct ``send_request`` form
(currently named ``assistant=``, with a legacy ``skill`` alias).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional


# Sentinels for detecting whether a callable accepts our optional keywords.
_MISSING = inspect.Parameter.empty


def _prefers_assistant_dict(fn: Callable) -> bool:
    """Return True if *fn* looks like ``send_request(user_message, assistant, ...)``.

    The adapter (from ui.pages.orchestrator._make_send_adapter) exposes
    ``(user_message, system='', history=None)``. A direct send_request exposes
    ``(user_message, assistant, file_context='', history=None, ...)`` (a legacy
    build may name the parameter ``skill``). We detect the second form by the
    presence of a parameter named ``assistant`` or ``skill``.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "assistant" in sig.parameters or "skill" in sig.parameters


# Legacy alias (old "skill" terminology).
_prefers_skill_dict = _prefers_assistant_dict


def _use_system_keyword(fn: Callable) -> bool:
    """Return True if *fn* accepts a ``system`` keyword argument."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "system" in sig.parameters


def call_llm_with_system(
    send_request_fn: Callable,
    user_message: str,
    system: str = "",
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    assistant_text: str = "",
    service: str = "",
    model: str = "",
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    tools: Optional[List[Any]] = None,
    max_tool_calls: Optional[int] = None,
    usage_callback: Optional[Callable[[Dict[str, int]], None]] = None,
    lang: Optional[str] = None,
    skill_text: str = "",  # legacy keyword, kept for compatibility
    json_schema: Optional[Dict[str, Any]] = None,
    json_schema_name: str = "",
) -> str:
    """Call *send_request_fn* with a system prompt, tolerating both signatures.

    Supports two callable forms:

    1. Legacy adapter (orchestrator UI):
           fn(user_message, system="", history=None)
       -> called as ``fn(user_message, system=system, history=history or [])``.

    2. Direct ``core.api_layer.send_request``:
           fn(user_message, assistant, file_context="", history=None, ...)``
       -> a synthetic assistant dict is built and passed as
          ``fn(user_message, assistant=assistant, file_context="", ...)``
          (legacy callables accepting ``skill`` are still supported via the
          ``skill=`` keyword instead).

    If *send_request_fn* accepts neither ``system`` nor ``assistant``/``skill``,
    the helper falls back to the adapter-style keyword call so the error
    message (if any) is raised by the callable itself with an explicit
    TypeError.

    Returns the plain text response. Propagates the callable's exceptions.
    """
    history = history or []
    effective_system_text = system or assistant_text or skill_text or ""

    if _prefers_assistant_dict(send_request_fn):
        try:
            sig = inspect.signature(send_request_fn)
            uses_skill = "skill" in sig.parameters and "assistant" not in sig.parameters
        except (TypeError, ValueError):
            uses_skill = False
        assistant: Dict[str, Any] = {
            "service": service,
            "model": model,
            "temperature": temperature,
            "text": effective_system_text,
            "tools": tools or [],
            "max_tool_calls": max_tool_calls,
            "max_tokens": max_tokens,
        }
        if json_schema:
            # Native structured output: bare schema dict or {name, schema}
            # envelope (core.api_layer._normalise_json_schema handles both).
            assistant["json_schema"] = (
                {"name": json_schema_name, "schema": json_schema}
                if json_schema_name
                else json_schema
            )
        # Prefer the canonical ``assistant=`` keyword; fall back to the
        # legacy ``skill=`` keyword if that is the only accepted parameter.
        call_kwargs: Dict[str, Any] = {
            "file_context": "",
            "history": history,
            "lang": lang,
            "usage_callback": usage_callback,
        }
        if uses_skill:
            call_kwargs["skill"] = assistant
        else:
            call_kwargs["assistant"] = assistant
        return send_request_fn(user_message, **call_kwargs)

    if _use_system_keyword(send_request_fn):
        return send_request_fn(
            user_message,
            system=effective_system_text,
            history=history,
        )

    # Fallback: adapter-style keywords anyway; the callable may raise a clear
    # TypeError that surfaces the contract mismatch.
    return send_request_fn(
        user_message,
        system=effective_system_text,
        history=history,
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
