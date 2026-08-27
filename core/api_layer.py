"""
core.api_layer - HTTP requests to AI service APIs.
No streamlit imports. Uses core.config and core.services.

All failures are raised as subclasses of APIError (core.api_errors).
Callers should catch APIError for user-facing messages and inspect
``exc.code`` for programmatic handling.
"""
import uuid
import json

import requests
from typing import Optional

from core.config import load_config
from core.services import get_services
from core.assistants import load_assistant_files_context

# Legacy alias (old "skill" terminology) for backward compatibility.
load_skill_files_context = load_assistant_files_context
from core.fs import combine_nonempty
from core.i18n import t
from core.prompt_guard import (
    detect_injection_signatures,
    is_tool_result_text,
    sanitize_text,
    sanitize_tool_result_content,
    wrap_data,
)
from core.api_errors import (
    APIError,
    ServiceNotFoundError,
    ApiKeyMissingError,
    AuthTypeUnknownError,
    ProviderHTTPError,
    RequestTimeoutError,
    NetworkError,
)

# Timeout (seconds) for a model COMPLETION request. Large models / long prompts
# can take a while to generate, so this is intentionally generous. Auxiliary
# calls (OAuth token, model listing) keep their own short timeouts below.
MODEL_RESPONSE_TIMEOUT = 600


# ─── TLS verification policy ────────────────────────────────────────────────
# By default ALL requests verify TLS certificates (verify=True).
# In rare cases (corporate proxies, self-signed internal CAs) an operator may
# explicitly opt out via SAGAAI_VERIFY_TLS=false, but this is strongly
# discouraged: disabling verification allows man-in-the-middle attacks that
# can expose API keys and conversation data.
import os as _os

_VERIFY_TLS = _os.environ.get("SAGAAI_VERIFY_TLS", "true").strip().lower() not in ("0", "false", "no", "off")

# GigaChat endpoints are signed by the Russian Trusted Root CA (Минцифры),
# which is not part of the default trust bundle used by requests/certifi.
# Instead of disabling TLS verification globally, requests to GigaChat use a
# dedicated CA bundle that contains this official root certificate. The path
# can be overridden with SAGAAI_GIGACHAT_CA_BUNDLE.
_GIGACHAT_DEFAULT_CA_BUNDLE = _os.path.abspath(_os.path.join(
    _os.path.dirname(__file__), "..", "certs", "russian_trusted_root_ca.pem",
))


def _gigachat_verify():
    """Return the ``verify`` argument for GigaChat requests.

    If global TLS verification is disabled, honour that setting for backward
    compatibility. Otherwise use the bundled Russian Trusted Root CA bundle,
    falling back to the normal global policy if the bundle is absent.
    """
    if not _VERIFY_TLS:
        return False
    bundle = _os.environ.get("SAGAAI_GIGACHAT_CA_BUNDLE", "").strip()
    if not bundle:
        bundle = _GIGACHAT_DEFAULT_CA_BUNDLE
    if _os.path.isfile(bundle):
        return bundle
    return _VERIFY_TLS


# Default fallback for max_tokens when neither the model entry nor the service
# definition provides an explicit value. Kept conservative to avoid hitting
# provider-side limits unexpectedly.
_DEFAULT_MAX_TOKENS = 4096


# ─── Sanitized-info helper ──────────────────────────────────────────────────
_SANITIZED_REASON = "[SANITIZED: potential prompt-injection signature detected; original content withheld]"


def _parse_sanitized_info(tool_result_text: str) -> dict:
    """Return a compact info dict describing a sanitized tool-result payload.

    The payload is a serialized ``{"tool_result": {...}}`` envelope. We
    extract the tool name and the file path (if present) so the caller/UI can
    show the user exactly which tool/result was withheld.
    """
    info: dict = {"reason": _SANITIZED_REASON}
    try:
        data = json.loads(tool_result_text)
        tr = data.get("tool_result", {})
        if isinstance(tr, dict):
            tname = tr.get("tool", "") or tr.get("name", "")
            if tname:
                info["tool"] = str(tname)
            path = tr.get("path", "")
            if path:
                info["path"] = str(path)
            err = tr.get("error", "")
            if err:
                info["error"] = str(err)
    except Exception:
        pass
    return info


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


