# -*- coding: utf-8 -*-
"""
core.orchestration.contracts - transport-neutral types of the agent run contract.

Everything here is a frozen dataclass or a Protocol: no Streamlit, no
SQLite, no provider calls. The UI, the HTTP layer and the in-process
delegation runtime all speak these types, so the launch contract cannot
drift between transports.

Design notes carried from the specification:

- ``stop_reason`` has exactly four values (``completed``, ``aborted``,
  ``error``, ``max-tokens``). A child refusing a task is NOT a fifth
  value: it is ``status='failed'``, ``stop_reason='error'``,
  ``error_code='refusal'`` - there is no mechanism that could recognise a
  refusal marker in the model output, and a value nothing can fill in
  would be filled in by guesswork.
- ``aborted`` and ``error`` never mix: the first is a known termination
  with a partial result, the second is a failure without one.
- ``AgentRunResult`` is a dataclass, but it reaches the model as a compact
  dict (``to_dict``): the full transcript stays in the child journal and in
  the event stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol

# ─── Stop reasons (exactly four) ─────────────────────────────────────────────

STOP_COMPLETED = "completed"
STOP_ABORTED = "aborted"
STOP_ERROR = "error"
STOP_MAX_TOKENS = "max-tokens"

STOP_REASONS = (STOP_COMPLETED, STOP_ABORTED, STOP_ERROR, STOP_MAX_TOKENS)

# ─── Run statuses ────────────────────────────────────────────────────────────

STATUS_RUNNING = "running"
STATUS_AWAITING_INPUT = "awaiting_input"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

# ─── Machine-readable error codes ────────────────────────────────────────────

ERR_AGENT_NOT_ALLOWED = "agent_not_allowed"
ERR_ASSISTANT_NOT_ALLOWED = "assistant_not_allowed"
ERR_UNKNOWN_AGENT = "unknown_agent"
ERR_UNKNOWN_ASSISTANT = "unknown_assistant"
ERR_INVALID_BRIEF = "invalid_brief"
ERR_INVALID_ARGS = "invalid_args"
ERR_CANCELLED_BY_PARENT = "cancelled_by_parent"
ERR_CANCELLED_BY_USER = "cancelled_by_user"
ERR_TIMEOUT = "timeout"
ERR_MAX_STEPS = "max_steps"
#: A child exhausted its autonomous continuations without ever signalling
#: completion. Kept apart from ``max_steps`` because the two demand different
#: reactions: a step budget needs a smaller task, while this one means the
#: child never learned its work was finished - its files are usually on disk.
ERR_CHILD_UNFINISHED = "child_unfinished"
ERR_SANITIZED_REQUIRED = "sanitized_required"
ERR_PROVIDER_ERROR = "provider_error"
#: The child loop itself raised - a defect in the platform, not a provider
#: hiccup. Kept apart from ``provider_error`` because the two demand
#: different reactions: a provider failure may be worth one retry, an
#: internal defect never is - the same call fails the same way, so a retry
#: only burns time and hides the bug behind a plausible code.
ERR_INTERNAL_ERROR = "internal_error"
ERR_REFUSAL = "refusal"
ERR_CONTEXT_TOO_LARGE = "context_too_large"
ERR_NOT_IMPLEMENTED = "not_implemented"
ERR_LIMIT_EXCEEDED = "limit_exceeded"

# ─── Background runs and resume (v2) ────────────────────────────────────────

#: A run id is unknown to the in-memory registry AND to the durable store.
ERR_RUN_NOT_FOUND = "run_not_found"
#: Neither a durable record nor a child journal exists, so resume cannot say
#: what was done and what remains.
ERR_RESUME_UNAVAILABLE = "resume_unavailable"
#: The run belongs to another root tree: a caller may only resume its own
#: descendants. Also used when the caller has no identity at all (fail closed).
ERR_NOT_YOUR_RUN = "not_your_run"
#: A child asked for an operation that needs a human confirmation. A child has
#: no interactive user, so the call is refused and the child rewrites it.
ERR_CONFIRMATION_REQUIRED = "confirmation_required"
#: A spawn arrived with an empty root identity: limits cannot be enforced, so
#: the call is refused instead of silently running without them.
ERR_NO_RUN_IDENTITY = "no_run_identity"
#: A durable record still claims ``running`` but its heartbeat is stale or its
#: process is gone: the run is healed to ``failed`` with this code.
ERR_STALE_RUN = "stale_run"
#: The resume chain reached its hard ceiling: resuming a resumed run would
#: otherwise walk around ``total_subagent_runs`` one link at a time.
ERR_RESUME_CHAIN_LIMIT = "resume_chain_limit"
ERR_BACKGROUND_UNSUPPORTED = "background_unsupported"

#: Hard ceiling of a resume chain. ``resume_subagent`` starts a NEW run, so
#: without a ceiling a caller could walk around ``total_subagent_runs`` one
#: link at a time by resuming its own resumes. Two links cover the real case
#: (a run failed half-way, was resumed, and that resume failed too) and stop
#: the loophole before it becomes a loop.
MAX_RESUME_CHAIN = 2

# ─── Defaults of the delegation limits ───────────────────────────────────────

#: Depth of the delegation tree below a dialog. One level - a dialog fans
#: out to workers - covers normal work; a child that delegates further turns
#: one runaway loop into a whole tree of them, so nesting stays opt-in.
DEFAULT_MAX_DEPTH = 1

#: Children that may run at once. Research-style tasks fan out 4-7 agents in
#: one batch, so a pool of three refuses the fourth child of a legitimate
#: batch; five admits that width and still bounds the provider load.
DEFAULT_MAX_ACTIVE_SUBAGENTS = 5

#: Steps one child may take. The cap must sit well above what a real task
#: uses (about 90 steps) so that the TIME budget ends a long run, not the
#: counter: a counter that trips first stops healthy work and reports a
#: misleading "split the task" hint.
DEFAULT_SUBAGENT_MAX_STEPS = 300

DEFAULT_SUBAGENT_BUDGET_TOKENS = 0          # 0 = unlimited
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 3600

#: Upper bound of one child run, in seconds. It mirrors the ``sleep`` ceiling:
#: a parent can wait an hour for a child, so a child must be allowed to work
#: that long. A larger value written by hand is clamped by ``read_limits`` -
#: an unbounded child would outlive the dialog that spawned it.
MAX_SUBAGENT_TIMEOUT_SECONDS = 3600

#: How long a BLOCKING spawn may wait for a free concurrency slot. It is a
#: separate value on purpose: the run timeout says how long a child may WORK,
#: while this one says how long a caller may QUEUE. Tying the queue wait to an
#: hour-long run budget would let a blocking call look hung for an hour while
#: the pool is simply busy.
DEFAULT_POOL_WAIT_SECONDS = 600

#: Total runs one dialog may start, root included. It guards against runaway
#: fan-out, not against normal work: the busiest observed dialog used 31 of
#: the old 32, so a doubling keeps the guard useful without refusing the
#: next batch of a long session.
DEFAULT_TOTAL_SUBAGENT_RUNS = 64


@dataclass(frozen=True)
class Artifact:
    """One artifact produced by a child run.

    ``kind`` is ``file`` / ``dir`` / ``url``. ``digest`` is ``mtime+size``
    for files and ``sha256`` for computed artifacts; it exists so the parent
    can tell two versions of the same path apart without reading the file.
    """

    path: str
    kind: str = "file"
    digest: str = ""

    def to_dict(self) -> Dict[str, str]:
        """Return the compact dict form used in tool results."""
        return {"path": self.path, "kind": self.kind, "digest": self.digest}


@dataclass(frozen=True)
class AgentRunResult:
    """Structured result of one delegated run.

    ``summary`` is the final text or the failure reason - never the
    transcript. ``steps_used`` and ``tokens_used`` are informational: the
    parent uses them to decide whether to split a task further.
    """

    run_id: str
    status: str = STATUS_RUNNING
    stop_reason: Optional[str] = None
    summary: str = ""
    artifacts: List[Artifact] = field(default_factory=list)
    error_code: Optional[str] = None
    steps_used: int = 0
    tokens_used: Dict[str, int] = field(default_factory=dict)

    # ── Background and resume extras ───────────────────────────────────────
    # ``child_thread_id`` lets the parent address the child journal and the
    # derived thread without parsing the run id. ``pending`` is the short list
    # of unfinished plan items (the primary resume signal) and ``resume_hint``
    # the single-line continuation text. All three default to empty, so a plain
    # synchronous result keeps its old shape and only grows new keys.
    child_thread_id: str = ""
    pending: List[str] = field(default_factory=list)
    resume_hint: str = ""

    # ── Failure classification ─────────────────────────────────────────────
    # ``retryable`` answers the one question a caller asks about a failed run:
    # would the same call plausibly succeed a second time? A provider hiccup is
    # retryable; a defect inside the loop is not - it fails identically on
    # every attempt, so retrying only burns time and hides the cause. Defaults
    # to True so a plain informational result keeps its old meaning.
    retryable: bool = True

    def to_dict(self) -> Dict[str, object]:
        """Return the compact dict form sent back to the parent model.

        The full transcript is deliberately absent: it stays in the child
        journal and in the event stream. Result size is additionally capped
        by the existing tool-result policy (spill/preview), so no separate
        limit on ``summary`` is introduced here.
        """
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "summary": self.summary,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "error_code": self.error_code,
            "steps_used": self.steps_used,
            "tokens_used": dict(self.tokens_used),
            "child_thread_id": self.child_thread_id,
            "pending": list(self.pending),
            "resume_hint": self.resume_hint,
            "retryable": bool(self.retryable),
        }

    @property
    def is_terminal(self) -> bool:
        """True when the run reached one of the terminal statuses."""
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class AgentBrief:
    """What the parent tells the child before it starts.

    ``brief`` must carry everything the child needs to know - paths, known
    errors, constraints - because the child gets a clean history: there is
    no follow-up into a live child in v1. ``artifacts_in`` maps a name to a
    path so the child can refer to inputs without guessing.
    """

    goal: str
    brief: str = ""
    artifacts_in: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SpawnRequest:
    """Request to launch a child orchestrator.

    ``thread_id`` is the PARENT thread: the child gets a derived thread id
    and its own journal, so delegated work never writes into the parent's
    Active Task.

    ``background`` starts the child on its own daemon thread and returns a
    ``running`` result immediately; the caller learns the outcome from
    ``poll``/``list_runs`` or from the notification the worker drops into the
    parent thread. Blocking stays the default so an existing call keeps its
    meaning.

    ``workspace`` is the ABSOLUTE project folder captured when the spawn is
    issued. It is frozen into the run record instead of being re-read from
    the process-wide config later: the live config belongs to whichever
    dialog happens to run, so a background worker that re-read it could work
    in another project's folder.

    ``resume_run_id`` marks the request as a continuation of an earlier run;
    the service then inherits that run's tree (root, depth) instead of
    starting a fresh one - otherwise every resume would reset the limits.

    ``inline_orchestrator`` is reserved for phase 2 and answers
    ``not_implemented``.

    ``enable_injection_protection`` carries the parent's prompt-injection
    protection setting into the child: a child inherits the mode of the run
    that started it instead of falling back to the loop default. ``None``
    means "the service decides" - an explicit argument wins, a resume takes
    the value frozen in the source run, and a fresh spawn without an
    argument uses the safe default (protection ON). A background child
    cannot read the parent's live setting at start-up: the process-wide
    config belongs to whichever dialog happens to run, so the value is
    resolved at spawn time and frozen into the run record.
    """

    agent_slug: Optional[str]
    request: AgentBrief
    thread_id: str = ""
    parent_run_id: Optional[str] = None
    background: bool = False
    inline_orchestrator: Optional[dict] = None
    workspace: str = ""
    resume_run_id: Optional[str] = None
    enable_injection_protection: Optional[bool] = None


@dataclass(frozen=True)
class InvokeRequest:
    """Request for one one-shot assistant call.

    Two forms share one implementation: an existing assistant by
    ``assistant_id`` (prompt/model/temperature come from its card) and an
    ephemeral call (``system`` + ``context_files``, nothing written to the
    database). ``assistant_id`` and ``system`` are mutually exclusive.
    """

    assistant_id: Optional[str] = None
    user: str = ""
    context_files: List[str] = field(default_factory=list)
    system: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    parent_run_id: Optional[str] = None


@dataclass(frozen=True)
class CancelReport:
    """Outcome of one cancellation.

    ``reason`` is ``requested`` (cancellation accepted, the run stops at the
    next boundary), ``already_terminal`` (nothing to cancel) or ``timeout``.
    ``children`` lists the run ids of descendants that received the cascade.
    """

    run_id: str
    cancelled: bool = False
    previous_status: str = STATUS_RUNNING
    reason: str = "requested"
    children: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        """Return the dict form used in tool results and events."""
        return {
            "run_id": self.run_id,
            "cancelled": self.cancelled,
            "previous_status": self.previous_status,
            "reason": self.reason,
            "children": list(self.children),
        }


class RunService(Protocol):
    """Public contract of the orchestration runtime.

    ``run`` is blocking and returns the child result. ``start`` launches the
    same child on its own thread and returns a ``running`` result at once;
    ``poll``, ``wait`` and ``list_runs`` report what happened afterwards.
    ``cancel`` cascades over ``parent_run_id`` by default and is idempotent.
    ``subscribe`` returns an unsubscribe callable; events of descendants are
    delivered to every ancestor subscriber automatically.

    ``take_notifications`` drains the per-tree mailbox: the worker never
    writes into the (Streamlit-owned) thread itself, it drops a notice here
    and the loop or the UI persists it exactly once.
    """

    def run(self, req: SpawnRequest) -> AgentRunResult:  # pragma: no cover - protocol
        ...

    def start(self, req: SpawnRequest, **kwargs: object) -> Dict[str, object]:  # pragma: no cover - protocol
        ...

    def poll(self, run_id: str, **kwargs: object) -> Dict[str, object]:  # pragma: no cover - protocol
        ...

    def wait(self, run_ids: List[str], timeout: float = 0.0,
             **kwargs: object) -> List[Dict[str, object]]:  # pragma: no cover - protocol
        ...

    def list_runs(self, root_run_id: Optional[str] = None,
                  **kwargs: object) -> List[Dict[str, object]]:  # pragma: no cover - protocol
        ...

    def take_notifications(self, root_run_id: str) -> List[Dict[str, object]]:  # pragma: no cover - protocol
        ...

    def cancel(self, run_id: str, *, cascade: bool = True) -> CancelReport:  # pragma: no cover - protocol
        ...

    def subscribe(self, run_id: str,
                  on_event: Callable[[dict], None]) -> Callable[[], None]:  # pragma: no cover - protocol
        ...


# ─── final_status -> (status, stop_reason, error_code) mapping (§1.3) ────────

#: Mapping of the executor's ``AgentResult.status`` onto the contract triple.
#: ``applied`` and ``discarded`` are successful outcomes from the execution
#: standpoint: the loop finished and produced a meaningful result. Whether a
#: file edit was applied or rejected belongs in ``summary``, not in
#: ``stop_reason``.
FINAL_STATUS_MAP: Dict[str, tuple] = {
    "done": (STATUS_DONE, STOP_COMPLETED, None),
    "applied": (STATUS_DONE, STOP_COMPLETED, None),
    "discarded": (STATUS_DONE, STOP_COMPLETED, None),
    "error": (STATUS_FAILED, STOP_ERROR, None),          # code from the exception
    "awaiting_confirmation": (STATUS_AWAITING_INPUT, None, None),
    "awaiting_user": (STATUS_AWAITING_INPUT, None, None),
    "awaiting_approval": (STATUS_AWAITING_INPUT, None, None),
    "sanitized_required": (STATUS_FAILED, STOP_ERROR, ERR_SANITIZED_REQUIRED),
    "stopped_by_user": (STATUS_CANCELLED, STOP_ABORTED, ERR_CANCELLED_BY_USER),
    "stopped_max_steps": (STATUS_CANCELLED, STOP_ABORTED, ERR_MAX_STEPS),
}


def map_final_status(final_status: str,
                     error_code: Optional[str] = None) -> tuple:
    """Map an executor ``final_status`` onto ``(status, stop_reason, error_code)``.

    Unknown statuses fall back to a failure with the ``provider_error`` code
    rather than silently claiming success: an unmapped status means the
    executor produced something the contract does not understand.
    """
    key = (final_status or "").strip()
    if key not in FINAL_STATUS_MAP:
        return (STATUS_FAILED, STOP_ERROR, error_code or ERR_PROVIDER_ERROR)
    status, stop_reason, mapped_code = FINAL_STATUS_MAP[key]
    return (status, stop_reason, error_code or mapped_code)


def error_result(run_id: str, error: str, error_code: str,
                 **details: object) -> Dict[str, object]:
    """Build the single error shape every orchestration tool returns.

    ``error`` is the human-readable text for the model, ``error_code`` is the
    machine code; the two-shape variant (``error`` carrying a code) is not
    used anywhere in the orchestration layer. Extra keyword arguments are
    merged in as details (``allowed``, ``available``, ``self_only``, ...).
    """
    payload: Dict[str, object] = {
        "ok": False,
        "error": error,
        "error_code": error_code,
        "run_id": run_id,
    }
    payload.update(details)
    return payload
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
