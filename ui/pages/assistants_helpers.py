# -*- coding: utf-8 -*-
"""
ui.pages.assistants_helpers - pure helpers of the assistants page.

Nothing here touches Streamlit, session state or the assistant store: every
function turns its arguments into a value. The page module re-exports them, so
the page, ``ui.pages.skills`` and the tests keep resolving the same names.
"""

def _format_tools_badge(tools) -> str:
    """Return a human-readable list of an assistant's tools.

    Handles both plain string tool names (e.g. ``web_search``) and dict tool
    definitions: a dict is shown as its ``name`` (function tools) or its
    ``type`` otherwise.
    """
    if not tools:
        return ""
    parts = []
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name") or tool.get("type") or "tool"
            parts.append(str(name))
        else:
            parts.append(str(tool))
    return ", ".join(parts)


def _get_model_max_tokens_limit(svc_def: dict, model_id: str) -> int:
    """Return the max_tokens limit for a service definition and model.

    The model's own ``max_tokens`` wins, then the service-level
    ``max_tokens_default``, then the historical 200000 fallback.
    """
    if svc_def:
        for m in svc_def.get("models", []):
            if isinstance(m, dict) and m.get("id") == model_id:
                mt = m.get("max_tokens")
                if mt:
                    return int(mt)
        svc_default = svc_def.get("max_tokens_default")
        if svc_default:
            return int(svc_default)
    return 200000


def _temperature_bounds(svc_def: dict) -> tuple:
    """Return (min, max, step) of the temperature slider for a service.

    Services without explicit bounds (or with an empty range) fall back to
    0.0..2.0 step 0.05, preserving the old hardcoded behaviour.
    """
    t_min = float(svc_def.get("temp_min", 0.0))
    t_max = float(svc_def.get("temp_max", 2.0))
    t_step = float(svc_def.get("temp_step", 0.05))
    if t_min >= t_max:
        t_min, t_max = 0.0, 2.0
    return t_min, t_max, t_step


def _clamp_temperature(value: float, svc_def: dict) -> float:
    """Clamp a temperature value into the service definition's valid range."""
    t_min, t_max, _ = _temperature_bounds(svc_def)
    return max(t_min, min(t_max, float(value)))


def _model_choice_lists(svc_def: dict) -> tuple:
    """Return (model_list, model_ids, model_display) for a service definition."""
    model_list = []
    for m in svc_def.get("models", []):
        m_id = m["id"] if isinstance(m, dict) else m
        m_name = m.get("name", m_id) if isinstance(m, dict) else m
        model_list.append((m_id, m_name))
    model_ids = [m_id for m_id, _ in model_list]
    model_display = [f"{name} ({m_id})" for m_id, name in model_list]
    return model_list, model_ids, model_display


def _finalize_tools(tools_selected, rag_ids: list, svc_info: dict,
                    build_search, build_chunks) -> list:
    """Attach or drop the native RAG function tools for the selected bases.

    A ``yandex_iam`` assistant with bound bases gets rag_search and
    rag_get_chunks as function tools; once no base remains selected both are
    removed again, so the tool set follows the actual bindings.
    """
    final_tools = list(tools_selected or [])
    if svc_info.get("auth_type") != "yandex_iam":
        return final_tools
    for tool_name in ("rag_search", "rag_get_chunks"):
        has_rag_tool = any(
            isinstance(t, dict)
            and t.get("type") == "function"
            and t.get("name") == tool_name
            for t in final_tools
        )
        if rag_ids and not has_rag_tool:
            final_tools.append(build_search(rag_ids) if tool_name == "rag_search"
                               else build_chunks(rag_ids))
        elif not rag_ids:
            final_tools = [
                t for t in final_tools
                if not (
                    isinstance(t, dict)
                    and t.get("type") == "function"
                    and t.get("name") == tool_name
                )
            ]
    return final_tools


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
