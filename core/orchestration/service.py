# -*- coding: utf-8 -*-
"""
core.orchestration.service - the in-process run registry and delegation runtime.

Responsibilities:

- keep the run records of the current process (parent links, depth, status,
  cancellation flags, subscribers) - v1 has no durable registry, a run lives
  in memory and the result goes back to the caller;
- enforce the delegation limits read from the PARENT's config
  (``max_depth``, ``max_active_subagents``, ``subagent_max_steps``,
  ``subagent_budget_tokens``, ``subagent_timeout_seconds``,
  ``total_subagent_runs``) with an immediate refusal and no queue;
- provide the synchronous fan-out primitive: ``spawn_many`` starts N children
  in parallel, waits for all of them and returns the results in call order;
- cascade cancellation over ``parent_run_id``.

Concurrency model (why orchestration calls are executed by the loop, not by
``dispatch``): ``UniversalDevAgent.dispatch`` wraps a tool call in
``with wb.thread_context(thread_id)``, and that context manager holds the
process-wide ``_BINDING_LOCK`` (an ``RLock``) for the whole block. A child
started *inside* the parent's dispatch would wait forever for that lock while
holding it - a guaranteed deadlock. The agent loop therefore executes
``spawn_agent`` and ``invoke_assistant`` BEFORE entering ``dispatch``, and a
waiting parent holds no lock. Child MODEL calls are parallel; child TOOL
calls are serialised by the same global lock, which is acceptable because
tools are short next to model calls.

Limits are read from config, never from call arguments: otherwise
``spawn_agent(..., max_depth=99)`` would be enough to bypass them.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("core.orchestration.service")

from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentQuestion,
    AgentRunResult,
    Artifact,
    AWAIT_QUESTION,
    AWAIT_UNFINISHED,
    CancelReport,
    DEFAULT_ANSWER_MAX_CHARS,
    DEFAULT_ANSWER_WAIT_SECONDS,
    DEFAULT_DIALOG_ROUNDS,
    DEFAULT_MAX_PARKED_RUNS,
    DEFAULT_QUESTION_MAX_CHARS,
    ERR_ANSWER_NOT_RECEIVED,
    ERR_NOTHING_TO_ANSWER,
    ERR_INVALID_ARGS,
    ERR_QUESTION_LIMIT,
    ERR_RUN_ALREADY_FINISHED,
    ERR_TOO_MANY_PARKED,
    MAX_ANSWER_WAIT_SECONDS,
    STATUS_AWAITING_INPUT,
    DEFAULT_MAX_ACTIVE_SUBAGENTS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_POOL_WAIT_SECONDS,
    DEFAULT_SUBAGENT_BUDGET_TOKENS,
    DEFAULT_SUBAGENT_MAX_STEPS,
    DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_SUBAGENT_RUNS,
    MAX_SUBAGENT_TIMEOUT_SECONDS,
    ERR_CANCELLED_BY_PARENT,
    ERR_LIMIT_EXCEEDED,
    ERR_INTERNAL_ERROR,
    ERR_MAX_STEPS,
    ERR_NO_RUN_IDENTITY,
    ERR_NOT_IMPLEMENTED,
    ERR_NOT_YOUR_RUN,
    ERR_PROVIDER_ERROR,
    ERR_RESUME_CHAIN_LIMIT,
    ERR_RESUME_UNAVAILABLE,
    ERR_RUN_NOT_FOUND,
    ERR_STALE_RUN,
    ERR_TIMEOUT,
    InvokeRequest,
    MAX_RESUME_CHAIN,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STOP_ABORTED,
    STOP_COMPLETED,
    STOP_ERROR,
    STOP_MAX_TOKENS,
    SpawnRequest,
    TERMINAL_STATUSES,
    map_final_status,
)
from core.orchestration import runs as run_store  # noqa: E402

#: Tools a child must NOT see in v1: a child never creates platform entities
#: (orchestrators, assistants). The parent keeps whatever it had.
#: Prefix of the message a parked child receives when its parent answers.
#: It is a marker, not decoration: the child must be able to tell the answer
#: from its own earlier turns, and a resume rebuilt from the transcript must
#: recognise where the continuation began.
_ANSWER_PREFIX = "[PARENT ANSWER] "

CHILD_DISABLED_TOOLS = (
    "create_orchestrator",
    "update_orchestrator",
    "delete_orchestrator",
    "reload_orchestrator",
    "create_assistant_for_task",
    "update_assistant_by_id",
    "save_orchestrator_function",
    "delete_orchestrator_function",
    "save_orchestrator_instruction",
    "delete_orchestrator_instruction",
    "create_connection",
    "update_connection",
    "delete_connection",
    "set_enabled_connections",
    "resume_subagent",
    "subagent_status",
    "read_subagent_state",
    # The ANSWER side is the parent's: a child has no children of its own to
    # unpack, and offering it would invite a call the runtime refuses. Asking
    # stays available - that is the child's half of the pair.
    "answer_child",
    # A child does not check a check: verification is the parent's decision,
    # and a verifier that could launch another verifier would multiply runs
    # with no end condition.
    "verify_result",
)


def _as_int(value: Any, default: int) -> int:
    """Return *value* as an int, falling back to *default* on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def read_limits(config: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Extract the six delegation limits from an orchestrator config.

    Missing or malformed values fall back to the documented defaults, so a
    broken config can never remove a limit entirely.
    """
    cfg = config if isinstance(config, dict) else {}
    limits = {
        "max_depth": _as_int(cfg.get("max_depth"), DEFAULT_MAX_DEPTH),
        "max_active_subagents": _as_int(
            cfg.get("max_active_subagents"), DEFAULT_MAX_ACTIVE_SUBAGENTS),
        "subagent_max_steps": _as_int(
            cfg.get("subagent_max_steps"), DEFAULT_SUBAGENT_MAX_STEPS),
        "subagent_budget_tokens": _as_int(
            cfg.get("subagent_budget_tokens"), DEFAULT_SUBAGENT_BUDGET_TOKENS),
        "subagent_timeout_seconds": _as_int(
            cfg.get("subagent_timeout_seconds"), DEFAULT_SUBAGENT_TIMEOUT_SECONDS),
        "total_subagent_runs": _as_int(
            cfg.get("total_subagent_runs"), DEFAULT_TOTAL_SUBAGENT_RUNS),
        # ── Agent dialog ───────────────────────────────────────────────────
        # Five question/answer pairs per run (the owner raised the design
        # default of three); the value is a per-run budget, so it is read once
        # at spawn time and frozen into the record.
        "dialog_rounds": _as_int(
            cfg.get("dialog_rounds"), DEFAULT_DIALOG_ROUNDS),
        "question_max_chars": _as_int(
            cfg.get("question_max_chars"), DEFAULT_QUESTION_MAX_CHARS),
        "answer_max_chars": _as_int(
            cfg.get("answer_max_chars"), DEFAULT_ANSWER_MAX_CHARS),
        "answer_wait_seconds": _as_int(
            cfg.get("answer_wait_seconds"), DEFAULT_ANSWER_WAIT_SECONDS),
        "max_parked_runs": _as_int(
            cfg.get("max_parked_runs"), DEFAULT_MAX_PARKED_RUNS),
    }
    # A non-positive pool would deadlock the very first spawn; clamp to 1.
    if limits["max_active_subagents"] < 1:
        limits["max_active_subagents"] = 1
    # A non-positive timeout would abort every run instantly; a value above
    # the ceiling would outlive the parent that can wait for it.
    if limits["subagent_timeout_seconds"] < 1:
        limits["subagent_timeout_seconds"] = DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    elif limits["subagent_timeout_seconds"] > MAX_SUBAGENT_TIMEOUT_SECONDS:
        limits["subagent_timeout_seconds"] = MAX_SUBAGENT_TIMEOUT_SECONDS
    # A dialog with no rounds would refuse the FIRST question, which reads as
    # "the feature is broken" rather than "the limit is set too low"; a dialog
    # with no wait window would park a run nobody could ever answer. Both are
    # clamped up to 1, and the wait additionally never outlives a run budget.
    if limits["dialog_rounds"] < 1:
        limits["dialog_rounds"] = 1
    if limits["answer_wait_seconds"] < 1:
        limits["answer_wait_seconds"] = DEFAULT_ANSWER_WAIT_SECONDS
    elif limits["answer_wait_seconds"] > MAX_ANSWER_WAIT_SECONDS:
        limits["answer_wait_seconds"] = MAX_ANSWER_WAIT_SECONDS
    if limits["question_max_chars"] < 1:
        limits["question_max_chars"] = DEFAULT_QUESTION_MAX_CHARS
    if limits["answer_max_chars"] < 1:
        limits["answer_max_chars"] = DEFAULT_ANSWER_MAX_CHARS
    if limits["max_parked_runs"] < 1:
        limits["max_parked_runs"] = DEFAULT_MAX_PARKED_RUNS
    return limits


_DIALOG_LIMIT_KEYS = ("dialog_rounds", "question_max_chars",
                      "answer_max_chars", "answer_wait_seconds",
                      "max_parked_runs")


def _dialog_limits_of(rec: Any,
                      parent_config: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, int]:
    """Return the dialog limits that govern ONE run.

    The values frozen on the record win over whatever live config the caller
    passes: a run keeps the budget it was started with even when the answer
    arrives from another process whose dialog has different settings.
    Concurrency, depth and timeout stay live - they belong to the tree that
    is doing the work now, not to the run being unpacked.
    """
    limits = read_limits(parent_config)
    frozen = dict(getattr(rec, "dialog_limits", None) or {})
    for key in _DIALOG_LIMIT_KEYS:
        value = frozen.get(key)
        if isinstance(value, int) and value > 0:
            limits[key] = value
    return limits


@dataclass
class RunRecord:
    """State of one run (the dialog itself or a spawned child).

    The record is the single in-memory truth about a run. The fields below the
    identity block exist because a BACKGROUND run outlives the step that
    started it: the workspace, the child journal path and the timeout are
    frozen here at spawn time, so nothing later has to re-read the process-wide
    config (which belongs to whichever dialog happens to run).
    """

    run_id: str
    thread_id: str = ""
    agent_slug: str = ""
    parent_run_id: Optional[str] = None
    root_run_id: str = ""
    depth: int = 0
    status: str = STATUS_RUNNING
    stop_reason: Optional[str] = None
    summary: str = ""
    error_code: Optional[str] = None
    artifacts: List[Artifact] = field(default_factory=list)
    steps_used: int = 0
    tokens_used: Dict[str, int] = field(default_factory=dict)
    cancel_requested: bool = False
    # True while a WORKER thread of this process is still executing the run.
    # The status cannot answer this: ``cancel()`` marks a run CANCELLED at
    # once, while the child keeps executing until its next step boundary.
    # Releasing the tree on the status would drop the record out from under
    # that worker, and the worker would then see no cancel marker at all.
    worker_running: bool = False
    children: List[str] = field(default_factory=list)

    # ── Frozen launch context (background and resume) ──────────────────────
    child_thread_id: str = ""
    workspace: str = ""
    journal_path: str = ""
    goal: str = ""
    # Prompt-injection protection mode inherited from the parent run at spawn
    # time. Frozen here for the same reason as ``workspace``: a background
    # child starts long after the parent step that launched it, and the live
    # setting belongs to whichever dialog happens to be active then.
    enable_injection_protection: bool = True
    background: bool = False
    resumed_from: Optional[str] = None
    resume_depth: int = 0
    timeout_seconds: int = DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    heartbeat_at: float = 0.0
    created_at: float = 0.0
    # Would the same call plausibly succeed a second time? False for a defect
    # inside the loop. Frozen on the record so the answer survives the process
    # that produced it - a durable status alone cannot tell a retryable failure
    # from a permanent one.
    retryable: bool = True

    # ── Agent dialog (park / unpack) ───────────────────────────────────────
    # A parked run is NOT working: it holds no pool slot and no worker thread.
    # ``await_reason`` separates the two meanings of the awaiting status, and
    # ``question`` carries what the parent has to answer. ``dialog_rounds``
    # counts question/answer pairs and is never reset by unpacking - one run
    # has one budget however many times it is parked.
    await_reason: str = ""
    question: Optional[AgentQuestion] = None
    dialog_rounds: int = 0
    parked_at: float = 0.0
    # Dialog limits frozen at SPAWN time, not read when the answer arrives.
    # The answering process may belong to another dialog whose live config
    # says something else; the run must keep the budget it was started with,
    # or a stricter parent could cut a child's rounds mid-dialog and a looser
    # one could extend them.
    dialog_limits: Dict[str, int] = field(default_factory=dict)

    @property
    def is_parked(self) -> bool:
        """True when the run waits for an ANSWER from its parent."""
        return (self.status == STATUS_AWAITING_INPUT
                and self.await_reason == AWAIT_QUESTION)

    @property
    def is_terminal(self) -> bool:
        """True when the run reached one of the terminal statuses."""
        return self.status in TERMINAL_STATUSES

    def to_result(self) -> AgentRunResult:
        """Freeze the current record into an ``AgentRunResult``."""
        return AgentRunResult(
            run_id=self.run_id,
            status=self.status,
            stop_reason=self.stop_reason,
            summary=self.summary,
            artifacts=list(self.artifacts),
            error_code=self.error_code,
            steps_used=self.steps_used,
            tokens_used=dict(self.tokens_used),
            child_thread_id=self.child_thread_id,
            retryable=bool(self.retryable),
            await_reason=self.await_reason,
            question=self.question,
            dialog_rounds=int(self.dialog_rounds or 0),
        )

    def to_record(self) -> Dict[str, Any]:
        """Return the durable-store form of this record.

        ``summary`` is truncated here on purpose: the store serves status and
        resume, and a runaway child summary would otherwise grow the file that
        is rewritten on every heartbeat.
        """
        payload: Dict[str, Any] = {
            "run_id": self.run_id,
            # The dialog id is what makes a waiting child findable after a
            # restart: the page forgets its tree root, and the store has to be
            # able to answer "which tree belongs to this thread".
            "thread_id": self.thread_id,
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "depth": self.depth,
            "agent_slug": self.agent_slug,
            "child_thread_id": self.child_thread_id,
            "workspace": self.workspace,
            "journal_path": self.journal_path,
            "goal": self.goal,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "error_code": self.error_code,
            "summary": (self.summary or "")[:2000],
            "steps_used": self.steps_used,
            "tokens_used": dict(self.tokens_used),
            "background": self.background,
            "resumed_from": self.resumed_from,
            "resume_depth": self.resume_depth,
            "enable_injection_protection": bool(self.enable_injection_protection),
            "cancel_requested": self.cancel_requested,
            "timeout_seconds": self.timeout_seconds,
            "heartbeat_at": self.heartbeat_at or time.time(),
            "created_at": self.created_at or time.time(),
            "pid": os.getpid(),
            "boot_id": run_store.current_boot_id(),
            "host": run_store.current_host(),
            "retryable": bool(self.retryable),
            # Dialog state is persisted, not kept in memory alone: a parked run
            # must stay readable and answerable after the process that parked
            # it is gone. ``parked_at`` is what the reader uses to tell a live
            # wait from one whose window has already expired.
            "await_reason": self.await_reason,
            "question": self.question.to_dict() if self.question else None,
            "dialog_rounds": int(self.dialog_rounds or 0),
            "parked_at": float(self.parked_at or 0.0),
            "dialog_limits": {str(k): int(v)
                              for k, v in dict(self.dialog_limits or {}).items()},
        }
        if self.is_terminal:
            # A finished run keeps its RESULT, not only its bookkeeping. The
            # top-level ``summary`` above is capped because the record is
            # rewritten on every heartbeat while a run is alive; a terminal
            # record is written once, and a caller asking what the run
            # produced must receive the whole answer instead of its first
            # two thousand characters. Everything the synchronous launch
            # returns is frozen here for the same reason.
            payload["result"] = {
                "run_id": self.run_id,
                "status": self.status,
                "stop_reason": self.stop_reason,
                "summary": self.summary or "",
                "artifacts": [a.to_dict() if hasattr(a, "to_dict") else dict(a)
                              for a in self.artifacts],
                "error_code": self.error_code,
                "steps_used": self.steps_used,
                "tokens_used": dict(self.tokens_used),
                "child_thread_id": self.child_thread_id,
                "retryable": bool(self.retryable),
            }
        return payload


class _PoolSlot:
    """Outcome of one pool acquisition: was it granted, and by which tree.

    ``bool(slot)`` answers the old question ("may this call proceed?") while
    ``slot.tree`` answers the new one ("which pool must be credited back?").
    A slot granted without a tree means the call is outside the delegated-limits
    system: it may proceed and releasing it is a no-op.
    """

    __slots__ = ("tree", "granted")

    def __init__(self, tree: Optional["_TreeState"] = None,
                 granted: bool = True):
        self.tree = tree
        self.granted = bool(granted)

    def __bool__(self) -> bool:
        return self.granted

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "_PoolSlot(granted=%r, tree=%r)" % (self.granted, self.tree)


class _TreeState:
    """Per-tree bookkeeping: one pool, one run counter, all subscribers.

    The mailbox is part of the tree because a notification belongs to the
    dialog that owns the children: when the tree is released its pending
    notices are dropped with it, and a fresh dialog never inherits stale
    ``finished`` messages from a previous run of the same thread id.
    """

    __slots__ = ("lock", "pool", "pool_size", "total_used", "total_limit",
                 "subscribers", "mailbox", "held_slots", "release_pending")

    def __init__(self, pool_size: int, total_limit: int):
        self.lock = threading.RLock()
        self.pool_size = max(1, int(pool_size))
        self.pool = threading.BoundedSemaphore(self.pool_size)
        # The root run counts itself, so the tree starts at 1.
        self.total_used = 1
        self.total_limit = max(1, int(total_limit))
        # run_id -> list of callbacks (ancestors included at delivery time).
        self.subscribers: Dict[str, List[Callable[[dict], None]]] = {}
        # Drained by the loop or the UI; the worker only appends.
        self.mailbox: deque = deque(maxlen=200)
        # Slots currently granted by THIS object: released against it, never
        # against a tree looked up by id (which may be a newer object).
        self.held_slots = 0
        # Set when the dialog asked to release the tree while a background
        # child was still running; the last finish performs the release.
        self.release_pending = False


def _notice_key(result: Any) -> str:
    """Return the per-notice discriminator of a child result.

    A dialog run publishes one notice per question, all with the same
    ``awaiting_input`` status. The dialog round is what tells them apart, so
    the claim is keyed by it; every other result has one notice per status
    and needs no key at all.
    """
    try:
        if str(getattr(result, "status", "") or
               (result.get("status") if isinstance(result, dict) else "")) != STATUS_AWAITING_INPUT:
            return ""
        rounds = getattr(result, "dialog_rounds", None)
        if rounds is None and isinstance(result, dict):
            rounds = result.get("dialog_rounds")
        return "question-" + str(int(rounds or 0))
    except Exception:  # pragma: no cover - a key is never worth a crash
        return ""


def _record_child_handoff(rec: Any, result: AgentRunResult) -> None:
    """Leave the run outcome in the child journal, best effort.

    Called from the one place every path passes through - the finish - so a
    child that never wrote a journal still leaves a readable trace of what it
    did. Never raises: a journal is an aid, and a broken file must not turn a
    finished run into a failed one.
    """
    try:
        thread = str(getattr(rec, "child_thread_id", "") or "")
        if not thread:
            return
        from dev_agent.task_state import record_runtime_handoff
        record_runtime_handoff(
            thread,
            run_id=str(getattr(rec, "run_id", "") or result.run_id or ""),
            goal=str(getattr(rec, "goal", "") or ""),
            status=result.status,
            stop_reason=str(result.stop_reason or ""),
            error_code=str(result.error_code or ""),
            steps_used=int(result.steps_used or 0),
            summary=str(result.summary or ""),
            pending=list(result.pending or []),
            retryable=bool(result.retryable),
        )
    except Exception as exc:  # pragma: no cover - journal is best effort
        log.debug("child handoff write failed: %s", exc)


class OrchestrationService:
    """The ``RunService`` implementation of a single process.

    One instance per process is enough (see ``get_service``); the registry
    holds only live runs, so an idle process holds an empty dict.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: Dict[str, RunRecord] = {}
        self._trees: Dict[str, _TreeState] = {}
        self._child_runner: Optional[Callable[..., AgentRunResult]] = None
        # Live loop states of parked runs, keyed by run id. An answer is
        # usually given in the same process that parked the run, and resuming
        # the loop from its own stowed state keeps the whole conversation and
        # the step budget intact. The entry is dropped as soon as it is taken:
        # a state that was continued once must not be continued again.
        self._parked_states: Dict[str, Any] = {}

    def sweep_dead_runs(self) -> int:
        """Repair stored ``running`` claims that cannot be alive any more.

        Called once when the process-wide service is created. A record whose
        writer is provably gone can never become live again, so leaving it
        untouched only made a dead child look like running work.

        This is the ONE place that writes the repair back: the startup sweep
        owns the files, while every other reader (status poll, listing, tool
        subprocess) is read-only by contract.

        Returns the number of records that are stale.
        """
        try:
            records = run_store.list_runs(None, self.store_root(), limit=0,
                                          persist=True)
        except Exception as exc:  # pragma: no cover - sweep is best effort
            log.warning("run store sweep failed: %s", exc)
            return 0
        return sum(1 for rec in records
                   if str(rec.get("error_code") or "") == ERR_STALE_RUN)

    # ── Records ────────────────────────────────────────────────────────────

    def ensure_run(self, run_id: str, *,
                   thread_id: str = "",
                   agent_slug: str = "",
                   config: Optional[Dict[str, Any]] = None,
                   parent_run_id: Optional[str] = None,
                   depth: int = 0) -> RunRecord:
        """Create the record for a run if it does not exist yet.

        The dialog that starts a loop registers itself as a ROOT run
        (``parent_run_id=None``, ``depth=0``). Creating the root also creates
        the tree state: the concurrency pool and the total-run counter are
        per tree, so that self-delegation cannot escape the limits by
        spawning from a copy of itself. The call is idempotent.
        """
        rid = (run_id or "").strip()
        if not rid:
            rid = uuid.uuid4().hex[:12]
        with self._lock:
            rec = self._runs.get(rid)
            if rec is not None:
                if thread_id and not rec.thread_id:
                    rec.thread_id = thread_id
                if agent_slug and not rec.agent_slug:
                    rec.agent_slug = agent_slug
                if not parent_run_id and rec.is_terminal:
                    # The root record stands for ONE step of the dialog, and the
                    # dialog id survives across steps. A new step therefore
                    # re-opens the finished record instead of inheriting a
                    # terminal status that would describe the previous step.
                    rec.status = STATUS_RUNNING
                    rec.stop_reason = None
                    rec.error_code = None
                    rec.summary = ""
                    rec.created_at = time.time()
                    rec.heartbeat_at = rec.created_at
                return rec
            root_id = rid
            if parent_run_id:
                parent = self._runs.get(parent_run_id)
                root_id = parent.root_run_id if parent and parent.root_run_id else parent_run_id
            rec = RunRecord(
                run_id=rid,
                thread_id=thread_id or "",
                agent_slug=agent_slug or "",
                parent_run_id=parent_run_id or None,
                root_run_id=root_id,
                depth=int(depth or 0),
                # A child record exists because a worker is about to execute
                # it; a root record stands for one dialog step and has none.
                worker_running=bool(parent_run_id),
            )
            self._runs[rid] = rec
            if not parent_run_id:
                limits = read_limits(config)
                self._trees[root_id] = _TreeState(
                    limits["max_active_subagents"], limits["total_subagent_runs"])
            else:
                parent = self._runs.get(parent_run_id)
                if parent is not None and rid not in parent.children:
                    parent.children.append(rid)
            return rec

    def get_record(self, run_id: str) -> Optional[RunRecord]:
        """Return a live record, or None when the run is unknown."""
        with self._lock:
            return self._runs.get((run_id or "").strip())

    def finish_run(self, run_id: str, result: AgentRunResult) -> AgentRunResult:
        """Store the terminal state of a run and return the EFFECTIVE result.

        The compare-and-set on ``cancel_requested`` lives here, not in ``run``:
        a cancellation that lands while the child is still finishing would
        otherwise be overwritten by the child's own ``done`` - the loop already
        passed its ``is_cancelled`` check, and the assignment below is
        unconditional. A background worker finishes long after ``cancel`` was
        accepted, so the protection has to sit in the one place both paths
        share.

        Returns the result that was actually stored: the caller publishes THAT,
        so a cancelled run never reports ``done`` to the feed.
        """
        rid = (run_id or "").strip()
        with self._lock:
            rec = self._runs.get(rid)
            if rec is None:
                return result
            effective = result
            if rec.cancel_requested and result.status in (STATUS_RUNNING, STATUS_DONE):
                effective = AgentRunResult(
                    run_id=result.run_id,
                    status=STATUS_CANCELLED,
                    stop_reason=STOP_ABORTED,
                    summary=result.summary,
                    artifacts=list(result.artifacts),
                    error_code=ERR_CANCELLED_BY_PARENT,
                    steps_used=result.steps_used,
                    tokens_used=dict(result.tokens_used),
                    child_thread_id=result.child_thread_id,
                    pending=list(result.pending),
                    resume_hint=result.resume_hint,
                    retryable=result.retryable,
                )
            rec.status = effective.status
            rec.stop_reason = effective.stop_reason
            rec.summary = effective.summary
            rec.error_code = effective.error_code
            rec.artifacts = list(effective.artifacts)
            rec.steps_used = effective.steps_used
            rec.tokens_used = dict(effective.tokens_used)
            rec.retryable = bool(effective.retryable)
            # Dialog state follows the RESULT, not only ``park_run``: a run
            # continued after an answer clears its question here, and a run
            # that parks again stores the new one. Leaving the fields stale
            # would show an answered question to the next reader.
            rec.await_reason = effective.await_reason or ""
            rec.question = effective.question
            if int(effective.dialog_rounds or 0) > int(rec.dialog_rounds or 0):
                rec.dialog_rounds = int(effective.dialog_rounds or 0)
            if getattr(effective, "status", "") != STATUS_AWAITING_INPUT \
                    or effective.await_reason != AWAIT_QUESTION:
                rec.parked_at = 0.0
            rec.heartbeat_at = time.time()
            rec.worker_running = False
        self.save_record(rid)
        # A parked run is not finished: the journal gets its "runtime outcome"
        # only when the run really ends, otherwise a later reader is told that
        # a child stopped while it actually waits for an answer.
        if effective.await_reason != AWAIT_QUESTION:
            _record_child_handoff(rec, effective)
        return effective

    def close_root_run(self, root_run_id: str,
                       status: str = STATUS_DONE) -> bool:
        """Give the dialog's own run a terminal status; True when it changed.

        A root run is not a child: it stands for one step of the dialog and has
        no worker that could ever finish it. Nothing else writes its final
        status, so without this call the record stays ``running`` on disk and
        keeps claiming that work is in progress long after the step ended.
        """
        rid = (root_run_id or "").strip()
        if not rid:
            return False
        with self._lock:
            rec = self._runs.get(rid)
            if rec is None or rec.is_terminal:
                return False
            rec.status = status
            rec.stop_reason = (STOP_COMPLETED if status == STATUS_DONE
                              else STOP_ERROR)
            rec.heartbeat_at = time.time()
        self.save_record(rid)
        return True

    def release_tree(self, root_run_id: str) -> Dict[str, Any]:
        """Forget a finished tree (records, pool, subscribers, mailbox).

        Called when the dialog finishes a step, but a BACKGROUND child may
        still be running: wiping the records then would make it uncancellable
        (``is_cancelled`` finds no record) and its ``run_finished`` would be
        dropped silently on the floor. So the release is DEFERRED while any
        record of the tree is still ``running``; the worker performs it on the
        last finish.

        Returns ``{released, deferred}`` so the caller can tell the two cases
        apart instead of assuming the tree is gone.
        """
        rid = (root_run_id or "").strip()
        if not rid:
            return {"released": False, "deferred": False}
        # The root record describes the DIALOG step, and this call happens
        # exactly when that step is over. Leaving it ``running`` is what made a
        # finished dialog look like live work forever: the record had no thread
        # behind it any more, yet every status reader reported it as running.
        self.close_root_run(rid)
        with self._lock:
            # A record holds the tree while its WORKER lives, not while its
            # status reads RUNNING: a cancelled child is CANCELLED already but
            # keeps executing to its next step boundary. Releasing here would
            # pop its record, ``is_cancelled`` would find nothing and the
            # child would run out its whole budget for a cancelled result.
            # A PARKED run counts as alive even though it has no worker: its
            # record is the only place its question lives, and dropping the
            # tree here would delete a question the user can still answer -
            # the child would wait forever for a reply nobody can deliver.
            alive = [r for r in self._runs.values()
                     if r.root_run_id == rid and r.run_id != rid
                     and (r.worker_running or r.is_parked)]
            if alive:
                tree = self._trees.get(rid)
                if tree is not None:
                    # Remember the request: the last background worker of this
                    # tree will perform the release once nothing is running.
                    tree.release_pending = True
                return {"released": False, "deferred": True}
            doomed = [r for r in self._runs.values() if r.root_run_id == rid]
            for rec in doomed:
                self._runs.pop(rec.run_id, None)
            self._trees.pop(rid, None)
        return {"released": True, "deferred": False}

    # ── Durable records ────────────────────────────────────────────────────

    def store_root(self) -> str:
        """Return the durable store root of this service.

        Read once from the installation config, never from the workspace: the
        live workspace belongs to whichever dialog is running, while records
        describe the whole installation.
        """
        root = getattr(self, "_store_root", "")
        return root or run_store.default_store_root()

    def set_store_root(self, path: str) -> None:
        """Point this service at another store root (tests and tooling)."""
        self._store_root = str(path or "")

    def save_record(self, run_id: str) -> bool:
        """Persist the current state of one run; never raises."""
        rec = self.get_record(run_id)
        if rec is None:
            return False
        try:
            return run_store.save(rec.to_record(), self.store_root())
        except Exception as exc:  # pragma: no cover - observability is best effort
            log.warning("run record save failed for %s: %s", run_id, exc)
            return False

    def refresh_heartbeat(self, run_id: str) -> None:
        """Touch the heartbeat of a run and its record on disk.

        A background run may sit inside one long tool call, so its heartbeat
        is what tells ``runs.heal`` that the process is alive; without it a
        slow-but-healthy child would be declared stale and un-resumable.
        """
        rec = self.get_record(run_id)
        if rec is None:
            return
        rec.heartbeat_at = time.time()
        self.save_record(run_id)

    def start_heartbeat(self, run_id: str) -> threading.Event:
        """Keep the heartbeat of a live run fresh until the returned event is set.

        The event-driven refresh alone is not enough: a child inside one long
        tool call publishes no event for minutes, and a reader that judges
        liveness by heartbeat age would wrongly declare it dead. The timer stops
        itself once the run is terminal, so a finished run writes nothing more.
        """
        stop = threading.Event()
        interval = float(run_store.HEARTBEAT_INTERVAL_SECONDS)

        def _beat() -> None:
            """Refresh the heartbeat until asked to stop or the run ends."""
            while not stop.wait(interval):
                try:
                    rec = self.get_record(run_id)
                    if rec is None or rec.is_terminal:
                        return
                    self.refresh_heartbeat(run_id)
                except Exception as exc:  # pragma: no cover - best effort
                    log.debug("heartbeat failed for %s: %s", run_id, exc)
                    return

        threading.Thread(target=_beat, name="orch-hb-" + str(run_id),
                         daemon=True).start()
        return stop

    def record_from_store(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return a durable record even when the in-memory one is gone.

        This is what makes a background run observable after ``release_tree``
        or a restart: the registry is a cache, the store is the truth. A
        ``running`` claim survives only while the record itself proves life -
        a live pid or a heartbeat inside the liveness window. A record with a
        dead pid is a leftover and is demoted before it reaches a caller.
        """
        try:
            record = run_store.load(run_id, self.store_root())
        except Exception as exc:  # pragma: no cover - unreadable store
            log.warning("run record load failed for %s: %s", run_id, exc)
            return None
        if record is None:
            return None
        return run_store.demote_if_dead(record, live_in_memory=False)

    def revive_parked_run(self, run_id: str,
                          limits: Optional[Dict[str, int]] = None,
                          ) -> Optional[RunRecord]:
        """Bring a waiting run back into the registry after a restart.

        A question outlives the process that asked it: the record is on disk
        while the registry is empty, and an answer would otherwise be refused
        as "not live" for a run that is perfectly answerable. Only a run that
        still waits for an ANSWER is revived - a finished, working or expired
        one is not resurrected by an answer attempt.

        The tree is re-created as well: the answer needs a pool slot and the
        child's next question needs a mailbox to reach its parent. The counters
        start fresh because the previous process released its slots and no
        worker of this process executes any run of that tree.
        """
        rid = (run_id or "").strip()
        if not rid:
            return None
        live = self.get_record(rid)
        if live is not None:
            return live
        stored = self.record_from_store(rid)
        if not stored:
            return None
        # ``record_from_store`` already denied a dead ``running`` claim and an
        # expired question, so only a genuinely answerable wait reaches here.
        if str(stored.get("status") or "") != STATUS_AWAITING_INPUT:
            return None
        if str(stored.get("await_reason") or "") != AWAIT_QUESTION:
            return None
        question = AgentQuestion.from_dict(stored.get("question"))
        if question is None:
            return None
        rec = RunRecord(
            run_id=rid,
            thread_id=str(stored.get("thread_id") or ""),
            agent_slug=str(stored.get("agent_slug") or ""),
            parent_run_id=stored.get("parent_run_id") or None,
            root_run_id=str(stored.get("root_run_id") or rid),
            depth=int(stored.get("depth") or 0),
            status=STATUS_AWAITING_INPUT,
            summary=str(stored.get("summary") or ""),
            child_thread_id=str(stored.get("child_thread_id") or ""),
            workspace=str(stored.get("workspace") or ""),
            journal_path=str(stored.get("journal_path") or ""),
            goal=str(stored.get("goal") or ""),
            enable_injection_protection=bool(
                stored.get("enable_injection_protection", True)),
            timeout_seconds=int(stored.get("timeout_seconds")
                                or DEFAULT_SUBAGENT_TIMEOUT_SECONDS),
            await_reason=AWAIT_QUESTION,
            question=question,
            dialog_rounds=int(stored.get("dialog_rounds") or 0),
            parked_at=float(stored.get("parked_at") or 0.0),
            dialog_limits={str(k): int(v) for k, v
                           in dict(stored.get("dialog_limits") or {}).items()},
            created_at=float(stored.get("created_at") or 0.0),
            steps_used=int(stored.get("steps_used") or 0),
            tokens_used=dict(stored.get("tokens_used") or {}),
        )
        root_id = rec.root_run_id or rid
        with self._lock:
            existing = self._runs.get(rid)
            if existing is not None:
                return existing
            self._runs[rid] = rec
            if root_id not in self._trees:
                pool = read_limits(limits if isinstance(limits, dict) else {})
                self._trees[root_id] = _TreeState(
                    pool["max_active_subagents"], pool["total_subagent_runs"])
            parent = self._runs.get(str(rec.parent_run_id or ""))
            if parent is not None and rid not in parent.children:
                parent.children.append(rid)
        return rec

    def list_records(self, root_run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Merge live records with durable ones, newest first, one per run.

        Live state wins over the stored copy (the store lags a heartbeat); a
        run known only to the store - a background child from a previous
        process - is still reported instead of silently disappearing.

        A stored record that still claims ``running`` and has no live
        counterpart is demoted to ``failed``/``stale_run`` only when the
        record itself proves its writer is gone (a dead pid or a heartbeat
        outside the liveness window). That verdict is what stops a crashed
        child from being reported as work in progress without ever killing a
        healthy child that another process owns; the repair of the file itself
        is ``runs.heal``'s job.
        """
        merged: Dict[str, Dict[str, Any]] = {}
        # An abandoned question must not be listed as a live wait either: the
        # sweep here also FINISHES the run, so the in-memory record and the
        # stored copy agree instead of showing two different states.
        self.expire_parked_in(root_run_id or "")
        try:
            for item in run_store.list_runs(root_run_id, self.store_root()):
                rid = str(item.get("run_id") or "")
                if rid:
                    merged[rid] = item
        except Exception as exc:  # pragma: no cover - unreadable store
            log.warning("run record list failed: %s", exc)
        with self._lock:
            live = [r for r in self._runs.values()
                    if not root_run_id or r.root_run_id == root_run_id]
        for rec in live:
            data = rec.to_record()
            data["live"] = True
            merged[rec.run_id] = data
        for rid, item in list(merged.items()):
            if item.get("live"):
                continue
            demoted = run_store.demote_if_dead(item, live_in_memory=False)
            demoted["live"] = False
            merged[rid] = demoted
        items = list(merged.values())
        items.sort(key=lambda d: float(d.get("created_at") or 0.0), reverse=True)
        return items

    def roots_for_thread(self, thread_id: str) -> List[str]:
        """Return the root runs of one dialog THREAD, newest first.

        A browser session forgets the tree root when the page reloads, while
        the run records stay durable. The root of a dialog is registered under
        its thread id, so it can be recovered from the store instead of
        leaving a waiting child invisible until the next step.
        """
        tid = (thread_id or "").strip()
        if not tid:
            return []
        roots: List[str] = []
        for item in self.list_records(None):
            if not isinstance(item, dict):
                continue
            if str(item.get("thread_id") or "") != tid:
                continue
            # Only the dialog itself is a root: a child carries its parent and
            # a run id of its own, and reporting it as a root would attach the
            # waiting line of one dialog to another.
            if item.get("parent_run_id"):
                continue
            rid = str(item.get("run_id") or "")
            if rid and rid not in roots:
                roots.append(rid)
        return roots

    # ── Notification mailbox ───────────────────────────────────────────────

    def post_notification(self, root_run_id: str, notice: Dict[str, Any]) -> bool:
        """Drop one notice into the mailbox of a tree (worker side).

        The worker never writes into the dialog thread: that structure is
        owned by Streamlit's script run, and a background thread touching it
        is a race at best and a lost message at worst. It posts here and the
        loop or the UI moves the notice into the history exactly once.
        """
        rid = (root_run_id or "").strip()
        if not rid or not isinstance(notice, dict):
            return False
        with self._lock:
            tree = self._trees.get(rid)
        if tree is None:
            return False
        with tree.lock:
            tree.mailbox.append(dict(notice))
        return True

    def take_notifications(self, root_run_id: str) -> List[Dict[str, Any]]:
        """Drain and return the notices of a tree, oldest first."""
        rid = (root_run_id or "").strip()
        if not rid:
            return []
        with self._lock:
            tree = self._trees.get(rid)
        if tree is None:
            return []
        out: List[Dict[str, Any]] = []
        with tree.lock:
            while tree.mailbox:
                out.append(tree.mailbox.popleft())
        return out

    def has_notifications(self, root_run_id: str) -> bool:
        """Return True when the tree mailbox holds at least one notice."""
        rid = (root_run_id or "").strip()
        if not rid:
            return False
        with self._lock:
            tree = self._trees.get(rid)
        if tree is None:
            return False
        with tree.lock:
            return bool(tree.mailbox)

    # ── Limits ─────────────────────────────────────────────────────────────

    def _limit_error(self, limit: str, value: int, **extra: int) -> Dict[str, Any]:
        """Build the ``limit_exceeded`` refusal in the documented shape."""
        payload: Dict[str, Any] = {
            "ok": False,
            "error": "Limit exceeded: " + limit + " = " + str(value) + ".",
            "error_code": ERR_LIMIT_EXCEEDED,
            "limit": limit,
            "value": value,
        }
        payload.update(extra)
        return payload

    def _check_depth(self, parent: Optional[RunRecord],
                     limits: Dict[str, int]) -> Optional[Dict[str, Any]]:
        """Refuse a spawn that would exceed ``max_depth``."""
        parent_depth = parent.depth if parent else 0
        if parent_depth + 1 > limits["max_depth"]:
            return self._limit_error("max_depth", limits["max_depth"],
                                     depth=parent_depth)
        return None

    def _claim_run_slot(self, root_id: str) -> Optional[Dict[str, Any]]:
        """Consume one slot of ``total_subagent_runs`` for the tree."""
        with self._lock:
            tree = self._trees.get(root_id)
            if tree is None:
                return None
            with tree.lock:
                if tree.total_used + 1 > tree.total_limit:
                    return self._limit_error(
                        "total_subagent_runs", tree.total_limit,
                        used=tree.total_used)
                tree.total_used += 1
                return None

    def acquire_pool(self, root_id: str, timeout: float = 0.0) -> "_PoolSlot":
        """Take one concurrency slot and return the slot token.

        The token carries BOTH the outcome and the owning tree. A bare
        ``Optional[tree]`` could not do this: ``None`` would mean "no tree, no
        limit to enforce" (allowed) and "pool exhausted" (refused) at the same
        time, and the two are opposites.

        The owner matters because the dialog may release and re-create the tree
        (a fresh ``_TreeState`` under the same id) while a background child
        still holds a slot. Releasing by id would then either hit the new pool -
        letting it admit one child too many - or raise ``ValueError`` on an
        over-release, which the old guard swallowed silently. The token pins the
        slot to the exact pool it came from.
        """
        with self._lock:
            tree = self._trees.get(root_id)
        if tree is None:
            # No tree: the caller is outside the delegated-limits system and
            # the call is allowed without consuming anything.
            return _PoolSlot(tree=None, granted=True)
        taken = (tree.pool.acquire(timeout=timeout) if timeout > 0
                 else tree.pool.acquire(blocking=False))
        if not taken:
            return _PoolSlot(tree=None, granted=False)
        with tree.lock:
            tree.held_slots += 1
        return _PoolSlot(tree=tree, granted=True)

    def release_pool(self, root_id: str,
                     token: Optional[object] = None) -> None:
        """Return one concurrency slot to the pool that granted it.

        ``token`` is what :meth:`acquire_pool` returned. When it is omitted the
        tree is looked up by id for backward compatibility, but callers that
        hold a slot must pass the token: only then is the release guaranteed to
        reach the pool that was actually taken from.

        A token that carries no tree means the acquisition was granted outside
        the limits system - there is nothing to credit back, so the call is a
        no-op rather than a lookup that could release a foreign slot.
        """
        tree: Optional[_TreeState]
        if token is None:
            with self._lock:
                tree = self._trees.get(root_id)
        elif isinstance(token, _PoolSlot):
            tree = token.tree
            if tree is None:
                return
        elif isinstance(token, _TreeState):
            tree = token
        else:  # pragma: no cover - a foreign token must not release anything
            return
        if tree is None:
            return
        with tree.lock:
            if tree.held_slots <= 0:
                return
            try:
                tree.pool.release()
            except ValueError:  # pragma: no cover - defensive double-release
                return
            tree.held_slots -= 1

    def pool_active(self, root_id: str) -> int:
        """Return how many slots of the tree pool are taken right now."""
        with self._lock:
            tree = self._trees.get(root_id)
        if tree is None:
            return 0
        return tree.pool_size - tree.pool._value  # noqa: SLF001 - read-only diagnostic

    # ── Events ─────────────────────────────────────────────────────────────

    def subscribe(self, run_id: str,
                  on_event: Callable[[dict], None]) -> Callable[[], None]:
        """Register an event listener for a run; returns the unsubscribe callable."""
        rid = (run_id or "").strip()
        rec = self.get_record(rid)
        if rec is None or not callable(on_event):
            return lambda: None
        with self._lock:
            tree = self._trees.get(rec.root_run_id)
        if tree is None:
            return lambda: None
        with tree.lock:
            tree.subscribers.setdefault(rid, []).append(on_event)

        def _unsubscribe() -> None:
            with tree.lock:
                listeners = tree.subscribers.get(rid) or []
                if on_event in listeners:
                    listeners.remove(on_event)
                if not listeners:
                    tree.subscribers.pop(rid, None)

        return _unsubscribe

    def _ancestors(self, run_id: str) -> List[str]:
        """Return the run itself plus every ancestor up to the root."""
        chain: List[str] = []
        cur = self.get_record(run_id)
        seen = set()
        while cur is not None and cur.run_id not in seen:
            seen.add(cur.run_id)
            chain.append(cur.run_id)
            cur = self.get_record(cur.parent_run_id) if cur.parent_run_id else None
        return chain

    def publish(self, event: dict) -> None:
        """Deliver an event to the subscribers of the run and all its ancestors."""
        if not isinstance(event, dict):
            return
        rid = str(event.get("run_id") or "")
        if not rid:
            return
        rec = self.get_record(rid)
        if rec is None:
            return
        with self._lock:
            tree = self._trees.get(rec.root_run_id)
        if tree is None:
            return
        targets = self._ancestors(rid)
        with tree.lock:
            listeners: List[Callable[[dict], None]] = []
            for target in targets:
                listeners.extend(tree.subscribers.get(target) or [])
        for cb in listeners:
            try:
                cb(event)
            except Exception as exc:  # pragma: no cover - a listener must not break a run
                log.warning("orchestration subscriber failed: %s", exc)

    # ── Cancellation ───────────────────────────────────────────────────────

    def cancel(self, run_id: str, *, cascade: bool = True) -> CancelReport:
        """Request cancellation of a run, optionally cascading to descendants.

        Idempotent: a terminal run reports ``already_terminal``. The flag is
        checked at step boundaries, so a long tool call finishes before the
        child stops - there is no safe way to kill a Python thread, and this
        limitation is part of the v1 contract.
        """
        rid = (run_id or "").strip()
        rec = self.get_record(rid)
        if rec is None:
            return CancelReport(run_id=rid, cancelled=False,
                                previous_status=STATUS_FAILED,
                                reason="unknown_run")
        # A parked run is not running, yet it is very much alive: without this
        # branch a cancellation would pass it by, the question would stay on
        # disk and the tree would keep waiting for an answer to a task nobody
        # is interested in any more.
        if rec.status not in (STATUS_RUNNING, STATUS_AWAITING_INPUT):
            return CancelReport(run_id=rid, cancelled=False,
                                previous_status=rec.status,
                                reason="already_terminal")
        cancelled_ids: List[str] = []
        with self._lock:
            rec.cancel_requested = True
            rec.status = STATUS_CANCELLED
            rec.stop_reason = STOP_ABORTED
            rec.error_code = ERR_CANCELLED_BY_PARENT
            rec.await_reason = ""
            cancelled_ids.append(rec.run_id)
            if cascade:
                stack = list(rec.children)
                while stack:
                    child = self._runs.get(stack.pop())
                    if child is None or child.status not in (STATUS_RUNNING,
                                                             STATUS_AWAITING_INPUT):
                        continue
                    child.cancel_requested = True
                    child.status = STATUS_CANCELLED
                    child.stop_reason = STOP_ABORTED
                    child.error_code = ERR_CANCELLED_BY_PARENT
                    cancelled_ids.append(child.run_id)
                    stack.extend(child.children)
        # The marker reaches the DISK before this call returns. An in-memory
        # flag dies with the process, and a cancel issued through another
        # entry point (a tool subprocess, a second window) must still be seen
        # by the worker: the worker re-reads the durable record at every step
        # boundary, and a cancel that never lands there is a cancel that
        # silently does nothing.
        for cid in cancelled_ids:
            self.save_record(cid)
        return CancelReport(run_id=rid, cancelled=True,
                            previous_status=STATUS_RUNNING,
                            reason="requested",
                            children=cancelled_ids[1:])

    def is_cancelled(self, run_id: str) -> bool:
        """True when cancellation was requested for the run.

        The in-memory record is asked first because it is never behind. When
        the registry no longer holds the run - a worker whose tree was
        released, or a cancel written by another process - the answer comes
        from the durable record instead of a silent ``False``: reporting
        "not cancelled" for missing state is what let a cancelled child run
        out its whole budget.
        """
        rec = self.get_record(run_id)
        if rec is not None:
            return bool(rec.cancel_requested)
        try:
            stored = run_store.load(run_id, self.store_root())
        except Exception:  # pragma: no cover - unreadable store
            return False
        if not stored:
            return False
        return bool(stored.get("cancel_requested"))

    # ── Agent dialog: park and unpack ──────────────────────────────────────

    def parked_runs(self, root_run_id: str) -> List[RunRecord]:
        """Return the runs of one tree that wait for an ANSWER right now.

        The tree holds them for two purposes: the parked-count guard and the
        status line that tells a human why the dialog is not idle. A run that
        waits because it ran out of autonomous continuations is NOT parked:
        nobody can answer it, it has to be resumed.
        """
        root = (root_run_id or "").strip()
        # A reader must never see a wait that can no longer be answered: the
        # sweep runs here, on the read path, because this is the one place
        # every consumer passes through - the duplicate guard below included.
        self.expire_parked_in(root)
        with self._lock:
            return [rec for rec in self._runs.values()
                    if rec.is_parked and (not root or rec.root_run_id == root)]

    def expire_parked(self, run_id: str) -> Dict[str, Any]:
        """Finish a parked run whose answer window has closed.

        A waiting run holds no pool slot, but it holds its TREE open: the
        question stays on disk, the parent may still spend an answer on it,
        and the run can never finish on its own. Once the deadline recorded
        with the question has passed, the honest outcome is ``failed`` with
        ``answer_not_received`` - not ``running`` forever and not a silent
        disappearance. The error code is deliberately separate from
        ``timeout``: nothing was working here, so the fix is to answer sooner
        or raise the wait, not to narrow the task.

        Idempotent: a run that is not parked, has no deadline or was already
        answered is left untouched.
        """
        rid = (run_id or "").strip()
        with self._lock:
            rec = self._runs.get(rid)
            if rec is None or not rec.is_parked:
                return {"expired": False, "run_id": rid}
            question = rec.question
            deadline = float(getattr(question, "deadline_at", 0.0) or 0.0)
            if deadline <= 0 or time.time() < deadline:
                return {"expired": False, "run_id": rid}
            rec.status = STATUS_FAILED
            rec.stop_reason = STOP_ERROR
            rec.error_code = ERR_ANSWER_NOT_RECEIVED
            rec.await_reason = ""
            rec.parked_at = 0.0
            rec.worker_running = False
            summary = ("Nobody answered the question before the wait window "
                       "closed. Question: "
                       + str(getattr(question, "text", "") or "")[:300])
            rec.summary = summary
        # The stowed loop state of an expired run has no reader left; keeping
        # it would pin a whole child state in memory for the life of the
        # process.
        self.take_parked_state(rid)
        self.save_record(rid)
        self.publish({"type": "run_finished", "run_id": rid,
                      "parent_run_id": rec.parent_run_id,
                      "thread_id": rec.child_thread_id,
                      "agent_slug": rec.agent_slug, "depth": rec.depth,
                      "payload": rec.to_result().to_dict()})
        # The expiring run may have been the LAST thing keeping its tree from
        # being released: a deferred release waits for exactly this moment,
        # and nobody else will come back to perform it.
        try:
            with self._lock:
                tree = self._trees.get(rec.root_run_id or "")
            if tree is not None and getattr(tree, "release_pending", False):
                self.release_tree(rec.root_run_id or "")
        except Exception as exc:  # pragma: no cover - release is best effort
            log.warning("deferred tree release failed after expiry of %s: %s",
                        rid, exc)
        return {"expired": True, "run_id": rid, "error_code": rec.error_code}

    def expire_parked_in(self, root_run_id: str = "") -> List[str]:
        """Expire every waiting run of one tree (or of the whole service).

        Returns the ids that expired. The sweep is what keeps a dialog from
        accumulating unanswerable questions: a parent that walked away without
        answering leaves rows nobody will ever look at again.
        """
        root = (root_run_id or "").strip()
        with self._lock:
            candidates = [rec.run_id for rec in self._runs.values()
                          if rec.is_parked and (not root or rec.root_run_id == root)]
        expired: List[str] = []
        for rid in candidates:
            if self.expire_parked(rid).get("expired"):
                expired.append(rid)
        return expired

    def park_run(self, run_id: str, question: AgentQuestion, *,
                 limits: Optional[Dict[str, int]] = None,
                 state: Any = None) -> Dict[str, Any]:
        """Park a child run on a question and hand control back to its parent.

        Parking does NOT finish the run: the record keeps its identity, its
        journal and the files the child already wrote, and the child loop is
        told to stop at this step boundary. The parent gets the question in
        the same call it used to launch the child (blocking) or through the
        mailbox (background).

        The dialog budget is spent HERE, not on the answer: a question that
        nobody answers still consumed a round, otherwise a child could park
        for free and the round limit would never bite.
        """
        rid = (run_id or "").strip()
        cfg = limits if isinstance(limits, dict) else {}
        # A question nobody answered must not occupy a slot of the parked-run
        # guard: without the sweep an expired wait would refuse a NEW question
        # on behalf of a run that is already finished.
        self.expire_parked_in(_tree_root_of(self, rid))
        with self._lock:
            rec = self._runs.get(rid)
            if rec is None:
                return {"ok": False,
                        "error": "Run " + rid + " is unknown; there is nothing to park.",
                        "error_code": ERR_RUN_NOT_FOUND}
            if rec.is_terminal:
                return {"ok": False,
                        "error": "Run " + rid + " is already finished.",
                        "error_code": ERR_RUN_ALREADY_FINISHED}
            # The run's own frozen budget wins over the caller's live config:
            # the caller may belong to a dialog with different settings.
            cfg = _dialog_limits_of(rec, cfg)
            rounds = int(rec.dialog_rounds or 0)
            round_limit = int(cfg.get("dialog_rounds") or DEFAULT_DIALOG_ROUNDS)
            if rounds >= round_limit:
                return {"ok": False,
                        "error": ("The dialog budget of this run is spent ("
                                  + str(round_limit) + " question/answer "
                                  "pairs). Finish with what you have and "
                                  "record the assumption in your result."),
                        "error_code": ERR_QUESTION_LIMIT,
                        "limit": "dialog_rounds", "value": round_limit,
                        "used": rounds}
            max_parked = int(cfg.get("max_parked_runs") or DEFAULT_MAX_PARKED_RUNS)
            already = sum(1 for other in self._runs.values()
                          if other.is_parked and other.root_run_id == rec.root_run_id
                          and other.run_id != rid)
            if already >= max_parked:
                return {"ok": False,
                        "error": ("Too many runs of this task wait for an answer "
                                  "(" + str(max_parked) + "). Continue with the "
                                  "most reasonable assumption and record it."),
                        "error_code": ERR_TOO_MANY_PARKED,
                        "limit": "max_parked_runs", "value": max_parked}
            now = time.time()
            wait = int(cfg.get("answer_wait_seconds") or DEFAULT_ANSWER_WAIT_SECONDS)
            rec.status = STATUS_AWAITING_INPUT
            rec.stop_reason = None
            rec.error_code = None
            rec.await_reason = AWAIT_QUESTION
            rec.question = question
            rec.parked_at = now
            rec.dialog_rounds = rounds + 1
            rec.heartbeat_at = now
        if state is not None:
            # The live loop state is stowed for a same-process answer; the
            # durable record above is what an answer after a restart relies on.
            with self._lock:
                self._parked_states[rid] = state
        self.save_record(rid)
        return {"ok": True, "run_id": rid, "parked": True,
                "dialog_rounds": rounds + 1, "limit": round_limit,
                "deadline_at": now + wait}

    def take_parked_state(self, run_id: str) -> Any:
        """Return and drop the stowed loop state of a parked run, if any."""
        with self._lock:
            return self._parked_states.pop((run_id or "").strip(), None)

    def continue_run(self, run_id: str, answer: str, *,
                     parent_config: Optional[Dict[str, Any]] = None,
                     base_dir: Optional[str] = None,
                     lang: Optional[str] = None,
                     caller_root_run_id: str = "") -> Dict[str, Any]:
        """Unpack a parked run and carry its work on to a result.

        The continuation is the SAME run: the record, the run id, the child
        thread, the journal and every file the child already wrote stay. The
        conversation is rebuilt from the child transcript, so it survives a
        restart between the question and the answer - the parked state in
        memory is used when present because it is exact, and the transcript is
        the fallback because it is durable.
        """
        rid = (run_id or "").strip()
        rec = self.get_record(rid)
        if rec is None:
            # The question may have been asked before this process started:
            # the record is durable, so an answer is still possible.
            rec = self.revive_parked_run(rid)
        if rec is None:
            return {"ok": False,
                    "error": ("Run " + rid + " is not live in this process. Its "
                              "question is on disk: read it with subagent_status "
                              "and continue it with resume_subagent."),
                    "error_code": ERR_RUN_NOT_FOUND}
        # The unpacked run keeps the dialog budget it was SPAWNED with, not
        # the live config of the process that happens to answer it.
        limits = _dialog_limits_of(rec, parent_config)
        accepted = self.answer_run(rid, answer, caller_root_run_id=caller_root_run_id,
                                   limits=limits)
        if not accepted.get("ok"):
            return accepted
        root_id = rec.root_run_id or rid
        slot = self.acquire_pool(root_id, timeout=0.0)
        if not slot:
            # The answer is recorded; the run simply has to wait for a free
            # slot. It stays parked in effect: the caller may try again.
            return {"ok": False,
                    "error": "All concurrency slots are busy; the answer is kept.",
                    "error_code": ERR_LIMIT_EXCEEDED,
                    "limit": "max_active_subagents",
                    "value": limits["max_active_subagents"],
                    "answered": True, "run_id": rid}
        deadline = time.monotonic() + int(limits["subagent_timeout_seconds"])
        prior_state = self.take_parked_state(rid)
        try:
            result = self._run_child(
                child_slug=rec.agent_slug or "",
                brief=AgentBrief(goal=rec.goal or "", brief=""),
                child_thread_id=rec.child_thread_id,
                run_id=rid,
                parent_run_id=rec.parent_run_id,
                depth=rec.depth,
                limits=limits,
                deadline=deadline,
                base_dir=base_dir or rec.workspace or None,
                lang=lang,
                parent_thread_id=rec.thread_id,
                enable_injection_protection=bool(rec.enable_injection_protection),
                prior_run=True,
                answer_text=answer,
                prior_state=prior_state,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("continued run %s failed: %s", rid, exc)
            result = AgentRunResult(run_id=rid, status=STATUS_FAILED,
                                    stop_reason=STOP_ERROR,
                                    summary=type(exc).__name__ + ": " + str(exc),
                                    error_code=ERR_INTERNAL_ERROR,
                                    retryable=False)
        finally:
            self.release_pool(root_id, slot)

        if result.status == STATUS_AWAITING_INPUT and result.await_reason == AWAIT_QUESTION:
            # It asked again: the run stays parked, and the caller gets the new
            # question in place of a result. No extra finish, no new run.
            return {"ok": True, "run_id": rid, "parked": True,
                    "result": result.to_dict()}
        effective = self.finish_run(rid, result)
        if not effective.child_thread_id and rec.child_thread_id:
            from dataclasses import replace as _dc_replace
            effective = _dc_replace(effective, child_thread_id=rec.child_thread_id)
        self.publish({"type": "run_finished", "run_id": rid,
                      "parent_run_id": rec.parent_run_id,
                      "thread_id": rec.child_thread_id,
                      "agent_slug": rec.agent_slug, "depth": rec.depth,
                      "payload": effective.to_dict()})
        return {"ok": True, "result": effective.to_dict()}

    def answer_run(self, run_id: str, answer: str, *,
                   caller_root_run_id: str = "",
                   answered_by: str = "",
                   limits: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """Record an answer to a parked run and put it back to work.

        The answer is accepted only for a run of the CALLER's own tree that
        currently waits on a question. Everything else is refused with a code
        the model can act on - answering a working run would deliver a reply
        to a question nobody asked, and answering a foreign one would cross a
        tree boundary nobody granted.
        """
        rid = (run_id or "").strip()
        text = str(answer or "").strip()
        cfg = limits if isinstance(limits, dict) else {}
        if not text:
            return {"ok": False, "error": "An answer cannot be empty.",
                    "error_code": ERR_INVALID_ARGS}
        limit_chars = int(cfg.get("answer_max_chars") or DEFAULT_ANSWER_MAX_CHARS)
        if len(text) > limit_chars:
            return {"ok": False,
                    "error": ("The answer is too long (" + str(len(text)) + " > "
                              + str(limit_chars) + " characters). An answer is a "
                              "decision, not a new briefing: shorten it, or start "
                              "a new delegation for a new scope."),
                    "error_code": ERR_INVALID_ARGS,
                    "limit": "answer_max_chars", "value": limit_chars}
        with self._lock:
            rec = self._runs.get(rid)
        if rec is None:
            # An answer may arrive in a process that never saw the question.
            rec = self.revive_parked_run(rid, limits=cfg)
        if rec is None:
            return {"ok": False,
                    "error": ("Run " + rid + " is not live in this process. "
                              "Its question is on disk: read it with "
                              "subagent_status and resume the run."),
                    "error_code": ERR_RUN_NOT_FOUND}
        with self._lock:
            if caller_root_run_id and rec.root_run_id != str(caller_root_run_id):
                return {"ok": False,
                        "error": "Run " + rid + " belongs to another tree.",
                        "error_code": ERR_NOT_YOUR_RUN}
            if rec.is_terminal:
                return {"ok": False,
                        "error": "Run " + rid + " is already finished.",
                        "error_code": ERR_RUN_ALREADY_FINISHED}
            if not rec.is_parked:
                return {"ok": False,
                        "error": ("Run " + rid + " is not waiting for an answer "
                                  "(status " + str(rec.status) + "). Nothing to "
                                  "unpack."),
                        "error_code": ERR_NOTHING_TO_ANSWER}
            # The deadline is checked BEFORE the answer is recorded: a reply
            # that arrives after the window closed must not revive a run the
            # reader already sees as failed, or two different truths about one
            # run would live at the same time.
            current = rec.question or AgentQuestion()
            deadline = float(getattr(current, "deadline_at", 0.0) or 0.0)
            expired_outcome = None
            if deadline > 0 and time.time() >= deadline:
                rec.status = STATUS_FAILED
                rec.stop_reason = STOP_ERROR
                rec.error_code = ERR_ANSWER_NOT_RECEIVED
                rec.await_reason = ""
                rec.parked_at = 0.0
                rec.worker_running = False
                rec.summary = ("Nobody answered the question before the wait "
                               "window closed. Question: "
                               + str(current.text or "")[:300])
                expired_outcome = {
                    "ok": False,
                    "error": ("The wait window of this question closed at "
                              + time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(deadline))
                              + ". The run is finished; launch a new one "
                              "with spawn_agent if the work is still needed."),
                    "error_code": ERR_ANSWER_NOT_RECEIVED,
                    "run_id": rid, "expired": True}
            if expired_outcome is not None:
                current = None
            else:
                answered = AgentQuestion(
                    text=current.text, options=list(current.options),
                    done=current.done, blocked=current.blocked,
                    asked_at=current.asked_at or rec.parked_at,
                    deadline_at=current.deadline_at,
                    answer=text, answered_at=time.time(),
                    answered_by=answered_by or "parent")
                rec.question = answered
                rec.status = STATUS_RUNNING
                rec.await_reason = ""
                rec.stop_reason = None
                rec.error_code = None
                rec.parked_at = 0.0
                rec.worker_running = True
                rec.heartbeat_at = time.time()
        if expired_outcome is not None:
            # The stowed loop state of a run nobody answered has no reader
            # left; keeping it would pin a whole child state in memory.
            self.take_parked_state(rid)
            self.save_record(rid)
            return expired_outcome
        self.save_record(rid)
        return {"ok": True, "run_id": rid, "answered": True,
                "dialog_rounds": int(rec.dialog_rounds or 0),
                "question": answered.to_dict()}

    # ── Child runner seam ──────────────────────────────────────────────────

    def set_child_runner(self, runner: Optional[Callable[..., AgentRunResult]]) -> None:
        """Install a child runner (tests use this instead of a real loop)."""
        self._child_runner = runner

    def _run_child(self, *, child_slug: str, brief: AgentBrief,
                   child_thread_id: str, run_id: str,
                   parent_run_id: Optional[str], depth: int,
                   limits: Dict[str, int], deadline: float,
                   base_dir: Optional[str], lang: Optional[str],
                   parent_thread_id: str = "",
                   enable_injection_protection: bool = True,
                   prior_run: bool = False,
                   answer_text: str = "",
                   prior_state: Any = None) -> AgentRunResult:
        """Execute one child run through the configured runner.

        ``parent_thread_id`` and ``enable_injection_protection`` reach a
        runner that declares them OR accepts ``**kwargs``; a runner with a
        FIXED old signature does not receive them. The runner is a test seam,
        and an existing fake must keep working instead of failing on an
        unexpected keyword.

        ``prior_run`` marks a CONTINUATION of a parked run: the child loop is
        resumed from ``prior_state`` (same process) or rebuilt from its own
        transcript (after a restart), and ``answer_text`` is delivered as the
        parent's reply. Those three keys are passed through the same
        signature check, so a fake runner that does not know them still works.
        """
        runner = self._child_runner or _default_child_runner
        kwargs = dict(
            child_slug=child_slug,
            brief=brief,
            child_thread_id=child_thread_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            depth=depth,
            limits=limits,
            deadline=deadline,
            base_dir=base_dir,
            lang=lang,
            service=self,
        )
        try:
            import inspect as _inspect
            parameters = _inspect.signature(runner).parameters
            # A runner with **kwargs accepts both keys by definition; a runner
            # with a FIXED old signature must not receive them (the seam keeps
            # working for every existing fake).
            accepts_extra = any(p.kind is _inspect.Parameter.VAR_KEYWORD
                                for p in parameters.values())
            if "parent_thread_id" in parameters or accepts_extra:
                kwargs["parent_thread_id"] = parent_thread_id
            if "enable_injection_protection" in parameters or accepts_extra:
                kwargs["enable_injection_protection"] = bool(enable_injection_protection)
            if prior_run:
                if "prior_run" in parameters or accepts_extra:
                    kwargs["prior_run"] = True
                if "answer_text" in parameters or accepts_extra:
                    kwargs["answer_text"] = str(answer_text or "")
                if "prior_state" in parameters or accepts_extra:
                    kwargs["prior_state"] = prior_state
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            pass
        return runner(**kwargs)

    # ── Spawning ───────────────────────────────────────────────────────────

    def run(self, req: SpawnRequest, *,
            parent_config: Optional[Dict[str, Any]] = None,
            base_dir: Optional[str] = None,
            lang: Optional[str] = None) -> Dict[str, Any]:
        """Run one child synchronously and return the tool-shaped result.

        Refusals happen BEFORE any work: depth, tree total, and pool slot are
        checked first, and each refusal carries the documented
        ``limit_exceeded`` payload with the limit name and value. No queue is
        involved - an over-limit spawn fails immediately so the model can see
        the cause and narrow the task.
        """
        prep = self._prepare_child(req, parent_config=parent_config)
        if prep.get("ok") is False:
            return prep
        return self._execute_child(prep, req=req, base_dir=base_dir, lang=lang)

    def _prepare_child(self, req: SpawnRequest, *,
                       parent_config: Optional[Dict[str, Any]] = None,
                       block_pool: bool = True,
                       caller_root_run_id: Optional[str] = None) -> Dict[str, Any]:
        """Validate a spawn and reserve its identity, record and pool slot.

        Everything that can REFUSE the call happens here, synchronously, so the
        caller learns about a limit in the same turn it asked - a background
        launch must not accept work it can never run.

        ``block_pool=False`` makes the concurrency check non-blocking. That is
        what a background launch needs: waiting inside a tool call would
        freeze the dialog while looking like a hang. A blocking spawn waits
        for the pool no longer than its own run budget and never past
        ``DEFAULT_POOL_WAIT_SECONDS``, so a busy pool cannot turn one call
        into an hour-long stall.

        ``req.resume_run_id`` turns the call into a CONTINUATION: the new run
        inherits the tree of the original (same root, same depth, same parent)
        instead of starting a fresh one. Without that inheritance every resume
        would reset ``max_depth`` and ``total_subagent_runs``, so a caller could
        delegate downwards indefinitely by resuming. The chain length is capped
        by ``MAX_RESUME_CHAIN`` for the same reason, and ``caller_root_run_id``
        confines resumes to the caller's own tree.
        """
        if req.inline_orchestrator:
            return {"ok": False,
                    "error": "inline_orchestrator is not implemented yet.",
                    "error_code": ERR_NOT_IMPLEMENTED}

        source: Optional[Dict[str, Any]] = None
        if req.resume_run_id and not caller_root_run_id:
            # Fail closed: confinement must not depend on the caller
            # remembering to pass its own root. A blocked spawn derives it from
            # the parent run record; a dialog-level call is its own root.
            caller_parent = (self.get_record(req.parent_run_id or "")
                             if req.parent_run_id else None)
            caller_root_run_id = (caller_parent.root_run_id
                                  if caller_parent is not None
                                  else (req.thread_id or None))
        if req.resume_run_id:
            rid_source = (req.resume_run_id or "").strip()
            live = self.get_record(rid_source)
            source = live.to_record() if live is not None else self.record_from_store(rid_source)
            if source is None:
                return {"ok": False,
                        "error": "Nothing to resume: run " + rid_source + " is unknown.",
                        "error_code": ERR_RESUME_UNAVAILABLE}
            if (caller_root_run_id
                    and str(source.get("root_run_id") or "") != str(caller_root_run_id)):
                return {"ok": False,
                        "error": "Run " + rid_source + " belongs to another tree.",
                        "error_code": ERR_NOT_YOUR_RUN}
            chain = int(source.get("resume_depth") or 0)
            if chain >= MAX_RESUME_CHAIN:
                return {"ok": False,
                        "error": ("Resume chain limit reached (" + str(MAX_RESUME_CHAIN)
                                  + "): start a new delegation instead."),
                        "error_code": ERR_RESUME_CHAIN_LIMIT,
                        "limit": "resume_chain", "value": chain}

        parent = self.get_record(req.parent_run_id or "") if req.parent_run_id else None
        if parent is None and source is not None and source.get("parent_run_id"):
            parent = self.get_record(str(source.get("parent_run_id")))
        if parent is None and req.thread_id:
            # The dialog registered no explicit run for this thread: treat the
            # thread itself as the root so limits still apply.
            parent = self.ensure_run(req.thread_id, thread_id=req.thread_id,
                                     config=parent_config)
        root_id = parent.root_run_id if parent else req.thread_id or ""
        if source is not None:
            root_id = str(source.get("root_run_id") or root_id)
        if not root_id:
            return {"ok": False,
                    "error": "A spawn needs a root run or thread: limits cannot be enforced.",
                    "error_code": ERR_NO_RUN_IDENTITY}
        limits = read_limits(parent_config if parent_config is not None
                             else getattr(self, "_last_config", None))

        refusal = self._check_depth(parent, limits)
        if refusal is not None:
            return refusal
        refusal = self._claim_run_slot(root_id)
        if refusal is not None:
            return refusal

        run_id = uuid.uuid4().hex[:12]
        depth = (parent.depth if parent else 0) + 1
        if source is not None:
            # A resume continues the SAME depth: it is the same unit of work,
            # not one level deeper.
            depth = int(source.get("depth") or depth)
        parent_thread = (parent.thread_id if parent and parent.thread_id
                         else req.thread_id or "")
        if not parent_thread and source is not None:
            parent_thread = str(source.get("thread_id") or "")
        child_thread_id = derive_child_thread_id(parent_thread, run_id)

        slot = self.acquire_pool(
            root_id,
            timeout=(min(float(limits["subagent_timeout_seconds"]),
                         float(DEFAULT_POOL_WAIT_SECONDS)) if block_pool else 0.0))
        if not slot:
            self._release_run_slot(root_id)
            return self._limit_error("max_active_subagents",
                                     limits["max_active_subagents"],
                                     active=self.pool_active(root_id))

        rec = self.ensure_run(run_id, thread_id=child_thread_id,
                              agent_slug=req.agent_slug or "",
                              parent_run_id=parent.run_id if parent else None,
                              depth=depth)
        rec.workspace = str(req.workspace or "")
        rec.goal = str(getattr(req.request, "goal", "") or "")
        # Injection protection is inherited, not defaulted: the child runs with
        # the mode of the run that started it. A resume continues the SAME unit
        # of work, so it takes the mode frozen in the source run unless the
        # caller states a different one explicitly; a fresh spawn without an
        # argument keeps the safe default. Resolving it here (once, before the
        # child thread exists) is what makes the inheritance reliable for a
        # background run.
        if req.enable_injection_protection is not None:
            rec.enable_injection_protection = bool(req.enable_injection_protection)
        elif source is not None and source.get("enable_injection_protection") is not None:
            rec.enable_injection_protection = bool(source.get("enable_injection_protection"))
        else:
            rec.enable_injection_protection = True
        rec.timeout_seconds = int(limits["subagent_timeout_seconds"])
        # The dialog budget is part of the launch context, exactly like the
        # timeout: an answer may arrive from another process, and the run must
        # still be judged by the rounds it started with.
        rec.dialog_limits = {key: int(limits[key]) for key in _DIALOG_LIMIT_KEYS}
        rec.created_at = time.time()
        rec.heartbeat_at = rec.created_at
        if source is not None:
            rec.resumed_from = str(source.get("run_id") or "") or None
            rec.resume_depth = int(source.get("resume_depth") or 0) + 1
        # Always the NEW child's own journal: a resumed run writes its own
        # record of what it did, and a later resume of THAT run reads this path.
        rec.journal_path = self._child_journal_path(child_thread_id)
        self.publish({"type": "run_started", "run_id": run_id,
                      "parent_run_id": req.parent_run_id, "thread_id": child_thread_id,
                      "agent_slug": req.agent_slug, "depth": depth, "payload": {}})
        return {"ok": True, "run_id": run_id, "depth": depth,
                "child_thread_id": child_thread_id, "parent_thread": parent_thread,
                "root_id": root_id, "limits": limits, "slot": slot, "rec": rec}

    def _child_journal_path(self, child_thread_id: str) -> str:
        """Return the absolute journal path of a child thread, best effort.

        Frozen on the record at spawn time so a resume can read the journal of
        a run whose thread is no longer the active one - resolving it later
        would depend on process-wide state that may already belong to another
        dialog.
        """
        tid = str(child_thread_id or "").strip()
        if not tid:
            return ""
        try:
            from dev_agent.task_state import task_state_path
            return str(task_state_path(tid))
        except Exception as exc:  # pragma: no cover - journal is best effort
            log.debug("child journal path resolution failed: %s", exc)
            return ""

    def _execute_child(self, prep: Dict[str, Any], *, req: SpawnRequest,
                       base_dir: Optional[str] = None,
                       lang: Optional[str] = None) -> Dict[str, Any]:
        """Run one prepared child to completion and publish its outcome.

        Shared by the blocking ``run`` and the background ``start``: the only
        difference between them is which thread calls this method. The pool slot
        reserved by :meth:`_prepare_child` is released in ``finally``, so a crash
        inside the child cannot leak a concurrency slot.

        The event published at the end carries the EFFECTIVE result: a run that
        was cancelled while finishing must not announce ``done``.
        """
        run_id = prep["run_id"]
        depth = prep["depth"]
        child_thread_id = prep["child_thread_id"]
        parent_thread = prep["parent_thread"]
        root_id = prep["root_id"]
        limits = prep["limits"]
        slot = prep["slot"]

        # Freeze the identity and the workspace on the record before the child
        # starts working. Both are resolved only here, where ``base_dir`` is
        # known: without them a caller cannot address the child thread or tell
        # where it wrote, and the durable record would carry empty strings for
        # the whole life of the run.
        rec = prep.get("rec")
        if rec is not None:
            rec.child_thread_id = child_thread_id or ""
            if not rec.workspace and base_dir:
                rec.workspace = str(base_dir)

        deadline = time.monotonic() + limits["subagent_timeout_seconds"]
        # A reader that judges liveness by heartbeat age must see a fresh beat
        # even while the child sits inside one long tool call. Only a BACKGROUND
        # child needs the timer: a blocking one keeps its record in memory until
        # it returns, and presence in the registry is the stronger signal there.
        heartbeat_stop = None
        if getattr(req, "background", False) or getattr(rec, "background", False):
            heartbeat_stop = self.start_heartbeat(run_id)
        try:
            result = self._run_child(
                child_slug=req.agent_slug or "",
                brief=req.request,
                child_thread_id=child_thread_id,
                run_id=run_id,
                parent_run_id=req.parent_run_id,
                depth=depth,
                limits=limits,
                deadline=deadline,
                base_dir=base_dir,
                lang=lang,
                parent_thread_id=parent_thread,
                enable_injection_protection=bool(
                    getattr(prep.get("rec"), "enable_injection_protection", True)),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("child run %s failed: %s", run_id, exc)
            result = AgentRunResult(run_id=run_id, status=STATUS_FAILED,
                                    stop_reason=STOP_ERROR,
                                    summary=type(exc).__name__ + ": " + str(exc),
                                    error_code=ERR_INTERNAL_ERROR,
                                    retryable=False)
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self.release_pool(root_id, slot)

        if self.is_cancelled(run_id) and result.status == STATUS_RUNNING:
            result = AgentRunResult(run_id=run_id, status=STATUS_CANCELLED,
                                    stop_reason=STOP_ABORTED,
                                    summary=result.summary,
                                    artifacts=result.artifacts,
                                    error_code=ERR_CANCELLED_BY_PARENT,
                                    steps_used=result.steps_used,
                                    tokens_used=result.tokens_used)
        effective = self.finish_run(run_id, result)
        # A runner that is not the real agent loop does not know its own child
        # thread, and a parent addressing the journal needs it in the RESULT,
        # not only in the registry record. Filling it here keeps the two views
        # consistent for every runner, including test seams.
        if not effective.child_thread_id and child_thread_id:
            from dataclasses import replace as _dc_replace
            effective = _dc_replace(effective, child_thread_id=child_thread_id)
        self.publish({"type": "run_finished", "run_id": run_id,
                      "parent_run_id": req.parent_run_id,
                      "thread_id": child_thread_id,
                      "agent_slug": req.agent_slug, "depth": depth,
                      "payload": effective.to_dict()})
        return {"ok": True, "result": effective.to_dict()}

    # ── Background runs ────────────────────────────────────────────────────

    def start(self, req: SpawnRequest, *,
              parent_config: Optional[Dict[str, Any]] = None,
              base_dir: Optional[str] = None,
              lang: Optional[str] = None) -> Dict[str, Any]:
        """Launch a child on its own thread and return at once.

        Everything that can refuse the call - depth, tree total, pool slot -
        is checked HERE, synchronously, so the caller never receives a
        ``running`` run id for work that will not happen. Only the child's own
        execution moves to the background.

        The worker owns the run from then on: it finishes the record, claims
        the notice and performs a deferred tree release. The caller of ``start``
        must not touch the record afterwards - it observes through ``poll``,
        ``wait`` or the mailbox.
        """
        prep = self._prepare_child(req, parent_config=parent_config,
                                   block_pool=False)
        if prep.get("ok") is False:
            return prep
        run_id = prep["run_id"]
        rec = prep["rec"]
        rec.background = True
        self.save_record(run_id)

        worker = threading.Thread(
            target=self._background_worker,
            args=(prep, req, base_dir, lang),
            name="orch-run-" + str(run_id),
            daemon=True,
        )
        worker.start()
        return {"ok": True, "run_id": run_id, "status": STATUS_RUNNING,
                "background": True, "child_thread_id": prep["child_thread_id"],
                "depth": prep["depth"]}

    def _background_worker(self, prep: Dict[str, Any], req: SpawnRequest,
                           base_dir: Optional[str],
                           lang: Optional[str]) -> None:
        """Run one prepared child off-thread and publish exactly one notice.

        A daemon thread cannot be joined by the interpreter, so every step here
        is written to leave a consistent record behind even when the process
        exits mid-run: the record is already on disk from ``start`` and
        ``runs.heal`` will turn a dead ``running`` claim into ``failed``.

        The notice is claimed with ``O_EXCL`` instead of being marked inside the
        message text: two writers would both see no marker and both write, so
        only a filesystem primitive gives exactly-once delivery.
        """
        run_id = prep["run_id"]
        root_id = prep["root_id"]
        try:
            payload = self._execute_child(prep, req=req, base_dir=base_dir, lang=lang)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("background run %s crashed: %s", run_id, exc)
            payload = {"ok": False,
                       "error": "background run failed: " + str(exc),
                       "error_code": ERR_INTERNAL_ERROR, "retryable": False}
            rec = self.get_record(run_id)
            if rec is not None and not rec.is_terminal:
                self.finish_run(run_id, AgentRunResult(
                    run_id=run_id, status=STATUS_FAILED, stop_reason=STOP_ERROR,
                    summary=type(exc).__name__ + ": " + str(exc),
                    error_code=ERR_INTERNAL_ERROR, retryable=False))

        result = dict((payload or {}).get("result") or {})
        status = str(result.get("status") or STATUS_FAILED)
        notice = {
            "type": "run_finished",
            "run_id": run_id,
            "root_run_id": root_id,
            "parent_run_id": req.parent_run_id,
            "agent_slug": req.agent_slug,
            "status": status,
            "child_thread_id": prep.get("child_thread_id", ""),
            "payload": result,
        }
        if run_store.claim_notice(run_id, status, self.store_root(),
                                  discriminator=_notice_key(result)):
            self.post_notification(root_id, notice)
        else:
            log.info("background notice for %s/%s was already claimed", run_id, status)

        try:
            with self._lock:
                tree = self._trees.get(root_id)
            if tree is not None and getattr(tree, "release_pending", False):
                self.release_tree(root_id)
        except Exception as exc:  # pragma: no cover - release is best effort
            log.warning("deferred tree release failed for %s: %s", root_id, exc)

    def poll(self, run_id: str, **_: object) -> Dict[str, Any]:
        """Return the current status of one run, live or durable.

        The in-memory record is preferred because it is never behind, but a
        child that outlived ``release_tree`` or a process restart is answered
        from the store instead of a misleading ``run_not_found``.
        """
        rid = (run_id or "").strip()
        rec = self.get_record(rid)
        if rec is not None:
            payload = rec.to_record()
            payload["live"] = True
            return {"ok": True, "run": payload}
        stored = self.record_from_store(rid)
        if stored is not None:
            stored["live"] = False
            return {"ok": True, "run": stored}
        return {"ok": False, "run_id": rid,
                "error": "Unknown run: " + rid,
                "error_code": ERR_RUN_NOT_FOUND}

    def wait(self, run_ids: List[str], timeout: float = 0.0,
             **_: object) -> List[Dict[str, Any]]:
        """Block until every run is terminal or the timeout expires.

        Positions are preserved, so the caller can map results back to the ids
        it passed. A timeout does not raise: the runs that are still working
        come back with ``status: running`` and a ``timed_out: True`` marker.
        """
        ids = [str(r or "").strip() for r in (run_ids or [])]
        if not ids:
            return []
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        interval = 0.05
        while True:
            out: List[Dict[str, Any]] = []
            pending = 0
            for rid in ids:
                item = self.poll(rid)
                run = dict(item.get("run") or {})
                if str(run.get("status") or "") == STATUS_RUNNING:
                    pending += 1
                out.append(item)
            if pending == 0 or (timeout and time.monotonic() >= deadline):
                if pending:
                    for item in out:
                        run = item.get("run") or {}
                        if str(run.get("status") or "") == STATUS_RUNNING:
                            run["timed_out"] = True
                return out
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(0.5, interval * 2)

    def list_runs(self, root_run_id: Optional[str] = None,
                  **_: object) -> List[Dict[str, Any]]:
        """Return the runs of one tree (or of all trees), newest first."""
        return self.list_records(root_run_id)

    def _release_run_slot(self, root_id: str) -> None:
        """Return one unit of the tree total counter (after a failed claim)."""
        with self._lock:
            tree = self._trees.get(root_id)
            if tree is None:
                return
            with tree.lock:
                if tree.total_used > 1:
                    tree.total_used -= 1

    # ── One-shot invocations ───────────────────────────────────────────────

    def invoke(self, req: InvokeRequest, *,
               parent_config: Optional[Dict[str, Any]] = None,
               parent_slug: str = "dev_agent",
               base_dir: Optional[str] = None,
               lang: Optional[str] = None,
               invoke_fn=None) -> Dict[str, Any]:
        """Run one one-shot assistant call under the shared pool.

        A one-shot call is not a child (no thread, no journal), but it is the
        same provider load, so it takes a slot of ``max_active_subagents`` for
        the duration of the network call and obeys
        ``subagent_timeout_seconds``. The token budget does not apply: a
        one-shot call has no steps, only input and output.
        """
        parent = self.get_record(req.parent_run_id or "") if req.parent_run_id else None
        root_id = parent.root_run_id if parent else ""
        limits = read_limits(parent_config)
        slot = self.acquire_pool(root_id,
                                 timeout=min(float(limits["subagent_timeout_seconds"]),
                                             float(DEFAULT_POOL_WAIT_SECONDS)))
        if not slot:
            return self._limit_error("max_active_subagents",
                                     limits["max_active_subagents"],
                                     active=self.pool_active(root_id))
        try:
            if invoke_fn is None:
                from core.orchestration.invoke import invoke_once as invoke_fn
            return invoke_fn(req, parent_slug=parent_slug, base_dir=base_dir, lang=lang)
        finally:
            self.release_pool(root_id, slot)

    # ── Fan-out ────────────────────────────────────────────────────────────

    def spawn_many(self, requests: List[SpawnRequest], *,
                   parent_config: Optional[Dict[str, Any]] = None,
                   parent_configs: Optional[List[Dict[str, Any]]] = None,
                   base_dir: Optional[str] = None,
                   lang: Optional[str] = None) -> List[Dict[str, Any]]:
        """Run several spawn requests in parallel, results in call order.

        Every request is started on its own thread and the call blocks until
        all of them finish. Results are returned positionally, so the model
        sees a predictable list that matches the order of its calls. A
        refusal of one request never cancels the others: that is the whole
        point of validating the batch up front and letting valid calls run.
        """
        if not requests:
            return []
        results: List[Optional[Dict[str, Any]]] = [None] * len(requests)
        threads: List[threading.Thread] = []

        def _worker(idx: int, request: SpawnRequest, cfg: Optional[Dict[str, Any]]) -> None:
            try:
                results[idx] = self.run(request, parent_config=cfg,
                                        base_dir=base_dir, lang=lang)
            except Exception as exc:  # pragma: no cover - defensive
                results[idx] = {"ok": False,
                                "error": "delegation failed: " + str(exc),
                                "error_code": ERR_PROVIDER_ERROR}

        for idx, request in enumerate(requests):
            cfg = parent_configs[idx] if parent_configs and idx < len(parent_configs) else parent_config
            thread = threading.Thread(target=_worker, args=(idx, request, cfg),
                                      name="orch-spawn-" + str(idx), daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        return [r if r is not None else {"ok": False,
                                          "error": "delegation produced no result",
                                          "error_code": ERR_PROVIDER_ERROR}
                for r in results]


def derive_child_thread_id(parent_thread_id: str, run_id: str) -> str:
    """Return the derived thread id of a child run.

    The child gets its OWN thread id and its OWN journal file: ``thread_id``
    is a pointer to the journal path, and inheriting the parent's would mean
    delegated work writing into the parent's Active Task (and
    ``task_state_init`` would archive the parent's working state). The
    workspace folder is inherited - only the journal differs.
    """
    parent = (parent_thread_id or "").strip() or "nothread"
    return parent + "__s" + (run_id or "")[:8]


# ─── Default child runner (the real agent loop) ──────────────────────────────


def _thread_files_dir_of(thread_id: Optional[str]) -> str:
    """Return the absolute dialog-uploads folder of a thread, or an empty string.

    The child gets this path in its task text so it can read the parent's
    uploads by an absolute path without probing the filesystem. An empty or
    unknown thread simply yields no line - the child keeps working from its
    own binding.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return ""
    try:
        from core.threads_devagent import _thread_files_dir
        return os.path.abspath(_thread_files_dir(tid))
    except Exception:  # pragma: no cover - defensive
        return ""


def _build_child_task_text(brief: AgentBrief, base_dir: Optional[str],
                          parent_thread_id: Optional[str]) -> str:
    """Compose the task text of a child run: goal, brief and absolute paths.

    The child starts with a CLEAN history and its own workspace binding, so
    it cannot inherit the parent's idea of "current folder" or its dialog
    uploads. Everything it may need is stated here: the goal, the brief, the
    input artifacts and two absolute paths - the shared workspace and the
    parent's dialog files folder. Without them the child spends its first
    steps guessing where the files are.
    """
    task_text = str(brief.goal or "")
    if brief.brief:
        task_text += "\n\n" + str(brief.brief)
    if brief.artifacts_in:
        lines = ["\n\nВходные артефакты:"]
        for name, path in brief.artifacts_in.items():
            lines.append("- " + str(name) + ": " + str(path))
        task_text += "\n".join(lines)

    context_lines: List[str] = []
    # A child is an executor, not a dialog partner. Without these lines it
    # starts with the full agent prompt, where Stage 1 demands a plan and an
    # approval stop - and a child has nobody to approve anything, so it
    # spends its step budget re-presenting a plan nobody will read.
    context_lines.append(
        "- Роль: делегированный исполнитель. Пользователя в этом диалоге нет, "
        "план утверждать некому."
    )
    context_lines.append(
        "- План в ответе не печатай: запиши его в журнал задачи "
        "(task_state_init) и сразу переходи к выполнению."
    )
    context_lines.append(
        "- Завершив работу, ответь итогом и закончи ответ строкой "
        '{"loop_status": "done"}. Значение "awaiting_user" здесь означает '
        "незавершённую работу и останавливает родителя."
    )
    workspace_root = str(base_dir or "").strip()
    if workspace_root:
        context_lines.append("- Рабочая папка: " + os.path.abspath(workspace_root))
    thread_files = _thread_files_dir_of(parent_thread_id)
    if thread_files:
        context_lines.append(
            "- Файлы диалога родителя: " + thread_files
            + " (передавай эти пути целиком, они абсолютные)"
        )
    if context_lines:
        task_text += ("\n\nКонтекст запуска (абсолютные пути):\n"
                      + "\n".join(context_lines))
    return task_text


def _tree_root_of(service: "OrchestrationService", run_id: str) -> str:
    """Return the root run id of the tree that owns ``run_id``.

    The mailbox hangs off the ROOT run: a worker posts a notice to the tree
    that owns the concurrency pool, not to its own record. When the record is
    already gone the run's own id is the best available answer - it at least
    keeps the drain from reading a foreign tree.
    """
    rid = (run_id or "").strip()
    if not rid:
        return ""
    try:
        rec = service.get_record(rid)
    except Exception:  # pragma: no cover - registry is best effort
        rec = None
    root = str(getattr(rec, "root_run_id", "") or "").strip()
    return root or rid


def _default_child_runner(*, child_slug: str, brief: AgentBrief,
                          child_thread_id: str, run_id: str,
                          parent_run_id: Optional[str], depth: int,
                          limits: Dict[str, int], deadline: float,
                          base_dir: Optional[str], lang: Optional[str],
                          service: "OrchestrationService",
                          parent_thread_id: str = "",
                          enable_injection_protection: bool = True,
                          prior_run: bool = False,
                          answer_text: str = "",
                          prior_state: Any = None) -> AgentRunResult:
    """Run one child orchestrator through the real agent loop.

    The child is a full agent: its own dispatcher, its own journal thread and
    its own tool catalog, narrowed to the tools the parent has. Platform
    entity creation (``create_orchestrator``, ``create_assistant_for_task``
    and friends) is excluded by ``CHILD_DISABLED_TOOLS`` - a child does not
    multiply platform entities in v1.
    """
    from core.orchestration.contracts import (
        ERR_PROVIDER_ERROR,
        ERR_UNKNOWN_AGENT,
    )
    from core.orchestrators import build_assistant_dicts, get_orchestrator

    slug = (child_slug or "").strip()
    orch = get_orchestrator(slug) if slug else None
    if not orch:
        return AgentRunResult(
            run_id=run_id, status=STATUS_FAILED, stop_reason=STOP_ERROR,
            summary="Unknown agent: " + (slug or "<empty>"),
            error_code=ERR_UNKNOWN_AGENT)

    # Task text: the goal plus everything the child must know. The child gets
    # a CLEAN history, so goal + brief + absolute paths is the only channel
    # besides artifacts.
    task_text = _build_child_task_text(brief, base_dir, parent_thread_id)

    # A CONTINUATION of a parked run does not start a new conversation: the
    # transcript already holds everything the child saw and said, and the
    # answer is the one thing it has not seen yet. Reusing the stowed state
    # when it is available keeps the exact conversation; the transcript is
    # the durable fallback for an answer that arrives after a restart.
    resume_history: List[Dict[str, Any]] = []
    if prior_run:
        if prior_state is not None and getattr(prior_state, "history", None):
            resume_history = list(prior_state.history)
        else:
            try:
                from core.thread_mirror import _transcript
                T = _transcript()
                for record in T.iter_records(child_thread_id, parent_thread_id or None):
                    if T.is_checkpoint(record):
                        continue
                    resume_history.append({
                        "role": str(record.get("role") or "user"),
                        "content": str(record.get("content") or ""),
                        "ts": str(record.get("ts") or ""),
                        "file_name": str(record.get("file_name") or ""),
                        "file_chars": int(record.get("file_chars") or 0),
                    })
            except Exception as exc:  # pragma: no cover - best effort
                log.warning("child history restore failed for %s: %s", run_id, exc)
                resume_history = []

    # Bind the derived child thread (workspace of the parent, own journal).
    try:
        from dev_agent import workspace_binding as wb
        binder = getattr(wb, "bind_child_thread", None)
        if callable(binder):
            binder(child_thread_id, parent_thread_id=parent_thread_id,
                   workspace=base_dir, target_file=None)
    except Exception as exc:  # pragma: no cover - binding is best effort
        log.warning("child thread binding failed: %s", exc)

    _scope = None
    try:
        from dev_agent.agent_loop import AgentLoopState, step_agent_loop
        from dev_agent.universal_agent import UniversalDevAgent
        from core.api_layer import send_request

        # Pin the journal thread for THIS Python thread for the whole child
        # run: every task_state call inside resolves to the child's file even
        # though the process-wide active thread id belongs to a dialog. The
        # scope is exited in the finally block below, next to the unbind.
        try:
            from dev_agent import task_state as _task_state
            _scope = _task_state.thread_scope(child_thread_id)
            _scope.__enter__()
        except Exception as exc:  # pragma: no cover - journal is best effort
            _scope = None
            log.warning("child journal scope failed: %s", exc)

        # A child is the ASKING side of the dialog: its prompt carries the
        # rules for ask_parent, not for answering somebody else's children.
        strong_assistant, weak_assistant = build_assistant_dicts(
            slug, dialog_role="child")
        dispatcher = UniversalDevAgent(workspace=base_dir, thread_id=child_thread_id)
        dispatcher.attach_orchestrator(slug)
        try:
            dispatcher.child_disabled_tools = set(CHILD_DISABLED_TOOLS)
            # A child that already sits at the depth ceiling cannot delegate
            # further: the service would refuse the call anyway. Hiding the
            # tools saves a wasted step and keeps ``sleep`` from waiting for
            # children that can never appear.
            max_depth = int(limits.get("max_depth") or 0)
            if max_depth and depth >= max_depth:
                dispatcher.child_disabled_tools |= {"spawn_agent", "sleep"}
        except Exception:
            pass

        def _send(user_message: str, system: str = "",
                  history: Optional[List[Dict[str, Any]]] = None) -> str:
            card = dict(strong_assistant)
            if system:
                card["text"] = system
            return send_request(user_message=user_message, assistant=card,
                                file_context="", history=history or [], lang=lang)

        dispatcher.core.set_send_request(_send)

        state = AgentLoopState(
            task=task_text,
            max_steps=int(limits.get("subagent_max_steps") or DEFAULT_SUBAGENT_MAX_STEPS),
            auto_apply=True,
            strong_assistant=strong_assistant,
            weak_assistant=weak_assistant,
            thread_id=child_thread_id,
            run_id=run_id,
            parent_run_id=parent_run_id or "",
            depth=depth,
            # A delegated child has no interactive user: the loop must not stop
            # on a confirmation request or on a question nobody will answer.
            child_mode=True,
            # Prompt-injection protection follows the run that started this
            # child: a dialog with the safety gate off gets a child without it
            # too. The value was frozen on the run record at spawn time, so a
            # background child never reads another dialog's live setting.
            enable_injection_protection=bool(enable_injection_protection),
            # The child drains the mailbox of the whole tree, not of its own
            # run: a worker posts a notice to the root that owns the pool.
            root_run_id=_tree_root_of(service, run_id),
            # Dialog limits are frozen at spawn time for the same reason the
            # run budget is: the answering process may belong to another
            # dialog, and its live config must not decide this run's rounds.
            dialog_limits=dict(limits),
        )

        if prior_run:
            state.history = resume_history
            state.next_index = len(resume_history)
            state.phase = "calling_llm"
            state.user_message = _ANSWER_PREFIX + str(answer_text or "")
            if prior_state is not None:
                # Continue the SAME state: the step counter, the token totals
                # and the plan all carry over, so an answer costs the child no
                # progress at all.
                state.steps = int(getattr(prior_state, "steps", 0) or 0)
                state.total_tokens_in = int(getattr(prior_state, "total_tokens_in", 0) or 0)
                state.total_tokens_out = int(getattr(prior_state, "total_tokens_out", 0) or 0)
                state.total_tokens_cache = int(getattr(prior_state, "total_tokens_cache", 0) or 0)
                state.auto_continue_count = int(getattr(prior_state, "auto_continue_count", 0) or 0)

        # Persist this child as it works. The index row makes the run
        # addressable; the transcript file is the copy of the conversation
        # that survives the process. Without the row a parent asking "what did
        # this child actually do" gets "not found", and without the file a
        # crash loses the whole run.
        try:
            from core.orchestration.transcript_sink import TranscriptSink
            if isinstance(orch, dict):
                _display = str(orch.get("name") or "")
            else:
                _display = str(getattr(orch, "name", "") or "")
            sink = TranscriptSink(
                thread_id=child_thread_id,
                parent_thread_id=parent_thread_id,
                slug=slug,
                display_name=_display or slug,
                workspace=base_dir,
            )
            sink.open(task_text)
        except Exception as exc:  # pragma: no cover - persistence is best effort
            log.warning("child transcript sink init failed: %s", exc)
            sink = None

        _heartbeat_at = [time.monotonic()]

        def _child_event(event: dict) -> None:
            """Forward a child event to the run's subscribers (parents included).

            The forwarder stamps the child identity explicitly: events built
            by a nested helper may lack it, and a subscriber that cannot tell
            the child run from its parent would draw a flat feed instead of a
            tree.
            """
            if not isinstance(event, dict):
                return
            # Refresh the durability heartbeat at most once a minute. heal()
            # treats a silent record as a dead process, and with an hour-long
            # run budget that verdict must not fall on a healthy child.
            try:
                now = time.monotonic()
                if now - _heartbeat_at[0] >= 60.0:
                    _heartbeat_at[0] = now
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

        budget = int(limits.get("subagent_budget_tokens") or 0)
        steps = 0
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
                                    on_event=_child_event)
            steps += 1
            # One drain per step: the transcript trails the child by at most
            # the step in flight, and a crash costs that step alone. The write
            # itself is a single append in the page cache - it does not wait
            # for the disk, so it cannot slow the child down.
            if sink is not None:
                sink.drain(state)
                # A step boundary is the only place a checkpoint is honest:
                # every message of the step is on disk and every file the step
                # wrote already exists. A crash after this point loses at most
                # the next step, and a resume knows exactly where to continue.
                sink.checkpoint(state)

        status, stop_reason, error_code = map_final_status(
            state.final_status, getattr(state, "error_code", None))
        if state.phase == "error" and not error_code:
            error_code = ERR_PROVIDER_ERROR
        # A run that stopped on a question is NOT an unfinished one: it waits
        # for an answer it can actually receive. The reason travels with the
        # result so the parent tells "answer me" apart from "read my journal".
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
    except Exception as exc:  # pragma: no cover - defensive
        # A failure HERE is a defect of the child loop (a TypeError in the
        # platform), not a provider hiccup. Reporting it as provider_error
        # invited a retry that could never succeed: the same call fails the
        # same way, and the plausible-looking code hid the real cause.
        log.warning("child agent loop failed: %s", exc)
        steps_done = 0
        try:
            steps_done = int(getattr(state, "steps", 0) or 0)
        except Exception:
            steps_done = 0
        return AgentRunResult(
            run_id=run_id, status=STATUS_FAILED,
            stop_reason=STOP_ERROR,
            summary=type(exc).__name__ + ": " + str(exc),
            error_code=ERR_INTERNAL_ERROR, steps_used=steps_done,
            retryable=False)
    finally:
        # Final drain runs on EVERY exit path - a normal finish, a cancelled
        # or timed-out run, and the exception below. The step loop already
        # drained what it could; this catches the last step and the counters,
        # so a reader sees an honest total even for a run that died.
        if sink is not None:
            try:
                sink.finish(state)
            except Exception as exc:  # pragma: no cover - never mask a result
                log.warning("child transcript finish failed: %s", exc)
        try:
            if _scope is not None:
                _scope.__exit__(None, None, None)
        except Exception:  # pragma: no cover - scope exit must never raise
            pass
        try:
            from dev_agent import workspace_binding as wb
            unbinder = getattr(wb, "unbind_thread", None)
            if callable(unbinder):
                unbinder(child_thread_id)
        except Exception:
            pass


# ─── Process-wide singleton ──────────────────────────────────────────────────

_SERVICE: Optional[OrchestrationService] = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> OrchestrationService:
    """Return the process-wide orchestration service (created on first use).

    Creation is the one moment the store is known to be read by a fresh
    process, so it is also where records of a previous instance are repaired:
    no live counterpart can exist for them yet.
    """
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = OrchestrationService()
                _SERVICE.sweep_dead_runs()
    return _SERVICE
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
