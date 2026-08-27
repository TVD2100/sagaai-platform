"""
core.prompt_guard - prompt-injection defense helpers.

Provides lightweight, dependency-free utilities used by the orchestration
layer to:
  1. Mark untrusted data with explicit [DATA_BEGIN / DATA_END] fences so the
     LLM can distinguish instructions from data.
  2. Sanitize untrusted text (control chars, null bytes, zero-width and bidi
     Unicode characters).
  3. Detect common prompt-injection signatures heuristically.

This module is intentionally in core/ (not dev_agent/) so it can be imported
by core.api_layer without creating an import cycle (dev_agent depends on
core, not the other way around).

The heuristics here are a fast first line of defense, NOT a complete
solution. They complement the system-prompt rules and, optionally, an LLM
classifier for high-risk actions.
"""

import re
from typing import List, Optional

# ─── Data-fence markers ──────────────────────────────────────────────────────
# Every untrusted block (file contents, tool results, web-search output) is
# wrapped between these markers before it is sent to the model. The system
# prompt tells the model that anything inside the fences is DATA, not
# instructions.
DATA_BEGIN_PREFIX = "[DATA_BEGIN:"
DATA_BEGIN_SUFFIX = "]"
DATA_END_TAG = "[DATA_END]"


def wrap_data(text: str, source: str = "data") -> str:
    """Return *text* wrapped in [DATA_BEGIN: <source>] ... [DATA_END] fences.

    Empty or whitespace-only input is returned unchanged (wrapping nothing
    adds noise). The source label is a short ASCII string such as
    "tool_result", "file_context", or "web_search".
    """
    if not text or not str(text).strip():
        return text or ""
    text = str(text)
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", str(source))[:40]
    return f"{DATA_BEGIN_PREFIX} {label}{DATA_END_TAG}\n{text}\n{DATA_END_TAG}"


def is_wrapped_data(text: str) -> bool:
    """Return True if *text* appears to already be a data-fenced block."""
    return DATA_BEGIN_PREFIX in (text or "") and DATA_END_TAG in (text or "")


# ─── Sanitization ────────────────────────────────────────────────────────────
# Remove bytes/characters that are often used to smuggle hidden instructions:
# null bytes, ASCII control characters, zero-width spaces, bidi overrides,
# and other Unicode format characters.
_CONTROL_CHARS_RE = re.compile(
    "["
    "\x00-\x08"      # NUL, SOH, STX, ETX, EOT, ENQ, ACK, BEL, BS
    "\x0b\x0c"      # VT, FF
    "\x0e-\x1f"      # SO ... US
    "\x7f"            # DEL
    "\u200b\u200c\u200d\u200e\u200f"   # zero-width space/non-joiner/joiner/LRE/RLE
    "\u202a\u202b\u202c\u202d\u202e"   # bidi overrides
    "\u2060\u2061\u2062\u2063\u2064"   # word joiner, function application, etc.
    "\ufeff"           # BOM / zero-width no-break space
    "]"
)


def sanitize_text(text: str, max_len: Optional[int] = None) -> str:
    """Remove control/format characters and optionally truncate *text*.

    Newlines and tabs are preserved (they are legitimate in data). This is
    a lossy sanitizer: it deletes the dangerous characters rather than
    replacing them with visible escapes, because invisible characters are
    almost never meaningful in tool/file payloads.
    """
    if text is None:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", str(text))
    if max_len is not None and len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "\n...[truncated]...\n"
    return cleaned


