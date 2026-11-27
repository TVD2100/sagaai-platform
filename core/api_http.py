"""
core.api_http - shared HTTP transport helpers for AI service calls.

Holds the TLS verification policy, the connection-retry budget and the
HTTP error-body extractors that every provider in core.api_layer uses.
No provider module is imported here, so the dependency graph stays acyclic:
providers depend on this module, never the other way round.
"""
import os as _os
import time

from core.api_errors import NetworkError, RequestTimeoutError

MODEL_REQUEST_TIMEOUT = (30, 600)
# Backward-compatible alias (same tuple; requests accepts it directly).
MODEL_RESPONSE_TIMEOUT = MODEL_REQUEST_TIMEOUT


# ─── TLS verification policy ────────────────────────────────────────────────
# By default ALL requests verify TLS certificates (verify=True).
# In rare cases (corporate proxies, self-signed internal CAs) an operator may
# explicitly opt out via SAGAAI_VERIFY_TLS=false, but this is strongly
# discouraged: disabling verification allows man-in-the-middle attacks that
# can expose API keys and conversation data.
_VERIFY_TLS = _os.environ.get("SAGAAI_VERIFY_TLS", "true").strip().lower() not in ("0", "false", "no", "off")


# ─── Connection-resilience retries ──────────────────────────────────────────
# Windows networks (and corporate proxies / NATs) occasionally drop long-lived
# quiet connections with ConnectionResetError. When a model request fails with
# a network-level error, retry it transparently: wait NETWORK_RETRY_DELAY
# seconds and re-send, up to NETWORK_RETRY_ATTEMPTS attempts in a row (3 by
# default). Provider HTTP errors (4xx/5xx) are NOT retried - only genuine
# transport failures.

_DEFAULT_RETRY_DELAY = 30.0
_DEFAULT_RETRY_ATTEMPTS = 3


def _retry_params() -> tuple:
    """Return (delay_seconds, max_attempts) read from env / defaults.

    Optional env overrides:
      SAGAAI_NETWORK_RETRY_DELAY    - pause between retries, seconds (default 30)
      SAGAAI_NETWORK_RETRY_ATTEMPTS - total attempts in a row (default 3)
    Invalid or mis-typed values silently fall back to the defaults.
    """
    delay = _DEFAULT_RETRY_DELAY
    attempts = _DEFAULT_RETRY_ATTEMPTS
    raw_delay = str(_os.environ.get("SAGAAI_NETWORK_RETRY_DELAY", "")).strip()
    if raw_delay:
        try:
            delay = max(0.0, float(raw_delay))
        except (TypeError, ValueError):
            pass
    raw_attempts = str(_os.environ.get("SAGAAI_NETWORK_RETRY_ATTEMPTS", "")).strip()
    if raw_attempts:
        try:
            attempts = max(1, int(raw_attempts))
        except (TypeError, ValueError):
            pass
    return delay, attempts


def retry_call(fn, *, retry_callback=None):
    """Call *fn* (one network request) with transparent connection retries.

    On RequestTimeoutError or NetworkError the call is repeated after a pause
    until ``_retry_params()[1]`` attempts are spent; *retry_callback*, when
    given, receives {"attempt", "attempts", "delay", "error"} before each
    scheduled retry. The budget is re-read every iteration, and the LAST
    exception is re-raised with an extra ``attempts`` attribute.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except (RequestTimeoutError, NetworkError) as exc:
            delay, total = _retry_params()
            if attempt >= total:
                # The attribute is attached to the exception class once, so
                # type checkers see it declared on every subclass.
                exc.attempts = attempt  # type: ignore[attr-defined]
                raise
            if retry_callback is not None:
                try:
                    retry_callback({
                        "attempt": attempt,
                        "attempts": total,
                        "delay": delay,
                        "error": str(exc),
                    })
                except Exception:
                    pass  # UI callbacks must never break the retry flow
            if delay > 0:
                time.sleep(delay)


def _extract_error_body(r) -> str:
    try:
        err_body = r.json()
        error = err_body.get("error", "")
        if isinstance(error, dict):
            return (
                error.get("message", "")
                or err_body.get("message", "")
                or r.text[:300]
            )
        if isinstance(error, str) and error.strip():
            return error
        return (
            err_body.get("message", "")
            or err_body.get("detail", "")
            or r.text[:300]
        )
    except Exception:
        return r.text[:300]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
