# -*- coding: utf-8 -*-
"""
tests.test_schedules_form - the conversions between the form and the engine.

A schedule is only trustworthy if the moment the owner types is the moment
the engine fires. These tests pin that agreement: a wall clock read in the
rule's zone, a preview that walks the real engine, and presets that map onto
the exclusion mask without surprises.
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from core.schedules import form  # noqa: E402
from core.schedules.calc import resolve_zone  # noqa: E402
from core.schedules.model import Schedule  # noqa: E402


def _rule(**overrides):
    base = Schedule(
        id="sch0001",
        title="Nightly digest",
        agent_slug="dev_agent",
        task_text="Summarize yesterday's commits",
        first_run_at=form.epoch_from_local(date(2026, 3, 2), "10:00", "Europe/Moscow"),
        interval_value=1,
        interval_unit="days",
        tz="Europe/Moscow",
    )
    return base.clone(**overrides)


class TestLocalTime:
    @pytest.mark.parametrize("text,expected", [
        ("09:00", (9, 0)),
        ("23:59", (23, 59)),
        ("00:00", (0, 0)),
        (" 7:05 ", (7, 5)),
    ])
    def test_a_valid_wall_clock_is_parsed(self, text, expected):
        assert form.parse_local_time(text) == expected

    @pytest.mark.parametrize("text", ["", "9", "24:00", "12:60", "abc", "-1:00"])
    def test_a_malformed_wall_clock_is_refused(self, text):
        assert form.parse_local_time(text) is None

    def test_the_moment_is_read_in_the_rules_own_zone(self):
        moment = form.epoch_from_local(date(2026, 3, 2), "10:00", "Europe/Moscow")
        zone = resolve_zone("Europe/Moscow")
        local = datetime.fromtimestamp(moment, tz=zone)
        assert (local.hour, local.minute) == (10, 0)
        assert local.date() == date(2026, 3, 2)

    def test_the_same_wall_clock_in_another_zone_is_another_instant(self):
        moscow = form.epoch_from_local(date(2026, 3, 2), "10:00", "Europe/Moscow")
        tokyo = form.epoch_from_local(date(2026, 3, 2), "10:00", "Asia/Tokyo")
        assert moscow != tokyo
        assert tokyo < moscow

    def test_a_malformed_clock_has_no_moment(self):
        assert form.epoch_from_local(date(2026, 3, 2), "nope", "UTC") is None

    def test_the_form_round_trips_its_own_value(self):
        moment = form.epoch_from_local(date(2026, 3, 2), "07:45", "Europe/Moscow")
        day, clock = form.local_inputs(moment, "Europe/Moscow")
        assert (day, clock) == (date(2026, 3, 2), "07:45")

    def test_an_empty_schedule_shows_the_current_minute(self):
        day, clock = form.local_inputs(None, "UTC")
        assert isinstance(day, date)
        assert len(clock) == 5 and ":" in clock


class TestFormatting:
    def test_a_moment_is_printed_in_the_rules_zone(self):
        moment = form.epoch_from_local(date(2026, 3, 2), "10:00", "Europe/Moscow")
        assert form.format_moment(moment, "Europe/Moscow") == "02.03.2026 10:00"

    def test_a_missing_moment_is_a_dash(self):
        assert form.format_moment(None, "UTC") == "-"

    def test_the_interval_label_keeps_its_number(self):
        assert form.humanize_interval(2, "days") == "2 days"
        assert form.humanize_interval(0, "hours") == "1 hours"


class TestPreview:
    def test_the_preview_walks_the_real_grid(self):
        points = form.next_points(_rule(), now_epoch=form.epoch_from_local(
            date(2026, 3, 2), "00:00", "Europe/Moscow"), count=3)
        zone = resolve_zone("Europe/Moscow")
        clocks = [datetime.fromtimestamp(item, tz=zone).strftime("%d.%m %H:%M") for item in points]
        assert clocks == ["02.03 10:00", "03.03 10:00", "04.03 10:00"]

    def test_blocked_weekdays_are_absent_from_the_preview(self):
        # Blocked = Saturday and Sunday, so the preview must jump over them.
        rule = _rule(blocked_weekdays=(6, 7))
        points = form.next_points(rule, now_epoch=form.epoch_from_local(
            date(2026, 3, 6), "12:00", "Europe/Moscow"), count=3)
        zone = resolve_zone("Europe/Moscow")
        weekdays = [datetime.fromtimestamp(item, tz=zone).isoweekday() for item in points]
        assert weekdays == [1, 2, 3]

    def test_a_finished_rule_has_no_preview(self):
        rule = _rule(ends_at=form.epoch_from_local(date(2026, 3, 2), "10:00", "Europe/Moscow"))
        assert form.next_points(rule, now_epoch=form.epoch_from_local(
            date(2026, 6, 1), "00:00", "Europe/Moscow")) == []


class TestPresets:
    def test_every_day_blocks_nothing(self):
        assert form.weekday_preset_of(()) == "every_day"

    def test_weekdays_only_blocks_the_weekend(self):
        assert form.weekday_preset_of((6, 7)) == "weekdays_only"

    def test_weekends_only_blocks_the_working_week(self):
        assert form.weekday_preset_of((1, 2, 3, 4, 5)) == "weekends_only"

    def test_an_unusual_mask_is_custom(self):
        assert form.weekday_preset_of((3,)) == "custom"

    def test_the_quiet_window_is_normalized(self):
        assert form.quiet_hours_value("22:0-7:0") == "22:00-07:00"
        assert form.quiet_hours_value("nonsense") == ""
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
