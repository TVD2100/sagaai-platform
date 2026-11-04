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

# ─── Agent dialog (parent <-> child question/answer) ────────────────────────

#: The question was parked, but nobody answered it before the wait expired.
#: Kept apart from ``timeout``: the run was not working, it was WAITING, so the
#: fix is to answer earlier or raise the wait, not to narrow the task.
ERR_ANSWER_NOT_RECEIVED = "answer_not_received"
#: The run used its whole question budget. A child that keeps asking instead
#: of working is the failure mode this limit exists for; the parent must
#: answer with a directive to finish, not with another long explanation.
ERR_QUESTION_LIMIT = "question_limit"
#: The run is not waiting for an answer, so there is nothing to unpack.
ERR_NOTHING_TO_ANSWER = "nothing_to_answer"
#: The run already reached a terminal state and cannot be continued in place.
ERR_RUN_ALREADY_FINISHED = "run_already_finished"
#: Too many runs of one tree are parked at once: a waiting run is cheap, but an
#: unbounded set of them would hold the tree open forever.
ERR_TOO_MANY_PARKED = "too_many_parked"

#: Reasons a run may sit in ``awaiting_input``. ``unfinished`` is the older
#: meaning (the child exhausted its autonomous continuations without ever
#: signalling completion) and stays the reading of a record that carries no
#: reason at all, so records written before this change keep their meaning.
AWAIT_QUESTION = "question"
AWAIT_UNFINISHED = "unfinished"

# ─── Result verification (o-4) ──────────────────────────────────────────────

#: No verifier is appointed for the orchestrator. Kept apart from
#: ``unknown_agent`` because the fix is a SETTING, not a different name: an
#: agent is never substituted silently - that would hide the fact that nobody
#: is appointed to check anything.
ERR_VERIFIER_NOT_CONFIGURED = "verifier_not_configured"
#: The run being verified is not terminal yet, so its result does not exist.
#: A verification of work in progress would judge a moving target.
ERR_RUN_NOT_FINISHED = "run_not_finished"

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

# ─── Defaults of the agent dialog ───────────────────────────────────────────

#: Question/answer pairs one run may exchange. Five covers a genuinely unclear
#: task (goal, format, source of truth, edge case, confirmation) and still ends
#: a child that asks instead of working.
DEFAULT_DIALOG_ROUNDS = 5

#: Longest question a child may send. A question is a question, not a report:
#: the report belongs in the run summary, and an unbounded question would let
#: one run flood the parent's context with a whole transcript.
DEFAULT_QUESTION_MAX_CHARS = 1500

#: Longest answer a parent may send. An answer that needs more room is a new
#: briefing, and a new briefing belongs in ``resume_subagent``.
DEFAULT_ANSWER_MAX_CHARS = 4000

#: How long a parked run waits for an answer. A waiting run costs no provider
#: time, so the window is short enough to be noticed: a question nobody answers
#: is a stalled task the user should see, not a state that lingers for hours.
DEFAULT_ANSWER_WAIT_SECONDS = 900

#: Ceiling of the wait, mirroring the child run budget: a parked run must not
#: outlive the dialog that can answer it.
MAX_ANSWER_WAIT_SECONDS = 3600

