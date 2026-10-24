# -*- coding: utf-8 -*-
"""tests/test_orchestrator_settings_tabs.py - settings tabs must hold their place.

A settings save ends with ``st.rerun()``. The page therefore has to remember
which section the user was editing: without it the rerun reopens the first
tab (Models) and the queued confirmation lands outside the visible area.

Covered here:

- the tabs container carries a stable key and the page reacts to a section
  pinned by a save handler;
- saving the prompt keeps the Prompt tab active and shows the confirmation on
  the next render pass - also on a repeated save;
- a failed save keeps the same tab and reports the error instead of silently
  doing nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun

SLUG = "myorch"
LANG = "English"


@pytest.fixture
def st_env():
    """Fresh Streamlit mock with real i18n and a clean ``ui`` import state."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as mock:
        mock.session_state.update({"ui_lang": LANG})
        yield mock


ORCH = {"slug": SLUG, "name": "MyOrch", "prompt_text": "SHIPPED",
        "config": {}, "is_builtin": True, "max_steps": 100}


def _page_modules(st_env):
    """Import the settings page bound to the active mock."""
    import ui.pages.orchestrator as orch_mod
    import ui.pages.orchestrator_settings as settings_mod
    orch_mod.st = st_env
    settings_mod.st = st_env
    return orch_mod, settings_mod


def _render_page(st_env, modules):
    """Run one render pass, swallowing the rerun unwind."""
    orch_mod, settings_mod = modules
    with patch.object(settings_mod, "get_orchestrator", return_value=dict(ORCH)), \
         patch.object(orch_mod, "get_orchestrator", return_value=dict(ORCH)):
        try:
            settings_mod.page_orchestrator_settings(SLUG)
        except StopRerun:
            pass


def _tabs_calls(st_env):
    return [(args, kwargs) for name, args, kwargs in st_env.calls if name == "tabs"]


def _tab_label(st_env, settings_mod):
    """Return the translated label of the Prompt tab."""
    from core.i18n import t
    return t("orch_tab_prompt", lang=LANG)


def test_tabs_container_has_a_stable_key(st_env):
    """Given the settings page, when rendered, then tabs carry their key."""
    modules = _page_modules(st_env)
    _render_page(st_env, modules)

    calls = _tabs_calls(st_env)
    assert calls, "settings tabs were not rendered"
    _args, kwargs = calls[-1]
    assert kwargs.get("key") == modules[0].settings_tabs_key(SLUG)


def test_pinned_section_activates_its_tab(st_env):
    """Given a pinned section, when rendered, then that tab becomes active."""
    orch_mod, settings_mod = _page_modules(st_env)
    orch_mod.pin_settings_section(SLUG, "prompt")

    _render_page(st_env, (orch_mod, settings_mod))

    expected = _tab_label(st_env, settings_mod)
    assert st_env.session_state[orch_mod.settings_tabs_key(SLUG)] == expected
    # The request is consumed: the NEXT pass keeps whatever the user picks.
    assert orch_mod.settings_section_key(SLUG) not in st_env.session_state


def test_unknown_section_is_ignored(st_env):
    """A pin for a non-existent section must not corrupt the tab state."""
    orch_mod, settings_mod = _page_modules(st_env)
    orch_mod.pin_settings_section(SLUG, "nonsense")

    _render_page(st_env, (orch_mod, settings_mod))

    assert orch_mod.settings_section_key(SLUG) not in st_env.session_state
    assert orch_mod.settings_tabs_key(SLUG) not in st_env.session_state


def test_prompt_save_keeps_the_prompt_tab(st_env):
    """Given the Prompt tab, when Save is clicked, then the tab stays open.

    Two passes are simulated like the real runner does them: the save pass
    queues the confirmation and requests a rerun; the next pass reads the
    pinned section before creating the tabs and paints the message.
    """
    orch_mod, settings_mod = _page_modules(st_env)
    from ui.components.flash import render_flash

    st_env.click(f"orch_save_prompt_{SLUG}")
    st_env._text_returns[f"orch_prompt_{SLUG}"] = "EDITED PROMPT"
    save_mock = MagicMock(return_value=True)

    with patch.object(orch_mod, "save_orchestrator", save_mock):
        _render_page(st_env, (orch_mod, settings_mod))

    save_mock.assert_called_once_with(SLUG, prompt_text="EDITED PROMPT")
    assert st_env.rerun_count == 1
    # Nothing was painted on the save pass itself.
    assert [a[0] for n, a, _k in st_env.calls if n == "success"] == []

    # Next pass: the page reopens the Prompt tab and paints the confirmation.
    st_env.reset_clicks()
    _render_page(st_env, (orch_mod, settings_mod))
    render_flash()

    expected = _tab_label(st_env, settings_mod)
    assert st_env.session_state[orch_mod.settings_tabs_key(SLUG)] == expected
    confirmations = [a[0] for n, a, _k in st_env.calls if n == "success"]
    from core.i18n import t
    assert confirmations == [t("orch_save_prompt_ok", lang=LANG)]


