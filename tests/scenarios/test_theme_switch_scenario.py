# -*- coding: utf-8 -*-
"""tests/scenarios/test_theme_switch_scenario.py - scenario tests for the
theme-switch UI-restore flow.

User-level scenarios (given -> when -> then) walking the public UI entry
points of the fix:

  Scenario 1 - switching the theme on an assistant chat returns to the same
               chat after the browser reload.
  Scenario 2 - switching the theme on an orchestrator dialog returns to the
               same orchestrator dialog.
  Scenario 3 - a stale _sagaai_ui_restore marker in the URL does not
               resurrect a dialog on a later manual reload.
"""
import json
import sys
from unittest.mock import patch

import pytest

from tests._st_mock import install_streamlit_mock


def _drop_ui_modules():
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)


def _fresh_app(settings=None):
    """Import ui.app under the Streamlit mock with a seeded session."""
    import importlib
    _drop_ui_modules()
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.clear()
        st_mock.query_params.clear()
        st_mock.session_state.update(settings or {})
        app_mod = importlib.import_module("ui.app")
        return app_mod, st_mock


# ─── Scenario 1: assistant chat survives the theme switch ──────────────────

def test_theme_switch_returns_to_assistant_chat():
    """
    Given a user is in an assistant chat (page run, thread t-1,
          assistant a-1 selected),
    when  the user switches the theme to Dark,
    then  the browser is replaced with a URL carrying the restore snapshot,
          and applying that snapshot restores page, thread and assistant.
    """
    app_mod, st_mock = _fresh_app({
        "current_page": "run",
        "active_thread_id": "t-1",
        "selected_assistant_id": "a-1",
        "selected_skill_id": "a-1",
        "last_active_entity_type": "assistant",
        "last_active_entity_id": "a-1",
    })

    payload = app_mod._build_ui_restore_payload()
    app_mod._apply_theme("Dark", payload)

    html_calls = [c for c in st_mock.calls if c[0] == "html"]
    assert html_calls, "theme switch must emit the JS payload"
    js = html_calls[0][1][0] if html_calls[0][1] else ""
    assert "_sagaai_ui_restore" in js
    assert "location.replace" in js

    # Simulate the browser reload: same session, marker now in the URL.
    st_mock.query_params["_sagaai_ui_restore"] = payload
    app_mod._restore_ui_reload_state()
    assert st_mock.session_state["current_page"] == "run"
    assert st_mock.session_state["active_thread_id"] == "t-1"
    assert st_mock.session_state["selected_assistant_id"] == "a-1"
    assert "_sagaai_ui_restore" not in st_mock.query_params


# ─── Scenario 2: orchestrator dialog survives the theme switch ──────────────

def test_theme_switch_returns_to_orchestrator_dialog():
    """
    Given a user is in the DevAgent orchestrator dialog with thread orch-t-2,
    when  the user switches the theme to Light,
    then  the restore re-initialises the orchestrator state and reloads the
          same thread, keeping the user on the orchestrator page.
    """
    app_mod, st_mock = _fresh_app({
        "current_page": "orchestrator:dev_agent",
        "orch_dev_agent_thread_id": "orch-t-2",
        "last_active_entity_type": "orchestrator",
        "last_active_entity_id": "dev_agent",
    })

    payload = app_mod._build_ui_restore_payload()
    data = json.loads(payload)
    assert data["orch_thread_id"] == "orch-t-2"

    app_mod._apply_theme("Light", payload)
    st_mock.query_params["_sagaai_ui_restore"] = payload
    with patch("ui.pages.orchestrator._init_orch_state") as init_fn:
        with patch("ui.pages.orchestrator._load_thread") as load_fn:
            app_mod._restore_ui_reload_state()

    init_fn.assert_called_once_with("dev_agent")
    load_fn.assert_called_once_with("dev_agent", "orch-t-2")
    assert st_mock.session_state["current_page"] == "orchestrator:dev_agent"


# ─── Scenario 3: stale marker is inert on a later manual reload ─────────────

def test_stale_restore_marker_does_not_resurrect_dialog():
    """
    Given a restore has already been applied once in this browser session,
    when  the user manually reloads (F5) while the old marker is still in
          the URL,
    then  the handled guard keeps the current state and does not reapply
          the old snapshot.
    """
    app_mod, st_mock = _fresh_app({
        "current_page": "run",
        "active_thread_id": "t-1",
        "selected_assistant_id": "a-1",
        "selected_skill_id": "a-1",
    })

    st_mock.query_params["_sagaai_ui_restore"] = json.dumps({
        "page": "run",
        "active_thread_id": "t-1",
        "selected_assistant_id": "a-1",
    })
    app_mod._restore_ui_reload_state()
    assert st_mock.session_state["_theme_restore_handled"] is True

    # User switches to a different assistant, then manually reloads.
    st_mock.query_params["_sagaai_ui_restore"] = json.dumps({
        "page": "run",
        "active_thread_id": "t-9",
        "selected_assistant_id": "a-9",
    })
    app_mod._restore_ui_reload_state()
    assert st_mock.session_state["active_thread_id"] == "t-1"
    assert st_mock.session_state["selected_assistant_id"] == "a-1"

