# -*- coding: utf-8 -*-
"""tests/scenarios/test_child_transcript_readability_scenario.py - parent reads child.

Scenario tier: the whole path a dialog walks when it inspects delegated work -
spawn a child, let it work (or let it die), then read its conversation back.
Each scenario starts from the PARENT's intent, goes through the binding and the
reader exactly as the running service does, and checks what the parent sees.

No model is involved: the binding, the transcript writer and the reader are the
same objects the live code uses, so a failure here is a failure of the path, not
of a mock that drifted away from it.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """A fresh data dir: its own database and its own history tree."""
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


PARENT = "20261104_153000_dlg001"


def _spawn_child(run_id):
    """Do what the service does when it prepares a child run.

    One call: the derived thread id, the workspace binding and the index row.
    """
    from core.orchestration.service import derive_child_thread_id
    from dev_agent import workspace_binding as wb

    child = derive_child_thread_id(PARENT, run_id)
    wb.bind_child_thread(child, parent_thread_id=PARENT,
                         workspace="/tmp/project", slug="dev_agent",
                         display_name="DevAgent",
                         title="Build the quarterly report")
    return child


def _read_as_parent(child):
    """Read the child the way the parent's tool does."""
    from core.orchestration import tools

    class _Record:
        def __init__(self, **kw):
            self._data = kw

        def to_record(self):
            return dict(self._data)

    class _Service:
        def get_record(self, run_id):
            return _Record(run_id="kid", root_run_id="kid", status="done",
                           child_thread_id=child)

        def record_from_store(self, run_id):
            return None

    return tools.read_subagent_thread(run_id="kid", service=_Service())


# ─── scenario 1: the child died before its first step ──────────────────────


def test_scenario_a_child_that_never_started_is_readable_as_empty():
    """given a child bound and gone before its first step,
    when the parent reads its conversation,
    then it sees an empty conversation with a reason - not a missing run.
    """
    child = _spawn_child("aaaaaaaa1111")

    out = _read_as_parent(child)

    assert out["ok"] is True, out.get("error")
    assert out["count"] == 0
    assert out["status"] == "done"
    assert "has not written anything" in out["note"]


# ─── scenario 2: the child worked and the parent inspects it ────────────────


def test_scenario_the_parent_reads_what_the_child_actually_said():
    """given a child that wrote two messages,
    when the parent reads the transcript,
    then both messages come back in order with their absolute indices.
    """
    from dev_agent import transcript as T

    child = _spawn_child("bbbbbbbb2222")
    T.append_messages(
        child,
        [{"role": "user", "content": "Build the report"},
         {"role": "assistant", "content": "Started: reading the data"}],
        parent_thread_id=PARENT)

    out = _read_as_parent(child)

    assert out["ok"] is True
    assert [m["content"] for m in out["messages"]] == [
        "Build the report", "Started: reading the data"]
    assert [m["index"] for m in out["messages"]] == [0, 1]
    assert out["source"] == "transcript"


# ─── scenario 3: a run recorded before this fix ────────────────────────────


def test_scenario_an_old_child_without_a_row_gets_a_usable_answer():
    """given a child recorded by an older build (a file but no index row),
    when the parent reads its conversation,
    then the answer names the real problem and points at the journal.
    """
    from dev_agent import transcript as T

    old_child = PARENT + "__s0ldrun01"
    T.append_messages(old_child, [{"role": "user", "content": "orphan work"}],
                      parent_thread_id=PARENT)

    out = _read_as_parent(old_child)

    assert out["ok"] is False
    assert out["error_code"] == "resume_unavailable"
    assert "no index row" in out["error"]
    assert "read_subagent_state" in out["note"]


# ─── scenario 4: a dialog is not a child ───────────────────────────────────


def test_scenario_a_dialog_keeps_its_own_reading_path(monkeypatch):
    """given a normal dialog row without a transcript,
    when it is read,
    then the legacy messages table still answers it (nothing regressed).
    """
    import core.orchestration.transcript_reader as R
    import core.threads_devagent as td

    calls = []

    def _legacy(thread_id, **kwargs):
        calls.append(thread_id)
        return {"ok": True, "thread_id": thread_id, "total": 1,
                "messages": [{"index": 0, "role": "user",
                              "content": "from the table"}]}

    # monkeypatch, not an assignment: a test that leaves a module global
    # rewired makes the NEXT test file fail for a reason that is not its own.
    monkeypatch.setattr(td, "read_thread_window", _legacy)
    monkeypatch.setattr(R, "_meta", lambda tid: {
        "thread_id": tid, "type": "devagent", "kind": "dialog",
        "transcript_path": None})

    out = R.read_window("a-plain-dialog")

    assert calls == ["a-plain-dialog"]
    assert out["source"] == "messages"
