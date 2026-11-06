"""Anchor search, line normalisation and Python syntax checking."""
from __future__ import annotations

import ast
import re
from typing import Optional

_LINE_NUMBER_RE = re.compile(r'^\s*\d+\|')


def _strip_line_numbers(text: str) -> str:
    """Remove line-number prefixes (like ' 17|text') from every line.

    WARNING: Do NOT call this automatically on user file content.
    It matches ANY line starting with whitespace+digits+pipe, including
    legitimate content like markdown tables, numbered lists, and log excerpts.
    Only use when you are certain the input is genuine read_file output.
    """
    lines = text.split('\n')
    out = []
    for ln in lines:
        m = _LINE_NUMBER_RE.match(ln)
        if m:
            idx = m.end()  # position right after '|'
            out.append(ln[idx:])
        else:
            out.append(ln)
    return '\n'.join(out)


def _normalize_line_endings(text: str) -> str:
    """Normalise CRLF/CR line endings to LF. Never alters other content."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _ws_tolerant_pattern(old: str) -> "re.Pattern":
    """Compile a whitespace-tolerant regex for a patch anchor.

    Non-whitespace characters must match literally, but any run of
    horizontal whitespace is accepted and every line is matched from its
    own line start, so indentation/spacing mismatches don't break the
    anchor. The pattern does NOT consume the trailing newline, so the
    returned span can be swapped 1:1 with the replacement.
    """
    norm = _normalize_line_endings(old)
    parts = []
    for idx, raw_line in enumerate(norm.split("\n")):
        if idx > 0:
            parts.append(r"\n")
        parts.append(r"^([ \t]*)")
        line = raw_line.replace("\t", " ").strip()
        if not line:
            parts.append(r"$")
            continue
        escaped = re.escape(line)
        escaped = re.sub(r"(\\ )+", r"[ \\t]+", escaped)
        parts.append(escaped)
    parts.append(r"[ \t]*(?=\n|$)")
    return re.compile("".join(parts), re.MULTILINE)


def _norm_to_orig_map(norm: str, orig: str) -> list:
    """Map each position in the LF-normalized text to the original string."""
    mapping = []
    oi = 0
    for ch in norm:
        while oi < len(orig) and orig[oi] != ch:
            oi += 1
        if oi >= len(orig):
            break
        mapping.append(oi)
        oi += 1
    return mapping


def _fuzzy_find_matches(working: str, old: str) -> list:
    """Whitespace-tolerant search; returns (start, end) spans in original coords."""
    pattern = _ws_tolerant_pattern(old)
    norm = _normalize_line_endings(working)
    spans = [(m.end(1), m.end()) for m in pattern.finditer(norm)]
    if norm == working:
        return spans
    mapping = _norm_to_orig_map(norm, working)
    converted = []
    for s, e in spans:
        s_o = mapping[s] if s < len(mapping) else len(working)
        e_o = (mapping[e - 1] + 1) if (e - 1) < len(mapping) and e > 0 else len(working)
        converted.append((s_o, e_o))
    return converted


def _exact_spans(text: str, old: str) -> list:
    """Return every (start, end) span of *old* in *text*."""
    spans = []
    idx = 0
    while True:
        idx = text.find(old, idx)
        if idx == -1:
            break
        spans.append((idx, idx + len(old)))
        idx += len(old)
    return spans


def _line_of(text: str, start: int, _end: int) -> int:
    """1-based line number of the span start."""
    return text.count("\n", 0, start) + 1


def _suggest_anchor_lines(old: str, working: str, limit: int = 3) -> list:
    """Return up to *limit* closest lines/blocks to a missing anchor."""
    import difflib
    old_lines = _normalize_line_endings(old).split("\n")
    work_lines = _normalize_line_endings(working).split("\n")
    out = []
    if len(old_lines) == 1 and old_lines[0].strip():
        for cand in difflib.get_close_matches(old_lines[0].strip(),
                                              [line.strip() for line in work_lines],
                                              n=limit, cutoff=0.35):
            for idx, wl in enumerate(work_lines):
                if wl.strip() == cand:
                    out.append({"line": idx + 1, "text": wl[:120]})
                    break
            if len(out) >= limit:
                break
        return out
    window = len(old_lines)
    scored = []
    for i in range(max(0, len(work_lines) - window + 1)):
        wnd = work_lines[i:i + window]
        sm = difflib.SequenceMatcher(None, old, "\n".join(wnd))
        if sm.quick_ratio() < 0.35:
            continue
        scored.append((sm.ratio(), i, "\n".join(wnd)))
    scored.sort(reverse=True)
    return [{"line": i + 1, "text": txt[:120]} for _, i, txt in scored[:limit]]


def _validate_python_syntax(content: str) -> Optional[str]:
    """Return an error message if `content` is invalid Python, else None.

    Uses only the stdlib `ast` module -- no external dependencies.
    Called by propose_file BEFORE any write so broken code is never
    staged or applied.
    """
    # Tolerate a UTF-8 BOM: Python itself accepts utf-8-sig source files,
    # so a leading U+FEFF must not be flagged as a syntax error. Only the
    # leading BOM is stripped; real syntax errors are still caught.
    if content.startswith("\ufeff"):
        content = content[len("\ufeff"):]
    try:
        ast.parse(content)
    except SyntaxError as e:
        line = e.lineno or "?"
        col = e.offset or "?"
        return (
            f"Python syntax error: {e.msg} (line {line}, column {col}). "
            "Fix the syntax and retry -- the file was NOT written."
        )
    return None


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
