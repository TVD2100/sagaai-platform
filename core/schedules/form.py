# -*- coding: utf-8 -*-
"""
core.schedules.form - the pure part of the schedule form.

The page must not invent its own time handling: a "09:00 Europe/Moscow"
entered in the form has to mean the same instant the engine will compute,
otherwise the preview and the actual run disagree. Everything that converts
between what a human types and what the store keeps lives here, free of
Streamlit, so it is testable on its own.
"""
from __future__ import annotations

import time
from datetime import date, datetime, time as _time, timezone
from typing import List, Optional, Tuple

from core.schedules.calc import compute_next_run, resolve_zone
from core.schedules.model import Schedule, format_quiet_hours, merge_weekday_presets

#: Time zones offered in the form. A free-form field is still accepted:
#: the list is a convenience, not a whitelist.
COMMON_ZONES: Tuple[str, ...] = (
    "Europe/Moscow", "Europe/Kaliningrad", "Europe/Samara", "Asia/Yekaterinburg",
    "Asia/Omsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk",
    "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka", "UTC",
)

#: Quick day presets: (id, blocked ISO weekdays).
WEEKDAY_PRESETS: Tuple[Tuple[str, Tuple[int, ...]], ...] = (
    ("every_day", ()),
    ("weekdays_only", merge_weekday_presets("weekdays_only")),
    ("weekends_only", merge_weekday_presets("weekends_only")),
)


def parse_local_time(text: str) -> Optional[Tuple[int, int]]:
    """Parse ``"HH:MM"`` into ``(hour, minute)``, or None when malformed."""
    raw = str(text or "").strip()
    if ":" not in raw:
        return None
    hours_text, _, minutes_text = raw.partition(":")
    try:
        hours = int(hours_text.strip())
        minutes = int(minutes_text.strip())
    except (TypeError, ValueError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours, minutes


def epoch_from_local(day: date, clock: str, tz_name: str) -> Optional[int]:
    """Return the epoch of a local wall-clock moment, or None.

    The wall clock is interpreted in *tz_name*: that is what makes a rule
    keep its "09:00" through a machine that travels or a server in another
    zone.
    """
    parsed = parse_local_time(clock)
    if parsed is None or not isinstance(day, date):
        return None
    hours, minutes = parsed
    zone = resolve_zone(tz_name)
    naive = datetime(day.year, day.month, day.day, hours, minutes)
    return int(naive.replace(tzinfo=zone).timestamp())


def local_inputs(epoch: Optional[int], tz_name: str) -> Tuple[date, str]:
    """Return ``(date, "HH:MM")`` for an epoch, as the form shows it."""
    zone = resolve_zone(tz_name)
    if epoch:
        moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(zone)
    else:
        moment = datetime.now(tz=zone).replace(second=0, microsecond=0)
    return moment.date(), "%02d:%02d" % (moment.hour, moment.minute)


def format_moment(epoch: Optional[int], tz_name: str) -> str:
    """Return a readable ``DD.MM.YYYY HH:MM`` in the rule's own zone."""
    if not epoch:
        return "-"
    zone = resolve_zone(tz_name)
    moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(zone)
    return moment.strftime("%d.%m.%Y %H:%M")


def next_points(schedule: Schedule, now_epoch: Optional[int] = None,
                count: int = 5) -> List[int]:
    """Return the next *count* firing moments the rule would actually take.

    The preview walks the real engine, exceptions included, so it shows what
    will happen rather than what the raw cadence suggests. A rule that can
    never fire returns an empty list.
    """
    moment = int(now_epoch if now_epoch is not None else time.time())
    points: List[int] = []
    cursor = schedule.clone(last_run_at=None)
    for _ in range(max(1, int(count))):
        zone = resolve_zone(cursor.tz)
        now_local = datetime.fromtimestamp(moment, tz=timezone.utc).astimezone(zone)
        upcoming = compute_next_run(cursor, now_local)
        if upcoming is None:
            break
        points.append(int(upcoming.timestamp()))
        moment = int(upcoming.timestamp())
        cursor = cursor.clone(last_run_at=moment)
    return points


def weekday_preset_of(blocked: Tuple[int, ...]) -> str:
    """Return the preset id matching *blocked*, or ``custom``."""
    normalized = tuple(sorted(set(int(day) for day in blocked)))
    for preset_id, days in WEEKDAY_PRESETS:
        if tuple(sorted(set(days))) == normalized:
            return preset_id
    return "custom"


def quiet_hours_value(text: str) -> str:
    """Return the normalized quiet window, or an empty string when invalid."""
    return format_quiet_hours(text)


def humanize_interval(value: int, unit: str) -> str:
    """Return a compact interval label such as ``2 days``.

    The unit word is looked up by the caller; this helper only decides the
    plural form and keeps the number formatting consistent.
    """
    number = max(1, int(value or 1))
    return "%d %s" % (number, str(unit or "days"))
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
