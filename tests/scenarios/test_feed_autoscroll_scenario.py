# -*- coding: utf-8 -*-
"""tests/scenarios/test_feed_autoscroll_scenario.py - user-level scenario for
feed auto-scroll in the orchestrator chat (given -> when -> then).

Scenario 1 - the user sends the first message: the feed is forced to the
              bottom so the new question and the agent's reply are visible.

Scenario 2 - the agent keeps working across several steps: every render emits
              the follow script (non-forced), so the feed trails the output.

Scenario 3 - the user scrolls up to read earlier messages: the emitted script
              carries the client-side pause logic (sessionStorage flag set on
              scroll-up / wheel-up, cleared at the bottom) so the automatic
              follow stops until the user returns to the bottom.

Scenario 4 - the chat is idle: no scroll script is emitted, so the user's own
              reading position is never disturbed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    """Isolated DATA_DIR + fresh ui.* modules under the streamlit mock."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


class _LoopState:
    """Minimal stand-in for the agent loop state."""

    def __init__(self, phase):
        self.phase = phase
        self.final_status = ""
        self.error_message = ""
        self.history = []


def _chat_env(ui_env, monkeypatch, *, loop_state=None, scroll_bottom=False):
    """Seed session state, patch dependencies and capture emitted scripts."""
    import ui.pages.orchestrator as orch_page

    orch = {"slug": "o1", "name": "DevAgent", "description": "",
            "is_builtin": True, "prompt_text": "",
            "config": {"strong_service": "Svc", "strong_model": "m1"}}
    monkeypatch.setattr(orch_page, "get_orchestrator", lambda slug: orch)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    monkeypatch.setattr(orch_page, "t", lambda key, *a, **k: key)
    monkeypatch.setattr(
        orch_page, "build_assistant_dicts",
        lambda slug: ({"service": "Svc", "model": "m1", "temperature": 0.5,
                       "text": "p", "max_tokens": 0}, {}),
    )
    monkeypatch.setattr(
        orch_page, "check_context",
        lambda *a, **k: {"total_tokens": 10, "ok": True,
                         "limit": 1000, "excess_chars": 0},
    )
    monkeypatch.setattr(orch_page, "sum_thread_tokens", lambda msgs: (0, 0, 0))
    monkeypatch.setattr(orch_page, "_do_step", lambda **k: None)
    monkeypatch.setattr(orch_page, "_render_token_line", lambda *a, **k: None)

    emitted: list = []
    monkeypatch.setattr(orch_page, "render_autoscroll",
                        lambda force=False: emitted.append(force))

    ui_env.session_state.update({
        "ui_lang": "English",
        "orch_o1_history": [],
        "orch_o1_loop_state": loop_state,
        "orch_o1_thread_id": None,
        "orch_o1_web_search": False,
        "orch_o1_economy_mode": False,
        "orch_o1_safety_mode": True,
        "orch_o1_saved_msg_count": 0,
        "orch_o1_dispatcher": None,
        "orch_o1_scroll_bottom": scroll_bottom,
    })
    return orch_page, emitted


def _render(orch_page):
    try:
        orch_page._render_chat_tab("o1", "English")
    except StopRerun:
        pass


# ── Scenario 1 ─────────────────────────────────────────────────────────────

def test_scenario_first_send_forces_scroll_to_bottom(ui_env, monkeypatch):
    """Given a fresh chat, when the user sends a message, then the feed is
    forced to the bottom exactly once and the pending flag is cleared."""
    orch_page, emitted = _chat_env(ui_env, monkeypatch, scroll_bottom=True)
    _render(orch_page)
    assert emitted == [True], f"emitted: {emitted}"
    assert ui_env.session_state["orch_o1_scroll_bottom"] is False


# ── Scenario 2 ─────────────────────────────────────────────────────────────

def test_scenario_active_agent_follows_each_step(ui_env, monkeypatch):
    """Given a running agent, when the feed re-renders for each step, then it
    emits the follow script every time (non-forced)."""
    orch_page, emitted = _chat_env(
        ui_env, monkeypatch, loop_state=_LoopState("running"))
    for _ in range(3):
        _render(orch_page)
    assert emitted == [False, False, False], f"emitted: {emitted}"


# ── Scenario 3 ─────────────────────────────────────────────────────────────

def test_scenario_script_pauses_on_scroll_up_and_resumes_at_bottom():
    """Given the emitted client script, when the user scrolls up, then the
    pause flag is set; when they return to the bottom, it is cleared."""
    from core import feed_autoscroll as fa

    js = fa.autoscroll_js(force=False)
    # Pause on a manual upward scroll (wheel/touch) and on the scroll event
    # landing away from the bottom.
    assert "sessionStorage" in js
    assert "setPause(true)" in js
    assert "e.deltaY < 0" in js          # wheel-up
    assert "touchmove" in js              # touch drag-up
    # Resume once the user is back at the bottom (within the threshold).
    assert "atBottom(el)" in js
    assert "setPause(false)" in js
    # A paused user is not dragged down by the next render.
    assert "if (FORCE || !getPause()) { scrollDown(); }" in js


# ── Scenario 4 ─────────────────────────────────────────────────────────────

def test_scenario_idle_chat_does_not_scroll(ui_env, monkeypatch):
    """Given an idle chat (no active agent, nothing sent), when the feed
    renders, then no scroll script is emitted."""
    orch_page, emitted = _chat_env(ui_env, monkeypatch)
    _render(orch_page)
    assert emitted == [], f"emitted: {emitted}"
