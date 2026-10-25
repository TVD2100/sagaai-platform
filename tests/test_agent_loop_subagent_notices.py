# -*- coding: utf-8 -*-
"""tests/test_agent_loop_subagent_notices.py - how a finished child reports back.

A background child cannot write into the dialog history: the history belongs
to the script run that drives the loop. The worker posts into the tree mailbox
and the loop moves the notice into the model context right before the next
call. These tests pin the two properties that make that safe - the notice is
delivered as a HIDDEN SYSTEM message (never as plain user text, which would
end the run) and it is delivered to the tree that owns the mailbox, once.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dev_agent.agent_loop as AL  # noqa: E402
from core.orchestration import service as service_mod  # noqa: E402


class _FakeService:
    """Stand-in for the orchestration service mailbox."""

    def __init__(self, notices=None):
        self.notices = list(notices or [])
        self.taken_from = []

    def take_notifications(self, root_run_id):
        self.taken_from.append(root_run_id)
        out, self.notices = self.notices, []
        return out


def _notice(**extra):
    data = {
        "type": "run_finished",
        "run_id": "abc123",
        "root_run_id": "root1",
        "agent_slug": "dev_agent",
        "status": "done",
        "payload": {"status": "done", "steps_used": 7},
    }
    data.update(extra)
    return data


# ─── notice text ──────────────────────────────────────────────────────────


def test_the_notice_names_the_run_the_agent_and_the_status():
    line = AL.format_subagent_notice(_notice())

    assert line.startswith(AL.NOTICE_PREFIX)
    assert "abc123" in line
    assert "dev_agent" in line
    assert "done" in line


def test_the_notice_carries_the_follow_up_calls():
    """The parent must be able to act without guessing the tool names."""
    line = AL.format_subagent_notice(_notice())

    assert "read_subagent_state(run_id=\"abc123\")" in line
    assert "resume_subagent(run_id=\"abc123\")" in line


def test_the_notice_reports_the_failure_reason():
    line = AL.format_subagent_notice(_notice(
        status="failed", payload={"stop_reason": "error",
                                 "error_code": "provider_error"}))

    assert "provider_error" in line or "error" in line


def test_only_three_open_steps_are_mentioned():
    """A notice is one line, not a copy of the child journal."""
    line = AL.format_subagent_notice(_notice(payload={
        "status": "awaiting_input",
        "pending": ["step_1", "step_2", "step_3", "step_4", "step_5"],
    }))

    assert "step_3" in line
    assert "step_4" not in line


def test_a_notice_without_a_run_id_is_ignored():
    assert AL.format_subagent_notice({}) == ""
    assert AL.format_subagent_notice(None) == ""


# ─── drain into the history ───────────────────────────────────────────────


def test_a_notice_becomes_a_hidden_system_message(monkeypatch):
    """Delivery must not look like the user speaking."""
    fake = _FakeService([_notice()])
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    state = AL.AgentLoopState(task="go", thread_id="t1", root_run_id="root1")

    lines = AL.drain_subagent_notices(state)

    assert len(lines) == 1
    entry = state.history[-1]
    assert entry["role"] == "system"
    assert entry["hidden"] is True
    assert entry["_notice"] is True
    assert entry["_category"] == "system_notice"
    assert "SUBAGENT_NOTICE" in entry["content"]


def test_the_drain_reads_the_tree_root(monkeypatch):
    fake = _FakeService([_notice()])
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    state = AL.AgentLoopState(task="go", thread_id="t1",
                              run_id="child7", root_run_id="root1")

    AL.drain_subagent_notices(state)

    assert fake.taken_from == ["root1"]


def test_a_run_without_identity_does_not_read_any_mailbox(monkeypatch):
    """Fail-closed: no identity means no tree, so nothing is drained."""
    fake = _FakeService([_notice()])
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    state = AL.AgentLoopState(task="go")

    assert AL.drain_subagent_notices(state) == []
    assert fake.taken_from == []


def test_the_drain_is_not_repeated(monkeypatch):
    fake = _FakeService([_notice()])
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    state = AL.AgentLoopState(task="go", thread_id="t1", root_run_id="root1")

    first = AL.drain_subagent_notices(state)
    second = AL.drain_subagent_notices(state)

    assert len(first) == 1
    assert second == []


def test_delivered_lines_are_recorded_on_the_state(monkeypatch):
    fake = _FakeService([_notice()])
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    state = AL.AgentLoopState(task="go", thread_id="t1", root_run_id="root1")

    AL.drain_subagent_notices(state)

    assert state.subagent_notices == [line for line in state.subagent_notices]
    assert len(state.subagent_notices) == 1


# ─── the loop drains before the model call ────────────────────────────────


def test_the_step_drains_the_mailbox_before_the_model_call(monkeypatch):
    """The notice must be inside the window sent with THIS step."""
    fake = _FakeService([_notice()])
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    captured = {}

    def _fake_send(user_message, assistant, **kwargs):
        captured["history"] = list(kwargs.get("history") or [])
        captured["user_message"] = user_message
        return "All set. " + json.dumps({"loop_status": "awaiting_user"})

    monkeypatch.setattr(AL, "send_request", _fake_send)
    state = AL.AgentLoopState(task="go", thread_id="t1", root_run_id="root1")
    state.phase = "calling_llm"

    AL.step_agent_loop(state, dispatcher=None, lang=None)

    sent = [m for m in captured["history"]
            if m.get("role") == "system" and "SUBAGENT_NOTICE" in str(m.get("content"))]
    assert sent, "the notice never reached the outgoing window"
    assert fake.taken_from == ["root1"]


def test_a_drained_notice_never_becomes_plain_user_text(monkeypatch):
    """A stray user line would end a run whose plan is still open."""
    fake = _FakeService([_notice()])
    monkeypatch.setattr(service_mod, "get_service", lambda: fake)
    captured = {}

    def _fake_send(user_message, assistant, **kwargs):
        captured["user_message"] = str(user_message or "")
        return "All set. " + json.dumps({"loop_status": "awaiting_user"})

    monkeypatch.setattr(AL, "send_request", _fake_send)
    state = AL.AgentLoopState(task="go", thread_id="t1", root_run_id="root1")
    state.phase = "calling_llm"

    AL.step_agent_loop(state, dispatcher=None, lang=None)

    assert "SUBAGENT_NOTICE" not in captured["user_message"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
