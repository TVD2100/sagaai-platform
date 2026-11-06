# -*- coding: utf-8 -*-
"""
The model call of one loop step.

Everything between "the step wants an answer" and "the answer is in the
state": the step budget, the outgoing history window (snapshots, drained
child notices, the economy trim), the usage/sanitization/retry/incomplete
callbacks of the provider call, the bounded retry when a truncated answer
carries no visible text, and the prompt-injection stop that asks the user
whether to view a withheld payload.

The persisted user message goes into the history BEFORE the call: when the
connection dies after every retry, the dialog still shows what the user (or
the loop) sent and can be resumed.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from typing import Any, Dict, List

from dev_agent.loop.context import (
    _classify_message,
    _ensure_snapshot_visibility,
    _index_message,
    build_economy_context,
    refresh_context_snapshots,
)
from dev_agent.loop.results import _TOOL_RESULT_PREFIX
from dev_agent.loop.state import AgentLoopState
from dev_agent.loop.steps.prepare import now_ts
from dev_agent.loop.steps.runtime import LoopRuntime

#: Prefix of the synthetic user message that keeps an autonomous run going.
AUTO_CONTINUE_PREFIX = "AUTO_CONTINUE:"


def _persist_user_message(state: AgentLoopState) -> None:
    """Append the outgoing user message to the history before the call."""
    if not str(state.user_message).strip():
        return
    entry = {"role": "user", "content": state.user_message, "ts": now_ts()}
    if state.file_name:
        entry["file_name"] = state.file_name
        entry["file_chars"] = len(state.file_context) if state.file_context else 0
    stripped = str(state.user_message).strip()
    if stripped.startswith(_TOOL_RESULT_PREFIX) or stripped.startswith(AUTO_CONTINUE_PREFIX):
        entry["hidden"] = True
    category = _classify_message(entry)
    _index_message(entry, state.next_index, category)
    state.next_index += 1
    state.history.append(entry)
    state.file_name = ""


def _deliver_notices(state: AgentLoopState, runtime: LoopRuntime) -> None:
    """Move finished-child notices into THIS step before the window is built.

    Draining later would delay each notice by a whole step, and posting it as
    plain user text would push the step into the ``awaiting_user`` branch and
    end a run whose plan is still open. The hidden system message is the
    delivery channel: it reaches the model with this call and never disturbs
    the role alternation.
    """
    notice_lines = runtime.drain(state)
    if not notice_lines:
        return
    runtime.emit(state, {"type": "subagent_notices", "notices": notice_lines,
                         "step": state.steps})
    if not str(state.user_message or "").strip():
        state.user_message = (
            AUTO_CONTINUE_PREFIX
            + " A background subagent finished (see the system "
            "notice above). React to it and continue."
        )


def _outgoing_history(state: AgentLoopState, assistant: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the history window sent with this call.

    Called after the context snapshots are refreshed: an unchanged snapshot
    adds nothing (the prefix stays byte-stable), a changed one grows the chain
    instead of rewriting the seam between the window and the step payload. The
    latest snapshot is then forced into the window; when it slid out, one copy
    is appended to the tail rather than rewriting the front.
    """
    if state.economy_mode:
        effective_history = build_economy_context(state, assistant)
    else:
        effective_history = list(state.history)
    return _ensure_snapshot_visibility(state, effective_history)


