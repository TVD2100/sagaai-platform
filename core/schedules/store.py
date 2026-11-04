# -*- coding: utf-8 -*-
"""
core.schedules.store - persistence for schedules and their run history.

Two concerns live here, and they are deliberately in ONE module because they
share the single invariant the feature rests on: a scheduled point fires at
most once. ``claim_due`` performs the claim and the history insert in one
``BEGIN IMMEDIATE`` transaction, so either both happen or neither does.

The storage is raw SQL over the main database (``sagaai.db``), the same
approach the marketplace uses: the due-point claim needs a conditional
``UPDATE ... WHERE`` plus a unique index, which is exactly what a relational
store is good at and what a JSON file cannot guarantee under a restart.

All times are integer epoch seconds in UTC. Nothing here formats a date for
display - that belongs to the UI layer.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from core.schedules.model import STATUS_ACTIVE, Schedule

log = logging.getLogger(__name__)

#: Statuses a run can never leave. Everything else is "in flight".
TERMINAL_RUN_STATUSES = (
    "done", "failed", "cancelled", "awaiting_input",
    "skipped_overlap", "skipped", "missed", "interrupted",
)

#: How many due schedules one tick may pick up. A larger batch would run the
#: platform out of memory before it could report progress; the rest wait for
#: the next tick, which is 20 seconds away.
MAX_DUE_BATCH = 10


def _now() -> int:
    return int(time.time())


def _engine():
    """Return the main database engine (imported lazily for test isolation)."""
    from storage.db import get_engine
    return get_engine()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


# ── Vocabulary ──────────────────────────────────────────────────────────────


def create_schedule(schedule: Schedule) -> Schedule:
    """Insert a new rule and return it with the stored timestamps filled in."""
    moment = _now()
    stored = schedule.clone(
        created_at=schedule.created_at or moment,
        updated_at=moment,
    )
    row = stored.to_row()
    columns = ", ".join(row.keys())
    placeholders = ", ".join(":" + key for key in row.keys())
    with _engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO schedules (" + columns + ") VALUES (" + placeholders + ")"
        ), row)
    return stored


def update_schedule(schedule: Schedule) -> Schedule:
    """Write every mutable field of an existing rule back to the table."""
    stored = schedule.clone(updated_at=_now())
    row = stored.to_row()
    assignments = ", ".join(key + " = :" + key for key in row.keys() if key != "id")
    with _engine().begin() as conn:
        conn.execute(text(
            "UPDATE schedules SET " + assignments + " WHERE id = :id"
        ), row)
    return stored


def get_schedule(schedule_id: str) -> Optional[Schedule]:
    """Return one rule by id, or None when it is gone."""
    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM schedules WHERE id = :id"),
            {"id": str(schedule_id or "")},
        ).fetchone()
    return None if row is None else Schedule.from_row(_row_to_dict(row))


def list_schedules(status: Optional[str] = None) -> List[Schedule]:
    """Return every rule, newest first, optionally filtered by status."""
    query = "SELECT * FROM schedules"
    params: Dict[str, Any] = {}
    if status:
        query += " WHERE status = :status"
        params["status"] = str(status)
    query += " ORDER BY created_at DESC"
    with _engine().connect() as conn:
        rows = conn.execute(text(query), params).fetchall()
    return [Schedule.from_row(_row_to_dict(row)) for row in rows]


def delete_schedule(schedule_id: str) -> bool:
    """Delete a rule together with its per-point history rows.

    The CONVERSATIONS of past runs are NOT touched: every run owns an ordinary
