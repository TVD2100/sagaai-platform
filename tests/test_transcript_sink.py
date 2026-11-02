# -*- coding: utf-8 -*-
"""tests/test_transcript_sink.py - persisting a running child as it works.

The sink is the bridge between a live agent loop and the file store, so its
failures are the ones that matter: a child that runs but is not addressable
afterwards, a message offered but not written yet counted as stored, or a
counter multiplied by the number of flushes. Each is pinned here against a
real (temporary) devagent database, because the sink's whole job is to keep
that database and the transcript file agreeing with each other.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point the platform at a fresh data dir with a fresh devagent DB."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import storage.db as db_mod
    db_mod.reset_engine()
    db_mod.reset_devagent_engine()

    import core.paths as paths_mod
    importlib.reload(paths_mod)
    importlib.reload(db_mod)

    yield tmp_path

    db_mod.reset_engine()
    db_mod.reset_devagent_engine()


class FakeState:
    """The one part of AgentLoopState the sink touches."""

    def __init__(self, history=None):
        self.history = list(history or [])


def _sink(thread_id="child-1", parent="root-1", slug="dev_agent"):
    """Build a sink pointed at this test's database and history root."""
    from core.orchestration.transcript_sink import TranscriptSink
    return TranscriptSink(thread_id=thread_id, parent_thread_id=parent,
                          slug=slug, display_name="DevAgent",
                          workspace="/tmp/project")


def _msgs(*pairs):
    """Build history messages from (role, content) pairs."""
    return [{"role": r, "content": c, "ts": "2026-11-02T14:00:00"}
            for r, c in pairs]


# ─── the row that makes a child addressable ────────────────────────────────


def test_open_creates_the_index_row():
    """Without the row a finished child answers "not found" forever."""
    sink = _sink()

    assert sink.open("Build the report") is True

    meta = _meta("child-1")
    assert meta["thread_id"] == "child-1"
    assert meta["parent_thread_id"] == "root-1"
    assert meta["kind"] == "subagent"
    assert meta["title"] == "Build the report"
    assert meta["assistant_id"] == "dev_agent"


def test_open_records_the_transcript_path():
    """The index must point at the file, or the reader cannot find it."""
    sink = _sink()
    sink.open("task")

    stored = _meta("child-1")["transcript_path"]
    assert stored
    assert not os.path.isabs(stored), "the index must survive a moved install"
    assert "subagents" in stored, "a child belongs inside its parent folder"


def test_open_does_not_duplicate_an_existing_row():
    """A retried or resumed run reuses its row instead of failing on it."""
    first = _sink()
    first.open("task")
    second = _sink()

    assert second.open("task") is True
    assert second.row_ready is True


def test_open_reports_failure_without_raising(monkeypatch):
    """An unusable database must not turn a working run into a failed one."""
    import storage.repository_devagent as repo
    monkeypatch.setattr(repo, "repo_devagent_create_thread",
                        lambda *a, **k: False)
    monkeypatch.setattr(repo, "repo_devagent_load_thread_meta", lambda tid: {})

    sink = _sink("child-broken")

    assert sink.open("task") is False


# ─── draining the live history ─────────────────────────────────────────────


def test_drain_writes_only_the_new_messages():
    """A drain per step must not rewrite the messages of the previous steps."""
    sink = _sink()
    sink.open("task")
    state = FakeState(_msgs(("user", "first"), ("assistant", "second")))

    assert sink.drain(state) == 2
    state.history.extend(_msgs(("user", "third")))
    assert sink.drain(state) == 1

    assert [r["content"] for r in _records("child-1")] == [
        "first", "second", "third"]


def test_drain_of_an_unchanged_state_writes_nothing():
    """Most steps add nothing; the sink must not append duplicates."""
    sink = _sink()
    sink.open("task")
    state = FakeState(_msgs(("user", "only")))
    sink.drain(state)

    assert sink.drain(state) == 0
    assert len(_records("child-1")) == 1


