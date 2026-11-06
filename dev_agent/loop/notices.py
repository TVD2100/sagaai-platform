# -*- coding: utf-8 -*-
"""
dev_agent.loop.notices -- delivery of finished-subagent notices to the parent.

A background child never writes into the dialog history: that structure
belongs to the script run that drives the loop, and a worker thread touching
it is a race at best and a lost message at worst. The child posts into the
tree mailbox instead, and the loop moves the notice into the outgoing message
of the next step - so the parent learns about the finished child on the very
next model call.

A notice reaches the model as a hidden SYSTEM message, which is why every
child-authored fragment is wrapped in a data fence by ``_fence``: a quoted
web page must never arrive with the force of a system instruction.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

log = logging.getLogger("dev_agent.agent_loop")

#: Marker of the notice line a finished background child leaves in the
#: parent context. The marker is for readability and audit only; the
#: exactly-once guarantee comes from the O_EXCL claim in the run store.
NOTICE_PREFIX = "[SUBAGENT_NOTICE"


def _fence(text: str, source: str = "subagent_data") -> str:
    """Return *text* inside data fences, or unchanged if the guard is absent.

    A notice is delivered as a hidden SYSTEM message, so anything quoted in it
    arrives with the force of a system turn. Child-authored text is data: it
    was written by a process that read files and web pages, and quoting a page
    must never turn the quote into an instruction for the parent.
    """
    value = str(text or "")
    if not value.strip():
        return value
    try:
        from core.prompt_guard import wrap_data
        return wrap_data(value, source)
    except Exception:  # pragma: no cover - the guard is best effort
        return value


def format_subagent_notice(notice: Dict[str, Any]) -> str:
    """Return the one-line, budgeted notice text about a finished child.

    Only what the parent needs to ACT is included: the run id, the agent, the
    status, up to three open steps and the exact follow-up calls. The handoff
    itself stays in the child journal - it is fetched on demand through
    ``read_subagent_state`` and would not fit into a notice line.
    """
    if not isinstance(notice, dict):
        return ""
    run_id = str(notice.get("run_id") or "").strip()
    if not run_id:
        return ""
    agent = str(notice.get("agent_slug") or "unknown").strip()
    status = str(notice.get("status") or "unknown").strip()
    payload = notice.get("payload") or {}
    reason = str(payload.get("stop_reason") or payload.get("error_code") or "").strip()
    parts = [
        NOTICE_PREFIX + ":" + run_id + ":" + status + "]",
        "Subagent " + agent + " finished with status " + status + ".",
    ]
    if reason:
        parts.append("Reason: " + reason + ".")
    steps = payload.get("steps_used")
    if steps:
        parts.append("Steps used: " + str(steps) + ".")
    # The outcome and the artifact list are what the parent needs to decide
    # whether to read, resume or stop. Without them every notice costs the
    # parent three extra calls (journal, status, directory listing) just to
    # learn whether the child produced anything at all.
    summary = " ".join(str(payload.get("summary") or "").split())
    if summary:
        if len(summary) > 280:
            summary = summary[:277] + "..."
        parts.append("Summary: " + summary)
    artifacts = payload.get("artifacts") or notice.get("artifacts") or []
    if isinstance(artifacts, (list, tuple)) and artifacts:
        parts.append("Artifacts: " + str(len(artifacts)) + ".")
    pending = payload.get("pending") or notice.get("pending") or []
    if isinstance(pending, (list, tuple)):
        short = [str(p).strip() for p in list(pending)[:3] if str(p).strip()]
        if short:
            parts.append("Open steps: " + ", ".join(short) + ".")
    # A parked child is not finished: it waits for an ANSWER that continues the
    # same run. Telling the parent to resume it would spend a second run and a
    # second budget on what one reply can do, so the follow-up call differs.
    if str(payload.get("await_reason") or notice.get("await_reason") or "") == "question":
        question = payload.get("question") or notice.get("question") or {}
        if isinstance(question, dict):
            text = " ".join(str(question.get("text") or "").split())
            if text:
                if len(text) > 400:
                    text = text[:397] + "..."
                parts.append("Question: " + _fence(text, "subagent_question"))
            blocked = " ".join(str(question.get("blocked") or "").split())
            if blocked:
                if len(blocked) > 200:
                    blocked = blocked[:197] + "..."
                parts.append("Blocked on: " + _fence(blocked, "subagent_question") + ".")
        parts.append("Call answer_child(run_id=\"" + run_id
                     + "\", answer=...) to reply; the child continues the SAME run.")
        return " ".join(parts)
    parts.append("Call read_subagent_state(run_id=\"" + run_id + "\") for the handoff.")
    parts.append("Call resume_subagent(run_id=\"" + run_id + "\") to continue it.")
    return " ".join(parts)


def drain_subagent_notices(state: "Any") -> List[str]:
    """Drain the tree mailbox and return the notice lines for this step.

    A background child never writes into the dialog history: that structure
    belongs to the script run that drives the loop, and a worker thread
    touching it is a race at best and a lost message at worst. The worker
    posts into the tree mailbox instead and the loop moves the notice into
    the outgoing message of the next step - so the parent learns about the
    finished child on the very next model call, not one step later.

    Delivery is guarded by the same ``O_EXCL`` claim the worker uses: a
    notice already published by the UI is not repeated here.
    """
    root_id = str(getattr(state, "root_run_id", "") or getattr(state, "run_id", "") or "").strip()
    if not root_id:
        return []
    try:
        from core.orchestration.service import get_service
        service = get_service()
        notices = service.take_notifications(root_id)
    except Exception as exc:  # pragma: no cover - orchestration is optional
        log.warning("subagent notice drain failed: %s", exc)
        return []
    # Imported lazily from the facade: the indexer and the clock stay single
    # definitions while the surrounding modules of the loop are still being
    # separated, and the facade always re-exports them.
    from dev_agent.agent_loop import _index_message, _now_ts

    lines: List[str] = []
    for notice in notices:
        line = format_subagent_notice(notice)
        if not line:
            continue
        lines.append(line)
        # A hidden SYSTEM message is the delivery channel: it reaches the model
        # with this call, stays out of the visible dialog, and never becomes
        # the plain user line that would push the step into ``awaiting_user``.
        entry = {
            "role": "system",
            "content": line,
            "hidden": True,
            "_notice": True,
            "_category": "system_notice",
            "_summary": "subagent finished: " + str(notice.get("run_id") or ""),
            "ts": _now_ts(),
        }
        try:
            _index_message(entry, state.next_index, "system_notice")
            state.next_index += 1
        except Exception:  # pragma: no cover - indexing must not break a step
            pass
        state.history.append(entry)
    if lines:
        state.subagent_notices = list(getattr(state, "subagent_notices", []) or []) + lines
    return lines


__all__ = ["NOTICE_PREFIX", "format_subagent_notice", "drain_subagent_notices"]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
