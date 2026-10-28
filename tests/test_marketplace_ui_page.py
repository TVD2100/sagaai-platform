"""
The marketplace page has to survive a real Streamlit script run.

Streamlit binds a widget to its ``key`` at creation time and rejects any
later assignment to that key, raising ``StreamlitWidgetAlreadyInstantiatedError``.
A plain dict mock accepts the write silently, so that bug reached the user
instead of a test. The guard test below renders the page with the mock's
widget-key locking switched ON: any write into an already-created widget
key now fails here first.

The page is also checked for the property that caused the original crash:
the search field must own its key and no code may copy its value back into
``session_state`` under the same name.
"""
import importlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PKG_ROOT = HERE.parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from tests._st_mock import WidgetLockedError, install_streamlit_mock  # noqa: E402


CATALOG_ANSWER = {
    "ok": True,
    "stale": False,
    "installed": 0,
    "items": [
        {
            "id": "sagai/demo/skill",
            "type": "skill",
            "name": "Demo skill",
            "description": "A skill used by the page tests",
            "version": "1.0.0",
            "installed": False,
            "installed_version": "",
            "user_modified": False,
        },
        {
            "id": "demo_agent",
            "type": "agent",
            "agent_kind": "orchestrator",
            "name": "Demo agent",
            "description": "An agent with a recommended package",
            "version": "2.0.0",
            "installed": True,
            "installed_version": "2.0.0",
            "user_modified": False,
        },
        {
            "id": "demo_helper",
            "type": "agent",
            "agent_kind": "assistant",
            "name": "Demo helper",
            "description": "A one-shot helper profile",
            "version": "1.1.0",
            "installed": False,
            "installed_version": "",
            "user_modified": False,
        },
    ],
}

#: Card of the agent above, including its recommended package.
AGENT_CARD = {
    "ok": True,
    "id": "demo_agent",
    "type": "agent",
    "name": "Demo agent",
    "version": "2.0.0",
    "state": "installed",
    "installed_version": "2.0.0",
    "license_spdx": "PolyForm-Noncommercial-1.0.0",
    "file_count": 2,
    "size_bytes": 2048,
    "entity": {
        "id": "demo_agent",
        "type": "agent",
        "version": "2.0.0",
        "bundle": [{"type": "skill", "id": "demo_skill"}],
    },
}

#: Plan of the agent's recommended package.
BUNDLE_PLAN = {
    "ok": True,
    "items": [
        {
            "type": "skill",
            "id": "demo_skill",
            "name": "Demo skill",
            "version": "1.0.0",
            "state": "install",
            "default_selected": True,
        },
    ],
    "total": 1,
    "installable": 1,
    "updates": 0,
    "selected": 1,
    "needs_license": False,
    "size_bytes": 1024,
}


# ─── Helpers ──────────────────────────────────────────────────────────────

def _fresh_page():
    """Import ``ui.pages.marketplace`` fresh under the active st mock."""
    for name in list(sys.modules):
        if name.startswith(("core.marketplace", "ui.pages.marketplace", "ui.components")):
            sys.modules.pop(name, None)
    return importlib.import_module("ui.pages.marketplace")


def _patched(monkeypatch, enabled=True, catalog=None, card=None, bundle_plan=None):
    """Import the page fresh, THEN patch what it imported.

    Patching before the import would be undone by the re-import: the page
    binds its own module objects at import time, so the patch has to land on
    the objects the page actually calls.
    """
    page = _fresh_page()
    monkeypatch.setattr(page.marketplace_config, "is_enabled", lambda: bool(enabled))
    monkeypatch.setattr(
        page.marketplace_catalog,
        "list_entities",
        catalog or (lambda *a, **kw: dict(CATALOG_ANSWER)),
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
        lambda *a, **kw: {"ok": True, "noop": False, "version": "1.0.0"},
    )
    return page


def _render(page, **state) -> None:
    """Render the page into the mock's session state.

    ``ui_lang`` carries the language display name, exactly what the app
    itself puts there; ``t()`` resolves a display name to its file, so a
    bare code would silently fall back to English.
    """
    import streamlit as st

    st.session_state.clear()
    st.session_state.update({"ui_lang": "Русский", **state})
    page.page_marketplace()


# ─── Tests ────────────────────────────────────────────────────────────────

