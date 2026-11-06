"""
core.api_deepseek - the DeepSeek transport.

DeepSeek exposes two compatible surfaces and this module drives both: the
OpenAI-shaped Responses API for regular completions, and an
Anthropic-compatible Messages endpoint for web search. The /responses route
accepts a built-in ``web_search`` tool but the provider silently ignores it,
so a search has to go through the Anthropic surface where the
``web_search_20250305`` server tool actually executes. Search results are only
trusted when the response carries the corresponding server-tool blocks;
otherwise the call fails instead of answering from model memory.
"""
from typing import Optional

import requests

from core.api_errors import NetworkError, ProviderHTTPError, RequestTimeoutError
from core.api_http import (
    _VERIFY_TLS,
    MODEL_REQUEST_TIMEOUT,
    MODEL_RESPONSE_TIMEOUT,
    _extract_error_body,
)
from core.api_payload import (
    _content_has_material,
    _estimate_tokens_in,
    _normalise_tools,
    _responses_json_format,
)
from core.api_responses import (
    _extract_deepseek_responses_text,
    _raise_responses_failure,
    _responses_incomplete_details,
    _unwrap_json_text,
)

# Valid reasoning-effort values accepted by DeepSeek's Responses API.
_DEEPSEEK_REASONING_EFFORTS = {
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
}


# Anthropic-version header value accepted by DeepSeek's Anthropic-compatible
# Messages API (https://api.deepseek.com/anthropic/v1/messages).
_ANTHROPIC_VERSION = "2023-06-01"
# Anthropic-format server tool type that actually executes the search on the
# DeepSeek side. On the OpenAI-compatible /responses route the built-in
# web_search tool is silently ignored, so web_search for DeepSeek must go
# through this endpoint.
_DEEPSEEK_SERVER_WEB_SEARCH_TOOL = "web_search_20250305"
_WEB_SEARCH_TOOL_NAME = "web_search"
# max_uses for the server tool depending on the user-selected search depth.
_MAX_USES_BY_CONTEXT = {"low": 1, "medium": 2, "high": 3}


def _deepseek_reasoning_effort(cfg: dict, svc_name: str,
                               effort: Optional[str] = None) -> str:
    """Resolve the reasoning effort for a DeepSeek service.

    *effort* (per-request value from the assistant/orchestrator) takes
    precedence over the persisted provider config
    ``<svc_name>_reasoning_effort`` (exposed by the provider's "Reasoning
    effort" select). ``none`` disables thinking; the other values enable it
    with increasing depth. Defaults to ``max``.
    """
    raw = effort if effort is not None else cfg.get(f"{svc_name}_reasoning_effort", "max")
    val = str(raw or "").strip().lower()
    if val not in _DEEPSEEK_REASONING_EFFORTS:
        val = "max"
    return val


