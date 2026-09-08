# -*- coding: utf-8 -*-
"""tests/test_orchestrator_other_settings.py - targeted UI tests for the
"Other" settings tab (max steps per task).

Renders ui.pages.orchestrator._render_other_settings under the Streamlit
mock and verifies:
- the number input renders with a real non-empty tooltip and the current
  default (500) when the orchestrator has no stored value;
- a stored max_steps value is respected;
- out-of-range user input is clamped to [1, 10000] before saving;
- the save button persists the value via save_orchestrator and shows a
  success message, or an error when the save fails.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_ui_tooltips import _invoke, mock_env  # noqa: F401

SLUG = "test"
WIDGET_KEY = f"orch_max_steps_{SLUG}"
SAVE_KEY = f"orch_save_other_{SLUG}"

ORCH_NO_MAX = {
    "name": "Test Orchestrator",
    "description": "t",
    "prompt_text": "p",
    "config": {},
}


def _render_other(st, orch, number_value=None, click=None):
    """Render the Other tab once under the active streamlit mock."""
    import ui.pages.orchestrator as orch_mod
    if click:
        st.click(click)
    if number_value is not None:
        st._number_returns[WIDGET_KEY] = number_value
    save_mock = MagicMock(return_value=True)
    with patch.object(orch_mod, "get_orchestrator", return_value=orch), \
         patch.object(orch_mod, "save_orchestrator", save_mock):
        _invoke(lambda: orch_mod._render_other_settings(SLUG, "English"))
    return save_mock


def _number_input_kwargs(st):
    for name, _args, kwargs in st.calls:
        if name == "number_input" and kwargs.get("key") == WIDGET_KEY:
            return kwargs
    return None


def test_other_tab_default_value_and_tooltip(mock_env):
    """Widget renders with the 500-step default and a real tooltip."""
    from core.i18n import t
    _render_other(mock_env, dict(ORCH_NO_MAX))

    kwargs = _number_input_kwargs(mock_env)
    assert kwargs is not None, "max-steps number input was not rendered"
    assert kwargs.get("value") == 500
    assert kwargs.get("min_value") == 1
    assert kwargs.get("max_value") == 10000
    help_text = kwargs.get("help")
    assert help_text == t("orch_max_steps_help", lang="English")
    assert help_text.strip() and help_text != "orch_max_steps_help"


def test_other_tab_respects_stored_value(mock_env):
    """A stored max_steps value is shown instead of the default."""
    _render_other(mock_env, dict(ORCH_NO_MAX, max_steps=250))
    kwargs = _number_input_kwargs(mock_env)
    assert kwargs is not None
    assert kwargs.get("value") == 250


def test_other_tab_save_persists_and_shows_success(mock_env):
    """Clicking save persists the entered value and shows a success message."""
    from core.i18n import t
    save_mock = _render_other(mock_env, dict(ORCH_NO_MAX, max_steps=100),
                              number_value=750, click=SAVE_KEY)
    save_mock.assert_called_once_with(SLUG, max_steps=750)
    success_calls = [args[0] for name, args, _kw in mock_env.calls if name == "success"]
    assert t("orch_max_steps_saved", lang="English", value=750) in success_calls


def test_other_tab_save_clamps_upper_bound(mock_env):
    """Values above 10000 are clamped to 10000 before saving."""
    save_mock = _render_other(mock_env, dict(ORCH_NO_MAX, max_steps=100),
                              number_value=25000, click=SAVE_KEY)
    save_mock.assert_called_once_with(SLUG, max_steps=10000)


def test_other_tab_save_clamps_lower_bound(mock_env):
    """Values below 1 are clamped to 1 before saving."""
    save_mock = _render_other(mock_env, dict(ORCH_NO_MAX, max_steps=100),
                              number_value=0, click=SAVE_KEY)
    save_mock.assert_called_once_with(SLUG, max_steps=1)


def test_other_tab_invalid_input_falls_back_to_current(mock_env):
    """Non-numeric input falls back to the currently stored value."""
    save_mock = _render_other(mock_env, dict(ORCH_NO_MAX, max_steps=123),
                              number_value="abc", click=SAVE_KEY)
    save_mock.assert_called_once_with(SLUG, max_steps=123)


def test_other_tab_save_failure_shows_error(mock_env):
    """A failed save shows the error message and does not claim success."""
    import ui.pages.orchestrator as orch_mod
    mock_env.click(SAVE_KEY)
    save_mock = MagicMock(return_value=False)
    with patch.object(orch_mod, "get_orchestrator",
                      return_value=dict(ORCH_NO_MAX, max_steps=100)), \
         patch.object(orch_mod, "save_orchestrator", save_mock):
        _invoke(lambda: orch_mod._render_other_settings(SLUG, "English"))
    save_mock.assert_called_once_with(SLUG, max_steps=100)
    from core.i18n import t
    assert t("orch_other_save_error", lang="English") in mock_env.errors
    success_calls = [args[0] for name, args, _kw in mock_env.calls if name == "success"]
    assert not success_calls
