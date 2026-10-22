# -*- coding: utf-8 -*-
"""
Scenario tests: the Connectors page as a user walks it.

Both scenarios go through the public UI entry point page_connectors() and
persist into a throwaway DATA_DIR, so the assertions describe real user
outcomes (what is stored, what is rendered) rather than mock calls.

Scenario 1 - MCP presets and a custom server:
  given  the Connectors page with exactly one saved MCP connection;
  when   the user clicks a preset tile;
  then   the create form opens on the same page, NOTHING is stored yet,
         the name is prefilled and the credential field is shown;
  when   the user types the token and presses Create;
  then   the connection is stored as ACTIVE (not a credential-less draft),
         the page returns to the Connections tab and the card is listed;
  when   the user opens the custom-server tile and submits the form;
  then   a stdio MCP connection is stored too.

Scenario 2 - a general service tile:
  given  the Connectors page;
  when   the user clicks the GitHub tile and then cancels the form;
  then   nothing is stored;
  when   the user repeats the click, fills the token and presses Create;
  then   an active GitHub connection appears on the Connections tab and the
         plaintext token is never rendered.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

import pytest

from tests._st_mock import install_streamlit_mock, StopRerun

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_EN_LANG = {"English": os.path.join(_REPO_ROOT, "defaults", "langs", "en.json")}


@pytest.fixture()
def st_mock(isolated_data_dir):
    """Fresh Streamlit mock with freshly imported ui.* modules."""
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


@pytest.fixture()
def isolated_data_dir():
    """Point DATA_DIR at a throwaway temp directory (real manifest writes)."""
    tmp = tempfile.mkdtemp(prefix="sagaai_mcp_tab_scenario_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_data_dir = core.paths.DATA_DIR
    core.paths.DATA_DIR = tmp

    yield tmp

    core.paths.DATA_DIR = old_data_dir
    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    shutil.rmtree(tmp, ignore_errors=True)


def _page():
    from ui.pages import connectors as page
    return page


def _render(page):
    try:
        page.page_connectors()
    except StopRerun:
        pass


def _tab_labels(st):
    """Return the labels of the rendered st.tabs call (first positional arg)."""
    labels = []
    for name, args, kwargs in st.calls:
        if name == "tabs":
            labels.extend(list(args[0]))
    return labels


def _errors(st):
    return list(st.errors)


def _flash_queue(st):
    from ui.components.flash import FLASH_KEY
    return st.session_state.get(FLASH_KEY) or []


def _mcp_connections():
    from core import connectors
    return [c for c in connectors.list_connections()
            if c.get("service") == "mcp"]


def _vkusvill_token(conn_id):
    """Return the decrypted VKUSVILL_TOKEN stored on a connection."""
    from core import connectors
    raw = connectors.get_connection_secrets(conn_id).get("env_json", "")
    return json.loads(raw).get("VKUSVILL_TOKEN") if raw else None


def test_mcp_preset_and_custom_server_scenario(st_mock, isolated_data_dir):
    """Preset tile -> form -> active connection; custom tile -> stdio server."""
    page = _page()

    # given: the Connectors page with its three tabs.
    _render(page)
    assert _tab_labels(st_mock) == ["General services", "MCP servers",
                                    "Connections"]
    assert _mcp_connections() == []

    # when: the user clicks the Vkusvill preset tile.
    st_mock.click("mcp_preset_tile_vkusvill")
    _render(page)

    # then: the create form is open and NOTHING is stored yet.
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_CREATE
    assert _mcp_connections() == []
    assert not _errors(st_mock)

    st_mock.reset_clicks()
    _render(page)
    assert "conn_new_name" in [c[2].get("key") for c in st_mock.calls
                                if c[0] == "text_input"]
    assert "conn_new_mcp_secret_token" in [c[2].get("key")
                                            for c in st_mock.calls
                                            if c[0] == "text_input"]

    # when: the user fills the token and presses Create.
    st_mock._text_returns["conn_new_mcp_secret_token"] = "tok_scenario"
    st_mock.click("conn_new_submit")
    _render(page)

    # then: the connection is stored ACTIVE with the typed credential, the
    # form is gone and the list is back on the Connections tab.
    conns = _mcp_connections()
    assert len(conns) == 1
    created = conns[0]
    assert created["credentials_status"] == "active"
    assert created["config"]["preset"] == "vkusvill"
    assert _vkusvill_token(created["id"]) == "tok_scenario"
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_LIST
    assert st_mock.session_state.get(page._TARGET_TAB_KEY) == 2
    assert [m["kind"] for m in _flash_queue(st_mock)][-1] == "success"

    st_mock.reset_clicks()
    _render(page)
    headers = [c[1][0] for c in st_mock.calls
               if c[0] == "expander" and len(c[1]) == 1]
    assert any("Vkusvill" in h for h in headers)
    # An active connection no longer asks for credentials.
    warnings = [c[1][0] for c in st_mock.calls if c[0] == "warning"]
    assert not any("credentials required" in w for w in warnings)

    # when: the user opens the custom-server tile and submits the form.
    st_mock.reset_clicks()
    st_mock.click("mcp_custom_tile")
    _render(page)
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_CREATE
    assert len(_mcp_connections()) == 1  # still nothing new

    st_mock.reset_clicks()
    st_mock._text_returns.update({
        "conn_new_name": "Local demo server",
        "conn_new_mcp_command": "python",
        "conn_new_mcp_args": "mcp_demo_server.py",
    })
    st_mock.click("conn_new_submit")
    _render(page)

    # then: the custom stdio server is stored with its transport config.
    conns = _mcp_connections()
    assert len(conns) == 2
    custom = next(c for c in conns if c["name"] == "Local demo server")
    assert custom["config"] == {"transport": "stdio", "command": "python",
                               "args": ["mcp_demo_server.py"]}


def test_general_service_tile_scenario(st_mock, isolated_data_dir):
    """Tile -> cancel writes nothing; tile -> filled form creates a card."""
    from core import connectors

    page = _page()

    # given: the general services tab offers GitHub and SSH, no MCP.
    _render(page)
    tile_keys = {c[2].get("key") for c in st_mock.calls
                 if c[0] == "button" and (c[2].get("key") or "").startswith(
                     "conn_service_tile_")}
    assert "conn_service_tile_github_rest" in tile_keys
    assert "conn_service_tile_ssh" in tile_keys
    assert "conn_service_tile_mcp" not in tile_keys

    # when: the user clicks the GitHub tile and cancels the form.
    st_mock.click("conn_service_tile_github_rest")
    _render(page)
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_CREATE
    assert st_mock.session_state.get(page._CREATE_SERVICE_KEY) == "github_rest"

    st_mock.reset_clicks()
    st_mock.click("conn_new_cancel")
    _render(page)

    # then: nothing was stored and the list is back.
    assert conns_of("github_rest") == []
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_LIST

    # when: the user repeats the click, fills the token and presses Create.
    st_mock.reset_clicks()
    st_mock.click("conn_service_tile_github_rest")
    _render(page)
    st_mock.reset_clicks()
    st_mock._text_returns.update({
        "conn_new_name": "Scenario GitHub",
        "conn_new_token": "ghp_scenario_token",
        "conn_new_account": "TVD2100",
    })
    st_mock.click("conn_new_submit")
    _render(page)

    # then: an active GitHub connection appears on the Connections tab and
    # the plaintext token is never rendered.
    conns = conns_of("github_rest")
    assert len(conns) == 1
    assert conns[0]["name"] == "Scenario GitHub"
    assert conns[0]["has_token"] is True
    assert conns[0]["credentials_status"] == "active"

    st_mock.reset_clicks()
    _render(page)
    headers = [c[1][0] for c in st_mock.calls
               if c[0] == "expander" and len(c[1]) == 1]
    assert any("Scenario GitHub" in h for h in headers)
    rendered = " ".join(headers)
    assert "token set" in rendered
    assert "ghp_scenario_token" not in rendered


def conns_of(service):
    """Return saved connections of one service (helper for the scenario)."""
    from core import connectors
    return [c for c in connectors.list_connections()
            if c.get("service") == service]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
