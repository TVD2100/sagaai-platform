"""
Sidebar navigation layout: the four blocks and their fixed order.

The sidebar is the only place where every part of the platform is
reachable, so its shape is a contract: employees, helpers, the marketplace
with one entry per catalog subsection, and the settings block whose first
three entries stay visible while the rest sit in a collapsed "More" block.
A regression here silently hides a feature instead of raising.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PKG_ROOT = HERE.parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402


#: Sidebar button keys of the marketplace subsections, in display order.
_MARKETPLACE_KEYS = [
    "nav_marketplace_agents",
    "nav_marketplace_helpers",
    "nav_marketplace_skills",
    "nav_marketplace_rag",
    "nav_marketplace_mcp",
]

#: Keys of the three settings entries that are always visible.
_PRIMARY_KEYS = ["nav_settings", "nav_connectors", "nav_updates"]

#: Keys inside the collapsed settings block, in display order.
_MORE_KEYS = [
    "nav_orchestrators",
    "nav_skills",
    "nav_skills_library",
    "nav_storage",
    "nav_access",
    "nav_stats",
    "nav_welcome",
]


def _settings(monkeypatch, enabled: bool = True) -> None:
    """Patch the marketplace settings for one render."""
    from core.marketplace import config as marketplace_config

    values = {
        "enabled": enabled,
        "base_url": "",
        "allow_code": True,
        "require_signature": False,
        "channel": "main",
    }
    monkeypatch.setattr(marketplace_config, "marketplace_settings", lambda: dict(values))


def _state() -> dict:
    """Return the session state a rendered main() expects."""
    return dict(
        show_skill_form=False,
        edit_skill_id=None,
        user_input_value="",
        force_send=False,
        active_thread_id=None,
        confirm_delete_all=False,
        attached_file_context="",
        attached_file_name="",
        input_key=0,
        current_page="run",
        ui_lang="English",
    )


def _render(monkeypatch, enabled: bool = True):
    """Render main() and return the Streamlit mock."""
    os.environ.setdefault("SAGAAI_DATA_DIR", tempfile.mkdtemp())
    with install_streamlit_mock() as mock:
        for name in list(sys.modules):
            if name.startswith(("core", "storage", "ui")):
                sys.modules.pop(name, None)
        ui_app = importlib.import_module("ui.app")
        _settings(monkeypatch, enabled=enabled)
        mock.session_state.update(_state())
        try:
            ui_app.main()
        except StopRerun:
            pass
    return mock


def _nav_keys(mock) -> list:
    """Return the ``key=`` of every rendered sidebar button, in order."""
    return [
        call[2].get("key") for call in mock.calls
        if call[0] == "button" and str(call[2].get("key") or "").startswith("nav_")
    ]


def _expander_labels(mock) -> list:
    """Return the labels of every rendered expander."""
    labels = []
    for call in mock.calls:
        if call[0] != "expander":
            continue
        args = call[1]
        labels.append(str(args[0]) if args else "")
    return labels


def test_the_marketplace_block_lists_all_five_subsections(isolated_app_modules, monkeypatch):
    keys = _nav_keys(_render(monkeypatch))

    assert [key for key in keys if key in _MARKETPLACE_KEYS] == _MARKETPLACE_KEYS


def test_the_marketplace_block_is_hidden_when_the_switch_is_off(isolated_app_modules, monkeypatch):
    keys = _nav_keys(_render(monkeypatch, enabled=False))

    assert not [key for key in keys if key in _MARKETPLACE_KEYS]
    assert "nav_marketplace" not in keys


def test_the_three_primary_settings_entries_are_always_visible(isolated_app_modules, monkeypatch):
    keys = _nav_keys(_render(monkeypatch))

    assert [key for key in keys if key in _PRIMARY_KEYS] == _PRIMARY_KEYS


def test_the_settings_block_hides_the_rest_behind_more(isolated_app_modules, monkeypatch):
    """The collapsed entries are rendered inside an expander, not loose."""
    mock = _render(monkeypatch)
    keys = _nav_keys(mock)

    assert [key for key in keys if key in _MORE_KEYS] == _MORE_KEYS
    assert any("More" in str(label) for label in _expander_labels(mock))


def test_the_marketplace_block_sits_before_the_settings_block(isolated_app_modules, monkeypatch):
    keys = _nav_keys(_render(monkeypatch))

    last_marketplace = max(keys.index(key) for key in _MARKETPLACE_KEYS)
    first_primary = min(keys.index(key) for key in _PRIMARY_KEYS)
    assert last_marketplace < first_primary


def test_mcp_is_a_marketplace_subsection_and_not_a_settings_entry(isolated_app_modules, monkeypatch):
    """MCP servers live inside the marketplace catalog, never as a menu entry."""
    keys = _nav_keys(_render(monkeypatch))

    assert "nav_marketplace_mcp" in keys
    assert not [key for key in keys if "mcp" in key and key != "nav_marketplace_mcp"]


def test_a_marketplace_subsection_click_records_the_section(isolated_app_modules, monkeypatch):
    """The subsection lands on the showcase with its own filter selected."""
    os.environ.setdefault("SAGAAI_DATA_DIR", tempfile.mkdtemp())
    with install_streamlit_mock() as mock:
        for name in list(sys.modules):
            if name.startswith(("core", "storage", "ui")):
                sys.modules.pop(name, None)
        ui_app = importlib.import_module("ui.app")
        _settings(monkeypatch)
        mock.session_state.update(_state())
        mock.click("nav_marketplace_rag")
        try:
            ui_app.main()
        except StopRerun:
            pass

    assert mock.session_state["current_page"] == "marketplace"
    assert mock.session_state["mp_section"] == "rag"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
