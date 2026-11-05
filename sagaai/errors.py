"""
sagaai.errors - the one exception a command raises, and its translation.

A command does not print and does not exit: it either returns a code or
raises :class:`CliError`. That keeps the formatting in one place and makes a
command testable without capturing stdout.

The translation of a core failure is deliberate. ``GatewayError`` already
carries a machine code and an HTTP status; the HTTP status maps onto the
console code almost one-to-one, and the few divergences are stated below.
Everything that is not a known error is a FAILURE with the exception text -
never a traceback, because a traceback in stdout breaks ``--json`` for every
script that reads it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sagaai import exit_codes


class CliError(Exception):
    """A command failed; carries the console code and a machine code.

    ``code`` is the stable identifier a script branches on (``unknown_agent``,
    ``api_disabled``, ...). ``exit_code`` is the number the process returns.
    ``hint`` is the one line a person needs to fix it, and it is optional: a
    report that has nothing useful to add says nothing rather than repeating
    the message.
    """

    def __init__(self, code: str, message: str = "", *,
                 exit_code: int = exit_codes.FAILURE,
                 hint: str = "",
                 details: Optional[Dict[str, Any]] = None) -> None:
        self.code = str(code or "failure")
        self.message = str(message or self.code)
        self.exit_code = int(exit_code)
        self.hint = str(hint or "")
        self.details: Dict[str, Any] = dict(details or {})
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        """Return the ``error`` object of the JSON envelope."""
        payload: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = dict(self.details)
        return payload


#: HTTP status of a gateway refusal -> console code.
#:
#: 400/404/422 - the caller asked for something that does not exist or does
#: not parse: CONFIG. 401/403 - the caller may not do it: CONFIG as well, since
#: nothing about the installation is broken. 409 - a state conflict: CONFLICT
#: only when a RUN holds the resource; an ordinary "already finished" answer
#: is CONFIG, because retrying will not change it. 429 and 503 - the resource
#: is busy or not ready: CONFLICT, the documented "ask again later".
_STATUS_TO_EXIT = {
    400: exit_codes.CONFIG,
    401: exit_codes.CONFIG,
    403: exit_codes.CONFIG,
    404: exit_codes.CONFIG,
    409: exit_codes.CONFIG,
    413: exit_codes.CONFIG,
    422: exit_codes.CONFIG,
    429: exit_codes.CONFLICT,
    500: exit_codes.FAILURE,
    501: exit_codes.CONFIG,
    503: exit_codes.CONFLICT,
}


def from_gateway(exc: Any) -> CliError:
    """Translate a ``core.gateway.GatewayError`` into a :class:`CliError`.

    The gateway error is duck-typed on purpose: importing the class here would
    tie the CLI to a specific core version for no benefit, and the attributes
    it reads (``code``, ``message``, ``status``, ``details``) are its public
    shape.
    """
    code = str(getattr(exc, "code", "") or "gateway_error")
    message = str(getattr(exc, "message", "") or str(exc))
    status = int(getattr(exc, "status", 0) or 0)
    exit_code = _STATUS_TO_EXIT.get(status, exit_codes.FAILURE)
    details = getattr(exc, "details", None)
    hint = ""
    if code == "agent_not_allowed":
        allowed = ", ".join(str(item) for item in (details or {}).get("allowed") or [])
        hint = "allowed employees: " + allowed if allowed else ""
    elif code == "unknown_agent":
        hint = "list the installed employees: sagaai agents"
    elif code == "run_in_progress":
        exit_code = exit_codes.CONFLICT
        active = str((details or {}).get("active_run_id") or "")
        hint = ("the thread already has a live run: " + active) if active else ""
    elif code in ("api_disabled", "api_not_configured"):
        hint = "enable the API and create a token: sagaai token create"
    elif code == "store_unavailable":
        hint = "check the data folder: sagaai doctor"
    return CliError(code, message, exit_code=exit_code,
                    hint=hint, details=dict(details or {}))


def config_error(code: str, message: str, hint: str = "") -> CliError:
    """Return a CONFIG failure - a fixable request or an unconfigured state."""
    return CliError(code, message, exit_code=exit_codes.CONFIG, hint=hint)


def conflict_error(code: str, message: str, hint: str = "") -> CliError:
    """Return a CONFLICT failure - the resource is held by something else."""
    return CliError(code, message, exit_code=exit_codes.CONFLICT, hint=hint)


def failure(code: str, message: str, hint: str = "") -> CliError:
    """Return a FAILURE - the operation was attempted and did not succeed."""
    return CliError(code, message, exit_code=exit_codes.FAILURE, hint=hint)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
