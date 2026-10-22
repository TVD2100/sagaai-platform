# -*- coding: utf-8 -*-
"""
Scenario: a save confirmation is actually VISIBLE to the user.

Given a settings page with a Save button,
when the user edits a value and clicks Save (the page saves and then calls
``st.rerun()``),
then the confirmation message must appear on the next render pass - not be
discarded together with the rerun that the save triggers.

Before this fix every such page called ``st.success(...)`` immediately before
``st.rerun()``, so the message was never painted at all.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from tests._st_mock import install_streamlit_mock, StopRerun


@pytest.fixture
def st_env():
    """Fresh Streamlit mock plus a clean ``ui``/``core`` import state."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as mock:
        mock.session_state.update({"ui_lang": "English"})
        yield mock


def _painted(mock, kind):
    """Messages painted through the given Streamlit call, in order."""
    return [
        args[0] if args else ""
        for name, args, _kwargs in mock.calls
        if name == kind
    ]


def _render(page_callable):
    """Run one render pass, swallowing the rerun unwind."""
    try:
        page_callable()
    except StopRerun:
        pass


def test_prompt_save_confirmation_survives_the_rerun(st_env):
    """Given the prompt tab, when Save is clicked, then the next pass shows it.

    The page queues the message and raises rerun; the framework then starts a
    new pass, where ``render_flash()`` (called by ui.app.main before the page
    dispatch) paints the queued confirmation.
    """
    from ui.components.flash import render_flash
    from ui.pages import orchestrator as orch_mod

    orch_mod.st = st_env
    st_env.click("orch_save_prompt_devagent")

    with patch.object(orch_mod, "get_orchestrator",
                      return_value={"slug": "devagent", "name": "DevAgent",
                                    "prompt_text": "old prompt",
                                    "is_builtin": True, "config": {}}), \
         patch.object(orch_mod, "save_orchestrator", return_value=True), \
         patch.object(orch_mod, "t", side_effect=lambda key, **k: key):
        _render(lambda: orch_mod._render_prompt_settings("devagent", "English"))

    # The save pass itself painted nothing and requested a rerun.
    assert _painted(st_env, "success") == []
    assert st_env.rerun_count == 1

    # Next pass: the framework paints the queued confirmation.
    render_flash()
    assert _painted(st_env, "success") == ["orch_save_prompt_ok"]

    # And it is painted exactly once.
    render_flash()
    assert _painted(st_env, "success") == ["orch_save_prompt_ok"]


def test_connector_save_confirmation_survives_the_rerun(st_env):
    """Given a connection edit form, when saved, then the toast is visible."""
    from ui.components.flash import render_flash
    from ui.pages import connectors as conn_mod

    conn_mod.st = st_env
    # The edit form is a full-page view: session_state selects it and names
    # the connection it edits.
    st_env.session_state[conn_mod.VIEW_KEY] = conn_mod.VIEW_EDIT
    st_env.session_state[conn_mod._EDIT_ID_KEY] = "c1"
    st_env.click("conn_edit_submit_c1")

    conn = {"id": "c1", "name": "GitHub", "service": "github_rest",
            "account": "user", "config": {}}
    with patch.object(conn_mod.connectors, "get_connection", return_value=conn), \
         patch.object(conn_mod, "update_connection", return_value=True), \
         patch.object(conn_mod, "t", side_effect=lambda key, **k: key):
        _render(lambda: conn_mod._render_edit_view("English"))

    # The save pass queued the confirmation and left the form for the list.
    assert _painted(st_env, "success") == []
    assert st_env.rerun_count == 1
    assert st_env.session_state.get(conn_mod.VIEW_KEY) == conn_mod.VIEW_LIST

    render_flash()
    assert _painted(st_env, "success") == ["connectors_saved"]


def test_skill_save_confirmation_survives_the_rerun(st_env):
    """Given the skill editor, when Save succeeds, then the toast is visible."""
    from ui.components.flash import render_flash
    from ui.pages import skills_library as slib_mod

    slib_mod.st = st_env
    st_env.session_state["slib_edit_id"] = "s1"
    st_env.click("slib_save_btn")

    skill = {"id": "s1", "name": "Alpha", "folder": "alpha",
             "description": "d"}
    with patch.object(slib_mod, "get_skill", return_value=skill), \
         patch.object(slib_mod, "update_skill", return_value=True), \
         patch.object(slib_mod, "t", side_effect=lambda key, **k: key):
        _render(lambda: slib_mod._render_edit_form("English"))

    assert _painted(st_env, "success") == []

    render_flash()
    assert _painted(st_env, "success") == ["slib_saved"]


def test_orchestrator_delete_confirmation_survives_the_rerun(st_env):
    """Given the employee list, when a delete succeeds, then it is announced."""
    from ui.components.flash import render_flash
    from ui.pages import orchestrators as orch_page

    orch_page.st = st_env
    st_env.session_state["orch_mgmt_confirm_delete"] = "custom"
    st_env.click("orch_mgmt_del_yes_custom")

    orch = {"slug": "custom", "name": "Custom", "is_builtin": False}
    with patch.object(orch_page, "list_orchestrators", return_value=[orch]), \
         patch.object(orch_page, "delete_orchestrator", return_value=True), \
         patch.object(orch_page, "t", side_effect=lambda key, **k: key):
        _render(lambda: orch_page.page_orchestrators())

    assert _painted(st_env, "success") == []

    render_flash()
    assert _painted(st_env, "success") == ["orch_mgmt_deleted"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