def test_page_renders_with_widget_key_locking(monkeypatch):
    """The page renders without writing into an instantiated widget key.

    This is the regression guard for the reported crash: with locking on,
    the old code raised ``WidgetLockedError`` on its first render.
    """
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page)

        assert not any("cannot be modified" in str(item) for item in st_mock.errors)
        assert st_mock.calls, "the page rendered nothing at all"
        assert any(name == "text_input" for name, _a, _k in st_mock.calls)


def test_search_key_is_never_written_back(monkeypatch):
    """The search field owns ``mp_query``; nothing reassigns it.

    The property is checked by the runtime guard above as well, but this
    test states the rule directly so a future refactor that reintroduces the
    write fails with a readable name instead of a widget-lifecycle error.
    """
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page, mp_query="demo")

        assert st_mock.session_state["mp_query"] == "demo"
        assert any(name == "text_input" for name, _a, _k in st_mock.calls)


def test_query_filters_the_list(monkeypatch):
    """The search text narrows the rendered cards."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page, mp_query="nothing-matches-this")

        expanders = [a for name, a, _k in st_mock.calls if name == "expander"]
        assert expanders == [], "filtered-out card still rendered"


def test_disabled_switch_stops_the_render(monkeypatch):
    """A disabled marketplace paints the notice and nothing else."""
    def _boom(**kw):
        raise AssertionError("the catalog must not be contacted when disabled")

    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch, enabled=False, catalog=_boom)
        _render(page)

        assert not any(name == "text_input" for name, _a, _k in st_mock.calls)


def test_guard_raises_on_a_locked_key():
    """The mock itself refuses a write into an instantiated widget key.

    Without this assertion the guard tests above could pass against a mock
    that silently accepts everything - exactly the hole that let the crash
    reach the user.
    """
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        st_mock.text_input("Search", key="mp_query")
        with pytest.raises(WidgetLockedError):
            st_mock.session_state["mp_query"] = "written too late"


def test_section_tabs_render_and_filter_the_list(monkeypatch):
    """A section tab narrows the list to its own catalog subsection.

    Streamlit runs every tab body on each render, so the page keeps the
    tabs as a switcher and paints the list below them. This test states
    that contract: with the employees section active, neither the skill
    card nor the helper card may be rendered.
    """
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page, mp_tabs="Сотрудники")

        tab_calls = [a for name, a, _k in st_mock.calls if name == "tabs"]
        assert tab_calls, "the section tabs did not render"
        assert tab_calls[0][0] == [
            "Сотрудники", "Помощники", "Навыки", "Базы знаний", "Коннекторы",
        ]

        expanders = [a[0] for name, a, _k in st_mock.calls if name == "expander"]
        assert len(expanders) == 1
        assert "Demo agent" in expanders[0]


def test_the_default_section_lists_employees_only(monkeypatch):
    """Without a stored tab the page opens on the employees subsection."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page)

        expanders = [a[0] for name, a, _k in st_mock.calls if name == "expander"]
        assert len(expanders) == 1
        assert "Demo agent" in expanders[0]


def test_the_helpers_section_shows_helpers_only(monkeypatch):
    """Employees and helpers share the agent type but never one list.

    They install into different places (an employee folder with
    instructions versus a one-shot profile), so a mixed list would offer
    two different products under one heading.
    """
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page, mp_tabs="Помощники")

        expanders = [a[0] for name, a, _k in st_mock.calls if name == "expander"]
        assert len(expanders) == 1
        assert "Demo helper" in expanders[0]


