# -*- coding: utf-8 -*-
"""core.orchestration.tools_rights - who a run may address.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.orchestration.contracts import (
    ERR_AGENT_NOT_ALLOWED,
    ERR_ASSISTANT_NOT_ALLOWED,
    ERR_INVALID_ARGS,
    ERR_INVALID_BRIEF,
    AgentBrief,
    error_result,
)


def _allowlist_context(parent_slug: str) -> Dict[str, Any]:
    """Return the allowlist view of a parent orchestrator.

    The result always contains the parent itself in ``allowed`` (self-
    delegation is not configurable) plus every agent it may actually spawn.
    The effective list is read rather than the raw config: the bundled
    Universal-Agent belongs to the default rights of every orchestrator, and
    enforcing the raw allowlist here would advertise a right the prompt
    offers and the runtime then refuses.

    The ``self_only`` flag is set when nothing but the parent remains: it
    exists to tell the model not to probe for other slugs.
    """
    try:
        from core.orchestrators import get_effective_agents
        configured = get_effective_agents(parent_slug)
    except Exception:
        configured = []
    allowed: List[str] = [parent_slug]
    for raw in configured:
        # Normalise here rather than trusting the source: a config written by
        # an import, an older version or a hand-edited folder can carry
        # blanks and duplicates, and this list is what the model is told is
        # callable - a whitespace entry in it is a dead end for the caller.
        slug = str(raw or "").strip()
        if slug and slug not in allowed:
            allowed.append(slug)
    return {"allowed": allowed, "self_only": len(allowed) == 1}

def _available_agents(parent_slug: str) -> List[str]:
    """Return the slugs the parent may address, for an ``unknown_agent`` reply."""
    return list(_allowlist_context(parent_slug)["allowed"])

def check_agent_allowed(parent_slug: str, agent_slug: str) -> Optional[Dict[str, Any]]:
    """Return a refusal payload when *agent_slug* may not be spawned, else None.

    An empty agent slug is a ``invalid_args`` error (the model must name an
    agent); an agent outside the allowlist is ``agent_not_allowed``. The
    refusal always carries ``allowed``; a self-only setup adds ``self_only``
    and ``hint`` so the model sees WHY the list is short.
    """
    slug = (agent_slug or "").strip()
    if not slug:
        return error_result(
            "", "spawn_agent needs agent_slug: the slug of an orchestrator.",
            ERR_INVALID_ARGS, missing=["agent_slug"])
    ctx = _allowlist_context(parent_slug)
    if slug in ctx["allowed"]:
        return None
    details: Dict[str, Any] = {"agent": slug, "allowed": ctx["allowed"]}
    if ctx["self_only"]:
        details["self_only"] = True
        details["hint"] = ("Only your own agent is available. Add other "
                           "orchestrators to allowed_agents to delegate to them.")
    return error_result("", "Agent not allowed: " + slug + ".",
                        ERR_AGENT_NOT_ALLOWED, **details)

def check_assistant_allowed(parent_slug: str,
                            assistant_id: str) -> Optional[Dict[str, Any]]:
    """Return a refusal payload when the assistant may not be called, else None."""
    aid = (assistant_id or "").strip()
    try:
        from core.orchestrators import get_allowed_assistants
        allowed = get_allowed_assistants(parent_slug)
    except Exception:
        allowed = []
    if aid in allowed:
        return None
    return error_result("", "Assistant not allowed: " + aid + ".",
                        ERR_ASSISTANT_NOT_ALLOWED, assistant=aid,
                        allowed=list(allowed))

def _validate_brief(request: Any) -> Optional[Dict[str, Any]]:
    """Return a refusal payload for a malformed brief, else None."""
    if not isinstance(request, AgentBrief):
        return error_result(
            "", "spawn_agent needs a brief object with at least a goal.",
            ERR_INVALID_BRIEF, expected=["goal", "brief", "artifacts_in"])
    if not str(request.goal or "").strip():
        return error_result("", "The brief has an empty goal: say what the subagent must do.",
                            ERR_INVALID_BRIEF, missing=["goal"])
    return None

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
