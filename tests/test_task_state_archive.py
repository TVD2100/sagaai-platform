# -*- coding: utf-8 -*-
"""What the Task History of a journal has to preserve.

An archived task is the only record left after the Active Task is replaced by
the next piece of work, so the archive must survive a render-parse round trip
with the parts a later reader needs:

- the PLAN, not just a count of its steps - ``2/3`` does not say what the
  third step was;
- the per-task working folder, so the notes written during the task stay
  reachable;
- the verification lines of the finished steps;
- multi-line step facts with their line structure, not flattened to one line.
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
    "- verification: unit tests\n"
    "### Step 2 - Export\n"
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
    config.ACTIVE_THREAD_ID = "archive_thread"
    try:
        yield tmp_path
    finally:
        config.restore_state(old_state)
        paths_mod.HISTORY_DIR = old_history


def test_archive_keeps_the_plan_and_the_working_folder(sandbox):
    """A finished task is archived with its plan and its task folder."""
    ts.archive_and_start_task(task="Build the exporter", plan=PLAN_TEXT)
    ts.update_plan_step_status("step_1", status="done",
                              verification="pytest tests/test_export.py")
    task_dir = ts.read_task_state()["task_dir"]
    assert task_dir

    ts.archive_and_start_task(task="Next task", plan="### Step 1 - Other\n")

    entry = ts.read_task_state()["history"][0]
    assert entry["task"] == "Build the exporter"
    assert "Step 1 - Data layer" in entry["plan"]
    assert "Step 2 - Export" in entry["plan"]
    assert entry["task_dir"] == task_dir


def test_archive_keeps_the_verification_of_a_finished_step(sandbox):
    """An archived step records how it was proven, not only what it did."""
    ts.archive_and_start_task(task="Task with proof", plan=PLAN_TEXT)
    ts.update_plan_step_status(
        "step_1", status="done",
        verification="tests/test_export.py - 12 passed",
        result="exporter written",
    )

    ts.archive_and_start_task(task="Next", plan="### Step 1 - Other\n")

    summary = ts.read_task_state()["history"][0]["summary"]
    assert "tests/test_export.py - 12 passed" in summary
    assert "exporter written" in summary


def test_archived_plan_survives_a_render_parse_round_trip(sandbox):
    """The journal file itself carries the plan back after a reload."""
    ts.archive_and_start_task(task="Round trip", plan=PLAN_TEXT)
    ts.archive_and_start_task(task="Next", plan="### Step 1 - Other\n")

    path = ts.task_state_path()
    from_path = ts.read_task_state_at(str(path))
    assert from_path["history"][0]["plan"].count("### Step") == 2


def test_multi_line_step_facts_keep_their_lines(sandbox):
    """A result written as a list stays a list inside the step block."""
    ts.archive_and_start_task(task="Multiline", plan=PLAN_TEXT)
    ts.update_plan_step_status(
        "step_1", status="done",
        result="first finding\nsecond finding",
    )

    plan = ts.read_task_state()["sections"]["plan"]
    assert "- result: first finding" in plan
    assert "  second finding" in plan


def test_progress_entry_does_not_repeat_the_status_marker(sandbox):
    """The Progress checklist shows the title, never the raw status."""
    ts.archive_and_start_task(task="Progress", plan=PLAN_TEXT)
    ts.update_plan_step_status("step_1", status="done")

    progress = ts.read_task_state()["sections"]["progress"]
    assert "[x] Step 1 - Data layer" in progress
    assert "(status:" not in progress
