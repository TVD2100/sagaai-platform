# -*- coding: utf-8 -*-
"""tests/scenarios/test_state_recovery_scenario.py - scenario tests for cheap
task-state recovery after context truncation (self-reflection item 2).

Scenario 1 - a long-running task whose journal grew past 30 KB:
    when the chat context was truncated, the agent restores its working
    state cheaply via task_state_read(compact=True): the digest is far
    smaller than the journal, keeps the goal/progress/handoff and the
    NEWEST plan facts, and points to the full journal; the full read keeps
    returning everything (nothing on disk was truncated for real).

Scenario 2 - the auto-injected CURRENT TASK STATE block for the same grown
    journal: the block stays within the hard cap, keeps every step heading
    plus the freshest facts (older ones replaced by an omitted marker), and
    advertises the full-journal pointer.
"""
from __future__ import annotations

import pytest

from dev_agent import config
from dev_agent import task_state as ts


@pytest.fixture
def sandbox(tmp_path):
    """Repoint DevAgent at a temp workspace and isolate the thread id."""
    old_root = config.PROJECT_ROOT
    old_thread = config.ACTIVE_THREAD_ID
    try:
        config.set_target_root(tmp_path)
        config.ACTIVE_THREAD_ID = "recovery_thread_1"
        yield tmp_path
    finally:
        config.set_target_root(old_root)
        config.ACTIVE_THREAD_ID = old_thread


PLAN = "\n".join(
    f"### Step {i} - Step number {i}\n- verification: run tests/test_{i}.py"
    for i in range(1, 13)
)


def _grow_journal() -> None:
    """Drive a 12-step task until its journal passes 30 KB, then stall on
    the last step - the state an agent must restore after its chat context
    was compressed."""
    ts.archive_and_start_task(task="Long running task", plan=PLAN)
    for i in range(1, 12):
        ts.update_plan_step_status(
            f"step_{i}", status="done",
            context="filler context " + ("q" * 150),
        )
    ts.update_plan_step_status(
        "step_12", status="in_progress", context="LATEST-STEP-CONTEXT-MARKER")
    ts.update_task_state_section(
        "handoff", "LATEST-HANDOFF-MARKER " + ("h" * 4000))
    ts.update_task_state_section(
        "analysis", "LATEST-ANALYSIS-TRAIL " + ("a" * 25000))


def test_compact_read_restores_state_cheaply_from_oversized_journal(sandbox):
    # given: a long-running task whose journal grew past 30 KB
    _grow_journal()

    # when: the chat history was truncated and the agent restores state
    result = ts.read_task_state(compact=True)

    # then: the digest is far smaller than the journal but keeps the goal,
    # the progress counter, the handoff and the NEWEST plan fact
    assert result["ok"] and result["exists"]
    assert result["size_bytes"] >= 30000
    assert result["compact"] is True
    assert result["content"] == "" and result["sections"] == {}
    assert result["step_ids"] == [f"step_{i}" for i in range(1, 13)]
    digest = result["digest"]
    assert digest.startswith("COMPACT TASK STATE:")
    assert len(digest) < 6000
    assert len(digest) < result["size_bytes"] // 5
    assert "Long running task" in digest
    assert "steps: 11/12 done" in digest
    assert "LATEST-HANDOFF-MARKER" in digest
    assert "LATEST-STEP-CONTEXT-MARKER" in digest
    assert "### Step 1 - Step number 1" in digest
    assert "task_state_read() for the full journal" in digest

    # then: the full read still returns EVERYTHING (no disk truncation)
    full = ts.read_task_state()
    assert full["compact"] is False
    assert "LATEST-ANALYSIS-TRAIL" in full["sections"]["analysis"]
    assert "LATEST-HANDOFF-MARKER" in full["sections"]["handoff"]


def test_injected_block_stays_bounded_and_keeps_newest_facts(sandbox):
    # given: the same oversized journal
    _grow_journal()

    # when: the platform injects the block at the end of the request
    block = ts.task_state_for_context()

    # then: the block respects the hard cap...
    assert block is not None
    assert len(block) <= ts.MAX_STATE_CHARS + 25

    # ...keeps the key sections and the freshest facts...
    assert "### Task" in block
    assert "LATEST-HANDOFF-MARKER" in block
    assert "LATEST-STEP-CONTEXT-MARKER" in block
    assert "LATEST-ANALYSIS-TRAIL" in block
    assert "### Step 1 - Step number 1" in block
    assert "[omitted" in block

    # ...and tells the agent how to reach the full journal.
    assert "task_state_read() returns the full journal" in block
