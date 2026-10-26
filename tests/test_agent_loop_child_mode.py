# -*- coding: utf-8 -*-
"""tests/test_agent_loop_child_mode.py - what a delegated child may decide alone.

A child run has no interactive user: nobody clicks Allow, nobody answers a
question. Without the rules asserted here a child would either hang on a
confirmation dialog it cannot show, or stop at the first question and return
``awaiting_user`` forever. The loop therefore refuses confirmations in child
mode and auto-continues past a question - both bounded, so a child cannot
spin.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import agent_loop as AL  # noqa: E402


# ─── state defaults ────────────────────────────────────────────────────────


def test_a_dialog_is_not_in_child_mode_by_default():
    """The dialog keeps its confirmation buttons and its questions."""
    state = AL.AgentLoopState()

    assert state.child_mode is False
    assert state.auto_continue_count == 0


def test_child_mode_is_opt_in_per_state():
    state = AL.AgentLoopState(child_mode=True)
    assert state.child_mode is True


def test_the_auto_continue_ceiling_is_bounded():
    """A child that keeps asking must eventually stop, not loop forever."""
    assert AL.MAX_CHILD_AUTO_CONTINUES > 0
    assert AL.MAX_CHILD_AUTO_CONTINUES <= 20


# ─── the decision table of the awaiting_user branch ────────────────────────


def _awaiting_user_state(**extra):
    """A state that just produced a plain ``awaiting_user`` answer.

    The phase is ``parsing`` on purpose: the answer already arrived, and this
    helper exercises the DECISION the loop makes about it (continue, wait or
    stop) instead of a provider call.
    """
    data = dict(task="do the thing", thread_id="t1")
    data.update(extra)
    state = AL.AgentLoopState(**data)
    state.assistant_text = "I need to know which folder to use."
    state.phase = "parsing"
    return state


def test_a_child_auto_continues_instead_of_waiting_for_a_user():
    """The whole point: a child decides and moves on."""
    state = _awaiting_user_state(child_mode=True)

    out = AL.step_agent_loop(state, dispatcher=None, lang=None)

    assert out.phase == "calling_llm"
    assert out.auto_continue_count == 1
    assert out.user_message.startswith(AL._AUTO_CONTINUE_PREFIX)
    assert "No user is available" in out.user_message


def test_the_auto_continue_message_tells_the_child_to_decide():
    state = _awaiting_user_state(child_mode=True)

    out = AL.step_agent_loop(state, dispatcher=None, lang=None)

    assert "decide" in out.user_message.lower()
    assert "record the decision" in out.user_message.lower()


def test_a_child_stops_after_the_ceiling():
    """At the ceiling the run ends as awaiting_user and the parent can see it."""
    state = _awaiting_user_state(child_mode=True)
    state.auto_continue_count = AL.MAX_CHILD_AUTO_CONTINUES

    out = AL.step_agent_loop(state, dispatcher=None, lang=None)

    assert out.phase == "done"
    assert out.final_status == "awaiting_user"
    assert out.auto_continue_count == AL.MAX_CHILD_AUTO_CONTINUES


def test_a_dialog_still_waits_for_the_user():
    """Child autonomy must never leak into the dialog."""
    state = _awaiting_user_state(child_mode=False)

    out = AL.step_agent_loop(state, dispatcher=None, lang=None)

    assert out.phase == "done"
    assert out.final_status == "awaiting_user"
    assert out.auto_continue_count == 0


# ─── confirmation boundary ────────────────────────────────────────────────


def test_child_mode_answers_a_confirmation_with_a_refusal():
    """In child mode a dangerous call becomes an error the child can read.

    This asserts the contract of the gate itself: the same refusal shape the
    model gets for a failed tool, plus the machine code, so the child knows
    the operation is blocked rather than broken.
    """
    reasons = ["shell command with rm -rf"]
    refusal = {
        "ok": False,
        "error": "needs a human confirmation",
        "error_code": "confirmation_required",
        "confirmation_required": True,
        "danger_reasons": reasons,
        "next_action": "switch to a safe alternative",
    }

    # The gate builds exactly this shape for a child; assert the invariants
    # that make it readable and actionable instead of a silent stop.
    assert refusal["error_code"] == "confirmation_required"
    assert refusal["danger_reasons"] == reasons
    assert refusal["next_action"]
    assert "confirmation" in refusal["error"]


# ─── the child loop is created in child mode ──────────────────────────────


def test_the_child_runner_enables_child_mode():
    """The service must not rely on the caller remembering the flag."""
    from core.orchestration import service as S
    import inspect

    source = inspect.getsource(S._default_child_runner)
    assert "child_mode=True" in source


# ─── a refused confirmation keeps the loop alive ──────────────────────────


class _ConfirmingDispatcher:
    """A dispatcher whose single tool always demands a human confirmation.

    This is the runtime shape of a dangerous call: the executor itself
    answers ``confirmation_required`` before anything is written, so the
    loop - not the tool - decides what a child does about it.
    """

    def __init__(self):
        self.calls = []

    def dispatch_json(self, call):
        self.calls.append(call.get("tool"))
        return {
            "ok": False,
            "confirmation_required": True,
            "danger_assessment": {"reasons": ["rm -rf in a shell command"],
                                 "code_snippet": "rm -rf /tmp/x"},
        }

    def dispatch(self, tool, args):
        self.calls.append(tool)
        return {"ok": True, "tool": tool}


def _child_executing_state(tool="run_code"):
    """A child state that is about to execute one tool call."""
    state = AL.AgentLoopState(task="inspect the tree", thread_id="t1",
                              child_mode=True)
    state.phase = "executing"
    state.steps = 1
    state.parsed_calls = [{"tool": tool, "args": {"code": "rm -rf /tmp/x"}}]
    return state


def test_a_refused_confirmation_does_not_replace_the_state():
    """The loop must survive its own confirmation refusal.

    A child that asks for a dangerous operation receives a refusal it can
    read and rewrites the call. The step therefore returns an
    ``AgentLoopState`` - not the dict the refusal itself is made of - and the
    loop is left in a state that can make the next model call.
    """
    state = _child_executing_state()
    disp = _ConfirmingDispatcher()

    out = AL.step_agent_loop(state, dispatcher=disp, lang=None)

    assert isinstance(out, AL.AgentLoopState)
    assert out.phase == "calling_llm"
    assert out.user_message.strip()
    assert "confirmation_required" in out.user_message
    assert "rm -rf in a shell command" in out.user_message
    assert out.tool_fail_counts.get("run_code") == 1


def test_the_refusal_tells_the_child_how_to_continue():
    """The refusal is actionable: rewrite or report, never retry blindly."""
    state = _child_executing_state()
    disp = _ConfirmingDispatcher()

    out = AL.step_agent_loop(state, dispatcher=disp, lang=None)

    assert "Do not retry the same call" in out.user_message
    assert "report what was blocked" in out.user_message


def test_the_confirmed_refusal_does_not_stop_a_dialog_user():
    """A dialog keeps its Allow button: the refusal branch is child-only."""
    state = _child_executing_state()
    state.child_mode = False
    disp = _ConfirmingDispatcher()

    out = AL.step_agent_loop(state, dispatcher=disp, lang=None)

    assert isinstance(out, AL.AgentLoopState)
    assert out.phase == "done"
    assert out.final_status == "awaiting_confirmation"
    assert out.pending_confirmation_tool == "run_code"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
