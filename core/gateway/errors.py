"""
core.gateway.errors - the gateway error type and its HTTP translation.

One exception type carries a machine code from ``core.orchestration.contracts``
and the HTTP status an API call should answer with. Routes raise it; the
service module turns it into a response. A route that picks its own 404 or
409 has already stepped over the boundary - the mapping lives here so it
stays reviewable in one table.

Codes are NOT invented here. The ``ERR_*`` constants of the run contract are
the vocabulary; a gateway-only condition (a thread outside the token scope,
for instance) reuses the closest contract code where one exists and adds a
documented local code where none does.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ─── Gateway-local codes (no contract equivalent) ───────────────────────────

#: The API is switched off in the settings page.
ERR_API_DISABLED = "api_disabled"
#: The API is switched on but no usable token exists. Fail closed.
ERR_API_NOT_CONFIGURED = "api_not_configured"
#: No Authorization header, or a header that is not a bearer token.
ERR_UNAUTHORIZED = "unauthorized"
#: A bearer token that matches no stored record.
ERR_INVALID_TOKEN = "invalid_token"
#: A token whose ``expires_at`` has passed.
ERR_TOKEN_EXPIRED = "token_expired"
#: The caller exceeded the failed-attempt allowance.
ERR_TOO_MANY_ATTEMPTS = "too_many_attempts"
#: The target thread does not exist, or lies outside the token scope. The two
#: are deliberately indistinguishable: a 403 would confirm that a thread with
#: that id exists, which is exactly what an enumeration wants to learn.
ERR_THREAD_NOT_FOUND = "thread_not_found"
#: A root run is already active on this thread.
ERR_RUN_IN_PROGRESS = "run_in_progress"
#: A launch was refused because too many roots are already running.
ERR_TOO_MANY_RUNS = "too_many_runs"
#: The database schema is not ready to serve requests.
ERR_DB_NOT_READY = "db_not_ready"
#: The run store itself is unreadable.
ERR_STORE_UNAVAILABLE = "store_unavailable"
#: The request body does not validate.
ERR_INVALID_REQUEST = "invalid_request"
#: The body exceeds the transport limit.
ERR_PAYLOAD_TOO_LARGE = "payload_too_large"
#: The workspace lock could not be taken in time. Retryable.
ERR_BINDING_BUSY = "binding_busy"


#: Mapping of a contract or gateway code onto the HTTP status that describes
#: the OPERATION. A failure inside a run (provider error, timeout, step limit)
#: is never in this table: the operation succeeded and the run's own outcome
#: travels in its body.
HTTP_STATUS: Dict[str, int] = {
    ERR_API_DISABLED: 503,
    ERR_API_NOT_CONFIGURED: 503,
    ERR_UNAUTHORIZED: 401,
    ERR_INVALID_TOKEN: 401,
    ERR_TOKEN_EXPIRED: 401,
    ERR_TOO_MANY_ATTEMPTS: 429,
    ERR_THREAD_NOT_FOUND: 404,
    ERR_RUN_IN_PROGRESS: 409,
    ERR_TOO_MANY_RUNS: 429,
    ERR_DB_NOT_READY: 503,
    ERR_STORE_UNAVAILABLE: 500,
    ERR_INVALID_REQUEST: 400,
    ERR_PAYLOAD_TOO_LARGE: 413,
    ERR_BINDING_BUSY: 503,
    # Contract codes that describe the REQUEST rather than the run.
    "agent_not_allowed": 403,
    "assistant_not_allowed": 403,
    "unknown_agent": 404,
    "unknown_assistant": 404,
    "invalid_brief": 400,
    "invalid_args": 400,
    "context_too_large": 413,
    "not_implemented": 501,
    "limit_exceeded": 429,
    "too_many_parked": 429,
    "run_not_found": 404,
    "resume_unavailable": 409,
    "resume_chain_limit": 409,
    "nothing_to_answer": 409,
    "run_already_finished": 409,
    "question_limit": 409,
    "background_unsupported": 400,
    "verifier_not_configured": 409,
    "run_not_finished": 409,
    "confirmation_required": 409,
    "no_run_identity": 500,
    "internal_error": 500,
    # A run that is not ours answers as "not found": the true cause belongs in
    # the server log, not in a response a stranger can read.
    "not_your_run": 404,
}

#: Codes that mean "ask again later".
RETRYABLE_CODES = frozenset({
    ERR_TOO_MANY_RUNS,
    ERR_TOO_MANY_ATTEMPTS,
    ERR_DB_NOT_READY,
    ERR_BINDING_BUSY,
    "limit_exceeded",
    "too_many_parked",
})

#: Suggested pause before a retry, in seconds, per code.
RETRY_AFTER: Dict[str, int] = {
    ERR_TOO_MANY_RUNS: 15,
    ERR_TOO_MANY_ATTEMPTS: 60,
    ERR_DB_NOT_READY: 5,
    ERR_BINDING_BUSY: 30,
    "limit_exceeded": 15,
    "too_many_parked": 15,
}


class GatewayError(Exception):
    """One refusal of the gateway, carrying a code and its HTTP status.

    ``details`` is merged into the error envelope at the top level (the run
    contract does the same for ``error_result``), so a caller reads
    ``allowed`` or ``active_run_id`` without a second level of nesting.
    """

    def __init__(self, code: str, message: str = "",
                 *, status: Optional[int] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        self.code = str(code or "internal_error")
        self.message = str(message or self.code)
        self.status = int(status or HTTP_STATUS.get(self.code, 500))
        self.details: Dict[str, Any] = dict(details or {})
        super().__init__(self.message)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    @property
    def retry_after(self) -> Optional[int]:
        return RETRY_AFTER.get(self.code)

    def to_envelope(self, request_id: str = "") -> Dict[str, Any]:
        """Return the JSON body of an error response.

        The shape matches ``contracts.error_result`` (``ok``/``error``/
        ``error_code``) plus ``request_id``; details sit at the top level so a
        client can read them directly.
        """
        payload: Dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "error_code": self.code,
        }
        if request_id:
            payload["request_id"] = request_id
        payload.update(self.details)
        if self.retryable:
            payload["retryable"] = True
        return payload


# ─── Factories ──────────────────────────────────────────────────────────────


def unauthorized(message: str = "Missing or malformed bearer token") -> GatewayError:
    return GatewayError(ERR_UNAUTHORIZED, message)


def invalid_token(message: str = "Unknown API token") -> GatewayError:
    return GatewayError(ERR_INVALID_TOKEN, message)


def token_expired(message: str = "API token expired") -> GatewayError:
    return GatewayError(ERR_TOKEN_EXPIRED, message)


def api_disabled(message: str = "The HTTP API is disabled") -> GatewayError:
    return GatewayError(ERR_API_DISABLED, message)


def api_not_configured(message: str = "The HTTP API has no usable token") -> GatewayError:
    return GatewayError(ERR_API_NOT_CONFIGURED, message)


def thread_not_found(thread_id: str = "") -> GatewayError:
    detail = {"thread_id": thread_id} if thread_id else {}
    return GatewayError(ERR_THREAD_NOT_FOUND, "Thread not found",
                        details=detail)


def run_not_found(run_id: str = "") -> GatewayError:
    detail = {"run_id": run_id} if run_id else {}
    return GatewayError("run_not_found", "Run not found", details=detail)


def run_in_progress(active_run_id: str = "", thread_id: str = "") -> GatewayError:
    details: Dict[str, Any] = {}
    if active_run_id:
        details["active_run_id"] = active_run_id
    if thread_id:
        details["thread_id"] = thread_id
    return GatewayError(
        ERR_RUN_IN_PROGRESS,
        "A run is already active on this thread",
        details=details,
    )


def too_many_runs(active: int = 0, limit: int = 0) -> GatewayError:
    return GatewayError(
        ERR_TOO_MANY_RUNS,
        "Too many concurrent runs",
        details={"active": int(active), "limit": int(limit)},
    )


def invalid_request(message: str = "Invalid request",
                    **details: Any) -> GatewayError:
    return GatewayError(ERR_INVALID_REQUEST, message, details=details)


def db_not_ready(missing: Optional[list] = None, hint: str = "") -> GatewayError:
    details: Dict[str, Any] = {"missing": list(missing or [])}
    if hint:
        details["hint"] = hint
    return GatewayError(
        ERR_DB_NOT_READY,
        "The database schema is not ready",
        details=details,
    )


def unknown_agent(slug: str = "") -> GatewayError:
    detail = {"agent_slug": slug} if slug else {}
    return GatewayError("unknown_agent", "Unknown agent: " + (slug or ""),
                        details=detail)


def agent_not_allowed(slug: str = "", allowed: Optional[list] = None) -> GatewayError:
    return GatewayError(
        "agent_not_allowed",
        "Agent not allowed: " + (slug or ""),
        details={"agent_slug": slug, "allowed": list(allowed or [])},
    )


def not_implemented(what: str = "") -> GatewayError:
    return GatewayError("not_implemented",
                        "Not implemented: " + (what or "feature"),
                        details={"feature": what} if what else {})
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
