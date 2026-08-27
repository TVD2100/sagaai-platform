# -*- coding: utf-8 -*-
"""tests/test_thread_deeplink.py - unit tests for the employee thread

deep-link handler in ui.app (Copy-URL feature).

The orchestrator chat toolbar copies deep links of the form
``?orchestrator=<slug>&thread=<tid>``.  On the first run with such a link
``ui.app._handle_thread_deeplink`` must:

- initialise the orchestrator session state,
- load the thread (or reset to a fresh dialog when the thread parameter is
  missing / invalid),
- switch the current page to the orchestrator chat,
- record the orchestrator as the last active entity,
- remove the parameters from the URL so a later reload does not repeat the
  navigation.

The handler is deliberately idempotent: a second run in the same session
must be a no-op.
"""

import sys
from unittest.mock import patch

import pytest


def _drop_ui_modules():
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)


@pytest.fixture
def deeplink_context():
    """Import ui.app under the shared Streamlit mock; yield (app, mock)."""
    import importlib
    from tests._st_mock import install_streamlit_mock

    _drop_ui_modules()
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.clear()
        st_mock.query_params.clear()
        app_mod = importlib.import_module("ui.app")
        yield app_mod, st_mock


# ---- navigation & thread handling ────────────────────────────────────

def test_deeplink_navigates_and_loads_thread(deeplink_context):
    """A full deep link loads the dialog and clears the URL params."""
    app_mod, st_mock = deeplink_context
    st_mock.query_params.update({
        "orchestrator": "dev_agent",
        "thread": "orch-t-7",
    })
    with patch("ui.pages.orchestrator._init_orch_state") as init_fn:
        with patch("ui.pages.orchestrator._load_thread") as load_fn:
            with patch("ui.pages.orchestrator._reset_dialog") as reset_fn:
                app_mod._handle_thread_deeplink()
    init_fn.assert_called_once_with("dev_agent")
    load_fn.assert_called_once_with("dev_agent", "orch-t-7")
    reset_fn.assert_not_called()
    assert st_mock.session_state["current_page"] == "orchestrator:dev_agent"
    assert st_mock.session_state["last_active_entity_type"] == "orchestrator"
    assert st_mock.session_state["last_active_entity_id"] == "dev_agent"
    assert st_mock.session_state["_thread_deeplink_handled"] is True
    assert "orchestrator" not in st_mock.query_params
    assert "thread" not in st_mock.query_params


def test_deeplink_without_thread_starts_fresh_dialog(deeplink_context):
    """A slug-only link opens a fresh dialog for that employee."""
    app_mod, st_mock = deeplink_context
    st_mock.query_params["orchestrator"] = "custom1"
    with patch("ui.pages.orchestrator._init_orch_state") as init_fn:
        with patch("ui.pages.orchestrator._reset_dialog") as reset_fn:
            app_mod._handle_thread_deeplink()
    init_fn.assert_called_once_with("custom1")
    reset_fn.assert_called_once_with("custom1")
    assert st_mock.session_state["current_page"] == "orchestrator:custom1"
    assert "orchestrator" not in st_mock.query_params


def test_deeplink_unknown_thread_falls_back_to_fresh_dialog(deeplink_context):
    """A corrupt / unknown thread id must not crash: reset instead."""
    app_mod, st_mock = deeplink_context
    st_mock.query_params.update({"orchestrator": "dev_agent", "thread": "bad-tid"})
    with patch("ui.pages.orchestrator._init_orch_state") as init_fn:
        with patch("ui.pages.orchestrator._load_thread",
                   side_effect=ValueError("no such thread")) as load_fn:
            with patch("ui.pages.orchestrator._reset_dialog") as reset_fn:
                app_mod._handle_thread_deeplink()
    init_fn.assert_called_once_with("dev_agent")
    load_fn.assert_called_once_with("dev_agent", "bad-tid")
    reset_fn.assert_called_once_with("dev_agent")
    assert st_mock.session_state["current_page"] == "orchestrator:dev_agent"
    assert "thread" not in st_mock.query_params


# ---- idempotency / no-params ─────────────────────────────────────────

def test_deeplink_without_params_is_a_noop(deeplink_context):
    """No orchestrator parameter means nothing happens."""
    app_mod, st_mock = deeplink_context
    app_mod._handle_thread_deeplink()
    assert st_mock.session_state.get("_thread_deeplink_handled") is None
    assert st_mock.session_state.get("current_page") is None


def test_deeplink_runs_only_once_per_session(deeplink_context):
    """A second run with (stale) params must not navigate again."""
    app_mod, st_mock = deeplink_context
    st_mock.query_params["orchestrator"] = "dev_agent"
    with patch("ui.pages.orchestrator._init_orch_state") as init_fn:
        app_mod._handle_thread_deeplink()
    assert st_mock.session_state["current_page"] == "orchestrator:dev_agent"
    # Simulate a later manual reload with stale params still in the URL.
    st_mock.query_params["orchestrator"] = "custom2"
    with patch("ui.pages.orchestrator._init_orch_state") as init_fn2:
        app_mod._handle_thread_deeplink()
    init_fn2.assert_not_called()
    assert st_mock.session_state["current_page"] == "orchestrator:dev_agent"
