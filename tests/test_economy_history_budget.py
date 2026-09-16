# -*- coding: utf-8 -*-
"""Unit tests for the M3 history token budget in build_economy_context.

The budget keeps estimated input + configured max output inside
``window * _ECONOMY_INPUT_BUDGET_RATIO`` by trimming the OLDEST sent
messages. The compact metadata system message (head) is never removed.
These tests monkeypatch the window / max-output / token-estimation lookups
so the assertions depend only on the trimming logic, not on provider
configs or tiktoken availability.
"""
from __future__ import annotations

import pytest

from dev_agent.agent_loop import AgentLoopState, build_economy_context


@pytest.fixture()
def services_window(monkeypatch):
    """Deterministic window/max-output/token counters for the budget."""
    monkeypatch.setattr("core.files.get_model_context_window",
                        lambda skill, services: 20_000)
    monkeypatch.setattr("core.api_layer._get_model_max_tokens",
                        lambda svc, model_id: 1_000)
    # 1 token per 4 chars: budget 15_000 tokens == 60_000 chars.
    monkeypatch.setattr("core.files.estimate_tokens",
                        lambda text: max(1, len(text or "") // 4))
    return 15_000  # effective input budget in tokens


@pytest.fixture()
def assistant():
    return {"service": "mock", "model": "mockmodel", "temperature": 0.1}


def _make_state(history):
    state = AgentLoopState()
    state.economy_tail_messages = 10
    state.economy_cache_enabled = False
    state.economy_cache_multiplier = 1
    state.history = history
    return state


def _msg(content, role="user"):
    return {"role": role, "content": content, "ts": "2026-01-01T00:00:00"}


def test_budget_trims_oldest_messages_from_front(services_window, assistant):
    """
    Given a history whose estimated input exceeds the budget, but only
    because of the OLD messages,
    when  build_economy_context is called with the assistant,
    then the oldest messages are dropped and the recent tail survives
    inside the budget; the meta head is preserved.
    """
    big = "x" * 40_000  # 10_000 tokens
    small_a = "a" * 100   # 25 tokens
    small_b = "b" * 100   # 25 tokens
    history = [
        _msg(big, "assistant"),
        _msg(big, "user"),
        _msg(big, "assistant"),
        _msg(small_a, "user"),
        _msg(small_b, "assistant"),
    ]
    state = _make_state(history)

    result = build_economy_context(state, assistant)

    # Meta message is the head and is preserved.
    assert result[0].get("role") == "system"
    assert result[0].get("hidden") is True
    # Two oldest 40k messages trimmed; the rest survives.
    sent = result[1:]
    assert [m["content"] for m in sent] == [big, small_a, small_b], (
        "the two oldest big messages must be trimmed, keeps "
        + str([len(m["content"]) for m in sent])
    )
    # Total estimate stays inside the budget.
    from dev_agent.agent_loop import _estimate_messages_tokens
    assert _estimate_messages_tokens(result) <= 15_000


def test_budget_keeps_everything_when_it_fits(services_window, assistant):
    """
    Given a small history that fits the budget,
    when  build_economy_context is called,
    then no messages are dropped.
    """
    history = [_msg("hello"), _msg("world"), _msg("ok")]
    state = _make_state(history)

    result = build_economy_context(state, assistant)

    assert len(result) == len(history) + 1  # meta + all messages
    assert [m["content"] for m in result[1:]] == ["hello", "world", "ok"]


def test_no_assistant_skips_budget_passthrough():
    """
    Given an existing call without an assistant (legacy usage),
    when  build_economy_context is invoked with only the state,
    then the previous behavior is preserved: meta + tail, no budget
    trimming, no errors.
    """
    history = [_msg("m" * 50_000), _msg("last")]
    state = _make_state(history)
    state.economy_tail_messages = 5

    result = build_economy_context(state)

    assert len(result) == len(history) + 1
    assert result[-1]["content"] == "last"


def test_unknown_window_falls_back_to_passthrough(services_window, monkeypatch, assistant):
    """
    Given a window lookup failure (unknown service/model),
    when  build_economy_context is called,
    then the history is returned untrimmed instead of crashing.
    """
    def _boom(skill, services):
        raise KeyError("no such service")

    monkeypatch.setattr("core.files.get_model_context_window", _boom)
    history = [_msg("m" * 50_000), _msg("last")]
    state = _make_state(history)

    result = build_economy_context(state, assistant)

    assert len(result) == len(history) + 1
