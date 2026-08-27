"""
core.assistant_creator - validation and linting helpers for assistant prompts.

Used by the Assistants page to give immediate feedback when a user writes
or edits an assistant's system prompt before saving it.

Public API:
    validate_prompt(text: str) -> list[str]
    lint_prompt(text: str) -> list[str]
"""
from __future__ import annotations


_REQUIRED_SECTIONS = ("## Role", "## Task")
_OPTIONAL_SECTIONS = (
    "## Context",
    "## Tone and style",
    "## Output format",
    "## Constraints",
)


def _section_headers(text: str) -> list[str]:
    """Return the markdown section headers found in *text* (normalized)."""
    headers: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            headers.append(line)
    return headers


def validate_prompt(text: str) -> list[str]:
    """Check an assistant prompt and return a list of human-readable issues.

    An empty list means the prompt is considered valid.
    """
    issues: list[str] = []

    if not text or not text.strip():
        issues.append("Prompt text is empty.")
        return issues

    normalized = text.strip()

    if len(normalized) < 100:
        issues.append("Prompt is too short - expected at least 100 characters.")

    headers = _section_headers(normalized)
    for required in _REQUIRED_SECTIONS:
        if required not in headers:
            issues.append(f"Missing required section: {required}")

    if not headers:
        issues.append("No markdown sections found. Use '## Section Name' headers.")

    if "you are" not in normalized.lower():
        issues.append("Consider adding a '## Role' paragraph that states who the model acts as.")

    return issues


def lint_prompt(text: str) -> list[str]:
    """Return an extended list of style/consistency warnings for *text*.

    This is a superset of :func:`validate_prompt`: it includes the same
    structural checks plus optional-section advice and repeated-header
    detection.
    """
    issues = validate_prompt(text)
    if not text or not text.strip():
        return issues

    normalized = text.strip()
    headers = _section_headers(normalized)

    for optional in _OPTIONAL_SECTIONS:
        if optional not in headers:
            issues.append(f"Optional section missing (recommended): {optional}")

    seen: set[str] = set()
    for header in headers:
        if header in seen:
            issues.append(f"Duplicate section header: {header}")
        seen.add(header)

    return issues
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
