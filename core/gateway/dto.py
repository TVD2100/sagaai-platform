"""
transport dataclasses of the SagaAI gateway.

This is the single home of transport types: everything a non-UI caller
receives is declared here and nowhere else. The types are deliberately
dumb - frozen dataclasses with a ``to_dict`` - so the same object can be
rendered by the HTTP layer, printed by a CLI or asserted in a test.

Run types are NOT declared here. ``core.orchestration.contracts`` owns
``AgentRunResult``, ``CancelReport`` and ``AgentQuestion``; this module
embeds those types instead of copying their fields, because a second copy
of a 15-field result would drift on the first change.

No streamlit, no fastapi, no pydantic: the gateway must import in a
process where none of them are installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ─── Economy options ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EconomyOptions:
    """Cache-friendly context settings carried from the dialog card.

    ``anchor`` is the message index the provider prefix cache was built
    from. It survives a step and is restored by the next step, so the
    cached prefix is reused instead of re-sent.
    """

    mode: bool = False
    cache_enabled: bool = False
    cache_multiplier: int = 1
    tail_messages: Optional[int] = None
    anchor: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": bool(self.mode),
            "cache_enabled": bool(self.cache_enabled),
            "cache_multiplier": int(self.cache_multiplier or 1),
            "tail_messages": (None if self.tail_messages is None
                              else int(self.tail_messages)),
            "anchor": None if self.anchor is None else int(self.anchor),
        }


# ─── Dialog request / handle ────────────────────────────────────────────────


@dataclass(frozen=True)
class DialogRequest:
    """Everything needed to start or continue a root dialog run.

    ``mode`` selects the execution model:

    - ``auto`` - a background loop runs until a terminal status or a pause.
      The loop must carry a deadline and a token budget, and it runs with
      ``child_mode`` semantics: nobody is watching, so a confirmation
      request becomes a refusal the agent must rewrite, and an
      ``awaiting_user`` outcome becomes a bounded automatic continuation.
    - ``step`` - exactly one loop step. The caller repeats the call. This
      mirrors what the Streamlit page does on every rerun and keeps an
      interactive pause reachable.

    ``auto_apply=None`` means "read it from the orchestrator card"; an
    explicit True/False wins. The UI pins it to True today, which silently
    overrides the card - the gateway closes that gap.
    """

    agent_slug: str
    message: str
    workspace: str = ""
    target_file: str = ""
    thread_id: str = ""
    title: str = ""
    mode: str = "auto"
    web_search: bool = False
    safety: bool = True
    auto_apply: Optional[bool] = None
    max_steps: int = 0
    deadline_seconds: int = 0
    budget_tokens: int = 0
    file_context: str = ""
    file_name: str = ""
    economy: EconomyOptions = field(default_factory=EconomyOptions)

    def validate(self) -> Optional[str]:
        """Return an error text, or None when the request is usable."""
        if not str(self.agent_slug or "").strip():
            return "agent_slug is required"
        if not str(self.message or "").strip():
            return "message is required"
        if self.mode not in ("auto", "step"):
            return "mode must be 'auto' or 'step'"
        if self.workspace and not self.workspace.startswith("/"):
            return "workspace must be an absolute path"
        return None


@dataclass(frozen=True)
class DialogHandle:
    """Identity of a started dialog run."""

    thread_id: str
    run_id: str
    agent_slug: str
    workspace: str = ""
    created: bool = False
    status: str = "running"
    mode: str = "auto"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "agent_slug": self.agent_slug,
            "workspace": self.workspace,
            "created": bool(self.created),
            "status": self.status,
            "mode": self.mode,
        }


# ─── Pending input (a pause that needs a human) ─────────────────────────────


@dataclass(frozen=True)
class PendingInput:
    """What a paused run needs from a human.

    ``kind`` is one of ``plan_approval``, ``confirmation``, ``sanitized``,
    ``question`` or ``unfinished``. The UI reads these fields straight out
    of the loop state (``pending_confirmation_tool`` and friends); a
    transport caller has no loop state, so the values are lifted into a
    transport type here.
    """

    kind: str = ""
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    code: str = ""
    question: Optional[Dict[str, Any]] = None
    plan: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "tool": self.tool,
            "args": dict(self.args),
            "reasons": list(self.reasons),
            "code": self.code,
            "question": dict(self.question) if self.question else None,
            "plan": self.plan,
        }


# ─── Message and thread views ───────────────────────────────────────────────


@dataclass(frozen=True)
class MessageView:
    """One dialog message as a transport caller sees it.

    ``index`` is the ABSOLUTE position in the thread, not the page offset:
    it is the anchor every other read (window, search) uses.
    """

    index: int = 0
    role: str = "user"
    content: str = ""
    ts: str = ""
    file_name: str = ""
    file_chars: int = 0
    hidden: bool = False
    events: List[Dict[str, Any]] = field(default_factory=list)
    tokens: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": int(self.index),
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "file_name": self.file_name,
            "file_chars": int(self.file_chars),
            "hidden": bool(self.hidden),
            "events": list(self.events),
            "tokens": dict(self.tokens),
        }


@dataclass(frozen=True)
class ThreadSummary:
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "agent_slug": self.agent_slug,
            "agent_name": self.agent_name,
            "workspace": self.workspace,
            "target_file": self.target_file,
            "kind": self.kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
        }


@dataclass(frozen=True)
class ThreadPage:
    """A page of thread summaries."""

    items: List[ThreadSummary] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 50
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": int(self.total),
            "offset": int(self.offset),
            "limit": int(self.limit),
            "truncated": bool(self.truncated),
        }


@dataclass(frozen=True)
class ThreadWindow:
    """A window of messages of one thread."""

    thread: Optional[ThreadSummary] = None
    messages: List[MessageView] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 50
    remaining: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread": self.thread.to_dict() if self.thread else None,
            "messages": [m.to_dict() for m in self.messages],
            "total": int(self.total),
            "offset": int(self.offset),
            "limit": int(self.limit),
            "remaining": int(self.remaining),
        }


# ─── Run snapshots ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSnapshot:
    """What a caller may know about one run.

    ``live`` is False when the record was rebuilt from the durable store:
    that is a normal answer, not a failure. A caller that sees
    ``live=False`` + ``status='running'`` + a fresh ``heartbeat_age_s`` is
    looking at a run owned by another process.
    """

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
    tokens_used: Dict[str, int] = field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    depth: int = 0
    background: bool = False
    live: bool = False
    heartbeat_age_s: Optional[float] = None
    stale_reason: str = ""
    pending_input: Optional[PendingInput] = None
    created_at: float = 0.0

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "root_run_id": self.root_run_id,
            "thread_id": self.thread_id,
            "parent_run_id": self.parent_run_id,
            "agent_slug": self.agent_slug,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "error_code": self.error_code,
            "summary": self.summary,
            "steps_used": int(self.steps_used),
            "tokens_used": dict(self.tokens_used),
            "artifacts": list(self.artifacts),
            "depth": int(self.depth),
            "background": bool(self.background),
            "live": bool(self.live),
            "heartbeat_age_s": self.heartbeat_age_s,
            "stale_reason": self.stale_reason,
            "pending_input": (self.pending_input.to_dict()
                              if self.pending_input else None),
            "created_at": float(self.created_at or 0.0),
            "terminal": self.is_terminal,
        }


@dataclass(frozen=True)
class DialogSnapshot:
    """A run plus the dialog-level flags a caller needs to act."""

    handle: Optional[DialogHandle] = None
    run: Optional[RunSnapshot] = None
    pending_input: Optional[PendingInput] = None
    can_advance: bool = False
    can_cancel: bool = False
    can_answer: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handle": self.handle.to_dict() if self.handle else None,
            "run": self.run.to_dict() if self.run else None,
            "pending_input": (self.pending_input.to_dict()
                              if self.pending_input else None),
            "can_advance": bool(self.can_advance),
            "can_cancel": bool(self.can_cancel),
            "can_answer": bool(self.can_answer),
        }


@dataclass(frozen=True)
class DialogStepResult:
    """Outcome of one executed step."""

    run_id: str
    thread_id: str
    phase: str = ""
    final_status: str = ""
    result: Optional[Any] = None          # AgentRunResult (contracts)
    messages: List[MessageView] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    pending_input: Optional[PendingInput] = None
    can_advance: bool = False
    can_cancel: bool = False

    def to_dict(self) -> Dict[str, Any]:
        result = self.result
        if result is not None and hasattr(result, "to_dict"):
            result = result.to_dict()
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "phase": self.phase,
            "final_status": self.final_status,
            "result": result,
            "messages": [m.to_dict() for m in self.messages],
            "events": list(self.events),
            "pending_input": (self.pending_input.to_dict()
                              if self.pending_input else None),
            "can_advance": bool(self.can_advance),
            "can_cancel": bool(self.can_cancel),
        }


# ─── Entity cards ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrchestratorCard:
    """One employee as the list endpoint shows it."""

    slug: str
    name: str = ""
    description: str = ""
    is_builtin: bool = False
    tools: List[str] = field(default_factory=list)
    max_steps: int = 0
    auto_apply: bool = True
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "is_builtin": bool(self.is_builtin),
            "tools": list(self.tools),
            "max_steps": int(self.max_steps),
            "auto_apply": bool(self.auto_apply),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class OrchestratorDetail:
    """One employee including its prompt and config."""

    card: OrchestratorCard
    prompt_text: str = ""
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = self.card.to_dict()
        payload["prompt_text"] = self.prompt_text
        payload["config"] = dict(self.config)
        return payload


@dataclass(frozen=True)
class AssistantCard:
    """One assistant as the list endpoint shows it."""

    assistant_id: str
    slug: str = ""
    name: str = ""
    description: str = ""
    service: str = ""
    model: str = ""
    temperature: float = 0.0
    tools: List[str] = field(default_factory=list)
    max_tool_calls: Optional[int] = None
    max_tokens: Optional[int] = None
    reasoning_effort: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assistant_id": self.assistant_id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "service": self.service,
            "model": self.model,
            "temperature": float(self.temperature or 0.0),
            "tools": list(self.tools),
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AssistantDetail:
    """One assistant including its prompt.

    The database stores an assistant prompt under the key ``text`` while an
    orchestrator prompt lives under ``prompt_text``. The DTO normalises BOTH
    to ``prompt_text`` so a transport never has to know which entity it is
    reading.
    """

    card: AssistantCard
    prompt_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = self.card.to_dict()
        payload["prompt_text"] = self.prompt_text
        return payload


# ─── Health ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthReport:
    """Result of a liveness or readiness probe."""

    status: str = "ok"
    version: str = ""
    pid: int = 0
    uptime_seconds: float = 0.0
    components: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    hint: str = ""

    @property
    def ready(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "pid": int(self.pid),
            "uptime_seconds": float(self.uptime_seconds or 0.0),
            "components": dict(self.components),
            "missing": list(self.missing),
            "hint": self.hint,
        }


# ─── Search ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchPage:
    """Result of a message search."""

    hits: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    query: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hits": list(self.hits),
            "total": int(self.total),
            "query": self.query,
        }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
