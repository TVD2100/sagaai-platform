# -*- coding: utf-8 -*-
"""
What happens after the loop stopped for a human decision.

Three dialogs park a run and wait for a click:

* a dangerous operation (``awaiting_confirmation``) - Allow re-dispatches the
  tool with ``confirmed_by_user``, Deny tells the model to find another way;
* a sanitized payload (``sanitized_required``) - Allow runs one step with
  prompt-injection protection off and remembers the approved paths, Deny
  continues with protection on;
* a staged edit (``awaiting_approval``) - the runner applies or discards it
  through ``agent_loop.approve_and_apply`` / ``discard``.

Each handler appends a HIDDEN history entry: the model must learn what the
user decided, and the entry must not look like a new user request.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from dev_agent.loop.context import _CATEGORY_USER_TASK, _index_message
from dev_agent.loop.state import AgentLoopState
from dev_agent.loop.steps.prepare import now_ts


def _emit(on_event: Optional[Callable[[dict], None]], event: dict) -> None:
    """Pass one event to the subscriber when there is one."""
    if on_event:
        on_event(event)


def _record_decision(state: AgentLoopState, text: str) -> None:
    """Append the user's decision to the history as a hidden message."""
    entry = {"role": "user", "content": text, "hidden": True, "ts": now_ts()}
    _index_message(entry, state.next_index, _CATEGORY_USER_TASK)
    state.next_index += 1
    state.history.append(entry)


def _clear_confirmation(state: AgentLoopState) -> None:
    """Forget the parked dangerous operation."""
    state.pending_confirmation_tool = None
    state.pending_confirmation_args = None
    state.pending_confirmation_reasons = []
    state.pending_confirmation_code = ""
    state.pending_confirmation_result = None


def approve_pending_confirmation(state: AgentLoopState, dispatcher: Any,
                                 lang: Optional[str] = None,
                                 on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Re-dispatch a pending dangerous operation with ``confirmed_by_user``.

    Called by the UI when the user clicks Allow. The tool runs, its result goes
    back to the model and the loop continues; a failure ends the run with an
    error instead of looping on the same refused operation.
    """
    tool = state.pending_confirmation_tool
    args = dict(state.pending_confirmation_args or {})
    if not tool:
        state.phase = "error"
        state.error_message = "No pending confirmation to approve."
        return state

    args["confirmed_by_user"] = True
    _record_decision(state, f"User approved dangerous operation: {tool}")
    _emit(on_event, {"type": "confirmation_approved", "tool": tool, "step": state.steps})

    result = dispatcher.dispatch(tool, args)
    if not result.get("ok"):
        _emit(on_event, {"type": "tool_result", "tool": tool, "result": result,
                         "step": state.steps})
        state.phase = "done"
        state.final_status = "error"
        state.final_text = f"Approved operation {tool} failed: {result.get('error', 'unknown')}"
        state.error_message = state.final_text
        _clear_confirmation(state)
        return state

    _emit(on_event, {"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
    state.user_message = json.dumps({"tool_result": result}, ensure_ascii=False)
    _clear_confirmation(state)
    state.phase = "calling_llm"
    return state


def deny_pending_confirmation(state: AgentLoopState,
                              lang: Optional[str] = None,
                              on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Cancel a pending dangerous operation when the user clicks Deny."""
    tool = state.pending_confirmation_tool
    if not tool:
        state.phase = "error"
        state.error_message = "No pending confirmation to deny."
        return state

    _record_decision(state, f"User DENIED dangerous operation: {tool}")
    _emit(on_event, {"type": "confirmation_denied", "tool": tool, "step": state.steps})

    denied_result = {
        "ok": False,
        "error": "The user denied permission to execute this operation. "
                 "Find another way that does not require this command.",
        "denied_by_user": True,
    }
    _emit(on_event, {"type": "tool_result", "tool": tool, "result": denied_result,
                     "step": state.steps})

    state.user_message = json.dumps({"tool_result": denied_result}, ensure_ascii=False)
    _clear_confirmation(state)
    state.phase = "calling_llm"
    return state


def approve_sanitized_content(state: AgentLoopState, dispatcher: Any,
                              lang: Optional[str] = None,
                              on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Allow one step with prompt-injection protection disabled.

    Called when the user clicks "Allow viewing" on the [SANITIZED] dialog. The
    approved paths are remembered so their results stay visible on later
    protected turns instead of asking again.
    """
    _emit(on_event, {"type": "sanitized_approved", "step": state.steps})

    for info in state.sanitized_events:
        path = info.get("path")
        if isinstance(path, str) and path:
            state.sanitized_approved_paths.add(path)

    state.enable_injection_protection = False
    state.injection_protection_bypassed = True
    state.sanitized_events = []

    _record_decision(
        state,
        "User approved viewing the sanitized tool result. "
        "Retry the previous step with prompt-injection protection disabled "
        "for this turn only.",
    )

    state.user_message = state.task if state.task else ""
    state.phase = "calling_llm"
    return state


def deny_sanitized_content(state: AgentLoopState,
                           lang: Optional[str] = None,
                           on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Deny viewing sanitized content and continue with protection enabled."""
    _emit(on_event, {"type": "sanitized_denied", "step": state.steps})
    state.sanitized_events = []

    _record_decision(
        state,
        "User denied viewing the sanitized tool result. "
        "Continue the task without the withheld content and avoid "
        "relying on it.",
    )

    state.user_message = state.task if state.task else ""
    state.phase = "calling_llm"
    return state
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
