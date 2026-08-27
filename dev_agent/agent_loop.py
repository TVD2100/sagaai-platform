# -*- coding: utf-8 -*-
"""
dev_agent.agent_loop -- provider-independent orchestrator loop.

Implements the working cycle: send_request -> parse_tool_calls
-> dispatch -> collect results -> repeat until propose_file/prose/max_steps.

No assistant detection phase (no detect_and_select_assistant auto-call).
Assistant creation (create_assistant_for_task) is handled by the LLM in the normal
calling_llm cycle -- no separate task_classification phase.

Supports two execution modes:
  * run_agent_loop() -- blocking, runs all steps in one call (legacy).
  * step_agent_loop() -- single-step, returns after one iteration.
    The UI calls it repeatedly with AgentLoopState stored in session_state.

Strength-based model routing:
  Tools are classified as "strong" (requires powerful model) or "weak"
  (lightweight operations). step_agent_loop() receives TWO assistant dicts
  (strong_assistant, weak_assistant) and selects the appropriate one at each
  LLM call using classify_step_strength().

Economy mode:
  When enabled, only the last L messages are sent to the model together
  with a compact metadata message (workspace, web-search flag, history
  counts). No "important" messages and no full history index are injected.
  The model can request older messages on demand via the
  get_history_index / get_history_messages tools, which are backed by the
  full history stored in the dispatcher's core via set_history().

  The tail length L is read from the orchestrator configuration at
  call time (via core.orchestrators.get_economy_tail_messages).

  Cache-friendly economy mode:
    When economy_cache_enabled is True and economy_cache_multiplier > 1,
    the beginning of the sent window is kept stable while it fits within
    L * multiplier messages, so providers can apply prefix caching. See
    build_economy_context() for the full anchor-shifting logic.

Token tracking:
  Each send_request call collects usage via a usage_callback. Tokens are
  accumulated on the last assistant message in history as ``_tokens``
  dict {"in": prompt_tokens, "out": completion_tokens,
        "cache": cached_input_tokens}.
  The loop state additionally keeps running totals in total_tokens_in,
  total_tokens_out and total_tokens_cache.

Dangerous-operation confirmation:
  When a tool (run_code, run_test) is about to execute potentially
  dangerous code, the executor returns {confirmation_required: True}
  with a human-readable reason.  step_agent_loop() then stops and sets
  final_status="awaiting_confirmation", storing the pending call.
  The UI renders Allow/Deny buttons; on Allow it calls
  approve_confirmation(), which re-dispatches the tool with
  confirmed_by_user=True.

Sanitization confirmation:
  When prompt-injection protection replaces a tool-result payload with
  [SANITIZED], step_agent_loop() automatically stops and sets
  final_status="sanitized_required". The UI shows the reasons and offers
  "Allow viewing" (one-step bypass via approve_sanitized_content) or
  "Deny" (continue without the sanitized content).
"""
from __future__ import annotations

import inspect
import json
import os
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.api_layer import send_request
from core.dangerous import format_reasons_for_ui

# Strong tools
_STRONG_TOOLS: Set[str] = {
    "propose_file",
    "apply_patch",
    "run_test",
    "run_code",
    "create_assistant_for_task",
    "detect_and_select_assistant",
    # Legacy tool names (old "skill" terminology).
    "create_skill_for_task",
    "detect_and_select_skill",
}

# Weak tools (read-only)
_WEAK_TOOLS: Set[str] = {
    "read_file",
    "list_files",
    "read_doc",
    "verify_file",
    "current_workspace",
    "set_workspace",
    "set_target_file",
    "scan_folder",
    "assess_workspace",
    "build_project_map",
    "create_backup",
    "restore_backup",
    "show_history",
    "snapshot_all",
    "list_snapshots",
    "restore_all",
    "write_project_map",
    "write_doc",
    "list_assistants",
    "get_assistant_by_id",
    # Legacy tool names (old "skill" terminology).
    "list_skills",
    "get_skill_by_id",
    # Standardized skills library tools (read-only).
    "list_skills_library",
    "get_skill_folder",
    "get_skill_prompt",
    "get_skill_file",
    "list_instructions",
    "get_instruction",
    "web_search",
    "get_history_messages",
    "get_history_index",
}

_WEAK_PROSE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(done|completed|applied|finished|okay|sure|got it|understood)\b",
        r"^\s*(ok|yes|no|done)\.?\s*$",
        r"\b(step\s+\d+\s*(completed|verified|done|finished|applied))\b",
        r"\b(proceeding|moving on|now editing|next edit|starting step)\b",
        r"\b(working on (step|file|the next))\b",
        r"\b(all steps? (completed|done|finished))\b",
        r"\b(plan steps? (completed|done|finished))\b",
    )
]

_STRONG_PROSE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(plan|architecture|analysis|requirement|spec|design|trade.off|evaluate)\b",
        r"^#{1,3}\s",
        r"^\s*[-*+]\s",
        r"^\s*\d+\.\s",
    )
]

_FINAL_PHRASE = re.compile(r"all plan steps completed", re.IGNORECASE)

def _now_ts() -> str:
    """Return the current local timestamp for live history entries."""
    return datetime.now().isoformat()


_TOOL_RESULT_PREFIX = '{"tool_result"'
_AUTO_CONTINUE_PREFIX = "AUTO_CONTINUE:"

_CONFIRMATION_REQUEST_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(approve|approv|утверждаете|утверди|подтвердите|подтверждаете|одобряете|одобри)\b",
        r"\b(напишите|напиши|скажите|скажи|ответьте|ответь|выберите|выбери)\b",
        r"\b(shall i|should i|do you want|would you like|can i|may i)\b",
        r"\b(wait|stop|pause|жд[её]м|жд[уи]|останови)\b",
        r"\b(ok|go|yes|no|да|нет)\b.*[\?]",
        r"[\?].*\b(ok|go|yes|no|да|нет)\b",
        r"\(.*напишите.*\)",
        r"\(.*ok.*\)",
    )
]

_REQUIRES_USER_RESPONSE_RE = re.compile(
    r"_requires_user_response\s*:\s*(true|false)\b", re.IGNORECASE
)

_LOOP_STATUS_RE = re.compile(
    r'"loop_status"\s*:\s*"(continue|awaiting_user)"', re.IGNORECASE
)


def _parse_loop_status(text: str) -> Optional[str]:
    if not text:
        return None
    m = _LOOP_STATUS_RE.search(text)
    if m:
        return m.group(1).lower()
    return None


def _parse_requires_user_response(text: str) -> Optional[bool]:
    m = _REQUIRES_USER_RESPONSE_RE.search(text)
    if m:
        return m.group(1).lower() == "true"
    return None

_PROGRESS_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(step\s+\d+\s*(completed|verified|done|finished|applied))\b",
        r"\b(proceeding|moving on|now editing|next edit|starting step)\b",
        r"\b(working on (step|file|the next))\b",
        r"\b(all steps? (completed|done|finished))\b",
        r"\b(plan steps? (completed|done|finished))\b",
    )
]


def _prose_contains_progress(text: str) -> bool:
    if not text:
        return False
    if _FINAL_PHRASE.search(text):
        return False
    return any(p.search(text) for p in _PROGRESS_PATTERNS)


def _prose_looks_like_question(text: str) -> bool:
    if not text:
        return False
    if "?" not in text:
        return False
    return any(p.search(text) for p in _CONFIRMATION_REQUEST_PATTERNS)


_CONFIRMATION_REQUEST_NO_Q_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(подтвердите|подтверди|одобрите|одобри|утвердите|утверди)\b",
        r"\b(жду|жд[её]м|ожидаю|ожидаем)\b.*\b(подтвержд|одобр|approv|утвержд|confirm)",
        r"\b(требуется|нужно|необходимо)\b.*\b(подтвержд|одобр|approv|утвержд|confirm)",
        r"\b(waiting for|please approve|confirm the plan|awaiting your|need your approval|requesting your approval)\b",
    )
]


def _looks_like_confirmation_request(text: str) -> bool:
    """Return True when the assistant explicitly asks for approval
    without using a question mark (e.g. "Жду подтверждения.")."""
    if not text:
        return False
    return any(p.search(text) for p in _CONFIRMATION_REQUEST_NO_Q_PATTERNS)


def _prose_looks_weak(text: str) -> bool:
    if any(p.search(text) for p in _STRONG_PROSE_PATTERNS):
        return False
    return True

_PLAN_WORD_RE = re.compile(r"\b(plan|план)\b", re.IGNORECASE)

# ── Plan detection ───────────────────────────────────────────────────────────
# Detects when the assistant has presented a plan (numbered list or plan word).
# Used to block AUTO_CONTINUE from proceeding past a plan without user approval.

