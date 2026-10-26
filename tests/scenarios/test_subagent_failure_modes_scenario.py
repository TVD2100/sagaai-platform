# -*- coding: utf-8 -*-
"""Scenario tests: how a delegated child FAILS and what the parent sees.

The unit tests pin each piece. This file walks the five situations a user
actually creates and asserts what the running system answers:

1. the child loop raises on its own state - the parent must see a platform
   defect it cannot fix by re-running, not a provider hiccup;
2. the provider fails - a different verdict, with a retry worth trying;
3. someone deletes the child journal - an empty read must not look like a
   child that did nothing;
4. the process restarts - the finished run is still readable, journal and
   identity included;
5. a background child is still working - the parent is told it is running,
   not hung, and told not to spawn it twice.

Each scenario goes through the public entry points the model calls (the tool
layer over a real OrchestrationService), never through an internal helper.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.orchestration import service as S  # noqa: E402
from core.orchestration import tools as T  # noqa: E402
from core.orchestration.contracts import (  # noqa: E402
    AgentBrief,
    AgentRunResult,
    ERR_INTERNAL_ERROR,
    ERR_PROVIDER_ERROR,
    STATUS_DONE,
    STATUS_FAILED,
    STOP_COMPLETED,
    STOP_ERROR,
)

CONFIG = {"max_depth": 1, "max_active_subagents": 2,
          "subagent_max_steps": 20, "subagent_timeout_seconds": 30}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolate the journals and the allowlists of the parent dialog."""
    from dev_agent import config as C

    old_root = C.PROJECT_ROOT
    old_thread = C.ACTIVE_THREAD_ID
    C.set_target_root(tmp_path)
    C.ACTIVE_THREAD_ID = "parent_dialog"
    monkeypatch.setattr("core.orchestrators.get_allowed_agents",
                        lambda slug="dev_agent": ["dev_agent"])
    monkeypatch.setattr("core.orchestrators.get_orchestrator",
                        lambda slug: {"slug": slug, "config": CONFIG})
    try:
        yield tmp_path
    finally:
        C.set_target_root(old_root)
        C.ACTIVE_THREAD_ID = old_thread


@pytest.fixture
def service(workspace):
    """A real service with an isolated store and a parent dialog to spawn from."""
    svc = S.OrchestrationService()
    svc.set_store_root(str(workspace / "runs"))
    svc.set_child_runner(_ok_runner)
    svc.ensure_run("dialog-1", thread_id="dialog-1", config=CONFIG)
    return svc


def _ok_runner(**kwargs):
    """A child that finishes its work."""
    return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_DONE,
                          stop_reason=STOP_COMPLETED, summary="did it",
                          steps_used=4, tokens_used={"in": 10, "out": 5})


def _crashing_runner(**kwargs):
    """A child whose loop raises on its own state, before doing any work."""
    raise TypeError("'dict' object has no attribute 'steps'")


def _provider_failure_runner(**kwargs):
    """A child that worked for a while and then lost the provider."""
    return AgentRunResult(run_id=kwargs["run_id"], status=STATUS_FAILED,
                          stop_reason=STOP_ERROR, summary="503 from the provider",
                          error_code=ERR_PROVIDER_ERROR, steps_used=2,
                          retryable=True)


def _spawn(service, **kwargs):
    """Ask for a child the way the dialog does, through the tool layer."""
    params = {"agent_slug": "dev_agent",
              "request": AgentBrief(goal="audit the tree"),
              "parent_run_id": "dialog-1", "thread_id": "dialog-1",
              "parent_config": CONFIG, "service": service}
    params.update(kwargs)
    return T.spawn_agent(**params)


# ─── 1. a crash inside the child loop ─────────────────────────────────────


