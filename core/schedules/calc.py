# -*- coding: utf-8 -*-
"""
core.schedules.calc - the pure grid calculation behind a schedule.

The single public function is :func:`compute_next_run`. It is pure in the
strict sense: it reads no clock, no database and no network, and it takes
``now`` as an argument. That is what makes the whole scheduling rule set
testable without freezing time and reproducible across processes.

Model in one paragraph. A rule owns an ANCHOR (``first_run_at``) and a
step (``interval_value`` x ``interval_unit``). The candidate points are the
anchor plus k steps, for k = 0, 1, 2, ... - always computed from the anchor,
never accumulated from the previous result. Accumulation is what makes a
monthly rule drift: 31 Jan -> 28 Feb -> 28 Mar instead of returning to the
31st in March. Exceptions (blocked weekdays, blocked periods, quiet hours)
are applied ON TOP of the grid: with ``skip`` the offending point simply
disappears, with ``postpone`` it moves to the nearest allowed moment inside
``max_shift_seconds``. The grid itself is never re-anchored by a postponement.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

from core.schedules.model import BlockedRange, Schedule, parse_quiet_hours

#: How many grid points may be probed before the rule is declared unfulfillable.
MAX_GRID_PROBES = 20000

#: How many boundary jumps a single postponement may take.
MAX_SHIFT_STEPS = 64

#: Attempts to move a wall-clock time out of a DST gap.
MAX_DST_FIXUPS = 4

_UTC = timezone.utc


def resolve_zone(name: str):
    """Return a tzinfo for an IANA name, falling back to UTC."""
    text = str(name or "").strip()
    if not text or text.upper() in ("UTC", "GMT", "Z"):
        return _UTC
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(text)
    except Exception:
        return _UTC


def _to_local(epoch: int, zone) -> datetime:
    """Convert epoch seconds to an aware datetime in *zone*."""
    return datetime.fromtimestamp(int(epoch), tz=_UTC).astimezone(zone)


def _to_epoch(moment: datetime) -> int:
    """Convert an aware datetime to integer epoch seconds."""
    return int(moment.timestamp())


def _safe_local(naive: datetime, zone) -> datetime:
    """Attach *zone* to a wall-clock value, resolving a DST gap forward."""
    candidate = naive
    for _ in range(MAX_DST_FIXUPS):
        aware = candidate.replace(tzinfo=zone)
        if aware.astimezone(_UTC).astimezone(zone).replace(tzinfo=None) == candidate:
            return aware
        candidate = candidate + timedelta(hours=1)
    return candidate.replace(tzinfo=zone)


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_months(anchor: datetime, months: int) -> datetime:
    """Shift *anchor* by *months*, clamping the day to the month end."""
    total = (anchor.year * 12 + (anchor.month - 1)) + int(months)
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(anchor.day, _days_in_month(year, month))
    return anchor.replace(year=year, month=month, day=day)


def _grid_point(schedule: Schedule, anchor: datetime, k: int) -> Optional[datetime]:
    """Return the k-th grid point in the schedule's zone (None on a bad unit)."""
    zone = resolve_zone(schedule.tz)
    step = max(1, int(schedule.interval_value or 1)) * int(k)
    unit = str(schedule.interval_unit or "days")
    naive = anchor.replace(tzinfo=None)

    if unit == "minutes":
        return _safe_local(naive + timedelta(minutes=step), zone)
    if unit == "hours":
        return _safe_local(naive + timedelta(hours=step), zone)
    if unit == "days":
        return _safe_local(naive + timedelta(days=step), zone)
    if unit == "weeks":
        return _safe_local(naive + timedelta(weeks=step), zone)
    if unit == "months":
        return _safe_local(_add_months(naive, step), zone)
    return None


