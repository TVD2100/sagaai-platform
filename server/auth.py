"""
server.auth - who is calling, and what may they reach.

The UI password is not reused here. ``core.auth`` is bound to a Streamlit
session: it imports streamlit at module level, it keeps the authenticated
flag in ``st.session_state`` and it has a UX exception that opens the gate
while an agent is working. None of that means anything to an HTTP caller,
and reusing it would drag the whole UI into the server process.

What this module does instead:

- reads the token records of ``core.api_token_store`` (hashes only);
- compares in constant time and refuses anything that does not start with
  the product prefix;
- returns the slug SCOPE the token carries, which every route passes down to
  the gateway;
- fails CLOSED: a switched-off API answers 503, an API without a usable
  token answers 503, and neither ever becomes "no token, so let it through".

The records are cached for a few seconds. A rotation in the settings page
then takes effect without restarting the server, while a burst of requests
still costs one settings read instead of one per call.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request

from core import api_token_store as store
from core.gateway import errors as gateway_errors

log = logging.getLogger("server.auth")

#: How long a token snapshot is reused. Short enough that a revoke is felt
#: almost immediately, long enough that a polling client does not turn every
#: status request into a settings read.
CACHE_TTL_SECONDS = 5.0
#: Failed attempts one address may make before it is throttled.
MAX_FAILED_ATTEMPTS = 10
#: Window of the failed-attempt counter.
ATTEMPT_WINDOW_SECONDS = 60.0

_CACHE_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"at": 0.0, "state": {}, "records": []}
_ATTEMPTS: Dict[str, List[float]] = {}


# ─── Cached snapshot ────────────────────────────────────────────────────────


def _snapshot(force: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return the cached API state and token records."""
    now = time.time()
    with _CACHE_LOCK:
        fresh = (now - float(_CACHE.get("at") or 0.0)) < CACHE_TTL_SECONDS
        if fresh and not force:
            return dict(_CACHE.get("state") or {}), list(_CACHE.get("records") or [])
    state = store.api_state()
    records = store.load_records()
    with _CACHE_LOCK:
        _CACHE["at"] = now
        _CACHE["state"] = state
        _CACHE["records"] = records
    return state, records


def reset_cache() -> None:
    """Drop the cached snapshot (tests and a forced reload)."""
    with _CACHE_LOCK:
        _CACHE["at"] = 0.0
        _CACHE["state"] = {}
        _CACHE["records"] = []
    _ATTEMPTS.clear()


# ─── Throttling ─────────────────────────────────────────────────────────────


def _client_key(request: Optional[Request]) -> str:
    """Return the throttle key of a caller."""
    if request is None:
        return "local"
    try:
        client = getattr(request, "client", None)
        return str(getattr(client, "host", "") or "local")
    except Exception:  # pragma: no cover - defensive
        return "local"


def _note_failure(key: str) -> None:
    """Record one failed attempt inside the current window."""
    now = time.time()
    with _CACHE_LOCK:
        stamps = [t for t in _ATTEMPTS.get(key, []) if (now - t) < ATTEMPT_WINDOW_SECONDS]
        stamps.append(now)
        _ATTEMPTS[key] = stamps[-MAX_FAILED_ATTEMPTS * 2:]


def _clear_failures(key: str) -> None:
    with _CACHE_LOCK:
        _ATTEMPTS.pop(key, None)


def _throttled(key: str) -> bool:
    """Return True when *key* exceeded its failed-attempt allowance."""
    now = time.time()
    with _CACHE_LOCK:
        stamps = [t for t in _ATTEMPTS.get(key, []) if (now - t) < ATTEMPT_WINDOW_SECONDS]
        _ATTEMPTS[key] = stamps
        return len(stamps) >= MAX_FAILED_ATTEMPTS


# ─── Header parsing ─────────────────────────────────────────────────────────


def bearer_of(request: Optional[Request]) -> str:
    """Return the bearer token of a request, or an empty string.

    Only the ``Bearer`` scheme is accepted. A bare token, a basic-auth pair
    and a query parameter are all refused: the first two are protocol
    errors, and a token in a query string ends up in every access log and
    proxy cache on the way.
    """
    if request is None:
        return ""
    raw = ""
    try:
        raw = str(request.headers.get("authorization") or "").strip()
    except Exception:  # pragma: no cover - defensive
        return ""
    if not raw:
        return ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


# ─── Verification ───────────────────────────────────────────────────────────


def authenticate(request: Optional[Request] = None,
                 *, token: str = "") -> Dict[str, Any]:
    """Return the caller identity, or raise a ``GatewayError``.

    The identity dict carries ``token_id`` (the short public id, safe to
    log), ``slugs`` (the scope) and ``note`` (the owner's label). It never
    carries the token itself.
    """
    key = _client_key(request)
    if _throttled(key):
        raise gateway_errors.GatewayError(
            gateway_errors.ERR_TOO_MANY_ATTEMPTS,
            "Too many failed authentication attempts",
        )

    state, records = _snapshot()
    if not state.get("enabled"):
        raise gateway_errors.api_disabled()
    if not state.get("available"):
        raise gateway_errors.api_not_configured()

    presented = str(token or "").strip() or bearer_of(request)
    if not presented:
        _note_failure(key)
        raise gateway_errors.unauthorized()

    record = store.verify_token(presented, records)
    if record is None:
        _note_failure(key)
        raise gateway_errors.invalid_token()
    if store.is_expired(record):
        _note_failure(key)
        raise gateway_errors.token_expired()

    _clear_failures(key)
    return {
        "token_id": str(record.get("id") or ""),
        "slugs": store.scope_of(record),
        "note": str(record.get("note") or ""),
    }


def identity_of(request: Request) -> Dict[str, Any]:
    """Return the identity the dependency stored on the request."""
    return dict(getattr(request.state, "identity", {}) or {})


def scope_of(request: Request) -> Optional[List[str]]:
    """Return the slug scope the middleware resolved, if any.

    ``None`` means "no authenticated caller" and is never a wildcard. A
    route that receives it must refuse the request.
    """
    identity = identity_of(request)
    if not identity:
        return None
    return list(identity.get("slugs") or [])
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