# Numbered items at the start of a line (strict, e.g. "\n1. Step")
_NUMBERED_ITEM_STRICT_RE = re.compile(r"(?:^|\n)\s*\d+\.\s+", re.IGNORECASE)
# Numbered items anywhere as a word boundary (soft, e.g. "Шаги: 1. первый")
_NUMBERED_ITEM_SOFT_RE = re.compile(r"\b\d+\.\s+", re.IGNORECASE)
# Bullet list items (markdown-style "- item" / "* item" / "• item")
_BULLET_ITEM_RE = re.compile(r"(?:^|\n)\s*[-*•]\s+", re.IGNORECASE)


def _looks_like_plan(text: str) -> bool:
    """Return True if `text` appears to contain a plan the user must approve.

    Detection rules:
    1. At least 3 numbered items at line starts → plan.
    2. At least 2 strict items + plan keyword → plan.
    3. At least 1 soft item + plan keyword → plan (handles inline plans).
    4. At least 2 bullet items + plan keyword → plan.
    5. At least 1 bullet item + 1 strict numbered item + plan keyword → plan.
    """
    if not text:
        return False
    n_strict = len(_NUMBERED_ITEM_STRICT_RE.findall(text))
    if n_strict >= 3:
        return True
    if n_strict >= 2 and _PLAN_WORD_RE.search(text):
        return True
    n_soft = len(_NUMBERED_ITEM_SOFT_RE.findall(text))
    if n_soft >= 1 and n_strict >= 1 and _PLAN_WORD_RE.search(text):
        return True
    n_bullet = len(_BULLET_ITEM_RE.findall(text))
    if n_bullet >= 2 and _PLAN_WORD_RE.search(text):
        return True
    if n_bullet >= 1 and n_strict >= 1 and _PLAN_WORD_RE.search(text):
        return True
    return False

# ── End plan detection ───────────────────────────────────────────────────────

_NON_ASCII_HYPHENS = re.compile(
    '[' +
    '\\u2010\\u2011\\u2012\\u2013\\u2014' +
    '\\u2212' +
    '\\uFE63\\uFF0D' +
    ']'
)


def normalize_hyphens(content: str) -> str:
    return _NON_ASCII_HYPHENS.sub('-', content)


def classify_step_strength(parsed_calls: List[Dict[str, Any]],
                           assistant_text: str) -> str:
    if parsed_calls:
        for call in parsed_calls:
            tool = call.get("tool", "")
            if tool in _STRONG_TOOLS:
                return "strong"
        return "weak"
    if assistant_text and not _prose_looks_weak(assistant_text):
        return "strong"
    return "weak"

# parse_tool_calls

_READ_TOOLS = {
    "read_file", "list_files", "scan_folder", "assess_workspace",
    "build_project_map", "read_doc", "current_workspace", "verify_file",
}


