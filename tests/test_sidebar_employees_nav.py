# -*- coding: utf-8 -*-
"""tests/test_sidebar_employees_nav.py - sidebar layout tests for employees.

The "Employees" block of the sidebar mirrors the "Assistants" block:
the first five entries are always visible, the rest live in a collapsed
"All (N)" expander, and the search field appears only when MORE than five
entries exist. The assistant search field now follows the same rule.

These tests execute ui.app.main() under the Streamlit mock against a real
isolated database, so the full path (DB -> core.orchestrator_nav -> render)
is exercised, not just the sort helpers.
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def isolated_data(monkeypatch, isolated_app_modules, tmp_path):
    """Fresh DATA_DIR + fresh app modules (matching the smoke-test pattern)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    yield data_dir


def _fresh_ui():
    """Import ui.app fresh under the active streamlit mock."""
    for m in list(sys.modules):
        if m.startswith(("core", "storage", "ui")):
            sys.modules.pop(m, None)
    return importlib.import_module("ui.app")


def _ensure_seeds():
    """Seed the built-in employees/instructions before counting them."""
    from core.bootstrap import ensure_devagent_settings
    from core.default_imports import ensure_all_defaults
    ensure_all_defaults()
    ensure_devagent_settings()


def _render(st, ui_app):
    """Render a fresh main() pass and assert it produced no errors."""
    st.calls.clear()
    st.reset_clicks()
    try:
        ui_app.main()
    except StopRerun:
        pass
    assert st.errors == [], f"render emitted errors: {st.errors}"


def _make_employees(count, start=0):
    """Create *count* employees and return their slugs."""
    from core.orchestrators import create_orchestrator
    slugs = []
    for i in range(start, start + count):
        slug = f"test_emp_{i}"
        oid = create_orchestrator(slug=slug, name=f"Test Employee {i}",
                                  prompt_text="prompt")
        assert oid, f"create_orchestrator failed for {slug}"
        slugs.append(slug)
    return slugs


def _make_assistants(count, start=0):
    """Create *count* assistants and return their ids."""
    import core.assistants as assistants
    ids = []
    for i in range(start, start + count):
        aid = assistants.create_assistant(
            name=f"Test Assistant {i}", service="DeepSeek",
            model="deepseek-chat", temperature=0.7,
            text="prompt", description="",
        )
        assert aid, f"create_assistant failed for index {i}"
        ids.append(aid)
    return ids


def _text_input_keys(st):
    return [kw.get("key") for name, _a, kw in st.calls
            if name == "text_input" and kw.get("key")]


def _button_keys(st):
    return [kw.get("key") for name, _a, kw in st.calls
            if name == "button" and kw.get("key")]


def _expander_labels(st):
    return [str(a[0]) for name, a, _kw in st.calls
            if name == "expander" and a]


# ─── Employees ────────────────────────────────────────────────────────────

def test_employee_search_hidden_with_five_or_fewer(isolated_data):
    """With exactly five employees there is no search field and no
    collapsed "All" expander - all five buttons are rendered directly."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        _ensure_seeds()
        st.session_state.update(dict(ui_lang="English"))
        base = len(ui_app.list_orchestrators())
        if base > 5:
            pytest.skip("more than five built-in employees")
        _make_employees(max(0, 5 - base))
        _render(st, ui_app)

        assert not [k for k in _text_input_keys(st)
                    if k.startswith("orch_search_input")]
        assert len([k for k in _button_keys(st)
                    if k.startswith("nav_orchestrator:")]) == 5
        assert not [label for label in _expander_labels(st)
                    if label.startswith("All (")]


def test_employee_search_shown_above_five(isolated_data):
    """With six employees the search field appears and the sixth one goes
    into the collapsed "All (1)" expander."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        _ensure_seeds()
        st.session_state.update(dict(ui_lang="English"))
        base = len(ui_app.list_orchestrators())
        if base > 5:
            pytest.skip("more than five built-in employees")
        _make_employees(max(0, 6 - base))
        _render(st, ui_app)

        assert "orch_search_input_0" in _text_input_keys(st)
        assert len([k for k in _button_keys(st)
                    if k.startswith("nav_orchestrator:")]) == 6
        assert "All (1)" in _expander_labels(st)


# ─── Assistants (mirror rule) ─────────────────────────────────────────────

def test_assistant_search_hidden_with_five_or_fewer(isolated_data):
    """The assistant search field is hidden while there are <= 5 assistants."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        _ensure_seeds()
        st.session_state.update(dict(ui_lang="English"))
        base = len(ui_app.load_assistants_index())
        if base > 5:
            pytest.skip("more than five built-in assistants")
        _make_assistants(max(0, 5 - base))
        _render(st, ui_app)

        assert "assistant_search_input_0" not in _text_input_keys(st)
        assert len([k for k in _button_keys(st)
                    if k.startswith("nav_assistant_")]) == 5


def test_assistant_search_shown_above_five(isolated_data):
    """The assistant search field returns as soon as a sixth assistant exists."""
    with install_streamlit_mock() as st:
        ui_app = _fresh_ui()
        _ensure_seeds()
        st.session_state.update(dict(ui_lang="English"))
        base = len(ui_app.load_assistants_index())
        if base > 5:
            pytest.skip("more than five built-in assistants")
        _make_assistants(max(0, 6 - base))
        _render(st, ui_app)

        assert "assistant_search_input_0" in _text_input_keys(st)
        assert len([k for k in _button_keys(st)
                    if k.startswith("nav_assistant_")]) == 6
        assert "All (1)" in _expander_labels(st)
