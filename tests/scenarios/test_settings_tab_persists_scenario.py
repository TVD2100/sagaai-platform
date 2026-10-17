# -*- coding: utf-8 -*-
"""
Scenario: editing an employee's settings keeps the user where they were.

Given an employee settings page opened on the Prompt section,
when the user edits the prompt and presses Save,
then the page comes back on the Prompt section (not on the first tab) and
shows the save confirmation right there; a second save behaves the same way,
and a failed save reports the error instead of silently doing nothing.

This walks the page through its public entry point
(``page_orchestrator_settings``) the way the runner does it: a save pass ends
with ``st.rerun()``, the next pass re-renders the page from the session state.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun

SLUG = "myorch"
LANG = "English"
ORCH = {"slug": SLUG, "name": "MyOrch", "prompt_text": "SHIPPED PROMPT",
        "config": {}, "is_builtin": True, "max_steps": 100}


@pytest.fixture
def app(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh Streamlit mock, isolated app modules and data dir.

    The page pulls core/storage modules that cache the database engine and
    the data directory at import time; a previous test file in the same run
    would otherwise leave them pointing at a removed temporary directory.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as mock:
        mock.session_state.update({"ui_lang": LANG})
        import ui.pages.orchestrator as orch_mod
        import ui.pages.orchestrator_settings as settings_mod
        orch_mod.st = mock
        settings_mod.st = mock
        yield mock, orch_mod, settings_mod


def _render(mock, settings_mod, orch_mod):
    """One page render pass, unwinding a requested rerun like the runner."""
    with patch.object(settings_mod, "get_orchestrator", return_value=dict(ORCH)), \
         patch.object(orch_mod, "get_orchestrator", return_value=dict(ORCH)):
        try:
            settings_mod.page_orchestrator_settings(SLUG)
        except StopRerun:
            pass


def _labels(mock):
    """Labels of the most recently rendered tabs container."""
    for name, args, _kwargs in reversed(mock.calls):
        if name == "tabs":
            return list(args[0])
    raise AssertionError("settings tabs were never rendered")


def _active_tab_label(mock, orch_mod):
    """Label the tabs container would activate on the next pass."""
    return mock.session_state.get(orch_mod.settings_tabs_key(SLUG))


def _painted_since(mock, baseline, kind):
    return [a[0] for n, a, _k in mock.calls[baseline:] if n == kind]


def test_prompt_save_keeps_the_user_on_the_prompt_section(app):
    """Given the Prompt section, when saved twice, then both saves are visible."""
    mock, orch_mod, settings_mod = app
    from core.i18n import t
    from ui.components.flash import render_flash

    prompt_label = t("orch_tab_prompt", lang=LANG)
    save_label = f"orch_save_prompt_{SLUG}"

    # The user opens the settings page and switches to the Prompt section by
    # choosing that tab; the page stores the choice under its tab key.
    _render(mock, settings_mod, orch_mod)
    mock.session_state[orch_mod.settings_tabs_key(SLUG)] = prompt_label

    for attempt in range(1, 3):
        baseline = len(mock.calls)
        mock.click(save_label)
        mock._text_returns[f"orch_prompt_{SLUG}"] = f"EDITED PROMPT {attempt}"

        with patch.object(orch_mod, "save_orchestrator", MagicMock(return_value=True)) as save:
            _render(mock, settings_mod, orch_mod)
        save.assert_called_once_with(SLUG, prompt_text=f"EDITED PROMPT {attempt}")

        # The save pass itself paints nothing: the page is about to rerun.
        assert _painted_since(mock, baseline, "success") == []

        baseline = len(mock.calls)
        mock.reset_clicks()
        _render(mock, settings_mod, orch_mod)
        render_flash()

        # Still on the Prompt section, and the confirmation is visible there.
        assert _active_tab_label(mock, orch_mod) == prompt_label
        assert _labels(mock)[_labels(mock).index(prompt_label)] == prompt_label
        assert _painted_since(mock, baseline, "success") == [
            t("orch_save_prompt_ok", lang=LANG)
        ]


def test_failed_save_reports_the_error_and_keeps_the_section(app):
    """Given a save that cannot be stored, then the user is told about it."""
    mock, orch_mod, settings_mod = app
    from core.i18n import t
    from ui.components.flash import render_flash

    prompt_label = t("orch_tab_prompt", lang=LANG)
    _render(mock, settings_mod, orch_mod)
    mock.session_state[orch_mod.settings_tabs_key(SLUG)] = prompt_label

    mock.click(f"orch_save_prompt_{SLUG}")
    mock._text_returns[f"orch_prompt_{SLUG}"] = "EDITED"
    with patch.object(orch_mod, "save_orchestrator", MagicMock(return_value=False)):
        _render(mock, settings_mod, orch_mod)

    baseline = len(mock.calls)
    mock.reset_clicks()
    _render(mock, settings_mod, orch_mod)
    render_flash()

    assert _painted_since(mock, baseline, "error") == [
        t("orch_save_prompt_error", lang=LANG)
    ]
    assert _active_tab_label(mock, orch_mod) == prompt_label
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
