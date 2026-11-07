# -*- coding: utf-8 -*-
"""core.orchestration.tools_wait - waiting for children.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.orchestration import tools_clock as clock
from core.orchestration.contracts import (
    ERR_INVALID_ARGS,
    TERMINAL_STATUSES,
    error_result,
)
from core.orchestration.tools_constants import (
    MAX_SLEEP_SECONDS,
)


def sleep_tool(
    seconds: Optional[float] = None,
    until: str = "any_child",
    *,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    root_run_id: Optional[str] = None,
    service=None,
) -> Dict[str, Any]:
    """Block until a child finishes or the timeout expires.

    The wait is a POLL, and it is called a poll on purpose: the orchestration
    core has no condition variable, so every tick reads the terminal runs of
    the tree and asks the mailbox whether anything arrived. For a handful of
    children both reads cost milliseconds next to the wait itself.

    The mailbox is only INSPECTED, never drained: notices are the model's
    delivery channel, and a ``sleep`` that emptied them would steal the notice
    from the next step.

    An event is one of two things: a run of the tree became terminal during
    the wait (its id is reported in ``finished``), or the tree mailbox holds a
    pending notice. Only identifiers are returned - the body of a result is
    read with ``subagent_status`` / ``read_subagent_state``, so this tool
    answers "who finished" without becoming a second result channel.

    A timeout does NOT cancel anything: it ends the wait, not the work.

    ``root_run_id`` is the tree the mailbox and the run list hang off. A child
    does not know it (it only knows its parent), so the root is resolved here
    from the parent record - an unknown key would make ``has_notifications``
    answer ``False`` instead of failing, a silent miss.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()

    if seconds is None:
        return error_result(
            "", "sleep needs seconds: the ceiling of the wait.",
            ERR_INVALID_ARGS, missing=["seconds"])
    try:
        wait = float(seconds)
    except (TypeError, ValueError):
        return error_result(
            "", "seconds must be a number of seconds.",
            ERR_INVALID_ARGS, invalid=["seconds"])
    if wait <= 0:
        return error_result(
            "", "seconds must be greater than zero.",
            ERR_INVALID_ARGS, invalid=["seconds"])

    mode = str(until or "any_child").strip().lower() or "any_child"
    if mode not in ("any_child", "timeout", "all_children"):
        return error_result(
            "", "until must be any_child, all_children or timeout.",
            ERR_INVALID_ARGS, invalid=["until"],
            allowed=["any_child", "all_children", "timeout"])

    clamped = wait > MAX_SLEEP_SECONDS
    if clamped:
        wait = MAX_SLEEP_SECONDS

    parent = str(parent_run_id or "").strip()
    root = str(root_run_id or "").strip()
    if not root and parent:
        try:
            rec = service.get_record(parent)
        except Exception:  # pragma: no cover - registry is best effort
            rec = None
        root = str(getattr(rec, "root_run_id", "") or "").strip() or parent

    track_events = mode == "any_child" and bool(root)
    snapshot = _terminal_run_ids(service, root, {parent}) if track_events else set()
    # ``all_children`` waits for the WHOLE fan-out: a parent that spawned five
    # auditors wants the batch, not a turn per child. The wait ends when no
    # non-terminal child of the tree remains, so a child that finished before
    # the call started does not shorten it and one that was cancelled mid-way
    # releases it just the same.
    track_all = mode == "all_children" and bool(root)
    finished: List[str] = []
    woken_by = "timeout"
    if track_all:
        remaining = _active_run_ids(service, root, {parent})
        if not remaining:
            return {"ok": True, "slept": 0.0, "woken_by": "all_done",
                    "finished": [], "clamped": False}

    started = clock.monotonic()
    deadline = started + wait
    interval = 0.2
    while True:
        if parent:
            try:
                cancelled = bool(service.is_cancelled(parent))
            except Exception:  # pragma: no cover - a wait must never raise
                cancelled = False
            if cancelled:
                woken_by = "cancelled"
                finished = []
                break

        if track_all and not _active_run_ids(service, root, {parent}):
            # The whole fan-out is quiet: that is a distinct outcome from one
            # child finishing, and the model acts on it differently.
            woken_by = "all_done"
            finished = sorted(_terminal_run_ids(service, root, {parent}))
            break

        if track_events:
            current = _terminal_run_ids(service, root, {parent})
            arrived = sorted(current - snapshot)
            if arrived:
                woken_by = "event"
                finished = arrived
                break

        if track_events:
            try:
                noticed = bool(service.has_notifications(root))
            except Exception:  # pragma: no cover - a wait must never raise
                noticed = False
            if noticed:
                # A notice without a new terminal run still means the tree has
                # news for the model. The set is re-read here so the answer is
                # not a snapshot taken before the mailbox fired; the ids stay
                # empty when nothing new finished inside this wait.
                finished = sorted(
                    _terminal_run_ids(service, root, {parent}) - snapshot)
                woken_by = "event"
                break

        now = clock.monotonic()
        if now >= deadline:
            woken_by = "timeout"
            break
        clock.sleep(min(interval, deadline - now))
        interval = min(1.0, interval * 2)

    return {"ok": True, "slept": round(clock.monotonic() - started, 1),
            "woken_by": woken_by, "finished": finished, "clamped": clamped}

def _terminal_run_ids(service: Any, root: str,
                      exclude: Optional[set] = None) -> set:
    """Return the ``run_id`` values of the tree that are already terminal.

    A tree read is best effort: a broken store must not turn a wait into an
    exception, and an empty answer simply means "nothing finished yet".
    ``exclude`` keeps the caller's own run out of the set - a sleep waits for
    children, and counting itself would fire an event on the first tick.
    """
    skip = set(exclude or ())
    try:
        runs = service.list_records(root) or []
    except Exception:  # pragma: no cover - a wait must never raise
        return set()
    out = set()
    for item in runs:
        rid = str((item or {}).get("run_id") or "").strip()
        if not rid or rid in skip:
            continue
        if str((item or {}).get("status") or "") in TERMINAL_STATUSES:
            out.add(rid)
    return out

def _active_run_ids(service: Any, root: str,
                    exclude: Optional[set] = None) -> set:
    """Return the ``run_id`` values of the tree that are still not terminal.

    The root record stands for the whole tree, so it is always excluded: while
    the dialog is open it is legitimately running, and counting it would make
    ``all_children`` wait for the very run that is doing the waiting.
    """
    skip = set(exclude or ())
    try:
        runs = service.list_records(root) or []
    except Exception:  # pragma: no cover - a wait must never raise
        return set()
    out = set()
    for item in runs:
        rid = str((item or {}).get("run_id") or "").strip()
        if not rid or rid in skip:
            continue
        if str((item or {}).get("status") or "") not in TERMINAL_STATUSES:
            out.add(rid)
    return out

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
