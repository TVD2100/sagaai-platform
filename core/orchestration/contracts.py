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
ERR_SANITIZED_REQUIRED = "sanitized_required"
ERR_PROVIDER_ERROR = "provider_error"
ERR_REFUSAL = "refusal"
ERR_CONTEXT_TOO_LARGE = "context_too_large"
ERR_NOT_IMPLEMENTED = "not_implemented"
ERR_LIMIT_EXCEEDED = "limit_exceeded"

# ─── Defaults of the delegation limits ───────────────────────────────────────

DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_ACTIVE_SUBAGENTS = 3
DEFAULT_SUBAGENT_MAX_STEPS = 20
DEFAULT_SUBAGENT_BUDGET_TOKENS = 0          # 0 = unlimited
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 600
DEFAULT_TOTAL_SUBAGENT_RUNS = 32


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
    Active Task. ``background`` is accepted and ignored in v1 (always
    blocking); ``inline_orchestrator`` is reserved for phase 2 and answers
    ``not_implemented``.
    """

    agent_slug: Optional[str]
    request: AgentBrief
    thread_id: str = ""
    parent_run_id: Optional[str] = None
    background: bool = False
    inline_orchestrator: Optional[dict] = None


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

    ``run`` is blocking in v1 and returns the child result. ``cancel``
    cascades over ``parent_run_id`` by default and is idempotent.
    ``subscribe`` returns an unsubscribe callable; events of descendants are
    delivered to every ancestor subscriber automatically.
    """

    def run(self, req: SpawnRequest) -> AgentRunResult:  # pragma: no cover - protocol
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
