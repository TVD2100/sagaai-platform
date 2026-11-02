# -*- coding: utf-8 -*-
"""tests/scenarios/test_subagent_result_scenario.py - the closed lifecycle.

Given a dialog that starts a child in the background
When it asks for the child's result before the child has finished
Then it is told "not ready" and receives NO result - never a guess
When it stops that child
Then asking again returns the terminal status of the stop, and a child that
     finished normally hands over the text and artifacts it produced.

A fake child runner drives the whole scenario, so it measures the lifecycle -
launch, collection, stop, collection again - and never a model.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    Artifact,
    ERR_NOT_YOUR_RUN,
    STATUS_CANCELLED,
    STATUS_DONE,
    STOP_ABORTED,
    STOP_COMPLETED,
)
from core.orchestration.service import OrchestrationService  # noqa: E402

CONFIG = {"max_depth": 1, "max_active_subagents": 3,
          "total_subagent_runs": 8, "subagent_timeout_seconds": 30}


def _use_service(monkeypatch, svc):
    """Point the process-wide registry at the private service."""
    import core.orchestration.service as service_mod
    monkeypatch.setattr(service_mod, "get_service", lambda: svc)
    return svc


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A private service whose store and journals live in the test folder."""
    from dev_agent import config as dev_config
    from dev_agent import workspace_binding

    monkeypatch.setattr(dev_config, "TASK_STATES_DIR", tmp_path / "journals",
                        raising=False)
    monkeypatch.setattr(workspace_binding, "get_thread_workspace",
                        lambda tid: None, raising=False)
    import core.orchestrators as orch_mod
    monkeypatch.setattr(orch_mod, "get_allowed_agents", lambda slug="dev_agent": [])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": CONFIG}
                        if slug == "dev_agent" else None)

    svc = OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    return _use_service(monkeypatch, svc)


