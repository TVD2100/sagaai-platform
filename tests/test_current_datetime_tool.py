# -*- coding: utf-8 -*-
"""
Tests for the ``current_datetime`` tool.

The system prompt states the date as a fact; the tool answers what the fact
cannot - the time of day and the exact weekday of any moment. Both read the
same clock and the same name tables, so they can never disagree.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core import orchestrators as orch
from dev_agent.tool_executor import ToolExecutor

FIXED = datetime(2026, 11, 2, 9, 30, 15, tzinfo=timezone(timedelta(hours=3)))


@pytest.fixture()
def frozen(monkeypatch):
    """Freeze the tool's clock at FIXED so the assertions stay stable."""
    import dev_agent.tool_executor as te

    real_datetime = te.datetime if hasattr(te, "datetime") else datetime

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED

    monkeypatch.setattr(te, "datetime", _FrozenDatetime, raising=False)
    yield FIXED
    monkeypatch.setattr(te, "datetime", real_datetime, raising=False)


@pytest.fixture()
def executor():
    return ToolExecutor()


# ─── Shape of the answer ────────────────────────────────────────────────────


def test_tool_returns_ok_and_all_fields(executor):
    result = executor.dispatch("current_datetime", {})
    assert result["ok"] is True
    for field in ("iso", "date", "date_iso", "weekday", "weekday_ru", "time",
                  "timezone", "utc_offset", "epoch", "date_line"):
        assert field in result, "missing field: " + field


def test_tool_reports_a_day_first_date(executor):
    result = executor.dispatch("current_datetime", {})
    day, month, year = result["date"].split(".")
    assert len(day) == 2 and len(month) == 2 and len(year) == 4


def test_tool_reports_a_sortable_iso_date(executor):
    result = executor.dispatch("current_datetime", {})
    parsed = datetime.strptime(result["date_iso"], "%Y-%m-%d")
    assert parsed.strftime("%Y-%m-%d") == result["date_iso"]


def test_tool_reports_the_time_of_day(executor):
    result = executor.dispatch("current_datetime", {})
    datetime.strptime(result["time"], "%H:%M:%S")


def test_tool_reports_a_numeric_epoch(executor):
    result = executor.dispatch("current_datetime", {})
    assert isinstance(result["epoch"], int)
    assert result["epoch"] > 1_600_000_000


def test_tool_reports_the_timezone_offset(executor):
    result = executor.dispatch("current_datetime", {})
    assert result["utc_offset"].startswith("UTC")
    assert ":" in result["utc_offset"]


# ─── Agreement with the prompt block ────────────────────────────────────────


def test_weekday_name_matches_the_prompt_table(executor):
    result = executor.dispatch("current_datetime", {})
    today = datetime.now()
    assert result["weekday"] == orch._WEEKDAY_NAMES[today.weekday()]


def test_russian_weekday_matches_the_prompt_table(executor):
    result = executor.dispatch("current_datetime", {})
    today = datetime.now()
    assert result["weekday_ru"] == orch._WEEKDAY_NAMES_RU[today.weekday()]


def test_date_line_equals_the_prompt_line(executor):
    """The tool and the system-prompt block must state the same sentence."""
    result = executor.dispatch("current_datetime", {})
    today = datetime.now()
    assert result["date_line"] == orch._current_date_line(today)


def test_date_and_iso_date_describe_the_same_day(executor):
    result = executor.dispatch("current_datetime", {})
    assert result["date"] == datetime.strptime(result["date_iso"], "%Y-%m-%d").strftime("%d.%m.%Y")


# ─── Frozen clock ───────────────────────────────────────────────────────────


def test_frozen_clock_yields_the_expected_values(monkeypatch):
    import dev_agent.tool_executor as te

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED

    monkeypatch.setattr(te, "datetime", _FrozenDatetime, raising=False)
    result = ToolExecutor().dispatch("current_datetime", {})
    assert result["date"] == "02.11.2026"
    assert result["date_iso"] == "2026-11-02"
    assert result["weekday"] == "Monday"
    assert result["weekday_ru"] == "понедельник"
    assert result["time"] == "09:30:15"
    assert result["utc_offset"] == "UTC+03:00"


# ─── Robustness ─────────────────────────────────────────────────────────────


def test_unknown_argument_is_rejected(executor):
    result = executor.dispatch("current_datetime", {"when": "now"})
    assert result["ok"] is False
    assert "unknown_args" in result
    assert result["unknown_args"] == ["when"]


def test_tool_is_offered_to_the_model(executor):
    """A tool the model cannot see is a tool that does not exist."""
    from dev_agent.tool_executor import TOOL_CATALOG
    names = {t["name"] for t in TOOL_CATALOG}
    assert "current_datetime" in names
