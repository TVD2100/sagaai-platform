# -*- coding: utf-8 -*-
"""tests/scenarios/test_schedules_scenario.py - user-level scenarios for
scheduled agent runs.

Each scenario walks the feature the way the owner meets it: a rule is created
through the "Run on a schedule" page, the scheduler loop ticks, and the result
is read back from the places the owner actually looks - the rule card, its run
history and the History page of dialogs. The language model is the only
stubbed part: it is an external service, and a canned answer keeps the run
offline and deterministic while the real loop, the real dialog and the real
history rows are exercised.

  Scenario 1 - happy path: a rule saved in the page fires once, the run ends
               as done, and the produced dialog is an ordinary entry in
               History with the owner's task inside.
  Scenario 2 - restart safety: a second tick at the same moment repeats
               nothing - one point fires at most once.
  Scenario 3 - platform was off: the backlog becomes a recorded miss, not a
               burst of catch-up runs, and no dialog is created.
  Scenario 4 - exclusion mask: a blocked weekday is skipped by the preview
               the owner sees before saving.
  Scenario 5 - repeated failure: the third failure in a row puts the rule on
               hold instead of burning runs forever.
"""
import importlib
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from tests._st_mock import StopRerun, install_streamlit_mock  # noqa: E402

#: Epoch of the anchor point used by every scenario: Monday 2026-03-02 10:00
#: in Europe/Moscow.
ANCHOR = 1772434800

#: A canned final answer of the model: the loop reads the marker and ends.
DONE_ANSWER = '{"loop_status": "done"} The report is ready.'


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh databases, history folder, scheduler loop and a Streamlit mock."""
    import core.paths as paths
    import storage.db as db
    from core.orchestration import runs as run_store
    from core.schedules import daemon, form, store

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
        try:
            yield {"mock": mock, "page": module, "store": store,
                   "daemon": daemon, "form": form, "root": tmp_path}
        finally:
            daemon.stop_scheduler(join_seconds=1.0)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(db, "_devagent_engine", None)
    monkeypatch.setattr(db, "_DevAgentSessionLocal", None)


@pytest.fixture
def offline_model(monkeypatch):
    """Answer every model call with a canned finished report.

    The scheduler's dialog would otherwise need a real provider. Everything
    between the tick and the stored answer stays real: the dispatcher, the
    loop, the transcript and the history row.
    """
    import dev_agent.agent_loop as loop

    def _answer(user_message, assistant=None, **kwargs):
        return DONE_ANSWER

    monkeypatch.setattr(loop, "send_request", _answer)
    return DONE_ANSWER


def _rule(store, form, **overrides):
    """Create one rule the way the form does: a local date plus a clock."""
    from core.schedules.model import Schedule

    anchor = form.epoch_from_local(date(2026, 3, 2), "10:00", "Europe/Moscow")
    base = Schedule(
        id="sch0001", title="Nightly digest", agent_slug="dev_agent",
        task_text="Summarize yesterday's commits", first_run_at=anchor,
        next_run_at=anchor, interval_value=1, interval_unit="days",
        tz="Europe/Moscow", max_steps=5,
    )
    return store.create_schedule(base.clone(**overrides))


def _wait_for_runs(loop, timeout=20.0):
    """Wait until the loop has no worker left.

    The scenarios drive a daemon instance directly (they do not go through
    ``start_scheduler``), so the wait must look at THAT instance: the process
    singleton is empty here and would let every assertion run too early.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if loop.running_workers == 0:
            return
        time.sleep(0.02)
    raise AssertionError("a scheduled run did not finish in time")


def test_scenario_a_scheduled_run_becomes_a_dialog_in_history(env, offline_model):
    """Scenario 1: the owner's rule fires once and lands in History.

    Given a rule saved with an employee, a task and a daily step,
    when  the scheduler ticks at the due moment,
    then  exactly one run is recorded as done and History shows the produced
          dialog with the owner's task as its first message.
    """
    from core.threads_devagent import list_devagent_threads, load_thread_messages

    store, daemon, form = env["store"], env["daemon"], env["form"]
    _rule(store, form)
    assert list_devagent_threads() == []

    loop = daemon.SchedulerDaemon()
    loop.tick_once(now=ANCHOR + 5)
    _wait_for_runs(loop)

    runs = store.list_runs("sch0001")
    assert len(runs) == 1
    assert runs[0]["status"] == "done"
    assert runs[0]["thread_id"], "a run must point at its dialog"

    threads = list_devagent_threads()
    assert len(threads) == 1
    thread = threads[0]
    assert thread["thread_id"] == runs[0]["thread_id"]
    assert not thread.get("parent_thread_id"), "a scheduled dialog has no parent"
    assert "Nightly digest" in str(thread.get("title") or "")

    messages = load_thread_messages(thread["thread_id"])
    assert messages
    # The dialog opens with the platform framing that explains the autonomous
    # run; the owner's own task follows it verbatim.
    user_messages = [item for item in messages if item["role"] == "user"]
    assert user_messages
    assert user_messages[0]["content"].startswith("Summarize yesterday's commits")

    rule = store.get_schedule("sch0001")
    assert rule.run_count == 1
    assert rule.next_run_at > ANCHOR, "the grid moved to the next point"


