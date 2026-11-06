# -*- coding: utf-8 -*-
"""
The execute phase of one loop step: running the calls of the batch.

A batch is executed in call order, but not uniformly:

* delegation calls run FIRST, as one parallel fan-out, because starting a
  child inside dispatch would deadlock against the binding lock the parent
  holds for the duration of the step;
* the two write tools (``propose_file``, ``apply_patch``) carry their own
  branches: hyphens are normalised in the staged text and edit anchors, the
  automatic-apply flag is supplied, and a draft that only STAGED the change
  ends the run as ``awaiting_approval`` for the user to Apply or Discard;
* ``apply_edit`` and ``discard_edit`` are refused in manual mode - only the
  user's buttons may call them;
* everything else goes through ``dispatch_json``, where an exception becomes
  a structured ``ok=False`` result instead of killing the loop, and a
  dangerous operation either parks the run for confirmation or, in a child,
  is refused with an explanation it can act on.

A repeated failing call is blocked: the same write/DB-mutation call with the
same arguments will not be re-executed, and a per-tool failure counter folds
fallback guidance into the error once a tool keeps failing.
"""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from dev_agent.loop.delegation import _TREE_CALL_NAMES, execute_delegation_calls
from dev_agent.loop.parse import (
    _DSML_STOP_NOTICE,
    _DUPLICATE_GUARD_TOOLS,
    _call_signature,
    _dsml_validation_error,
    _uses_dsml_markup,
)
from dev_agent.loop.signals import normalize_hyphens
from dev_agent.loop.state import AgentLoopState
from dev_agent.loop.steps.runtime import LoopRuntime

#: Tools only the user may invoke, and only outside automatic mode.
_USER_GATED = {"apply_edit", "discard_edit"}

#: Read-only tools whose result carries a one-line summary for the feed.
_READ_TOOLS = {
    "read_file", "list_files", "scan_folder", "assess_workspace",
    "build_project_map", "read_doc", "current_workspace", "verify_file",
}

#: Tools that move the workspace, so their result refreshes the state summary.
_WORKSPACE_TOOLS = {"current_workspace", "set_workspace", "set_target_file"}

_MAX_CONSECUTIVE_ERRORS = 3


def _summarise_result(tool: str, result: Dict[str, Any]) -> str:
    """Return the one-line summary shown for a read-only tool result."""
    if not result.get("ok"):
        return f"{tool}: error -- {result.get('error', 'unknown')}"
    if tool == "read_file":
        return f"Read {result.get('path', '?')} ({result.get('total_lines', 0)} lines)"
    if tool == "list_files":
        return f"Listed {result.get('count', 0)} files"
    if tool == "scan_folder":
        files = result.get("files", [])
        langs = result.get("languages", {})
        top = ", ".join(f"{name}:{count}" for name, count in sorted(langs.items(), key=lambda x: -x[1])[:3])
        return f"Scanned workspace: {len(files)} files, {top}"
    if tool == "assess_workspace":
        return f"Assessment: {result.get('state', '?')} ({result.get('code_files', 0)} code files)"
    if tool == "build_project_map":
        return f"Project map built: {len(result.get('files', {}))} files"
    if tool == "read_doc":
        found = "found" if result.get("exists", False) else "not found"
        return f"Read doc '{result.get('doc', '?')}': {found}"
    if tool == "current_workspace":
        return f"Current workspace: {result.get('root', '?')}"
    if tool == "verify_file":
        return f"Verified {result.get('path', '?')}: {'OK' if result.get('ok', False) else 'FAILED'}"
    return f"{tool} completed"


def _update_workspace_info(state: AgentLoopState, result: Dict[str, Any]) -> None:
    """Refresh the workspace summary carried in the state prompt."""
    if not result.get("ok"):
        return
    parts = [f"root={result.get('root', '')}"]
    if result.get("single_file_mode", False):
        parts.append("single_file_mode=true")
        if result.get("target_file", ""):
            parts.append(f"target_file={result['target_file']}")
    state.workspace_info = ", ".join(parts)


def _record_tool_failure(state: AgentLoopState, tool: str) -> None:
    """Count one consecutive failure of *tool* (arguments are ignored).

    The counter is per tool name; a failure of a different tool resets the
    others, and any success resets the tool's own counter. Reaching the
    threshold does NOT hard-stop the loop; guidance is appended to the next
    error message instead.
    """
    counts = getattr(state, "tool_fail_counts", None)
    if counts is None:
        counts = {}
        state.tool_fail_counts = counts
    counts[tool] = counts.get(tool, 0) + 1
    for other in list(counts.keys()):
        if other != tool:
            counts[other] = 0


