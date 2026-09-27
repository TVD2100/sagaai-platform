# -*- coding: utf-8 -*-
"""tests/test_ui_connectors_ssh.py - Connectors page: SSH support tests.

Renders ui.pages.connectors under the Streamlit mock and verifies that:
  - the create form switches to SSH-specific fields (host/port/username +
    password / private key / passphrase) when the ssh service is selected,
    and keeps the GitHub token flow otherwise;
  - submitting the SSH form passes config + non-empty secrets to
    core.connectors.create_connection;
  - the edit form of an SSH connection renders its config fields and the
    save handler calls update_connection with config/secrets;
  - SSH connection cards show the user@host:port target and the masked
    secret field names;
  - the "test connection" button routes ssh connections through
    core.ssh_connector.test_connection on both the success and the failure
    path.
"""
from __future__ import annotations

import os
import sys

import pytest
from unittest.mock import MagicMock, patch

from tests._st_mock import install_streamlit_mock, StopRerun

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EN_LANG = {"English": os.path.join(_REPO_ROOT, "defaults", "langs", "en.json")}

SSH_CONN = {
    "id": "ssh00001",
    "service": "ssh",
    "name": "Prod server",
    "account": "deploy@203.0.113.10",
    "config": {"host": "203.0.113.10", "port": 2222, "username": "deploy"},
    "has_token": False,
    "token_masked": "",
    "has_secrets": True,
    "secrets_masked": {"password": "***"},
    "created_at": "2026-09-27T12:00:00+00:00",
}


@pytest.fixture()
def st_mock():
    """Fresh Streamlit mock with freshly imported ui.* modules.

    Drops ui.* modules before installing the mock (so the page binds to THIS
    mock instance) and after the test (so later test files re-import
    cleanly under their own mock).
    """
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st:
        with patch("core.i18n.get_langs", return_value=dict(_EN_LANG)):
            st.session_state.update({"ui_lang": "English"})
            yield st
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)


def _page():
    """Import the connectors page inside the active streamlit mock."""
    from ui.pages import connectors as page
    return page


def _render_page(page, connections):
    """Render page_connectors() with list_connections patched."""
    with patch.object(page, "list_connections", return_value=list(connections)):
        try:
            page.page_connectors()
        except StopRerun:
            pass


def _keys(st, widget_name):
    """Return the key= values of all recorded calls of *widget_name*."""
    return [c[2].get("key") for c in st.calls if c[0] == widget_name]


# ── create form: service-specific fields ──────────────────────────────────────

def test_create_form_renders_ssh_fields_when_ssh_selected(st_mock):
    page = _page()
    st_mock._selectbox_returns["conn_new_service"] = "ssh"
    _render_page(page, [])

    text_keys = _keys(st_mock, "text_input")
    for key in ("conn_new_host", "conn_new_username", "conn_new_password",
                "conn_new_key_passphrase"):
        assert key in text_keys, key
    assert "conn_new_private_key" in _keys(st_mock, "text_area")
    assert "conn_new_port" in _keys(st_mock, "number_input")
    # The GitHub token field must not be rendered for the ssh service.
    assert "conn_new_token" not in text_keys


def test_create_form_keeps_github_token_by_default(st_mock):
    page = _page()
    _render_page(page, [])

    text_keys = _keys(st_mock, "text_input")
    assert "conn_new_token" in text_keys
    assert "conn_new_host" not in text_keys
    assert "conn_new_private_key" not in _keys(st_mock, "text_area")


# ── create form: submit handlers ──────────────────────────────────────────────

