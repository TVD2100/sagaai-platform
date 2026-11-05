"""
core.gateway.runs - what happened to a run, and how to stop or unblock it.

The service already answers these questions; this module turns its record
dicts into transport types and adds the two things a transport cannot do
for itself: a cross-process cancel marker and a pending-input view.

Cancel deserves a note. ``OrchestrationService.cancel`` looks the run up in
an in-process registry; a run started by the Streamlit process is invisible
to the HTTP process and vice versa. The cancel flag file closes that gap
with the same primitive the notice mailbox uses - an ``O_CREAT | O_EXCL``
file - so two writers cannot disagree and a reader cannot miss it.

Nothing here talks to a database directly. Run state lives in the JSON
store of the service; the dialog content lives in the thread module.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.gateway import dto, errors
from core.gateway.dto import CancelOutcome, PendingInput, RunSnapshot

log = logging.getLogger("core.gateway.runs")

#: Directory of cancel marker files inside the run store.
CANCELS_DIR = "cancels"
#: Longest a caller may park a status request. Below the usual proxy idle
#: timeout, so a waiting request is never cut mid-flight.
MAX_WAIT_SECONDS = 25.0
#: Polling interval inside a waiting status request.
WAIT_STEP_SECONDS = 0.25


# ─── Helpers ────────────────────────────────────────────────────────────────


def get_service() -> Any:
    """Return the process-wide orchestration service."""
    from core.orchestration.service import get_service as _get
    return _get()


def store_root() -> str:
    """Return the durable store root of the run records."""
    try:
        return str(get_service().store_root() or "")
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("store root unavailable: %s", exc)
        return ""


def cancels_dir() -> Path:
    """Return the directory that holds cancel marker files."""
    return Path(store_root()) / CANCELS_DIR


def request_cancel(run_id: str) -> bool:
    """Create the cross-process cancel marker for one run.

    Idempotent: the second caller learns that the marker already exists and
    treats it as success. The file is never removed by this module - the
    run's finish path and the store sweeper own its lifetime, and a marker
    deleted too early would resurrect a cancelled run on the next read.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return False
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_"
                   for ch in rid)[:120]
    directory = cancels_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (safe + ".flag")
        handle = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return True
    except OSError as exc:
        log.warning("cancel marker failed for %s: %s", rid, exc)
        return False
    try:
        os.write(handle, str(time.time()).encode("utf-8"))
    except OSError:  # pragma: no cover - writing the timestamp is best effort
        pass
    finally:
        try:
            os.close(handle)
        except OSError:  # pragma: no cover - closing is best effort
            pass
    return True


def cancel_requested(run_id: str) -> bool:
    """Return True when a cancel marker exists for this run."""
    rid = str(run_id or "").strip()
    if not rid:
        return False
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_"
                   for ch in rid)[:120]
    try:
        return (cancels_dir() / (safe + ".flag")).exists()
    except OSError:  # pragma: no cover - defensive
        return False


def _pending_input_of(record: Dict[str, Any]) -> Optional[PendingInput]:
    """Build the pause description of a run record, when it has one."""
    record = record if isinstance(record, dict) else {}
    status = str(record.get("status") or "")
    if status not in ("awaiting_input",):
        return None
    question = record.get("question")
    if isinstance(question, dict) and str(question.get("text") or "").strip():
        return PendingInput(kind="question", question=dict(question),
                            code=str(record.get("error_code") or ""))
    await_reason = str(record.get("await_reason") or "")
    if await_reason == "question":
        return PendingInput(kind="question", code=str(record.get("error_code") or ""))
    if await_reason == "unfinished":
        return PendingInput(kind="unfinished", plan=str(record.get("summary") or ""))
    return PendingInput(kind="unfinished", plan=str(record.get("summary") or ""))


def snapshot_of(record: Dict[str, Any], *, live: bool = False) -> RunSnapshot:
    """Build a ``RunSnapshot`` from a service record.

    ``live`` says whether the record came from the in-memory registry of
    THIS process. A record rebuilt from disk answers ``live=False`` and a
    caller must not read it as "the run died".
    """
    record = record if isinstance(record, dict) else {}
    heartbeat = record.get("heartbeat_at")
    age: Optional[float] = None
    try:
        if heartbeat:
            age = max(0.0, time.time() - float(heartbeat))
    except (TypeError, ValueError):
        age = None
    artifacts = record.get("artifacts") or []
    tokens = record.get("tokens_used") or {}
    nested_raw = record.get("result")
    nested: Dict[str, Any] = nested_raw if isinstance(nested_raw, dict) else {}
    return RunSnapshot(
        run_id=str(record.get("run_id") or ""),
        root_run_id=str(record.get("root_run_id") or ""),
        thread_id=str(record.get("thread_id") or ""),
        parent_run_id=str(record.get("parent_run_id") or ""),
        agent_slug=str(record.get("agent_slug") or ""),
        status=str(record.get("status") or "running"),
        stop_reason=record.get("stop_reason"),
        error_code=record.get("error_code"),
        summary=str(nested.get("summary") or record.get("summary") or ""),
        steps_used=int(record.get("steps_used") or 0),
        tokens_used=dict(tokens) if isinstance(tokens, dict) else {},
        artifacts=[dict(a) if isinstance(a, dict) else {"path": str(a)}
                   for a in artifacts],
        depth=int(record.get("depth") or 0),
        background=bool(record.get("background")),
        live=bool(live),
        heartbeat_age_s=age,
        stale_reason=str(record.get("stale_reason") or ""),
        pending_input=_pending_input_of(record),
        created_at=float(record.get("created_at") or 0.0),
    )