def test_repeated_prompt_save_reports_every_time(st_env):
    """Given two saves in a row, then each one is confirmed and stays put."""
    orch_mod, settings_mod = _page_modules(st_env)
    from core.i18n import t
    from ui.components.flash import render_flash

    confirmations = []
    for attempt in range(2):
        baseline = len(st_env.calls)
        st_env.click(f"orch_save_prompt_{SLUG}")
        st_env._text_returns[f"orch_prompt_{SLUG}"] = f"EDIT {attempt}"
        with patch.object(orch_mod, "save_orchestrator", MagicMock(return_value=True)):
            _render_page(st_env, (orch_mod, settings_mod))
        st_env.reset_clicks()
        _render_page(st_env, (orch_mod, settings_mod))
        render_flash()

        # Only the calls of THIS attempt count: the previous confirmation was
        # already painted and consumed.
        confirmations = [a[0] for n, a, _k in st_env.calls[baseline:] if n == "success"]
        assert confirmations == [t("orch_save_prompt_ok", lang=LANG)]
        assert st_env.session_state[orch_mod.settings_tabs_key(SLUG)] == \
            t("orch_tab_prompt", lang=LANG)

    assert confirmations, "no confirmation was painted at all"


def test_failed_prompt_save_reports_the_error_in_place(st_env):
    """Given a failing save, then the error is shown and the tab is kept."""
    orch_mod, settings_mod = _page_modules(st_env)
    from core.i18n import t
    from ui.components.flash import render_flash

    st_env.click(f"orch_save_prompt_{SLUG}")
    st_env._text_returns[f"orch_prompt_{SLUG}"] = "EDITED"

    with patch.object(orch_mod, "save_orchestrator", MagicMock(return_value=False)):
        _render_page(st_env, (orch_mod, settings_mod))

    st_env.reset_clicks()
    _render_page(st_env, (orch_mod, settings_mod))
    render_flash()

    errors = [a[0] for n, a, _k in st_env.calls if n == "error"]
    assert errors == [t("orch_save_prompt_error", lang=LANG)]
    assert st_env.session_state[orch_mod.settings_tabs_key(SLUG)] == \
        t("orch_tab_prompt", lang=LANG)


def test_stale_tab_label_after_language_switch_is_dropped(st_env):
    """Given a selection from another language, then it is discarded."""
    orch_mod, settings_mod = _page_modules(st_env)
    st_env.session_state[orch_mod.settings_tabs_key(SLUG)] = "Промпт"

    _render_page(st_env, (orch_mod, settings_mod))

    assert st_env.session_state.get(orch_mod.settings_tabs_key(SLUG)) is None


def test_current_language_selection_is_kept(st_env):
    """Given a valid selection, then it survives an ordinary render."""
    orch_mod, settings_mod = _page_modules(st_env)
    label = _tab_label(st_env, settings_mod)
    st_env.session_state[orch_mod.settings_tabs_key(SLUG)] = label

    _render_page(st_env, (orch_mod, settings_mod))

    assert st_env.session_state[orch_mod.settings_tabs_key(SLUG)] == label


def test_every_section_save_pins_its_own_tab(st_env):
    """Each settings section maps to a distinct tab and pin helper call."""
    orch_mod, settings_mod = _page_modules(st_env)
    assert len(orch_mod.SETTINGS_SECTIONS) == 10
    assert len(set(orch_mod.SETTINGS_SECTIONS)) == 10

    for section in orch_mod.SETTINGS_SECTIONS:
        st_env.session_state.pop(orch_mod.settings_section_key(SLUG), None)
        orch_mod.pin_settings_section(SLUG, section)
        _render_page(st_env, (orch_mod, settings_mod))
        expected_index = orch_mod.SETTINGS_SECTIONS.index(section)
        labels = _tabs_calls(st_env)[-1][0][0]
        assert st_env.session_state[orch_mod.settings_tabs_key(SLUG)] == \
            labels[expected_index]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
