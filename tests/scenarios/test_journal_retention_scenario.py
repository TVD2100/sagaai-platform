# -*- coding: utf-8 -*-
"""tests/scenarios/test_journal_retention_scenario.py - journal retention.

Walks the retention feature the way an install meets it: a workspace that has
served several dialogs and delegated runs for a while, then the agent loop's
first step of a new process. Nothing here calls the helper directly with
hand-made state - the scenario builds a realistic folder, triggers the hook
that production uses, and checks what a user (and the next agent step) sees.
"""
import os

import pytest

from dev_agent import config
from dev_agent import task_state as ts


OLD_DAYS = 40


def _backdate(path, days=OLD_DAYS):
    """Make a file look untouched for *days* days."""
    stamp = os.path.getmtime(path) - days * 86400
    os.utime(path, (stamp, stamp))


def _journal(directory, thread, *, task=None, history=None, outcome=None,
             age_days=OLD_DAYS):
    """Write one journal of a given shape and age; returns its path."""
    text = ts.build_task_state(task=task or "", history=history or [])
    if outcome:
        text = text.replace(
            "### Handoff\n\n_(not set)_",
            "### Handoff\n\n### Runtime outcome (run deadbeef)\n"
            f"- status: {outcome}\n- steps_used: 7",
        )
    path = directory / f"{ts.TASK_STATE_PREFIX}{thread}{ts.TASK_STATE_SUFFIX}"
    path.write_text(text, encoding="utf-8")
    _backdate(path, age_days)
    return path


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A workspace journal folder plus a data dir with dialog journals."""
    runtime = tmp_path / "project" / ".dev_agent" / "task_states"
    runtime.mkdir(parents=True)
    history = tmp_path / "data" / "history"
    (history / "dialog_a" / "task_states").mkdir(parents=True)
    (history / "dialog_b" / "task_states").mkdir(parents=True)

    monkeypatch.setattr(config, "TASK_STATES_DIR", runtime, raising=False)
    monkeypatch.setattr(config, "ACTIVE_THREAD_ID", "dialog_a", raising=False)
    import core.paths as paths
    monkeypatch.setattr(paths, "HISTORY_DIR", str(history), raising=False)
    monkeypatch.setattr(ts, "_PRUNE_DONE", False, raising=False)
    return {"runtime": runtime, "history": history,
            "dialog_a": history / "dialog_a" / "task_states",
            "dialog_b": history / "dialog_b" / "task_states"}


def test_a_long_lived_install_keeps_only_what_it_can_still_read(install):
    """Given months of dialogs, one sweep leaves only readable journals.

    Given  a workspace that served many dialogs: abandoned scaffolds, a
    dialog with real history, an unfinished task, finished delegated runs
    and one run that never reported an ending,
    when   the agent loop starts a new process and runs its retention hook,
    then   only the journals that still hold something readable survive, and
    the reported counters add up to what the folders contain.
    """
    runtime = install["runtime"]
    dialog_a = install["dialog_a"]
    dialog_b = install["dialog_b"]

    # Abandoned scaffolds - opened once, never used.
    _journal(runtime, "abandoned_1")
    _journal(runtime, "abandoned_2")
    _journal(dialog_b, "abandoned_3")

    # A dialog with real history: must survive.
    kept_history = _journal(
        runtime, "productive_dialog",
        history=[{"task": "Built the exporter", "finished": "2026-01-02",
                  "status": "done", "completed_steps": "3/3",
                  "summary": "exporter shipped"}],
    )
    # An unfinished task: must survive.
    kept_task = _journal(runtime, "unfinished_work", task="Half-done migration")
    # The live dialog of the running process: must survive even when empty.
    live = _journal(dialog_a, "dialog_a")

    # Delegated runs: finished ones behind a missing parent are unreadable.
    _journal(runtime, "vanished_parent__s11112222", task="checked the parser",
             outcome="done")
    _journal(runtime, "vanished_parent__s33334444", task="never reported back")

    report = ts.prune_task_state_journals_once()

    # then: every surviving file still holds something a reader can use.
    survivors = {p.name for p in runtime.iterdir()}
    survivors |= {p.name for p in dialog_a.iterdir()}
    survivors |= {p.name for p in dialog_b.iterdir()}

    assert kept_history.name in survivors
    assert kept_task.name in survivors
    assert live.name in survivors
    assert f"{ts.TASK_STATE_PREFIX}vanished_parent__s33334444{ts.TASK_STATE_SUFFIX}" in survivors

    assert f"{ts.TASK_STATE_PREFIX}abandoned_1{ts.TASK_STATE_SUFFIX}" not in survivors
    assert f"{ts.TASK_STATE_PREFIX}abandoned_3{ts.TASK_STATE_SUFFIX}" not in survivors
    assert f"{ts.TASK_STATE_PREFIX}vanished_parent__s11112222{ts.TASK_STATE_SUFFIX}" not in survivors

    # and the counters describe the folders that were scanned.
    assert report["ok"] is True
    assert report["directories"] == 3
    assert report["scanned"] == report["removed"] + report["kept"]
    assert report["removed"] == 4
    assert report["freed_bytes"] > 0


def test_a_second_step_of_the_same_process_does_no_work(install):
    """Given the process pruned once, later agent steps stay cheap.

    Given  a folder with an abandoned scaffold,
    when   the retention hook runs twice within one process (two agent
    steps, or a Streamlit rerun),
    then   the first call removes the file and the second reports a skip
    instead of rescanning the folders.
    """
    runtime = install["runtime"]
    _journal(runtime, "abandoned_once")

    first = ts.prune_task_state_journals_once()
    second = ts.prune_task_state_journals_once()

    assert first["removed"] == 1
    assert second.get("skipped") is True
    assert second["removed"] == 0
    assert list(runtime.iterdir()) == []


def test_a_journal_of_a_running_dialog_is_never_swept(install):
    """Given the running dialog's own empty journal, it survives the sweep.

    Given  an empty journal belonging to the dialog the process is serving,
    when   retention runs,
    then   the file stays: the agent is about to write its task into it.
    """
    live = _journal(install["dialog_a"], "dialog_a")

    report = ts.prune_task_state_journals_once()

    assert report["removed"] == 0
    assert live.exists()


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
