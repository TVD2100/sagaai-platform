# -*- coding: utf-8 -*-
"""
dev_agent.loop.json_repair -- recovering JSON the model did not emit cleanly.

A tool call is a JSON object, but a model does not always produce one: braces
may stay unbalanced, a string value may carry raw newlines, an escape may be
doubled or a long call may be cut off mid-object. This module is the repair
path for that text, ordered from the cheapest interpretation to the most
aggressive: the balanced-object scan, the lenient parser, the layered cascade
(bracket, brace, escape and newline fixes applied one at a time) and the
recovery of truncated calls. When every layer fails, the diagnostics assembled
here explain what was wrong with each block, so the caller can show or log a
usable message instead of a bare parse error.

Everything here is pure text processing: no I/O, no loop state, so each repair
rule is testable on its own string.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


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


def _repair_stray_bracket(raw: str) -> Optional[str]:
    """Remove ONE stray closing bracket near the JSON error position.

    LLMs frequently emit an extra ``]``/``}`` right after a fully closed
    nested construct. json.loads then fails with "Expecting ',' delimiter"
    exactly at the extra bracket. We delete the bracket at (or adjacent to)
    the reported error position and require the result to parse STRICTLY
    into a valid tool call, so accidental damage is rejected.
    """
    if '"tool"' not in raw or "{" not in raw:
        return None
    try:
        json.loads(raw)
        return None  # already valid; nothing to repair
    except json.JSONDecodeError as exc:
        err_pos = exc.pos
    for shift in (0, -1, 1, -2):
        pos = err_pos - 1 + shift
        if 0 <= pos < len(raw) and raw[pos] in "]}":
            trial = raw[:pos] + raw[pos + 1:]
            try:
                obj = json.loads(trial)
            except json.JSONDecodeError:
                continue
            if _normalize_call(obj) is not None:
                return trial
    return None


def _collapse_value_escapes(obj: Any) -> Any:
    """Collapse double-escaped sequences inside the STRING VALUES of *obj*.

    After repairing over-escaped JSON the parsed values may still contain
    backslash-quote and backslash-n/t/r artifacts (the second escaping
    level). This recursive walk restores the intended plain characters.
    Only repaired calls reach this helper - valid JSON is never touched.
    """
    if isinstance(obj, dict):
        return {k: _collapse_value_escapes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_collapse_value_escapes(v) for v in obj]
    if isinstance(obj, str):
        out = obj
        for _ in range(3):  # tolerate up to triple over-escaping
            nxt = (out.replace('\\"', '"')
                      .replace('\\n', '\n')
                      .replace('\\t', '\t')
                      .replace('\\r', '\r'))
            if nxt == out:
                break
            out = nxt
        return out
    return obj


def _unescape_json_string_body(raw: str) -> str:
    """Decode backslash escapes the way a JSON string interior does.

    Handles \\" \\n \\t \\r \\b \\f \\/ \\\\ and \\uXXXX pairs WITHOUT touching
    non-ASCII characters (codecs' unicode_escape would corrupt Cyrillic).
    Unknown escapes are kept literally. Used to recover tool-call bodies
    the model emitted one escaping level too deep.
    """
    simple = {'"': '"', '\\': '\\', 'n': '\n', 't': '\t',
              'r': '\r', 'b': '\b', 'f': '\f', '/': '/'}
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == '\\' and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == 'u':
                hex4 = raw[i + 2:i + 6]
                if len(hex4) == 4 and all(h in '0123456789abcdefABCDEF' for h in hex4):
                    out.append(chr(int(hex4, 16)))
                    i += 6
                    continue
            out.append(ch)
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _try_layer_parse_chain(text: str) -> Optional[Any]:
    """Strict-parse helper for the cascade layers.

    Parses *text* directly; on failure removes one stray bracket near the
    error position and retries. The result is verified as a tool call and
    string values get the double-escape collapse."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        fixed = _repair_stray_bracket(text)
        if fixed is None:
            return None
        try:
            obj = json.loads(fixed)
        except json.JSONDecodeError:
            return None
    obj = _collapse_value_escapes(obj)
    if _normalize_call(obj) is not None:
        return obj
    return None


def _json_repair_cascade(raw: str) -> Optional[Any]:
    """Layered repair for tool-call JSON emitted with systematic damage.

    Tries, in order: strict parse (idempotence for recursive calls),
    stray-bracket deletion at the reported error position, whole-body
    JSON-string-interior unescape (quotes/newlines escaped one level too
    deep), and JSON-string wrap decoding. Every candidate must parse
    STRICTLY into a valid tool call and gets its string values collapsed.
    Valid JSON passes through untouched."""
    if "{" not in raw or "tool" not in raw:
        return None

    # Layer 0: already-valid tool calls return as-is (recursive calls).
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict) and _normalize_call(obj) is not None:
        return obj

    # Layer 1: one stray closing bracket at/near the reported position.
    fixed = _repair_stray_bracket(raw)
    if fixed is not None:
        obj = _collapse_value_escapes(json.loads(fixed))
        if _normalize_call(obj) is not None:
            return obj

    # Layer 2: whole-body JSON-string-interior unescape (escaped quotes,
    # escaped newlines, escaped tabs). Real emissions can be TWO escaping
    # levels deep (a backslash before every structural quote): try one and
    # two unescape passes, each followed by raw-newline repair and a strict
    # parse gated by a valid-tool-call shape.
    body = raw
    for _pass in (1, 2, 3):
        body = _unescape_json_string_body(body)
        if body == raw:
            break
        nl_fixed = _escape_raw_newlines_in_strings(body)
        obj = _try_layer_parse_chain(nl_fixed)
        if obj is not None:
            return obj

    # Layer 3: the block is a JSON string encoding the whole call.
    stripped = raw.strip()
    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        decoded = None
    if isinstance(decoded, str) and decoded.strip() and decoded != raw:
        return _json_repair_cascade(decoded)

    return None