def _estimate_k(schedule: Schedule, anchor: datetime, target: datetime) -> int:
    """Return a starting index near *target* so probing does not start at zero."""
    step = max(1, int(schedule.interval_value or 1))
    seconds = (target.replace(tzinfo=None) - anchor.replace(tzinfo=None)).total_seconds()
    unit = str(schedule.interval_unit or "days")
    if unit == "minutes":
        return max(0, int(seconds // (60 * step)))
    if unit == "hours":
        return max(0, int(seconds // (3600 * step)))
    if unit == "days":
        return max(0, int(seconds // (86400 * step)))
    if unit == "weeks":
        return max(0, int(seconds // (604800 * step)))
    if unit == "months":
        months = (target.year - anchor.year) * 12 + (target.month - anchor.month)
        return max(0, int(months // step))
    return 0


def _in_quiet_hours(moment: datetime, spec: str) -> bool:
    """True when the wall-clock time falls inside the quiet window."""
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start_minute, end_minute = window
    minute = moment.hour * 60 + moment.minute
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def _in_blocked_range(moment: datetime, ranges: Tuple[BlockedRange, ...]) -> Optional[BlockedRange]:
    """Return the blocked period containing *moment*, or None."""
    today = moment.date().isoformat()
    for item in ranges:
        if str(item.from_date) <= today <= str(item.to_date):
            return item
    return None


def exclusion_reason(schedule: Schedule, moment: datetime) -> str:
    """Return why *moment* is not allowed, or an empty string when it is.

    The codes are stable and localized by the caller:
    ``weekday``, ``blocked_range``, ``quiet_hours``.
    """
    local = moment.astimezone(resolve_zone(schedule.tz))
    if local.isoweekday() in set(int(d) for d in schedule.blocked_weekdays):
        return "weekday"
    if _in_blocked_range(local, schedule.blocked_ranges) is not None:
        return "blocked_range"
    if schedule.quiet_hours and _in_quiet_hours(local, schedule.quiet_hours):
        return "quiet_hours"
    return ""


def _next_boundary(schedule: Schedule, moment: datetime) -> datetime:
    """Return the next moment at which a violated condition may have ended."""
    zone = resolve_zone(schedule.tz)
    local = moment.astimezone(zone)

    if schedule.quiet_hours and _in_quiet_hours(local, schedule.quiet_hours):
        window = parse_quiet_hours(schedule.quiet_hours)
        if window is not None:
            _, end_minute = window
            candidate = local.replace(hour=end_minute // 60, minute=end_minute % 60,
                                      second=0, microsecond=0)
            if candidate <= local:
                candidate = candidate + timedelta(days=1)
            return candidate

    blocked = _in_blocked_range(local, schedule.blocked_ranges)
    if blocked is not None:
        try:
            end_day = date.fromisoformat(str(blocked.to_date))
        except ValueError:
            return local + timedelta(days=1)
        naive = datetime.combine(end_day + timedelta(days=1), local.time())
        return _safe_local(naive, zone)

    naive = local.replace(tzinfo=None) + timedelta(days=1)
    return _safe_local(naive, zone)


def _apply_exceptions(schedule: Schedule, moment: datetime) -> Optional[datetime]:
    """Return the allowed moment for a grid point, or None when it is dropped."""
    if not exclusion_reason(schedule, moment):
        return moment
    if schedule.exception_policy != "postpone":
        return None

    limit = moment + timedelta(seconds=max(0, int(schedule.max_shift_seconds)))
    current = moment
    for _ in range(MAX_SHIFT_STEPS):
        current = _next_boundary(schedule, current)
        if current > limit:
            return None
        if not exclusion_reason(schedule, current):
            return current
    return None


def compute_next_run(schedule: Schedule, now: datetime) -> Optional[datetime]:
    """Return the first allowed grid point strictly after *now*.

    The search starts at the anchor and skips forward analytically, so a rule
    that has been idle for years costs the same as one evaluated a minute
    after creation. Returns an aware datetime in the schedule's zone, or
    None when the rule can never fire again (its end date passed, its day
    mask blocks every weekday, or the exception rules leave no allowed
    moment at all).

    ``last_run_at`` acts as a floor: a clock that was set back must never
    make the engine run a point a second time. Idempotency is additionally
    protected by a unique key on (schedule, planned time) in the database -
    this floor is the polite half of that guard, not the only one.
    """
    zone = resolve_zone(schedule.tz)
    anchor = _to_local(schedule.first_run_at, zone)

    end_moment = None
    if schedule.ends_at is not None:
        end_moment = _to_local(schedule.ends_at, zone)

    floor_at = now.astimezone(zone)
    if schedule.last_run_at is not None:
        last = _to_local(schedule.last_run_at, zone)
        if last > floor_at:
            floor_at = last

    if end_moment is not None and floor_at >= end_moment:
        return None

    start_k = max(0, _estimate_k(schedule, anchor, floor_at) - 1)
    for offset in range(MAX_GRID_PROBES):
        moment = _grid_point(schedule, anchor, start_k + offset)
        if moment is None:
            return None
        if moment <= floor_at:
            continue
        if end_moment is not None and moment > end_moment:
            return None
        candidate = _apply_exceptions(schedule, moment)
        if candidate is None:
            continue
        if end_moment is not None and candidate > end_moment:
            return None
        return candidate
    return None


def next_run_epoch(schedule: Schedule, now_epoch: int) -> Optional[int]:
    """Return the next run as epoch seconds (the form stored in SQLite)."""
    now = _to_local(int(now_epoch), resolve_zone(schedule.tz))
    moment = compute_next_run(schedule, now)
    return None if moment is None else _to_epoch(moment)


def describe_grid(schedule: Schedule) -> List[int]:
    """Return the first five raw grid points as epoch seconds (diagnostics).

    The exceptions are deliberately NOT applied here: this helper exists so a
    caller can show which cadence the rule would fire at before any day mask
    or vacation narrows it further.
    """
    zone = resolve_zone(schedule.tz)
    anchor = _to_local(schedule.first_run_at, zone)
    points: List[int] = []
    for k in range(5):
        moment = _grid_point(schedule, anchor, k)
        if moment is None:
            break
        points.append(_to_epoch(moment))
    return points


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
