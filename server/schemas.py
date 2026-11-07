"""
server.schemas - the public request and response models.

The ONLY module of the platform that imports pydantic. Everything below it
speaks dataclasses, so the core keeps working in a process where the
transport stack is not installed at all.

Why a separate model layer instead of reusing the gateway dataclasses:

- the gateway types describe the CORE interface - they may gain a field for
  an internal reason without that field becoming part of a public contract;
- pydantic's job here is validation of untrusted input and the description
  of the OpenAPI schema, neither of which a core type should carry;
- the two are connected by explicit mappers at the bottom of this module, so
  a drift between them is a diff in one file rather than a silent API change.

Response models are declared even where a route could return a dict. A
declared model is what makes the generated schema useful and what makes a
missing field a test failure instead of a surprise for a client.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# ─── Common ─────────────────────────────────────────────────────────────────


class ErrorEnvelope(BaseModel):
    """The one shape every error response carries.

    Details are FLATTENED into the envelope rather than nested under a
    ``details`` key (``GatewayError.to_envelope`` does the same), so a client
    reads ``payload["field"]`` or ``payload["issues"]`` directly. The model
    allows extra keys for exactly that reason.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool = Field(default=False, description="Always false for an error.")
    error: str = Field(description="Human-readable description.")
    error_code: str = Field(description="Machine-readable code.")
    request_id: Optional[str] = Field(
        default=None, description="Correlates the response with the server log.")
    retryable: Optional[bool] = Field(
        default=None, description="True when the same call may succeed later.")


# ─── Health ─────────────────────────────────────────────────────────────────


class HealthOut(BaseModel):
    """Liveness of the process. Never touches the database."""

    status: str = Field(description="'ok' while the process answers.")
    pid: int = Field(description="Operating-system process id.")
    uptime_seconds: float = Field(description="Seconds since the process started.")
    api_enabled: bool = Field(description="Whether the API switch is on.")


class ReadyOut(BaseModel):
    """Readiness: can this process serve a data request right now."""

    status: str = Field(description="'ok' or 'not_ready'.")
    version: str = Field(description="Platform version.")
    checks: Dict[str, str] = Field(
        default_factory=dict, description="Per-component verdict.")
    missing: List[str] = Field(
        default_factory=list, description="Schema pieces that are absent.")
    hint: str = Field(default="", description="What to do about a failure.")


class VersionOut(BaseModel):
    """Version of the platform and of the API contract."""

    version: str = Field(description="Platform version.")
    api_version: int = Field(description="Version of the HTTP contract.")


# ─── Agents and assistants ─────────────────────────────────────────────────


class AgentOut(BaseModel):
    """One employee as the list endpoint shows it."""

    slug: str
    name: str = ""
    description: str = ""
    is_builtin: bool = False
    tools: List[str] = Field(default_factory=list)
    max_steps: int = 0
    auto_apply: bool = True
    created_at: str = ""
    updated_at: str = ""


class AgentDetailOut(AgentOut):
    """One employee including its prompt and configuration."""

    prompt_text: str = ""
    config: Dict[str, Any] = Field(default_factory=dict)


class AssistantOut(BaseModel):
    """One assistant as the list endpoint shows it."""

    assistant_id: str
    slug: str = ""
    name: str = ""
    description: str = ""
    service: str = ""
    model: str = ""
    temperature: float = 0.0
    tools: List[str] = Field(default_factory=list)
    max_tool_calls: Optional[int] = None
    max_tokens: Optional[int] = None
    reasoning_effort: str = ""
    created_at: str = ""


class AssistantDetailOut(AssistantOut):
    """One assistant including its prompt."""

    prompt_text: str = ""


class AgentListOut(BaseModel):
    """List wrapper: a bare array is harder to extend without a break."""

    items: List[AgentOut] = Field(default_factory=list)
    total: int = 0


class AssistantListOut(BaseModel):
    items: List[AssistantOut] = Field(default_factory=list)
    total: int = 0


# ─── Dialogs ───────────────────────────────────────────────────────────────


class DialogIn(BaseModel):
    """Body of a launch request.

    Unknown fields are IGNORED on purpose: a client built against a newer
    schema must not fail on an older server. The rule is part of the
    compatibility policy of the API.
    """

    model_config = ConfigDict(extra="ignore")

    agent_slug: str = Field(description="Employee to run.")
    message: str = Field(description="Task text of the first message.")
    workspace: str = Field(default="", description="Absolute project folder.")
    target_file: str = Field(default="", description="Single-file mode target.")
    thread_id: str = Field(default="", description="Continue this dialog.")
    title: str = Field(default="", description="Title of a new dialog.")
    mode: str = Field(default="auto", description="'auto' or 'step'.")
    web_search: bool = False
    safety: bool = True
    auto_apply: Optional[bool] = Field(
        default=None, description="None reads the value from the employee card.")
    max_steps: int = 0
    deadline_seconds: int = 0
    budget_tokens: int = 0
    file_context: str = ""
    file_name: str = ""


class MessageIn(BaseModel):
    """Body of a follow-up message."""

    model_config = ConfigDict(extra="ignore")

    message: str = Field(description="Text of the new message.")


class AnswerIn(BaseModel):
    """Body of an answer to a parked question."""

    model_config = ConfigDict(extra="ignore")

    answer: str = Field(description="Answer the run is waiting for.")