# ─── Public reads ───────────────────────────────────────────────────────────


def get_run(run_id: str) -> RunSnapshot:
    """Return the current state of one run, or raise ``run_not_found``.

    The service heals a dead ``running`` claim on the read path and demotes
    an unverifiable one for this caller only; the record on disk is repaired
    by the startup sweep. Both are wanted here: a status endpoint that
    reported a crashed run as alive would make a client wait forever.
    """
    rid = str(run_id or "").strip()
    if not rid:
        raise errors.invalid_request("run id is required", field="run_id")
    service = get_service()
    try:
        payload = service.poll(rid) or {}
    except Exception as exc:
        log.warning("run poll failed for %s: %s", rid, exc)
        raise errors.GatewayError(errors.ERR_STORE_UNAVAILABLE,
                                  "The run store is unavailable")
    if not payload.get("ok"):
        raise errors.run_not_found(rid)
    record = dict(payload.get("run") or {})
    return snapshot_of(record, live=bool(payload.get("live")))


def wait_run(run_id: str, *, wait_seconds: float = 0.0) -> RunSnapshot:
    """Return the state of a run, waiting a bounded time for a change.

    The wait is clamped: a long-poll that outlives the proxy in front of it
    turns a working run into a broken connection. The caller learns whether
    the wait expired from ``status`` staying non-terminal, not from an error.
    """
    try:
        seconds = float(wait_seconds or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    seconds = max(0.0, min(MAX_WAIT_SECONDS, seconds))
    snapshot = get_run(run_id)
    if seconds <= 0 or snapshot.is_terminal:
        return snapshot
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(WAIT_STEP_SECONDS)
        try:
            snapshot = get_run(run_id)
        except errors.GatewayError:
            return snapshot
        if snapshot.is_terminal:
            return snapshot
    return snapshot


def list_runs(*, thread_id: str = "", root_run_id: Optional[str] = None,
              limit: int = 50) -> List[RunSnapshot]:
    """Return recent runs, newest first."""
    service = get_service()
    try:
        records = list(service.list_runs(root_run_id) or [])
    except Exception as exc:
        log.warning("run listing failed: %s", exc)
        return []
    out: List[RunSnapshot] = []
    wanted = str(thread_id or "").strip()
    for record in records:
        item = snapshot_of(record)
        if wanted and item.thread_id != wanted:
            continue
        out.append(item)
        if len(out) >= max(1, int(limit or 50)):
            break
    return out


# ─── Control ────────────────────────────────────────────────────────────────


def cancel_run(run_id: str, *, cascade: bool = True) -> "CancelOutcome":
    """Cancel a run in this process and mark it for any other one.

    Two mechanisms on purpose: the in-process flag stops a run this process
    owns at its next step boundary, the marker file does the same for a run
    owned by a process this one cannot reach. Writing both is what makes the
    answer true regardless of which process serves the request.
    """
    rid = str(run_id or "").strip()
    if not rid:
        raise errors.invalid_request("run id is required", field="run_id")
    service = get_service()
    marker = request_cancel(rid)
    try:
        result = service.cancel(rid, cascade=bool(cascade))
        outcome = dto.CancelOutcome.from_report(result, marker_written=bool(marker))
    except Exception as exc:
        log.warning("in-process cancel failed for %s: %s", rid, exc)
        outcome = dto.CancelOutcome(run_id=rid, cancelled=False,
                                    reason="unknown_run",
                                    marker_written=bool(marker))
    if not outcome.cancelled and not marker:
        # Neither a live record nor a marker: the run truly does not exist.
        raise errors.run_not_found(rid)
    return outcome


def answer_run(run_id: str, answer: str) -> "RunSnapshot":
    """Deliver an answer to a parked question and return the new state.

    Only a run parked on ``awaiting_question`` can be answered; anything else
    answers ``nothing_to_answer`` instead of silently doing nothing.
    ``awaiting_input`` without a question is a run that stopped short of its
    goal and needs ``resume_run``, not an answer.

    The returned snapshot is the state AFTER delivery: a caller that needs to
    know what to do next - keep polling, or stop - reads it from here instead
    of issuing a second status call.
    """
    rid = str(run_id or "").strip()
    text = str(answer or "").strip()
    if not rid:
        raise errors.invalid_request("run id is required", field="run_id")
    if not text:
        raise errors.invalid_request("answer is required", field="answer")
    service = get_service()
    snapshot = get_run(rid)
    if snapshot.status != "awaiting_input":
        raise errors.GatewayError("nothing_to_answer",
                                  "The run is not waiting for an answer",
                                  details={"run_id": rid,
                                           "status": snapshot.status})
    try:
        service.answer_run(rid, text)
    except Exception as exc:
        log.warning("answer delivery failed for %s: %s", rid, exc)
        raise errors.GatewayError("nothing_to_answer", str(exc),
                                  details={"run_id": rid})
    return get_run(rid)


def can_answer(snapshot: RunSnapshot) -> bool:
    """Return True when the run waits for an answer a caller can give."""
    if snapshot.status != "awaiting_input":
        return False
    pending = snapshot.pending_input
    return bool(pending and pending.kind == "question")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
