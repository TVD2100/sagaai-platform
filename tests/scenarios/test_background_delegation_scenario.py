# -*- coding: utf-8 -*-
"""tests/scenarios/test_background_delegation_scenario.py - two children in the background.

Given a dialog that launches two children with ``background=true``
When it keeps working, then asks for their status and finally stops one of them
Then each child is reported exactly once, the notice reaches the dialog as a
hidden system line rather than a user message, and the cancelled child reports
``cancelled`` instead of overwriting it with ``done``.

Everything runs against a fake child runner, so the scenario measures the
lifecycle - launch, observation, delivery, cancellation - and never a model.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import dev_agent.agent_loop as AL  # noqa: E402
from core.orchestration import tools  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    STATUS_CANCELLED,
    STATUS_DONE,
    STOP_ABORTED,
    STOP_COMPLETED,
)
from core.orchestration.service import OrchestrationService  # noqa: E402

CONFIG = {"max_depth": 1, "max_active_subagents": 3,
          "total_subagent_runs": 8, "subagent_timeout_seconds": 30}


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A private service whose store and journals live in the test folder."""
    from dev_agent import config as dev_config
    from dev_agent import workspace_binding

    monkeypatch.setattr(dev_config, "TASK_STATES_DIR", tmp_path / "journals",
                        raising=False)
    monkeypatch.setattr(workspace_binding, "get_thread_workspace",
                        lambda tid: None, raising=False)
    # The tools read the allowlist and the card of the caller.
    import core.orchestrators as orch_mod
    monkeypatch.setattr(orch_mod, "get_allowed_agents", lambda slug="dev_agent": [])
    monkeypatch.setattr(orch_mod, "get_orchestrator",
                        lambda slug: {"slug": slug, "config": CONFIG}
                        if slug == "dev_agent" else None)

    svc = OrchestrationService()
    svc.set_store_root(str(tmp_path / "runs"))
    _use_service(monkeypatch, svc)
    return svc


def _use_service(monkeypatch, svc):
    """Point the process-wide registry at the private service."""
    import core.orchestration.service as service_mod
    monkeypatch.setattr(service_mod, "get_service", lambda: svc)
    return svc


