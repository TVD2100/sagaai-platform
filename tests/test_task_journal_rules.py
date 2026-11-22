# -*- coding: utf-8 -*-
"""The rules a model reads about the task journal.

A journal the model does not know how to use is a file written for nobody.
These tests pin the three places the rules reach it -

the system prompt section, the tool catalog entry and the platform block
appended to every employee - and the facts each of them has to state:

- where the journal lives (the dialog's own folder, not the project);
- that a step marked ``done`` carries a verification line;
- that the archive keeps the plan, the verification lines and the task
  folder.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import prompt_blocks

PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dev_agent", "system_prompt.md")


def _system_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as fh:
        return fh.read()


def _catalog_entry(name: str) -> dict:
    from dev_agent.tools import catalog
    for entry in catalog.TOOL_CATALOG:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"{name} is not in the tool catalog")


def test_system_prompt_says_the_journal_belongs_to_the_dialog():
    """The prompt must not send the journal into the project folder."""
    prompt = _system_prompt()

    assert "task_states/TASK_STATE__<thread_id>.md" in prompt
    assert "the DIALOG's own folder" in prompt
    assert "`.dev_agent/task_states/TASK_STATE__<thread_id>.md` inside the active project" not in prompt


def test_system_prompt_states_what_the_archive_keeps():
    """A model planning a task knows what survives it."""
    prompt = _system_prompt()

    assert "keeps the\n  PLAN text" in prompt or "keeps the PLAN text" in prompt
    assert "verification" in prompt


def test_tool_catalog_states_the_journal_location():
    """The tool description repeats the rule where the call is made."""
    desc = _catalog_entry("task_state_init")["desc"]

    assert "dialog" in desc.lower()
    assert "migrated" in desc.lower()


def test_tool_catalog_requires_a_verification_line_for_done():
    """Marking a step done is a claim that has to be proven."""
    desc = _catalog_entry("task_state_mark_step")["desc"]

    assert "verification" in desc
    assert "unproven" in desc.lower()


def test_platform_parent_block_requires_verification():
    """Every employee, not only this one, reads the same duty."""
    block = "".join(prompt_blocks.TASK_JOURNAL_RULES_PARENT)

    assert "verification" in block
    assert "task_dir" in block
    assert "archive" in block.lower()


def test_platform_child_block_requires_verification():
    """A delegated run reports a proven step, not a claimed one."""
    block = "".join(prompt_blocks.TASK_JOURNAL_RULES_CHILD)

    assert "verification" in block
    assert "unproven" in block.lower()