dialog thread, and those threads live in the History page like any other
dialog. That is the record the owner actually reads, so removing a rule
never destroys what a scheduled run did - it only removes the rule and its
compact per-point log.
    """
    target = str(schedule_id or "")
    if not target:
        return False
    with _engine().begin() as conn:
        conn.execute(text("DELETE FROM schedule_runs WHERE schedule_id = :id"),
                     {"id": target})
        result = conn.execute(text("DELETE FROM schedules WHERE id = :id"),
                              {"id": target})
    return bool(result.rowcount)


# ── Due-point claim ─────────────────────────────────────────────────────────


def claim_due(schedule_id: str, planned_at: int, *, next_run_at: Optional[int],
              trigger: str = "schedule", now: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Atomically claim one scheduled point and open its history row.

    Returns the history row as a dict when THIS caller won the claim, and
    None when someone else already took the point (another process, an
    earlier tick, or a manual run of the same instant).

    The conditional update is the whole concurrency story: it moves
    ``next_run_at`` forward only while the row still points at the instant the
    caller saw. The unique index on (schedule_id, planned_at) is the second
    line of defence, catching a caller that somehow skipped the first.
    """
    moment = _now() if now is None else int(now)
    run_row_id = uuid.uuid4().hex[:12]
    try:
        with _engine().begin() as conn:
            result = conn.execute(text(
                "UPDATE schedules SET next_run_at = :next_run_at, "
                "last_run_at = :planned_at, status = :status, "
                "lock_owner = :owner, lock_expires_at = :expires, "
                "updated_at = :now "
                "WHERE id = :id AND status = :active AND next_run_at = :planned_at"
            ), {
                "next_run_at": next_run_at,
                "planned_at": int(planned_at),
                "status": "running",
                "active": STATUS_ACTIVE,
                "owner": _owner_token(),
                "expires": moment + 3600,
                "now": moment,
                "id": str(schedule_id),
            })
            if not result.rowcount:
                return None
            conn.execute(text(
                "INSERT INTO schedule_runs "
                "(id, schedule_id, planned_at, trigger, status, started_at, reason) "
                "VALUES (:id, :schedule_id, :planned_at, :trigger, 'started', :started_at, '')"
            ), {
                "id": run_row_id,
                "schedule_id": str(schedule_id),
                "planned_at": int(planned_at),
                "trigger": str(trigger or "schedule"),
                "started_at": moment,
            })
    except Exception as exc:
        # A unique-index violation means the point was already claimed; any
        # other failure is logged so a broken database does not look like a
        # quiet "nothing was due".
        if "UNIQUE" in str(exc).upper():
            return None
        log.warning("claim_due failed for %s at %s: %s", schedule_id, planned_at, exc)
        return None
    return get_run_row(run_row_id)


