# -*- coding: utf-8 -*-
"""
core.schedules.runner - execute one scheduled run as a real dialog.

A scheduled run is not a hidden background job: it is an ordinary dialog
with the chosen employee, stored in the same thread table with
``kind='dialog'`` and no parent. That is a product requirement, not an
implementation detail - the owner must be able to open History tomorrow and
read what the agent did last night, exactly as if they had typed the task
themselves.

The module therefore mirrors what the chat page does for one message:
create the thread, register its workspace binding, open the journal scope,
run the agent loop step by step, append every message to the transcript, and
finish with a terminal status. What it does NOT do is wait for a human: no
plan approval, no confirmation dialog. Those interactions are answered
automatically with a refusal the model can read (``child_mode``), and the run
is bounded by steps, tokens and wall-clock time.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core.schedules import store
from core.schedules.calc import next_run_epoch
from core.schedules.form import format_moment
from core.schedules.model import Schedule

log = logging.getLogger(__name__)

#: How long a finished run's messages are given to reach the transcript before
#: the runner reports anyway. Appending is synchronous, so this is a guard
#: against a stuck database rather than a real wait.
TRANSCRIPT_FLUSH_TIMEOUT = 30

#: Tools a scheduled run must not use. The list extends the delegation child
#: list with the conversational ones: nobody answers an ``ask_parent`` at
#: 3 a.m., so offering it would only burn steps.
SCHEDULED_DISABLED_TOOLS = (
    "ask_parent",
    "answer_child",
    "spawn_agent",
    "sleep",
    "resume_subagent",
    "subagent_status",
    "subagent_result",
    "read_subagent_state",
    "cancel_subagent",
    "verify_result",
)


def build_task_text(schedule: Schedule, planned_at: int) -> str:
    """Compose the user message of one scheduled run.

    The task text is what the owner wrote, unchanged. The framing below is
    platform context: it tells the agent that this is an autonomous run and
    how to finish, which is information a human would otherwise supply by
    answering questions. It is deliberately short - a long preamble eats the
    context that the actual task needs.
    """
    # The moment must read in the rule's own zone: the agent is told when it runs,
    # and a server-local rendering would state a different wall-clock time.
    moment = format_moment(int(planned_at), schedule.tz)
    lines = [str(schedule.task_text or "").strip()]
    if schedule.brief_text.strip():
        lines.append("")
        lines.append(str(schedule.brief_text).strip())
    lines.append("")
    lines.append("---")
    lines.append("Автономный запуск по расписанию «%s» в %s." % (schedule.title, moment))
    lines.append("Пользователя в этом диалоге нет: подтвердить действие или ответить "
                 "на вопрос некому. Опасные и необратимые операции недоступны - "
                 "если без них не обойтись, опиши блокер в итоге.")
    lines.append("Заверши работу итоговым сообщением и закончи ответ строкой "
                 '{"loop_status": "done"}.')
    return "\n".join(lines)


def _thread_title(schedule: Schedule, planned_at: int) -> str:
    """Return the History title of one run: schedule name plus its moment."""
    stamp = format_moment(int(planned_at), schedule.tz)
    name = str(schedule.title or schedule.id).strip() or schedule.id
    return "\u23f0 %s \u00b7 %s" % (name, stamp)


def _effective_config(schedule: Schedule) -> Dict[str, int]:
    """Return the delegation limits a scheduled run is allowed to use.

    A scheduled run never delegates further: unattended self-multiplication is
    the fastest way to an unexpected bill, and the owner asked for ONE task to
    be executed, not for a team.
    """
    return {
        "max_depth": max(0, int(schedule.max_depth or 0)),
        "max_active_subagents": 1,
        "subagent_max_steps": max(1, int(schedule.max_steps or 60)),
        "subagent_budget_tokens": max(0, int(schedule.budget_tokens or 0)),
        "subagent_timeout_seconds": max(30, int(schedule.timeout_seconds or 900)),
        "total_subagent_runs": 0,
    }


def _register_workspace(thread_id: str, schedule: Schedule) -> None:
    """Bind the run's dialog to the workspace the owner chose.

    The binding is what makes the tools resolve paths against the right
    folder; without it the agent would work in the neutral empty directory
    and could not read the project at all.
    """
    import os

    workspace = str(schedule.workspace or "").strip()
    if not workspace or not os.path.isdir(workspace):
        workspace = ""
    try:
        from dev_agent import workspace_binding as wb
        wb.register_thread(thread_id, workspace=workspace or None, target_file=None)
    except Exception as exc:  # pragma: no cover - binding is best effort
        log.warning("schedule workspace binding failed for %s: %s", thread_id, exc)


def _create_dialog(schedule: Schedule, planned_at: int) -> str:
    """Create the ordinary dialog thread of one run and return its id."""
    from core.orchestrators import get_orchestrator
    from core.threads_devagent import create_devagent_thread

    orchestrator = get_orchestrator(schedule.agent_slug) or {}
    display_name = str(orchestrator.get("name") or schedule.agent_slug or "")
    import os

    workspace = str(schedule.workspace or "").strip()
    thread_id = create_devagent_thread(
        title=_thread_title(schedule, planned_at),
        orchestrator_slug=schedule.agent_slug,
        orchestrator_name=display_name or schedule.agent_slug,
        workspace=workspace if workspace and os.path.isdir(workspace) else None,
        target_file=None,
    )
    _register_workspace(thread_id, schedule)
    return thread_id


def _make_dispatcher(schedule: Schedule, thread_id: str):
    """Build the agent dispatcher of one scheduled run."""
    from core.orchestrators import build_assistant_dicts
    from dev_agent.universal_agent import UniversalDevAgent

    strong_assistant, weak_assistant = build_assistant_dicts(
        schedule.agent_slug, workspace=str(schedule.workspace or ""),
        dialog_role="child",
    )
    dispatcher = UniversalDevAgent(workspace=str(schedule.workspace or "") or None,
                                   thread_id=thread_id)
    dispatcher.attach_orchestrator(schedule.agent_slug)
    try:
        from core.orchestration.service import CHILD_DISABLED_TOOLS
        dispatcher.child_disabled_tools = set(CHILD_DISABLED_TOOLS) | set(SCHEDULED_DISABLED_TOOLS)
    except Exception:
        dispatcher.child_disabled_tools = set(SCHEDULED_DISABLED_TOOLS)

    def _send(user_message: str, system: str = "",
              history: Optional[List[Dict[str, Any]]] = None) -> str:
        from core.api_layer import send_request
        card = dict(strong_assistant)
        if system:
            card["text"] = system
        return send_request(user_message=user_message, assistant=card,
                            file_context="", history=history or [], lang=None)

    dispatcher.core.set_send_request(_send)
    return dispatcher, strong_assistant, weak_assistant


def _append_messages(thread_id: str, messages: List[Dict[str, Any]],
                     start: int) -> int:
    """Append new loop messages to the dialog and return the new cursor.

    The dialog is the artifact the owner reads in History, so every message of
