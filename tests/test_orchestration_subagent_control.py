# -*- coding: utf-8 -*-
"""tests/test_orchestration_subagent_control.py - stopping and inspecting children.

A parent that can launch a child but not stop or inspect it has only one exit
from a wrong child: wait for the timeout. These tests pin the two tools that
close that hole. Every assertion goes through a fake service, so the test
shows WHICH service call the tool makes and with which arguments - a tool that
answers from its own state would pass a weaker test and fail here.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    ERR_INVALID_ARGS,
    ERR_NOT_YOUR_RUN,
    ERR_RESUME_UNAVAILABLE,
    ERR_RUN_NOT_FOUND,
    CancelReport,
)


class FakeRecord:
    """A live run record with the fields the tools read."""

    def __init__(self, **data):
        self._data = data

    def to_record(self):
        return dict(self._data)


class FakeService:
    """A service double that records every call it receives."""

    def __init__(self, records=None, stored=None, cancel_report=None):
        self.calls = []
        self._records = dict(records or {})
        self._stored = dict(stored or {})
        self._cancel_report = cancel_report

    def get_record(self, run_id):
        self.calls.append(("get_record", run_id))
        return self._records.get(run_id)

    def record_from_store(self, run_id):
        self.calls.append(("record_from_store", run_id))
        return self._stored.get(run_id)

    def cancel(self, run_id, cascade=True, **kwargs):
        self.calls.append(("cancel", run_id, cascade))
        if self._cancel_report is not None:
            return self._cancel_report
        return CancelReport(run_id=run_id, cancelled=True)


def _tree():
    """A root record and two children, one of them in another tree."""
    return {
        "root": FakeRecord(run_id="root", root_run_id="root", status="running"),
        "kid": FakeRecord(run_id="kid", root_run_id="root", status="running",
                          child_thread_id="th-kid"),
        "grandchild": FakeRecord(run_id="grandchild", root_run_id="root",
                                 status="running"),
        "stranger": FakeRecord(run_id="stranger", root_run_id="other-tree",
                               status="running"),
    }


# ─── cancel_subagent ────────────────────────────────────────────────────────


def test_cancel_asks_the_service_with_the_cascade_flag():
    """The tool must not cancel on its own: the service owns the run tree."""
    service = FakeService(records=_tree(),
                          cancel_report=CancelReport(run_id="kid", cancelled=True,
                                                     children=["grandchild"]))

    result = tools.cancel_subagent(run_id="kid", cascade=True, service=service)

    assert result["ok"] is True
    assert result["cancelled"] is True
    assert ("cancel", "kid", True) in service.calls


def test_cancel_without_cascade_passes_false():
    """A caller that wants one child stopped gets exactly that."""
    service = FakeService(records=_tree())

    tools.cancel_subagent(run_id="kid", cascade=False, service=service)

    assert ("cancel", "kid", False) in service.calls


def test_cancel_reports_the_descendants_it_took_down():
    service = FakeService(records=_tree(),
                          cancel_report=CancelReport(run_id="kid", cancelled=True,
                                                     children=["grandchild", "g2"]))

    result = tools.cancel_subagent(run_id="kid", service=service)

    assert result["children"] == ["grandchild", "g2"]
    assert "2 descendant" in result["note"]


def test_cancel_of_a_finished_run_says_there_was_nothing_to_do():
    """Cancelling a terminal run is not an error, but it must not claim success."""
    service = FakeService(records=_tree(),
                          cancel_report=CancelReport(run_id="kid", cancelled=False,
                                                     previous_status="done"))

    result = tools.cancel_subagent(run_id="kid", service=service)

    assert result["ok"] is True
    assert result["cancelled"] is False
    assert "nothing to cancel" in result["note"].lower()
    assert "done" in result["note"]


def test_cancel_keeps_the_reason_the_parent_gave():
    """The reason travels into the result so the model sees why work stopped."""
    service = FakeService(records=_tree())

    result = tools.cancel_subagent(run_id="kid", reason="wrong workspace",
                                   service=service)

    assert result["reason_text"] == "wrong workspace"


def test_cancel_needs_a_run_id():
    result = tools.cancel_subagent(run_id="   ", service=FakeService())

    assert result["ok"] is False
    assert result["error_code"] == ERR_INVALID_ARGS
    assert "run_id" in result["missing"]


def test_cancel_of_an_unknown_run_is_reported_as_such():
    """An unknown run is not a silent success."""
    result = tools.cancel_subagent(run_id="ghost", service=FakeService())

    assert result["ok"] is False
    assert result["error_code"] == ERR_RUN_NOT_FOUND


def test_cancel_refuses_a_run_of_another_tree():
    """One dialog must not be able to stop another dialog's work."""
    result = tools.cancel_subagent(run_id="stranger", parent_run_id="root",
                                   service=FakeService(records=_tree()))

    assert result["ok"] is False
    assert result["error_code"] == ERR_NOT_YOUR_RUN
    assert result["root_run_id"] == "other-tree"


def test_cancel_allows_a_run_of_the_same_tree():
    service = FakeService(records=_tree())

    result = tools.cancel_subagent(run_id="grandchild", parent_run_id="kid",
                                   service=service)

    assert result["ok"] is True
    assert ("cancel", "grandchild", True) in service.calls