#: Parked runs one tree may hold at once. They no longer occupy a concurrency
#: slot (a waiting run is not working), so this is the separate guard that keeps
#: a fan-out from turning into a warehouse of half-finished children.
DEFAULT_MAX_PARKED_RUNS = 8


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
class AgentQuestion:
    """One question a child parked its run to ask its parent.

    A question is structured rather than free prose in the journal: the parent
    must be able to tell WHAT is blocked, WHAT is already done and WHICH answer
    is expected without reading the child's whole conversation. ``text`` is the
    question itself; ``done``/``blocked`` are the short report that keeps a
    question from becoming a status update.
    """

    text: str = ""
    options: List[str] = field(default_factory=list)
    done: str = ""
    blocked: str = ""
    asked_at: float = 0.0
    deadline_at: float = 0.0
    answer: str = ""
    answered_at: float = 0.0
    answered_by: str = ""

    @property
    def answered(self) -> bool:
        """True once an answer was recorded for this question."""
        return bool((self.answer or "").strip())

    def to_dict(self) -> Dict[str, object]:
        """Return the compact dict form used in tool results and records."""
        return {
            "text": self.text,
            "options": list(self.options),
            "done": self.done,
            "blocked": self.blocked,
            "asked_at": self.asked_at,
            "deadline_at": self.deadline_at,
            "answer": self.answer,
            "answered_at": self.answered_at,
            "answered_by": self.answered_by,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Optional["AgentQuestion"]:
        """Rebuild a question from a stored record, or None when absent."""
        if isinstance(raw, AgentQuestion):
            return raw
        if not isinstance(raw, dict):
            return None
        options = raw.get("options")
        return cls(
            text=str(raw.get("text") or ""),
            options=[str(x) for x in options] if isinstance(options, (list, tuple)) else [],
            done=str(raw.get("done") or ""),
            blocked=str(raw.get("blocked") or ""),
            asked_at=_float_of(raw.get("asked_at")),
            deadline_at=_float_of(raw.get("deadline_at")),
            answer=str(raw.get("answer") or ""),
            answered_at=_float_of(raw.get("answered_at")),
            answered_by=str(raw.get("answered_by") or ""),
        )


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

    # ── Agent dialog ───────────────────────────────────────────────────────
    # ``await_reason`` tells the two meanings of ``awaiting_input`` apart: a
    # run parked on a QUESTION (answer it, then it continues in place) and a
    # run that exhausted its autonomous continuations (read, then resume).
    # ``question`` carries the parked question itself so the parent does not
    # have to read a journal to learn what is blocked. Both are empty for a
    # run that never asked anything, so the old shape keeps working.
    await_reason: str = ""
    question: Optional[AgentQuestion] = None
    dialog_rounds: int = 0

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
            "await_reason": self.await_reason,
            "question": self.question.to_dict() if self.question else None,
            "dialog_rounds": int(self.dialog_rounds or 0),
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
    # A run parked on a question is not UNFINISHED: it waits for an answer it
    # can actually receive and then continues in place. The two share the
    # status and differ by the reason carried on the record.
    "awaiting_question": (STATUS_AWAITING_INPUT, None, None),
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


# ─── Result view of a run record ────────────────────────────────────────────


def _artifacts_of(raw: object) -> List["Artifact"]:
    """Return the artifact list of a record field, dropping unusable items.

    Two shapes reach here: already-built ``Artifact`` objects (a live record)
    and plain dicts (a serialized one). An entry without a path names nothing
    and is skipped instead of producing an artifact nobody can open.
    """
    out: List[Artifact] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, Artifact):
            out.append(item)
            continue
        if isinstance(item, dict) and str(item.get("path") or "").strip():
            out.append(Artifact(
                path=str(item.get("path") or ""),
                kind=str(item.get("kind") or "file"),
                digest=str(item.get("digest") or ""),
            ))
    return out


def _int_of(value: object) -> int:
    """Return *value* as an int, or 0 when it is not a number."""
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _float_of(value: object) -> float:
    """Return *value* as a float, or 0.0 when it is not a number."""
    try:
        return float(value or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def result_from_record(record: Dict[str, Any]) -> Optional[AgentRunResult]:
    """Build an ``AgentRunResult`` view of a live or stored run record.

    This is the ONE place where a record turns back into a result, so the
    answer a caller receives does not depend on which layer produced it - the
    same fields reach the model whether the run is still in the registry or
    was rebuilt from the durable store.

    Two record shapes are accepted. A record written AFTER the run finished
    carries a nested ``result`` object with the full summary and artifacts. An
    older or live-shaped record keeps those fields at the top level, so a
    record from a previous version still answers instead of failing.

    Returns None when *record* is not a mapping at all.
    """
    if not isinstance(record, dict):
        return None
    # The nested object is the authoritative one when present: it was frozen
    # from the result itself, not reconstructed from the run bookkeeping.
    nested = record.get("result")
    source: Dict[str, Any]
    if isinstance(nested, dict) and str(nested.get("run_id") or "").strip():
        source = nested
    else:
        source = record
    tokens = source.get("tokens_used")
    pending = source.get("pending")
    return AgentRunResult(
        run_id=str(source.get("run_id") or record.get("run_id") or ""),
        status=str(source.get("status") or record.get("status") or STATUS_RUNNING),
        stop_reason=source.get("stop_reason"),
        summary=str(source.get("summary") or ""),
        artifacts=_artifacts_of(source.get("artifacts")),
        error_code=source.get("error_code"),
        steps_used=_int_of(source.get("steps_used")),
        tokens_used=dict(tokens) if isinstance(tokens, dict) else {},
        child_thread_id=str(source.get("child_thread_id")
                             or record.get("child_thread_id") or ""),
        pending=[str(item) for item in pending] if isinstance(pending, (list, tuple)) else [],
        resume_hint=str(source.get("resume_hint") or ""),
        retryable=bool(source.get("retryable", record.get("retryable", True))),
        # A parked run answers from either shape: the frozen result of a
        # finished record carries the question, and a live record carries it
        # at the top level. Both are read so a caller sees the same answer
        # whether the run is still in the registry or was rebuilt from disk.
        await_reason=str(source.get("await_reason")
                          or record.get("await_reason") or ""),
        question=AgentQuestion.from_dict(source.get("question")
                                         if source.get("question") is not None
                                         else record.get("question")),
        dialog_rounds=_int_of(source.get("dialog_rounds")
                              if source.get("dialog_rounds") is not None
                              else record.get("dialog_rounds")),
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
