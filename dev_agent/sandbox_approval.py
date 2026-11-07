"""How an approved execution is distinguished from a requested one.

Consent must not travel inside a tool call: an argument is visible to the
model, it can be emitted by mistake or on the instruction of a web page, and
then the confirmation dialog never appears. The approval therefore lives in
a context variable that only the agent loop sets, and only for the duration
of the dispatch the user actually approved.

The variable is per execution context, not a process global: two dialogs in
one Streamlit process never see each other's approval.
"""

from __future__ import annotations

import contextlib
import contextvars

#: True only inside the dispatch of an operation the user just approved.
_APPROVAL_GRANTED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sagaai_tool_approval_granted", default=False,
)

#: The run whose dispatch is executing now. A transport has no click to
#: offer, so its approval is a ticket bound to this run and to one exact
#: operation; the scope is what tells the consumer which ticket to read.
_APPROVAL_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sagaai_tool_approval_scope", default="",
)


def approvals_granted() -> bool:
    """Return True when the current call runs under a user approval."""
    return bool(_APPROVAL_GRANTED.get())


@contextlib.contextmanager
def approval_scope(scope: str):
    """Mark the enclosed dispatch as belonging to *scope* (a run id).

    The value comes from the loop state, never from a tool argument: it is
    the identity of the run whose human decision a ticket records.
    """
    token = _APPROVAL_SCOPE.set(str(scope or ""))
    try:
        yield
    finally:
        _APPROVAL_SCOPE.reset(token)


def current_scope() -> str:
    """Return the run whose dispatch is executing, or an empty string."""
    return str(_APPROVAL_SCOPE.get() or "")


def consume_transport_approval(tool: str, args) -> str:
    """Return the transport decision for ONE operation: allow, deny or "".

    An interactive dialog answers with a click; a transport answers with a
    ticket the gateway issued after a human decided. The ticket is read
    here, at the moment of the re-dispatch, and it is single-use: consuming
    it before the operation runs is what makes a second identical call
    require a second decision.

    An empty string means "no answer applies" - no scope, no ticket, an
    expired one, or a ticket issued for a different operation. The caller
    then parks the run as usual, which is the safe reading of "unknown".
    """
    scope = current_scope()
    if not scope:
        return ""
    try:
        from core.gateway import approvals as tickets

        return str(tickets.consume(scope, tool, args or {}) or "")
    except Exception:  # pragma: no cover - no gateway means no ticket
        return ""


@contextlib.contextmanager
def approval_context():
    """Mark the enclosed dispatch as approved by the user.

    Nothing a tool call can carry reaches this flag: the loop enters the
    context only after the Allow button was clicked.
    """
    token = _APPROVAL_GRANTED.set(True)
    try:
        yield
    finally:
        _APPROVAL_GRANTED.reset(token)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