the run is written there - not just the final answer. Events stay attached to
their message, exactly as the chat page stores them, so reopening the dialog
shows the same tool feed the run produced live.
    """
    from core.threads_devagent import append_thread_message

    cursor = start
    for message in messages[cursor:]:
        try:
            append_thread_message(
                thread_id,
                str(message.get("role") or "assistant"),
                str(message.get("content") or ""),
                file_name=str(message.get("file_name") or ""),
                file_chars=int(message.get("file_chars") or 0),
                events=message.get("_events"),
                tokens=message.get("_tokens"),
                hidden=bool(message.get("hidden")),
            )
        except Exception as exc:  # pragma: no cover - transcript is best effort
            log.warning("schedule message append failed for %s: %s", thread_id, exc)
        cursor += 1
    return cursor


def _counters(state: Any) -> Dict[str, int]:
    """Return the token counters of a loop state in the store's shape."""
    return {
        "in": int(getattr(state, "total_tokens_in", 0) or 0),
        "out": int(getattr(state, "total_tokens_out", 0) or 0),
        "cache": int(getattr(state, "total_tokens_cache", 0) or 0),
    }


def _final_status(state: Any) -> str:
    """Map the loop's final status onto the history vocabulary."""
    status = str(getattr(state, "final_status", "") or "")
    if status in ("done", "applied"):
        return "done"
    if status in ("awaiting_user", "awaiting_approval", "awaiting_confirmation",
                  "awaiting_question"):
        # Nobody is there to answer: the run stopped short of its own goal.
        return "awaiting_input"
    if status == "stopped_by_user":
        return "cancelled"
    if status == "stopped_max_steps":
        return "failed"
    return "failed"


