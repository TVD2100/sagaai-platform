# -*- coding: utf-8 -*-
"""tests/scenarios/test_assistant_sidebar_scenarios.py - scenario tests for the
assistant sidebar ordering feature.

Each test walks user-level scenarios through the real building block used by
``ui.app`` (``_build_assistants_nav``), backed by real storage (SQLite via
SQLAlchemy and the core CRUD layer), so the scenarios validate the full
persistent path, not just isolated sort helpers:

  Scenario 1 - app restart: the assistant order is computed from persistent
               data (threads/creation timestamps), never from session-only
               recent lists, so a restart does not collapse everyone.
  Scenario 2 - fresh assistant: a newly created assistant (no dialogues yet)
               lands at the very top of the sidebar block.
  Scenario 3 - more than five: with 7 assistants the first 5 are visible and
               the other 2 stay in the collapsed list, ordered consistently.
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


def _set_column(model_cls, key, field, value):
    """Directly update a single column of an ORM row for test data setup."""
    from storage.db import get_session
    with get_session() as s:
        row = s.get(model_cls, key)
        assert row is not None, f"row not found: {model_cls.__name__}[{key}]"
        setattr(row, field, value)
        s.commit()


def _make_assistant(name, created_at):
    """Create an assistant and pin its created_at/updated_at timestamps."""
    import core.assistants as assistants
    from storage.models import Assistant
    aid = assistants.create_assistant(
        name=name, service="DeepSeek", model="deepseek-chat", temperature=0.7,
        text="prompt", description="",
    )
    assert aid
    _set_column(Assistant, aid, "created_at", created_at)
    _set_column(Assistant, aid, "updated_at", created_at)
    return aid


def _make_thread(assistant_id, assistant_name, updated_at):
    """Create a chat thread for an assistant and pin its updated_at."""
    import core.threads as threads
    from storage.models import Thread
    tid = threads.create_thread(assistant_id, assistant_name, title="dialog")
    _set_column(Thread, tid, "updated_at", updated_at)
    return tid


def _nav(app_mod):
    return app_mod._build_assistants_nav("English")


def _ids(entries):
    return [sid for sid, _name in entries]


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

def test_scenario_restart_keeps_five_most_active(isolated_data):
    """
    Given 5 assistants with dialogues at known times
    and 2 older ones,
    when the sidebar nav is built twice from scratch (simulating restarts
    with empty recent lists),
    then both builds return the same fixed block of 5 assistants,
    ordered by the newest dialogue / creation time.
    """
    a1 = _make_assistant("Active One", "2026-03-01T00:00:00")
    a2 = _make_assistant("Active Two", "2026-03-01T00:00:00")
    a3 = _make_assistant("Active Three", "2026-03-01T00:00:00")
    a4 = _make_assistant("Active Four", "2026-03-01T00:00:00")
    a5 = _make_assistant("Active Five", "2026-03-01T00:00:00")
    a6 = _make_assistant("Old Six", "2026-01-01T00:00:00")
    a7 = _make_assistant("Old Seven", "2026-01-01T00:00:00")

    _make_thread(a1, "Active One", "2026-04-05T00:00:00")
    _make_thread(a2, "Active Two", "2026-04-04T00:00:00")
    _make_thread(a3, "Active Three", "2026-04-03T00:00:00")
    _make_thread(a4, "Active Four", "2026-04-02T00:00:00")
    _make_thread(a5, "Active Five", "2026-04-01T00:00:00")
    _make_thread(a6, "Old Six", "2026-02-01T00:00:00")
    _make_thread(a7, "Old Seven", "2026-02-01T00:00:00")

    # "Restart" 1: fresh module import with empty session state.
    app_mod = _fresh_app_mod()
    visible1, collapsed1 = _nav(app_mod)

    # "Restart" 2: a completely fresh import again.
    app_mod = _fresh_app_mod()
    visible2, collapsed2 = _nav(app_mod)

    expected_visible = [a1, a2, a3, a4, a5]
    assert _ids(visible1) == expected_visible
    assert _ids(visible2) == expected_visible
    assert set(_ids(collapsed1)) == {a6, a7}
    assert set(_ids(collapsed2)) == {a6, a7}


# ─── Scenario 2: a freshly created assistant is on top ────────────────────

def test_scenario_fresh_assistant_appears_first(isolated_data):
    """
    Given several assistants with old dialogues,
    when a new assistant is created (no dialogues, creation time = now),
    then the sidebar block shows it first and the previously active
    assistants follow, in activity order.
    """
    a1 = _make_assistant("Veteran One", "2026-01-01T00:00:00")
    a2 = _make_assistant("Veteran Two", "2026-01-01T00:00:00")
    a3 = _make_assistant("Veteran Three", "2026-01-01T00:00:00")
    _make_thread(a1, "Veteran One", "2026-02-01T00:00:00")
    _make_thread(a2, "Veteran Two", "2026-02-02T00:00:00")
    _make_thread(a3, "Veteran Three", "2026-02-03T00:00:00")

    # The freshly created assistant gets created_at=now automatically.
    fresh = _make_assistant("Brand New", datetime.now().isoformat())

    app_mod = _fresh_app_mod()
    visible, collapsed = _nav(app_mod)

    assert _ids(visible) == [fresh, a3, a2, a1]
    assert collapsed == []


# ─── Scenario 3: more than five assistants ────────────────────────────────

def test_scenario_more_than_five_assistants(isolated_data):
    """
    Given 7 assistants with mixed activity
    (some with dialogues, some without),
    when the sidebar nav is built,
    then exactly 5 are visible in the global activity order and the rest
    stay in the collapsed list in the same order.
    """
    with_dial = []
    for i in range(1, 6):  # 5 assistants with dialogues
        aid = _make_assistant(f"Dialogue {i}", "2026-01-01T00:00:00")
        _make_thread(aid, f"Dialogue {i}", f"2026-04-{i:02d}T00:00:00")
        with_dial.append(aid)
    silent_new = _make_assistant("Silent New", "2026-02-01T00:00:00")
    silent_old = _make_assistant("Silent Old", "2026-01-15T00:00:00")

    app_mod = _fresh_app_mod()
    visible, collapsed = _nav(app_mod)

    # Dialogue order is newest-first: Dialogue 5..1, then silent by creation.
    expected = [with_dial[4], with_dial[3], with_dial[2], with_dial[1],
                with_dial[0], silent_new, silent_old]
    assert _ids(visible) == expected[:5]
    assert _ids(collapsed) == expected[5:]


# ─── Scenario 4: no dialogues at all ──────────────────────────────────────

def test_scenario_order_without_dialogues(isolated_data):
    """
    Given assistants with no dialogues,
    when the sidebar nav is built,
    then they are ordered by creation time (newest first).
    """
    a1 = _make_assistant("Created First", "2026-01-01T00:00:00")
    a2 = _make_assistant("Created Last", "2026-03-01T00:00:00")

    app_mod = _fresh_app_mod()
    visible, collapsed = _nav(app_mod)

    assert _ids(visible) == [a2, a1]
    assert collapsed == []
