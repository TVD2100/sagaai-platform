# -*- coding: utf-8 -*-
"""
tests.test_schedules_store - persistence and the single-fire invariant.

The scheduler's promise is that one planned point runs at most once, even
after a restart or when two processes race. That promise is a database
constraint here, so the tests drive the store directly against a temporary
database instead of mocking it away.
"""
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from core.schedules import store  # noqa: E402
from core.schedules.model import Schedule, STATUS_ACTIVE  # noqa: E402


@pytest.fixture

def isolated_db(tmp_path, monkeypatch):
    """Point the main database at a fresh file for the duration of a test."""
    import storage.db as db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr("core.paths.DB_PATH", str(tmp_path / "test.db"))
    yield db
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)


def _rule(**overrides):
    """Return a valid daily schedule (10:00 local) for the store tests."""
    from datetime import datetime
    from core.schedules.calc import resolve_zone

    anchor = int(datetime(2026, 3, 2, 10, 0, tzinfo=resolve_zone("Europe/Moscow")).timestamp())
    base = Schedule(
        id="sch0001",
        title="Nightly digest",
        agent_slug="dev_agent",
        task_text="Summarize the project",
        first_run_at=anchor,
        next_run_at=anchor,
        interval_value=1,
        interval_unit="days",
        tz="Europe/Moscow",
    )
    return base.clone(**overrides)


class TestMigration:
    def test_tables_and_indexes_are_created(self, isolated_db):
        engine = isolated_db.get_engine()
        from sqlalchemy import inspect

        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        assert "schedules" in names
        assert "schedule_runs" in names
        index_names = {item["name"] for item in inspector.get_indexes("schedule_runs")}
        assert "ux_schedule_runs_point" in index_names

    def test_the_migration_is_idempotent(self, isolated_db):
        engine = isolated_db.get_engine()
        isolated_db._ensure_schedules_tables(engine)
        isolated_db._ensure_schedules_tables(engine)
        from sqlalchemy import inspect
        assert "schedules" in set(inspect(engine).get_table_names())


class TestCrud:
    def test_create_and_read_back(self, isolated_db):
        store.create_schedule(_rule())
        loaded = store.get_schedule("sch0001")
        assert loaded is not None
        assert loaded.title == "Nightly digest"
        assert loaded.interval_unit == "days"
        assert loaded.created_at > 0

    def test_blocked_weekdays_and_ranges_survive_a_round_trip(self, isolated_db):
        from core.schedules.model import BlockedRange

        store.create_schedule(_rule(
            blocked_weekdays=(6, 7),
            blocked_ranges=(BlockedRange("2026-08-01", "2026-08-10", "trip"),),
            quiet_hours="22:00-07:00",
        ))
        loaded = store.get_schedule("sch0001")
        assert loaded.blocked_weekdays == (6, 7)
        assert len(loaded.blocked_ranges) == 1
        assert loaded.blocked_ranges[0].note == "trip"
        assert loaded.quiet_hours == "22:00-07:00"

    def test_update_keeps_the_creation_stamp(self, isolated_db):
        created = store.create_schedule(_rule())
        updated = store.update_schedule(created.clone(title="Renamed"))
        assert updated.created_at == created.created_at
        assert store.get_schedule("sch0001").title == "Renamed"

    def test_list_filters_by_status(self, isolated_db):
        store.create_schedule(_rule(id="a1"))
        store.create_schedule(_rule(id="a2", status="paused"))
        assert [item.id for item in store.list_schedules(STATUS_ACTIVE)] == ["a1"]
        assert len(store.list_schedules()) == 2

    def test_delete_removes_the_point_log_but_keeps_the_rule_identity(self, isolated_db):
        # The per-point log belongs to the rule and goes with it. The RUN
        # ITSELF survives elsewhere: every run owns an ordinary dialog thread,
        # and deleting a rule must never erase what an agent actually did.
        store.create_schedule(_rule())
        store.claim_due("sch0001", 1000, next_run_at=2000, now=1000)
        assert store.delete_schedule("sch0001") is True
        assert store.get_schedule("sch0001") is None
        assert store.list_runs("sch0001") == []


