"""
core.api_chat - request shaping for OpenAI-compatible chat/completions calls.

Two places send the same body shape: the single-request transport
(:mod:`core.api_bearer`) and the function-calling loop
(:func:`core.assistant_tools.run_bearer_tool_loop`). Both need the same
tools conversion and the same treatment of provider-specific fields, so the
assembly lives here and neither caller duplicates it.

No streamlit imports.
"""

from typing import Optional

#: Request keys the platform owns. A service catalog may declare extra provider
#: fields, but it must never replace these: the model, the conversation and the
#: tools come from the call itself.
_RESERVED_PAYLOAD_KEYS = (
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "response_format", "input", "instructions",
)


def _merge_extra_payload(payload: dict, extra: Optional[dict]) -> None:
    """Merge provider-specific fields into a request body in place.

    A service catalog may declare fields the shared transport does not model
    (for example a provider-specific reasoning switch). Such a declaration
    must never replace the platform's own request keys - overriding them would
    corrupt every request the service sends.
    """
    if not extra:
        return
    for key, value in extra.items():
        if key in _RESERVED_PAYLOAD_KEYS:
            continue
        payload[key] = value


def _to_chat_tools(tools: Optional[list]) -> list:
    """Convert a tools list to the chat/completions tool shape.

    The platform stores native function tools in the Responses API shape, with
    the function fields at the top level of the tool object. An
    OpenAI-compatible chat/completions gateway expects the same tool wrapped
    one level deeper, under a ``function`` key. A gateway that receives the
    flat shape rejects the request outright or silently treats the tool as
    absent - the model then imitates a call in plain text instead of emitting
    a real one.

    Dicts that are not function tools (server-side tools such as
    ``{"type": "web_search"}``) and already-nested function tools pass through
    unchanged, so the conversion is idempotent. Strings become
    ``{"type": <name>}`` exactly as in ``_normalise_tools``.
    """
    if not tools:
        return []
    converted: list = []
    for tool in tools:
        if isinstance(tool, str):
            converted.append({"type": tool})
            continue
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function" or isinstance(tool.get("function"), dict):
            converted.append(tool)
            continue
        function = {key: value for key, value in tool.items()
                    if key not in ("type", "function")}
        converted.append({"type": "function", "function": function})
    return converted


#: Prefix that marks a hidden platform snapshot (the task journal, the thread
#: artefacts folder, the economy-mode header) once it is moved out of a second
#: ``system`` message. The marker stays in the text, so the model can tell the
#: platform context from the user's own words.
SNAPSHOT_MARKER = "[PLATFORM CONTEXT]"


def _snapshot_block(text) -> str:
    """Render one folded ``system`` text as a marked data block."""
    return f"{SNAPSHOT_MARKER}\n{str(text).strip()}"


def collapse_system_messages(messages: list) -> list:
    """Reduce a message list to a single leading ``system`` message.

    A provider may keep only the last ``system`` message of a request and drop
    every earlier one. The platform sends the orchestrator prompt first and its
    hidden snapshots (task journal, thread artefacts folder, economy header)
    after it, so on such a provider the short snapshot wins and the prompt is
    lost - the model answers without its role, its tools and its rules.

    Every ``system`` text is therefore merged into the first ``system``
    message, in the order it arrived, each snapshot behind a marker so the
    model can tell the agent prompt from platform context. The user and
    assistant turns are left exactly as they were: the user's own words are
    never rewritten. The input is never mutated.

    :param messages: the message list of one request.
    :return: a list with at most one ``system`` message.
    """
    result = [dict(m) for m in messages if isinstance(m, dict)]
    system_ids = [i for i, m in enumerate(result) if m.get("role") == "system"]
    if len(system_ids) <= 1:
        return result
    blocks = []
    for position, index in enumerate(system_ids):
        text = result[index].get("content") or ""
        if not str(text).strip():
            continue
        blocks.append(str(text).strip() if position == 0 else _snapshot_block(text))
    head = dict(result[system_ids[0]])
    head["content"] = "\n\n".join(blocks)
    merged: list = []
    placed = False
    for message in result:
        if message.get("role") == "system":
            if not placed:
                merged.append(head)
                placed = True
            continue
        merged.append(message)
    return merged


def needs_single_system(service: Optional[dict], model: str = "") -> bool:
    """Tell whether a service keeps only one ``system`` message.

    The rule is declared by the service catalog, so the workaround reaches
    exactly the models that need it and no other provider sees a different
    request. Two forms are accepted:

    - ``"single_system_message": true`` - every model of the service;
    - ``"single_system_message": {"models": ["Prefix", ...]}`` - the models
      whose id starts with one of the prefixes.

    :param service: the service catalog entry.
    :param model: the model id of the request.
    :return: True when the request must be collapsed to one system message.
    """
    if not isinstance(service, dict):
        return False
    rule = service.get("single_system_message")
    if rule is True:
        return True
    if not isinstance(rule, dict):
        return False
    prefixes = rule.get("models") or []
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    name = str(model or "")
    return any(name.startswith(str(prefix))
               for prefix in prefixes if str(prefix))


def report_chat_usage(usage_callback, data: dict, messages: list, result_text: str) -> None:
    """Report token usage for one chat/completions round.

    Reads the provider's own counters when present and falls back to a local
    estimate otherwise, so a gateway that omits ``usage`` still yields a
    non-zero accounting. ``cache`` is the cached prompt share, read from
    ``prompt_tokens_details.cached_tokens`` or, on DeepSeek's chat route,
    from ``prompt_cache_hit_tokens``.
    """
    if not usage_callback:
        return
    from core.api_payload import _estimate_tokens_in
    from core.files import estimate_tokens

    usage = data.get("usage")
    cache = 0
    if usage and isinstance(usage, dict):
        tokens_in = usage.get("prompt_tokens") or 0
        tokens_out = usage.get("completion_tokens") or 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache = details.get("cached_tokens") or 0
        if not cache:
            cache = usage.get("prompt_cache_hit_tokens") or 0
    else:
        tokens_in = _estimate_tokens_in([m for m in messages if isinstance(m, dict)])
        tokens_out = estimate_tokens(result_text)
    usage_callback({"in": int(tokens_in), "out": int(tokens_out), "cache": int(cache)})


def build_chat_payload(model: str, messages: list, temperature: float, *,
                       max_tokens: Optional[int] = None,
                       tools: Optional[list] = None,
                       tool_choice=None,
                       json_schema=None,
                       extra_payload: Optional[dict] = None,
                       single_system: bool = False) -> tuple:
    """Assemble a chat/completions request body.

    Returns ``(payload, json_format)``: *json_format* tells the caller whether
    the answer is expected as JSON and therefore needs unwrapping.

    *single_system* collapses the message list to one leading ``system``
    message for the providers that keep only the last one. It defaults to
    False, so a service that does not declare the rule sends the conversation
    exactly as it was assembled.
    """
    from core.api_payload import _openai_response_format

    if single_system:
        messages = collapse_system_messages(messages)
    payload: dict = {
        "model":       model,
        "messages":    messages,
        "temperature": float(temperature),
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = _to_chat_tools(tools)
        if tool_choice:
            payload["tool_choice"] = tool_choice
    json_format = _openai_response_format(json_schema) if json_schema else None
    if json_format:
        payload["response_format"] = json_format
    _merge_extra_payload(payload, extra_payload)
    return payload, json_format

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
