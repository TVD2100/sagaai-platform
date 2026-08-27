"""
core.api_errors - unified exception contract for AI-service API calls.

All failures inside ``core.api_layer`` (and callers of ``send_request``)
are raised as subclasses of ``APIError`` instead of being returned as
Russian-language strings or leaking raw ``requests`` exceptions.

Callers can catch ``APIError`` and use ``api_error_message(e, lang)`` to
render a localised, user-facing message, or inspect ``e.code`` for
programmatic handling.

Hierarchy:
    APIError
    ├── ServiceNotFoundError   (service name is not in services/)
    ├── ApiKeyMissingError     (required key/credential is not configured)
    ├── AuthTypeUnknownError   (service has an unknown auth_type)
    ├── ProviderHTTPError      (provider returned a non-200 status)
    ├── RequestTimeoutError    (requests.exceptions.Timeout)
    └── NetworkError           (requests.exceptions.RequestException)
"""

from __future__ import annotations

from typing import Optional


class APIError(Exception):
    """Base class for all API-layer failures.

    Attributes:
        code: short machine-readable identifier (e.g. "service_not_found").
        service: optional name of the AI service that failed.
        detail: optional extra context (provider message, status code, ...).
    """

    code = "api_error"

    def __init__(
        self,
        message: str = "API error",
        *,
        service: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.service = service
        self.detail = detail
        super().__init__(message)

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


class ServiceNotFoundError(APIError):
    """Raised when a skill references a service that is not registered."""

    code = "service_not_found"

    def __init__(self, service: str, **kwargs) -> None:
        super().__init__(
            f"Service '{service}' not found in services/",
            service=service,
            **kwargs,
        )


class ApiKeyMissingError(APIError):
    """Raised when a service's required key/credential is not configured."""

    code = "api_key_missing"

    def __init__(self, service: str, field: str = "api key", **kwargs) -> None:
        self.field = field
        super().__init__(
            f"API key for '{service}' is not configured (missing {field})",
            service=service,
            **kwargs,
        )


class AuthTypeUnknownError(APIError):
    """Raised when a service declares an unsupported auth_type."""

    code = "auth_type_unknown"

    def __init__(self, service: str, auth_type: str, **kwargs) -> None:
        self.auth_type = auth_type
        super().__init__(
            f"Unknown auth_type '{auth_type}' in service '{service}'",
            service=service,
            **kwargs,
        )


class ProviderHTTPError(APIError):
    """Raised when the AI provider returns a non-200 HTTP status."""

    code = "provider_http_error"

    def __init__(
        self,
        status_code: int,
        body: str = "",
        *,
        service: Optional[str] = None,
        **kwargs,
    ) -> None:
        self.status_code = status_code
        self.body = body
        msg = f"HTTP {status_code}"
        if body:
            msg += f": {body}"
        super().__init__(msg, service=service, detail=body or None, **kwargs)


class RequestTimeoutError(APIError):
    """Raised when the provider request times out."""

    code = "request_timeout"

    def __init__(self, service: Optional[str] = None, **kwargs) -> None:
        super().__init__("Request timed out", service=service, **kwargs)


class NetworkError(APIError):
    """Raised for network-level failures (DNS, connection refused, etc.)."""

    code = "network_error"

    def __init__(self, message: str = "Network error", *, service: Optional[str] = None, **kwargs) -> None:
        super().__init__(message, service=service, **kwargs)


# ─── Localised rendering helpers ──────────────────────────────────────────────


def api_error_message(exc: APIError, lang: Optional[str] = None) -> str:
    """Return a localised, user-facing message for an *APIError*.

    Uses the existing i18n keys (err_timeout / err_network / err_request)
    where they map cleanly; all other errors fall back to a comprehensible
    English message with the error code prefix.

    This function NEVER raises. If something unexpected happens (missing
    i18n keys, invalid input) it returns a plain string so UI code can
    always display something.
    """
    try:
        from core.i18n import t
    except Exception:
        t = None

    if t is not None:
        try:
            if isinstance(exc, RequestTimeoutError):
                return t("err_timeout", lang=lang)
            if isinstance(exc, NetworkError):
                return t("err_network", lang=lang, error=exc.message)
            if isinstance(exc, ApiKeyMissingError):
                return f"{exc.message} - please configure it in Settings."
            if isinstance(exc, ProviderHTTPError):
                return t("err_request", lang=lang, error=exc.message)
            if isinstance(exc, ServiceNotFoundError):
                return exc.message
            if isinstance(exc, AuthTypeUnknownError):
                return exc.message
            return t("err_request", lang=lang, error=exc.message)
        except Exception:
            pass

    if isinstance(exc, RequestTimeoutError):
        return "Request timed out"
    if isinstance(exc, NetworkError):
        return f"Network error: {exc.message}"
    return exc.message
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
