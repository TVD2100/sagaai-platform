"""
core.prompt_improver - improve an assistant system prompt with the weak model.

The Assistants page exposes an "Improve prompt" button. Instead of the old
local heuristic validation (core.assistant_creator.validate_prompt), it calls
an LLM (the weak model configured for the DevAgent orchestrator) with a
system prompt taken from the built-in "Prompt Improver" instruction
(defaults/orchestrators/dev_agent/instructions/prompt_improver.md,
stored at runtime as orchestrator instruction ``prompt_improver`` of the
DevAgent orchestrator). The improved prompt is returned as plain text and
inserted back into the prompt editor.

Public API:
    get_improver_instruction() -> str
    improve_prompt_with_weak_model(prompt_text, lang=None, send_request_fn=None,
                                   instruction_text=None) -> str
"""
from __future__ import annotations

from typing import Callable, Optional


# Stable id of the built-in Prompt Improver instruction.
PROMPT_IMPROVER_INSTRUCTION_ID = "prompt_improver"

# Substring used as a sanity marker of the instruction content.
_INSTRUCTION_MARKER = "Prompt Improver"


def get_improver_instruction(orchestrator_slug: str = "dev_agent") -> str:
    """Return the Prompt Improver instruction text, or empty string.

    The instruction is stored as an orchestrator-specific instruction of the
    DevAgent orchestrator and is seeded from
    defaults/orchestrators/dev_agent/instructions/prompt_improver.md during
    bootstrap. A missing/empty instruction returns "" so the caller can
    surface a user-friendly error.
    """
    try:
        import core.orchestrators as orch_mod
        full = orch_mod.orch_get_instruction(orchestrator_slug, PROMPT_IMPROVER_INSTRUCTION_ID)
        if isinstance(full, dict):
            return str(full.get("text", "") or "")
    except Exception:
        pass
    return ""


def improve_prompt_with_weak_model(
    prompt_text: str,
    lang: Optional[str] = None,
    send_request_fn: Optional[Callable] = None,
    instruction_text: Optional[str] = None,
) -> str:
    """Improve *prompt_text* using the weak DevAgent model.

    Args:
        prompt_text: The assistant prompt to improve. Must be non-empty.
        lang: Optional UI language passed to the underlying send_request.
        send_request_fn: Optional callable with the same signature as
            ``core.api_layer.send_request``. Defaults to the real one.
        instruction_text: Optional Prompt Improver system prompt. When None,
            it is loaded from the DevAgent orchestrator instructions.

    Returns:
        The improved prompt text (trimmed, non-empty).

    Raises:
        ValueError: when the prompt or the instruction is empty, when no
        weak model is configured, or when the LLM returns an empty answer.
        The send_request_fn exceptions (e.g. ApiKeyMissingError) propagate.
    """
    if not prompt_text or not str(prompt_text).strip():
        raise ValueError("Prompt text is empty.")

    if instruction_text is None:
        instruction_text = get_improver_instruction()
    if not instruction_text or not instruction_text.strip():
        raise ValueError("Prompt Improver instruction is not available.")

    if send_request_fn is None:
        from core.api_layer import send_request
        send_request_fn = send_request

    from core.orchestrators import DEVAGENT_SLUG, build_assistant_dicts
    _strong, weak = build_assistant_dicts(DEVAGENT_SLUG)
    if not weak.get("service") or not weak.get("model"):
        raise ValueError("Weak model is not configured for DevAgent.")

    assistant = dict(weak)
    assistant["text"] = str(instruction_text).strip()

    user_message = (
        "Improve the following assistant system prompt. "
        "Return only the improved prompt text, ready to paste into the prompt field.\n\n"
        f"{prompt_text}"
    )
    result_text = send_request_fn(
        user_message=user_message,
        assistant=assistant,
        file_context="",
        history=[],
        lang=lang,
    )
    improved = str(result_text or "").strip()
    if not improved:
        raise ValueError("The model returned an empty prompt.")
    return improved
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
