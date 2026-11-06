# -*- coding: utf-8 -*-
"""
One step of the agent loop: dispatch on the phase the state is in.

A step handles exactly ONE phase. It opens with the per-run housekeeping
(publishing the active thread and keeping its journal), and then hands over to
the module of that phase: ``calling_llm`` asks the model, ``parsing`` reads the
answer, ``executing`` runs the calls. The two entry phases ``init`` and
``pending_action`` are resolved by the opening itself, so a step that starts
in one of them does no model call at all.

The phase a handler leaves behind is what the NEXT step starts in, which is
why every handler returns the state instead of looping on its own.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from dev_agent.loop.state import AgentLoopState
from dev_agent.loop.steps.executing import execute_step
from dev_agent.loop.steps.models import call_model
from dev_agent.loop.steps.parsing import parse_step
from dev_agent.loop.steps.prepare import begin_step
from dev_agent.loop.steps.runtime import LoopRuntime, build_runtime


def step_agent_loop_impl(
    state: AgentLoopState,
    *,
    dispatcher: Any,
    lang: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
    send_request: Optional[Callable[..., str]] = None,
    drain_notices: Optional[Callable[[Any], List[str]]] = None,
) -> AgentLoopState:
    """Execute one phase of the loop and return the state it leaves behind.

    :param state: the loop state; its ``phase`` selects the handler.
    :param dispatcher: the tool dispatcher of the run.
    :param lang: the answer language.
    :param on_event: the raw event sink of the run.
    :param send_request: the model call; defaults to the one bound by the caller.
    :param drain_notices: the subagent-notice drain bound by the caller.
    """
    runtime = build_runtime(
        dispatcher=dispatcher,
        lang=lang,
        on_event=on_event,
        send_request=send_request,
        drain_notices=drain_notices,
    )
    return run_step(state, runtime)


def run_step(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Open the step and dispatch it on the phase it starts in."""
    initial_phase = state.phase
    state = begin_step(state, runtime)

    if state.phase in ("done", "error"):
        return state
    if initial_phase in ("init", "pending_action"):
        # The opening already resolved the step: it staged a reply or ran the
        # user's apply/discard, and the next step picks up from its phase.
        return state

    if state.phase == "calling_llm":
        return call_model(state, runtime, state.strong_assistant)
    if state.phase == "parsing":
        return parse_step(state, runtime)
    if state.phase == "executing":
        return execute_step(state, runtime)

    state.phase = "error"
    state.error_message = f"Unknown phase: {state.phase}"
    return state


def runtime_handlers() -> Dict[str, Callable[..., AgentLoopState]]:
    """Return the phase handlers keyed by phase name (introspection helper)."""
    return {
        "calling_llm": call_model,
        "parsing": parse_step,
        "executing": execute_step,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
