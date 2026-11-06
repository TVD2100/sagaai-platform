"""The installation clock."""
from __future__ import annotations

from typing import Any, Dict

from .deps import ToolMixin


class ClockTools(ToolMixin):
    """The installation clock."""

    def current_datetime(self) -> Dict[str, Any]:
        """Return the installation's current date and time.

        The system prompt carries the date as a fact, but a fact cannot answer
        "what time is it" or "what day of the week is the 15th". This tool
        answers those questions from the same clock, so the prompt and the tool
        can never disagree: the weekday name and the date line come from the
        helpers that build the prompt block (``core.orchestrators``).

        Returns:
            ``{ok, iso, date, date_iso, weekday, weekday_ru, time, timezone,
            utc_offset, epoch, date_line}``. ``date`` uses the day-first
            ``DD.MM.YYYY`` form common in the installation's schedules, while
            ``date_iso`` keeps the sortable ISO form.
        """
        try:
            # A module-level ``datetime`` binding (installed by tests to freeze
            # the clock) wins; normal runs import the real class.
            _dt = self.deps().datetime
        except NameError:
            from datetime import datetime as _dt

        try:
            from core import orchestrators as _orch
            moment = _dt.now()
            if moment.tzinfo is None:
                moment = moment.astimezone()
            weekday = _orch._WEEKDAY_NAMES[moment.weekday()]
            date_line = _orch._current_date_line(moment)
            utc_offset = _orch._format_utc_offset(moment)
            weekday_ru = _orch._WEEKDAY_NAMES_RU[moment.weekday()]
        except Exception:
            moment = _dt.now()
            if moment.tzinfo is None:
                moment = moment.astimezone()
            weekday = moment.strftime("%A")
            weekday_ru = ""
            date_line = moment.strftime("Today is %d.%m.%Y.")
            offset = moment.utcoffset()
            total = int(offset.total_seconds()) if offset else 0
            sign = "+" if total >= 0 else "-"
            total = abs(total)
            utc_offset = "UTC%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)

        return {
            "ok": True,
            "iso": moment.isoformat(timespec="seconds"),
            "date": moment.strftime("%d.%m.%Y"),
            "date_iso": moment.strftime("%Y-%m-%d"),
            "weekday": weekday,
            "weekday_ru": weekday_ru,
            "time": moment.strftime("%H:%M:%S"),
            "timezone": moment.tzname() or "",
            "utc_offset": utc_offset,
            "epoch": int(moment.timestamp()),
            "date_line": date_line,
        }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
