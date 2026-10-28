"""
The "Extras" block of a catalog card: what installs together with it.

A card may declare companion entities (a knowledge base for an agent, a
connector for a search skill). The block lists them as checkboxes whose
defaults come from the manifest, spells their types in the reader's
language, and reports connector credentials the user still has to fill in.
The block is the same for an employee and for a helper: the mechanism
belongs to the card, not to the kind of agent.
"""
import importlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PKG_ROOT = HERE.parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402


#: Card of an agent whose package mixes three entity types.
AGENT_CARD = {
    "ok": True,
    "id": "ya_agent",
    "type": "agent",
    "agent_kind": "orchestrator",
    "name": "YaAgent",
    "version": "2.11.1",
    "state": "installed",
    "installed_version": "2.11.1",
    "license_spdx": "PolyForm-Noncommercial-1.0.0",
    "file_count": 3,
    "size_bytes": 4096,
    "entity": {
        "id": "ya_agent",
        "type": "agent",
        "version": "2.11.1",
        "recommends": [
            {"type": "rag", "id": "yaagentai_2020"},
            {"type": "mcp", "id": "vkusvill"},
            {"type": "skill", "id": "rag_base_creator"},
        ],
    },
}

#: Plan of that package: one pre-ticked base, one unticked connector.
BUNDLE_PLAN = {
    "ok": True,
    "items": [
        {
            "type": "rag",
            "id": "yaagentai_2020",
            "name": "YaAgentAI",
            "version": "1.0.0",
            "state": "install",
            "default_selected": True,
        },
        {
            "type": "mcp",
            "id": "vkusvill",
            "name": "VkusVill",
            "version": "1.0.0",
            "state": "install",
            "default_selected": False,
        },
        {
            "type": "skill",
            "id": "rag_base_creator",
            "name": "RAG Base Creator",
            "version": "1.0.0",
            "state": "installed",
            "default_selected": True,
        },
    ],
    "total": 3,
    "installable": 2,
    "updates": 0,
    "selected": 1,
    "needs_license": False,
    "size_bytes": 2048,
}


def _patched(monkeypatch, card=None, bundle_plan=None):
    """Import the page fresh and patch what it bound at import time."""
    for name in list(sys.modules):
        if name.startswith(("core.marketplace", "ui.pages.marketplace", "ui.components")):
            sys.modules.pop(name, None)
    page = importlib.import_module("ui.pages.marketplace")
    monkeypatch.setattr(page.marketplace_config, "is_enabled", lambda: True)
    monkeypatch.setattr(page.marketplace_config, "marketplace_settings", lambda: {
        "enabled": True,
        "allow_code": True,
        "channel": "main",
    })
    monkeypatch.setattr(
        page.marketplace_catalog,
        "list_entities",
        lambda *a, **kw: {
            "ok": True,
            "stale": False,
            "installed": 1,
            "items": [{
                "id": "ya_agent",
                "type": "agent",
                "agent_kind": "orchestrator",
                "name": "YaAgent",
                "description": "An agent with a package",
                "version": "2.11.1",
                "installed": True,
                "installed_version": "2.11.1",
                "user_modified": False,
            }],
        },
    )
    monkeypatch.setattr(
        page.marketplace_catalog, "fetch_card", card or (lambda *a, **kw: dict(AGENT_CARD))
    )
    monkeypatch.setattr(
        page.marketplace_bundles,
        "plan",
        bundle_plan or (lambda *a, **kw: dict(BUNDLE_PLAN)),
    )
    monkeypatch.setattr(
        page.marketplace_installer,
        "install_entity",
        lambda *a, **kw: {"ok": True, "noop": False, "version": "2.11.1"},
    )
    return page


def _render(page, **state) -> None:
    """Render the page with the details of the only card already open."""
    import streamlit as st

    st.session_state.clear()
    st.session_state.update({"ui_lang": "Русский", **state})
    page.page_marketplace()


def _checkboxes(mock) -> list:
    """Return ``(label, kwargs)`` of every rendered checkbox."""
    return [(a[0] if a else "", k) for name, a, k in mock.calls if name == "checkbox"]


def test_the_block_is_titled_extras(monkeypatch):
    """The block is called "Дополнительно", not "Рекомендуемый пакет"."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        st_mock.click("mp_details_ya_agent")
        _render(page)

        headers = [a[0] for name, a, _k in st_mock.calls if name == "markdown" and a]
        assert any("Дополнительно" in str(header) for header in headers)


def test_every_member_gets_a_checkbox(monkeypatch):
    """All three members appear, including the already installed one."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        st_mock.click("mp_details_ya_agent")
        _render(page)

        labels = [label for label, _k in _checkboxes(st_mock) if "YaAgentAI" in label or "VkusVill" in label or "RAG Base" in label]
        assert len(labels) == 3


