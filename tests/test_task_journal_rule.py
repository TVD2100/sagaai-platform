"""Tests of the platform-level applicability rule of the task journal.

The journal is a capability: the tools exist for every employee that keeps
them enabled, so the rule that says WHEN a journal is worth creating is
rendered by the platform rather than written into each role prompt. These
tests pin the two halves that matter: every role with the tools receives the
rule, and the rule itself refuses a journal for an informational request or a
one-step change.
"""
import pytest

from core.orchestrator_prompt import _extend_prompt_with_task_journal_impl

JOURNAL_TOOLS = ["task_state_init", "task_state_read", "task_state_mark_step",
                 "task_state_clear"]


@pytest.fixture()
def journal_enabled(monkeypatch):
    """Make the employee look like one with the whole journal capability."""
    import core.orchestrators as orch
    monkeypatch.setattr(orch, "get_disabled_tools", lambda slug="dev_agent": [])


@pytest.fixture()
def journal_disabled(monkeypatch):
    """Disable one of the journal tools, which breaks the capability."""
    import core.orchestrators as orch
    monkeypatch.setattr(orch, "get_disabled_tools",
                        lambda slug="dev_agent": ["task_state_clear"])


def test_rule_is_rendered_for_a_role_with_the_journal(journal_enabled):
    out = _extend_prompt_with_task_journal_impl("# ROLE", "dev_agent", "parent")
    assert "## Task journal (task_state)" in out
    assert out.startswith("# ROLE")


def test_rule_is_withheld_when_the_capability_is_incomplete(journal_disabled):
    out = _extend_prompt_with_task_journal_impl("# ROLE", "dev_agent", "parent")
    assert out == "# ROLE"


def test_rule_names_both_the_threshold_and_the_skips(journal_enabled):
    block = _extend_prompt_with_task_journal_impl("", "dev_agent", "parent")
    lowered = block.lower()
    assert "3 or more steps" in lowered
    assert "informational" in lowered
    assert "one step" in lowered
    assert "same step" in lowered


def test_rule_points_a_one_step_request_at_long_term_memory(journal_enabled):
    block = _extend_prompt_with_task_journal_impl("", "dev_agent", "parent")
    assert "memory_append" in block
    assert "duplicate" in block.lower()


def test_rule_requires_a_close_and_forbids_deletion(journal_enabled):
    block = _extend_prompt_with_task_journal_impl("", "dev_agent", "parent")
    assert "task_state_clear" in block
    assert "never delete the" in block.lower()


def test_parent_and_child_rules_differ(journal_enabled):
    parent = _extend_prompt_with_task_journal_impl("", "dev_agent", "parent")
    child = _extend_prompt_with_task_journal_impl("", "dev_agent", "child")
    assert parent != child
    assert "OWN file" in child
    assert "read_subagent_state" in child
    assert "read_subagent_state" not in parent


def test_child_rule_keeps_the_answer_out_of_the_journal(journal_enabled):
    child = _extend_prompt_with_task_journal_impl("", "dev_agent", "child")
    assert "final answer" in child.lower()
    assert "3 or more steps" in child.lower()


def test_role_without_the_capability_gets_no_block(journal_disabled):
    out = _extend_prompt_with_task_journal_impl("BASE", "task_scout", "parent")
    assert "Task journal" not in out


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
