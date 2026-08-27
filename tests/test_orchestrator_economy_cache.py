# -*- coding: utf-8 -*-
"""
UI regression test for the cache-friendly economy window.

Reproduces the reported bug: after a terminal loop status (done/applied/
error) clears ``loop_state``, the next turn created a fresh AgentLoopState
with ``economy_anchor=None``, so ``build_economy_context`` re-anchored at
``total - tail`` on EVERY turn and the cache-friendly window never grew
past 30 messages.

Expected behavior: the anchor/meta/tail are persisted separately in
session_state (``orch_<slug>_economy_cache``), survive loop-state clearing,
and the window grows again: 30 -> 32 -> 34 -> ... up to 90 before the next
reset.
"""
from __future__ import annotations

import sys

import pytest

from tests._st_mock import install_streamlit_mock


class _FakeCore:
    """Minimal dispatcher core used by step_agent_loop."""

    def __init__(self):
        self._web_search_enabled = False
        self._safety_enabled = True

    def set_history(self, *a, **k):
        pass

    def set_send_request(self, *a, **k):
        pass


class _FakeDispatcher:
    """Dispatcher stub: turns are orchestrated by send_request, so dispatch
    methods are never reached in these tests."""

    def __init__(self):
        self.core = _FakeCore()

    def dispatch(self, tool, args):
        return {"ok": True, "tool": tool, "args": args}

    def dispatch_json(self, call):
        return {"ok": True, "tool": call.get("tool", "")}


@pytest.fixture()
def ui_env(tmp_path):
    """Streamlit mock with freshly imported ui.* modules.

    The mock must be installed BEFORE ``ui.pages.orchestrator`` is imported,
    otherwise the page binds ``st`` to the real Streamlit package and its
    session_state silently no-ops in bare mode.
    """
    import os

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["SAGAAI_DATA_DIR"] = str(data_dir)

    saved_ui = {}
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            saved_ui[m] = sys.modules.pop(m, None)

    with install_streamlit_mock() as st:
        try:
            yield st
        finally:
            for m, mod in saved_ui.items():
                if mod is not None:
                    sys.modules[m] = mod
                else:
                    sys.modules.pop(m, None)


def _mk(i: int) -> dict:
    return {
        "role": "user" if i % 2 == 0 else "assistant",
        "content": f"m{i}",
        "ts": "2026-01-01T00:00:00",
    }


def _sent_len(history) -> int:
    """Count the non-meta messages actually sent to the model."""
    return len([m for m in history if m.get("role") in ("user", "assistant")])


def _drive(st, orch_page, slug, limit=60):
    """Call _do_step repeatedly until the loop reaches a terminal state.

    The real UI advances the loop by rerunning the page; each rerun calls
    _do_step once. Here we simulate the repeated reruns until the terminal
    status clears loop_state (or a user-gated status keeps it).
    """
    for _ in range(limit):
        orch_page._do_step(slug, "English")
        ls = st.session_state.get(f"orch_{slug}_loop_state")
        if ls is None:
            return
        if getattr(ls, "final_status", None) in (
            "awaiting_user", "awaiting_approval",
            "awaiting_confirmation", "sanitized_required",
        ):
            return
    raise AssertionError("agent loop did not reach a terminal state")


def _setup_page(monkeypatch):
    """Import the orchestrator page under the streamlit mock and wire the
    agent-loop seams: scripted send_request, light dispatcher/send-adapter
    stubs, and the economy config (tail 30, cache enabled, x3).
    """
    import dev_agent.agent_loop as al
    import ui.pages.orchestrator as orch_page

    responses = iter(["All plan steps completed."] * 500)
    sent_histories = []

    def fake_send(user_message, assistant, file_context="", history=None,
                  lang=None, **kwargs):
        sent_histories.append(list(history or []))
        try:
            return next(responses)
        except StopIteration:
            return ""

    monkeypatch.setattr(al, "send_request", fake_send)
    monkeypatch.setattr(orch_page, "_make_dispatcher", lambda s: _FakeDispatcher())
    monkeypatch.setattr(orch_page, "_make_send_adapter",
                        lambda lang, slug: (lambda *a, **k: ""))
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: {"max_steps": 100})
    monkeypatch.setattr(orch_page, "build_assistant_dicts",
                        lambda s: ({"service": "mock", "model": "m", "temperature": 0.1},
                                   {"service": "mock", "model": "m", "temperature": 0.1}))
    monkeypatch.setattr(orch_page, "get_economy_config",
                        lambda s: {"tail_messages": 30, "cache_enabled": True,
                                   "cache_multiplier": 3})
    return orch_page, sent_histories


