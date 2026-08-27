"""
dev_agent.assistant_model_resolver - automatic model selection for assistant creation.

Provides:
- classify_assistant_requirements(): determine strong/weak complexity and web_search need.
- resolve_service_model_for_assistant(): pick service+model+tools based on classification.

Legacy function names (classify_skill_requirements, resolve_service_model_for_skill)
are kept as aliases for backward compatibility.
"""

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .llm_utils import call_llm_with_system


# --- Classification prompt ----------------------------------------------------

CLASSIFICATION_PROMPT = (
    "You are a task classifier. Analyse the user's assistant creation request "
    "and determine TWO things:\n\n"
    "1. **complexity**: Does this assistant need a STRONG or WEAK model?\n"
    "   - \"strong\": complex reasoning, code generation, deep analysis, "
    "creative writing, multi-step planning, technical documentation.\n"
    "   - \"weak\": simple formatting, basic translation, spell checking, "
    "chat-style replies, simple Q&A, lightweight tasks.\n\n"
    "2. **needs_web_search**: Does this assistant benefit from Internet access?\n"
    "   - true: the assistant needs real-time data, current events, fact-checking, "
    "news, latest documentation, or up-to-date information.\n"
    "   - false: the assistant works from the model's internal knowledge only.\n\n"
    "Return ONLY a JSON object with keys 'complexity' and 'needs_web_search'.\n"
    "Example: {\"complexity\": \"strong\", \"needs_web_search\": false}"
)


# --- YandexAI model sets ------------------------------------------------------

# Native structured-output schema for the classifier (Responses API text.format
# / OpenAI response_format). The fallback parsers below remain as a safety net.
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "complexity": {"type": "string", "enum": ["strong", "weak"]},
        "needs_web_search": {"type": "boolean"},
    },
    "required": ["complexity", "needs_web_search"],
    "additionalProperties": False,
}

YANDEX_PRO_MODELS = {"yandexgpt-5-pro", "yandexgpt-5.1"}
YANDEX_LITE_MODELS = {"yandexgpt-lite", "yandexgpt-5-lite"}


# --- Classification -----------------------------------------------------------


def classify_assistant_requirements(
    task: str,
    send_request_fn: Callable,
) -> Dict[str, Any]:
    """Classify an assistant creation task -> complexity (strong|weak) + web_search (bool)."""
    try:
        response = call_llm_with_system(
            send_request_fn,
            user_message=f"Classify this assistant creation request:\n\n{task}",
            system=CLASSIFICATION_PROMPT,
            history=[],
            json_schema=CLASSIFICATION_SCHEMA,
            json_schema_name="classification_result",
        )
    except Exception:
        return {"complexity": "strong", "needs_web_search": False}

    text = response.strip()

    # Try fenced JSON
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        try:
            data = json.loads(fenced.group(1).strip())
            return _validate_classification(data)
        except (json.JSONDecodeError, TypeError):
            pass

    # Try raw JSON
    try:
        data = json.loads(text)
        return _validate_classification(data)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try extracting a JSON object
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            data = json.loads(obj_match.group(0))
            return _validate_classification(data)
        except (json.JSONDecodeError, TypeError):
            pass

    # Fall back to simple keyword hints when JSON parsing fails, so the
    # classification remains useful even with a sloppy model response.
    task_lower = task.lower()
    web_hints = ("актуальн", "свеж", "новост", "up-to-date", "latest",
                 "current event", "real-time", "today", "сегодня", "факт-чекинг",
                 "fact-check", "news", "поиск", "search")
    strong_hints = ("код", "code", "анализ", "analysis", "сложн", "complex",
                    "multi-step", "documentation", "документац", "deep")
    needs_web = any(h in task_lower for h in web_hints)
    complexity = "strong" if any(h in task_lower for h in strong_hints) else "weak"
    return {"complexity": complexity, "needs_web_search": needs_web}


def _validate_classification(data: dict) -> Dict[str, Any]:
    complexity = data.get("complexity", "strong")
    if complexity not in ("strong", "weak"):
        complexity = "strong"
    needs = data.get("needs_web_search", False)
    if isinstance(needs, str):
        needs = needs.lower() in ("true", "yes", "1")
    return {"complexity": complexity, "needs_web_search": bool(needs)}


# --- Service/model resolution -------------------------------------------------


def _get_available_services_with_keys() -> List[Dict[str, Any]]:
    from core.services import get_services
    from core.config import has_key
    services = get_services()
    result = []
    for name, svc in services.items():
        if has_key(svc):
            result.append({"name": name, "data": svc})
    return result


def _get_first_model(service_data: dict) -> str:
    raw_models = service_data.get("models", [])
    if raw_models:
        first = raw_models[0]
        return first["id"] if isinstance(first, dict) else str(first)
    return ""


def _find_model_in_service(service_data: dict, model_ids: set) -> Optional[str]:
    raw_models = service_data.get("models", [])
    for m in raw_models:
        mid = m["id"] if isinstance(m, dict) else str(m)
        if mid in model_ids:
            return mid
    return None


