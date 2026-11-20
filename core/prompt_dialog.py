"""The agent-dialog prompt block: how a parent answers and a child asks.

The block is rendered from the tool catalog rather than from the role
prompt: a role that cannot answer a child has no business reading the rule
about answering one.
"""
from __future__ import annotations

from core.orchestrator_defaults import DEVAGENT_SLUG
from core.prompt_blocks import DIALOG_RULES_CHILD, DIALOG_RULES_PARENT


def extend_prompt_with_dialog(prompt: str, orchestrator_slug: str = DEVAGENT_SLUG,
                              role: str = "parent") -> str:
    """Append the rules of the agent dialog for the given role.

    *role* is ``parent`` for an orchestrator that may answer a parked child
    and ``child`` for the subagent that may ask. The block is best effort: a
    config read failure returns the prompt unchanged rather than breaking it.

    Args:
        prompt: current system prompt text.
        orchestrator_slug: employee whose tools decide whether to render.
        role: ``parent`` or ``child``.

    Returns:
        The prompt with the block appended, or the prompt unchanged when the
        matching tool is not offered to the employee.
    """
    try:
        from core.orchestrator_tools import is_conditional_tool_enabled
        tool = "answer_child" if role == "parent" else "ask_parent"
        if not is_conditional_tool_enabled(tool, orchestrator_slug):
            return prompt
    except Exception:
        return prompt
    lines = DIALOG_RULES_PARENT if role == "parent" else DIALOG_RULES_CHILD
    block = "".join(lines).rstrip()
    if prompt.strip():
        return prompt + "\n\n" + block
    return block

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
