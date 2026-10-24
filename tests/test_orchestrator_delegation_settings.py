# -*- coding: utf-8 -*-
"""tests/test_orchestrator_delegation_settings.py - the Delegation settings tab.

The tab is the only place where a user can hand out delegation rights, so
the properties pinned here are the ones a silent regression would cost a
real feature: checkboxes are keyed per entity (two employees never share a
widget), stale ids are shown instead of being dropped from the form, every
limit number input carries a real tooltip, and Save persists exactly what
the widgets returned.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_ui_tooltips import _help_by_key, _invoke, mock_env  # noqa: F401

SLUG = "deleg_emp"
SAVE_KEY = f"orch_save_delegation_{SLUG}"

LIVE_ORCHESTRATORS = [
    {"slug": "deleg_emp", "name": "This Employee"},
    {"slug": "helper_emp", "name": "Helper"},
    {"slug": "other_emp", "name": "Other"},
]

LIVE_ASSISTANTS = [
    {"id": "asst0001", "name": "Analyst"},
    {"id": "asst0002", "name": "Writer"},
]

BASE_LIMITS = {
    "max_depth": 1,
    "max_active_subagents": 3,
    "subagent_max_steps": 20,
    "subagent_budget_tokens": 0,
    "subagent_timeout_seconds": 600,
    "total_subagent_runs": 32,
}

LIMIT_WIDGET_KEYS = (
    f"orch_delegation_max_depth_{SLUG}",
    f"orch_delegation_max_active_{SLUG}",
    f"orch_delegation_max_steps_{SLUG}",
    f"orch_delegation_total_runs_{SLUG}",
    f"orch_delegation_timeout_{SLUG}",
    f"orch_delegation_budget_{SLUG}",
)


@pytest.fixture
def env(mock_env):
    """Patch the data layer of the tab and return (st, mocks)."""
    import core.assistants as assistants_mod
    import ui.pages.orchestrator as orch_mod

    mocks = {
        "orch": {"name": "This Employee", "config": {}},
        "agents": ["helper_emp"],
        "assistants": ["asst0001"],
        "limits": dict(BASE_LIMITS),
        "set_agents": MagicMock(return_value=True),
        "set_assistants": MagicMock(return_value=True),
        "set_limits": MagicMock(return_value=True),
        "success": MagicMock(),
        "failed": MagicMock(),
        "pinned": MagicMock(),
    }

    with patch.object(orch_mod, "get_orchestrator", lambda s: mocks["orch"]), \
         patch.object(orch_mod, "list_orchestrators", lambda: list(LIVE_ORCHESTRATORS)), \
         patch.object(orch_mod, "get_allowed_agents", lambda s=SLUG: list(mocks["agents"])), \
         patch.object(orch_mod, "get_allowed_assistants", lambda s=SLUG: list(mocks["assistants"])), \
         patch.object(orch_mod, "get_delegation_limits", lambda s=SLUG: dict(mocks["limits"])), \
         patch.object(orch_mod, "set_allowed_agents", mocks["set_agents"]), \
         patch.object(orch_mod, "set_allowed_assistants", mocks["set_assistants"]), \
         patch.object(orch_mod, "set_delegation_limits", mocks["set_limits"]), \
         patch.object(orch_mod, "flash_success", mocks["success"]), \
         patch.object(orch_mod, "settings_save_failed", mocks["failed"]), \
         patch.object(orch_mod, "pin_settings_section", mocks["pinned"]), \
         patch.object(assistants_mod, "load_assistants_index",
                      lambda: list(LIVE_ASSISTANTS)):
        yield mock_env, mocks, orch_mod


def _render(st, orch_mod, *, clicks=(), numbers=None, checks=None):
    """Render the tab once with pre-seeded widget answers.

    Checkboxes are stateful widgets: the mock keeps their value in
    ``session_state`` keyed by the widget key, so a "click" is a seeded
    session value, not a one-shot button press.
    """
    for key, value in (checks or {}).items():
        st.session_state[key] = value
    for click in clicks:
        st.click(click)
    for key, value in (numbers or {}).items():
        st._number_returns[key] = value
    _invoke(lambda: orch_mod._render_delegation_settings(SLUG, "English"))
    st.reset_clicks()
    for key in (checks or {}):
        st.session_state.pop(key, None)


def _keys(st):
    return [kwargs.get("key") for _name, _args, kwargs in st.calls if kwargs.get("key")]


# ─── entity checkboxes ─────────────────────────────────────────────────────


def test_agent_checkboxes_cover_live_agents_and_skip_self(env):
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    keys = _keys(st)
    assert f"orch_allowed_agent_{SLUG}_helper_emp" in keys
    assert f"orch_allowed_agent_{SLUG}_other_emp" in keys
    # Delegating to yourself needs no configuration, so no checkbox for self.
    assert f"orch_allowed_agent_{SLUG}_{SLUG}" not in keys


def test_agent_checkbox_reflects_the_stored_allowlist(env):
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    values = {kwargs["key"]: kwargs.get("value")
              for _n, _a, kwargs in st.calls if kwargs.get("key")}
    assert values[f"orch_allowed_agent_{SLUG}_helper_emp"] is True
    assert values[f"orch_allowed_agent_{SLUG}_other_emp"] is False


def test_assistant_checkboxes_reflect_the_stored_allowlist(env):
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    values = {kwargs["key"]: kwargs.get("value")
              for _n, _a, kwargs in st.calls if kwargs.get("key")}
    assert values[f"orch_allowed_assistant_{SLUG}_asst0001"] is True
    assert values[f"orch_allowed_assistant_{SLUG}_asst0002"] is False


def test_no_assistants_yet_shows_a_hint(env):
    st, _mocks, orch_mod = env
    import core.assistants as assistants_mod

    with patch.object(assistants_mod, "load_assistants_index", lambda: []):
        _render(st, orch_mod)

    from core.i18n import t
    captions = [args[0] for name, args, _k in st.calls if name == "info" and args]
    assert t("orch_delegation_no_assistants", lang="English") in captions


def test_stale_agent_id_is_shown_with_a_warning(env):
    """A deleted employee must not vanish silently: the config still names it."""
    st, mocks, orch_mod = env
    mocks["agents"] = ["deleted_emp"]

    _render(st, orch_mod)

    from core.i18n import t
    warnings = [args[0] for name, args, _k in st.calls if name == "warning" and args]
    assert t("orch_delegation_stale_agent", lang="English", slug="deleted_emp") in warnings
    assert f"orch_allowed_agent_stale_{SLUG}_deleted_emp" in _keys(st)


def test_stale_assistant_id_is_shown_with_a_warning(env):
    st, mocks, orch_mod = env
    mocks["assistants"] = ["goneasst"]

    _render(st, orch_mod)

    from core.i18n import t
    warnings = [args[0] for name, args, _k in st.calls if name == "warning" and args]
    assert t("orch_delegation_stale_assistant", lang="English",
             ident="goneasst") in warnings


# ─── limits ────────────────────────────────────────────────────────────────


def test_limit_inputs_show_the_stored_values(env):
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    values = {kwargs["key"]: kwargs.get("value")
              for _n, _a, kwargs in st.calls if kwargs.get("key")}
    assert values[f"orch_delegation_max_depth_{SLUG}"] == 1
    assert values[f"orch_delegation_max_active_{SLUG}"] == 3
    assert values[f"orch_delegation_max_steps_{SLUG}"] == 20
    assert values[f"orch_delegation_total_runs_{SLUG}"] == 32
    assert values[f"orch_delegation_timeout_{SLUG}"] == 600
    assert values[f"orch_delegation_budget_{SLUG}"] == 0


def test_every_limit_input_has_a_real_tooltip(env):
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    helps = _help_by_key(st)
    for key in LIMIT_WIDGET_KEYS:
        assert key in helps, "limit widget not rendered: " + key
        text = helps[key]
        assert isinstance(text, str) and text.strip(), "empty tooltip for " + key
        assert text != key, "raw i18n key leaked for " + key
        assert not text.startswith("{"), "unformatted placeholder for " + key


def test_budget_default_of_zero_means_unlimited(env):
    """0 is the documented "no limit" value and must stay selectable."""
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    kwargs = {k.get("key"): k for _n, _a, k in st.calls if k.get("key")}
    budget = kwargs[f"orch_delegation_budget_{SLUG}"]
    assert budget["min_value"] == 0
    depth = kwargs[f"orch_delegation_max_depth_{SLUG}"]
    assert depth["min_value"] == 0
    pool = kwargs[f"orch_delegation_max_active_{SLUG}"]
    assert pool["min_value"] == 1


# ─── saving ────────────────────────────────────────────────────────────────


def test_save_persists_selection_and_limits(env):
    st, mocks, orch_mod = env
    _render(st, orch_mod,
            clicks=(SAVE_KEY,),
            checks={f"orch_allowed_agent_{SLUG}_other_emp": True,
                    f"orch_allowed_assistant_{SLUG}_asst0002": True},
            numbers={f"orch_delegation_max_depth_{SLUG}": 2,
                     f"orch_delegation_max_active_{SLUG}": 5})

    agents_arg = mocks["set_agents"].call_args[0]
    assert agents_arg[0] == SLUG
    assert set(agents_arg[1]) == {"helper_emp", "other_emp"}

    assistants_arg = mocks["set_assistants"].call_args[0]
    assert set(assistants_arg[1]) == {"asst0001", "asst0002"}

    limits_arg = mocks["set_limits"].call_args[0][1]
    assert limits_arg["max_depth"] == 2
    assert limits_arg["max_active_subagents"] == 5
    assert limits_arg["subagent_max_steps"] == 20
    assert limits_arg["total_subagent_runs"] == 32

    mocks["success"].assert_called_once()
    mocks["pinned"].assert_called_once_with(SLUG, "delegation")


def test_save_reports_failure_when_a_setter_fails(env):
    st, mocks, orch_mod = env
    mocks["set_limits"].return_value = False

    _render(st, orch_mod, clicks=(SAVE_KEY,))

    mocks["failed"].assert_called_once()
    mocks["success"].assert_not_called()


def test_unchecking_a_stored_agent_removes_it(env):
    """Clearing a stored right is a valid configuration, not an error."""
    st, mocks, orch_mod = env
    mocks["agents"] = ["helper_emp"]
    mocks["assistants"] = ["asst0001"]

    _render(st, orch_mod, clicks=(SAVE_KEY,),
            checks={f"orch_allowed_agent_{SLUG}_helper_emp": False,
                    f"orch_allowed_assistant_{SLUG}_asst0001": False})

    mocks["set_agents"].assert_called_once()
    assert mocks["set_agents"].call_args[0][1] == []
    assert mocks["set_assistants"].call_args[0][1] == []


# ─── missing orchestrator ──────────────────────────────────────────────────


def test_unknown_orchestrator_renders_an_error(env):
    st, mocks, orch_mod = env
    mocks["orch"] = None

    _render(st, orch_mod)

    from core.i18n import t
    errors = [args[0] for name, args, _k in st.calls if name == "error" and args]
    assert t("orch_not_found", lang="English") in errors
    mocks["set_agents"].assert_not_called()