def _parse_explicit_service_model(task: str, available: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    task_lower = task.lower()
    model_to_service = {}
    for entry in available:
        svc_name = entry["name"]
        svc_data = entry["data"]
        raw_models = svc_data.get("models", [])
        for m in raw_models:
            mid = m["id"] if isinstance(m, dict) else str(m)
            model_to_service[mid] = svc_name
    for mid, svc_name in model_to_service.items():
        if mid.lower() in task_lower:
            return svc_name, mid
    for entry in available:
        svc_name = entry["name"]
        svc_name_lower = svc_name.lower()
        if svc_name_lower in task_lower:
            first_model = _get_first_model(entry["data"])
            return svc_name, first_model if first_model else None
    return None, None


def _service_supports_web_search(service_data: dict) -> bool:
    tools_options = service_data.get("tools_options", []) or []
    for opt in tools_options:
        if isinstance(opt, dict) and opt.get("key") == "web_search":
            return True
        if isinstance(opt, str) and opt.strip() == "web_search":
            return True
    return False


def _pick_web_search_service(available: List[Dict[str, Any]], complexity: str) -> Tuple[str, str, Optional[str]]:
    """Choose a web-search-capable service and model.

    Prefers YandexAI (pro/lite set depending on complexity); otherwise picks
    the first available web-search-capable service. Returns (service, model,
    warning) where warning is None when the chosen provider actually supports
    the web_search tool.
    """
    yandex_data = None
    for entry in available:
        if entry["name"].lower() == "yandexai":
            yandex_data = entry["data"]
            break

    if yandex_data and _service_supports_web_search(yandex_data):
        if complexity == "strong":
            mdl = _find_model_in_service(yandex_data, YANDEX_PRO_MODELS)
        else:
            mdl = _find_model_in_service(yandex_data, YANDEX_LITE_MODELS)
        if mdl:
            return "YandexAI", mdl, None
        first_mdl = _get_first_model(yandex_data)
        if first_mdl:
            return "YandexAI", first_mdl, None

    # Any other web-search-capable provider.
    for entry in available:
        if _service_supports_web_search(entry["data"]):
            first_mdl = _get_first_model(entry["data"])
            if first_mdl:
                return entry["name"], first_mdl, None

    # No web-search-capable provider is configured: keep the requested
    # provider but warn that the web_search tool cannot be activated.
    if available:
        first = available[0]
        return first["name"], _get_first_model(first["data"]), (
            "web_search is required but no configured provider supports the "
            "web_search tool. The assistant is created without it; configure "
            "a web-search-capable provider (e.g. YandexAI) in Settings."
        )
    return "", "", "No configured services found."


def resolve_service_model_for_assistant(
    task: str,
    complexity: str,
    needs_web_search: bool,
) -> Tuple[str, str, List[str], str, bool, Optional[str], Optional[int]]:
    """Determine service, model, tools, log message and extra assistant settings.

    Logic:
      1. Explicit mention in task -> use that service/model; when web_search
         is needed but the requested provider does not support it, fall back
         to a web-search-capable provider and report a warning.
      2. No web_search -> pick from DevAgent settings (strong/weak model).
      3. Web_search needed -> YandexAI pro/lite (or another web-search-capable
         provider) with the web_search tool activated.
      4. Fallback: first available service.

    Returns (service, model, tools_list, log_message, web_search_supported,
             reasoning_effort, max_tool_calls).
    """
    from core.config import load_devagent_config
    from core.services import service_supports_reasoning_effort
    from core.services import default_reasoning_effort

    available = _get_available_services_with_keys()
    tools: List[str] = []

    svc_map: Dict[str, dict] = {}
    for entry in available:
        svc_map[entry["name"].lower()] = entry["data"]

    warning: Optional[str] = None

    # 1. Explicit mention.
    svc, mdl = _parse_explicit_service_model(task, available)
    if svc and mdl:
        svc_data = svc_map.get(svc.lower(), {})
        if needs_web_search:
            if _service_supports_web_search(svc_data):
                tools = ["web_search"]
            else:
                # The requested provider cannot search: try to switch to a
                # web-search-capable provider so the assistant still works.
                alt_svc, alt_mdl, alt_warning = _pick_web_search_service(available, complexity)
                if alt_mdl:
                    local_effort = _resolve_reasoning_effort(
                        svc_map.get(alt_svc.lower(), {}), default_reasoning_effort, service_supports_reasoning_effort)
                    return alt_svc, alt_mdl, ["web_search"], (
                        f"Requested service '{svc}' does not support web_search; "
                        f"switched to '{alt_svc}' > '{alt_mdl}'."
                    ), True, local_effort, 1
                warning = alt_warning
        tools_effort = _resolve_reasoning_effort(svc_data, default_reasoning_effort, service_supports_reasoning_effort)
        max_calls = max(1, int(svc_data.get("max_tool_calls_default", 3) or 3)) if tools else None
        return svc, mdl, tools, (
            f"Using explicitly requested service '{svc}' and model '{mdl}'."
        ), bool(tools), tools_effort, max_calls

    if svc:
        for entry in available:
            if entry["name"] == svc:
                mdl = _get_first_model(entry["data"])
                break
        if mdl:
            svc_data = svc_map.get(svc.lower(), {})
            if needs_web_search:
                if _service_supports_web_search(svc_data):
                    tools = ["web_search"]
                else:
                    alt_svc, alt_mdl, alt_warning = _pick_web_search_service(available, complexity)
                    if alt_mdl:
                        local_effort = _resolve_reasoning_effort(
                            svc_map.get(alt_svc.lower(), {}), default_reasoning_effort, service_supports_reasoning_effort)
                        return alt_svc, alt_mdl, ["web_search"], (
                            f"Requested service '{svc}' does not support web_search; "
                            f"switched to '{alt_svc}' > '{alt_mdl}'."
                        ), True, local_effort, 1
                    warning = alt_warning
            tools_effort = _resolve_reasoning_effort(svc_data, default_reasoning_effort, service_supports_reasoning_effort)
            max_calls = max(1, int(svc_data.get("max_tool_calls_default", 3) or 3)) if tools else None
            return svc, mdl, tools, (
                f"Using service '{svc}' (mentioned in request) with first model '{mdl}'."
            ), bool(tools), tools_effort, max_calls

    # 2. No web_search -> DevAgent settings.
    if not needs_web_search:
        dev_cfg = load_devagent_config()
        if complexity == "strong":
            svc = dev_cfg.get("strong_service", "")
            mdl = dev_cfg.get("strong_model", "")
        else:
            svc = dev_cfg.get("weak_service", "")
            mdl = dev_cfg.get("weak_model", "")

        if svc and mdl:
            svc_data = svc_map.get(svc.lower(), {})
            tools_effort = _resolve_reasoning_effort(svc_data, default_reasoning_effort, service_supports_reasoning_effort)
            return svc, mdl, [], (
                f"Using {complexity} model from DevAgent settings: '{svc}' > '{mdl}'."
            ), False, tools_effort, None
        if svc and not mdl:
            svc_data = svc_map.get(svc.lower(), {})
            first_mdl = _get_first_model(svc_data)
            if first_mdl:
                tools_effort = _resolve_reasoning_effort(svc_data, default_reasoning_effort, service_supports_reasoning_effort)
                return svc, first_mdl, [], (
                    f"Using {complexity} service from DevAgent settings: '{svc}' > '{first_mdl}' (first available)."
                ), False, tools_effort, None

    # 3. Web_search -> YandexAI (or another web-search-capable provider).
    if needs_web_search:
        svc, mdl, warning = _pick_web_search_service(available, complexity)
        if mdl:
            svc_data = svc_map.get(svc.lower(), {})
            local_effort = _resolve_reasoning_effort(svc_data, default_reasoning_effort, service_supports_reasoning_effort)
            web_ok = _service_supports_web_search(svc_data)
            tools_used = ["web_search"] if web_ok else []
            msg = (
                f"Web search assistant using '{svc}' > '{mdl}'. "
                f"Tool 'web_search' activated."
            ) if web_ok else (
                f"Web search needed but '{svc}' does not support the tool. "
                f"Assistant created without web_search."
            )
            return svc, mdl, tools_used, msg, web_ok, local_effort, 1 if web_ok else None
        return svc, mdl, [], warning or "No configured services found.", False, None, None

    # 4. Fallback.
    if not available:
        return "", "", [], (
            "No configured services found. Assistant created without service/model. "
            "Configure an API key in Settings to use it."
        ), False, None, None
    first = available[0]
    svc_name = first["name"]
    first_model = _get_first_model(first["data"])
    if needs_web_search and _service_supports_web_search(first["data"]):
        tools = ["web_search"]
    tools_effort = _resolve_reasoning_effort(first["data"], default_reasoning_effort, service_supports_reasoning_effort)
    max_calls = 1 if tools else None
    if first_model:
        return svc_name, first_model, tools, (
            f"Auto-selected service '{svc_name}' with model '{first_model}'."
        ), bool(tools), tools_effort, max_calls
    return svc_name, "", tools, (
        f"Auto-selected service '{svc_name}' but no models available."
    ), bool(tools), tools_effort, max_calls


def _resolve_reasoning_effort(
    service_data: dict,
    default_reasoning_effort: Callable,
    service_supports_reasoning_effort: Callable,
) -> Optional[str]:
    """Return the default reasoning effort for a service, or None when unsupported."""
    if not service_data or not service_supports_reasoning_effort(service_data):
        return None
    return default_reasoning_effort(service_data, strong=True) or None


# ─── Legacy aliases (old "skill" terminology) ────────────────────────────

classify_skill_requirements = classify_assistant_requirements
resolve_service_model_for_skill = resolve_service_model_for_assistant
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
