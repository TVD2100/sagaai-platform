# -*- coding: utf-8 -*-
"""
core.orchestration.tools - the delegation tools exposed to the model.

Two tools, one contract:

- spawn_agent launches a CHILD ORCHESTRATOR: a full agent with its own
  tools, its own thread and its own journal. Blocking in v1 - it returns the
  child result once the child finishes.
- invoke_assistant makes ONE model call to an existing assistant (or an
  ephemeral prompt): no tools, no RAG bases, no agent loop.

Rights are enforced here, next to the arguments, so the same refusal is
produced whether the tool is reached through the loop branch or through
dispatch. The rule set is small and explicit:

- spawn_agent is ALWAYS callable: self-delegation needs no configuration, so
  the orchestrator's own slug is allowed by definition. The allowlist
  narrows the circle of OTHER agents only.
- an empty allowlist therefore never produces an empty ``allowed`` list;
  it produces ``allowed: [self]``, ``self_only: true`` and a hint, so the
  model does not spend steps probing for other names.
- invoke_assistant is registered only when the allowlist is non-empty:
  there are no assistants by default, unlike the right to call yourself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.orchestration.contracts import (
    AgentBrief,
    ERR_AGENT_NOT_ALLOWED,
    ERR_ASSISTANT_NOT_ALLOWED,
    ERR_INVALID_ARGS,
    ERR_INVALID_BRIEF,
    ERR_NOT_IMPLEMENTED,
    ERR_UNKNOWN_AGENT,
    ERR_UNKNOWN_ASSISTANT,
    InvokeRequest,
    SpawnRequest,
    error_result,
)


def _allowlist_context(parent_slug: str) -> Dict[str, Any]:
    """Return the allowlist view of a parent orchestrator.

    The result always contains the parent itself in ``allowed`` (self-
    delegation is not configurable) plus every configured agent. The
    ``self_only`` flag is set when the configured list is empty: it exists
    to tell the model not to probe for other slugs.
    """
    try:
        from core.orchestrators import get_allowed_agents
        configured = get_allowed_agents(parent_slug)
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


def spawn_agent(
    agent_slug: Optional[str] = None,
    request: Optional[AgentBrief] = None,
    inline_orchestrator: Optional[dict] = None,
    *,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    thread_id: str = "",
    parent_config: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
    lang: Optional[str] = None,
    service=None,
) -> Dict[str, Any]:
    """Launch a child orchestrator and return its structured result.

    Blocking: the call returns once the child finishes, so the parent sees a
    predictable result in the same step. ``background`` is not accepted in
    v1 - there is no job registry; a caller that needs several children
    simply issues several calls in one message and the loop runs them in
    parallel.

    ``inline_orchestrator`` is reserved for phase 2 and answers
    ``not_implemented``; the parameter exists so the phase-2 contract does
    not change the call shape.
    """
    if inline_orchestrator is not None:
        return error_result(
            "", "inline_orchestrator is not implemented in v1: pass the slug "
            "of an existing orchestrator in agent_slug.",
            ERR_NOT_IMPLEMENTED, planned="phase 2")

    refusal = check_agent_allowed(parent_slug, agent_slug or "")
    if refusal is not None:
        return refusal

    brief_refusal = _validate_brief(request)
    if brief_refusal is not None:
        return brief_refusal
    brief: AgentBrief = request  # type: ignore[assignment]

    # An unknown slug is a caller mistake, not a rights problem: report it
    # with the list of slugs this caller may actually address.
    try:
        from core.orchestrators import get_orchestrator
        target = get_orchestrator((agent_slug or "").strip())
    except Exception:
        target = None
    if not target:
        return error_result(
            "", "Agent not found: " + str(agent_slug) + ".",
            ERR_UNKNOWN_AGENT, agent=agent_slug,
            available=_available_agents(parent_slug))

    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    spawn = SpawnRequest(
        agent_slug=(agent_slug or "").strip(),
        request=brief,
        thread_id=thread_id or "",
        parent_run_id=parent_run_id,
    )
    return service.run(spawn, parent_config=parent_config,
                       base_dir=base_dir, lang=lang)


def invoke_assistant(
    assistant_id: Optional[str] = None,
    user: str = "",
    context_files: Optional[List[str]] = None,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    *,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    parent_config: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
    lang: Optional[str] = None,
    service=None,
    invoke_fn=None,
) -> Dict[str, Any]:
    """Make one model call to an assistant or to an ephemeral prompt.

    One step, no agent loop: the assistant's tools and RAG bases are NOT
    applied, and nothing is truncated - an oversized context is refused with
    ``context_too_large``. The result carries ``assistant: {id, name}`` so the
    feed shows who answered instead of an eight-character id.
    """
    if assistant_id and system is not None:
        return error_result(
            "", "assistant_id and system are mutually exclusive.",
            ERR_INVALID_ARGS, conflicting=["assistant_id", "system"])
    if not assistant_id and system is None:
        return error_result(
            "", "Pass assistant_id (an existing assistant) or system (an "
            "ephemeral prompt); at least one is required.",
            ERR_INVALID_ARGS, missing=["assistant_id|system"])
    if not str(user or "").strip() and not context_files:
        return error_result(
            "", "Nothing to analyse: pass user text and/or context_files.",
            ERR_INVALID_ARGS, missing=["user|context_files"])

    if assistant_id:
        refusal = check_assistant_allowed(parent_slug, assistant_id)
        if refusal is not None:
            return refusal
        try:
            from core.assistants import get_assistant_by_id
            card = get_assistant_by_id((assistant_id or "").strip())
        except Exception:
            card = None
        if not card:
            return error_result(
                "", "Unknown assistant id: " + str(assistant_id) + ".",
                ERR_UNKNOWN_ASSISTANT, assistant=assistant_id)

    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    call = InvokeRequest(
        assistant_id=(assistant_id or "").strip() or None,
        user=str(user or ""),
        context_files=list(context_files or []),
        system=system,
        model=model,
        max_tokens=max_tokens,
        parent_run_id=parent_run_id,
    )
    return service.invoke(call, parent_config=parent_config,
                          parent_slug=parent_slug, base_dir=base_dir,
                          lang=lang, invoke_fn=invoke_fn)


def spawn_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``spawn_agent``.

    The description states the boundaries the model must know: the child is a
    full agent (unlike a one-shot assistant call), the call is blocking, and
    self-delegation needs no configuration.
    """
    return {
        "name": "spawn_agent",
        "desc": (
            "Launch a subagent (a full agent with its own tools, "
            "thread and journal) and get its structured result. Args: "
            "agent_slug (the orchestrator slug; your own slug is always "
            "allowed), request {goal, brief, artifacts_in}. Blocking: returns "
            "once the subagent finishes. Several calls in ONE message run in "
            "parallel. The subagent gets a CLEAN history - put everything it "
            "needs into goal and brief, and give it ABSOLUTE paths: the "
            "subagent starts in your workspace and its own dialog has no "
            "uploads, so a relative path or a bare file name is a dead end. "
            "Limits (max_depth, max_active_subagents, total_subagent_runs) "
            "are read from your config, not from the call."
        ),
    }


def invoke_tool_entry() -> Dict[str, str]:
    """Return the catalog entry of ``invoke_assistant``.

    The description spells out the one-shot boundary: no tools, no RAG bases,
    no agent loop. Without that sentence the behaviour would drift silently
    as soon as someone moves code around.
    """
    return {
        "name": "invoke_assistant",
        "desc": (
            "One-shot call to an existing assistant or to an ephemeral prompt: "
            "a SINGLE model call, no agent loop. Args: assistant_id (an "
            "assistant from allowed_assistants) OR system (ephemeral prompt; "
            "mutually exclusive with assistant_id), user, [context_files], "
            "[model], [max_tokens]. The assistant's tools and knowledge "
            "bases are NOT applied; for tool work use spawn_agent with an "
            "orchestrator. Pass a document as a file in context_files with "
            "an ABSOLUTE path (PDF, DOCX, PPTX, XLSX, RTF, ODT, HTML, text) "
            "instead of pasting its text or splitting it into several calls: "
            "one file - one call, the whole document reaches the assistant. "
            "An oversized context is refused, never truncated."
        ),
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