def _wait_until(predicate, timeout=5.0):
    """Wait for a background thread to reach a state, or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _spawn(service, goal="inspect the module", parent="dialog-1", **kwargs):
    """Launch one child through the public tool."""
    return tools.spawn_agent(
        agent_slug="dev_agent", request=AgentBrief(goal=goal),
        parent_slug="dev_agent", parent_run_id=parent, thread_id="dialog-1",
        parent_config=CONFIG, service=service, **kwargs,
    )


def _finished_result(run_id, summary="wrote the report"):
    """The result a child hands back when its work is done."""
    return AgentRunResult(
        run_id=run_id, status=STATUS_DONE, stop_reason=STOP_COMPLETED,
        summary=summary,
        artifacts=[Artifact(path="/tmp/report.md", kind="file", digest="7-9")],
        steps_used=5, tokens_used={"in": 400, "out": 90, "cache": 20},
    )


# ─── given: a background child, then a premature collection ───────────────


def test_a_premature_request_answers_not_ready_and_invents_nothing(service):
    """Partial work is never presented as the result: it would look like one."""
    gate = threading.Event()

    def _runner(**kwargs):
        gate.wait(5.0)
        return _finished_result(kwargs["run_id"], summary="only the first half")

    service.set_child_runner(_runner)
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service, background=True)["run_id"]
    assert _wait_until(lambda: service.get_record(rid) is not None
                       and service.get_record(rid).status == "running")

    early = tools.subagent_result(run_id=rid, parent_run_id="dialog-1",
                                  service=service)

    assert early["ok"] is True
    assert early["ready"] is False
    assert "result" not in early
    assert early["status"] == "running"
    # The state is reported, the produced text is not - it does not exist yet.
    assert "only the first half" not in str(early)

    gate.set()


# ─── when: the child finishes on its own ──────────────────────────────────


def test_a_finished_child_hands_over_its_text_artifacts_and_resources(service):
    """The answer is the same result object a blocking spawn returns."""
    service.set_child_runner(lambda **kw: _finished_result(kw["run_id"]))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service, background=True)["run_id"]
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_DONE)

    out = tools.subagent_result(run_id=rid, parent_run_id="dialog-1",
                                service=service)

    assert out["ready"] is True
    result = out["result"]
    assert result["status"] == STATUS_DONE
    assert result["summary"] == "wrote the report"
    assert result["artifacts"] == [
        {"path": "/tmp/report.md", "kind": "file", "digest": "7-9"}]
    assert result["tokens_used"] == {"in": 400, "out": 90, "cache": 20}
    assert result["steps_used"] == 5


def test_a_blocking_launch_is_collected_the_same_way(service):
    """The origin of the run does not change the shape of its result."""
    service.set_child_runner(lambda **kw: _finished_result(kw["run_id"]))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    blocking = _spawn(service, background=False)
    rid = blocking["result"]["run_id"]

    collected = tools.subagent_result(run_id=rid, parent_run_id="dialog-1",
                                      service=service)

    assert collected["ready"] is True
    assert collected["result"]["summary"] == blocking["result"]["summary"]
    assert set(collected["result"]) == set(blocking["result"])


def test_the_wait_collects_the_child_in_the_same_step(service):
    """A known-to-be-finishing child needs no polling loop."""
    done = threading.Event()

    def _runner(**kwargs):
        done.wait(5.0)
        return _finished_result(kwargs["run_id"])

    service.set_child_runner(_runner)
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service, background=True)["run_id"]

    threading.Timer(0.1, done.set).start()
    out = tools.subagent_result(run_id=rid, wait_seconds=5.0,
                                parent_run_id="dialog-1", service=service)

    assert out["ready"] is True
    assert out["result"]["summary"] == "wrote the report"


# ─── then: a stop is a terminal answer, not a success ─────────────────────


def test_a_stopped_child_answers_with_the_terminal_status_of_the_stop(service):
    """Launch, premature request, stop, collect again - the whole loop."""
    gate = threading.Event()

    def _runner(**kwargs):
        gate.wait(5.0)
        return _finished_result(kwargs["run_id"], summary="finished anyway")

    service.set_child_runner(_runner)
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service, background=True)["run_id"]
    assert _wait_until(lambda: service.get_record(rid) is not None
                       and service.get_record(rid).status == "running")

    early = tools.subagent_result(run_id=rid, parent_run_id="dialog-1",
                                  service=service)
    assert early["ready"] is False and "result" not in early

    stopped = tools.cancel_subagent(run_id=rid, parent_run_id="dialog-1",
                                    reason="not needed any more", service=service)
    assert stopped["cancelled"] is True

    after = tools.subagent_result(run_id=rid, parent_run_id="dialog-1",
                                  service=service)

    assert after["ready"] is True
    assert after["result"]["status"] == STATUS_CANCELLED
    assert after["result"]["stop_reason"] == STOP_ABORTED
    # The child's own "done" must not overwrite the stop.
    assert after["result"]["status"] != STATUS_DONE

    gate.set()
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_CANCELLED)
    settled = tools.subagent_result(run_id=rid, parent_run_id="dialog-1",
                                    service=service)
    assert settled["result"]["status"] == STATUS_CANCELLED


# ─── the answer survives the registry ─────────────────────────────────────


def test_a_run_released_from_the_registry_is_still_collectable(service):
    """The registry is a cache: a finished run keeps its result on disk."""
    service.set_child_runner(lambda **kw: _finished_result(kw["run_id"]))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service, background=True)["run_id"]
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_DONE)

    service.release_tree("dialog-1")
    assert service.get_record(rid) is None

    out = tools.subagent_result(run_id=rid, parent_run_id="dialog-1",
                                service=service)

    assert out["ready"] is True
    assert out["result"]["summary"] == "wrote the report"
    assert out["result"]["artifacts"][0]["path"] == "/tmp/report.md"


# ─── the dialog boundary holds ────────────────────────────────────────────


def test_another_dialogs_run_is_not_collectable(service):
    """Collecting a result must not become a way across the dialog boundary."""
    service.set_child_runner(lambda **kw: _finished_result(kw["run_id"]))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    service.ensure_run("dialog-2", thread_id="dialog-2", config=CONFIG)
    rid = _spawn(service, background=True)["run_id"]
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_DONE)

    out = tools.subagent_result(run_id=rid, parent_run_id="dialog-2",
                                service=service)

    assert out["ok"] is False
    assert out["error_code"] == ERR_NOT_YOUR_RUN
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