def record_skipped(schedule_id: str, planned_at: int, *, reason: str,
                   status: str = "skipped", next_run_at: Optional[int] = None,
                   now: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Write a history row for a point that will NOT run, and move the grid.

    A missed or excluded point must leave a trace: the most common support
    question about a scheduler is "why did it not run at 9", and a silent skip
    makes that question unanswerable.
    """
    moment = _now() if now is None else int(now)
    run_row_id = uuid.uuid4().hex[:12]
    try:
        with _engine().begin() as conn:
            if next_run_at is not None:
                conn.execute(text(
                    "UPDATE schedules SET next_run_at = :next_run_at, "
                    "updated_at = :now WHERE id = :id"
                ), {"next_run_at": next_run_at, "now": moment, "id": str(schedule_id)})
            conn.execute(text(
                "INSERT INTO schedule_runs "
                "(id, schedule_id, planned_at, trigger, status, started_at, "
                "finished_at, reason) "
                "VALUES (:id, :schedule_id, :planned_at, 'schedule', :status, "
                ":started_at, :finished_at, :reason)"
            ), {
                "id": run_row_id,
                "schedule_id": str(schedule_id),
                "planned_at": int(planned_at),
                "status": str(status),
                "started_at": moment,
                "finished_at": moment,
                "reason": str(reason or "")[:500],
            })
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            return None
        log.warning("record_skipped failed for %s: %s", schedule_id, exc)
        return None
    return get_run_row(run_row_id)


def finish_run(run_row_id: str, *, status: str, run_id: str = "",
               thread_id: str = "", stop_reason: str = "", error_code: str = "",
               reason: str = "", steps_used: int = 0, tokens: Optional[Dict[str, int]] = None,
               summary: str = "", now: Optional[int] = None) -> None:
    """Close one history row with the outcome of its run."""
    moment = _now() if now is None else int(now)
    counters = tokens or {}
    with _engine().begin() as conn:
        conn.execute(text(
            "UPDATE schedule_runs SET status = :status, finished_at = :finished_at, "
            "run_id = :run_id, thread_id = :thread_id, stop_reason = :stop_reason, "
            "error_code = :error_code, reason = :reason, steps_used = :steps_used, "
            "tokens_in = :tokens_in, tokens_out = :tokens_out, "
            "tokens_cache = :tokens_cache, summary = :summary "
            "WHERE id = :id"
        ), {
            "status": str(status),
            "finished_at": moment,
            "run_id": str(run_id or ""),
            "thread_id": str(thread_id or ""),
            "stop_reason": str(stop_reason or ""),
            "error_code": str(error_code or ""),
            "reason": str(reason or "")[:500],
            "steps_used": int(steps_used or 0),
            "tokens_in": int(counters.get("in") or 0),
            "tokens_out": int(counters.get("out") or 0),
            "tokens_cache": int(counters.get("cache") or 0),
            "summary": str(summary or "")[:4000],
            "id": str(run_row_id),
        })


def settle_schedule(schedule_id: str, *, status: str, last_status: str,
                    error_code: str = "", success: bool = False,
                    run_count: int = 0, fail_count: int = 0,
                    consecutive_failures: int = 0, now: Optional[int] = None) -> None:
    """Update the rule after a run: counters, last outcome and lifecycle."""
    moment = _now() if now is None else int(now)
    with _engine().begin() as conn:
        conn.execute(text(
            "UPDATE schedules SET status = :status, last_status = :last_status, "
            "last_error_code = :error_code, run_count = :run_count, "
            "fail_count = :fail_count, consecutive_failures = :consecutive_failures, "
            "lock_owner = NULL, lock_expires_at = NULL, updated_at = :now "
            "WHERE id = :id"
        ), {
            "status": str(status),
            "last_status": str(last_status or ""),
            "error_code": str(error_code or ""),
            "run_count": int(run_count),
            "fail_count": int(fail_count),
            "consecutive_failures": int(consecutive_failures),
            "now": moment,
            "id": str(schedule_id),
        })


def _owner_token() -> str:
    """Return the ``host:pid`` token of the process holding a claim."""
    import os
    import socket
    try:
        host = socket.gethostname()
    except Exception:
        host = "localhost"
    return "%s:%s" % (host, os.getpid())


def open_manual_run(schedule_id: str, planned_at: int, *,
                    now: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Open a history row for a run the owner started by hand.

    A manual run is an ordinary run of the rule, not a special mode: it gets
    the same history row, the same dialog and the same counters, so "what
    happened" reads the same way whatever started it. It deliberately does
    NOT touch ``next_run_at`` - pressing "run now" must not shift the grid.
    """
    moment = _now() if now is None else int(now)
    run_row_id = uuid.uuid4().hex[:12]
    try:
        with _engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO schedule_runs "
                "(id, schedule_id, planned_at, trigger, status, started_at, reason) "
                "VALUES (:id, :schedule_id, :planned_at, 'manual', 'started', "
                ":started_at, '')"
            ), {
                "id": run_row_id,
                "schedule_id": str(schedule_id),
                "planned_at": int(planned_at),
                "started_at": moment,
            })
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            return None
        log.warning("open_manual_run failed for %s: %s", schedule_id, exc)
        return None
    return get_run_row(run_row_id)


def list_due(now: Optional[int] = None, limit: int = MAX_DUE_BATCH) -> List[Schedule]:
    """Return active rules whose next point is due, earliest first."""
    moment = _now() if now is None else int(now)
    with _engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT * FROM schedules WHERE status = :active "
            "AND next_run_at IS NOT NULL AND next_run_at <= :now "
            "ORDER BY next_run_at ASC LIMIT :limit"
        ), {"active": STATUS_ACTIVE, "now": moment, "limit": int(limit)}).fetchall()
    return [Schedule.from_row(_row_to_dict(row)) for row in rows]


def get_run_row(run_row_id: str) -> Optional[Dict[str, Any]]:
    """Return one history row as a dict, or None."""
    with _engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM schedule_runs WHERE id = :id"),
            {"id": str(run_row_id or "")},
        ).fetchone()
    return None if row is None else _row_to_dict(row)


def list_runs(schedule_id: str, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """Return the history of one schedule, newest planned point first."""
    with _engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT * FROM schedule_runs WHERE schedule_id = :id "
            "ORDER BY planned_at DESC LIMIT :limit OFFSET :offset"
        ), {
            "id": str(schedule_id or ""),
            "limit": int(limit),
            "offset": max(0, int(offset)),
        }).fetchall()
    return [_row_to_dict(row) for row in rows]