def _prepare_response_content(message: dict) -> str:
    """Extract readable text from a chat completion response message.

    Handles:
    - 'content' field (standard)
    - 'tool_calls' field (serialised as JSON)

    'reasoning_content' (DeepSeek thinking mode) is ignored on purpose:
    chain-of-thought is internal and must never be shown to the user, fed
    to parse_tool_calls, or persisted.
    """
    parts = []
    content = message.get("content")
    if content and str(content).strip():
        parts.append(str(content))

    tool_calls = message.get("tool_calls")
    if tool_calls:
        json_calls = []
        for tc in tool_calls:
            func = tc.get("function")
            if func:
                name = func.get("name", "")
                try:
                    arguments = json.loads(func.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                json_calls.append({"tool": name, "args": arguments})
        if json_calls:
            fenced = "```json\n" + json.dumps(json_calls, ensure_ascii=False, indent=2) + "\n```"
            parts.append(fenced)

    return "\n\n".join(parts) if parts else ""


def _format_function_call_item(item: dict) -> str:
    """Render a native Responses API ``function_call`` item as fenced JSON.

    The agent-loop parser expects the legacy ``{"tool": ..., "args": {...}}``
    shape, so native function calls are converted before returning the text.
    Returns an empty string when the item carries no usable name/arguments.
    """
    name = item.get("name", "")
    arguments = item.get("arguments", "{}")
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else arguments
        if not isinstance(args, dict):
            args = {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    return "```json\n" + json.dumps(
        [{"tool": name, "args": args}], ensure_ascii=False, indent=2
    ) + "\n```"


# ─── structured output (native JSON Schema) ─────────────────────────────────
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


def _unwrap_json_text(text) -> str:
    """Best-effort extraction of a JSON payload from a provider response.

    Some providers (GigaChat) return structured outputs wrapped in markdown
    fences, ``<unk>`` control tokens and an outer ``{schema_name: {...}}``
    envelope. This helper strips that noise, unwraps the single-key envelope
    and returns the bare JSON text, so structured-output consumers receive
    the schema-compliant object.
    """
    t = (text or "").strip()
    t = t.replace("<unk>", "").replace("```", "")
    t = t.strip().strip(";").strip()
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end < start:
        return t
    t = t[start:end + 1]
    try:
        obj = json.loads(t)
    except Exception:
        return t
    if isinstance(obj, dict) and len(obj) == 1:
        inner = next(iter(obj.values()))
        if isinstance(inner, dict):
            return json.dumps(inner, ensure_ascii=False)
    return t


def _is_schema_rejection(status_code: int, body: str) -> bool:
    """True when a provider error looks like a structured-output rejection.

    Used by the retry-fallback: when a request carrying a ``json_schema`` is
    rejected with a format/parameters error mentioning the schema or the
    format, the same request is retried WITHOUT the schema so the call still
    succeeds on providers that only partially support structured output.
    """
    if status_code not in (400, 404, 415, 422):
        return False
    b = (body or "").lower()
    if not b:
        return False
    if "json_schema" not in b and "response_format" not in b:
        return False
    return any(k in b for k in (
        "invalid", "unsupported", "not support", "unknown", "unrecognized",
        "unexpected", "bad request", "extra inputs", "unknown parameter",
        "unprocessable",
    ))


def _extract_responses_text(data: dict) -> str:
    """Extract readable text from a Responses API response (non-streaming).

    Unified extractor for every AI Studio model (yandexgpt, aliceai, qwen,
    gpt-oss, deepseek-v4-*) which all share the same Responses contract.
    Resolution order:

    1. Top-level ``output_text`` - the convenience property exposed by the
       official SDKs (and some API responses). Some responses carry it,
       others leave it empty/absent, so this is the preferred but not the
       only path.
    2. Deep traversal of ``output``:
       - ``message`` items: non-blank ``content[].text`` blocks are collected
         (types ``output_text`` / ``text``); ``content`` may be a string or
         a list of content blocks;
       - native ``function_call`` items are rendered as fenced JSON so the
         agent loop can parse them.

    ``reasoning``, ``web_search_call``, ``file_search_call`` and other
    auxiliary items are ignored: reasoning is chain-of-thought and must
    never leak into the user-visible text, tool-call parsing, or history.
    """
    text = data.get("output_text", "") or ""
    if isinstance(text, str) and text.strip():
        return text.strip()

    output = data.get("output", [])
    if not isinstance(output, list):
        return ""

    parts: list = []
    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            content = item.get("content")
            if isinstance(content, str):
                if content.strip():
                    parts.append(content.strip())
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype not in (None, "text", "output_text"):
                        continue
                    btext = block.get("text", "")
                    if isinstance(btext, str) and btext.strip():
                        parts.append(btext.strip())
        elif itype == "function_call":
            fenced = _format_function_call_item(item)
            if fenced:
                parts.append(fenced)

    return "\n\n".join(p for p in parts if p)


def _extract_deepseek_responses_text(data: dict) -> str:
    """Extract the final text from a DeepSeek Responses API response.

    DeepSeek models are served through the same Responses API contract as
    AI Studio models, so this is a thin wrapper over the unified extractor
    (:func:`_extract_responses_text`). Reasoning is never leaked; native
    ``function_call`` items are converted to the fenced-JSON shape the
    agent-loop parser understands.
    """
    return _extract_responses_text(data)


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


def _protect_history(hist_msgs: list,
                     enable_injection_protection: bool = True,
                     sanitized_callback=None,
                     approved_paths=None) -> list:
    """Return a copy of *hist_msgs* with tool-result payloads sanitized.

    Messages that look like serialized tool results (content starting with
    `{"tool_result"`) have their content passed through
    ``sanitize_tool_result_content``: control characters are removed, the
    payload is wrapped in [DATA_BEGIN / DATA_END] fences.

    When *enable_injection_protection* is True (default), injection-signature
    matches cause the payload to be replaced by a short [SANITIZED]
    placeholder.  When False, the content is still scrubbed of control
    characters and wrapped in data fences, but the original text is
    preserved (the ``strict`` flag is False).

    When a payload is actually replaced by the [SANITIZED] placeholder,
    *sanitized_callback* (if provided) is called with a compact info dict:
    {"reason", "tool"?, "path"?, "error"?}. This lets the caller detect
    sanitization and, for example, stop the loop for user approval.

    *approved_paths* may be provided as an iterable of file path strings.
    Tool-result messages whose embedded "path" is in that set have been
    explicitly approved by the user and are wrapped with data fences but
    NOT re-sanitized (and therefore do not re-trigger the callback), even
    when protection is enabled.

    Non-dict messages and regular user/assistant text are returned
    unchanged (the same object, not copied) to keep the overhead minimal.
    """
    if not hist_msgs:
        return hist_msgs or []

    approved_paths = set(approved_paths or ())
    protected: list = []
    for i, msg in enumerate(hist_msgs):
        if not isinstance(msg, dict):
            protected.append(msg)
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and is_tool_result_text(content):
            m = dict(msg)
            info = _parse_sanitized_info(content)
            path = info.get("path", "")
            strict = enable_injection_protection and path not in approved_paths

            # Determine whether this payload will actually be replaced by the
            # [SANITIZED] placeholder BEFORE sanitizing, rather than checking
            # for the literal "[SANITIZED" substring in the result. The raw
            # file/tool content may legitimately contain that literal (e.g.
            # core/api_layer.py itself), which would otherwise cause a false
            # positive and stop the loop even when protection is disabled.
            will_sanitize = strict and bool(
                detect_injection_signatures(sanitize_text(content))
            )

            new_content = sanitize_tool_result_content(content, source="tool_result",
                                                       strict=strict)
            if will_sanitize and sanitized_callback is not None:
                info = _parse_sanitized_info(content)
                info["message_index"] = i
                try:
                    sanitized_callback(info)
                except Exception:
                    pass
            m["content"] = new_content
            protected.append(m)
        else:
            protected.append(msg)
    return protected


def _estimate_tokens_in(messages: list) -> int:
    """Estimate input tokens for a list of API messages."""
    from core.files import estimate_tokens
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
    return max(1, total)


def _bearer_request(url: str, api_key: str, model: str,
                    messages: list, temperature: float,
                    tools: list = None, usage_callback=None,
                    max_tokens: int = None, service: str = "",
                    json_schema=None) -> str:
    """Send a standard Bearer-token chat completion request.

    *tools*: optional list of tool keys (str) or tool dicts to include in the payload.
    *max_tokens*: optional maximum number of output tokens.
    *json_schema*: optional JSON Schema (dict) or {name, schema} envelope.
        When present, rendered as the OpenAI ``response_format`` json_schema
        block and the response is expected (and unwrapped) as JSON text.
    *usage_callback*: callable({"in": N, "out": M, "cache": C}) for token tracking.
        "cache" is the number of cached input tokens (0 for chat-completion APIs
        that do not report cache details).
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
        if usage and isinstance(usage, dict):
            tokens_in = usage.get("prompt_tokens") or 0
            tokens_out = usage.get("completion_tokens") or 0
        else:
            tokens_in = _estimate_tokens_in(messages)
            from core.files import estimate_tokens
            tokens_out = estimate_tokens(result_text)
        usage_callback({"in": int(tokens_in), "out": int(tokens_out), "cache": 0})

    return result_text


# Valid reasoning-effort values accepted by DeepSeek's Responses API.
_DEEPSEEK_REASONING_EFFORTS = {
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
}


def _deepseek_reasoning_effort(cfg: dict, svc_name: str, effort: str = None) -> str:
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


def _deepseek_responses_request(base_url: str, api_key: str, model: str,
                                sys_text: str, hist_msgs: list, user_content: str,
                                *, max_tokens: int = None, tools_list: list = None,
                                max_tool_calls=None, reasoning_effort: str = None,
                                cfg: dict = None,
                                svc_name: str = "", usage_callback=None,
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
        if content is None:
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


# Valid reasoning-effort values accepted by the Yandex Responses API.
_YANDEX_REASONING_EFFORTS = {
    "none", "minimal", "low", "medium", "high", "xhigh",
}


def _yandex_reasoning_effort(cfg: dict, svc_name: str, effort: str = None,
                             model: str = "", svc: dict = None) -> str:
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


def _yandex_web_search_config(cfg: dict, svc_name: str) -> tuple:
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


def _assistant_web_search_config(assistant: Optional[dict], cfg: dict,
                                 svc_name: str) -> tuple:
    """Return (context_size, allowed_domains) for an assistant's web search.

    Priority:
      1. Per-assistant overrides from DATA_DIR/assistants/<slug>/manifest.json
         (``web_search_context_size`` / ``web_search_allowed_domains``);
      2. Provider-level persisted config (same keys);
      3. Defaults (medium / empty domains).
    """
    ctx_size, allowed = _yandex_web_search_config(cfg or {}, svc_name)
    if not assistant:
        return ctx_size, allowed
    try:
        from core.assistant_folders import get_assistant_web_search_settings
        slug = str(assistant.get("slug") or "")
        if not slug:
            return ctx_size, allowed
        settings = get_assistant_web_search_settings(slug) or {}
        if settings.get("context_size") in ("low", "medium", "high"):
            ctx_size = settings["context_size"]
        doms = settings.get("allowed_domains")
        if isinstance(doms, list) and doms:
            allowed = list(dict.fromkeys(str(d).strip() for d in doms if str(d).strip()))
    except Exception:
        pass  # fall back to provider-level defaults
    return ctx_size, allowed


def _yandex_responses_request(base_url: str, api_key: str, folder_id: str,
                              model: str, sys_text: str, hist_msgs: list,
                              user_content: str, *, temperature: float,
                              max_tokens: int = None, tools_list: list = None,
                              max_tool_calls=None, reasoning_effort: str = None,
                              cfg: dict = None,
                              svc_name: str = "", usage_callback=None,
                              tool_choice=None, svc: dict = None,
                              assistant: Optional[dict] = None,
                              json_schema=None) -> str:
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
        if content is None:
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

    effort = _yandex_reasoning_effort(cfg or {}, svc_name, reasoning_effort,
                                      model=model, svc=svc)
    if effort:
        payload["reasoning"] = {"effort": effort}

    ctx_size, allowed_domains = _assistant_web_search_config(
        assistant, cfg or {}, svc_name
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

    r = requests.post(
        f"{base_url}/responses", headers=headers, json=payload,
        timeout=MODEL_RESPONSE_TIMEOUT, verify=_VERIFY_TLS,
    )
    if r.status_code != 200:
        body = _extract_error_body(r)
        raise ProviderHTTPError(r.status_code, body, service=svc_name)

    data = r.json()
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


def _gigachat_token(credentials: str, scope: str = "GIGACHAT_API_PERS") -> str:
    """Obtain an OAuth token for GigaChat. Raises ProviderHTTPError on failure."""
    r = requests.post(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        headers={
            "Authorization": f"Basic {credentials}",
            "RqUID":         str(uuid.uuid4()),
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={"scope": scope},
        timeout=30,
        verify=_gigachat_verify(),
    )
    if r.status_code != 200:
        body = r.text[:300]
        raise ProviderHTTPError(r.status_code, body, service="GigaChat")
    return r.json()["access_token"]


def _assistant_rag_context(assistant: dict, user_message: str) -> str:
    """Build an auto-RAG context block from the assistant's bound knowledge bases.

    The binding is stored in the assistant folder manifest under the
    ``rag_bases`` key (list of base slugs). Bases with non-empty
    ``rag_slots`` must include the assistant slug / id / name to be used.
    Failures are swallowed so chat continues with an empty context.
    """
    try:
        from core.assistant_folders import load_assistant_bundle
        from core.assistants import get_assistant_by_id
        from core.rag import get_base
        from core.rag_search import chat_context

        a_id = str(assistant.get("id") or "")
        slug = str(assistant.get("slug") or "")
        if not slug and a_id:
            full = get_assistant_by_id(a_id)
            if full:
                slug = str(full.get("slug") or "")
        if not slug:
            return ""
        bundle = load_assistant_bundle(slug) or {}
        bases = [
            str(b).strip().lower()
            for b in (bundle.get("rag_bases") or [])
            if str(b).strip()
        ]
        if not bases:
            return ""
        allowed_names = {
            slug.lower(),
            a_id.lower(),
            str(assistant.get("name") or "").strip().lower(),
        }
        parts = []
        for bslug in bases:
            b = get_base(bslug)
            if not b:
                continue
            slots = {str(s).strip().lower() for s in (b.get("rag_slots") or [])}
            if slots and not slots.intersection(allowed_names):
                continue
            ctx = chat_context(bslug, str(user_message))
            if ctx:
                parts.append(ctx)
        return "\n\n".join(parts)
    except Exception:
        return ""


def send_request(user_message: str, assistant: Optional[dict] = None,
                 file_context: str = "", history: list = None,
                 lang: str = None,
                 usage_callback=None,
                 enable_injection_protection: bool = True,
                 sanitized_callback=None,
                 sanitized_approved_paths=None,
                 **kwargs) -> str:
    """Send a chat-completion request and return the text response.

    Args:
        enable_injection_protection: Forwarded to ``_protect_history`` to
            control the strictness of prompt-injection counter-measures.
        sanitized_callback: optional callable invoked when a tool-result
            payload is replaced by the [SANITIZED] placeholder. Receives a
            compact dict with "reason" and optional "tool" / "path" / "error".
        usage_callback: optional callable invoked with
            {"in": N, "out": M, "cache": C} where C is the number of cached
            input tokens reported by the provider (0 when not available).

    Raises:
        ServiceNotFoundError  - assistant's service is not registered.
        ApiKeyMissingError    - required credential is absent.
        AuthTypeUnknownError  - service has an unknown auth_type.
        ProviderHTTPError     - provider responded non-200.
        RequestTimeoutError   - request timed out.
        NetworkError          - DNS / connection-level failure.
    """
    # Backward compatibility: accept the legacy 'skill' argument name.
    if assistant is None and 'skill' in kwargs:
        assistant = kwargs.pop('skill')
    if assistant is None:
        assistant = {}
    cfg      = load_config()
    services = get_services()
    svc_name = assistant.get("service", "")
    svc      = services.get(svc_name)
    if not svc:
        raise ServiceNotFoundError(svc_name)

    model    = assistant["model"]
    temp     = float(assistant.get("temperature", svc.get("temp_default", 0.7)))
    sys_text = assistant.get("text", "")
    max_tokens = assistant.get("max_tokens") or _get_model_max_tokens(svc, model)

    assistant_file_ctx = load_assistant_files_context(assistant.get("id", ""))
    combined_file_ctx = combine_nonempty([
        f"**Материалы помощника:**\n{assistant_file_ctx}" if assistant_file_ctx else "",
        f"**Файл пользователя:**\n{file_context}"   if file_context   else "",
    ])

    # Untrusted content (assistant attachments, user-provided file context) must
    # be marked as DATA, not instructions, before it reaches the model.
    protected_file_ctx = wrap_data(
        sanitize_text(combined_file_ctx, max_len=20000),
        source="file_context",
    ) if combined_file_ctx.strip() else ""

    # RAG auto-search: knowledge bases bound to the assistant (folder
    # manifest key ``rag_bases``) are searched for the current message and
    # the formatted context is appended as untrusted DATA.
    # Assistants with a native ``rag_search`` function tool search the base
    # themselves through the function-calling loop; the auto-context would
    # duplicate the search (and search the raw message without history), so
    # it is skipped for them.
    assistant_tools_early = assistant.get("tools", [])
    if _has_native_function_tools(assistant_tools_early):
        rag_ctx = ""
    else:
        rag_ctx = _assistant_rag_context(assistant, user_message)
    protected_rag_ctx = wrap_data(
        sanitize_text(rag_ctx, max_len=20000),
        source="rag_context",
    ) if rag_ctx.strip() else ""

    if protected_file_ctx and not user_message.strip():
        user_content = (
            "Проанализируй содержимое файла согласно системному промпту."
            f"\n\n---\n**Контекст:**\n{protected_file_ctx}"
            + (f"\n\n---\n**База знаний:**\n{protected_rag_ctx}" if protected_rag_ctx else "")
        )
    elif protected_file_ctx:
        user_content = (
            f"{user_message}"
            f"\n\n---\n**Контекст из файлов:**\n{protected_file_ctx}"
            + (f"\n\n---\n**База знаний:**\n{protected_rag_ctx}" if protected_rag_ctx else "")
        )
    elif protected_rag_ctx:
        user_content = (
            f"{user_message}"
            f"\n\n---\n**База знаний:**\n{protected_rag_ctx}"
        )
    else:
        user_content = user_message

    auth_type = svc.get("auth_type", "bearer")
    base_url  = svc.get("base_url", "")
    # Tool-result payloads in history are sanitized and wrapped as data
    # so they cannot inject instructions into the running conversation.
    hist_msgs = _protect_history(
        history if history else [],
        enable_injection_protection=enable_injection_protection,
        sanitized_callback=sanitized_callback,
        approved_paths=sanitized_approved_paths,
    )
    # Extract tools from assistant (list of strings or dicts, e.g. ["web_search"]).
    tools_list = assistant.get("tools", [])
    max_tool_calls = assistant.get("max_tool_calls", None)
    native_function_tools = _has_native_function_tools(tools_list)
    # Optional native structured output (JSON Schema) request option.
    json_schema = assistant.get("json_schema")
    # Optional per-assistant reasoning effort (overrides the provider default).
    reasoning_effort = assistant.get("reasoning_effort")
    # Optional forced-tool selection, e.g. {"type": "web_search"}.
    tool_choice = assistant.get("tool_choice", None)

    # Providers/models with an unreliable auto tool invocation: Yandex models
    # (aliceai-*) may answer with placeholder text instead of actually calling
    # web_search when tool_choice is "auto". If the assistant declares a
    # web_search tool and did not set an explicit tool_choice, force the search
    # for yandex_iam (the same mechanism the orchestrator web_search tool uses).
    # DeepSeek is intentionally left UNFORCED: forced tool_choice makes it loop
    # through many searches and finish with an empty answer, so for
    # deepseek_responses the prompt-based "exactly one search" rule is used
    # instead (see dev_agent/tool_executor.py).
    if tool_choice is None and auth_type == "yandex_iam" and not native_function_tools:
        for t in tools_list:
            t_type = t if isinstance(t, str) else (t.get("type") if isinstance(t, dict) else None)
            if t_type == "web_search":
                tool_choice = {"type": "web_search"}
                break

    try:
        def _call(with_schema: bool):
            return _do_request(
                auth_type=auth_type, svc_name=svc_name, svc=svc,
                cfg=cfg, base_url=base_url,
                model=model, sys_text=sys_text,
                hist_msgs=hist_msgs, user_content=user_content,
                temp=temp, max_tokens=max_tokens,
                tools_list=tools_list, max_tool_calls=max_tool_calls,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
                usage_callback=usage_callback,
                native_function_tools=native_function_tools,
                assistant=assistant,
                on_tool_call=kwargs.get("on_tool_call"),
                json_schema=json_schema if with_schema else None,
            )

        try:
            return _call(True)
        except ProviderHTTPError as exc:
            if json_schema and _is_schema_rejection(exc.status_code, exc.body or ""):
                # Structured output rejected by the provider: retry without
                # the schema so the call still succeeds.
                return _call(False)
            raise
    except APIError:
        raise
    except requests.exceptions.Timeout:
        raise RequestTimeoutError(service=svc_name)
    except requests.exceptions.RequestException as e:
        raise NetworkError(str(e), service=svc_name)
    except Exception as e:
        raise NetworkError(str(e), service=svc_name)


def _do_request(auth_type: str, svc_name: str, svc: dict, cfg: dict,
                base_url: str, model: str, sys_text: str,
                hist_msgs: list, user_content: str,
                temp: float, max_tokens: int,
                tools_list: list, max_tool_calls, usage_callback,
                tool_choice=None, reasoning_effort=None,
                native_function_tools: bool = False, assistant: dict = None,
                on_tool_call=None, json_schema=None) -> str:
    """Dispatch to the appropriate auth-handler. Internal helper of send_request."""

    if auth_type == "bearer":
        api_key = cfg.get(svc.get("config_key", ""), "")
        if isinstance(api_key, str):
            api_key = api_key.strip()
        if not api_key:
            raise ApiKeyMissingError(svc_name)
        messages = (
            [{"role": "system", "content": sys_text}]
            + hist_msgs
            + [{"role": "user", "content": user_content}]
        )
        return _bearer_request(
            base_url, api_key, model, messages, temp,
            tools=tools_list, usage_callback=usage_callback,
            max_tokens=max_tokens, service=svc_name,
            json_schema=json_schema,
        )

    elif auth_type == "deepseek_responses":
        api_key = cfg.get(svc.get("config_key", ""), "")
        if isinstance(api_key, str):
            api_key = api_key.strip()
        if not api_key:
            raise ApiKeyMissingError(svc_name)
        return _deepseek_responses_request(
            base_url, api_key, model, sys_text, hist_msgs, user_content,
            max_tokens=max_tokens,
            tools_list=tools_list,
            max_tool_calls=max_tool_calls,
            reasoning_effort=reasoning_effort,
            cfg=cfg,
            svc_name=svc_name,
            usage_callback=usage_callback,
            tool_choice=tool_choice,
            json_schema=json_schema,
        )

    elif auth_type == "yandex_iam":
        api_key = cfg.get(svc.get("config_key", ""), "")
        if isinstance(api_key, str):
            api_key = api_key.strip()
        if not api_key:
            raise ApiKeyMissingError(svc_name, field="IAM token")
        folder_id = cfg.get(svc.get("config_key2", ""), "")
        if isinstance(folder_id, str):
            folder_id = folder_id.strip()
        if not folder_id:
            raise ApiKeyMissingError(svc_name, field="Folder ID")
        if native_function_tools:
            from core.assistant_tools import run_yandex_responses_tool_loop
            return run_yandex_responses_tool_loop(
                base_url, api_key, folder_id, model,
                sys_text, hist_msgs, user_content,
                temperature=temp,
                max_tokens=max_tokens,
                tools_list=tools_list,
                max_tool_calls=max_tool_calls,
                reasoning_effort=reasoning_effort,
                cfg=cfg,
                svc_name=svc_name,
                usage_callback=usage_callback,
                tool_choice=tool_choice,
                svc=svc,
                assistant=assistant,
                on_tool_call=on_tool_call,
            )
        return _yandex_responses_request(
            base_url, api_key, folder_id, model,
            sys_text, hist_msgs, user_content,
            temperature=temp,
            max_tokens=max_tokens,
            tools_list=tools_list,
            max_tool_calls=max_tool_calls,
            reasoning_effort=reasoning_effort,
            cfg=cfg,
            svc_name=svc_name,
            usage_callback=usage_callback,
            tool_choice=tool_choice,
            svc=svc,
            assistant=assistant,
            json_schema=json_schema,
        )

    elif auth_type == "gigachat_oauth":
        creds = cfg.get(svc.get("config_key", ""), "")
        if isinstance(creds, str):
            creds = creds.strip()
        if not creds:
            raise ApiKeyMissingError(svc_name)
        scope = cfg.get(svc.get("config_key2", ""), "GIGACHAT_API_PERS")
        if isinstance(scope, str):
            scope = scope.strip() or "GIGACHAT_API_PERS"
        else:
            scope = "GIGACHAT_API_PERS"
        token = _gigachat_token(creds, scope)
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        })
        messages = (
            [{"role": "system", "content": sys_text}]
            + hist_msgs
            + [{"role": "user", "content": user_content}]
        )
        payload = {
            "model":    model,
            "messages": messages,
            "temperature":     float(temp),
            "max_tokens":      int(max_tokens),
            "stream":          False,
            "update_interval": 0,
        }
        giga_format = _gigachat_response_format(json_schema) if json_schema else None
        if giga_format:
            payload["response_format"] = giga_format
        r = session.post(
            base_url, json=payload,
            timeout=MODEL_RESPONSE_TIMEOUT, verify=_gigachat_verify(),
        )
        if not r.ok:
            body = _extract_gigachat_error(r)
            raise ProviderHTTPError(r.status_code, body, service=svc_name)
        response_body = r.json()
        message = response_body["choices"][0]["message"]
        result_text = _prepare_response_content(message)
        if giga_format and result_text.strip():
            result_text = _unwrap_json_text(result_text)

        if usage_callback:
            usage = response_body.get("usage")
            if usage and isinstance(usage, dict):
                tokens_in = usage.get("prompt_tokens") or 0
                tokens_out = usage.get("completion_tokens") or 0
            else:
                tokens_in = _estimate_tokens_in(messages)
                from core.files import estimate_tokens
                tokens_out = estimate_tokens(result_text)
            usage_callback({"in": int(tokens_in), "out": int(tokens_out), "cache": 0})

        return result_text

    else:
        raise AuthTypeUnknownError(svc_name, auth_type)


# ─── helpers ──────────────────────────────────────────────────────────────────


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


def _extract_gigachat_error(r) -> str:
    try:
        err_body = r.json()
        return (
            err_body.get("message")
            or err_body.get("error", {}).get("message")
            or r.text[:300]
        )
    except Exception:
        return r.text[:300]


# ─── test_connection ──────────────────────────────────────────────────────────


def test_connection(svc_name: str, cfg: dict) -> tuple:
    """Test connectivity to *svc_name*. Returns (ok: bool, message: str).

    Does NOT use the new exception contract - this is a UI helper and
    callers expect the legacy ``(bool, str)`` return.
    """
    services  = get_services()
    svc       = services.get(svc_name)
    if not svc:
        return False, f"Сервис {svc_name} не найден"
    auth_type = svc.get("auth_type", "bearer")
    base_url  = svc.get("base_url", "")
    try:
        if auth_type == "bearer":
            key = cfg.get(svc.get("config_key", ""), "")
            if isinstance(key, str): key = key.strip()
            if not key:
                return False, "API key not set"
            models     = svc.get("models", [])
            test_model = (
                (models[0]["id"] if isinstance(models[0], dict) else models[0])
                if models else "gpt-3.5-turbo"
            )
            r = requests.post(
                base_url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": test_model, "messages": [{"role": "user", "content": "Hi"}],
                      "max_tokens": 5},
                timeout=30,
                verify=_VERIFY_TLS,
            )
            return (
                (True, f"OK (HTTP {r.status_code})")
                if r.status_code == 200
                else (False, f"HTTP {r.status_code}: {r.text[:200]}")
            )

        elif auth_type == "deepseek_responses":
            key = cfg.get(svc.get("config_key", ""), "")
            if isinstance(key, str): key = key.strip()
            if not key:
                return False, "API key not set"
            models     = svc.get("models", [])
            test_model = (
                (models[0]["id"] if isinstance(models[0], dict) else models[0])
                if models else "deepseek-v4-flash"
            )
            r = requests.post(
                base_url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": test_model,
                    "input": [{"role": "user", "content": "ping"}],
                    "max_output_tokens": 5,
                    "stream": False,
                },
                timeout=30,
                verify=_VERIFY_TLS,
            )
            return (
                (True, f"OK (HTTP {r.status_code})")
                if r.status_code == 200
                else (False, f"HTTP {r.status_code}: {r.text[:200]}")
            )

        elif auth_type == "yandex_iam":
            key = cfg.get(svc.get("config_key", ""), "")
            if isinstance(key, str): key = key.strip()
            if not key:
                return False, "IAM token not set"
            folder_id = cfg.get(svc.get("config_key2", ""), "")
            if isinstance(folder_id, str): folder_id = folder_id.strip()
            if not folder_id:
                return False, "Folder ID not set"
            models = svc.get("models", [])
            test_model = (
                (models[0]["id"] if isinstance(models[0], dict) else models[0])
                if models else "yandexgpt-5.1"
            )
            full_model_uri = f"gpt://{folder_id}/{test_model}"
            r = requests.post(
                f"{base_url}/responses",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       full_model_uri,
                    "input":       [{"role": "user", "content": "ping"}],
                    "max_output_tokens": 5,
                    "stream":      False,
                },
                timeout=30,
                verify=_VERIFY_TLS,
            )
            return (
                (True, f"OK (HTTP {r.status_code})")
                if r.status_code == 200
                else (False, f"HTTP {r.status_code}: {r.text[:200]}")
            )

        elif auth_type == "gigachat_oauth":
            key = cfg.get(svc.get("config_key", ""), "")
            if isinstance(key, str): key = key.strip()
            if not key:
                return False, "API key not set"
            scope = cfg.get(svc.get("config_key2", ""), "GIGACHAT_API_PERS")
            if isinstance(scope, str): scope = scope.strip() or "GIGACHAT_API_PERS"
            else: scope = "GIGACHAT_API_PERS"
            token = _gigachat_token(key, scope)
            if not token:
                return False, "Failed to get token"
            session = requests.Session()
            session.headers.update({
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            })
            r2 = session.get(
                "https://gigachat.devices.sberbank.ru/api/v1/models",
                timeout=15, verify=_gigachat_verify(),
            )
            if r2.status_code == 200:
                return True, "OK - token received, /models available"
            else:
                return True, f"Token OK, but /models returned {r2.status_code}"

    except Exception as e:
        return False, str(e)
    return False, "Unknown auth_type"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
