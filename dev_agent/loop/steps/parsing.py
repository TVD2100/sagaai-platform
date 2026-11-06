# -*- coding: utf-8 -*-
"""
The parse phase of one loop step: what the answer asks us to do.

Two outcomes are possible. The answer carries tool calls, and the step moves
on to executing them; or it carries prose only, and the step ends - in which
case this module decides WHICH ending: an explicit ``loop_status`` block (the
primary contract), the legacy ``_requires_user_response`` field, the English
final phrase, a question, a plan awaiting approval, or the auto-continue
rules of autonomous mode.

A delegated child never has a user to answer it, so a would-be ``awaiting_user``
ending is turned into one more autonomous step until the child's own continue
budget runs out; the ceiling ends the run as ``awaiting_user`` with the
distinct ``child_unfinished`` cause so the parent can tell it apart from an
ordinary step-budget failure.

A degraded model repeats the same call or the same broken JSON dozens of
times. Both floods end the step with ONE compact error instead of executing
the batch or dumping every diagnostic back into the context.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from dev_agent.loop.parse import (
    _fallback_parse_propose_file,
    _unparsed_block_signature,
    _unparsed_tool_json_blocks,
    _unparsed_tool_json_diagnostics,
    parse_tool_calls,
)
from dev_agent.loop.results import _TOOL_RESULT_PREFIX
from dev_agent.loop.signals import (
    _FINAL_PHRASE,
    _looks_like_confirmation_request,
    _looks_like_plan,
    _parse_loop_status,
    _parse_requires_user_response,
    _prose_contains_progress,
    _prose_looks_like_question,
)
from dev_agent.loop.state import AgentLoopState
from dev_agent.loop.steps.models import AUTO_CONTINUE_PREFIX
from dev_agent.loop.steps.runtime import LoopRuntime

#: How many times a CHILD loop may auto-continue instead of stopping at
#: ``awaiting_user``. A child has no user to answer, so stopping would mean
#: waiting forever; a counter keeps that autonomy bounded and turns a child
#: that keeps asking into a normal ``awaiting_user`` result the parent reads.
MAX_CHILD_AUTO_CONTINUES = 8

_MAX_CONSECUTIVE_ERRORS = 3
_MAX_DUPLICATE_BLOCKS = 8
_MAX_DIAGNOSTIC_ENTRIES = 5

#: User reply that keeps an autonomous run going after a tool result.
_AUTO_CONTINUE_MESSAGE = AUTO_CONTINUE_PREFIX + " Continue to the next step."


def _identical_block_count(text: str) -> int:
    """Count the LARGEST group of whitespace-identical tool-call JSON blocks.

    A degraded model in an over-committed context repeatedly emits the same
    broken call dozens/hundreds of times. One compact 'N identical blocks'
    error gets it unstuck far better than a per-block diagnostic dump that
    re-floods the context.
    """
    import re

    groups: Dict[str, int] = {}
    for body in re.findall(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.DOTALL):
        if '"tool"' not in body or "{" not in body:
            continue
        key = re.sub(r"\s+", "", body)
        groups[key] = groups.get(key, 0) + 1
    # Unclosed tail (truncated message): everything after the last closing fence.
    tail_start = 0
    for match in re.finditer(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.DOTALL):
        tail_start = match.end()
    tail = text[tail_start:]
    if tail.count("```") % 2 == 1:
        fence_start = tail.find("```")
        body = tail[fence_start + 3:]
        if '"tool"' in body and "{" in body:
            key = re.sub(r"\s+", "", body)
            groups[key] = groups.get(key, 0) + 1
    return max(groups.values()) if groups else 0


def _dedupe_diagnostics(diagnostics: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Collapse repeated diagnostic entries into a single counter record.

    Keeps at most :data:`_MAX_DIAGNOSTIC_ENTRIES` entries TOTAL (unique
    per-block entries plus one final omission counter), so a flood of identical
    broken blocks can never flood the model's context back.
    """
    if len(diagnostics) <= _MAX_DIAGNOSTIC_ENTRIES:
        return diagnostics
    import hashlib

    seen = set()
    unique: List[Dict[str, str]] = []
    skipped = 0
    unique_limit = max(1, _MAX_DIAGNOSTIC_ENTRIES - 1)
    for item in diagnostics:
        key = (item.get("snippet") or "") + "\x00" + (item.get("cause") or "")
        signature = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:32]
        if signature in seen:
            skipped += 1
            continue
        if len(unique) < unique_limit:
            unique.append(item)
            seen.add(signature)
        else:
            skipped += 1
    if skipped:
        unique.append({
            "snippet": "",
            "cause": f"+{skipped} more identical/unparsed block diagnostics omitted",
        })
    return unique


