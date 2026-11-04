"""
server.middleware - request identity, access logging and host allowlisting.

Three jobs, all of them about what a request leaves behind:

- **A request id** travels from the header (or is generated), into the log
  line and into any error body. It is the only way to tie a client report to
  a server event without logging the content of the request.
- **Access logging** records the route TEMPLATE, the status, the duration and
  the caller's token id - never the query string, which carries search terms
  (business data), and never a header value.
- **A host allowlist** answers plain DNS-rebinding: without it any page the
  owner opens in a browser could reach a loopback API, and the bearer token
  would not help because the browser would be sending it.

The redaction filter lives here too. It is a ``logging.Filter`` rather than
a convention, because a convention loses against one careless
``log.info("headers=%s", headers)``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

log = logging.getLogger("server.access")

#: Header a caller may supply to correlate its own logs.
REQUEST_ID_HEADER = "X-Request-Id"
#: Longest accepted request id. A longer one is replaced, not truncated.
MAX_REQUEST_ID_LEN = 64
#: Paths excluded from the access log: a health probe every ten seconds
#: would otherwise drown every real event.
QUIET_PATHS = ("/health", "/ready", "/version")
#: Hosts a loopback-bound server answers to.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "testserver")

_TOKEN_PATTERN = re.compile(r"saga_[A-Za-z0-9_\-]{20,}")
_BEARER_PATTERN = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


# ─── Redaction ──────────────────────────────────────────────────────────────


def scrub(text: str) -> str:
    """Return *text* with bearer tokens and token values removed.

    Used by the log filter and available to callers that must include a
    fragment of a request in a message.
    """
    if not text:
        return ""
    cleaned = _BEARER_PATTERN.sub("Bearer <redacted>", str(text))
    return _TOKEN_PATTERN.sub("saga_<redacted>", cleaned)


class RedactFilter(logging.Filter):
    """Strip credentials out of any record that reached a handler.

    The filter rewrites the formatted message and clears the argument tuple,
    so a ``log.info("auth=%s", header)`` cannot leak a token even though the
    call itself was careless.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def install_log_filter(name: str = "server") -> None:
    """Attach the redaction filter to the server loggers."""
    redactor = RedactFilter()
    for logger_name in (name, name + ".access", name + ".errors"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, RedactFilter) for item in logger.filters):
            logger.addFilter(redactor)


# ─── Request id ─────────────────────────────────────────────────────────────


def new_request_id() -> str:
    """Return a fresh request id in the documented shape."""
    return "req_" + uuid.uuid4().hex[:12]


def _accepted_request_id(raw: str) -> str:
    """Return a caller-supplied id when it is usable, else a fresh one."""
    text = str(raw or "").strip()
    if not text or len(text) > MAX_REQUEST_ID_LEN:
        return new_request_id()
    if not all(ch.isalnum() or ch in "-_.:" for ch in text):
        return new_request_id()
    return text


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id and log one line per request."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _accepted_request_id(request.headers.get(REQUEST_ID_HEADER, ""))
        try:
            request.state.request_id = request_id
        except Exception:  # pragma: no cover - defensive
            pass
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            self._log(request, request_id, 500, started)
            raise
        try:
            response.headers[REQUEST_ID_HEADER] = request_id
        except Exception:  # pragma: no cover - a streaming body may forbid it
            pass
        self._log(request, request_id, int(response.status_code), started)
        return response

    def _log(self, request: Request, request_id: str, status: int,
             started: float) -> None:
        path = str(getattr(request.url, "path", "") or "")
        if path in QUIET_PATHS:
            return
        duration_ms = int((time.monotonic() - started) * 1000)
        identity: Dict[str, Any] = getattr(request.state, "identity", {}) or {}
        route = str(getattr(request.scope.get("route"), "path", "") or path)
        log.info(
            "%s %s status=%s ms=%s token=%s request_id=%s",
            str(getattr(request, "method", "")), route, status, duration_ms,
            str(identity.get("token_id") or "-"), request_id,
        )


# ─── Host allowlist ─────────────────────────────────────────────────────────


def allowed_hosts(host: str, port: int,
                  allow_remote: bool = False) -> List[str]:
    """Return the ``Host`` header values this bind may answer.

    A loopback bind answers its own host forms only. An explicitly allowed
    remote bind keeps the loopback forms (the owner still reaches it locally)
    and adds its own address; a wildcard bind cannot enumerate hosts, so the
    check is left to whoever terminated TLS in front of it and the allowlist
    is bypassed by design - documented, not accidental.
    """
    host_text = str(host or "127.0.0.1").strip()
    port_text = int(port or 8765)
    hosts = ["%s:%d" % (item, port_text) for item in LOOPBACK_HOSTS]
    hosts += list(LOOPBACK_HOSTS)
    if allow_remote:
        if host_text in ("0.0.0.0", "::", ""):
            return ["*"]
        hosts.append(host_text)
        hosts.append("%s:%d" % (host_text, port_text))
    return hosts


def host_allowed(header: str, allowed: List[str]) -> bool:
    """Return True when a ``Host`` header may reach this server."""
    if not allowed:
        return True
    text = str(header or "").strip().lower()
    for item in allowed:
        candidate = str(item or "").strip().lower()
        if candidate == "*" or candidate == text:
            return True
    return False


class HostGuardMiddleware(BaseHTTPMiddleware):
    """Refuse a request whose ``Host`` is not on the allowlist.

    DNS rebinding is the reason this exists: a page in the owner's browser
    can send a request to ``127.0.0.1`` with its own name in the ``Host``
    header, and the browser will happily attach whatever credentials it has.
    Refusing the host breaks that path without touching a legitimate client.
    """

    def __init__(self, app: ASGIApp, allowed: Optional[List[str]] = None) -> None:
        super().__init__(app)
        self.allowed = list(allowed or [])

    async def dispatch(self, request: Request, call_next) -> Response:
        host = str(request.headers.get("host") or "")
        if not host_allowed(host, self.allowed):
            log.warning("rejected host %r request_id=%s", host,
                        str(getattr(request.state, "request_id", "")))
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Host not allowed",
                         "error_code": "host_not_allowed"},
            )
        return await call_next(request)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