def count_runs(schedule_id: str) -> Dict[str, int]:
    """Return ``{status: count}`` for one schedule's history."""
    with _engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT status, COUNT(*) AS total FROM schedule_runs "
            "WHERE schedule_id = :id GROUP BY status"
        ), {"id": str(schedule_id or "")}).fetchall()
    return {str(_row_to_dict(row).get("status") or ""):
            int(_row_to_dict(row).get("total") or 0) for row in rows}


def active_run(schedule_id: str) -> Optional[Dict[str, Any]]:
    """Return the unfinished history row of a schedule, if any."""
    with _engine().connect() as conn:
        row = conn.execute(text(
            "SELECT * FROM schedule_runs WHERE schedule_id = :id "
            "AND status NOT IN ('done','failed','cancelled','awaiting_input',"
            "'skipped_overlap','skipped','missed','interrupted') "
            "ORDER BY planned_at DESC LIMIT 1"
        ), {"id": str(schedule_id or "")}).fetchone()
    return None if row is None else _row_to_dict(row)


def runs_today(schedule_id: str, tz_name: str = "Europe/Moscow",
               now: Optional[int] = None) -> int:
    """Return how many runs of a schedule started today in its own zone."""
    from datetime import datetime
    from datetime import timezone as _tz

    from core.schedules.calc import resolve_zone

    zone = resolve_zone(tz_name)
    moment = datetime.fromtimestamp(_now() if now is None else int(now), tz=_tz.utc)
    local = moment.astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    with _engine().connect() as conn:
        row = conn.execute(text(
            "SELECT COUNT(*) AS total FROM schedule_runs "
            "WHERE schedule_id = :id AND started_at >= :since"
        ), {"id": str(schedule_id or ""), "since": int(start.timestamp())}).fetchone()
    return int(_row_to_dict(row).get("total") or 0) if row is not None else 0


def unfinished_runs() -> List[Dict[str, Any]]:
    """Return every history row still marked as in flight (after a restart)."""
    with _engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT * FROM schedule_runs WHERE status = 'started' "
            "ORDER BY started_at ASC"
        )).fetchall()
    return [_row_to_dict(row) for row in rows]


def mark_run_status(run_row_id: str, status: str, reason: str = "",
                    now: Optional[int] = None) -> None:
    """Set a history row's status without touching the counters."""
    moment = _now() if now is None else int(now)
    with _engine().begin() as conn:
        conn.execute(text(
            "UPDATE schedule_runs SET status = :status, finished_at = :finished_at, "
            "reason = :reason WHERE id = :id"
        ), {
            "status": str(status),
            "finished_at": moment,
            "reason": str(reason or "")[:500],
            "id": str(run_row_id),
        })


def release_stale_locks(now: Optional[int] = None) -> int:
    """Return schedules stuck in ``running`` whose claim has expired.

    A process killed mid-run leaves its rule claiming to be running; without
    this sweep the schedule would never fire again.
    """
    moment = _now() if now is None else int(now)
    with _engine().begin() as conn:
        result = conn.execute(text(
            "UPDATE schedules SET status = :active, lock_owner = NULL, "
            "lock_expires_at = NULL, updated_at = :now "
            "WHERE status = 'running' "
            "AND (lock_expires_at IS NULL OR lock_expires_at < :now)"
        ), {"active": STATUS_ACTIVE, "now": moment})
    return int(result.rowcount or 0)


def set_next_run(schedule_id: str, next_run_at: Optional[int],
                 status: Optional[str] = None, now: Optional[int] = None) -> None:
    """Move a rule's next point (and optionally its lifecycle status)."""
    moment = _now() if now is None else int(now)
    query = "UPDATE schedules SET next_run_at = :next_run_at, updated_at = :now"
    params: Dict[str, Any] = {
        "next_run_at": next_run_at,
        "now": moment,
        "id": str(schedule_id),
    }
    if status:
        query += ", status = :status"
        params["status"] = str(status)
    query += " WHERE id = :id"
    with _engine().begin() as conn:
        conn.execute(text(query), params)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
