"""Prompt blocks of the employee's continuity: declared memory and the task journal.

Both blocks answer a question the PLATFORM owns, not the role prompt: where
the durable records live and when a task journal is worth creating. They are
rendered here rather than inside ``core.orchestrator_prompt`` so that module
stays a wiring layer, and so the two contracts keep a home of their own.

Every function returns the prompt unchanged when the block has nothing to
say: a prompt never breaks because a configuration read failed.
"""
from __future__ import annotations

from core.orchestrator_defaults import DEVAGENT_SLUG
from core.prompt_blocks import TASK_JOURNAL_RULES_CHILD, TASK_JOURNAL_RULES_PARENT

# ─── Declared memory levels ─────────────────────────────────────────────────


def extend_prompt_with_memory_profile(prompt: str,
                                      orchestrator_slug: str = DEVAGENT_SLUG,
                                      workspace: str = "",
                                      dialog_role: str = "parent") -> str:
    """Append the ``## Memory profile`` block built from the declaration.

    The employee declares its memory levels in ``config["memory_profile"]``;
    this extender renders them into one contract (folder, read policy and
    budget per level, the append rule). A role therefore never repeats the
    memory mechanism in its own prose - which is what let several role files
    drift into different descriptions of the same folder.

    A delegated subagent gets the opposite block: it has no levels and it
    must not write into the registries of the employee that launched it.

    Args:
        prompt: current system prompt text.
        orchestrator_slug: employee whose levels are rendered.
        workspace: active project folder; empty means "no project selected".
        dialog_role: ``parent`` for a dialog, ``child`` for a subagent.

    Returns:
        The prompt with the block appended, or the prompt unchanged when the
        employee declares no levels (the block would have nothing to say).
    """
    is_child = str(dialog_role or "parent") == "child"
    try:
        from core import memory_profile as mp
        from core import orchestrator_folders as of
        from core.orchestrators import get_orchestrator

        orchestrator = get_orchestrator(orchestrator_slug) or {}
        profile = mp.profile_of(orchestrator.get("config", {}))
        if not is_child and not profile.get("levels"):
            return prompt
        resolved = of.resolve_data_dir(orchestrator_slug, workspace)
        block = mp.render_block(
            profile,
            str(resolved.get("path") or ""),
            is_child=is_child,
            source=str(resolved.get("source") or "default"),
            scope=str(resolved.get("scope") or "central"),
        )
    except Exception:
        return prompt
    return _join(prompt, block)


# ─── Task journal applicability ─────────────────────────────────────────────

#: The journal only exists when the employee can open, read, advance AND close
#: a task. An employee that can start one but not close it would leave a file
#: open forever, so a partial capability gets no rule at all.
_JOURNAL_TOOLS = frozenset({"task_state_init", "task_state_read",
                            "task_state_mark_step", "task_state_clear"})


def extend_prompt_with_task_journal(prompt: str,
                                    orchestrator_slug: str = DEVAGENT_SLUG,
                                    dialog_role: str = "parent") -> str:
    """Append the rule that says WHEN the task journal is worth creating.

    The journal is a platform capability: the tools exist for every employee
    that keeps them enabled, while the rule about applicability belongs to no
    single role. Rendering it here means every employee receives the same
    rule, and that a one-step request does not pay for a plan, a file and a
    close it does not need.

    Args:
        prompt: current system prompt text.
        orchestrator_slug: employee whose tool set decides whether to render.
        dialog_role: ``parent`` for a dialog, ``child`` for a subagent.

    Returns:
        The prompt with the block appended, or the prompt unchanged when the
        employee has no journal tools.
    """
    try:
        from core.orchestrator_tools import build_tool_catalog
        from core.orchestrators import get_disabled_tools

        disabled = set(get_disabled_tools(orchestrator_slug))
        names = {t.get("name") for t in build_tool_catalog(orchestrator_slug,
                                                          disabled=disabled)}
    except Exception:
        return prompt
    if not _JOURNAL_TOOLS.issubset(names):
        return prompt
    lines = (TASK_JOURNAL_RULES_CHILD if str(dialog_role or "parent") == "child"
             else TASK_JOURNAL_RULES_PARENT)
    return _join(prompt, "".join(lines))


# ─── Shared tail ────────────────────────────────────────────────────────────


def _join(prompt: str, block: str) -> str:
    """Append *block* to *prompt* on a blank line, skipping an empty block."""
    text = (block or "").strip()
    if not text:
        return prompt
    if prompt.strip():
        return prompt + "\n\n" + text
    return text

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
