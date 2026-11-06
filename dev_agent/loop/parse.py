# -*- coding: utf-8 -*-
"""
dev_agent.loop.parse -- turning a model answer into tool calls.

The model does not always emit clean JSON: a call may arrive as a fenced block,
as raw DSML markup, with unbalanced braces, an unclosed string or raw newlines
inside a value. This module owns the outer layers of that recovery path: the
DSML dialect (markup extraction, argument unwrapping, required/oneof validation
and the hint explaining a rejection), the guards that mark a duplicate re-send
of a mutating tool, and ``parse_tool_calls``, which tries the layers in order
and, when nothing parses, produces the diagnostics a caller can show or log.

The JSON repair primitives the layers build on live in
``dev_agent.loop.json_repair``; they are re-exported here so the public surface
of this module stays the single entry point for parsing.
"""
from __future__ import annotations

import inspect
import json
import re
from typing import Any, Dict, List, Optional

from dev_agent.loop.json_repair import _collapse_value_escapes as _collapse_value_escapes
from dev_agent.loop.json_repair import _escape_raw_newlines_in_strings as _escape_raw_newlines_in_strings
from dev_agent.loop.json_repair import _extract_balanced_json_objects as _extract_balanced_json_objects
from dev_agent.loop.json_repair import _json_loads_lenient as _json_loads_lenient
from dev_agent.loop.json_repair import _json_parse_cause as _json_parse_cause
from dev_agent.loop.json_repair import _json_repair_cascade as _json_repair_cascade
from dev_agent.loop.json_repair import _normalize_call as _normalize_call
from dev_agent.loop.json_repair import _repair_stray_bracket as _repair_stray_bracket
from dev_agent.loop.json_repair import _repair_unclosed_braces as _repair_unclosed_braces
from dev_agent.loop.json_repair import _truncated_tool_json_segments as _truncated_tool_json_segments
from dev_agent.loop.json_repair import _try_layer_parse_chain as _try_layer_parse_chain
from dev_agent.loop.json_repair import _unbalanced_json_details as _unbalanced_json_details
from dev_agent.loop.json_repair import _unclosed_summary as _unclosed_summary
from dev_agent.loop.json_repair import _unescape_json_string_body as _unescape_json_string_body
from dev_agent.loop.json_repair import _unparsed_block_signature as _unparsed_block_signature
from dev_agent.loop.json_repair import _unparsed_tool_json_blocks as _unparsed_tool_json_blocks
from dev_agent.loop.json_repair import _unparsed_tool_json_diagnostics as _unparsed_tool_json_diagnostics

# Tools for which an identical re-send after a failure is always a mistake.
# These are file-write / DB-mutation operations: re-executing them with the
# SAME arguments cannot fix the first error, and a repeat may double-apply a
# change or create a duplicate DB record. Read-only tools, run_test/run_code
# and the idempotent backup/snapshot tools are deliberately EXCLUDED: after
# the code under test was fixed, re-running the same tests with the same
# arguments is the INTENDED workflow (the inputs are immutable, the world
# under test has changed).
_DUPLICATE_GUARD_TOOLS = {
    "propose_file",
    "apply_patch",
    "write_doc",
    "write_project_map",
    "create_assistant_for_task",
    "update_assistant_by_id",
    "create_skill_for_task",
    "update_skill_by_id",
    "save_orchestrator_instruction",
    "delete_orchestrator_instruction",
}


def _call_signature(tool: str, args: Any) -> str:
    """Stable fingerprint of a tool call for duplicate detection."""
    try:
        return json.dumps({"tool": tool, "args": args or {}},
                          ensure_ascii=False, sort_keys=True)
    except Exception:
        return f"{tool}:" + str(args or {})


def _coerce_dsml_param(value: str, attrs: str) -> Any:
    """Convert a DSML <parameter> value according to its XML attributes.

    By default (or when string="true") the raw text is returned.  If the
    parameter is explicitly typed (string="false", type="number",
    type="integer", type="boolean", ...) the value is parsed with json.loads
    so numbers/booleans arrive as native Python values instead of strings.
    """
    text_val = value.strip()

    def _attr(name: str) -> Optional[str]:
        m = re.search(
            rf"\b{name}\s*=\s*[\"\']([^\"\']*)[\"\']",
            attrs,
            re.IGNORECASE,
        )
        return m.group(1).strip().lower() if m else None

    str_attr = _attr("string")
    type_attr = _attr("type")
    if str_attr == "true":
        return text_val
    typed = str_attr == "false" or type_attr in (
        "number", "integer", "float", "int", "double", "boolean", "bool"
    )
    if typed:
        lowered = text_val.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        try:
            parsed = json.loads(text_val)
            if isinstance(parsed, bool):
                return parsed
            if isinstance(parsed, (int, float)):
                if isinstance(parsed, float) and parsed.is_integer():
                    return int(parsed)
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return text_val


