# -*- coding: utf-8 -*-
"""tests/scenarios/test_incomplete_responses_recovery_scenario.py - user-level
scenarios for the truncated (incomplete) Responses recovery feature.

Scenarios walk the PUBLIC agent-loop entry point
``dev_agent.agent_loop.step_agent_loop`` the way a user experiences it, with a
scripted ``send_request`` that mimics the real provider behaviour: an HTTP 200
body whose ``status == "incomplete"`` (reason ``max_output_tokens``) carries no
user-visible text, so the raw answer looks like a normal empty reply.

  Scenario 1 - recovery (truncation then success): the first answer is
               truncated with no text; the loop retries ONCE with the
               reasoning effort lowered to ``none`` and the user gets a real
               answer plus an ``incomplete_retry`` notification.
  Scenario 2 - partial text kept: the truncated body still carries visible
               text, so the user keeps it - no retry, no notification.
  Scenario 3 - truncation persists: even the lowered-effort retry is
               truncated; the loop terminates gracefully (exactly two calls)
               instead of looping forever.
"""

import json

from dev_agent.agent_loop import AgentLoopState, step_agent_loop


class _FakeCore:
    """Minimal dispatcher core used by step_agent_loop."""

    def set_history(self, *a, **k):
        pass

    def set_send_request(self, *a, **k):
        pass


class _FakeDispatcher:
    """Dispatcher whose only active path is the LLM call."""

    def __init__(self):
        self.core = _FakeCore()

    def dispatch(self, tool, args):
        return {"ok": True, "tool": tool, "args": args}

    def dispatch_json(self, call):
        return {"ok": True, "tool": call.get("tool", "")}


_DONE = json.dumps({"loop_status": "awaiting_user"}) + " "
_INCOMPLETE = {
    "status": "incomplete",
    "reason": "max_output_tokens",
    "id": "resp_scenario",
    "output_tokens": 384000,
}


def _make_state(user_message):
    state = AgentLoopState(task=user_message, auto_apply=False)
    state.phase = "calling_llm"
    state.user_message = user_message
    return state


def _run_until_terminal(state, dispatcher, events):
    for _ in range(10):
        state = step_agent_loop(state, dispatcher=dispatcher, on_event=events.append)
        if state.phase in ("done", "error"):
            break
    return state


def test_scenario_truncation_then_recovery(monkeypatch):
    """Scenario 1: given a user turn whose first answer is truncated with no
    text, when the loop reacts, then it retries once with reasoning_effort
    'none' and the user receives a real answer plus one incomplete_retry
    notification."""
    import dev_agent.agent_loop as loop

    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(dict(assistant))
        cb = kwargs.get("incomplete_callback")
        assert cb is not None
        if len(calls) == 1:
            cb(dict(_INCOMPLETE))
            return ""  # truncated: no user-visible text
        return _DONE + "Вот полный ответ на ваш вопрос."

    monkeypatch.setattr(loop, "send_request", send)
    state = _make_state("объясни сложную тему")
    events = []

    state = _run_until_terminal(state, _FakeDispatcher(), events)

    assert len(calls) == 2
    assert calls[0].get("reasoning_effort") != "none"
    assert calls[1].get("reasoning_effort") == "none"
    assert "Вот полный ответ" in state.assistant_text
    retries = [e for e in events if e.get("type") == "incomplete_retry"]
    assert len(retries) == 1
    assert retries[0]["reason"] == "max_output_tokens"


def test_scenario_partial_text_is_kept_without_retry(monkeypatch):
    """Scenario 2: given a truncated answer that still carries visible text,
    when the loop reacts, then the user keeps that text and no retry happens."""
    import dev_agent.agent_loop as loop

    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(dict(assistant))
        cb = kwargs.get("incomplete_callback")
        if cb:
            cb(dict(_INCOMPLETE))
        return _DONE + "Начало ответа, которое видно пользователю."

    monkeypatch.setattr(loop, "send_request", send)
    state = _make_state("вопрос")
    events = []

    state = _run_until_terminal(state, _FakeDispatcher(), events)

    assert len(calls) == 1
    assert "Начало ответа" in state.assistant_text
    assert not [e for e in events if e.get("type") == "incomplete_retry"]


def test_scenario_persistent_truncation_terminates(monkeypatch):
    """Scenario 3: given truncation that persists even after the lowered-effort
    retry, when the loop reacts, then it stops gracefully after exactly two
    calls instead of looping forever."""
    import dev_agent.agent_loop as loop

    calls = []

    def send(user_message, assistant, **kwargs):
        calls.append(dict(assistant))
        cb = kwargs.get("incomplete_callback")
        if cb:
            cb(dict(_INCOMPLETE))
        return ""

    monkeypatch.setattr(loop, "send_request", send)
    state = _make_state("очень большой контекст")
    events = []

    state = _run_until_terminal(state, _FakeDispatcher(), events)

    assert state.phase == "done"
    assert len(calls) == 2, "recovery retry must be bounded to one attempt"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
