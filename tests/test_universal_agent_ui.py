# -*- coding: utf-8 -*-
"""tests/test_universal_agent_ui.py - the checkbox that switches the executor off.

The right is a default, so the settings tab is where a user takes it away.
These tests pin the three things that make the box honest: it starts
checked, clearing it is what gets stored, and the executor never appears
TWICE - once as the default right and once as a hand-added allowlist entry.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_ui_tooltips import _invoke, mock_env  # noqa: F401

SLUG = "ua_ui_employee"
SAVE_KEY = f"orch_save_delegation_{SLUG}"
UNIVERSAL_KEY = f"orch_allowed_universal_{SLUG}"

LIVE_ORCHESTRATORS = [
    {"slug": SLUG, "name": "This Employee"},
    {"slug": "helper_emp", "name": "Helper"},
    {"slug": "universal_agent", "name": "Universal-Agent"},
]

LIMITS = {
    "max_depth": 1,
    "max_active_subagents": 5,
    "subagent_max_steps": 300,
    "subagent_budget_tokens": 0,
    "subagent_timeout_seconds": 3600,
    "total_subagent_runs": 64,
    "dialog_rounds": 5,
    "question_max_chars": 1500,
    "answer_max_chars": 4000,
    "answer_wait_seconds": 900,
    "max_parked_runs": 8,
}


@pytest.fixture
def env(mock_env):
    """Patch the tab's data layer; the executor is present and enabled."""
    import core.assistants as assistants_mod
    import ui.pages.orchestrator as orch_mod

    mocks = {
        "orch": {"name": "This Employee", "config": {}},
        "agents": [],
        "universal": True,
        "live": list(LIVE_ORCHESTRATORS),
        "set_agents": MagicMock(return_value=True),
        "set_universal": MagicMock(return_value=True),
        "set_assistants": MagicMock(return_value=True),
        "set_limits": MagicMock(return_value=True),
        "set_verifier": MagicMock(return_value=True),
        "success": MagicMock(),
        "failed": MagicMock(),
        "pinned": MagicMock(),
    }

    with patch.object(orch_mod, "get_orchestrator", lambda s: mocks["orch"]), \
         patch.object(orch_mod, "list_orchestrators", lambda: list(mocks["live"])), \
         patch.object(orch_mod, "get_allowed_agents", lambda s=SLUG: list(mocks["agents"])), \
         patch.object(orch_mod, "get_allowed_assistants", lambda s=SLUG: []), \
         patch.object(orch_mod, "get_universal_agent_enabled",
                      lambda s=SLUG: bool(mocks["universal"])), \
         patch.object(orch_mod, "set_universal_agent_enabled", mocks["set_universal"]), \
         patch.object(orch_mod, "get_delegation_limits", lambda s=SLUG: dict(LIMITS)), \
         patch.object(orch_mod, "set_allowed_agents", mocks["set_agents"]), \
         patch.object(orch_mod, "set_allowed_assistants", mocks["set_assistants"]), \
         patch.object(orch_mod, "set_delegation_limits", mocks["set_limits"]), \
         patch.object(orch_mod, "set_verifier_agent", mocks["set_verifier"]), \
         patch.object(orch_mod, "flash_success", mocks["success"]), \
         patch.object(orch_mod, "settings_save_failed", mocks["failed"]), \
         patch.object(orch_mod, "pin_settings_section", mocks["pinned"]), \
         patch.object(assistants_mod, "load_assistants_index", lambda: []):
        yield mock_env, mocks, orch_mod


def _render(st, orch_mod, *, clicks=(), checks=None, slug=SLUG):
    """Render the tab once with pre-seeded widget answers."""
    checks = checks or {}
    for key, value in checks.items():
        st.session_state[key] = value
    for click in clicks:
        st.click(click)
    _invoke(lambda: orch_mod._render_delegation_settings(slug, "English"))
    st.reset_clicks()
    for key in checks:
        st.session_state.pop(key, None)


