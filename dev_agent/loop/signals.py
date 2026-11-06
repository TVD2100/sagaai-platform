# -*- coding: utf-8 -*-
"""
dev_agent.loop.signals -- reading the meaning of a model answer.

Everything here is text analysis on the answer itself, with no loop state and
no side effects: which tool class a step belongs to (``classify_step_strength``),
the trailing ``loop_status`` block and the legacy ``_requires_user_response``
field, whether the prose asks the user a question or waits for approval, and
whether it presents a plan. Those answers decide whether AUTO_CONTINUE may run
past the turn and whether the plan-approval gate applies, so the patterns are
deliberately conservative: a false "plan" only costs one extra user round,
while a missed plan would let the loop write before approval.

Non-ASCII hyphens are normalised here as well - a model routinely emits
U+2013 instead of a minus, which silently breaks the ASCII-anchored patches.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

# Tool classes: a STRONG tool changes state (a write, a test run, an assistant
# creation), a WEAK one only reads. The class drives the step strength, which
# in turn decides how much autonomy the loop keeps on the next request.
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

_WEAK_TOOLS: Set[str] = {
    "read_file",
    "list_files",
    "read_doc",
    "read_document",
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
    r'"loop_status"\s*:\s*"(continue|awaiting_user|done)"', re.IGNORECASE
)


def _parse_loop_status(text: str) -> Optional[str]:
    """Return the ``loop_status`` value of the answer, or None when absent."""
    if not text:
        return None
    m = _LOOP_STATUS_RE.search(text)
    if m:
        return m.group(1).lower()
    return None


def _parse_requires_user_response(text: str) -> Optional[bool]:
    """Return the legacy ``_requires_user_response`` flag, or None."""
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
    """Return True when the prose reports progress but is not the final report."""
    if not text:
        return False
    if _FINAL_PHRASE.search(text):
        return False
    return any(p.search(text) for p in _PROGRESS_PATTERNS)


def _prose_looks_like_question(text: str) -> bool:
    """Return True when the prose asks the user for a decision."""
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
    """Return True when the prose carries no plan, heading or list."""
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
    """Replace every non-ASCII hyphen (U+2010..U+FF0D) with an ASCII '-'."""
    return _NON_ASCII_HYPHENS.sub('-', content)


def classify_step_strength(parsed_calls: List[Dict[str, Any]],
                           assistant_text: str) -> str:
    """Return 'strong' or 'weak' for the step that produced the calls.

    A step is strong when at least one parsed call belongs to a state-changing
    tool, or when the answer carries no calls but the prose is not a mere
    acknowledgement. Everything else is weak.
    """
    if parsed_calls:
        for call in parsed_calls:
            tool = call.get("tool", "")
            if tool in _STRONG_TOOLS:
                return "strong"
        return "weak"
    if assistant_text and not _prose_looks_weak(assistant_text):
        return "strong"
    return "weak"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
