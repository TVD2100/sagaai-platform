# -*- coding: utf-8 -*-
"""tests/test_system_prompt_subagents.py - the subagent rules of the shipped prompt.

The prompt is the only place where the delegation contract is explained to the
model, so a silently dropped rule turns into a runtime failure that no code
test can catch: the model simply stops checking its children. These tests pin
the version header and each rule by a phrase that carries its meaning.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "dev_agent" / "system_prompt.md"


def _text() -> str:
    """Return the shipped system prompt."""
    return PROMPT.read_text(encoding="utf-8")


def test_header_carries_the_version():
    assert _text().startswith("# DevAgent - System Prompt (v3.26)")


def test_a_subagent_does_not_ask_for_plan_approval():
    """Nobody can approve a plan inside a child run - it must not wait."""
    text = _text()

    assert "Subagent exception to the approval gate" in text
    assert "`parent_run_id` is not empty" in text
    assert "task_state_init" in text


def test_the_exception_keeps_the_other_gates():
    """Autonomy inside a child covers the plan gate only, not every gate."""
    text = _text()
    start = text.index("Subagent exception to the approval gate")
    block = text[start:start + 1200]

    assert "manual-mode staging" in block
    assert "dangerous-command confirmations" in block
    assert "§15" in block


def test_a_subagent_never_waits_for_a_confirmation():
    """A blocked child must rewrite or report, never retry the same call."""
    text = _text()

    assert "A subagent never waits for a confirmation" in text
    assert "confirmation_required" in text
    assert "pending" in text


def test_the_tools_section_documents_background_runs():
    """Background launch, the notice marker and the status check."""
    text = _text()

    assert "**Background subagents.**" in text
    assert "`spawn_agent(..., background=true)`" in text
    assert "[SUBAGENT_NOTICE:<run_id>:<status>]" in text
    assert "subagent_status" in text


def test_unfinished_states_are_named_and_resumed():
    """Stopping states must be spelled out, each paired with a resume path."""
    text = _text()

    for state in ("awaiting_input", "error", "timeout", "max_steps"):
        assert state in text, state
    assert "read_subagent_state(run_id=...)" in text
    assert "resume_subagent(run_id=..., request={" in text
    assert "omit `request`" in text


def test_the_resume_chain_is_capped():
    """Two resumes, then escalation - the third is refused by the limit."""
    text = _text()

    assert "After two resumes" in text
    assert "chain limit" in text


def test_claims_about_earlier_dialogs_need_a_search():
    """Memory alone is not evidence, and a blocked payload is not retried."""
    text = _text()

    assert "**Claims about earlier dialogs.**" in text
    assert "search_in_threads" in text
    assert "sanitized_required" in text
    assert "switch to another tool" in text


def test_the_approval_gate_itself_is_unchanged_for_top_level_dialogs():
    """The exception must not weaken the gate the user relies on."""
    text = _text()
    start = text.index("**CRITICAL - approval gate:**")
    block = text[start:start + 1600]

    assert "MUST NOT write, edit, or modify any file" in block
    assert "Stage 1 item 3" in block
    assert "loop_status" in block


def test_the_prompt_stays_one_managed_file():
    """The shipped prompt is the single source of truth for the built-in agent."""
    assert PROMPT.is_file()
    assert PROMPT.stat().st_size > 60_000
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