class TestClaim:
    def test_the_first_claim_wins(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000))
        row = store.claim_due("sch0001", 1000, next_run_at=2000, now=1000)
        assert row is not None
        assert row["status"] == "started"
        assert row["planned_at"] == 1000

    def test_the_second_claim_of_the_same_point_is_refused(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000))
        assert store.claim_due("sch0001", 1000, next_run_at=2000, now=1000)
        # A second tick that still sees the same point must not run it again.
        assert store.claim_due("sch0001", 1000, next_run_at=3000, now=1001) is None
        assert len(store.list_runs("sch0001")) == 1

    def test_a_stale_point_is_refused(self, isolated_db):
        store.create_schedule(_rule(next_run_at=2000))
        assert store.claim_due("sch0001", 1000, next_run_at=3000, now=1000) is None

    def test_the_claim_moves_the_grid_and_marks_running(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000))
        store.claim_due("sch0001", 1000, next_run_at=2000, now=1000)
        loaded = store.get_schedule("sch0001")
        assert loaded.next_run_at == 2000
        assert loaded.status == "running"
        assert loaded.last_run_at == 1000

    def test_a_paused_rule_cannot_be_claimed(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000, status="paused"))
        assert store.claim_due("sch0001", 1000, next_run_at=2000, now=1000) is None


class TestRunHistory:
    def test_finish_run_records_the_outcome(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000))
        row = store.claim_due("sch0001", 1000, next_run_at=2000, now=1000)
        store.finish_run(
            row["id"], status="done", run_id="run1", thread_id="thread1",
            steps_used=7, tokens={"in": 100, "out": 20, "cache": 5},
            summary="Report written",
        )
        stored = store.get_run_row(row["id"])
        assert stored["status"] == "done"
        assert stored["thread_id"] == "thread1"
        assert stored["tokens_in"] == 100
        assert stored["steps_used"] == 7

    def test_counts_group_by_status(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000))
        first = store.claim_due("sch0001", 1000, next_run_at=2000, now=1000)
        store.finish_run(first["id"], status="done")
        store.record_skipped("sch0001", 2000, reason="weekday", next_run_at=3000, now=2000)
        counts = store.count_runs("sch0001")
        assert counts.get("done") == 1
        assert counts.get("skipped") == 1

    def test_active_run_reports_the_unfinished_row(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000))
        row = store.claim_due("sch0001", 1000, next_run_at=2000, now=1000)
        assert store.active_run("sch0001")["id"] == row["id"]
        store.finish_run(row["id"], status="done")
        assert store.active_run("sch0001") is None

    def test_settle_schedule_writes_the_counters(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000))
        store.settle_schedule(
            "sch0001", status=STATUS_ACTIVE, last_status="done",
            run_count=3, fail_count=1, consecutive_failures=0,
        )
        loaded = store.get_schedule("sch0001")
        assert loaded.run_count == 3
        assert loaded.fail_count == 1
        assert loaded.last_status == "done"


class TestDueSelection:
    def test_only_due_active_rules_are_returned(self, isolated_db):
        store.create_schedule(_rule(id="due", next_run_at=1000))
        store.create_schedule(_rule(id="future", next_run_at=9999))
        store.create_schedule(_rule(id="paused", next_run_at=1000, status="paused"))
        due = store.list_due(now=2000)
        assert [item.id for item in due] == ["due"]

    def test_earliest_point_comes_first(self, isolated_db):
        store.create_schedule(_rule(id="late", next_run_at=1500))
        store.create_schedule(_rule(id="early", next_run_at=1200))
        assert [item.id for item in store.list_due(now=2000)] == ["early", "late"]

    def test_a_stale_lock_is_released(self, isolated_db):
        store.create_schedule(_rule(next_run_at=1000, status="running"))
        with isolated_db.get_engine().begin() as conn:
            from sqlalchemy import text
            conn.execute(text(
                "UPDATE schedules SET lock_expires_at = 10 WHERE id = 'sch0001'"))
        assert store.release_stale_locks(now=1000) == 1
        assert store.get_schedule("sch0001").status == STATUS_ACTIVE

    def test_runs_today_counts_only_today(self, isolated_db):
        from datetime import datetime
        from core.schedules.calc import resolve_zone

        zone = resolve_zone("Europe/Moscow")
        today = int(datetime(2026, 3, 2, 12, 0, tzinfo=zone).timestamp())
        store.create_schedule(_rule(next_run_at=today))
        store.claim_due("sch0001", today, next_run_at=today + 3600, now=today)
        # A point a week earlier must not be counted as today.
        store.record_skipped("sch0001", today - 7 * 86400, reason="weekday", now=today - 7 * 86400)
        assert store.runs_today("sch0001", "Europe/Moscow", now=today) == 1
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