def test_the_task_is_not_written_twice():
    """open() titles the row; the loop writes the real first message."""
    sink = _sink()
    sink.open("the whole task text")
    state = FakeState(_msgs(("user", "the whole task text")))

    sink.drain(state)

    contents = [r["content"] for r in _records("child-1")]
    assert contents == ["the whole task text"]


def test_a_failed_write_is_retried_by_the_next_drain(monkeypatch):
    """A message that never landed must not be skipped forever.

    The cursor advances by what reached the disk, not by the length of the
    history: an outage costs a retry, never a hole in the transcript.
    """
    from dev_agent import transcript as T
    sink = _sink()
    sink.open("task")
    state = FakeState(_msgs(("user", "first"), ("assistant", "second")))

    real_append = T.append_record
    calls = {"n": 0}

    def _flaky(record, thread_id, parent=None):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"ok": False, "path": "", "bytes": 0, "error": "disk full"}
        return real_append(record, thread_id, parent)

    monkeypatch.setattr(T, "append_record", _flaky)
    written = sink.drain(state)

    assert written == 1, "a failed write must not report success"
    assert sink.error
    # The retry succeeds and the transcript ends up complete.
    monkeypatch.setattr(T, "append_record", real_append)
    sink.drain(state)
    assert [r["content"] for r in _records("child-1")] == ["first", "second"]


def test_the_counts_are_absolute_not_multiplied(monkeypatch):
    """Flushing three times must not triple the message count."""
    sink = _sink()
    sink.open("task")
    state = FakeState(_msgs(("user", "one")))
    sink.drain(state)
    state.history.extend(_msgs(("assistant", "two")))
    sink.drain(state)
    sink.finish(state)

    assert _meta("child-1")["message_count"] == 2


def test_token_totals_accumulate():
    """Cost data rides on the messages and must reach the index."""
    sink = _sink()
    sink.open("task")
    state = FakeState([
        {"role": "assistant", "content": "a",
         "_tokens": {"in": 10, "out": 5, "cache": 2}},
        {"role": "assistant", "content": "b",
         "_tokens": {"in": 3, "out": 1, "cache": 0}},
    ])

    sink.drain(state)

    meta = _meta("child-1")
    assert meta["tokens_in"] == 13
    assert meta["tokens_out"] == 6
    assert meta["tokens_cache"] == 2


def test_finish_reports_what_was_stored():
    """The runner gets a summary it can log, without reading the file back."""
    sink = _sink()
    sink.open("task")
    state = FakeState(_msgs(("user", "one"), ("assistant", "two")))

    summary = sink.finish(state, status="done")

    assert summary["messages"] == 2
    assert summary["bytes"] > 0
    assert summary["thread_id"] == "child-1"
    assert summary["status"] == "done"
    assert summary["error"] == ""


def test_a_crashed_run_still_leaves_what_it_wrote():
    """The whole point: a run that dies keeps everything up to the crash.

    This is what a parent reads to answer "how far did it get" - so the file
    must be complete on disk the moment a message was written, with no
    buffer waiting for a clean shutdown.
    """
    sink = _sink()
    sink.open("task")
    state = FakeState(_msgs(("user", "step one done"), ("assistant", "next")))
    sink.drain(state)

    # No finish(): the process is gone. The transcript is still whole.
    assert [r["content"] for r in _records("child-1")] == [
        "step one done", "next"]


# ─── helpers ───────────────────────────────────────────────────────────────


def _meta(thread_id):
    """Read the index row through the real repository."""
    from storage.repository_devagent import repo_devagent_load_thread_meta
    return repo_devagent_load_thread_meta(thread_id) or {}


def _records(thread_id):
    """Read the transcript records of a child of root-1."""
    from dev_agent import transcript as T
    return list(T.iter_records(thread_id, "root-1"))
