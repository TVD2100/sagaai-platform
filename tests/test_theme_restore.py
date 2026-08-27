# -*- coding: utf-8 -*-
"""tests/test_theme_restore.py - unit tests for the theme-switch UI-restore
mechanism in ui.app.

Covers the three building blocks added for the theme-switch fix:
- _build_ui_restore_payload serialises the active page/dialog snapshot,
- _apply_theme emits location.replace with the _sagaai_ui_restore marker
  via st.html(unsafe_allow_javascript=True), so the script runs in the main
  document instead of Streamlit's sandboxed component iframe,
- _restore_ui_reload_state reapplies the snapshot exactly once and clears
  the marker from the URL, without resurrecting state on later reloads.
"""

import json
import sys
from unittest.mock import patch

import pytest


def _drop_ui_modules():
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)


@pytest.fixture
def app_under_mock():
    """Import ui.app under the shared Streamlit mock; yield (module, mock)."""
    import importlib
    from tests._st_mock import install_streamlit_mock

    _drop_ui_modules()
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.clear()
        st_mock.query_params.clear()
        app_mod = importlib.import_module("ui.app")
        yield app_mod, st_mock


# ---- payload building ────────────────────────────────────────────────

def test_payload_for_assistant_page(app_under_mock):
    """The snapshot carries page, thread and selected assistant ids."""
    app_mod, st_mock = app_under_mock
    st_mock.session_state.update({
        "current_page": "run",
        "active_thread_id": "t-1",
        "selected_assistant_id": "a-1",
        "selected_skill_id": "a-1",
        "last_active_entity_type": "assistant",
        "last_active_entity_id": "a-1",
    })
    data = json.loads(app_mod._build_ui_restore_payload())
    assert data == {
        "page": "run",
        "active_thread_id": "t-1",
        "selected_assistant_id": "a-1",
        "last_active_entity_type": "assistant",
        "last_active_entity_id": "a-1",
    }


def test_payload_for_orchestrator_page(app_under_mock):
    """Orchestrator pages additionally carry the active thread id."""
    app_mod, st_mock = app_under_mock
    st_mock.session_state.update({
        "current_page": "orchestrator:dev_agent",
        "orch_dev_agent_thread_id": "orch-t-9",
    })
    data = json.loads(app_mod._build_ui_restore_payload())
    assert data["page"] == "orchestrator:dev_agent"
    assert data["orch_thread_id"] == "orch-t-9"


def test_payload_skips_settings_pages(app_under_mock):
    """Orchestrator SETTINGS pages are not chat dialogs - no thread marker."""
    app_mod, st_mock = app_under_mock
    st_mock.session_state.update({
        "current_page": "orchestrator_settings:dev_agent",
    })
    data = json.loads(app_mod._build_ui_restore_payload())
    assert "orch_thread_id" not in data


def test_apply_theme_uses_replace_with_restore_marker(app_under_mock):
    """Mode selection emits location.replace with the restore query param."""
    app_mod, st_mock = app_under_mock
    st_mock.session_state.update({"current_page": "run"})
    payload = app_mod._build_ui_restore_payload()
    app_mod._apply_theme("Dark", payload)
    html_calls = [c for c in st_mock.calls if c[0] == "html"]
    assert html_calls
    js = html_calls[0][1][0] if html_calls[0][1] else ""
    assert "stActiveTheme-" in js
    assert "_sagaai_ui_restore" in js
    assert "location.replace" in js
    assert "location.reload()" not in js


# ---- restore ──────────────────────────────────────────────────────────

def test_restore_reapplies_assistant_snapshot_once(app_under_mock):
    """The first run applies the snapshot and clears the URL marker."""
    app_mod, st_mock = app_under_mock
    st_mock.query_params["_sagaai_ui_restore"] = json.dumps({
        "page": "run",
        "active_thread_id": "t-7",
        "selected_assistant_id": "a-3",
    })
    app_mod._restore_ui_reload_state()
    assert st_mock.session_state["current_page"] == "run"
    assert st_mock.session_state["active_thread_id"] == "t-7"
    assert st_mock.session_state["selected_assistant_id"] == "a-3"
    assert st_mock.session_state["selected_skill_id"] == "a-3"
    assert st_mock.session_state["_theme_restore_handled"] is True
    assert "_sagaai_ui_restore" not in st_mock.query_params

    # A later reload with a stale marker must NOT resurrect the old state.
    st_mock.query_params["_sagaai_ui_restore"] = json.dumps({"page": "welcome"})
    app_mod._restore_ui_reload_state()
    assert st_mock.session_state["current_page"] == "run"


def test_restore_without_marker_leaves_state_untouched(app_under_mock):
    """No marker in the URL means no restore record at all."""
    app_mod, st_mock = app_under_mock
    app_mod._restore_ui_reload_state()
    assert st_mock.session_state.get("_theme_restore_handled") is None
    assert st_mock.session_state.get("current_page") is None


def test_restore_orchestrator_reloads_thread(app_under_mock):
    """Orchestrator snapshots re-init state and reload the saved thread."""
    app_mod, st_mock = app_under_mock
    st_mock.query_params["_sagaai_ui_restore"] = json.dumps({
        "page": "orchestrator:dev_agent",
        "orch_thread_id": "orch-t-5",
    })
    with patch("ui.pages.orchestrator._init_orch_state") as init_fn:
        with patch("ui.pages.orchestrator._load_thread") as load_fn:
            app_mod._restore_ui_reload_state()
    init_fn.assert_called_once_with("dev_agent")
    load_fn.assert_called_once_with("dev_agent", "orch-t-5")
    assert st_mock.session_state["current_page"] == "orchestrator:dev_agent"
    assert "_sagaai_ui_restore" not in st_mock.query_params

