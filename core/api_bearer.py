"""
core.api_bearer - the OpenAI-compatible chat/completions transport.

Covers every provider that exposes the classic ``/chat/completions`` shape
with a bearer token. The request carries the conversation as-is, attaches
native function tools and an optional ``response_format`` for structured
output, and reports token usage through a callback when the caller wants it.
"""

from typing import Optional

import requests

from core.api_errors import ProviderHTTPError
from core.api_http import _VERIFY_TLS, MODEL_RESPONSE_TIMEOUT
from core.api_payload import (
    _estimate_tokens_in,
    _normalise_tools,
    _openai_response_format,
)
from core.api_responses import (
    _prepare_response_content,
    _unwrap_json_text,
)


def bearer_request_impl(url: str, api_key: str, model: str, messages: list, temperature: float,
                        *, tools: Optional[list] = None, usage_callback=None,
                        max_tokens: Optional[int] = None, service: str = "", json_schema=None) -> str:
    """Send a standard Bearer-token chat completion request.

    *tools*: optional list of tool keys (str) or tool dicts to include in the payload.
    *max_tokens*: optional maximum number of output tokens.
    *json_schema*: optional JSON Schema (dict) or {name, schema} envelope.
        When present, rendered as the OpenAI ``response_format`` json_schema
        block and the response is expected (and unwrapped) as JSON text.
    *usage_callback*: callable({"in": N, "out": M, "cache": C}) for token tracking.
        "cache" is read from ``usage.prompt_tokens_details.cached_tokens`` or,
        on DeepSeek's OpenAI-compatible chat route, from
        ``usage.prompt_cache_hit_tokens``; 0 when the provider reports none.
    *service*: optional service name for error reporting.

    Raises ProviderHTTPError on non-200 responses.
    Returns the extracted text on success.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload: dict = {
        "model":       model,
        "messages":    messages,
        "temperature": float(temperature),
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = _normalise_tools(tools)
    json_format = _openai_response_format(json_schema) if json_schema else None
    if json_format:
        payload["response_format"] = json_format
    r = requests.post(
        url, headers=headers, json=payload,
        timeout=MODEL_RESPONSE_TIMEOUT, verify=_VERIFY_TLS,
    )
    if r.status_code != 200:
        try:
            err_json = r.json()
            body = err_json.get("error", {}).get("message") or err_json.get("message") or str(err_json)
        except Exception:
            body = r.text[:300]
        raise ProviderHTTPError(r.status_code, body, service=service)

    response_body = r.json()
    message = response_body["choices"][0]["message"]
    result_text = _prepare_response_content(message)
    if json_format and result_text.strip():
        result_text = _unwrap_json_text(result_text)

    # ── Estimate token usage ────────────────────────────────────────────────
    if usage_callback:
        usage = response_body.get("usage")
        cache = 0
        if usage and isinstance(usage, dict):
            tokens_in = usage.get("prompt_tokens") or 0
            tokens_out = usage.get("completion_tokens") or 0
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict):
                cache = details.get("cached_tokens") or 0
            if not cache:
                # DeepSeek's OpenAI-compatible chat route reports the cached
                # prompt share as a dedicated usage field (a subset of
                # prompt_tokens), not inside prompt_tokens_details.
                cache = usage.get("prompt_cache_hit_tokens") or 0
        else:
            tokens_in = _estimate_tokens_in(messages)
            from core.files import estimate_tokens
            tokens_out = estimate_tokens(result_text)
        usage_callback({"in": int(tokens_in), "out": int(tokens_out), "cache": int(cache)})

    return result_text

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