def run_schedule(schedule: Schedule, planned_at: int, *,
                 run_row_id: str = "", run_id: str = "",
                 manual: bool = False, lang: Optional[str] = None) -> Dict[str, Any]:
    """Execute ONE scheduled run and record its outcome.

    Returns a compact report ``{status, thread_id, run_id, steps, tokens}``.
    The function never raises: a scheduled run happens while nobody watches,
    so a crash has to become a recorded failure rather than a dead thread.
    """
    from dev_agent.agent_loop import AgentLoopState, step_agent_loop

    started = int(time.time())
    thread_id = ""
    state: Any = None
    try:
        thread_id = _create_dialog(schedule, planned_at)
        dispatcher, strong_assistant, weak_assistant = _make_dispatcher(schedule, thread_id)
        limits = _effective_config(schedule)
        task_text = build_task_text(schedule, planned_at)

        state = AgentLoopState(
            task=task_text,
            max_steps=int(limits["subagent_max_steps"]),
            auto_apply=True,
            strong_assistant=strong_assistant,
            weak_assistant=weak_assistant,
            thread_id=thread_id,
            run_id=run_id or ("sched-" + thread_id),
            parent_run_id="",
            depth=0,
            # No human is present: a confirmation request becomes a refusal
            # the model must read, and an awaiting_user outcome becomes a
            # bounded automatic continuation.
            child_mode=True,
            enable_injection_protection=True,
        )
        state.user_message = task_text
        state.phase = "calling_llm"

        cursor = 0
        budget = int(limits["subagent_budget_tokens"] or 0)
        deadline = time.monotonic() + int(limits["subagent_timeout_seconds"] or 900)
        stopped_reason = ""
        while state.phase not in ("done", "error"):
            if time.monotonic() > deadline:
                stopped_reason = "timeout"
                break
            if budget > 0 and (int(state.total_tokens_in) + int(state.total_tokens_out)) > budget:
                stopped_reason = "budget"
                break
            state = step_agent_loop(state, dispatcher=dispatcher, lang=lang)
            cursor = _append_messages(thread_id, state.history, cursor)

        cursor = _append_messages(thread_id, state.history, cursor)
        status = _final_status(state)
        if stopped_reason:
            status = "failed"
        error_code = ""
        if stopped_reason == "timeout":
            error_code = "timeout"
        elif stopped_reason == "budget":
            error_code = "budget_exhausted"
        elif status == "failed":
            error_code = str(getattr(state, "error_code", "") or "run_failed")
        elif status == "awaiting_input":
            error_code = "needs_attention"

        report = {
            "status": status,
            "thread_id": thread_id,
            "run_id": str(run_id or ""),
            "steps": int(getattr(state, "steps", 0) or 0),
            "tokens": _counters(state),
            "stop_reason": stopped_reason,
            "error_code": error_code,
            "summary": str(getattr(state, "final_text", "") or "")[:4000],
            "manual": bool(manual),
            "duration": int(time.time()) - started,
        }
    except Exception as exc:  # pragma: no cover - a run must never raise
        log.warning("scheduled run %s failed: %s", schedule.id, exc)
        report = {
            "status": "failed",
            "thread_id": thread_id,
            "run_id": str(run_id or ""),
            "steps": int(getattr(state, "steps", 0) or 0) if state is not None else 0,
            "tokens": _counters(state) if state is not None else {},
            "stop_reason": "error",
            "error_code": "internal_error",
            "summary": "%s: %s" % (type(exc).__name__, exc),
            "manual": bool(manual),
            "duration": int(time.time()) - started,
        }

    _record_result(schedule, planned_at, run_row_id, report)
    return report


def _record_result(schedule: Schedule, planned_at: int, run_row_id: str,
                   report: Dict[str, Any]) -> None:
    """Write the outcome to the history row and to the rule's counters."""
    status = str(report.get("status") or "failed")
    try:
        if run_row_id:
            store.finish_run(
                run_row_id, status=status,
                run_id=str(report.get("run_id") or ""),
                thread_id=str(report.get("thread_id") or ""),
                stop_reason=str(report.get("stop_reason") or ""),
                error_code=str(report.get("error_code") or ""),
                reason=str(report.get("error_code") or ""),
                steps_used=int(report.get("steps") or 0),
                tokens=report.get("tokens") or {},
                summary=str(report.get("summary") or ""),
            )
    except Exception as exc:  # pragma: no cover - history is best effort
        log.warning("schedule history write failed for %s: %s", schedule.id, exc)

    success = status == "done"
    run_count = int(schedule.run_count or 0) + 1
    fail_count = int(schedule.fail_count or 0) + (0 if success else 1)
    consecutive = 0 if success else int(schedule.consecutive_failures or 0) + 1

    try:
        current = store.get_schedule(schedule.id) or schedule
        now = int(time.time())
        lifecycle = str(current.status or "active")
        if lifecycle == "running":
            lifecycle = "active"
        if report.get("manual"):
            # A manual run is a checkout, not a schedule move: the grid and
            # the enabled/paused state stay exactly where the owner left
            # them, and only the counters learn about the run.
            lifecycle = str(current.status or "active")
        else:
            upcoming = next_run_epoch(current, now)
            if current.max_runs is not None and run_count >= int(current.max_runs):
                lifecycle = "finished"
            elif consecutive >= 3:
                # Three failures in a row mean the rule cannot work as configured
                # (a deleted employee, a missing folder). Holding it stops the
                # waste and puts the problem where the owner will see it.
                lifecycle = "error_hold"
            store.set_next_run(schedule.id, upcoming, status=lifecycle)
        store.settle_schedule(
            schedule.id, status=lifecycle, last_status=status,
            error_code=str(report.get("error_code") or ""),
            success=success, run_count=run_count, fail_count=fail_count,
            consecutive_failures=consecutive,
        )
    except Exception as exc:  # pragma: no cover - counters are best effort
        log.warning("schedule settle failed for %s: %s", schedule.id, exc)


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