def _wait_until(predicate, timeout=5.0):
    """Wait for a background thread to reach a state, or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _drain_notices(service, root_id, expect, timeout=5.0):
    """Collect notices until *expect* of them arrived (or the wait expired).

    The record reaches its terminal status inside ``finish_run``, while the
    notice is posted a moment later by the worker, so a test that only waits
    for the status races the mailbox. Collecting until the expected count is
    what makes the assertion about DELIVERY, not about timing.
    """
    collected = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        collected.extend(service.take_notifications(root_id))
        if len(collected) >= expect:
            break
        time.sleep(0.01)
    return collected


def _spawn(service, slug="dev_agent", goal="inspect the module", parent="dialog-1"):
    """Launch one background child through the public tool."""
    return tools.spawn_agent(
        agent_slug=slug, request=AgentBrief(goal=goal), background=True,
        parent_slug="dev_agent", parent_run_id=parent, thread_id="dialog-1",
        parent_config=CONFIG, service=service,
    )


def _state(root_id):
    """A minimal loop state that can receive notices."""
    state = AL.AgentLoopState()
    state.run_id = root_id
    state.root_run_id = root_id
    return state


# ─── given: a dialog with two background children ──────────────────────────


def test_two_children_run_without_blocking_the_dialog(service, monkeypatch):
    """Both launches return at once, before either child has finished."""
    gate = threading.Event()
    started = []
    lock = threading.Lock()

    def _runner(**kwargs):
        with lock:
            started.append(kwargs["run_id"])
        gate.wait(5.0)
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED, summary="ok")

    service.set_child_runner(_runner)
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    first = _spawn(service, goal="read the parser")
    second = _spawn(service, goal="read the writer")

    assert first["ok"] is True and second["ok"] is True
    assert first["background"] is True and second["background"] is True
    # The dialog got its run ids while both children were still blocked.
    assert first["run_id"] != second["run_id"]
    assert _wait_until(lambda: len(started) == 2)
    assert service.get_record(first["run_id"]).status == "running"

    gate.set()
    assert _wait_until(lambda: service.get_record(first["run_id"]).status == STATUS_DONE)


# ─── when: the dialog observes them ────────────────────────────────────────


def test_status_answers_for_one_child_and_for_the_whole_tree(service):
    """The dialog can ask about one run or about everything it launched."""
    service.set_child_runner(lambda **kw: AgentRunResult(
        run_id=kw["run_id"], status=STATUS_DONE, stop_reason=STOP_COMPLETED))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    child = _spawn(service)
    rid = child["run_id"]
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_DONE)

    one = tools.subagent_status(run_id=rid, service=service)
    assert one["ok"] is True
    assert one["status"] == STATUS_DONE
    assert one["background"] is True
    # A finished run is a resume candidate, not a dead end.
    assert one["resumable"] is True

    tree = tools.subagent_status(root_run_id="dialog-1", service=service)
    assert tree["ok"] is True
    assert tree["count"] == 2  # the dialog itself plus its child
    assert rid in {r["run_id"] for r in tree["runs"]}


def test_a_status_wait_returns_the_finished_child(service):
    """``wait_seconds`` turns the same call into a bounded wait."""
    done = threading.Event()

    def _runner(**kwargs):
        done.wait(5.0)
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED)

    service.set_child_runner(_runner)
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service)["run_id"]

    threading.Timer(0.1, done.set).start()
    item = tools.subagent_status(run_id=rid, wait_seconds=5.0, service=service)

    assert item["status"] == STATUS_DONE
    assert not item["run"].get("timed_out")


# ─── then: exactly one notice, delivered as a system line ──────────────────


def test_each_child_reports_exactly_once(service):
    """Two finished children leave two notices - never three, never zero."""
    service.set_child_runner(lambda **kw: AgentRunResult(
        run_id=kw["run_id"], status=STATUS_DONE, stop_reason=STOP_COMPLETED))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)

    ids = {_spawn(service)["run_id"], _spawn(service)["run_id"]}
    assert _wait_until(lambda: all(
        service.get_record(r).status == STATUS_DONE for r in ids))

    notices = _drain_notices(service, "dialog-1", 2)
    assert len(notices) == 2
    assert {n["run_id"] for n in notices} == ids
    assert all(n["type"] == "run_finished" for n in notices)
    # Draining is destructive: the same notice is never shown twice.
    assert service.take_notifications("dialog-1") == []


def test_the_notice_reaches_the_dialog_as_a_hidden_system_line(service):
    """The parent learns about the child without a visible user message."""
    service.set_child_runner(lambda **kw: AgentRunResult(
        run_id=kw["run_id"], status=STATUS_DONE, stop_reason=STOP_COMPLETED))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service)["run_id"]
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_DONE)

    state = _state("dialog-1")
    # The notice is posted by the worker after the record is finished: give it
    # the moment it needs, then drain through the loop's own delivery path.
    assert _wait_until(lambda: service.has_notifications("dialog-1"))
    lines = AL.drain_subagent_notices(state)

    assert len(lines) == 1
    assert lines[0].startswith("[SUBAGENT_NOTICE:" + rid + ":")
    assert "read_subagent_state" in lines[0]
    assert "resume_subagent" in lines[0]
    entry = state.history[-1]
    assert entry["role"] == "system"
    assert entry["hidden"] is True
    assert entry["_notice"] is True
    # The mailbox is empty afterwards, so the next step shows nothing twice.
    assert AL.drain_subagent_notices(state) == []


# ─── stop: a cancelled child must not report success ───────────────────────


def test_cancelling_a_background_child_reports_cancelled(service):
    """The user's Stop wins over the child's own ``done``."""
    gate = threading.Event()

    def _runner(**kwargs):
        gate.wait(5.0)
        return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                              stop_reason=STOP_COMPLETED, summary="too late")

    service.set_child_runner(_runner)
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service)["run_id"]
    assert _wait_until(lambda: service.get_record(rid) is not None
                       and service.get_record(rid).status == "running")

    report = service.cancel("dialog-1", cascade=True)
    assert report.cancelled is True
    assert rid in report.children

    gate.set()
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_CANCELLED)

    notices = _drain_notices(service, "dialog-1", 1)
    child_notices = [n for n in notices if n["run_id"] == rid]
    assert len(child_notices) == 1
    assert child_notices[0]["status"] == STATUS_CANCELLED
    assert child_notices[0]["payload"]["stop_reason"] == STOP_ABORTED


def test_a_cancelled_child_is_not_reported_as_resumable_success(service):
    """Status must agree with the notice: cancelled is cancelled."""
    service.set_child_runner(lambda **kw: AgentRunResult(
        run_id=kw["run_id"], status=STATUS_CANCELLED, stop_reason=STOP_ABORTED))
    service.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    rid = _spawn(service)["run_id"]
    assert _wait_until(lambda: service.get_record(rid).status == STATUS_CANCELLED)

    item = tools.subagent_status(run_id=rid, service=service)

    assert item["status"] == STATUS_CANCELLED
    # A cancelled run can still be continued - it just did not finish.
    assert item["resumable"] is True
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
