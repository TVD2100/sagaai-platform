"""
server.routes.runs - what happened to a run, and how to stop or unblock it.

The status endpoint is the ONLY way a caller learns the outcome of a launch:
the launch itself answered ``202``. Two properties matter more than the
shape of the body:

- a run that no longer exists answers ``404`` and never a stale ``running``;
- a run that exists but belongs to another process answers ``live: false``
  with a heartbeat age, which is a FACT, not an error. A client that reads
  it as a failure would give up on work that is still going.

The long-poll variant (``?wait=N``) exists because polling every two seconds
for an hour is wasteful. It is capped well below the usual proxy idle
timeout, so a waiting request is never cut mid-flight.

Handlers return the response MODEL, not its dumped form: FastAPI serialises
it once against ``response_model``.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from core.gateway import errors as gateway_errors
from core.gateway import runs as gateway_runs
from core.gateway.dto import CancelOutcome
from server import deps, schemas
from server.errors import error_responses

log = logging.getLogger("server.routes.runs")

router = APIRouter(tags=["runs"])


# ─── Reads ──────────────────────────────────────────────────────────────────


@router.get("/runs/{run_id}", response_model=schemas.RunOut,
            responses=error_responses(),
            summary="State of one run")
def get_run(run_id: str,
            wait: float = Query(default=0.0,
                                description="Seconds to wait for a change (max 25)."),
            scope: List[str] = Depends(deps.caller_scope)
            ) -> schemas.RunOut:
    """Return the state of a run, optionally waiting for it to change."""
    run = deps.run_id_path(run_id)
    started = time.monotonic()
    if wait and float(wait) > 0:
        snapshot = gateway_runs.wait_run(run, wait_seconds=wait)
        waited_ms = int((time.monotonic() - started) * 1000)
    else:
        snapshot = gateway_runs.get_run(run)
        waited_ms = None
    _cleanup_if_finished(snapshot)
    return schemas.run_out(snapshot, waited_ms=waited_ms)


def _cleanup_if_finished(snapshot) -> None:
    """Drop a terminal run from the process bookkeeping.

    The admission slot itself is released by the close listener the gateway
    calls when the driver finishes, so a client that never polls cannot leak
    one. This call only keeps the remembered-id set from growing; it is
    idempotent and safe to call on every read.
    """
    if not getattr(snapshot, "is_terminal", False):
        return
    try:
        from server import app as server_app
        server_app.forget_run(snapshot.run_id)
    except Exception:  # pragma: no cover - bookkeeping is best effort
        pass


@router.get("/runs", response_model=schemas.RunListOut,
            responses=error_responses(),
            summary="Recent runs")
def list_runs(thread_id: str = Query(default=""),
              root: Optional[str] = Query(default=None),
              limit: int = Query(default=50, ge=1, le=200),
              scope: List[str] = Depends(deps.caller_scope)
              ) -> schemas.RunListOut:
    """Return recent runs, optionally narrowed to one thread or tree."""
    items = gateway_runs.list_runs(thread_id=thread_id, root_run_id=root,
                                   limit=limit)
    payload = [schemas.run_out(item) for item in items]
    return schemas.RunListOut(items=payload, total=len(payload))


# ─── Control ────────────────────────────────────────────────────────────────


@router.post("/runs/{run_id}/cancel", status_code=202,
             response_model=schemas.CancelOut,
             responses=error_responses(),
             summary="Cancel a run")
def cancel_run(run_id: str, body: Optional[schemas.CancelIn] = None,
               scope: List[str] = Depends(deps.caller_scope)
               ) -> schemas.CancelOut:
    """Ask a run to stop at its next step boundary.

    ``202`` and not ``200``: cancellation is asynchronous by nature. A step
    is a provider call plus its tools, and a Python thread cannot be
    interrupted safely in the middle of one. The client is told the request
    was accepted; the status endpoint tells it when the run actually stopped.
    """
    run = deps.run_id_path(run_id)
    cascade = True if body is None else bool(body.cascade)
    try:
        outcome = gateway_runs.cancel_run(run, cascade=cascade)
    except gateway_errors.GatewayError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("cancel failed for %s: %s", run, exc)
        raise gateway_errors.GatewayError(
            "internal_error", "The cancellation could not be requested")
    return _cancel_out(outcome, run)


def _cancel_out(outcome, run: str) -> schemas.CancelOut:
    """Turn a cancellation outcome into the response model.

    Both a ``CancelOutcome`` and a plain mapping are accepted: a test that
    replaces the gateway function is not obliged to build the dataclass, and
    the shape of the answer does not depend on which one arrived.
    """
    if not isinstance(outcome, CancelOutcome):
        outcome = CancelOutcome.from_report(outcome)
    return schemas.CancelOut(
        run_id=str(outcome.run_id or run),
        cancelled=bool(outcome.cancelled),
        previous_status=outcome.previous_status or "running",
        reason=outcome.reason or "requested",
        children=[str(item) for item in outcome.children],
        marker_written=bool(outcome.marker_written),
    )


@router.post("/runs/{run_id}/answer", status_code=202,
             response_model=schemas.RunOut,
             responses=error_responses(),
             summary="Answer a parked question")
def answer_run(run_id: str, body: schemas.AnswerIn,
               scope: List[str] = Depends(deps.caller_scope)
               ) -> schemas.RunOut:
    """Deliver an answer to a run that parked on a question.

    Only a run waiting on a QUESTION can be answered. A run that stopped
    short of its goal (``awaiting_input`` without a question) needs a new
    message, not an answer, and saying so is more useful than accepting the
    answer silently.
    """
    run = deps.run_id_path(run_id)
    if not str(body.answer or "").strip():
        raise gateway_errors.invalid_request("answer is required",
                                             field="answer")
    snapshot = gateway_runs.answer_run(run, body.answer)
    _cleanup_if_finished(snapshot)
    return schemas.run_out(snapshot)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
