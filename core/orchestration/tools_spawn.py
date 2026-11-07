# -*- coding: utf-8 -*-
"""core.orchestration.tools_spawn - starting a delegated run.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.orchestration.contracts import (
    ERR_INVALID_ARGS,
    ERR_NOT_IMPLEMENTED,
    ERR_UNKNOWN_AGENT,
    ERR_UNKNOWN_ASSISTANT,
    AgentBrief,
    InvokeRequest,
    SpawnRequest,
    error_result,
)
from core.orchestration.tools_notes import (
    _attach_note,
    _note_for_payload,
)
from core.orchestration.tools_rights import (
    _available_agents,
    _validate_brief,
    check_agent_allowed,
    check_assistant_allowed,
)


def spawn_agent(
    agent_slug: Optional[str] = None,
    request: Optional[AgentBrief] = None,
    inline_orchestrator: Optional[dict] = None,
    *,
    background: bool = False,
    resume_run_id: Optional[str] = None,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    thread_id: str = "",
    parent_config: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
    lang: Optional[str] = None,
    service=None,
    enable_injection_protection: Optional[bool] = None,
) -> Dict[str, Any]:
    """Launch a child orchestrator and return its structured result.

    Blocking by default: the call returns once the child finishes, so the
    parent sees a predictable result in the same step. With ``background=True``
    the child runs on its own thread and the call returns its ``run_id`` at
    once; the finish reaches the dialog later as a mailbox notice, and the
    caller observes it through ``subagent_status``. Either way the refusals
    (depth, tree total, pool slot) are raised synchronously, so a caller never
    receives a run id for work that will not happen.

    ``resume_run_id`` turns the call into a continuation of that run: the child
    inherits the tree, the depth and the budget of the original, and continues
    from the handoff in its own journal instead of starting over.

    ``inline_orchestrator`` is reserved for phase 2 and answers
    ``not_implemented``; the parameter exists so the phase-2 contract does
    not change the call shape.

    ``enable_injection_protection`` is the parent's prompt-injection gate
    setting. It is an internal caller argument, not a model-facing one: the
    agent loop reads it from the live parent state and passes it here, so a
    child inherits the mode of the run that started it. ``None`` leaves the
    decision to the service (resume inheritance or the safe default).
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
        background=bool(background),
        resume_run_id=(resume_run_id or "").strip() or None,
        enable_injection_protection=enable_injection_protection,
    )
    if background:
        # ``start`` validates synchronously and only then moves the child to
        # its own thread, so a refusal still reaches the caller in this step.
        out = service.start(spawn, parent_config=parent_config,
                            base_dir=base_dir, lang=lang)
        if isinstance(out, dict) and out.get("ok"):
            out = _attach_note(out, "The child runs in the background: observe it "
                                     "with subagent_status(run_id=...) or wait for "
                                     "the finish notice. Do not spawn it again.")
        return out
    return _note_for_payload(service.run(spawn, parent_config=parent_config,
                                         base_dir=base_dir, lang=lang))

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

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
