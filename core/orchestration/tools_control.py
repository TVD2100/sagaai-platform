# -*- coding: utf-8 -*-
"""core.orchestration.tools_control - stopping and continuing runs.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.orchestration.contracts import (
    ERR_INTERNAL_ERROR,
    ERR_INVALID_ARGS,
    ERR_PROVIDER_ERROR,
    ERR_RESUME_UNAVAILABLE,
    AgentBrief,
    error_result,
)
from core.orchestration.tools_notes import (
    _attach_note,
    _note_for_payload,
)
from core.orchestration.tools_records import (
    _confine_run,
)


def cancel_subagent(run_id: Optional[str] = None, *,
                     cascade: bool = True,
                     reason: str = "",
                     parent_slug: str = "dev_agent",
                     parent_run_id: Optional[str] = None,
                     service=None) -> Dict[str, Any]:
    """Stop a running child, optionally together with its descendants.

    Stopping work is the parent's decision, not the child's: a child that
    misunderstands the task, loops on a wrong path or proves unnecessary must
    be stoppable from the dialog that launched it. Without this tool the only
    way out was to wait for the timeout.

    ``cascade`` also cancels every descendant of the run, which is what a
    parent usually wants - a grandchild working for a cancelled child is dead
    weight. The cancellation is a FLAG checked at step boundaries: a tool call
    already in flight finishes first, because a Python thread cannot be killed
    safely.

    ``reason`` travels into the notice so the model sees why the work stopped.
    """
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    rid = str(run_id or "").strip()
    if not rid:
        return error_result("", "cancel_subagent needs run_id: the run to stop.",
                            ERR_INVALID_ARGS, missing=["run_id"])

    # Confinement: a caller may only cancel its own descendants. A dialog-level
    # call owns its tree; a child may stop what it started, nothing else.
    checked = _confine_run(service, rid, parent_run_id)
    if not checked.get("ok"):
        return checked

    try:
        report = service.cancel(rid, cascade=bool(cascade))
    except Exception as exc:  # pragma: no cover - defensive
        return error_result(rid, "Cancellation failed: " + str(exc),
                            ERR_INTERNAL_ERROR)

    payload: Dict[str, Any] = {"ok": True, "run_id": rid}
    payload.update(report.to_dict())
    if reason:
        payload["reason_text"] = str(reason)
    if not report.cancelled:
        payload["note"] = ("Nothing to cancel: the run is already "
                           + str(report.previous_status) + ".")
    elif cascade and report.children:
        payload["note"] = (str(len(report.children)) + " descendant run(s) were "
                           "cancelled with it. Cancellation is a flag checked "
                           "at step boundaries: a tool call already in flight "
                           "finishes before the child stops.")
    else:
        payload["note"] = ("Cancellation requested. The child stops at its next "
                           "step boundary; a tool call already in flight "
                           "finishes first.")
    return payload

def resume_subagent(run_id: Optional[str] = None, *,
                    request: Optional[AgentBrief] = None,
                    background: bool = False,
                    parent_slug: str = "dev_agent",
                    parent_run_id: Optional[str] = None,
                    thread_id: str = "",
                    parent_config: Optional[Dict[str, Any]] = None,
                    base_dir: Optional[str] = None,
                    lang: Optional[str] = None,
                    service=None,
                    enable_injection_protection: Optional[bool] = None,
                    ) -> Dict[str, Any]:
    """Continue a finished child run from its own journal.

    The new run inherits the tree of the original (see ``resume_run_id`` in the
    service), so resuming never resets the depth or the total-run counter, and
    the chain is capped by ``MAX_RESUME_CHAIN``. ``request`` is optional: a
    caller that supplies nothing continues the original goal with the built
    continuation hint, which is the common case ("the child stopped at a
    question, let it continue").
    """
    from core.orchestration.resume import build_resume

    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    rid = str(run_id or "").strip()
    if not rid:
        return error_result("", "resume_subagent needs run_id: the run to continue.",
                            ERR_INVALID_ARGS, missing=["run_id"])

    record = None
    live = service.get_record(rid)
    if live is not None:
        record = live.to_record()
    if record is None:
        record = service.record_from_store(rid)
    if record is None:
        return error_result(rid, "Nothing to resume: run " + rid + " is unknown.",
                            ERR_RESUME_UNAVAILABLE)

    try:
        from core.orchestration.resume import read_checkpoint
        from dev_agent.task_state import read_task_state_at
        built = build_resume(record, read_state=read_task_state_at,
                             read_checkpoint=read_checkpoint)
    except Exception as exc:  # pragma: no cover - defensive
        return error_result(rid, "Resume planning failed: " + str(exc),
                            ERR_PROVIDER_ERROR)
    if not built.get("ok"):
        payload = error_result(rid,
                               str(built.get("error") or "Nothing to resume."),
                               str(built.get("error_code") or ERR_RESUME_UNAVAILABLE))
        payload.update({k: v for k, v in built.items()
                        if k not in ("ok", "error", "error_code")})
        code = str(built.get("error_code") or "")
        if code == "resume_chain_limit":
            payload = _attach_note(
                payload, "The resume chain reached its ceiling: continuing this "
                         "line of work needs a fresh spawn with a narrowed brief "
                         "or a decision from the user, not another resume.")
        elif code == ERR_RESUME_UNAVAILABLE:
            payload = _attach_note(
                payload, "There is no journal to continue from. Start a fresh "
                         "spawn and put everything the child needs into goal and "
                         "brief - a resume cannot invent the missing state.")
        else:
            payload = _attach_note(
                payload, "Resume was refused. Read the code above: a retry with "
                         "the same request will be refused the same way.")
        return payload

    agent = str((request.goal if request else "") or "").strip()
    if request is not None and agent:
        brief = request
    else:
        original = request or AgentBrief(goal=str(built.get("goal") or ""))
        brief = AgentBrief(
            goal=str(original.goal or built.get("goal") or "Continue the task."),
            brief=str(built.get("resume_hint") or ""),
            artifacts_in=dict(getattr(original, "artifacts_in", {}) or {}),
        )

    # The facade owns the name: a test replaces it there, and a local
    # binding would freeze the original function at import time.
    from core.orchestration import tools as facade
    return _note_for_payload(facade.spawn_agent(
        agent_slug=str(record.get("agent_slug") or "") or parent_slug,
        request=brief,
        background=background,
        resume_run_id=rid,
        parent_slug=parent_slug,
        parent_run_id=parent_run_id,
        thread_id=thread_id,
        parent_config=parent_config,
        base_dir=base_dir,
        lang=lang,
        service=service,
        enable_injection_protection=enable_injection_protection,
    ))

def verify_result(
    run_id: Optional[str] = None,
    criteria: str = "",
    *,
    parent_slug: str = "dev_agent",
    parent_run_id: Optional[str] = None,
    thread_id: str = "",
    parent_config: Optional[Dict[str, Any]] = None,
    base_dir: Optional[str] = None,
    lang: Optional[str] = None,
    service=None,
) -> Dict[str, Any]:
    """Run the configured verifier against one finished run.

    A thin entry point: the whole contract - the refusal order, the brief that
    hides the transcript and the verdict parser - lives in
    ``core.orchestration.verify``. Keeping the tool layer thin is what makes
    the boundary testable: this function turns model arguments into a call and
    nothing else, so a change here cannot quietly change what a verifier is
    allowed to see.
    """
    from core.orchestration.verify import verify_result as _verify
    return _verify(
        str(run_id or ""),
        criteria,
        parent_slug=parent_slug,
        parent_run_id=parent_run_id,
        thread_id=thread_id,
        parent_config=parent_config,
        base_dir=base_dir,
        lang=lang,
        service=service,
    )

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
