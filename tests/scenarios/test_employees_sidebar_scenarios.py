# -*- coding: utf-8 -*-
"""tests/scenarios/test_employees_sidebar_scenarios.py - scenario tests for the
employee sidebar layout feature.

Each scenario walks the real UI building block used by ``ui.app``
(``_build_orch_nav``) backed by real storage (the main DB for employees and
``devagent.db`` for their dialogues), so the scenarios validate the full
persistent path, not just isolated sort helpers:

  Scenario 1 - app restart: the employee order is computed from persistent
               data (dialogues/creation timestamps), so a restart does not
               change the visible block.
  Scenario 2 - fresh employee: a newly created employee (no dialogues yet)
               lands at the very top of the sidebar block.
  Scenario 3 - dialogue activity drives the order (newest dialogue first).
  Scenario 4 - more than five: with 7 employees the first 5 are visible and
               the other 2 stay in the collapsed list, ordered consistently.
  Scenario 5 - five or fewer: everyone is visible, nothing is collapsed.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


@pytest.fixture()
def isolated_data(isolated_app_modules, monkeypatch, tmp_path):
    """Fresh DATA_DIR + fresh app modules, matching the smoke-test pattern."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    yield data_dir


def _set_orch_column(slug, field, value):
    """Directly update a single column of an employee row for test setup."""
    from storage.db import get_session
    from storage.models import Orchestrator
    with get_session() as s:
        row = s.query(Orchestrator).filter(Orchestrator.slug == slug).first()
        assert row is not None, f"employee not found: {slug}"
        setattr(row, field, value)
        s.commit()


def _set_thread_column(tid, field, value):
    """Directly update a single column of a devagent thread row."""
    from storage.db import get_devagent_session
    from storage.models import Thread
    with get_devagent_session() as s:
        row = s.get(Thread, tid)
        assert row is not None, f"thread not found: {tid}"
        setattr(row, field, value)
        s.commit()


def _make_employee(slug, name, created_at):
    """Create an employee and pin its created_at/updated_at timestamps."""
    from core.orchestrators import create_orchestrator
    oid = create_orchestrator(slug=slug, name=name, prompt_text="prompt")
    assert oid, f"create_orchestrator failed: {slug}"
    _set_orch_column(slug, "created_at", created_at)
    _set_orch_column(slug, "updated_at", created_at)
    return slug


def _make_dialogue(slug, name, updated_at):
    """Create a devagent dialogue for an employee and pin its timestamps."""
    from core.threads_devagent import create_devagent_thread
    tid = create_devagent_thread(title="dialog", orchestrator_slug=slug,
                                 orchestrator_name=name)
    _set_thread_column(tid, "created_at", updated_at)
    _set_thread_column(tid, "updated_at", updated_at)
    return tid


def _nav(app_mod):
    return app_mod._build_orch_nav()


def _slugs(entries):
    return [slug for _pid, _label, slug in entries]


def _fresh_app_mod():
    from tests._st_mock import install_streamlit_mock
    import importlib
    # Drop cached app modules so the fresh DATA_DIR is picked up.
    for m in list(sys.modules):
        if m.startswith(("core", "storage", "ui")):
            sys.modules.pop(m, None)
    with install_streamlit_mock():
        return importlib.import_module("ui.app")


# ─── Scenario 1: app restart keeps the fixed block ────────────────────────

def test_scenario_restart_keeps_visible_block(isolated_data):
    """
    Given 5 employees with dialogues at known times
    and 2 older ones,
    when the sidebar nav is built twice from scratch (simulating restarts),
    then both builds return the same block of 5 visible employees
    ordered by the newest dialogue time.
    """
    e1 = _make_employee("scen_a1", "Active One", "2026-03-01T00:00:00")
    e2 = _make_employee("scen_a2", "Active Two", "2026-03-01T00:00:00")
    e3 = _make_employee("scen_a3", "Active Three", "2026-03-01T00:00:00")
    e4 = _make_employee("scen_a4", "Active Four", "2026-03-01T00:00:00")
    e5 = _make_employee("scen_a5", "Active Five", "2026-03-01T00:00:00")
    e6 = _make_employee("scen_old6", "Old Six", "2026-01-01T00:00:00")
    e7 = _make_employee("scen_old7", "Old Seven", "2026-01-01T00:00:00")

    _make_dialogue(e1, "Active One", "2026-04-05T00:00:00")
    _make_dialogue(e2, "Active Two", "2026-04-04T00:00:00")
    _make_dialogue(e3, "Active Three", "2026-04-03T00:00:00")
    _make_dialogue(e4, "Active Four", "2026-04-02T00:00:00")
    _make_dialogue(e5, "Active Five", "2026-04-01T00:00:00")
    _make_dialogue(e6, "Old Six", "2026-02-01T00:00:00")
    _make_dialogue(e7, "Old Seven", "2026-02-01T00:00:00")

    app_mod = _fresh_app_mod()
    visible1, collapsed1 = _nav(app_mod)

    app_mod = _fresh_app_mod()
    visible2, collapsed2 = _nav(app_mod)

    expected_visible = [e1, e2, e3, e4, e5]
    assert _slugs(visible1) == expected_visible
    assert _slugs(visible2) == expected_visible
    assert set(_slugs(collapsed1)) == {e6, e7}
    assert set(_slugs(collapsed2)) == {e6, e7}


