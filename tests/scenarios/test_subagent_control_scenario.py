# -*- coding: utf-8 -*-
"""tests/scenarios/test_subagent_control_scenario.py - user-level scenario
for stopping and inspecting a delegated child.

The scenario walks the same public entry points the agent loop uses, against
a real orchestration service with an isolated store:

  Scenario 1 - a running child is STOPPED by its parent through the tool
               surface: the run ends as cancelled, its descendants are
               cancelled with it, and the result says so.

  Scenario 2 - the same call on a run of ANOTHER dialog is refused, and the
               refused run keeps running: a dialog cannot stop a stranger.

  Scenario 3 - the parent reads the transcript of its own child (what the
               child saw and said), and cannot read the transcript of a run
               outside its tree.

  Scenario 4 - a child that already finished is reported as "nothing to
               cancel" rather than as a failure, so the model is not pushed
               into a retry loop after a correct call.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import core.orchestration.service as S  # noqa: E402
from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    ERR_NOT_YOUR_RUN,
    STATUS_CANCELLED,
    STATUS_DONE,
)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """A private service with its OWN store root and no network."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))
    service = S.OrchestrationService()
    service.set_store_root(str(tmp_path / "orchestration"))
    return service


def _child(service, root, run_id, *, thread_id=""):
    """Register a child record of *root* the way a spawn would."""
    rec = service.ensure_run(run_id, parent_run_id=root, depth=1)
    if thread_id:
        rec.child_thread_id = thread_id
    return rec


# ─── Scenario 1: the parent stops a running child ──────────────────────────


def test_scenario_1_the_parent_stops_a_running_child_and_its_descendants(svc):
    """Given  a parent and a running child with a grandchild,
    when   the parent cancels the child through the tool,
    then   the run ends as cancelled together with its descendants.
    """
    svc.ensure_run("root", thread_id="thread-1")
    child = _child(svc, "root", "child-a", thread_id="th-a")
    grandchild = _child(svc, "child-a", "grand-a")

    result = tools.cancel_subagent(run_id="child-a", parent_run_id="root",
                                   reason="the task is no longer needed",
                                   service=svc)

    assert result["ok"] is True
    assert result["cancelled"] is True
    assert "grand-a" in result["children"]
    assert child.status == STATUS_CANCELLED
    assert grandchild.status == STATUS_CANCELLED
    assert result["reason_text"] == "the task is no longer needed"
    assert "step" in result["note"]


# ─── Scenario 2: another dialog's run is not addressable ────────────────────


def test_scenario_2_a_stranger_run_cannot_be_stopped(svc):
    """Given  a run of another dialog,
    when   this parent tries to cancel it,
    then   the call is refused and the run keeps going.
    """
    svc.ensure_run("root", thread_id="thread-1")
    svc.ensure_run("foreign", thread_id="thread-2")
    victim = _child(svc, "foreign", "foreign-child")

    result = tools.cancel_subagent(run_id="foreign-child",
                                   parent_run_id="root", service=svc)

    assert result["ok"] is False
    assert result["error_code"] == ERR_NOT_YOUR_RUN
    assert victim.status != STATUS_CANCELLED


# ─── Scenario 3: the parent reads its own child's transcript ───────────────


def test_scenario_3_the_parent_reads_its_own_child_transcript(svc, monkeypatch):
    """Given  a child that has been talking to its model,
    when   the parent reads the transcript of the run,
    then   the conversation of the CHILD comes back, not an empty window.
    """
    svc.ensure_run("root", thread_id="thread-1")
    _child(svc, "root", "child-a", thread_id="th-a")
    seen = {}

    def fake_window(thread_id, offset=0, limit=50, include_tool_results=False,
                    allowed_slugs=None):
        seen["thread_id"] = thread_id
        return {"ok": True, "thread_id": thread_id, "total": 1,
                "messages": [{"index": 0, "role": "assistant",
                              "content": "I will inspect the parser first."}]}

    import core.threads_devagent as td
    monkeypatch.setattr(td, "read_thread_window", fake_window)

    result = tools.read_subagent_thread(run_id="child-a", parent_run_id="root",
                                        service=svc)

    assert result["ok"] is True
    assert seen["thread_id"] == "th-a"
    assert result["messages"][0]["content"].startswith("I will inspect")


def test_scenario_3b_a_stranger_transcript_is_refused(svc):
    """Given  a run of another dialog,
    when   this parent asks for its transcript,
    then   the call is refused before any thread is touched.
    """
    svc.ensure_run("root", thread_id="thread-1")
    svc.ensure_run("foreign", thread_id="thread-2")
    _child(svc, "foreign", "foreign-child", thread_id="th-f")

    result = tools.read_subagent_thread(run_id="foreign-child",
                                        parent_run_id="root", service=svc)

    assert result["ok"] is False
    assert result["error_code"] == ERR_NOT_YOUR_RUN


# ─── Scenario 4: cancelling a finished run is not an error ─────────────────


def test_scenario_4_cancelling_a_finished_child_is_a_no_op(svc):
    """Given  a child that already finished,
    when   the parent cancels it anyway,
    then   the answer says there was nothing to cancel, without an error code.
    """
    svc.ensure_run("root", thread_id="thread-1")
    done = _child(svc, "root", "child-a")
    done.status = STATUS_DONE

    result = tools.cancel_subagent(run_id="child-a", parent_run_id="root",
                                   service=svc)

    assert result["ok"] is True
    assert result["cancelled"] is False
    assert "nothing to cancel" in result["note"].lower()
    assert "error_code" not in result
