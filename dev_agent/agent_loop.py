# -*- coding: utf-8 -*-
"""
dev_agent.agent_loop -- provider-independent orchestrator loop.

Implements the working cycle: send_request -> parse_tool_calls
-> dispatch -> collect results -> repeat until propose_file/prose/max_steps.

No assistant detection phase (no detect_and_select_assistant auto-call).
Assistant creation (create_assistant_for_task) is handled by the LLM in the normal
calling_llm cycle -- no separate task_classification phase.

Supports two execution modes:
  * run_agent_loop() -- blocking, runs all steps in one call (legacy).
  * step_agent_loop() -- single-step, returns after one iteration.
    The UI calls it repeatedly with AgentLoopState stored in session_state.

Single-model routing:
  One main model serves every step. step_agent_loop() reads the assistant
  dict from ``strong_assistant``; the legacy ``weak_assistant`` field is
  kept for backward compatibility and mirrors the main assistant.
  classify_step_strength() remains a public helper for step bookkeeping
  (it no longer switches models).

Economy mode:
  When enabled, only the last L messages are sent to the model together
  with a compact metadata message (workspace, web-search flag, history
  counts). No "important" messages and no full history index are injected.
  The model can request older messages on demand via the
  get_history_index / get_history_messages tools, which are backed by the
  full history stored in the dispatcher's core via set_history().

  The tail length L is read from the orchestrator configuration at
  call time (via core.orchestrators.get_economy_tail_messages).

  Cache-friendly economy mode:
    When economy_cache_enabled is True and economy_cache_multiplier > 1,
    the beginning of the sent window is kept stable while it fits within
    L * multiplier messages, so providers can apply prefix caching. See
    build_economy_context() for the full anchor-shifting logic.

Token tracking:
  Each send_request call collects usage via a usage_callback. Tokens are
  accumulated on the last assistant message in history as ``_tokens``
  dict {"in": prompt_tokens, "out": completion_tokens,
        "cache": cached_input_tokens}.
  The loop state additionally keeps running totals in total_tokens_in,
  total_tokens_out and total_tokens_cache.

Dangerous-operation confirmation:
  When a tool (run_code, run_test) is about to execute potentially
  dangerous code, the executor returns {confirmation_required: True}
  with a human-readable reason.  step_agent_loop() then stops and sets
  final_status="awaiting_confirmation", storing the pending call.
  The UI renders Allow/Deny buttons; on Allow it calls
  approve_confirmation(), which re-dispatches the tool with
  confirmed_by_user=True.

Sanitization confirmation:
  When prompt-injection protection replaces a tool-result payload with
  [SANITIZED], step_agent_loop() automatically stops and sets
  final_status="sanitized_required". The UI shows the reasons and offers
  "Allow viewing" (one-step bypass via approve_sanitized_content) or
  "Deny" (continue without the sanitized content).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from core.api_layer import send_request
from dev_agent.loop.context import _CATEGORY_ERROR as _CATEGORY_ERROR
from dev_agent.loop.context import _CATEGORY_OTHER as _CATEGORY_OTHER
from dev_agent.loop.context import _CATEGORY_PLAN as _CATEGORY_PLAN
from dev_agent.loop.context import _CATEGORY_PROGRESS as _CATEGORY_PROGRESS
from dev_agent.loop.context import _CATEGORY_SUMMARY as _CATEGORY_SUMMARY
from dev_agent.loop.context import _CATEGORY_TOOL_CALL as _CATEGORY_TOOL_CALL
from dev_agent.loop.context import _CATEGORY_TOOL_RESULT as _CATEGORY_TOOL_RESULT
from dev_agent.loop.context import _CATEGORY_USER_TASK as _CATEGORY_USER_TASK
from dev_agent.loop.context import _SNAPSHOT_SUPERSEDE_NOTE as _SNAPSHOT_SUPERSEDE_NOTE
from dev_agent.loop.context import _SNAPSHOT_TC as _SNAPSHOT_TC
from dev_agent.loop.context import _SNAPSHOT_TC_HEAD as _SNAPSHOT_TC_HEAD
from dev_agent.loop.context import _SNAPSHOT_TS as _SNAPSHOT_TS
from dev_agent.loop.context import _SNAPSHOT_TS_HEAD as _SNAPSHOT_TS_HEAD
from dev_agent.loop.context import _append_context_snapshot as _append_context_snapshot
from dev_agent.loop.context import _classify_message as _classify_message
from dev_agent.loop.context import _economy_meta_key as _economy_meta_key
from dev_agent.loop.context import _enforce_history_token_budget as _enforce_history_token_budget
from dev_agent.loop.context import _ensure_snapshot_visibility as _ensure_snapshot_visibility
from dev_agent.loop.context import _estimate_messages_tokens as _estimate_messages_tokens
from dev_agent.loop.context import _get_economy_tail_messages as _get_economy_tail_messages
from dev_agent.loop.context import _index_message as _index_message
from dev_agent.loop.context import _last_snapshot as _last_snapshot
from dev_agent.loop.context import _make_meta_msg as _make_meta_msg
from dev_agent.loop.context import _make_short_summary as _make_short_summary
from dev_agent.loop.context import _maybe_task_state_context as _maybe_task_state_context
from dev_agent.loop.context import _maybe_thread_context as _maybe_thread_context
from dev_agent.loop.context import _snapshot_content as _snapshot_content
from dev_agent.loop.context import _snapshot_kind_of as _snapshot_kind_of
from dev_agent.loop.context import apply_economy_cache as apply_economy_cache
from dev_agent.loop.context import build_economy_context as build_economy_context
from dev_agent.loop.context import carry_over_economy_cache as carry_over_economy_cache
from dev_agent.loop.context import economy_cache_to_dict as economy_cache_to_dict
from dev_agent.loop.context import refresh_context_snapshots as refresh_context_snapshots
from dev_agent.loop.delegation import _DELEGATION_CALL_NAMES as _DELEGATION_CALL_NAMES
from dev_agent.loop.delegation import _TREE_CALL_NAMES as _TREE_CALL_NAMES
from dev_agent.loop.delegation import _agent_brief_from_args as _agent_brief_from_args
from dev_agent.loop.delegation import _delegation_base_dir as _delegation_base_dir
from dev_agent.loop.delegation import _delegation_job as _delegation_job
from dev_agent.loop.delegation import _delegation_parent_context as _delegation_parent_context
from dev_agent.loop.delegation import execute_delegation_calls as execute_delegation_calls
from dev_agent.loop.notices import NOTICE_PREFIX as NOTICE_PREFIX
from dev_agent.loop.notices import drain_subagent_notices as drain_subagent_notices
from dev_agent.loop.notices import format_subagent_notice as format_subagent_notice
from dev_agent.loop.parse import _DSML_ARGS_WRAPPERS as _DSML_ARGS_WRAPPERS
from dev_agent.loop.parse import _DSML_KWARGS_REQUIRED as _DSML_KWARGS_REQUIRED
from dev_agent.loop.parse import _DSML_MARK as _DSML_MARK
from dev_agent.loop.parse import _DSML_ONEOF as _DSML_ONEOF
from dev_agent.loop.parse import _DSML_STOP_NOTICE as _DSML_STOP_NOTICE
from dev_agent.loop.parse import _DSML_USAGE_RE as _DSML_USAGE_RE
from dev_agent.loop.parse import _DUPLICATE_GUARD_TOOLS as _DUPLICATE_GUARD_TOOLS
from dev_agent.loop.parse import _JSON_FENCE_RE as _JSON_FENCE_RE
from dev_agent.loop.parse import _call_signature as _call_signature
from dev_agent.loop.parse import _cascade_recover_tool_calls as _cascade_recover_tool_calls
from dev_agent.loop.parse import _coerce_dsml_param as _coerce_dsml_param
from dev_agent.loop.parse import _collapse_value_escapes as _collapse_value_escapes
from dev_agent.loop.parse import _dsml_json_hint as _dsml_json_hint
from dev_agent.loop.parse import _dsml_required_args as _dsml_required_args
from dev_agent.loop.parse import _dsml_validation_error as _dsml_validation_error
from dev_agent.loop.parse import _escape_raw_newlines_in_strings as _escape_raw_newlines_in_strings
from dev_agent.loop.parse import _extract_balanced_json_objects as _extract_balanced_json_objects
from dev_agent.loop.parse import _extract_dsml_calls as _extract_dsml_calls
from dev_agent.loop.parse import _fallback_parse_propose_file as _fallback_parse_propose_file
from dev_agent.loop.parse import _json_loads_lenient as _json_loads_lenient
from dev_agent.loop.parse import _json_parse_cause as _json_parse_cause
from dev_agent.loop.parse import _json_repair_cascade as _json_repair_cascade
from dev_agent.loop.parse import _normalize_call as _normalize_call
from dev_agent.loop.parse import _parse_dsml_json_value as _parse_dsml_json_value
from dev_agent.loop.parse import _repair_stray_bracket as _repair_stray_bracket
from dev_agent.loop.parse import _repair_unclosed_braces as _repair_unclosed_braces
from dev_agent.loop.parse import _repair_unclosed_tool_json as _repair_unclosed_tool_json
from dev_agent.loop.parse import _truncated_tool_json_segments as _truncated_tool_json_segments
from dev_agent.loop.parse import _try_layer_parse_chain as _try_layer_parse_chain
from dev_agent.loop.parse import _unbalanced_json_details as _unbalanced_json_details
from dev_agent.loop.parse import _unclosed_summary as _unclosed_summary
from dev_agent.loop.parse import _unescape_json_string_body as _unescape_json_string_body
from dev_agent.loop.parse import _unparsed_block_signature as _unparsed_block_signature
from dev_agent.loop.parse import _unparsed_tool_json_blocks as _unparsed_tool_json_blocks
from dev_agent.loop.parse import _unparsed_tool_json_diagnostics as _unparsed_tool_json_diagnostics
from dev_agent.loop.parse import _unwrap_dsml_args as _unwrap_dsml_args
from dev_agent.loop.parse import _uses_dsml_markup as _uses_dsml_markup
from dev_agent.loop.parse import parse_tool_calls as parse_tool_calls
from dev_agent.loop.results import _TOOL_RESULT_BULK_KEYS as _TOOL_RESULT_BULK_KEYS
from dev_agent.loop.results import _TOOL_RESULT_INLINE_LIMIT as _TOOL_RESULT_INLINE_LIMIT
from dev_agent.loop.results import _TOOL_RESULT_PREFIX as _TOOL_RESULT_PREFIX
from dev_agent.loop.results import _TOOL_RESULT_PREVIEW_SHRINK as _TOOL_RESULT_PREVIEW_SHRINK
from dev_agent.loop.results import _TOOL_RESULT_PREVIEW_SIDE as _TOOL_RESULT_PREVIEW_SIDE
from dev_agent.loop.results import _TOOL_RESULT_SPILL_KEEP as _TOOL_RESULT_SPILL_KEEP
from dev_agent.loop.results import _TOOL_RESULT_STORAGE_FALLBACK_LIMIT as _TOOL_RESULT_STORAGE_FALLBACK_LIMIT
from dev_agent.loop.results import _TOOL_RESULT_STORAGE_FIELD_LIMIT as _TOOL_RESULT_STORAGE_FIELD_LIMIT
from dev_agent.loop.results import _TOOL_RESULT_STORAGE_KEEP_LIMIT as _TOOL_RESULT_STORAGE_KEEP_LIMIT
from dev_agent.loop.results import MAX_TOOL_RESULT_CHARS as MAX_TOOL_RESULT_CHARS
from dev_agent.loop.results import _apply_tool_result_cap as _apply_tool_result_cap
from dev_agent.loop.results import _build_tool_result_preview as _build_tool_result_preview
from dev_agent.loop.results import _compact_result_fields as _compact_result_fields
from dev_agent.loop.results import _rotate_tool_result_spills as _rotate_tool_result_spills
from dev_agent.loop.results import _spill_tool_result_doc as _spill_tool_result_doc
from dev_agent.loop.results import _summarize_single_tool_result_doc as _summarize_single_tool_result_doc
from dev_agent.loop.results import _tool_results_dir as _tool_results_dir
from dev_agent.loop.results import summarize_tool_result_for_storage as summarize_tool_result_for_storage
from dev_agent.loop.signals import _BULLET_ITEM_RE as _BULLET_ITEM_RE
from dev_agent.loop.signals import _CONFIRMATION_REQUEST_NO_Q_PATTERNS as _CONFIRMATION_REQUEST_NO_Q_PATTERNS
from dev_agent.loop.signals import _CONFIRMATION_REQUEST_PATTERNS as _CONFIRMATION_REQUEST_PATTERNS
from dev_agent.loop.signals import _FINAL_PHRASE as _FINAL_PHRASE
from dev_agent.loop.signals import _LOOP_STATUS_RE as _LOOP_STATUS_RE
from dev_agent.loop.signals import _NON_ASCII_HYPHENS as _NON_ASCII_HYPHENS
from dev_agent.loop.signals import _NUMBERED_ITEM_SOFT_RE as _NUMBERED_ITEM_SOFT_RE
from dev_agent.loop.signals import _NUMBERED_ITEM_STRICT_RE as _NUMBERED_ITEM_STRICT_RE
from dev_agent.loop.signals import _PLAN_WORD_RE as _PLAN_WORD_RE
from dev_agent.loop.signals import _PROGRESS_PATTERNS as _PROGRESS_PATTERNS
from dev_agent.loop.signals import _REQUIRES_USER_RESPONSE_RE as _REQUIRES_USER_RESPONSE_RE
from dev_agent.loop.signals import _STRONG_PROSE_PATTERNS as _STRONG_PROSE_PATTERNS
from dev_agent.loop.signals import _STRONG_TOOLS as _STRONG_TOOLS
from dev_agent.loop.signals import _WEAK_PROSE_PATTERNS as _WEAK_PROSE_PATTERNS
from dev_agent.loop.signals import _WEAK_TOOLS as _WEAK_TOOLS
from dev_agent.loop.signals import _looks_like_confirmation_request as _looks_like_confirmation_request
from dev_agent.loop.signals import _looks_like_plan as _looks_like_plan
from dev_agent.loop.signals import _parse_loop_status as _parse_loop_status
from dev_agent.loop.signals import _parse_requires_user_response as _parse_requires_user_response
from dev_agent.loop.signals import _prose_contains_progress as _prose_contains_progress
from dev_agent.loop.signals import _prose_looks_like_question as _prose_looks_like_question
from dev_agent.loop.signals import _prose_looks_weak as _prose_looks_weak
from dev_agent.loop.signals import classify_step_strength as classify_step_strength
from dev_agent.loop.signals import normalize_hyphens as normalize_hyphens
from dev_agent.loop.state import AgentLoopState, AgentResult
from dev_agent.loop.steps.approvals import approve_pending_confirmation as approve_pending_confirmation
from dev_agent.loop.steps.approvals import approve_sanitized_content as approve_sanitized_content
from dev_agent.loop.steps.approvals import deny_pending_confirmation as deny_pending_confirmation
from dev_agent.loop.steps.approvals import deny_sanitized_content as deny_sanitized_content
from dev_agent.loop.steps.executing import _MAX_CONSECUTIVE_ERRORS as _MAX_CONSECUTIVE_ERRORS
from dev_agent.loop.steps.executing import _WORKSPACE_TOOLS as _WORKSPACE_TOOLS
from dev_agent.loop.steps.executing import _mark_tool_failure as _mark_tool_failure
from dev_agent.loop.steps.executing import _record_tool_failure as _record_tool_failure
from dev_agent.loop.steps.executing import _reset_tool_failures as _reset_tool_failures
from dev_agent.loop.steps.executing import _summarise_result as _summarise_result
from dev_agent.loop.steps.executing import _tool_fail_guidance as _tool_fail_guidance
from dev_agent.loop.steps.executing import _update_workspace_info as _update_workspace_info
from dev_agent.loop.steps.models import AUTO_CONTINUE_PREFIX as AUTO_CONTINUE_PREFIX
from dev_agent.loop.steps.parsing import _MAX_DIAGNOSTIC_ENTRIES as _MAX_DIAGNOSTIC_ENTRIES
from dev_agent.loop.steps.parsing import _MAX_DUPLICATE_BLOCKS as _MAX_DUPLICATE_BLOCKS
from dev_agent.loop.steps.parsing import MAX_CHILD_AUTO_CONTINUES as MAX_CHILD_AUTO_CONTINUES
from dev_agent.loop.steps.parsing import _dedupe_diagnostics as _dedupe_diagnostics
from dev_agent.loop.steps.parsing import _dominant_duplicate_call as _dominant_duplicate_call
from dev_agent.loop.steps.parsing import _identical_block_count as _identical_block_count
from dev_agent.loop.steps.prepare import now_ts as _ts_now
from dev_agent.loop.steps.runner import step_agent_loop_impl as _run_step_impl
from storage.models import DEFAULT_MAX_STEPS

log = logging.getLogger("dev_agent.agent_loop")


def _now_ts() -> str:
    """Return the current local timestamp for live history entries."""
    return _ts_now()


_AUTO_CONTINUE_PREFIX = "AUTO_CONTINUE:"

# Economy budget hysteresis: once the estimate exceeds the budget it is
# cut down to this ratio of it, so the window front moves rarely.
_ECONOMY_TRIM_LOW_RATIO: float = 0.75

# Pad kept below the guard cap so its stateless trim stays unused.
_ECONOMY_GUARD_MARGIN: int = 2048


_READ_TOOLS = {
    "read_file", "list_files", "scan_folder", "assess_workspace",
    "build_project_map", "read_doc", "current_workspace", "verify_file",
}



def step_agent_loop(
    state: AgentLoopState,
    *,
    dispatcher: Any,
    lang: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> AgentLoopState:
    """Execute one iteration of the agent loop."""
    if hasattr(dispatcher, 'core') and hasattr(dispatcher.core, '_in_agent_loop'):
        dispatcher.core._in_agent_loop = True
    try:
        return _step_agent_loop_impl(state, dispatcher=dispatcher, lang=lang, on_event=on_event)
    finally:
        if hasattr(dispatcher, 'core') and hasattr(dispatcher.core, '_in_agent_loop'):
            dispatcher.core._in_agent_loop = False


def _step_agent_loop_impl(
    state: AgentLoopState,
    *,
    dispatcher: Any,
    lang: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> AgentLoopState:
    """Execute one phase of the loop through the steps package.

    The two callables a test may replace on THIS module are looked up here, at
    call time, and handed to the step: patching ``agent_loop.send_request`` or
    ``agent_loop.drain_subagent_notices`` therefore stays effective without the
    steps importing the facade.
    """
    return _run_step_impl(
        state,
        dispatcher=dispatcher,
        lang=lang,
        on_event=on_event,
        send_request=send_request,
        drain_notices=drain_subagent_notices,
    )


# run_agent_loop (legacy blocking wrapper)


def run_agent_loop(
    task: str,
    assistant: dict,
    dispatcher: Any,
    *,
    on_event: Optional[Callable[[dict], None]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    auto_apply: bool = False,
    lang: Optional[str] = None,
    pending_staged_path: Optional[str] = None,
    pending_action: Optional[str] = None,
    staged_new_text: Optional[str] = None,
    staged_tool: Optional[str] = None,
    file_context: str = "",
    file_name: str = "",
    thread_id: str = "",
    run_id: str = "",
    parent_run_id: str = "",
    depth: int = 0,
) -> AgentResult:
    """Run the agent loop until it stops, and return the structured result.

    The last four keyword arguments carry the delegation identity: a root
    dialog passes ``thread_id`` and lets the orchestration service fill the
    run fields, while a child run passes all four so cascade cancellation,
    event delivery and the feed tree know where the run belongs.
    """
    base_history = history if history is not None else []
    # Normalise the resumed history: _index/_category are runtime-only and
    # are NOT persisted, so after a UI round-trip the entries come back
    # without them. Re-number by position so appended messages (and
    # get_history_index / get_history_messages) never desync from the list.
    for i, msg in enumerate(base_history):
        if not isinstance(msg, dict):
            continue
        if "_index" not in msg:
            msg["_index"] = i
        if "_category" not in msg:
            msg["_category"] = _classify_message(msg)

    state = AgentLoopState(
        task=task,
        max_steps=max_steps,
        auto_apply=auto_apply,
        strong_assistant=assistant,
        weak_assistant=assistant,
        history=base_history,
        next_index=len(base_history),
        file_context=file_context,
        file_name=file_name,
        pending_action=pending_action,
        pending_staged_path=pending_staged_path,
        staged_new_text=staged_new_text,
        staged_tool=staged_tool,
        thread_id=thread_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        depth=depth,
    )

    while state.phase not in ("done", "error"):
        state = step_agent_loop(state, dispatcher=dispatcher, lang=lang, on_event=on_event)
        if state.final_status == "awaiting_confirmation":
            break
        if state.final_status == "sanitized_required":
            break

    if state.phase == "error":
        return AgentResult(
            status="error",
            text=state.error_message,
            history=state.history,
            steps=state.steps,
            error_code=state.error_code or "provider_error",
            error_message=state.error_message,
        )

    return AgentResult(
        status=state.final_status,
        text=state.final_text,
        history=state.history,
        staged_path=state.final_staged_path,
        diff=state.final_diff,
        steps=state.steps,
        staged_new_text=state.final_staged_new_text,
        staged_tool=state.final_staged_tool,
        applied=state.final_applied,
        discarded=state.final_discarded,
        # A pending dangerous operation must survive the loop: the UI renders
        # Allow/Deny from the result, and the service maps this status onto
        # ``awaiting_confirmation`` without reaching into the state object.
        pending_confirmation_tool=state.pending_confirmation_tool,
        pending_confirmation_args=state.pending_confirmation_args,
        pending_confirmation_reasons=list(state.pending_confirmation_reasons or []),
        pending_confirmation_code=state.pending_confirmation_code or "",
        pending_confirmation_result=state.pending_confirmation_result,
        error_code=state.error_code,
        error_message=state.error_message or "",
    )


def approve_and_apply(path: str, dispatcher: Any, note: str = "") -> Dict[str, Any]:
    return dispatcher.dispatch("apply_edit", {"path": path, "note": note})


def discard(path: str, dispatcher: Any) -> Dict[str, Any]:
    return dispatcher.dispatch("discard_edit", {"path": path})
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
