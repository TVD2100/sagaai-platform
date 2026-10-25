# -*- coding: utf-8 -*-
"""tests/scenarios/test_delegation_run_scenario.py - one delegated run, start to finish.

Given a dialog registered as the root run with a small budget
When it spawns two children and the user presses Stop
Then both children are cancelled, the feed receives their events, and the
tree refuses further spawns with a limit the model can read.

Everything runs against a fake child runner: the scenario measures the
lifecycle (records, events, cancellation, limits), not model output.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.orchestration.service import OrchestrationService  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    ERR_CANCELLED_BY_PARENT,
    ERR_LIMIT_EXCEEDED,
    SpawnRequest,
    STATUS_CANCELLED,
    STATUS_DONE,
    STOP_ABORTED,
    STOP_COMPLETED,
)


@pytest.fixture
def service(tmp_path):
    svc = OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))

    def _runner(**kwargs):
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED,
                              summary="child " + str(kwargs["child_slug"]) + " done",
                              steps_used=2)

    svc.set_child_runner(_runner)
    return svc


def _request(slug, parent_run_id="dialog-1"):
    return SpawnRequest(agent_slug=slug, request=AgentBrief(goal="do it"),
                        parent_run_id=parent_run_id)


CONFIG = {"max_depth": 1, "max_active_subagents": 3,
          "total_subagent_runs": 4, "subagent_timeout_seconds": 30}


def test_two_children_report_into_the_dialog_feed(service):
    """A dialog sees its children's lifecycle events without polling."""
    service.ensure_run("dialog-1", thread_id="dialog-1", agent_slug="dev_agent",
                       config=CONFIG)
    seen = []
    service.subscribe("dialog-1", seen.append)

    first = service.run(_request("helper"), parent_config=CONFIG)
    second = service.run(_request("dev_agent"), parent_config=CONFIG)

    assert first["ok"] is True and second["ok"] is True
    assert first["result"]["status"] == STATUS_DONE

    types = [ev["type"] for ev in seen]
    assert types.count("run_started") == 2
    assert types.count("run_finished") == 2
    # Both children are attributed to the dialog's own depth + 1.
    assert {ev["depth"] for ev in seen} == {1}
    assert {ev["agent_slug"] for ev in seen} == {"helper", "dev_agent"}


def test_each_child_gets_its_own_thread_and_journal(service):
    """Children never share a thread id: a shared id would mix journals."""
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    first = service.run(_request("helper"), parent_config=CONFIG)
    second = service.run(_request("helper"), parent_config=CONFIG)

    run_a = first["result"]["run_id"]
    run_b = second["result"]["run_id"]
    thread_a = service.get_record(run_a).thread_id
    thread_b = service.get_record(run_b).thread_id

    assert thread_a != thread_b
    assert thread_a.startswith("dialog-1__s")
    assert thread_b.startswith("dialog-1__s")
    assert run_a[:8] in thread_a


def test_stop_cancels_running_children_with_a_reason(service):
    """The user's Stop reaches children that never see the dialog's flag."""
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    service.ensure_run("child-1", parent_run_id="dialog-1", depth=1)

    report = service.cancel("dialog-1", cascade=True)

    assert report.cancelled is True
    assert report.children == ["child-1"]
    child = service.get_record("child-1")
    assert child.status == STATUS_CANCELLED
    assert child.stop_reason == STOP_ABORTED
    assert child.error_code == ERR_CANCELLED_BY_PARENT


def test_budget_of_runs_is_exhausted_with_a_readable_refusal(service):
    """After total_subagent_runs the model gets a limit it can act on."""
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    assert service.run(_request("helper"), parent_config=CONFIG)["ok"] is True
    assert service.run(_request("helper"), parent_config=CONFIG)["ok"] is True
    assert service.run(_request("helper"), parent_config=CONFIG)["ok"] is True

    refused = service.run(_request("helper"), parent_config=CONFIG)
    assert refused["ok"] is False
    assert refused["error_code"] == ERR_LIMIT_EXCEEDED
    assert refused["limit"] == "total_subagent_runs"
    assert refused["value"] == 4
    assert refused["used"] == 4


def test_a_finished_dialog_releases_its_tree(service):
    """No record or semaphore survives the dialog that owned it."""
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    service.run(_request("helper"), parent_config=CONFIG)

    service.release_tree("dialog-1")

    assert service.get_record("dialog-1") is None
    assert service.pool_active("dialog-1") == 0
    # A fresh dialog reuses the same id without inheriting the old budget.
    service.ensure_run("dialog-1", config=CONFIG)
    assert service.run(_request("helper"), parent_config=CONFIG)["ok"] is True
