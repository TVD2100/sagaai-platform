# -*- coding: utf-8 -*-
"""tests/test_ui_connectors_mcp.py - Connectors page: MCP flows and tabs.

Renders ui.pages.connectors under the Streamlit mock and verifies the
list <-> full-page form model:
  - the list shows three tabs rendered by a STATEFUL st.tabs widget
    (on_change="rerun") and re-targets a tab through ``default``;
  - the MCP tab renders a tile per preset (name prefixed with the preset
    icon) plus the custom-server tile;
  - clicking a preset tile opens the create form WITHOUT creating anything
    (no instantiate_preset call, no connection on disk); the form is
    prefilled with the preset name and shows the preset credential fields;
  - submitting the create form instantiates the preset with the typed
    credentials (pending=False) - the connection appears only now;
  - Cancel returns to the list and writes nothing;
  - the custom-server tile opens the raw transport form and submitting it
    creates a stdio connection;
  - the edit form of a preset-based MCP connection opens as a full page
    and saves env_pairs;
  - saved connections render as cards on the Connections tab; the general
    services tab uses icon tiles without mcp;
  - a connection created from a preset that is hidden from the catalogue
    keeps its preset edit form (the page resolves the preset by id) while
    the MCP tab offers no tile for it.
"""
from __future__ import annotations

import os
import sys

import pytest
from unittest.mock import MagicMock, patch

from tests._st_mock import install_streamlit_mock, StopRerun

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EN_LANG = {"English": os.path.join(_REPO_ROOT, "defaults", "langs", "en.json")}

MCP_CONN = {
    "id": "mcp000001",
    "service": "mcp",
    "name": "Vkusvill",
    "account": "",
    "config": {"transport": "http", "url": "https://mcp.vkusvill.ru/mcp",
               "preset": "vkusvill_local"},
    "has_token": False,
    "has_secrets": False,
    "secrets_masked": {},
    "credentials_status": "pending",
    "created_at": "2026-10-20T10:00:00+00:00",
}

PRESET_VKUSVILL = {
    "id": "vkusvill_local",
    "service": "mcp",
    "name": "Vkusvill local",
    "icon": "🧪",
    "description": "Local fake of the Vkusvill MCP endpoint.",
    "config": {
        "transport": "http",
        "url": "https://mcp.vkusvill.ru/mcp",
        "auth_header": "Bearer {VKUSVILL_TOKEN}",
        "secret_env_fields": {
            "token": {
                "env": "VKUSVILL_TOKEN",
                "label": "Bearer token",
                "help": "Unlocks personal data.",
                "password": True,
            },
        },
    },
    "sort_order": 10,
}

PRESETS = [PRESET_VKUSVILL]


@pytest.fixture()
def st_mock():
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


def _page():
    """Import the connectors page inside the active streamlit mock."""
    from ui.pages import connectors as page
    return page


def _render_page(page, connections, presets=None):
    """Render page_connectors() with list_connections and presets patched."""
    with patch.object(page, "list_connections", return_value=list(connections)), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(presets or PRESETS)):
        try:
            page.page_connectors()
        except StopRerun:
            pass


def _keys(st, widget_name):
    """Return the key= values of all recorded calls of *widget_name*."""
    return [c[2].get("key") for c in st.calls if c[0] == widget_name]


def _tabs_calls(st):
    """Return the kwargs of the rendered st.tabs calls."""
    return [c[2] for c in st.calls if c[0] == "tabs"]


def _button_labels(st):
    """Return the labels of all recorded button calls."""
    return [c[1][0] for c in st.calls if c[0] == "button"]


def _open_create(page, st, service, preset_id=""):
    """Put the page into the create view for *service* (as a tile click does)."""
    st.session_state[page.VIEW_KEY] = page.VIEW_CREATE
    st.session_state[page._CREATE_SERVICE_KEY] = service
    st.session_state[page._CREATE_PRESET_KEY] = preset_id


# ── tabs ─────────────────────────────────────────────────────────────────────

