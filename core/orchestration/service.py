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
    AgentRunResult,
    Artifact,
    CancelReport,
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
        )

    def to_record(self) -> Dict[str, Any]:
        """Return the durable-store form of this record.

        ``summary`` is truncated here on purpose: the store serves status and
        resume, and a runaway child summary would otherwise grow the file that
        is rewritten on every heartbeat.
        """
        return {
            "run_id": self.run_id,
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
            "retryable": bool(self.retryable),
        }


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
            rec.heartbeat_at = time.time()
        self.save_record(rid)
        _record_child_handoff(rec, effective)
        return effective

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
        with self._lock:
            alive = [r for r in self._runs.values()
                     if r.root_run_id == rid and r.run_id != rid
                     and r.status == STATUS_RUNNING]
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

    def record_from_store(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return a durable record even when the in-memory one is gone.

        This is what makes a background run observable after ``release_tree``
        or a restart: the registry is a cache, the store is the truth.
        """
        try:
            return run_store.load(run_id, self.store_root())
        except Exception as exc:  # pragma: no cover - unreadable store
            log.warning("run record load failed for %s: %s", run_id, exc)
            return None

    def list_records(self, root_run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Merge live records with durable ones, newest first, one per run.

        Live state wins over the stored copy (the store lags a heartbeat); a
        run known only to the store - a background child from a previous
        process - is still reported instead of silently disappearing.
        """
        merged: Dict[str, Dict[str, Any]] = {}
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
            merged[rec.run_id] = rec.to_record()
        items = list(merged.values())
        items.sort(key=lambda d: float(d.get("created_at") or 0.0), reverse=True)
        return items

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
        if rec.status not in (STATUS_RUNNING,):
            return CancelReport(run_id=rid, cancelled=False,
                                previous_status=rec.status,
                                reason="already_terminal")
        cancelled_ids: List[str] = []
        with self._lock:
            rec.cancel_requested = True
            rec.status = STATUS_CANCELLED
            rec.stop_reason = STOP_ABORTED
            rec.error_code = ERR_CANCELLED_BY_PARENT
            cancelled_ids.append(rec.run_id)
            if cascade:
                stack = list(rec.children)
                while stack:
                    child = self._runs.get(stack.pop())
                    if child is None or child.status != STATUS_RUNNING:
                        continue
                    child.cancel_requested = True
                    child.status = STATUS_CANCELLED
                    child.stop_reason = STOP_ABORTED
                    child.error_code = ERR_CANCELLED_BY_PARENT
                    cancelled_ids.append(child.run_id)
                    stack.extend(child.children)
        return CancelReport(run_id=rid, cancelled=True,
                            previous_status=STATUS_RUNNING,
                            reason="requested",
                            children=cancelled_ids[1:])

    def is_cancelled(self, run_id: str) -> bool:
        """True when cancellation was requested for the run."""
        rec = self.get_record(run_id)
        return bool(rec and rec.cancel_requested)

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
                   enable_injection_protection: bool = True) -> AgentRunResult:
        """Execute one child run through the configured runner.

        ``parent_thread_id`` and ``enable_injection_protection`` reach a
        runner that declares them OR accepts ``**kwargs``; a runner with a
        FIXED old signature does not receive them. The runner is a test seam,
        and an existing fake must keep working instead of failing on an
        unexpected keyword.
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
        if run_store.claim_notice(run_id, status, self.store_root()):
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
                          enable_injection_protection: bool = True) -> AgentRunResult:
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

        strong_assistant, weak_assistant = build_assistant_dicts(slug)
        dispatcher = UniversalDevAgent(workspace=base_dir, thread_id=child_thread_id)
        dispatcher.attach_orchestrator(slug)
        try:
            dispatcher.child_disabled_tools = set(CHILD_DISABLED_TOOLS)
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
        )

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

        status, stop_reason, error_code = map_final_status(
            state.final_status, getattr(state, "error_code", None))
        if state.phase == "error" and not error_code:
            error_code = ERR_PROVIDER_ERROR
        return AgentRunResult(
            run_id=run_id, status=status, stop_reason=stop_reason,
            summary=state.final_text or state.error_message or "",
            error_code=error_code, steps_used=state.steps,
            tokens_used={"in": state.total_tokens_in,
                         "out": state.total_tokens_out,
                         "cache": state.total_tokens_cache})
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
    """Return the process-wide orchestration service (created on first use)."""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = OrchestrationService()
    return _SERVICE
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
