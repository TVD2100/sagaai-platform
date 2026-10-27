# -*- coding: utf-8 -*-
"""tests/test_task_state_prune.py - journal retention (pruning).

Covers dev_agent.task_state.prune_task_state_journals: which journals are
removable (empty ones and orphans of a delegated run), and - more important -
which ones are NOT (too fresh, the live dialog, a journal with history, an
unparseable file). Retention deletes files, so the negative cases carry most
of the weight here.
"""
import os

import pytest

from dev_agent import config
from dev_agent import task_state as ts


OLD = 40  # days: comfortably past the default 7-day threshold


def _age(path, days=OLD):
    """Backdate a file so the retention threshold applies to it."""
    stamp = os.path.getmtime(path) - days * 86400
    os.utime(path, (stamp, stamp))


def _write(directory, thread, text=None, age_days=OLD, parent_exists=True):
    """Create a journal file for *thread* and backdate it.

    With ``parent_exists=False`` the parent journal of a delegated-run id is
    deliberately NOT created, which makes the file an orphan.
    """
    if not parent_exists and ts.SUBAGENT_MARKER in thread:
        pass  # the parent is simply never written
    path = directory / f"{ts.TASK_STATE_PREFIX}{thread}{ts.TASK_STATE_SUFFIX}"
    path.write_text(text if text is not None else ts.build_task_state(), encoding="utf-8")
    _age(path, age_days)
    return path


@pytest.fixture
def journals(tmp_path, monkeypatch):
    """A private journal folder, non-empty task, live thread isolated."""
    directory = tmp_path / "task_states"
    directory.mkdir()
    monkeypatch.setattr(config, "TASK_STATES_DIR", directory, raising=False)
    monkeypatch.setattr(config, "ACTIVE_THREAD_ID", "live_dialog", raising=False)
    return directory


def test_an_old_empty_journal_is_removed(journals):
    """Given an old journal with no task and no history, it is deleted."""
    path = _write(journals, "old_empty")

    report = ts.prune_task_state_journals(journals)

    assert report["ok"] is True
    assert report["removed"] == 1
    assert not path.exists()
    assert report["freed_bytes"] > 0


def test_a_fresh_empty_journal_is_kept(journals):
    """Given a journal younger than the threshold, it survives.

    A journal is scaffolded before its task starts, so a fresh empty file is
    normal - deleting it would only make the scaffold reappear.
    """
    path = _write(journals, "fresh_empty", age_days=0)

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert report["kept"] == 1
    assert path.exists()


def test_a_journal_with_history_is_kept(journals):
    """Given a journal whose Task History is non-empty, it survives."""
    text = ts.build_task_state(
        history=[{"task": "Shipped the feature", "finished": "2026-01-01",
                  "status": "done", "completed_steps": "2/2",
                  "summary": "done"}],
    )
    path = _write(journals, "has_history", text=text)

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert path.exists()


def test_a_journal_with_an_active_task_is_kept(journals):
    """Given a journal with real Active Task content, it survives."""
    text = ts.build_task_state(task="Some unfinished work")
    path = _write(journals, "active_task", text=text)

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert path.exists()


def test_the_live_dialog_journal_is_never_removed(journals):
    """Given the live dialog's own empty journal, it survives."""
    path = _write(journals, "live_dialog")

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert path.exists()


def test_an_orphaned_subagent_journal_is_removed_when_its_run_finished(journals):
    """Given a finished delegated run whose parent journal is gone, it goes.

    The parent is the only reader of a child journal, so a finished run
    behind a missing parent is an archive nobody can open.
    """
    text = ts.build_task_state(task="investigated the parser")
    text = text.replace(
        "### Handoff\n\n_(not set)_",
        "### Handoff\n\n### Runtime outcome (run 1a2b3c4d)\n"
        "- status: done\n- steps_used: 12",
    )
    path = _write(journals, "absent_parent__s1a2b3c4d", text=text)

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 1
    assert not path.exists()


def test_an_orphan_without_a_recorded_outcome_is_kept(journals):
    """Given an orphan whose run never recorded an ending, it survives.

    No outcome means the work may still be in progress (a crash or a killed
    process), so the journal is not evidence that the run is over.
    """
    text = ts.build_task_state(task="started the investigation")
    path = _write(journals, "absent_parent__s5e6f7a8b", text=text)

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert path.exists()