def test_page_renders_stateful_tabs(st_mock):
    page = _page()
    _render_page(page, [])
    labels = []
    for name, args, kwargs in st_mock.calls:
        if name == "tabs":
            labels.extend(list(args[0]))
    assert labels == ["General services", "MCP servers", "Connections"]
    kwargs = _tabs_calls(st_mock)[0]
    assert kwargs.get("key") == page.CONNECTORS_TABS_KEY
    # Without on_change="rerun" st.tabs keeps no state at all, so the key
    # would be a mere CSS identity and a rerun would fall back to tab one.
    assert kwargs.get("on_change") == "rerun"


def test_return_targets_connections_tab_through_default(st_mock):
    page = _page()
    st_mock.session_state[page._TARGET_TAB_KEY] = 2
    st_mock.session_state[page.CONNECTORS_TABS_KEY] = "General services"
    _render_page(page, [])
    kwargs = _tabs_calls(st_mock)[0]
    assert kwargs.get("default") == "Connections"
    # The stale stored value is dropped before the widget is created, which
    # is the only moment Streamlit accepts a state reset for a widget.
    assert page.CONNECTORS_TABS_KEY not in st_mock.session_state
    assert page._TARGET_TAB_KEY not in st_mock.session_state


# ── MCP tab: preset tiles + custom tile ──────────────────────────────────────

def test_mcp_tab_renders_preset_and_custom_tiles(st_mock):
    page = _page()
    _render_page(page, [])

    labels = _button_labels(st_mock)
    assert "🧪 Vkusvill local" in labels
    assert "+ Custom MCP server" in labels
    captions = [c[1][0] for c in st_mock.calls if c[0] == "caption"]
    assert ("Local fake of the Vkusvill MCP endpoint." in captions)


def test_preset_tile_click_only_opens_form(st_mock):
    page = _page()
    st_mock.click("mcp_preset_tile_vkusvill_local")
    instantiate = MagicMock()
    with patch.object(page, "list_connections", return_value=[]), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(PRESETS)), \
         patch.object(page.connector_presets, "instantiate_preset", instantiate):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    # Nothing is written on a tile click - the form is just opened.
    instantiate.assert_not_called()
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_CREATE
    assert st_mock.session_state.get(page._CREATE_SERVICE_KEY) == "mcp"
    assert st_mock.session_state.get(page._CREATE_PRESET_KEY) == "vkusvill_local"


def test_preset_create_form_prefills_preset(st_mock):
    page = _page()
    _open_create(page, st_mock, "mcp", "vkusvill_local")
    # The mock takes ``value`` as a named parameter, so it never lands in the
    # recorded kwargs; capture it with a thin wrapper instead.
    seen = {}
    original_text_input = st_mock.text_input

    def _capture(label="", value="", *a, **k):
        seen[k.get("key")] = value
        return original_text_input(label, value, *a, **k)

    st_mock.text_input = _capture
    try:
        _render_page(page, [])
    finally:
        st_mock.text_input = original_text_input

    assert seen.get("conn_new_name") == "Vkusvill local"
    # The preset credential field is rendered right in the create form.
    assert "conn_new_mcp_secret_token" in _keys(st_mock, "text_input")
    # The list tabs are not rendered while the form is open.
    assert not _tabs_calls(st_mock)


def test_preset_create_submit_creates_active_connection(st_mock):
    page = _page()
    _open_create(page, st_mock, "mcp", "vkusvill_local")
    st_mock._text_returns["conn_new_mcp_secret_token"] = "tok_123"
    st_mock.click("conn_new_submit")

    instantiate = MagicMock(return_value={"connection": {"id": "new1"},
                                          "preset": "vkusvill_local"})
    with patch.object(page, "list_connections", return_value=[]), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(PRESETS)), \
         patch.object(page.connector_presets, "instantiate_preset", instantiate):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    instantiate.assert_called_once_with(
        "vkusvill_local", "mcp", name="Vkusvill local", account="",
        secrets={"env_pairs": {"VKUSVILL_TOKEN": "tok_123"}}, pending=False,
    )
    # The page returns to the list on the Connections tab.
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_LIST
    assert st_mock.session_state.get(page._TARGET_TAB_KEY) == 2


