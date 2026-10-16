# -*- coding: utf-8 -*-
"""tests/test_cold_start_deps.py - cold-start dependency warning in the UI.

Verifies that ui.app renders a warning listing missing and outdated
packages exactly once per session at cold start, and renders nothing when
every dependency is up to date. Reuses the shared Streamlit mock and the
patch bundles from tests.test_ui_pages.

Only messages that start with the dependency i18n keys are inspected: the
chat page emits unrelated warnings of its own during a full main() render.
"""
from __future__ import annotations

import sys
import pytest
from unittest.mock import patch

from tests._st_mock import install_streamlit_mock, StopRerun
from tests.test_ui_pages import (
    PATCHES_CORE,
    PATCHES_API,
    PATCHES_SKILLS,
    PATCHES_MISC,
    PATCHES_INSTRUCTIONS,
    _apply_all,
)


@pytest.fixture

def st_env():
    """Streamlit mock + core mocks, mirroring test_ui_pages.mock_env."""
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as st_mock:
        st_mock.session_state.clear()
        st_mock.session_state.update({"ui_lang": "English",
                                      "current_page": "run"})
        with _apply_all([PATCHES_CORE, PATCHES_API, PATCHES_SKILLS,
                         PATCHES_MISC, PATCHES_INSTRUCTIONS]):
            yield st_mock


def _render_main(st_mock, report):
    """Render ui.app.main() with check_dependencies() returning *report*.

    ``t`` is replaced by a plain formatter so the dependency warning payloads
    are readable strings instead of MagicMocks.
    """
    from ui.app import main
    st_mock.calls.clear()
    st_mock.warnings.clear()
    fmt = lambda key, lang=None, **kw: f"{key}|{kw.get('libs', '')}"  # noqa: E731
    with patch("ui.app.check_dependencies", return_value=report), \
         patch("ui.app.t", side_effect=fmt):
        try:
            main()
        except StopRerun:
            pass


def _deps_warnings(st_mock):
    """Only the dependency warnings (keys deps_missing/deps_outdated)."""
    return [w for w in st_mock.warnings
            if isinstance(w, str) and w.startswith("deps_")]


def test_no_warning_when_all_up_to_date(st_env):
    _render_main(st_env, {"ok": True, "missing": [], "outdated": []})
    assert _deps_warnings(st_env) == []


def test_warning_lists_missing_packages(st_env):
    report = {"ok": False,
              "missing": [{"package": "reportlab", "required": False},
                          {"package": "paramiko", "required": False}],
              "outdated": []}
    _render_main(st_env, report)
    deps = _deps_warnings(st_env)
    assert len(deps) == 1
    assert deps[0].startswith("deps_missing|")
    assert "reportlab" in deps[0] and "paramiko" in deps[0]


def test_warning_lists_outdated_packages(st_env):
    report = {"ok": False, "missing": [],
              "outdated": [{"package": "streamlit", "installed": "1.60.0",
                            "required": "1.66.0"}]}
    _render_main(st_env, report)
    deps = _deps_warnings(st_env)
    assert len(deps) == 1
    assert deps[0].startswith("deps_outdated|")
    assert "streamlit 1.60.0 -> 1.66.0" in deps[0]


def test_both_missing_and_outdated_warn_separately(st_env):
    report = {"ok": False,
              "missing": [{"package": "openpyxl", "required": False}],
              "outdated": [{"package": "sqlalchemy", "installed": "1.4",
                            "required": "2.0"}]}
    _render_main(st_env, report)
    deps = _deps_warnings(st_env)
    assert len(deps) == 2
    joined = "\n".join(deps)
    assert "deps_missing|" in joined and "openpyxl" in joined
    assert "deps_outdated|" in joined and "sqlalchemy" in joined


def test_check_runs_only_once_per_session(st_env):
    """The audit is skipped on reruns within the same session."""
    calls = []

    def _fake():
        calls.append(1)
        return {"ok": True, "missing": [], "outdated": []}

    from ui.app import main
    with patch("ui.app.check_dependencies", side_effect=_fake):
        try:
            main()
        except StopRerun:
            pass
        try:
            main()
        except StopRerun:
            pass
    assert len(calls) == 1


def test_probe_error_never_blocks_start(st_env):
    """A raising check must be swallowed (no dependency warning, no crash)."""
    from ui.app import main
    with patch("ui.app.check_dependencies", side_effect=RuntimeError("boom")):
        try:
            main()
        except StopRerun:
            pass
    assert _deps_warnings(st_env) == []
