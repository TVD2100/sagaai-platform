"""
core.api_payload - request-payload assembly shared by the API transports.

Turns assistant-level settings into provider arguments: token limits, the
tools list, content checks and the JSON-Schema request-format blocks each
provider expects. Everything here is a pure function over its arguments -
no network calls and no module-level mutable state - so every transport in
core.api_layer can share one implementation.
"""


# Default fallback for max_tokens when neither the model entry nor the service
# definition provides an explicit value. Kept conservative to avoid hitting
# provider-side limits unexpectedly.
_DEFAULT_MAX_TOKENS = 4096


# ─── token limits ─────────────────────────────────────────────────────────


def _get_model_max_tokens(svc: dict, model_id: str) -> int:
    """Return the max_tokens value configured for *model_id* in service *svc*.

    Priority:
      1. The model entry's "max_tokens" field (if set).
      2. The service-level "max_tokens_default" field (if set).
      3. A built-in conservative fallback (``_DEFAULT_MAX_TOKENS``).

    Model entries may be dicts (recommended) or plain strings.
    """
    for m in svc.get("models", []):
        if isinstance(m, dict) and m.get("id") == model_id:
            mt = m.get("max_tokens")
            if mt:
                return int(mt)
    svc_default = svc.get("max_tokens_default")
    if svc_default:
        return int(svc_default)
    return _DEFAULT_MAX_TOKENS


def _clamp_max_tokens(max_tokens, svc: dict, model_id: str):
    """Clamp *max_tokens* to the model's EXPLICIT output limit.

    A value saved in the orchestrator config for one model (e.g. 384000 for
    a DeepSeek model) must not be forwarded verbatim after the service or
    model was switched: providers with a lower cap reject such requests
    (GigaChat allows at most 32768 output tokens).

    Only limits declared explicitly in the service definition are enforced
    (the model entry's "max_tokens" or the service-level
    "max_tokens_default"); services without a declared limit keep the
    requested value unchanged. Returns the value unchanged when it is
    falsy.
    """
    if not max_tokens:
        return max_tokens
    for m in svc.get("models", []):
        if isinstance(m, dict) and m.get("id") == model_id:
            mt = m.get("max_tokens")
            if mt:
                return min(int(max_tokens), int(mt))
            break
    svc_default = svc.get("max_tokens_default")
    if svc_default:
        return min(int(max_tokens), int(svc_default))
    return max_tokens


# Producers of structured-data calls can attach a ``json_schema`` request
# option (a bare JSON Schema dict, or {"name": ..., "schema": ...}) to an
# assistant dict. Each transport renders it in the shape the provider accepts;
# verified live for YandexAI / DeepSeek (Responses API ``text.format``) and
# GigaChat (``response_format``), see .dev_agent/scratch/probe_structured_output.py.


def _normalise_json_schema(json_schema, default_name: str = "structured_output"):
    """Return a {"name", "schema"} envelope for *json_schema*, or None.

    Accepts both a bare JSON Schema dict (a default *default_name* is used)
    and an explicit {"name": ..., "schema": ...} envelope.
    """
    if not json_schema or not isinstance(json_schema, dict):
        return None
    if "schema" in json_schema:
        schema = json_schema.get("schema")
    else:
        schema = json_schema
    if not isinstance(schema, dict):
        return None
    raw_name = str(json_schema.get("name") or default_name).strip() or default_name
    name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in raw_name)
    return {"name": name, "schema": schema}


def _responses_json_format(json_schema):
    """Return the Responses API (Yandex/DeepSeek) ``text.format`` block."""
    norm = _normalise_json_schema(json_schema)
    if not norm:
        return None
    return {
        "type": "json_schema",
        "name": norm["name"],
        "schema": norm["schema"],
        "strict": True,
    }


def _openai_response_format(json_schema):
    """Return the OpenAI (Bearer Chat Completions) ``response_format`` block."""
    norm = _normalise_json_schema(json_schema)
    if not norm:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": norm["name"],
            "schema": norm["schema"],
        },
    }


def _gigachat_response_format(json_schema):
    """Return the GigaChat ``response_format`` block.

    GigaChat accepts ``{type, schema, strict}`` (no ``name`` field) but
    wraps the produced JSON in a ``{schema_name: {...}}`` envelope, so
    the result is unwrapped with :func:`_unwrap_json_text`.
    """
    norm = _normalise_json_schema(json_schema)
    if not norm:
        return None
    return {
        "type": "json_schema",
        "schema": norm["schema"],
        "strict": True,
    }


def _content_has_material(content) -> bool:
    """Return True when a history message's content carries anything sendable.

    The Responses API rejects payloads whose input contains empty content
    (HTTP 400 "Content of input is empty"); that used to happen when a
    stored empty assistant answer made it back into the history. Empty
    strings, whitespace-only strings, empty lists and lists without a
    single non-blank text/image block are treated as empty.
    """
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if isinstance(block, dict):
                if str(block.get("text") or "").strip():
                    return True
                if block.get("type") in ("input_image", "image_url", "image"):
                    return True
        return False
    return True


def _normalise_tools(tools: list) -> list:
    """Convert a tools list to API payload format.

    Each element can be:
      - a string (e.g. 'web_search') -> {"type": "web_search"}
      - a dict (e.g. {"type": "web_search", "filters": {...}}) -> used as-is
    """
    if not tools:
        return []
    result = []
    for t in tools:
        if isinstance(t, dict):
            result.append(t)
        elif isinstance(t, str):
            result.append({"type": t})
    return result


def _has_native_function_tools(tools: list) -> bool:
    """True when *tools* contains at least one native function tool.

    A native function tool is a dict with ``type == "function"`` (the
    Responses API FunctionTool shape: name/description/parameters). Such
    tools are executed by the platform-side function-calling loop
    (core.assistant_tools), so they disable the legacy auto-context RAG
    and the forced web_search tool_choice.
    """
    for t in tools or []:
        if isinstance(t, dict) and t.get("type") == "function":
            return True
    return False


def _estimate_tokens_in(messages: list) -> int:
    """Estimate input tokens for a list of API messages."""
    from core.files import estimate_tokens
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
    return max(1, total)

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
