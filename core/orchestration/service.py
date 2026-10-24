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
    DEFAULT_SUBAGENT_BUDGET_TOKENS,
    DEFAULT_SUBAGENT_MAX_STEPS,
    DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_SUBAGENT_RUNS,
    ERR_CANCELLED_BY_PARENT,
    ERR_LIMIT_EXCEEDED,
    ERR_MAX_STEPS,
    ERR_PROVIDER_ERROR,
    ERR_TIMEOUT,
    InvokeRequest,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STOP_ABORTED,
    STOP_COMPLETED,
    STOP_ERROR,
    STOP_MAX_TOKENS,
    SpawnRequest,
    map_final_status,
)

#: Tools a child must NOT see in v1: a child never creates platform entities
#: (orchestrators, assistants). The parent keeps whatever it had.
CHILD_DISABLED_TOOLS = (
    "create_orchestrator",
    "update_orchestrator",
    "delete_orchestrator",
    "create_assistant_for_task",
    "update_assistant_by_id",
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
    # A non-positive timeout would abort every run instantly.
    if limits["subagent_timeout_seconds"] < 1:
        limits["subagent_timeout_seconds"] = DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    return limits


@dataclass
class RunRecord:
    """In-memory state of one run (the dialog itself or a spawned child)."""

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
        )


