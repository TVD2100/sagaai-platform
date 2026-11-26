# -*- coding: utf-8 -*-
"""
Scenario: a click inside a pinned settings section keeps the user in it.

Given an employee settings page opened on the Security section,
when the user picks another access mode in that section,
then the page stays on Security and raises no error.

The scenario runs against the real Streamlit runtime (``AppTest``) rather
than the lightweight mock: the defect it guards against lives in how
Streamlit gives a widget its identity, which a hand-written mock only
reproduces if it models that rule explicitly. Every other settings section
is replaced with a single line of text so the page renders without the
services those sections talk to; the Security section is the real one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SLUG = "myorch"
TABS_KEY = "orch_settings_tabs_" + SLUG
SECTION_KEY = "orch_settings_section_" + SLUG

#: The settings page with every section but Security replaced by a stub.
PAGE_SCRIPT = '''
import os
import sys

sys.path.insert(0, os.environ["SAGAAI_REPO_ROOT"])

import streamlit as st  # noqa: E402

ORCH = {"slug": "myorch", "name": "MyOrch", "prompt_text": "PROMPT",
        "config": {}, "is_builtin": True, "max_steps": 100}

import ui.pages.orchestrator as facade  # noqa: E402
facade.get_orchestrator = lambda slug: dict(ORCH)
facade.save_orchestrator = lambda slug, **kwargs: True

import ui.pages.orchestrator_settings as page  # noqa: E402
for _name in (
    "_render_models_settings",
    "_render_prompt_settings",
    "_render_economy_settings",
    "_render_orchestrator_functions_settings",
    "_render_orchestrator_instructions_settings",
    "_render_orch_skills_settings",
    "_render_orch_rag_bases",
    "_render_orch_connections",
    "_render_orch_memory",
    "_render_delegation_settings",
    "_render_other_settings",
):
    setattr(page, _name, (lambda n: (lambda slug, lang: st.write("BODY:" + n)))(_name))
page.get_orchestrator = lambda slug: dict(ORCH)

st.session_state.setdefault("ui_lang", "English")
page.page_orchestrator_settings("myorch")
'''


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A settings page rendered by the real Streamlit runtime.

    The ``ui.*`` modules are dropped from ``sys.modules`` around the test:
    other suites render the same pages against a stand-in Streamlit and bind
    that stand-in on the module object, so a cached copy would make this
    scenario measure the stand-in instead of the real runtime.
    """
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SAGAAI_REPO_ROOT", str(ROOT))
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "ui" or name.startswith("ui.")}
    for name in saved:
        sys.modules.pop(name, None)
    try:
        at = AppTest.from_string(PAGE_SCRIPT, default_timeout=90)
        at.run()
        assert not at.exception, [str(e.value) for e in at.exception]
        yield at
    finally:
        for name in [n for n in sys.modules if n == "ui" or n.startswith("ui.")]:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def _security_label() -> str:
    from core.i18n import t
    return t("orch_tab_security", lang="English")


def test_widget_click_inside_a_pinned_section_keeps_the_user_there(app):
    """Opening Security and then using it must not send the user to Models."""
    security_label = _security_label()

    # The chat badge pins the section before the page paints it.
    app.session_state[SECTION_KEY] = "security"
    app.run()
    assert app.session_state[TABS_KEY] == security_label

    # The user picks an access mode inside the section. The click reruns the
    # page with no request left, so only the widget's own stored label can
    # keep the section open.
    assert len(app.radio) == 1, "the Security section should render one mode selector"
    app.radio[0].set_value("off").run()

    assert not app.exception, [str(e.value) for e in app.exception]
    assert app.session_state[TABS_KEY] == security_label, (
        "the page jumped away from the section the user was working in"
    )


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
