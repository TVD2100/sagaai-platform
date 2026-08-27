# -*- coding: utf-8 -*-
"""
core.assistant_tools - local execution of function calls for assistant chat.

Implements a Responses-API function-calling loop for assistant chat:

1. send a request with native function tools (e.g. ``rag_search``);
2. when the model returns ``function_call`` items, execute them locally;
3. append ``function_call_output`` items to the input and re-send;
4. return the final text after the model stops calling tools.

The loop is currently wired for the Yandex Responses API (auth_type
``yandex_iam``). Access control for ``rag_search`` is based on the
assistant's folder manifest ``rag_bases`` list.

No streamlit imports.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import requests

from core.rag_search import search_base

RAG_SEARCH_TOOL_NAME = "rag_search"
_DEFAULT_MAX_TOOL_ITERATIONS = 3
_MAX_TOOL_ITERATIONS = 10


def _yandex_reasoning_effort(cfg: dict, svc_name: str, effort: str = None,
                             model: str = "", svc: dict = None) -> str:
    """Resolve reasoning effort via core.api_layer (lazy import)."""
    from core.api_layer import _yandex_reasoning_effort as _f
    return _f(cfg or {}, svc_name, effort, model=model, svc=svc)


def _normalise_tools(tools_list):
    """Normalise tools via core.api_layer (lazy import)."""
    from core.api_layer import _normalise_tools
    return _normalise_tools(tools_list)


def _yandex_web_search_config(cfg: dict, svc_name: str, assistant=None):
    """Return (context_size, allowed_domains) via core.api_layer.

    Per-assistant overrides from the assistant's manifest take priority over
    provider-level persisted config.
    """
    from core.api_layer import _yandex_web_search_config as _base
    from core.api_layer import _assistant_web_search_config as _with_assistant
    if assistant:
        return _with_assistant(assistant, cfg, svc_name)
    return _base(cfg, svc_name)


def _build_responses_input_items(hist_msgs: list, user_content: str,
                                 output_additions: list = None) -> list:
    """Build the ``input`` array for a stateless /responses request.

    Plain ``{role, content}`` messages are app-ended first, then the current
    user message, then any native items (function_call / function_call_output)
    added by the loop.
    """
    input_items: List[Dict[str, Any]] = []
    for m in hist_msgs or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if content is None:
            continue
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system", "developer"):
            role = "user"
        input_items.append({"role": role, "content": content})
    if user_content and str(user_content).strip():
        input_items.append({"role": "user", "content": user_content})
    for item in output_additions or []:
        if isinstance(item, dict):
            input_items.append(item)
    return input_items


def _build_yandex_tool_payload(base_url: str, api_key: str, folder_id: str,
                               model: str, sys_text: str, input_items: list,
                               *, temperature: float, max_tokens: int = None,
                               tools_list: list = None, max_tool_calls=None,
                               reasoning_effort: str = None, cfg: dict = None,
                               svc_name: str = "", tool_choice=None,
                               svc: dict = None, assistant: dict = None) -> dict:
    """Build the Yandex /responses payload for the tool loop.

    Mirrors the payload construction in ``_yandex_responses_request`` and
    additionally accepts native ``input`` items (function_call/
    function_call_output).
    """
    from core.api_layer import _DEFAULT_MAX_TOKENS

    model_uri = f"gpt://{folder_id}/{model}"
    payload: dict = {
        "model": model_uri,
        "input": input_items,
        "temperature": float(temperature),
        "max_output_tokens": int(max_tokens) if max_tokens else _DEFAULT_MAX_TOKENS,
        "stream": False,
    }
    if sys_text and str(sys_text).strip():
        payload["instructions"] = str(sys_text)

    effort = _yandex_reasoning_effort(cfg or {}, svc_name, reasoning_effort,
                                      model=model, svc=svc)
    if effort:
        payload["reasoning"] = {"effort": effort}

    ctx_size, allowed_domains = _yandex_web_search_config(
        cfg or {}, svc_name, assistant=assistant
    )
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
    return payload


def _post_yandex_responses(base_url: str, api_key: str, payload: dict,
                           svc_name: str):
    """POST the payload to /responses and return the decoded JSON body."""
    from core.api_layer import MODEL_RESPONSE_TIMEOUT, _VERIFY_TLS, _extract_error_body
    from core.api_errors import ProviderHTTPError

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = requests.post(
        f"{base_url}/responses", headers=headers, json=payload,
        timeout=MODEL_RESPONSE_TIMEOUT, verify=_VERIFY_TLS,
    )
    if r.status_code != 200:
        body = _extract_error_body(r)
        raise ProviderHTTPError(r.status_code, body, service=svc_name)
    return r.json()


def _extract_function_calls(data: dict) -> list:
    """Return native ``function_call`` items as {"call_id", "name", "arguments"}."""
    output = data.get("output", [])
    calls = []
    if not isinstance(output, list):
        return calls
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call":
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        try:
            arguments = json.loads(item.get("arguments", "{}"))
            if not isinstance(arguments, dict):
                arguments = {}
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        calls.append({
            "call_id": str(item.get("call_id") or ""),
            "name": name,
            "arguments": arguments,
        })
    return calls


def _item_text(item: dict) -> str:
    """Extract readable text from a single Responses output item."""
    from core.api_layer import _extract_responses_text
    return _extract_responses_text({"output": [item]})


def _assistant_allowed_rag_bases(assistant: dict) -> set:
    """Return the set of RAG base slugs bound to the assistant's manifest.

    The binding lives in DATA_DIR/assistants/<slug>/manifest.json under the
    ``rag_bases`` key. An EMPTY set means the assistant may not call
    ``rag_search`` at all: only bases explicitly bound to the assistant's
    folder manifest are allowed.
    """
    allowed = set()
    if not assistant:
        return allowed
    try:
        from core.assistant_folders import load_assistant_bundle
        slug = str(assistant.get("slug") or "")
        bundle = load_assistant_bundle(slug) if slug else None
        if isinstance(bundle, dict):
            for b in bundle.get("rag_bases") or []:
                val = str(b or "").strip().lower()
                if val:
                    allowed.add(val)
    except Exception:
        pass
    return allowed


def execute_assistant_rag_search(args, assistant=None) -> str:
    """Execute a local rag_search call and return the output as a JSON string.

    The model invokes this through the Responses ``function_call`` mechanism;
    the platform runs the semantic search locally and feeds the result back as
    ``function_call_output``.
    """
    if not isinstance(args, dict):
        args = {}
    slug = str(args.get("slug") or "").strip().lower()
    query = str(args.get("query") or "").strip()
    if not slug or not query:
        return json.dumps(
            {"ok": False, "error": "Missing required argument 'slug' / 'query'."},
            ensure_ascii=False,
        )
    allowed = _assistant_allowed_rag_bases(assistant)
    if not allowed or slug not in allowed:
        return json.dumps(
            {
                "ok": False,
                "error": f"Access denied: RAG base '{slug}' is not assigned to this assistant.",
            },
            ensure_ascii=False,
        )
    try:
        from core.rag_search import build_search_context
        try:
            top_k = int(args.get("top_k") or 5)
        except (TypeError, ValueError):
            top_k = 5
        try:
            min_score = float(args.get("min_score") or 0.0)
        except (TypeError, ValueError):
            min_score = 0.0
        hits = search_base(slug, query, top_k=max(1, top_k), min_score=min_score)
        ctx = build_search_context(hits, max_chars=4000)
        return json.dumps(
            {
                "ok": True,
                "slug": slug,
                "query": query,
                "count": len(hits),
                "text": ctx,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def _report_usage(usage_callback, data: dict, input_items: list, result_text: str) -> None:
    """Report token usage for one request when a callback is provided."""
    if not usage_callback:
        return
    from core.api_layer import _estimate_tokens_in
    from core.files import estimate_tokens

    usage = data.get("usage")
    cache = 0
    if usage and isinstance(usage, dict):
        tokens_in = usage.get("input_tokens") or 0
        tokens_out = usage.get("output_tokens") or 0
        details = usage.get("input_tokens_details")
        if isinstance(details, dict):
            cache = details.get("cached_tokens") or 0
    else:
        tokens_in = _estimate_tokens_in([m for m in input_items if isinstance(m, dict)])
        tokens_out = estimate_tokens(result_text)
    usage_callback({"in": int(tokens_in), "out": int(tokens_out), "cache": int(cache)})


def run_yandex_responses_tool_loop(
    base_url: str, api_key: str, folder_id: str, model: str,
    sys_text: str, hist_msgs: list, user_content: str,
    *, temperature: float, max_tokens: int = None,
    tools_list: list = None, max_tool_calls=None,
    reasoning_effort: str = None, cfg: dict = None,
    svc_name: str = "", usage_callback=None,
    tool_choice=None, svc: dict = None, assistant: dict = None,
    on_tool_call: Optional[Callable[[dict], None]] = None,
) -> str:
    """Run a Yandex Responses conversation with a native function-call loop.

    The model decides which tools to invoke (``rag_search`` and/or
    ``web_search``). Every ``rag_search`` call is executed locally through
    ``core.rag_search.search_base``; the result is returned to the model as a
    ``function_call_output`` item. The loop ends when the model produces a
    plain message or the iteration limit is reached.

    Returns the final assistant text.
    """
    from core.api_layer import _extract_responses_text

    max_requests = int(max_tool_calls or _DEFAULT_MAX_TOOL_ITERATIONS)
    max_requests = max(1, min(max_requests, _MAX_TOOL_ITERATIONS))
    # +1 for the final non-tool request after the last tool result.
    max_requests += 1

    input_items = _build_responses_input_items(hist_msgs, user_content)
    last_text = ""

    for _ in range(max_requests):
        payload = _build_yandex_tool_payload(
            base_url, api_key, folder_id, model, sys_text, input_items,
            temperature=temperature, max_tokens=max_tokens,
            tools_list=tools_list, max_tool_calls=max_tool_calls,
            reasoning_effort=reasoning_effort, cfg=cfg, svc_name=svc_name,
            tool_choice=tool_choice, svc=svc, assistant=assistant,
        )
        try:
            data = _post_yandex_responses(base_url, api_key, payload, svc_name)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            has_native_output = any(
                isinstance(i, dict) and i.get("type") == "function_call_output"
                for i in input_items
            )
            if status != 400 or not has_native_output:
                raise
            # Fallback: the provider rejected native function items; retry with
            # a textual representation of the tool results.
            textual: List[Dict[str, Any]] = []
            for item in input_items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in ("function_call", "function_call_output"):
                    continue
                textual.append(item)
            for item in input_items:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    textual.append({
                        "role": "user",
                        "content": (
                            f"Результат вызова функции ({item.get('call_id', '')}):\n"
                            f"{item.get('output', '')}"
                        ),
                    })
            input_items = textual
            payload = _build_yandex_tool_payload(
                base_url, api_key, folder_id, model, sys_text, input_items,
                temperature=temperature, max_tokens=max_tokens,
                tools_list=tools_list, max_tool_calls=max_tool_calls,
                reasoning_effort=reasoning_effort, cfg=cfg, svc_name=svc_name,
                tool_choice=tool_choice, svc=svc, assistant=assistant,
            )
            data = _post_yandex_responses(base_url, api_key, payload, svc_name)

        result_text = _extract_responses_text(data)
        # Collect real assistant text only (ignore function_call fenced JSON).
        msg_parts = []
        for item in data.get("output", []):
            if isinstance(item, dict) and item.get("type") == "message":
                t = _item_text(item)
                if t.strip():
                    msg_parts.append(t.strip())
        if msg_parts:
            last_text = "\n\n".join(msg_parts)
        _report_usage(usage_callback, data, input_items, result_text)

        calls = _extract_function_calls(data)
        if not calls:
            break

        # Preserve the model's turn in the conversation for the next request.
        for item in data.get("output", []):
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                text = _item_text(item)
                if text.strip():
                    input_items.append({
                        "role": item.get("role", "assistant"),
                        "content": text,
                    })
            elif itype == "function_call":
                input_items.append({
                    "type": "function_call",
                    "call_id": item.get("call_id", ""),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                })

        for call in calls:
            name = call.get("name", "")
            if name == RAG_SEARCH_TOOL_NAME:
                output = execute_assistant_rag_search(
                    call.get("arguments", {}), assistant=assistant
                )
            else:
                output = json.dumps(
                    {"ok": False, "error": f"Unknown function tool: {name}"},
                    ensure_ascii=False,
                )
            if on_tool_call:
                try:
                    on_tool_call({
                        "name": name,
                        "arguments": call.get("arguments", {}),
                        "output": output,
                    })
                except Exception:
                    pass
            input_items.append({
                "type": "function_call_output",
                "call_id": call.get("call_id", ""),
                "output": output,
            })

    return last_text
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
