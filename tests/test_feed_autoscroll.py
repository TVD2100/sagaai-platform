# -*- coding: utf-8 -*-
"""tests.test_feed_autoscroll - auto-scroll of the orchestrator chat feed.

Covers ``core.feed_autoscroll`` (JS generation and component emission) and its
wiring into ``_render_chat_tab``: the script is emitted while the agent works
and right after a message is sent, and stays quiet when the chat is idle.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

from core import feed_autoscroll as fa  # noqa: E402


# ─── unit: JS generation & component emission ───────────────────────────────

def test_autoscroll_js_has_required_markers():
    """The snippet carries the threshold and the pause flag, and adds no padding."""
    js = fa.autoscroll_js()
    assert "<script>" in js and "</script>" in js
    assert str(fa.AUTOSCROLL_THRESHOLD_PX) in js
    assert "sessionStorage" in js
    assert "__saga_feed_autoscroll_paused" in js
    # Streamlit 1.66 reserves room for the bottom bar itself; the module must
    # NOT add its own padding (that caused a large empty gap in the feed).
    assert "padForBottomBar" not in js
    assert "paddingBottom" not in js
    assert "padding-bottom" not in js
    # No unsubstituted placeholders may survive into the client script.
    assert "__THRESHOLD_PX__" not in js
    assert "__FORCE_FLAG__" not in js


def test_autoscroll_js_force_flag_toggles():
    """force=True flips the client FORCE constant; default stays false."""
    assert "var FORCE = false;" in fa.autoscroll_js(False)
    assert "var FORCE = true;" in fa.autoscroll_js(True)


def test_render_autoscroll_emits_zero_height_component():
    """The helper emits exactly one zero-height components.html block."""
    with install_streamlit_mock() as st:
        fa.render_autoscroll(force=True)
        html_calls = [c for c in st.calls if c[0] == "components.v1.html"]
        assert len(html_calls) == 1, f"calls: {st.calls}"
        args, kwargs = html_calls[0][1], html_calls[0][2]
        assert kwargs.get("height") == 0
        assert "var FORCE = true;" in args[0]


# ─── integration: _render_chat_tab wiring ───────────────────────────────────

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
    """Seed session state, patch dependencies and record autoscroll calls."""
    import ui.pages.orchestrator as orch_page

    orch = {
        "slug": "o1", "name": "DevAgent", "description": "",
        "is_builtin": True, "prompt_text": "",
        "config": {"strong_service": "Svc", "strong_model": "m1"},
    }
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

    calls: list = []
    monkeypatch.setattr(orch_page, "render_autoscroll",
                        lambda force=False: calls.append(force))

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
    return orch_page, calls


def _render(orch_page):
    try:
        orch_page._render_chat_tab("o1", "English")
    except StopRerun:
        pass


def test_idle_chat_does_not_scroll(ui_env, monkeypatch):
    """With no active agent and no pending send the feed is left alone."""
    orch_page, calls = _chat_env(ui_env, monkeypatch)
    _render(orch_page)
    assert calls == []


def test_active_agent_scrolls(ui_env, monkeypatch):
    """A running agent keeps the feed following (non-forced follow)."""
    orch_page, calls = _chat_env(
        ui_env, monkeypatch, loop_state=_LoopState("running"))
    _render(orch_page)
    assert calls == [False]


def test_forced_scroll_on_send_clears_flag(ui_env, monkeypatch):
    """A just-sent message forces one scroll and resets the flag."""
    orch_page, calls = _chat_env(ui_env, monkeypatch, scroll_bottom=True)
    _render(orch_page)
    assert calls == [True]
    assert ui_env.session_state["orch_o1_scroll_bottom"] is False
