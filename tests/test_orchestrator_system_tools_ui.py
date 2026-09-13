# -*- coding: utf-8 -*-
"""tests/test_orchestrator_system_tools_ui.py - system-tools block UI tests.

Renders ui.pages.orchestrator._render_orch_system_tools under the Streamlit
mock and verifies the checkbox list and the save flow of the disabled_tools
blacklist.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

TOOLS = [
    {"name": "read_file", "desc": "Read a file."},
    {"name": "list_files", "desc": "List files."},
]


@pytest.fixture
def st():
    'Fresh Streamlit mock; ui.* modules re-imported per test.'
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as mock:
        mock.session_state.update(ui_lang="English")
        yield mock


def _invoke(fn):
    try:
        fn()
    except StopRerun:
        pass


def _render(module, st, slug="demo"):
    st.click("orch_save_sysfunc_" + slug)
    _invoke(lambda: module._render_orch_system_tools(slug, "English"))


def test_render_and_save_disabled_tools(st):
    'Unchecked tools are saved as the disabled_tools blacklist.'
    import ui.pages.orchestrator as orch_mod

    calls = []
    st.session_state["orch_sysfunc_demo_list_files"] = False
    with patch.object(orch_mod, "list_system_tools", return_value=TOOLS), \
         patch.object(orch_mod, "get_disabled_tools", return_value=["read_file"]), \
         patch.object(orch_mod, "set_disabled_tools",
                      side_effect=lambda slug, names: calls.append((slug, names)) or True):
        _render(orch_mod, st)

    assert calls == [("demo", ["read_file", "list_files"])]
    checkboxes = [
        (args[0], kwargs["value"])
        for name, args, kwargs in st.calls
        if name == "checkbox"
    ]
    assert checkboxes == [("**read_file**", False), ("**list_files**", False)]
    assert any(name == "success" for name, _a, _k in st.calls)


def test_render_all_enabled_saves_empty(st):
    'All tools checked saves an empty blacklist.'
    import ui.pages.orchestrator as orch_mod

    calls = []
    with patch.object(orch_mod, "list_system_tools", return_value=TOOLS), \
         patch.object(orch_mod, "get_disabled_tools", return_value=[]), \
         patch.object(orch_mod, "set_disabled_tools",
                      side_effect=lambda slug, names: calls.append((slug, names)) or True):
        _render(orch_mod, st)

    assert calls == [("demo", [])]


def test_list_failure_shows_info(st):
    'A catalog failure renders the empty-state message, not widgets.'
    import ui.pages.orchestrator as orch_mod

    with patch.object(orch_mod, "list_system_tools", side_effect=RuntimeError("boom")):
        _invoke(lambda: orch_mod._render_orch_system_tools("demo", "English"))

    assert any(name == "info" for name, _a, _k in st.calls)
    assert not any(name == "checkbox" for name, _a, _k in st.calls)


def test_save_failure_shows_error(st):
    'A failed write shows the error message.'
    import ui.pages.orchestrator as orch_mod

    with patch.object(orch_mod, "list_system_tools", return_value=TOOLS), \
         patch.object(orch_mod, "get_disabled_tools", return_value=[]), \
         patch.object(orch_mod, "set_disabled_tools", return_value=False):
        _render(orch_mod, st)

    assert any(name == "error" for name, _a, _k in st.calls)