def test_the_skills_section_hides_agents(monkeypatch):
    """The skill subsection lists skills and nothing else."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page, mp_tabs="Навыки")

        expanders = [a[0] for name, a, _k in st_mock.calls if name == "expander"]
        assert len(expanders) == 1
        assert "Demo skill" in expanders[0]


def test_a_sidebar_request_opens_the_matching_tab(monkeypatch):
    """A sidebar click hands its subsection to the tabs of the page.

    The sidebar cannot write the tab widget state after creation, so it
    records a request; the page consumes it before building the tabs.
    """
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        _render(page, mp_section_target="helpers")

        tab_calls = [(a, k) for name, a, k in st_mock.calls if name == "tabs"]
        assert tab_calls, "the section tabs did not render"
        assert tab_calls[0][1].get("default") == "Помощники"
        assert "mp_section_target" not in st_mock.session_state
        assert st_mock.session_state["mp_section"] == "helpers"


def test_agent_card_lists_its_recommended_package(monkeypatch):
    """The card of an agent paints one checkbox per package member.

    The member states come from the plan; the page only renders them. A
    disabled member (already installed, unsupported type) still appears so
    the package intent stays visible.
    """
    plan = dict(BUNDLE_PLAN)
    plan["items"] = list(BUNDLE_PLAN["items"]) + [
        {
            "type": "rag",
            "id": "demo_rag",
            "name": "Demo base",
            "version": "1.0.0",
            "state": "unsupported",
            "default_selected": True,
        },
    ]
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch, bundle_plan=lambda *a, **kw: dict(plan))
        st_mock.click("mp_details_demo_agent")
        _render(page)

        boxes = [(a[0], k) for name, a, k in st_mock.calls if name == "checkbox"]
        labels = [label for label, _k in boxes]
        assert any("Demo skill" in label and "будет установлено" in label for label in labels)
        assert any("Demo base" in label and "пока нельзя установить" in label for label in labels)
        disabled = [k for _label, k in boxes if k.get("disabled")]
        assert any("demo_rag" in str(key) for key in disabled)
        assert any(name == "markdown" for name, _a, _k in st_mock.calls)


def test_bundle_install_sends_the_ticked_members(monkeypatch):
    """Pressing the package button installs exactly the ticked members."""
    from tests._st_mock import StopRerun

    calls = {}

    def _install(entity, **kwargs):
        calls["entity"] = entity
        calls["kwargs"] = kwargs
        return {"ok": True, "installed": [{"id": "demo_skill"}]}

    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        monkeypatch.setattr(page.marketplace_bundles, "install", _install)
        monkeypatch.setattr(page.marketplace_config, "marketplace_settings", lambda: {
            "enabled": True,
            "allow_code": True,
            "channel": "main",
        })
        st_mock.click("mp_details_demo_agent", "mp_install_demo_agent")
        with pytest.raises(StopRerun):
            _render(page)

        assert calls["kwargs"]["selected"] == [{"type": "skill", "id": "demo_skill"}]
        assert calls["entity"]["id"] == "demo_agent"
        assert st_mock.rerun_count == 1


def test_install_works_on_the_rerun_after_the_card_was_opened(monkeypatch):
    """Pressing Install in a later pass still reaches the installer.

    This is the regression guard for the reported dead button: Streamlit
    reruns the whole page on every click, and on that pass no details button
    is pressed. A card that derived its expanded state from the click alone
    folded itself away and swallowed the install, so the user saw nothing at
    all. The state now lives in ``session_state`` and survives the rerun.
    """
    from tests._st_mock import StopRerun

    installed = []

    def _install(entity, **kwargs):
        installed.append(entity["id"])
        return {"ok": True, "noop": False, "version": "1.0.0"}

    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        monkeypatch.setattr(page.marketplace_installer, "install_entity", _install)

        import streamlit as st

        st.session_state.clear()
        st.session_state.update({"ui_lang": "Русский"})
        # First pass: the user only opens the card.
        st_mock.click("mp_details_demo_agent")
        page.page_marketplace()
        carried = dict(st.session_state)
        assert carried.get("mp_open_demo_agent") is True
        assert installed == []

        # Second pass: a rerun keeps the session state, but the details
        # button is not pressed any more - only Install is.
        st_mock.reset_clicks()
        st.session_state.clear()
        st.session_state.update(carried)
        st_mock.click("mp_install_demo_agent")
        with pytest.raises(StopRerun):
            page.page_marketplace()

    assert installed == ["demo_agent"]


def test_the_card_shows_a_single_install_button(monkeypatch):
    """A card offers one Install button, never a second for its package."""
    with install_streamlit_mock() as st_mock:
        st_mock.lock_widgets = True
        page = _patched(monkeypatch)
        st_mock.click("mp_details_demo_agent")
        _render(page)

        keys = [str(k.get("key") or "") for name, _a, k in st_mock.calls if name == "button"]
        install_keys = [key for key in keys if key.startswith("mp_install_")]
        assert install_keys == ["mp_install_demo_agent"]
        assert not [key for key in keys if key.startswith("mp_bundle_go_")]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
