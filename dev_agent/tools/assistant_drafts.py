"""Draft handling of the Assistant Creator answer.

The creation tool asks the Assistant Creator instruction for one JSON object
and then has to survive an answer that arrives fenced, wrapped in prose or
without the structured-output channel. The helpers here are pure: they parse,
unwrap and compose text, and never touch storage or the model.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List


def assistant_creation_schema() -> Dict[str, Any]:
    """Return the structured-output schema the Assistant Creator answers with.

    A fresh dict every call: the schema is handed to the model layer, which is
    free to normalise or mutate it.

    ``name`` is the user-defined name slot. The three optional translation
    slots let the creator propose an English, Russian and Chinese label next to
    it; they are optional so a creator that only knows one name still produces
    a valid answer.
    """
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "name_en": {"type": "string"},
            "name_ru": {"type": "string"},
            "name_zh": {"type": "string"},
            "description": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["name", "description", "prompt"],
        "additionalProperties": False,
    }


def looks_like_draft(data: Any) -> bool:
    """Tell whether a decoded value carries the two mandatory draft fields."""
    return isinstance(data, dict) and "name" in data and "prompt" in data


def parse_assistant_draft(text: str):
    """Extract the assistant draft from the Assistant Creator answer.

    Fallback levels, tried in order: a fenced code block, the raw answer as
    JSON, and the first {...} object found anywhere in the text. Parsers stay
    as a safety net for schema-less retries.

    Returns ``(draft, None)`` or ``(None, error_message)``.
    """
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        try:
            data = json.loads(fenced.group(1).strip())
            if looks_like_draft(data):
                return data, None
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        data = json.loads(text)
        if looks_like_draft(data):
            return data, None
    except (json.JSONDecodeError, TypeError):
        pass
    obj_match = re.search(r"\{.*?\}", text, re.DOTALL)
    if obj_match:
        try:
            data = json.loads(obj_match.group(0))
            if looks_like_draft(data):
                return data, None
        except (json.JSONDecodeError, TypeError):
            pass
    return None, "Could not parse Assistant Creator response as JSON."


def strip_prompt_fence(prompt_text: str) -> str:
    """Unwrap a markdown fence around the generated prompt, when present."""
    fence = re.search(r"```(?:markdown)?\s*([\s\S]*?)```", prompt_text)
    if fence:
        return fence.group(1).strip()
    return prompt_text


def build_creation_evaluation(assistant_name: str, assistant_desc: str,
                             complexity_str: str, service: str, model: str,
                             tools: List[str], log_msg: str,
                             needs_web_search: bool,
                             web_search_supported: bool) -> str:
    """Compose the human-readable report shown to the caller after creation."""
    service_info = f" | Service: {service} > {model}" if service and model else ""
    web_search_str = " + web_search" if ("web_search" in tools) else ""
    parts = [
        f"Created assistant '{assistant_name}' ({complexity_str}{web_search_str}).{service_info}",
    ]
    if assistant_desc:
        parts.append(f"Description: {assistant_desc}")
    parts.append(log_msg)
    if needs_web_search and not web_search_supported:
        parts.append(
            "Warning: web_search was requested but the selected provider "
            "does not support it. Enable a web-search-capable provider "
            "(e.g. YandexAI) in Settings to use search in this assistant."
        )
    return "\n".join(parts)


def assistant_create_kwargs(name: str, description: str, prompt_text: str,
                            profile: Dict[str, Any],
                            slots: Dict[str, str]) -> Dict[str, Any]:
    """Return the keyword arguments of a new-assistant save call.

    ``profile`` is the (service, model, tools, reasoning effort, tool-call
    budget) tuple resolved for the task. An empty name translation is passed
    as ``None`` so the repository stores no translation instead of a blank
    string.
    """
    service, model, tools, reasoning_effort, max_tool_calls = profile
    kwargs = {
        "name": name,
        "service": service,
        "model": model,
        "temperature": 0.7,
        "text": prompt_text,
        "description": description,
        "tools": tools,
        "max_tool_calls": max_tool_calls,
        "reasoning_effort": reasoning_effort,
    }
    for field in ("name_en", "name_ru", "name_zh"):
        kwargs[field] = slots.get(field) or None
    return kwargs


def assistant_update_kwargs(current: Dict[str, Any], overrides: Dict[str, Any],
                            slots: Dict[str, str]) -> Dict[str, Any]:
    """Return the keyword arguments of an assistant update call.

    A field passed as ``None`` keeps its stored value; the prompt lives in the
    ``text`` column, so an omitted ``prompt_text`` falls back to it. The name
    slots come from a merged slot dict, so an omitted translation keeps its
    stored text instead of being cleared.
    """
    current = current or {}
    overrides = overrides or {}

    def pick(field: str, source: str = "", default: Any = None) -> Any:
        value = overrides.get(field)
        if value is None:
            value = current.get(source or field)
        return default if value is None else value

    return {
        "name": slots["name"],
        "name_en": slots["name_en"],
        "name_ru": slots["name_ru"],
        "name_zh": slots["name_zh"],
        "service": pick("service", default=""),
        "model": pick("model", default=""),
        "temperature": float(pick("temperature", default=0.7)),
        "text": pick("prompt_text", source="text", default=""),
        "description": pick("description", default=""),
        "tools": pick("tools", default=[]),
        "max_tool_calls": pick("max_tool_calls"),
        "max_tokens": pick("max_tokens"),
        "reasoning_effort": pick("reasoning_effort"),
    }


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