# ─── Scenario 2: a freshly created employee is on top ─────────────────────

def test_scenario_fresh_employee_appears_first(isolated_data):
    """
    Given several employees with old dialogues,
    when a new employee is created (no dialogues, creation time = now),
    then the sidebar block shows it first and the previously active
    employees follow, in activity order.
    """
    e1 = _make_employee("scen_v1", "Veteran One", "2026-01-01T00:00:00")
    e2 = _make_employee("scen_v2", "Veteran Two", "2026-01-01T00:00:00")
    e3 = _make_employee("scen_v3", "Veteran Three", "2026-01-01T00:00:00")
    _make_dialogue(e1, "Veteran One", "2026-02-01T00:00:00")
    _make_dialogue(e2, "Veteran Two", "2026-02-02T00:00:00")
    _make_dialogue(e3, "Veteran Three", "2026-02-03T00:00:00")

    fresh = _make_employee("scen_fresh", "Brand New",
                           datetime.now().isoformat())

    app_mod = _fresh_app_mod()
    visible, collapsed = _nav(app_mod)

    assert _slugs(visible) == [fresh, e3, e2, e1]
    assert collapsed == []


# ─── Scenario 3: dialogue activity drives the order ───────────────────────

def test_scenario_dialogue_activity_drives_order(isolated_data):
    """
    Given employees created at the same time but with dialogues at
    different times,
    when the sidebar nav is built,
    then they are ordered by the newest dialogue first.
    """
    e_jan = _make_employee("scen_jan", "January", "2025-01-01T00:00:00")
    e_mar = _make_employee("scen_mar", "March", "2025-01-01T00:00:00")
    e_feb = _make_employee("scen_feb", "February", "2025-01-01T00:00:00")
    _make_dialogue(e_jan, "January", "2026-01-01T00:00:00")
    _make_dialogue(e_mar, "March", "2026-03-01T00:00:00")
    _make_dialogue(e_feb, "February", "2026-02-01T00:00:00")

    app_mod = _fresh_app_mod()
    visible, collapsed = _nav(app_mod)

    assert _slugs(visible) == [e_mar, e_feb, e_jan]
    assert collapsed == []


# ─── Scenario 4: more than five employees ─────────────────────────────────

def test_scenario_more_than_five_employees(isolated_data):
    """
    Given 7 employees with mixed activity
    (some with dialogues, some without),
    when the sidebar nav is built,
    then exactly 5 are visible in the global activity order and the rest
    stay in the collapsed list in the same order.
    """
    with_dial = []
    for i in range(1, 6):  # 5 employees with dialogues
        slug = f"scen_d{i}"
        _make_employee(slug, f"Dialogue {i}", "2026-01-01T00:00:00")
        _make_dialogue(slug, f"Dialogue {i}", f"2026-04-{i:02d}T00:00:00")
        with_dial.append(slug)
    silent_new = _make_employee("scen_sil_new", "Silent New", "2026-02-01T00:00:00")
    silent_old = _make_employee("scen_sil_old", "Silent Old", "2026-01-15T00:00:00")

    app_mod = _fresh_app_mod()
    visible, collapsed = _nav(app_mod)

    # Dialogue order is newest-first: Dialogue 5..1, then silent by creation.
    expected = [with_dial[4], with_dial[3], with_dial[2], with_dial[1],
                with_dial[0], silent_new, silent_old]
    assert len(visible) == 5
    assert len(collapsed) == 2
    assert _slugs(visible) == expected[:5]
    assert _slugs(collapsed) == expected[5:]


# ─── Scenario 5: five or fewer employees ──────────────────────────────────

def test_scenario_five_or_fewer_no_collapsed_block(isolated_data):
    """
    Given three employees with no dialogues,
    when the sidebar nav is built,
    then all three are visible (ordered by creation time, newest first)
    and nothing is collapsed - the "All" expander must not appear.
    """
    e_old = _make_employee("scen_5old", "Created First", "2026-01-01T00:00:00")
    e_mid = _make_employee("scen_5mid", "Created Second", "2026-02-01T00:00:00")
    e_new = _make_employee("scen_5new", "Created Last", "2026-03-01T00:00:00")

    app_mod = _fresh_app_mod()
    visible, collapsed = _nav(app_mod)

    assert _slugs(visible) == [e_new, e_mid, e_old]
    assert collapsed == []
