# -*- coding: utf-8 -*-
"""tests/test_child_thread_binding_index.py - a child stays readable from bind on.

A delegated child's conversation lives in a transcript FILE, and every reader
reaches that file through the thread index ROW. The row used to be created
only once the child runner opened its transcript sink, so any run whose runner
never got there - a replaced runner, a crash before the first step, a
cancellation while preparing - had a complete file on disk that no reader
could address, and every read answered "thread not found".

The tests below pin the two halves of the fix: the binding creates the row
(idempotently and without ever failing the run), and a reader tells "has not
spoken yet" apart from "does not exist".
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


PARENT = "20261104_120000_par001"
CHILD = PARENT + "__s1a2b3c4d"


def _meta(thread_id):
    """Read the index row through the real repository."""
    from storage.repository_devagent import repo_devagent_load_thread_meta
    return repo_devagent_load_thread_meta(thread_id) or {}


def _bind(**kwargs):
    """Bind the standard child thread, with overridable arguments."""
    from dev_agent import workspace_binding as wb
    params = {"parent_thread_id": PARENT, "workspace": "/tmp/project",
              "slug": "dev_agent", "display_name": "DevAgent",
              "title": "Build the report"}
    params.update(kwargs)
    return wb.bind_child_thread(CHILD, **params)


# ─── the row that makes a child addressable ─────────────────────────────────


def test_binding_creates_the_child_index_row():
    """Without the row a child is unreadable however complete its file is."""
    _bind()

    row = _meta(CHILD)
    assert row["thread_id"] == CHILD
    assert row["parent_thread_id"] == PARENT
    assert row["kind"] == "subagent"
    assert row["assistant_id"] == "dev_agent"
    assert row["title"] == "Build the report"
    assert row["workspace"] == "/tmp/project"


def test_the_row_points_at_the_transcript_relative_to_history():
    """The index must survive a moved installation, like every other row."""
    _bind()

    stored = _meta(CHILD)["transcript_path"]
    assert stored
    assert not os.path.isabs(stored)
    assert "subagents" in stored, "a child belongs inside its parent folder"
    assert CHILD in stored


def test_binding_twice_keeps_one_row():
    """A resumed or retried run reuses its row instead of duplicating it."""
    _bind()
    first = _meta(CHILD)

    _bind(title="a different title")

    second = _meta(CHILD)
    assert second["thread_id"] == CHILD
    # The existing row is not rewritten: its counters and title stay true to
    # the run that owns them.
    assert second["title"] == first["title"]


def test_an_empty_child_id_binds_nothing():
    """A missing id is a no-op, not an exception for the caller."""
    from dev_agent import workspace_binding as wb

    state = wb.bind_child_thread("", parent_thread_id=PARENT)

    assert state == {"workspace": None, "target_file": None}


def test_a_failing_store_never_breaks_the_binding(monkeypatch):
    """The index is an aid to debugging, not a precondition for work."""
    import storage.repository_devagent as repo
    monkeypatch.setattr(repo, "repo_devagent_load_thread_meta", lambda tid: {})
    monkeypatch.setattr(repo, "repo_devagent_create_thread", lambda *a, **k: False)

    state = _bind()

    assert state["workspace"] == "/tmp/project", "the run must still be bound"
    assert _meta(CHILD) == {}


def test_a_child_row_never_appears_in_the_dialog_list():
    """A child is run bookkeeping; listing it as a dialog would be a lie."""
    from storage.repository_devagent import repo_devagent_list_threads

    _bind()

    assert [t["thread_id"] for t in repo_devagent_list_threads()] == []


# ─── a child that has not spoken yet ────────────────────────────────────────


def _reader():
    """Import the reader module the tools call."""
    from core.orchestration import transcript_reader
    return transcript_reader


def test_a_bound_child_reads_as_an_empty_conversation():
    """A run that has not taken its first step is empty, not missing.

    This is the half of the defect a reader used to get wrong on its own: the
    row exists, the file does not, and the answer must describe an empty
    conversation instead of "thread not found".
    """
    _bind()

    out = _reader().read_window(CHILD)

    assert out["ok"] is True
    assert out["source"] == "transcript"
    assert out["total"] == 0
    assert out["messages"] == []
    assert "has not written anything" in out["note"]


def test_tail_and_around_of_a_pending_child_are_empty_too():
    """Every mode must answer the same way about the same state."""
    _bind()

    tail = _reader().read_tail(CHILD, limit=10)
    around = _reader().read_around(CHILD, index=3, before=2, after=2)

    for out in (tail, around):
        assert out["ok"] is True
        assert out["messages"] == []
        assert "has not written anything" in out["note"]


def test_describing_a_pending_child_names_the_transcript_source():
    """A description of a child must not claim a table that never held it."""
    _bind()

    out = _reader().describe(CHILD)

    assert out["ok"] is True
    assert out["source"] == "transcript"
    assert out["transcript_on_disk"] is False
    assert out["message_count"] == 0
    assert "has not written anything" in out["note"]


def test_a_child_message_appears_once_it_is_written():
    """The empty answer is a state, not a cap: the first message shows up."""
    from dev_agent import transcript as T

    _bind()
    T.append_messages(CHILD, [{"role": "user", "content": "go"},
                              {"role": "assistant", "content": "done"}],
                      parent_thread_id=PARENT)

    out = _reader().read_window(CHILD)

    assert out["source"] == "transcript"
    assert [m["content"] for m in out["messages"]] == ["go", "done"]


# ─── what the tool answers ──────────────────────────────────────────────────


class _Record:
    """A live run record with the fields the tools read."""

    def __init__(self, **kw):
        self._data = kw

    def to_record(self):
        return dict(self._data)


class _Service:
    """A registry holding exactly one run."""

    def __init__(self, record):
        self._record = record

    def get_record(self, run_id):
        return self._record if run_id == self._record.to_record()["run_id"] else None

    def record_from_store(self, run_id):
        return None


def _tool_for(child_thread_id):
    """Run read_subagent_thread against a child record of the given thread."""
    from core.orchestration import tools
    record = _Record(run_id="kid-1", root_run_id="kid-1",
                     child_thread_id=child_thread_id, status="done")
    return tools.read_subagent_thread(run_id="kid-1",
                                      service=_Service(record))


def test_the_tool_reads_the_transcript_of_a_bound_child():
    """The regression this whole change exists for: no more "not found".

    given a child thread bound before its first step,
    when its conversation is written and the parent reads it,
    then the parent gets the messages instead of a "not found" error.
    """
    from dev_agent import transcript as T

    _bind()
    T.append_messages(CHILD, [{"role": "user", "content": "first"},
                              {"role": "assistant", "content": "second"}],
                      parent_thread_id=PARENT)

    out = _tool_for(CHILD)

    assert out["ok"] is True
    assert [m["content"] for m in out["messages"]] == ["first", "second"]
    assert out["child_thread_id"] == CHILD
    assert out["run_id"] == "kid-1"


def test_the_tool_reports_a_child_without_a_row_as_unaddressable():
    """A transcript no reader can reach is a journal problem, not a retry."""
    out = _tool_for("20261104_120000_par001__smissing")

    assert out["ok"] is False
    assert out["error_code"] == "resume_unavailable"
    assert "no index row" in out["error"]
    assert "read_subagent_state" in out["note"]


def test_an_empty_window_is_not_mistaken_for_a_failure():
    """A bound child with no messages answers ok with a note about it."""
    _bind()

    out = _tool_for(CHILD)

    assert out["ok"] is True
    assert out["count"] == 0
    assert "has not written anything" in out["note"]


# ─── what must NOT change ───────────────────────────────────────────────────


def test_a_legacy_dialog_still_falls_back_to_the_messages_table(monkeypatch):
    """A dialog written before the file store keeps opening unchanged."""
    import core.orchestration.transcript_reader as R
    import core.threads_devagent as td
    sentinel = {"ok": True, "thread_id": "old-dialog", "total": 1,
                "messages": [{"index": 0, "role": "user",
                              "content": "from the table"}]}
    monkeypatch.setattr(td, "read_thread_window", lambda *a, **k: dict(sentinel))
    monkeypatch.setattr(R, "_meta", lambda tid: {
        "thread_id": tid, "type": "devagent", "title": "Old",
        "transcript_path": None})

    out = R.read_window("old-dialog")

    assert out["source"] == "messages"
    assert out["messages"][0]["content"] == "from the table"
    assert "note" not in out
