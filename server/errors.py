"""
server.errors - turning a gateway refusal into an HTTP response.

The gateway raises ``GatewayError`` with a machine code and an HTTP status;
this module is the single place those become bytes on the wire. Keeping the
translation here means a route never chooses a status code itself, and the
whole error surface of the API is reviewable in one table.

Two shapes, never mixed:

- **HTTP status** describes the OPERATION - did the caller's request make
  sense, was it allowed, does the target exist, is the server able to serve.
- **``error_code`` in the body** describes what went wrong, including the
  outcome of a RUN that failed after the request was accepted. A provider
  failure inside a run answers 200 with ``error_code=provider_error``: the
  call worked, the work did not.

The envelope matches ``core.orchestration.contracts.error_result``
(``ok``/``error``/``error_code``) plus ``request_id``, so a client that
already parses tool results parses this too.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.gateway import errors as gateway_errors

log = logging.getLogger("server.errors")


# ─── Response builders ──────────────────────────────────────────────────────


def _headers_for(code: str, retry_after: Optional[int]) -> Dict[str, str]:
    """Return the response headers a code requires."""
    headers: Dict[str, str] = {}
    if retry_after:
        headers["Retry-After"] = str(int(retry_after))
    if code in (gateway_errors.ERR_UNAUTHORIZED,
                gateway_errors.ERR_INVALID_TOKEN,
                gateway_errors.ERR_TOKEN_EXPIRED):
        # A bearer challenge tells a client HOW to authenticate, which a bare
        # 401 does not. The realm is static: it names the product, not a
        # user.
        headers["WWW-Authenticate"] = 'Bearer realm="sagaai"'
    return headers


def error_response(exc: gateway_errors.GatewayError,
                   request_id: str = "") -> JSONResponse:
    """Build the JSON response of one gateway refusal."""
    return JSONResponse(
        status_code=exc.status,
        content=exc.to_envelope(request_id=request_id),
        headers=_headers_for(exc.code, exc.retry_after),
    )


def _request_id_of(request: Request) -> str:
    """Return the request id the middleware attached, if any."""
    return str(getattr(request.state, "request_id", "") or "")


# ─── Handlers ───────────────────────────────────────────────────────────────


def gateway_handler(request: Request, exc: gateway_errors.GatewayError) -> JSONResponse:
    """Answer a ``GatewayError`` with its own status and envelope."""
    return error_response(exc, request_id=_request_id_of(request))


def validation_handler(request: Request,
                       exc: RequestValidationError) -> JSONResponse:
    """Answer a body that failed validation as ``400 invalid_request``.

    FastAPI answers ``422`` by default. That code is a WebDAV extension which
    a fair share of clients, proxies and libraries treat as "the server
    broke"; the request is simply wrong, so it is a ``400`` here.

    The ``input`` field of every issue is DROPPED. FastAPI echoes the
    offending value back, and a client that accidentally sent a token in the
    body would then see it in the response - and in any log that records
    error bodies.
    """
    issues = []
    for item in exc.errors():
        if not isinstance(item, dict):
            continue
        issues.append({
            "loc": [str(part) for part in (item.get("loc") or [])],
            "msg": str(item.get("msg") or ""),
            "type": str(item.get("type") or ""),
        })
    error = gateway_errors.invalid_request("The request body is not valid",
                                           issues=issues)
    return error_response(error, request_id=_request_id_of(request))


def http_exception_handler(request: Request,
                          exc: StarletteHTTPException) -> JSONResponse:
    """Answer a framework-raised status in the same envelope.

    A 404 raised by the router (unknown path, wrong method) would otherwise
    carry a plain ``{"detail": ...}`` and break every client that parses the
    error shape. The original status is preserved; only the body changes.
    """
    code = {404: "not_found", 405: "method_not_allowed",
            413: gateway_errors.ERR_PAYLOAD_TOO_LARGE}.get(
                int(exc.status_code), "http_error")
    payload: Dict[str, Any] = {
        "ok": False,
        "error": str(exc.detail or code),
        "error_code": code,
    }
    request_id = _request_id_of(request)
    if request_id:
        payload["request_id"] = request_id
    return JSONResponse(status_code=int(exc.status_code), content=payload,
                        headers=dict(exc.headers or {}))


def internal_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer an unexpected failure without leaking its text.

    A traceback carries paths, SQL and fragments of the configuration; none
    of it belongs in a response. The log keeps the whole exception, the
    client gets a code and the request id that ties its report to that log
    line.
    """
    request_id = _request_id_of(request)
    log.exception("unhandled error on %s %s (request_id=%s)",
                  request.method, request.url.path, request_id)
    payload: Dict[str, Any] = {
        "ok": False,
        "error": "Internal error",
        "error_code": "internal_error",
    }
    if request_id:
        payload["request_id"] = request_id
    return JSONResponse(status_code=500, content=payload)


def install(app: FastAPI) -> None:
    """Register every handler on an application instance."""
    app.add_exception_handler(gateway_errors.GatewayError, gateway_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, internal_handler)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