class PendingInputOut(BaseModel):
    """What a paused run needs from a human."""

    kind: str = ""
    tool: str = ""
    args: Dict[str, Any] = Field(default_factory=dict)
    reasons: List[str] = Field(default_factory=list)
    code: str = ""
    question: Optional[Dict[str, Any]] = None
    plan: str = ""


class RunLinksOut(BaseModel):
    """Addresses of the resources related to a run."""

    self: str = ""
    cancel: str = ""
    events: Optional[str] = None


class DialogAcceptedOut(BaseModel):
    """Answer of a launch request: the run exists, the work has not started."""

    run_id: str
    thread_id: str
    status: str = "running"
    agent_slug: str = ""
    workspace: str = ""
    mode: str = "auto"
    created_at: str = ""
    poll_after_ms: int = 2000
    idempotent_replay: bool = False
    links: RunLinksOut = Field(default_factory=RunLinksOut)


class RunOut(BaseModel):
    """State of one run."""

    run_id: str
    root_run_id: str = ""
    thread_id: str = ""
    parent_run_id: str = ""
    agent_slug: str = ""
    status: str = "running"
    stop_reason: Optional[str] = None
    error_code: Optional[str] = None
    summary: str = ""
    steps_used: int = 0
    tokens_used: Dict[str, int] = Field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    depth: int = 0
    background: bool = False
    live: bool = False
    heartbeat_age_s: Optional[float] = None
    stale_reason: str = ""
    pending_input: Optional[PendingInputOut] = None
    created_at: float = 0.0
    terminal: bool = False
    waited_ms: Optional[int] = None


class RunListOut(BaseModel):
    """List of runs."""

    items: List[RunOut] = Field(default_factory=list)
    total: int = 0


class ConfirmIn(BaseModel):
    """Body of a decision on a parked dangerous operation.

    ``decision`` is ``allow`` or ``deny``. Anything else is refused: the
    default is not "allow", because a typo must never approve a run.
    """

    model_config = ConfigDict(extra="ignore")

    decision: str = Field(description="'allow' or 'deny'.")
    ttl_seconds: int = Field(default=0, ge=0, le=3600,
                             description="Lifetime of the issued ticket.")


class ConfirmOut(BaseModel):
    """What the gateway did with a decision."""

    run_id: str
    decision: str = ""
    tool: str = ""
    resumed: bool = False
    expires_at: float = 0.0


class CancelIn(BaseModel):
    """Body of a cancel request."""

    model_config = ConfigDict(extra="ignore")

    cascade: bool = True


class CancelOut(BaseModel):
    """Outcome of a cancellation."""

    run_id: str
    cancelled: bool = False
    previous_status: str = "running"
    reason: str = "requested"
    children: List[str] = Field(default_factory=list)
    marker_written: bool = False


# ─── Threads ───────────────────────────────────────────────────────────────


class MessageOut(BaseModel):
    """One dialog message."""

    index: int = 0
    role: str = "user"
    content: str = ""
    ts: str = ""
    file_name: str = ""
    file_chars: int = 0
    hidden: bool = False
    events: List[Dict[str, Any]] = Field(default_factory=list)
    tokens: Dict[str, int] = Field(default_factory=dict)


class ThreadOut(BaseModel):
    """Index row of one dialog."""

    thread_id: str
    title: str = ""
    agent_slug: str = ""
    agent_name: str = ""
    workspace: str = ""
    target_file: str = ""
    kind: str = ""
    created_at: str = ""
    updated_at: str = ""
    message_count: Optional[int] = None


class ThreadListOut(BaseModel):
    items: List[ThreadOut] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 50
    truncated: bool = False


class ThreadWindowOut(BaseModel):
    thread: Optional[ThreadOut] = None
    messages: List[MessageOut] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 50
    remaining: int = 0


class SearchOut(BaseModel):
    hits: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    query: str = ""


# ─── Mappers ────────────────────────────────────────────────────────────────


def agent_out(card) -> AgentOut:
    """Return the response model of an employee card."""
    return AgentOut(**card.to_dict())


def agent_detail_out(detail) -> AgentDetailOut:
    """Return the response model of an employee with its prompt."""
    return AgentDetailOut(**detail.to_dict())


def assistant_out(card) -> AssistantOut:
    """Return the response model of an assistant card."""
    return AssistantOut(**card.to_dict())


def assistant_detail_out(detail) -> AssistantDetailOut:
    """Return the response model of an assistant with its prompt."""
    return AssistantDetailOut(**detail.to_dict())


def run_out(snapshot, *, waited_ms: Optional[int] = None) -> RunOut:
    """Return the response model of a run snapshot."""
    payload = snapshot.to_dict()
    pending = payload.get("pending_input")
    if isinstance(pending, dict):
        payload["pending_input"] = PendingInputOut(**pending)
    if waited_ms is not None:
        payload["waited_ms"] = int(waited_ms)
    return RunOut(**payload)


def thread_out(summary) -> ThreadOut:
    """Return the response model of a thread summary."""
    return ThreadOut(**summary.to_dict())


def message_out(view) -> MessageOut:
    """Return the response model of one message."""
    return MessageOut(**view.to_dict())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0