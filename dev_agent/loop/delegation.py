# -*- coding: utf-8 -*-
"""
dev_agent.loop.delegation -- the fan-out phase of the agent loop.

The delegation tools are NOT dispatched through the tool executor. A child
started inside ``dispatch`` would wait for the workspace-binding lock while
the parent holds it, and ``ask_parent`` has to stop the loop at a step
boundary rather than inside a tool call. The loop therefore handles this
family itself, outside dispatch, and runs the calls of one step in a plain
thread pool.

The service, not the loop, owns the pool semaphore, the tree counter and
cancellation: this module only turns tool-call arguments into requests and
maps the results back into their call order.

Two identities are easy to confuse here. ``parent_run_id`` is the run that
LAUNCHES the child, while ``state.run_id`` is the run the CALLER loop
represents - ``ask_parent`` parks the latter. Both travel explicitly, because
a delegation call may be made outside a loop (a custom function, a test) and
the fallback must still produce a working request.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - the annotation is a string at runtime
    from dev_agent.loop.state import AgentLoopState

#: Tools whose call is a delegation and therefore leaves ``dispatch``.
_DELEGATION_CALL_NAMES = ("spawn_agent", "invoke_assistant", "subagent_status",
                         "read_subagent_state", "resume_subagent", "sleep",
                         "cancel_subagent", "read_subagent_thread")

#: Calls the fan-out phase must route, on top of the delegation set above.
#: ``verify_result`` LAUNCHES a child, so it belongs to the same phase for the
#: same reason ``spawn_agent`` does: a child started inside ``dispatch`` would
#: wait for the workspace-binding lock the parent holds for the whole step.
#: The delegation tuple keeps its documented shape - it is the contract of the
#: tools that touch the run tree, not the phase's call filter.
#: The dialog pair belongs to the same phase for the same reason: asking
#: parks the run (the loop has to stop at a step boundary, not inside a tool
#: call) and answering unpacks it (the child loop continues in the answering
#: thread). Neither can run inside ``dispatch``.
_TREE_CALL_NAMES = _DELEGATION_CALL_NAMES + ("verify_result", "subagent_result",
                                             "ask_parent", "answer_child")


def _agent_brief_from_args(args: Dict[str, Any]):
    """Build an ``AgentBrief`` from tool-call arguments, or None.

    The model sends ``request`` as a JSON object; the contract layer wants an
    ``AgentBrief``. An already-built brief passes through unchanged, and a
    malformed one returns None so the refusal is produced by the tool layer
    (which owns the wording) rather than here.
    """
    raw = (args or {}).get("request")
    if raw is None:
        return None
    try:
        from core.orchestration.contracts import AgentBrief
    except Exception:
        return None
    if isinstance(raw, AgentBrief):
        return raw
    if not isinstance(raw, dict):
        return None
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        return None
    raw_artifacts = raw.get("artifacts_in")
    artifacts_in: Dict[str, str] = {}
    if isinstance(raw_artifacts, dict):
        for name, path in raw_artifacts.items():
            key = str(name or "").strip()
            value = str(path or "").strip()
            if key and value:
                artifacts_in[key] = value
    elif isinstance(raw_artifacts, (list, tuple)):
        # A bare list of paths is accepted and named by its basename: the
        # contract wants a name -> path map, and the model often sends the
        # files it was given without inventing names for them.
        for item in raw_artifacts:
            value = str(item or "").strip()
            if value:
                artifacts_in[os.path.basename(value) or value] = value
    elif raw_artifacts:
        value = str(raw_artifacts).strip()
        if value:
            artifacts_in[os.path.basename(value) or value] = value
    return AgentBrief(goal=goal, brief=str(raw.get("brief") or ""),
                      artifacts_in=artifacts_in)


def _delegation_parent_context(state: "AgentLoopState", dispatcher: Any):
    """Return (orchestrator slug, config, run id, thread id) for a step.

    The slug comes from the dispatcher when it knows its orchestrator; the
    config is read once per batch, not per call, so a batch of twenty calls
    does not hit the database twenty times. ``run_id`` falls back to the
    thread id, which makes limits work for a dialog that never registered a
    run of its own.
    """
    slug = "dev_agent"
    try:
        getter = getattr(dispatcher, "_current_orchestrator_slug", None)
        if callable(getter):
            slug = getter() or slug
    except Exception:
        slug = "dev_agent"
    cfg: Optional[Dict[str, Any]] = None
    try:
        from core.orchestrators import get_orchestrator
        orch = get_orchestrator(slug)
        if orch:
            cfg = orch.get("config") or {}
    except Exception:
        cfg = None
    return slug, cfg, (state.run_id or state.thread_id or ""), (state.thread_id or "")


def _delegation_base_dir(thread_id: str) -> Optional[str]:
    """Return the project folder a spawned child must work in, or None.

    Resolution order: the parent thread's own binding (the folder the dialog
    actually selected), then the live config root when a workspace is
    selected. A thread in the neutral empty state returns None, so the child
    starts in the empty state too instead of silently adopting another
    dialog's folder. The value is reported in the run record and in the
    child's transcript meta, which is what makes a lost child traceable to
    the project it worked on.
    """
    tid = (thread_id or "").strip()
    if tid:
        try:
            from dev_agent.workspace_binding import get_thread_workspace
            bound = get_thread_workspace(tid)
            if bound:
                return str(bound)
        except Exception:
            pass
    try:
        from dev_agent import config as _dagent_config
        if not getattr(_dagent_config, "WORKSPACE_SELECTED", True):
            return None
        root = getattr(_dagent_config, "PROJECT_ROOT", None)
        return str(root) if root else None
    except Exception:
        return None


def _job_spawn_agent(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Start a new child run (or resume one) and return its tool-shaped result."""
    from core.orchestration.tools import spawn_agent
    return spawn_agent(
        agent_slug=args.get("agent_slug"),
        request=_agent_brief_from_args(args),
        inline_orchestrator=args.get("inline_orchestrator"),
        background=bool(args.get("background")),
        resume_run_id=args.get("resume_run_id"),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        thread_id=ctx["thread_id"],
        parent_config=ctx["parent_config"],
        base_dir=ctx["base_dir"],
        lang=ctx["lang"],
        service=ctx["service"],
        enable_injection_protection=ctx["enable_injection_protection"],
    )


