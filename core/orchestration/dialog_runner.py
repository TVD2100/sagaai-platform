"""
core.orchestration.dialog_runner - the one place a root dialog step is built.

Before this module the root run existed twice: ``core/schedules/runner.py``
built it headless and ``ui/pages/orchestrator.py`` built it inside a
Streamlit rerun. The two copies already disagreed - the schedule carried a
deadline and a token budget, the page carried neither, and both pinned
``auto_apply`` to True while the orchestrator card stores its own value.
The HTTP transport would have made it a third copy.

This module owns the MECHANICS of a step: build the loop state, build the
dispatcher, wrap the provider call, run one step or a bounded loop, persist
what the step produced. It owns no POLICY: who may run, with which limits,
where the run record lives and what happens on a pause are decided by the
caller (``core.gateway.dialog`` for the facade, the schedule store for a
timed run, the page for an interactive dialog).

Deliberately free of streamlit, fastapi and pydantic. ``dev_agent.agent_loop``
is imported lazily inside the functions: the gateway and this module are
imported by the HTTP server at start-up, and the agent loop pulls the whole
tool catalog with it.

Cancellation is checked on the STEP BOUNDARY, before the next step starts.
A step is one provider call plus its tools - up to ten minutes for the
model and five for ``run_code`` - and a Python thread cannot be interrupted
safely in the middle of one. The caller therefore learns the truth
"cancelled at the next boundary", not "stopped instantly".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from core.orchestration.contracts import (
    STATUS_CANCELLED,
    STOP_ABORTED,
    map_final_status,
)

log = logging.getLogger("core.orchestration.dialog_runner")

#: Text prepended to the task when the caller supplies file context. Kept
#: identical to the schedule runner's framing so a task reads the same
#: whichever path started it.
FILE_CONTEXT_HEADER = "Attached file context:\n"


# ─── Loop state and dispatcher ──────────────────────────────────────────────


def build_state(*, task: str, thread_id: str, run_id: str = "",
                history: Optional[List[Dict[str, Any]]] = None,
                max_steps: int = 0, auto_apply: bool = False,
                strong_assistant: Optional[Dict[str, Any]] = None,
                weak_assistant: Optional[Dict[str, Any]] = None,
                root_run_id: str = "",
                child_mode: bool = False,
                web_search: bool = False,
                safety: bool = True,
                economy: Any = None,
                file_context: str = "",
                file_name: str = "") -> Any:
    """Build the ``AgentLoopState`` of one root dialog step.

    ``child_mode`` is what makes an unattended run finish at all: without it
    the loop stops on the first plan it presents (``awaiting_user``) and the
    caller never sees a terminal status. A scheduled run has always set it;
    the HTTP transport needs the same and an interactive page must not.

    ``auto_apply`` is passed explicitly instead of being pinned: the
    orchestrator card stores its own value, and a transport that silently
    applies file edits the card forbade is a behaviour difference nobody
    would find until the first surprised user.
    """
    from dev_agent.agent_loop import AgentLoopState

    limits = 0
    try:
        from dev_agent.agent_loop import DEFAULT_MAX_STEPS as _DEFAULT
        limits = _DEFAULT
    except Exception:  # pragma: no cover - defensive
        limits = 30

    state = AgentLoopState(
        task=task,
        max_steps=int(max_steps or limits),
        auto_apply=bool(auto_apply),
        strong_assistant=dict(strong_assistant or {}),
        weak_assistant=dict(weak_assistant or strong_assistant or {}),
        thread_id=thread_id,
        run_id=run_id,
        root_run_id=root_run_id or run_id,
        child_mode=bool(child_mode),
        enable_injection_protection=bool(safety),
        web_search_enabled=bool(web_search),
        history=list(history or []),
        file_context=file_context,
        file_name=file_name,
        next_index=len(history or []),
    )
    state.user_message = task
    state.phase = "calling_llm"

    if economy is not None:
        try:
            state.economy_mode = bool(getattr(economy, "mode", False))
            state.economy_cache_enabled = bool(
                getattr(economy, "cache_enabled", False))
            state.economy_cache_multiplier = int(
                getattr(economy, "cache_multiplier", 1) or 1)
            tail = getattr(economy, "tail_messages", None)
            state.economy_tail_messages = None if tail is None else int(tail)
            anchor = getattr(economy, "anchor", None)
            state.economy_anchor = None if anchor is None else int(anchor)
        except Exception as exc:  # pragma: no cover - economy is best effort
            log.warning("economy options ignored: %s", exc)

    return state


def build_dispatcher(slug: str, thread_id: str, *, workspace: str = "",
                     target_file: str = "",
                     disabled_tools: Optional[set] = None,
                     web_search: bool = False,
                     safety: bool = True) -> Any:
    """Build the tool dispatcher of one dialog step.

    The dispatcher writes the live workspace state of its thread into the
    process-wide config on every tool call, so it must be created per run
    and never shared between threads.
    """
    from dev_agent.universal_agent import UniversalDevAgent

    dispatcher = UniversalDevAgent(
        workspace=workspace or None,
        target_file=target_file or None,
        thread_id=thread_id or None,
    )
    try:
        dispatcher.attach_orchestrator(slug)
    except Exception as exc:  # pragma: no cover - custom tools are optional
        log.warning("orchestrator tool attach failed for %s: %s", slug, exc)
    if disabled_tools:
        try:
            dispatcher.child_disabled_tools = set(disabled_tools)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("disabled tool list rejected: %s", exc)
    try:
        dispatcher.core._web_search_enabled = bool(web_search)
        dispatcher.core._safety_enabled = bool(safety)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("dispatcher flags not applied: %s", exc)
    return dispatcher


def send_adapter(assistant_card: Dict[str, Any], *, safety: bool = True,
                 lang: Optional[str] = None) -> Callable[..., str]:
    """Return the provider call the loop uses for this dialog.

    A ``system`` argument replaces the orchestrator prompt for that one
    call; everything else (service, model, temperature) comes from the
    card. The injection-protection flag travels with the call instead of
    being read from a global, so two dialogs of one process cannot
    influence each other's safety mode.
    """
    from core.api_layer import send_request

    card = dict(assistant_card or {})

    def _send(user_message: str, system: str = "",
              history: Optional[List[Dict[str, Any]]] = None) -> str:
        assistant = dict(card)
        if system:
            assistant["text"] = system
        return send_request(
            user_message=user_message,
            assistant=assistant,
            file_context="",
            history=history or [],
            lang=lang,
            enable_injection_protection=bool(safety),
        )

    return _send


def attach_sender(dispatcher: Any, sender: Callable[..., str]) -> None:
    """Install the provider call on a dispatcher, tolerating older builds."""
    try:
        dispatcher.core.set_send_request(sender)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("send adapter not installed: %s", exc)


# ─── Steps and loops ────────────────────────────────────────────────────────


def run_step(state: Any, dispatcher: Any, *, on_event: Optional[Callable] = None,
             lang: Optional[str] = None) -> Any:
    """Execute exactly one loop step and return the advanced state."""
    from dev_agent.agent_loop import step_agent_loop

    if on_event is not None:
        return step_agent_loop(state, dispatcher=dispatcher, lang=lang,
                               on_event=on_event)
    return step_agent_loop(state, dispatcher=dispatcher, lang=lang)


def run_loop(state: Any, dispatcher: Any, *, deadline: Optional[float] = None,
             budget: Optional[int] = None,
             cancelled: Optional[Callable[[], bool]] = None,
             on_event: Optional[Callable] = None,
             on_step: Optional[Callable[[Any], None]] = None,
             lang: Optional[str] = None, max_steps: Optional[int] = None) -> Any:
    """Run steps until the loop finishes, the budget runs out or it is cancelled.

    Three stops, and their distinction matters to the caller:

    - the loop reached a terminal phase - the state carries its own verdict;
    - ``deadline`` passed or ``budget`` was exhausted - a failure of the run,
      reported as cancelled/aborted so nobody mistakes it for success;
    - ``cancelled()`` returned True - a deliberate stop, also cancelled.

    ``deadline`` is an absolute ``time.monotonic()`` value, not a duration:
    the caller computes it once and every step compares against the same
    instant.
    """
    steps = 0
    cap = int(max_steps or 0)

    while getattr(state, "phase", "") not in ("done", "error"):
        if cancelled is not None:
            try:
                if cancelled():
                    state.phase = "done"
                    state.final_status = "stopped_by_user"
                    state.final_text = "Cancelled by request."
                    return state
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("cancel probe failed: %s", exc)
        if deadline is not None and time.monotonic() > float(deadline):
            state.phase = "done"
            state.final_status = "stopped_max_steps"
            state.final_text = "Run deadline exceeded."
            state.error_code = "timeout"
            return state
        if budget and budget > 0:
            spent = int(getattr(state, "total_tokens_in", 0) or 0) + \
                    int(getattr(state, "total_tokens_out", 0) or 0)
            if spent > int(budget):
                state.phase = "done"
                state.final_status = "stopped_max_steps"
                state.final_text = "Token budget exhausted."
                state.error_code = "budget_exhausted"
                return state
        if cap and steps >= cap:
            state.phase = "done"
            state.final_status = "stopped_max_steps"
            state.final_text = "Step limit reached."
            state.error_code = "max_steps"
            return state

        state = run_step(state, dispatcher, on_event=on_event, lang=lang)
        steps += 1
        if on_step is not None:
            try:
                on_step(state)
            except Exception as exc:  # pragma: no cover - a hook never breaks a run
                log.warning("step hook failed: %s", exc)

    return state


# ─── Persistence ────────────────────────────────────────────────────────────


def persist_step(thread_id: str, history: List[Dict[str, Any]],
                 cursor: int) -> int:
    """Append the messages a step produced and return the new cursor.

    The whole transcript of a run belongs in the dialog the owner reads, not
    only the final answer, and the events stay attached to the message that
    rendered them. A failed append is logged and skipped: losing a mirror
    must not fail a run that already did its work.
    """
    from core.threads_devagent import append_thread_message

    position = int(cursor or 0)
    for message in history[position:]:
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
            log.warning("message append failed for %s: %s", thread_id, exc)
        position += 1
    return position


def final_triple(state: Any) -> tuple:
    """Map a loop state onto ``(status, stop_reason, error_code)``.

    Delegates to :func:`map_final_status` instead of re-deriving the mapping:
    a fourth copy of that table is how a new stop reason ends up handled by
    one path and silently mishandled by another.
    """
    status = str(getattr(state, "final_status", "") or "")
    code = getattr(state, "error_code", None)
    return map_final_status(status, code)


def final_summary(state: Any) -> str:
    """Return the text a finished run reports as its summary."""
    return str(getattr(state, "final_text", "") or
               getattr(state, "error_message", "") or "")


# ─── Root run registration ──────────────────────────────────────────────────


def register_root(service: Any, run_id: str, *, thread_id: str,
                  agent_slug: str, config: Optional[Dict[str, Any]] = None,
                  parent_run_id: Optional[str] = None,
                  depth: int = 0) -> Any:
    """Open the run record of a root dialog.

    A root run has no worker of its own: without an explicit record nobody
    can answer "what happened to this dialog", and a background child would
    have no tree to attach to.
    """
    return service.ensure_run(
        run_id,
        thread_id=thread_id,
        agent_slug=agent_slug,
        config=config,
        parent_run_id=parent_run_id,
        depth=depth,
    )


def close_root(service: Any, run_id: str, status: str) -> bool:
    """Close the root record; the tree is released separately."""
    try:
        return bool(service.close_root_run(run_id, status=status))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("root close failed for %s: %s", run_id, exc)
        return False


def release_root(service: Any, run_id: str) -> Dict[str, Any]:
    """Release the run tree and report whether the release was deferred.

    ``deferred`` means a background child is still working; the last child
    finish performs the release instead. A caller that retried on deferred
    would fight the worker for the same tree.
    """
    try:
        return dict(service.release_tree(run_id) or {})
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("tree release failed for %s: %s", run_id, exc)
        return {"released": False, "deferred": False}


def cancel_report(state: Any, run_id: str) -> tuple:
    """Return the cancelled triple for a run stopped on request."""
    return (STATUS_CANCELLED, STOP_ABORTED, "cancelled_by_user")
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
