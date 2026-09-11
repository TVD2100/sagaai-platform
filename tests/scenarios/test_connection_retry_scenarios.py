# -*- coding: utf-8 -*-
"""tests/scenarios/test_connection_retry_scenarios.py - user-level scenarios
 for the Windows connection-resilience feature (Steps 1-5).

Scenarios walk the PUBLIC agent-loop entry point
``dev_agent.agent_loop.step_agent_loop`` with the real
``core.api_layer.retry_call`` wiring, the way a user experiences it:

  Scenario 1 - happy path with a flaky network: two connection drops during
               a user turn are retried transparently, the UI receives
               ``retrying_llm`` events, and the assistant still answers; the
               user's message stays in history exactly once.
  Scenario 2 - permanent outage: after the configured attempts the loop ends
               in the error state, the last error carries ``attempts``, and
               the user's message is PRESERVED in history (nothing is lost).
  Scenario 3 - provider HTTP error: a 500 response is NOT retried - the loop
               fails fast without any ``retrying_llm`` events.
"""

import json

import pytest

from core.api_errors import NetworkError, ProviderHTTPError


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Keep retries instant so scenarios run without real 30 s pauses."""
    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_DELAY", "0")
    monkeypatch.setenv("SAGAAI_NETWORK_RETRY_ATTEMPTS", "3")


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


def _make_state(user_message):
    from dev_agent.agent_loop import AgentLoopState

    state = AgentLoopState(task=user_message, auto_apply=False)
    state.phase = "calling_llm"
    state.user_message = user_message
    return state


def _patch_send_request_with_real_retry(monkeypatch, attempts_sequence):
    """Replace send_request with the PRODUCTION wiring: retry_call over a
    flaky inner request whose outcomes are given by ``attempts_sequence``
    (each entry is either an exception to raise, or a callable returning the
    reply text). The retry_callback is forwarded exactly like
    core.api_layer.send_request does."""
    import dev_agent.agent_loop as loop
    from core.api_layer import retry_call

    def production_like_send(user_message, assistant=None, **kwargs):
        callback = kwargs.get("retry_callback")
        outcomes = list(attempts_sequence)
        hold = {"last": None}

        def attempt():
            outcome = outcomes.pop(0) if outcomes else hold["last"]
            hold["last"] = outcome
            if isinstance(outcome, Exception):
                raise outcome
            return outcome()

        return retry_call(attempt, retry_callback=callback)

    monkeypatch.setattr(loop, "send_request", production_like_send)


def _run_until_terminal(state, dispatcher, events):
    for _ in range(10):
        state = step_agent_loop(state, dispatcher=dispatcher, on_event=events.append)
        if state.phase in ("done", "error"):
            break
    return state


from dev_agent.agent_loop import step_agent_loop  # noqa: E402


def test_scenario_flaky_network_recovers_transparently(monkeypatch):
    """Scenario 1: two connection drops during a turn are invisible to the
    user apart from retrying_llm notifications; the answer still arrives."""
    user_text = "как поживаешь?"
    _patch_send_request_with_real_retry(monkeypatch, [
        NetworkError("connection reset", service="DeepSeek"),
        NetworkError("connection reset", service="DeepSeek"),
        lambda: _DONE + "Спасибо, хорошо!",
    ])

    state = _make_state(user_text)
    events = []

    state = _run_until_terminal(state, _FakeDispatcher(), events)

    # then: the turn completed, retry notifications reached the UI (2 retries)
    assert state.phase == "done"
    retries = [e for e in events if e.get("type") == "retrying_llm"]
    assert [e["attempt"] for e in retries] == [1, 2]
    assert all(e["attempts"] == 3 for e in retries)
    # the assistant answered and the user message survived exactly once
    assistant_msgs = [m for m in state.history if m.get("role") == "assistant"]
    assert assistant_msgs
    assert "Спасибо, хорошо!" in assistant_msgs[-1].get("content", "")
    user_msgs = [
        m for m in state.history
        if m.get("role") == "user" and m.get("content") == user_text
    ]
    assert len(user_msgs) == 1


def test_scenario_permanent_outage_preserves_user_message(monkeypatch):
    """Scenario 2: when the network never comes back, the loop ends in the
    error phase after 3 attempts and the user's message is NOT lost."""
    user_text = "важное сообщение, которое нельзя терять"
    _patch_send_request_with_real_retry(monkeypatch, [
        NetworkError("no route to host", service="DeepSeek"),
    ])

    state = _make_state(user_text)
    events = []

    state = _run_until_terminal(state, _FakeDispatcher(), events)

    assert state.phase == "error"
    retries = [e for e in events if e.get("type") == "retrying_llm"]
    assert [e["attempt"] for e in retries] == [1, 2]
    user_msgs = [
        m for m in state.history
        if m.get("role") == "user" and m.get("content") == user_text
    ]
    assert len(user_msgs) == 1
    assert "no route to host" in (state.error_message or "")


def test_scenario_provider_http_error_fails_fast(monkeypatch):
    """Scenario 3: a 500 from the provider is a definitive answer - the loop
    fails immediately and never emits a retrying_llm notification."""
    user_text = "простой запрос"
    _patch_send_request_with_real_retry(monkeypatch, [
        ProviderHTTPError(500, "internal error", service="DeepSeek"),
    ])

    state = _make_state(user_text)
    events = []

    state = _run_until_terminal(state, _FakeDispatcher(), events)

    assert state.phase == "error"
    assert not [e for e in events if e.get("type") == "retrying_llm"]
    user_msgs = [
        m for m in state.history
        if m.get("role") == "user" and m.get("content") == user_text
    ]
    assert len(user_msgs) == 1