def _dominant_duplicate_call(parsed_calls: List[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """Detect a flood of IDENTICAL parsed calls (tool+args) in one message.

    A degraded model repeats the same call hundreds of times; dispatching
    them all would re-execute the same action over and over. Returns
    ``{'count': N, 'total': M}`` when N > :data:`_MAX_DUPLICATE_BLOCKS`,
    else ``None``.
    """
    if not parsed_calls:
        return None
    counts: Dict[str, int] = {}
    for call in parsed_calls:
        if not isinstance(call, dict):
            continue
        tool = call.get("tool")
        if tool is None:
            continue
        key = str(tool) + "\x00" + json.dumps(call.get("args") or {}, sort_keys=True)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    if top > _MAX_DUPLICATE_BLOCKS:
        return {"count": top, "total": len(parsed_calls)}
    return None


def _continue_loop(state: AgentLoopState, runtime: LoopRuntime,
                   reason: str, message: str = _AUTO_CONTINUE_MESSAGE) -> AgentLoopState:
    """Emit the auto-continue phase and schedule the next model call."""
    runtime.emit(state, {"type": "phase", "phase": "auto_continuing",
                         "reason": reason, "step": state.steps})
    state.user_message = message
    state.phase = "calling_llm"
    return state


def _finish(state: AgentLoopState, runtime: LoopRuntime, status: str) -> AgentLoopState:
    """End the run with a final answer in *status*."""
    runtime.emit(state, {"type": "final", "text": state.assistant_text, "status": status})
    state.phase = "done"
    state.final_status = status
    state.final_text = state.assistant_text
    return state


def _fail_json(state: AgentLoopState, runtime: LoopRuntime,
               compact: Dict[str, Any]) -> AgentLoopState:
    """Feed one compact parse error back and ask for a corrected call."""
    runtime.emit(state, {"type": "tool_result", "tool": "parse_tool_calls",
                         "result": compact, "step": state.steps})
    state.user_message = json.dumps({"tool_result": compact}, ensure_ascii=False)
    state.phase = "calling_llm"
    return state


def parse_step(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Turn the answer into tool calls, or decide how the run ends."""
    runtime.emit(state, {"type": "phase", "phase": "parsing", "step": state.steps})
    state.parsed_calls = parse_tool_calls(state.assistant_text)
    state.partial_parse_warning = None

    duplicate = _dominant_duplicate_call(state.parsed_calls)
    if duplicate is not None:
        compact = {
            "ok": False,
            "error": (
                f"Detected {duplicate['count']} IDENTICAL tool calls out of "
                f"{duplicate['total']} in one message. Send ONE copy "
                "of the call, then process the response before the "
                "next step - do not repeat the same call in bulk."
            ),
            "identical_calls": duplicate["count"],
        }
        return _fail_json(state, runtime, compact)

    if not state.parsed_calls:
        fallback = _fallback_parse_propose_file(state.assistant_text)
        if fallback is not None:
            state.parsed_calls = [fallback]
            runtime.emit(state, {"type": "phase", "phase": "executing",
                                 "tool": "propose_file", "step": state.steps,
                                 "fallback": True})
            state.phase = "executing"
            return state

    if not state.parsed_calls and _unparsed_tool_json_blocks(state.assistant_text) > 0:
        return _handle_unparsed_blocks(state, runtime)

    _warn_partial_batch(state)

    if not state.parsed_calls:
        return _conclude_prose_step(state, runtime)

    state.phase = "executing"
    return state


def _handle_unparsed_blocks(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Report tool-call JSON that failed to parse even after repair."""
    identical = _identical_block_count(state.assistant_text)
    if identical > _MAX_DUPLICATE_BLOCKS:
        compact = {
            "ok": False,
            "error": (
                f"Detected {identical} IDENTICAL malformed tool-call "
                "JSON blocks in one message. Stop repeating the broken "
                "call; emit ONE correct fenced tool call, or "
                "switch to a different tool."
            ),
            "identical_blocks": identical,
        }
        return _fail_json(state, runtime, compact)

    diagnostics = _dedupe_diagnostics(_unparsed_tool_json_diagnostics(state.assistant_text))
    signature = _unparsed_block_signature(diagnostics)
    repeated = bool(signature and signature == state.last_unparsed_signature)
    state.unparsed_repeat_count = state.unparsed_repeat_count + 1 if repeated else 1
    if not signature:
        state.unparsed_repeat_count = 0
    if state.unparsed_repeat_count > _MAX_CONSECUTIVE_ERRORS:
        hard_msg = (
            "The SAME broken tool-call JSON was sent "
            + str(_MAX_CONSECUTIVE_ERRORS)
            + " times in a row. Manual intervention is needed."
        )
        runtime.emit(state, {"type": "error", "error": hard_msg})
        state.phase = "error"
        state.error_message = hard_msg
        return state
    if signature:
        state.last_unparsed_signature = signature

    warn_prefix = (
        "WARNING: you just resent the SAME broken call. DO NOT repeat "
        "it - switch to another tool in the fallback chain."
    )
    base_msg = (
        "Tool-call JSON was detected but could not be parsed. "
        "Read the cause below, check that the JSON syntax is correct "
        "and the function call and arguments match the system prompt, "
        "fix it in ONE retry; if unclear or the retry fails, switch "
        "immediately down the fallback chain."
    )
    err_result = {
        "ok": False,
        "error": warn_prefix + " " + base_msg if repeated else base_msg,
        "diagnostics": diagnostics,
    }
    return _fail_json(state, runtime, err_result)


def _warn_partial_batch(state: AgentLoopState) -> None:
    """Warn about blocks of a batch that parsed and therefore will not run."""
    if not state.parsed_calls or any(call.get("_json_repaired") for call in state.parsed_calls):
        return
    raw = _unparsed_tool_json_diagnostics(state.assistant_text)
    if not raw:
        return
    warn_text = (
        "Fenced tool-call block(s) in this message failed to parse "
        f"({len(raw)} failing construct(s) diagnosed) and "
        "were NOT executed; the other "
        + str(len(state.parsed_calls))
        + " call(s) of the batch were executed (results above). "
        "Re-emit ONLY the failed call(s) with correct JSON syntax - "
        "batching the retry together with the remaining independent "
        "calls is still fine."
    )
    state.partial_parse_warning = {
        "ok": False,
        "error": warn_text,
        "partial_batch": True,
        "failed_constructs": len(raw),
        "executed_calls": len(state.parsed_calls),
        "diagnostics": _dedupe_diagnostics(raw),
    }


def _conclude_prose_step(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Decide how a prose-only step ends and leave the state in that phase."""
    text = state.assistant_text
    loop_status = _parse_loop_status(text)

    if loop_status == "continue":
        return _continue_loop(state, runtime, "loop_status: continue")

    if loop_status == "done":
        final_status = "done"
    elif loop_status == "awaiting_user":
        if _looks_like_plan(text) and not state.child_mode:
            state.plan_presented = True
        final_status = "awaiting_user"
    elif _parse_requires_user_response(text) is False:
        return _continue_loop(state, runtime, "_requires_user_response: false")
    elif _parse_requires_user_response(text) is True:
        final_status = "awaiting_user"
    elif _FINAL_PHRASE.search(text):
        final_status = "done"
    elif _prose_looks_like_question(text):
        final_status = "awaiting_user"
    elif _looks_like_plan(text) or _looks_like_confirmation_request(text):
        # A child has nobody to approve a plan: latching the flag would make
        # every following turn stop on the same text until the auto-continue
        # budget burns out, while the child keeps doing real work.
        if _looks_like_plan(text) and not state.child_mode:
            state.plan_presented = True
        runtime.emit(state, {"type": "phase", "phase": "awaiting_plan_approval",
                             "reason": "plan or confirmation request detected", "step": state.steps})
        final_status = "awaiting_user"
    else:
        auto = _auto_apply_decision(state, runtime)
        if auto is not None:
            return auto
        final_status = "awaiting_user"

    if final_status == "awaiting_user" and state.child_mode:
        return _child_decision(state, runtime)

    return _finish(state, runtime, final_status)


def _auto_apply_decision(state: AgentLoopState, runtime: LoopRuntime) -> Optional[AgentLoopState]:
    """Decide whether an autonomous run keeps going on a prose step.

    Returns the continued state, or ``None`` when the run must wait for the
    user (the caller then ends the step as ``awaiting_user``).
    """
    if not state.auto_apply:
        return None
    previous = str(state.user_message).strip()
    if state.plan_presented:
        runtime.emit(state, {"type": "phase", "phase": "awaiting_plan_approval",
                             "reason": "plan was presented; ignoring AUTO_CONTINUE", "step": state.steps})
        return None
    if previous.startswith(AUTO_CONTINUE_PREFIX):
        return _continue_loop(state, runtime, "previous turn was auto-continue")
    if previous.startswith(_TOOL_RESULT_PREFIX):
        return _continue_loop(state, runtime, "previous turn was a tool result")
    if _prose_contains_progress(state.assistant_text):
        return _continue_loop(state, runtime, "progress phrase detected")
    return None


def _child_decision(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Let a delegated child decide on its own instead of waiting for a user."""
    if state.auto_continue_count < MAX_CHILD_AUTO_CONTINUES:
        state.auto_continue_count += 1
        message = (
            AUTO_CONTINUE_PREFIX
            + " No user is available to answer: decide yourself on the "
            "most reasonable option, record the decision in your "
            "result, and continue to the next step."
        )
        return _continue_loop(state, runtime, "child decides without a user", message)

    # The child burned its whole autonomy budget without ever saying the work
    # was finished. Its files are usually on disk, so the parent needs a cause
    # it can tell apart from a real step-budget failure.
    state.error_code = "child_unfinished"
    state.error_message = (
        "The child exhausted its autonomous continuations "
        "without signalling completion."
    )
    return _finish(state, runtime, "awaiting_user")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
