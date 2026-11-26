# -*- coding: utf-8 -*-
"""tests/scenarios/test_access_badge_scenario.py - the access badge journey.

The badge in the chat toolbar is the shortest way to the setting that owns
the access mode. These scenarios walk the public entry points:

  Scenario 1 - given an employee with the full-access grant, when the chat
               renders, then the badge shows an explicit open lock instead
               of the emoji that reads as a closed one.
  Scenario 2 - given a user who worked in the settings before, when the
               badge is clicked, then the settings page opens on the
               Security section rather than on the stored tab.
  Scenario 3 - given the settings page, when it renders without any request,
               then the tabs hold the section the user picked.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402

SLUG = "custom1"
LANG = "English"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Isolated DATA_DIR and a fresh ``ui.*`` package under the mock."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st:
        st.session_state.update({"ui_lang": LANG})
        yield st, monkeypatch


def _orch(config=None):
    """Build a minimal employee record; *config* is merged over the base."""
    cfg = {"strong_service": "DeepSeek", "strong_model": "m1"}
    if config:
        cfg.update(config)
    return {"slug": SLUG, "name": "Custom", "description": "",
            "is_builtin": False, "prompt_text": "", "config": cfg}


def _bind(st, monkeypatch, orch):
    """Bind the page facade and the settings page to the mocked employee."""
    import ui.pages.orchestrator as orch_page
    import ui.pages.orchestrator_settings as settings_page

    orch_page.st = st
    settings_page.st = st
    patch = lambda slug: orch if slug == SLUG else None  # noqa: E731
    monkeypatch.setattr(orch_page, "get_orchestrator", patch)
    monkeypatch.setattr(settings_page, "get_orchestrator", patch)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    return orch_page, settings_page


def _render(fn):
    """Run one render pass, swallowing the rerun unwind."""
    try:
        fn()
    except StopRerun:
        pass


def _badge(st):
    """Return the recorded kwargs of the access badge button."""
    for name, args, kwargs in st.calls:
        if name == "button" and kwargs.get("key") == f"orch_access_badge_{SLUG}":
            return {"label": str(args[0]) if args else "", **kwargs}
    return None


def test_the_grant_badge_reads_as_an_open_lock(env):
    """Given the full-access grant, when the chat renders, then it reads open."""
    st, monkeypatch = env
    orch_page, _settings = _bind(st, monkeypatch, _orch({"env_isolation": False}))

    _render(lambda: orch_page.page_orchestrator(SLUG))

    badge = _badge(st)
    assert badge is not None
    assert badge["label"].startswith(":material/lock_open:"), badge
    # The emoji form is what the user could not tell apart from a closed lock.
    assert "\U0001f513" not in badge["label"]


def test_the_badge_opens_the_security_section_of_a_worked_in_page(env):
    """Given an earlier settings visit, when the badge is clicked, then

    the settings page opens on Security instead of the remembered tab.
    """
    st, monkeypatch = env
    orch_page, settings_page = _bind(st, monkeypatch, _orch())

    _render(lambda: settings_page.page_orchestrator_settings(SLUG))
    labels = [args[0] for name, args, _kw in st.calls if name == "tabs"][-1]
    tabs_key = orch_page.settings_tabs_key(SLUG)
    # The user worked in the Models section and then went back to the chat.
    st.session_state[tabs_key] = labels[0]

    _render(lambda: orch_page.page_orchestrator(SLUG))
    st.click(f"orch_access_badge_{SLUG}")
    _render(lambda: orch_page.page_orchestrator(SLUG))
    _render(lambda: settings_page.page_orchestrator_settings(SLUG))

    expected = labels[orch_page.SETTINGS_SECTIONS.index("security")]
    assert st.session_state[tabs_key] == expected
    # The request is consumed: the next pass keeps the user's own choice.
    assert orch_page.settings_section_key(SLUG) not in st.session_state


def test_an_ordinary_render_keeps_the_chosen_section(env):
    """Given a stored section, when the page renders, then it stays put."""
    st, monkeypatch = env
    orch_page, settings_page = _bind(st, monkeypatch, _orch())

    _render(lambda: settings_page.page_orchestrator_settings(SLUG))
    labels = [args[0] for name, args, _kw in st.calls if name == "tabs"][-1]
    chosen = labels[orch_page.SETTINGS_SECTIONS.index("memory")]
    st.session_state[orch_page.settings_tabs_key(SLUG)] = chosen

    _render(lambda: settings_page.page_orchestrator_settings(SLUG))

    assert st.session_state[orch_page.settings_tabs_key(SLUG)] == chosen

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