def call_model(state: AgentLoopState, runtime: LoopRuntime, assistant: Dict[str, Any]) -> AgentLoopState:
    """Call the model for this step and store the answer in the state.

    Leaves the state in ``parsing`` with ``assistant_text`` set, in ``done``
    with ``sanitized_required`` when protection withheld a payload, or in
    ``error`` when the call failed after every retry.
    """
    from dev_agent.loop.signals import classify_step_strength

    if state.steps >= state.max_steps:
        runtime.emit(state, {"type": "stopped_max_steps"})
        state.phase = "done"
        state.final_status = "stopped_max_steps"
        return state

    state.steps += 1
    strength = classify_step_strength(state.parsed_calls, state.assistant_text)

    try:
        runtime.emit(state, {"type": "phase", "phase": "calling_llm", "step": state.steps,
                             "strength": strength, "model": assistant.get("model")})

        # Snapshots grow the chain BEFORE the notices land in it: an unchanged
        # snapshot adds nothing, a changed one appends at the seam instead of
        # rewriting the sent prefix of the next request.
        refresh_context_snapshots(state)
        _deliver_notices(state, runtime)
        effective_history = _outgoing_history(state, assistant)

        core = getattr(runtime.dispatcher, "core", None)
        if core is not None and hasattr(core, "set_history"):
            core.set_history(state.history)

        state.last_step_tokens = None

        def _on_tokens(tokens: Dict[str, int]) -> None:
            cache = tokens.get("cache", 0) or 0
            state.last_step_tokens = {"in": tokens["in"], "out": tokens["out"], "cache": cache}
            state.total_tokens_in += tokens["in"]
            state.total_tokens_out += tokens["out"]
            state.total_tokens_cache += cache

        sanitized_events: List[Dict[str, Any]] = []

        def _on_sanitized(info: dict) -> None:
            sanitized_events.append(info)

        def _on_retry(info: dict) -> None:
            runtime.emit(state, {
                "type": "retrying_llm",
                "step": state.steps,
                "attempt": info.get("attempt"),
                "attempts": info.get("attempts"),
                "delay": info.get("delay"),
                "error": info.get("error"),
            })

        incomplete_events: List[Dict[str, Any]] = []

        def _on_incomplete(info: dict) -> None:
            incomplete_events.append(info)

        _persist_user_message(state)

        # The file context goes with the FIRST call and is reset below; keep a
        # copy so a truncated-answer recovery retry resends it too.
        turn_file_context = state.file_context
        send_request = runtime.require_send_request()

        state.assistant_text = send_request(
            state.user_message, assistant,
            file_context=state.file_context,
            history=effective_history,
            lang=runtime.lang,
            usage_callback=_on_tokens,
            incomplete_callback=_on_incomplete,
            enable_injection_protection=state.enable_injection_protection,
            sanitized_callback=_on_sanitized,
            sanitized_approved_paths=state.sanitized_approved_paths,
            retry_callback=_on_retry,
        )
        state.file_context = ""

        state.assistant_text = _recover_truncated_answer(
            state, runtime, assistant, incomplete_events, turn_file_context,
            effective_history, _on_tokens, _on_incomplete, _on_sanitized, _on_retry,
        )

        if sanitized_events:
            state.sanitized_events = sanitized_events
            runtime.emit(state, {"type": "sanitized_detected",
                                 "events": sanitized_events, "step": state.steps})
            state.phase = "done"
            state.final_status = "sanitized_required"
            state.final_text = state.assistant_text
            return state

        # After a one-step bypass, restore the original protection setting.
        if state.injection_protection_bypassed:
            state.injection_protection_bypassed = False
            state.enable_injection_protection = True

    except Exception as exc:
        runtime.emit(state, {"type": "error", "error": str(exc)})
        state.phase = "error"
        state.error_message = str(exc)
        return state

    _append_assistant_message(state, runtime)
    state.phase = "parsing"
    return state


def _recover_truncated_answer(
    state: AgentLoopState,
    runtime: LoopRuntime,
    assistant: Dict[str, Any],
    incomplete_events: List[Dict[str, Any]],
    turn_file_context: str,
    effective_history: List[Dict[str, Any]],
    on_tokens: Any,
    on_incomplete: Any,
    on_sanitized: Any,
    on_retry: Any,
) -> str:
    """Retry ONCE with the reasoning effort lowered when the answer is empty.

    An HTTP 200 body with ``status == 'incomplete'`` (usually the reasoning
    budget ate the whole output allowance) can carry no user-visible text, so
    the raw answer looks normally empty. Lowering the effort to ``none`` frees
    the whole budget for visible text; an answer that already has text is kept
    as is.
    """
    if not incomplete_events or str(state.assistant_text).strip():
        return state.assistant_text

    info = incomplete_events[0]
    runtime.emit(state, {"type": "incomplete_retry",
                         "step": state.steps,
                         "reason": str(info.get("reason") or ""),
                         "output_tokens": info.get("output_tokens", 0)})
    retry_assistant = dict(assistant)
    retry_assistant["reasoning_effort"] = "none"
    incomplete_events.clear()
    send_request = runtime.require_send_request()
    return send_request(
        state.user_message, retry_assistant,
        file_context=turn_file_context,
        history=effective_history,
        lang=runtime.lang,
        usage_callback=on_tokens,
        incomplete_callback=on_incomplete,
        enable_injection_protection=state.enable_injection_protection,
        sanitized_callback=on_sanitized,
        sanitized_approved_paths=state.sanitized_approved_paths,
        retry_callback=on_retry,
    )


def _append_assistant_message(state: AgentLoopState, runtime: LoopRuntime) -> None:
    """Record the answer in the history and report it to the subscriber."""
    assistant_msg = {"role": "assistant", "content": state.assistant_text, "ts": now_ts()}
    if state.last_step_tokens:
        assistant_msg["_tokens"] = state.last_step_tokens
    category = _classify_message(assistant_msg)
    _index_message(assistant_msg, state.next_index, category)
    state.next_index += 1
    state.history.append(assistant_msg)
    runtime.emit(state, {"type": "assistant_text", "text": state.assistant_text, "step": state.steps})


def dump_tool_result(result: Dict[str, Any]) -> str:
    """Serialise one tool result for the next user message."""
    return json.dumps({"tool_result": result}, ensure_ascii=False)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