def test_scenario_a_restart_does_not_repeat_the_point(env, offline_model):
    """Scenario 2: a second tick at the same moment fires nothing.

    Given one rule whose point was already claimed,
    when  another tick looks at the same moment (a restart, a slow loop),
    then  the history keeps exactly one run for that point.
    """
    store, daemon, form = env["store"], env["daemon"], env["form"]
    _rule(store, form)

    loop = daemon.SchedulerDaemon()
    loop.tick_once(now=ANCHOR + 5)
    _wait_for_runs(loop)
    loop.tick_once(now=ANCHOR + 6)
    _wait_for_runs(loop)

    runs = store.list_runs("sch0001")
    assert [row["planned_at"] for row in runs] == [ANCHOR]
    assert store.count_runs("sch0001") == {"done": 1}


def test_scenario_a_backlog_is_recorded_as_a_miss(env, offline_model):
    """Scenario 3: downtime produces a miss, not a burst of runs.

    Given a rule whose point came due while the platform was off,
    when  the loop ticks two hours later,
    then  the point is recorded as missed, the grid moves forward and no
          dialog is created.
    """
    from core.threads_devagent import list_devagent_threads

    store, daemon, form = env["store"], env["daemon"], env["form"]
    _rule(store, form)

    fired = daemon.SchedulerDaemon().tick_once(now=ANCHOR + 7200)
    assert fired == []

    runs = store.list_runs("sch0001")
    assert len(runs) == 1
    # A point that never ran keeps the generic "skipped" status and carries
    # the reason the owner needs: it was missed while the platform was off.
    assert runs[0]["status"] == "skipped"
    assert runs[0]["reason"] == "missed"
    assert list_devagent_threads() == []
    assert store.get_schedule("sch0001").next_run_at > ANCHOR + 7200 - 1


def test_scenario_a_blocked_weekday_is_skipped_in_the_preview(env):
    """Scenario 4: the owner sees the exclusion before saving.

    Given a daily rule that must not run on weekends,
    when  the owner opens the preview of the next points,
    then  every shown moment falls on a working day.
    """
    store, form = env["store"], env["form"]
    from core.schedules.model import merge_weekday_presets

    rule = _rule(store, form, blocked_weekdays=merge_weekday_presets("weekdays_only"))
    points = form.next_points(rule, now_epoch=ANCHOR - 60, count=5)

    assert len(points) == 5
    for moment in points:
        weekday = datetime.fromtimestamp(moment, tz=timezone.utc).isoweekday()
        assert weekday <= 5, "a weekend moment slipped into the preview"


def test_scenario_three_failures_hold_the_rule(env, monkeypatch):
    """Scenario 5: repeated failures stop the rule instead of repeating.

    Given a rule that already failed twice,
    when  the next scheduled run hits the same broken provider,
    then  the run is recorded as failed, the failure streak reaches three and
          the rule goes on hold with the reason left on its card.
    """
    import dev_agent.agent_loop as loop_module

    store, daemon, form = env["store"], env["daemon"], env["form"]
    _rule(store, form, consecutive_failures=2, fail_count=2, run_count=2)

    def _boom(user_message, assistant=None, **kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(loop_module, "send_request", _boom)
    scheduler = daemon.SchedulerDaemon()
    scheduler.tick_once(now=ANCHOR + 5)
    _wait_for_runs(scheduler)

    runs = store.list_runs("sch0001")
    assert runs[0]["status"] == "failed"
    rule = store.get_schedule("sch0001")
    assert rule.consecutive_failures == 3
    assert rule.status == "error_hold"
    assert rule.last_error_code


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0