# -*- coding: utf-8 -*-
"""tests/scenarios/test_transcript_lifecycle_scenario.py - a child from birth to recovery.

The unit tests check one function at a time. This file walks the whole life
of a delegated run the way the platform actually uses it, because the pieces
only help together:

    a child writes its conversation
      -> the writing process dies
        -> the parent reads what the child did
          -> the parent resumes it from where it stopped
            -> months later, retention reclaims the space

Each scenario states a given/when/then and goes through the public entry
points only: ``transcript``, ``transcript_reader`` and ``resume``. None of
them reaches into a private helper, so a scenario that passes is evidence
about the modules, not about the internals of one test.
"""
import gzip
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.orchestration import transcript_reader as R  # noqa: E402
from core.orchestration.resume import build_resume  # noqa: E402
from dev_agent import transcript as T  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """A private history tree and index row for every scenario."""
    monkeypatch.setattr(T, "history_root", lambda: tmp_path)
    rows = {}
    monkeypatch.setattr(R, "_meta", lambda tid: rows.get(tid) or {
        "transcript_path": f"{tid}/transcript.jsonl",
        "parent_thread_id": None,
    })
    return {"root": tmp_path, "rows": rows}


def _say(thread_id, role, text, parent=None, store=None):
    """Append one message the way the runner does.

    The index row is registered here because a child is only findable through
    its parent: the reader resolves the folder from the row, and a fake that
    always answered "no parent" would look for a child at the top level where
    nothing was ever written.
    """
    if store is not None:
        store["rows"][thread_id] = {
            "transcript_path": f"{thread_id}/transcript.jsonl",
            "parent_thread_id": parent,
        }
    T.append_record({"v": 1, "seq": 1, "role": role, "content": text,
                     "ts": ""}, thread_id, parent)


# ─── scenario 1: the child dies and the parent recovers it ─────────────────

def test_a_dead_child_is_readable_and_resumable(store):
    """given a child that wrote its work, when its process is gone, then the
    parent can read it and resume it without redoing what is on disk."""
    # given: a child that worked two steps and then died
    _say("child-1", "user", "write the parser", parent="dlg-1", store=store)
    _say("child-1", "assistant", '{"tool": "propose_file", "args": {"path": "core/parser.py"}}',
         parent="dlg-1", store=store)
    checkpoint = T.build_checkpoint(0, step=2, max_steps=6,
                                    written=["core/parser.py"], status="failed")
    T.append_checkpoint("child-1", checkpoint, 3, "dlg-1")

    # when: the parent reads the child and builds the continuation
    window = R.read_window("child-1")
    record = {"run_id": "run-1", "status": "failed", "agent_slug": "dev_agent",
              "goal": "write the parser", "child_thread_id": "child-1"}
    resume = build_resume(record,
                          read_checkpoint=lambda tid: T.last_checkpoint(tid, "dlg-1") or {})

    # then: it sees the conversation, not the progress marker, and the hint
    # names the file that already exists
    assert [m["role"] for m in window["messages"]] == ["user", "assistant"]
    assert resume["ok"] is True
    assert "core/parser.py" in resume["resume_hint"]
    assert "step 2 of 6" in resume["resume_hint"]


def test_a_torn_last_line_does_not_hide_the_rest(store):
    """given a write cut off by a crash, when the child is read, then every
    complete record before the cut is still there."""
    _say("child-2", "user", "first", parent="dlg-2", store=store)
    _say("child-2", "assistant", "second", parent="dlg-2", store=store)
    active = T.thread_dir("child-2", "dlg-2") / T.TRANSCRIPT_NAME
    with open(active, "a", encoding="utf-8") as handle:
        handle.write('{"role": "assistant", "content": "half a me')

    window = R.read_window("child-2")

    assert [m["content"] for m in window["messages"]] == ["first", "second"]


# ─── scenario 2: an old store is reclaimed without losing a message ────────

def test_retention_shrinks_the_store_and_keeps_it_readable(store):
    """given a large old conversation, when retention runs, then the cold
    segments shrink and the conversation still reads in full."""
    # given: a dialog that rotated, with a cold segment
    for i in range(20):
        _say("dlg-3", "user", f"message {i} " + "x" * 200)
    folder = T.thread_dir("dlg-3")
    original = T.MAX_FILE_BYTES
    try:
        T.MAX_FILE_BYTES = 0            # force a rotation
        T._active_segment(folder)
    finally:
        T.MAX_FILE_BYTES = original
    segment = folder / "transcript.1.jsonl"
    ancient = time.time() - 30 * 86400
    os.utime(segment, (ancient, ancient))
    before = [m["content"] for m in R.read_window("dlg-3")["messages"]]

    # when: retention runs for real
    result = T.sweep(dry_run=False, is_known=lambda tid: True)

    # then: space was reclaimed and not a single message went missing
    assert result["compressed"]
    assert segment.exists() is False
    assert (folder / "transcript.1.jsonl.gz").exists() is True
    after = [m["content"] for m in R.read_window("dlg-3")["messages"]]
    assert after == before


def test_a_dry_run_leaves_the_store_exactly_as_it_was(store):
    """given any store, when retention is asked what it WOULD do, then nothing
    on disk changes - not one byte."""
    _say("dlg-4", "user", "hello")
    folder = T.thread_dir("dlg-4")
    active = folder / T.TRANSCRIPT_NAME
    ancient = time.time() - 400 * 86400
    os.utime(active, (ancient, ancient))
    before = active.stat().st_size

    report = T.sweep(is_known=lambda tid: False)

    assert report["expired"]
    assert active.stat().st_size == before
    assert T.exists("dlg-4") is True


# ─── scenario 3: an unreachable child is reported, not silently kept ───────

def test_an_unreachable_child_is_reported_before_it_is_ever_removed(store):
    """given a child folder whose row is gone, when retention runs without the
    purge flag, then the folder is reported and still on disk."""
    _say("dlg-5", "user", "parent work")
    _say("lost", "user", "orphan work", parent="dlg-5")

    report = T.sweep(dry_run=False, is_known=lambda tid: False)

    assert [o["thread_id"] for o in report["orphans"]] == ["lost"]
    assert T.thread_dir("lost", "dlg-5").exists() is True


def test_a_known_child_is_never_reported_as_unreachable(store):
    """given a live child, when retention looks for orphans, then it is not one."""
    _say("dlg-6", "user", "parent work")
    _say("live", "user", "child work", parent="dlg-6")

    assert T.find_orphans(is_known=lambda tid: tid == "live") == []


# ─── scenario 4: a legacy transcript written before compression ────────────

def test_a_store_mixing_plain_and_compressed_segments_reads_in_order(store):
    """given one compressed and one plain segment, when the thread is read,
    then both contribute their records in sequence order."""
    folder = T.thread_dir("dlg-7")
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / "transcript.1.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "user", "content": "old"}) + "\n")
    (folder / "transcript.2.jsonl").write_text(
        json.dumps({"role": "assistant", "content": "newer"}) + "\n", encoding="utf-8")
    _say("dlg-7", "user", "newest")

    window = R.read_window("dlg-7")

    assert [m["content"] for m in window["messages"]] == ["old", "newer", "newest"]


def test_the_describe_view_counts_a_compressed_segment(store):
    """given a threaded whose oldest segment was compressed, when the status
    view runs, then the thread is not reported as empty."""
    folder = T.thread_dir("dlg-8")
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / "transcript.1.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "user", "content": "kept"}) + "\n")

    view = R.describe("dlg-8")

    assert view["segments"] == 1
    assert view["file_bytes"] > 0
