# -*- coding: utf-8 -*-
"""
core.schedules.model - the Schedule rule and its validation.

The module is pure: it holds no clock, no database handle and no UI text.
Display strings are produced by the UI layer from the structured fields
below, so the same rule renders in any interface language.

Times are integer epoch seconds in UTC. A rule also carries its own IANA
time zone: the grid is built in the zone the owner picked, which keeps a
"09:00 every day" schedule at 09:00 local time whatever the machine does.

Weekday handling is EXCLUSION based: ``blocked_weekdays`` lists the ISO
days (1=Monday .. 7=Sunday) on which the schedule must NOT fire. An empty
tuple means "every day is allowed".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Tuple

#: Supported interval units. ``minutes`` exists for genuinely short
#: schedules; the usual presets map onto hours/days/weeks/months.
INTERVAL_UNITS: Tuple[str, ...] = ("minutes", "hours", "days", "weeks", "months")

#: What to do when a grid point falls into an excluded window.
EXCEPTION_POLICIES: Tuple[str, ...] = ("skip", "postpone")

#: What to do with a point whose time already passed while the platform was off.
MISFIRE_POLICIES: Tuple[str, ...] = ("skip", "catch_up_one")

#: What to do when the previous run of the SAME schedule is still working.
OVERLAP_POLICIES: Tuple[str, ...] = ("skip", "queue_one", "cancel_previous")

#: Lifecycle states of a schedule.
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_FINISHED = "finished"
STATUS_ERROR_HOLD = "error_hold"

STATUSES: Tuple[str, ...] = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_FINISHED, STATUS_ERROR_HOLD)

#: Hard limits that keep a malformed rule from reaching the grid calculation.
MAX_INTERVAL_VALUE = 100000
MAX_TASK_CHARS = 20000
MAX_TITLE_CHARS = 120
MAX_BLOCKED_RANGES = 50
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600
MAX_STEPS_RANGE = (1, 10000)


@dataclass(frozen=True)
class BlockedRange:
    """One excluded calendar period (a vacation, a freeze, a blackout)."""

    from_date: str          # ISO date (YYYY-MM-DD), inclusive
    to_date: str            # ISO date (YYYY-MM-DD), inclusive
    note: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"from": self.from_date, "to": self.to_date, "note": self.note}

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["BlockedRange"]:
        """Build a range from a stored dict; None when the value is unusable."""
        if not isinstance(raw, dict):
            return None
        start = str(raw.get("from") or raw.get("from_date") or "").strip()
        end = str(raw.get("to") or raw.get("to_date") or "").strip()
        if not start or not end:
            return None
        return cls(from_date=start, to_date=end, note=str(raw.get("note") or "").strip())


@dataclass
class Schedule:
    """One recurring task assigned to an employee.

    ``first_run_at`` is both the first firing time and the ANCHOR of the
    grid: every later point is computed from it, never from the previous
    result. That is what keeps a monthly rule on the 31st from drifting
    into the 28th after February.

    ``ends_at`` is the optional last moment the rule may fire at; ``None``
    means "never ends" (the UI calls it "Never"). It is NOT the last actual
    run - that is ``last_run_at``, which only the executor writes.
    """

    id: str = ""
    title: str = ""
    agent_slug: str = ""
    task_text: str = ""
    brief_text: str = ""
    workspace: str = ""

    first_run_at: int = 0
    ends_at: Optional[int] = None
    interval_value: int = 1
    interval_unit: str = "days"
    tz: str = "Europe/Moscow"

    #: ISO weekdays that are NOT allowed (empty = all days allowed).
    blocked_weekdays: Tuple[int, ...] = ()
    blocked_ranges: Tuple[BlockedRange, ...] = ()
    #: "HH:MM-HH:MM" local window during which nothing may fire.
    quiet_hours: str = ""

    exception_policy: str = "skip"
    max_shift_seconds: int = 7 * 24 * 3600
    misfire_policy: str = "skip"
    misfire_grace_seconds: int = 3600
    overlap_policy: str = "skip"

    max_runs: Optional[int] = None
    max_runs_per_day: Optional[int] = None

    max_steps: int = 60
    budget_tokens: int = 0
    timeout_seconds: int = 900
    max_depth: int = 0

    next_run_at: Optional[int] = None
    last_run_at: Optional[int] = None
    last_status: str = ""
    last_error_code: str = ""

    status: str = STATUS_ACTIVE
    run_count: int = 0
    fail_count: int = 0
    consecutive_failures: int = 0

    created_at: int = 0
    updated_at: int = 0
    created_by: str = ""

    # ── Derived helpers ────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """True while the rule is allowed to fire."""
        return self.status == STATUS_ACTIVE

    def is_open_ended(self) -> bool:
        """True when the rule has no end date ("Never")."""
        return self.ends_at is None

    def effective_status(self, now: int, running: bool = False,
                         overdue_grace: int = 300) -> str:
        """Return the status the UI shows, computed from the live facts.

        ``running`` tells whether a run of this schedule is executing right
        now. A past-due ``next_run_at`` beyond ``overdue_grace`` means the
        tick did not happen (the platform was off), which is a different
        state from a disabled rule.
        """
        if running:
            return "running"
        if self.status == STATUS_ERROR_HOLD:
            return "error"
        if self.status == STATUS_FINISHED:
            return "finished"
        if self.status != STATUS_ACTIVE:
            return "paused"
        if self.next_run_at is not None and self.next_run_at < now - overdue_grace:
            return "overdue"
        return "active"

    def weekday_spec(self) -> str:
        """Return blocked weekdays as the stored comma-separated string."""
        return ",".join(str(int(day)) for day in sorted(set(self.blocked_weekdays)))

    def blocked_ranges_json(self) -> str:
        """Return the excluded periods in the stored JSON form."""
        return json.dumps([item.to_dict() for item in self.blocked_ranges],
                          ensure_ascii=False)

    # ── Validation ─────────────────────────────────────────────────────────

    def validate(self, now: Optional[int] = None, *, creating: bool = False,
                 check_first_run: bool = True) -> List[Dict[str, str]]:
        """Return validation errors as ``[{"field", "code"}]``.

        An empty list means the rule may be stored. Codes are stable
        identifiers; the UI maps each of them onto a localized message, so
        no display text lives here. ``creating`` relaxes nothing by itself -
        it is accepted so a caller can express intent without branching on
        the code list.
        """
        errors: List[Dict[str, str]] = []

        def fail(field_name: str, code: str) -> None:
            errors.append({"field": field_name, "code": code})

        if not str(self.title or "").strip():
            fail("title", "title_required")
        elif len(str(self.title).strip()) > MAX_TITLE_CHARS:
            fail("title", "title_too_long")

        if not str(self.agent_slug or "").strip():
            fail("agent_slug", "employee_required")

        if not str(self.task_text or "").strip():
            fail("task_text", "task_required")
        elif len(str(self.task_text)) > MAX_TASK_CHARS:
            fail("task_text", "task_too_long")

        if not self.first_run_at:
            fail("first_run_at", "first_run_required")
        elif (check_first_run and creating and now is not None
              and int(self.first_run_at) < int(now) - 60):
            # A rule created in the past has no honest "first run"; the
            # one-minute slack absorbs the delay of typing the form.
            fail("first_run_at", "first_run_past")

        if self.ends_at is not None and self.first_run_at \
                and int(self.ends_at) <= int(self.first_run_at):
            fail("ends_at", "end_before_start")

        try:
            value = int(self.interval_value)
        except (TypeError, ValueError):
            value = 0
        if value < 1 or value > MAX_INTERVAL_VALUE:
            fail("interval_value", "interval_out_of_range")
        if self.interval_unit not in INTERVAL_UNITS:
            fail("interval_unit", "interval_unit_unknown")
        elif self.interval_unit == "minutes" and 0 < value < 1:
            fail("interval_value", "interval_below_minute")

        for day in self.blocked_weekdays:
            try:
                number = int(day)
            except (TypeError, ValueError):
                fail("blocked_weekdays", "weekday_invalid")
                break
            if number < 1 or number > 7:
                fail("blocked_weekdays", "weekday_invalid")
                break

        if len(self.blocked_ranges) > MAX_BLOCKED_RANGES:
            fail("blocked_ranges", "too_many_ranges")
        for item in self.blocked_ranges:
            if str(item.from_date) > str(item.to_date):
                fail("blocked_ranges", "range_reversed")
                break

        if self.quiet_hours and not parse_quiet_hours(self.quiet_hours):
            fail("quiet_hours", "quiet_hours_invalid")

        if self.exception_policy not in EXCEPTION_POLICIES:
            fail("exception_policy", "exception_policy_unknown")
        if self.misfire_policy not in MISFIRE_POLICIES:
            fail("misfire_policy", "misfire_policy_unknown")
        if self.overlap_policy not in OVERLAP_POLICIES:
            fail("overlap_policy", "overlap_policy_unknown")

        low, high = MAX_STEPS_RANGE
        if not (low <= int(self.max_steps or 0) <= high):
            fail("max_steps", "max_steps_out_of_range")
        if int(self.budget_tokens or 0) < 0:
            fail("budget_tokens", "budget_negative")
        if not (MIN_TIMEOUT_SECONDS <= int(self.timeout_seconds or 0) <= MAX_TIMEOUT_SECONDS):
            fail("timeout_seconds", "timeout_out_of_range")
        if self.status not in STATUSES:
            fail("status", "status_unknown")

        return errors

    # ── Database mapping ───────────────────────────────────────────────────

    def to_row(self) -> Dict[str, Any]:
        """Return the column values of this rule for the ``schedules`` table."""
        return {
            "id": self.id,
            "title": self.title,
            "agent_slug": self.agent_slug,
            "task_text": self.task_text,
            "brief_text": self.brief_text,
            "workspace": self.workspace,
            "first_run_at": int(self.first_run_at or 0),
            "ends_at": None if self.ends_at is None else int(self.ends_at),
            "interval_value": int(self.interval_value or 1),
            "interval_unit": self.interval_unit,
            "tz": self.tz,
            "blocked_weekdays": self.weekday_spec(),
            "blocked_ranges_json": self.blocked_ranges_json(),
            "quiet_hours": self.quiet_hours,
            "exception_policy": self.exception_policy,
            "max_shift_seconds": int(self.max_shift_seconds),
            "misfire_policy": self.misfire_policy,
            "misfire_grace_seconds": int(self.misfire_grace_seconds),
            "overlap_policy": self.overlap_policy,
            "max_runs": self.max_runs,
            "max_runs_per_day": self.max_runs_per_day,
            "max_steps": int(self.max_steps),
            "budget_tokens": int(self.budget_tokens),
            "timeout_seconds": int(self.timeout_seconds),
            "max_depth": int(self.max_depth),
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "last_error_code": self.last_error_code,
            "status": self.status,
            "run_count": int(self.run_count),
            "fail_count": int(self.fail_count),
            "consecutive_failures": int(self.consecutive_failures),
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            "created_by": self.created_by,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Schedule":
        """Build a rule from a database row (mapping or sqlite3.Row)."""
        data = dict(row) if not isinstance(row, dict) else row

        def text(key: str, default: str = "") -> str:
            raw = data.get(key)
            return default if raw is None else str(raw)

        def number(key: str, default: int = 0) -> int:
            try:
                return int(data.get(key))
            except (TypeError, ValueError):
                return default

        def optional_number(key: str) -> Optional[int]:
            raw = data.get(key)
            if raw is None or raw == "":
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        weekdays: List[int] = []
        for part in text("blocked_weekdays").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                weekdays.append(int(part))
            except ValueError:
                continue

        ranges: List[BlockedRange] = []
        try:
            raw_ranges = json.loads(text("blocked_ranges_json", "[]"))
        except (TypeError, ValueError):
            raw_ranges = []
        if isinstance(raw_ranges, list):
            for item in raw_ranges:
                parsed = BlockedRange.from_dict(item)
                if parsed is not None:
                    ranges.append(parsed)

        return cls(
            id=text("id"),
            title=text("title"),
            agent_slug=text("agent_slug"),
            task_text=text("task_text"),
            brief_text=text("brief_text"),
            workspace=text("workspace"),
            first_run_at=number("first_run_at"),
            ends_at=optional_number("ends_at"),
            interval_value=number("interval_value", 1),
            interval_unit=text("interval_unit", "days"),
            tz=text("tz", "Europe/Moscow"),
            blocked_weekdays=tuple(sorted(set(weekdays))),
            blocked_ranges=tuple(ranges),
            quiet_hours=text("quiet_hours"),
            exception_policy=text("exception_policy", "skip"),
            max_shift_seconds=number("max_shift_seconds", 7 * 24 * 3600),
            misfire_policy=text("misfire_policy", "skip"),
            misfire_grace_seconds=number("misfire_grace_seconds", 3600),
            overlap_policy=text("overlap_policy", "skip"),
            max_runs=optional_number("max_runs"),
            max_runs_per_day=optional_number("max_runs_per_day"),
            max_steps=number("max_steps", 60),
            budget_tokens=number("budget_tokens"),
            timeout_seconds=number("timeout_seconds", 900),
            max_depth=number("max_depth"),
            next_run_at=optional_number("next_run_at"),
            last_run_at=optional_number("last_run_at"),
            last_status=text("last_status"),
            last_error_code=text("last_error_code"),
            status=text("status", STATUS_ACTIVE),
            run_count=number("run_count"),
            fail_count=number("fail_count"),
            consecutive_failures=number("consecutive_failures"),
            created_at=number("created_at"),
            updated_at=number("updated_at"),
            created_by=text("created_by"),
        )

    def clone(self, **changes: Any) -> "Schedule":
        """Return a copy of this rule with *changes* applied."""
        return replace(self, **changes)


# ── Quiet-hours helpers ─────────────────────────────────────────────────────


def parse_quiet_hours(spec: str) -> Optional[Tuple[int, int]]:
    """Parse ``"HH:MM-HH:MM"`` into ``(start_minute, end_minute)``.

    Returns None when the spec is malformed or when both ends are equal
    (an empty window is not a quiet window). The window may cross midnight:
    ``"22:00-07:00"`` is valid and means 22:00 up to 06:59 next day.
    """
    text = str(spec or "").strip()
    if not text or "-" not in text:
        return None
    left, _, right = text.partition("-")
    start = _parse_hhmm(left)
    end = _parse_hhmm(right)
    if start is None or end is None or start == end:
        return None
    return start, end


def _parse_hhmm(value: str) -> Optional[int]:
    """Return minutes since midnight for ``"HH:MM"``, or None."""
    text = str(value or "").strip()
    if ":" not in text:
        return None
    hours_text, _, minutes_text = text.partition(":")
    try:
        hours = int(hours_text)
        minutes = int(minutes_text)
    except (TypeError, ValueError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def format_quiet_hours(spec: str) -> str:
    """Return a normalized ``"HH:MM-HH:MM"`` string (empty when invalid)."""
    parsed = parse_quiet_hours(spec)
    if parsed is None:
        return ""
    start, end = parsed
    return "%02d:%02d-%02d:%02d" % (start // 60, start % 60, end // 60, end % 60)


def new_schedule_id() -> str:
    """Return a fresh schedule identifier (12 hex characters)."""
    import uuid
    return uuid.uuid4().hex[:12]


def now_epoch() -> int:
    """Return the current time as integer epoch seconds (UTC)."""
    return int(time.time())


def merge_weekday_presets(preset: str) -> Tuple[int, ...]:
    """Translate a quick preset into the ISO days it BLOCKS.

    ``weekdays_only`` blocks Saturday and Sunday; ``weekends_only`` blocks
    Monday through Friday; anything else blocks nothing.
    """
    key = str(preset or "").strip().lower()
    if key == "weekdays_only":
        return (6, 7)
    if key == "weekends_only":
        return (1, 2, 3, 4, 5)
    return ()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
