# -*- coding: utf-8 -*-
"""The journal belongs to its dialog, not to the workspace it develops.

A task started in one workspace and continued in another is the SAME task, so
its journal must not live in either project: it lives in the dialog's own
folder in the data dir (``history/<tid>/task_states``). A journal written by an
older install into the project runtime folder is moved on first use, with its
per-task working folders, and the content survives the move.

Covered:

- a workspace dialog writes its journal into the dialog folder, outside the
  project;
- a journal created by an older install in the project runtime folder is
  moved on the next write, content and task folders intact;
- changing the workspace keeps the same journal and its Task History;
- an explicit journal folder chosen by a caller (a test, a maintenance
  script) is not mixed with a file from another location.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent import config
from dev_agent import task_state as ts

THREAD = "journal_home_thread"
PLAN_TEXT = "### Step 1 - first\n- verification: run tests\n"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Temp workspace, temp data dir, isolated thread and default paths."""
    import core.paths as paths_mod

    old_state = config.snapshot_state()
    old_history = paths_mod.HISTORY_DIR
    monkeypatch.setattr(paths_mod, "HISTORY_DIR",
                        str(tmp_path / "data" / "history"), raising=False)
    config.set_target_root(tmp_path / "project")
    config.ACTIVE_THREAD_ID = THREAD
    try:
        yield tmp_path
    finally:
        config.restore_state(old_state)
        paths_mod.HISTORY_DIR = old_history


def _history_journal(root: Path) -> Path:
    """Where this dialog's journal is expected to end up."""
    name = f"{ts.TASK_STATE_PREFIX}{THREAD}{ts.TASK_STATE_SUFFIX}"
    return root / "data" / "history" / THREAD / "task_states" / name


def test_journal_lives_in_the_dialog_folder(sandbox):
    """A workspace dialog writes its journal outside the project."""
    assert config.WORKSPACE_SELECTED is True
    res = ts.ensure_task_state_file()
    assert res["ok"] and res["wrote"]
    assert Path(res["path"]) == _history_journal(sandbox)
    assert Path(res["path"]).is_file()


def test_legacy_runtime_journal_is_moved_with_its_content(sandbox):
    """A journal left in the project runtime folder moves on first use."""
    legacy_dir = config.TASK_STATES_DIR
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy = legacy_dir / f"{ts.TASK_STATE_PREFIX}{THREAD}{ts.TASK_STATE_SUFFIX}"
    legacy.write_text(ts.build_task_state(task="Task carried over"), encoding="utf-8")
    legacy_tasks = legacy_dir / THREAD / "task_1"
    legacy_tasks.mkdir(parents=True, exist_ok=True)
    (legacy_tasks / "notes.md").write_text("working notes", encoding="utf-8")

    ts.update_task_state_section("handoff", "facts from the old task")

    home = _history_journal(sandbox)
    assert home.is_file()
    assert not legacy.exists()
    assert (home.parent / THREAD / "task_1" / "notes.md").is_file()
    journal = ts.read_task_state()
    assert journal["sections"]["task"] == "Task carried over"
    assert journal["sections"]["handoff"] == "facts from the old task"


def test_workspace_switch_keeps_the_same_journal(sandbox):
    """Switching the workspace must not lose or duplicate the journal."""
    ts.archive_and_start_task(task="Long task", plan=PLAN_TEXT, thread_id=THREAD)
    first = ts.task_state_path(THREAD)
    assert first == _history_journal(sandbox)

    config.set_target_root(sandbox / "other_project")

    second = ts.task_state_path(THREAD)
    assert second == first
    journal = ts.read_task_state()
    assert journal["sections"]["task"] == "Long task"
    assert journal["step_ids"] == ["step_1"]


def test_explicit_journal_folder_is_not_mixed_with_another_location(sandbox, monkeypatch):
    """A caller that redirects the journal storage means exactly that folder."""
    ts.ensure_task_state_file()
    assert _history_journal(sandbox).is_file()

    isolated = sandbox / "isolated_journals"
    monkeypatch.setattr(config, "TASK_STATES_DIR", isolated, raising=False)

    candidates = ts.journal_candidates(THREAD)
    assert candidates == [isolated]
    res = ts.ensure_task_state_file()
    assert Path(res["path"]).parent == isolated
