"""
core.gateway.dialog - starting and driving a root dialog run.

This is the hole the platform had: ``OrchestrationService.run`` launches a
CHILD (a derived thread, a narrowed tool catalog, ``child_mode`` semantics),
and nothing launched the dialog a person actually talks to. The page did it
inline on every Streamlit rerun and the scheduler did it inline in its
worker thread, so a third transport had no entry point at all.

The module owns the SEQUENCE of a launch, not the mechanics of a step
(``core.orchestration.dialog_runner`` owns those):

1. validate and authorise the agent;
2. refuse a second root on a thread that already has a live one;
3. create the thread and bind the workspace before any model call;
4. open the run record and attach it to its tree;
5. start the driver - a background thread for ``mode='auto'``, nothing for
   ``mode='step'``;
6. close the record, release the tree and unbind the thread on every exit.

Two execution models, and the difference is deliberate:

- ``auto`` runs the loop to a terminal status or a pause. It must carry a
  deadline and a token budget: nobody is watching, and an unbounded
  background loop applies file edits until the step cap stops it.
- ``step`` runs exactly one step and keeps the state in memory for the next
  call. This is what the page does, and it keeps an interactive pause
  reachable.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.gateway import entities, errors, runs, threads
from core.gateway.dto import (
    DialogHandle,
    DialogOutcome,
    DialogRequest,
    DialogSnapshot,
    DialogStepResult,
    MessageView,
    PendingInput,
    RunSnapshot,
)
from core.orchestration import dialog_runner as runner

log = logging.getLogger("core.gateway.dialog")

#: Default wall-clock budget of an unattended run. Mirrors the child budget:
#: a root may work as long as a child it could have spawned.
DEFAULT_DEADLINE_SECONDS = 3600
#: Default token budget of an unattended run. 0 means "no limit"; the
#: default is a real ceiling because the loop is unattended.
DEFAULT_BUDGET_TOKENS = 2_000_000
#: Prefix of a root run id. The shape is validated on input so a caller
#: cannot address an arbitrary string.
RUN_ID_PREFIX = "r_"


# ─── Live state of step-mode dialogs ────────────────────────────────────────


class _LiveDialog:
    """The parts of a running dialog that only live in memory.

    A step-mode dialog advances one call at a time and needs its loop state
    between calls; an auto-mode dialog needs nothing but its thread handle.
    The state is NOT durable on purpose: it holds the provider card, the
    dispatcher and the closure that calls the model, none of which can be
    serialised. A step-mode dialog therefore does not survive a restart - a
    fact the read path reports honestly instead of inventing a resume.
    """

    __slots__ = ("state", "dispatcher", "cursor", "thread_id", "agent_slug",
                 "workspace", "request", "lock", "last_error")

    def __init__(self) -> None:
        self.state: Any = None
        self.dispatcher: Any = None
        self.cursor: int = 0
        self.thread_id: str = ""
        self.agent_slug: str = ""
        self.workspace: str = ""
        self.request: Optional[DialogRequest] = None
        self.lock = threading.RLock()
        self.last_error: str = ""


_LIVE: Dict[str, _LiveDialog] = {}
_LIVE_LOCK = threading.RLock()

#: Callbacks invoked once when a root run closes: ``(run_id, status)``.
#:
#: The server needs this to release the admission slot exactly when the work
#: ends, not when a client happens to poll the status. The gateway must not
#: import the transport, so the dependency points this way: the transport
#: registers, the core calls.
_CLOSE_LISTENERS: List[Callable[[str, str], None]] = []
_CLOSE_LOCK = threading.RLock()


def add_close_listener(listener: Callable[[str, str], None]) -> None:
    """Register a callback invoked when a root run reaches a terminal status."""
    with _CLOSE_LOCK:
        if listener not in _CLOSE_LISTENERS:
            _CLOSE_LISTENERS.append(listener)


def remove_close_listener(listener: Callable[[str, str], None]) -> None:
    """Drop a previously registered close callback."""
    with _CLOSE_LOCK:
        if listener in _CLOSE_LISTENERS:
            _CLOSE_LISTENERS.remove(listener)


def _notify_closed(run_id: str, status: str) -> None:
    """Tell every registered listener that a run closed."""
    with _CLOSE_LOCK:
        listeners = list(_CLOSE_LISTENERS)
    for listener in listeners:
        try:
            listener(run_id, status)
        except Exception as exc:  # pragma: no cover - a listener never breaks a run
            log.warning("close listener failed for %s: %s", run_id, exc)


# ─── Identifiers ────────────────────────────────────────────────────────────


def new_run_id() -> str:
    """Return a fresh root run id."""
    return RUN_ID_PREFIX + uuid.uuid4().hex[:28]


def is_run_id(value: str) -> bool:
    """Return True when *value* has the shape of a root run id."""
    text = str(value or "").strip()
    if not text.startswith(RUN_ID_PREFIX):
        return False
    tail = text[len(RUN_ID_PREFIX):]
    return len(tail) == 28 and all(ch in "0123456789abcdef" for ch in tail)


def _live_state(run_id: str) -> Optional[_LiveDialog]:
    with _LIVE_LOCK:
        return _LIVE.get(str(run_id or ""))


def _remember(run_id: str, live: _LiveDialog) -> None:
    with _LIVE_LOCK:
        _LIVE[run_id] = live


def _forget(run_id: str) -> None:
    with _LIVE_LOCK:
        _LIVE.pop(str(run_id or ""), None)


# ─── Pending input from a loop state ────────────────────────────────────────


def pending_of_state(state: Any) -> Optional[PendingInput]:
    """Read the pause description out of a live loop state.

    The page reads these fields straight from its session; a transport has
    no session, so the values are lifted into a transport type here once.
    """
    if state is None:
        return None
    status = str(getattr(state, "final_status", "") or "")
    if status == "awaiting_confirmation":
        args = getattr(state, "pending_confirmation_args", None)
        reasons = getattr(state, "pending_confirmation_reasons", None)
        return PendingInput(
            kind="confirmation",
            tool=str(getattr(state, "pending_confirmation_tool", "") or ""),
            args=dict(args) if isinstance(args, dict) else {},
            reasons=[str(r) for r in reasons] if isinstance(reasons, (list, tuple)) else [],
            code=str(getattr(state, "pending_confirmation_code", "") or ""),
        )
    if status == "sanitized_required":
        return PendingInput(kind="sanitized",
                            code=str(getattr(state, "error_code", "") or ""))
    if status == "awaiting_question":
        question = getattr(state, "parked_question", None)
        return PendingInput(kind="question",
                            question=dict(question) if isinstance(question, dict) else None)
    if status in ("awaiting_user", "awaiting_approval"):
        plan = str(getattr(state, "final_text", "") or "")
        kind = "plan_approval" if getattr(state, "plan_presented", False) else "unfinished"
        return PendingInput(kind=kind, plan=plan)
    return None


def _messages_since(thread_id: str, cursor: int) -> List[MessageView]:
    """Return the messages a step appended, by absolute index."""
    if not thread_id:
        return []
    try:
        window = threads.read_messages(thread_id, offset=max(0, cursor), limit=200)
    except errors.GatewayError:
        return []
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("step messages unavailable for %s: %s", thread_id, exc)
        return []
    return list(window.messages)


# ─── Creation ───────────────────────────────────────────────────────────────


def _resolve_agent(slug: str, allowed_slugs: Optional[List[str]]) -> Dict[str, Any]:
    """Validate the agent slug and return its config.

    Authorisation comes BEFORE creation: a caller must learn that it may not
    address an employee before the gateway creates a thread for it.
    """
    key = str(slug or "").strip()
    if not key:
        raise errors.invalid_request("agent_slug is required", field="agent_slug")
    detail = entities.get_orchestrator(key, allowed_slugs=allowed_slugs)
    return dict(detail.config or {})


def _active_root(thread_id: str) -> Optional[str]:
    """Return the id of a non-terminal root run of this thread, if any.

    One live root per thread is a hard rule, not a nicety. Two roots on one
    thread share a run id (the thread id is the root id in the page), so a
    second launch would overwrite the first one's record and each caller
    would read the other's outcome.
    """
    if not thread_id:
        return None
    try:
        service = runs.get_service()
        roots = list(service.roots_for_thread(thread_id) or [])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("root lookup failed for %s: %s", thread_id, exc)
        return None
    for root_id in roots:
        try:
            snapshot = runs.get_run(root_id)
        except errors.GatewayError:
            continue
        if not snapshot.is_terminal:
            return root_id
    return None


def create_dialog(req: DialogRequest, *,
                  allowed_slugs: Optional[List[str]] = None) -> DialogHandle:
    """Create the thread and the run record of a root dialog.

    No model call happens here. A caller that only wants a place to write
    into can create the dialog now and start it later; the HTTP layer uses
    that to answer an error before a run id is handed out.
    """
    problem = req.validate()
    if problem:
        raise errors.invalid_request(problem)

    config = _resolve_agent(req.agent_slug, allowed_slugs)

    thread_id = str(req.thread_id or "").strip()
    created = False
    if thread_id:
        threads.get_thread(thread_id, allowed_slugs=None)
        active = _active_root(thread_id)
        if active:
            raise errors.run_in_progress(active, thread_id)
    else:
        from core.threads_devagent import create_devagent_thread
        title = str(req.title or "").strip() or str(req.message or "")[:80]
        thread_id = create_devagent_thread(
            title=title,
            orchestrator_slug=req.agent_slug,
            workspace=req.workspace or None,
            target_file=req.target_file or None,
        )
        created = True
        if not thread_id:
            raise errors.GatewayError("internal_error",
                                      "The dialog thread could not be created")

    _bind_workspace(thread_id, req.workspace, req.target_file)

    run_id = new_run_id()
    try:
        service = runs.get_service()
        runner.register_root(service, run_id, thread_id=thread_id,
                             agent_slug=req.agent_slug, config=config)
    except Exception as exc:
        log.warning("root registration failed for %s: %s", run_id, exc)
        raise errors.GatewayError("internal_error",
                                  "The run record could not be opened")

    return DialogHandle(thread_id=thread_id, run_id=run_id,
                        agent_slug=req.agent_slug,
                        workspace=req.workspace or "",
                        created=created, status="running", mode=req.mode)


def _bind_workspace(thread_id: str, workspace: str, target_file: str) -> bool:
    """Bind a thread to its project folder before any tool call runs.

    Without this the dispatcher falls back to the process-wide config, which
    belongs to whichever dialog happened to switch folders last - the exact
    leak the binding registry exists to prevent.
    """
    if not thread_id:
        return False
    try:
        from dev_agent import workspace_binding as wb
        wb.register_thread(thread_id, workspace=workspace or None,
                           target_file=target_file or None)
        wb.ensure_thread_active(thread_id)
        return True
    except Exception as exc:  # pragma: no cover - binding is best effort
        log.warning("workspace binding failed for %s: %s", thread_id, exc)
        return False


def _unbind_workspace(thread_id: str) -> None:
    if not thread_id:
        return
    try:
        from dev_agent import workspace_binding as wb
        wb.unbind_thread(thread_id)
    except Exception as exc:  # pragma: no cover - unbinding is best effort
        log.warning("workspace unbind failed for %s: %s", thread_id, exc)


# ─── Step assembly ──────────────────────────────────────────────────────────


def _assistant_dicts(slug: str, workspace: str) -> Dict[str, Any]:
    """Return the provider card of an employee for a root dialog."""
    from core.orchestrators import build_assistant_dicts
    try:
        strong, weak = build_assistant_dicts(slug, workspace or "", dialog_role="parent")
    except TypeError:  # pragma: no cover - older signature
        strong, weak = build_assistant_dicts(slug)
    card = dict(strong or {})
    if weak:
        card.setdefault("weak", weak)
    return card


def _prepare_step(handle: DialogHandle, req: DialogRequest, *,
                  history: Optional[List[Dict[str, Any]]] = None) -> _LiveDialog:
    """Build the live state of one dialog step."""
    auto_apply = req.auto_apply
    if auto_apply is None:
        auto_apply = entities.auto_apply_of(req.agent_slug)

    limits = _limits_for(req)
    card = _assistant_dicts(req.agent_slug, req.workspace)
    state = runner.build_state(
        task=req.message,
        thread_id=handle.thread_id,
        run_id=handle.run_id,
        history=history or [],
        max_steps=int(limits["max_steps"]),
        auto_apply=bool(auto_apply),
        strong_assistant=card,
        weak_assistant=card.get("weak") or card,
        root_run_id=handle.run_id,
        child_mode=(req.mode == "auto"),
        web_search=bool(req.web_search),
        safety=bool(req.safety),
        economy=req.economy,
        file_context=req.file_context,
        file_name=req.file_name,
    )

    dispatcher = runner.build_dispatcher(
        req.agent_slug, handle.thread_id,
        workspace=req.workspace, target_file=req.target_file,
        web_search=bool(req.web_search), safety=bool(req.safety),
    )
    runner.attach_sender(dispatcher, runner.send_adapter(card, safety=bool(req.safety)))

    live = _LiveDialog()
    live.state = state
    live.dispatcher = dispatcher
    live.thread_id = handle.thread_id
    live.agent_slug = req.agent_slug
    live.workspace = req.workspace or ""
    live.request = req
    try:
        live.cursor = len(history or [])
    except Exception:  # pragma: no cover - defensive
        live.cursor = 0
    return live


def _limits_for(req: DialogRequest) -> Dict[str, int]:
    """Return the step, deadline and budget limits of one launch."""
    config = entities.orchestrator_config(req.agent_slug)
    try:
        from core.orchestration.service import read_limits
        limits = dict(read_limits(config) or {})
    except Exception:  # pragma: no cover - defensive
        limits = {}
    return {
        "max_steps": int(req.max_steps or limits.get("subagent_max_steps") or 30),
        "deadline": int(req.deadline_seconds or DEFAULT_DEADLINE_SECONDS),
        "budget": int(req.budget_tokens or DEFAULT_BUDGET_TOKENS),
    }


# ─── Advancing ──────────────────────────────────────────────────────────────


def _result_of(state: Any, run_id: str, thread_id: str,
               live: _LiveDialog, events: Optional[List[Dict]] = None) -> DialogStepResult:
    """Build the transport result of one finished step."""
    from core.orchestration.contracts import AgentRunResult

    status, stop_reason, error_code = runner.final_triple(state)
    phase = str(getattr(state, "phase", "") or "")
    terminal = phase in ("done", "error")
    result = AgentRunResult(
        run_id=run_id,
        status=status,
        stop_reason=stop_reason,
        summary=runner.final_summary(state),
        error_code=error_code,
        steps_used=int(getattr(state, "steps", 0) or 0),
        tokens_used={
            "in": int(getattr(state, "total_tokens_in", 0) or 0),
            "out": int(getattr(state, "total_tokens_out", 0) or 0),
            "cache": int(getattr(state, "total_tokens_cache", 0) or 0),
        },
        await_reason=("question" if str(getattr(state, "final_status", "")) == "awaiting_question" else ""),
    )
    try:
        messages = _messages_since(thread_id, live.cursor)
    except Exception:  # pragma: no cover - defensive
        messages = []
    return DialogStepResult(
        run_id=run_id,
        thread_id=thread_id,
        phase=phase,
        final_status=str(getattr(state, "final_status", "") or ""),
        result=result,
        messages=messages,
        events=list(events or []),
        pending_input=pending_of_state(state),
        can_advance=not terminal,
        can_cancel=not terminal,
    )


def advance_dialog(run_id: str, *, on_event: Optional[Callable] = None,
                   lang: Optional[str] = None) -> DialogStepResult:
    """Execute exactly one step of a step-mode dialog.

    The live state is rebuilt from storage when this process has none: a
    step-mode dialog is not durable, and pretending otherwise would report a
    run that no longer exists as if it were waiting for its next step.
    """
    rid = str(run_id or "").strip()
    if not rid:
        raise errors.invalid_request("run id is required", field="run_id")
    live = _live_state(rid)
    if live is None:
        raise errors.GatewayError(
            "run_not_found",
            "The dialog is not active in this process",
            details={"run_id": rid},
        )
    with live.lock:
        if live.state is None:
            raise errors.run_not_found(rid)
        events: List[Dict[str, Any]] = []
        collector = on_event
        if collector is None:
            collector = events.append
        try:
            live.state = runner.run_step(live.state, live.dispatcher,
                                         on_event=collector, lang=lang)
        except Exception as exc:
            log.warning("dialog step failed for %s: %s", rid, exc)
            live.last_error = str(exc)
            live.state.phase = "error"
            live.state.error_message = str(exc)
        live.cursor = runner.persist_step(live.thread_id,
                                          list(getattr(live.state, "history", []) or []),
                                          live.cursor)
        result = _result_of(live.state, rid, live.thread_id, live, events)
        if not result.can_advance:
            _close(rid, live, result)
        return result


def _close(run_id: str, live: _LiveDialog, result: DialogStepResult) -> None:
    """Close the record of a finished dialog and release its tree."""
    status = "done"
    if result.result is not None:
        raw = (result.result.to_dict()
               if hasattr(result.result, "to_dict") else result.result)
        if isinstance(raw, dict):
            status = str(raw.get("status") or "done")
    try:
        runner.close_root(runs.get_service(), run_id, status)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("root close failed for %s: %s", run_id, exc)
    try:
        runs.get_service().save_record(run_id)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("record save failed for %s: %s", run_id, exc)
    try:
        runner.release_root(runs.get_service(), run_id)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("tree release failed for %s: %s", run_id, exc)
    _unbind_workspace(live.thread_id)
    _forget(run_id)
    _notify_closed(run_id, status)


# ─── Launch ─────────────────────────────────────────────────────────────────


def start_dialog(req: DialogRequest, *, on_event: Optional[Callable] = None,
                 allowed_slugs: Optional[List[str]] = None,
                 lang: Optional[str] = None) -> DialogHandle:
    """Create and launch a root dialog.

    ``mode='auto'`` starts a background driver; ``mode='step'`` prepares the
    live state and returns, so the caller drives the loop itself.
    """
    handle = create_dialog(req, allowed_slugs=allowed_slugs)

    if req.mode == "step":
        live = _prepare_step(handle, req)
        _remember(handle.run_id, live)
        return handle

    _remember_placeholder(handle.run_id, handle, req)
    thread = threading.Thread(
        target=_drive,
        args=(handle, req, on_event, lang),
        name="dialog-" + handle.run_id,
        daemon=True,
    )
    thread.start()
    return handle


def _remember_placeholder(run_id: str, handle: DialogHandle,
                          req: DialogRequest) -> None:
    """Reserve the slot of an auto dialog before its driver starts."""
    live = _LiveDialog()
    live.thread_id = handle.thread_id
    live.agent_slug = handle.agent_slug
    live.workspace = handle.workspace
    live.request = req
    _remember(run_id, live)


def _drive(handle: DialogHandle, req: DialogRequest,
           on_event: Optional[Callable], lang: Optional[str]) -> None:
    """Run an unattended dialog to its terminal status or pause."""
    run_id = handle.run_id
    live = _live_state(run_id) or _LiveDialog()
    try:
        prepared = _prepare_step(handle, req)
        prepared.request = req
        _remember(run_id, prepared)
        live = prepared

        limits = _limits_for(req)
        deadline = time.monotonic() + max(1, int(limits["deadline"]))

        def _cancelled() -> bool:
            try:
                if runs.get_service().is_cancelled(run_id):
                    return True
            except Exception:  # pragma: no cover - defensive
                pass
            return runs.cancel_requested(run_id)

        def _persist(state: Any) -> None:
            live.cursor = runner.persist_step(
                live.thread_id, list(getattr(state, "history", []) or []),
                live.cursor)

        state = runner.run_loop(
            live.state, live.dispatcher,
            deadline=deadline,
            budget=int(limits["budget"]),
            cancelled=_cancelled,
            on_event=on_event,
            on_step=_persist,
            lang=lang,
            max_steps=int(limits["max_steps"]),
        )
        live.state = state
        live.cursor = runner.persist_step(
            live.thread_id, list(getattr(state, "history", []) or []),
            live.cursor)
        result = _result_of(state, run_id, live.thread_id, live)
        _close(run_id, live, result)
    except Exception as exc:  # pragma: no cover - a driver never raises out
        log.warning("dialog driver failed for %s: %s", run_id, exc)
        try:
            runner.close_root(runs.get_service(), run_id, "failed")
            runs.get_service().save_record(run_id)
            runner.release_root(runs.get_service(), run_id)
        except Exception:  # pragma: no cover - defensive
            pass
        _unbind_workspace(handle.thread_id)
        _forget(run_id)
        # A driver that died still has to release its admission slot: the
        # listener is the only place that returns one, and a crash is exactly
        # when nobody polls the run any more.
        _notify_closed(run_id, "failed")


# ─── Continuation ───────────────────────────────────────────────────────────


def submit_message(run_id: str, text: str, *, on_event: Optional[Callable] = None,
                   allowed_slugs: Optional[List[str]] = None,
                   lang: Optional[str] = None) -> DialogStepResult:
    """Run one more step of a dialog with a new human message.

    The message is appended to the dialog FIRST and then becomes the task of
    the step, so a reader of the thread sees the request even when the step
    fails before it produces an answer.
    """
    rid = str(run_id or "").strip()
    message = str(text or "").strip()
    if not rid:
        raise errors.invalid_request("run id is required", field="run_id")
    if not message:
        raise errors.invalid_request("message is required", field="message")

    live = _live_state(rid)
    if live is None:
        raise errors.GatewayError(
            "run_not_found",
            "The dialog is not active in this process",
            details={"run_id": rid},
        )
    with live.lock:
        base = live.request
        if base is None:
            raise errors.run_not_found(rid)
        from dataclasses import replace
        request = replace(base, message=message)
        history = threads.load_history(live.thread_id)
        try:
            from core.threads_devagent import append_thread_message
            append_thread_message(live.thread_id, "user", message)
        except Exception as exc:  # pragma: no cover - persistence is best effort
            log.warning("user message append failed for %s: %s", live.thread_id, exc)
        live.cursor = len(history) + 1

        fresh = _prepare_step(
            DialogHandle(thread_id=live.thread_id, run_id=rid,
                         agent_slug=live.agent_slug,
                         workspace=live.workspace, mode=request.mode),
            request, history=history,
        )
        fresh.cursor = live.cursor
        _remember(rid, fresh)
        if request.mode == "step":
            live.state = fresh.state
            events: List[Dict[str, Any]] = []
            try:
                live.state = runner.run_step(live.state, fresh.dispatcher,
                                             on_event=on_event or events.append,
                                             lang=lang)
            except Exception as exc:
                log.warning("continuation step failed for %s: %s", rid, exc)
                live.state.phase = "error"
                live.state.error_message = str(exc)
            live.cursor = runner.persist_step(
                live.thread_id, list(getattr(live.state, "history", []) or []),
                live.cursor)
            result = _result_of(live.state, rid, live.thread_id, live, events)
            if not result.can_advance:
                _close(rid, live, result)
            return result

        _remember(rid, fresh)
        thread = threading.Thread(
            target=_drive,
            args=(DialogHandle(thread_id=fresh.thread_id, run_id=rid,
                               agent_slug=fresh.agent_slug,
                               workspace=fresh.workspace, mode="auto"),
                  request, on_event, lang),
            name="dialog-" + rid,
            daemon=True,
        )
        thread.start()
        return DialogStepResult(
            run_id=rid, thread_id=fresh.thread_id, phase="calling_llm",
            final_status="", result=None, can_advance=False, can_cancel=True,
        )


# ─── Reads ──────────────────────────────────────────────────────────────────


def get_dialog(run_id: str) -> DialogSnapshot:
    """Return everything a caller needs to act on a dialog."""
    snapshot = runs.get_run(run_id)
    live = _live_state(str(run_id or ""))
    pending = snapshot.pending_input
    if pending is None and live is not None and live.state is not None:
        pending = pending_of_state(live.state)
    status = snapshot.status
    return DialogSnapshot(
        handle=DialogHandle(
            thread_id=snapshot.thread_id,
            run_id=snapshot.run_id,
            agent_slug=snapshot.agent_slug,
            workspace=(live.workspace if live is not None else ""),
            created=False,
            status=status,
        ),
        run=snapshot,
        pending_input=pending,
        can_advance=(not snapshot.is_terminal and live is not None),
        can_cancel=not snapshot.is_terminal,
        can_answer=runs.can_answer(snapshot),
    )


def list_dialog_runs(thread_id: str) -> List[RunSnapshot]:
    """Return the runs of one dialog, newest first."""
    return runs.list_runs(thread_id=thread_id)

# ─── Synchronous run ────────────────────────────────────────────────────────


def run_dialog(req: DialogRequest, *, wait_seconds: int = 3600,
               allowed_slugs: Optional[List[str]] = None,
               poll_seconds: float = 1.0) -> DialogOutcome:
    """Start a root dialog and BLOCK until it finishes or the wait expires.

    This is the shape a console command needs: it has no session to poll from
    and no rerun to hang the next step on. It reuses the same launch path as
    every other transport (``start_dialog`` in ``auto`` mode) - it does not
    re-implement a single step - and only adds the waiting:

    - terminal status, a pause that needs a human, or ``wait_seconds``;
    - a timeout does NOT cancel the run: the caller learns ``timed_out`` and
      the run keeps working, because a command that gave up waiting must not
      destroy work somebody may still want;
    - a cancelled run is reported as ``cancelled``, never as a failure.

    The final assistant message is read from the thread AFTER the run reaches
    its status, so the caller receives what the employee actually said instead
    of a summary of what it was asked.
    """
    from core.gateway.dto import DialogOutcome

    started = time.monotonic()
    handle = start_dialog(req, allowed_slugs=allowed_slugs)
    deadline_pause = {"awaiting_input", "awaiting_user", "awaiting_approval",
                      "awaiting_confirmation", "awaiting_question"}
    snapshot = None
    timed_out = False
    while True:
        try:
            snapshot = runs.get_run(handle.run_id)
        except errors.GatewayError:
            snapshot = None
        if snapshot is not None and (snapshot.is_terminal
                                     or snapshot.status in deadline_pause):
            break
        if snapshot is not None and snapshot.status == "running":
            pending = snapshot.pending_input
            if pending is not None and pending.kind:
                break
        if (time.monotonic() - started) >= max(1, int(wait_seconds)):
            timed_out = True
            break
        time.sleep(max(0.05, min(float(poll_seconds), 5.0)))

    elapsed = max(0.0, time.monotonic() - started)
    if snapshot is None:
        return DialogOutcome(
            run_id=handle.run_id, thread_id=handle.thread_id,
            agent_slug=handle.agent_slug, status="failed",
            error_code="run_not_found",
            summary="the run record disappeared while waiting",
            timed_out=timed_out, elapsed_seconds=elapsed,
        )

    messages: List[MessageView] = []
    try:
        window = threads.read_messages(snapshot.thread_id or handle.thread_id,
                                       offset=0, limit=200)
        messages = list(window.messages)
    except errors.GatewayError:
        messages = []
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("outcome messages unavailable for %s: %s", handle.run_id, exc)
        messages = []

    summary = snapshot.summary
    if not summary:
        for message in reversed(messages):
            if message.role in ("assistant", "agent") and message.content.strip():
                summary = message.content
                break

    return DialogOutcome(
        run_id=snapshot.run_id or handle.run_id,
        thread_id=snapshot.thread_id or handle.thread_id,
        agent_slug=snapshot.agent_slug or handle.agent_slug,
        status=snapshot.status,
        stop_reason=snapshot.stop_reason,
        error_code=snapshot.error_code,
        summary=summary,
        steps_used=snapshot.steps_used,
        tokens_used=dict(snapshot.tokens_used or {}),
        artifacts=list(snapshot.artifacts or []),
        pending_input=snapshot.pending_input,
        messages=messages,
        timed_out=timed_out,
        elapsed_seconds=elapsed,
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
