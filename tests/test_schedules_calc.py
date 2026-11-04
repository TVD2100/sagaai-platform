# -*- coding: utf-8 -*-
"""
tests.test_schedules_calc - the pure scheduling grid.

The calculation is the part of the feature that cannot be fixed by looking
at the screen: a wrong next-run time either fires an agent at 3 a.m. or
silently never fires it. These tests therefore pin the arithmetic directly,
with an explicit ``now`` and no clock manipulation.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from core.schedules.calc import (  # noqa: E402
    compute_next_run,
    exclusion_reason,
    next_run_epoch,
    resolve_zone,
)
from core.schedules.model import BlockedRange, Schedule  # noqa: E402

MSK = resolve_zone("Europe/Moscow")


def _at(year=2026, month=3, day=2, hour=9, minute=0):
    """Return an aware MSK datetime for the given wall clock."""
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def _epoch(moment):
    return int(moment.timestamp())


def _schedule(**overrides):
    """Return a daily 09:00 schedule anchored on 2 March 2026."""
    base = Schedule(
        id="s1",
        title="Daily digest",
        agent_slug="dev_agent",
        task_text="Summarize the project",
        first_run_at=_epoch(_at()),
        interval_value=1,
        interval_unit="days",
        tz="Europe/Moscow",
    )
    return base.clone(**overrides)


class TestDailyGrid:
    def test_before_the_anchor_returns_the_anchor(self):
        schedule = _schedule()
        assert compute_next_run(schedule, _at(hour=8)) == _at(hour=9)

    def test_after_the_anchor_returns_the_next_day(self):
        schedule = _schedule()
        assert compute_next_run(schedule, _at(hour=10)) == _at(day=3, hour=9)

    def test_exactly_at_the_anchor_moves_forward(self):
        # "strictly after" is what makes a claim idempotent: the same point
        # can never be returned twice for the same now.
        schedule = _schedule()
        assert compute_next_run(schedule, _at(hour=9)) == _at(day=3, hour=9)

    def test_long_idle_period_still_returns_one_point(self):
        schedule = _schedule()
        later = _at(month=6, day=15, hour=7)
        assert compute_next_run(schedule, later) == _at(month=6, day=15, hour=9)


class TestIntervalUnits:
    def test_hourly(self):
        schedule = _schedule(interval_value=1, interval_unit="hours")
        assert compute_next_run(schedule, _at(hour=10, minute=5)) == _at(hour=11)

    def test_every_three_hours(self):
        schedule = _schedule(interval_value=3, interval_unit="hours")
        assert compute_next_run(schedule, _at(hour=10)) == _at(hour=12)

    def test_weekly_keeps_the_weekday_of_the_anchor(self):
        schedule = _schedule(interval_value=1, interval_unit="weeks")
        moment = compute_next_run(schedule, _at(day=3, hour=10))
        assert moment == _at(day=9, hour=9)
        assert moment.isoweekday() == _at().isoweekday()

    def test_minutes(self):
        schedule = _schedule(interval_value=30, interval_unit="minutes")
        assert compute_next_run(schedule, _at(hour=9, minute=1)) == _at(hour=9, minute=30)


class TestMonthlyClamp:
    def _monthly(self, day):
        anchor = _epoch(_at(month=1, day=day))
        return _schedule(first_run_at=anchor, interval_value=1, interval_unit="months")

    def test_the_31st_returns_to_the_31st_after_february(self):
        schedule = self._monthly(31)
        february = compute_next_run(schedule, _at(month=1, day=31, hour=10))
        assert february == _at(month=2, day=28, hour=9)
        march = compute_next_run(schedule, february)
        # The anchor - not the clamped February point - drives the day.
        assert march == _at(month=3, day=31, hour=9)

    def test_leap_day_anchor_returns_to_february_29(self):
        anchor = _epoch(_at(year=2024, month=2, day=29))
        schedule = _schedule(first_run_at=anchor, interval_value=12, interval_unit="months")
        next_year = compute_next_run(schedule, _at(year=2024, month=3, day=1))
        assert (next_year.year, next_year.month, next_year.day) == (2025, 2, 28)
        leap = compute_next_run(schedule, next_year)
        assert (leap.year, leap.month, leap.day) == (2026, 2, 28)


class TestExceptions:
    def test_blocked_weekend_is_skipped(self):
        # 2 March 2026 is a Monday; blocking Sat/Sun moves the Wednesday
        # point to Thursday only if Friday is blocked too - so block all
        # weekdays except Wednesday to make the expectation explicit.
        schedule = _schedule(blocked_weekdays=(1, 2, 4, 5, 6, 7))
        moment = compute_next_run(schedule, _at(day=2, hour=10))
        assert moment.isoweekday() == 3

    def test_vacation_is_skipped(self):
        schedule = _schedule(blocked_ranges=(BlockedRange("2026-03-03", "2026-03-05", "trip"),))
        assert compute_next_run(schedule, _at(day=2, hour=10)) == _at(day=6, hour=9)

    def test_quiet_hours_move_the_point_when_postponing(self):
        schedule = _schedule(quiet_hours="08:00-10:00", exception_policy="postpone")
        moment = compute_next_run(schedule, _at(day=2, hour=7))
        assert moment == _at(day=2, hour=10)

    def test_quiet_hours_drop_the_point_when_skipping(self):
        # Hourly rule anchored at 07:00 with a quiet window 08:00-10:00:
        # the 08:00 and 09:00 points are dropped, 10:00 is allowed.
        schedule = _schedule(
            first_run_at=_epoch(_at(hour=7)),
            interval_value=1,
            interval_unit="hours",
            quiet_hours="08:00-10:00",
            exception_policy="skip",
        )
        assert compute_next_run(schedule, _at(hour=7, minute=30)) == _at(hour=10)

    def test_a_daily_point_inside_quiet_hours_never_fires(self):
        # The only grid point of a daily rule sits inside the window, so the
        # rule is honestly unfulfillable instead of silently running anyway.
        schedule = _schedule(quiet_hours="08:00-10:00", exception_policy="skip")
        assert compute_next_run(schedule, _at(day=2, hour=7)) is None

    def test_postpone_respects_its_window(self):
        schedule = _schedule(
            blocked_ranges=(BlockedRange("2026-03-03", "2026-03-20", "long trip"),),
            exception_policy="postpone",
            max_shift_seconds=3600,
        )
        # The point falls inside a 17-day period; a one-hour window cannot
        # reach the end of it, so the point is dropped rather than moved.
        assert compute_next_run(schedule, _at(day=2, hour=10)) == _at(day=21, hour=9)

    def test_exclusion_reason_reports_the_blocking_rule(self):
        schedule = _schedule(blocked_weekdays=(7,))
        sunday = _at(day=8, hour=9)
        assert exclusion_reason(schedule, sunday) == "weekday"
        assert exclusion_reason(schedule, _at(day=9, hour=9)) == ""

    def test_unfulfillable_rule_returns_none(self):
        schedule = _schedule(blocked_weekdays=(1, 2, 3, 4, 5, 6, 7))
        assert compute_next_run(schedule, _at(day=2)) is None


class TestLifecycle:
    def test_end_date_stops_the_rule(self):
        schedule = _schedule(ends_at=_epoch(_at(day=4, hour=9)))
        assert compute_next_run(schedule, _at(day=3, hour=10)) == _at(day=4, hour=9)
        assert compute_next_run(schedule, _at(day=4, hour=9)) is None

    def test_a_point_that_ends_exactly_at_the_limit_is_allowed(self):
        schedule = _schedule(ends_at=_epoch(_at(day=4, hour=9)))
        assert compute_next_run(schedule, _at(day=3, hour=23)) == _at(day=4, hour=9)

    def test_last_run_acts_as_a_floor(self):
        schedule = _schedule(last_run_at=_epoch(_at(day=5, hour=9)))
        moment = compute_next_run(schedule, _at(day=2, hour=10))
        assert moment == _at(day=6, hour=9)

    def test_next_run_epoch_matches_the_datetime_form(self):
        schedule = _schedule()
        expected = _epoch(_at(day=3, hour=9))
        assert next_run_epoch(schedule, _epoch(_at(day=2, hour=10))) == expected


class TestDeterminism:
    def test_two_calls_with_the_same_now_agree(self):
        schedule = _schedule()
        now = _at(hour=10)
        assert compute_next_run(schedule, now) == compute_next_run(schedule, now)

    def test_the_function_does_not_touch_the_wall_clock(self, monkeypatch):
        # The scheduling engine must stay reproducible: any accidental
        # datetime.now() inside would make the tests above order-dependent.
        import core.schedules.calc as calc

        class _NoClock:
            """datetime stand-in that refuses every wall-clock read."""

            fromtimestamp = staticmethod(datetime.fromtimestamp)
            combine = staticmethod(datetime.combine)

            @staticmethod
            def now(*_args, **_kwargs):
                raise AssertionError("compute_next_run must not read the clock")

            @staticmethod
            def utcnow(*_args, **_kwargs):
                raise AssertionError("compute_next_run must not read the clock")

            @staticmethod
            def today(*_args, **_kwargs):
                raise AssertionError("compute_next_run must not read the clock")

        monkeypatch.setattr(calc, "datetime", _NoClock)
        schedule = _schedule()
        assert compute_next_run(schedule, _at(hour=10)).day == 3


class TestQuietHoursAcrossMidnight:
    def test_window_crossing_midnight_blocks_the_late_point(self):
        schedule = _schedule(first_run_at=_epoch(_at(hour=23)), quiet_hours="22:00-07:00")
        assert exclusion_reason(schedule, _at(hour=23)) == "quiet_hours"
        assert exclusion_reason(schedule, _at(hour=12)) == ""

    def test_postponing_across_midnight_lands_after_the_window(self):
        schedule = _schedule(
            first_run_at=_epoch(_at(hour=23)),
            quiet_hours="22:00-07:00",
            exception_policy="postpone",
        )
        moment = compute_next_run(schedule, _at(hour=20))
        assert moment == _at(day=3, hour=7)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