def _tool_fail_guidance(state: AgentLoopState, tool: str) -> str:
    """Return switch-tool guidance once the tool hit the failure threshold."""
    counts = getattr(state, "tool_fail_counts", None) or {}
    if counts.get(tool, 0) < _MAX_CONSECUTIVE_ERRORS:
        return ""
    return (
        f" Tool '{tool}' has now failed {counts[tool]} times in a row "
        f"(even with different arguments). Stop trying to fix it with small "
        f"variations: switch to the next tool in the fallback chain or use "
        f"run_code for the operation, whichever is appropriate."
    )


def _mark_tool_failure(state: AgentLoopState, tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Record a failed tool call and fold switch-tool guidance into the result."""
    _record_tool_failure(state, tool)
    guidance = _tool_fail_guidance(state, tool)
    if guidance:
        result = dict(result)
        result["error"] = (str(result.get("error") or "The tool call failed.")) + guidance
    return result


def _reset_tool_failures(state: AgentLoopState) -> None:
    """Clear all per-tool failure counters after any successful tool call."""
    counts = getattr(state, "tool_fail_counts", None)
    if counts:
        counts.clear()


class _Batch:
    """The accumulated results of one batch, shared by the call handlers."""

    def __init__(self, state: AgentLoopState, runtime: LoopRuntime) -> None:
        self.state = state
        self.runtime = runtime
        self.parts: List[str] = []
        self.all_ok = True
        self.any_call_ok = False

    def append(self, result: Dict[str, Any]) -> None:
        """Add one tool result to the next user message."""
        self.parts.append(json.dumps({"tool_result": result}, ensure_ascii=False))

    def fail(self, tool: str, result: Dict[str, Any], *,
             signature: Optional[str] = None) -> Dict[str, Any]:
        """Record a failed call, remember its signature, and return the result."""
        self.all_ok = False
        if signature is not None:
            self.state.last_failed_signature = signature
        return _mark_tool_failure(self.state, tool, result)

    def succeed(self) -> None:
        """Record a successful call and clear the failure memory."""
        self.state.last_failed_signature = ""
        _reset_tool_failures(self.state)
        self.any_call_ok = True

    def emit_result(self, tool: str, result: Dict[str, Any]) -> None:
        """Report one tool result to the subscriber."""
        self.runtime.emit(self.state, {"type": "tool_result", "tool": tool,
                                      "result": result, "step": self.state.steps})


def _duplicate_refusal(tool: str) -> Dict[str, Any]:
    """The result of a call blocked for repeating an already-failed one."""
    return {
        "ok": False,
        "error": (
            f"The same {tool} call with the same arguments already failed. "
            "Do NOT repeat it. Switch to the next tool in the fallback chain."
        ),
        "duplicate_call_blocked": True,
    }


def _run_delegation(state: AgentLoopState, runtime: LoopRuntime) -> Dict[int, Dict[str, Any]]:
    """Run every delegation call of the batch as one parallel fan-out.

    The whole batch is validated first, then the valid calls run in parallel;
    handling them inside the per-call loop would serialise the children.
    """
    calls = [(index, call) for index, call in enumerate(state.parsed_calls)
             if (call.get("tool") or "") in _TREE_CALL_NAMES and not call.get("_dsml")]
    if not calls:
        return {}

    for _index, call in calls:
        runtime.emit(state, {"type": "phase", "phase": "executing",
                             "tool": call.get("tool"), "step": state.steps})
        runtime.emit(state, {"type": "tool_call", "tool": call.get("tool"),
                             "args": call.get("args") or {}, "step": state.steps})

    batch = execute_delegation_calls(
        [call for _index, call in calls], state=state,
        dispatcher=runtime.dispatcher, lang=runtime.lang, on_event=runtime.on_event)
    return {index: result for (index, _call), result in zip(calls, batch)}


def _run_write_tool(batch: _Batch, tool: str, call: Dict[str, Any]) -> bool:
    """Execute one write tool; return False when the run ended on a staged draft."""
    state = batch.state
    signature = _call_signature(tool, call.get("args"))
    if signature == state.last_failed_signature:
        result = _duplicate_refusal(tool)
        batch.all_ok = False
        batch.emit_result(tool, result)
        batch.append(result)
        return True

    batch.runtime.emit(state, {"type": "phase", "phase": "executing", "tool": tool, "step": state.steps})
    batch.runtime.emit(state, {"type": "tool_call", "tool": tool, "args": call["args"], "step": state.steps})

    args = dict(call["args"])
    if tool == "propose_file":
        if isinstance(args.get("content"), str):
            args["content"] = normalize_hyphens(args["content"])
    else:
        edits = args.get("edits") or []
        if isinstance(edits, list):
            args["edits"] = [
                {k: (normalize_hyphens(v) if k in ("old", "new") and isinstance(v, str) else v)
                 for k, v in edit.items()}
                if isinstance(edit, dict) else edit
                for edit in edits
            ]
    args.setdefault("auto_apply", state.auto_apply)

    result = batch.runtime.dispatcher.dispatch(tool, args)
    if not result.get("ok"):
        result = batch.fail(tool, result, signature=signature)
        batch.append(result)
        batch.emit_result(tool, result)
        return True

    if result.get("applied", False):
        batch.succeed()
        batch.runtime.emit(state, {"type": "applied", "tool": tool, "result": result,
                                   "path": result.get("path", ""), "step": state.steps})
        batch.append(result)
        return True

    path = result.get("path", "")
    diff = result.get("diff", "")
    batch.runtime.emit(state, {"type": "awaiting_approval", "path": path,
                               "diff": diff, "step": state.steps})
    state.phase = "done"
    state.final_status = "awaiting_approval"
    state.final_text = state.assistant_text
    state.final_staged_path = path
    state.final_diff = diff
    state.final_staged_new_text = result.get("new_text")
    state.final_staged_tool = tool
    return False


def _refuse_dangerous(batch: _Batch, tool: str, call: Dict[str, Any],
                      result: Dict[str, Any], signature: str) -> None:
    """Refuse a dangerous call of a child, which has no user to confirm it."""
    state = batch.state
    assessment = result.get("danger_assessment") or {}
    refusal = {
        "ok": False,
        "error": (
            "This operation needs a human confirmation, which "
            "a subagent cannot obtain. Rewrite it so it needs "
            "no confirmation (avoid destructive or system-level "
            "commands, or split off the part that needs one), "
            "or report the blocker in your result."
        ),
        "error_code": "confirmation_required",
        "confirmation_required": True,
        "danger_reasons": list(assessment.get("reasons", [])),
        "next_action": (
            "Do not retry the same call. Switch to a safe "
            "alternative or stop and report what was blocked."
        ),
    }
    state.last_failed_signature = signature
    refusal = _mark_tool_failure(state, tool, refusal)
    batch.emit_result(tool, refusal)
    batch.append(refusal)


def _park_for_confirmation(batch: _Batch, tool: str, call: Dict[str, Any],
                           result: Dict[str, Any]) -> None:
    """Stop the run and let the user allow or deny a dangerous operation."""
    state = batch.state
    assessment = result.get("danger_assessment") or {}
    reasons = assessment.get("reasons", [])
    code_snippet = assessment.get("code_snippet", "")
    batch.runtime.emit(state, {
        "type": "confirmation_required",
        "tool": tool,
        "args": call["args"],
        "reasons": reasons,
        "code_snippet": code_snippet,
        "step": state.steps,
    })
    state.phase = "done"
    state.final_status = "awaiting_confirmation"
    state.final_text = state.assistant_text
    state.pending_confirmation_tool = tool
    state.pending_confirmation_args = dict(call["args"])
    state.pending_confirmation_reasons = reasons
    state.pending_confirmation_code = code_snippet
    state.pending_confirmation_result = result


def _dispatch_result(batch: _Batch, tool: str, call: Dict[str, Any]) -> Dict[str, Any]:
    """Call the tool, turning a raised exception into a structured failure."""
    try:
        return batch.runtime.dispatcher.dispatch_json(call)
    except TypeError as exc:
        return {
            "ok": False,
            "error": (
                f"Tool '{tool}' was called with invalid arguments: {exc}. "
                "Use ONLY the documented arguments for this tool "
                "(from the system prompt) and re-issue the call."
            ),
            "invalid_arguments": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": (
                f"Tool '{tool}' failed with an internal error: "
                f"{type(exc).__name__}: {exc}. Read this cause, "
                "fix it and continue - the loop will not stop."
            ),
            "tool_raised_exception": True,
        }


def _run_generic_tool(batch: _Batch, tool: str, call: Dict[str, Any]) -> bool:
    """Execute one ordinary tool; return False when the run ended on a gate."""
    state = batch.state
    signature = _call_signature(tool, call.get("args"))
    if tool in _DUPLICATE_GUARD_TOOLS and signature == state.last_failed_signature:
        result = _duplicate_refusal(tool)
        batch.all_ok = False
        batch.emit_result(tool, result)
        batch.append(result)
        state.last_failed_signature = signature
        return True

    batch.runtime.emit(state, {"type": "phase", "phase": "executing", "tool": tool, "step": state.steps})
    batch.runtime.emit(state, {"type": "tool_call", "tool": tool, "args": call["args"], "step": state.steps})

    result = _dispatch_result(batch, tool, call)

    if result.get("confirmation_required"):
        if state.child_mode:
            _refuse_dangerous(batch, tool, call, result, signature)
            return True
        _park_for_confirmation(batch, tool, call, result)
        return False

    if not result.get("ok"):
        result = batch.fail(tool, result,
                            signature=signature if tool in _DUPLICATE_GUARD_TOOLS else None)
        result["next_action"] = (
            "Fix this ONCE, then switch to the next fallback tool. Never "
            "resend the same problematic call more than twice. Hard "
            "errors only for genuine blockers (manual-mode staging, "
            "task-direction conflict)."
        )
    else:
        batch.succeed()

    if tool in _WORKSPACE_TOOLS:
        _update_workspace_info(state, result)

    if tool in _READ_TOOLS:
        summary = _summarise_result(tool, result)
        batch.emit_result(tool, {**result, "summary": summary})
    else:
        batch.emit_result(tool, result)
    batch.append(result)
    return True


def _execute_call(batch: _Batch, index: int, call: Dict[str, Any],
                  delegation_results: Dict[int, Dict[str, Any]]) -> bool:
    """Execute one call of the batch; return False when the run ended."""
    state = batch.state
    tool = call["tool"]

    if index in delegation_results:
        # Produced by the fan-out phase; appended in call order so the model
        # pairs each result with its brief.
        result = delegation_results[index]
        if result.get("ok"):
            batch.succeed()
        else:
            result = batch.fail(tool, result)
        batch.emit_result(tool, result)
        batch.append(result)
        return True

    if call.get("_dsml"):
        # Validate a fallback-dialect call against the real signature BEFORE
        # dispatch; an invalid one is not executed and gets JSON guidance.
        dsml_error = _dsml_validation_error(tool, call.get("args") or {})
        if dsml_error is not None:
            result = {"ok": False, "error": dsml_error, "dsml_invalid": True}
            batch.all_ok = False
            batch.emit_result(tool, result)
            batch.append(result)
            return True

    if tool in ("propose_file", "apply_patch"):
        return _run_write_tool(batch, tool, call)

    if tool in _USER_GATED and not state.auto_apply:
        result = {
            "ok": False,
            "error": (
                f"In manual mode you must NOT call {tool} yourself. "
                "After propose_file, stop and let the user click "
                "\u2705 (apply) or \u274c (discard). Do not narrate the edit as 'applied'."
            ),
        }
        batch.all_ok = False
        batch.emit_result(tool, result)
        batch.append(result)
        return True

    return _run_generic_tool(batch, tool, call)


def _finish_batch(batch: _Batch, dsml_in_step: bool) -> AgentLoopState:
    """Close the batch: counters, reminders, the next user message, parking."""
    state = batch.state
    runtime = batch.runtime

    if batch.all_ok:
        state.consecutive_errors = 0
    if batch.any_call_ok and state.child_mode:
        # The continue budget stops a child that SPINS on questions, not one
        # that WORKS: a step that executed real calls is progress, so the
        # budget is refilled.
        state.auto_continue_count = 0

    # A successfully executed fallback-dialect call still means the step used
    # DSML; send the one-line contract reminder once per step.
    if dsml_in_step and batch.any_call_ok:
        notice = {"ok": True, "notice": _DSML_STOP_NOTICE}
        runtime.emit(state, {"type": "tool_result", "tool": "parse_tool_calls",
                             "result": notice, "step": state.steps})
        batch.append(notice)

    # Report the blocks of this batch that failed to parse next to the results
    # of the calls that DID execute (one-shot, then cleared).
    if state.partial_parse_warning is not None:
        warning = state.partial_parse_warning
        state.partial_parse_warning = None
        runtime.emit(state, {"type": "tool_result", "tool": "parse_tool_calls",
                             "result": warning, "step": state.steps})
        batch.append(warning)

    state.user_message = "\n".join(batch.parts)

    # The run asked its parent something and the answer arrives through a
    # LATER call, so another step here would keep working on a question the
    # model already declared blocking. The run keeps its record, its journal
    # and its step budget; the answer continues it from this point.
    if state.parked_question:
        runtime.emit(state, {"type": "final", "text": state.assistant_text,
                             "status": "awaiting_question", "step": state.steps})
        state.phase = "done"
        state.final_status = "awaiting_question"
        state.final_text = state.assistant_text
        return state

    state.phase = "calling_llm"
    return state


def execute_step(state: AgentLoopState, runtime: LoopRuntime) -> AgentLoopState:
    """Execute every call of the batch and leave the state ready for the next one."""
    batch = _Batch(state, runtime)
    dsml_in_step = (any(call.get("_dsml") for call in state.parsed_calls)
                    or _uses_dsml_markup(state.assistant_text))

    delegation_results = _run_delegation(state, runtime)

    for index, call in enumerate(state.parsed_calls):
        if not _execute_call(batch, index, call, delegation_results):
            return state

    return _finish_batch(batch, dsml_in_step)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
