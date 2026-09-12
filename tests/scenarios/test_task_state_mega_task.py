# -*- coding: utf-8 -*-
"""tests/scenarios/test_task_state_mega_task.py - scenario-level tests for
multi-step task memory (task_state journal).

End-to-end scenarios (given -> when -> then) walking the REAL journal API
(dev_agent.task_state) through a full multi-step task lifecycle:

  Scenario 1 - big multi-step task runs to completion:
               a numbered per-task folder is allocated, step statuses move
               through in_progress -> done with the [~]/[x] Progress markers,
               Analysis/Requests sections keep the reasoning trail, the task
               is archived into Task History, and the journal plus folder
               survive (the journal file is never deleted).

  Scenario 2 - next task continues from the handoff record:
               after an archive, a new task in the same thread reuses the
               SAME journal, gets the next task_NN folder (the `(current)`
               marker moves), and the agent can recover the handoff
               facts/new task_dir from the injected context block alone
               (economy-mode continuation).
"""
from __future__ import annotations

from pathlib import Path

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
        config.ACTIVE_THREAD_ID = "mega_thread_1"
        yield tmp_path
    finally:
        config.set_target_root(old_root)
        config.ACTIVE_THREAD_ID = old_thread


PLAN = (
    "### Step 1 - Data layer\n- verification: run tests/test_data.py\n"
    "### Step 2 - API layer\n- verification: run tests/test_api.py\n"
    "### Step 3 - UI layer\n- verification: run tests/test_ui.py\n"
    "### Step 4 - Docs\n- verification: run tests/test_docs.py\n"
)


# ── Scenario 1: big multi-step task runs to completion ------------------------

def test_big_task_runs_to_completion_keeps_journal(sandbox):
    """Given a fresh thread, run a 4-step task through the full lifecycle."""
    # given: a new task with a 4-step plan
    init = ts.archive_and_start_task(task="Mega API rewrite", plan=PLAN)
    assert init.get("ok")
    journal = ts.task_state_path()
    task_dir = Path(init["task_dir"])
    assert task_dir.name == "task_1"

    # when: record a problem + a question BEFORE investigating, then run
    # steps 1..4 through in_progress -> done (markers must follow)
    ts.update_task_state_section("requests", "- confirm API naming?")
    ts.update_task_state_section("analysis", "- considered JSON vs YAML")
    for step, verification in (
        ("step_1", "tests/test_data.py: 4 passed"),
        ("step_2", "tests/test_api.py: 3 passed"),
        ("step_3", "tests/test_ui.py: 2 passed"),
        ("step_4", "tests/test_docs.py: 1 passed"),
    ):
        assert ts.update_plan_step_status(
            step, status="in_progress").get("ok")
        assert ts.update_plan_step_status(
            step, status="done", verification=verification,
            context=f"{step} completed, next needs {verification}",
        ).get("ok")
    ts.update_task_state_section("handoff", "release note ready - publish next")

    # then: Progress shows [x] for all 4 steps, the counter is not a step
    r = ts.read_task_state()
    progress = r["sections"]["progress"]
    assert "- [x] Step 1 - Data layer" in progress
    assert "- [x] Step 2 - API layer" in progress
    assert "- [x] Step 3 - UI layer" in progress
    assert "- [x] Step 4 - Docs" in progress
    assert "Progress: 4/4 steps done." in progress
    assert r["sections"]["analysis"].startswith("- considered")
    assert "confirm API naming" in r["sections"]["requests"]
    assert r["task_dir"].endswith("task_1")

    # when: archive the completed task
    cleared = ts.clear_task_state()
    assert cleared.get("ok") and cleared.get("archived")

    # then: journal + folder survive, the task is in history with its facts
    assert journal.exists()
    assert ts.current_task_dir() is None or ts.current_task_dir().exists()
    r2 = ts.read_task_state()
    assert len(r2["history"]) == 1
    entry = r2["history"][0]
    assert entry["task"] == "Mega API rewrite"
    assert entry["completed_steps"] == "4/4"


# ── Scenario 2: next task continues from the handoff record -------------------

def test_next_task_reuses_journal_and_continues_from_handoff(sandbox):
    """Given a completed task, the next task in the same thread gets a new
    folder and the context block carries what the agent needs to continue."""
    # given: first task completed and archived with a handoff fact
    first = ts.archive_and_start_task(task="First task", plan=PLAN)
    ts.update_plan_step_status("step_1", status="in_progress")
    ts.update_plan_step_status("step_1", status="done",
                               context="storage API chosen: SQLite")
    ts.update_task_state_section("handoff", "use SQLite; docs pending")
    ts.clear_task_state()

    # when: a new task starts in the same thread
    second = ts.archive_and_start_task(task="Second task", plan=PLAN)
    assert second.get("ok")
    # clear_task_state() already archived the first task, so there is no
    # active content left to archive again - archived_previous is False.
    assert second.get("archived_previous") is False
    assert second["path"] == first["path"]
    assert Path(second["task_dir"]).name == "task_2"

    # then: the injected context block carries the new task, its task_dir,
    # and the handoff/step-context facts an agent needs in economy mode
    block = ts.task_state_for_context()
    assert block is not None
    assert "Second task" in block
    assert "- task_dir:" in block and "task_2" in block
    assert "use SQLite; docs pending" in block
    assert "storage API chosen: SQLite" in block
    assert "## Recent Task History" in block and "First task" in block

    # then: folders are numbered per thread and the marker moved
    r = ts.read_task_state()
    assert r["task_dir"].endswith("task_2")
    base = Path(first["task_dir"]).parent
    assert (base / "task_1").exists()
    assert (base / "task_2 (current)").exists()
