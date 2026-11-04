# -*- coding: utf-8 -*-
"""
tests.test_schedules_daemon - the tick loop that fires schedules.

These tests pin the behaviour a scheduler is judged by: a due point runs
exactly once, a backlog after downtime does not become a burst, an overlap is
recorded instead of silently dropped, and the loop survives a restart.
The tick is called directly, so no test waits for wall-clock time.
"""
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from core.schedules import daemon, store  # noqa: E402
from core.schedules.model import Schedule  # noqa: E402


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Give the test its own database, history folder and run store."""
    import storage.db as db
    import core.paths as paths
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
    for module in ("core.threads_devagent",):
        mod = sys.modules.get(module)
        if mod is not None:
            monkeypatch.setattr(mod, "HISTORY_DIR", str(history), raising=False)
    monkeypatch.setattr(run_store, "default_store_root",
                        lambda: str(tmp_path / "orchestration"))
    daemon.stop_scheduler(join_seconds=1.0)
    yield tmp_path
    daemon.stop_scheduler(join_seconds=1.0)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(db, "_devagent_engine", None)
    monkeypatch.setattr(db, "_DevAgentSessionLocal", None)


def _rule(**overrides):
    base = Schedule(
        id="sch0001",
        title="Nightly digest",
        agent_slug="dev_agent",
        task_text="Summarize yesterday's commits",
        first_run_at=1772000000,
        next_run_at=1772000000,
        interval_value=1,
        interval_unit="days",
        misfire_grace_seconds=60,
    )
    return base.clone(**overrides)


class TestDueTick:
    def test_a_due_point_starts_exactly_one_run(self, isolated_state, monkeypatch):
        executed = []

        def _fake_execute(schedule, planned, run_row_id):
            executed.append((schedule.id, planned, run_row_id))
            store.finish_run(run_row_id, status="done")

        monkeypatch.setattr(daemon.SchedulerDaemon, "_execute", staticmethod(_fake_execute))
        store.create_schedule(_rule(next_run_at=1772000000))
        loop = daemon.SchedulerDaemon()
        fired = loop.tick_once(now=1772000010)

        assert len(fired) == 1
        assert fired[0]["schedule_id"] == "sch0001"
        # The worker runs on its own thread; wait for it to finish.
        for _ in range(100):
            if executed:
                break
            import time as _time
            _time.sleep(0.01)
        assert len(executed) == 1
        # A second tick must not start the same point again.
        fired_again = loop.tick_once(now=1772000015)
        assert fired_again == []

    def test_a_future_point_is_not_started(self, isolated_state):
        store.create_schedule(_rule(next_run_at=1772000000))
        loop = daemon.SchedulerDaemon()
        assert loop.tick_once(now=1771999000) == []

    def test_a_paused_rule_is_never_picked(self, isolated_state):
        store.create_schedule(_rule(next_run_at=1772000000, status="paused"))
        loop = daemon.SchedulerDaemon()
        assert loop.tick_once(now=1772000010) == []

    def test_the_heartbeat_is_written(self, isolated_state):
        loop = daemon.SchedulerDaemon()
        loop.tick_once(now=1772000010)
        assert daemon.read_heartbeat() == 1772000010

    def test_a_tick_on_an_empty_base_is_harmless(self, isolated_state):
        assert daemon.SchedulerDaemon().tick_once(now=1772000010) == []


class TestMisfire:
    def test_a_long_backlog_is_recorded_as_missed(self, isolated_state):
        store.create_schedule(_rule(next_run_at=1772000000, misfire_grace_seconds=60,
                                    interval_unit="hours"))
        loop = daemon.SchedulerDaemon()
        # Two days of downtime at an hourly cadence must not produce 48 runs.
        fired = loop.tick_once(now=1772000000 + 2 * 86400)
        assert fired == []
        runs = store.list_runs("sch0001")
        # The point is recorded as skipped with the reason that explains it:
        # "nothing ran" without a reason is what makes a scheduler look broken.
        assert [item["status"] for item in runs] == ["skipped"]
        assert runs[0]["reason"] == "missed"

    def test_catch_up_one_run_is_allowed_inside_the_grace(self, isolated_state, monkeypatch):
        started = []
        monkeypatch.setattr(daemon.SchedulerDaemon, "_start",
                            lambda self, schedule, planned, now: started.append(planned) or {"ok": True})
        store.create_schedule(_rule(next_run_at=1772000000, misfire_policy="catch_up_one",
                                    misfire_grace_seconds=600))
        daemon.SchedulerDaemon().tick_once(now=1772000300)
        assert started == [1772000000]

    def test_catch_up_is_refused_beyond_the_grace(self, isolated_state, monkeypatch):
        started = []
        monkeypatch.setattr(daemon.SchedulerDaemon, "_start",
                            lambda self, schedule, planned, now: started.append(planned) or {"ok": True})
        store.create_schedule(_rule(next_run_at=1772000000, misfire_policy="catch_up_one",
                                    misfire_grace_seconds=60, interval_unit="hours"))
        daemon.SchedulerDaemon().tick_once(now=1772000000 + 5 * 3600)
        assert started == []


class TestOverlap:
    def test_a_running_point_blocks_the_next_one(self, isolated_state):
        store.create_schedule(_rule(next_run_at=1772000000))
        first = store.claim_due("sch0001", 1772000000, next_run_at=1772086400,
                                now=1772000000)
        assert first is not None
        # The rule is back to active (as the runner would set it) but the
        # first run is still in flight.
        store.set_next_run("sch0001", 1772086400, status="active")
        with store._engine().begin() as conn:
            from sqlalchemy import text
            conn.execute(text("UPDATE schedules SET next_run_at = 1772086400 "
                              "WHERE id = 'sch0001'"))
        loop = daemon.SchedulerDaemon()
        loop.tick_once(now=1772086410)
        runs = store.list_runs("sch0001")
        statuses = [item["status"] for item in runs]
        assert "skipped_overlap" in statuses

    def test_queue_policy_keeps_the_point_for_the_next_tick(self, isolated_state, monkeypatch):
        store.create_schedule(_rule(next_run_at=1772000000, overlap_policy="queue_one"))
        store.claim_due("sch0001", 1772000000, next_run_at=1772086400, now=1772000000)
        store.set_next_run("sch0001", 1772086400, status="active")
        fired = daemon.SchedulerDaemon().tick_once(now=1772086410)
        assert fired == []
        # The grid must not have moved: the point is retried later.
        assert store.get_schedule("sch0001").next_run_at == 1772086400


class TestDailyLimit:
    def test_the_daily_cap_skips_the_point(self, isolated_state):
        from datetime import datetime
        from core.schedules.calc import resolve_zone

        zone = resolve_zone("Europe/Moscow")
        noon = int(datetime(2026, 2, 25, 12, 0, tzinfo=zone).timestamp())
        store.create_schedule(_rule(next_run_at=noon, max_runs_per_day=1, tz="Europe/Moscow"))
        store.claim_due("sch0001", noon, next_run_at=noon + 3600, now=noon)
        store.set_next_run("sch0001", noon + 3600, status="active")
        fired = daemon.SchedulerDaemon().tick_once(now=noon + 3600)
        assert fired == []
        runs = store.list_runs("sch0001")
        assert any(item["reason"] == "daily_limit" for item in runs)


class TestReconcile:
    def test_a_stuck_history_row_becomes_interrupted(self, isolated_state):
        store.create_schedule(_rule(next_run_at=1772000000))
        row = store.claim_due("sch0001", 1772000000, next_run_at=1772086400,
                              now=1772000000)
        result = daemon.SchedulerDaemon().reconcile(now=1772000000 + 7200)
        assert result["released"] >= 1
        assert result["interrupted"] >= 1
        assert store.get_run_row(row["id"])["status"] == "interrupted"
        # The rule is free to fire again.
        assert store.get_schedule("sch0001").status == "active"

    def test_reconcile_on_a_clean_base_changes_nothing(self, isolated_state):
        result = daemon.SchedulerDaemon().reconcile(now=1772000000)
        assert result["interrupted"] == 0


class TestSingleInstance:
    def test_two_starts_give_one_loop(self, isolated_state):
        first = daemon.start_scheduler(tick_seconds=3600)
        second = daemon.start_scheduler(tick_seconds=3600)
        assert first is second
        assert daemon.get_scheduler() is first

    def test_stop_releases_the_singleton(self, isolated_state):
        daemon.start_scheduler(tick_seconds=3600)
        daemon.stop_scheduler(join_seconds=1.0)
        assert daemon.get_scheduler() is None

    def test_is_alive_reports_a_fresh_heartbeat(self, isolated_state):
        assert daemon.is_alive() is False
        daemon.SchedulerDaemon().tick_once(now=int(__import__("time").time()))
        assert daemon.is_alive() is True


class TestBootstrap:
    """The platform start-up hook must never be the reason the UI stays shut."""

    def test_bootstrap_starts_the_loop(self, isolated_state):
        loop = daemon.bootstrap()
        assert loop is not None
        assert daemon.get_scheduler() is loop

    def test_bootstrap_is_idempotent(self, isolated_state):
        first = daemon.bootstrap()
        second = daemon.bootstrap()
        assert first is second

    def test_bootstrap_swallows_a_broken_start(self, isolated_state, monkeypatch):
        def _explode(tick_seconds=daemon.TICK_SECONDS):
            raise RuntimeError("database is not ready")

        monkeypatch.setattr(daemon, "start_scheduler", _explode)
        assert daemon.bootstrap() is None
        assert daemon.get_scheduler() is None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