def test_types_are_spelled_out_in_the_page_language(monkeypatch):
    """A member is labelled "база знаний", never as the machine tag ``[rag]``."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        st_mock.click("mp_details_ya_agent")
        _render(page)

        labels = [label for label, _k in _checkboxes(st_mock)]
        assert any("база знаний" in label for label in labels)
        assert any("коннектор" in label for label in labels)
        assert not any("[rag]" in label or "[mcp]" in label for label in labels)


def test_the_manifest_decides_which_members_are_ticked(monkeypatch):
    """Defaults follow ``default_selected``: the base is on, the connector off."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        st_mock.click("mp_details_ya_agent")
        _render(page)

        by_name = {}
        for label, kwargs in _checkboxes(st_mock):
            if "YaAgentAI" in label:
                by_name["rag"] = kwargs.get("value")
            elif "VkusVill" in label:
                by_name["mcp"] = kwargs.get("value")
        assert by_name == {"rag": True, "mcp": False}


def test_the_block_works_for_a_helper_card(monkeypatch):
    """A helper card carries its own package through the same block."""
    helper_card = dict(AGENT_CARD)
    helper_card.update({
        "id": "meeting_minutes",
        "agent_kind": "assistant",
        "name": "Meeting Minutes",
        "entity": dict(AGENT_CARD["entity"], id="meeting_minutes"),
    })
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch, card=lambda *a, **kw: dict(helper_card))
        st_mock.click("mp_details_ya_agent")
        _render(page)

        labels = [label for label, _k in _checkboxes(st_mock)]
        assert any("YaAgentAI" in label for label in labels)


def test_one_press_installs_the_entity_and_its_extras(monkeypatch):
    """A single press covers both the entity and the ticked package members.

    This is the behaviour that made a second button unnecessary: the extras
    belong to the entity, so one action installs them together and reports
    both results in one summary.
    """
    order = []

    def _entity_install(entity, **kwargs):
        order.append("entity")
        return {"ok": True, "noop": False, "version": "2.11.1"}

    def _bundle_install(entity, **kwargs):
        order.append("bundle")
        assert kwargs["selected"] == [{"type": "rag", "id": "yaagentai_2020"}]
        return {"ok": True, "installed": [{"type": "rag", "id": "yaagentai_2020"}]}

    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        monkeypatch.setattr(page.marketplace_installer, "install_entity", _entity_install)
        monkeypatch.setattr(page.marketplace_bundles, "install", _bundle_install)
        st_mock.click("mp_details_ya_agent", "mp_install_ya_agent")
        with pytest.raises(StopRerun):
            _render(page)

        queued = [item.get("text") for item in st_mock.session_state.get("_flash_messages") or []]

    assert order == ["entity", "bundle"], "the extras must follow the entity"
    assert any("YaAgent" in str(text) for text in queued)
    assert any("yaagentai_2020" in str(text) for text in queued)


def test_connector_credentials_are_reported_after_the_install(monkeypatch):
    """A connector member installs without credentials; the user is told.

    The connector is created with status ``pending``: without the hint the
    user would have a tile that silently answers nothing, so the install
    confirmation has to name the fields to fill in.
    """
    def _install(entity, **kwargs):
        return {
            "ok": True,
            "installed": [
                {"type": "rag", "id": "yaagentai_2020"},
                {
                    "type": "mcp",
                    "id": "vkusvill",
                    "requires_secrets": ["api_key"],
                    "credentials_status": "pending",
                },
            ],
        }

    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        monkeypatch.setattr(page.marketplace_bundles, "install", _install)
        st_mock.click("mp_details_ya_agent", "mp_install_ya_agent")
        with pytest.raises(StopRerun):
            _render(page)

        queued = list(st_mock.session_state.get("_flash_messages") or [])

    hints = [
        item for item in queued
        if item.get("kind") == "info" and "api_key" in str(item.get("text"))
    ]
    assert hints, f"no credentials hint was queued: {queued}"


def test_an_active_connector_needs_no_hint(monkeypatch):
    """A connector that came with working credentials stays quiet."""
    def _install(entity, **kwargs):
        return {
            "ok": True,
            "installed": [
                {
                    "type": "mcp",
                    "id": "vkusvill",
                    "requires_secrets": ["api_key"],
                    "credentials_status": "active",
                },
            ],
        }

    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        monkeypatch.setattr(page.marketplace_bundles, "install", _install)
        st_mock.click("mp_details_ya_agent", "mp_install_ya_agent")
        with pytest.raises(StopRerun):
            _render(page)

        queued = list(st_mock.session_state.get("_flash_messages") or [])

    assert not [item for item in queued if item.get("kind") == "info"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
