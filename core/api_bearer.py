"""
core.api_bearer - the OpenAI-compatible chat/completions transport.

Covers every provider that exposes the classic ``/chat/completions`` shape
with a bearer token. The request carries the conversation as-is, attaches
native function tools and an optional ``response_format`` for structured
output, and reports token usage through a callback when the caller wants it.

The body is assembled by :func:`core.api_chat.build_chat_payload`, which the
function-calling loop uses as well, so both paths send the same shape.
"""

from typing import Optional

import requests

from core.api_chat import build_chat_payload, report_chat_usage
from core.api_errors import ProviderHTTPError
from core.api_http import _VERIFY_TLS, MODEL_RESPONSE_TIMEOUT
from core.api_responses import _prepare_response_content, _unwrap_json_text


def bearer_request_impl(url: str, api_key: str, model: str, messages: list, temperature: float,
                        *, tools: Optional[list] = None, usage_callback=None,
                        max_tokens: Optional[int] = None, service: str = "", json_schema=None,
                        extra_payload: Optional[dict] = None,
                        extra_headers: Optional[dict] = None,
                        single_system: bool = False) -> str:
    """Send a standard Bearer-token chat completion request.

    *tools*: optional list of tool keys (str) or tool dicts to include in the payload.
    *max_tokens*: optional maximum number of output tokens.
    *json_schema*: optional JSON Schema (dict) or {name, schema} envelope.
        When present, rendered as the OpenAI ``response_format`` json_schema
        block and the response is expected (and unwrapped) as JSON text.
    *usage_callback*: callable({"in": N, "out": M, "cache": C}) for token tracking.
    *service*: optional service name for error reporting.
    *extra_payload*: optional provider-specific fields declared by the service
        catalog; merged into the request body after the platform's own keys.
    *extra_headers*: optional provider-specific HTTP headers declared by the
        service catalog (a per-conversation cache identifier and the like).

    Raises ProviderHTTPError on non-200 responses.
    Returns the extracted text on success.
    """
    payload, json_format = build_chat_payload(
        model, messages, temperature,
        max_tokens=max_tokens, tools=tools, json_schema=json_schema,
        extra_payload=extra_payload,
        single_system=single_system,
    )
    request_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    # Provider-declared headers come last: a catalog may add a cache-binding
    # identifier, but it must never be able to override the credentials.
    for name, value in (extra_headers or {}).items():
        if name not in ("Authorization", "Content-Type"):
            request_headers[name] = value
    r = requests.post(
        url, json=payload, timeout=MODEL_RESPONSE_TIMEOUT, verify=_VERIFY_TLS,
        headers=request_headers,
    )
    if r.status_code != 200:
        try:
            err_json = r.json()
            body = err_json.get("error", {}).get("message") or err_json.get("message") or str(err_json)
        except Exception:
            body = r.text[:300]
        raise ProviderHTTPError(r.status_code, body, service=service)

    response_body = r.json()
    result_text = _prepare_response_content(response_body["choices"][0]["message"])
    if json_format and result_text.strip():
        result_text = _unwrap_json_text(result_text)
    report_chat_usage(usage_callback, response_body, messages, result_text)
    return result_text

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