class _TreeState:
    """Per-tree bookkeeping: one pool, one run counter, all subscribers."""

    __slots__ = ("lock", "pool", "pool_size", "total_used", "total_limit",
                 "subscribers")

    def __init__(self, pool_size: int, total_limit: int):
        self.lock = threading.RLock()
        self.pool_size = max(1, int(pool_size))
        self.pool = threading.BoundedSemaphore(self.pool_size)
        # The root run counts itself, so the tree starts at 1.
        self.total_used = 1
        self.total_limit = max(1, int(total_limit))
        # run_id -> list of callbacks (ancestors included at delivery time).
        self.subscribers: Dict[str, List[Callable[[dict], None]]] = {}


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

    def finish_run(self, run_id: str, result: AgentRunResult) -> None:
        """Store the terminal state of a run and drop its tree when empty."""
        rid = (run_id or "").strip()
        with self._lock:
            rec = self._runs.get(rid)
            if rec is None:
                return
            rec.status = result.status
            rec.stop_reason = result.stop_reason
            rec.summary = result.summary
            rec.error_code = result.error_code
            rec.artifacts = list(result.artifacts)
            rec.steps_used = result.steps_used
            rec.tokens_used = dict(result.tokens_used)

    def release_tree(self, root_run_id: str) -> None:
        """Forget a finished tree (records, pool, subscribers).

        Called when the dialog finishes its loop: the tree exists only while
        the dialog runs, and keeping it would leak one semaphore per dialog.
        """
        rid = (root_run_id or "").strip()
        if not rid:
            return
        with self._lock:
            doomed = [r for r in self._runs.values() if r.root_run_id == rid]
            for rec in doomed:
                self._runs.pop(rec.run_id, None)
            self._trees.pop(rid, None)

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

    def acquire_pool(self, root_id: str, timeout: float = 0.0) -> bool:
        """Take one concurrency slot of the tree pool (non-blocking by default)."""
        with self._lock:
            tree = self._trees.get(root_id)
        if tree is None:
            return True
        return bool(tree.pool.acquire(timeout=timeout)) if timeout > 0 else bool(tree.pool.acquire(blocking=False))

    def release_pool(self, root_id: str) -> None:
        """Return one concurrency slot to the tree pool."""
        with self._lock:
            tree = self._trees.get(root_id)
        if tree is None:
            return
        try:
            tree.pool.release()
        except ValueError:  # pragma: no cover - over-release guard
            pass

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
                   parent_thread_id: str = "") -> AgentRunResult:
        """Execute one child run through the configured runner.

        ``parent_thread_id`` is passed only to a runner that declares it: the
        runner is a test seam, and an existing fake with the old signature
        must keep working instead of failing on an unexpected keyword.
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
            if "parent_thread_id" in _inspect.signature(runner).parameters:
                kwargs["parent_thread_id"] = parent_thread_id
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
        parent = self.get_record(req.parent_run_id or "") if req.parent_run_id else None
        if parent is None and req.thread_id:
            # The dialog registered no explicit run for this thread: treat the
            # thread itself as the root so limits still apply.
            parent = self.ensure_run(req.thread_id, thread_id=req.thread_id,
                                     config=parent_config)
        root_id = parent.root_run_id if parent else req.thread_id or ""
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
        parent_thread = (parent.thread_id if parent and parent.thread_id
                         else req.thread_id or "")
        child_thread_id = derive_child_thread_id(parent_thread, run_id)

        if not self.acquire_pool(root_id,
                                  timeout=float(limits["subagent_timeout_seconds"])):
            self._release_run_slot(root_id)
            return self._limit_error("max_active_subagents",
                                     limits["max_active_subagents"],
                                     active=self.pool_active(root_id))

        rec = self.ensure_run(run_id, thread_id=child_thread_id,
                              agent_slug=req.agent_slug or "",
                              parent_run_id=parent.run_id if parent else None,
                              depth=depth)
        self.publish({"type": "run_started", "run_id": run_id,
                      "parent_run_id": req.parent_run_id, "thread_id": child_thread_id,
                      "agent_slug": req.agent_slug, "depth": depth, "payload": {}})

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
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("child run %s failed: %s", run_id, exc)
            result = AgentRunResult(run_id=run_id, status=STATUS_FAILED,
                                    stop_reason=STOP_ERROR,
                                    summary=str(exc),
                                    error_code=ERR_PROVIDER_ERROR)
        finally:
            self.release_pool(root_id)

        if self.is_cancelled(run_id) and result.status == STATUS_RUNNING:
            result = AgentRunResult(run_id=run_id, status=STATUS_CANCELLED,
                                    stop_reason=STOP_ABORTED,
                                    summary=result.summary,
                                    artifacts=result.artifacts,
                                    error_code=ERR_CANCELLED_BY_PARENT,
                                    steps_used=result.steps_used,
                                    tokens_used=result.tokens_used)
        self.finish_run(run_id, result)
        self.publish({"type": "run_finished", "run_id": run_id,
                      "parent_run_id": req.parent_run_id,
                      "thread_id": child_thread_id,
                      "agent_slug": req.agent_slug, "depth": depth,
                      "payload": result.to_dict()})
        return {"ok": True, "result": result.to_dict()}

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
        if not self.acquire_pool(root_id,
                                  timeout=float(limits["subagent_timeout_seconds"])):
            return self._limit_error("max_active_subagents",
                                     limits["max_active_subagents"],
                                     active=self.pool_active(root_id))
        try:
            if invoke_fn is None:
                from core.orchestration.invoke import invoke_once as invoke_fn
            return invoke_fn(req, parent_slug=parent_slug, base_dir=base_dir, lang=lang)
        finally:
            self.release_pool(root_id)

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


def _default_child_runner(*, child_slug: str, brief: AgentBrief,
                          child_thread_id: str, run_id: str,
                          parent_run_id: Optional[str], depth: int,
                          limits: Dict[str, int], deadline: float,
                          base_dir: Optional[str], lang: Optional[str],
                          service: "OrchestrationService",
                          parent_thread_id: str = "") -> AgentRunResult:
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
        )

        def _child_event(event: dict) -> None:
            """Forward a child event to the run's subscribers (parents included).

            The forwarder stamps the child identity explicitly: events built
            by a nested helper may lack it, and a subscriber that cannot tell
            the child run from its parent would draw a flat feed instead of a
            tree.
            """
            if not isinstance(event, dict):
                return
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
        log.warning("child agent loop failed: %s", exc)
        return AgentRunResult(run_id=run_id, status=STATUS_FAILED,
                              stop_reason=STOP_ERROR, summary=str(exc),
                              error_code=ERR_PROVIDER_ERROR)
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
