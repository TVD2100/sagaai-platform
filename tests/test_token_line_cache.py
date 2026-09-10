# -*- coding: utf-8 -*-
"""tests/test_token_line_cache.py - cached token/economy indicator.

The expensive token/economy line of the orchestrator chat tab must not be
recomputed on every rerun: the page caches the rendered HTML keyed by the
dialog state (thread id, history, economy config, loop phase, prompt,
attachments, services) and only recomputes when one of those inputs changes.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
        # Suppress the chat-input flow: the mock's catch-all stub returns a
        # truthy object, which would trigger the send path on every render.
        def _no_input(*args, **kwargs):
            st._rec("chat_input", args, kwargs)
            return ""
        st.chat_input = _no_input
        yield st


def _rerender(st, fn):
    try:
        fn()
    except StopRerun:
        pass


def _mk(i: int) -> dict:
    return {
        "role": "user" if i % 2 == 0 else "assistant",
        "content": f"message-{i}",
        "ts": f"2026-01-01T00:00:{i:02d}",
    }


def _token_markdowns(st):
    """Markdown calls that carry the token/economy indicator line."""
    return [c[1][0] for c in st.calls
            if c[0] == "markdown" and "Context: current" in c[1][0]]


def _setup_page(ui_env, monkeypatch, slug):
    """Wire the chat page with counting stubs for the expensive parts."""
    import ui.pages.orchestrator as orch_page

    orch = {
        "slug": slug, "name": "Custom", "description": "",
        "is_builtin": False, "prompt_text": "You are a test assistant.",
        "config": {"strong_service": "Mock", "strong_model": "m1"},
    }
    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: orch if s == slug else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    monkeypatch.setattr(
        orch_page, "build_assistant_dicts",
        lambda s: ({"service": "Mock", "model": "m1", "temperature": 0.1},
                   {"service": "Mock", "model": "m1", "temperature": 0.1}))
    monkeypatch.setattr(orch_page, "get_economy_config",
                        lambda s: {"tail_messages": 30, "cache_enabled": True,
                                   "cache_multiplier": 3})

    counter = {"check_context": 0, "sum_thread_tokens": 0}

    def fake_check_context(*args, **kwargs):
        counter["check_context"] += 1
        return {"total_tokens": 1234}

    def fake_sum_thread_tokens(history):
        counter["sum_thread_tokens"] += 1
        return (100, 50, 10)

    monkeypatch.setattr(orch_page, "check_context", fake_check_context)
    monkeypatch.setattr(orch_page, "sum_thread_tokens", fake_sum_thread_tokens)
    return orch_page, counter


def _render_chat(ui_env, slug, history):
    import ui.pages.orchestrator as orch_page

    st = ui_env
    st.session_state.update({"ui_lang": "English"})
    st.session_state[f"orch_{slug}_history"] = list(history)
    st.session_state[f"orch_{slug}_economy_mode"] = True
    st.session_state[f"orch_{slug}_web_search"] = False
    st.session_state[f"orch_{slug}_safety_mode"] = True
    _rerender(st, lambda: orch_page.page_orchestrator(slug))


def test_token_line_cached_across_rerenders(monkeypatch, ui_env):
    """Same dialog state on the next rerun reuses the cached HTML."""
    slug = "custom1"
    orch_page, counter = _setup_page(ui_env, monkeypatch, slug)
    st = ui_env

    _render_chat(ui_env, slug, [_mk(i) for i in range(3)])
    assert counter["check_context"] == 1
    assert counter["sum_thread_tokens"] == 1
    assert len(_token_markdowns(st)) == 1
    cache1 = st.session_state.get(f"orch_{slug}_token_line_cache")
    assert isinstance(cache1, dict) and cache1.get("html")

    # Unchanged inputs: no recomputation, the line is still rendered.
    _render_chat(ui_env, slug, [_mk(i) for i in range(3)])
    assert counter["check_context"] == 1
    assert counter["sum_thread_tokens"] == 1
    assert len(_token_markdowns(st)) == 2
    cache2 = st.session_state[f"orch_{slug}_token_line_cache"]
    assert cache2["key"] == cache1["key"]


def test_token_line_recomputed_when_history_changes(monkeypatch, ui_env):
    """New history content changes the cache key and recomputes the line."""
    slug = "custom1"
    orch_page, counter = _setup_page(ui_env, monkeypatch, slug)
    st = ui_env

    _render_chat(ui_env, slug, [_mk(i) for i in range(3)])
    assert counter["check_context"] == 1
    cache1 = st.session_state[f"orch_{slug}_token_line_cache"]

    _render_chat(ui_env, slug, [_mk(i) for i in range(4)])
    assert counter["check_context"] == 2
    assert counter["sum_thread_tokens"] == 2
    cache2 = st.session_state[f"orch_{slug}_token_line_cache"]
    assert cache2["key"] != cache1["key"]
    assert len(_token_markdowns(st)) == 2


def test_reset_dialog_clears_token_line_cache(monkeypatch, ui_env):
    """_reset_dialog drops the cached indicator along with the history."""
    slug = "custom1"
    orch_page, counter = _setup_page(ui_env, monkeypatch, slug)
    st = ui_env

    _render_chat(ui_env, slug, [_mk(i) for i in range(3)])
    assert isinstance(st.session_state[f"orch_{slug}_token_line_cache"], dict)

    orch_page._reset_dialog(slug)
    assert st.session_state[f"orch_{slug}_token_line_cache"] is None
    assert st.session_state[f"orch_{slug}_history"] == []
