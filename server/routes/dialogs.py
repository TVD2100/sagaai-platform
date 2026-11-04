"""
server.routes.dialogs - launch a dialog, and continue one.

The endpoint answers ``202 Accepted`` and hands back a run id. That is not a
shortcut: a dialog step is one or more provider calls and their tools, so a
synchronous answer would keep an HTTP connection open for minutes, which
proxies, mobile networks and load balancers cut. The caller learns the
outcome from ``GET /v1/runs/{id}``.

The work that happens BEFORE the answer is deliberately minimal and fixed:
validate the agent, refuse a second live run on the same thread, take an
admission slot, create the thread, register the run, start the driver. Each
step is milliseconds; none of them calls a model. Everything a caller can
get wrong is refused synchronously with a real status code, so a run id is
never handed out for work that cannot start.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, Response

from core.gateway import dialog as gateway_dialog
from core.gateway import errors as gateway_errors
from core.gateway.dto import DialogRequest, EconomyOptions
from server import deps, schemas

log = logging.getLogger("server.routes.dialogs")

router = APIRouter(tags=["dialogs"])

#: How soon a client should look at the run for the first time.
POLL_AFTER_MS = 2000


def _admission():
    """Return the admission controller of the running application.

    Imported lazily because ``server.app`` imports this module to register
    the router; a module-level import would close the cycle at import time.
    """
    from server import app as server_app
    return server_app


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _accepted(handle) -> dict:
    """Build the body of a ``202`` answer."""
    run_id = handle.run_id
    return schemas.DialogAcceptedOut(
        run_id=run_id,
        thread_id=handle.thread_id,
        status=handle.status,
        agent_slug=handle.agent_slug,
        workspace=handle.workspace,
        mode=handle.mode,
        created_at=_now_iso(),
        poll_after_ms=POLL_AFTER_MS,
        links=schemas.RunLinksOut(
            self="/v1/runs/" + run_id,
            cancel="/v1/runs/" + run_id + "/cancel",
            events=None,
        ),
    ).model_dump()


def _request_of(body: schemas.DialogIn) -> DialogRequest:
    """Convert the validated body into a gateway request."""
    return DialogRequest(
        agent_slug=body.agent_slug,
        message=body.message,
        workspace=body.workspace,
        target_file=body.target_file,
        thread_id=body.thread_id,
        title=body.title,
        mode=body.mode or "auto",
        web_search=bool(body.web_search),
        safety=bool(body.safety),
        auto_apply=body.auto_apply,
        max_steps=int(body.max_steps or 0),
        deadline_seconds=int(body.deadline_seconds or 0),
        budget_tokens=int(body.budget_tokens or 0),
        file_context=body.file_context,
        file_name=body.file_name,
        economy=EconomyOptions(),
    )


@router.post("/dialogs", status_code=202,
             response_model=schemas.DialogAcceptedOut,
             summary="Start a dialog")
def start_dialog(body: schemas.DialogIn, response: Response,
                 scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Create and launch a dialog; answer ``202`` with the run id."""
    server_app = _admission()
    request = _request_of(body)

    if not server_app.STATE.try_admit():
        raise gateway_errors.too_many_runs(
            active=server_app.STATE.active_runs,
            limit=server_app.server_app_limit(),
        )

    try:
        ready = _ready_or_refuse()
        if ready is not None:
            raise ready
        handle = gateway_dialog.start_dialog(request, allowed_slugs=scope)
    except Exception:
        server_app.STATE.release()
        raise

    server_app.note_run(handle.run_id)
    response.headers["Location"] = "/v1/runs/" + handle.run_id
    return _accepted(handle)


def _ready_or_refuse():
    """Return a ``GatewayError`` when the schema is not ready, else None.

    Checked AFTER admission and before creation: a caller that is going to
    be refused must not leave a thread behind, and must not consume an
    admission slot for longer than the check takes.
    """
    try:
        from core.gateway import health as gateway_health
        report = gateway_health.ready()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("readiness probe failed: %s", exc)
        return None
    if report.ready:
        return None
    return gateway_errors.db_not_ready(missing=list(report.missing),
                                       hint=report.hint)


@router.post("/dialogs/{run_id}/messages", status_code=202,
             response_model=schemas.DialogAcceptedOut,
             summary="Send a message into an existing dialog")
def send_message(run_id: str, body: schemas.MessageIn, response: Response,
                 scope: List[str] = Depends(deps.caller_scope)) -> dict:
    """Append a message and run the next step of the dialog."""
    run = deps.run_id_path(run_id)
    request = schemas.MessageIn(message=body.message)
    if not str(request.message or "").strip():
        raise gateway_errors.invalid_request("message is required",
                                             field="message")
    server_app = _admission()
    if not server_app.STATE.try_admit():
        raise gateway_errors.too_many_runs(
            active=server_app.STATE.active_runs,
            limit=server_app.server_app_limit(),
        )
    try:
        ready = _ready_or_refuse()
        if ready is not None:
            raise ready
        result = gateway_dialog.submit_message(run, request.message,
                                               allowed_slugs=scope)
    except Exception:
        server_app.STATE.release()
        raise
    server_app.note_run(run)
    response.headers["Location"] = "/v1/runs/" + run
    return schemas.DialogAcceptedOut(
        run_id=run,
        thread_id=result.thread_id,
        status="running",
        agent_slug="",
        workspace="",
        mode="auto",
        created_at=_now_iso(),
        poll_after_ms=POLL_AFTER_MS,
        links=schemas.RunLinksOut(
            self="/v1/runs/" + run,
            cancel="/v1/runs/" + run + "/cancel",
            events=None,
        ),
    ).model_dump()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
