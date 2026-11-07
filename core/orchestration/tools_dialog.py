# -*- coding: utf-8 -*-
"""core.orchestration.tools_dialog - the question protocol.

Split out of ``core.orchestration.tools``: the public path of every name
below stays the same, the facade module re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.orchestration import tools_clock as clock
from core.orchestration.contracts import (
    AWAIT_QUESTION,
    DEFAULT_ANSWER_MAX_CHARS,
    DEFAULT_ANSWER_WAIT_SECONDS,
    DEFAULT_DIALOG_ROUNDS,
    DEFAULT_MAX_PARKED_RUNS,
    DEFAULT_QUESTION_MAX_CHARS,
    ERR_INVALID_ARGS,
    STATUS_AWAITING_INPUT,
    AgentQuestion,
    error_result,
)
from core.orchestration.tools_notes import (
    _note_for_payload,
)


def ask_parent(question: str, options: Any = None, done: str = "",
               blocked: str = "", *, run_id: str = "", parent_slug: str = "",
               parent_config: Optional[Dict[str, Any]] = None,
               service: Any = None, state: Any = None) -> Dict[str, Any]:
    """Park the current run on a question to the agent that started it.

    A child cannot reach a human: only the parent that briefed it can answer,
    and the parent may itself ask the user. The call therefore does two
    things at once - it records the question on the run (durable, readable
    after a restart) and tells the loop to stop at this step boundary, so the
    parent gets control back and can reply.

    Refusals are decided HERE, before anything is parked: an empty or
    oversized question, a spent dialog budget and too many parked siblings
    all leave the run running, so the child can carry on with an assumption
    instead of waiting for an answer that will never come.
    """
    rid = (run_id or "").strip()
    if not rid:
        return error_result("", "A question needs a run to park: the loop reported "
                           "no run id.", ERR_INVALID_ARGS)
    text = str(question or "").strip()
    if not text:
        return error_result(rid, "The question is empty. State what you cannot "
                           "decide and what you need in order to continue.",
                           ERR_INVALID_ARGS)
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    # A top-level dialog has nobody above it: parking it on a question would
    # stop the very run the user is waiting for, and no tool could answer it.
    # The child is the only role that may ask, so the refusal names the tool
    # to use instead.
    record = service.get_record(rid)
    if record is not None and not record.parent_run_id:
        return error_result(
            rid,
            "This run is a top-level dialog: there is no parent agent to ask. "
            "Ask the USER in your reply instead - end the turn with the "
            "question and loop_status awaiting_user.",
            ERR_INVALID_ARGS)
    limits = read_dialog_limits(parent_config)
    if len(text) > limits["question_max_chars"]:
        return error_result(
            rid,
            "The question is too long (" + str(len(text)) + " > "
            + str(limits["question_max_chars"]) + " characters). Keep the "
            "question itself short and put the details into 'done' and "
            "'blocked'.",
            ERR_INVALID_ARGS, limit="question_max_chars",
            value=limits["question_max_chars"])
    opts: List[str] = []
    if isinstance(options, (list, tuple)):
        opts = [str(item) for item in options if str(item or "").strip()][:8]
    elif isinstance(options, str) and options.strip():
        opts = [options.strip()]
    try:
        asker = str(parent_slug or "")
        record = service.get_record(rid)
        if record is not None:
            asker = "subagent of " + (record.parent_run_id or "parent")
    except Exception:  # pragma: no cover - naming is cosmetic
        asker = str(parent_slug or "")
    now = clock.wall_time()
    agent_question = AgentQuestion(
        text=text, options=opts, done=str(done or "")[:1000],
        blocked=str(blocked or "")[:1000], asked_at=now,
        deadline_at=now + int(limits["answer_wait_seconds"]))
    parked = service.park_run(rid, agent_question, limits=limits, state=state)
    if not parked.get("ok"):
        return parked
    if state is not None:
        try:
            state.parked_question = agent_question.to_dict()
        except Exception:  # pragma: no cover - state is best effort
            pass
    return {
        "ok": True,
        "parked": True,
        "run_id": rid,
        "status": STATUS_AWAITING_INPUT,
        "await_reason": AWAIT_QUESTION,
        "dialog_rounds": parked.get("dialog_rounds"),
        "dialog_rounds_left": max(0, int(parked.get("limit") or 0)
                                  - int(parked.get("dialog_rounds") or 0)),
        "question": agent_question.to_dict(),
        "note": ("Your run is parked until the parent answers: stop this turn "
                 "and do not repeat the question. The answer arrives as a "
                 "user message and your work continues from here. Rounds left "
                 "after this question: "
                 + str(max(0, int(parked.get("limit") or 0)
                           - int(parked.get("dialog_rounds") or 0))) + "."),
        "asked_by": asker,
    }

def answer_child(run_id: str, answer: str, *, parent_slug: str = "",
                 parent_run_id: Optional[str] = None, thread_id: str = "",
                 parent_config: Optional[Dict[str, Any]] = None,
                 base_dir: Optional[str] = None, lang: Optional[str] = None,
                 service: Any = None) -> Dict[str, Any]:
    """Answer a parked subagent and let it continue the SAME run.

    The reply is delivered as the parent's answer to the child's question, the
    child loop continues from the step it stopped at, and the call returns the
    child result - or the child's NEXT question when it asks again.
    """
    rid = (run_id or "").strip()
    if not rid:
        return error_result("", "answer_child needs the run_id of the parked "
                           "subagent.", ERR_INVALID_ARGS)
    text = str(answer or "").strip()
    if not text:
        return error_result(rid, "The answer is empty. Tell the subagent what "
                           "to do next.", ERR_INVALID_ARGS)
    if service is None:
        from core.orchestration.service import get_service
        service = get_service()
    caller_root = ""
    try:
        caller = service.get_record((parent_run_id or "")) if parent_run_id else None
        caller_root = (caller.root_run_id if caller is not None
                       else (thread_id or parent_run_id or ""))
    except Exception:  # pragma: no cover - confinement is checked again below
        caller_root = thread_id or ""
    payload = service.continue_run(
        rid, text, parent_config=parent_config, base_dir=base_dir, lang=lang,
        caller_root_run_id=caller_root)
    payload = _note_for_payload(payload)
    if payload.get("ok") and payload.get("parked"):
        payload["note"] = ("The subagent asked another question instead of "
                           "finishing: answer it again, or reply with a "
                           "directive to complete the work with the "
                           "information it already has.")
    return payload

def read_dialog_limits(config: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Return the dialog limits of a config, with the documented defaults.

    Kept as a thin wrapper so the tools never import the service module just
    to read five numbers: a broken config must degrade to safe defaults here
    too, not raise.
    """
    try:
        from core.orchestration.service import read_limits
        limits = read_limits(config)
    except Exception:  # pragma: no cover - defaults are safe by definition
        limits = {}
    return {
        "dialog_rounds": int(limits.get("dialog_rounds") or DEFAULT_DIALOG_ROUNDS),
        "question_max_chars": int(limits.get("question_max_chars")
                                  or DEFAULT_QUESTION_MAX_CHARS),
        "answer_max_chars": int(limits.get("answer_max_chars")
                                or DEFAULT_ANSWER_MAX_CHARS),
        "answer_wait_seconds": int(limits.get("answer_wait_seconds")
                                   or DEFAULT_ANSWER_WAIT_SECONDS),
        "max_parked_runs": int(limits.get("max_parked_runs")
                               or DEFAULT_MAX_PARKED_RUNS),
    }

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