# Marker prefix of the DeepSeek tool-call dialect. Models emit the reserved
# token before EVERY tag (``<｜｜DSML｜｜ parameter ...>``, U+FF5C fullwidth
# vertical bars), so a parser expecting a bare ``<parameter`` sees nothing.
_DSML_MARK = r"(?:[\s\u2502\uFF5C|:]*DSML[\s\u2502\uFF5C|:]*)?"

# Parameter names models use to wrap the whole argument object as JSON.
_DSML_ARGS_WRAPPERS = ("args", "arguments", "parameters", "kwargs")

# Raw-markup detector for the mixed JSON+DSML case: the fenced JSON part of a
# message parses and executes while DSML tags remain beside it. Fenced JSON is
# masked first, so DSML-looking text inside string arguments is not counted.
_JSON_FENCE_RE = re.compile(r"```[a-zA-Z0-9_\-]*\s*.*?```", re.DOTALL)
_DSML_USAGE_RE = re.compile(r"<\s*/?\s*[^<>]*DSML[^<>]*>", re.IGNORECASE)


def _uses_dsml_markup(text: str) -> bool:
    """Return True when *text* carries DSML tag markup outside JSON fences."""
    if not text:
        return False
    return bool(_DSML_USAGE_RE.search(_JSON_FENCE_RE.sub("", text)))

# Contract guidance returned to the model after a successfully executed
# fallback-dialect (DSML or mixed DSML+JSON) step: the call worked, but the
# only supported tool-call format is fenced JSON. The reminder travels with
# the tool results until the model switches to plain JSON.
_DSML_STOP_NOTICE = "Don't use DSML anymore, use only JSON for tool calling"


def _parse_dsml_json_value(raw: str, _depth: int = 0) -> Optional[Any]:
    """Parse a DSML parameter value that carries JSON, tolerating stray braces.

    Models often duplicate the closing brace of the argument object
    (``{"path": "a.py"}}``), which breaks ``json.loads``. Repair order: exact
    parse, then trim trailing ``}``/``]`` one at a time, then decode the first
    complete JSON value and ignore the trailing garbage.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    trimmed = text
    while trimmed and trimmed[-1] in "}]":
        trimmed = trimmed[:-1]
        try:
            return json.loads(trimmed)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    # Leading garbage: models may glue a stray attribute fragment in front of
    # the argument object (``string="false">false">{"path": "a.py"}``), so
    # decode from the first structural character. ``{``/``[`` win over ``"``:
    # a stray quote inside that fragment would otherwise decode into a bogus
    # string literal and mask the real argument object.
    for char in "{[":
        idx = text.find(char)
        if idx < 0:
            continue
        try:
            value, _end = json.JSONDecoder().raw_decode(text[idx:])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(value, (dict, list)):
            return value
    # A JSON *string* whose text is itself the argument object (double wrap).
    if _depth < 2:
        for idx, char in enumerate(text):
            if char != '"':
                continue
            try:
                value, _end = json.JSONDecoder().raw_decode(text[idx:])
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(value, str) and value.strip() != text:
                nested = _parse_dsml_json_value(value, _depth + 1)
                if isinstance(nested, (dict, list)):
                    return nested
    return None


def _unwrap_dsml_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a single ``args``/``arguments``-style wrapper parameter.

    Models usually emit the whole call as one JSON parameter
    (``<parameter name="args" string="false">{...}</parameter>``). Without
    unwrapping the tool would receive ``{"args": {...}}`` and reject the call
    as missing its required arguments.
    """
    if not isinstance(args, dict) or len(args) != 1:
        return args
    key = next(iter(args))
    if str(key).strip().lower() not in _DSML_ARGS_WRAPPERS:
        return args
    value = args[key]
    # Two-level wrapping occurs too: the wrapper holds a JSON *string* whose
    # text is the argument object, so unwrap until a dict surfaces (bounded).
    for _ in range(2):
        if isinstance(value, dict):
            return dict(value)
        if not isinstance(value, str):
            break
        parsed = _parse_dsml_json_value(value)
        if parsed is value:
            break
        value = parsed
        if isinstance(value, dict):
            return dict(value)
    return args


