"""
server.deps - dependencies every authenticated route shares.

Three things come out of here: the caller identity, the slug scope it
carries, and the run-id path component validated against its documented
shape. Routes receive them as ordinary FastAPI dependencies, so the
signature of a handler states what it needs.

The scope is deliberately ``Optional[List[str]]`` and NOT a wildcard when
absent: ``None`` means "no authenticated caller" and every route refuses it.
An empty list is a real answer - a token that may reach nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Depends, Request

from core.gateway import errors as gateway_errors
from core.gateway.dialog import is_run_id
from server import auth

# ─── Authentication ─────────────────────────────────────────────────────────


def require_caller(request: Request) -> Dict[str, Any]:
    """Authenticate the caller and attach its identity to the request.

    The identity is resolved once per request and stored on ``request.state``
    so a route, a middleware and an error handler all read the same answer
    instead of authenticating three times.
    """
    cached = auth.identity_of(request)
    if cached:
        return cached
    identity = auth.authenticate(request)
    try:
        request.state.identity = identity
    except Exception:  # pragma: no cover - defensive
        pass
    return identity


def caller_scope(caller: Dict[str, Any] = Depends(require_caller)
                ) -> List[str]:
    """Return the slug scope of the authenticated caller."""
    return list(caller.get("slugs") or [])


# ─── Path validation ────────────────────────────────────────────────────────


def run_id_path(run_id: str) -> str:
    """Return a path run id after checking its shape.

    The shape is part of the contract: a caller cannot address an arbitrary
    string, and a malformed id never reaches the run store where it would
    cost a directory lookup per attempt.
    """
    text = str(run_id or "").strip()
    if not is_run_id(text):
        raise gateway_errors.invalid_request(
            "The run id has the wrong shape",
            field="run_id", expected="r_<28 hex characters>",
        )
    return text


def thread_id_path(thread_id: str) -> str:
    """Return a path thread id after a light sanity check."""
    text = str(thread_id or "").strip()
    if not text or len(text) > 120:
        raise gateway_errors.invalid_request(
            "The thread id is missing or too long", field="thread_id")
    return text


def optional_scope(caller: Dict[str, Any] = Depends(require_caller)
                   ) -> Optional[List[str]]:
    """Return the scope as a nullable list, for owner-only helpers."""
    slugs = caller.get("slugs")
    if slugs is None:
        return None
    return list(slugs)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
