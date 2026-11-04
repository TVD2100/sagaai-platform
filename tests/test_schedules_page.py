# -*- coding: utf-8 -*-
"""
tests.test_schedules_page - the "Run on a schedule" section renders and acts.

The page is the only place where a rule can be created, paused or removed, so
these tests walk it the way an owner does: open the section, create a rule
from the form, pause it, run it once by hand. A Streamlit mock keeps the test
headless and records the widgets the page produces.
"""
import importlib
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh databases, a scheduler store and a Streamlit mock."""
    import core.paths as paths
    import storage.db as db
    from core.orchestration import runs as run_store

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(db, "_devagent_engine", None)
    monkeypatch.setattr(db, "_DevAgentSessionLocal", None)
    monkeypatch.setattr(paths, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(paths, "DEVAGENT_DB_PATH", str(tmp_path / "devagent.db"))
    history = tmp_path / "history"
    history.mkdir()
    monkeypatch.setattr(paths, "HISTORY_DIR", str(history))
    monkeypatch.setattr(run_store, "default_store_root",
                        lambda: str(tmp_path / "orchestration"))
    for name in list(sys.modules):
        if name.startswith("ui.pages.schedules"):
            sys.modules.pop(name, None)
    with install_streamlit_mock() as mock:
        mock.session_state["ui_lang"] = "en"
        module = importlib.import_module("ui.pages.schedules")
        yield mock, module
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(db, "_devagent_engine", None)
    monkeypatch.setattr(db, "_DevAgentSessionLocal", None)


def _render(mock, module):
    """Render the page, absorbing the rerun it may raise."""
    try:
        module.page_schedules()
    except StopRerun:
        pass


def _rule(**overrides):
    """Return a valid rule for the store, anchored on a Monday."""
    from core.schedules import form
    from core.schedules.model import Schedule

    anchor = form.epoch_from_local(date(2026, 3, 2), "10:00", "Europe/Moscow")
    base = Schedule(
        id="sch0001", title="Nightly digest", agent_slug="dev_agent",
        task_text="Summarize yesterday's commits", first_run_at=anchor,
        next_run_at=anchor, interval_value=1, interval_unit="days",
        tz="Europe/Moscow", max_steps=5,
    )
    return base.clone(**overrides)


class TestEmptyState:
    def test_the_section_explains_itself_when_nothing_exists(self, env):
        mock, module = env
        _render(mock, module)
        infos = [args[0] for name, args, _kw in mock.calls if name == "info"]
        assert any("No schedules yet" in text for text in infos)

    def test_the_new_button_opens_the_form(self, env):
        mock, module = env
        mock.click("sched_new_btn")
        _render(mock, module)
        assert mock.session_state.get("sched_form_open") is True

    def test_the_scheduler_health_is_reported(self, env):
        mock, module = env
        _render(mock, module)
        warnings = [args[0] for name, args, _kw in mock.calls if name == "warning"]
        assert any("scheduler is not running" in str(text) for text in warnings)


class TestList:
    def test_each_rule_gets_its_actions(self, env):
        mock, module = env
        module.store.create_schedule(_rule())
        _render(mock, module)
        keys = [call[2].get("key") for call in mock.calls if call[0] == "button"]
        assert "sched_edit_sch0001" in keys
        assert "sched_toggle_sch0001" in keys
        assert "sched_run_sch0001" in keys
        assert "sched_del_sch0001" in keys

    def test_the_card_shows_the_employee_and_the_next_run(self, env):
        mock, module = env
        module.store.create_schedule(_rule())
        _render(mock, module)
        markdowns = [args[0] for name, args, _kw in mock.calls if name == "markdown"]
        joined = " ".join(str(item) for item in markdowns)
        assert "Nightly digest" in joined
        assert "02.03.2026 10:00" in joined

    def test_a_paused_rule_offers_resume(self, env):
        mock, module = env
        module.store.create_schedule(_rule(status="paused"))
        _render(mock, module)
        labels = [call[1][0] for call in mock.calls
                  if call[0] == "button" and call[2].get("key") == "sched_toggle_sch0001"]
        assert labels and "Resume" in labels[0]


class TestActions:
    def test_pausing_keeps_the_rule_and_stops_it_firing(self, env):
        mock, module = env
        module.store.create_schedule(_rule())
        mock.click("sched_toggle_sch0001")
        _render(mock, module)
        assert module.store.get_schedule("sch0001").status == "paused"

    def test_resuming_from_a_hold_makes_it_active_again(self, env):
        mock, module = env
        module.store.create_schedule(_rule(status="error_hold", consecutive_failures=3))
        mock.click("sched_toggle_sch0001")
        _render(mock, module)
        rule = module.store.get_schedule("sch0001")
        assert rule.status == "active"
        assert rule.consecutive_failures == 0

    def test_delete_asks_first_and_only_then_removes(self, env):
        mock, module = env
        module.store.create_schedule(_rule())
        mock.click("sched_del_sch0001")
        _render(mock, module)
        # The rule is still there: the button asked for confirmation.
        assert module.store.get_schedule("sch0001") is not None
        assert mock.session_state.get("sched_confirm_sch0001") is True

        mock.reset_clicks()
        mock.click("sched_del_yes_sch0001")
        _render(mock, module)
        assert module.store.get_schedule("sch0001") is None

    def test_cancel_keeps_the_rule(self, env):
        mock, module = env
        module.store.create_schedule(_rule())
        mock.session_state["sched_confirm_sch0001"] = True
        mock.click("sched_del_no_sch0001")
        _render(mock, module)
        assert module.store.get_schedule("sch0001") is not None
        assert mock.session_state.get("sched_confirm_sch0001") is None

    def test_run_now_opens_a_history_row_with_the_manual_trigger(self, env, monkeypatch):
        mock, module = env
        module.store.create_schedule(_rule())
        started = {}

        def _fake_run_now(rule):
            started["id"] = rule.id
            return {"run_row_id": "row1", "planned_at": 1}

        monkeypatch.setattr(module.daemon, "run_now", _fake_run_now)
        mock.click("sched_run_sch0001")
        _render(mock, module)
        assert started["id"] == "sch0001"

    def test_run_now_is_refused_while_a_run_is_in_flight(self, env, monkeypatch):
        mock, module = env
        rule = _rule()
        module.store.create_schedule(rule)
        module.store.claim_due("sch0001", rule.next_run_at, next_run_at=rule.next_run_at + 86400,
                               now=rule.next_run_at)
        called = []
        monkeypatch.setattr(module.daemon, "run_now", lambda item: called.append(item.id))
        mock.click("sched_run_sch0001")
        _render(mock, module)
        assert called == []


class TestForm:
    def _open_new(self, mock, module):
        mock.session_state["sched_form_open"] = True
        _render(mock, module)

    def test_the_form_renders_every_required_field(self, env):
        mock, module = env
        self._open_new(mock, module)
        keys = {call[2].get("key") for call in mock.calls}
        assert "sched_f_title" in keys
        assert "sched_f_employee" in keys
        assert "sched_f_task" in keys
        assert "sched_f_tz" in keys
        assert "sched_f_first_day" in keys
        assert "sched_f_first_time" in keys
        assert "sched_f_interval" in keys

    def test_the_preview_shows_the_next_runs(self, env):
        mock, module = env
        self._open_new(mock, module)
        captions = [args[0] for name, args, _kw in mock.calls if name == "caption"]
        assert any("Next runs" in str(text) for text in captions)

    def test_saving_a_valid_draft_stores_it(self, env):
        mock, module = env
        tomorrow = date.today() + timedelta(days=1)
        mock.session_state["sched_form_open"] = True
        mock.click("Save")
        mock._text_returns["sched_f_title"] = "Nightly digest"
        mock._text_returns["sched_f_task"] = "Summarize yesterday's commits"
        mock._text_returns["sched_f_first_time"] = "10:00"
        mock._date_returns["sched_f_first_day"] = tomorrow
        _render(mock, module)
        rules = module.store.list_schedules()
        assert len(rules) == 1
        assert rules[0].title == "Nightly digest"
        assert rules[0].task_text == "Summarize yesterday's commits"
        assert mock.session_state.get("sched_form_open") is False

    def test_an_empty_task_is_refused_with_a_message(self, env):
        mock, module = env
        tomorrow = date.today() + timedelta(days=1)
        mock.session_state["sched_form_open"] = True
        mock.click("Save")
        mock._text_returns["sched_f_title"] = "Nightly digest"
        mock._text_returns["sched_f_task"] = "   "
        mock._text_returns["sched_f_first_time"] = "10:00"
        mock._date_returns["sched_f_first_day"] = tomorrow
        _render(mock, module)
        assert module.store.list_schedules() == []
        errors = [args[0] for name, args, _kw in mock.calls if name == "error"]
        assert any("task" in str(text).lower() for text in errors)

    def test_editing_loads_the_stored_values(self, env):
        mock, module = env
        module.store.create_schedule(_rule())
        mock.click("sched_edit_sch0001")
        # The click asks for a rerun; the form is painted on the next pass,
        # exactly as it is in the browser.
        _render(mock, module)
        assert mock.session_state.get("sched_edit_id") == "sch0001"
        _render(mock, module)
        values = [call[2].get("value") for call in mock.calls
                  if call[0] == "text_area" and call[2].get("key") == "sched_f_task"]
        assert values and values[0] == "Summarize yesterday's commits"

    def test_back_closes_the_form_without_saving(self, env):
        mock, module = env
        mock.session_state["sched_form_open"] = True
        mock.click("sched_back")
        _render(mock, module)
        assert mock.session_state.get("sched_form_open") is False
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