def test_create_cancel_returns_without_writing(st_mock):
    page = _page()
    _open_create(page, st_mock, "mcp", "vkusvill_local")
    st_mock.click("conn_new_cancel")

    instantiate = MagicMock()
    with patch.object(page, "list_connections", return_value=[]), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(PRESETS)), \
         patch.object(page.connector_presets, "instantiate_preset", instantiate):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    instantiate.assert_not_called()
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_LIST
    # The form target is dropped, so the next create starts clean.
    assert page._CREATE_SERVICE_KEY not in st_mock.session_state
    assert page._CREATE_PRESET_KEY not in st_mock.session_state


def test_custom_tile_click_opens_create_form(st_mock):
    page = _page()
    st_mock.click("mcp_custom_tile")
    _render_page(page, [])
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_CREATE
    assert st_mock.session_state.get(page._CREATE_SERVICE_KEY) == "mcp"
    assert st_mock.session_state.get(page._CREATE_PRESET_KEY) == ""

    st_mock.reset_clicks()
    _render_page(page, [])
    text_keys = _keys(st_mock, "text_input")
    assert "conn_new_name" in text_keys
    assert "conn_new_mcp_command" in text_keys
    assert "conn_new_account" in text_keys
    assert "conn_new_mcp_env_json" in text_keys
    assert "conn_new_mcp_args" in _keys(st_mock, "text_area")


def test_custom_create_submit_creates_stdio_connection(st_mock):
    page = _page()
    _open_create(page, st_mock, "mcp")
    st_mock._text_returns.update({
        "conn_new_name": "My local server",
        "conn_new_mcp_command": "python",
        "conn_new_mcp_args": "server.py",
    })
    st_mock.click("conn_new_submit")

    create = MagicMock()
    with patch.object(page, "list_connections", return_value=[]), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(PRESETS)), \
         patch.object(page, "create_connection", create):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    create.assert_called_once_with(
        "mcp", "My local server", "", account="",
        config={"transport": "stdio", "command": "python",
                "args": ["server.py"]},
        secrets={},
    )


# ── edit form (preset-based) ─────────────────────────────────────────────────

def test_preset_mcp_edit_view_saves_env_pairs(st_mock):
    page = _page()
    st_mock.session_state[page.VIEW_KEY] = page.VIEW_EDIT
    st_mock.session_state[page._EDIT_ID_KEY] = "mcp000001"
    st_mock._text_returns.update({
        "conn_edit_mcp000001_mcp_secret_token": "tok_123",
    })
    st_mock.click("conn_edit_submit_mcp000001")

    update = MagicMock()
    with patch.object(page, "list_connections", return_value=[dict(MCP_CONN)]), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(PRESETS)), \
         patch.object(page.connectors, "get_connection",
                      return_value=dict(MCP_CONN)), \
         patch.object(page, "update_connection", update):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    text_keys = _keys(st_mock, "text_input")
    assert "conn_edit_mcp000001_mcp_secret_token" in text_keys
    # A preset-based edit form never shows the raw transport fields.
    assert "conn_edit_mcp000001_mcp_command" not in text_keys

    update.assert_called_once_with(
        "mcp000001",
        name="Vkusvill",
        account="",
        token="",
        config={"transport": "http",
                "url": "https://mcp.vkusvill.ru/mcp",
                "auth_header": "Bearer {VKUSVILL_TOKEN}",
                "secret_env_fields": PRESET_VKUSVILL["config"]["secret_env_fields"],
                "preset": "vkusvill_local"},
        secrets={"env_pairs": {"VKUSVILL_TOKEN": "tok_123"}},
    )
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_LIST


def test_edit_view_cancel_returns_without_saving(st_mock):
    page = _page()
    st_mock.session_state[page.VIEW_KEY] = page.VIEW_EDIT
    st_mock.session_state[page._EDIT_ID_KEY] = "mcp000001"
    st_mock.click("conn_edit_cancel_mcp000001")

    update = MagicMock()
    with patch.object(page, "list_connections", return_value=[dict(MCP_CONN)]), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(PRESETS)), \
         patch.object(page.connectors, "get_connection",
                      return_value=dict(MCP_CONN)), \
         patch.object(page, "update_connection", update):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    update.assert_not_called()
    assert st_mock.session_state.get(page.VIEW_KEY) == page.VIEW_LIST