def _job_ask_parent(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Park the caller's own run on a question for its parent."""
    from core.orchestration.tools import ask_parent
    state = ctx["state"]
    # The run to park is the CALLER's own: ``parent_run_id`` already holds
    # it (the loop identity), and the state carries the same value when
    # the job was started by a loop step. The fallback keeps a call made
    # outside a loop (a custom function, a test) working.
    own_run_id = str(getattr(state, "run_id", "") or ctx["parent_run_id"] or "")
    # Dialog limits were frozen when the run started: the answering
    # process may belong to another dialog whose live config must not
    # change this run's rounds or question length.
    frozen = getattr(state, "dialog_limits", None) or ctx["parent_config"]
    return ask_parent(
        question=str(args.get("question") or ""),
        options=args.get("options") or [],
        done=str(args.get("done") or ""),
        blocked=str(args.get("blocked") or ""),
        run_id=own_run_id,
        parent_slug=ctx["parent_slug"],
        parent_config=frozen,
        service=ctx["service"],
        state=state,
    )


def _job_answer_child(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Continue a PARKED child in this thread with the parent's answer."""
    from core.orchestration.tools import answer_child
    return answer_child(
        run_id=args.get("run_id"),
        answer=str(args.get("answer") or ""),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        thread_id=ctx["thread_id"],
        parent_config=ctx["parent_config"],
        base_dir=ctx["base_dir"],
        lang=ctx["lang"],
        service=ctx["service"],
    )


def _job_verify_result(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Launch a verifier child for one finished run."""
    from core.orchestration.tools import verify_result
    return verify_result(
        run_id=args.get("run_id"),
        criteria=str(args.get("criteria") or ""),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        thread_id=ctx["thread_id"],
        parent_config=ctx["parent_config"],
        base_dir=ctx["base_dir"],
        lang=ctx["lang"],
        service=ctx["service"],
    )


def _job_sleep(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Wait for a child of the caller's tree.

    The wait needs the parent run id and the service from this context: the
    tree root is derived from the record, and the generic dispatch has neither
    value. Without this branch the call would reach the unconditional
    invoke_assistant fallback with assistant_id=None.
    """
    from core.orchestration.tools import sleep_tool
    return sleep_tool(
        seconds=args.get("seconds"),
        until=str(args.get("until") or "any_child"),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        service=ctx["service"],
    )


def _job_cancel_subagent(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Stop a child of the caller's tree."""
    from core.orchestration.tools import cancel_subagent
    return cancel_subagent(
        run_id=args.get("run_id"),
        cascade=bool(args.get("cascade", True)),
        reason=str(args.get("reason") or ""),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        service=ctx["service"],
    )


def _job_read_subagent_thread(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Read the conversation of a child of the caller's tree."""
    from core.orchestration.tools import read_subagent_thread
    # Every recovery argument has to survive this closure: a mode the
    # loop drops here is a mode the model can never use, however well the
    # tool itself handles it.
    raw_index = args.get("index")
    return read_subagent_thread(
        run_id=args.get("run_id"),
        offset=int(args.get("offset") or 0),
        limit=int(args.get("limit") or 50),
        include_tool_results=bool(args.get("include_tool_results")),
        mode=str(args.get("mode") or "window"),
        index=None if raw_index is None else int(raw_index),
        before=int(args.get("before") or 5),
        after=int(args.get("after") or 5),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        service=ctx["service"],
    )


def _job_subagent_result(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Collect what a finished child produced."""
    from core.orchestration.tools import subagent_result
    return subagent_result(
        run_id=args.get("run_id"),
        wait_seconds=float(args.get("wait_seconds") or 0.0),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        service=ctx["service"],
    )


def _job_subagent_status(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Report what is happening to one child, or to a whole tree."""
    from core.orchestration.tools import subagent_status
    return subagent_status(
        run_id=args.get("run_id"),
        root_run_id=args.get("root_run_id"),
        wait_seconds=float(args.get("wait_seconds") or 0.0),
        service=ctx["service"],
    )


def _job_read_subagent_state(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Read one section of a child's task journal."""
    from core.orchestration.tools import read_subagent_state
    return read_subagent_state(
        run_id=args.get("run_id"),
        section=args.get("section"),
        service=ctx["service"],
    )


def _job_resume_subagent(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Continue a finished child from its own journal."""
    from core.orchestration.tools import resume_subagent
    return resume_subagent(
        run_id=args.get("run_id"),
        request=_agent_brief_from_args(args),
        background=bool(args.get("background")),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        thread_id=ctx["thread_id"],
        parent_config=ctx["parent_config"],
        base_dir=ctx["base_dir"],
        lang=ctx["lang"],
        service=ctx["service"],
        enable_injection_protection=ctx["enable_injection_protection"],
    )


def _job_invoke_assistant(ctx: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """One-shot call to an assistant: the fallback of the fan-out phase."""
    from core.orchestration.tools import invoke_assistant
    return invoke_assistant(
        assistant_id=args.get("assistant_id"),
        user=str(args.get("user") or ""),
        context_files=args.get("context_files") or [],
        system=args.get("system"),
        model=args.get("model"),
        max_tokens=args.get("max_tokens"),
        parent_slug=ctx["parent_slug"],
        parent_run_id=ctx["parent_run_id"] or None,
        parent_config=ctx["parent_config"],
        base_dir=ctx["base_dir"],
        lang=ctx["lang"],
        service=ctx["service"],
    )


#: One handler per routed call. ``invoke_assistant`` is the documented
#: fallback, so an unknown name reaches it exactly as it always did.
_DELEGATION_HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = {
    "spawn_agent": _job_spawn_agent,
    "ask_parent": _job_ask_parent,
    "answer_child": _job_answer_child,
    "verify_result": _job_verify_result,
    "sleep": _job_sleep,
    "cancel_subagent": _job_cancel_subagent,
    "read_subagent_thread": _job_read_subagent_thread,
    "subagent_result": _job_subagent_result,
    "subagent_status": _job_subagent_status,
    "read_subagent_state": _job_read_subagent_state,
    "resume_subagent": _job_resume_subagent,
}


def _delegation_job(tool: str, args: Dict[str, Any], *,
                    parent_slug: str, parent_run_id: str, thread_id: str,
                    parent_config: Optional[Dict[str, Any]],
                    base_dir: Optional[str], lang: Optional[str],
                    service: Any, state: Any = None,
                    enable_injection_protection: Optional[bool] = None) -> Dict[str, Any]:
    """Execute ONE delegation call and return its tool-shaped result.

    ``state`` is the loop state of the CALLER: ``ask_parent`` parks the run
    named by it and hands the same object to the service, so a later answer
    can continue the run from the step that asked. Every other call ignores
    it and reads what it needs from the explicit identity arguments.
    """
    ctx = {
        "parent_slug": parent_slug,
        "parent_run_id": parent_run_id,
        "thread_id": thread_id,
        "parent_config": parent_config,
        "base_dir": base_dir,
        "lang": lang,
        "service": service,
        "state": state,
        "enable_injection_protection": enable_injection_protection,
    }
    handler = _DELEGATION_HANDLERS.get(tool, _job_invoke_assistant)
    return handler(ctx, args or {})


def _inherited_injection_protection(state: Any) -> bool:
    """Return the protection mode a child of this run inherits.

    A child inherits the prompt-injection gate of the run that started it.
    The one-step bypass created by approve_sanitized_content() is NOT
    inherited: it grants the dialog permission to view ONE withheld payload,
    not a permanent mode, and a child has no user to approve anything. While
    the bypass is active the state flag reads False, so the bypass flag
    itself decides: a bypassed step always hands a protected child.
    """
    if getattr(state, "injection_protection_bypassed", False):
        return True
    return bool(getattr(state, "enable_injection_protection", True))


def _validate_delegation_batch(calls: List[Dict[str, Any]], *,
                               state: Any, slug: str,
                               extra_disabled: Any,
                               emit: Callable[[dict], None]
                               ) -> Tuple[List[Optional[Dict[str, Any]]],
                                          List[Tuple[int, str, Dict[str, Any]]]]:
    """Check the WHOLE batch before a single pool slot is taken.

    A malformed or forbidden call costs no provider round-trip, and the model
    receives every complaint in one turn instead of discovering them one
    refusal at a time. A refusal does NOT cancel its neighbours - the batch is
    a fan-out, and one bad branch must not discard the work of the others.
    """
    from dev_agent.tool_validation import validate_tool_call

    jobs: List[Optional[Dict[str, Any]]] = [None] * len(calls)
    runnable: List[Tuple[int, str, Dict[str, Any]]] = []
    for idx, call in enumerate(calls):
        tool = (call.get("tool") or "").strip()
        args = call.get("args") or {}
        refusal = None
        if validate_tool_call is not None:
            refusal = validate_tool_call(tool, args, slug,
                                         extra_disabled=extra_disabled)
        if refusal is not None:
            jobs[idx] = refusal
            emit({"type": "tool_result", "tool": tool, "result": refusal,
                  "step": state.steps})
            continue
        runnable.append((idx, tool, args))
    return jobs, runnable


def _run_delegation_batch(runnable: List[Tuple[int, str, Dict[str, Any]]],
                          ctx: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Run the valid calls in parallel; one failure never cancels the rest."""
    results: Dict[int, Dict[str, Any]] = {}

    def _work(idx: int, tool: str, args: Dict[str, Any]) -> None:
        try:
            results[idx] = _delegation_job(
                tool, args, parent_slug=ctx["parent_slug"],
                parent_run_id=ctx["parent_run_id"],
                thread_id=ctx["thread_id"], parent_config=ctx["parent_config"],
                base_dir=ctx["base_dir"], lang=ctx["lang"],
                service=ctx["service"], state=ctx["state"],
                enable_injection_protection=ctx["enable_injection_protection"])
        except Exception as exc:  # pragma: no cover - defensive
            results[idx] = {"ok": False,
                            "error": "delegation failed: " + str(exc),
                            "error_code": "provider_error"}

    if len(runnable) == 1:
        idx, tool, args = runnable[0]
        _work(idx, tool, args)
        return results
    import concurrent.futures as _futures
    with _futures.ThreadPoolExecutor(max_workers=len(runnable)) as pool:
        futures = [pool.submit(_work, idx, tool, args)
                   for idx, tool, args in runnable]
        for future in futures:
            future.result()
    return results


def execute_delegation_calls(calls: List[Dict[str, Any]], *,
                             state: "AgentLoopState", dispatcher: Any,
                             lang: Optional[str] = None,
                             on_event: Optional[Callable[[dict], None]] = None,
                             service: Any = None) -> List[Dict[str, Any]]:
    """Run a step's delegation calls in parallel; results in call order.

    Two phases, deliberately separated: the WHOLE batch is validated first,
    then the valid calls run in parallel. Results are returned positionally,
    matching the order of the model's calls, so the parent can pair a result
    with the brief it sent.
    """
    if not calls:
        return []
    emit = on_event or (lambda _e: None)
    slug, cfg, parent_run_id, thread_id = _delegation_parent_context(state, dispatcher)
    inherited_protection = _inherited_injection_protection(state)
    base_dir = _delegation_base_dir(thread_id)
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    extra_disabled = getattr(dispatcher, "child_disabled_tools", None)
    jobs, runnable = _validate_delegation_batch(
        calls, state=state, slug=slug, extra_disabled=extra_disabled, emit=emit)

    if runnable:
        ctx = {
            "parent_slug": slug,
            "parent_run_id": parent_run_id,
            "thread_id": thread_id,
            "parent_config": cfg,
            "base_dir": base_dir,
            "lang": lang,
            "service": service,
            "state": state,
            "enable_injection_protection": inherited_protection,
        }
        results = _run_delegation_batch(runnable, ctx)
        for idx, _tool, _args in runnable:
            jobs[idx] = results.get(idx) or {
                "ok": False, "error": "delegation produced no result",
                "error_code": "provider_error"}

    return [j if j is not None else {"ok": False,
                                     "error": "delegation produced no result",
                                     "error_code": "provider_error"}
            for j in jobs]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
