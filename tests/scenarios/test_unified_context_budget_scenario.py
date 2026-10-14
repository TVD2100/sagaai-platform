"""
tests/scenarios/test_unified_context_budget_scenario.py - user-level
scenario tests for the unified context budget (B4), walking the real public
paths (build_economy_context + core.context_guard) like a user session:

  Scenario 1 - happy path: an economy window built on the unified budget is
               passed through by the pre-flight guard UNCHANGED, so the
               provider prefix keeps its position (no front slide).
  Scenario 2 - edge case: the token budget overflowing (while the message
               window is still unfilled) resets the sent front to the
               configured tail - the same reset a window overflow produces.
  Scenario 3 - resilience: even the worst case (budget fully used + output
               reserve + fixed prompt) stays below the guard's hard cap, so
               a saturated request never turns into ContextWindowError.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

import core.context_guard as cg
from dev_agent.agent_loop import (
    AgentLoopState,
    _estimate_messages_tokens,
    build_economy_context,
)


def _patch_budget(monkeypatch, window, max_out, est):
    """Wire one deterministic model: window, declared output limit, tokens."""
    monkeypatch.setattr(cg, "get_model_context_window", lambda a, s: window)
    monkeypatch.setattr("core.files.get_model_context_window", lambda a, s: window)
    monkeypatch.setattr("core.api_layer._get_model_max_tokens", lambda svc, m: max_out)
    monkeypatch.setattr(cg, "estimate_tokens", est)
    monkeypatch.setattr("core.files.estimate_tokens", est)


def _state(history, *, tail, multiplier, cache=True):
    state = AgentLoopState()
    state.economy_tail_messages = tail
    state.economy_cache_enabled = cache
    state.economy_cache_multiplier = multiplier
    state.history = history
    return state


# ── Scenario 1 - happy path: the unified budget clears the guard ─────────────


def test_scenario_economy_payload_passes_the_guard_unchanged(monkeypatch):
    """Given a DevAgent-size model (1M window, 384k declared output) and a
    thread long enough to fill the unified budget,
    when  the economy window is built and then handed to the pre-flight
    guard,
    then  the guard returns it unchanged (same list, same front), so the
    provider prefix stays cacheable."""
    window, max_out = 1_000_000, 384_000

    def est(text):
        return max(1, len(text or "") // 4)

    _patch_budget(monkeypatch, window, max_out, est)

    assistant = {"service": "mock", "model": "m", "text": "s" * 68_000}
    msg = "x" * 40_000  # 10_000 tokens each
    state = _state([{"role": "user", "content": msg, "ts": "t"}
                    for _ in range(400)],
                   tail=42, multiplier=100)

    payload = build_economy_context(state, assistant)

    # The unified budget cut the long history (not the whole thread sent).
    assert len(payload) < len(state.history) + 1
    # The payload sits inside the shared budget.
    assert _estimate_messages_tokens(payload) <= cg.history_cap(
        window, max_out, est(assistant["text"]))

    out = cg.apply_context_guard(payload, "hello", assistant=assistant,
                                 max_tokens=max_out, services={})
    # Untouched: no front slide, the cached prefix survives.
    assert out == payload


# ── Scenario 2 - edge case: token overflow resets the front to the tail ─────


def test_scenario_token_overflow_resets_front_to_tail(monkeypatch):
    """Given a cache-friendly window whose payload exceeds the TOKEN budget
    while the message window (tail x multiplier) is not yet full,
    when  the window is built,
    then  the sent front is reset to the configured tail - the same front a
    window overflow would pick - and stays put on the next small step."""
    def est(text):
        return max(1, len(text or "") // 4)

    _patch_budget(monkeypatch, 23_733, 1_000, est)  # budget == 15_000 tokens

    assistant = {"service": "mock", "model": "m"}
    msg = "x" * 8_000  # 2_000 tokens each
    state = _state([{"role": "user", "content": msg, "ts": "t"}
                    for _ in range(20)],
                   tail=5, multiplier=20)  # window 100 msgs stays unfilled

    result = build_economy_context(state, assistant)

    assert state.economy_anchor == 15        # 20 - tail(5)
    assert [m["content"] for m in result[1:]] == [msg] * 5

    # A following step that only appends a small message keeps the front.
    state.history.append({"role": "user", "content": "s" * 100, "ts": "t"})
    again = build_economy_context(state, assistant)
    assert state.economy_anchor == 15
    assert again[1]["content"] == msg


# ── Scenario 3 - resilience: the worst case never hits the hard cap ─────────


def test_scenario_worst_case_stays_below_hard_cap(monkeypatch):
    """Given a fully saturated unified budget plus the output reserve and the
    fixed prompt,
    when  the numbers are combined,
    then  the total stays below the guard's hard cap, so an economy request
    never raises ContextWindowError mid-conversation."""
    window, max_out = 1_000_000, 384_000
    prompt_tokens = 26_112

    budget = cg.history_cap(window, max_out, prompt_tokens)
    reserve = cg._output_reserve(window, max_out)
    worst_total = budget + reserve + prompt_tokens + max(cg._MIN_OUTPUT_RESERVE, 1)
    hard = int(window * cg._HARD_RATIO)

    assert worst_total < hard
    # A comfortable margin: at least 5% of the window of headroom.
    assert hard - worst_total >= int(window * 0.05)

    # And the construction is consistent: the budget never exceeds the
    # soft threshold minus the reserve and the prompt.
    soft = int(window * cg._SOFT_RATIO)
    assert budget <= soft - reserve - prompt_tokens