# ── connection cards ────────────────────────────────────────────────────────

def test_mcp_connection_card_renders_on_connections_tab(st_mock):
    page = _page()
    _render_page(page, [dict(MCP_CONN)])

    headers = [c[1][0] for c in st_mock.calls
               if c[0] == "expander" and len(c[1]) == 1]
    assert any("Vkusvill" in h and "MCP server (stdio)" in h for h in headers)
    warnings = [c[1][0] for c in st_mock.calls if c[0] == "warning"]
    assert any("credentials required" in w for w in warnings)
    captions = [c[1][0] for c in st_mock.calls if c[0] == "caption"]
    assert "https://mcp.vkusvill.ru/mcp" in captions


def test_general_tab_uses_service_tiles_without_mcp(st_mock):
    page = _page()
    _render_page(page, [])

    tile_keys = {c[2].get("key") for c in st_mock.calls
                 if c[0] == "button" and (c[2].get("key") or "").startswith(
                     "conn_service_tile_")}
    assert "conn_service_tile_github_rest" in tile_keys
    assert "conn_service_tile_ssh" in tile_keys
    assert "conn_service_tile_mcp" not in tile_keys
    labels = _button_labels(st_mock)
    assert any("GitHub (REST API)" in label for label in labels)
    assert any("SSH server" in label for label in labels)
    # No service selectbox remains on the page.
    selectboxes = [c[2] for c in st_mock.calls if c[0] == "selectbox"]
    assert not any(k.get("key") == "conn_new_service" for k in selectboxes)
# ── hidden presets ──────────────────────────────────────────────────────────

PRESET_GITHUB_HIDDEN = {
    "id": "github_mcp",
    "service": "mcp",
    "name": "GitHub MCP (remote)",
    "icon": "🐙",
    "description": "Remote GitHub MCP server.",
    "config": {
        "transport": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "auth_header": "Bearer {GITHUB_TOKEN}",
        "secret_env_fields": {
            "token": {
                "env": "GITHUB_TOKEN",
                "label": "GitHub PAT",
                "help": "Fine-grained PAT.",
                "password": True,
            },
        },
    },
    "sort_order": 20,
    "hidden": True,
}

GITHUB_CONN = dict(
    MCP_CONN,
    id="mcp000002",
    name="GitHub MCP",
    config={"transport": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "preset": "github_mcp"},
)


def test_hidden_preset_offers_no_tile(st_mock):
    page = _page()
    _render_page(page, [])

    preset_tiles = [c[2].get("key") for c in st_mock.calls
                    if c[0] == "button"
                    and (c[2].get("key") or "").startswith("mcp_preset_tile_")]
    assert "mcp_preset_tile_vkusvill_local" in preset_tiles
    assert "mcp_preset_tile_github_mcp" not in preset_tiles


def test_connection_from_hidden_preset_keeps_preset_edit_form(st_mock):
    page = _page()
    st_mock.session_state[page.VIEW_KEY] = page.VIEW_EDIT
    st_mock.session_state[page._EDIT_ID_KEY] = "mcp000002"

    resolve = MagicMock(return_value=dict(PRESET_GITHUB_HIDDEN))
    with patch.object(page, "list_connections", return_value=[dict(GITHUB_CONN)]), \
         patch.object(page.connector_presets, "list_presets",
                      return_value=list(PRESETS)), \
         patch.object(page.connector_presets, "resolve_preset", resolve), \
         patch.object(page.connectors, "get_connection",
                      return_value=dict(GITHUB_CONN)):
        try:
            page.page_connectors()
        except StopRerun:
            pass

    # The catalogue does not carry the preset, so the page asks for it by id.
    resolve.assert_called_once_with("github_mcp", "mcp")
    text_keys = _keys(st_mock, "text_input")
    assert "conn_edit_mcp000002_mcp_secret_token" in text_keys
    assert "conn_edit_mcp000002_mcp_command" not in text_keys
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
