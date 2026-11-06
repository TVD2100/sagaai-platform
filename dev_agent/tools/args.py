"""Call-argument validation and numeric coercion."""
from __future__ import annotations

import inspect
from typing import Any, Dict, List, get_args, get_origin

_LEGACY_TOOL_ALIASES = {
    "page_fetch": "url_fetch",
    "list_skills": "list_assistants",
    "get_skill_by_id": "get_assistant_by_id",
    "update_skill_by_id": "update_assistant_by_id",
    "create_skill_for_task": "create_assistant_for_task",
    "detect_and_select_skill": "detect_and_select_assistant",
}


_UNKNOWN_ARGS_ERROR = (
    "Tool '{tool}' got unexpected argument(s): {unknown}. "
    "Use only the standard arguments documented in the system prompt "
    "(section 6) for this tool - do not invent parameter names."
)


_ARGS_ALLOWED_NAMES = {
    "get_history_messages": {"indices"},
    "rag_search": {"slug", "query", "top_k", "min_score"},
    "rag_get_chunks": {"slug", "chunk_ids", "source", "chunk_indices"},
    "web_search": {"query", "instructions", "allowed_domains", "search_context_size"},
    "url_fetch": {"url"},
    "run_test": {"code", "path", "confirmed_by_user"},
    "run_code": {"code", "path", "confirmed_by_user"},
}


def _unknown_args(method: Any, args: Dict[str, Any]) -> List[str]:
    """Return argument names not accepted by *method*'s signature.

    Uses ``inspect.signature``; callables without an introspectable signature
    (builtins, functools.partial, lambdas) are never flagged, so the dispatch
    falls through to the normal TypeError handling. Callables that accept
    ``**kwargs`` receive a truncated allow-list: only the names explicitly
    documented in the system prompt (``_ARGS_ALLOWED_NAMES``) are accepted,
    and any other unexpected name is reported so the LLM sees a structured
    error instead of a silent no-op.
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return []
    params = [p for p in sig.parameters.values() if p.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY
    )]
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    ok_names = {p.name for p in params}
    if has_var_kw:
        ok_names = _ARGS_ALLOWED_NAMES.get(getattr(method, "__name__", ""), ok_names) & ok_names
    return [k for k in args if k not in ok_names]


def _usage_string(tool_name: str, method: Any) -> str:
    """Build a short usage/signature hint for *method*.

    Formats required and optional keyword parameters from the introspected
    signature, e.g. ``Usage: apply_patch(path, edits, [note], [auto_apply], [fuzzy]).``
    When the signature is not introspectable, the tool name alone is used.
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return f"Usage: {tool_name}(...). See the '{tool_name}' entry in system prompt section 6."
    parts = []
    for p in sig.parameters.values():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if getattr(method, "__name__", "") in _ARGS_ALLOWED_NAMES and p.name not in _ARGS_ALLOWED_NAMES[method.__name__]:
            continue
        default = p.default
        has_default = default is not inspect.Parameter.empty
        parts.append(f"[{p.name}]" if has_default else p.name)
    if not parts:
        return f"Usage: {tool_name}(...). See the '{tool_name}' entry in system prompt section 6."
    return f"Usage: {tool_name}({', '.join(parts)})."


def _coerce_numeric_args(method: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Convert stringified numeric/bool arguments to real int/bool values.

    The LLM sometimes emits JSON like ``{"offset": "1182"}`` instead of
    ``{"offset": 1182}``, which then fails integer validation inside the
    tool method. This helper inspects the method's type annotations and
    coerces string values for parameters annotated as ``int``/``bool``
    (including ``Optional[int]``/``Optional[bool]``). Unrecognized strings
    are left untouched so the method's own validation still reports them.

    Returns the (possibly modified) arguments dict.
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return args
    for name, param in sig.parameters.items():
        if name not in args:
            continue
        value = args[name]
        if not isinstance(value, str):
            continue
        ann = param.annotation
        if ann is inspect.Parameter.empty:
            continue
        # Collect the concrete types from the annotation (unwraps Optional/Union).
        targets = set()
        origin = get_origin(ann)
        if origin is not None:
            targets.update(t for t in get_args(ann) if t in (int, bool))
        elif ann is int or ann is bool:
            targets.add(ann)
        elif isinstance(ann, str):
            stripped = ann.strip()
            if stripped in ("int", "Optional[int]", "Union[int, None]"):
                targets.add(int)
            elif stripped in ("bool", "Optional[bool]", "Union[bool, None]"):
                targets.add(bool)
        if not targets:
            continue
        if int in targets:
            try:
                args[name] = int(value)
            except (ValueError, TypeError):
                continue
        elif bool in targets:
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                args[name] = True
            elif low in ("false", "0", "no", "off"):
                args[name] = False
    return args


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
