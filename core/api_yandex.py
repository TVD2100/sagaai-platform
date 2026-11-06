"""
core.api_yandex - the Yandex AI Studio transport.

Yandex speaks the OpenAI Responses API with a few local conventions: the model
is addressed as ``gpt://<folder_id>/<model>``, the system prompt travels in
``instructions`` and web_search tools are enriched with a context size and an
allowed-domain filter. Reasoning effort is normalised to the values the
service accepts before it reaches the payload.
"""
from typing import Optional

import requests

from core.api_errors import ProviderHTTPError
from core.api_http import _VERIFY_TLS, MODEL_RESPONSE_TIMEOUT, _extract_error_body
from core.api_payload import (
    _DEFAULT_MAX_TOKENS,
    _content_has_material,
    _estimate_tokens_in,
    _normalise_tools,
    _responses_json_format,
)
from core.api_responses import (
    _extract_responses_text,
    _raise_responses_failure,
    _responses_incomplete_details,
    _unwrap_json_text,
)

# Valid reasoning-effort values accepted by the Yandex Responses API.
_YANDEX_REASONING_EFFORTS = {
    "none", "minimal", "low", "medium", "high", "xhigh",
}


def yandex_reasoning_effort(cfg: dict, svc_name: str, effort: Optional[str] = None,
                             model: str = "", svc: Optional[dict] = None) -> str:
    """Resolve the reasoning effort for a Yandex AI Studio service.

    *effort* (per-request value from the assistant/orchestrator) takes
    precedence over the persisted provider config
    ``<svc_name>_reasoning_effort`` (exposed by the "Reasoning effort"
    select, if defined in services/*.json). Returns "" when not configured
    or invalid, so the ``reasoning`` block is omitted and the model default
    applies.

    When *model* is known and the service declares per-model
    ``reasoning_effort_options`` (services/*.json), the resolved value is
    additionally constrained to the options supported by that model.
    """
    raw = effort if effort is not None else cfg.get(f"{svc_name}_reasoning_effort", "")
    val = str(raw or "").strip().lower()
    if val not in _YANDEX_REASONING_EFFORTS:
        return ""
    if model and svc:
        from core.services import get_model_reasoning_effort_options
        model_opts = get_model_reasoning_effort_options(svc, model)
        if model_opts and val not in model_opts:
            return ""
    return val


def yandex_web_search_config(cfg: dict, svc_name: str) -> tuple:
    """Return (context_size, allowed_domains) for Yandex web_search tools.

    Both values come from the persisted provider config and are applied to
    every ``web_search`` tool entry before the request is sent.
    """
    context_size = str(
        cfg.get(f"{svc_name}_web_search_context_size", "medium") or "medium"
    ).strip().lower()
    if context_size not in ("low", "medium", "high"):
        context_size = "medium"

    domains_raw = cfg.get(f"{svc_name}_web_search_allowed_domains", "") or ""
    allowed: list = []
    if isinstance(domains_raw, str):
        for part in domains_raw.replace(",", " ").split():
            part = part.strip()
            if part and part not in allowed:
                allowed.append(part)
    else:
        # Fallback: allow list-like values (old persisted arrays).
        try:
            for part in domains_raw:
                if isinstance(part, str) and part.strip() and part.strip() not in allowed:
                    allowed.append(part.strip())
        except TypeError:
            pass
    return context_size, allowed


def yandex_responses_request_impl(base_url: str, api_key: str, folder_id: str, model: str,
                              sys_text: str, hist_msgs: list, user_content: str, *, temperature: float,
                              max_tokens: Optional[int] = None, tools_list: Optional[list] = None,
                              max_tool_calls=None, reasoning_effort: Optional[str] = None,
                              cfg: Optional[dict] = None, svc_name: str = "", usage_callback=None,
                              incomplete_callback=None, tool_choice=None, svc: Optional[dict] = None,
                              assistant: Optional[dict] = None, json_schema=None,
                              assistant_web_search_config=None) -> str:
    """Send a Yandex Responses API request (POST {base}/responses).

    Follows the official Yandex AI Studio Responses API contract:
    - model URI: ``gpt://<folder_id>/<model>`` (no ``/latest`` suffix);
    - conversation items passed under ``input`` as ``{role, content}``;
    - system prompt passed via ``instructions``;
    - optional ``reasoning.effort`` (when configured for the service);
    - web_search tools enriched with ``search_context_size`` and
      ``filters.allowed_domains`` from the provider config;
    - optional ``tools`` and ``max_tool_calls``;
    - optional ``tool_choice`` to force a specific tool (e.g.
      ``{"type": "web_search"}``);
    - token usage read from ``usage.input_tokens`` / ``usage.output_tokens``,
      with cached tokens from ``usage.input_tokens_details.cached_tokens``.
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

    model_uri = f"gpt://{folder_id}/{model}"
    payload: dict = {
        "model":             model_uri,
        "input":             input_items,
        "temperature":       float(temperature),
        "max_output_tokens": int(max_tokens) if max_tokens else _DEFAULT_MAX_TOKENS,
        "stream":            False,
    }
    if sys_text and str(sys_text).strip():
        payload["instructions"] = str(sys_text)

    json_format = _responses_json_format(json_schema) if json_schema else None
    if json_format:
        payload["text"] = {"format": json_format}

    effort = yandex_reasoning_effort(cfg or {}, svc_name, reasoning_effort,
                                      model=model, svc=svc)
    if effort:
        payload["reasoning"] = {"effort": effort}

    ctx_size_fn = assistant_web_search_config or (
        lambda _a, _c, _s: yandex_web_search_config(_c or {}, _s)
    )
    ctx_size, allowed_domains = ctx_size_fn(assistant, cfg or {}, svc_name)
    if tools_list:
        normalised = _normalise_tools(tools_list)
        enriched = []
        for tool in normalised:
            if tool.get("type") == "web_search":
                tool = dict(tool)
                tool.setdefault("search_context_size", ctx_size)
                if allowed_domains:
                    filters = dict(tool.get("filters", {}) or {})
                    filters["allowed_domains"] = allowed_domains
                    tool["filters"] = filters
            enriched.append(tool)
        payload["tools"] = enriched

    if max_tool_calls is not None:
        payload["max_tool_calls"] = int(max_tool_calls)
    if tool_choice:
        payload["tool_choice"] = tool_choice

    r = requests.post(
        f"{base_url}/responses", headers=headers, json=payload,
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
    result_text = _extract_responses_text(data)
    if json_format and result_text.strip():
        result_text = _unwrap_json_text(result_text)

    if usage_callback:
        usage = data.get("usage")
        cache = 0
        if usage and isinstance(usage, dict):
            tokens_in = usage.get("input_tokens") or 0
            tokens_out = usage.get("output_tokens") or 0
            details = usage.get("input_tokens_details")
            if isinstance(details, dict):
                cache = details.get("cached_tokens") or 0
        else:
            tokens_in = _estimate_tokens_in(input_items)
            from core.files import estimate_tokens
            tokens_out = estimate_tokens(result_text)
        usage_callback({"in": int(tokens_in), "out": int(tokens_out), "cache": int(cache)})

    return result_text

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
