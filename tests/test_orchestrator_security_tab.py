# -*- coding: utf-8 -*-
"""tests/test_orchestrator_security_tab.py - the Security settings section.

The section owns ONE choice: the access mode a code run of this employee
executes under. These tests pin the behaviour a user sees:

- the listed modes are exactly read_only, workspace_write, legacy_process,
  off and full access, as a single choice rather than independent switches;
- the chosen mode shows the facts of its profile right below the selector;
- saving writes the whole decision (the environment grant and the profile);
- the list of recent runs is not rendered at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import StopRerun, install_streamlit_mock

SLUG = "myorch"
LANG = "English"


@pytest.fixture()
def st_env():
    """Fresh Streamlit mock with real i18n and a clean ``ui`` import state."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as mock:
        mock.session_state.update({"ui_lang": LANG})
        yield mock


def _orch(config=None):
    """Build a minimal employee record; *config* is merged over the base."""
    cfg = {"strong_service": "DeepSeek", "strong_model": "m1"}
    if config:
        cfg.update(config)
    return {"slug": SLUG, "name": "MyOrch", "prompt_text": "", "config": cfg}


def _modules(st_env, orch):
    """Import the panel bound to the active mock and a fixed employee."""
    import ui.pages.orchestrator as orch_mod
    import ui.pages.orchestrator_sandbox as sandbox_mod

    # The panel resolves streamlit through the page facade, so only the facade
    # is bound to this mock. Rebinding the panel's own proxy would leave the
    # module holding THIS test's double and break the next test in the run.
    orch_mod.st = st_env
    sandbox_mod.get_orchestrator = lambda slug: orch if slug == SLUG else None
    sandbox_mod.save_orchestrator = lambda slug, config=None, **kw: True
    return orch_mod, sandbox_mod


def _render(sandbox_mod) -> None:
    """Render one pass, swallowing the rerun unwind."""
    try:
        sandbox_mod.render_sandbox_settings(SLUG, LANG)
    except StopRerun:
        pass


def _radio(st_env, key):
    """Return the recorded kwargs of a radio widget, or None."""
    for name, args, kwargs in st_env.calls:
        if name == "radio" and kwargs.get("key") == key:
            return kwargs
    return None


def _tables(st_env):
    """Return every rendered table."""
    return [args[0] for name, args, _kw in st_env.calls if name == "table"]


def test_the_listed_modes_are_exactly_the_five_documented_ones(st_env):
    """Given the Security tab, when rendered, then one choice with five modes.

    A run has exactly one operating-system boundary, so the modes cannot be
    independent checkboxes: two of them would contradict each other.
    """
    _orch_mod, sandbox_mod = _modules(st_env, _orch())

    _render(sandbox_mod)

    radio = _radio(st_env, f"orch_access_mode_{SLUG}")
    assert radio is not None, "the access mode is not a single choice"
    assert list(radio["options"]) == ["read_only", "workspace_write",
                                      "legacy_process", "off", "full"]


def test_every_mode_has_a_label_that_separates_off_from_full_access(st_env):
    """Off and full access must read as different modes, not as synonyms."""
    _orch_mod, sandbox_mod = _modules(st_env, _orch())

    _render(sandbox_mod)

    radio = _radio(st_env, f"orch_access_mode_{SLUG}")
    labels = {mode: radio["format_func"](mode) for mode in radio["options"]}
    assert all(labels.values()), labels
    assert labels["off"] != labels["full"]
    assert "platform environment" in labels["full"].lower()


def test_the_chosen_mode_shows_the_facts_of_its_profile(st_env):
    """Given workspace_write, when rendered, then its profile facts are shown.

    The table appears below the selector in the same render pass: the user
    must not have to save first to learn what the chosen mode does.
    """
    _orch_mod, sandbox_mod = _modules(st_env, _orch({"sandbox_profile": "workspace_write"}))

    _render(sandbox_mod)

    facts = [table for table in _tables(st_env)
             if table and isinstance(table[0], dict)
             and table[0].get("profile") == "workspace_write"]
    assert facts, f"no facts for the chosen mode: {_tables(st_env)}"
    row = facts[0][0]
    assert row["isolation"] == "os"
    assert row["network"]
    assert set(row) >= {"profile", "isolation", "network", "via", "status"}