# ─── Injection-signature heuristics ──────────────────────────────────────────
# These patterns catch the most common public prompt-injection payloads.
# They are intentionally conservative: false positives are better than
# letting a malicious instruction reach the model unmarked.
_INJECTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"(?i)\bignore\s+(all\s+)?(previous|above|prior|earlier)\s+(instructions?|prompts?|messages?|contexts?|rules?)\b"),
    re.compile(r"(?i)\bdisregard\s+(all\s+)?(previous|above|prior|earlier)\s+(instructions?|prompts?|messages?|contexts?|rules?)\b"),
    re.compile(r"(?i)\b(?:forget|ignore)\s+everything\s+(?:above|before|previous|prior)\b"),
    re.compile(r"(?i)\byou\s+are\s+(?:now\s+)?(?:not\s+)?(?:no\s+longer\s+)?[a-z]+\b.*\b(?:assistant|devagent|agent|bot)\b"),
    re.compile(r"(?i)\b(?:new|updated|override(?:ing)?)\s+(?:system\s+)?(?:instructions?|prompts?|rules?)\b"),
    re.compile(r"(?i)\b(?:reveal|expose|show|print|leak|output)\s+(?:your|the|my)\s+(?:system\s+)?(?:prompt|instructions?|rules?|api[_-]?keys?|secrets?|password|credentials?)\b"),
    re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*[\w-]{8,}\b"),
    re.compile(r"(?i)\b(?:now\s+)?(?:repeat|echo)\s+(?:after\s+me|the\s+following)\b"),
    re.compile(r"(?i)\b(?:pretend|act)\s+(?:(?:like|as)\s+)?(?:an?\s+)?(?:you\s+are\s+)?[^.]*\b(?:admin|root|system|developer|god)\b"),
    re.compile(r"(?i)\b(?:bypass|ignore)\s+(?:the\s+)?(?:above\s+)?(?:rules?|policies?|guardrails?|constraints?)\b"),
    re.compile(r"(?i)\b(?:system\s+pipeline\s+override|set\s+system\s+prompt)\b"),
    re.compile(r"(?i)\bnow\s+do\s+as\s+I\s+say\b"),
]


def detect_injection_signatures(text: str) -> List[str]:
    """Return a list of matched injection-signature names for *text*.

    Empty list means no signature fired. This is a heuristic layer; absence
    of a match does NOT guarantee safety.
    """
    if not text:
        return []
    haystack = str(text)
    hits: List[str] = []
    for i, pattern in enumerate(_INJECTION_PATTERNS):
        if pattern.search(haystack):
            hits.append(f"pattern_{i}")
    return hits


# ─── High-risk tool related helpers ──────────────────────────────────────────
# The agent loop / API layer can use these to decide whether to sanitize/wrap
# a tool result before it is fed back into the conversation.

_TOOL_RESULT_PREFIX = '{"tool_result"'


def is_tool_result_text(text: str) -> bool:
    """Return True if *text* looks like a serialized tool-result envelope."""
    return str(text or "").strip().startswith(_TOOL_RESULT_PREFIX)


def sanitize_tool_result_content(text: str, source: str = "tool_result",
                                 max_len: Optional[int] = None,
                                 strict: bool = True) -> str:
    """Sanitize tool-result text and wrap it in data fences.

    Args:
        text: The raw tool-result payload.
        source: Label for the data-fence markers (e.g. "tool_result").
        max_len: Optional max character count; excess is truncated.
        strict: If True (default), content that matches an injection signature
                is replaced with a short [SANITIZED] placeholder. If False,
                the content is still scrubbed of control characters and wrapped
                in data fences, but injection-signature matches are ignored
                (the original text is preserved).

    This allows the master "Safe mode" toggle to disable aggressive prompt-
    injection counter-measures while keeping basic sanitation active.
    """
    if not text:
        return text or ""
    raw = sanitize_text(text, max_len=max_len)
    if strict and detect_injection_signatures(raw):
        return (
            f"{DATA_BEGIN_PREFIX} {source}{DATA_END_TAG}\n"
            "[SANITIZED: potential prompt-injection signature detected; "
            "original content withheld]\n"
            f"{DATA_END_TAG}"
        )
    return wrap_data(raw, source=source)


# ─── Web-search specific helpers ─────────────────────────────────────────────
def sanitize_search_result(text: str, max_len: int = 6000) -> str:
    """Sanitize and optionally truncate a web-search response.

    Also strips HTML tags if present (simple regex) and adds a visible
    ``[DATA_FROM_WEB_SEARCH]`` label so the orchestrator can tell the model
    the content came from an untrusted external source.
    """
    if not text:
        return text or ""
    raw = sanitize_text(text, max_len=max_len)
    # Strip common HTML tags (best-effort; not a full HTML parser).
    raw = re.sub(r"<[^>]+>", " ", raw)
    # Collapse runs of blank lines to keep the payload compact.
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    if not raw:
        return ""
    return f"[DATA_FROM_WEB_SEARCH]\n{raw}"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