def _deepseek_responses_request(base_url: str, api_key: str, model: str, sys_text: str,
                                hist_msgs: list, user_content: str, *, max_tokens: Optional[int] = None,
                                tools_list: Optional[list] = None, max_tool_calls=None,
                                reasoning_effort: Optional[str] = None, cfg: Optional[dict] = None,
                                svc_name: str = "", usage_callback=None, incomplete_callback=None,
                                tool_choice=None, json_schema=None) -> str:
    """Send a DeepSeek Responses API request (POST /responses).

    The Responses API is stateless: the full conversation is passed as a list
    of ``{role, content}`` items under ``input``. The system prompt is passed
    via ``instructions``. Reasoning is controlled with ``reasoning.effort``
    and the maximum output length with ``max_output_tokens``.

    *tool_choice*: optional value for the ``tool_choice`` request parameter.
        When set (e.g. ``{"type": "web_search"}``) the provider is forced to
        call that tool. When omitted the model decides automatically.

    *usage_callback*: callable({"in": N, "out": M, "cache": C}) for token
        tracking. C is read from ``usage.input_tokens_details.cached_tokens``
        (tokens served from the provider's context cache).
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    input_items: list = []
    for m in hist_msgs or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if not _content_has_material(content):
            continue
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system", "developer"):
            role = "user"
        input_items.append({"role": role, "content": content})
    if user_content and str(user_content).strip():
        input_items.append({"role": "user", "content": user_content})

    payload: dict = {
        "model":  model,
        "input":  input_items,
        "stream": False,
    }
    if sys_text and str(sys_text).strip():
        payload["instructions"] = str(sys_text)
    if max_tokens:
        payload["max_output_tokens"] = int(max_tokens)

    json_format = _responses_json_format(json_schema) if json_schema else None
    if json_format:
        payload["text"] = {"format": json_format}

    effort = _deepseek_reasoning_effort(cfg or {}, svc_name, reasoning_effort)
    if effort:
        payload["reasoning"] = {"effort": effort}
    if tools_list:
        payload["tools"] = _normalise_tools(tools_list)
    if max_tool_calls is not None:
        payload["max_tool_calls"] = int(max_tool_calls)
    if tool_choice:
        payload["tool_choice"] = tool_choice

    r = requests.post(
        base_url, headers=headers, json=payload,
        timeout=MODEL_RESPONSE_TIMEOUT, verify=_VERIFY_TLS,
    )
    if r.status_code != 200:
        body = _extract_error_body(r)
        raise ProviderHTTPError(r.status_code, body, service=svc_name)

    data = r.json()
    _raise_responses_failure(data, svc_name)
    incomplete = _responses_incomplete_details(data)
    if incomplete and incomplete_callback:
        incomplete_callback(incomplete)
    result_text = _extract_deepseek_responses_text(data)
    if json_format and result_text.strip():
        result_text = _unwrap_json_text(result_text)

    if usage_callback:
        usage = data.get("usage")
        cache = 0
        if usage and isinstance(usage, dict):
            tokens_in = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
            tokens_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
            details = usage.get("input_tokens_details")
            if isinstance(details, dict):
                cache = details.get("cached_tokens") or 0
        else:
            tokens_in = _estimate_tokens_in(input_items)
            from core.files import estimate_tokens
            tokens_out = estimate_tokens(result_text)
        usage_callback({"in": int(tokens_in), "out": int(tokens_out), "cache": int(cache)})

    return result_text


def _anthropic_web_search_used(data: dict) -> bool:
    """True when an Anthropic Messages response shows real search activity.

    The server marks an executed search with ``server_tool_use`` blocks and
    returns the found snippets as ``web_search_tool_result`` blocks. Their
    presence is the only reliable machine-readable trace that the provider
    actually searched the web.
    """
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("server_tool_use", "web_search_tool_result"):
            return True
    return False


def _extract_anthropic_text(data: dict) -> str:
    """Extract the final answer text from an Anthropic Messages response.

    ``thinking`` and server tool blocks are internal and are never returned.
    When the response contains search-tool activity, only text blocks AFTER
    the last tool block are considered; earlier blocks ("I'll search the
    web...") are announcements and are dropped. The LAST non-empty text
    block of that tail is the model's final answer.
    """
    blocks = [b for b in (data.get("content") or []) if isinstance(b, dict)]
    start_index = 0
    for i, block in enumerate(blocks):
        if block.get("type") in ("server_tool_use", "web_search_tool_result"):
            start_index = i + 1
    parts = []
    for block in blocks[start_index:]:
        if block.get("type") != "text":
            continue
        txt = str(block.get("text") or "").strip()
        if txt:
            parts.append(txt)
    return parts[-1] if parts else ""


def _deepseek_anthropic_web_search(
        base_url: str, api_key: str, model: str,
        system_prompt: str, query: str,
        *, allowed_domains: Optional[list] = None,
        search_context_size: Optional[str] = None,
        max_tokens: int = 4096, svc_name: str = "",
        timeout: tuple = MODEL_REQUEST_TIMEOUT) -> dict:
    """Run a server-side web search through DeepSeek's Anthropic-compatible API.

    DeepSeek ignores built-in web_search tools on the OpenAI-compatible
    /responses route (HTTP 200, no search executed), so the search must go
    through the Anthropic-compatible Messages endpoint with the
    ``web_search_20250305`` server tool, which the provider actually
    executes on its side.

    The request is sent WITHOUT ``tool_choice``: forcing tool selection on
    DeepSeek's thinking models fails with HTTP 400. Instead the system prompt
    enforces one search followed by the final answer.

    *allowed_domains* are passed to the model as a prompt instruction (the
    Anthropic server tool format has no native domain filter).

    The fact of the search is verified from the response itself. When the
    response contains no ``server_tool_use``/``web_search_tool_result``
    blocks, the request is retried once with a reinforced instruction; if the
    second attempt again shows no search activity, an explicit error is
    returned instead of an answer that may come purely from model memory.

    Returns:
        {"ok": True, "text": <final answer>} - search confirmed and answer
        present, OR {"ok": False, "error": <message>} when the provider did
        not execute the search.

    Raises ProviderHTTPError / RequestTimeoutError / NetworkError for
    transport-level failures (same APIError contract as send_request).
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    ctx_key = str(search_context_size or "medium").strip().lower()
    if ctx_key not in _MAX_USES_BY_CONTEXT:
        ctx_key = "medium"
    max_uses = _MAX_USES_BY_CONTEXT[ctx_key]

    system_text = str(system_prompt or "").strip()
    if allowed_domains:
        domains = ", ".join(
            str(d).strip() for d in allowed_domains if str(d).strip()
        )
        if domains:
            system_text = (
                f"{system_text}\n\n"
                f"Allowed domains for the search: {domains}."
            )

    base_payload = {
        "model": model,
        "max_tokens": int(max_tokens),
        "tools": [{
            "type": _DEEPSEEK_SERVER_WEB_SEARCH_TOOL,
            "name": _WEB_SEARCH_TOOL_NAME,
            "max_uses": int(max_uses),
        }],
    }
    if system_text:
        base_payload["system"] = system_text

    user_texts = [
        str(query).strip(),
        (
            f"{str(query).strip()}\n\n"
            "System note: you MUST use the web_search tool right now and "
            "answer only from its results. Do not answer from memory."
        ),
    ]

    for content in user_texts:
        payload = dict(base_payload)
        payload["messages"] = [{"role": "user", "content": content}]
        try:
            r = requests.post(
                base_url, headers=headers, json=payload,
                timeout=timeout, verify=_VERIFY_TLS,
            )
        except requests.exceptions.Timeout:
            raise RequestTimeoutError(service=svc_name)
        except requests.exceptions.RequestException as e:
            raise NetworkError(str(e), service=svc_name)
        if r.status_code != 200:
            body = _extract_error_body(r)
            raise ProviderHTTPError(r.status_code, body, service=svc_name)
        data = r.json()
        search_ran = _anthropic_web_search_used(data)
        if search_ran:
            text = _extract_anthropic_text(data)
            return {"ok": True, "text": text}

    return {
        "ok": False,
        "error": (
            "DeepSeek did not execute the web search: the response contains "
            "no search blocks. The provider may have temporarily disabled "
            "server-side search - retry later or rephrase the query."
        ),
    }

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