def test_full_access_is_shown_as_the_off_profile_with_the_platform_env(st_env):
    """Full access shows the profile its runs use and warns about the grant."""
    _orch_mod, sandbox_mod = _modules(st_env, _orch({"env_isolation": False}))

    _render(sandbox_mod)

    radio = _radio(st_env, f"orch_access_mode_{SLUG}")
    assert _current(radio) == "full"
    facts = [table for table in _tables(st_env)
             if table and isinstance(table[0], dict) and "off" in table[0].get("profile", "")]
    assert facts, f"full access shows no profile facts: {_tables(st_env)}"
    assert facts[0][0]["isolation"] == "none"
    assert any("platform environment" in str(msg).lower() for msg in st_env.errors)


def _current(radio):
    """Return the option the radio selected."""
    options = list(radio["options"])
    index = radio.get("index", 0) or 0
    return options[index] if options and index < len(options) else None


def test_saving_a_profile_mode_writes_both_config_keys(st_env):
    """Given a chosen profile, when saved, then grant and profile are written.

    The mode is one decision stored as two keys; writing only one of them
    would let the leftover value decide the next run.
    """
    orch = _orch({"sandbox_profile": "workspace_write", "chat_sandbox": False})
    _orch_mod, sandbox_mod = _modules(st_env, orch)
    saved = {}
    sandbox_mod.save_orchestrator = lambda slug, config=None, **kw: (
        saved.update({"config": dict(config or {})}) or True)
    st_env._selectbox_returns[f"orch_access_mode_{SLUG}"] = "read_only"
    st_env.session_state[f"orch_access_mode_{SLUG}"] = "read_only"
    st_env.click(f"orch_access_save_{SLUG}")

    _render(sandbox_mod)

    assert saved["config"]["sandbox_profile"] == "read_only"
    assert saved["config"]["env_isolation"] is True
    # The removed toolbar switch must not keep overriding the choice.
    assert "chat_sandbox" not in saved["config"]


def test_saving_full_access_writes_the_grant_and_the_off_profile(st_env):
    """Given full access, when saved, then the grant and the off profile land."""
    orch = _orch({"sandbox_profile": "read_only"})
    _orch_mod, sandbox_mod = _modules(st_env, orch)
    saved = {}
    sandbox_mod.save_orchestrator = lambda slug, config=None, **kw: (
        saved.update({"config": dict(config or {})}) or True)
    st_env.session_state[f"orch_access_mode_{SLUG}"] = "full"
    st_env.click(f"orch_access_save_{SLUG}")

    _render(sandbox_mod)

    assert saved["config"]["env_isolation"] is False
    assert saved["config"]["sandbox_profile"] == "off"


def test_the_recent_runs_list_is_not_rendered(st_env):
    """The section reports the mode, never a history of past runs."""
    from core.i18n import t

    _orch_mod, sandbox_mod = _modules(st_env, _orch())

    _render(sandbox_mod)

    recent = t("orch_sandbox_recent_runs", lang=LANG)
    markdown = [args[0] for name, args, _kw in st_env.calls if name == "markdown"]
    assert recent not in markdown


def test_a_legacy_record_reads_as_the_off_mode(st_env):
    """A cleared toolbar checkbox is the off mode, not a silent default."""
    _orch_mod, sandbox_mod = _modules(st_env, _orch({"chat_sandbox": False,
                                                    "sandbox_profile": "read_only"}))

    _render(sandbox_mod)

    radio = _radio(st_env, f"orch_access_mode_{SLUG}")
    assert _current(radio) == "off"


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