def _extract_dsml_calls(text: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    invoke_pat = re.compile(
        r"invoke\s+name\s*=\s*[\"\']([A-Za-z_][A-Za-z0-9_]*)[\"\']",
        re.IGNORECASE,
    )
    param_pat = re.compile(
        rf"<\s*{_DSML_MARK}\s*parameter\s+([^>]*)>(.*?)<\s*/\s*{_DSML_MARK}\s*parameter\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    attr_pat = re.compile(
        r"\bname\s*=\s*[\"\']([A-Za-z_][A-Za-z0-9_]*)[\"\']",
        re.IGNORECASE,
    )
    for m in invoke_pat.finditer(text):
        tool_name = m.group(1)
        tail = text[m.end():]
        close_match = re.search(r"</[^>]*invoke[^>]*>", tail, re.IGNORECASE)
        scope = tail[: close_match.start()] if close_match else tail[:2000]
        args: Dict[str, Any] = {}
        for pm in param_pat.finditer(scope):
            attrs = pm.group(1)
            name_m = attr_pat.search(attrs)
            if not name_m:
                continue
            args[name_m.group(1)] = _coerce_dsml_param(pm.group(2), attrs)
        results.append({"tool": tool_name, "args": _unwrap_dsml_args(args), "_dsml": True})
    return results


_DSML_KWARGS_REQUIRED: Dict[str, set] = {
    # Tools whose signature accepts **kwargs: their true required parameters
    # are not visible via inspect.signature, so they are listed explicitly here.
    "rag_search": {"slug", "query"},
    "rag_get_chunks": {"slug"},
}

# Tools where exactly one of the given parameters must be provided.
_DSML_ONEOF: Dict[str, set] = {
    "run_test": {"code", "path"},
    "run_code": {"code", "path"},
}


def _dsml_required_args(tool: str) -> Optional[set]:
    """Return the required argument names for *tool*, or None when unknown.

    Resolution order: (1) the static map for **kwargs-based tools,
    (2) WORKSPACE_TOOL_ARGS for workspace/orchestrator tools,
    (3) introspective inspection of ToolExecutor methods for core tools.

    Lazy imports keep this module free of circular-import problems
    (tool_executor itself imports dev_agent.agent_loop).
    """
    if tool in _DSML_KWARGS_REQUIRED:
        return set(_DSML_KWARGS_REQUIRED[tool])

    from dev_agent.tool_catalog_data import WORKSPACE_TOOL_ARGS
    spec = WORKSPACE_TOOL_ARGS.get(tool) if isinstance(WORKSPACE_TOOL_ARGS, dict) else None
    if spec is not None:
        return set(spec.get("required", set()))

    try:
        from dev_agent.tool_executor import ToolExecutor
    except ImportError:
        return None
    method = getattr(ToolExecutor, tool, None)
    if method is None or not callable(method):
        return None
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return None
    required: set = set()
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if p.default is inspect.Parameter.empty:
            required.add(p.name)
    return required


def _dsml_json_hint(tool: str, params: str) -> str:
    """Build the JSON-format guidance message for an invalid DSML call."""
    return (
        f"Invalid DSML/XML tool call for '{tool}': required parameter(s) "
        f"missing or empty: {params}. DSML/XML is a fallback format: it needs "
        f"every argument present (own <parameter> tags or one JSON object "
        f"under name='args'). "
        f"Emit the call as a single fenced JSON block instead, for example:\n"
        f"```json\n{{\"tool\": \"{tool}\", \"args\": {{...}}}}\n```"
    )


def _dsml_validation_error(tool: str, args: Dict[str, Any]) -> Optional[str]:
    """Return an error for an invalid DSML/XML tool call, or None when valid.

    A DSML call is invalid when a required parameter is missing, or (for
    run_test/run_code) when none of the mode parameters is present. The
    returned message directs the model to the fenced-JSON tool-call format -
    the only format the runner actually parses.
    """
    if not isinstance(args, dict):
        args = {}

    oneof = _DSML_ONEOF.get(tool)
    if oneof is not None and not (set(args) & oneof):
        return _dsml_json_hint(tool, " or ".join(sorted(oneof)))

    required = _dsml_required_args(tool)
    if required is None:
        return None
    missing = sorted(r for r in required if r not in args)
    if not missing:
        return None
    return _dsml_json_hint(tool, ", ".join(sorted(required)))


def _fallback_parse_propose_file(text: str) -> Optional[Dict[str, Any]]:
    if "propose_file" not in text:
        return None
    path_match = re.search(r'"path"\s*:\s*"([^"]*)"', text)
    if not path_match:
        return None
    path = path_match.group(1)
    content_start = text.find('"content":')
    if content_start == -1:
        return None
    after_key = text[content_start:]
    m = re.search(r'"content"\s*:\s*"', after_key)
    if not m:
        return None
    string_start = content_start + m.end()
    # Scan forward from the opening quote, honouring backslash escapes, to
    # find the UNESCAPED quote that closes the content value. The previous
    # regex started at the same place but matched the first internal quote
    # and silently trimmed the file head.
    value_start = string_start - 1
    i = value_start + 1
    bs = chr(92)
    q = chr(34)
    while i < len(text):
        ch = text[i]
        if ch == bs:
            i += 2
            continue
        if ch == q:
            break
        i += 1
    else:
        return None
    raw_string = text[value_start : i + 1]
    try:
        content_val = json.loads(raw_string)
    except json.JSONDecodeError:
        s = raw_string[1:-1]
        s = s.replace('\\\\', '\\x00')
        s = s.replace('\\"', '"')
        s = s.replace('\\n', '\\n')
        s = s.replace('\\t', '\\t')
        s = s.replace('\\x00', '\\\\')
        content_val = s
    if not content_val or len(content_val) < 10:
        return None
    return {"tool": "propose_file", "args": {"path": path, "content": content_val}}


def _repair_unclosed_tool_json(text: str) -> List[Dict[str, Any]]:
    """Recover tool calls whose JSON was cut off before the closing braces.

    Called only after normal extraction produced NO parseable tool call.
    Scans paired fenced blocks, truncated fence tails and (as the last
    resort) the bare message text; each segment that looks like a tool
    call is handed to _repair_unclosed_braces. Recovered calls carry the
    ``_json_repaired`` marker (number of appended closing braces) so the
    loop/UI can report that an automatic repair was applied.
    """
    segments: List[str] = []
    blocks = re.findall(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.DOTALL)
    for block in blocks:
        if '"tool"' in block and "{" in block:
            segments.append(block)
    for seg in _truncated_tool_json_segments(text):
        stripped_seg = re.sub(r"^`{3}[a-zA-Z0-9_-]*\s*", "", seg)
        if stripped_seg not in segments:
            segments.append(stripped_seg)
    if not segments and text.strip() and '"tool"' in text and "{" in text:
        segments.append(text)

    repaired_calls: List[Dict[str, Any]] = []
    for seg in segments:
        fixed = _repair_unclosed_braces(seg)
        if fixed is None:
            continue
        obj = _json_loads_lenient(fixed)
        if obj is None:
            continue
        call = _normalize_call(obj)
        if call is None:
            continue
        call["_json_repaired"] = fixed.count("}") - seg.count("}")
        repaired_calls.append(call)
    return repaired_calls


def _cascade_recover_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Last-resort recovery via the JSON repair cascade.

    Runs only after balanced-object extraction, DSML parsing and
    unclosed-brace repair all failed. Targets blocks whose structural
    quotes are over-escaped (a backslash before every quote) or whose
    body was wrapped as a JSON string - shapes the balanced-object
    scanner cannot even see. Each recovered call is verified as a valid
    tool call and carries the ``_json_repaired`` marker.
    """
    _fence = chr(96) * 3
    blocks = re.findall(_fence + r"[a-zA-Z0-9_-]*\s*(.*?)" + _fence, text, re.DOTALL)
    if not blocks:
        blocks = [text]
    recovered: List[Dict[str, Any]] = []
    for block in blocks:
        obj = _json_repair_cascade(block.strip())
        if obj is None:
            continue
        call = _normalize_call(obj)
        if call is None:
            continue
        call["_json_repaired"] = 1
        recovered.append(call)
    return recovered


def parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    fenced_blocks = re.findall(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.DOTALL)
    candidates: List[str] = []
    for block in fenced_blocks:
        candidates.extend(_extract_balanced_json_objects(block))
    if not candidates:
        candidates = _extract_balanced_json_objects(text)
    results: List[Dict[str, Any]] = []
    for raw in candidates:
        obj = _json_loads_lenient(raw)
        if obj is None:
            continue
        call = _normalize_call(obj)
        if call is not None:
            results.append(call)
    if not results:
        dsml_calls = _extract_dsml_calls(text)
        if dsml_calls and not any(call.get("args") for call in dsml_calls):
            # DSML markup was recognised but no argument could be recovered
            # (e.g. a truncated parameter block). Give the JSON repair cascade
            # a chance before handing the validator a call with empty args -
            # otherwise the whole step is lost to a format complaint.
            repaired = _repair_unclosed_tool_json(text)
            if not repaired:
                repaired = _cascade_recover_tool_calls(text)
            results.extend(repaired or dsml_calls)
        else:
            results.extend(dsml_calls)
    if not results:
        results = _repair_unclosed_tool_json(text)
    if not results:
        results = _cascade_recover_tool_calls(text)
    return results

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