def test_create_ssh_connection_passes_config_and_secrets(st_mock):
    page = _page()
    st_mock._selectbox_returns["conn_new_service"] = "ssh"
    st_mock._text_returns.update({
        "conn_new_name": "Prod SSH",
        "conn_new_host": "203.0.113.10",
        "conn_new_username": "deploy",
        "conn_new_password": "s3cret",
    })
    st_mock._number_returns["conn_new_port"] = 2222
    st_mock.click("conn_new_submit")

    create = MagicMock()
    with patch.object(page, "list_connections", return_value=[]), \
         patch.object(page, "create_connection", create):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    create.assert_called_once_with(
        "ssh", "Prod SSH", "",
        account="",
        config={"host": "203.0.113.10", "port": 2222, "username": "deploy"},
        secrets={"password": "s3cret"},
    )
    assert any(c[0] == "success" for c in st_mock.calls)


def test_create_github_connection_keeps_token_flow(st_mock):
    page = _page()
    st_mock._text_returns.update({
        "conn_new_name": "My GitHub",
        "conn_new_token": "ghp_secret",
    })
    st_mock.click("conn_new_submit")

    create = MagicMock()
    with patch.object(page, "list_connections", return_value=[]), \
         patch.object(page, "create_connection", create):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    create.assert_called_once_with(
        "github_rest", "My GitHub", "ghp_secret",
        account="", config=None, secrets=None,
    )


# ── connection card ───────────────────────────────────────────────────────────

def test_ssh_card_shows_target_and_masked_secrets(st_mock):
    page = _page()
    _render_page(page, [dict(SSH_CONN)])

    captions = [c[1][0] for c in st_mock.calls if c[0] == "caption"]
    assert "deploy@203.0.113.10:2222" in captions

    headers = [c[1][0] for c in st_mock.calls
               if c[0] == "expander" and len(c[1]) == 1]
    assert any("Prod server" in h and "secrets set: password" in h
               for h in headers)
    assert not any("token set" in h for h in headers)


# ── edit form ─────────────────────────────────────────────────────────────────

def test_edit_ssh_form_renders_fields_and_saves(st_mock):
    page = _page()
    st_mock.session_state["conn_edit_ssh00001"] = True
    st_mock._text_returns.update({
        "conn_edit_ssh00001_host": "203.0.113.20",
        "conn_edit_ssh00001_username": "deploy",
    })
    st_mock.click("Save")

    update = MagicMock()
    with patch.object(page, "list_connections", return_value=[dict(SSH_CONN)]), \
         patch.object(page, "update_connection", update):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    text_keys = _keys(st_mock, "text_input")
    assert "conn_edit_ssh00001_host" in text_keys
    assert "conn_edit_ssh00001_password" in text_keys
    assert "conn_edit_token_ssh00001" not in text_keys

    update.assert_called_once_with(
        "ssh00001",
        name="Prod server",
        account="deploy@203.0.113.10",
        token="",
        config={"host": "203.0.113.20", "port": 2222, "username": "deploy"},
        secrets={},
    )


# ── test-connection button routing ────────────────────────────────────────────

def test_ssh_test_button_routes_to_ssh_connector(st_mock):
    page = _page()
    st_mock.click("conn_test_ssh00001")
    ssh_test = MagicMock(return_value={
        "ok": True, "host": "203.0.113.10", "port": 2222,
        "username": "deploy",
    })

    with patch.object(page, "list_connections", return_value=[dict(SSH_CONN)]), \
         patch("core.connectors.get_connection", return_value=dict(SSH_CONN)), \
         patch("core.ssh_connector.test_connection", ssh_test):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    ssh_test.assert_called_once_with("ssh00001")
    assert not st_mock.errors
    success_args = [c[1][0] for c in st_mock.calls if c[0] == "success"]
    assert any("deploy@203.0.113.10" in a for a in success_args)


def test_ssh_test_button_reports_failure(st_mock):
    page = _page()
    st_mock.click("conn_test_ssh00001")
    ssh_test = MagicMock(side_effect=Exception("auth failed"))

    with patch.object(page, "list_connections", return_value=[dict(SSH_CONN)]), \
         patch("core.connectors.get_connection", return_value=dict(SSH_CONN)), \
         patch("core.ssh_connector.test_connection", ssh_test):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    assert st_mock.errors, "expected an st.error for the failed ssh test"
    assert "auth failed" in st_mock.errors[0]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
