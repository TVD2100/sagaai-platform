# -*- coding: utf-8 -*-
"""
The opening of one loop step: which dialog we serve and what it should do.

Everything that must happen before the model is called lives here: publishing
the active thread id so the task-state journal of THIS dialog is the one that
is written, keeping that journal in existence, and turning the phase the step
starts in (``init``, ``pending_action``) into the phase it continues in.

The pending-action branch executes a staged edit (apply or discard) on behalf
of the user; it is the only part of a step that runs without calling a model.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from typing import Any

from dev_agent.loop.signals import classify_step_strength, normalize_hyphens
from dev_agent.loop.state import AgentLoopState
from dev_agent.loop.steps.runtime import LoopRuntime

log = logging.getLogger("dev_agent.agent_loop")


def now_ts() -> str:
    """Return the current local timestamp for live history entries."""
    from datetime import datetime

    return datetime.now().isoformat()


def publish_active_thread(state: AgentLoopState) -> None:
    """Publish the active dialog thread id for the task-state journal layer.

    Threads with a registered workspace binding publish it via the binding, so
    the switch and the thread id change atomically under the RLock. Failures
    never break the loop: the journal is a side channel.
    """
    thread_id = (getattr(state, "thread_id", "") or "").strip()
    try:
        from dev_agent import config as dagent_config
        from dev_agent import workspace_binding as wb

        if not wb.ensure_thread_active(thread_id):
            dagent_config.ACTIVE_THREAD_ID = thread_id
    except Exception:
        try:
            from dev_agent import config as dagent_config

            dagent_config.ACTIVE_THREAD_ID = thread_id
        except Exception:
            pass


def ensure_journal() -> None:
    """Create the per-thread journal when it does not exist yet.

    The journal is written for every task, so it is scaffolded even when the
    model never calls ``task_state_init``. Idempotent and cheap; a failure
    must never break the agent loop.
    """
    try:
        from dev_agent import task_state

        task_state.ensure_task_state_file()
    except Exception:
        pass


def prune_journals() -> None:
    """Run the once-per-process retention pass over the journal folder.

    A scaffold journal per thread accumulates files nobody will read again.
    The wrapper scans only on the first step of a process (a rerun or the next
    step is a no-op) and never removes the live journal or a young file.
    """
    try:
        from dev_agent import task_state

        task_state.prune_task_state_journals_once()
    except Exception:
        pass


def begin_step(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Publish the thread, keep the journal, and resolve ``init``/pending phases."""
    publish_active_thread(state)
    ensure_journal()
    prune_journals()

    if state.phase in ("done", "error"):
        return state

    if state.phase == "init":
        if state.pending_action and state.pending_staged_path:
            return run_pending_action(state, runtime)
        state.user_message = state.task if state.task else ""
        state.phase = "calling_llm"
        return state

    if state.phase == "pending_action":
        return run_pending_action(state, runtime)

    return state


def step_strength(state: AgentLoopState) -> str:
    """Classify how strong the previous answer was (reported on the next call)."""
    return classify_step_strength(state.parsed_calls, state.assistant_text)


def _append_pair(state: AgentLoopState, user_text: str, assistant_text: str) -> None:
    """Append a user/assistant exchange to the history with its index records."""
    from dev_agent.loop.context import _classify_message, _index_message

    ts = now_ts()
    user_msg = {"role": "user", "content": user_text, "ts": ts}
    _index_message(user_msg, state.next_index, _classify_message(user_msg))
    state.next_index += 1
    state.history.append(user_msg)
    done_msg = {"role": "assistant", "content": assistant_text, "ts": ts}
    _index_message(done_msg, state.next_index, _classify_message(done_msg))
    state.next_index += 1
    state.history.append(done_msg)


def run_pending_action(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Execute the staged edit the user approved or discarded.

    ``apply`` re-sends the staged text through ``propose_file`` (normalising
    the hyphens that break patch anchors) and then applies it; ``discard``
    drops the draft. Both end the run in a final phase. Any other action is
    taken as the user's message and the step continues to the model call.
    """
    if not (state.pending_action and state.pending_staged_path):
        state.phase = "calling_llm"
        return state

    path = state.pending_staged_path

    if state.pending_action == "apply":
        if state.staged_new_text:
            runtime.dispatcher.dispatch(
                "propose_file",
                {"path": path, "content": normalize_hyphens(state.staged_new_text), "note": ""},
            )
        apply_result = runtime.dispatcher.dispatch("apply_edit", {"path": path, "note": ""})
        if not apply_result.get("ok"):
            err_msg = apply_result.get("error", "apply_edit: not ok")
            runtime.emit(state, {"type": "error", "error": f"apply_edit failed: {err_msg}"})
            state.phase = "error"
            state.error_message = f"apply_edit failed: {err_msg}"
            return state
        _append_pair(state, f"apply_edit {path}", f"Applied: {apply_result}")
        runtime.emit(state, {"type": "applied", "tool": "apply_edit", "result": apply_result, "path": path})
        runtime.emit(state, {"type": "final", "text": f"Applied {path}.", "status": "applied"})
        state.steps = 1
        state.phase = "done"
        state.final_status = "applied"
        state.final_text = f"Applied {path}"
        state.final_applied = True
        return state

    if state.pending_action == "discard":
        discard_result = runtime.dispatcher.dispatch("discard_edit", {"path": path})
        _append_pair(state, f"discard_edit {path}", f"Discarded: {discard_result}")
        runtime.emit(state, {"type": "tool_result", "tool": "discard_edit", "result": discard_result})
        runtime.emit(state, {"type": "final", "text": f"Discarded {path}.", "status": "discarded"})
        state.steps = 1
        state.phase = "done"
        state.final_status = "discarded"
        state.final_text = f"Discarded {path}"
        state.final_discarded = True
        return state

    if state.pending_action == "ok":
        state.user_message = "OK, proceed."
    else:
        state.user_message = state.pending_action
    state.pending_action = None
    state.pending_staged_path = None
    state.phase = "calling_llm"
    return state


def describe_missing_phase(state: AgentLoopState) -> AgentLoopState:
    """End the run on a phase the step does not know how to handle."""
    state.phase = "error"
    state.error_message = f"Unknown phase: {state.phase}"
    return state


def safe_state(state: Any) -> Any:
    """Return *state* unchanged; a typing helper for the phase signatures."""
    return state
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
