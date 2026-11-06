# -*- coding: utf-8 -*-
"""core.orchestration.child_runner - the machinery around one child run.

A child is a full agent with its own dispatcher, journal and transcript, and
a run of it passes through four phases: resolve the agent and its launch
context, build the dispatcher and the loop state, drive the step loop, then
release everything on every exit path. These helpers hold that machinery.
They keep no state of their own: the run record, the transcript sink and the
journal scope arrive as arguments and leave the same way.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from core.orchestration.contracts import (
    AWAIT_QUESTION,
    AWAIT_UNFINISHED,
    ERR_CANCELLED_BY_PARENT,
    ERR_PROVIDER_ERROR,
    ERR_TIMEOUT,
    ERR_UNKNOWN_AGENT,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STOP_ABORTED,
    STOP_ERROR,
    STOP_MAX_TOKENS,
    AgentQuestion,
    AgentRunResult,
    map_final_status,
)

log = logging.getLogger("core.orchestration.child_runner")

#: Prefix of the message a parked child receives when its parent answers.
#: It is a marker, not decoration: the child must be able to tell the answer
#: from its own earlier turns, and a resume rebuilt from the transcript must
#: recognise where the continuation began.
ANSWER_PREFIX = "[PARENT ANSWER] "


def resolve_child_agent(slug: str, run_id: str):
    """Return ``(orchestrator, failure)`` for a child slug.

    ``failure`` is a ready-made result when the slug is unknown: a spawn that
    names a missing agent must fail with a readable code, not blow up halfway
    through the setup.
    """
    from core.orchestrators import get_orchestrator

    orch = get_orchestrator(slug) if slug else None
    if orch:
        return orch, None
    return None, AgentRunResult(
        run_id=run_id, status=STATUS_FAILED, stop_reason=STOP_ERROR,
        summary="Unknown agent: " + (slug or "<empty>"),
        error_code=ERR_UNKNOWN_AGENT)


def restore_child_history(prior_run: bool, prior_state: Any,
                          child_thread_id: str, parent_thread_id: str,
                          run_id: str) -> List[Dict[str, Any]]:
    """Rebuild the transcript of a continued run.

    A CONTINUATION of a parked run does not start a new conversation: the
    transcript already holds everything the child saw and said, and the
    answer is the one thing it has not seen yet. Reusing the stowed state
    when it is available keeps the exact conversation; the transcript is the
    durable fallback for an answer that arrives after a restart.
    """
    if not prior_run:
        return []
    if prior_state is not None and getattr(prior_state, "history", None):
        return list(prior_state.history)
    history: List[Dict[str, Any]] = []
    try:
        from core.thread_mirror import _transcript
        mirror = _transcript()
        for record in mirror.iter_records(child_thread_id, parent_thread_id or None):
            if mirror.is_checkpoint(record):
                continue
            history.append({
                "role": str(record.get("role") or "user"),
                "content": str(record.get("content") or ""),
                "ts": str(record.get("ts") or ""),
                "file_name": str(record.get("file_name") or ""),
                "file_chars": int(record.get("file_chars") or 0),
            })
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("child history restore failed for %s: %s", run_id, exc)
        return []
    return history


def child_display_name(orch: Any) -> str:
    """Return the human name of an orchestrator record (dict or object)."""
    if isinstance(orch, dict):
        return str(orch.get("name") or "")
    return str(getattr(orch, "name", "") or "")


def bind_child_thread(child_thread_id: str, parent_thread_id: str,
                      base_dir: Optional[str], slug: str, orch: Any,
                      task_text: str) -> None:
    """Bind the derived child thread before its first step.

    The child gets the workspace of the parent, its own journal and its own
    index row. The row is what a transcript reader resolves the child
    through, so it is created here - before the first step - rather than only
    when the sink opens: a child that dies before writing anything stays
    readable as an empty transcript instead of "thread not found".
    """
    try:
        from dev_agent import workspace_binding as wb
        binder = getattr(wb, "bind_child_thread", None)
        if callable(binder):
            binder(child_thread_id, parent_thread_id=parent_thread_id,
                   workspace=base_dir, target_file=None, slug=slug,
                   display_name=child_display_name(orch), title=task_text)
    except Exception as exc:  # pragma: no cover - binding is best effort
        log.warning("child thread binding failed: %s", exc)


def build_child_dispatcher(slug: str, base_dir: Optional[str],
                           child_thread_id: str, depth: int,
                           limits: Dict[str, int],
                           strong_assistant: Dict[str, Any],
                           lang: Optional[str],
                           disabled_tools: Any) -> Any:
    """Create the child's dispatcher with its provider call wired in.

    The child reaches the model through the SAME send helper a dialog uses,
    with the card built for the child role. The tool catalog is narrowed to
    what the parent had, minus platform entity creation; a child that already
    sits at the depth ceiling loses delegation too, because the service would
    refuse the call anyway.
    """
    from core.api_layer import send_request
    from dev_agent.universal_agent import UniversalDevAgent

    dispatcher = UniversalDevAgent(workspace=base_dir, thread_id=child_thread_id)
    dispatcher.attach_orchestrator(slug)
    try:
        disabled = set(disabled_tools)
        max_depth = int(limits.get("max_depth") or 0)
        if max_depth and depth >= max_depth:
            disabled |= {"spawn_agent", "sleep"}
        setattr(dispatcher, "child_disabled_tools", disabled)
    except Exception:
        pass

    def _send(user_message: str, system: str = "",
              history: Optional[List[Dict[str, Any]]] = None) -> str:
        card = dict(strong_assistant)
        if system:
            card["text"] = system
        return send_request(user_message=user_message, assistant=card,
                            file_context="", history=history or [],
                            lang=lang or "")

    dispatcher.core.set_send_request(_send)
    return dispatcher


def open_child_sink(child_thread_id: str, parent_thread_id: str,
                    slug: str, orch: Any, base_dir: Optional[str],
                    task_text: str) -> Any:
    """Open the durable transcript of the child, or return ``None``.

    Persistence is best effort: a run that cannot open its transcript still
    runs, it just leaves no copy behind.
    """
    try:
        from core.orchestration.transcript_sink import TranscriptSink
        sink = TranscriptSink(
            thread_id=child_thread_id,
            parent_thread_id=parent_thread_id,
            slug=slug,
            display_name=child_display_name(orch) or slug,
            workspace=base_dir,
        )
        sink.open(task_text)
        return sink
    except Exception as exc:  # pragma: no cover - persistence is best effort
        log.warning("child transcript sink init failed: %s", exc)
        return None


def child_event_forwarder(service: Any, run_id: str,
                          parent_run_id: Optional[str], depth: int
                          ) -> Callable[[dict], None]:
    """Return the forwarder of a child's events to the run's subscribers.

    The forwarder stamps the child identity explicitly: events built by a
    nested helper may lack it, and a subscriber that cannot tell the child
    run from its parent would draw a flat feed instead of a tree. It also
    keeps the durability heartbeat fresh at most once a minute - ``heal()``
    treats a silent record as a dead process, and that verdict must not fall
    on a healthy child.
    """
    heartbeat_at = [time.monotonic()]

    def _forward(event: dict) -> None:
        if not isinstance(event, dict):
            return
        try:
            now = time.monotonic()
            if now - heartbeat_at[0] >= 60.0:
                heartbeat_at[0] = now
                service.refresh_heartbeat(run_id)
        except Exception:  # pragma: no cover - heartbeat is best effort
            pass
        event.setdefault("run_id", run_id)
        event.setdefault("parent_run_id", parent_run_id or "")
        event.setdefault("depth", depth)
        try:
            service.publish(event)
        except Exception as exc:  # pragma: no cover - a listener must not break a run
            log.warning("child event forward failed: %s", exc)

    return _forward


def apply_resume_state(state: Any, resume_history: List[Dict[str, Any]],
                       answer_text: str, prior_state: Any) -> None:
    """Turn a fresh child state into the continuation of a parked run.

    The history comes back from the transcript (or from the stowed state),
    the answer is the one message the child has not seen, and the counters
    carry over so the answer costs the child no progress at all.
    """
    state.history = resume_history
    state.next_index = len(resume_history)
    state.phase = "calling_llm"
    state.user_message = ANSWER_PREFIX + str(answer_text or "")
    if prior_state is None:
        return
    state.steps = int(getattr(prior_state, "steps", 0) or 0)
    state.total_tokens_in = int(getattr(prior_state, "total_tokens_in", 0) or 0)
    state.total_tokens_out = int(getattr(prior_state, "total_tokens_out", 0) or 0)
    state.total_tokens_cache = int(getattr(prior_state, "total_tokens_cache", 0) or 0)
    state.auto_continue_count = int(getattr(prior_state, "auto_continue_count", 0) or 0)


def step_child_loop(state: Any, dispatcher: Any, *, run_id: str,
                    service: Any, deadline: float, budget: int, sink: Any,
                    on_event: Callable[[dict], None],
                    lang: Optional[str]) -> Optional[AgentRunResult]:
    """Drive the child's step loop until it settles.

    Returns ``None`` when the loop ended on its own (``done`` or ``error``);
    otherwise returns the result that ended it early - a cancellation, the
    run deadline or the token budget. One drain per step keeps the transcript
    at most one step behind, and a step boundary is the only place a
    checkpoint is honest: every message of the step is on disk and every file
    the step wrote already exists.
    """
    from dev_agent.agent_loop import step_agent_loop

    while state.phase not in ("done", "error"):
        if service.is_cancelled(run_id):
            return AgentRunResult(
                run_id=run_id, status=STATUS_CANCELLED,
                stop_reason=STOP_ABORTED, summary=state.final_text or "",
                error_code=ERR_CANCELLED_BY_PARENT,
                steps_used=state.steps,
                tokens_used={"in": state.total_tokens_in,
                             "out": state.total_tokens_out,
                             "cache": state.total_tokens_cache})
        if time.monotonic() > deadline:
            return AgentRunResult(
                run_id=run_id, status=STATUS_CANCELLED,
                stop_reason=STOP_ABORTED, summary=state.final_text or "",
                error_code=ERR_TIMEOUT, steps_used=state.steps,
                tokens_used={"in": state.total_tokens_in,
                             "out": state.total_tokens_out,
                             "cache": state.total_tokens_cache})
        if budget > 0 and (state.total_tokens_in + state.total_tokens_out) > budget:
            return AgentRunResult(
                run_id=run_id, status=STATUS_DONE,
                stop_reason=STOP_MAX_TOKENS, summary=state.final_text or "",
                steps_used=state.steps,
                tokens_used={"in": state.total_tokens_in,
                             "out": state.total_tokens_out,
                             "cache": state.total_tokens_cache})
        state = step_agent_loop(state, dispatcher=dispatcher, lang=lang,
                               on_event=on_event)
        if sink is not None:
            sink.drain(state)
            sink.checkpoint(state)
    return None


def child_final_result(state: Any, run_id: str) -> AgentRunResult:
    """Build the result of a child that settled on its own.

    A run that stopped on a question is NOT an unfinished one: it waits for
    an answer it can actually receive. The reason travels with the result so
    the parent tells "answer me" apart from "read my journal".
    """
    status, stop_reason, error_code = map_final_status(
        state.final_status, getattr(state, "error_code", None))
    if state.phase == "error" and not error_code:
        error_code = ERR_PROVIDER_ERROR
    await_reason = ""
    question = None
    if status == STATUS_AWAITING_INPUT:
        if state.final_status == "awaiting_question":
            await_reason = AWAIT_QUESTION
            question = AgentQuestion.from_dict(state.parked_question)
        else:
            await_reason = AWAIT_UNFINISHED
    return AgentRunResult(
        run_id=run_id, status=status, stop_reason=stop_reason,
        summary=state.final_text or state.error_message or "",
        error_code=error_code, steps_used=state.steps,
        tokens_used={"in": state.total_tokens_in,
                     "out": state.total_tokens_out,
                     "cache": state.total_tokens_cache},
        await_reason=await_reason, question=question)


def finish_child(sink: Any, state: Any, scope: Any,
                 child_thread_id: str) -> None:
    """Release everything a child run holds, on EVERY exit path.

    The final drain catches the last step and the counters, so a reader sees
    an honest total even for a run that died; the journal scope and the
    workspace binding are released afterwards. None of the three may raise
    into the caller: the result is already decided by the time this runs.
    """
    if sink is not None:
        try:
            sink.finish(state)
        except Exception as exc:  # pragma: no cover - never mask a result
            log.warning("child transcript finish failed: %s", exc)
    try:
        if scope is not None:
            scope.__exit__(None, None, None)
    except Exception:  # pragma: no cover - scope exit must never raise
        pass
    try:
        from dev_agent import workspace_binding as wb
        unbinder = getattr(wb, "unbind_thread", None)
        if callable(unbinder):
            unbinder(child_thread_id)
    except Exception:
        pass
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