def test_a_subagent_journal_with_a_live_parent_is_kept(journals):
    """Given the parent journal still present, the child with content stays."""
    _write(journals, "present_parent",
           text=ts.build_task_state(task="parent is busy"))
    child = _write(journals, "present_parent__s9f8e7d6c",
                   text=ts.build_task_state(task="child work"))

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert child.exists()


def test_removing_an_empty_parent_does_not_cascade_in_the_same_pass(journals):
    """Given an empty parent and a busy child, one pass removes only the parent.

    Orphan detection uses the folder as it was at scan time: a child must not
    be deleted because its parent disappeared a moment earlier in the same
    scan - that would delete work that is still readable.
    """
    parent = _write(journals, "cascade_parent")
    child = _write(journals, "cascade_parent__sdeadbeef",
                   text=ts.build_task_state(task="child still working"))

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 1
    assert not parent.exists()
    assert child.exists()


def test_an_unparseable_file_is_kept(journals):
    """Given a file that is not a journal, it is left alone."""
    path = _write(journals, "broken", text="not a journal at all\n")

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert path.exists()


def test_dry_run_reports_without_deleting(journals):
    """Given dry_run, the report names the candidate but the file stays."""
    path = _write(journals, "dry_run_candidate")

    report = ts.prune_task_state_journals(journals, dry_run=True)

    assert report["dry_run"] is True
    assert report["removed"] == 1
    assert path.name in report["removed_files"]
    assert path.exists()


def test_non_journal_files_are_ignored(journals):
    """Given unrelated files in the folder, the scan does not touch them."""
    other = journals / "notes.txt"
    other.write_text("keep me\n", encoding="utf-8")
    _age(other)

    report = ts.prune_task_state_journals(journals)

    assert report["scanned"] == 0
    assert other.exists()


def test_a_missing_folder_is_not_an_error(tmp_path):
    """Given no journal folder yet, the report is empty and ok."""
    report = ts.prune_task_state_journals(tmp_path / "does_not_exist")

    assert report["ok"] is True
    assert report["scanned"] == 0
    assert report["removed"] == 0


def test_the_report_counts_match_the_folder(journals):
    """Given a mixed folder, scanned == removed + kept."""
    _write(journals, "gone_empty")
    _write(journals, "stays_fresh", age_days=0)
    _write(journals, "stays_busy", text=ts.build_task_state(task="busy"))

    report = ts.prune_task_state_journals(journals)

    assert report["scanned"] == 3
    assert report["removed"] == 1
    assert report["kept"] == 2
    assert report["scanned"] == report["removed"] + report["kept"]


def test_the_once_wrapper_scans_only_once(monkeypatch, journals):
    """Given the process already pruned, a second call skips the scan."""
    monkeypatch.setattr(ts, "_PRUNE_DONE", False, raising=False)
    _write(journals, "old_empty")

    first = ts.prune_task_state_journals_once(journals)
    second = ts.prune_task_state_journals_once(journals)

    assert first.get("skipped") is None
    assert first["removed"] == 1
    assert second.get("skipped") is True
    assert second["removed"] == 0


def test_a_journal_written_during_the_scan_is_kept(journals, monkeypatch):
    """Given a journal that is written to WHILE the sweep runs, it survives.

    The emptiness check and the unlink are two separate moments: a resumed
    dialog or a background child can write into an old empty journal between
    them. The sweep re-reads the mtime right before the unlink, so a file
    touched in that window is kept instead of being deleted with fresh work
    inside it.
    """
    path = _write(journals, "woken_up")
    real_is_empty = ts._journal_is_empty

    def touch_then_report(target):
        # A concurrent writer lands between the emptiness check and the unlink.
        result = real_is_empty(target)
        target.write_text(ts.build_task_state(task="Work arrived"), encoding="utf-8")
        return result

    monkeypatch.setattr(ts, "_journal_is_empty", touch_then_report)

    report = ts.prune_task_state_journals(journals)

    assert report["removed"] == 0
    assert report["kept"] == 1
    assert path.exists()
    assert "Work arrived" in path.read_text(encoding="utf-8")


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