def _widgets(st):
    return {kwargs.get("key"): kwargs
            for name, _args, kwargs in st.calls
            if name == "checkbox" and kwargs.get("key")}


# ─── the box itself ────────────────────────────────────────────────────────


def test_executor_has_its_own_checkbox(env):
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    assert UNIVERSAL_KEY in _widgets(st)


def test_box_starts_checked(env):
    """The default is ON: an untouched installation shows a checked box."""
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    assert _widgets(st)[UNIVERSAL_KEY]["value"] is True


def test_box_reflects_a_stored_decision(env):
    st, mocks, orch_mod = env
    mocks["universal"] = False
    _render(st, orch_mod)

    assert _widgets(st)[UNIVERSAL_KEY]["value"] is False


def test_executor_is_not_listed_twice(env):
    """One agent, one checkbox - even when it is ALSO in the allowlist."""
    st, mocks, orch_mod = env
    mocks["agents"] = ["universal_agent"]
    _render(st, orch_mod)

    widgets = _widgets(st)
    assert UNIVERSAL_KEY in widgets
    assert f"orch_allowed_agent_{SLUG}_universal_agent" not in widgets


def test_box_is_hidden_for_the_executor_itself(env):
    """The executor cannot delegate to itself through this right."""
    st, _mocks, orch_mod = env
    _render(st, orch_mod, slug="universal_agent")

    assert "orch_allowed_universal_universal_agent" not in _widgets(st)


def test_box_is_hidden_when_the_executor_is_not_installed(env):
    """A checkbox for a deleted employee would be a dead end."""
    st, mocks, orch_mod = env
    mocks["live"] = [e for e in LIVE_ORCHESTRATORS
                     if e["slug"] != "universal_agent"]
    _render(st, orch_mod)

    assert UNIVERSAL_KEY not in _widgets(st)


def test_label_is_translated_not_a_raw_key(env):
    st, _mocks, orch_mod = env
    _render(st, orch_mod)

    labels = [args[0] for name, args, kwargs in st.calls
              if name == "checkbox" and kwargs.get("key") == UNIVERSAL_KEY]
    assert labels and labels[0] != "orch_delegation_universal_agent"


# ─── saving ────────────────────────────────────────────────────────────────


def test_clearing_the_box_stores_the_decision(env):
    st, mocks, orch_mod = env
    _render(st, orch_mod, clicks=(SAVE_KEY,), checks={UNIVERSAL_KEY: False})

    mocks["set_universal"].assert_called_once_with(SLUG, False)


def test_untouched_box_writes_nothing(env):
    """Saving the other settings must not record a decision nobody made."""
    st, mocks, orch_mod = env
    _render(st, orch_mod, clicks=(SAVE_KEY,))

    mocks["set_universal"].assert_not_called()


def test_switching_back_on_is_stored(env):
    st, mocks, orch_mod = env
    mocks["universal"] = False
    _render(st, orch_mod, clicks=(SAVE_KEY,), checks={UNIVERSAL_KEY: True})

    mocks["set_universal"].assert_called_once_with(SLUG, True)


def test_failed_write_is_reported(env):
    st, mocks, orch_mod = env
    mocks["set_universal"].return_value = False
    _render(st, orch_mod, clicks=(SAVE_KEY,), checks={UNIVERSAL_KEY: False})

    mocks["failed"].assert_called_once()
    mocks["success"].assert_not_called()


def test_clearing_the_box_leaves_the_allowlist_alone(env):
    """The two rights are saved separately: one never rewrites the other."""
    st, mocks, orch_mod = env
    mocks["agents"] = ["helper_emp"]
    _render(st, orch_mod, clicks=(SAVE_KEY,), checks={UNIVERSAL_KEY: False})

    assert mocks["set_agents"].call_args[0][1] == ["helper_emp"]