def test_cancel_reaches_a_run_that_outlived_its_live_record():
    """A run whose tree is gone is still stoppable through the store."""
    service = FakeService(stored={"old": {"run_id": "old", "root_run_id": "root",
                                          "status": "running"}})

    result = tools.cancel_subagent(run_id="old", service=service)

    assert result["ok"] is True
    assert ("cancel", "old", True) in service.calls


# ─── read_subagent_thread ───────────────────────────────────────────────────


@pytest.fixture
def thread_reader(monkeypatch):
    """Replace the window reader and record how it was called."""
    import core.threads_devagent as td

    seen = {}

    def fake_read_thread_window(thread_id, offset=0, limit=50,
                                include_tool_results=False, allowed_slugs=None):
        seen.update(thread_id=thread_id, offset=offset, limit=limit,
                    include_tool_results=include_tool_results)
        return {"ok": True, "thread_id": thread_id, "total": 4,
                "messages": [{"index": offset, "role": "assistant",
                              "content": "working on it"}]}

    monkeypatch.setattr(td, "read_thread_window", fake_read_thread_window)
    return seen


def test_read_thread_uses_the_child_thread_of_the_run(thread_reader):
    service = FakeService(records=_tree())

    result = tools.read_subagent_thread(run_id="kid", service=service)

    assert result["ok"] is True
    assert result["child_thread_id"] == "th-kid"
    assert thread_reader["thread_id"] == "th-kid"


def test_read_thread_forwards_the_window_arguments(thread_reader):
    service = FakeService(records=_tree())

    tools.read_subagent_thread(run_id="kid", offset=20, limit=5,
                               include_tool_results=True, service=service)

    assert thread_reader["offset"] == 20
    assert thread_reader["limit"] == 5
    assert thread_reader["include_tool_results"] is True


def test_read_thread_reports_the_run_status_next_to_the_messages(thread_reader):
    """A transcript is read in the context of the run state."""
    service = FakeService(records=_tree())

    result = tools.read_subagent_thread(run_id="kid", service=service)

    assert result["run_id"] == "kid"
    assert result["status"] == "running"


def test_read_thread_needs_a_run_id():
    result = tools.read_subagent_thread(run_id="", service=FakeService())

    assert result["ok"] is False
    assert result["error_code"] == ERR_INVALID_ARGS


def test_read_thread_of_an_unknown_run_is_reported_as_such():
    result = tools.read_subagent_thread(run_id="ghost", service=FakeService())

    assert result["ok"] is False
    assert result["error_code"] == ERR_RUN_NOT_FOUND


def test_read_thread_explains_a_run_without_a_child_thread(thread_reader):
    """No transcript is a fact to report, not an empty success."""
    service = FakeService(records=_tree())

    result = tools.read_subagent_thread(run_id="root", service=service)

    assert result["ok"] is False
    assert result["error_code"] == ERR_RESUME_UNAVAILABLE
    assert "read_subagent_state" in result["note"]
    assert "thread_id" not in thread_reader


def test_read_thread_refuses_a_run_of_another_tree(thread_reader):
    result = tools.read_subagent_thread(run_id="stranger", parent_run_id="root",
                                        service=FakeService(records=_tree()))

    assert result["ok"] is False
    assert result["error_code"] == ERR_NOT_YOUR_RUN
    assert "thread_id" not in thread_reader


# ─── catalog and routing ────────────────────────────────────────────────────


def test_catalog_entries_of_the_control_tools():
    for entry_fn, name in (
        (tools.cancel_subagent_tool_entry, "cancel_subagent"),
        (tools.read_subagent_thread_tool_entry, "read_subagent_thread"),
    ):
        entry = entry_fn()
        assert entry["name"] == name
        assert entry["desc"].strip()


def test_cancel_entry_states_the_boundary_conditions():
    """The description must warn about the step boundary and the tree limit."""
    desc = tools.cancel_subagent_tool_entry()["desc"]

    assert "step" in desc
    assert "in flight" in desc
    assert "own tree" in desc


def test_both_tools_are_offered_by_the_catalog():
    from core.orchestrator_tools import _delegation_catalog

    names = [entry["name"] for entry in _delegation_catalog()]

    assert "cancel_subagent" in names
    assert "read_subagent_thread" in names


def test_both_tools_are_routed_by_the_delegation_phase():
    """The loop must not send them down the plain tool path."""
    from dev_agent import agent_loop as L
    from dev_agent import universal_agent as UA

    assert "cancel_subagent" in L._DELEGATION_CALL_NAMES
    assert "read_subagent_thread" in L._DELEGATION_CALL_NAMES
    assert "cancel_subagent" in UA.DELEGATION_TOOL_NAMES
    assert "read_subagent_thread" in UA.DELEGATION_TOOL_NAMES


def test_unknown_argument_names_are_refused_before_the_call():
    """A naming mistake is reported as one, not swallowed by the tool."""
    from dev_agent.universal_agent import validate_tool_call

    assert validate_tool_call("cancel_subagent", {"run": "x"}, "dev_agent") is not None
    assert validate_tool_call("read_subagent_thread", {"rid": "x"}, "dev_agent") is not None
    assert validate_tool_call("cancel_subagent", {"run_id": "x", "cascade": False},
                              "dev_agent") is None
