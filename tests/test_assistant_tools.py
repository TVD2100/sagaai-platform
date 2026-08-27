"""
UI tests: assistant form renders tools filtered by provider capabilities.
"""
from __future__ import annotations

import sys
import pytest
from unittest.mock import patch

from tests._st_mock import install_streamlit_mock, StopRerun

TOOL_CATALOG_MOCK = [{"name": "web_search"}, {"name": "read_file"}]




@pytest.fixture
def env():
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.clear()
        st_mock.session_state.update({"ui_lang": "English", "current_page": "run"})
        with patch("core.i18n.t") as mock_t:
            mock_t.side_effect = lambda key, *a, **kw: key
            yield st_mock


def _invoke(page_fn, **session_state):
    """Call *page_fn* inside a try/except that swallows StopRerun."""
    import streamlit as st
    for k, v in session_state.items():
        st.session_state[k] = v
    try:
        page_fn()
    except StopRerun:
        pass


def _tools_multiselect_options(env):
    """Return the options arg of every st.multiselect call."""
    return [
        kwargs.get("options")
        for name, args, kwargs in env.calls
        if name == "multiselect"
    ]


def _caption_texts(env):
    """Return the label texts of every st.caption call."""
    return [
        args[0] if args else kwargs.get("label")
        for name, args, kwargs in env.calls
        if name == "caption"
    ]


def test_assistant_form_shows_only_provider_tools(env):
    """A provider with tools_options=web_search must show only web_search."""
    services = {
        "YandexAI": {
            "auth_type": "yandex_iam",
            "base_url": "https://mock",
            "config_key": "k",
            "config_key2": "k2",
            "models": [{"id": "m1"}],
            "temp_min": 0, "temp_max": 1, "temp_step": 0.1,
            "max_tokens_default": 32000,
            "tools_options": [{"key": "web_search"}],
        },
    }
    with patch("core.services.get_services", return_value=services), \
         patch("ui.pages.assistants.list_tool_definitions", return_value=TOOL_CATALOG_MOCK), \
         patch("ui.pages.assistants.list_rag_bases", return_value=[]):
        from ui.pages.assistants import page_assistants
        _invoke(page_assistants, show_assistant_form=True, edit_assistant_id=None)

    options_list = _tools_multiselect_options(env)
    assert options_list, "tools multiselect was not rendered"
    assert options_list[0] == ["web_search"]


def test_assistant_form_hides_tools_unsupported_provider(env):
    """A provider without tools_options must not render the tools multiselect."""
    services = {
        "GigaChat": {
            "auth_type": "gigachat_oauth",
            "base_url": "https://mock",
            "config_key": "k",
            "models": [{"id": "g1"}],
            "temp_min": 0.1, "temp_max": 2.0, "temp_step": 0.1,
            "max_tokens_default": 32768,
        },
    }
    with patch("core.services.get_services", return_value=services), \
         patch("ui.pages.assistants.list_tool_definitions", return_value=TOOL_CATALOG_MOCK), \
         patch("ui.pages.assistants.list_rag_bases", return_value=[]):
        from ui.pages.assistants import page_assistants
        _invoke(page_assistants, show_assistant_form=True, edit_assistant_id=None)

    assert _tools_multiselect_options(env) == []
    assert any("tools_not_supported" in text for text in _caption_texts(env))