def test_do_step_window_grows_again_after_terminal_status(monkeypatch, ui_env):
    """
    Given a long dialog whose last loop ended with a terminal status
    (e.g. ``done``) and thus cleared ``loop_state``,
    when  the user sends the next message,
    then the new loop restores the persisted economy anchor and the
    cache-friendly window GROWS (32, not 30) instead of restarting at 30.
    """
    orch_page, sent_histories = _setup_page(monkeypatch)
    slug = orch_page.DEVAGENT_SLUG
    st = ui_env

    orch_page._init_orch_state(slug)
    st.session_state[f"orch_{slug}_economy_mode"] = True
    st.session_state[f"orch_{slug}_web_search"] = False
    st.session_state[f"orch_{slug}_safety_mode"] = True

    # ── Turn 1: history 92 > window 90 → anchor fixed at 62 → sent 30.
    st.session_state[f"orch_{slug}_history"] = [_mk(i) for i in range(92)]
    st.session_state[f"orch_{slug}_user_message"] = "continue"
    _drive(st, orch_page, slug)

    # Terminal status must clear loop_state but persist the economy cache.
    assert st.session_state.get(f"orch_{slug}_loop_state") is None
    cache = st.session_state.get(f"orch_{slug}_economy_cache")
    assert isinstance(cache, dict), f"economy cache missing: {cache}"
    assert cache.get("economy_anchor") == 62, cache
    assert _sent_len(sent_histories[-1]) == 30

    # ── Turn 2: history 94, same anchor → window GROWS to 32.
    st.session_state[f"orch_{slug}_history"] = [_mk(i) for i in range(94)]
    st.session_state[f"orch_{slug}_user_message"] = "continue"
    _drive(st, orch_page, slug)

    assert _sent_len(sent_histories[-1]) == 32, (
        "cache-friendly window must grow to 32 after a terminal-status "
        "turn; got " + str(_sent_len(sent_histories[-1]))
    )

    # ── Turn 3: history 96 → window grows to 34.
    st.session_state[f"orch_{slug}_history"] = [_mk(i) for i in range(96)]
    st.session_state[f"orch_{slug}_user_message"] = "continue"
    _drive(st, orch_page, slug)

    assert _sent_len(sent_histories[-1]) == 34


def test_do_step_full_cycle_resets_then_grows(monkeypatch, ui_env):
    """
    Given a dialog progressing through a full cache-friendly cycle,
    when  the history crosses the window boundary multiple times,
    then  the sent count follows: ... -> 90 -> reset 30 -> grow 32,
    proving the accumulation restarts only when the prefix overflows.
    """
    orch_page, sent_histories = _setup_page(monkeypatch)
    slug = orch_page.DEVAGENT_SLUG
    st = ui_env

    orch_page._init_orch_state(slug)
    st.session_state[f"orch_{slug}_economy_mode"] = True
    st.session_state[f"orch_{slug}_web_search"] = False
    st.session_state[f"orch_{slug}_safety_mode"] = True

    def run_turn(n):
        st.session_state[f"orch_{slug}_history"] = [_mk(i) for i in range(n)]
        st.session_state[f"orch_{slug}_user_message"] = "continue"
        _drive(st, orch_page, slug)
        return _sent_len(sent_histories[-1])

    assert run_turn(40) == 40   # initial accumulation
    assert run_turn(42) == 42
    assert run_turn(92) == 30   # first reset after window overflow
    assert run_turn(94) == 32   # growth resumes
    assert run_turn(96) == 34
    assert run_turn(152) == 90  # reaches the window again
    assert run_turn(154) == 30  # second reset
    assert run_turn(156) == 32  # third growth cycle