def test_scenario_a_child_loop_crash_is_a_platform_defect(service):
    """Given a dialog that delegated work to a child,
    when  the child loop raises on its own state before doing anything,
    then  the parent sees a non-retryable platform defect and is told not to
          spawn the same call again - and the child journal explains it even
          though the child itself never wrote a line.
    """
    service.set_child_runner(_crashing_runner)

    out = _spawn(service)

    assert out["ok"] is True
    result = out["result"]
    assert result["status"] == STATUS_FAILED
    assert result["error_code"] == ERR_INTERNAL_ERROR
    assert result["retryable"] is False
    assert "TypeError" in result["summary"]
    assert "platform defect" in out["note"]
    assert "do not spawn" in out["note"].lower()

    journal = T.read_subagent_state(run_id=result["run_id"], service=service)
    handoff = journal["sections"]["handoff"]
    assert "Runtime outcome" in handoff
    assert "internal_error" in handoff
    assert "TypeError" in handoff


# ─── 2. a provider failure is a different verdict ─────────────────────────


def test_scenario_a_provider_failure_is_worth_one_more_try(service):
    """Given a dialog that delegated work to a child,
    when  the child loses the provider after a couple of steps,
    then  the failure is classified as retryable and the parent is told to
          change something rather than repeat the call verbatim.
    """
    service.set_child_runner(_provider_failure_runner)

    out = _spawn(service)

    result = out["result"]
    assert result["status"] == STATUS_FAILED
    assert result["error_code"] == ERR_PROVIDER_ERROR
    assert result["retryable"] is True
    assert result["steps_used"] == 2
    assert "retryable" in out["note"]
    assert "instead of repeating" in out["note"]
    assert "platform defect" not in out["note"]


# ─── 3. a journal that is no longer there ─────────────────────────────────


def test_scenario_a_deleted_journal_does_not_read_as_an_empty_run(service):
    """Given a child that finished and left a journal,
    when  the journal file is gone and the parent asks for it,
    then  the answer says the record is missing - not that the child did
          nothing - and points at the run summary instead.
    """
    out = _spawn(service)
    run_id = out["result"]["run_id"]
    path = service.get_record(run_id).journal_path
    assert os.path.isfile(path)
    os.remove(path)

    read = T.read_subagent_state(run_id=run_id, service=service)

    assert read["ok"] is False
    assert "does not prove" in read["note"]
    assert "subagent_status" in read["note"]


# ─── 4. the process restarted ─────────────────────────────────────────────


def test_scenario_a_finished_run_stays_readable_after_a_restart(service, workspace):
    """Given a child that finished during an earlier session,
    when  a fresh process asks about it,
    then  the durable record still carries its identity and workspace, and
          its journal is readable - the registry is a cache, not the truth.
    """
    out = _spawn(service, base_dir=str(workspace))
    result = out["result"]
    assert result["child_thread_id"]

    fresh = S.OrchestrationService()
    fresh.set_store_root(str(workspace / "runs"))

    stored = fresh.record_from_store(result["run_id"])
    assert stored["child_thread_id"] == result["child_thread_id"]
    assert stored["workspace"] == str(workspace)
    assert stored["journal_path"].endswith(".md")

    read = T.read_subagent_state(run_id=result["run_id"], service=fresh)
    assert read["ok"] is True
    assert "Runtime outcome" in read["sections"]["handoff"]


# ─── 5. a background child that is still working ──────────────────────────


def test_scenario_a_background_child_is_not_a_hang(service):
    """Given a dialog that launched a background child,
    when  the parent checks it while it is still working,
    then  the answer says it is running, not hung, and the launch result told
          the parent not to spawn the same work again; once the child
          finishes the same call reports the outcome.
    """
    gate = threading.Event()

    def _slow_runner(**kwargs):
        gate.wait(5)
        return _ok_runner(**kwargs)

    service.set_child_runner(_slow_runner)
    try:
        launched = _spawn(service, background=True)

        assert launched["background"] is True
        assert "Do not spawn it again" in launched["note"]
        run_id = launched["run_id"]

        live = T.subagent_status(run_id=run_id, service=service)
        assert live["status"] == "running"
        assert "still working" in live["note"]
    finally:
        gate.set()

    deadline = time.time() + 5
    final = live
    while time.time() < deadline:
        final = T.subagent_status(run_id=run_id, service=service)
        if final["status"] == STATUS_DONE:
            break
        time.sleep(0.02)

    assert final["status"] == STATUS_DONE
    assert "finished" in final["note"]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
