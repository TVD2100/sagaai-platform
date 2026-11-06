# -*- coding: utf-8 -*-
"""
dev_agent.loop.state -- the state object of one agent-loop run.

``AgentLoopState`` is the mutable record the UI keeps in ``session_state``
between single-step calls, and ``AgentResult`` is the value a finished run
returns to its caller. Both live here so the rest of the loop package can
import them without pulling in the whole facade module.

The fields carry the loop's behaviour, not just data, so the comments below
are part of the contract: a field that changes a decision of the loop says
which decision it changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from storage.models import DEFAULT_MAX_STEPS


@dataclass
class AgentResult:
    """Outcome of a finished (or paused) agent-loop run."""

    status: str = ""
    text: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    staged_path: Optional[str] = None
    diff: Optional[str] = None
    steps: int = 0
    staged_new_text: Optional[str] = None
    staged_tool: Optional[str] = None
    applied: bool = False
    discarded: bool = False
    # Pending dangerous-operation confirmation, carried out of the loop so the
    # UI can render Allow/Deny without reaching into the state object. The
    # reasons list defaults to a fresh list (never None) so callers can iterate
    # it unconditionally.
    pending_confirmation_tool: Optional[str] = None
    pending_confirmation_args: Optional[Dict[str, Any]] = None
    pending_confirmation_reasons: List[str] = field(default_factory=list)
    pending_confirmation_code: str = ""
    pending_confirmation_result: Optional[Dict[str, Any]] = None
    # Machine-readable failure cause and its human-readable counterpart.
    # The delegation contract maps these onto status/stop_reason/error_code
    # instead of re-deriving a cause from free text.
    error_code: Optional[str] = None
    error_message: str = ""


@dataclass
class AgentLoopState:
    """Mutable state of a running agent loop."""

    phase: str = "init"

    task: str = ""
    max_steps: int = DEFAULT_MAX_STEPS
    auto_apply: bool = False

    strong_assistant: Dict[str, Any] = field(default_factory=dict)
    # Legacy alias kept for backward compatibility; mirrors strong_assistant
    # (single-model mode) and is no longer used for routing.
    weak_assistant: Dict[str, Any] = field(default_factory=dict)

    economy_mode: bool = False
    web_search_enabled: bool = False
    next_index: int = 0

    workspace_info: str = ""  # Cached workspace info for economy mode
    # Active thread id (a new thread is created by the UI per dialog).
    # When set, the thread context (thread_id + thread_files_dir) is
    # injected into the LLM context on every step.
    thread_id: str = ""

    # ── Delegation identity ────────────────────────────────────────────────
    # run_id identifies THIS loop in the run registry; parent_run_id is set
    # for a child run and drives cascade cancellation and event delivery;
    # depth is the distance from the root run ("О-2" starts at 1). All three
    # are empty/zero for a plain dialog that never delegates.
    run_id: str = ""
    parent_run_id: str = ""
    depth: int = 0

    # ── Child autonomy ─────────────────────────────────────────────────────
    # A delegated child has no interactive user: nobody will click Allow and
    # nobody will answer a question. ``child_mode`` therefore changes two
    # decisions of the loop - a confirmation request becomes a refusal the
    # child must rewrite, and an ``awaiting_user`` outcome becomes an
    # automatic continuation. ``auto_continue_count`` caps that autonomy so a
    # child that keeps asking cannot spin forever.
    child_mode: bool = False
    auto_continue_count: int = 0

    # ── Agent dialog (ask the parent / answer a child) ─────────────────────
    # ``dialog_limits`` carries the question/answer limits frozen at spawn
    # time: the loop asks the service to park the run, and the service needs
    # those numbers to refuse a question that is too long or one round too
    # many. ``parked_question`` holds the question this step parked on, so the
    # runner can report it without re-reading the record.
    dialog_limits: Dict[str, Any] = field(default_factory=dict)
    parked_question: Optional[Dict[str, Any]] = None

    # ── Subagent notices ───────────────────────────────────────────────────
    # ``root_run_id`` identifies the tree whose mailbox this loop drains;
    # it defaults to the run's own id, which is what a top-level dialog uses.
    # ``subagent_notices`` accumulates the notice lines already delivered, so
    # the UI can render them after the step and a resume can replay them.
    root_run_id: str = ""
    subagent_notices: List[str] = field(default_factory=list)

    # Cache-friendly economy mode settings.
    economy_cache_enabled: bool = False
    economy_cache_multiplier: int = 1
    # Explicit economy tail length for THIS orchestrator (per-state override;
    # None falls back to the orchestrator config read at call time).
    economy_tail_messages: Optional[int] = None
    economy_anchor: Optional[int] = None
    economy_meta_key: str = ""

    steps: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)
    user_message: str = ""
    file_context: str = ""
    file_name: str = ""

    pending_staged_path: Optional[str] = None
    pending_action: Optional[str] = None
    staged_new_text: Optional[str] = None
    staged_tool: Optional[str] = None

    assistant_text: str = ""
    parsed_calls: List[Dict[str, Any]] = field(default_factory=list)

    # Partial-batch warning: fenced tool-call blocks that failed to parse
    # while OTHER blocks of the same message parsed and were executed.
    # Appended to the executing phase's tool results, then cleared.
    partial_parse_warning: Optional[Dict[str, Any]] = None

    final_status: str = ""
    final_text: str = ""
    final_staged_path: Optional[str] = None
    final_diff: Optional[str] = None
    final_staged_new_text: Optional[str] = None
    final_staged_tool: Optional[str] = None
    final_applied: bool = False
    final_discarded: bool = False

    error_message: str = ""

    consecutive_errors: int = 0

    # Consecutive failures of the SAME tool, regardless of call arguments.
    # Distinct failed variants still accumulate here (a stuck tool is a stuck
    # tool); after _MAX_CONSECUTIVE_ERRORS failures an explicit "switch tool"
    # recommendation is appended to the error feedback. Reset on success.
    tool_fail_counts: Dict[str, int] = field(default_factory=dict)

    # Consecutive IDENTICAL unparsed tool-call JSON signatures. Varied
    # broken attempts never accumulate here; only literal repeats count.
    unparsed_repeat_count: int = 0

    # Fingerprint of the last unparsed tool-call JSON block; an IDENTICAL
    # repeat yields a stronger warning and, after _MAX_CONSECUTIVE_ERRORS
    # repeats, a hard stop (manual intervention).
    last_unparsed_signature: str = ""
    # Last dispatched (tool, stripped args-JSON). Used to detect the model
    # re-sending the SAME tool call after a failure; identical repeats are
    # blocked before dispatch, with guidance to switch down the fallback
    # chain (no hard stop). Cleared after any successful call.
    last_failed_signature: str = ""

    # Flag to remember that we just presented a plan and should not auto-continue.
    # Set by the parsing phase when loop_status=awaiting_user + plan detected.
    plan_presented: bool = False

    # Pending dangerous-operation confirmation.
    # When a tool returns confirmation_required=True, the loop stops and
    # stores the call here so the UI can render Allow/Deny buttons.
    pending_confirmation_tool: Optional[str] = None
    pending_confirmation_args: Optional[Dict[str, Any]] = None
    pending_confirmation_reasons: List[str] = field(default_factory=list)
    pending_confirmation_code: str = ""
    pending_confirmation_result: Optional[Dict[str, Any]] = None

    # Machine-readable failure cause for the delegation contract. ``error``
    # phase text stays human-facing; ``error_code`` lets the service map the
    # outcome onto stop_reason/error_code without parsing prose.
    error_code: Optional[str] = None
    error_message: str = ""

    # Pending sanitization confirmation.
    # When prompt-injection protection substitutes a tool-result payload
    # with [SANITIZED], the loop stops and these fields hold the details.
    sanitized_events: List[Dict[str, Any]] = field(default_factory=list)
    enable_injection_protection: bool = True
    injection_protection_bypassed: bool = False
    # File paths the user explicitly approved for viewing. _protect_history
    # keeps tool results for these paths visible on subsequent protected
    # turns instead of asking for approval again.
    sanitized_approved_paths: Set[str] = field(default_factory=set)

    # ── Token tracking (cumulative across all LLM calls in this loop) ───────
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tokens_cache: int = 0
    # Per-step tokens tracked via callback; attached to assistant message.
    # Dict keys: "in", "out", "cache" (the latter may be absent for providers
    # that do not report cached input tokens).
    last_step_tokens: Optional[Dict[str, int]] = None


__all__ = ["AgentLoopState", "AgentResult"]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
