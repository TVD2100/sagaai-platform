# -*- coding: utf-8 -*-
"""tests/scenarios/test_orchestrator_other_settings_scenario.py - user-level
scenario tests for the orchestrator "Other" settings tab (max steps per task).

Scenarios (given -> when -> then), walking the public UI entry point
``ui.app.main`` with ``current_page = orchestrator_settings:<slug>``:

  Scenario 1 - the Other tab renders the step-limit widget seeded with the
               500-step default and no errors.
  Scenario 2 - an employee raises the limit to 350 for one task and saves:
               the value is persisted in the real repository (not a mock).
  Scenario 3 - the saved limit survives a brand-new render of the settings
               page.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

DEVAGENT_SLUG = "dev_agent"
MAX_STEPS_KEY = f"orch_max_steps_{DEVAGENT_SLUG}"
SAVE_KEY = f"orch_save_other_{DEVAGENT_SLUG}"


@pytest.fixture()
def isolated_data(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh DATA_DIR + fresh app modules, matching the welcome-scenario
    fixture."""
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


def _render_settings(st):
    """Render ui.app.main with the DevAgent settings page active."""
    app_mod = importlib.import_module("ui.app")
    st.session_state.update({
        "_defaults_seeded": True,
        "ui_lang": "English",
        "current_page": f"orchestrator_settings:{DEVAGENT_SLUG}",
    })
    _render(app_mod.main)


def _number_input_kwargs(st):
    for name, _args, kwargs in st.calls:
        if name == "number_input" and kwargs.get("key") == MAX_STEPS_KEY:
            return kwargs
    return None


def _success_calls(st):
    return [args[0] for name, args, _kw in st.calls if name == "success"]


def test_scenario_other_tab_shows_default_500(isolated_data):
    """Scenario 1: opening the settings page on a fresh install.

    Given the built-in DevAgent profile exists with no stored step limit,
    when  the user opens the Other settings tab,
    then  the step-limit widget renders with the 500-step default,
          clamped to [1, 10000], a real tooltip and no errors.
    """
    from core.orchestrators import ensure_builtin_orchestrators, get_orchestrator
    from storage.models import DEFAULT_MAX_STEPS
    from core.i18n import t

    ensure_builtin_orchestrators()
    assert get_orchestrator(DEVAGENT_SLUG)["max_steps"] == DEFAULT_MAX_STEPS

    with install_streamlit_mock() as st:
        _render_settings(st)

    assert st.errors == [], "settings render emitted errors: %r" % st.errors
    kwargs = _number_input_kwargs(st)
    assert kwargs is not None, "max-steps number input was not rendered"
    assert kwargs.get("value") == 500
    assert kwargs.get("min_value") == 1
    assert kwargs.get("max_value") == 10000
    help_text = kwargs.get("help")
    assert help_text == t("orch_max_steps_help", lang="English")
    assert help_text.strip() and help_text != "orch_max_steps_help"


def test_scenario_other_tab_save_persists_to_repository(isolated_data):
    """Scenario 2: raising the limit for one task.

    Given the DevAgent profile and the Other tab rendered,
    when  the user enters 350 and clicks the save button,
    then  the value is persisted in the real repository and a localized
          success message is shown.
    """
    from core.orchestrators import ensure_builtin_orchestrators, get_orchestrator
    from core.i18n import t

    ensure_builtin_orchestrators()

    with install_streamlit_mock() as st:
        _render_settings(st)
        st._number_returns[MAX_STEPS_KEY] = 350
        st.click(SAVE_KEY)
        _render_settings(st)

    assert st.errors == [], "save render emitted errors: %r" % st.errors
    orch = get_orchestrator(DEVAGENT_SLUG)
    assert orch["max_steps"] == 350, "saved value was not persisted: %r" % orch
    success = _success_calls(st)
    expected = t("orch_max_steps_saved", lang="English", value=350)
    assert expected in success, "success message missing: %r" % success


def test_scenario_other_tab_saved_limit_survives_rerender(isolated_data):
    """Scenario 3: the saved limit survives a brand-new render.

    Given the user saved 350 in the previous session,
    when  the settings page is rendered again from scratch,
    then  the step-limit widget shows the persisted 350, not the default.
    """
    from core.orchestrators import ensure_builtin_orchestrators, save_orchestrator

    ensure_builtin_orchestrators()
    assert save_orchestrator(DEVAGENT_SLUG, max_steps=350)

    with install_streamlit_mock() as st:
        _render_settings(st)

    assert st.errors == [], "settings render emitted errors: %r" % st.errors
    kwargs = _number_input_kwargs(st)
    assert kwargs is not None, "max-steps number input was not rendered"
    assert kwargs.get("value") == 350
