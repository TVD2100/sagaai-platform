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


def _envelope(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an error body against the published envelope.

    The body is assembled by hand in several places, and a missing key would
    otherwise only surface at the client. Validating it here turns such a
    drift into a log line next to the request id that produced it.

    The response itself is NOT withheld when validation fails: a client that
    asked a reasonable question should still learn that the server refused,
    and a 500 raised inside the 500-handler explains nothing. The mismatch is
    reported loudly instead, and the body is returned unchanged.
    """
    from server import schemas

    try:
        schemas.ErrorEnvelope.model_validate(body)
    except Exception as exc:  # pragma: no cover - defensive reporting
        log.warning("error envelope does not match the model: %s (%s)", exc, body)
    return body


def error_response(exc: gateway_errors.GatewayError,
                   request_id: str = "") -> JSONResponse:
    """Build the JSON response of one gateway refusal."""
    return JSONResponse(
        status_code=exc.status,
        content=_envelope(exc.to_envelope(request_id=request_id)),
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
    return JSONResponse(status_code=int(exc.status_code),
                        content=_envelope(payload),
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
    return JSONResponse(status_code=500, content=_envelope(payload))


# ─── OpenAPI documentation ─────────────────────────────────────────────────

#: Statuses an authenticated route may answer with. The body is always the
#: same envelope; ``error_code`` inside it names the actual reason, so the
#: client branches on the code and not on the status.
ERROR_STATUSES = (400, 401, 403, 404, 409, 413, 429, 500, 501, 503)

#: One short line per status, used in the generated schema.
ERROR_DESCRIPTIONS = {
    400: "The request is not valid",
    401: "A bearer token is required",
    403: "The token may not do this",
    404: "The resource does not exist or is outside the token scope",
    409: "The resource is in a state that refuses the operation",
    413: "The request body is too large",
    429: "Too many requests",
    500: "Internal error",
    501: "The feature is not enabled",
    503: "The service is not ready",
}


def error_responses() -> Dict[Any, Any]:
    """Return the ``responses`` mapping that documents the error envelope.

    Declared once here instead of per route: the envelope is identical on
    every route, and a route documenting a different one would itself be the
    defect. The model is imported lazily because ``server.schemas`` is the
    transport layer and this module is the error layer - neither should
    import the other at module import time.
    """
    from server import schemas

    return {status: {"model": schemas.ErrorEnvelope,
                     "description": ERROR_DESCRIPTIONS[status]}
            for status in ERROR_STATUSES}


def install(app: FastAPI) -> None:
    """Register every handler on an application instance.

    The three ignores below are a limitation of the exception-handler
    typing, not of the handlers: Starlette declares the parameter as
    ``Callable[[Request, Exception], Response]`` while each handler takes
    the exception TYPE it was registered for. That narrowing is exactly how
    the framework is meant to be used, so the mismatch is silenced here
    rather than widening every handler to ``Exception`` and losing the type
    inside it.
    """
    app.add_exception_handler(gateway_errors.GatewayError,
                              gateway_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError,
                              validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException,
                              http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, internal_handler)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
