"""
core.api_responses - parsing the body of an OpenAI Responses API reply.

The Responses API returns a ``output`` array of typed items rather than a
single message: assistant text, reasoning items, function calls and refusal
blocks can all appear, and failures arrive as either an ``error`` object or
an ``incomplete_details`` record. This module turns that structure into the
text (or the exception) the transports in core.api_layer need, and unwraps
the JSON a structured-output call returns.

Every function here is pure: it inspects the decoded body and returns a
value or raises ProviderResponseError.
"""
import json

from core.api_errors import ProviderResponseError


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
# ─── structured-output formats (moved to core.api_payload) ──────────────────

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
    deepseek-v4-*) which all share the same Responses contract.
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


# ─── Responses API failure detection / input sanitising ─────────────────────


def _responses_failure_details(data: dict) -> dict:
    """Return provider failure details for a Responses API body (or {}).

    Some providers answer with HTTP 200 while the generation itself failed:
    ``status == "failed"`` and/or a non-null ``error`` object (Yandex AI
    Studio reports an unsupported ``reasoning.effort`` as ``invalid_prompt``
    with an empty ``output``). Without this check such a body is silently
    treated as a normal empty answer.
    """
    if not isinstance(data, dict):
        return {}
    err = data.get("error")
    status = str(data.get("status") or "").strip().lower()
    if not err and status != "failed":
        return {}
    code = ""
    message = ""
    if isinstance(err, dict):
        code = str(err.get("code") or "")
        message = str(err.get("message") or "")
    elif isinstance(err, str):
        message = err
    elif err:
        message = str(err)[:300]
    return {
        "status": status or "failed",
        "code": code,
        "message": message,
        "id": str(data.get("id") or ""),
    }


# ─── Incomplete (truncated) Responses detection ─────────────────────────────


def _responses_incomplete_details(data: dict) -> dict:
    """Return details when a Responses body was truncated by the token limit.

    Some providers answer with HTTP 200 and ``status == "incomplete"`` when
    the generation stopped early - most commonly because the reasoning
    budget consumed the whole ``max_output_tokens`` allowance
    (``incomplete_details.reason == "max_output_tokens"``). In that case the
    message ``output`` may contain only reasoning items and NO user-visible
    text, so the raw body looks like a normal empty answer. Callers use this
    to distinguish "the model said nothing" from "the model ran out of
    budget" and to recover (e.g. lower the reasoning effort and retry).

    Returns {} when the body is not an incomplete response; otherwise a
    compact dict with the status, the machine-readable reason and the
    provider response id.
    """
    if not isinstance(data, dict):
        return {}
    status = str(data.get("status") or "").strip().lower()
    if status != "incomplete":
        return {}
    reason = ""
    details = data.get("incomplete_details")
    if isinstance(details, dict):
        reason = str(details.get("reason") or "").strip()
    usage = data.get("usage")
    output_tokens = 0
    if isinstance(usage, dict):
        output_tokens = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
    return {
        "status": status,
        "reason": reason,
        "id": str(data.get("id") or ""),
        "output_tokens": output_tokens,
    }


def _responses_failure_message(data: dict) -> str:
    """Return a user-facing message for a failed Responses body (or "")."""
    details = _responses_failure_details(data)
    if not details:
        return ""
    status, code = details["status"], details["code"]
    message = details["message"]
    text = f"Provider response status '{status}'"
    if code:
        text += f" ({code})"
    if message:
        text += f": {message}"
    return text


def _raise_responses_failure(data: dict, svc_name: str = "") -> None:
    """Raise ProviderResponseError when the body reports a failed generation."""
    details = _responses_failure_details(data)
    if not details:
        return
    raise ProviderResponseError(
        details["status"], details["code"], details["message"],
        service=svc_name or None, response_id=details["id"] or None,
    )


def _extract_openai_message_text(message: dict) -> str:
    """Extract text from an OpenAI-style assistant message.

    Handles both a plain string ``content`` and the content-block list
    shape some providers return; non-text blocks are ignored.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in (None, "text", "output_text"):
                continue
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)
    return ""

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
