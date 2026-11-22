# -*- coding: utf-8 -*-
"""The journal names the steps that were marked done without proof.

A step marked ``done`` without a verification line is an unproven claim. It
must not pass silently: the injected working block reminds the agent while the
task is running, and the archive reports what it could not prove instead of
carrying the claim into history as if it had been checked.

Covered:

- detection: a done step without a verification line is reported, one with a
  line is not, and ``in_progress``/``pending`` steps are never reported;
- the injected context block carries the reminder;
- ``task_state_clear`` returns the unproven ids plus a warning;
- a fully proven task produces no reminder and no warning.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import config
from dev_agent import task_state as ts

PLAN_TEXT = (
    "### Step 1 - Data layer\n"
    "### Step 2 - Export\n"
    "### Step 3 - Docs\n"
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated workspace, thread and journal storage."""
    import core.paths as paths_mod

    old_state = config.snapshot_state()
    old_history = paths_mod.HISTORY_DIR
    monkeypatch.setattr(paths_mod, "HISTORY_DIR",
                        str(tmp_path / "data" / "history"), raising=False)
    config.set_target_root(tmp_path / "project")
    config.ACTIVE_THREAD_ID = "unverified_thread"
    try:
        yield tmp_path
    finally:
        config.restore_state(old_state)
        paths_mod.HISTORY_DIR = old_history


def test_detection_reports_only_unproven_done_steps():
    """A done step is checked for a verification line; other states are not."""
    plan = (
        "### Step 1 - Proven (status: done)\n"
        "- verification: pytest - 4 passed\n"
        "### Step 2 - Claimed (status: done)\n"
        "### Step 3 - Running (status: in_progress)\n"
        "### Step 4 - Empty proof (status: done)\n"
        "- verification:\n"
    )

    assert ts.steps_missing_verification(plan) == ["step_2", "step_4"]


def test_a_proven_plan_reports_nothing():
    """No false alarm when every finished step carries its proof."""
    plan = (
        "### Step 1 - Done (status: done)\n"
        "- verification: unit tests - 3 passed\n"
    )

    assert ts.steps_missing_verification(plan) == []


def test_injected_context_names_the_unproven_step(sandbox):
    """The working block reminds the agent while the task is still running."""
    ts.archive_and_start_task(task="Half proven", plan=PLAN_TEXT)
    ts.update_plan_step_status("step_1", status="done",
                              verification="tests - 2 passed")
    ts.update_plan_step_status("step_2", status="done")

    block = ts.task_state_for_context()

    assert block is not None
    assert "unverified done steps: step_2" in block
    assert "step_1" not in block.split("unverified done steps:")[1].split("\n")[0]


def test_the_archive_reports_what_it_could_not_prove(sandbox):
    """Archiving an unproven task says so instead of claiming completion."""
    ts.archive_and_start_task(task="Half proven", plan=PLAN_TEXT)
    ts.update_plan_step_status("step_1", status="done",
                              verification="tests - 2 passed")
    ts.update_plan_step_status("step_2", status="done")

    res = ts.clear_task_state()

    assert res["ok"] and res["archived"]
    assert res["unverified_steps"] == ["step_2"]
    assert "step_2" in res["warning"]


def test_a_fully_proven_task_archives_without_a_warning(sandbox):
    """A proven task archives quietly."""
    ts.archive_and_start_task(task="Proven", plan="### Step 1 - Work\n")
    ts.update_plan_step_status("step_1", status="done",
                              verification="pytest tests - 9 passed")

    res = ts.clear_task_state()

    assert res["ok"] and res["archived"]
    assert res["unverified_steps"] == []
    assert "warning" not in res
    assert "unverified done steps" not in (ts.task_state_for_context() or "")
