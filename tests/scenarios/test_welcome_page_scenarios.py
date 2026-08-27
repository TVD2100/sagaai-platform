# -*- coding: utf-8 -*-
"""tests/scenarios/test_welcome_page_scenarios.py - welcome-page scenarios.

Regressions for the welcome -> DevAgent-settings navigation fix. Each
scenario is written in given -> when -> then form and walks the UI through
public entry points (ui.app.main and the real orchestrator settings
render), so prompts/injections never bypass the application flow.

  Scenario 1 - every welcome step button navigates: the four onboarding
               cards route to providers settings, DevAgent settings,
               skills and employees respectively. The DevAgent button opens
               the dedicated settings page (8 native tabs), NOT the
               half-empty chat page that shows the missing-API-key warning.
  Scenario 2 - full DevAgent settings render: with the built-in DevAgent
               profile seeded, the orchestrator_settings page renders all
               eight tabs and the back-to-chat button without the
               orchestrator no-API-key warning and without errors.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

DEVAGENT_SLUG = "dev_agent"


@pytest.fixture()
def isolated_data(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh DATA_DIR + fresh app modules, matching the smoke-test pattern."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    yield data_dir


def _render(fn):
    """Run a render function, swallowing the expected StopRerun."""
    try:
        fn()
    except StopRerun:
        pass


# (welcome button key, target page after the click)
BUTTON_NAV_CASES = [
    ("welcome_step_settings", "settings"),
    (f"welcome_step_orchestrator_settings:{DEVAGENT_SLUG}",
     f"orchestrator_settings:{DEVAGENT_SLUG}"),
    ("welcome_step_skills", "skills"),
    ("welcome_step_orchestrators", "orchestrators"),
]


@pytest.mark.parametrize("button_key,expected_page", BUTTON_NAV_CASES)
def test_welcome_step_button_navigates(isolated_data, button_key, expected_page):
    """
    Given the welcome page rendered on a fresh install,
    when the newcomer clicks one of the four onboarding buttons,
    then the app navigates to that button's page (the DevAgent button must
    open the dedicated settings page, never the chat page).
    """
    with install_streamlit_mock() as st:
        app_mod = importlib.import_module("ui.app")
        st.session_state.update({
            "_defaults_seeded": True,
            "ui_lang": "English",
            "current_page": "welcome",
        })
        _render(app_mod.main)
        assert st.errors == [], "welcome render emitted errors: %r" % st.errors

        rendered_keys = {kwargs.get("key") for _n, _a, kwargs in st.calls}
        assert button_key in rendered_keys, "onboarding button missing: " + button_key

        st.click(button_key)
        _render(app_mod.main)
        assert st.session_state["current_page"] == expected_page, (
            "button %r navigated to %r instead of %r"
            % (button_key, st.session_state["current_page"], expected_page)
        )


def test_devagent_settings_full_render_without_api_key_warning(isolated_data):
    """
    Given a fresh install with the built-in DevAgent profile seeded,
    when the dedicated DevAgent settings page is opened,
    then all eight settings tabs and the back-to-chat button render without
    the orchestrator no-API-key warning and without errors.
    """
    from core.orchestrators import ensure_builtin_orchestrators
    ensure_builtin_orchestrators()

    with install_streamlit_mock() as st:
        app_mod = importlib.import_module("ui.app")
        st.session_state.update({
            "_defaults_seeded": True,
            "ui_lang": "English",
            "current_page": f"orchestrator_settings:{DEVAGENT_SLUG}",
        })
        _render(app_mod.main)

        assert st.errors == [], "settings render emitted errors: %r" % st.errors

        from core.i18n import t
        api_key_warning = t("orch_no_api_key", lang="English")
        warning_texts = [str(w) for w in st.warnings]
        assert api_key_warning not in warning_texts, (
            "orchestrator settings page shows the missing-API-key warning: %r"
            % warning_texts
        )

        tab_calls = [
            labels
            for name, args, _kw in st.calls
            if name == "tabs"
            for labels in args
        ]
        assert any(len(labels) == 8 for labels in tab_calls), (
            "expected the 8 native settings tabs, got %r" % tab_calls
        )

        rendered_keys = {kwargs.get("key") for _n, _a, kwargs in st.calls}
        assert f"orch_settings_back_{DEVAGENT_SLUG}" in rendered_keys, (
            "back-to-chat button is missing from the settings page"
        )