def _json_loads_lenient(raw: str) -> Optional[Any]:
    """json.loads with layered repair for LLM-emitted tool-call JSON.

    Order: strict parse (valid JSON passes through untouched), raw-newline
    repair (existing behaviour), then the _json_repair_cascade layers
    (stray bracket removal, whole-text escape collapse, JSON-string wrap
    decode). Recovered calls already have value-level double escapes
    collapsed."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _escape_raw_newlines_in_strings(raw)
        if repaired != raw:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
    return _json_repair_cascade(raw)


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
    and switching tools. Called both when NO block parsed and when only
    PART of a batch parsed - the failed blocks are then reported next to
    the successful calls they were batched with.
    """
    diagnostics: List[Dict[str, str]] = []
    blocks = re.findall(r"```[a-zA-Z0-9_-]*\s*(.*?)```", text, re.DOTALL)
    # Batches are allowed: every fenced block is validated on its own and the
    # per-block diagnostics below name exactly WHICH blocks failed to parse,
    # whether the message was a single call or a (partially parsed) batch.
    tool_blocks = [b for b in blocks if '"tool"' in b and "{" in b]
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


#: Arguments that belong to the platform, not to the model.
#:
#: The set is EMPTY on purpose. A model-supplied consent flag used to be
#: dropped here, before dispatch; the silent drop made a refused call look
#: like a successful one, and the model never learned why it could not
#: confirm its own operation. The flag now travels to the dispatcher, which
#: rejects it with a structured error (``unknown_args``) the model can act on.
_INTERNAL_ONLY_ARGS: Dict[str, set] = {}


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
    # Consent is never part of a tool call. The model can emit this argument
    # (it is in the schema of some tools as an internal one); it is dropped
    # here so no parsing path - fenced JSON, repaired JSON or DSML - can turn
    # a model-generated field into a user approval.
    if name in _INTERNAL_ONLY_ARGS:
        args = dict(args)
        for internal in _INTERNAL_ONLY_ARGS[name]:
            args.pop(internal, None)
    return {"tool": name, "args": args}

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
