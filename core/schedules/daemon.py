# -*- coding: utf-8 -*-
"""
core.schedules.daemon - the in-process tick loop that fires schedules.

The platform is a local Streamlit application: the view is re-rendered in
response to a browser action, but the PROCESS stays alive as long as it is
running. That process is therefore the right place for a small background
thread - it keeps working while no page is open, and it dies with the
process instead of leaving an orphan service behind. The honest limitation,
stated in the interface, is that scheduled runs stop while the platform is
not running.

Responsibilities are narrow on purpose. A tick does NOT execute an agent: it
picks the due points, claims them atomically and hands each one to the
runner in its own thread. A slow or stuck run can therefore never delay the
next schedule, and a failing one cannot take the loop down.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from core.schedules import runner, store
from core.schedules.calc import next_run_epoch
from core.schedules.model import Schedule

log = logging.getLogger(__name__)

#: How often the loop looks for due points. Short enough to keep the promise
#: "it runs at 09:00", long enough that an idle platform costs nothing.
TICK_SECONDS = 20.0

#: How long a claim may stay on a rule before another tick treats it as dead.
#: It must exceed the longest allowed run so a healthy run is never stolen.
LOCK_TTL_SECONDS = 3600

#: How far behind a point may be and still be executed once ("catch up").
DEFAULT_CATCH_UP_GRACE = 3600

_process_daemon: Optional["SchedulerDaemon"] = None
_daemon_lock = threading.Lock()


def heartbeat_path() -> str:
    """Return the file the loop touches on every tick.

    The UI reads it to answer one question honestly: is anything actually
    watching the clock right now? A stale stamp explains a missed run better
    than any log line.
    """
    from core.orchestration import runs as run_store
    root = run_store.default_store_root()
    return os.path.join(root, "scheduler", "heartbeat")


def read_heartbeat() -> Optional[int]:
    """Return the epoch of the last tick, or None when the loop never ran."""
    try:
        with open(heartbeat_path(), "r", encoding="utf-8") as handle:
            return int(str(handle.read() or "0").strip() or 0)
    except Exception:
        return None


def _write_heartbeat(now: Optional[int] = None) -> None:
    """Record that the loop is alive."""
    path = heartbeat_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(int(now if now is not None else time.time())))
    except Exception as exc:  # pragma: no cover - heartbeat is best effort
        log.debug("scheduler heartbeat write failed: %s", exc)


def is_alive(max_age: float = TICK_SECONDS * 3) -> bool:
    """True when the last tick is recent enough to trust."""
    stamp = read_heartbeat()
    if not stamp:
        return False
    return (time.time() - int(stamp)) <= float(max_age)


class SchedulerDaemon(threading.Thread):
    """One background thread that fires due schedules.

    There is exactly one instance per process. ``start_scheduler`` is the only
    sanctioned entry point: it refuses to create a second loop and reports the
    one that already runs.
    """

    def __init__(self, tick_seconds: float = TICK_SECONDS) -> None:
        super().__init__(name="sagaai-scheduler", daemon=True)
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def stop(self, join_seconds: float = 5.0) -> None:
        """Ask the loop to finish; running agents are NOT interrupted.

        A scheduled run is an ordinary agent loop with its own limits. Killing
        it on shutdown would leave a half-written dialog in History, so it is
        allowed to end on its own terms; the next start reconciles whatever
        was in flight.
        """
        self._stop.set()
        self._wake.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=max(0.0, float(join_seconds)))

    def wake(self) -> None:
        """Ask the loop to look for due points right now.

        Saving a schedule with an imminent first run calls this, so the owner
        does not wait a full tick to see it fire.
        """
        self._wake.set()

    @property
    def running_workers(self) -> int:
        """Return how many runs this loop currently executes."""
        with self._lock:
            self._workers = [item for item in self._workers if item.is_alive()]
            return len(self._workers)

    # ── Main loop ───────────────────────────────────────────────────────────

    def run(self) -> None:  # pragma: no cover - exercised through tick_once
        """Tick forever until ``stop`` is called."""
        try:
            self.reconcile()
        except Exception as exc:
            log.warning("scheduler reconcile failed: %s", exc)
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as exc:  # pragma: no cover - loop must survive
                log.warning("scheduler tick failed: %s", exc)
            self._wake.wait(timeout=self.tick_seconds)
            self._wake.clear()

    def tick_once(self, now: Optional[int] = None) -> List[Dict[str, Any]]:
        """Process one pass over the due schedules and return what fired.

        Split out from ``run`` so the behaviour of a tick is testable without
        starting a thread or waiting for wall-clock time.
        """
        moment = int(now if now is not None else time.time())
        _write_heartbeat(moment)
        fired: List[Dict[str, Any]] = []
        try:
            store.release_stale_locks(moment)
        except Exception as exc:
            log.warning("scheduler lock sweep failed: %s", exc)
        try:
            due = store.list_due(moment)
        except Exception as exc:
            log.warning("scheduler due query failed: %s", exc)
            return fired
        for schedule in due:
            try:
                report = self.fire(schedule, moment)
                if report:
                    fired.append(report)
            except Exception as exc:  # pragma: no cover - one rule must not stop others
                log.warning("scheduler fire failed for %s: %s", schedule.id, exc)
        return fired

    # ── One point ───────────────────────────────────────────────────────────

    def fire(self, schedule: Schedule, now: int) -> Optional[Dict[str, Any]]:
        """Handle one due rule: skip it, catch it up, or start it.

        Returns the started report, or None when nothing was executed (the
        reason is still recorded in the history - a silent skip is exactly
        what makes a scheduler untrustworthy).
        """
        planned = int(schedule.next_run_at or 0)
        if not planned:
            return None

        if schedule.max_runs_per_day is not None:
            try:
                today = store.runs_today(schedule.id, schedule.tz, now)
            except Exception:
                today = 0
            if today >= int(schedule.max_runs_per_day):
                upcoming = next_run_epoch(schedule, now)
                store.record_skipped(schedule.id, planned, reason="daily_limit",
                                     next_run_at=upcoming, now=now)
                return None

        lag = now - planned
        grace = max(0, int(schedule.misfire_grace_seconds or 0))
        if lag > grace:
            # The platform was off (or the rule was paused) while the point
            # came due. Catching up a whole backlog would start a burst of
            # agents, so only ONE point may be recovered and only inside the
            # grace window; everything else is recorded as missed.
            if schedule.misfire_policy == "catch_up_one" and lag <= max(grace, DEFAULT_CATCH_UP_GRACE):
                return self._start(schedule, planned, now)
            upcoming = next_run_epoch(schedule, now)
            store.record_skipped(schedule.id, planned, reason="missed",
                                 next_run_at=upcoming, now=now)
            return None

        if self._overlapping(schedule.id, now):
            if schedule.overlap_policy == "queue_one":
                # Leave the grid alone: the next tick tries the same point
                # again once the previous run is over.
                return None
            upcoming = next_run_epoch(schedule, now)
            store.record_skipped(schedule.id, planned, reason="overlap",
                                 status="skipped_overlap", next_run_at=upcoming,
                                 now=now)
            return None

        return self._start(schedule, planned, now)

    def _overlapping(self, schedule_id: str, now: int) -> bool:
        """True while a previous run of the SAME rule is still working."""
        try:
            return store.active_run(schedule_id) is not None
        except Exception:  # pragma: no cover - a lookup failure must not block runs
            return False

    def _start(self, schedule: Schedule, planned: int, now: int) -> Optional[Dict[str, Any]]:
        """Claim the point atomically and execute it on its own thread.

        The claim happens BEFORE the thread starts, in this thread: a point
        that cannot be claimed must not produce a worker, and a crash between
        the claim and the thread leaves a ``started`` history row that the
        reconcile pass turns into an honest ``interrupted``.
        """
        if schedule.overlap_policy == "cancel_previous":
            try:
                previous = store.active_run(schedule.id)
                if previous and previous.get("run_id"):
                    from core.orchestration.service import get_service
                    get_service().cancel(str(previous["run_id"]), cascade=True)
            except Exception as exc:  # pragma: no cover - cancellation is best effort
                log.warning("scheduler could not cancel the previous run: %s", exc)

        upcoming = next_run_epoch(schedule, now)
        row = store.claim_due(schedule.id, planned, next_run_at=upcoming,
                              trigger="schedule", now=now)
        if row is None:
            # Another tick or another process won the point. Nothing to do:
            # the history row it created is the record of that run.
            return None

        worker = threading.Thread(
            target=self._execute,
            args=(schedule, planned, str(row.get("id") or "")),
            name="sagaai-schedule-%s" % str(schedule.id)[:8],
            daemon=True,
        )
        with self._lock:
            self._workers = [item for item in self._workers if item.is_alive()]
            self._workers.append(worker)
        worker.start()
        return {"schedule_id": schedule.id, "planned_at": planned,
                "run_row_id": row.get("id"), "status": "started"}

    def _execute(self, schedule: Schedule, planned: int, run_row_id: str) -> None:
        """Run one claimed point; never lets an exception escape the thread."""
        try:
            report = runner.run_schedule(schedule, planned, run_row_id=run_row_id)
            log.info("scheduled run of %s finished as %s", schedule.id,
                     report.get("status"))
        except Exception as exc:  # pragma: no cover - runner already guards
            log.warning("scheduled run of %s crashed: %s", schedule.id, exc)
            try:
                store.finish_run(run_row_id, status="failed",
                                 error_code="internal_error",
                                 summary=type(exc).__name__ + ": " + str(exc))
            except Exception:
                pass

    # ── Recovery ────────────────────────────────────────────────────────────

    def reconcile(self, now: Optional[int] = None) -> Dict[str, int]:
        """Repair what a previous process left behind.

        Two leftovers matter. A rule still marked ``running`` with an expired
        claim would never fire again; and a history row stuck at ``started``
        describes a run whose process is gone. Neither is guessed silently:
        the rule is released and the run is recorded as interrupted, which is
        exactly what happened.
        """
        moment = int(now if now is not None else time.time())
        released = 0
        interrupted = 0
        try:
            released = store.release_stale_locks(moment)
        except Exception as exc:
            log.warning("scheduler reconcile: lock release failed: %s", exc)
        try:
            for row in store.unfinished_runs():
                if row.get("run_id"):
                    try:
                        from core.orchestration.service import get_service
                        record = get_service().record_from_store(str(row["run_id"]))
                        if record and record.get("status") not in ("running", "awaiting_input"):
                            store.finish_run(
                                str(row["id"]), status="done",
                                run_id=str(row["run_id"]),
                                thread_id=str(row.get("thread_id") or ""),
                                summary="recovered from the run store",
                            )
                            continue
                    except Exception:
                        pass
                store.mark_run_status(str(row["id"]), "interrupted",
                                      reason="platform_stopped", now=moment)
                interrupted += 1
        except Exception as exc:
            log.warning("scheduler reconcile: run sweep failed: %s", exc)
        return {"released": released, "interrupted": interrupted}


def start_scheduler(tick_seconds: float = TICK_SECONDS) -> Optional[SchedulerDaemon]:
    """Start the process-wide scheduler loop, or return the one running.

    Called once per process at cold start. Streamlit re-executes the entry
    module on every rerun, so the guard is what keeps one loop instead of a
    new thread per interaction.
    """
    global _process_daemon
    with _daemon_lock:
        if _process_daemon is not None and _process_daemon.is_alive():
            return _process_daemon
        daemon = SchedulerDaemon(tick_seconds=tick_seconds)
        daemon.start()
        _process_daemon = daemon
        return daemon


def get_scheduler() -> Optional[SchedulerDaemon]:
    """Return the running loop of this process, or None."""
    current = _process_daemon
    if current is not None and current.is_alive():
        return current
    return None


def stop_scheduler(join_seconds: float = 5.0) -> None:
    """Stop the loop of this process (used on shutdown and in tests)."""
    global _process_daemon
    with _daemon_lock:
        current = _process_daemon
        _process_daemon = None
    if current is not None:
        try:
            current.stop(join_seconds=join_seconds)
        except Exception:
            pass


def wake_scheduler() -> None:
    """Ask a running loop to re-check its due points immediately."""
    current = get_scheduler()
    if current is not None:
        current.wake()


def bootstrap() -> Optional[SchedulerDaemon]:
    """Start the loop at application start-up, never raising into the caller.

    The platform must open even when scheduling cannot work at all - a
    half-migrated database, a read-only install, a broken clock source. An
    exception here would take the whole UI down for a feature the owner may
    not even use, so the failure is logged and the page reports an idle loop
    instead of promising runs that will not happen.

    Returns the running loop, or None when start-up failed.
    """
    try:
        return start_scheduler()
    except Exception as exc:
        log.warning("scheduler bootstrap failed: %s", exc)
        return None


def run_now(schedule: Schedule) -> Optional[Dict[str, Any]]:
    """Start one run of *schedule* immediately, without touching its grid.

    The button exists because "did I configure it right" is answered fastest
    by one real run. It produces exactly what a scheduled point produces -
    same history row, same dialog - and differs only in the trigger, so the
    owner reads manual and automatic runs in one place.
    """
    now = int(time.time())
    row = store.open_manual_run(schedule.id, now, now=now)
    if row is None:
        return None
    worker = threading.Thread(
        target=runner.run_schedule,
        args=(schedule, now),
        kwargs={"run_row_id": str(row.get("id") or ""), "manual": True},
        name="sagaai-schedule-manual",
        daemon=True,
    )
    worker.start()
    return {"run_row_id": row.get("id"), "planned_at": now}


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