def _summarise_result(tool: str, result: Dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"{tool}: error -- {result.get('error', 'unknown')}"
    if tool == "read_file":
        path = result.get("path", "?")
        lines = result.get("total_lines", 0)
        return f"Read {path} ({lines} lines)"
    elif tool == "list_files":
        count = result.get("count", 0)
        return f"Listed {count} files"
    elif tool == "scan_folder":
        files = result.get("files", [])
        langs = result.get("languages", {})
        top_langs = ", ".join(f"{l}:{c}" for l, c in sorted(langs.items(), key=lambda x: -x[1])[:3])
        return f"Scanned workspace: {len(files)} files, {top_langs}"
    elif tool == "assess_workspace":
        state = result.get("state", "?")
        code_files = result.get("code_files", 0)
        return f"Assessment: {state} ({code_files} code files)"
    elif tool == "build_project_map":
        files = len(result.get("files", {}))
        return f"Project map built: {files} files"
    elif tool == "read_doc":
        doc = result.get("doc", "?")
        exists = result.get("exists", False)
        return f"Read doc '{doc}': {'found' if exists else 'not found'}"
    elif tool == "current_workspace":
        return f"Current workspace: {result.get('root', '?')}"
    elif tool == "verify_file":
        path = result.get("path", "?")
        ok = result.get("ok", False)
        return f"Verified {path}: {'OK' if ok else 'FAILED'}"
    return f"{tool} completed"


def _extract_balanced_json_objects(text: str) -> List[str]:
    objects: List[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    objects.append(text[start : i + 1])
                    start = -1
    return objects


def _unbalanced_json_details(raw: str) -> Dict[str, Any]:
    """Analyse *raw* for unclosed JSON constructs.

    Walks the text once, honouring string literals and escape sequences, and
    reports the remaining brace/bracket depth and whether a string literal
    was left open. Used to enrich truncated/unbalanced diagnostics with the
    CONCRETE unclosed constructs instead of a generic failure label.
    """
    brace_depth = 0
    bracket_depth = 0
    in_string = False
    escape = False
    for ch in raw:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            if brace_depth > 0:
                brace_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            if bracket_depth > 0:
                bracket_depth -= 1
    return {
        "brace_depth": brace_depth,
        "bracket_depth": bracket_depth,
        "unterminated_string": in_string,
        "has_content": bool(raw.strip()),
    }


def _unclosed_summary(details: Dict[str, Any]) -> str:
    """Human-readable list of the unclosed constructs found by
    _unbalanced_json_details, or a fallback note for a clean cut."""
    parts: List[str] = []
    if details["unterminated_string"]:
        parts.append("unterminated string literal")
    if details["brace_depth"]:
        parts.append(f"unclosed brace depth {details['brace_depth']}")
    if details["bracket_depth"]:
        parts.append(f"unclosed bracket depth {details['bracket_depth']}")
    if parts:
        return "unclosed constructs: " + ", ".join(parts)
    return "no unbalanced braces/quotes, the fence itself was cut off"


def _repair_unclosed_braces(raw: str) -> Optional[str]:
    """Return *raw* with missing closing braces appended, or None.

    Applies ONLY when the single structural problem is unclosed brace
    depth outside string literals: no unterminated string, no unbalanced
    brackets, and the patched text must parse into a valid tool-call
    object. Any other damage is left untouched so the caller keeps its
    normal diagnostics path.
    """
    if '"tool"' not in raw or "{" not in raw or raw.find("{") > raw.find('"tool"'):
        return None
    details = _unbalanced_json_details(raw)
    if (details["unterminated_string"] or not details["has_content"]
            or details["bracket_depth"] or details["brace_depth"] <= 0):
        return None
    repaired = raw + ("}" * details["brace_depth"])
    obj = _json_loads_lenient(repaired)
    if obj is None:
        return None
    if _normalize_call(obj) is None:
        return None
    return repaired


def _escape_raw_newlines_in_strings(text: str) -> str:
    """Escape RAW newline characters inside JSON string literals.

    LLMs frequently emit multi-line new/content values with literal
    line breaks; json.loads then fails. This helper converts raw
    newlines inside strings into escaped sequences, restoring valid
    JSON. Already-escaped sequences are left untouched.
    """
    out: List[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                out.append(ch)
                escape = False
            elif ch == '\\':
                out.append(ch)
                escape = True
            elif ch == '"':
                in_string = False
                out.append(ch)
            elif ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                continue  # normalize CRLF to LF
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
    return "".join(out)


def _json_loads_lenient(raw: str) -> Optional[Any]:
    """json.loads with a repair fallback for raw newlines in strings."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _escape_raw_newlines_in_strings(raw)
        if repaired != raw:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                return None
    return None


def _truncated_tool_json_segments(text: str) -> List[str]:
    """Return tail segments that look like TRUNCATED fenced tool-call JSON.

    The paired-fence regex used by _unparsed_tool_json_blocks cannot see a
    block whose closing fence was cut off by a token-limited emission.
    Detected by an odd number of fence marks after the last paired block,
    combined with the double-quoted tool marker and a brace opener.
    """
    tool_marker = '"tool"'
    brace = '{'
    if tool_marker not in text or brace not in text:
        return []
    pair_re = re.compile(r'```[a-zA-Z0-9_-]*(.*?)```', re.DOTALL)
    last_pair_end = 0
    for m in pair_re.finditer(text):
        last_pair_end = m.end()
    tail = text[last_pair_end:]
    if tail.count('```') % 2 != 1:
        return []
    fence_start = tail.find('```')
    seg = tail[fence_start:]
    if tool_marker in seg and brace in seg:
        return [seg]
    return []

def _unparsed_tool_json_blocks(text: str) -> int:
    """Count fenced JSON blocks that look like tool calls but cannot be
    parsed even with the lenient repair, or truncated before the closing
    fence. Used to stop silent spins when the model emits malformed tool-call
    JSON."""
    count = 0
    for block in re.findall(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.DOTALL):
        if '"tool"' not in block or "{" not in block:
            continue
        objs = _extract_balanced_json_objects(block)
        if objs and not any(_json_loads_lenient(o) is not None for o in objs):
            count += 1
    trunc = _truncated_tool_json_segments(text)
    if trunc:
        # A cut-off tail is a single failure; count it and stop, otherwise
        # the unbalanced-tail branch below would double-count it.
        return count + len(trunc)
    if "```" in text and '"tool"' in text and "{" in text:
        if not _extract_balanced_json_objects(text):
            count += 1
    return count


def _json_parse_cause(raw: str) -> str:
    """Return a human-readable reason why *raw* is not valid JSON.

    When the plain parse fails, the lenient repair is also attempted so the
    cause reflects the state after repair (the most actionable diagnosis).
    """
    first_cause = ""
    try:
        json.loads(raw)
        return ""
    except Exception as e:
        first_cause = f"{type(e).__name__}: {e}"
    repaired = _escape_raw_newlines_in_strings(raw)
    if repaired != raw:
        try:
            json.loads(repaired)
            return ("repaired by escaping raw newlines in string values "
                    f"(original error: {first_cause})")
        except Exception as e:
            return f"{first_cause}; after newline repair: {type(e).__name__}: {e}"
    return first_cause


def _unparsed_tool_json_diagnostics(text: str) -> List[Dict[str, str]]:
    """Return per-block diagnostics for tool-call JSON that did not parse.

    Each entry: {"snippet": first 220 chars of the failing block/object,
    "cause": parse-failure reason}. This gives the model actionable details
    (the exact JSONDecodeError position/message) instead of the generic
    "could not be parsed" text, so it can decide between fixing one field
    and switching tools.
    """
    diagnostics: List[Dict[str, str]] = []
    blocks = re.findall(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.DOTALL)
    tool_blocks = [b for b in blocks if '"tool"' in b and "{" in b]
    if len(tool_blocks) > 1:
        # The runtime accepts exactly one tool call per message. Multiple
        # fenced tool-call blocks in one message can never all be executed;
        # say so explicitly instead of reporting only the parse state.
        diagnostics.append({
            "snippet": tool_blocks[0][:220],
            "cause": (
                f"the message contains {len(tool_blocks)} fenced tool-call blocks; "
                "the runtime accepts exactly ONE tool call per message - "
                "send each tool call in its own message and wait for the result"
            ),
        })
    for seg in _truncated_tool_json_segments(text):
        seg_details = _unbalanced_json_details(seg)
        diagnostics.append({
            "snippet": seg[:220],
            "cause": (f"the fenced JSON block is TRUNCATED (no closing fence before "
                      f"the end of the message); {_unclosed_summary(seg_details)} - "
                      "re-emit the complete call")
        })
    for block in tool_blocks:
        objs = _extract_balanced_json_objects(block)
        if not objs:
            block_details = _unbalanced_json_details(block)
            diagnostics.append({
                "snippet": block[:220],
                "cause": ("no balanced JSON object found in the fenced block: "
                          + _unclosed_summary(block_details)),
            })
            continue
        for obj in objs:
            if _json_loads_lenient(obj) is not None:
                continue
            cause = _json_parse_cause(obj) or "unknown parse failure"
            diagnostics.append({"snippet": obj[:220], "cause": cause})
    return diagnostics


def _unparsed_block_signature(diagnostics: List[Dict[str, str]]) -> str:
    """Fingerprint the diagnostics list so IDENTICAL repeats can be caught.

    Two malformed blocks that produce the same snippet set are considered
    the same mistake; the parse-error branch uses the signature to warn the
    model instead of re-feeding the identical broken call.
    """
    if not diagnostics:
        return ""
    import hashlib
    h = hashlib.sha256()
    for d in diagnostics:
        h.update((d.get("snippet") or "").encode("utf-8", "replace"))
        h.update(b":")
    return h.hexdigest()[:40]


def _normalize_call(obj: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    name = obj.get("tool") or obj.get("name") or obj.get("tool_name")
    if not name or not isinstance(name, str):
        return None
    reserved = {"tool", "name", "tool_name", "args", "arguments", "parameters"}
    if isinstance(obj.get("args"), dict):
        args = obj["args"]
    elif isinstance(obj.get("arguments"), dict):
        args = obj["arguments"]
    elif isinstance(obj.get("parameters"), dict):
        args = obj["parameters"]
    else:
        args = {k: v for k, v in obj.items() if k not in reserved}
    if not isinstance(args, dict):
        args = {}
    return {"tool": name, "args": args}


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


def _extract_dsml_calls(text: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    invoke_pat = re.compile(
        r"invoke\s+name\s*=\s*[\"\']([A-Za-z_][A-Za-z0-9_]*)[\"\']",
        re.IGNORECASE,
    )
    param_pat = re.compile(
        r"<parameter\s+([^>]*)>(.*?)</\s*parameter\s*>",
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
        results.append({"tool": tool_name, "args": args, "_dsml": True})
    return results


_DSML_KWARGS_REQUIRED: Dict[str, set] = {
    # Tools whose signature accepts **kwargs: their true required parameters
    # are not visible via inspect.signature, so they are listed explicitly here.
    "rag_search": {"slug", "query"},
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

    try:
        from dev_agent.universal_agent import WORKSPACE_TOOL_ARGS
    except ImportError:
        WORKSPACE_TOOL_ARGS = {}
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
        f"missing or empty: {params}. DSML/XML/HTML tags are never parsed. "
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
    string_match = re.search(r'("(?:[^"\\]|\\.)*")', text[string_start:])
    if not string_match:
        return None
    raw_string = string_match.group(1)
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
        results.extend(_extract_dsml_calls(text))
    if not results:
        results = _repair_unclosed_tool_json(text)
    return results

# AgentResult


@dataclass
class AgentResult:
    status: str = ""
    text: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    staged_path: Optional[str] = None
    diff: Optional[str] = None
    steps: int = 0
    staged_new_text: Optional[str] = None
    staged_tool: Optional[str] = None
    applied: bool = False
    discarded: bool = False

# ── Task-state external memory (per-thread journal) ───────────────────────────
# For every task the agent keeps a per-thread task-state journal
# (.dev_agent/task_states/TASK_STATE__<thread_id>.md: Active Task +
# Task History). The Active Task is injected as a system message at the
# END of every request, so the agent always knows where it is even when
# economy mode truncates history.

def _maybe_task_state_context() -> Optional[str]:
    """Return the task-state block for context injection, or None."""
    try:
        from dev_agent import task_state as ts
        return ts.task_state_for_context()
    except Exception:
        return None


def _with_task_state(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Append the current task state to *history* when it exists."""
    ts_text = _maybe_task_state_context()
    if not ts_text:
        return history
    out = list(history)
    out.append({"role": "system", "content": ts_text, "hidden": True})
    return out


# ── Thread context (thread_id + thread files dir) ─────────────────────────
# Injected alongside the task state so the model always knows the folder
# where "non-project" artifacts of the current dialog must be saved (see the
# "Current thread artifacts" section of the system prompt).

def _maybe_thread_context(state: "AgentLoopState") -> Optional[str]:
    """Return the thread context block for injection, or None."""
    tid = (getattr(state, "thread_id", "") or "").strip()
    if not tid:
        return None
    try:
        from core.paths import get_thread_dir
        files_dir = os.path.join(get_thread_dir(tid), "files")
    except Exception:
        files_dir = ""
    lines = [
        "## CURRENT THREAD ARTIFACTS DIR",
        "",
        f"thread_id: {tid}",
    ]
    if files_dir:
        lines.append(f"thread_files_dir: {files_dir}")
    lines.append(
        "Files created during this task that do not belong to any existing "
        "or newly created project folder must be saved into thread_files_dir."
    )
    return "\n".join(lines)


def _with_thread_context(history: List[Dict[str, Any]], state: "AgentLoopState") -> List[Dict[str, Any]]:
    """Append the current thread context to *history* when set."""
    ctx_text = _maybe_thread_context(state)
    if not ctx_text:
        return history
    out = list(history)
    out.append({"role": "system", "content": ctx_text, "hidden": True})
    return out


# Economy mode helpers

_CATEGORY_PLAN = "plan"
_CATEGORY_TOOL_CALL = "tool_call"
_CATEGORY_TOOL_RESULT = "tool_result"
_CATEGORY_PROGRESS = "progress"
_CATEGORY_ERROR = "error"
_CATEGORY_SUMMARY = "summary"
_CATEGORY_USER_TASK = "user_task"
_CATEGORY_OTHER = "other"


def _make_short_summary(msg: Dict[str, Any], category: str) -> str:
    content = str(msg.get("content", ""))
    if content.startswith(_TOOL_RESULT_PREFIX):
        try:
            data = json.loads(content)
            tr = data.get("tool_result", {})
            tool_name = tr.get("tool", "?")
            ok = tr.get("ok", False)
            if not ok:
                return f"Error in {tool_name}: {tr.get('error', 'unknown')[:60]}"
            path = tr.get("path", "")
            if path:
                return f"File {path} {'applied' if tr.get('applied') else 'updated'}"
            return f"{tool_name} completed"
        except Exception:
            pass
        return content[:80]
    if content.startswith(_AUTO_CONTINUE_PREFIX):
        return content[len(_AUTO_CONTINUE_PREFIX):].strip()[:80]
    if msg.get("role") == "assistant":
        lines = [l.strip() for l in content.split("\n") if l.strip()
                 and not l.strip().startswith("```") and not l.strip().startswith("{")]
        if lines:
            return lines[0][:80]
        return content[:80]
    return content[:80]


def _classify_message(msg: Dict[str, Any]) -> str:
    role = msg.get("role", "")
    content = str(msg.get("content", ""))
    if role == "user":
        if content.startswith(_TOOL_RESULT_PREFIX):
            try:
                data = json.loads(content)
                tr = data.get("tool_result", {})
                if not tr.get("ok"):
                    return _CATEGORY_ERROR
            except Exception:
                pass
            return _CATEGORY_TOOL_RESULT
        if content.startswith(_AUTO_CONTINUE_PREFIX):
            return _CATEGORY_PROGRESS
        return _CATEGORY_USER_TASK
    if _FINAL_PHRASE.search(content):
        return _CATEGORY_PROGRESS
    if _prose_contains_progress(content):
        return _CATEGORY_PROGRESS
    if re.search(r"\b(plan|план)\b", content, re.IGNORECASE) and re.search(r"\d+\.", content):
        return _CATEGORY_PLAN
    return _CATEGORY_OTHER


def _index_message(msg: Dict[str, Any], index: int, category: str) -> None:
    msg["_index"] = index
    msg["_category"] = category
    msg["_summary"] = _make_short_summary(msg, category)


def _get_economy_tail_messages() -> int:
    """Return the economy tail length from the orchestrator configuration.

    Reads the value from the active orchestrator config. Falls back to
    the default from the external JSON config file if not stored yet.
    """
    try:
        from core.orchestrators import get_economy_tail_messages as _g
        return _g()
    except Exception:
        # Bootstrap / import-time fallback
        from core.config import get_default_economy_tail_messages
        return get_default_economy_tail_messages()


def _economy_meta_key(state: "AgentLoopState") -> str:
    """Return a key identifying the meta-parameters of the economy window.

    Used to detect changes in economy-mode environment (workspace or
    web-search flag) that require resetting the cache anchor.
    """
    ws_info = state.workspace_info or "(workspace not yet queried)"
    web_search = "enabled" if state.web_search_enabled else "disabled"
    return f"{ws_info}|{web_search}"


def _make_meta_msg(state: "AgentLoopState") -> Dict[str, Any]:
    """Build the system message prepended in economy mode.

    Contains only metadata that is static within an economy window so the
    prefix stays cacheable: workspace and web-search flag. History counts and
    the pointer to get_history_index / get_history_messages belong in the main
    system prompt and are intentionally omitted here (see §13 of the system
    prompt).
    """
    ws_info = state.workspace_info or "(workspace not yet queried)"
    web_search = "enabled" if state.web_search_enabled else "disabled"
    content = (
        "ECONOMY MODE: ENABLED\n"
        f"Current workspace: {ws_info}\n"
        f"Web search: {web_search}\n"
    )
    return {
        "role": "system",
        "content": content,
        "hidden": True,
        "_index": -2,
        "_category": "system_notice",
    }


def build_economy_context(state: "AgentLoopState") -> List[Dict[str, Any]]:
    """Build a reduced history for economy mode.

    Two strategies are supported:

    * Legacy mode (``economy_cache_enabled=False`` or
      ``economy_cache_multiplier <= 1``): only the last *tail* messages are
      kept. The prefix changes on every request, which prevents providers
      from applying prefix caching.

    * Cache-friendly mode (``economy_cache_enabled=True`` and
      ``economy_cache_multiplier > 1``): the beginning of the sent window is
      kept stable so the provider can cache the repeated prefix. While
      ``len(history) <= tail * multiplier`` the full thread is sent from the
      start. Once the window is exceeded, the anchor is fixed at
      ``len(history) - tail`` and stays stable until the sent part grows
      beyond ``tail * multiplier``, at which point it shifts again to
      ``len(history) - tail``. If the workspace or web-search meta parameters
      change, the anchor is reset to ``max(0, total - tail)`` and a new
      accumulation cycle begins.

    Both modes prepend a compact metadata system message. In cache-friendly
    mode that message contains only static fields so it does not break the
    prefix cache.
    """
    history = state.history
    if state.economy_tail_messages is not None and int(state.economy_tail_messages) > 0:
        tail = int(state.economy_tail_messages)
    else:
        tail = _get_economy_tail_messages()
    total = len(history)

    result: List[Dict[str, Any]] = [_make_meta_msg(state)]

    if not state.economy_cache_enabled or state.economy_cache_multiplier <= 1:
        # Legacy mode: keep only the tail.
        if total <= tail:
            result.extend(history)
        else:
            result.extend(history[-tail:])
        return result

    # Cache-friendly mode.
    multiplier = max(1, state.economy_cache_multiplier)
    window = tail * multiplier

    meta_key = _economy_meta_key(state)
    if state.economy_anchor is not None and state.economy_meta_key != meta_key:
        # Meta context changed mid-window. Exception: the first detection of
        # the workspace (previous meta key had no workspace info yet) is the
        # normal initial query, not a workspace switch - keep accumulating.
        if "(workspace not yet queried)" not in (state.economy_meta_key or ""):
            state.economy_anchor = max(0, total - tail)
    state.economy_meta_key = meta_key

    if state.economy_anchor is None:
        # First call: send the full thread while it fits, otherwise start
        # from the tail.
        if total <= window:
            state.economy_anchor = 0
        else:
            state.economy_anchor = max(0, total - tail)
    else:
        sent_len = total - state.economy_anchor
        if sent_len > window:
            # The stable prefix has grown too long; shift the anchor.
            state.economy_anchor = max(0, total - tail)

    result.extend(history[state.economy_anchor:])
    return result


def carry_over_economy_cache(source: Optional["AgentLoopState"],
                             target: "AgentLoopState") -> None:
    """Copy cache-window anchor info from *source* to *target*.

    Called when a fresh AgentLoopState is created for a follow-up user
    message in the same thread. Without this, every new turn would start
    the cache-friendly window from the bare tail (tail messages) and the
    prefix would never grow to tail * multiplier, defeating prefix caching.
    """
    if source is None:
        return
    target.workspace_info = getattr(source, "workspace_info", "")
    target.economy_tail_messages = getattr(source, "economy_tail_messages", None)
    target.economy_anchor = getattr(source, "economy_anchor", None)
    target.economy_meta_key = getattr(source, "economy_meta_key", "")


def economy_cache_to_dict(state: "AgentLoopState") -> Dict[str, Any]:
    """Serialize cache-friendly economy window parameters.

    The UI persists these in session_state so the anchor survives
    loop-state clearing between user turns (terminal statuses like
    done/applied/error clear the loop state, but the anchor must survive
    so the cache-friendly window keeps growing to tail * multiplier).
    """
    return {
        "workspace_info": getattr(state, "workspace_info", ""),
        "economy_tail_messages": getattr(state, "economy_tail_messages", None),
        "economy_anchor": getattr(state, "economy_anchor", None),
        "economy_meta_key": getattr(state, "economy_meta_key", ""),
        "web_search_enabled": getattr(state, "web_search_enabled", False),
    }


def apply_economy_cache(data: Optional[Dict[str, Any]],
                        target: "AgentLoopState") -> None:
    """Restore cache-friendly economy parameters onto *target* from *data*.

    Called when a fresh AgentLoopState is created for a follow-up user
    message and the previous loop_state was cleared. Restoring the anchor
    lets the cache-friendly window continue growing to tail * multiplier
    instead of restarting from the bare tail on every turn.

    ``web_search_enabled`` is intentionally NOT restored here: the target
    state is created with the live session value, and the economy meta key
    will detect a web-search toggle and reset the anchor appropriately.
    """
    if not data:
        return
    target.workspace_info = data.get("workspace_info") or ""
    target.economy_tail_messages = data.get("economy_tail_messages") or None
    target.economy_anchor = data.get("economy_anchor")
    target.economy_meta_key = data.get("economy_meta_key") or ""

# AgentLoopState


@dataclass
class AgentLoopState:
    """Mutable state of a running agent loop."""

    phase: str = "init"

    task: str = ""
    max_steps: int = 100
    auto_apply: bool = False

    strong_assistant: Dict[str, Any] = field(default_factory=dict)
    weak_assistant: Dict[str, Any] = field(default_factory=dict)

    economy_mode: bool = False
    web_search_enabled: bool = False
    next_index: int = 0

    workspace_info: str = ""  # Cached workspace info for economy mode
    # Active thread id (a new thread is created by the UI per dialog).
    # When set, the thread context (thread_id + thread_files_dir) is
    # injected into the LLM context on every step.
    thread_id: str = ""

    # Cache-friendly economy mode settings.
    economy_cache_enabled: bool = False
    economy_cache_multiplier: int = 1
    # Explicit economy tail length for THIS orchestrator (per-state override;
    # None falls back to the orchestrator config read at call time).
    economy_tail_messages: Optional[int] = None
    economy_anchor: Optional[int] = None
    economy_meta_key: str = ""

    steps: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)
    user_message: str = ""
    file_context: str = ""
    file_name: str = ""

    pending_staged_path: Optional[str] = None
    pending_action: Optional[str] = None
    staged_new_text: Optional[str] = None
    staged_tool: Optional[str] = None

    assistant_text: str = ""
    parsed_calls: List[Dict[str, Any]] = field(default_factory=list)

    final_status: str = ""
    final_text: str = ""
    final_staged_path: Optional[str] = None
    final_diff: Optional[str] = None
    final_staged_new_text: Optional[str] = None
    final_staged_tool: Optional[str] = None
    final_applied: bool = False
    final_discarded: bool = False

    error_message: str = ""

    consecutive_errors: int = 0

    # Fingerprint of the last unparsed tool-call JSON block; an IDENTICAL
    # repeat only yields a stronger warning, never a hard stop.
    last_unparsed_signature: str = ""
    # Last dispatched (tool, stripped args-JSON). Used to detect the model
    # re-sending the SAME tool call after a failure; identical repeats are
    # blocked before dispatch, with guidance to switch down the fallback
    # chain (no hard stop). Cleared after any successful call.
    last_failed_signature: str = ""

    # Flag to remember that we just presented a plan and should not auto-continue.
    # Set by the parsing phase when loop_status=awaiting_user + plan detected.
    plan_presented: bool = False

    # Pending dangerous-operation confirmation.
    # When a tool returns confirmation_required=True, the loop stops and
    # stores the call here so the UI can render Allow/Deny buttons.
    pending_confirmation_tool: Optional[str] = None
    pending_confirmation_args: Optional[Dict[str, Any]] = None
    pending_confirmation_reasons: List[str] = field(default_factory=list)
    pending_confirmation_code: str = ""
    pending_confirmation_result: Optional[Dict[str, Any]] = None

    # Pending sanitization confirmation.
    # When prompt-injection protection substitutes a tool-result payload
    # with [SANITIZED], the loop stops and these fields hold the details.
    sanitized_events: List[Dict[str, Any]] = field(default_factory=list)
    enable_injection_protection: bool = True
    injection_protection_bypassed: bool = False
    # File paths the user explicitly approved for viewing. _protect_history
    # keeps tool results for these paths visible on subsequent protected
    # turns instead of asking for approval again.
    sanitized_approved_paths: Set[str] = field(default_factory=set)

    # ── Token tracking (cumulative across all LLM calls in this loop) ───────
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tokens_cache: int = 0
    # Per-step tokens tracked via callback; attached to assistant message.
    # Dict keys: "in", "out", "cache" (the latter may be absent for providers
    # that do not report cached input tokens).
    last_step_tokens: Optional[Dict[str, int]] = None


_MAX_CONSECUTIVE_ERRORS = 3

# step_agent_loop


def step_agent_loop(
    state: AgentLoopState,
    *,
    dispatcher: Any,
    lang: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> AgentLoopState:
    """Execute one iteration of the agent loop."""
    if hasattr(dispatcher, 'core') and hasattr(dispatcher.core, '_in_agent_loop'):
        dispatcher.core._in_agent_loop = True
    try:
        return _step_agent_loop_impl(state, dispatcher=dispatcher, lang=lang, on_event=on_event)
    finally:
        if hasattr(dispatcher, 'core') and hasattr(dispatcher.core, '_in_agent_loop'):
            dispatcher.core._in_agent_loop = False


_WORKSPACE_TOOLS = {"current_workspace", "set_workspace", "set_target_file"}


def _update_workspace_info(state: AgentLoopState, result: Dict[str, Any]) -> None:
    """Update workspace_info from the result of a workspace tool call."""
    if result.get("ok"):
        root = result.get("root", "")
        mode = result.get("single_file_mode", False)
        target = result.get("target_file", "")
        parts = [f"root={root}"]
        if mode:
            parts.append(f"single_file_mode=true")
            if target:
                parts.append(f"target_file={target}")
        state.workspace_info = ", ".join(parts)


def _step_agent_loop_impl(
    state: AgentLoopState,
    *,
    dispatcher: Any,
    lang: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> AgentLoopState:

    # Publish the active dialog thread id so the per-thread task-state
    # journal layer writes/reads TASK_STATE__<thread_id>.md for THIS dialog.
    try:
        from dev_agent import config as _dagent_config
        _dagent_config.ACTIVE_THREAD_ID = (getattr(state, "thread_id", "") or "").strip()
    except Exception:
        pass

    # Requirement: the journal is written for EVERY task. Auto-create a
    # scaffold journal for this thread when it does not exist yet, so the
    # file always exists even if the model never calls task_state_init.
    # Idempotent and cheap; failures must never break the agent loop.
    try:
        from dev_agent import task_state as _ts
        _ts.ensure_task_state_file()
    except Exception:
        pass

    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    strength = classify_step_strength(state.parsed_calls, state.assistant_text)

    def _effective_assistant() -> Dict[str, Any]:
        assistant = state.strong_assistant
        if strength == "weak" and state.weak_assistant.get("model"):
            assistant = state.weak_assistant
        return assistant

    def _process_pending_action() -> AgentLoopState:
        if state.pending_action and state.pending_staged_path:
            if state.pending_action == "apply":
                if state.staged_new_text:
                    dispatcher.dispatch(
                        "propose_file",
                        {"path": state.pending_staged_path, "content": normalize_hyphens(state.staged_new_text), "note": ""}
                    )
                apply_result = dispatcher.dispatch("apply_edit", {"path": state.pending_staged_path, "note": ""})
                if not apply_result.get("ok"):
                    err_msg = apply_result.get("error", "apply_edit: not ok")
                    emit({"type": "error", "error": f"apply_edit failed: {err_msg}"})
                    state.phase = "error"
                    state.error_message = f"apply_edit failed: {err_msg}"
                    return state
                now_ts = _now_ts()
                apply_user_msg = {"role": "user", "content": f"apply_edit {state.pending_staged_path}", "ts": now_ts}
                _index_message(apply_user_msg, state.next_index, _classify_message(apply_user_msg))
                state.next_index += 1
                state.history.append(apply_user_msg)
                apply_done_msg = {"role": "assistant", "content": f"Applied: {apply_result}", "ts": now_ts}
                _index_message(apply_done_msg, state.next_index, _classify_message(apply_done_msg))
                state.next_index += 1
                state.history.append(apply_done_msg)
                emit({"type": "applied", "tool": "apply_edit", "result": apply_result, "path": state.pending_staged_path})
                emit({"type": "final", "text": f"Applied {state.pending_staged_path}.", "status": "applied"})
                state.steps = 1
                state.phase = "done"
                state.final_status = "applied"
                state.final_text = f"Applied {state.pending_staged_path}"
                state.final_applied = True
                return state
            elif state.pending_action == "discard":
                discard_result = dispatcher.dispatch("discard_edit", {"path": state.pending_staged_path})
                now_ts = _now_ts()
                discard_user_msg = {"role": "user", "content": f"discard_edit {state.pending_staged_path}", "ts": now_ts}
                _index_message(discard_user_msg, state.next_index, _classify_message(discard_user_msg))
                state.next_index += 1
                state.history.append(discard_user_msg)
                discard_done_msg = {"role": "assistant", "content": f"Discarded: {discard_result}", "ts": now_ts}
                _index_message(discard_done_msg, state.next_index, _classify_message(discard_done_msg))
                state.next_index += 1
                state.history.append(discard_done_msg)
                emit({"type": "tool_result", "tool": "discard_edit", "result": discard_result})
                emit({"type": "final", "text": f"Discarded {state.pending_staged_path}.", "status": "discarded"})
                state.steps = 1
                state.phase = "done"
                state.final_status = "discarded"
                state.final_text = f"Discarded {state.pending_staged_path}"
                state.final_discarded = True
                return state
            elif state.pending_action == "ok":
                state.user_message = "OK, proceed."
            else:
                state.user_message = state.pending_action
        state.pending_action = None
        state.pending_staged_path = None
        state.phase = "calling_llm"
        return state

    def _recoverable_error(err_msg: str, tool_name: str, tool_results_parts: List[str]) -> AgentLoopState:
        state.consecutive_errors += 1
        if state.consecutive_errors > _MAX_CONSECUTIVE_ERRORS:
            hard_msg = (
                f"I've encountered the same type of error {_MAX_CONSECUTIVE_ERRORS} times in a row: {err_msg}. "
                "I can't resolve this automatically. Manual intervention is needed."
            )
            emit({"type": "error", "error": hard_msg})
            state.phase = "error"
            state.error_message = hard_msg
            return state

        err_result = {"ok": False, "error": err_msg}
        emit({"type": "tool_result", "tool": tool_name, "result": err_result})
        tool_results_parts.append(
            json.dumps({"tool_result": err_result}, ensure_ascii=False)
        )
        state.user_message = "\n".join(tool_results_parts)
        state.phase = "calling_llm"
        return state

    if state.phase in ("done", "error"):
        return state

    if state.phase == "init":
        if state.pending_action and state.pending_staged_path:
            return _process_pending_action()
        state.user_message = state.task if state.task else ""
        state.phase = "calling_llm"
        return state

    if state.phase == "pending_action":
        return _process_pending_action()

    if state.phase == "calling_llm":
        if state.steps >= state.max_steps:
            emit({"type": "stopped_max_steps"})
            state.phase = "done"
            state.final_status = "stopped_max_steps"
            return state

        state.steps += 1

        try:
            assistant = _effective_assistant()
            emit({"type": "phase", "phase": "calling_llm", "step": state.steps,
                  "strength": strength, "model": assistant.get("model")})

            if state.economy_mode:
                effective_history = build_economy_context(state)
            else:
                effective_history = state.history

            # ── External memory: append the current task state (when present)
            # so the model always sees architecture/plan/progress without
            # digging through history. ───────────────────────────────────────
            effective_history = _with_task_state(effective_history)
            # ── Thread context: tell the model where non-project artifacts
            # of this dialog are saved (thread_id + thread_files_dir). ─────
            effective_history = _with_thread_context(effective_history, state)

            if hasattr(dispatcher, 'core') and hasattr(dispatcher.core, 'set_history'):
                dispatcher.core.set_history(state.history)

            # ── Token tracking: capture usage via callback ───────────────────
            state.last_step_tokens = None
            def _on_tokens(t: Dict[str, int]) -> None:
                cache = t.get("cache", 0) or 0
                state.last_step_tokens = {"in": t["in"], "out": t["out"], "cache": cache}
                state.total_tokens_in += t["in"]
                state.total_tokens_out += t["out"]
                state.total_tokens_cache += cache

            # ── Sanitization detection: capture events via callback ──────────
            sanitized_events: List[Dict[str, Any]] = []
            def _on_sanitized(info: dict) -> None:
                sanitized_events.append(info)

            state.assistant_text = send_request(
                state.user_message, assistant,
                file_context=state.file_context,
                history=effective_history,
                lang=lang,
                usage_callback=_on_tokens,
                enable_injection_protection=state.enable_injection_protection,
                sanitized_callback=_on_sanitized,
                sanitized_approved_paths=state.sanitized_approved_paths,
            )
            state.file_context = ""

            # ── If prompt-injection protection just sanitized a payload, stop ──
            # and ask the user whether to view the withheld content.
            if sanitized_events:
                state.sanitized_events = sanitized_events
                emit({"type": "sanitized_detected",
                      "events": sanitized_events, "step": state.steps})
                state.phase = "done"
                state.final_status = "sanitized_required"
                state.final_text = state.assistant_text
                return state

            # After a one-step bypass, restore the original protection setting.
            if state.injection_protection_bypassed:
                state.injection_protection_bypassed = False
                state.enable_injection_protection = True

        except Exception as exc:
            emit({"type": "error", "error": str(exc)})
            state.phase = "error"
            state.error_message = str(exc)
            return state

        if str(state.user_message).strip():
            entry = {"role": "user", "content": state.user_message, "ts": _now_ts()}
            if state.file_name:
                entry["file_name"] = state.file_name
                entry["file_chars"] = len(state.file_context) if state.file_context else 0
            stripped = str(state.user_message).strip()
            if stripped.startswith(_TOOL_RESULT_PREFIX) or stripped.startswith(_AUTO_CONTINUE_PREFIX):
                entry["hidden"] = True
            cat = _classify_message(entry)
            _index_message(entry, state.next_index, cat)
            state.next_index += 1
            state.history.append(entry)
            state.file_name = ""

        assistant_msg = {"role": "assistant", "content": state.assistant_text, "ts": _now_ts()}
        # Attach per-step token info to assistant message
        if state.last_step_tokens:
            assistant_msg["_tokens"] = state.last_step_tokens
        cat = _classify_message(assistant_msg)
        _index_message(assistant_msg, state.next_index, cat)
        state.next_index += 1
        state.history.append(assistant_msg)
        emit({"type": "assistant_text", "text": state.assistant_text, "step": state.steps})

        state.phase = "parsing"
        return state

    if state.phase == "parsing":
        emit({"type": "phase", "phase": "parsing", "step": state.steps})
        state.parsed_calls = parse_tool_calls(state.assistant_text)

        if not state.parsed_calls:
            fallback = _fallback_parse_propose_file(state.assistant_text)
            if fallback is not None:
                state.parsed_calls = [fallback]
                emit({"type": "phase", "phase": "executing",
                      "tool": "propose_file", "step": state.steps,
                      "fallback": True})
                state.phase = "executing"
                return state

        if not state.parsed_calls and _unparsed_tool_json_blocks(state.assistant_text) > 0:
            # The model emitted tool-call JSON that failed to parse even after
            # repair. Feed the error back instead of silently spinning.
            diagnostics = _unparsed_tool_json_diagnostics(state.assistant_text)
            sig = _unparsed_block_signature(diagnostics)
            state.consecutive_errors += 1
            if state.consecutive_errors > _MAX_CONSECUTIVE_ERRORS:
                hard_msg = (
                    "Tool-call JSON could not be parsed "
                    + str(_MAX_CONSECUTIVE_ERRORS)
                    + " times in a row. Manual intervention is needed."
                )
                emit({"type": "error", "error": hard_msg})
                state.phase = "error"
                state.error_message = hard_msg
                return state
            repeated = bool(sig and sig == state.last_unparsed_signature)
            if sig:
                state.last_unparsed_signature = sig
            warn_prefix = (
                "WARNING: you just resent the SAME broken call. DO NOT repeat "
                "it - switch to another tool in the fallback chain."
            )
            base_msg = (
                "Tool-call JSON was detected but could not be parsed. "
                "Read the cause below, check that the JSON syntax is correct "
                "and the function call and arguments match the system prompt, "
                "fix it in ONE retry; if unclear or the retry fails, switch "
                "immediately down the fallback chain."
            )
            err_result = {
                "ok": False,
                "error": warn_prefix + " " + base_msg if repeated else base_msg,
                "diagnostics": diagnostics,
            }
            emit({"type": "tool_result", "tool": "parse_tool_calls",
                  "result": err_result, "step": state.steps})
            state.user_message = json.dumps({"tool_result": err_result}, ensure_ascii=False)
            state.phase = "calling_llm"
            return state

        if not state.parsed_calls:
            # No tool calls found -- determine final status from prose.
            loop_status = _parse_loop_status(state.assistant_text)
            if loop_status == "continue":
                emit({"type": "phase", "phase": "auto_continuing",
                      "reason": "loop_status: continue", "step": state.steps})
                state.user_message = _AUTO_CONTINUE_PREFIX + " Continue to the next step."
                state.phase = "calling_llm"
                return state
            elif loop_status == "awaiting_user":
                if _looks_like_plan(state.assistant_text):
                    state.plan_presented = True
                final_status = "awaiting_user"
            elif _parse_requires_user_response(state.assistant_text) is False:
                emit({"type": "phase", "phase": "auto_continuing",
                      "reason": "_requires_user_response: false", "step": state.steps})
                state.user_message = _AUTO_CONTINUE_PREFIX + " Continue to the next step."
                state.phase = "calling_llm"
                return state
            elif _parse_requires_user_response(state.assistant_text) is True:
                final_status = "awaiting_user"
            elif _FINAL_PHRASE.search(state.assistant_text):
                final_status = "done"
            elif _prose_looks_like_question(state.assistant_text):
                final_status = "awaiting_user"
            elif _looks_like_plan(state.assistant_text) or _looks_like_confirmation_request(state.assistant_text):
                if _looks_like_plan(state.assistant_text):
                    state.plan_presented = True
                emit({"type": "phase", "phase": "awaiting_plan_approval",
                      "reason": "plan or confirmation request detected", "step": state.steps})
                final_status = "awaiting_user"
            elif state.auto_apply:
                prev_msg = str(state.user_message).strip()
                if state.plan_presented:
                    emit({"type": "phase", "phase": "awaiting_plan_approval",
                          "reason": "plan was presented; ignoring AUTO_CONTINUE", "step": state.steps})
                    final_status = "awaiting_user"
                elif prev_msg.startswith(_AUTO_CONTINUE_PREFIX):
                    emit({"type": "phase", "phase": "auto_continuing",
                          "reason": "previous turn was auto-continue", "step": state.steps})
                    state.user_message = _AUTO_CONTINUE_PREFIX + " Continue to the next step."
                    state.phase = "calling_llm"
                    return state
                elif prev_msg.startswith(_TOOL_RESULT_PREFIX):
                    state.user_message = _AUTO_CONTINUE_PREFIX + " Continue to the next step."
                    state.phase = "calling_llm"
                    return state
                elif _prose_contains_progress(state.assistant_text):
                    emit({"type": "phase", "phase": "auto_continuing",
                          "reason": "progress phrase detected", "step": state.steps})
                    state.user_message = _AUTO_CONTINUE_PREFIX + " Continue to the next step."
                    state.phase = "calling_llm"
                    return state
                else:
                    final_status = "awaiting_user"
            else:
                final_status = "awaiting_user"

            emit({"type": "final", "text": state.assistant_text, "status": final_status})
            state.phase = "done"
            state.final_status = final_status
            state.final_text = state.assistant_text
            return state

        state.phase = "executing"
        return state

    if state.phase == "executing":
        tool_results_parts: List[str] = []
        _USER_GATED = {"apply_edit", "discard_edit"}
        all_ok = True

        for call in state.parsed_calls:
            tool = call["tool"]
            if call.get("_dsml"):
                # DSML fallback call: validate it against the real tool
                # signature BEFORE dispatch. Invalid calls are NOT executed;
                # instead the model gets direct JSON-format guidance.
                dsml_error = _dsml_validation_error(tool, call.get("args") or {})
                if dsml_error is not None:
                    result = {"ok": False, "error": dsml_error, "dsml_invalid": True}
                    all_ok = False
                    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
                    tool_results_parts.append(
                        json.dumps({"tool_result": result}, ensure_ascii=False)
                    )
                    continue
            if tool == "propose_file":
                # Duplicate-guard for write tools: never re-send the identical
                # failing call. Read/test tools are not guarded (see set above).
                if _call_signature(tool, call.get("args")) == state.last_failed_signature:
                    result = {
                        "ok": False,
                        "error": (
                            f"The same {tool} call with the same arguments already failed. "
                            "Do NOT repeat it. Switch to the next tool in the fallback chain."
                        ),
                        "duplicate_call_blocked": True,
                    }
                    all_ok = False
                    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
                    tool_results_parts.append(
                        json.dumps({"tool_result": result}, ensure_ascii=False)
                    )
                    continue
                emit({"type": "phase", "phase": "executing", "tool": tool, "step": state.steps})
                emit({"type": "tool_call", "tool": tool, "args": call["args"], "step": state.steps})
                args_with_auto = dict(call["args"])
                if "content" in args_with_auto and isinstance(args_with_auto["content"], str):
                    args_with_auto["content"] = normalize_hyphens(args_with_auto["content"])
                args_with_auto.setdefault("auto_apply", state.auto_apply)
                result = dispatcher.dispatch(tool, args_with_auto)
                if not result.get("ok"):
                    all_ok = False
                    state.last_failed_signature = _call_signature(tool, call.get("args"))
                    tool_results_parts.append(
                        json.dumps({"tool_result": result}, ensure_ascii=False)
                    )
                    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
                else:
                    applied = result.get("applied", False)
                    if applied:
                        state.last_failed_signature = ""
                        emit({"type": "applied", "tool": tool, "result": result, "path": result.get("path", ""), "step": state.steps})
                        tool_results_parts.append(
                            json.dumps({"tool_result": result}, ensure_ascii=False)
                        )
                    else:
                        path = result.get("path", "")
                        diff = result.get("diff", "")
                        emit({"type": "awaiting_approval", "path": path, "diff": diff, "step": state.steps})
                        state.phase = "done"
                        state.final_status = "awaiting_approval"
                        state.final_text = state.assistant_text
                        state.final_staged_path = path
                        state.final_diff = diff
                        state.final_staged_new_text = result.get("new_text")
                        state.final_staged_tool = tool
                        return state
                continue
            elif tool == "apply_patch":
                # Duplicate-guard for write tools: never re-send the identical
                # failing call. Read/test tools are not guarded (see set above).
                if _call_signature(tool, call.get("args")) == state.last_failed_signature:
                    result = {
                        "ok": False,
                        "error": (
                            f"The same {tool} call with the same arguments already failed. "
                            "Do NOT repeat it. Switch to the next tool in the fallback chain."
                        ),
                        "duplicate_call_blocked": True,
                    }
                    all_ok = False
                    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
                    tool_results_parts.append(
                        json.dumps({"tool_result": result}, ensure_ascii=False)
                    )
                    continue
                emit({"type": "phase", "phase": "executing", "tool": tool, "step": state.steps})
                emit({"type": "tool_call", "tool": tool, "args": call["args"], "step": state.steps})
                args_with_auto = dict(call["args"])
                edits = args_with_auto.get('edits') or []
                if isinstance(edits, list):
                    args_with_auto['edits'] = [{k: (normalize_hyphens(v) if k in ('old', 'new') and isinstance(v, str) else v) for k, v in e.items()} if isinstance(e, dict) else e for e in edits]
                args_with_auto.setdefault("auto_apply", state.auto_apply)
                result = dispatcher.dispatch(tool, args_with_auto)
                if not result.get("ok"):
                    all_ok = False
                    state.last_failed_signature = _call_signature(tool, call.get("args"))
                    tool_results_parts.append(
                        json.dumps({"tool_result": result}, ensure_ascii=False)
                    )
                    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
                else:
                    applied = result.get("applied", False)
                    if applied:
                        state.last_failed_signature = ""
                        emit({"type": "applied", "tool": tool, "result": result, "path": result.get("path", ""), "step": state.steps})
                        tool_results_parts.append(
                            json.dumps({"tool_result": result}, ensure_ascii=False)
                        )
                    else:
                        path = result.get("path", "")
                        diff = result.get("diff", "")
                        emit({"type": "awaiting_approval", "path": path, "diff": diff, "step": state.steps})
                        state.phase = "done"
                        state.final_status = "awaiting_approval"
                        state.final_text = state.assistant_text
                        state.final_staged_path = path
                        state.final_diff = diff
                        state.final_staged_new_text = result.get("new_text")
                        state.final_staged_tool = tool
                        return state
                continue
            elif tool in _USER_GATED and not state.auto_apply:
                err_result = {
                    "ok": False,
                    "error": (
                        f"In manual mode you must NOT call {tool} yourself. "
                        "After propose_file, stop and let the user click "
                        "\u2705 (apply) or \u274c (discard). Do not narrate the edit as 'applied'."
                    ),
                }
                all_ok = False
                emit({"type": "tool_result", "tool": tool, "result": err_result, "step": state.steps})
                tool_results_parts.append(
                    json.dumps({"tool_result": err_result}, ensure_ascii=False)
                )
            else:
                # Duplicate-guard: only for write/DB-mutation tools. Read and
                # test tools may legitimately be re-sent with the same args
                # (e.g. run_test after the code under test was fixed).
                call_sig = _call_signature(tool, call.get("args"))
                if tool in _DUPLICATE_GUARD_TOOLS and call_sig == state.last_failed_signature:
                    result = {
                        "ok": False,
                        "error": (
                            f"The same {tool} call with the same arguments already failed. "
                            "Do NOT repeat it. Switch to the next tool in the fallback chain."
                        ),
                        "duplicate_call_blocked": True,
                    }
                    all_ok = False
                    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
                    tool_results_parts.append(
                        json.dumps({"tool_result": result}, ensure_ascii=False)
                    )
                    state.last_failed_signature = call_sig
                    continue
                emit({"type": "phase", "phase": "executing", "tool": tool, "step": state.steps})
                emit({"type": "tool_call", "tool": tool, "args": call["args"], "step": state.steps})
                # ── Non-blocking tool failure ──────────────────────────────
                # A tool that RAISES (bad argument, internal error) must NOT
                # kill the loop. Catch the exception, feed a structured ok=False
                # result back to the LLM and continue; the orchestrator reads
                # the cause and retries with correct arguments on its own.
                try:
                    result = dispatcher.dispatch_json(call)
                except TypeError as exc:
                    result = {
                        "ok": False,
                        "error": (
                            f"Tool '{tool}' was called with invalid arguments: {exc}. "
                            "Use ONLY the documented arguments for this tool "
                            "(from the system prompt) and re-issue the call."
                        ),
                        "invalid_arguments": True,
                    }
                except Exception as exc:
                    result = {
                        "ok": False,
                        "error": (
                            f"Tool '{tool}' failed with an internal error: "
                            f"{type(exc).__name__}: {exc}. Read this cause, "
                            "fix it and continue - the loop will not stop."
                        ),
                        "tool_raised_exception": True,
                    }

                # ── Safety gate: dangerous operation needs user confirmation ──
                if result.get("confirmation_required"):
                    da = (result.get("danger_assessment") or {})
                    reasons = da.get("reasons", [])
                    code_snippet = da.get("code_snippet", "")
                    emit({
                        "type": "confirmation_required",
                        "tool": tool,
                        "args": call["args"],
                        "reasons": reasons,
                        "code_snippet": code_snippet,
                        "step": state.steps,
                    })
                    state.phase = "done"
                    state.final_status = "awaiting_confirmation"
                    state.final_text = state.assistant_text
                    state.pending_confirmation_tool = tool
                    state.pending_confirmation_args = dict(call["args"])
                    state.pending_confirmation_reasons = reasons
                    state.pending_confirmation_code = code_snippet
                    state.pending_confirmation_result = result
                    return state

                if not result.get("ok"):
                    all_ok = False
                    if tool in _DUPLICATE_GUARD_TOOLS:
                        state.last_failed_signature = call_sig
                    result["next_action"] = (
                        "Fix this ONCE, then switch to the next fallback tool. Never "
                        "resend the same problematic call more than twice. Hard "
                        "errors only for genuine blockers (manual-mode staging, "
                        "task-direction conflict)."
                    )
                else:
                    state.last_failed_signature = ""
                if tool in _WORKSPACE_TOOLS:
                    _update_workspace_info(state, result)
                if tool in _READ_TOOLS:
                    summary = _summarise_result(tool, result)
                    emit({"type": "tool_result", "tool": tool, "result": {**result, "summary": summary}, "step": state.steps})
                else:
                    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
                tool_results_parts.append(
                    json.dumps({"tool_result": result}, ensure_ascii=False)
                )

        if all_ok:
            state.consecutive_errors = 0

        state.user_message = "\n".join(tool_results_parts)
        state.phase = "calling_llm"
        return state

    state.phase = "error"
    state.error_message = f"Unknown phase: {state.phase}"
    return state


def approve_pending_confirmation(state: AgentLoopState, dispatcher: Any,
                                 lang: Optional[str] = None,
                                 on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Re-dispatch a pending dangerous operation with confirmed_by_user=True.

    Called by the UI when the user clicks "Allow". Returns a new state with
    the tool executed and the loop continuing (or error if the call fails).
    """
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    tool = state.pending_confirmation_tool
    args = dict(state.pending_confirmation_args or {})
    if not tool:
        state.phase = "error"
        state.error_message = "No pending confirmation to approve."
        return state

    args["confirmed_by_user"] = True

    history_entry = {
        "role": "user",
        "content": f"User approved dangerous operation: {tool}",
        "hidden": True,
        "ts": _now_ts(),
    }
    _index_message(history_entry, state.next_index, _CATEGORY_USER_TASK)
    state.next_index += 1
    state.history.append(history_entry)

    emit({"type": "confirmation_approved", "tool": tool, "step": state.steps})
    result = dispatcher.dispatch(tool, args)

    if not result.get("ok"):
        emit({"type": "tool_result", "tool": tool, "result": result,
              "step": state.steps})
        state.phase = "done"
        state.final_status = "error"
        state.final_text = f"Approved operation {tool} failed: {result.get('error', 'unknown')}"
        state.error_message = state.final_text
        state.pending_confirmation_tool = None
        state.pending_confirmation_args = None
        state.pending_confirmation_reasons = []
        state.pending_confirmation_code = ""
        state.pending_confirmation_result = None
        return state

    emit({"type": "tool_result", "tool": tool, "result": result, "step": state.steps})
    state.user_message = json.dumps({"tool_result": result}, ensure_ascii=False)
    state.pending_confirmation_tool = None
    state.pending_confirmation_args = None
    state.pending_confirmation_reasons = []
    state.pending_confirmation_code = ""
    state.pending_confirmation_result = None
    state.phase = "calling_llm"
    return state


def deny_pending_confirmation(state: AgentLoopState,
                              lang: Optional[str] = None,
                              on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Cancel a pending dangerous operation when the user clicks "Deny".

    Informs the model that the operation was denied so it can try a
    different approach.
    """
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    tool = state.pending_confirmation_tool
    if not tool:
        state.phase = "error"
        state.error_message = "No pending confirmation to deny."
        return state

    history_entry = {
        "role": "user",
        "content": f"User DENIED dangerous operation: {tool}",
        "hidden": True,
        "ts": _now_ts(),
    }
    _index_message(history_entry, state.next_index, _CATEGORY_USER_TASK)
    state.next_index += 1
    state.history.append(history_entry)

    emit({"type": "confirmation_denied", "tool": tool, "step": state.steps})

    denied_result = {
        "ok": False,
        "error": "The user denied permission to execute this operation. "
                 "Find another way that does not require this command.",
        "denied_by_user": True,
    }
    emit({"type": "tool_result", "tool": tool, "result": denied_result, "step": state.steps})

    state.user_message = json.dumps({"tool_result": denied_result}, ensure_ascii=False)
    state.pending_confirmation_tool = None
    state.pending_confirmation_args = None
    state.pending_confirmation_reasons = []
    state.pending_confirmation_code = ""
    state.pending_confirmation_result = None
    state.phase = "calling_llm"
    return state


# ── Sanitization approval helpers ──────────────────────────────────────────────

def approve_sanitized_content(state: AgentLoopState, dispatcher: Any,
                              lang: Optional[str] = None,
                              on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Allow one step with prompt-injection protection disabled.

    Called by the UI when the user clicks "Allow viewing" on the
    [SANITIZED] dialog. Re-runs the loop with
    enable_injection_protection=False once, so the model can see the
    original tool-result payload that triggered the false positive.
    """
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    emit({"type": "sanitized_approved", "step": state.steps})

    # The user explicitly allows this payload to be seen. Remember which
    # paths were approved so _protect_history can keep their results visible
    # on subsequent protected turns instead of asking again.
    for info in state.sanitized_events:
        path = info.get("path")
        if isinstance(path, str) and path:
            state.sanitized_approved_paths.add(path)

    state.enable_injection_protection = False
    state.injection_protection_bypassed = True
    state.sanitized_events = []

    history_entry = {
        "role": "user",
        "content": (
            "User approved viewing the sanitized tool result. "
            "Retry the previous step with prompt-injection protection disabled "
            "for this turn only."
        ),
        "hidden": True,
        "ts": _now_ts(),
    }
    _index_message(history_entry, state.next_index, _CATEGORY_USER_TASK)
    state.next_index += 1
    state.history.append(history_entry)

    state.user_message = state.task if state.task else ""
    state.phase = "calling_llm"
    return state


def deny_sanitized_content(state: AgentLoopState,
                           lang: Optional[str] = None,
                           on_event: Optional[Callable[[dict], None]] = None) -> AgentLoopState:
    """Deny viewing sanitized content and continue with protection enabled.

    When the user clicks "Deny" on the [SANITIZED] dialog, the loop is
    resumed with prompt-injection protection still active. The model is
    told the sanitized content was not viewed.
    """
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    emit({"type": "sanitized_denied", "step": state.steps})

    state.sanitized_events = []

    history_entry = {
        "role": "user",
        "content": (
            "User denied viewing the sanitized tool result. "
            "Continue the task without the withheld content and avoid "
            "relying on it."
        ),
        "hidden": True,
        "ts": _now_ts(),
    }
    _index_message(history_entry, state.next_index, _CATEGORY_USER_TASK)
    state.next_index += 1
    state.history.append(history_entry)

    state.user_message = state.task if state.task else ""
    state.phase = "calling_llm"
    return state


# run_agent_loop (legacy blocking wrapper)


def run_agent_loop(
    task: str,
    assistant: dict,
    dispatcher: Any,
    *,
    on_event: Optional[Callable[[dict], None]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    max_steps: int = 100,
    auto_apply: bool = False,
    lang: Optional[str] = None,
    pending_staged_path: Optional[str] = None,
    pending_action: Optional[str] = None,
    staged_new_text: Optional[str] = None,
    staged_tool: Optional[str] = None,
    file_context: str = "",
    file_name: str = "",
) -> AgentResult:
    base_history = history if history is not None else []
    # Normalise the resumed history: _index/_category are runtime-only and
    # are NOT persisted, so after a UI round-trip the entries come back
    # without them. Re-number by position so appended messages (and
    # get_history_index / get_history_messages) never desync from the list.
    for i, msg in enumerate(base_history):
        if not isinstance(msg, dict):
            continue
        if "_index" not in msg:
            msg["_index"] = i
        if "_category" not in msg:
            msg["_category"] = _classify_message(msg)

    state = AgentLoopState(
        task=task,
        max_steps=max_steps,
        auto_apply=auto_apply,
        strong_assistant=assistant,
        weak_assistant=assistant,
        history=base_history,
        next_index=len(base_history),
        file_context=file_context,
        file_name=file_name,
        pending_action=pending_action,
        pending_staged_path=pending_staged_path,
        staged_new_text=staged_new_text,
        staged_tool=staged_tool,
    )

    while state.phase not in ("done", "error"):
        state = step_agent_loop(state, dispatcher=dispatcher, lang=lang, on_event=on_event)
        if state.final_status == "awaiting_confirmation":
            break
        if state.final_status == "sanitized_required":
            break

    if state.phase == "error":
        return AgentResult(
            status="error",
            text=state.error_message,
            history=state.history,
            steps=state.steps,
        )

    return AgentResult(
        status=state.final_status,
        text=state.final_text,
        history=state.history,
        staged_path=state.final_staged_path,
        diff=state.final_diff,
        steps=state.steps,
        staged_new_text=state.final_staged_new_text,
        staged_tool=state.final_staged_tool,
        applied=state.final_applied,
        discarded=state.final_discarded,
    )


def approve_and_apply(path: str, dispatcher: Any, note: str = "") -> Dict[str, Any]:
    return dispatcher.dispatch("apply_edit", {"path": path, "note": note})


def discard(path: str, dispatcher: Any) -> Dict[str, Any]:
    return dispatcher.dispatch("discard_edit", {"path": path})
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
