# -*- coding: utf-8 -*-
"""Tests for how the agent dialog reaches a model: catalog and prompt.

A tool the model cannot see does not exist for it, and a rule it cannot read
is not followed. These tests pin both halves: the pair is registered in the
catalog with a description that states the contract, and each side of the
dialog gets the rules of ITS half only.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import orchestrators  # noqa: E402
from core import orchestrator_tools  # noqa: E402
from core.orchestration import service as orchestration_service  # noqa: E402
from core.orchestration import tools  # noqa: E402


SLUG = "dev_agent"


def _catalog_names():
    return [entry["name"] for entry in
            orchestrator_tools.build_tool_catalog(SLUG, disabled=set())]


def test_both_halves_of_the_dialog_are_in_the_catalog():
    """A tool the model cannot see does not exist for it."""
    names = _catalog_names()

    assert "ask_parent" in names
    assert "answer_child" in names


def test_the_catalog_entry_states_the_contract_of_asking():
    """The description has to say that the run is parked, not finished."""
    entry = tools.ask_parent_tool_entry()

    assert entry["name"] == "ask_parent"
    description = entry["desc"].lower()
    assert "parked" in description
    assert "same" in description


def test_the_catalog_entry_states_the_contract_of_answering():
    """The parent must know the reply continues the SAME run."""
    entry = tools.answer_child_tool_entry()

    assert entry["name"] == "answer_child"
    description = entry["desc"].lower()
    assert "same run" in description


def test_a_child_may_not_answer_its_own_children():
    """The answering half belongs to the parent, so a child loses the tool."""
    assert "answer_child" in orchestration_service.CHILD_DISABLED_TOOLS
    assert "ask_parent" not in orchestration_service.CHILD_DISABLED_TOOLS


def test_the_answering_tool_is_withheld_when_the_child_set_is_applied():
    """The same catalog loses the answering half once a child is configured."""
    parent_names = [entry["name"] for entry in
                    orchestrator_tools.build_tool_catalog(SLUG, disabled=set())]
    child_names = [entry["name"] for entry in
                   orchestrator_tools.build_tool_catalog(
                       SLUG,
                       disabled=set(orchestration_service.CHILD_DISABLED_TOOLS))]

    assert "answer_child" in parent_names
    assert "answer_child" not in child_names
    # Asking stays available to the child: it is the child's own half.
    assert "ask_parent" in child_names


def test_the_parent_prompt_teaches_answering_and_not_asking():
    """Each side reads the rules of its own half of the dialog."""
    prompt = orchestrators._extend_prompt_with_dialog("BASE", SLUG, role="parent")

    assert "Answering a subagent's question" in prompt
    assert "answer_child" in prompt
    assert "Asking the agent that launched you" not in prompt


def test_the_child_prompt_teaches_asking_and_not_answering():
    """A child that read the parent's rules would look for children of its own."""
    prompt = orchestrators._extend_prompt_with_dialog("BASE", SLUG, role="child")

    assert "Asking the agent that launched you" in prompt
    assert "ask_parent" in prompt
    assert "Answering a subagent's question" not in prompt


def test_the_prompt_block_appends_instead_of_replacing():
    """The dialog rules are added to a prompt, never substituted for it."""
    prompt = orchestrators._extend_prompt_with_dialog("MY OWN RULES", SLUG,
                                                     role="parent")

    assert prompt.startswith("MY OWN RULES")
    assert len(prompt) > len("MY OWN RULES")


def test_an_empty_prompt_gets_the_block_alone():
    """An orchestrator without its own prompt still learns the rules."""
    prompt = orchestrators._extend_prompt_with_dialog("", SLUG, role="parent")

    assert prompt.startswith("## Answering a subagent's question")


def test_the_builder_passes_the_role_through():
    """A child is built with the child role - the default is the parent."""
    default_prompt, _ = orchestrators.build_assistant_dicts(SLUG)
    child_prompt, _ = orchestrators.build_assistant_dicts(SLUG,
                                                          dialog_role="child")

    assert "Answering a subagent's question" in default_prompt["text"]
    assert "Asking the agent that launched you" in child_prompt["text"]
    assert "Asking the agent that launched you" not in default_prompt["text"]
